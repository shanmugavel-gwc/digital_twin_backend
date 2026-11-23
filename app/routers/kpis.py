from fastapi import APIRouter, Depends

from ..deps import envelope, query, require_api_key

router = APIRouter(tags=["kpis"], dependencies=[Depends(require_api_key)])

VERDICT_RANK = {"VALIDATED": 0, "NEEDS_REVIEW": 1, "NOT_VALIDATED": 2}


@router.get("/kpis")
def kpis():
    """The 5 dashboard KPIs. A KPI is None (n/a) when the data to compute it does not exist yet: never invented."""
    fresh = query("SELECT source, max_ts, age_hours FROM twin.v_data_freshness ORDER BY source")
    mes = next((f for f in fresh if f["source"].startswith("MES")), None)
    cal = query("SELECT DISTINCT ON (line_id) line_id, status, mape_pct FROM twin.twin_calibration_run ORDER BY line_id, created_at DESC, run_id DESC")
    runs = query("SELECT scenario_name, turnaround_ms, params->'results'->'delta' AS d FROM twin.twin_scenario_run")
    non_base = [r for r in runs if r["scenario_name"] != "Baseline" and r["d"]]
    verdict = max((c["status"] for c in cal), key=lambda v: VERDICT_RANK.get(v, 2)) if cal else "NOT_CALIBRATED"
    ms = [r["turnaround_ms"] for r in runs if r["turnaround_ms"] is not None]
    return envelope(
        "SIMULATED", validation_status=verdict, calibration_verdict=verdict,
        assumptions=[
            {"id": "KPI-1", "text": "Prediction error = mean hold-out daily MAPE across calibrated lines (twin vs MES)."},
            {"id": "KPI-2", "text": "Throughput improvement = best output-per-hour change vs baseline among non-baseline scenario runs (best case, not an average)."},
            {"id": "KPI-3", "text": "Bottleneck reduction = best fall in utilisation of the baseline bottleneck station among non-baseline runs."},
            {"id": "KPI-4", "text": "Scenario turnaround = mean wall-clock time of stored runs (baseline + scenario, replications included)."}],
        kpis={
            "prediction_error_pct": sum(float(c["mape_pct"]) for c in cal) / len(cal) if cal else None,
            "throughput_improvement_pct": max(float(r["d"]["output_per_hour_change_pct"]) for r in non_base) if non_base else None,
            "bottleneck_reduction_pct": max(float(r["d"]["bottleneck_util_reduction_pct"]) for r in non_base) if non_base else None,
            "scenario_turnaround_ms": sum(ms) / len(ms) if ms else None,
            "data_freshness_hours": float(mes["age_hours"]) if mes else None},
        scenario_runs=len(runs), calibrated_lines={c["line_id"]: c["status"] for c in cal}, data_freshness=fresh)
