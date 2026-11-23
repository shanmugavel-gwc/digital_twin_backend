import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..deps import envelope, query, require_api_key
from ..twin import calibration as cal

router = APIRouter(prefix="/calibration", tags=["calibration"], dependencies=[Depends(require_api_key)])
_JOB: dict = {"state": "IDLE"}
_LOCK = threading.Lock()
RANK = {"VALIDATED": 0, "NEEDS_REVIEW": 1, "NOT_VALIDATED": 2}


class CalibrateRequest(BaseModel):
    replications: int = Field(default=8, ge=2, le=40)
    lines: list[str] | None = None


def overall(verdicts: list[str]) -> str:
    return "NOT_CALIBRATED" if not verdicts else max(verdicts, key=lambda v: RANK.get(v, 2))


def latest_per_line() -> list[dict]:
    return query("SELECT DISTINCT ON (line_id) run_id, line_id, status, mape_pct, bias_pct, rmse, n_hours, period_start, period_end, params, created_at "
                 "FROM twin.twin_calibration_run ORDER BY line_id, created_at DESC, run_id DESC")


def shape_line(r: dict) -> dict:
    p = r["params"]
    h, t = p["holdout"], p["train"]
    return {
        "run_id": r["run_id"], "line_id": r["line_id"], "verdict": r["status"], "created_at": r["created_at"],
        "parallelism_factor": p["parallelism"], "speed_factor": p["speed"], "cv": p["cv"], "lot_units": p["lot_units"],
        "train": {**t["daily"], "n_hours": t["n_days_scored"] * 24, "window": p["train_window"], "hourly": t["hourly"]},
        "holdout": {**h["daily"], "n_hours": r["n_hours"], "window": p["holdout_window"], "hourly": h["hourly"], "naive_mean_baseline_daily": h.get("naive_mean_baseline_daily")},
        "daily": p["daily_series"], "downtime_model": p["downtime_model"], "fit": p["fit"],
        "reject_rate": p["reject_rate"], "rework_rate": p["rework_rate"], "replications": p["replications"], "notes": p.get("notes", []),
    }


@router.get("/report")
def report():
    """Calibration report per line: train/hold-out MAPE, bias, RMSE (daily and hourly), actual vs simulated series, verdict."""
    rows = latest_per_line()
    lines = [shape_line(r) for r in rows]
    status = overall([r["verdict"] for r in lines])
    return envelope(
        "SIMULATED", validation_status=status, lines=lines, thresholds={"validated_max": settings.mape_validated_max, "needs_review_max": settings.mape_needs_review_max},
        split={"train_start": lines[0]["train"]["window"][0], "train_end": lines[0]["train"]["window"][1],
               "holdout_start": lines[0]["holdout"]["window"][0], "holdout_end": lines[0]["holdout"]["window"][1]} if lines else None,
        message=None if lines else "No calibration has been run yet. The twin model is uncalibrated, so no scenario result can be treated as validated.",
        assumptions=[{"id": "CAL-1", "text": "Verdict uses hold-out DAILY MAPE: VALIDATED <= 8%, NEEDS_REVIEW <= 15%, else NOT_VALIDATED (configurable)."},
                     {"id": "CAL-2", "text": "Hourly error is dominated by the unpredictable timing of stops and is reported for transparency, not used for the verdict."},
                     {"id": "CAL-3", "text": "Validated on baseline operation only: scenario parameter changes are extrapolations and carry an explicit confidence penalty."}])


def _work(reps: int, lines: list[str] | None):
    started = time.time()
    try:
        _JOB.update(state="RUNNING", progress=[], started_at=started)
        out = cal.calibrate_all(lines=lines, reps=reps, source="db", save=True, actor="api", log=lambda m: _JOB["progress"].append(m))
        _JOB.update(state="DONE", finished_at=time.time(), seconds=round(time.time() - started, 1),
                    results={r["line_id"]: {"verdict": r["status"], "holdout_daily_mape_pct": r["mape_pct"]} for r in out})
    except Exception as e:                                   # report the class, never connection details
        _JOB.update(state="FAILED", error=f"{type(e).__name__}: {str(e)[:200]}")


@router.post("/run", status_code=202)
def run(req: CalibrateRequest | None = None):
    """Starts calibration in the background (takes a couple of minutes); poll GET /calibration/status."""
    req = req or CalibrateRequest()
    with _LOCK:
        if _JOB.get("state") == "RUNNING":
            raise HTTPException(409, "a calibration is already running")
        _JOB.clear(); _JOB.update(state="RUNNING", progress=[], started_at=time.time())
    threading.Thread(target=_work, args=(req.replications, req.lines), daemon=True).start()
    return envelope("SIMULATED", job=_JOB)


@router.get("/status")
def status():
    return envelope("SIMULATED", job=_JOB)
