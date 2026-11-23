"""The ONE central Manufacturing Digital Twin Agent: a configurable 10-step workflow run in a background thread.

Every step does real work against the loaded data and writes its progress to twin.agent_run_event as it happens; the UI streams those
rows over Server-Sent Events (no frontend timers). The agent can only read, calculate, explain, recommend and prepare messages:
recommendations are created PENDING_REVIEW and only a human planner can approve them (enforced by a DB trigger). There is no tool or
code path to any PLC/SCADA or other plant control system."""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

from ..deps import get_engine, query, query_many
from ..twin.scenarios import audit
from . import agent_config, reports, rules

log = logging.getLogger("twin.agent")

STEPS = [
    (1, "Agent Initialization", ["Initialize the central agent", "Load agent configuration", "Verify database connectivity"]),
    (2, "Factory Context Retrieval", ["Machine records", "Production line information", "Sensor records", "Quality records", "Maintenance history"]),
    (3, "Data Validation", ["Required fields", "Missing records", "Timestamps", "Machine relationships"]),
    (4, "Data Preparation", ["Prepare datasets", "Apply manufacturing rules", "Calculate operational metrics", "Generate features"]),
    (5, "Digital Twin Synchronization", ["Map records to 3D machines", "Update machine status", "Synchronize production information"]),
    (6, "Intelligent Analysis", ["Machine conditions", "Production performance", "Abnormalities", "Root causes", "Supported prediction models"]),
    (7, "Agent Reasoning", ["Rank issues", "Explain findings", "Evaluate evidence", "Generate recommendations"]),
    (8, "Report Generation", ["Save findings", "Save recommendations", "Generate reports", "Update dashboard"]),
    (9, "Notifications", ["Prepare email", "Prepare Microsoft Teams message", "Notify via configured integrations"]),
    (10, "Human Approval", ["Submit recommendations for review", "Wait for an authorized decision"]),
]
SEV_ORDER = {"CRITICAL": 0, "OFFLINE": 1, "ATTENTION": 2, "WARNING": 3, "NORMAL": 4}
RISK_LEVEL = {"CRITICAL": "CRITICAL", "OFFLINE": "HIGH", "ATTENTION": "HIGH", "WARNING": "MEDIUM", "NORMAL": "LOW"}
STALE_RUN_MIN = 15
_LOCK = threading.Lock()


class RunConflict(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------- event sink
class Sink:
    """Writes events on a dedicated autocommit connection so each row is visible to the SSE stream immediately."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.records = 0
        self._c = None

    def _conn(self):
        if self._c is None or self._c.closed:
            self._c = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
        return self._c

    def _exec(self, sql: str, **p):
        for attempt in range(2):
            try:
                return self._conn().execute(text(sql), p)
            except Exception:
                try:
                    self._c.close()
                except Exception:
                    pass
                self._c = None
                if attempt:
                    raise

    def emit(self, step: int, kind: str, message: str, status: str | None = None, data: dict | None = None):
        self._exec("INSERT INTO twin.agent_run_event (run_id, step, kind, status, message, data) VALUES (CAST(:r AS uuid), :s, :k, :st, :m, CAST(:d AS jsonb))",
                   r=self.run_id, s=step, k=kind, st=status, m=message[:2000], d=json.dumps(data, default=str) if data is not None else None)
        if kind == "STEP":
            self._exec("UPDATE twin.agent_run SET current_step = :s, records_processed = :n WHERE run_id = CAST(:r AS uuid)", s=step, n=self.records, r=self.run_id)

    def close(self):
        try:
            if self._c is not None:
                self._c.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------------------------- public API
def start(actor: str) -> str:
    """Create a run row and start the workflow in a background thread. 409 if another run is in progress."""
    with _LOCK:
        with get_engine().begin() as c:
            c.execute(text("UPDATE twin.agent_run SET status = 'FAILED', error = 'interrupted (backend restarted or stalled)', finished_at = now() "
                           "WHERE status = 'RUNNING' AND started_at < now() - make_interval(mins => :m)"), dict(m=STALE_RUN_MIN))
            busy = c.execute(text("SELECT run_id FROM twin.agent_run WHERE status = 'RUNNING' LIMIT 1")).scalar()
            if busy:
                raise RunConflict(f"run {busy} is still in progress")
            cfg = agent_config.load()
            rid = str(c.execute(text("INSERT INTO twin.agent_run (agent_name, triggered_by, config) VALUES (:n, :a, CAST(:c AS jsonb)) RETURNING run_id"),
                                dict(n=cfg["agent_name"], a=actor, c=json.dumps(cfg))).scalar_one())
            audit(c, actor, "AGENT_RUN_STARTED", "agent_run", rid, {"config": {k: cfg[k] for k in ("analysis_window_days", "sensor_sigma", "status_thresholds")}})
            for n, name, subs in STEPS:
                c.execute(text("INSERT INTO twin.agent_run_event (run_id, step, kind, status, message, data) VALUES (CAST(:r AS uuid), :s, 'STEP', 'PENDING', :m, CAST(:d AS jsonb))"),
                          dict(r=rid, s=n, m=name, d=json.dumps({"substeps": subs})))
    threading.Thread(target=_run_safe, args=(rid, cfg, actor), daemon=True, name=f"agent-run-{rid[:8]}").start()
    return rid


def _run_safe(rid: str, cfg: dict, actor: str):
    sink = Sink(rid)
    step = 0
    try:
        for step, fn in enumerate(_PIPELINE, 1):
            name = STEPS[step - 1][1]
            if not _enabled(cfg, step):
                sink.emit(step, "STEP", f"{name}: skipped by configuration", "SKIPPED")
                continue
            t0 = time.perf_counter()
            sink.emit(step, "STEP", name, "RUNNING")
            out = fn(sink, cfg, actor) or {}
            sink.emit(step, "STEP", name, "COMPLETED", {"duration_ms": round((time.perf_counter() - t0) * 1000), **out})
        summary = _CTX.pop(rid, {}).get("summary", {})
        with get_engine().begin() as c:
            c.execute(text("UPDATE twin.agent_run SET status = 'COMPLETED', finished_at = now(), current_step = 10, records_processed = :n, summary = CAST(:s AS jsonb) "
                           "WHERE run_id = CAST(:r AS uuid)"), dict(n=sink.records, s=json.dumps(summary, default=str), r=rid))
            audit(c, actor, "AGENT_RUN_COMPLETED", "agent_run", rid, {"records_processed": sink.records, **{k: summary.get(k) for k in ("findings", "recommendations", "reports")}})
    except Exception as e:
        log.exception("agent run failed")
        msg = f"{type(e).__name__}: {str(e)[:400]}"
        try:
            sink.emit(step or 1, "ERROR", msg, data={"trace": traceback.format_exc()[-1500:]})
            sink.emit(step or 1, "STEP", STEPS[(step or 1) - 1][1], "FAILED")
            for n, name, _ in STEPS[step:]:
                sink.emit(n, "STEP", f"{name}: not run (an earlier step failed)", "SKIPPED")
            with get_engine().begin() as c:
                c.execute(text("UPDATE twin.agent_run SET status = 'FAILED', error = :e, finished_at = now(), records_processed = :n WHERE run_id = CAST(:r AS uuid)"),
                          dict(e=msg, n=sink.records, r=rid))
                audit(c, actor, "AGENT_RUN_FAILED", "agent_run", rid, {"error": msg})
        except Exception:
            log.exception("could not record the failure")
        _CTX.pop(rid, None)
    finally:
        sink.close()


def _enabled(cfg: dict, step: int) -> bool:
    return not (step == 9 and not cfg["steps"].get("notifications", True))


# per-run working memory (in-process, cleared at the end of the run)
_CTX: dict[str, dict] = {}


def _ctx(sink: Sink) -> dict:
    return _CTX.setdefault(sink.run_id, {})


# ---------------------------------------------------------------------------------------------------------------- steps
def s1_init(sink: Sink, cfg: dict, actor: str):
    sink.emit(1, "LOG", f"Central agent '{cfg['agent_name']}' initialised for run {sink.run_id} (triggered by {actor}). One agent, internal capabilities, no sub-agents.")
    sink.emit(1, "LOG", f"Configuration loaded: analysis window {cfg['analysis_window_days']} days, sensor threshold {cfg['sensor_sigma']} sigma, "
                        f"status bands {cfg['status_thresholds']}, max {cfg['max_recommendations']} recommendations.", data={"config": cfg})
    t0 = time.perf_counter()
    v = query("SELECT version() AS v, now() AS now")[0]
    sink.emit(1, "LOG", f"Database reachable in {round((time.perf_counter() - t0) * 1000)} ms ({v['v'].split(' on ')[0]}).")
    from ..config import settings
    sink.emit(1, "LOG", "LLM for narrative: " + ("Domo AI configured" if settings.domo_token.get_secret_value() and cfg["llm_narrative"] else "not used (deterministic text only)"))
    sink.emit(1, "LOG", "Safety: the agent has no write path to PLC/SCADA; recommendations need human approval.")
    return {"records": 0}


def s2_context(sink: Sink, cfg: dict, actor: str):
    at = rules.anchor()
    start = at - timedelta(days=int(cfg["analysis_window_days"]))
    c = _ctx(sink)
    c.update(anchor=at, start=start)
    p = {"s": start, "a": at}
    R = query_many({
        "machines": ("SELECT count(*) AS n FROM twin.dim_machine", {}),
        "lines": ("SELECT count(*) AS n FROM twin.dim_line", {}),
        "stations": ("SELECT count(*) AS n FROM twin.dim_station", {}),
        "telemetry": ('SELECT count(*) AS n, count(DISTINCT machine_id) AS m FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" > :s AND "timestamp" <= :a', p),
        "telemetry_all": ("SELECT count(*) AS n FROM twin.fact_machine_telemetry_hourly", {}),
        "production": ("SELECT count(*) AS n FROM twin.fact_hourly_production WHERE event_ts > :s AND event_ts <= :a", p),
        "quality": ("SELECT count(*) AS n FROM twin.fact_quality_inspection WHERE inspection_ts > :s AND inspection_ts <= :a", p),
        "downtime": ("SELECT count(*) AS n FROM twin.fact_downtime_event WHERE start_ts > :s AND start_ts <= :a", p),
        "work_orders": ("SELECT count(*) AS n FROM twin.fact_maintenance_work_order", {}),
        "ncr": ("SELECT count(*) AS n FROM twin.fact_ncr_capa", {}),
    })
    n = {k: int(v[0]["n"]) for k, v in R.items()}
    sink.emit(2, "LOG", f"Machine records: {n['machines']} machines on {n['lines']} lines, {n['stations']} stations (dim_machine, dim_line, dim_station).")
    sink.emit(2, "LOG", f"Data anchored on the latest telemetry hour {at:%Y-%m-%d %H:%M} (historical export, not a live stream). Window {start:%Y-%m-%d %H:%M} to {at:%Y-%m-%d %H:%M}.")
    sink.emit(2, "LOG", f"Sensor records: {n['telemetry']:,} telemetry hours in the window for {R['telemetry'][0]['m']} machines ({n['telemetry_all']:,} in total, used as baseline).")
    sink.emit(2, "LOG", f"Production records: {n['production']:,} MES line-hours. Quality records: {n['quality']:,} inspections, {n['ncr']} NCRs.")
    sink.emit(2, "LOG", f"Maintenance history: {n['work_orders']} work orders, {n['downtime']} downtime events in the window.")
    sink.records += n["telemetry_all"] + n["production"] + n["quality"] + n["downtime"] + n["work_orders"] + n["ncr"] + n["machines"] + n["stations"]
    c["counts"] = n
    return {"records": sink.records, "counts": n, "data_as_of": at.isoformat()}


def s3_validate(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    p = {"s": c["start"], "a": c["anchor"]}
    hours = int((c["anchor"] - c["start"]).total_seconds() // 3600)
    R = query_many({
        "nulls": ('SELECT count(*) FILTER (WHERE temperature_c IS NULL OR vibration_mm_s IS NULL OR motor_current_a IS NULL OR machine_state IS NULL) AS n '
                  'FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" > :s AND "timestamp" <= :a', p),
        "gaps": ('SELECT m.machine_id, count(t.telemetry_id) AS n FROM twin.dim_machine m LEFT JOIN twin.fact_machine_telemetry_hourly t '
                 'ON t.machine_id = m.machine_id AND t."timestamp" > :s AND t."timestamp" <= :a GROUP BY 1', p),
        "dups": ('SELECT count(*) AS n FROM (SELECT machine_id, "timestamp" FROM twin.fact_machine_telemetry_hourly GROUP BY 1, 2 HAVING count(*) > 1) d', {}),
        "future": ('SELECT count(*) AS n FROM twin.fact_machine_telemetry_hourly WHERE "timestamp" > now() AT TIME ZONE \'Asia/Kolkata\'', {}),
        "orphan_t": ("SELECT count(*) AS n FROM twin.fact_machine_telemetry_hourly t LEFT JOIN twin.dim_machine m USING (machine_id) WHERE m.machine_id IS NULL", {}),
        "orphan_d": ("SELECT count(*) AS n FROM twin.fact_downtime_event t LEFT JOIN twin.dim_machine m USING (machine_id) WHERE m.machine_id IS NULL", {}),
        "orphan_w": ("SELECT count(*) AS n FROM twin.fact_maintenance_work_order t LEFT JOIN twin.dim_machine m USING (machine_id) WHERE m.machine_id IS NULL", {}),
        "dt_plant": ("SELECT count(*) AS n FROM twin.fact_downtime_event t JOIN twin.dim_machine m USING (machine_id) WHERE t.plant_id <> m.plant_id", {}),
        "m_plant": ("SELECT m.machine_id, m.plant_id AS listed, l.plant_id AS line_plant FROM twin.dim_machine m JOIN twin.dim_line l USING (line_id) WHERE m.plant_id <> l.plant_id", {}),
        "unmapped": ("SELECT count(*) AS n FROM twin.v_machine_3d_mapping WHERE station_id IS NULL", {}),
        "wo_null": ("SELECT count(*) AS n FROM twin.fact_maintenance_work_order WHERE failure_mode IS NULL", {}),
    })
    issues = []
    n = lambda k: int(R[k][0]["n"])
    sink.emit(3, "LOG", f"Required fields: {n('nulls')} telemetry rows with a missing temperature, vibration, current or state in the window.")
    short = [r for r in R["gaps"] if r["n"] < hours]
    sink.emit(3, "WARN" if short else "LOG", f"Missing records: {len(short)} machines with fewer than {hours} hourly readings in the window"
              + (": " + ", ".join(f"{r['machine_id']} ({r['n']})" for r in short) if short else "."))
    sink.emit(3, "LOG", f"Timestamps: {n('dups')} duplicate (machine, hour) rows, {n('future')} readings in the future.")
    sink.emit(3, "LOG", f"Machine relationships: {n('orphan_t') + n('orphan_d') + n('orphan_w')} telemetry/downtime/work-order rows reference an unknown machine; "
                        f"{n('unmapped')} machines without a 3D station mapping.")
    if n("dt_plant"):
        issues.append({"check": "downtime plant_id differs from the machine's plant", "rows": n("dt_plant")})
        sink.emit(3, "WARN", f"{n('dt_plant')} downtime events carry a plant_id that differs from their machine's plant. Reported, not corrected (docs/DATA_QUALITY.md).")
    for r in R["m_plant"]:
        issues.append({"check": "machine plant differs from its line's plant", "machine_id": r["machine_id"], "listed": r["listed"], "line_plant": r["line_plant"]})
        sink.emit(3, "WARN", f"{r['machine_id']} is listed under {r['listed']} but its line belongs to {r['line_plant']}. Kept as supplied; shown with its line.")
    if n("wo_null"):
        sink.emit(3, "WARN", f"{n('wo_null')} work orders have no failure mode; they are excluded from root-cause ranking.")
        issues.append({"check": "work orders without failure_mode", "rows": n("wo_null")})
    c["dq_issues"] = issues
    return {"warnings": len(issues) + (1 if short else 0), "issues": issues}


def s4_prepare(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    sink.emit(4, "LOG", "Applying manufacturing rules: utilisation = RUNNING share; sensor excursion = reading > own baseline mean + "
                        f"{cfg['sensor_sigma']} sigma; PM overdue = days since last completed preventive WO > planned interval; "
                        "station quality = inspection fail rate at the machine's station vs plant average.")
    M, mctx = rules.machine_metrics(cfg, c["anchor"])
    c["machines"] = M
    sink.emit(4, "LOG", f"Machine features prepared for {len(M)} machines from {mctx['telemetry_rows_window']:,} telemetry hours (baseline = all earlier hours of the same machine).")
    p = {"s": c["start"], "a": c["anchor"], "ps": c["start"] - (c["anchor"] - c["start"])}
    R = query_many({
        "prod": ("SELECT line_id, sum(target_qty) AS target, sum(actual_qty) AS actual, sum(good_qty) AS good, sum(downtime_min) AS downtime_min, count(*) AS hours "
                 "FROM twin.fact_hourly_production WHERE event_ts > :s AND event_ts <= :a GROUP BY 1", p),
        "prev": ("SELECT line_id, sum(actual_qty)::numeric / NULLIF(sum(target_qty), 0) * 100 AS att FROM twin.fact_hourly_production "
                 "WHERE event_ts > :ps AND event_ts <= :s GROUP BY 1", p),
        "reason": ("SELECT line_id, downtime_reason, sum(downtime_min) AS minutes FROM twin.fact_hourly_production WHERE event_ts > :s AND event_ts <= :a "
                   "AND downtime_min > 0 GROUP BY 1, 2 ORDER BY 3 DESC", p),
        "q": ("SELECT line_id, count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fails FROM twin.fact_quality_inspection "
              "WHERE inspection_ts > :s AND inspection_ts <= :a GROUP BY 1", p),
        "defects": ("SELECT q.line_id, q.defect_code, d.defect_name, q.station_id, count(*) AS fails FROM twin.fact_quality_inspection q "
                    "LEFT JOIN twin.dim_defect_code d USING (defect_code) WHERE q.result = 'FAIL' AND q.inspection_ts > :s AND q.inspection_ts <= :a "
                    "GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC", p),
        "ncr": ("SELECT q.line_id, n.root_cause_category, count(*) AS n FROM twin.fact_ncr_capa n JOIN twin.fact_quality_inspection q USING (inspection_id) "
                "WHERE n.opened_ts > CAST(:a AS timestamp) - interval '90 days' AND n.opened_ts <= :a GROUP BY 1, 2 ORDER BY 3 DESC", p),
        "ncr_open": ("SELECT count(*) AS n FROM twin.fact_ncr_capa WHERE capa_status <> 'CLOSED' AND opened_ts <= :a", p),
        "backlog": ("SELECT machine_id, count(*) AS n, min(created_ts) AS oldest FROM twin.fact_maintenance_work_order WHERE status IN ('PLANNED', 'IN_PROGRESS') "
                    "AND created_ts < CAST(:a AS timestamp) - interval '30 days' GROUP BY 1", p),
    })
    lines = {}
    for r in R["prod"]:
        lid = r["line_id"]
        t, a, g = float(r["target"]), float(r["actual"]), float(r["good"])
        q = next((x for x in R["q"] if x["line_id"] == lid), {"n": 0, "fails": 0})
        reasons = [x for x in R["reason"] if x["line_id"] == lid and x["downtime_reason"] != "NONE"]
        lines[lid] = {"target": t, "actual": a, "good": g, "downtime_min": float(r["downtime_min"]), "hours": r["hours"],
                      "attainment_pct": a / t * 100 if t else None, "oee_pct": g / t * 100 if t else None,
                      "prev_attainment_pct": next((float(x["att"]) for x in R["prev"] if x["line_id"] == lid and x["att"] is not None), None),
                      "units_per_running_hour": a / max(1e-9, r["hours"] - float(r["downtime_min"]) / 60),
                      "downtime_by_reason": [{"reason": x["downtime_reason"], "minutes": float(x["minutes"])} for x in reasons],
                      "top_downtime_reason": reasons[0]["downtime_reason"] if reasons else None,
                      "inspections": q["n"], "fails": q["fails"], "fail_rate_pct": q["fails"] / q["n"] * 100 if q["n"] else None,
                      "top_defects": [{"defect_code": x["defect_code"], "defect_name": x["defect_name"], "station_id": x["station_id"], "fails": x["fails"]}
                                      for x in R["defects"] if x["line_id"] == lid][:5],
                      "ncr_root_causes": [{"root_cause_category": x["root_cause_category"], "n": x["n"]} for x in R["ncr"] if x["line_id"] == lid]}
    c["lines"] = lines
    c["backlog"] = R["backlog"]
    c["ncr_open"] = int(R["ncr_open"][0]["n"])
    for lid, l in sorted(lines.items()):
        sink.emit(4, "LOG", f"{lid}: plan attainment {l['attainment_pct']:.1f}%, OEE {l['oee_pct']:.1f}%, downtime {l['downtime_min']:.0f} min, "
                            f"inspection fail rate {l['fail_rate_pct'] or 0:.2f}% ({l['inspections']} inspections).")
    sink.emit(4, "LOG", f"Model features: {len(M)} machines x 6 risk components; {len(lines)} lines x 9 performance metrics.")
    return {"machines": len(M), "lines": len(lines)}


def s5_sync(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    M = c["machines"]
    scores = {mid: rules.score(m, cfg) for mid, m in M.items()}
    c["scores"] = scores
    with get_engine().begin() as conn:
        for mid, s in scores.items():
            conn.execute(text("INSERT INTO twin.agent_machine_state (run_id, machine_id, status, health_score, risk_score, metrics) "
                              "VALUES (CAST(:r AS uuid), :m, :st, :h, :rs, CAST(:x AS jsonb))"),
                         dict(r=sink.run_id, m=mid, st=s["status"], h=s["health_score"], rs=s["risk_score"],
                              x=json.dumps({**M[mid], "score": s}, default=str)))
    counts = {k: sum(1 for s in scores.values() if s["status"] == k) for k in SEV_ORDER}
    for mid in sorted(M):
        m, s = M[mid], scores[mid]
        sink.emit(5, "LOG", f"{m['asset_key']} -> zone {m['zone']} / station {m['station_id']}: {s['status']} (health {s['health_score']:.0f}, risk {s['risk_score']:.0f}).",
                  data={"machine_id": mid, "status": s["status"]})
    sink.emit(5, "LOG", f"3D twin synchronised: {counts}. Production information synchronised for {len(c['lines'])} lines.", data={"counts": counts})
    return {"status_counts": counts}


def s6_analyse(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    M, S, L = c["machines"], c["scores"], c["lines"]
    findings: list[dict] = []
    # machine condition (one finding per machine at WARNING or worse; anomaly-led findings get the ANOMALY category)
    for mid, m in M.items():
        s = S[mid]
        if s["status"] == "NORMAL":
            continue
        cat = "ANOMALY" if s["drivers"] and s["drivers"][0] in ("anomalies", "sensor_excursions") else \
              "MAINTENANCE" if s["drivers"] and s["drivers"][0] == "maintenance_due" else \
              "DOWNTIME" if s["drivers"] and s["drivers"][0] in ("down_time", "downtime_events") else "MACHINE_HEALTH"
        title = {"OFFLINE": f"{mid} is offline (state DOWN in the latest hour)"}.get(s["status"], f"{mid} {m['machine_name']}: {s['status'].lower()} condition, risk {s['risk_score']:.0f}/100")
        findings.append({"category": cat, "severity": s["status"], "machine_id": mid, "line_id": m["line_id"], "station_id": m["station_id"], "title": title,
                         "explanation": rules.explain(m, s), "root_causes": rules.root_causes(m), "value_label": "CALCULATED", "risk": s["risk_score"],
                         "evidence": {"risk_components": s["components"], "anomalies": m["anomalies"], "sensor_excursions": m["sensor_excursions"],
                                      "state_hours": m["state_hours"], "downtime": {k: m["downtime"][k] for k in ("events", "minutes", "impact_units", "open_events")},
                                      "maintenance": {k: m["maintenance"][k] for k in ("last_pm", "days_since_pm", "pm_overdue", "open_work_orders")},
                                      "latest": m["latest"], "window": m["window"]},
                         "_m": m, "_s": s})
    n_mach = len(findings)
    sink.emit(6, "LOG", f"Machine conditions analysed: {n_mach} of {len(M)} machines at WARNING or worse.")
    anom = sorted(((mid, m["anomalies"]) for mid, m in M.items() if m["anomalies"]), key=lambda x: -x[1])
    sink.emit(6, "LOG", f"Abnormalities: {sum(a for _, a in anom)} anomaly-flagged hours in the window" + (f", most on {anom[0][0]} ({anom[0][1]})." if anom else "."))
    # production performance
    tmin = float(cfg["plan_attainment_min_pct"])
    for lid, l in sorted(L.items()):
        att = l["attainment_pct"]
        if att is None or att >= tmin:
            continue
        sev = "CRITICAL" if att < tmin - 20 else "ATTENTION" if att < tmin - 10 else "WARNING"
        top = l["downtime_by_reason"][:3]
        rate = l["units_per_running_hour"]
        lost = (top[0]["minutes"] / 60 * rate) if top else 0
        trend = f" (previous window {l['prev_attainment_pct']:.1f}%)" if l["prev_attainment_pct"] is not None else ""
        findings.append({"category": "PRODUCTION", "severity": sev, "line_id": lid, "title": f"{lid} plan attainment {att:.1f}% below the {tmin:.0f}% rule",
                         "explanation": f"{lid} produced {l['actual']:,.0f} of {l['target']:,.0f} target units ({att:.1f}%{trend}); line downtime {l['downtime_min']:.0f} min, "
                                        f"largest reasons " + ", ".join(f"{x['reason']} {x['minutes']:.0f} min" for x in top) + ".",
                         "root_causes": [{"cause": x["reason"], "basis": f"{x['minutes']:.0f} min of MES line downtime in the window", "source": "MES (actual)"} for x in top],
                         "value_label": "CALCULATED", "risk": tmin - att + 20,
                         "evidence": {"target": l["target"], "actual": l["actual"], "good": l["good"], "attainment_pct": att, "oee_pct": l["oee_pct"],
                                      "downtime_by_reason": l["downtime_by_reason"], "units_per_running_hour": rate, "prev_attainment_pct": l["prev_attainment_pct"]},
                         "_lost_units": lost, "_line": l})
    sink.emit(6, "LOG", "Production performance: " + ", ".join(f"{lid} {l['attainment_pct']:.1f}%" for lid, l in sorted(L.items())) + f" plan attainment (rule: >= {tmin:.0f}%).")
    # quality
    qmax = float(cfg["quality_fail_rate_max_pct"])
    for lid, l in sorted(L.items()):
        fr = l["fail_rate_pct"]
        if fr is None or fr <= qmax:
            continue
        sev = "CRITICAL" if fr > 2 * qmax else "ATTENTION" if fr > 1.5 * qmax else "WARNING"
        d = l["top_defects"]
        findings.append({"category": "QUALITY", "severity": sev, "line_id": lid, "station_id": d[0]["station_id"] if d else None,
                         "title": f"{lid} inspection fail rate {fr:.2f}% above the {qmax:.1f}% rule",
                         "explanation": f"{l['fails']} of {l['inspections']} inspections failed on {lid}. Top defects: " +
                                        ", ".join(f"{x['defect_code']} {x['defect_name']} at {x['station_id']} ({x['fails']})" for x in d[:3]) + ".",
                         "root_causes": [{"cause": x["root_cause_category"], "basis": f"{x['n']} NCRs on this line in the last 90 days", "source": "QMS NCR/CAPA (actual, recorded)"}
                                         for x in l["ncr_root_causes"][:3]],
                         "value_label": "CALCULATED", "risk": fr * 5,
                         "evidence": {"inspections": l["inspections"], "fails": l["fails"], "fail_rate_pct": fr, "top_defects": d, "ncr_root_causes": l["ncr_root_causes"]},
                         "_line": l})
    sink.emit(6, "LOG", "Quality: " + ", ".join(f"{lid} {l['fail_rate_pct'] or 0:.2f}%" for lid, l in sorted(L.items())) + f" inspection fail rate (rule: <= {qmax:.1f}%).")
    # maintenance backlog
    if c["backlog"]:
        tot = sum(r["n"] for r in c["backlog"])
        oldest = min(r["oldest"] for r in c["backlog"])
        findings.append({"category": "MAINTENANCE", "severity": "WARNING", "title": f"{tot} work orders still open more than 30 days after creation",
                         "explanation": f"{tot} work orders on {len(c['backlog'])} machines are PLANNED or IN_PROGRESS although created over 30 days before the data end "
                                        f"(oldest {oldest:%Y-%m-%d}). Either the backlog is real or CMMS statuses are not being closed.",
                         "root_causes": [], "value_label": "ACTUAL", "risk": 22,
                         "evidence": {"by_machine": [{"machine_id": r["machine_id"], "open": r["n"], "oldest": r["oldest"]} for r in c["backlog"]]}})
    # data quality from step 3
    for i in c.get("dq_issues", []):
        if i.get("rows"):
            findings.append({"category": "DATA_QUALITY", "severity": "WARNING", "title": f"Data quality: {i['check']} ({i['rows']} rows)",
                             "explanation": f"{i['rows']} rows: {i['check']}. Reported, not corrected; affected values are shown as supplied.",
                             "root_causes": [], "value_label": "ACTUAL", "risk": 10, "evidence": i})
    # root causes
    rc = sum(1 for f in findings if f["root_causes"])
    sink.emit(6, "LOG", f"Root-cause investigation: {rc} findings have recorded causes (failure modes, downtime reasons, NCR categories). Causes are ranked from records, not inferred.")
    # prediction models
    if cfg["steps"].get("prediction_models", True):
        findings += _predictions(sink, c)
    else:
        sink.emit(6, "LOG", "Prediction models skipped by configuration.")
    sink.emit(6, "LOG", "Failure / time-to-failure prediction: UNAVAILABLE. No validated failure model exists for this data; the risk index is a rule set, not a prediction.")
    c["findings"] = findings
    return {"findings": len(findings)}


def _predictions(sink: Sink, c: dict) -> list[dict]:
    from ..twin.bottleneck import bottleneck_map
    from ..twin.scenarios import latest_calibration
    out = []
    for lid in sorted(c["lines"]):
        cal = latest_calibration(lid) or {}
        status = cal.get("status", "NOT_CALIBRATED")
        base = query("SELECT run_id, confidence_score, run_ts FROM twin.twin_scenario_run WHERE line_id = :l AND scenario_name = 'Baseline' "
                     "ORDER BY run_ts DESC LIMIT 1", l=lid)
        if status != "VALIDATED" or not base:
            sink.emit(6, "WARN", f"Throughput/bottleneck model for {lid}: not used (calibration {status}{', no baseline run' if not base else ''}).")
            continue
        b = bottleneck_map(lid, str(base[0]["run_id"]), marginal=False)
        st = next((s for s in b["stations"] if s["station_id"] == b["bottleneck_station_id"]), None)
        if not st:
            continue
        hold = ((cal.get("params") or {}).get("holdout") or {})
        twin_mape = (hold.get("daily") or {}).get("mape_pct")
        naive = (hold.get("naive_mean_baseline_daily") or {}).get("mape_pct")
        freq = b["shifting_bottleneck_frequency"].get(st["station_id"], 0)
        conf = float(base[0]["confidence_score"]) / 100 if base[0]["confidence_score"] is not None else None
        sink.emit(6, "LOG", f"Twin model (TWIN-2.0, {status}) for {lid}: predicted bottleneck {st['station_id']} at {st['utilization_pct']:.0f}% utilisation "
                            f"(most utilised in {freq * 100:.0f}% of replications).")
        out.append({"category": "BOTTLENECK", "severity": "WARNING" if st["utilization_pct"] >= 85 else "NORMAL", "line_id": lid, "station_id": st["station_id"],
                    "title": f"{lid} predicted bottleneck: {st['station_id']} ({st['utilization_pct']:.0f}% utilisation, SIMULATED baseline)",
                    "explanation": f"The calibrated line twin (hold-out daily MAPE {twin_mape:.1f}%" + (f", naive mean baseline {naive:.1f}%" if naive is not None else "") +
                                   f") predicts {st['station_id']} as the constraint with {st['utilization_pct']:.0f}% utilisation and an average queue of "
                                   f"{st['avg_queue_units']:.1f} units. Station-level values are SIMULATED (no station telemetry in MES).",
                    "root_causes": [], "value_label": "PREDICTED", "state_type": "SIMULATED", "risk": max(0, st["utilization_pct"] - 60),
                    "evidence": {"twin_run_id": str(base[0]["run_id"]), "calibration": status, "holdout_mape_pct": twin_mape, "naive_mape_pct": naive,
                                 "station": {k: st[k] for k in ("utilization_pct", "avg_queue_units", "blocked_pct", "starved_pct")}, "bottleneck_frequency": freq},
                    "_conf": conf})
    return out


def _llm_summary(sink: Sink, cfg: dict, ranked: list[dict], lines: dict) -> str | None:
    from ..config import settings
    if not (cfg["llm_narrative"] and settings.domo_token.get_secret_value()):
        return None
    from ..domo_llm import default_llm
    facts = [{"rank": f["rank"], "severity": f["severity"], "asset": f.get("machine_id") or f.get("line_id"), "title": f["title"], "label": f["value_label"]} for f in ranked[:10]]
    lm = {lid: {k: round(v, 1) if isinstance(v, float) else v for k, v in l.items() if k in ("attainment_pct", "oee_pct", "fail_rate_pct", "downtime_min")} for lid, l in lines.items()}
    prompt = ("Write a 4-6 sentence executive summary for a factory manager. Use ONLY the facts below; do not add any number that is not in them; "
              "keep the labels CALCULATED / PREDICTED where you cite a value; say the data is a historical export. No markdown.\n"
              f"LINE METRICS: {json.dumps(lm)}\nFINDINGS: {json.dumps(facts)}")
    try:
        t0 = time.perf_counter()
        out = default_llm().invoke(prompt, system="You summarise manufacturing findings faithfully. You never invent data.").strip()
        sink.emit(7, "LOG", f"LLM narrative generated from {len(facts)} findings in {time.perf_counter() - t0:.1f} s (Domo AI). Numbers in it come from the findings.")
        return out[:2500]
    except Exception as e:
        sink.emit(7, "WARN", f"LLM narrative unavailable ({type(e).__name__}); using the deterministic summary only.")
        return None


def s7_reason(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    F = sorted(c["findings"], key=lambda f: (SEV_ORDER.get(f["severity"], 9), -f.get("risk", 0)))
    for i, f in enumerate(F, 1):
        f["rank"] = i
    sink.emit(7, "LOG", f"Ranked {len(F)} findings by severity, then risk.")
    for f in F[:8]:
        sink.emit(7, "FINDING", f"#{f['rank']} [{f['severity']}] {f['title']}", data={"category": f["category"], "machine_id": f.get("machine_id"), "line_id": f.get("line_id"),
                                                                                     "label": f["value_label"]})
    ev = sum(1 for f in F if f["evidence"])
    sink.emit(7, "LOG", f"Evidence evaluated: {ev}/{len(F)} findings carry their supporting numbers; {sum(1 for f in F if f['value_label'] == 'PREDICTED')} use a validated model.")
    recs = []
    for f in F:
        if len(recs) >= int(cfg["max_recommendations"]) or f["severity"] == "NORMAL" or f["category"] == "DATA_QUALITY":
            continue
        if "_m" in f:
            m, s = f["_m"], f["_s"]
            d = m["downtime"]
            impact = (f"Addresses {d['minutes']:.0f} min of recorded machine downtime ({d['impact_units']:.0f} impact units) and "
                      f"{m['state_hours']['DOWN']} DOWN hours in the last {m['window']['days']} days (ACTUAL).") if d["minutes"] or m["state_hours"]["DOWN"] else None
            recs.append({"finding": f, "machine_id": m["machine_id"], "line_id": m["line_id"], "category": f["category"],
                         "issue": f["title"], "explanation": f["explanation"], "risk_level": RISK_LEVEL[f["severity"]], "action": rules.action_for(m, s),
                         "expected_impact": impact, "confidence": None, "evidence": f["evidence"]})
        elif f["category"] == "PRODUCTION":
            l = f["_line"]
            top = l["downtime_by_reason"][0] if l["downtime_by_reason"] else None
            gain = f["_lost_units"] / l["target"] * 100 if l["target"] else None
            recs.append({"finding": f, "machine_id": None, "line_id": f["line_id"], "category": "PRODUCTION", "issue": f["title"], "explanation": f["explanation"],
                         "risk_level": RISK_LEVEL[f["severity"]],
                         "action": f"Run a focused review of {top['reason']} stops on {f['line_id']} with production and maintenance; set a containment owner per shift." if top else
                                   f"Review the plan versus capacity on {f['line_id']}.",
                         "expected_impact": (f"Recovering the {top['reason']} downtime ({top['minutes']:.0f} min) at the line's observed {l['units_per_running_hour']:.0f} units per running hour "
                                             f"is about {f['_lost_units']:,.0f} units, +{gain:.1f} pts plan attainment (CALCULATED estimate).") if top else None,
                         "confidence": None, "evidence": f["evidence"]})
        elif f["category"] == "QUALITY":
            d = f["evidence"]["top_defects"]
            rc = f["root_causes"]
            recs.append({"finding": f, "machine_id": None, "line_id": f["line_id"], "category": "QUALITY", "issue": f["title"], "explanation": f["explanation"],
                         "risk_level": RISK_LEVEL[f["severity"]],
                         "action": (f"Quality engineering to contain {d[0]['defect_code']} {d[0]['defect_name']} at {d[0]['station_id']} and check the recorded root-cause categories "
                                    f"({', '.join(x['cause'] for x in rc[:2])}) before release." if d else f"Quality engineering review of {f['line_id']}."),
                         "expected_impact": f"{f['evidence']['fails']} failed inspections in the window (ACTUAL).", "confidence": None, "evidence": f["evidence"]})
        elif f["category"] == "BOTTLENECK":
            recs.append({"finding": f, "machine_id": None, "line_id": f["line_id"], "category": "BOTTLENECK", "issue": f["title"], "explanation": f["explanation"],
                         "risk_level": "MEDIUM", "action": f"Evaluate a cycle-time improvement at {f['station_id']} in the Scenario Lab before any physical change (SIMULATED, needs engineering review).",
                         "expected_impact": None, "confidence": f.get("_conf"), "evidence": f["evidence"]})
        elif f["category"] == "MAINTENANCE":
            recs.append({"finding": f, "machine_id": None, "line_id": None, "category": "MAINTENANCE", "issue": f["title"], "explanation": f["explanation"],
                         "risk_level": "MEDIUM", "action": "Maintenance planning to review the open work orders older than 30 days and close or reschedule them in the CMMS.",
                         "expected_impact": None, "confidence": None, "evidence": f["evidence"]})
    sink.emit(7, "LOG", f"Generated {len(recs)} recommendations (all require human approval; confidence is shown only where a validated model supplies it).")
    narrative = _llm_summary(sink, cfg, F, c["lines"])
    c.update(findings=F, recs=recs, narrative=narrative)
    return {"findings": len(F), "recommendations": len(recs), "llm_narrative": narrative is not None}


def s8_reports(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    F, recs = c["findings"], c["recs"]
    with get_engine().begin() as conn:
        for f in F:
            f["finding_id"] = str(conn.execute(text(
                "INSERT INTO twin.agent_finding (run_id, rank, category, severity, machine_id, line_id, station_id, title, explanation, root_causes, evidence, value_label, state_type) "
                "VALUES (CAST(:r AS uuid), :rk, :c, :sv, :m, :l, :st, :t, :e, CAST(:rc AS jsonb), CAST(:ev AS jsonb), :vl, :stt) RETURNING finding_id"),
                dict(r=sink.run_id, rk=f["rank"], c=f["category"], sv=f["severity"], m=f.get("machine_id"), l=f.get("line_id"), st=f.get("station_id"), t=f["title"],
                     e=f["explanation"], rc=json.dumps(f["root_causes"], default=str), ev=json.dumps(f["evidence"], default=str), vl=f["value_label"],
                     stt=f.get("state_type", "LIVE"))).scalar_one())
        sink.emit(8, "LOG", f"Saved {len(F)} findings.")
        for r in recs:
            r["rec_id"] = str(conn.execute(text(
                "INSERT INTO twin.recommendation (run_id, finding_id, machine_id, line_id, category, issue, explanation, evidence, risk_level, action, expected_impact, confidence) "
                "VALUES (CAST(:r AS uuid), CAST(:f AS uuid), :m, :l, :c, :i, :e, CAST(:ev AS jsonb), :rl, :a, :x, :cf) RETURNING rec_id"),
                dict(r=sink.run_id, f=r["finding"]["finding_id"], m=r["machine_id"], l=r["line_id"], c=r["category"], i=r["issue"], e=r["explanation"],
                     ev=json.dumps(r["evidence"], default=str), rl=r["risk_level"], a=r["action"], x=r["expected_impact"], cf=r["confidence"])).scalar_one())
        sink.emit(8, "LOG", f"Saved {len(recs)} recommendations with status PENDING_REVIEW.")
        clean = lambda f: {k: v for k, v in f.items() if not k.startswith("_")}
        reps = reports.build(sink.run_id, sink.run_id[:8].upper(), c["anchor"], {"start": c["start"].isoformat(), "end": c["anchor"].isoformat()},
                             c["machines"], c["scores"], c["lines"], [clean(f) for f in F], [{k: v for k, v in r.items() if k != "finding"} for r in recs], c["narrative"])
        for rp in reps:
            conn.execute(text("INSERT INTO twin.report (report_id, run_id, category, title, audience, scope, priority, content, generated_by) "
                              "VALUES (:id, CAST(:r AS uuid), :c, :t, :a, :s, :p, CAST(:ct AS jsonb), :g)"),
                         dict(id=rp["report_id"], r=sink.run_id, c=rp["category"], t=rp["title"], a=rp["audience"], s=rp["scope"], p=rp["priority"],
                              ct=json.dumps(rp["content"], default=str), g=cfg["agent_name"]))
        audit(conn, "agent", "AGENT_RESULTS_SAVED", "agent_run", sink.run_id, {"findings": len(F), "recommendations": len(recs), "reports": len(reps)})
    for rp in reps:
        sink.emit(8, "LOG", f"Report {rp['report_id']} '{rp['title']}' for {rp['audience']} (priority {rp['priority']}).")
    sink.emit(8, "LOG", "Dashboard, 3D twin and approval queue now read this run's results.")
    c["reports"] = reps
    c["summary"] = {"findings": len(F), "recommendations": len(recs), "reports": len(reps), "status_counts": {k: sum(1 for s in c["scores"].values() if s["status"] == k) for k in SEV_ORDER},
                    "data_as_of": c["anchor"].isoformat(), "window_days": int(cfg["analysis_window_days"]), "narrative": c["narrative"],
                    "top_findings": [{"rank": f["rank"], "severity": f["severity"], "title": f["title"]} for f in F[:5]]}
    return {"reports": len(reps)}


def s9_notify(sink: Sink, cfg: dict, actor: str):
    from . import notify
    c = _ctx(sink)
    rep = next(r for r in c["reports"] if r["category"] == "FACTORY_OVERVIEW")
    rep_full = {**rep, "status": "GENERATED", "generated_by": cfg["agent_name"]}
    subject, body = reports.email_text(rep_full)
    to = cfg["notify"].get("email_recipients") or None
    e = notify.prepare("EMAIL", subject, body, "agent", to, sink.run_id, rep["report_id"])
    t = notify.prepare("TEAMS", subject, body, "agent", None, sink.run_id, rep["report_id"])
    st = notify.channel_status()
    sink.emit(9, "LOG", f"Email prepared (notification {e['id']}): {e['status']}" + ("" if st["EMAIL"] == "CONFIGURED" else " - SMTP is not configured, nothing is sent."))
    sink.emit(9, "LOG", f"Microsoft Teams message prepared (notification {t['id']}): {t['status']}" + ("" if st["TEAMS"] == "CONFIGURED" else " - Teams webhook not configured, nothing is sent."))
    sent = []
    if cfg["notify"].get("auto_send"):
        for n in (e, t):
            if n["status"] == "PREPARED":
                res = notify.send(n["id"], "agent")
                sent.append(res)
                sink.emit(9, "LOG" if res["status"] == "SENT" else "WARN", f"{res['channel']} notification {res['id']}: {res['status']}" + (f" ({res['error']})" if res["error"] else ""))
    else:
        sink.emit(9, "LOG", "Auto-send is off: a person reviews and sends the prepared messages from Reports & Approval.")
    return {"email": e["status"], "teams": t["status"], "sent": [s["status"] for s in sent]}


def s10_approval(sink: Sink, cfg: dict, actor: str):
    c = _ctx(sink)
    superseded = 0
    if cfg["supersede_previous"]:
        with get_engine().begin() as conn:
            superseded = conn.execute(text("UPDATE twin.recommendation SET status = 'SUPERSEDED' WHERE status = 'PENDING_REVIEW' AND run_id <> CAST(:r AS uuid)"),
                                      dict(r=sink.run_id)).rowcount
            if superseded:
                audit(conn, "agent", "RECOMMENDATIONS_SUPERSEDED", "agent_run", sink.run_id, {"count": superseded})
    if superseded:
        sink.emit(10, "LOG", f"{superseded} pending recommendations from earlier runs marked SUPERSEDED by this run.")
    n = len(c["recs"])
    sink.emit(10, "LOG", f"{n} recommendations submitted for human review (PENDING_REVIEW). Only an authorised planner can approve, reject or request changes; "
                         "the database refuses any other path.")
    sink.emit(10, "LOG", "No work order or external action is created until a planner approves. The run is complete; decisions are tracked on Reports & Approval.")
    return {"pending_review": n, "superseded": superseded, "awaiting": "human decision"}


_PIPELINE = [s1_init, s2_context, s3_validate, s4_prepare, s5_sync, s6_analyse, s7_reason, s8_reports, s9_notify, s10_approval]
