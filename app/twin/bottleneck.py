"""Bottleneck map, shifting-bottleneck frequency, marginal 'what to fix first' analysis, and the throughput/WIP forecast."""
from __future__ import annotations

from ..deps import query
from .scenarios import ScenarioError, load_params, shape
from .simulator import RunConfig, replicate

MARGINAL_STEP_PCT = 5.0


def latest_run_row(line_id: str, run_id: str | None):
    rows = query("SELECT * FROM twin.twin_scenario_run WHERE line_id = :l AND (CAST(:r AS text) IS NULL OR CAST(run_id AS text) = :r) "
                 "ORDER BY run_ts DESC LIMIT 1", l=line_id, r=run_id)
    return rows[0] if rows else None


def bottleneck_map(line_id: str, run_id: str | None = None, marginal: bool = False, reps: int = 8) -> dict:
    row = latest_run_row(line_id, run_id)
    if row is None:
        return {"line_id": line_id, "run_id": None, "stations": [], "message": "No simulated run exists for this line yet. Run a scenario first."}
    res = (row["params"] or {}).get("results") or {}
    stations = [dict(s) for s in res.get("stations", [])]
    order = [s["station_id"] for s in stations]
    for i, s in enumerate(stations):
        s["upstream_blocked_pct"] = stations[i - 1]["blocked_pct"] if i else 0.0      # blocking this station inflicts on its upstream neighbour
    by_util = sorted(stations, key=lambda s: -s["utilization_pct"])
    by_block = sorted(stations, key=lambda s: -s["upstream_blocked_pct"])
    for r, s in enumerate(by_util, 1):
        s["rank_by_utilization"] = r
    for r, s in enumerate(by_block, 1):
        s["rank_by_upstream_blocking"] = r
    out = {
        "line_id": line_id, "run_id": str(row["run_id"]), "scenario_name": row["scenario_name"], "bottleneck_station_id": row["bottleneck_station_id"],
        "stations": [s for s in sorted(stations, key=lambda x: order.index(x["station_id"]))],
        "ranking_by_utilization": [s["station_id"] for s in by_util], "ranking_by_upstream_blocking": [s["station_id"] for s in by_block],
        "shifting_bottleneck_frequency": {s["station_id"]: s.get("bottleneck_frequency", 0.0) for s in stations if s.get("bottleneck_frequency", 0) > 0},
        "method": "utilisation = busy / (scheduled - line stops); bottleneck = highest mean utilisation (tie: most upstream blocking); "
                  "frequency = share of replications in which the station was the most utilised.",
    }
    if marginal:
        out["what_to_fix_first"] = marginal_analysis(line_id, reps)
    return out


def marginal_analysis(line_id: str, reps: int = 8, horizon: float = 24.0, seed: int = 20260801) -> list[dict]:
    """Throughput gain per 1% cycle-time reduction at each station (common random numbers), ranked best first."""
    p, _ = load_params(line_id)
    base = replicate(p, RunConfig(horizon_hours=horizon), reps, seed)
    rows = []
    for i, s in enumerate(p.stations):
        r = replicate(p, RunConfig(horizon_hours=horizon, cycle_mult={s: 1 - MARGINAL_STEP_PCT / 100}), reps, seed)
        gain = r["output"]["mean"] - base["output"]["mean"]
        rows.append({"station_id": s, "gain_units_per_24h": gain, "gain_pct": gain / base["output"]["mean"] * 100,
                     "gain_units_per_pct_cycle_reduction": gain / MARGINAL_STEP_PCT,
                     "baseline_utilization_pct": base["stations"][i]["utilization_pct"]})
    rows.sort(key=lambda x: -x["gain_units_per_pct_cycle_reduction"])
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    return rows


def forecast(line_id: str, horizon: float, reps: int = 20, seed: int = 20260801) -> dict:
    """Baseline throughput / WIP forecast for the next `horizon` hours (SIMULATED), with 90% replication band."""
    p, cal = load_params(line_id)
    r = replicate(p, RunConfig(horizon_hours=horizon), reps, seed)
    n = int(horizon)
    series = [{"hour": h + 1, "output_mean": r["hourly_mean"][h], "output_p05": r["hourly_p05"][h], "output_p95": r["hourly_p95"][h],
               "wip_mean": r["wip_hourly_mean"][h] if h < len(r["wip_hourly_mean"]) else None} for h in range(min(n, len(r["hourly_mean"])))]
    return {"line_id": line_id, "horizon_hours": horizon, "replications": reps, "validation_status": cal["status"], "series": series,
            "total_output": r["output"], "wip": r["wip"], "calibration_holdout_mape_pct": float(cal["mape_pct"])}
