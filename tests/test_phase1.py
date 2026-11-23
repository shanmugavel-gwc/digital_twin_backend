"""Phase 1 offline tests (no database needed). DB-backed checks run inside load_data.py before commit."""
import pandas as pd
from app.config import settings
from app.twin import ref_config as rc

D = settings.data_dir


def test_csv_row_counts():
    for table, n in rc.EXPECTED_ROWS.items():
        assert len(pd.read_csv(D / f"{table}.csv")) == n, table


def test_every_station_maps_to_exactly_one_line_per_plant():
    stn = pd.read_csv(D / "dim_station.csv")
    for area in stn.process_area.unique():
        fam = rc.AREA_TO_FAMILY[area]
        lines = [l for l, f in rc.LINE_FAMILY.items() if f == fam]
        plants = {rc.LINE_PLANT[l] for l in lines}
        assert len(lines) == len(plants) == 2, area   # one line per plant


def test_all_machine_types_mapped_to_real_stations():
    m = pd.read_csv(D / "dim_machine.csv")
    stn = set(pd.read_csv(D / "dim_station.csv").station_id)
    for t in m.machine_type.unique():
        assert rc.MACHINE_TYPE_STATION[t] in stn, t


def test_hourly_facts_agree_with_line_plant_mapping():
    f = pd.read_csv(D / "fact_hourly_production.csv")
    assert all(rc.LINE_PLANT[l] == p for l, p in zip(f.line_id, f.plant_id))


def test_routing_pk_is_station_not_seq():
    r = pd.read_csv(D / "dim_product_routing.csv")
    assert r.duplicated(["routing_id", "operation_seq"]).any()
    assert not r.duplicated(["routing_id", "station_id"]).any()


def test_migration_is_schema_qualified_and_audit_append_only():
    sql = (settings.docs_dir.parent / "backend/migrations/V001__init.sql").read_text(encoding="utf-8")
    code = " ".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    assert "search_path" not in code.lower()
    assert "audit_log_immutable" in sql
