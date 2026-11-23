from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import envelope, require_api_key
from ..twin.bottleneck import forecast
from ..twin.scenarios import ScenarioError

router = APIRouter(prefix="/forecast", tags=["forecast"], dependencies=[Depends(require_api_key)])


@router.get("/throughput-wip")
def throughput_wip(line_id: str = Query(pattern="^(SMT|FAL)-L[12]$"), horizon_hours: int = Query(default=24, ge=1, le=168),
                   replications: int = Query(default=20, ge=2, le=100)):
    """Baseline throughput and WIP forecast per hour, with a 90% replication band. SIMULATED, never a measurement."""
    try:
        f = forecast(line_id, horizon_hours, replications)
    except ScenarioError as e:
        raise HTTPException(e.status, e.message)
    return envelope("SIMULATED", validation_status=f.pop("validation_status"),
                    assumptions=[{"id": "FC-1", "text": "Baseline operation from the calibrated model; the line starts empty, so hour 1 is lower than steady state."},
                                 {"id": "FC-2", "text": "Band = 5th to 95th percentile across replications (stop timing and cycle variability)."}], **f)
