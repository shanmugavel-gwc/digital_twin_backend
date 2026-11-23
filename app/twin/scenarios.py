"""Scenario engine: parameter validation, baseline-vs-scenario simulation, confidence, persistence, status workflow.

Status workflow DRAFT -> SIMULATED -> PENDING_REVIEW -> APPROVED | REJECTED (also enforced by a DB trigger, migration V002).
The agent (and this module) can only reach PENDING_REVIEW. APPROVED/REJECTED are written only by decide(), which the
human reviewer endpoint calls. Approval records a recommendation only: nothing here touches the plant.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace

from sqlalchemy import text

from ..deps import get_engine, query
from . import confidence as conf
from .loader import params_from_calibration, static_frames_from_db
from .simulator import LineParams, Outage, RunConfig, replicate

SCENARIO_NAMES = ["Baseline", "Demand +15%", "Additional shift", "Buffer increase", "Cycle time improvement", "SMT machine outage", "Custom"]
LINES = ["SMT-L1", "SMT-L2", "FAL-L1", "FAL-L2"]
DEFAULT_SEED = 20260801
AUTOMATED_IDENTITIES = {"agent", "digital_twin", "digital twin", "twin", "system", "bot", "ai", "claude", "llm", "assistant", "api", "ui"}

ALLOWED = {
    "Baseline": set(), "Demand +15%": set(),
    "Additional shift": {"extra_shift_hours"},
    "Buffer increase": {"buffer_factor", "buffer_units"},
    "Cycle time improvement": {"cycle_improvement_pct", "stations"},
    "SMT machine outage": {"outage_station_id", "outage_start_hour", "outage_hours", "reroute_capacity_pct"},
    "Custom": {"demand_change_pct", "capacity_change_pct", "buffer_factor", "buffer_overrides", "cycle_overrides", "outages", "extra_shift_hours"},
}


class ScenarioError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ------------------------------------------------------------------ parameters
def _num(params: dict, key: str, lo: float, hi: float, default: float) -> float:
    v = params.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ScenarioError(422, f"{key} must be a number")
    if not (lo <= v <= hi):
        raise ScenarioError(422, f"{key}={v} is outside the allowed range [{lo}, {hi}]")
    return float(v)


def _station(p: LineParams, sid) -> str:
    if sid not in p.stations:
        raise ScenarioError(422, f"station {sid!r} is not on line {p.line_id}; valid: {p.stations}")
    return sid


def resolve(name: str, params: dict, p: LineParams, base: dict, horizon: float) -> tuple[RunConfig, list[dict], dict, dict]:
    """-> (RunConfig, assumptions, extrapolation inputs, normalised params). Raises ScenarioError(422) on nonsense."""
    if name not in ALLOWED:
        raise ScenarioError(422, f"unknown scenario {name!r}; valid: {SCENARIO_NAMES}")
    params = dict(params or {})
    unknown = set(params) - ALLOWED[name]
    if unknown:
        raise ScenarioError(422, f"parameter(s) {sorted(unknown)} not valid for {name}; allowed: {sorted(ALLOWED[name])}")
    cfg = RunConfig(horizon_hours=horizon)
    ass: list[dict] = []
    ex = dict(demand_pct=0.0, capacity_pct=0.0, buffer_factor=1.0, cycle_change_pct=0.0, outage_hours=0.0, extra_shift_hours=0.0)
    norm: dict = {}
    A = lambda i, t: ass.append({"id": i, "text": t})

    if name == "Baseline":
        A("S-BASE", "Calibrated parameters unchanged; planned release rate equals the MES target_qty of the line.")
    elif name == "Demand +15%":
        cfg.demand_pct = 15.0; ex["demand_pct"] = 15.0; norm["demand_change_pct"] = 15
        A("S-DEM", "Release (order load) rate +15% over plan. Output is capacity-bound, so extra demand shows up as backlog and WIP, not throughput.")
    elif name == "Additional shift":
        h = _num(params, "extra_shift_hours", 0, 16, 8); cfg.horizon_hours = horizon + h; ex["extra_shift_hours"] = h; norm["extra_shift_hours"] = h
        A("S-SHIFT", f"Operating window extended by {h:g} h at the calibrated rate. MES shows all 24 h already producing, so this is modelled as an extra "
                     "overtime/weekend shift beyond the baseline horizon. Compare output per hour for a rate change.")
        A("S-LABOUR", "Labour and shift staffing are NOT modelled (workforce data is out of scope); the extra shift is assumed fully crewed.")
    elif name == "Buffer increase":
        if "buffer_units" in params and "buffer_factor" in params:
            raise ScenarioError(422, "give either buffer_factor or buffer_units, not both")
        if "buffer_units" in params:
            u = _num(params, "buffer_units", 1, 500, 40)
            cfg.buffer_override = {s: int(u) for s in p.stations}; ex["buffer_factor"] = u / max(1, p.buffers_units[-1]); norm["buffer_units"] = u
            A("S-BUF", f"Every buffer set to {u:g} units (baseline default 20, an engineering assumption).")
        else:
            f = _num(params, "buffer_factor", 0.25, 10, 2.0); cfg.buffer_factor = f; ex["buffer_factor"] = f; norm["buffer_factor"] = f
            A("S-BUF", f"Every buffer capacity multiplied by {f:g} (baseline 20 units each, an engineering assumption, not measured).")
    elif name == "Cycle time improvement":
        pct = _num(params, "cycle_improvement_pct", 0, 50, 10)
        sts = params.get("stations") or [base["bottleneck_station_id"]]
        if isinstance(sts, str):
            sts = [s.strip() for s in sts.split(",") if s.strip()] or [base["bottleneck_station_id"]]
        if not isinstance(sts, list):
            raise ScenarioError(422, "stations must be a list of station ids")
        sts = [_station(p, s) for s in sts]
        cfg.cycle_mult = {s: 1 - pct / 100 for s in sts}; ex["cycle_change_pct"] = pct; norm.update(cycle_improvement_pct=pct, stations=sts)
        A("S-CYC", f"Cycle time reduced by {pct:g}% at {', '.join(sts)}" + (" (baseline bottleneck, chosen automatically)." if not params.get("stations") else "."))
    elif name == "SMT machine outage":
        if not p.line_id.startswith("SMT"):
            raise ScenarioError(422, "SMT machine outage applies to SMT lines only")
        sid = _station(p, params.get("outage_station_id", "ST-SMT-040"))
        dur = _num(params, "outage_hours", 0.5, 48, 4); start = _num(params, "outage_start_hour", 0, 10_000, 6)
        rr = _num(params, "reroute_capacity_pct", 0, 100, 0)
        if start >= horizon:
            raise ScenarioError(422, f"outage_start_hour={start} is not inside the {horizon:g} h horizon")
        cfg.outages = [Outage(sid, start, dur, rr / 100)]; ex["outage_hours"] = dur
        norm.update(outage_station_id=sid, outage_start_hour=start, outage_hours=dur, reroute_capacity_pct=rr)
        A("S-OUT", f"{sid} offline for {dur:g} h from hour {start:g}" + (f"; alternate machine/re-routing keeps {rr:g}% of its capacity." if rr else "; no re-routing."))
    else:  # Custom
        d = _num(params, "demand_change_pct", -50, 50, 0); c = _num(params, "capacity_change_pct", -50, 50, 0)
        f = _num(params, "buffer_factor", 0.25, 10, 1.0); extra = _num(params, "extra_shift_hours", 0, 16, 0)
        cfg.demand_pct, cfg.capacity_pct, cfg.buffer_factor = d, c, f
        cfg.horizon_hours = horizon + extra
        ex.update(demand_pct=d, capacity_pct=c, buffer_factor=f, extra_shift_hours=extra)
        norm.update(demand_change_pct=d, capacity_change_pct=c, buffer_factor=f, extra_shift_hours=extra)
        bo = params.get("buffer_overrides", {})
        if not isinstance(bo, dict):
            raise ScenarioError(422, "buffer_overrides must be an object {station_id: units}")
        for s, u in bo.items():
            _station(p, s)
            if not isinstance(u, (int, float)) or not (1 <= u <= 500):
                raise ScenarioError(422, f"buffer_overrides[{s}] must be in [1, 500] units")
        cfg.buffer_override = {s: int(u) for s, u in bo.items()}; norm["buffer_overrides"] = cfg.buffer_override
        co = params.get("cycle_overrides", {})
        if not isinstance(co, dict):
            raise ScenarioError(422, "cycle_overrides must be an object {station_id: percent change}")
        for s, pc in co.items():
            _station(p, s)
            if not isinstance(pc, (int, float)) or not (-50 <= pc <= 50):
                raise ScenarioError(422, f"cycle_overrides[{s}] must be in [-50, 50] percent")
        cfg.cycle_mult = {s: 1 + pc / 100 for s, pc in co.items()}; norm["cycle_overrides"] = co
        if co:
            ex["cycle_change_pct"] = max(abs(v) for v in co.values())
        outs = params.get("outages", [])
        if not isinstance(outs, list):
            raise ScenarioError(422, "outages must be a list")
        for o in outs:
            if not isinstance(o, dict) or set(o) - {"station_id", "start_hour", "duration_hours", "capacity_pct"}:
                raise ScenarioError(422, "each outage needs station_id, start_hour, duration_hours and optional capacity_pct")
            sid = _station(p, o.get("station_id"))
            st, du, cp = _num(o, "start_hour", 0, 10_000, 0), _num(o, "duration_hours", 0.1, 168, 1), _num(o, "capacity_pct", 0, 100, 0)
            if st >= cfg.horizon_hours:
                raise ScenarioError(422, f"outage start_hour={st} is outside the horizon")
            cfg.outages.append(Outage(sid, st, du, cp / 100)); ex["outage_hours"] = max(ex["outage_hours"], du)
        norm["outages"] = outs
        A("S-CUSTOM", "Custom scenario: " + (", ".join(f"{k}={v}" for k, v in norm.items() if v not in ({}, [], 0, 0.0, 1.0)) or "no changes (equals baseline)") + ".")
    return cfg, ass, ex, norm


GENERAL_ASSUMPTIONS = [
    {"id": "G-LOT", "text": "Units flow as lots of 10; buffers are in units (default 20, an engineering assumption, A-04)."},
    {"id": "G-STOP", "text": "Downtime is modelled as whole-line stops sampled from the empirical MES (minutes, reason) distribution of the line; changeover is part of it."},
    {"id": "G-OUT", "text": "Output = units finished + units scrapped (MES actual_qty counts good + reject). Utilisation = busy time / (scheduled time - line stop time)."},
    {"id": "G-CAL", "text": "Parallelism, speed and variability are calibrated on 70% of the MES days; the model has not been validated for parameter changes (no MES data exists for them)."},
]


# ------------------------------------------------------------------ calibration access
def latest_calibration(line_id: str) -> dict | None:
    rows = query("SELECT run_id, status, mape_pct, params, created_at FROM twin.twin_calibration_run WHERE line_id = :l ORDER BY created_at DESC, run_id DESC LIMIT 1", l=line_id)
    if not rows:
        return None
    r = dict(rows[0])
    r["mape_pct"] = float(r["mape_pct"]) if r["mape_pct"] is not None else None
    return r


_BASE_CACHE: dict = {}
_STATIC: dict = {"t": 0.0, "fr": None}


def static_frames() -> dict:
    if _STATIC["fr"] is None or time.time() - _STATIC["t"] > 300:
        _STATIC.update(fr=static_frames_from_db(), t=time.time())
    return _STATIC["fr"]


def load_params(line_id: str) -> tuple[LineParams, dict]:
    if line_id not in LINES:
        raise ScenarioError(422, f"unknown line {line_id!r}; valid: {LINES}")
    cal = latest_calibration(line_id)
    if cal is None:
        raise ScenarioError(409, f"The twin is not calibrated for {line_id}. Run POST /api/calibration/run (or `make calibrate`) first.")
    return params_from_calibration(static_frames(), line_id, cal["params"]), cal


def data_age_hours() -> float | None:
    r = query("SELECT age_hours FROM twin.v_data_freshness WHERE source LIKE 'MES%'")
    return float(r[0]["age_hours"]) if r and r[0]["age_hours"] is not None else None


# ------------------------------------------------------------------ run
def _summ(r: dict) -> dict:
    return {"output_units": r["output"]["mean"], "good_units": r["good_units"], "wip_units": r["wip"]["mean"], "utilization_pct": r["utilization_pct"],
            "bottleneck_station_id": r["bottleneck_station_id"], "backlog_units": r["backlog_units"], "horizon_hours": r["horizon_hours"],
            "output_per_hour": r["output"]["mean"] / r["horizon_hours"]}


def _delta(base: dict, scen: dict) -> dict:
    bs = {s["station_id"]: s for s in base["stations"]}
    sc = {s["station_id"]: s for s in scen["stations"]}
    bn = base["bottleneck_station_id"]
    u0, u1 = bs[bn]["utilization_pct"], sc[bn]["utilization_pct"]
    shift = "none" if scen["bottleneck_station_id"] == bn else f"{bn} -> {scen['bottleneck_station_id']}"
    return {
        "output_units": scen["output"]["mean"] - base["output"]["mean"],
        "throughput_improvement_pct": (scen["output"]["mean"] / base["output"]["mean"] - 1) * 100,
        "output_per_hour_change_pct": ((scen["output"]["mean"] / scen["horizon_hours"]) / (base["output"]["mean"] / base["horizon_hours"]) - 1) * 100,
        "wip_units": scen["wip"]["mean"] - base["wip"]["mean"],
        "utilization_pct": scen["utilization_pct"] - base["utilization_pct"],
        "backlog_units": scen["backlog_units"] - base["backlog_units"],
        "bottleneck_shift": shift,
        "bottleneck_util_reduction_pct": (u0 - u1) / u0 * 100 if u0 else 0.0,
    }


def run_scenario(name: str, line_id: str, params: dict | None = None, reps: int = 30, horizon: float = 24.0, seed: int = DEFAULT_SEED,
                 requested_by: str = "api", persist: bool = True) -> dict:
    t0 = time.perf_counter()
    p, cal = load_params(line_id)
    key = (line_id, cal["run_id"], horizon, reps, seed)           # baseline is deterministic for these inputs: reuse it
    base = _BASE_CACHE.get(key)
    if base is None:
        base = replicate(p, RunConfig(horizon_hours=horizon), reps, seed)
        if len(_BASE_CACHE) > 64:
            _BASE_CACHE.clear()
        _BASE_CACHE[key] = base
    cfg, ass, ex, norm = resolve(name, params or {}, p, base, horizon)
    scen = base if name == "Baseline" else replicate(p, cfg, reps, seed)
    band = (scen["output"]["p05"], scen["output"]["p95"])
    ext, parts = conf.extrapolation_distance(**ex)
    c = conf.confidence(calibration_mape_pct=cal["mape_pct"], band_low=band[0], band_high=band[1], mean_output=scen["output"]["mean"],
                        extrapolation=ext, extrapolation_parts=parts, data_age_hours=data_age_hours())
    delta = _delta(base, scen)
    cp = cal["params"]
    assumptions = ass + GENERAL_ASSUMPTIONS + [{"id": "G-CALRUN", "text": f"Calibration run {cal['run_id']} for {line_id}: hold-out daily MAPE {cal['mape_pct']:.2f}% -> {cal['status']}; "
                                                f"parallelism {cp['parallelism']:.3f}, speed factor {cp['speed']:.3f}, cv {cp['cv']}."}]
    results = {"baseline": _summ(base), "scenario": _summ(scen), "delta": delta, "output_band_p05_p95": list(band), "good_units": scen["good_units"],
               "scrapped_units": scen["scrapped_units"], "line_down_min": scen["line_down_min"], "stations": scen["stations"],
               "max_conservation_gap": scen["max_conservation_gap"], "calibration_run_id": cal["run_id"], "confidence": c,
               "hourly_mean": scen["hourly_mean"]}
    turnaround = int((time.perf_counter() - t0) * 1000)
    run = {"scenario_name": name, "line_id": line_id, "model_version": "TWIN-2.0", "requested_by": requested_by,
           "params": {"request": norm, "replications": reps, "horizon_hours": horizon, "seed": seed, "results": results},
           "assumptions": assumptions, "status": "SIMULATED", "approval_required": True, "state_type": "SIMULATED",
           "predicted_output_units": scen["output"]["mean"], "predicted_wip_units": scen["wip"]["mean"],
           "predicted_utilization_pct": scen["utilization_pct"], "bottleneck_station_id": scen["bottleneck_station_id"],
           "confidence_score": c["score"], "confidence_band_low": band[0], "confidence_band_high": band[1],
           "validation_status": cal["status"], "turnaround_ms": turnaround}
    if not persist:
        run.update(run_id=None, persisted=False, status="DRAFT")
        return shape(run, scen["stations"])
    run["run_id"] = _persist(run, scen["stations"])
    run["persisted"] = True
    return shape(run, scen["stations"])


def audit(conn, actor: str, action: str, entity: str, entity_id: str, details: dict):
    conn.execute(text("INSERT INTO twin.audit_log (actor, action, entity, entity_id, details) VALUES (:a, :ac, :e, :i, CAST(:d AS jsonb))"),
                 dict(a=actor, ac=action, e=entity, i=str(entity_id), d=json.dumps(details, default=str)))


def _persist(run: dict, stations: list[dict]) -> str:
    rid = str(uuid.uuid4())
    plant = query("SELECT plant_id FROM twin.dim_line WHERE line_id = :l", l=run["line_id"])[0]["plant_id"]
    with get_engine().begin() as c:                       # one transaction: DRAFT -> SIMULATED, results, audit
        c.execute(text("INSERT INTO twin.twin_scenario_run (run_id, scenario_name, line_id, plant_id, model_version, requested_by, params, assumptions, status, approval_required) "
                       "VALUES (CAST(:r AS uuid), :n, :l, :p, :mv, :by, CAST(:pa AS jsonb), CAST(:a AS jsonb), 'DRAFT', true)"),
                  dict(r=rid, n=run["scenario_name"], l=run["line_id"], p=plant, mv=run["model_version"], by=run["requested_by"],
                       pa=json.dumps(run["params"]), a=json.dumps(run["assumptions"])))
        c.execute(text("UPDATE twin.twin_scenario_run SET status='SIMULATED', predicted_output_units=:o, predicted_wip_units=:w, predicted_utilization_pct=:u, "
                       "bottleneck_station_id=:b, confidence_score=:cs, confidence_band_low=:lo, confidence_band_high=:hi, validation_status=:v, turnaround_ms=:t "
                       "WHERE run_id = CAST(:r AS uuid)"),
                  dict(o=run["predicted_output_units"], w=run["predicted_wip_units"], u=run["predicted_utilization_pct"], b=run["bottleneck_station_id"],
                       cs=run["confidence_score"], lo=run["confidence_band_low"], hi=run["confidence_band_high"], v=run["validation_status"],
                       t=run["turnaround_ms"], r=rid))
        for s in stations:
            c.execute(text("INSERT INTO twin.twin_station_result (run_id, station_id, utilization_pct, avg_queue_units, avg_wait_sec, blocked_pct, starved_pct, is_bottleneck) "
                           "VALUES (CAST(:r AS uuid), :s, :u, :q, :w, :b, :st, :bn)"),
                      dict(r=rid, s=s["station_id"], u=s["utilization_pct"], q=s["avg_queue_units"], w=s["avg_wait_sec"], b=s["blocked_pct"], st=s["starved_pct"], bn=s["is_bottleneck"]))
        audit(c, run["requested_by"], "SCENARIO_RUN", "twin_scenario_run", rid,
              {"scenario": run["scenario_name"], "line": run["line_id"], "status_change": "DRAFT->SIMULATED", "turnaround_ms": run["turnaround_ms"],
               "validation_status": run["validation_status"], "confidence": run["confidence_score"]})
    return rid


# ------------------------------------------------------------------ shaping for API / UI
def shape(row: dict, stations: list[dict] | None = None) -> dict:
    res = (row.get("params") or {}).get("results") or {}
    c = res.get("confidence") or {}
    out = {
        "run_id": str(row["run_id"]) if row.get("run_id") else None, "scenario_name": row["scenario_name"], "line_id": row["line_id"],
        "plant_id": row.get("plant_id"), "model_version": row["model_version"], "state_type": "SIMULATED", "status": row["status"],
        "approval_required": True, "requested_by": row.get("requested_by"), "run_ts": row.get("run_ts"),
        "predicted_output_units": _f(row.get("predicted_output_units")), "predicted_wip_units": _f(row.get("predicted_wip_units")),
        "predicted_utilization_pct": _f(row.get("predicted_utilization_pct")), "bottleneck_station_id": row.get("bottleneck_station_id"),
        "confidence_score": _f(row.get("confidence_score")),
        "confidence": {"score": _f(row.get("confidence_score")), "band_low": _f(row.get("confidence_band_low")), "band_high": _f(row.get("confidence_band_high")),
                       "components": c.get("components")},
        "validation_status": row.get("validation_status"), "turnaround_ms": row.get("turnaround_ms"),
        "baseline_delta": res.get("delta"), "baseline": res.get("baseline"), "assumptions": row.get("assumptions") or [],
        "params": (row.get("params") or {}).get("request"), "replications": (row.get("params") or {}).get("replications"),
        "horizon_hours": (row.get("params") or {}).get("horizon_hours"), "persisted": row.get("persisted", True),
        "good_units": res.get("good_units"), "scrapped_units": res.get("scrapped_units"),
        "station_results": stations if stations is not None else res.get("stations"),
        "calibration_run_id": res.get("calibration_run_id"), "hourly_mean": res.get("hourly_mean"),
    }
    return out


def _f(v):
    return None if v is None else float(v)


def get_run(run_id: str) -> dict | None:
    try:
        uuid.UUID(run_id)
    except ValueError:
        return None
    rows = query("SELECT * FROM twin.twin_scenario_run WHERE run_id = CAST(:r AS uuid)", r=run_id)
    if not rows:
        return None
    st = query("SELECT station_id, utilization_pct, avg_queue_units, avg_wait_sec, blocked_pct, starved_pct, is_bottleneck FROM twin.twin_station_result "
               "WHERE run_id = CAST(:r AS uuid) ORDER BY utilization_pct DESC", r=run_id)
    return shape(rows[0], [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in s.items()} for s in st] or None)


# ------------------------------------------------------------------ workflow (agent may call submit_for_review; only humans call decide)
def submit_for_review(run_id: str, actor: str) -> dict:
    r = get_run(run_id)
    if r is None:
        raise ScenarioError(404, "scenario run not found")
    if r["status"] != "SIMULATED":
        raise ScenarioError(409, f"only a SIMULATED scenario can be submitted for review (current status: {r['status']})")
    with get_engine().begin() as c:
        c.execute(text("UPDATE twin.twin_scenario_run SET status='PENDING_REVIEW' WHERE run_id = CAST(:r AS uuid)"), dict(r=run_id))
        audit(c, actor, "SUBMIT_FOR_REVIEW", "twin_scenario_run", run_id, {"status_change": "SIMULATED->PENDING_REVIEW"})
    return get_run(run_id)


def decide(run_id: str, decision: str, reviewer: str, comment: str) -> dict:
    """HUMAN reviewer decision. Records a recommendation only; triggers no plant change."""
    reviewer, comment = (reviewer or "").strip(), (comment or "").strip()
    if decision not in ("APPROVED", "REJECTED"):
        raise ScenarioError(422, "decision must be APPROVED or REJECTED")
    if not reviewer or not comment:
        raise ScenarioError(422, "reviewer name and comment are required")
    if reviewer.lower() in AUTOMATED_IDENTITIES:
        raise ScenarioError(403, "decisions must come from a named human reviewer, not an automated identity")
    r = get_run(run_id)
    if r is None:
        raise ScenarioError(404, "scenario run not found")
    if r["status"] != "PENDING_REVIEW":
        raise ScenarioError(409, f"only a PENDING_REVIEW scenario can be decided (current status: {r['status']})")
    if (r.get("requested_by") or "").strip().lower() == reviewer.lower():
        raise ScenarioError(403, "the requester cannot review their own scenario")
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO twin.scenario_review (run_id, reviewer, decision, comment) VALUES (CAST(:r AS uuid), :rv, :d, :c)"),
                  dict(r=run_id, rv=reviewer, d=decision, c=comment))
        c.execute(text("UPDATE twin.twin_scenario_run SET status = :d WHERE run_id = CAST(:r AS uuid)"), dict(d=decision, r=run_id))
        audit(c, reviewer, "REVIEW_DECISION", "twin_scenario_run", run_id,
              {"decision": decision, "comment": comment, "status_change": f"PENDING_REVIEW->{decision}", "note": "recommendation only; no plant change"})
    return get_run(run_id)


def compare(run_ids: list[str]) -> dict:
    if not (2 <= len(run_ids) <= 6):
        raise ScenarioError(422, "compare needs 2 to 6 run ids")
    runs = []
    for i in run_ids:
        r = get_run(i)
        if r is None:
            raise ScenarioError(404, f"run {i} not found")
        runs.append(r)
    return {"runs": runs}
