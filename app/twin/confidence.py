"""Confidence score (0-100) with its components returned, not just the number.

score = 100 * sum(weight_i * score_i), each score_i in [0, 1]:
  calibration   (0.40)  1 - holdout daily MAPE / 15%        (15% = NOT_VALIDATED threshold)
  interval      (0.20)  1 - width of the 90% replication band / 30% of the mean
  extrapolation (0.25)  1 - normalised distance of the scenario parameters from the calibrated range (baseline operation)
  freshness     (0.15)  1 - age of the latest MES hour / 30 days
The weights are judgement calls, documented in MODEL_CARD.md; they are not statistically derived.
"""
from __future__ import annotations

import math

WEIGHTS = {"calibration": 0.40, "interval": 0.20, "extrapolation": 0.25, "freshness": 0.15}

# Parameter change at which the extrapolation penalty reaches 100% (calibration only covers baseline operation)
LIMITS = {"demand_pct": 30.0, "capacity_pct": 30.0, "buffer_log2": 2.0, "cycle_pct": 30.0, "outage_hours": 24.0, "extra_shift_hours": 16.0}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def extrapolation_distance(*, demand_pct=0.0, capacity_pct=0.0, buffer_factor=1.0, cycle_change_pct=0.0, outage_hours=0.0,
                           extra_shift_hours=0.0) -> tuple[float, dict]:
    parts = {
        "demand_pct": abs(demand_pct) / LIMITS["demand_pct"],
        "capacity_pct": abs(capacity_pct) / LIMITS["capacity_pct"],
        "buffer_log2": abs(math.log2(buffer_factor)) / LIMITS["buffer_log2"] if buffer_factor > 0 else 1.0,
        "cycle_pct": abs(cycle_change_pct) / LIMITS["cycle_pct"],
        "outage_hours": outage_hours / LIMITS["outage_hours"],
        "extra_shift_hours": extra_shift_hours / LIMITS["extra_shift_hours"],
    }
    return min(1.0, sum(parts.values())), {k: round(v, 3) for k, v in parts.items() if v}


def confidence(*, calibration_mape_pct: float | None, band_low: float, band_high: float, mean_output: float,
               extrapolation: float, extrapolation_parts: dict, data_age_hours: float | None) -> dict:
    s_cal = 0.0 if calibration_mape_pct is None else _clamp(1 - calibration_mape_pct / 15.0)
    width = (band_high - band_low) / mean_output if mean_output else 1.0
    s_int = _clamp(1 - width / 0.30)
    s_ext = _clamp(1 - extrapolation)
    s_fre = 0.0 if data_age_hours is None else _clamp(1 - data_age_hours / (24 * 30))
    scores = {"calibration": s_cal, "interval": s_int, "extrapolation": s_ext, "freshness": s_fre}
    total = 100 * sum(WEIGHTS[k] * v for k, v in scores.items())
    return {
        "score": round(total, 1),
        "band_low": band_low, "band_high": band_high,
        "components": {
            "calibration_holdout_mape_pct": calibration_mape_pct, "calibration_score": round(s_cal, 3),
            "replication_band_rel_width": round(width, 4), "interval_score": round(s_int, 3),
            "extrapolation_distance": round(extrapolation, 3), "extrapolation_score": round(s_ext, 3), "extrapolation_parts": extrapolation_parts,
            "data_age_hours": data_age_hours, "freshness_score": round(s_fre, 3),
            "weights": WEIGHTS,
        },
    }
