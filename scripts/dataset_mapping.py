"""Generate docs/DATASET_MAPPING.md from the supplied CSVs (data/ and data_factory/) and the Neon schema.
Every count, field list, type and time range is computed here; only the descriptions and use cases are written by hand.
Run from backend/:  python scripts/dataset_mapping.py   (DB part is skipped when NEON_DATABASE_URL is not set)."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402

ROOT = settings.data_dir.parent
SRC = ROOT / "data_factory"
OUT = settings.docs_dir / "DATASET_MAPPING.md"

# dataset -> (source system, primary key, relationships, description, use cases)
META: dict[str, tuple[str, str, str, str, str]] = {
    "dim_plant": ("ERP master", "plant_id", "plant_id -> all plant-level facts", "The two plants (country, state, timezone, currency).", "Plant scoping, filters."),
    "dim_production_line": ("MES master", "line_id", "plant_id -> dim_plant", "4 lines with type, nominal capacity per hour, takt time and area.", "Capacity, plan attainment, line comparison."),
    "dim_station": ("MES master", "station_id", "station_id -> routing, quality inspections", "21 process stations with process area, sequence and critical control point flag.", "Twin graph, 3D layout zones, quality by station."),
    "dim_machine": ("CMMS / asset register", "machine_id", "line_id -> dim_production_line; plant_id -> dim_plant (2 rows disagree with their line, DQ)", "15 machines: make/model, type, line, criticality, commissioning date, PM interval.", "Machine registry, 3D machine objects, PM due dates."),
    "dim_product": ("PLM / ERP master", "product_id", "product_id -> production, routing, BOM", "3 smartphone models with spec, revision, standard cycle and cost.", "Product mix, cost."),
    "dim_product_routing": ("MES / engineering", "routing_id", "product_id, station_id", "Operation sequence per product with standard cycle seconds and setup minutes.", "Twin simulation parameters."),
    "dim_shift_calendar": ("Workforce / MES", "calendar_date + shift_id", "shift_id -> production, attendance", "Shift windows (A/B/C), planned and break minutes per day.", "Shift filter, planned time."),
    "dim_workforce": ("Workforce (HR)", "employee_id", "employee_id -> attendance, work orders (technician_id)", "180 employees: team, role, skill grade, certification, home shift.", "Staffing context."),
    "dim_defect_code": ("QMS master", "defect_code", "defect_code -> quality inspections, NCR/CAPA", "12 defect codes with category, severity and default disposition.", "Defect Pareto, quality root cause."),
    "dim_material": ("ERP master", "material_id", "material_id -> BOM, inventory, purchasing", "18 materials with price, lead time, safety stock.", "Material constraints (not used by the agent yet)."),
    "dim_supplier": ("ERP master", "supplier_id", "supplier_id -> purchasing, genealogy", "10 suppliers with approval status and risk tier.", "Supplier quality context (not used by the agent yet)."),
    "dim_customer": ("ERP / CRM master", "customer_id", "customer_id -> customer orders", "4 customers with segment and service level.", "Order context (not used by the agent yet)."),
    "fact_hourly_production": ("MES", "event_id", "line_id, plant_id, shift_id, product_id, production_order_id", "Hourly target, actual, good, reject, rework quantity and downtime minutes with reason per line.", "Production trend, target vs actual, OEE, downtime, line comparison, twin calibration."),
    "mart_daily_line_shift_kpi": ("BI mart", "event_date + line_id + shift_id + product_id", "derived from MES hourly production", "Daily KPI mart: plan attainment, FPY, availability, performance, quality, OEE per line and shift.", "Cross-check of calculated OEE (the app computes OEE from MES hourly rows)."),
    "fact_machine_telemetry_hourly": ("SCADA / IoT historian (export)", "telemetry_id", "machine_id -> dim_machine; line/plant as supplied (some disagree, DQ)", "Hourly temperature, vibration, motor current, pressure, rpm, machine state, alarm code, sensor health and an is_anomaly flag per machine.", "Machine condition, status colours in 3D, anomaly detection, risk index, sensor trends."),
    "fact_downtime_event": ("MES / CMMS", "downtime_id", "machine_id -> dim_machine", "Machine downtime events with duration, reason, impact units, maintenance flag and closure status.", "Downtime analysis, machine risk, root cause."),
    "fact_maintenance_work_order": ("CMMS", "work_order_id", "machine_id -> dim_machine; technician_id -> dim_workforce", "Corrective, preventive and predictive work orders with failure mode, priority, dates and status.", "Last maintenance, PM overdue, failure modes, maintenance recommendations."),
    "fact_quality_inspection": ("QMS / MES", "inspection_id", "station_id -> dim_station; defect_code -> dim_defect_code; serial_number", "Inspection results by stage with defect code, severity, measurement vs spec and disposition.", "Quality pass rate, defect trend, quality analysis."),
    "fact_ncr_capa": ("QMS", "ncr_id", "inspection_id -> fact_quality_inspection; defect_code", "Non-conformance reports with root-cause category, containment and CAPA status.", "Quality root cause (recorded categories)."),
    "fact_vision_inspection": ("Vision AI system", "image_record_id", "inspection_id -> fact_quality_inspection", "Vision model predictions with confidence and human review label.", "Not used yet (would support vision model accuracy)."),
    "fact_energy_meter_hourly": ("Energy management / BMS", "meter_reading_id", "plant_id, area", "Hourly kWh, demand, power factor, tariff, cost and CO2e per meter and area.", "Energy consumption per area (not per machine: no machine key)."),
    "fact_utility_telemetry_hourly": ("BMS / utilities", "utility_reading_id", "plant_id, zone, equipment_id", "Compressed air, chilled water and other utilities: flow, pressure, temperature, status.", "Not used yet."),
    "fact_workforce_attendance": ("Workforce (T&A)", "attendance_id", "employee_id -> dim_workforce; shift_id", "Daily attendance with hours, overtime, skill match and training compliance.", "Staffing context in the 3D view."),
    "fact_inventory_daily_snapshot": ("ERP / WMS", "snapshot_date + plant + material + warehouse + lot", "material_id -> dim_material", "Daily stock by lot: on hand, allocated, blocked, quality hold, in transit.", "Raw material and FG stock in the 3D warehouse zones."),
    "fact_shipment_milestone": ("TMS / ERP", "shipment_id", "customer_order_id -> customer orders", "Shipments with carrier, ETA, delivery, quantity and status.", "Dispatch context."),
    "fact_production_order": ("ERP / MES", "production_order_id", "customer_order_id, product_id", "Production orders with quantity, plan dates, due date and status.", "Not used yet."),
    "fact_customer_order": ("ERP", "customer_order_id", "customer_id, product_id", "Customer orders with requested/confirmed dates and status.", "Not used yet."),
    "fact_bom_component": ("PLM / ERP", "bom_id", "product_id, material_id", "Bill of materials per product revision.", "Not used yet."),
    "fact_manufacturing_cost": ("ERP (CO)", "cost_record_id", "production_order_id, line_id", "Cost postings by element per production order.", "Not used yet."),
    "fact_product_genealogy_event": ("MES traceability", "genealogy_event_id", "serial_number, material_lot_id, station_id", "Serial-level genealogy events (one column is empty in every row, DQ).", "Not used yet."),
    "fact_purchase_order_receipt": ("ERP (MM)", "po_id + po_line", "supplier_id, material_id", "Purchase orders and receipts.", "Not used yet."),
    "fact_rfq_quote": ("ERP (sourcing)", "rfq_id", "supplier_id, material_id", "Supplier quotes with scores.", "Not used yet."),
    "fact_engineering_change": ("PLM", "change_request_id", "product_id", "Engineering changes with cut-in dates and approvals.", "Not used yet."),
    "fact_ehs_event": ("EHS", "ehs_event_id", "plant_id, area", "Safety events with severity and action status.", "Not used yet."),
    "fact_executive_decision_log": ("BI / management", "decision_id", "plant_id", "Management KPI reviews with recommended actions.", "Not used (records by others, not agent output)."),
    "fact_digital_twin_scenario": ("Earlier twin (TWIN-1.7)", "scenario_id", "line_id, plant_id (158 rows disagree, DQ-02)", "Historical twin scenario predictions vs actual output.", "Comparison only (see MODEL_CARD)."),
}


def _kind(s: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_float_dtype(s):
        return "decimal"
    if s.name and any(k in s.name for k in ("_ts", "timestamp", "date", "_start", "_finish", "_eta", "due_")):
        parsed = pd.to_datetime(s, errors="coerce")
        if parsed.notna().mean() > 0.9:
            return "timestamp" if parsed.dt.time.astype(str).ne("00:00:00").any() else "date"
    return "text"


def _range(df: pd.DataFrame) -> str:
    for c in df.columns:
        if any(k in c for k in ("event_ts", "timestamp", "inspection_ts", "start_ts", "created_ts", "opened_ts", "posting_ts", "snapshot_date",
                                "work_date", "order_date", "po_date", "dispatch_date", "request_date", "review_date", "run_ts", "calendar_date", "event_date")):
            s = pd.to_datetime(df[c], errors="coerce")
            if s.notna().any():
                return f"{s.min():%Y-%m-%d} to {s.max():%Y-%m-%d} ({c})"
    return "no time column (master data)"


def db_tables() -> dict[str, int]:
    if not settings.neon_database_url:
        return {}
    from sqlalchemy import text
    from app.deps import get_engine
    out = {}
    with get_engine().connect() as c:
        names = [r[0] for r in c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'twin' AND table_type = 'BASE TABLE'"))]
        for n in names:
            out[n] = c.execute(text(f'SELECT count(*) FROM twin."{n}"')).scalar()
    return out


def main():
    loaded = db_tables()
    files = sorted(SRC.glob("*.csv"))
    lines = ["# Dataset mapping", "",
             "Generated by `backend/scripts/dataset_mapping.py` from the supplied files in `data_factory/` and the Neon schema `twin`. "
             "Record counts, field counts, types, null counts and time ranges are computed from the files; descriptions and use cases are written by hand. "
             "Nothing here is estimated.", "",
             "Labels used in the app: **ACTUAL** = a value as supplied in a dataset; **CALCULATED** = a metric derived from actual values by a stated formula; "
             "**PREDICTED** = output of a validated model (only the calibrated SimPy line twin qualifies today); **SIMULATED** = a what-if run of the twin. "
             "Every API response also carries `state_type` LIVE (from loaded plant data) or SIMULATED (twin).", "",
             "The supplied data is a historical export (1 Aug to 31 Oct 2026), not a live stream. The app anchors 'latest' on the last hour in the files and shows the data age.", "",
             "## Summary", "", "| Dataset | Source system | Records | Fields | Time range | In Neon | Used by the agent |", "|---|---|---:|---:|---|---|---|"]
    detail = []
    for f in files:
        name = f.stem
        df = pd.read_csv(f)
        src, pk, rel, desc, use = META.get(name, ("unknown", "?", "?", "", ""))
        in_db = loaded.get(name if name != "dim_production_line" else "dim_production_line")
        if name == "dim_plant":
            in_db = loaded.get("dim_plant")
        used = "yes" if in_db is not None and "not used" not in use.lower() else "no"
        lines.append(f"| `{name}` | {src} | {len(df):,} | {len(df.columns)} | {_range(df)} | {'yes (' + format(in_db, ',') + ' rows)' if in_db is not None else 'no (CSV only)'} | {used} |")
        cols = "\n".join(f"| `{c}` | {_kind(df[c])} | {int(df[c].isna().sum())} | {df[c].nunique()} | {', '.join(map(str, df[c].dropna().unique()[:3]))[:70]} |" for c in df.columns)
        detail += [f"### {name}", "", f"- **Source system:** {src}", f"- **Records:** {len(df):,}  **Fields:** {len(df.columns)}  **Duplicate rows:** {int(df.duplicated().sum())}",
                   f"- **Primary identifier:** `{pk}`", f"- **Relationships:** {rel}", f"- **Time range:** {_range(df)}", f"- **Description:** {desc}",
                   f"- **Manufacturing use cases:** {use}", "", "| Field | Type | Nulls | Distinct | Examples |", "|---|---|---:|---:|---|", cols, ""]
    lines += ["", "## Capability support", "",
              "| Capability | Status | Based on |", "|---|---|---|",
              "| Machine registry and 3D mapping | AVAILABLE | dim_machine (15), dim_station (21). Machines are placed at stations by machine type (assumption A-03): no machine-to-station key is supplied. |",
              "| Machine sensor readings | AVAILABLE (historical) | fact_machine_telemetry_hourly: temperature, vibration, motor current, pressure, rpm; hourly, all 15 machines. |",
              "| Energy per machine | UNAVAILABLE | fact_energy_meter_hourly is per area/meter, with no machine key. Shown per area only. |",
              "| Utilisation per machine | CALCULATED | share of telemetry hours in state RUNNING. |",
              "| Production count per machine | UNAVAILABLE | MES production is per line, not per machine. Line counts are shown instead. |",
              "| OEE | CALCULATED | from MES hourly rows (availability x performance x quality); mart OEE kept as cross-check. |",
              "| Quality pass rate, defects | CALCULATED | fact_quality_inspection (inspections, not units). |",
              "| Anomaly detection | ACTUAL + CALCULATED | is_anomaly flag as supplied, plus z-score of sensor values vs each machine's own history. |",
              "| Machine health / risk score | CALCULATED (rule-based index) | weighted rules over anomalies, sensor deviation, downtime, open events, PM overdue, station quality. Not a trained model. |",
              "| Failure prediction (time to failure) | UNAVAILABLE | no validated failure model; failure modes in work orders are not linked to telemetry windows. |",
              "| Throughput / bottleneck prediction | PREDICTED (validated) | calibrated SimPy line twin (TWIN-2.0), hold-out verdicts in docs/MODEL_CARD.md. |",
              "| Quality root cause | ACTUAL (recorded) | NCR root-cause categories as recorded by quality engineers; the agent ranks them, it does not infer causes. |",
              ""]
    OUT.write_text("\n".join(lines + ["## Datasets", ""] + detail), encoding="utf-8")
    print(f"wrote {OUT} ({len(files)} datasets, {len(loaded)} tables in Neon)")


if __name__ == "__main__":
    main()
