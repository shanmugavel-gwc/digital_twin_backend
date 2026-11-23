"""Data connectors: one read-only interface per source system.

Rules (CLAUDE.md): connectors only READ. There is no write, send, command or setpoint method anywhere in this module and
a test asserts it. A source that is not wired up reports NOT_CONNECTED and never returns made-up data.
Configuration is described by key NAMES only; values (endpoints, credentials) belong in backend/.env and are never returned.
"""
from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

STALE_AFTER_HOURS = 24.0


class NotConnected(Exception):
    """Raised by a connector that has no real source behind it."""


@dataclass
class Info:
    id: str
    name: str
    source_system: str
    category: str
    status: str                 # CONNECTED | NOT_CONNECTED
    mode: str                   # BATCH_LOAD | STATIC_FILE | NONE
    state_type: str             # LIVE | SIMULATED | NOT_CONNECTED  (label of the data this connector would supply)
    used_for: str
    message: str
    tables: list[str] = field(default_factory=list)
    latest_ts: str | None = None
    age_hours: float | None = None
    stale: bool | None = None
    rows: int | None = None
    config_keys: list[str] = field(default_factory=list)
    needed_to_connect: str | None = None
    direction: str = "READ_ONLY"
    config_origin: str | None = None      # env (backend/.env) | session (added in the UI, memory only)


class Connector(ABC):
    id: str
    name: str

    @abstractmethod
    def describe(self, facts: dict) -> Info: ...

    @abstractmethod
    def read_latest(self, facts: dict) -> list[dict]:
        """Latest records from the source, or NotConnected."""


STATIC_DIR = Path(__file__).resolve().parents[2] / "data" / "static"


def read_static(filename: str, columns: tuple[str, ...]) -> tuple[list[dict], str | None]:
    """Rows of a static CSV plus a problem message. A missing file or a file with only a header gives no rows."""
    f = STATIC_DIR / filename
    if not f.exists():
        return [], f"{filename} not found in data/static"
    with f.open(newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        missing = [c for c in columns if c not in (rd.fieldnames or [])]
        if missing:
            return [], f"{filename} is missing columns: {', '.join(missing)}"
        return [r for r in rd if any((v or "").strip() for v in r.values())], None


class _Static(Connector):
    """A source supplied as a static CSV in data/static (columns below). Read-only. With no data rows it reports NOT_CONNECTED:
    the connector never fabricates a record. Rows you add are labelled as a static file, not a live feed, and are not yet used by
    calibration or scenarios unless the usage note says so."""
    category = ""
    source_system = ""
    used_for = ""
    needed = ""
    filename = ""
    columns: tuple[str, ...] = ()
    date_col: str | None = None
    keys: tuple[str, ...] = ()

    def describe(self, facts: dict) -> Info:
        rows, problem = read_static(self.filename, self.columns)
        if rows:
            dates = sorted(r[self.date_col] for r in rows if self.date_col and r.get(self.date_col)) if self.date_col else []
            return Info(id=self.id, name=self.name, source_system=f"Static file data/static/{self.filename}", category=self.category, status="CONNECTED",
                        mode="STATIC_FILE", state_type="LIVE", used_for=self.used_for,
                        message="Static file, not a live feed. Shown for visibility; not yet used by calibration or scenarios.",
                        tables=[f"data/static/{self.filename}"], latest_ts=dates[-1] if dates else None, rows=len(rows),
                        config_keys=list(self.keys), needed_to_connect=f"Columns: {', '.join(self.columns)}")
        return Info(id=self.id, name=self.name, source_system=f"Static file data/static/{self.filename}", category=self.category, status="NOT_CONNECTED",
                    mode="NONE", state_type="NOT_CONNECTED", used_for=self.used_for,
                    message=problem or "Template only: no data rows yet. Nothing from this system is used or shown.",
                    tables=[f"data/static/{self.filename}"], config_keys=list(self.keys), needed_to_connect=f"Fill data/static/{self.filename}. {self.needed} Columns: {', '.join(self.columns)}")

    def read_latest(self, facts: dict, limit: int = 100) -> list[dict]:
        rows, problem = read_static(self.filename, self.columns)
        if not rows:
            raise NotConnected(problem or f"{self.name} has no data rows")
        return rows[-limit:]


class MesHourly(Connector):
    id, name = "mes", "MES hourly production"

    def describe(self, facts: dict) -> Info:
        f = facts.get("MES:fact_hourly_production") or {}
        age = f.get("age_hours")
        stale = None if age is None else age > STALE_AFTER_HOURS
        ok = f.get("rows", 0) > 0
        return Info(id=self.id, name=self.name, source_system="MES (CSV export loaded into Neon)", category="Production actuals",
                    status="CONNECTED" if ok else "NOT_CONNECTED", mode="BATCH_LOAD" if ok else "NONE", state_type="LIVE" if ok else "NOT_CONNECTED",
                    used_for="Calibration, validation, line OEE and data freshness. Labelled LIVE; it is a loaded export, not a streaming feed.",
                    message=("Loaded and queryable. Not streaming: values are as fresh as the last load." if ok else "No MES rows loaded. Run make load."),
                    tables=["twin.fact_hourly_production", "twin.v_line_hourly_oee", "twin.v_line_daily_actuals"], latest_ts=f.get("max_ts"), age_hours=age,
                    stale=stale, rows=f.get("rows"), config_keys=["NEON_DIRECT_URL (load)", "NEON_POOLED_URL (read)"])

    def read_latest(self, facts: dict) -> list[dict]:
        raise NotConnected("use the /api/twin/live-state endpoint for MES reads")


class TwinHistory(Connector):
    id, name = "twin-history", "Prior twin runs (TWIN-1.7)"

    def describe(self, facts: dict) -> Info:
        f = facts.get("TWIN:fact_digital_twin_scenario") or {}
        ok = f.get("rows", 0) > 0
        return Info(id=self.id, name=self.name, source_system="Earlier twin export (CSV loaded into Neon)", category="Model history",
                    status="CONNECTED" if ok else "NOT_CONNECTED", mode="BATCH_LOAD" if ok else "NONE", state_type="SIMULATED" if ok else "NOT_CONNECTED",
                    used_for="Comparison only. Not used for calibration: its output horizon is undocumented and its error column is inconsistent (DQ-03, DQ-05).",
                    message="Loaded. 158 of 300 rows disagree with the line to plant mapping (DQ-02)." if ok else "Not loaded.",
                    tables=["twin.fact_digital_twin_scenario", "twin.v_scenario_accuracy"], latest_ts=f.get("max_ts"), age_hours=f.get("age_hours"),
                    stale=None if f.get("age_hours") is None else f["age_hours"] > STALE_AFTER_HOURS, rows=f.get("rows"))

    def read_latest(self, facts: dict) -> list[dict]:
        raise NotConnected("use /api/scenarios/history for prior runs")


class MasterData(Connector):
    id, name = "master-data", "Master data (machines, stations, routing)"

    def describe(self, facts: dict) -> Info:
        n = facts.get("master_rows", 0)
        ok = n > 0
        return Info(id=self.id, name=self.name, source_system="Master data CSVs loaded into Neon", category="Structure",
                    status="CONNECTED" if ok else "NOT_CONNECTED", mode="BATCH_LOAD" if ok else "NONE", state_type="LIVE" if ok else "NOT_CONNECTED",
                    used_for="Twin graph: stations in routing order, machines, standard cycle times.",
                    message="Loaded." if ok else "Not loaded.", tables=["twin.dim_machine", "twin.dim_station", "twin.dim_product_routing"], rows=n)

    def read_latest(self, facts: dict) -> list[dict]:
        raise NotConnected("use /api/twin/model for the asset hierarchy")


class ScadaIot(_Static):
    id, name = "telemetry", "SCADA / IoT telemetry"
    category = "Machine signals"
    filename, columns, date_col = "telemetry.csv", ("ts", "machine_id", "metric", "value", "unit"), "ts"
    used_for = "Station-level utilisation, cycle times and downtime per machine; would let the twin be validated below line level."
    needed = "Read-only export from the historian. machine_id must exist in dim_machine."


class ErpBom(_Static):
    id, name = "erp-bom", "ERP bill of materials"
    category = "Materials"
    filename, columns, date_col = "erp_bom.csv", ("plant_id", "product_id", "material_id", "qty_per_unit", "on_hand_qty", "as_of"), "as_of"
    used_for = "Material availability and shortages as a constraint in scenarios."
    needed = "Read-only ERP extract of BOM and stock by plant."


class CadLayout(_Static):
    id, name = "cad-layout", "CAD / line layout"
    category = "Layout"
    filename, columns = "cad_layout.csv", ("line_id", "from_station_id", "to_station_id", "buffer_capacity_units", "distance_m")
    used_for = "Measured buffer capacities and distances (today every buffer is a 20 unit assumption, A-04) and real machine models for the 3D view."
    needed = "Layout export with buffer capacity per station pair."


class CycleStudies(_Static):
    id, name = "cycle-studies", "Cycle-time studies"
    category = "Engineering"
    filename, columns, date_col = "cycle_studies.csv", ("product_id", "station_id", "measured_cycle_sec", "sample_size", "study_date"), "study_date"
    used_for = "Measured cycle times to replace routing standards and the fitted speed factor."
    needed = "Table of measured cycle times by product and station."


REGISTRY: list[Connector] = [MesHourly(), TwinHistory(), MasterData(), ScadaIot(), ErpBom(), CadLayout(), CycleStudies()]


def gather_facts(query) -> dict:
    """Facts the connectors report on, read from Neon with the app's read helper."""
    out: dict = {}
    for r in query("SELECT source, max_ts, age_hours FROM twin.v_data_freshness"):
        out[r["source"]] = {"max_ts": r["max_ts"].isoformat() if r["max_ts"] else None, "age_hours": float(r["age_hours"]) if r["age_hours"] is not None else None}
    for key, tbl in (("MES:fact_hourly_production", "twin.fact_hourly_production"), ("TWIN:fact_digital_twin_scenario", "twin.fact_digital_twin_scenario")):
        out.setdefault(key, {})["rows"] = int(query(f"SELECT count(*) AS n FROM {tbl}")[0]["n"])
    out["master_rows"] = int(query("SELECT (SELECT count(*) FROM twin.dim_machine) + (SELECT count(*) FROM twin.dim_station) + (SELECT count(*) FROM twin.dim_product_routing) AS n")[0]["n"])
    return out
