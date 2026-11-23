"""Factory analytics for the Overview dashboard, the 3D machine panel and the chatbot. Read-only.

Every number is ACTUAL (as supplied) or CALCULATED with the formula in `DEFINITIONS`; a metric is None when its data does not exist.
Filters: date range (default: last 7 days of data), line, machine, shift, factory section (process area)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from ..deps import query, query_many

AREAS = ("SMT", "FINAL_ASSEMBLY", "TEST", "QUALITY", "PACKAGING", "WAREHOUSE")
LINES = ("SMT-L1", "SMT-L2", "FAL-L1", "FAL-L2")

DEFINITIONS = [
    {"id": "K-01", "metric": "Total / active / idle / setup / offline machines", "label": "ACTUAL",
     "text": "dim_machine count; state of each machine in the last telemetry hour of the range (RUNNING = active, DOWN = offline)."},
    {"id": "K-02", "metric": "Machines under maintenance", "label": "CALCULATED",
     "text": "machines with a work order IN_PROGRESS whose actual start is within 72 h before the end of the range. Telemetry has no maintenance state."},
    {"id": "K-03", "metric": "Total production / target", "label": "ACTUAL", "text": "sum of MES hourly actual_qty / target_qty (line level)."},
    {"id": "K-04", "metric": "Production efficiency", "label": "CALCULATED", "text": "actual_qty / target_qty (plan attainment)."},
    {"id": "K-05", "metric": "OEE", "label": "CALCULATED",
     "text": "availability x performance x quality per MES hour = good_qty / target_qty; aggregated as sum(good) / sum(target). Performance is not capped."},
    {"id": "K-06", "metric": "Quality pass rate / defects", "label": "CALCULATED",
     "text": "share of inspections with result PASS (inspections, not units); defects = inspections with result FAIL. MES reject_qty is shown separately (units)."},
    {"id": "K-07", "metric": "Downtime", "label": "ACTUAL", "text": "MES line downtime minutes; machine downtime events are reported separately."},
    {"id": "K-08", "metric": "Machine utilisation", "label": "CALCULATED", "text": "share of telemetry hours in state RUNNING."},
    {"id": "K-09", "metric": "Machine health", "label": "CALCULATED", "text": "rule-based risk index from the latest agent run (not a trained model)."},
]


class FilterError(ValueError):
    pass


_STATIC: dict = {}


def _static(key: str, fn, ttl: float = 300.0):
    """Tiny TTL cache for data that only changes on a reload (data bounds, machine registry)."""
    import time
    hit = _STATIC.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _STATIC[key] = (time.time(), val)
    return val


def data_bounds() -> dict:
    return _static("bounds", _data_bounds)


def _data_bounds() -> dict:
    r = query_many({
        "mes": ("SELECT min(event_ts) AS lo, max(event_ts) AS hi FROM twin.fact_hourly_production", {}),
        "tel": ('SELECT min("timestamp") AS lo, max("timestamp") AS hi FROM twin.fact_machine_telemetry_hourly', {}),
    })
    return {"mes": r["mes"][0], "telemetry": r["tel"][0]}


def resolve_filters(date_from: str | None, date_to: str | None, line_id: str | None, machine_id: str | None, shift_id: str | None, area: str | None) -> dict:
    b = data_bounds()
    hi: datetime = b["mes"]["hi"]
    lo: datetime = b["mes"]["lo"]
    try:
        d_to = datetime.combine(date.fromisoformat(date_to), datetime.max.time()).replace(microsecond=0) if date_to else hi
        # default: the last 7 whole days of data (a window starting mid-day would show a near-empty first day)
        d_from = datetime.combine(date.fromisoformat(date_from), datetime.min.time()) if date_from else datetime.combine((d_to - timedelta(days=6)).date(), datetime.min.time())
    except ValueError:
        raise FilterError("dates must be YYYY-MM-DD")
    if d_from > d_to:
        raise FilterError("date_from is after date_to")
    d_to, d_from = min(d_to, hi), max(d_from, lo)
    if line_id and line_id not in LINES:
        raise FilterError("unknown line_id")
    if shift_id and shift_id not in ("A", "B", "C"):
        raise FilterError("shift_id must be A, B or C")
    if area and area not in AREAS:
        raise FilterError("unknown factory section")
    machines = _static("machines", lambda: query("SELECT m.machine_id, m.line_id, mp.zone FROM twin.dim_machine m LEFT JOIN twin.v_machine_3d_mapping mp USING (machine_id)"))
    if machine_id and machine_id not in {m["machine_id"] for m in machines}:
        raise FilterError("unknown machine_id")
    sel = [m for m in machines if (not line_id or m["line_id"] == line_id) and (not area or m["zone"] == area) and (not machine_id or m["machine_id"] == machine_id)]
    lines = [line_id] if line_id else list(LINES)
    if area:     # MES is line level: a section maps to the lines that run it (SMT -> SMT lines, every other section -> FAL lines)
        lines = [l for l in lines if l.startswith("SMT") == (area == "SMT")]
    if machine_id:
        lines = [m["line_id"] for m in machines if m["machine_id"] == machine_id]
    return {"from": d_from, "to": d_to, "line_id": line_id, "machine_id": machine_id, "shift_id": shift_id, "area": area,
            "lines": lines, "machines": [m["machine_id"] for m in sel], "bounds": {"from": lo.isoformat(), "to": hi.isoformat()}}


def _num(v):
    return float(v) if v is not None else None


def overview(f: dict) -> dict:
    p = {"f": f["from"], "t": f["to"], "lines": f["lines"] or ["__none__"], "ms": f["machines"] or ["__none__"], "sh": f["shift_id"]}
    shift = " AND (CAST(:sh AS text) IS NULL OR shift_id = :sh)"
    J = {
        "prod": ("SELECT sum(target_qty) AS target, sum(actual_qty) AS actual, sum(good_qty) AS good, sum(reject_qty) AS reject, "
                 "sum(rework_qty) AS rework, sum(downtime_min) AS downtime, count(*) AS line_hours FROM twin.fact_hourly_production "
                 "WHERE event_ts BETWEEN :f AND :t AND line_id = ANY(:lines)" + shift, p),
        "trend": ("SELECT event_date AS d, sum(target_qty) AS target, sum(actual_qty) AS actual, sum(good_qty) AS good, sum(reject_qty) AS reject, "
                  "sum(downtime_min) AS downtime FROM twin.fact_hourly_production WHERE event_ts BETWEEN :f AND :t AND line_id = ANY(:lines)" + shift +
                  " GROUP BY 1 ORDER BY 1", p),
        "by_line": ("SELECT line_id, sum(target_qty) AS target, sum(actual_qty) AS actual, sum(good_qty) AS good, sum(downtime_min) AS downtime "
                    "FROM twin.fact_hourly_production WHERE event_ts BETWEEN :f AND :t AND line_id = ANY(:lines)" + shift + " GROUP BY 1 ORDER BY 1", p),
        "dt_reason": ("SELECT downtime_reason AS reason, sum(downtime_min) AS minutes, count(*) FILTER (WHERE downtime_min > 0) AS line_hours "
                      "FROM twin.fact_hourly_production WHERE event_ts BETWEEN :f AND :t AND line_id = ANY(:lines) AND downtime_min > 0" + shift +
                      " GROUP BY 1 ORDER BY 2 DESC", p),
        "state": ('SELECT DISTINCT ON (machine_id) machine_id, machine_state, "timestamp" AS ts FROM twin.fact_machine_telemetry_hourly '
                  'WHERE "timestamp" <= :t AND machine_id = ANY(:ms) ORDER BY machine_id, "timestamp" DESC', p),
        "util": ("SELECT machine_id, count(*) AS hours, count(*) FILTER (WHERE machine_state = 'RUNNING') AS running, "
                 "count(*) FILTER (WHERE machine_state = 'DOWN') AS down, count(*) FILTER (WHERE is_anomaly) AS anomalies "
                 'FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" BETWEEN :f AND :t AND machine_id = ANY(:ms)' + shift + " GROUP BY 1 ORDER BY 1", p),
        "maint": ("SELECT count(DISTINCT machine_id) AS n FROM twin.fact_maintenance_work_order WHERE status = 'IN_PROGRESS' "
                  "AND actual_start > CAST(:t AS timestamp) - interval '72 hours' AND actual_start <= :t AND machine_id = ANY(:ms)", p),
        "mevents": ("SELECT reason_code AS reason, count(*) AS events, sum(duration_min) AS minutes FROM twin.fact_downtime_event "
                    "WHERE start_ts BETWEEN :f AND :t AND machine_id = ANY(:ms) GROUP BY 1 ORDER BY 3 DESC", p),
        "quality": ("SELECT count(*) AS n, count(*) FILTER (WHERE result = 'PASS') AS pass, count(*) FILTER (WHERE result = 'FAIL') AS fail "
                    "FROM twin.fact_quality_inspection WHERE inspection_ts BETWEEN :f AND :t AND line_id = ANY(:lines)", p),
        "q_line": ("SELECT line_id, count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fail FROM twin.fact_quality_inspection "
                   "WHERE inspection_ts BETWEEN :f AND :t AND line_id = ANY(:lines) GROUP BY 1", p),
        "q_trend": ("SELECT inspection_ts::date AS d, count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fail FROM twin.fact_quality_inspection "
                    "WHERE inspection_ts BETWEEN :f AND :t AND line_id = ANY(:lines) GROUP BY 1 ORDER BY 1", p),
        "pareto": ("SELECT q.defect_code, d.defect_name, d.defect_category, count(*) AS n FROM twin.fact_quality_inspection q "
                   "LEFT JOIN twin.dim_defect_code d USING (defect_code) WHERE q.result = 'FAIL' AND q.inspection_ts BETWEEN :f AND :t "
                   "AND q.line_id = ANY(:lines) GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 12", p),
        "machines": ("SELECT m.machine_id, m.machine_name, m.machine_type, m.line_id, m.criticality, mp.station_id, mp.zone "
                     "FROM twin.dim_machine m LEFT JOIN twin.v_machine_3d_mapping mp USING (machine_id) WHERE m.machine_id = ANY(:ms) ORDER BY 1", p),
    }
    J["run"] = ("SELECT run_id, status, started_at, finished_at, current_step, records_processed, data_as_of, triggered_by FROM twin.agent_run "
                "ORDER BY started_at DESC LIMIT 1", {})
    J["pend"] = ("SELECT count(*) AS n FROM twin.recommendation WHERE status = 'PENDING_REVIEW'", {})
    J["pending"] = ("SELECT rec_id, machine_id, line_id, category, issue, risk_level, created_at FROM twin.recommendation "
                    "WHERE status = 'PENDING_REVIEW' ORDER BY CASE risk_level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, "
                    "created_at DESC LIMIT 8", {})
    J["health"] = ("SELECT s.machine_id, s.status, s.health_score, s.risk_score, s.run_id FROM twin.agent_machine_state s WHERE s.run_id = "
                   "(SELECT run_id FROM twin.agent_run WHERE status = 'COMPLETED' ORDER BY started_at DESC LIMIT 1) ORDER BY s.risk_score DESC", {})
    J["findings"] = ("SELECT finding_id, rank, category, severity, machine_id, line_id, title, value_label FROM twin.agent_finding WHERE run_id = "
                     "(SELECT run_id FROM twin.agent_run WHERE status = 'COMPLETED' ORDER BY started_at DESC LIMIT 1) ORDER BY rank LIMIT 8", {})
    R = query_many(J, workers=10)
    pr = R["prod"][0]
    target, actual, good = _num(pr["target"]), _num(pr["actual"]), _num(pr["good"])
    states = {r["machine_id"]: r["machine_state"] for r in R["state"]}
    count = lambda s: sum(1 for v in states.values() if v == s)
    q = R["quality"][0]
    run = ({**R["run"][0], "run_id": str(R["run"][0]["run_id"])} if R["run"] else None)
    pend = R["pend"][0]["n"]
    health = _health_from_rows(R["health"], f["machines"])
    pending = [{**r, "rec_id": str(r["rec_id"])} for r in R["pending"]]
    findings = [{**r, "finding_id": str(r["finding_id"])} for r in R["findings"]]
    util = [{"machine_id": r["machine_id"], "utilization_pct": r["running"] / r["hours"] * 100 if r["hours"] else None,
             "down_pct": r["down"] / r["hours"] * 100 if r["hours"] else None, "anomalies": r["anomalies"], "hours": r["hours"]} for r in R["util"]]
    kpis = {
        "total_machines": len(f["machines"]), "active_machines": count("RUNNING"), "idle_machines": count("IDLE"), "setup_machines": count("SETUP"),
        "offline_machines": count("DOWN"), "machines_under_maintenance": int(R["maint"][0]["n"]),
        "total_production": actual, "production_target": target, "good_units": good, "reject_units": _num(pr["reject"]),
        "production_efficiency_pct": actual / target * 100 if target else None,
        "oee_pct": good / target * 100 if target else None,
        "quality_pass_rate_pct": q["pass"] / q["n"] * 100 if q["n"] else None, "inspections": q["n"], "total_defects": q["fail"],
        "downtime_min": _num(pr["downtime"]), "pending_recommendations": pend,
        "agent_status": run["status"] if run else "NEVER_RUN", "agent_last_run": run,
        "machine_state_as_of": max((r["ts"] for r in R["state"]), default=None),
    }
    return {
        "filters": {**{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in f.items()}},
        "kpis": kpis,
        "charts": {
            "production_trend": [{"date": r["d"].isoformat(), "target": _num(r["target"]), "actual": _num(r["actual"]), "good": _num(r["good"]),
                                  "reject": _num(r["reject"]), "downtime_min": _num(r["downtime"])} for r in R["trend"]],
            "line_comparison": [{"line_id": r["line_id"], "target": _num(r["target"]), "actual": _num(r["actual"]), "good": _num(r["good"]),
                                 "efficiency_pct": _num(r["actual"]) / _num(r["target"]) * 100 if r["target"] else None,
                                 "oee_pct": _num(r["good"]) / _num(r["target"]) * 100 if r["target"] else None, "downtime_min": _num(r["downtime"]),
                                 "fail_rate_pct": next((x["fail"] / x["n"] * 100 for x in R["q_line"] if x["line_id"] == r["line_id"] and x["n"]), None)}
                                for r in R["by_line"]],
            "machine_status": [{"state": s, "machines": count(s)} for s in ("RUNNING", "IDLE", "SETUP", "DOWN")],
            "machine_utilization": util,
            "quality_trend": [{"date": r["d"].isoformat(), "inspections": r["n"], "fails": r["fail"], "fail_rate_pct": r["fail"] / r["n"] * 100 if r["n"] else None} for r in R["q_trend"]],
            "defect_pareto": [{"defect_code": r["defect_code"], "defect_name": r["defect_name"], "category": r["defect_category"], "fails": r["n"]} for r in R["pareto"]],
            "downtime_by_reason": [{"reason": r["reason"], "minutes": _num(r["minutes"]), "line_hours": r["line_hours"]} for r in R["dt_reason"]],
            "machine_downtime_events": [{"reason": r["reason"], "events": r["events"], "minutes": _num(r["minutes"])} for r in R["mevents"]],
            "machine_health": health,
            "recent_findings": findings,
            "pending_approvals": pending,
        },
        "machines": R["machines"],
        "definitions": DEFINITIONS,
    }


def latest_run_summary() -> dict | None:
    r = query("SELECT run_id, status, started_at, finished_at, current_step, records_processed, data_as_of, triggered_by FROM twin.agent_run "
              "ORDER BY started_at DESC LIMIT 1")
    if not r:
        return None
    x = r[0]
    return {**x, "run_id": str(x["run_id"])}


def latest_completed_run_id() -> str | None:
    r = query("SELECT run_id FROM twin.agent_run WHERE status = 'COMPLETED' ORDER BY started_at DESC LIMIT 1")
    return str(r[0]["run_id"]) if r else None


def _health_from_rows(rows: list[dict], machine_ids: list[str] | None) -> dict:
    if not rows:
        return {"run_id": None, "counts": {}, "machines": [], "note": "Run the Digital Twin Agent to compute machine health."}
    rid = str(rows[0]["run_id"])
    if machine_ids is not None:
        rows = [r for r in rows if r["machine_id"] in machine_ids]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("NORMAL", "WARNING", "ATTENTION", "CRITICAL", "OFFLINE")}
    return {"run_id": rid, "counts": counts, "value_label": "CALCULATED", "method": "RULE_BASED_INDEX",
            "machines": [{"machine_id": r["machine_id"], "status": r["status"], "health_score": _num(r["health_score"]), "risk_score": _num(r["risk_score"])} for r in rows]}


def machine_health(run_id: str | None, machine_ids: list[str] | None = None) -> dict:
    """Status counts and per-machine health from the latest COMPLETED agent run (None when the agent has never completed)."""
    rid = latest_completed_run_id()
    if not rid:
        return {"run_id": None, "counts": {}, "machines": [], "note": "Run the Digital Twin Agent to compute machine health."}
    rows = query("SELECT machine_id, status, health_score, risk_score FROM twin.agent_machine_state WHERE run_id = CAST(:r AS uuid) ORDER BY risk_score DESC", r=rid)
    if machine_ids is not None:
        rows = [r for r in rows if r["machine_id"] in machine_ids]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("NORMAL", "WARNING", "ATTENTION", "CRITICAL", "OFFLINE")}
    return {"run_id": rid, "counts": counts, "machines": [{**r, "health_score": _num(r["health_score"]), "risk_score": _num(r["risk_score"])} for r in rows],
            "value_label": "CALCULATED", "method": "RULE_BASED_INDEX"}


def machine_states(run_id: str | None = None) -> dict:
    """Machine status for the 3D twin: registry + 3D mapping + latest agent-computed state + open findings/recommendations."""
    rid = run_id or latest_completed_run_id()
    reg = query("SELECT * FROM twin.v_machine_3d_mapping ORDER BY machine_id")
    st = {r["machine_id"]: r for r in query("SELECT machine_id, status, health_score, risk_score, metrics FROM twin.agent_machine_state "
                                             "WHERE run_id = CAST(:r AS uuid)", r=rid)} if rid else {}
    fnd = query("SELECT finding_id, machine_id, severity, category, title, explanation, root_causes FROM twin.agent_finding "
                "WHERE run_id = CAST(:r AS uuid) AND machine_id IS NOT NULL ORDER BY rank", r=rid) if rid else []
    recs = query("SELECT rec_id, machine_id, action, risk_level, status, action_status FROM twin.recommendation WHERE run_id = CAST(:r AS uuid) "
                 "AND machine_id IS NOT NULL", r=rid) if rid else []
    out = []
    for m in reg:
        s = st.get(m["machine_id"])
        out.append({**m, "status": s["status"] if s else None, "health_score": _num(s["health_score"]) if s else None,
                    "risk_score": _num(s["risk_score"]) if s else None, "metrics": s["metrics"] if s else None,
                    "findings": [{**x, "finding_id": str(x["finding_id"])} for x in fnd if x["machine_id"] == m["machine_id"]],
                    "recommendations": [{**x, "rec_id": str(x["rec_id"])} for x in recs if x["machine_id"] == m["machine_id"]]})
    run = query("SELECT run_id, finished_at, data_as_of FROM twin.agent_run WHERE run_id = CAST(:r AS uuid)", r=rid) if rid else []
    return {"run_id": rid, "run": ({**run[0], "run_id": str(run[0]["run_id"])} if run else None), "machines": out}


def machine_history(machine_id: str, days: int = 7) -> dict:
    at = query('SELECT max("timestamp") AS ts FROM twin.fact_machine_telemetry_hourly WHERE machine_id = :m', m=machine_id)[0]["ts"]
    if at is None:
        return {"machine_id": machine_id, "series": [], "work_orders": [], "downtime": []}
    p = {"m": machine_id, "a": at, "d": days}
    R = query_many({
        "series": ('SELECT "timestamp" AS ts, temperature_c, vibration_mm_s, motor_current_a, pressure_bar, rpm, machine_state, alarm_code, is_anomaly '
                   'FROM twin.fact_machine_telemetry_hourly WHERE machine_id = :m AND "timestamp" > CAST(:a AS timestamp) - make_interval(days => :d) '
                   'ORDER BY 1', p),
        "wo": ("SELECT work_order_id, created_ts, maintenance_type, failure_mode, priority, actual_start, status, technician_id "
               "FROM twin.fact_maintenance_work_order WHERE machine_id = :m ORDER BY created_ts DESC LIMIT 12", p),
        "dt": ("SELECT downtime_id, start_ts, duration_min, reason_code, impact_units, closure_status FROM twin.fact_downtime_event "
               "WHERE machine_id = :m ORDER BY start_ts DESC LIMIT 12", p),
        "line_prod": ("SELECT sum(actual_qty) AS actual, sum(target_qty) AS target FROM twin.fact_hourly_production h JOIN twin.dim_machine m "
                      "ON m.line_id = h.line_id WHERE m.machine_id = :m AND h.event_ts > CAST(:a AS timestamp) - make_interval(days => :d)", p),
        "energy": ("SELECT e.area, sum(e.consumption_kwh) AS kwh FROM twin.fact_energy_meter_hourly e JOIN twin.dim_line l ON l.plant_id = e.plant_id "
                   "JOIN twin.dim_machine m ON m.line_id = l.line_id WHERE m.machine_id = :m AND e.\"timestamp\" > CAST(:a AS timestamp) - make_interval(days => :d) "
                   "GROUP BY 1 ORDER BY 2 DESC", p),
    })
    iso = lambda r: {k: (v.isoformat() if isinstance(v, datetime) else (float(v) if hasattr(v, "is_integer") or type(v).__name__ == "Decimal" else v)) for k, v in r.items()}
    lp = R["line_prod"][0]
    return {"machine_id": machine_id, "as_of": at.isoformat(), "days": days, "series": [iso(r) for r in R["series"]],
            "work_orders": [iso(r) for r in R["wo"]], "downtime": [iso(r) for r in R["dt"]],
            "line_production": {"actual": _num(lp["actual"]), "target": _num(lp["target"]), "note": "MES production is per line, not per machine."},
            "plant_energy_by_area": [iso(r) for r in R["energy"]],
            "energy_note": "Energy meters are per plant area; there is no per-machine energy reading in the supplied data."}
