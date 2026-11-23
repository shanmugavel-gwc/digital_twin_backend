"""Calibration of the twin against known MES production (Aug-Oct 2026).

Per line, fitted on the first ~70% of days and validated on the last ~30% (hold-out):
  * parallelism (units per cycle): reconciles the no-disruption slowest-station rate with MES target_qty (loader.py)
  * speed factor + cycle variability (cv): fitted on hours with NO downtime so downtime does not contaminate the fit
    (speed matches the mean hourly output; cv is the grid value whose hourly relative std is closest to MES)
  * downtime model: empirical (minutes, reason) distribution of line-hours; NONE-with-downtime is kept as UNATTRIBUTED
  * reject rate by product, rework rate
Validation: for every hold-out day the simulator is run with the ACTUAL product schedule of that day and freshly sampled
stops (the real stop timing is unknown to the model), N replications, compared with the actual daily and hourly output.
The verdict uses hold-out DAILY MAPE; hourly error is reported too but is dominated by unpredictable stop timing.
Run:  python -m app.twin.calibration   (saves to Neon)
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from dataclasses import replace

import numpy as np
import pandas as pd

from ..config import settings
from .loader import build_structure, frames_from_csv, frames_from_db, line_ids
from .simulator import LineParams, RunConfig, run_many

CV_GRID = [0.0, 0.1, 0.2, 0.35]
MODEL_VERSION = "TWIN-2.0"


def verdict(mape: float | None) -> str:
    if mape is None or np.isnan(mape):
        return "NOT_VALIDATED"
    if mape <= settings.mape_validated_max:
        return "VALIDATED"
    return "NEEDS_REVIEW" if mape <= settings.mape_needs_review_max else "NOT_VALIDATED"


def _metrics(sim: np.ndarray, act: np.ndarray, mape_mask=None) -> dict:
    sim, act = np.asarray(sim, float), np.asarray(act, float)
    m = act > 0 if mape_mask is None else mape_mask
    err = sim - act
    return {"mape_pct": float(np.mean(np.abs(err[m]) / act[m]) * 100) if m.any() else None,
            "bias_pct": float(err.sum() / act.sum() * 100), "rmse": float(np.sqrt(np.mean(err ** 2))), "n": int(len(act))}


def _fit(p0: LineParams, hr_train: pd.DataFrame, reps: int, seed: int) -> dict:
    z = hr_train[hr_train.downtime_min == 0]
    act_mean = float(z.actual_qty.mean())
    act_rel_std = float(z.actual_qty.std() / act_mean)
    best = None
    for cv in CV_GRID:
        speed = 1.0
        for _ in range(3):
            cfg = RunConfig(horizon_hours=30, stops=False)
            runs = run_many([(replace(p0, cv=cv, speed=speed), cfg, seed + r) for r in range(reps)])
            h = np.array([r["hourly_output"][2:] for r in runs])          # drop start-up hours
            speed *= float(h.mean()) / act_mean
        rel_std = float(h.std() / h.mean())
        cand = {"cv": cv, "speed": speed, "sim_hourly_rel_std": rel_std, "sim_hourly_mean": float(h.mean()),
                "fit_gap": abs(rel_std - act_rel_std)}
        if best is None or cand["fit_gap"] < best["fit_gap"]:
            best = cand
    best.update({"mes_hourly_mean_no_downtime": act_mean, "mes_hourly_rel_std_no_downtime": act_rel_std,
                 "mean_fit_error_pct": (best["sim_hourly_mean"] / act_mean - 1) * 100})
    return best


def _days(hr: pd.DataFrame) -> list[pd.DataFrame]:
    return [g.sort_values("event_ts") for _, g in hr.groupby("event_date") if len(g) == 24]


def _score_days(p: LineParams, days: list[pd.DataFrame], reps: int, seed: int) -> dict:
    tasks = []
    for d, g in enumerate(days):
        cfg = RunConfig(horizon_hours=24, product_schedule=g.product_id.tolist(), stops=True)
        tasks += [(p, cfg, seed + d * 1000 + r) for r in range(reps)]
    res = run_many(tasks)
    out_day, out_hr, series = [], [], []
    for d, g in enumerate(days):
        rr = res[d * reps:(d + 1) * reps]
        o = np.array([r["output_units"] for r in rr])
        hh = np.array([r["hourly_output"] for r in rr]).mean(axis=0)
        out_day.append(o.mean()); out_hr.append(hh)
        series.append({"date": str(g.event_date.iloc[0]), "actual": int(g.actual_qty.sum()), "simulated": float(o.mean()),
                       "p05": float(np.percentile(o, 5)), "p95": float(np.percentile(o, 95))})
    act_day = np.array([s["actual"] for s in series], float)
    act_hr = np.concatenate([g.actual_qty.values for g in days]).astype(float)
    return {"daily": _metrics(np.array(out_day), act_day), "hourly": _metrics(np.concatenate(out_hr), act_hr),
            "series": series, "n_hours": len(act_hr)}


def calibrate_line(fr: dict, line_id: str, reps: int = 8, train_frac: float = 0.7, seed: int = 20260801, log=print) -> dict:
    t0 = time.time()
    hr = fr["hourly"][fr["hourly"].line_id == line_id].sort_values("event_ts")
    dates = sorted(hr.event_date.unique())
    n_train = int(len(dates) * train_frac)
    train_dates, hold_dates = dates[:n_train], dates[n_train:]
    train_end = pd.Timestamp(hold_dates[0])
    hr_tr, hr_ho = hr[hr.event_date.isin(train_dates)], hr[hr.event_date.isin(hold_dates)]
    p0 = build_structure(fr, line_id, train_end=train_end)           # statistics from TRAINING data only
    fit = _fit(p0, hr_tr, reps=6, seed=seed)
    p = replace(p0, cv=fit["cv"], speed=fit["speed"])
    train_days = _days(hr_tr)[::3]                                   # every 3rd training day (cost)
    tr = _score_days(p, train_days, max(4, reps // 2), seed + 7)
    ho = _score_days(p, _days(hr_ho), reps, seed + 13)
    v = verdict(ho["daily"]["mape_pct"])
    # context metric: trivial forecast = mean daily output of the training days (what the twin must beat to add forecasting value)
    tr_daily = hr_tr.groupby("event_date").actual_qty.sum()
    ho_daily = hr_ho.groupby("event_date").actual_qty.sum()
    naive = _metrics(np.full(len(ho_daily), tr_daily.mean()), ho_daily.values)
    stop_hist = Counter((int(m), w) for m, w in p0.stop_samples)
    dt_model = {}
    for (m, w), c in stop_hist.items():
        if m > 0:
            d = dt_model.setdefault(w, {"events": 0, "minutes": 0})
            d["events"] += c; d["minutes"] += c * m
    for w, d in dt_model.items():
        d["freq_per_hour"] = d["events"] / len(p0.stop_samples); d["mean_minutes"] = d["minutes"] / d["events"]
    params = {
        "parallelism": p.parallelism, "speed": p.speed, "cv": p.cv, "lot_units": p.lot_units, "target_per_hour": p.target_per_hour,
        "products": p.products, "mix": p.mix, "reject_rate": p.reject_rate, "rework_rate": p.rework_rate,
        "stop_hist": [[m, w, c] for (m, w), c in sorted(stop_hist.items())], "downtime_model": dt_model,
        "fit": fit, "replications": reps, "seed": seed,
        "train_window": [str(train_dates[0]), str(train_dates[-1])], "holdout_window": [str(hold_dates[0]), str(hold_dates[-1])],
        "train": {"daily": tr["daily"], "hourly": tr["hourly"], "n_days_scored": len(train_days)},
        "holdout": {"daily": ho["daily"], "hourly": ho["hourly"], "naive_mean_baseline_daily": naive}, "daily_series": ho["series"],
        "notes": ["Changeover is part of the empirical downtime distribution: in MES, CHANGEOVER downtime is no more likely after a product change "
                  "(8.5% vs 8.4% of hours), so routing setup_min is not applied on product change.",
                  "Hourly error is dominated by unpredictable stop timing; the verdict uses daily MAPE.",
                  "Simulated days start from an empty line (about 0.1% start-up loss).",
                  "MES hourly output is insensitive to product, while routing cycle times differ by product; the twin follows the routing, "
                  "which adds some day-level error. Compare holdout.daily with holdout.naive_mean_baseline_daily."],
    }
    log(f"  {line_id}: par={p.parallelism:.3f} speed={p.speed:.3f} cv={p.cv} | train daily MAPE {tr['daily']['mape_pct']:.2f}% | "
        f"HOLD-OUT daily MAPE {ho['daily']['mape_pct']:.2f}% bias {ho['daily']['bias_pct']:+.2f}% RMSE {ho['daily']['rmse']:.0f} "
        f"| hourly MAPE {ho['hourly']['mape_pct']:.1f}% | naive-mean MAPE {naive['mape_pct']:.2f}% -> {v} ({time.time() - t0:.0f}s)")
    return {"line_id": line_id, "status": v, "params": params, "mape_pct": ho["daily"]["mape_pct"], "bias_pct": ho["daily"]["bias_pct"],
            "rmse": ho["daily"]["rmse"], "n_hours": ho["n_hours"], "period_start": str(hold_dates[0]), "period_end": str(hold_dates[-1])}


def save_runs(runs: list[dict], actor: str = "system") -> list[int]:
    from sqlalchemy import text
    from ..deps import get_engine
    ids = []
    with get_engine().begin() as c:
        for r in runs:
            rid = c.execute(text(
                "INSERT INTO twin.twin_calibration_run (model_version, period_start, period_end, line_id, params, mape_pct, bias_pct, rmse, n_hours, status) "
                "VALUES (:mv, :ps, :pe, :l, CAST(:p AS jsonb), :m, :b, :r, :n, :s) RETURNING run_id"),
                dict(mv=MODEL_VERSION, ps=r["period_start"], pe=r["period_end"] + " 23:00", l=r["line_id"], p=json.dumps(r["params"]),
                     m=r["mape_pct"], b=r["bias_pct"], r=r["rmse"], n=r["n_hours"], s=r["status"])).scalar_one()
            ids.append(rid)
        c.execute(text("UPDATE twin.twin_model_version SET calibration_run_id = :i WHERE model_version = :mv"), dict(i=ids[-1], mv=MODEL_VERSION))
        c.execute(text("INSERT INTO twin.audit_log (actor, action, entity, entity_id, details) VALUES (:a, 'CALIBRATION_RUN', 'twin_calibration_run', :e, CAST(:d AS jsonb))"),
                  dict(a=actor, e=",".join(map(str, ids)),
                       d=json.dumps({"lines": {r["line_id"]: {"status": r["status"], "holdout_daily_mape_pct": r["mape_pct"]} for r in runs}})))
    return ids


def calibrate_all(lines: list[str] | None = None, reps: int = 8, source: str = "db", save: bool = True, actor: str = "system", log=print) -> list[dict]:
    fr = frames_from_db() if source == "db" else frames_from_csv(settings.data_dir)
    out = []
    for ln in (lines or line_ids(fr)):
        out.append(calibrate_line(fr, ln, reps=reps, log=log))
    if save:
        save_runs(out, actor=actor)
        log("saved to twin.twin_calibration_run")
    return out


if __name__ == "__main__":
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    print(f"Calibrating TWIN-2.0 (replications={reps}) ...")
    calibrate_all(reps=reps)
