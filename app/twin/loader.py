"""Build simulator LineParams (structure + MES-derived statistics) from Neon tables or, for offline tests, the CSVs.
Calibrated values (parallelism, speed, cv) come from twin_calibration_run via calibration.apply_calibration()."""
from __future__ import annotations

import pandas as pd

from . import ref_config as rc
from .model import AREA_RANK
from .simulator import LineParams

HOURLY_COLS = "event_ts, event_date, line_id, product_id, target_qty, actual_qty, good_qty, reject_qty, rework_qty, downtime_min, downtime_reason"


def frames_from_db() -> dict[str, pd.DataFrame]:
    from ..deps import query
    return {
        "stations": pd.DataFrame(query("SELECT * FROM twin.dim_station")),
        "slm": pd.DataFrame(query("SELECT * FROM twin.station_line_map")),
        "routing": pd.DataFrame(query("SELECT * FROM twin.dim_product_routing")),
        "buffers": pd.DataFrame(query("SELECT * FROM twin.buffer_config")),
        "hourly": pd.DataFrame(query(f"SELECT {HOURLY_COLS} FROM twin.fact_hourly_production ORDER BY line_id, event_ts")),
    }


def frames_from_csv(data_dir) -> dict[str, pd.DataFrame]:
    """Offline equivalent (mapping from ref_config, same rules as scripts/load_data.py)."""
    stn = pd.read_csv(f"{data_dir}/dim_station.csv")
    rt = pd.read_csv(f"{data_dir}/dim_product_routing.csv")
    hr = pd.read_csv(f"{data_dir}/fact_hourly_production.csv", parse_dates=["event_ts"])
    hr["event_date"] = hr.event_ts.dt.date
    slm = pd.DataFrame([(s.station_id, ln, s.process_area) for s in stn.itertuples() for ln, f in rc.LINE_FAMILY.items()
                        if f == rc.AREA_TO_FAMILY[s.process_area]], columns=["station_id", "line_id", "role"])
    buf = []
    for ln in rc.LINE_FAMILY:
        ids = _ordered(stn, slm, ln)
        buf += [(ln, a, b, rc.DEFAULT_BUFFER_UNITS) for a, b in zip(ids, ids[1:])]
    return {"stations": stn, "slm": slm, "routing": rt, "hourly": hr,
            "buffers": pd.DataFrame(buf, columns=["line_id", "from_station_id", "to_station_id", "capacity_units"])}


def _ordered(stations: pd.DataFrame, slm: pd.DataFrame, line_id: str) -> list[str]:
    ids = set(slm[slm.line_id == line_id].station_id)
    s = stations[stations.station_id.isin(ids)].copy()
    s["rank"] = s.process_area.map(AREA_RANK)
    return s.sort_values(["rank", "sequence_no"]).station_id.tolist()


def line_ids(fr: dict) -> list[str]:
    return sorted(fr["slm"].line_id.unique())


def build_structure(fr: dict[str, pd.DataFrame], line_id: str, train_end=None) -> LineParams:
    """Structure + MES statistics. If train_end is given, statistics use only hourly rows with event_ts < train_end."""
    ids = _ordered(fr["stations"], fr["slm"], line_id)
    rt = fr["routing"][fr["routing"].station_id.isin(ids)]
    hr = fr["hourly"][fr["hourly"].line_id == line_id]
    if train_end is not None:
        hr = hr[hr.event_ts < train_end]
    products = sorted(hr.product_id.unique())
    mix = (hr.product_id.value_counts(normalize=True).reindex(products)).tolist()
    cycle, gate = [], []
    for s in ids:
        r = rt[rt.station_id == s]
        cycle.append({row.product_id: float(row.standard_cycle_sec) for row in r.itertuples()})
        gate.append(bool(r.quality_gate.any()))
    bmap = {(b.line_id, b.to_station_id): int(b.capacity_units) for b in fr["buffers"].itertuples()}
    buffers = [rc.DEFAULT_BUFFER_UNITS] + [bmap.get((line_id, s), rc.DEFAULT_BUFFER_UNITS) for s in ids[1:]]
    target = float(hr.target_qty.mean())
    # parallelism so that the no-disruption slowest-station rate reconciles with MES target_qty (mix-weighted over products)
    rates = []
    for pr, share in zip(products, mix):
        c = max(cycle[i][pr] for i in range(len(ids)))
        rates.append(share * 3600.0 / c)
    parallelism = target / sum(rates)
    rej = (hr.groupby("product_id").reject_qty.sum() / hr.groupby("product_id").actual_qty.sum()).to_dict()
    rework = float(hr.rework_qty.sum() / hr.actual_qty.sum())
    stops = list(zip(hr.downtime_min.astype(int), hr.downtime_reason.map(lambda x: "UNATTRIBUTED" if x == "NONE" else x)))
    stops = [(m, "NONE" if m == 0 else why) for m, why in stops]
    setup = {pr: float(rt[rt.product_id == pr].setup_min.mean()) for pr in products}
    return LineParams(line_id=line_id, stations=ids, gate=gate, cycle=cycle, buffers_units=buffers, products=products, mix=mix,
                      target_per_hour=target, parallelism=float(parallelism), reject_rate={k: float(v) for k, v in rej.items()},
                      rework_rate=rework, stop_samples=stops, setup_min=setup)


def static_frames_from_db() -> dict[str, pd.DataFrame]:
    """Structure only (no hourly facts): cheap enough to load per scenario run."""
    from ..deps import query
    return {
        "stations": pd.DataFrame(query("SELECT * FROM twin.dim_station")),
        "slm": pd.DataFrame(query("SELECT * FROM twin.station_line_map")),
        "routing": pd.DataFrame(query("SELECT * FROM twin.dim_product_routing")),
        "buffers": pd.DataFrame(query("SELECT * FROM twin.buffer_config")),
    }


def params_from_calibration(fr: dict[str, pd.DataFrame], line_id: str, cal: dict) -> LineParams:
    """Structure from the master data + calibrated values from a twin_calibration_run.params JSON."""
    ids = _ordered(fr["stations"], fr["slm"], line_id)
    rt = fr["routing"][fr["routing"].station_id.isin(ids)]
    cycle, gate = [], []
    for s in ids:
        r = rt[rt.station_id == s]
        cycle.append({row.product_id: float(row.standard_cycle_sec) for row in r.itertuples()})
        gate.append(bool(r.quality_gate.any()))
    bmap = {(b.line_id, b.to_station_id): int(b.capacity_units) for b in fr["buffers"].itertuples()}
    buffers = [rc.DEFAULT_BUFFER_UNITS] + [bmap.get((line_id, s), rc.DEFAULT_BUFFER_UNITS) for s in ids[1:]]
    stops = []
    for m, w, c in cal["stop_hist"]:
        stops += [(int(m), w)] * int(c)
    setup = {pr: float(rt[rt.product_id == pr].setup_min.mean()) for pr in cal["products"]}
    return LineParams(line_id=line_id, stations=ids, gate=gate, cycle=cycle, buffers_units=buffers, products=cal["products"],
                      mix=cal["mix"], target_per_hour=cal["target_per_hour"], parallelism=cal["parallelism"], speed=cal["speed"],
                      cv=cal["cv"], lot_units=cal["lot_units"], stop_samples=stops, reject_rate=cal["reject_rate"],
                      rework_rate=cal["rework_rate"], setup_min=setup)
