"""Simulator sanity + scenario sign tests. Offline: structure from the CSVs, no database."""
import os
from dataclasses import replace

import pytest

from app.config import settings
from app.twin.loader import build_structure, frames_from_csv
from app.twin.simulator import Outage, RunConfig, replicate, run_many, simulate_once
from app.twin import scenarios as sc

FR = frames_from_csv(settings.data_dir)


def params(line="SMT-L1", **kw):
    p = build_structure(FR, line)
    return replace(p, speed=1.1, cv=0.1, **kw)


def test_units_are_conserved():
    p = params()
    for seed in range(5):
        r = simulate_once(p, RunConfig(horizon_hours=12), seed)
        assert r["conservation_gap"] == 0          # released = finished + scrapped + still in line
        assert r["good_units"] + r["scrapped_units"] == r["output_units"]


def test_deterministic_bottleneck_is_slowest_station():
    p = replace(params(), cv=0.0, products=["SPH-A15-128"], mix=[1.0])
    cyc = [c["SPH-A15-128"] for c in p.cycle]
    r = simulate_once(p, RunConfig(horizon_hours=24, stops=False, cv=0), 1)
    assert cyc[r["bottleneck_idx"]] == max(cyc)
    rate = p.parallelism * 3600 / max(cyc) / p.speed                      # units/h at the slowest station
    assert r["output_units"] == pytest.approx(rate * 24 * (1 - p.reject_rate["SPH-A15-128"] * 0 ), rel=0.04)


def test_seeded_runs_are_reproducible():
    p = params()
    a = simulate_once(p, RunConfig(horizon_hours=6), 42)
    b = simulate_once(p, RunConfig(horizon_hours=6), 42)
    c = simulate_once(p, RunConfig(horizon_hours=6), 43)
    assert a == b and a["output_units"] != c["output_units"]


def test_parallel_and_serial_execution_agree(monkeypatch):
    p = params()
    tasks = [(p, RunConfig(horizon_hours=4), s) for s in range(12)]
    par = [r["output_units"] for r in run_many(tasks)]
    monkeypatch.setenv("TWIN_WORKERS", "0")
    ser = [r["output_units"] for r in run_many(tasks)]
    assert par == ser


def _out(p, cfg, reps=12):
    return replicate(p, cfg, reps, 7)


def test_sign_more_buffer_never_reduces_throughput():
    p = params()
    base = _out(p, RunConfig(horizon_hours=12))["output"]["mean"]
    big = _out(p, RunConfig(horizon_hours=12, buffer_factor=4))["output"]["mean"]
    assert big >= base * 0.995


def test_sign_outage_never_increases_throughput():
    p = params()
    base = _out(p, RunConfig(horizon_hours=12))["output"]["mean"]
    out = _out(p, RunConfig(horizon_hours=12, outages=[Outage("ST-SMT-040", 2, 3, 0.0)]))["output"]["mean"]
    assert out <= base * 1.005 and out < base          # a 3 h outage must actually cost output


def test_sign_cycle_improvement_at_bottleneck_helps():
    p = params()
    b = _out(p, RunConfig(horizon_hours=12))
    bn = b["bottleneck_station_id"]
    g = _out(p, RunConfig(horizon_hours=12, cycle_mult={bn: 0.8}))["output"]["mean"]
    assert g >= b["output"]["mean"] * 0.995


def test_demand_increase_creates_backlog_not_free_throughput():
    p = params()
    b = _out(p, RunConfig(horizon_hours=12))
    d = _out(p, RunConfig(horizon_hours=12, demand_pct=15))
    assert d["backlog_units"] > b["backlog_units"]
    assert d["output"]["mean"] <= b["output"]["mean"] * 1.02     # capacity-bound


# ---------------------------------------------------------------- parameter validation (422, clear messages)
BASE = {"bottleneck_station_id": "ST-SMT-080"}


@pytest.mark.parametrize("name,params_,frag", [
    ("Custom", {"demand_change_pct": 80}, "outside the allowed range"),
    ("Custom", {"capacity_change_pct": -90}, "outside the allowed range"),
    ("Custom", {"buffer_factor": 0}, "outside the allowed range"),
    ("Custom", {"nonsense": 1}, "not valid for Custom"),
    ("Baseline", {"demand_change_pct": 5}, "not valid for Baseline"),
    ("Custom", {"cycle_overrides": {"ST-NOPE": 5}}, "not on line"),
    ("Custom", {"outages": [{"station_id": "ST-SMT-040", "start_hour": 99, "duration_hours": 1}]}, "outside the horizon"),
    ("Buffer increase", {"buffer_factor": 2, "buffer_units": 30}, "either"),
    ("Cycle time improvement", {"cycle_improvement_pct": 80}, "outside the allowed range"),
    ("SMT machine outage", {"outage_hours": "x"}, "must be a number"),
    ("Nope", {}, "unknown scenario"),
])
def test_parameter_validation_rejects_nonsense(name, params_, frag):
    with pytest.raises(sc.ScenarioError) as e:
        sc.resolve(name, params_, params(), BASE, 24)
    assert e.value.status == 422 and frag in e.value.message


def test_smt_outage_rejected_on_fal_line():
    with pytest.raises(sc.ScenarioError) as e:
        sc.resolve("SMT machine outage", {}, params("FAL-L1"), BASE, 24)
    assert e.value.status == 422 and "SMT lines only" in e.value.message


def test_all_six_named_scenarios_resolve():
    for n in sc.SCENARIO_NAMES:
        cfg, ass, ex, norm = sc.resolve(n, {}, params(), BASE, 24)
        assert ass and ass[0]["text"]
