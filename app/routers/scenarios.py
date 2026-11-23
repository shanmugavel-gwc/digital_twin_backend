from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..deps import envelope, query, require_api_key
from ..twin import scenarios as sc

router = APIRouter(prefix="/scenarios", tags=["scenarios"], dependencies=[Depends(require_api_key)])
sim_router = APIRouter(tags=["twin"], dependencies=[Depends(require_api_key)])

HIST_NOTES = [
    {"id": "DQ-03", "text": "Stored prediction_error_pct does not match abs(predicted-actual)/actual in the source; both are shown."},
    {"id": "DQ-02b", "text": "plant_id on many historical rows contradicts the line's plant; line_id is treated as authoritative."},
    {"id": "A-07", "text": "The output horizon of TWIN-1.7 history is unknown; not used for calibration."},
]


class RunRequest(BaseModel):
    scenario_name: str
    line_id: str
    replications: int = Field(default=30, ge=1, le=200)
    horizon_hours: float = Field(default=24, ge=1, le=168)
    params: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = Field(default="api", max_length=60)
    seed: int = Field(default=sc.DEFAULT_SEED, ge=0, le=2**31)


class CompareRequest(BaseModel):
    run_ids: list[str] = Field(min_length=2, max_length=6)


class DecisionRequest(BaseModel):
    decision: str
    reviewer: str = Field(min_length=1, max_length=80)
    comment: str = Field(min_length=1, max_length=2000)


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except sc.ScenarioError as e:
        raise HTTPException(e.status, e.message)


def _run(req: RunRequest, persist: bool):
    r = _guard(sc.run_scenario, req.scenario_name, req.line_id, req.params, req.replications, req.horizon_hours, req.seed, req.requested_by, persist)
    return envelope("SIMULATED", model_version=r["model_version"], validation_status=r["validation_status"], assumptions=r["assumptions"], **{
        k: v for k, v in r.items() if k not in ("model_version", "validation_status", "assumptions", "state_type")})


@router.post("/run")
def run(req: RunRequest):
    """Run baseline + scenario with explicit assumptions; persists the run (DRAFT -> SIMULATED) and audit-logs it."""
    return _run(req, persist=True)


@sim_router.post("/twin/simulate")
def simulate(req: RunRequest):
    """Same engine, NOT persisted and cannot be submitted for review (status DRAFT, run_id null). SIMULATED."""
    return _run(req, persist=False)


@router.get("/history")
def history(line_id: str | None = None):
    """The 300 historical TWIN-1.7 runs with stored vs recomputed error (from v_scenario_accuracy). SIMULATED."""
    rows = query("SELECT * FROM twin.v_scenario_accuracy WHERE (CAST(:l AS text) IS NULL OR line_id = :l) ORDER BY scenario_id", l=line_id)
    return envelope("SIMULATED", model_version="TWIN-1.7", validation_status="NOT_VALIDATED", assumptions=HIST_NOTES, items=rows)


@router.get("")
def list_runs(status: str | None = Query(default=None, pattern="^(DRAFT|SIMULATED|PENDING_REVIEW|APPROVED|REJECTED)$"),
              line_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    rows = query("SELECT * FROM twin.twin_scenario_run WHERE (CAST(:s AS text) IS NULL OR status = :s) "
                 "AND (CAST(:l AS text) IS NULL OR line_id = :l) ORDER BY run_ts DESC LIMIT :n", s=status, l=line_id, n=limit)
    items = [sc.shape(r) for r in rows]
    for i in items:                                   # list view: drop heavy per-station arrays
        i.pop("station_results", None)
    return envelope("SIMULATED", items=items, assumptions=[{"id": "LIST", "text": "Each run carries its own assumptions; open a run to see them."}])


@router.post("/compare")
def compare(req: CompareRequest):
    out = _guard(sc.compare, req.run_ids)
    assumptions = {a["id"] + a["text"]: a for r in out["runs"] for a in r["assumptions"]}
    vs = {r["validation_status"] for r in out["runs"]}
    return envelope("SIMULATED", validation_status=vs.pop() if len(vs) == 1 else "MIXED", assumptions=list(assumptions.values()), runs=out["runs"])


@router.get("/{run_id}")
def get_run(run_id: str):
    r = sc.get_run(run_id)
    if r is None:
        raise HTTPException(404, "scenario run not found")
    return envelope("SIMULATED", model_version=r["model_version"], validation_status=r["validation_status"], assumptions=r["assumptions"], **{
        k: v for k, v in r.items() if k not in ("model_version", "validation_status", "assumptions", "state_type")})


@router.post("/{run_id}/submit-review")
def submit_review(run_id: str, body: dict | None = None):
    """Moves SIMULATED -> PENDING_REVIEW. The only status change the agent is allowed to make."""
    r = _guard(sc.submit_for_review, run_id, (body or {}).get("actor", "api"))
    return envelope("SIMULATED", validation_status=r["validation_status"], assumptions=r["assumptions"], run_id=r["run_id"], status=r["status"])


@router.post("/{run_id}/decision")
def decision(run_id: str, req: DecisionRequest):
    """Human reviewer only (named reviewer + comment, audit-logged). Records a recommendation; it triggers NO plant change."""
    r = _guard(sc.decide, run_id, req.decision, req.reviewer, req.comment)
    return envelope("SIMULATED", validation_status=r["validation_status"], run_id=r["run_id"], status=r["status"],
                    assumptions=[{"id": "APPROVAL", "text": "Approval/rejection is a recorded recommendation only. Any change goes through normal change control."}])
