"""Agent tools (LangChain @tool). Every call is written to audit_log by agent.py.
There is deliberately NO approve/decide tool and no tool that can reach PLC/SCADA: the agent can only read, simulate,
recommend and submit_for_review."""
from __future__ import annotations

import re
from typing import Any, Optional

from langchain_core.tools import tool
from sqlalchemy import text

from .db import readonly_engine
from .twin import scenarios as sc
from .twin.bottleneck import bottleneck_map
from .twin.model import ASSUMPTIONS

# ------------------------------------------------------------------ SQL guard (filter) + twin_ro role (the real safeguard)
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|merge|vacuum|analyze|lock|listen|notify|"
    r"set|reset|show|explain|into|pg_sleep|lo_import|lo_export|dblink|set_config|current_setting|nextval|setval)\b|\bpg_\w+|\binformation_schema\b", re.I)
MAX_ROWS, TIMEOUT = 50, "8s"


class SqlRejected(ValueError):
    pass


def validate_sql(q: str) -> str:
    q = (q or "").strip()
    if not q:
        raise SqlRejected("empty query")
    if len(q) > 4000:
        raise SqlRejected("query too long (max 4000 characters)")
    if ";" in q:
        raise SqlRejected("semicolons are not allowed (single statement only)")
    if "--" in q or "/*" in q:
        raise SqlRejected("SQL comments are not allowed")
    if not re.match(r"^(select|with)\b", q, re.I):
        raise SqlRejected("only a single SELECT or WITH ... SELECT is allowed")
    m = FORBIDDEN.search(q)
    if m:
        raise SqlRejected(f"forbidden keyword: {m.group(0)!r}")
    return q


def _execute_raw(sql: str, rows: int = MAX_ROWS) -> list[dict]:
    """Runs on the twin_ro role in a READ ONLY transaction with an 8 s timeout. Used by run_sql AFTER the filter; tests call it
    directly with hostile SQL to prove the DB role itself blocks writes and protected tables."""
    eng = readonly_engine()
    with eng.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        c.execute(text(f"SET LOCAL statement_timeout = '{TIMEOUT}'"))
        res = c.execute(text(f"SELECT * FROM ({sql.replace(':', chr(92) + ':')}) AS _q LIMIT {int(rows)}"))
        return [dict(r) for r in res.mappings().all()]


@tool
def get_schema() -> dict:
    """List the tables and columns the read-only SQL role can see (analytical tables and views only)."""
    eng = readonly_engine()
    with eng.connect() as c:
        rows = c.execute(text("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = 'twin' "
                              "ORDER BY table_name, ordinal_position")).mappings().all()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["table_name"], []).append(f"{r['column_name']} {r['data_type']}")
    return {"schema": "twin", "tables": out, "note": "Qualify tables as twin.<name>. fact_hourly_production and v_line_hourly_oee are LIVE (MES); "
            "twin_* tables and fact_digital_twin_scenario are SIMULATED."}


@tool
def run_sql(query: str) -> dict:
    """Run ONE read-only SELECT (or WITH ... SELECT) against the twin schema. Max 50 rows, 8 s timeout, no semicolons."""
    q = validate_sql(query)
    return {"rows": _execute_raw(q), "row_cap": MAX_ROWS, "state_type_note": "Rows from MES tables are LIVE; rows from twin_* tables are SIMULATED."}


def _compact(run: dict) -> dict:
    keep = ("run_id", "scenario_name", "line_id", "model_version", "state_type", "status", "approval_required", "predicted_output_units",
            "predicted_wip_units", "predicted_utilization_pct", "bottleneck_station_id", "confidence", "validation_status", "turnaround_ms",
            "baseline_delta", "baseline", "assumptions", "params", "horizon_hours", "replications", "persisted")
    return {k: run.get(k) for k in keep}


@tool
def run_scenario(scenario_name: str, line_id: str, params: Optional[dict] = None, replications: int = 30, horizon_hours: float = 24) -> dict:
    """Simulate a scenario on a line vs its baseline. scenario_name: Baseline | Demand +15% | Additional shift | Buffer increase |
    Cycle time improvement | SMT machine outage | Custom. line_id: SMT-L1 | SMT-L2 | FAL-L1 | FAL-L2. params examples:
    {"buffer_factor": 2}, {"cycle_improvement_pct": 10, "stations": ["ST-SMT-080"]}, {"outage_station_id": "ST-SMT-040", "outage_hours": 4},
    {"extra_shift_hours": 8}, Custom: {"demand_change_pct": 10, "capacity_change_pct": 5}. Result is SIMULATED and needs human review."""
    try:
        return _compact(sc.run_scenario(scenario_name, line_id, params or {}, int(replications), float(horizon_hours), requested_by="agent"))
    except sc.ScenarioError as e:
        return {"error": e.message, "status_code": e.status}


@tool
def compare_scenarios(run_ids: list[str]) -> dict:
    """Compare 2 to 6 stored scenario runs side by side (use run_ids returned by run_scenario)."""
    try:
        return {"runs": [_compact(r) for r in sc.compare(run_ids)["runs"]]}
    except sc.ScenarioError as e:
        return {"error": e.message, "status_code": e.status}


@tool
def get_bottleneck_map(line_id: str, run_id: Optional[str] = None) -> dict:
    """Bottleneck map of the latest (or given) twin run for a line: stations ranked by utilisation and upstream blocking, shifting-bottleneck frequency."""
    try:
        return bottleneck_map(line_id, run_id, marginal=False)
    except sc.ScenarioError as e:
        return {"error": e.message, "status_code": e.status}


@tool
def get_calibration_report(line_id: Optional[str] = None) -> dict:
    """Calibration report: hold-out MAPE/bias/RMSE and verdict (VALIDATED | NEEDS_REVIEW | NOT_VALIDATED) per line."""
    from .routers.calibration import report
    r = report()
    lines = [{k: l[k] for k in ("line_id", "verdict", "parallelism_factor", "speed_factor", "train", "holdout")} for l in r["lines"]
             if line_id in (None, l["line_id"])]
    return {"validation_status": r["validation_status"], "thresholds": r["thresholds"], "lines": lines, "message": r.get("message"),
            "state_type": "SIMULATED"}


@tool
def get_model_assumptions(run_id: Optional[str] = None) -> dict:
    """Assumptions of one scenario run, or the model-wide assumptions when no run_id is given."""
    if run_id:
        r = sc.get_run(run_id)
        return {"run_id": run_id, "assumptions": r["assumptions"], "validation_status": r["validation_status"]} if r else {"error": "run not found"}
    return {"assumptions": ASSUMPTIONS + sc.GENERAL_ASSUMPTIONS}


@tool
def get_kpis() -> dict:
    """The 5 dashboard KPIs: prediction error, throughput improvement, bottleneck reduction, scenario turnaround, model data freshness."""
    from .routers.kpis import kpis
    return kpis()


@tool
def submit_for_review(run_id: str) -> dict:
    """Submit a SIMULATED scenario to human engineering review (status PENDING_REVIEW). You cannot approve or reject."""
    try:
        r = sc.submit_for_review(run_id, "agent")
        return {"run_id": r["run_id"], "status": r["status"], "note": "Awaiting a human reviewer under change control."}
    except sc.ScenarioError as e:
        return {"error": e.message, "status_code": e.status}


from .factory.chat_tools import FACTORY_TOOLS  # noqa: E402  (factory data tools: machines, production, quality, findings, approvals)

TOOLS = [*FACTORY_TOOLS, get_schema, run_sql, run_scenario, compare_scenarios, get_bottleneck_map, get_calibration_report, get_model_assumptions, get_kpis,
         submit_for_review]
TOOL_MAP = {t.name: t for t in TOOLS}
FORBIDDEN_TOOL_HINT = ("approve", "reject", "decide", "plc", "scada", "write", "actuate", "setpoint", "modbus", "opc")
