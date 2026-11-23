"""Twin asset graph: Plant -> Line -> Station (routing order) -> Machine, with buffers between stations.
Built from the Neon tables (never invented). Pure read; no write path of any kind to the plant."""
from sqlalchemy import text
from sqlalchemy.engine import Engine

AREA_RANK = {"SMT": 0, "FINAL_ASSEMBLY": 1, "TEST": 2, "QUALITY": 3, "PACKAGING": 4, "WAREHOUSE": 5}

SQL_LINES = text("SELECT l.line_id, l.plant_id, l.process_family, p.plant_name FROM twin.dim_line l "
                 "JOIN twin.dim_plant p USING (plant_id) ORDER BY l.plant_id, l.line_id")
SQL_STATIONS = text("""
    SELECT m.line_id, s.station_id, s.station_name, s.process_area, s.sequence_no, s.critical_control_point,
           round(avg(r.standard_cycle_sec)::numeric, 2) AS cycle_sec_mean,
           jsonb_object_agg(r.product_id, r.standard_cycle_sec) AS cycle_sec_by_product,
           bool_or(r.quality_gate) AS quality_gate,
           round(avg(r.setup_min)::numeric, 1) AS setup_min_mean
    FROM twin.station_line_map m
    JOIN twin.dim_station s USING (station_id)
    LEFT JOIN twin.dim_product_routing r USING (station_id)
    GROUP BY m.line_id, s.station_id, s.station_name, s.process_area, s.sequence_no, s.critical_control_point""")
SQL_MACHINES = text("""
    SELECT d.line_id, ms.station_id, d.machine_id, d.machine_name, d.machine_type, d.criticality, d.plant_id AS plant_id_as_listed
    FROM twin.machine_station_map ms JOIN twin.dim_machine d USING (machine_id)""")
SQL_BUFFERS = text("SELECT line_id, from_station_id, to_station_id, capacity_units FROM twin.buffer_config")

ASSUMPTIONS = [
    {"id": "A-01", "text": "Line to plant: SMT-L1/FAL-L1 in IN-TN-01, SMT-L2/FAL-L2 in IN-KA-01 (confirmed by 8,832/8,832 MES rows)."},
    {"id": "A-02", "text": "Routing has no line id: SMT stations belong to the plant's SMT line; all other areas to its FAL line."},
    {"id": "A-03", "text": "Machines are mapped to stations by machine type (editable); Assembly Cell assumed at ST-FAL-010."},
    {"id": "A-04", "text": "Inter-station buffers are an engineering assumption (default 20 units), not measured."},
    {"id": "A-08", "text": "Station order is process-area rank then sequence_no; routing operation_seq restarts per area."},
]


def build_model(engine: Engine) -> dict:
    with engine.connect() as c:
        lines = c.execute(SQL_LINES).mappings().all()
        stations = c.execute(SQL_STATIONS).mappings().all()
        machines = c.execute(SQL_MACHINES).mappings().all()
        buffers = c.execute(SQL_BUFFERS).mappings().all()

    buf = {(b["line_id"], b["from_station_id"]): b["capacity_units"] for b in buffers}
    mach: dict[tuple, list] = {}
    flagged = []
    for m in machines:
        mach.setdefault((m["line_id"], m["station_id"]), []).append(
            {k: m[k] for k in ("machine_id", "machine_name", "machine_type", "criticality")})
    plants: dict[str, dict] = {}
    for ln in lines:
        p = plants.setdefault(ln["plant_id"], {"plant_id": ln["plant_id"], "plant_name": ln["plant_name"], "lines": []})
        sts = sorted((s for s in stations if s["line_id"] == ln["line_id"]),
                     key=lambda s: (AREA_RANK[s["process_area"]], s["sequence_no"]))
        nodes = []
        for i, s in enumerate(sts):
            nodes.append({
                "station_id": s["station_id"], "station_name": s["station_name"], "process_area": s["process_area"],
                "order": i + 1, "critical_control_point": s["critical_control_point"], "quality_gate": s["quality_gate"],
                "cycle_sec": float(s["cycle_sec_mean"]) if s["cycle_sec_mean"] is not None else None,
                "cycle_sec_by_product": s["cycle_sec_by_product"], "setup_min": float(s["setup_min_mean"]) if s["setup_min_mean"] is not None else None,
                "machines": mach.get((ln["line_id"], s["station_id"]), []),
                "buffer_after_units": buf.get((ln["line_id"], s["station_id"])),
            })
        p["lines"].append({"line_id": ln["line_id"], "process_family": ln["process_family"], "stations": nodes,
                           "buffers": [{"from": a["station_id"], "to": b["station_id"], "capacity_units": a["buffer_after_units"]}
                                       for a, b in zip(nodes, nodes[1:])]})
    # data-quality flags carried into the model (machines whose listed plant contradicts the line's plant)
    line_plant = {ln["line_id"]: ln["plant_id"] for ln in lines}
    for m in machines:
        if m["plant_id_as_listed"] != line_plant[m["line_id"]]:
            flagged.append({"machine_id": m["machine_id"], "line_id": m["line_id"], "listed_plant": m["plant_id_as_listed"], "line_plant": line_plant[m["line_id"]]})
    return {"plants": list(plants.values()), "data_quality_flags": flagged}
