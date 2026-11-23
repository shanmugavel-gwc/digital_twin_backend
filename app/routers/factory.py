"""Factory data for the Overview dashboard, the 3D twin and the Architecture page. Read-only; state_type LIVE (loaded plant data,
historical export). Machine health is CALCULATED by the latest agent run (rule-based index)."""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import envelope, query, require_api_key
from ..factory import analytics, architecture, home, replay

router = APIRouter(prefix="/factory", tags=["factory"], dependencies=[Depends(require_api_key)])
NOTE = [{"id": "F-1", "text": "Values come from a historical export (Aug-Oct 2026), not a live stream; 'latest' means the last hour in the data."},
        {"id": "F-2", "text": "Machine health and risk are a rule-based index (CALCULATED), not a trained prediction model."}]


@router.get("/overview")
def overview(date_from: str | None = None, date_to: str | None = None, line_id: str | None = None, machine_id: str | None = None,
             shift_id: str | None = None, area: str | None = None):
    try:
        f = analytics.resolve_filters(date_from, date_to, line_id, machine_id, shift_id, area)
    except analytics.FilterError as e:
        raise HTTPException(422, str(e))
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, **analytics.overview(f))


@router.get("/filters")
def filters():
    return envelope("LIVE", validation_status="NOT_APPLICABLE", bounds=analytics.data_bounds(),
                    lines=list(analytics.LINES), areas=list(analytics.AREAS), shifts=["A", "B", "C"],
                    machines=query("SELECT machine_id, machine_name, line_id FROM twin.dim_machine ORDER BY 1"))


@router.get("/machines")
def machines(run_id: str | None = None):
    """Every machine with its 3D asset key, zone, station and the status/health/findings of the latest completed agent run."""
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE + [
        {"id": "F-3", "text": "Machines are placed at stations by machine type (assumption A-03); the data has no machine-to-station key."}],
        **analytics.machine_states(run_id))


@router.get("/machines/{machine_id}/history")
def machine_history(machine_id: str, days: int = Query(default=7, ge=1, le=92)):
    if not query("SELECT 1 FROM twin.dim_machine WHERE machine_id = :m", m=machine_id):
        raise HTTPException(404, "unknown machine")
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, **analytics.machine_history(machine_id, days))


@router.get("/architecture")
def arch():
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, **architecture.architecture())


@router.get("/replay")
def replay_frames(plant_id: str = Query(pattern="^IN-(TN|KA)-01$"), start: str | None = None, hours: int = Query(default=24, ge=1, le=48)):
    """REPLAY: real recorded hours of one plant (machine states and sensors, line output, inspections, energy by area, daily stock,
    shipments, attendance), for the campus twin to play back on a clock. LIVE data, historical; nothing is generated."""
    return envelope("LIVE", validation_status="NOT_APPLICABLE", mode="REPLAY", assumptions=NOTE + [
        {"id": "F-4", "text": "Replay plays back recorded hours from the historical export on a clock; it is not a live stream."}],
        **replay.frames(plant_id, start, hours))


@router.get("/home")
def home_page():
    """Home page: pipeline counts per source system, problems the agent addresses (current figures), line x day heatmap."""
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, **home.home())
