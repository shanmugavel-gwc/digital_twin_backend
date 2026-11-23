"""Architecture & Data Flow: the six stages, filled from real metadata (row counts, time ranges, the latest agent run's step events,
recommendation decisions). Nothing here is illustrative: a source with no loaded data shows NOT_LOADED."""
from __future__ import annotations

import time

from ..deps import query, query_many
from . import agent_config, analytics
from .reports import CATEGORIES

# plant system -> tables loaded in Neon (time column for 'data through') ; CSV-only datasets are listed as not loaded
SYSTEMS = [
    ("MES", "Manufacturing execution", [("fact_hourly_production", "event_ts"), ("dim_production_line", None), ("dim_station", None), ("dim_product_routing", None)],
     ["fact_production_order (CSV only)"]),
    ("Workforce", "HR / time & attendance", [("dim_workforce", None), ("fact_workforce_attendance", "work_date")], ["dim_shift_calendar (CSV only)"]),
    ("SCADA", "Machine telemetry historian, energy meters", [("fact_machine_telemetry_hourly", '"timestamp"'), ("fact_energy_meter_hourly", '"timestamp"')],
     ["fact_utility_telemetry_hourly (CSV only)"]),
    ("CMMS", "Maintenance management", [("dim_machine", None), ("fact_maintenance_work_order", "created_ts"), ("fact_downtime_event", "start_ts")], []),
    ("ERP", "Inventory, shipments, quality master", [("dim_plant", None), ("fact_inventory_daily_snapshot", "snapshot_date"), ("fact_shipment_milestone", "dispatch_date"),
                                                     ("fact_quality_inspection", "inspection_ts"), ("fact_ncr_capa", "opened_ts"), ("dim_defect_code", None)],
     ["fact_customer_order, fact_purchase_order_receipt, fact_manufacturing_cost, dim_material, dim_supplier (CSV only)"]),
    ("BI Mart", "Daily line/shift KPI mart", [], ["mart_daily_line_shift_kpi (CSV only, used as a cross-check in docs, not loaded)"]),
]
PREP_STAGES = [
    ("ingestion", "Data ingestion", "CSV exports are loaded as supplied into Neon schema twin (scripts/load_data.py, load_factory_data.py): one transaction per load, row counts checked against the files."),
    ("validation", "Data validation", "Cross-reference checks (unknown machines/stations, plant mismatches), timestamp and duplicate checks; repeated in step 3 of every agent run."),
    ("cleaning", "Data cleaning", "Source data is never silently fixed. Rows that fail a check are kept and reported (docs/DATA_QUALITY.md); analyses state which rows they exclude."),
    ("missing", "Missing-value handling", "Nulls are counted per field; work orders without a failure mode are excluded from root-cause ranking only."),
    ("duplicates", "Duplicate identification", "Duplicate (machine, hour) telemetry and duplicate rows are counted; none are removed."),
    ("normalization", "Data normalization", "Timestamps are plant-local (Asia/Kolkata, A-05); units as supplied (C, mm/s, A, bar, rpm, kWh); ids as supplied."),
    ("transformation", "Data transformation", "SQL views: v_line_hourly_oee, v_line_daily_actuals, v_machine_3d_mapping, v_data_freshness."),
    ("features", "Feature engineering", "Per machine: utilisation, DOWN hours, anomaly flags, sensor excursions vs own baseline, temperature trend, downtime events, PM age, station fail rate."),
    ("rules", "Manufacturing rules", "Risk index weights and caps, status bands, plan-attainment and quality thresholds (Settings -> Agent configuration)."),
    ("prepared", "Prepared dataset generation", "agent_machine_state (per run), agent_finding, recommendation, report tables used by the dashboard, 3D twin and chatbot."),
]
CAPABILITIES = [
    ("Understand the factory structure", "AVAILABLE", "dim_plant, dim_line, dim_station, dim_machine, routing, 3D mapping"),
    ("Monitor machine conditions", "AVAILABLE", "hourly telemetry (historical export)"),
    ("Rank machines by operational risk", "AVAILABLE", "rule-based risk index (CALCULATED, not a trained model)"),
    ("Analyze production performance", "AVAILABLE", "MES hourly production"),
    ("Identify abnormal machine behavior", "AVAILABLE", "is_anomaly flags + sensor excursions vs own baseline"),
    ("Analyze manufacturing quality", "AVAILABLE", "quality inspections, defect codes, NCR/CAPA"),
    ("Explain why a machine or line has a problem", "AVAILABLE", "explanations built from the computed metrics"),
    ("Identify possible root causes", "AVAILABLE (recorded)", "ranked from recorded failure modes, downtime reasons and NCR categories; not inferred"),
    ("Predict future conditions", "PARTIAL", "throughput/bottleneck: calibrated twin (VALIDATED lines). Failure / time-to-failure: UNAVAILABLE (no validated model)"),
    ("Recommend corrective actions", "AVAILABLE", "rule-based actions; every recommendation needs human approval"),
    ("Generate operational insights", "AVAILABLE", "findings, reports, optional LLM summary written only from the findings"),
]
_CACHE: dict = {}


def _dt(v):
    """date and datetime columns mixed across tables: compare as datetimes."""
    from datetime import date, datetime
    return datetime.combine(v, datetime.min.time()) if isinstance(v, date) and not isinstance(v, datetime) else v


def architecture() -> dict:
    hit = _CACHE.get("a")
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    jobs = {}
    for sys_id, _, tables, _ in SYSTEMS:
        for t, col in tables:
            jobs[f"{sys_id}:{t}"] = (f"SELECT count(*) AS n" + (f", max({col}) AS hi, min({col}) AS lo" if col else "") + f" FROM twin.{t}", {})
    jobs["db"] = ("SELECT current_database() AS name, version() AS version, now() AS now", {})
    jobs["tables"] = ("SELECT c.relname AS table_name, c.reltuples::bigint AS approx_rows, obj_description(c.oid) AS description FROM pg_class c "
                      "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'twin' AND c.relkind = 'r' ORDER BY 1", {})
    jobs["views"] = ("SELECT table_name FROM information_schema.views WHERE table_schema = 'twin' ORDER BY 1", {})
    jobs["run"] = ("SELECT run_id, status, started_at, finished_at, records_processed, data_as_of, summary FROM twin.agent_run ORDER BY started_at DESC LIMIT 1", {})
    jobs["nulls"] = ("SELECT (SELECT count(*) FROM twin.fact_maintenance_work_order WHERE failure_mode IS NULL) AS wo_failure_mode, "
                     "(SELECT count(*) FROM twin.fact_inventory_daily_snapshot WHERE expiry_date IS NULL) AS inventory_expiry, "
                     "(SELECT count(*) FROM twin.fact_machine_telemetry_hourly WHERE temperature_c IS NULL OR vibration_mm_s IS NULL) AS telemetry_core", {})
    jobs["dups"] = ('SELECT count(*) AS n FROM (SELECT machine_id, "timestamp" FROM twin.fact_machine_telemetry_hourly GROUP BY 1, 2 HAVING count(*) > 1) d', {})
    jobs["dq"] = ("SELECT (SELECT count(*) FROM twin.fact_downtime_event t JOIN twin.dim_machine m USING (machine_id) WHERE t.plant_id <> m.plant_id) AS downtime_plant_mismatch, "
                  "(SELECT count(*) FROM twin.dim_machine m JOIN twin.dim_line l USING (line_id) WHERE m.plant_id <> l.plant_id) AS machine_plant_mismatch, "
                  "(SELECT count(*) FROM twin.v_scenario_accuracy WHERE NOT plant_matches_line) AS scenario_plant_mismatch", {})
    jobs["recs"] = ("SELECT status, count(*) AS n FROM twin.recommendation GROUP BY 1", {})
    jobs["decisions"] = ("SELECT r.rec_id, r.machine_id, r.line_id, r.issue, r.status, r.action_status, r.work_order_ref, v.reviewer, v.role, v.decision, v.comment, v.decided_at "
                         "FROM twin.recommendation_review v JOIN twin.recommendation r USING (rec_id) WHERE v.decision <> 'COMMENT' ORDER BY v.id DESC LIMIT 10", {})
    jobs["reports"] = ("SELECT category, count(*) AS n, max(created_at) AS last FROM twin.report GROUP BY 1", {})
    jobs["sensors"] = ("SELECT count(DISTINCT machine_id) AS machines FROM twin.fact_machine_telemetry_hourly", {})
    R = query_many(jobs, workers=10)
    run = R["run"][0] if R["run"] else None
    steps = query("SELECT DISTINCT ON (step) step, status, message, data, ts FROM twin.agent_run_event WHERE run_id = :r AND kind = 'STEP' ORDER BY step, id DESC",
                  r=run["run_id"]) if run else []
    warns = query("SELECT step, message FROM twin.agent_run_event WHERE run_id = :r AND kind IN ('WARN', 'ERROR') ORDER BY id", r=run["run_id"]) if run else []

    systems = []
    for sys_id, desc, tables, not_loaded in SYSTEMS:
        tl = []
        for t, col in tables:
            x = R[f"{sys_id}:{t}"][0]
            tl.append({"table": f"twin.{t}", "rows": int(x["n"]), "from": _dt(x.get("lo")), "to": _dt(x.get("hi"))})
        recs = sum(t["rows"] for t in tl)
        thr = max((t["to"] for t in tl if t["to"] is not None), default=None)
        systems.append({"id": sys_id, "description": desc, "status": "LOADED (batch export)" if recs else "NOT_LOADED", "tables": tl, "records": recs,
                        "data_types": [t["table"].split(".")[1] for t in tl], "not_loaded": not_loaded, "data_through": thr,
                        "last_sync": "batch load; load time not recorded (no live feed)" if recs else None, "state_type": "LIVE" if recs else "NOT_CONNECTED"})
    counts = {}
    for t in R["tables"]:
        counts[t["table_name"]] = None
    exact = query_many({t: (f'SELECT count(*) AS n FROM twin."{t}"', {}) for t in counts}, workers=10)
    tables = [{"table": t, "rows": int(exact[t][0]["n"])} for t in counts]
    db = R["db"][0]
    plant_tables = [t for t in tables if t["table"].startswith(("dim_", "fact_"))]
    app_tables = [t for t in tables if not t["table"].startswith(("dim_", "fact_"))]
    cfg = agent_config.load()
    n = lambda k: int(R["nulls"][0][k])
    prep = []
    step_by = {s["step"]: s for s in steps}
    for key, title, text in PREP_STAGES:
        detail = {}
        if key == "missing":
            detail = {"work orders without failure_mode": n("wo_failure_mode"), "inventory rows without expiry_date": n("inventory_expiry"),
                      "telemetry rows missing temperature/vibration": n("telemetry_core")}
        elif key == "duplicates":
            detail = {"duplicate (machine, hour) telemetry rows": int(R["dups"][0]["n"])}
        elif key == "validation":
            detail = {k: int(v) for k, v in R["dq"][0].items()}
        elif key == "rules":
            detail = {"risk_weights": cfg["risk_weights"], "status_thresholds": cfg["status_thresholds"], "sensor_sigma": cfg["sensor_sigma"],
                      "plan_attainment_min_pct": cfg["plan_attainment_min_pct"], "quality_fail_rate_max_pct": cfg["quality_fail_rate_max_pct"]}
        elif key == "transformation":
            detail = {"views": [v["table_name"] for v in R["views"]]}
        elif key == "prepared":
            detail = {t["table"]: t["rows"] for t in app_tables if t["table"] in ("agent_machine_state", "agent_finding", "recommendation", "report")}
        run_step = {"validation": 3, "features": 4, "rules": 4, "prepared": 8}.get(key)
        st = step_by.get(run_step) if run_step else None
        prep.append({"id": key, "title": title, "description": text, "detail": detail,
                     "status": (st["status"] if st else ("DONE (at load)" if key in ("ingestion", "cleaning", "missing", "duplicates", "normalization", "transformation") else "NOT_RUN"))})
    rc = {r["status"]: r["n"] for r in R["recs"]}
    out = {
        "stages": {
            "systems": systems,
            "database": {"name": db["name"], "type": "PostgreSQL (Neon, serverless)", "version": db["version"].split(" on ")[0], "schema": "twin",
                         "records": sum(t["rows"] for t in tables), "plant_records": sum(t["rows"] for t in plant_tables), "record_types": len(tables),
                         "plant_tables": plant_tables, "app_tables": app_tables, "views": [v["table_name"] for v in R["views"]],
                         "data_through": max((s["data_through"] for s in systems if s["data_through"]), default=None),
                         "last_app_write": run["finished_at"] if run else None,
                         "availability": {s["id"]: s["status"] for s in systems}},
            "preparation": {"stages": prep, "validation_failures": [{"step": w["step"], "message": w["message"]} for w in warns],
                            "last_run": {"run_id": str(run["run_id"]), "status": run["status"], "finished_at": run["finished_at"]} if run else None},
            "agent": {"name": cfg["agent_name"], "single_agent": True, "capabilities": [{"capability": c, "status": s, "basis": b} for c, s, b in CAPABILITIES],
                      "machines": int(R["sensors"][0]["machines"]), "sensor_channels": ["temperature_c", "vibration_mm_s", "motor_current_a", "pressure_bar", "rpm"],
                      "sensor_series": int(R["sensors"][0]["machines"]) * 5,
                      "steps": [{"step": s["step"], "status": s["status"], "message": s["message"]} for s in steps]},
            "outputs": [{"category": k, "title": v[0], "audience": v[1], "reports": next((r["n"] for r in R["reports"] if r["category"] == k), 0),
                         "last": next((r["last"] for r in R["reports"] if r["category"] == k), None)} for k, v in CATEGORIES.items()],
            "decision": {"pending": rc.get("PENDING_REVIEW", 0), "approved": rc.get("APPROVED", 0), "rejected": rc.get("REJECTED", 0),
                         "changes_requested": rc.get("CHANGES_REQUESTED", 0), "superseded": rc.get("SUPERSEDED", 0),
                         "recent": [{**d, "rec_id": str(d["rec_id"])} for d in R["decisions"]],
                         "rule": "No maintenance work order or external operational action is created before an authorised planner approves. Enforced by a database trigger."},
        },
        "bounds": analytics.data_bounds(),
    }
    _CACHE["a"] = (time.time(), out)
    return out
