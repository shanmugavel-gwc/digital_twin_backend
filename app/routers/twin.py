from fastapi import APIRouter, Depends

from ..db import run_with_retry
from fastapi import HTTPException, Query

from ..deps import envelope, get_engine, query, require_api_key
from ..twin.bottleneck import bottleneck_map
from ..twin.model import ASSUMPTIONS, build_model
from ..twin.scenarios import ScenarioError, latest_calibration

_RANK = {"VALIDATED": 0, "NEEDS_REVIEW": 1, "NOT_VALIDATED": 2, "NOT_CALIBRATED": 3}


def _overall_status() -> str:
    """Worst latest calibration verdict across the 4 lines; NOT_CALIBRATED if any line has no run."""
    out = [(latest_calibration(l) or {}).get("status", "NOT_CALIBRATED") for l in ("SMT-L1", "SMT-L2", "FAL-L1", "FAL-L2")]
    return max(out, key=lambda v: _RANK.get(v, 2))

router = APIRouter(prefix="/twin", tags=["twin"], dependencies=[Depends(require_api_key)])


@router.get("/model")
def twin_model():
    """Asset hierarchy and process flow as JSON for the UI. Structure only: SIMULATED (twin), not a measurement.
    validation_status is the worst latest calibration verdict across the lines."""
    m = run_with_retry(get_engine(), build_model)
    return envelope("SIMULATED", validation_status=_overall_status(), assumptions=ASSUMPTIONS, **m)


@router.get("/lines")
def twin_lines():
    m = run_with_retry(get_engine(), build_model)
    lines = [{"line_id": ln["line_id"], "plant_id": p["plant_id"], "process_family": ln["process_family"], "n_stations": len(ln["stations"])}
             for p in m["plants"] for ln in p["lines"]]
    return envelope("SIMULATED", assumptions=ASSUMPTIONS, lines=lines)


@router.get("/live-state")
def live_state(line_id: str = Query(pattern="^(SMT|FAL)-L[12]$"), hours: int = Query(default=24, ge=1, le=168)):
    """LIVE: measured MES values for the latest `hours` of data (anchored on the latest MES hour, not wall-clock)."""
    last = query("SELECT max(event_ts) AS ts FROM twin.fact_hourly_production WHERE line_id = :l", l=line_id)[0]["ts"]
    if last is None:
        raise HTTPException(404, "no MES data for this line")
    rows = query("SELECT event_ts, shift_id, product_id, target_qty, actual_qty, good_qty, downtime_min, downtime_reason, "
                 "availability, performance, quality, oee FROM twin.v_line_hourly_oee "
                 "WHERE line_id = :l AND event_ts > :t - make_interval(hours => :h) ORDER BY event_ts", l=line_id, t=last, h=hours)
    dt = query("SELECT downtime_reason, sum(downtime_min) AS minutes, count(*) AS line_hours FROM twin.fact_hourly_production "
               "WHERE line_id = :l AND event_ts > :t - make_interval(hours => :h) AND downtime_min > 0 "
               "GROUP BY 1 ORDER BY 2 DESC", l=line_id, t=last, h=hours)
    tot = lambda k: sum((r[k] or 0) for r in rows)

    def avg(k):
        v = [float(r[k]) for r in rows if r[k] is not None]
        return sum(v) / len(v) if v else None
    age = query("SELECT age_hours FROM twin.v_data_freshness WHERE source LIKE 'MES%'")[0]["age_hours"]
    return envelope(
        "LIVE", validation_status="NOT_APPLICABLE", line_id=line_id, as_of=last, data_age_hours=float(age), hours=rows,
        stations=[],  # no station-level measurements exist (SCADA/IoT is NOT_CONNECTED)
        line={"hours_reported": len(rows), "target_qty": tot("target_qty"), "actual_qty": tot("actual_qty"), "good_qty": tot("good_qty"),
              "downtime_min": tot("downtime_min"), "availability": avg("availability"), "performance": avg("performance"),
              "quality": avg("quality"), "oee": avg("oee")},
        downtime_by_reason=dt,
        assumptions=[{"id": "LIVE", "text": "Values are MES measurements; OEE performance is not capped at 100%. "
                                            "SCADA/IoT station telemetry is NOT_CONNECTED."}])


@router.get("/bottleneck")
def bottleneck(line_id: str = Query(pattern="^(SMT|FAL)-L[12]$"), scenario_run_id: str | None = None, marginal: bool = False):
    """Bottleneck map for the latest (or given) twin run: stations ranked by utilisation and by upstream blocking, shifting-bottleneck
    frequency, and (marginal=true) a 'what to fix first' ranking by throughput gain per 1% cycle-time reduction. SIMULATED."""
    try:
        m = bottleneck_map(line_id, scenario_run_id, marginal)
    except ScenarioError as e:
        raise HTTPException(e.status, e.message)
    cal = latest_calibration(line_id)
    return envelope("SIMULATED", validation_status=(cal or {}).get("status", "NOT_CALIBRATED"), assumptions=ASSUMPTIONS, **m)
