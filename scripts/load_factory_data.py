"""Load plant-context tables from data_factory/ into Neon (DIRECT URL), one transaction, truncate + COPY per table (idempotent).
Tables are loaded AS SUPPLIED. Row counts are checked against the CSVs before commit. Cross-reference problems are REPORTED
(never dropped or corrected silently), see docs/DATA_QUALITY.md. Run after `make db-init` (needs migration V003)."""
import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402
from app.db import direct_connection  # noqa: E402

SRC = settings.data_dir.parent / "data_factory"
TABLES = [  # table = csv stem
    "dim_production_line", "dim_workforce", "fact_machine_telemetry_hourly", "fact_downtime_event", "fact_maintenance_work_order",
    "fact_quality_inspection", "fact_inventory_daily_snapshot", "fact_shipment_milestone", "fact_energy_meter_hourly", "fact_workforce_attendance",
    "dim_defect_code", "fact_ncr_capa",
]


def load_table(conn, table: str) -> int:
    df = pd.read_csv(SRC / f"{table}.csv")
    cols = ", ".join(f'"{c}"' for c in df.columns)
    conn.execute(f"TRUNCATE twin.{table}")
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    with conn.cursor().copy(f"COPY twin.{table} ({cols}) FROM STDIN WITH (FORMAT csv)") as cp:
        cp.write(buf.getvalue())
    got = conn.execute(f"SELECT count(*) FROM twin.{table}").fetchone()[0]
    assert got == len(df), f"{table}: {got} rows in DB, {len(df)} in CSV"
    print(f"loaded {table}: {got} rows")
    return got


def main():
    if not SRC.is_dir():
        sys.exit(f"FATAL: {SRC} not found")
    with direct_connection() as conn:
        for t in TABLES:
            load_table(conn, t)
        # reported, never fixed: cross-references that disagree between the supplied files
        checks = {
            "telemetry machine_id not in dim_machine": "SELECT count(*) FROM twin.fact_machine_telemetry_hourly t LEFT JOIN twin.dim_machine m USING (machine_id) WHERE m.machine_id IS NULL",
            "downtime machine_id not in dim_machine": "SELECT count(*) FROM twin.fact_downtime_event t LEFT JOIN twin.dim_machine m USING (machine_id) WHERE m.machine_id IS NULL",
            "work order machine_id not in dim_machine": "SELECT count(*) FROM twin.fact_maintenance_work_order t LEFT JOIN twin.dim_machine m USING (machine_id) WHERE m.machine_id IS NULL",
            "NCR defect_code not in dim_defect_code": "SELECT count(*) FROM twin.fact_ncr_capa n LEFT JOIN twin.dim_defect_code d USING (defect_code) WHERE d.defect_code IS NULL",
            "NCR inspection_id not in quality inspections": "SELECT count(*) FROM twin.fact_ncr_capa n LEFT JOIN twin.fact_quality_inspection q USING (inspection_id) WHERE q.inspection_id IS NULL",
            "quality station_id not in dim_station": "SELECT count(*) FROM twin.fact_quality_inspection t LEFT JOIN twin.dim_station s USING (station_id) WHERE s.station_id IS NULL",
            "telemetry line differs from dim_machine line": "SELECT count(*) FROM twin.fact_machine_telemetry_hourly t JOIN twin.dim_machine m USING (machine_id) WHERE t.line_id <> m.line_id",
            "telemetry plant differs from dim_machine plant": "SELECT count(*) FROM twin.fact_machine_telemetry_hourly t JOIN twin.dim_machine m USING (machine_id) WHERE t.plant_id <> m.plant_id",
            "downtime plant differs from dim_machine plant": "SELECT count(*) FROM twin.fact_downtime_event t JOIN twin.dim_machine m USING (machine_id) WHERE t.plant_id <> m.plant_id",
        }
        for name, q in checks.items():
            n = conn.execute(q).fetchone()[0]
            print(f"{'ok     ' if n == 0 else 'REPORT '} {name}: {n}")
        conn.commit()
    print("factory-context load OK (committed)")


if __name__ == "__main__":
    main()
