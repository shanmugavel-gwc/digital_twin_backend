"""Home page: architecture pipeline counts, problems the agent addresses (with current figures), and a line x day heatmap.
Every figure is read from the database (historical export + latest agent run); nothing is illustrative."""
from __future__ import annotations

import time

from ..deps import query_many

_CACHE: dict = {}
TABLES = {
    "mes": ["fact_hourly_production"],
    "scada": ["fact_machine_telemetry_hourly", "fact_energy_meter_hourly"],
    "cmms": ["fact_maintenance_work_order", "fact_downtime_event", "dim_machine"],
    "qms": ["fact_quality_inspection", "fact_ncr_capa", "dim_defect_code"],
    "erp": ["fact_inventory_daily_snapshot", "fact_shipment_milestone", "dim_plant"],
    "workforce": ["fact_workforce_attendance", "dim_workforce"],
}


def home() -> dict:
    hit = _CACHE.get("h")
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    jobs = {f"n:{t}": (f"SELECT count(*) AS n FROM twin.{t}", {}) for ts in TABLES.values() for t in ts}
    jobs.update({
        "tables": ("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'twin' AND table_type = 'BASE TABLE'", {}),
        "anchor": ("SELECT max(event_ts) AS hi FROM twin.fact_hourly_production", {}),
        "run": ("SELECT run_id, status, finished_at, started_at, records_processed, summary FROM twin.agent_run ORDER BY started_at DESC LIMIT 1", {}),
        "state": ("SELECT status, count(*) AS n FROM twin.agent_machine_state WHERE run_id = (SELECT run_id FROM twin.agent_run WHERE status = 'COMPLETED' "
                  "ORDER BY started_at DESC LIMIT 1) GROUP BY 1", {}),
        "pending": ("SELECT count(*) AS n FROM twin.recommendation WHERE status = 'PENDING_REVIEW'", {}),
        "approved": ("SELECT count(*) AS n FROM twin.recommendation WHERE status = 'APPROVED'", {}),
        "down7": ("SELECT sum(downtime_min) AS m, sum(actual_qty) AS a, sum(target_qty) AS t, sum(good_qty) AS g FROM twin.fact_hourly_production "
                  "WHERE event_ts > (SELECT max(event_ts) FROM twin.fact_hourly_production) - interval '7 days'", {}),
        "top_reason": ("SELECT downtime_reason AS r, sum(downtime_min) AS m FROM twin.fact_hourly_production WHERE downtime_min > 0 AND downtime_reason <> 'NONE' "
                       "AND event_ts > (SELECT max(event_ts) FROM twin.fact_hourly_production) - interval '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 1", {}),
        "q7": ("SELECT count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS f FROM twin.fact_quality_inspection "
               "WHERE inspection_ts > (SELECT max(inspection_ts) FROM twin.fact_quality_inspection) - interval '7 days'", {}),
        "backlog": ("SELECT count(*) AS n FROM twin.fact_maintenance_work_order WHERE status IN ('PLANNED', 'IN_PROGRESS') "
                    "AND created_ts < (SELECT max(created_ts) FROM twin.fact_maintenance_work_order) - interval '30 days'", {}),
        "anom7": ('SELECT count(*) AS n FROM twin.fact_machine_telemetry_hourly WHERE is_anomaly AND "timestamp" > '
                  '(SELECT max("timestamp") FROM twin.fact_machine_telemetry_hourly) - interval \'7 days\'', {}),
        "heat": ("SELECT line_id, event_date AS d, sum(actual_qty)::numeric / NULLIF(sum(target_qty), 0) * 100 AS att, "
                 "sum(good_qty)::numeric / NULLIF(sum(target_qty), 0) * 100 AS oee, sum(actual_qty) AS actual, sum(downtime_min) AS dt "
                 "FROM twin.fact_hourly_production WHERE event_date > (SELECT max(event_date) FROM twin.fact_hourly_production) - 7 "
                 "GROUP BY 1, 2 ORDER BY 1, 2", {}),
        "runs": ("SELECT count(*) AS n FROM twin.agent_run WHERE status = 'COMPLETED'", {}),
        "audit": ("SELECT count(*) AS n FROM twin.audit_log", {}),
        "reports": ("SELECT count(*) AS n FROM twin.report", {}),
        "quality": ("SELECT count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fails, count(DISTINCT serial_number) AS serials, "
                    "count(*) FILTER (WHERE result = 'FAIL' AND severity = 'CRITICAL') AS critical FROM twin.fact_quality_inspection", {}),
        "ncr": ("SELECT count(*) AS n, count(*) FILTER (WHERE capa_status <> 'CLOSED') AS open FROM twin.fact_ncr_capa", {}),
        "mes_q": ("SELECT sum(actual_qty) AS actual, sum(good_qty) AS good, sum(reject_qty) AS reject FROM twin.fact_hourly_production", {}),
        "top_defect": ("SELECT q.defect_code, d.defect_name, count(*) AS n FROM twin.fact_quality_inspection q LEFT JOIN twin.dim_defect_code d USING (defect_code) "
                       "WHERE q.result = 'FAIL' GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 1", {}),
    })
    R = query_many(jobs, workers=10)
    n = lambda k: int(R[k][0]["n"]) if R[k] and R[k][0]["n"] is not None else 0
    sources = {k: {"tables": ts, "records": sum(n(f"n:{t}") for t in ts)} for k, ts in TABLES.items()}
    run = R["run"][0] if R["run"] else None
    s = (run or {}).get("summary") or {}
    st = {r["status"]: r["n"] for r in R["state"]}
    d7 = R["down7"][0]
    q7 = R["q7"][0]
    heat_days = sorted({r["d"].isoformat() for r in R["heat"]})
    heat = {}
    for r in R["heat"]:
        heat.setdefault(r["line_id"], {})[r["d"].isoformat()] = {"attainment_pct": float(r["att"]) if r["att"] is not None else None,
                                                                 "oee_pct": float(r["oee"]) if r["oee"] is not None else None,
                                                                 "actual": float(r["actual"] or 0), "downtime_min": float(r["dt"] or 0)}
    out = {
        "data_through": R["anchor"][0]["hi"],
        "database": {"engine": "PostgreSQL on Neon", "schema": "twin", "tables": n("tables"),
                     "plant_records": sum(v["records"] for v in sources.values())},
        "sources": sources,
        "agent": {"run_id": str(run["run_id"]) if run else None, "status": run["status"] if run else "NEVER_RUN",
                  "finished_at": run["finished_at"] if run else None, "started_at": run["started_at"] if run else None,
                  "records_processed": run["records_processed"] if run else 0, "findings": s.get("findings"), "recommendations": s.get("recommendations"),
                  "reports": s.get("reports"), "completed_runs": n("runs")},
        "machine_status": {k: st.get(k, 0) for k in ("NORMAL", "WARNING", "ATTENTION", "CRITICAL", "OFFLINE")},
        "approvals": {"pending": n("pending"), "approved": n("approved")},
        "problems": [
            {"id": "degradation", "title": "Unseen machine degradation", "value": st.get("WARNING", 0) + st.get("ATTENTION", 0) + st.get("CRITICAL", 0),
             "unit": "machines flagged", "detail": f"{n('anom7')} anomaly-flagged hours in the last 7 days of data", "go": "twin"},
            {"id": "downtime", "title": "Downtime", "value": float(d7["m"] or 0), "unit": "min line downtime (7 d)",
             "detail": f"largest reason: {R['top_reason'][0]['r']}" if R["top_reason"] else "", "go": "overview"},
            {"id": "quality", "title": "Quality drift", "value": (q7["f"] / q7["n"] * 100) if q7["n"] else None, "unit": "% inspections failed (7 d)",
             "detail": f"{q7['f']} of {q7['n']} inspections", "go": "reports"},
            {"id": "backlog", "title": "Maintenance backlog", "value": n("backlog"), "unit": "work orders open > 30 days", "detail": "CMMS status not closed", "go": "reports"},
            {"id": "decisions", "title": "Slow, untracked decisions", "value": n("pending"), "unit": "recommendations awaiting a planner",
             "detail": f"{n('approved')} approved and tracked · {n('audit')} audit entries", "go": "reports"},
        ],
        "kpis": {"plan_attainment_pct": float(d7["a"]) / float(d7["t"]) * 100 if d7["t"] else None,
                 "oee_pct": float(d7["g"]) / float(d7["t"]) * 100 if d7["t"] else None, "reports": n("reports")},
        "heatmap": {"days": heat_days, "lines": heat},
        "quality": {"inspections": R["quality"][0]["n"], "failed": R["quality"][0]["fails"], "critical": R["quality"][0]["critical"],
                    "serials": R["quality"][0]["serials"], "pass_rate_pct": (1 - R["quality"][0]["fails"] / R["quality"][0]["n"]) * 100 if R["quality"][0]["n"] else None,
                    "ncr": R["ncr"][0]["n"], "ncr_open": R["ncr"][0]["open"],
                    "reject_units": float(R["mes_q"][0]["reject"] or 0), "good_rate_pct": float(R["mes_q"][0]["good"]) / float(R["mes_q"][0]["actual"]) * 100 if R["mes_q"][0]["actual"] else None,
                    "top_defect": R["top_defect"][0] if R["top_defect"] else None},
    }
    _CACHE["h"] = (time.time(), out)
    return out
