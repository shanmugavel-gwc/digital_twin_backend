"""Manufacturing rules: per-machine condition metrics and the rule-based risk index.

Everything here is CALCULATED from ACTUAL loaded data with the formulas written below. The risk index is a transparent weighted rule set
(weights and caps in agent_config), NOT a trained or validated failure model, and it is labelled that way everywhere it is shown.
'Now' is the latest hour in the telemetry export (the data is historical, not streamed)."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from ..deps import query, query_many

FAILURE_MODE_ACTION = {
    "BEARING_WEAR": "Inspect bearings and run a vibration analysis; replace worn bearings at the next planned stop.",
    "BELT_MISALIGNMENT": "Check belt alignment and tension on the conveyor and drive.",
    "NOZZLE_CLOG": "Clean or replace placement/dispensing nozzles and verify vacuum levels.",
    "MOTOR_OVERHEAT": "Check motor cooling, ventilation and current draw; inspect the drive for overload.",
    "SENSOR_DRIFT": "Calibrate the machine sensors against a reference before trusting readings.",
}
COMPONENT_LABEL = {
    "anomalies": "anomaly flags in telemetry", "sensor_excursions": "sensor readings above the machine's own baseline",
    "down_time": "hours in state DOWN", "downtime_events": "downtime event minutes", "maintenance_due": "preventive maintenance due/overdue",
    "station_quality": "inspection fail rate at the machine's station above the plant average",
}


def anchor() -> datetime:
    return query('SELECT max("timestamp") AS ts FROM twin.fact_machine_telemetry_hourly')[0]["ts"]


def _f(v, d=None):
    return float(v) if v is not None else d


def machine_metrics(cfg: dict, at: datetime | None = None) -> tuple[dict[str, dict], dict]:
    """Per-machine metrics for the analysis window ending at `at` (default: latest data hour). Returns (metrics by machine, context)."""
    at = at or anchor()
    days = int(cfg["analysis_window_days"])
    start = at - timedelta(days=days)
    k = float(cfg["sensor_sigma"])
    p = {"s": start, "a": at}
    J: dict = {}
    J["reg"] = ("SELECT m.machine_id, m.machine_name, m.machine_type, m.line_id, l.plant_id AS line_plant_id, m.plant_id AS listed_plant_id, "
                "m.criticality, m.planned_maintenance_interval_days AS pm_interval_days, mp.station_id, mp.zone, mp.asset_key "
                "FROM twin.dim_machine m JOIN twin.dim_line l USING (line_id) LEFT JOIN twin.v_machine_3d_mapping mp USING (machine_id) ORDER BY 1", {})
    J["win"] = ('SELECT machine_id, count(*) AS hours, count(*) FILTER (WHERE machine_state = \'RUNNING\') AS running_h, '
                "count(*) FILTER (WHERE machine_state = 'DOWN') AS down_h, count(*) FILTER (WHERE machine_state = 'IDLE') AS idle_h, "
                "count(*) FILTER (WHERE machine_state = 'SETUP') AS setup_h, count(*) FILTER (WHERE is_anomaly) AS anomalies, "
                "count(*) FILTER (WHERE alarm_code <> 'NONE') AS alarms, count(*) FILTER (WHERE sensor_health <> 'GOOD') AS sensor_faults, "
                "avg(temperature_c) AS temp_avg, max(temperature_c) AS temp_max, avg(vibration_mm_s) AS vib_avg, max(vibration_mm_s) AS vib_max, "
                "avg(motor_current_a) AS cur_avg, regr_slope(temperature_c, extract(epoch FROM \"timestamp\") / 86400.0) AS temp_slope_per_day, "
                "regr_slope(vibration_mm_s, extract(epoch FROM \"timestamp\") / 86400.0) AS vib_slope_per_day "
                'FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" > :s AND "timestamp" <= :a GROUP BY 1', p)
    J["base"] = ('SELECT machine_id, avg(temperature_c) AS t_mu, stddev_samp(temperature_c) AS t_sd, avg(vibration_mm_s) AS v_mu, '
                 'stddev_samp(vibration_mm_s) AS v_sd, avg(motor_current_a) AS c_mu, stddev_samp(motor_current_a) AS c_sd, count(*) AS n '
                 'FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" <= :s GROUP BY 1', p)
    J["exc"] = ('WITH b AS (SELECT machine_id, avg(temperature_c) t_mu, stddev_samp(temperature_c) t_sd, avg(vibration_mm_s) v_mu, '
                'stddev_samp(vibration_mm_s) v_sd, avg(motor_current_a) c_mu, stddev_samp(motor_current_a) c_sd '
                'FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" <= :s GROUP BY 1) '
                'SELECT t.machine_id, count(*) FILTER (WHERE t.temperature_c > b.t_mu + :k * b.t_sd) AS temp_exc, '
                'count(*) FILTER (WHERE t.vibration_mm_s > b.v_mu + :k * b.v_sd) AS vib_exc, '
                'count(*) FILTER (WHERE t.motor_current_a > b.c_mu + :k * b.c_sd) AS cur_exc '
                'FROM twin.fact_machine_telemetry_hourly t JOIN b USING (machine_id) WHERE t."timestamp" > :s AND t."timestamp" <= :a GROUP BY 1', {"k": k, **p})
    J["last"] = ('SELECT DISTINCT ON (machine_id) machine_id, "timestamp" AS ts, machine_state, alarm_code, sensor_health, is_anomaly, '
                 'temperature_c, vibration_mm_s, motor_current_a, pressure_bar, rpm FROM twin.fact_machine_telemetry_hourly '
                 'WHERE "timestamp" <= :a ORDER BY machine_id, "timestamp" DESC', {"a": at})
    J["dt"] = ("SELECT machine_id, count(*) AS events, sum(duration_min) AS minutes, sum(impact_units) AS impact_units, "
               "count(*) FILTER (WHERE closure_status = 'OPEN') AS open_events, "
               "mode() WITHIN GROUP (ORDER BY reason_code) FILTER (WHERE reason_code <> 'NONE') AS top_reason "
               "FROM twin.fact_downtime_event WHERE start_ts > :s AND start_ts <= :a GROUP BY 1", p)
    J["dt_reasons"] = ("SELECT machine_id, reason_code, count(*) AS n, sum(duration_min) AS minutes FROM twin.fact_downtime_event "
                       "WHERE start_ts > :s AND start_ts <= :a GROUP BY 1, 2", p)
    J["wo"] = ("SELECT machine_id, max(actual_start) FILTER (WHERE status = 'COMPLETED') AS last_maint, "
               "max(actual_start) FILTER (WHERE status = 'COMPLETED' AND maintenance_type = 'PREVENTIVE') AS last_pm, "
               "count(*) FILTER (WHERE status IN ('PLANNED', 'IN_PROGRESS') AND created_ts <= :a) AS open_wo, "
               "min(created_ts) FILTER (WHERE status IN ('PLANNED', 'IN_PROGRESS') AND created_ts <= :a) AS oldest_open_wo, "
               "count(*) FILTER (WHERE status = 'IN_PROGRESS' AND actual_start > :a - interval '72 hours' AND actual_start <= :a) AS wip_recent "
               "FROM twin.fact_maintenance_work_order WHERE created_ts <= :a GROUP BY 1", {"a": at})
    J["fm"] = ("SELECT machine_id, failure_mode, count(*) AS n FROM twin.fact_maintenance_work_order "
               "WHERE created_ts > :a - interval '90 days' AND created_ts <= :a AND maintenance_type = 'CORRECTIVE' AND failure_mode IS NOT NULL GROUP BY 1, 2", {"a": at})
    J["qs"] = ("SELECT line_id, station_id, count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fails FROM twin.fact_quality_inspection "
               "WHERE inspection_ts > :s AND inspection_ts <= :a GROUP BY 1, 2", p)
    J["q_all"] = ("SELECT count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fails FROM twin.fact_quality_inspection "
                  "WHERE inspection_ts > :s AND inspection_ts <= :a", p)

    R = query_many(J)
    reg, win, base, exc, last, dt, dt_reasons, wo, fm, qs = (R[k] for k in ("reg", "win", "base", "exc", "last", "dt", "dt_reasons", "wo", "fm", "qs"))
    q_all = R["q_all"][0]
    by = lambda rows: {r["machine_id"]: r for r in rows}
    W, B, E, L, D, O = by(win), by(base), by(exc), by(last), by(dt), by(wo)
    fleet_fail = (q_all["fails"] / q_all["n"]) if q_all["n"] else None
    q_by = {(r["line_id"], r["station_id"]): r for r in qs}
    q_station: dict[str, list] = {}
    for r in qs:
        q_station.setdefault(r["station_id"], []).append(r)
    out: dict[str, dict] = {}
    for m in reg:
        mid = m["machine_id"]
        w, b, e, lt, d, o = W.get(mid, {}), B.get(mid, {}), E.get(mid, {}), L.get(mid, {}), D.get(mid, {}), O.get(mid, {})
        hours = int(w.get("hours") or 0)
        qrow = q_by.get((m["line_id"], m["station_id"]))
        if qrow is None and m["station_id"] in q_station:
            rows = q_station[m["station_id"]]
            qrow = {"n": sum(r["n"] for r in rows), "fails": sum(r["fails"] for r in rows)}
        st_fail = (qrow["fails"] / qrow["n"]) if qrow and qrow["n"] else None
        last_pm = o.get("last_pm")
        days_since_pm = (at - last_pm).total_seconds() / 86400 if last_pm else None
        z = lambda val, mu, sd: (float(val) - float(mu)) / float(sd) if val is not None and mu is not None and sd else None
        out[mid] = {
            **{k: m[k] for k in ("machine_id", "machine_name", "machine_type", "line_id", "line_plant_id", "listed_plant_id", "criticality",
                                 "pm_interval_days", "station_id", "zone", "asset_key")},
            "window": {"start": start.isoformat(), "end": at.isoformat(), "days": days, "hours": hours},
            "latest": {k: (v.isoformat() if isinstance(v, datetime) else (float(v) if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) else v))
                       for k, v in lt.items() if k != "machine_id"} if lt else None,
            "latest_z": {"temperature": z(lt.get("temperature_c"), b.get("t_mu"), b.get("t_sd")), "vibration": z(lt.get("vibration_mm_s"), b.get("v_mu"), b.get("v_sd")),
                         "motor_current": z(lt.get("motor_current_a"), b.get("c_mu"), b.get("c_sd"))},
            "baseline": {"temp_mean": _f(b.get("t_mu")), "temp_std": _f(b.get("t_sd")), "vib_mean": _f(b.get("v_mu")), "vib_std": _f(b.get("v_sd")),
                         "current_mean": _f(b.get("c_mu")), "current_std": _f(b.get("c_sd")), "hours": int(b.get("n") or 0)},
            "utilization_pct": (int(w["running_h"]) / hours * 100) if hours else None,
            "state_hours": {"RUNNING": int(w.get("running_h") or 0), "IDLE": int(w.get("idle_h") or 0), "SETUP": int(w.get("setup_h") or 0), "DOWN": int(w.get("down_h") or 0)},
            "anomalies": int(w.get("anomalies") or 0), "alarms": int(w.get("alarms") or 0), "sensor_faults": int(w.get("sensor_faults") or 0),
            "sensor_excursions": {"temperature": int(e.get("temp_exc") or 0), "vibration": int(e.get("vib_exc") or 0), "motor_current": int(e.get("cur_exc") or 0)},
            "temp_avg": _f(w.get("temp_avg")), "temp_max": _f(w.get("temp_max")), "vib_avg": _f(w.get("vib_avg")), "vib_max": _f(w.get("vib_max")),
            "temp_slope_per_day": _f(w.get("temp_slope_per_day")), "vib_slope_per_day": _f(w.get("vib_slope_per_day")),
            "downtime": {"events": int(d.get("events") or 0), "minutes": _f(d.get("minutes"), 0.0), "impact_units": _f(d.get("impact_units"), 0.0),
                         "open_events": int(d.get("open_events") or 0), "top_reason": d.get("top_reason"),
                         "by_reason": sorted([{"reason": r["reason_code"], "events": r["n"], "minutes": float(r["minutes"] or 0)} for r in dt_reasons if r["machine_id"] == mid],
                                             key=lambda x: -x["minutes"])},
            "maintenance": {"last_maintenance": o["last_maint"].isoformat() if o.get("last_maint") else None, "last_pm": last_pm.isoformat() if last_pm else None,
                            "days_since_pm": days_since_pm, "pm_overdue": bool(days_since_pm is not None and days_since_pm > (m["pm_interval_days"] or 30)),
                            "open_work_orders": int(o.get("open_wo") or 0), "oldest_open_wo": o["oldest_open_wo"].isoformat() if o.get("oldest_open_wo") else None,
                            "in_progress_last_72h": int(o.get("wip_recent") or 0),
                            "corrective_failure_modes_90d": sorted([{"failure_mode": r["failure_mode"], "n": r["n"]} for r in fm if r["machine_id"] == mid], key=lambda x: -x["n"])},
            "station_quality": {"inspections": int(qrow["n"]) if qrow else 0, "fails": int(qrow["fails"]) if qrow else 0,
                                "fail_rate_pct": st_fail * 100 if st_fail is not None else None, "plant_fail_rate_pct": fleet_fail * 100 if fleet_fail is not None else None},
        }
    ctx = {"anchor": at, "start": start, "fleet_fail_rate": fleet_fail, "telemetry_rows_window": sum(int(r["hours"]) for r in win)}
    return out, ctx


def score(m: dict, cfg: dict) -> dict:
    """Rule-based risk index 0..100 = sum(weight * component score in 0..1); health = 100 - risk. Returns components for explainability."""
    caps, wts = cfg["risk_caps"], cfg["risk_weights"]
    exc = sum(m["sensor_excursions"].values())
    q = m["station_quality"]
    q_score = 0.0
    if q["fail_rate_pct"] is not None and q["plant_fail_rate_pct"]:
        q_score = min(1.0, max(0.0, (q["fail_rate_pct"] - q["plant_fail_rate_pct"]) / q["plant_fail_rate_pct"]))
    mt = m["maintenance"]
    if mt["days_since_pm"] is None:
        pm_score = 0.5          # no completed preventive maintenance on record: partly counted, stated in the explanation
    else:
        pm_score = 1.0 if mt["pm_overdue"] else min(1.0, mt["days_since_pm"] / (m["pm_interval_days"] or 30)) * 0.5
    comps = {
        "anomalies": min(1.0, m["anomalies"] / caps["anomalies"]),
        "sensor_excursions": min(1.0, exc / caps["sensor_excursions"]),
        "down_time": min(1.0, m["state_hours"]["DOWN"] / caps["down_hours"]),
        "downtime_events": min(1.0, (m["downtime"]["minutes"] or 0) / caps["downtime_event_minutes"]),
        "maintenance_due": pm_score,
        "station_quality": q_score,
    }
    parts = {k: round(comps[k] * float(wts[k]), 2) for k in comps}
    risk = round(sum(parts.values()), 1)
    latest_state = (m.get("latest") or {}).get("machine_state")
    t = cfg["status_thresholds"]
    if latest_state == "DOWN":
        status = "OFFLINE"
    elif risk >= t["CRITICAL"]:
        status = "CRITICAL"
    elif risk >= t["ATTENTION"]:
        status = "ATTENTION"
    elif risk >= t["WARNING"]:
        status = "WARNING"
    else:
        status = "NORMAL"
    top = sorted(parts.items(), key=lambda kv: -kv[1])
    return {"risk_score": risk, "health_score": round(100 - risk, 1), "status": status, "components": parts, "component_scores": comps,
            "drivers": [k for k, v in top if v > 0][:3], "method": "RULE_BASED_INDEX", "value_label": "CALCULATED"}


def explain(m: dict, s: dict) -> str:
    """Plain-language explanation built only from the metrics (no invented values)."""
    bits = []
    for k in s["drivers"]:
        if k == "anomalies":
            bits.append(f"{m['anomalies']} hourly readings flagged as anomalies in the last {m['window']['days']} days")
        elif k == "sensor_excursions":
            ex = m["sensor_excursions"]
            bits.append(f"sensor readings above its own baseline + k*sigma for {ex['temperature']} h (temperature), {ex['vibration']} h (vibration), {ex['motor_current']} h (motor current)")
        elif k == "down_time":
            bits.append(f"{m['state_hours']['DOWN']} h in state DOWN")
        elif k == "downtime_events":
            d = m["downtime"]
            bits.append(f"{d['events']} downtime events totalling {d['minutes']:.0f} min (most frequent reason {d['top_reason'] or 'n/a'})")
        elif k == "maintenance_due":
            mt = m["maintenance"]
            bits.append("no completed preventive maintenance on record" if mt["days_since_pm"] is None else
                        f"last completed preventive maintenance {mt['days_since_pm']:.0f} days ago (interval {m['pm_interval_days']} days)"
                        + (" - overdue" if mt["pm_overdue"] else ""))
        elif k == "station_quality":
            q = m["station_quality"]
            bits.append(f"inspection fail rate {q['fail_rate_pct']:.1f}% at {m['station_id']} vs {q['plant_fail_rate_pct']:.1f}% overall")
    state = (m.get("latest") or {}).get("machine_state", "n/a")
    head = f"{m['machine_id']} ({m['machine_name']}, {m['line_id']}) latest state {state}; risk index {s['risk_score']:.0f}/100 (rule-based)."
    return head + (" Main drivers: " + "; ".join(bits) + "." if bits else " No rule is triggered.")


def root_causes(m: dict) -> list[dict]:
    """Possible causes ranked from RECORDED history only (corrective failure modes, downtime reasons). Not inferred."""
    out = []
    for f in m["maintenance"]["corrective_failure_modes_90d"][:3]:
        out.append({"cause": f["failure_mode"], "basis": f"{f['n']} corrective work orders with this failure mode in the last 90 days", "source": "CMMS (actual)"})
    for r in [x for x in m["downtime"]["by_reason"] if x["reason"] != "NONE"][:2]:
        out.append({"cause": r["reason"], "basis": f"{r['events']} downtime events, {r['minutes']:.0f} min in the analysis window", "source": "MES/CMMS downtime events (actual)"})
    if m["sensor_excursions"]["temperature"] and m.get("temp_slope_per_day") and m["temp_slope_per_day"] > 0:
        out.append({"cause": "Rising temperature trend", "basis": f"+{m['temp_slope_per_day']:.2f} C/day linear trend in the window", "source": "telemetry (calculated)"})
    return out


def action_for(m: dict, s: dict) -> str:
    fm = m["maintenance"]["corrective_failure_modes_90d"]
    acts = []
    if "maintenance_due" in s["drivers"] and m["maintenance"]["pm_overdue"]:
        acts.append(f"Schedule preventive maintenance for {m['machine_id']} (interval {m['pm_interval_days']} days exceeded).")
    if fm:
        acts.append(FAILURE_MODE_ACTION.get(fm[0]["failure_mode"], f"Investigate recurring {fm[0]['failure_mode']}."))
    if "sensor_excursions" in s["drivers"] or "anomalies" in s["drivers"]:
        acts.append("Review the flagged telemetry hours with maintenance engineering before the next shift.")
    if "station_quality" in s["drivers"]:
        acts.append(f"Ask quality engineering to check inspections at {m['station_id']} against this machine's condition.")
    if not acts:
        acts.append(f"Inspect {m['machine_id']} and confirm the cause of the downtime pattern.")
    return " ".join(acts)
