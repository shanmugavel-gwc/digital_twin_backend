"""Factory tools for the chatbot: the same prepared data and agent results the central agent uses. Read-only.
Each tool returns compact JSON; `table` / `chart` keys are rendered by the UI under the answer (supporting data)."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

from langchain_core.tools import tool

from ..deps import query
from . import analytics

MID = re.compile(r"\bM-[A-Z0-9]+(?:-[A-Z0-9]+)?\b", re.I)     # broad on purpose: an unknown id gets a clear 'unknown machine' answer


def _f(v, d=1):
    return round(float(v), d) if v is not None else None


def _window(days: int):
    days = max(1, min(int(days or 7), 92))
    hi = analytics.data_bounds()["mes"]["hi"]
    return (hi - timedelta(days=days)).date().isoformat(), hi.date().isoformat()


@tool
def get_factory_status() -> dict:
    """Current factory status (last 7 days of data): machines active/idle/offline, production vs target, OEE, quality pass rate, downtime,
    machine health counts from the latest agent run, pending recommendations. LIVE (historical export)."""
    o = analytics.overview(analytics.resolve_filters(None, None, None, None, None, None))
    k = o["kpis"]
    h = o["charts"]["machine_health"]
    return {"state_type": "LIVE", "data_as_of": str(k["machine_state_as_of"]), "window": [o["filters"]["from"], o["filters"]["to"]],
            "machines": {"total": k["total_machines"], "active": k["active_machines"], "idle": k["idle_machines"], "setup": k["setup_machines"],
                         "offline": k["offline_machines"], "under_maintenance": k["machines_under_maintenance"]},
            "production": {"actual": k["total_production"], "target": k["production_target"], "efficiency_pct": _f(k["production_efficiency_pct"]),
                           "oee_pct": _f(k["oee_pct"])},
            "quality": {"pass_rate_pct": _f(k["quality_pass_rate_pct"], 2), "failed_inspections": k["total_defects"], "inspections": k["inspections"]},
            "downtime_min": k["downtime_min"], "health_counts": h.get("counts"), "pending_recommendations": k["pending_recommendations"],
            "agent_last_run": {x: str(y) for x, y in (k["agent_last_run"] or {}).items() if x in ("run_id", "status", "finished_at")},
            "labels": "machines/production ACTUAL; efficiency, OEE, pass rate, health CALCULATED",
            "table": {"title": "Factory status (last 7 days of data)", "columns": ["Metric", "Value"],
                      "rows": [["Active / total machines", f"{k['active_machines']} / {k['total_machines']}"], ["Offline machines", k["offline_machines"]],
                               ["Production / target", f"{k['total_production']:,.0f} / {k['production_target']:,.0f}"],
                               ["Plan attainment %", _f(k["production_efficiency_pct"])], ["OEE %", _f(k["oee_pct"])],
                               ["Quality pass rate %", _f(k["quality_pass_rate_pct"], 2)], ["Line downtime min", k["downtime_min"]],
                               ["Pending recommendations", k["pending_recommendations"]]]}}


@tool
def get_machine_health(machine_id: Optional[str] = None) -> dict:
    """Machine health and risk from the latest agent run (rule-based index, CALCULATED). Without machine_id: all machines ranked by risk.
    With machine_id (e.g. M-SPI-02): status, latest sensor readings, drivers, findings, possible causes and recommended action."""
    st = analytics.machine_states()
    if not st["run_id"]:
        return {"error": "The Digital Twin Agent has not completed a run yet; run it on the Agent Execution page."}
    ms = st["machines"]
    if machine_id:
        mid = machine_id.strip().upper()
        m = next((x for x in ms if x["machine_id"] == mid), None)
        if not m:
            return {"error": f"unknown machine {mid}. Known: {', '.join(x['machine_id'] for x in ms)}"}
        met = m["metrics"] or {}
        sc = met.get("score", {})
        return {"machine_id": mid, "name": m["machine_name"], "type": m["machine_type"], "line_id": m["line_id"], "station_id": m["station_id"], "zone": m["zone"],
                "status": m["status"], "health_score": m["health_score"], "risk_score": m["risk_score"], "method": "RULE_BASED_INDEX (CALCULATED, not a trained model)",
                "drivers": sc.get("drivers"), "risk_components": sc.get("components"), "latest_reading": met.get("latest"),
                "anomalies_window": met.get("anomalies"), "sensor_excursions": met.get("sensor_excursions"), "state_hours": met.get("state_hours"),
                "downtime": {k: (met.get("downtime") or {}).get(k) for k in ("events", "minutes", "top_reason")}, "maintenance": met.get("maintenance"),
                "findings": [{"severity": f["severity"], "title": f["title"], "explanation": f["explanation"], "possible_causes": f["root_causes"]} for f in m["findings"]],
                "recommendations": [{"action": r["action"], "risk_level": r["risk_level"], "status": r["status"]} for r in m["recommendations"]],
                "run_id": st["run_id"], "data_as_of": str((st["run"] or {}).get("data_as_of"))}
    rows = sorted([x for x in ms if x["risk_score"] is not None], key=lambda x: -x["risk_score"])
    return {"run_id": st["run_id"], "method": "RULE_BASED_INDEX (CALCULATED)",
            "table": {"title": "Machine health (latest agent run)", "columns": ["Machine", "Name", "Line", "Status", "Health", "Risk"],
                      "rows": [[x["machine_id"], x["machine_name"], x["line_id"], x["status"], x["health_score"], x["risk_score"]] for x in rows]},
            "chart": {"type": "bar", "title": "Risk index by machine", "x": "machine_id", "series": ["risk_score"],
                      "data": [{"machine_id": x["machine_id"], "risk_score": x["risk_score"]} for x in rows]}}


@tool
def get_abnormal_machines() -> dict:
    """Machines with anomaly-flagged telemetry or sensor readings above their own baseline in the latest agent run window (ACTUAL flags,
    CALCULATED excursions), most abnormal first."""
    st = analytics.machine_states()
    if not st["run_id"]:
        return {"error": "No completed agent run yet."}
    rows = []
    for m in st["machines"]:
        met = m["metrics"] or {}
        ex = met.get("sensor_excursions") or {}
        if met.get("anomalies") or sum(ex.values() or [0]):
            lt = met.get("latest") or {}
            rows.append([m["machine_id"], m["line_id"], met.get("anomalies"), ex.get("temperature"), ex.get("vibration"), ex.get("motor_current"),
                         _f(met.get("temp_max")), _f(met.get("vib_max"), 2), lt.get("machine_state")])
    rows.sort(key=lambda r: -((r[2] or 0) + (r[3] or 0) + (r[4] or 0) + (r[5] or 0)))
    return {"run_id": st["run_id"], "window_days": ((st["machines"][0]["metrics"] or {}).get("window") or {}).get("days"),
            "table": {"title": "Abnormal readings (analysis window)", "columns": ["Machine", "Line", "Anomaly flags", "Temp exc h", "Vib exc h", "Current exc h",
                                                                                  "Temp max C", "Vib max mm/s", "Latest state"], "rows": rows},
            "note": "Anomaly flags are as supplied (is_anomaly). Excursions = hours above the machine's own baseline mean + k sigma."}


@tool
def get_production_performance(days: int = 7, line_id: Optional[str] = None) -> dict:
    """Production for the last N days of data (default 7): daily target vs actual vs good, plan attainment and OEE, per line. ACTUAL + CALCULATED."""
    if line_id and line_id not in analytics.LINES:
        return {"error": f"line_id must be one of {', '.join(analytics.LINES)}"}
    d0, d1 = _window(days)
    o = analytics.overview(analytics.resolve_filters(d0, d1, line_id, None, None, None))
    tr = o["charts"]["production_trend"]
    return {"window": [d0, d1], "line_id": line_id or "all", "total": {k: o["kpis"][k] for k in ("total_production", "production_target", "good_units", "downtime_min")},
            "plan_attainment_pct": _f(o["kpis"]["production_efficiency_pct"]), "oee_pct": _f(o["kpis"]["oee_pct"]),
            "by_line": [{k: (_f(v) if isinstance(v, float) else v) for k, v in l.items()} for l in o["charts"]["line_comparison"]],
            "chart": {"type": "line", "title": f"Daily production, {line_id or 'all lines'}", "x": "date", "series": ["target", "actual", "good"], "data": tr},
            "table": {"title": "Daily production", "columns": ["Date", "Target", "Actual", "Good", "Downtime min"],
                      "rows": [[r["date"], r["target"], r["actual"], r["good"], r["downtime_min"]] for r in tr]}}


@tool
def compare_lines(line_a: str, line_b: str, days: int = 7) -> dict:
    """Compare two production lines over the last N days of data: target, actual, plan attainment, OEE, downtime, inspection fail rate."""
    for l in (line_a, line_b):
        if l not in analytics.LINES:
            return {"error": f"line ids must be among {', '.join(analytics.LINES)}"}
    d0, d1 = _window(days)
    o = analytics.overview(analytics.resolve_filters(d0, d1, None, None, None, None))
    rows = [l for l in o["charts"]["line_comparison"] if l["line_id"] in (line_a, line_b)]
    return {"window": [d0, d1], "lines": rows,
            "table": {"title": f"{line_a} vs {line_b}", "columns": ["Line", "Target", "Actual", "Attainment %", "OEE %", "Downtime min", "Fail rate %"],
                      "rows": [[r["line_id"], r["target"], r["actual"], _f(r["efficiency_pct"]), _f(r["oee_pct"]), r["downtime_min"], _f(r["fail_rate_pct"], 2)] for r in rows]},
            "chart": {"type": "bar", "title": "Plan attainment and OEE", "x": "line_id", "series": ["efficiency_pct", "oee_pct"],
                      "data": [{"line_id": r["line_id"], "efficiency_pct": _f(r["efficiency_pct"]), "oee_pct": _f(r["oee_pct"])} for r in rows]}}


@tool
def get_quality_issues(days: int = 7) -> dict:
    """Quality over the last N days of data: inspection fail rate per line, defect Pareto (codes + names) and recorded NCR root-cause categories."""
    d0, d1 = _window(days)
    o = analytics.overview(analytics.resolve_filters(d0, d1, None, None, None, None))
    ncr = query("SELECT n.root_cause_category, count(*) AS n FROM twin.fact_ncr_capa n WHERE n.opened_ts >= CAST(:d AS date) - 90 GROUP BY 1 ORDER BY 2 DESC", d=d1)
    return {"window": [d0, d1], "pass_rate_pct": _f(o["kpis"]["quality_pass_rate_pct"], 2), "failed_inspections": o["kpis"]["total_defects"],
            "fail_rate_by_line": {l["line_id"]: _f(l["fail_rate_pct"], 2) for l in o["charts"]["line_comparison"]},
            "ncr_root_causes_90d": ncr, "note": "Pass rate counts inspections, not units. NCR root causes are as recorded by quality engineers.",
            "table": {"title": "Defect Pareto", "columns": ["Code", "Defect", "Category", "Failed inspections"],
                      "rows": [[d["defect_code"], d["defect_name"], d["category"], d["fails"]] for d in o["charts"]["defect_pareto"]]},
            "chart": {"type": "bar", "title": "Failed inspections by defect", "x": "defect_code", "series": ["fails"], "data": o["charts"]["defect_pareto"]}}


@tool
def get_agent_findings(category: Optional[str] = None, limit: int = 10) -> dict:
    """Ranked findings of the latest completed Digital Twin Agent run (category optional: MACHINE_HEALTH, ANOMALY, DOWNTIME, MAINTENANCE, QUALITY,
    PRODUCTION, BOTTLENECK, DATA_QUALITY). Includes bottlenecks (PREDICTED by the calibrated twin) and maintenance attention."""
    rid = analytics.latest_completed_run_id()
    if not rid:
        return {"error": "No completed agent run yet."}
    rows = query("SELECT rank, category, severity, machine_id, line_id, station_id, title, explanation, value_label FROM twin.agent_finding "
                 "WHERE run_id = CAST(:r AS uuid) AND (CAST(:c AS text) IS NULL OR category = :c) ORDER BY rank LIMIT :n",
                 r=rid, c=(category or "").upper() or None, n=max(1, min(int(limit or 10), 30)))
    return {"run_id": rid, "findings": rows,
            "table": {"title": "Agent findings", "columns": ["#", "Severity", "Category", "Asset", "Finding", "Label"],
                      "rows": [[r["rank"], r["severity"], r["category"], r["machine_id"] or r["line_id"] or "", r["title"], r["value_label"]] for r in rows]}}


@tool
def get_pending_recommendations() -> dict:
    """Recommendations waiting for a human decision (PENDING_REVIEW), highest risk first. The agent cannot approve them."""
    rows = query("SELECT rec_id, risk_level, category, machine_id, line_id, issue, action, created_at FROM twin.recommendation WHERE status = 'PENDING_REVIEW' "
                 "ORDER BY CASE risk_level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, created_at DESC LIMIT 30")
    return {"count": len(rows), "note": "Only a planner can approve, reject or request changes (Reports & Approval page).",
            "table": {"title": "Pending approval", "columns": ["Risk", "Asset", "Issue", "Recommended action"],
                      "rows": [[r["risk_level"], r["machine_id"] or r["line_id"] or "", r["issue"], r["action"]] for r in rows]}}


@tool
def get_latest_agent_run() -> dict:
    """Summary of the latest Digital Twin Agent execution: status, timing, records processed, step outcomes, warnings, counts of findings/recommendations."""
    r = query("SELECT run_id, status, triggered_by, started_at, finished_at, records_processed, data_as_of, summary, error FROM twin.agent_run ORDER BY started_at DESC LIMIT 1")
    if not r:
        return {"error": "The agent has never run."}
    r = r[0]
    steps = query("SELECT DISTINCT ON (step) step, status, message FROM twin.agent_run_event WHERE run_id = :i AND kind = 'STEP' ORDER BY step, id DESC", i=r["run_id"])
    warns = query("SELECT step, message FROM twin.agent_run_event WHERE run_id = :i AND kind IN ('WARN', 'ERROR') ORDER BY id", i=r["run_id"])
    s = r["summary"] or {}
    return {"run_id": str(r["run_id"]), "status": r["status"], "triggered_by": r["triggered_by"], "started_at": str(r["started_at"]), "finished_at": str(r["finished_at"]),
            "records_processed": r["records_processed"], "data_as_of": str(r["data_as_of"]), "error": r["error"],
            "findings": s.get("findings"), "recommendations": s.get("recommendations"), "reports": s.get("reports"), "status_counts": s.get("status_counts"),
            "top_findings": s.get("top_findings"), "warnings": [w["message"] for w in warns],
            "table": {"title": "Steps", "columns": ["Step", "Status", "Name"], "rows": [[x["step"], x["status"], x["message"]] for x in steps]}}


FACTORY_TOOLS = [get_factory_status, get_machine_health, get_abnormal_machines, get_production_performance, compare_lines, get_quality_issues,
                 get_agent_findings, get_pending_recommendations, get_latest_agent_run]


# ------------------------------------------------------------------------------------------------ deterministic fallback (no LLM)
def route(message: str) -> tuple[str, dict]:
    """Keyword intent router used when the LLM is unavailable. Returns (tool name, args)."""
    m = message.lower()
    mid = MID.search(message)
    lines = re.findall(r"\b(smt|fal)[- ]?l?([12])\b", m)
    days = int(x.group(1)) if (x := re.search(r"(\d{1,2})\s*day", m)) else (7 if "week" in m or "seven" in m else 7)
    if mid:
        return "get_machine_health", {"machine_id": mid.group(0).upper()}
    if "compare" in m and len(lines) >= 2:
        a, b = (f"{p.upper()}-L{n}" for p, n in lines[:2])
        return "compare_lines", {"line_a": a, "line_b": b, "days": days}
    if any(w in m for w in ("pending", "approval", "approve")):
        return "get_pending_recommendations", {}
    if any(w in m for w in ("abnormal", "anomal", "sensor")):
        return "get_abnormal_machines", {}
    if any(w in m for w in ("quality", "defect", "inspection", "ncr")):
        return "get_quality_issues", {"days": days}
    if any(w in m for w in ("bottleneck",)):
        return "get_agent_findings", {"category": "BOTTLENECK"}
    if any(w in m for w in ("maintenance", "health", "risk")):
        return "get_machine_health", {}
    if any(w in m for w in ("production", "output", "performance", "oee")):
        return "get_production_performance", {"days": days, "line_id": f"{lines[0][0].upper()}-L{lines[0][1]}" if lines else None}
    if any(w in m for w in ("latest", "last run", "execution", "summar")):
        return "get_latest_agent_run", {}
    return "get_factory_status", {}


def fallback_answer(tool_name: str, out: dict) -> str:
    if "error" in out:
        return out["error"]
    if tool_name == "get_factory_status":
        mc, p, q = out["machines"], out["production"], out["quality"]
        return (f"Factory status for {out['window'][0][:10]} to {out['window'][1][:10]} (historical export, data as of {out['data_as_of']}): "
                f"{mc['active']} of {mc['total']} machines running, {mc['offline']} offline, {mc['under_maintenance']} under maintenance. "
                f"Production {p['actual']:,.0f} of {p['target']:,.0f} target ({p['efficiency_pct']}% plan attainment, OEE {p['oee_pct']}%, CALCULATED). "
                f"Quality pass rate {q['pass_rate_pct']}% of {q['inspections']} inspections. {out['pending_recommendations']} recommendations await approval.")
    if tool_name == "get_machine_health" and "machine_id" in out:
        f = out["findings"][0]["explanation"] if out["findings"] else "No finding was raised for this machine in the latest run."
        a = out["recommendations"][0]["action"] if out["recommendations"] else "No recommendation."
        return f"{out['machine_id']} ({out['name']}) is {out['status']} with health {out['health_score']} (rule-based index). {f} Recommended: {a}"
    if tool_name == "get_pending_recommendations":
        return f"{out['count']} recommendations are pending approval. {out['note']}"
    if tool_name == "get_latest_agent_run":
        return (f"Latest run {out['run_id'][:8]} {out['status']}: {out['records_processed']:,} records processed, {out['findings']} findings, "
                f"{out['recommendations']} recommendations, data as of {out['data_as_of']}.")
    if tool_name == "compare_lines":
        return "Comparison: " + "; ".join(f"{l['line_id']} attainment {_f(l['efficiency_pct'])}%, OEE {_f(l['oee_pct'])}%, downtime {l['downtime_min']:.0f} min" for l in out["lines"]) + "."
    if tool_name == "get_production_performance":
        return f"Production {out['window'][0]} to {out['window'][1]}: {out['total']['total_production']:,.0f} of {out['total']['production_target']:,.0f} target, plan attainment {out['plan_attainment_pct']}%, OEE {out['oee_pct']}%."
    if tool_name == "get_quality_issues":
        top = out["table"]["rows"][:3]
        return f"Quality pass rate {out['pass_rate_pct']}% ({out['failed_inspections']} failed inspections). Top defects: " + ", ".join(f"{r[0]} {r[1]} ({r[3]})" for r in top) + "."
    return "Here is the data I found."


FOLLOW_UPS = {
    "get_factory_status": ["Which machines need maintenance attention?", "What are the major quality issues?", "Summarize the latest Digital Twin Agent execution."],
    "get_machine_health": ["Which machine has abnormal sensor readings?", "What recommendations are pending approval?"],
    "get_abnormal_machines": ["Explain why M-SPI-02 is showing a warning.", "Which machines need maintenance attention?"],
    "get_production_performance": ["Compare SMT-L1 and SMT-L2.", "What are the main production bottlenecks?"],
    "compare_lines": ["Show production performance for the last seven days.", "What are the major quality issues?"],
    "get_quality_issues": ["Compare FAL-L1 and FAL-L2.", "What recommendations are pending approval?"],
    "get_agent_findings": ["What recommendations are pending approval?", "Which machine has abnormal sensor readings?"],
    "get_pending_recommendations": ["Summarize the latest Digital Twin Agent execution.", "What is the current factory status?"],
    "get_latest_agent_run": ["What recommendations are pending approval?", "Which machines need maintenance attention?"],
}
