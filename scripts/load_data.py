"""Idempotent loader: CSVs -> Neon (DIRECT URL), one transaction, COPY into temp table + upsert.
Row-count and FK checks run before commit; any failure rolls everything back.

Hard failure : hourly facts or dim_line contradict the line->plant mapping (A-01).
Loud warning : fact_digital_twin_scenario.plant_id contradicts it (known issue, line_id is treated as authoritative).
Never        : rows are never dropped or 'fixed' silently."""
import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402
from app.db import direct_connection  # noqa: E402
from app.twin import ref_config as rc  # noqa: E402

BOOL_COLS = {"critical_control_point", "quality_gate", "approval_required"}
LOAD_ORDER = [  # (table, csv, pk)
    ("dim_machine", "dim_machine.csv", ["machine_id"]),
    ("dim_station", "dim_station.csv", ["station_id"]),
    ("dim_product_routing", "dim_product_routing.csv", ["routing_id", "station_id"]),
    ("fact_hourly_production", "fact_hourly_production.csv", ["event_id"]),
    ("fact_digital_twin_scenario", "fact_digital_twin_scenario.csv", ["scenario_id"]),
]


def read(name: str) -> pd.DataFrame:
    df = pd.read_csv(settings.data_dir / name)
    for c in BOOL_COLS & set(df.columns):
        df[c] = df[c].astype(str).str.lower().map({"true": True, "false": False})
        assert df[c].notna().all(), f"{name}.{c}: non-boolean values"
    return df


def upsert(conn, table: str, df: pd.DataFrame, pk: list[str]):
    cols = list(df.columns)
    tmp = f"_stg_{table}"
    conn.execute(f"CREATE TEMP TABLE {tmp} (LIKE twin.{table} INCLUDING DEFAULTS) ON COMMIT DROP")
    buf = io.StringIO()
    df.to_csv(buf, index=False, header=False)
    buf.seek(0)
    collist = ", ".join(cols)
    with conn.cursor().copy(f"COPY {tmp} ({collist}) FROM STDIN WITH (FORMAT csv)") as cp:
        cp.write(buf.getvalue())
    setc = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in pk)
    conn.execute(f"INSERT INTO twin.{table} ({collist}) SELECT {collist} FROM {tmp} "
                 f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {setc}")


def main():
    src = {t: read(csv) for t, csv, _ in LOAD_ORDER}
    fh, sc, mach, stn, rt = (src["fact_hourly_production"], src["fact_digital_twin_scenario"],
                             src["dim_machine"], src["dim_station"], src["dim_product_routing"])

    # ---- A-01 line->plant verification
    bad_h = fh[fh.apply(lambda r: rc.LINE_PLANT.get(r.line_id) != r.plant_id, axis=1)]
    if len(bad_h):
        sys.exit(f"FATAL: {len(bad_h)} hourly rows contradict the configured line->plant mapping (A-01): "
                 f"{bad_h.groupby(['line_id', 'plant_id']).size().to_dict()}")
    bad_s = sc[sc.apply(lambda r: rc.LINE_PLANT.get(r.line_id) != r.plant_id, axis=1)]
    if len(bad_s):
        print("!" * 78)
        print(f"WARNING: {len(bad_s)}/{len(sc)} fact_digital_twin_scenario rows have plant_id that contradicts "
              f"the line->plant mapping.\n         Loaded AS SUPPLIED; line_id treated as authoritative. See DATA_QUALITY.md DQ-02b.")
        print("!" * 78)
    viol = fh[fh.good_qty + fh.reject_qty > fh.actual_qty]
    if len(viol):
        sys.exit(f"FATAL: {len(viol)} rows violate good+reject<=actual; refusing to drop them silently. See DATA_QUALITY.md.")
    for t, n in rc.EXPECTED_ROWS.items():
        if len(src[t]) != n:
            sys.exit(f"FATAL: {t} CSV has {len(src[t])} rows, expected {n}")

    # ---- machine plant source switch (A-03)
    if settings.machine_plant_source == "line":
        mach = mach.assign(plant_id=mach.line_id.map(rc.LINE_PLANT))
        src["dim_machine"] = mach
        print("MACHINE_PLANT_SOURCE=line: dim_machine.plant_id overridden from line->plant mapping")

    with direct_connection() as conn:
        # reference data
        for pid, name in rc.PLANT_NAMES.items():
            conn.execute("INSERT INTO twin.dim_plant VALUES (%s,%s) ON CONFLICT (plant_id) DO UPDATE SET plant_name=EXCLUDED.plant_name", (pid, name))
        for lid, pid in rc.LINE_PLANT.items():
            conn.execute("INSERT INTO twin.dim_line VALUES (%s,%s,%s) ON CONFLICT (line_id) DO UPDATE SET plant_id=EXCLUDED.plant_id, process_family=EXCLUDED.process_family",
                         (lid, pid, rc.LINE_FAMILY[lid]))
        for table, _, pk in LOAD_ORDER:
            upsert(conn, table, src[table], pk)
            print(f"loaded {table}: {len(src[table])} rows")

        # derived mapping tables (rebuilt from config each load; editable by changing ref_config / the tables)
        conn.execute("DELETE FROM twin.station_line_map")
        for r in stn.itertuples():
            fam = rc.AREA_TO_FAMILY[r.process_area]
            for lid, f in rc.LINE_FAMILY.items():
                if f == fam:
                    conn.execute("INSERT INTO twin.station_line_map VALUES (%s,%s,%s)", (r.station_id, lid, r.process_area))
        conn.execute("DELETE FROM twin.machine_station_map")
        for r in mach.itertuples():
            conn.execute("INSERT INTO twin.machine_station_map VALUES (%s,%s)", (r.machine_id, rc.MACHINE_TYPE_STATION[r.machine_type]))
        # buffers: between consecutive stations (by routing order within each line); keep existing edits
        order = {s.station_id: (s.process_area, s.sequence_no) for s in stn.itertuples()}
        area_rank = {"SMT": 0, "FINAL_ASSEMBLY": 1, "TEST": 2, "QUALITY": 3, "PACKAGING": 4, "WAREHOUSE": 5}
        for lid, fam in rc.LINE_FAMILY.items():
            sts = sorted((s for s, (a, _) in order.items() if rc.AREA_TO_FAMILY[a] == fam),
                         key=lambda s: (area_rank[order[s][0]], order[s][1]))
            for a, b in zip(sts, sts[1:]):
                conn.execute("INSERT INTO twin.buffer_config VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (lid, a, b, rc.DEFAULT_BUFFER_UNITS))

        # ---- checks (before commit)
        for t, n in rc.EXPECTED_ROWS.items():
            got = conn.execute(f"SELECT count(*) FROM twin.{t}").fetchone()[0]
            assert got == n, f"{t}: {got} rows in DB, expected {n}"
        fk = {
            "routing->station": "SELECT count(*) FROM twin.dim_product_routing r LEFT JOIN twin.dim_station s USING (station_id) WHERE s.station_id IS NULL",
            "hourly->line": "SELECT count(*) FROM twin.fact_hourly_production f LEFT JOIN twin.dim_line l USING (line_id) WHERE l.line_id IS NULL",
            "scenario->line": "SELECT count(*) FROM twin.fact_digital_twin_scenario f LEFT JOIN twin.dim_line l USING (line_id) WHERE l.line_id IS NULL",
            "hourly line-hours": "SELECT count(*) FROM (SELECT line_id,event_ts FROM twin.fact_hourly_production GROUP BY 1,2 HAVING count(*)>1) x",
            "station with !=1 line per plant": "SELECT count(*) FROM (SELECT m.station_id, l.plant_id FROM twin.station_line_map m JOIN twin.dim_line l USING(line_id) GROUP BY 1,2 HAVING count(*)<>1) x",
            "stations unmapped": "SELECT count(*) FROM twin.dim_station s WHERE NOT EXISTS (SELECT 1 FROM twin.station_line_map m WHERE m.station_id=s.station_id)",
        }
        for name, q in fk.items():
            n = conn.execute(q).fetchone()[0]
            assert n == 0, f"integrity check failed: {name} = {n}"
            print(f"check ok: {name} = 0")
        conn.commit()
    print("load OK (committed)")


if __name__ == "__main__":
    main()
