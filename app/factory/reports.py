"""Report generation (7 categories) and export to PDF (reportlab) and Excel (openpyxl). Content comes only from the agent run's
computed metrics, findings and recommendations; every table states whether its values are ACTUAL, CALCULATED or PREDICTED."""
from __future__ import annotations

import io
from datetime import datetime

CATEGORIES = {
    "FACTORY_OVERVIEW": ("Factory Overview", "Factory management"),
    "MACHINE_HEALTH": ("Machine Health", "Maintenance planners"),
    "PRODUCTION_PERFORMANCE": ("Production Performance", "Production managers"),
    "QUALITY_ANALYSIS": ("Quality Analysis", "Quality team"),
    "ANOMALY_DETECTION": ("Anomaly Detection", "Maintenance and process engineering"),
    "ROOT_CAUSE": ("Root Cause Analysis", "Process engineering"),
    "MAINTENANCE_RECOMMENDATIONS": ("Maintenance Recommendations", "Maintenance planners"),
}
SEV_RANK = {"CRITICAL": 4, "OFFLINE": 3, "ATTENTION": 3, "WARNING": 2, "NORMAL": 1}
PRIO = {4: "CRITICAL", 3: "HIGH", 2: "MEDIUM", 1: "LOW", 0: "LOW"}


def _prio(findings: list[dict]) -> str:
    return PRIO[max((SEV_RANK.get(f["severity"], 0) for f in findings), default=0)]


def _fin(f: dict) -> dict:
    return {k: f.get(k) for k in ("rank", "category", "severity", "machine_id", "line_id", "station_id", "title", "explanation", "value_label", "root_causes")}


def _rec(r: dict) -> dict:
    return {k: r.get(k) for k in ("category", "machine_id", "line_id", "issue", "risk_level", "action", "expected_impact", "confidence")}


def build(run_id: str, short: str, as_of: datetime, window: dict, machines: dict, scores: dict, lines: dict, findings: list[dict],
          recs: list[dict], narrative: str | None) -> list[dict]:
    """-> report rows ready to insert. machines/scores keyed by machine_id; lines keyed by line_id (line metrics from the run)."""
    win = f"{window['start'][:10]} to {window['end'][:10]}"
    base = {"run_id": run_id, "data_as_of": as_of.isoformat(), "window": win,
            "labels": "ACTUAL = as supplied; CALCULATED = derived by a stated rule; PREDICTED = validated twin model output. Historical export, not a live stream."}
    mrows = [[mid, m["machine_name"], m["line_id"], scores[mid]["status"], scores[mid]["health_score"], scores[mid]["risk_score"],
              round(m["utilization_pct"] or 0, 1), m["anomalies"], m["state_hours"]["DOWN"], round(m["downtime"]["minutes"] or 0),
              (m["maintenance"]["last_pm"] or "")[:10]] for mid, m in sorted(machines.items(), key=lambda kv: -scores[kv[0]]["risk_score"])]
    mcols = ["Machine", "Name", "Line", "Status", "Health", "Risk index", "Utilisation %", "Anomalies", "DOWN h", "Downtime event min", "Last PM"]
    lrows = [[lid, l["target"], l["actual"], l["good"], round(l["attainment_pct"] or 0, 1), round(l["oee_pct"] or 0, 1), l["downtime_min"],
              round(l["fail_rate_pct"] or 0, 2), l["top_downtime_reason"] or ""] for lid, l in sorted(lines.items())]
    lcols = ["Line", "Target", "Actual", "Good", "Plan attainment %", "OEE %", "Downtime min", "Inspection fail %", "Top downtime reason"]
    by_cat = lambda *cats: [f for f in findings if f["category"] in cats]
    out = []

    def add(cat: str, scope: str, fs: list[dict], rs: list[dict], tables: list[dict], kpis: list[dict], summary: str):
        title, audience = CATEGORIES[cat]
        out.append({"report_id": f"RPT-{short}-{cat[:3]}{list(CATEGORIES).index(cat) + 1}", "category": cat, "title": f"{title} ({win})",
                    "audience": audience, "scope": scope, "priority": _prio(fs), "content": {**base, "summary": summary, "kpis": kpis,
                    "findings": [_fin(f) for f in fs], "recommendations": [_rec(r) for r in rs], "tables": tables,
                    "narrative": narrative if cat == "FACTORY_OVERVIEW" else None}})

    tot = {k: sum(l[k] or 0 for l in lines.values()) for k in ("target", "actual", "good", "downtime_min")}
    status_counts = {s: sum(1 for x in scores.values() if x["status"] == s) for s in ("NORMAL", "WARNING", "ATTENTION", "CRITICAL", "OFFLINE")}
    fk = [{"label": "Production (actual)", "value": tot["actual"], "unit": "units", "value_label": "ACTUAL"},
          {"label": "Plan attainment", "value": round(tot["actual"] / tot["target"] * 100, 1) if tot["target"] else None, "unit": "%", "value_label": "CALCULATED"},
          {"label": "OEE", "value": round(tot["good"] / tot["target"] * 100, 1) if tot["target"] else None, "unit": "%", "value_label": "CALCULATED"},
          {"label": "Line downtime", "value": tot["downtime_min"], "unit": "min", "value_label": "ACTUAL"},
          *[{"label": f"Machines {s.lower()}", "value": n, "unit": "", "value_label": "CALCULATED"} for s, n in status_counts.items()]]
    add("FACTORY_OVERVIEW", "All lines, both plants", findings[:10], recs[:10], [{"title": "Lines (ACTUAL / CALCULATED)", "columns": lcols, "rows": lrows}], fk,
        f"{len(findings)} findings and {len(recs)} recommendations from {len(machines)} machines and {len(lines)} lines over {win}.")
    mf = by_cat("MACHINE_HEALTH", "ANOMALY", "DOWNTIME", "MAINTENANCE")
    add("MACHINE_HEALTH", "All machines", mf, [r for r in recs if r.get("machine_id")],
        [{"title": "Machine health (rule-based index, CALCULATED)", "columns": mcols, "rows": mrows}],
        [{"label": f"Machines {s.lower()}", "value": n, "unit": "", "value_label": "CALCULATED"} for s, n in status_counts.items()],
        f"Health index per machine from anomalies, sensor excursions, DOWN hours, downtime events, preventive maintenance and station quality. Not a trained model.")
    pf = by_cat("PRODUCTION", "BOTTLENECK")
    add("PRODUCTION_PERFORMANCE", "All lines", pf, [r for r in recs if r["category"] in ("PRODUCTION", "BOTTLENECK")],
        [{"title": "Line performance (ACTUAL / CALCULATED)", "columns": lcols, "rows": lrows}], fk[:4],
        "Plan attainment, OEE and downtime per line from MES hourly records; bottleneck stations from the calibrated twin (SIMULATED baseline).")
    qf = by_cat("QUALITY")
    qrows = [[lid, l["inspections"], l["fails"], round(l["fail_rate_pct"] or 0, 2), "; ".join(f"{d['defect_code']} {d['defect_name']} ({d['fails']})" for d in l["top_defects"][:3]),
              "; ".join(f"{c['root_cause_category']} ({c['n']})" for c in l["ncr_root_causes"][:3])] for lid, l in sorted(lines.items())]
    add("QUALITY_ANALYSIS", "All lines", qf, [r for r in recs if r["category"] == "QUALITY"],
        [{"title": "Quality by line (CALCULATED; NCR root causes ACTUAL as recorded)", "columns": ["Line", "Inspections", "Fails", "Fail %", "Top defects", "NCR root causes (90 d)"], "rows": qrows}],
        [{"label": "Inspections", "value": sum(l["inspections"] for l in lines.values()), "unit": "", "value_label": "ACTUAL"},
         {"label": "Failed inspections", "value": sum(l["fails"] for l in lines.values()), "unit": "", "value_label": "ACTUAL"}],
        "Inspection fail rates, defect Pareto and recorded NCR root-cause categories.")
    arows = [[mid, m["anomalies"], m["sensor_excursions"]["temperature"], m["sensor_excursions"]["vibration"], m["sensor_excursions"]["motor_current"],
              round(m["temp_max"] or 0, 1), round(m["vib_max"] or 0, 2), round(m["temp_slope_per_day"] or 0, 3)] for mid, m in machines.items() if m["anomalies"] or sum(m["sensor_excursions"].values())]
    add("ANOMALY_DETECTION", "Machines with anomalies or sensor excursions", by_cat("ANOMALY", "MACHINE_HEALTH"), [],
        [{"title": "Anomalies (ACTUAL flag) and excursions (CALCULATED vs own baseline)", "columns": ["Machine", "Anomaly flags", "Temp exc h", "Vib exc h", "Current exc h", "Temp max C", "Vib max mm/s", "Temp trend C/day"], "rows": arows}],
        [{"label": "Anomaly flags", "value": sum(m["anomalies"] for m in machines.values()), "unit": "", "value_label": "ACTUAL"}],
        "Hourly readings flagged is_anomaly in the source, and readings above each machine's own baseline mean + k sigma.")
    rc = [[f.get("machine_id") or f.get("line_id") or "", f["title"], "; ".join(f"{c['cause']} ({c['basis']})" for c in f["root_causes"][:3])] for f in findings if f["root_causes"]]
    add("ROOT_CAUSE", "Findings with recorded causes", [f for f in findings if f["root_causes"]], [],
        [{"title": "Possible causes ranked from recorded history (ACTUAL), not inferred", "columns": ["Asset", "Finding", "Possible causes"], "rows": rc}], [],
        "Causes come from recorded corrective work-order failure modes, downtime reasons and NCR root-cause categories; the agent ranks them, it does not invent them.")
    mr = [r for r in recs if r["category"] in ("MACHINE_HEALTH", "ANOMALY", "DOWNTIME", "MAINTENANCE")]
    add("MAINTENANCE_RECOMMENDATIONS", "Machines", by_cat("MACHINE_HEALTH", "MAINTENANCE"), mr,
        [{"title": "Recommended maintenance actions (need planner approval)", "columns": ["Machine", "Risk", "Action", "Expected impact"],
          "rows": [[r.get("machine_id") or "", r["risk_level"], r["action"], r.get("expected_impact") or ""] for r in mr]}], [],
        "No work order is created until an authorised planner approves the recommendation.")
    return out


# ------------------------------------------------------------------------------------------------ exports
def to_pdf(rep: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    c = rep["content"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=rep["title"], author="Manufacturing Digital Twin Agent")
    ss = getSampleStyleSheet()
    small = ss["BodyText"].clone("small", fontSize=8, leading=10)
    esc = lambda s: (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    el = [Paragraph(esc(rep["title"]), ss["Title"]),
          Paragraph(f"Report {esc(rep['report_id'])} | audience: {esc(rep['audience'])} | priority: {esc(rep['priority'])} | status: {esc(rep['status'])} | "
                    f"generated by {esc(rep['generated_by'])} | data as of {esc(c['data_as_of'])}", small),
          Paragraph(esc(c["labels"]), small), Spacer(1, 6), Paragraph(esc(c["summary"]), ss["BodyText"])]
    if c.get("narrative"):
        el += [Spacer(1, 4), Paragraph("<b>Agent summary</b> (LLM text written from the findings below; numbers are from the findings)", small), Paragraph(esc(c["narrative"]), ss["BodyText"])]
    navy = colors.HexColor("#1f2f59")      # PDF output only (theme tokens do not apply to generated files)

    def table(cols, rows, widths=None):
        data = [[Paragraph(f"<b>{esc(h)}</b>", small) for h in cols]] + [[Paragraph(esc(v), small) for v in r] for r in rows]
        t = Table(data, repeatRows=1, colWidths=widths)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        t.setStyle(TableStyle([("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]))
        return t
    if c.get("kpis"):
        el += [Spacer(1, 6), table(["KPI", "Value", "Label"], [[k["label"], f"{k['value']} {k['unit']}".strip(), k["value_label"]] for k in c["kpis"]])]
    if c.get("findings"):
        el += [Spacer(1, 8), Paragraph("Findings", ss["Heading3"]),
               table(["#", "Severity", "Asset", "Finding", "Explanation", "Label"],
                     [[f.get("rank"), f["severity"], f.get("machine_id") or f.get("line_id") or "", f["title"], f["explanation"], f["value_label"]] for f in c["findings"]],
                     [10 * mm, 22 * mm, 24 * mm, 58 * mm, 124 * mm, 30 * mm])]
    if c.get("recommendations"):
        el += [Spacer(1, 8), Paragraph("Recommendations (approval required; no action before a planner approves)", ss["Heading3"]),
               table(["Risk", "Asset", "Issue", "Action", "Expected impact"],
                     [[r["risk_level"], r.get("machine_id") or r.get("line_id") or "", r["issue"], r["action"], r.get("expected_impact") or "n/a"] for r in c["recommendations"]],
                     [20 * mm, 24 * mm, 60 * mm, 100 * mm, 64 * mm])]
    for t in c.get("tables", []):
        if t["rows"]:
            el += [Spacer(1, 8), Paragraph(esc(t["title"]), ss["Heading3"]), table(t["columns"], t["rows"])]
    doc.build(el)
    return buf.getvalue()


def to_xlsx(rep: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    c = rep["content"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    for row in [["Report", rep["report_id"]], ["Title", rep["title"]], ["Audience", rep["audience"]], ["Priority", rep["priority"]],
                ["Status", rep["status"]], ["Generated by", rep["generated_by"]], ["Data as of", c["data_as_of"]], ["Labels", c["labels"]],
                ["Summary", c["summary"]], ["Agent summary (LLM, from findings)", c.get("narrative") or ""]]:
        ws.append(row)
    ws.append([])
    ws.append(["KPI", "Value", "Unit", "Label"])
    for k in c.get("kpis", []):
        ws.append([k["label"], k["value"], k["unit"], k["value_label"]])

    def sheet(name, cols, rows):
        s = wb.create_sheet(name[:31])
        s.append(cols)
        for cell in s[1]:
            cell.font = Font(bold=True)
        for r in rows:
            s.append([("; ".join(f"{x.get('cause')}: {x.get('basis')}" for x in v) if isinstance(v, list) else v) for v in r])
    sheet("Findings", ["Rank", "Category", "Severity", "Machine", "Line", "Title", "Explanation", "Label", "Possible causes"],
          [[f.get("rank"), f["category"], f["severity"], f.get("machine_id"), f.get("line_id"), f["title"], f["explanation"], f["value_label"], f.get("root_causes") or []]
           for f in c.get("findings", [])])
    sheet("Recommendations", ["Category", "Machine", "Line", "Issue", "Risk", "Action", "Expected impact", "Confidence"],
          [[r["category"], r.get("machine_id"), r.get("line_id"), r["issue"], r["risk_level"], r["action"], r.get("expected_impact"), r.get("confidence")]
           for r in c.get("recommendations", [])])
    for i, t in enumerate(c.get("tables", []), 1):
        sheet(f"Table {i}", t["columns"], t["rows"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def email_text(rep: dict) -> tuple[str, str]:
    c = rep["content"]
    lines = [f"{rep['title']}", f"Report {rep['report_id']} | priority {rep['priority']} | for {rep['audience']}", f"Data as of {c['data_as_of']} (historical export).", "",
             c["summary"], ""]
    if c.get("findings"):
        lines.append("Top findings:")
        lines += [f"  {f.get('rank')}. [{f['severity']}] {f['title']}" for f in c["findings"][:8]]
        lines.append("")
    if c.get("recommendations"):
        lines.append("Recommendations awaiting approval (no action is taken before an authorised planner approves):")
        lines += [f"  - [{r['risk_level']}] {r.get('machine_id') or r.get('line_id') or ''}: {r['action']}" for r in c["recommendations"][:8]]
        lines.append("")
    lines.append("Sent by the Manufacturing Digital Twin Agent. Values are labelled ACTUAL, CALCULATED or PREDICTED in the full report.")
    return f"[Digital Twin] {rep['title']} - priority {rep['priority']}", "\n".join(lines)
