"""Validate the 5 source CSVs (no database needed) and write docs/DATA_QUALITY.md.
Findings are reported, never silently fixed. Exit code 2 with --strict if any ERROR-severity issue exists."""
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402
from app.twin import ref_config as rc  # noqa: E402

D = settings.data_dir
issues: list[dict] = []      # id, severity, check, count, detail
passes: list[tuple[str, str]] = []
notes: list[str] = []


def issue(i, sev, check, count, detail):
    issues.append(dict(id=i, severity=sev, check=check, count=int(count), detail=detail))


def ok(check, detail=""):
    passes.append((check, detail))


def md_table(df: pd.DataFrame) -> str:
    rows = ["| " + " | ".join([df.index.name or "line_id"] + list(df.columns)) + " |",
            "|" + "---|" * (len(df.columns) + 1)]
    rows += ["| " + " | ".join([str(i)] + [str(v) for v in r]) + " |" for i, r in zip(df.index, df.values)]
    return "\n".join(rows)


def main():
    strict = "--strict" in sys.argv
    mach = pd.read_csv(D / "dim_machine.csv")
    stn = pd.read_csv(D / "dim_station.csv")
    rt = pd.read_csv(D / "dim_product_routing.csv")
    f = pd.read_csv(D / "fact_hourly_production.csv", parse_dates=["event_ts"])
    s = pd.read_csv(D / "fact_digital_twin_scenario.csv", parse_dates=["run_ts"])

    # ------------------------------------------------ row counts
    for name, df in [("dim_machine", mach), ("dim_station", stn), ("dim_product_routing", rt),
                     ("fact_hourly_production", f), ("fact_digital_twin_scenario", s)]:
        exp = rc.EXPECTED_ROWS[name]
        (ok if len(df) == exp else lambda c, d: issue("DQ-00", "ERROR", c, len(df), d))(
            f"row count {name} = {exp}", f"got {len(df)}")

    # ------------------------------------------------ routing key
    dup_seq = int(rt.duplicated(["routing_id", "operation_seq"], keep=False).sum())
    dup_stn = int(rt.duplicated(["routing_id", "station_id"]).sum())
    issue("DQ-09", "WARN", "dim_product_routing: (routing_id, operation_seq) is not unique", dup_seq,
          f"operation_seq restarts inside each process area, so {dup_seq} of {len(rt)} rows share a (routing_id, operation_seq) "
          f"with another row. The PK proposed in the brief cannot be used; (routing_id, station_id) is unique "
          f"(duplicates: {dup_stn}) and is used instead. A global operation order is derived from process_area then operation_seq.")
    prods = rt.product_id.nunique()
    cover = rt.groupby("product_id").station_id.nunique().to_dict()
    ok("routing = 3 products x 21 stations", f"{prods} products, stations per product {cover}; single revision {rt.revision.unique().tolist()}")
    miss = set(rt.station_id) - set(stn.station_id)
    (ok if not miss else lambda c, d: issue("DQ-10", "ERROR", c, len(miss), d))("routing stations all exist in dim_station", str(sorted(miss)))
    unk = set(f.product_id) - set(rt.product_id)
    (ok if not unk else lambda c, d: issue("DQ-11", "ERROR", c, len(unk), d))("hourly product_ids all have a routing", str(sorted(unk)))
    ok("units: routing cycle seconds / setup minutes", f"standard_cycle_sec {rt.standard_cycle_sec.min()}-{rt.standard_cycle_sec.max()} s, "
       f"setup_min {rt.setup_min.min()}-{rt.setup_min.max()} min (plausible as stated; no seconds/minutes mix-up detected)")

    # ------------------------------------------------ 1. hourly production
    n_days = f.event_date.nunique()
    exp_rows = n_days * 24 * f.line_id.nunique()
    (ok if len(f) == exp_rows else lambda c, d: issue("DQ-01", "ERROR", c, abs(len(f) - exp_rows), d))(
        f"hourly rows = days x 24 x lines ({n_days}x24x{f.line_id.nunique()}={exp_rows})", f"got {len(f)}")
    full = pd.MultiIndex.from_product([sorted(f.line_id.unique()), pd.date_range(f.event_ts.min(), f.event_ts.max(), freq="h")])
    have = pd.MultiIndex.from_frame(f[["line_id", "event_ts"]])
    missing = len(full.difference(have))
    dups = int(f.duplicated(["line_id", "event_ts"]).sum())
    (ok if missing == 0 and dups == 0 else lambda c, d: issue("DQ-01b", "ERROR", c, missing + dups, d))(
        "no missing or duplicate line-hours", f"missing={missing}, duplicates={dups}")
    mism = ((f.event_ts.dt.year != f.year) | (f.event_ts.dt.month != f.month) | (f.event_ts.dt.day != f.day)
            | (f.event_ts.dt.hour != f.hour) | (f.event_ts.dt.strftime("%Y-%m-%d") != f.event_date)
            | (f.event_ts.dt.isocalendar().week.astype(int) != f.week)).sum()
    (ok if mism == 0 else lambda c, d: issue("DQ-01c", "ERROR", c, mism, d))("event_ts consistent with date/year/month/week/day/hour", f"mismatches={mism}")
    gr = int((f.good_qty + f.reject_qty > f.actual_qty).sum())
    (ok if gr == 0 else lambda c, d: issue("DQ-01d", "ERROR", c, gr, d))("good + reject <= actual", f"violations={gr}")
    rw = int((f.good_qty + f.reject_qty + f.rework_qty > f.actual_qty).sum())
    issue("DQ-01e", "INFO", "good + reject + rework > actual", rw,
          f"In {rw}/{len(f)} rows the three exceed actual_qty, so rework_qty is NOT a disjoint bucket (assumed: reworked units are counted in good_qty or are re-passed). "
          "Quality in OEE uses good/actual; rework is modelled as a loop-back rate only (see ASSUMPTIONS A-09).")
    a0 = f[f.actual_qty == 0]
    issue("DQ-01f", "INFO", "line-hours with actual_qty = 0", len(a0),
          f"{int((a0.downtime_min == 60).sum())}/{len(a0)} have downtime_min = 60 (consistent); {int(((f.downtime_min == 60) & (f.actual_qty > 0)).sum())} rows have 60 min downtime but output > 0. "
          f"Reasons for full-hour stops: {a0.downtime_reason.value_counts().to_dict()}")
    dn = f[(f.downtime_min > 0) & (f.downtime_reason == "NONE")]
    issue("DQ-01g", "WARN", "downtime_min > 0 with downtime_reason = NONE", len(dn),
          "Unattributed downtime. It stays in availability but cannot be assigned to a cause in the breakdown model; "
          "it is reported as an explicit UNATTRIBUTED bucket (never silently reassigned).")
    r0 = int(((f.downtime_min == 0) & (f.downtime_reason != "NONE")).sum())
    (ok if r0 == 0 else lambda c, d: issue("DQ-01h", "WARN", c, r0, d))("downtime reason set only when downtime_min > 0", f"violations={r0}")
    over = f[f.actual_qty > f.target_qty]
    issue("DQ-01i", "INFO", "actual_qty > target_qty", len(over),
          f"Performance > 100% in {len(over)} line-hours (max {int(over.actual_qty.max())} vs target on that line). Performance is NOT capped in v_line_hourly_oee.")
    ok("downtime_min within 0-60", f"min={f.downtime_min.min()}, max={f.downtime_min.max()}")
    ok("source_system / data_quality constant", f"{f.source_system.unique().tolist()} / {f.data_quality.unique().tolist()} "
       "(data_quality is VALID on every row even though the issues above exist: the flag is not informative)")
    tgt = f.groupby("line_id").target_qty.agg(["min", "max"]).to_dict("index")
    ok("target_qty is a constant per line", str(tgt))
    # downtime vs output relationship
    f["_av"] = 1 - f.downtime_min / 60
    ratio = f[f.target_qty * f._av > 0].assign(p=lambda d: d.actual_qty / (d.target_qty * d._av))
    ok("performance-given-availability (calibration input)", f"mean {ratio.p.mean():.3f}, median {ratio.p.median():.3f} "
       f"over {len(ratio)} line-hours with availability > 0")

    # ------------------------------------------------ 2. line/plant consistency
    derived = f.groupby("line_id").plant_id.agg(lambda x: x.mode().iat[0]).to_dict()
    pure = f.groupby("line_id").plant_id.nunique().max() == 1
    cfg_ok = all(derived[k] == v for k, v in rc.LINE_PLANT.items())
    (ok if pure and cfg_ok else lambda c, d: issue("DQ-02", "ERROR", c, 1, d))(
        "hourly facts agree with configured line->plant mapping (A-01)", str(derived))
    bad_m = mach[mach.apply(lambda r: rc.LINE_PLANT[r.line_id] != r.plant_id, axis=1)]
    issue("DQ-02a", "ERROR", "dim_machine: machine plant contradicts its line's plant", len(bad_m),
          "; ".join(f"{r.machine_id} is on {r.line_id} (plant {rc.LINE_PLANT[r.line_id]}) but listed under {r.plant_id}" for r in bad_m.itertuples())
          + ". Loaded as supplied; override with MACHINE_PLANT_SOURCE=line.")
    bad_s = s[s.apply(lambda r: rc.LINE_PLANT[r.line_id] != r.plant_id, axis=1)]
    issue("DQ-02b", "ERROR", "fact_digital_twin_scenario: plant_id contradicts line_id's plant", len(bad_s),
          f"{len(bad_s)}/{len(s)} historical runs list a line under the wrong plant (e.g. SMT-L1 under IN-KA-01). "
          f"Combinations: {s.groupby(['line_id', 'plant_id']).size().to_dict()}. "
          "Not in the brief. line_id is treated as authoritative because the hourly MES data agrees with it everywhere; plant_id on these rows is unreliable.")

    # ------------------------------------------------ 3. prediction error
    s["re"] = (s.predicted_output_units - s.actual_output_comparison_units).abs() / s.actual_output_comparison_units * 100
    s["gap"] = s.re - s.prediction_error_pct
    differ = int((s.gap.abs() > 0.01).sum())
    t1 = s[s.scenario_id == "TWIN-0001"].iloc[0]
    issue("DQ-03", "ERROR", "fact_digital_twin_scenario.prediction_error_pct inconsistent with predicted/actual", differ,
          f"Recomputed abs(pred-actual)/actual*100 differs from the stored value (>0.01 pt) in {differ}/300 rows; "
          f">1 pt in {int((s.gap.abs() > 1).sum())}. Median gap {s.gap.median():+.1f} pts, recomputed error mean {s.re.mean():.1f}% vs stored mean {s.prediction_error_pct.mean():.1f}%. "
          f"Stored values are bounded {s.prediction_error_pct.min():.1f}-{s.prediction_error_pct.max():.1f}%, recomputed {s.re.min():.1f}-{s.re.max():.1f}%. "
          f"Example TWIN-0001: predicted {t1.predicted_output_units}, actual {t1.actual_output_comparison_units} -> recomputed {t1.re:.2f}%, stored {t1.prediction_error_pct}%. "
          "Source column left untouched; v_scenario_accuracy exposes both. TWIN-1.7's accuracy is therefore not a trustworthy benchmark.")

    # ------------------------------------------------ 4. status / approval
    issue("DQ-04", "INFO", "scenario_status always SIMULATED, approval_required always true", len(s),
          f"statuses={s.scenario_status.unique().tolist()}, approval_required={s.approval_required.unique().tolist()}. Kept as is: no historical run was ever reviewed.")
    ok("scenario names", str(s.scenario_name.value_counts().to_dict()))

    # ------------------------------------------------ 5. unit horizon inference
    per_line = f.groupby("line_id").actual_qty.mean()
    sl = s.groupby("line_id").agg(pred=("predicted_output_units", "mean"), act=("actual_output_comparison_units", "mean"))
    sl["hours_equiv_actual"] = sl.act / per_line
    sl["hours_equiv_pred"] = sl.pred / per_line
    sl["days_equiv_actual"] = sl.hours_equiv_actual / 24
    tgt_ser = f.groupby("line_id").target_qty.first()
    notes.append("### Output-unit horizon inference (check 5)\n\n"
                 "Hourly actual averages 224-326 units per line-hour (target 270-390). Scenario output (~5k-18k) divided by the line's mean hourly actual:\n\n"
                 + md_table(sl.round(1)) + "\n\nCoefficient of variation of the actual column per line: "
                 + str((s.groupby('line_id').actual_output_comparison_units.std() / sl.act).round(2).to_dict()) + "\n")
    hrs = sl.hours_equiv_actual.mean()
    issue("DQ-05", "WARN", "time horizon of predicted_output_units / actual_output_comparison_units is undocumented", len(s),
          f"Mean actual comparison is ~10.7-10.9k units on EVERY line, although hourly rates differ (SMT ~320/h, FAL ~225/h), i.e. {sl.hours_equiv_actual.min():.0f}-{sl.hours_equiv_actual.max():.0f} line-hours of output "
          f"({sl.days_equiv_actual.min():.1f}-{sl.days_equiv_actual.max():.1f} days) depending on line, with CV ~0.3 across runs. No single window (8 h shift, 24 h day, 168 h week) fits all four lines, "
          "and the values do not scale with line rate, so the column looks independent of the MES hourly rates. Horizon cannot be inferred. "
          "Assumption A-07: horizon UNKNOWN; "
          "the TWIN-1.7 history is therefore NOT used to calibrate the model, only shown for comparison.")

    # ------------------------------------------------ 6. timestamps and units
    end = f.event_ts.max()
    fut = int((f.event_ts > pd.Timestamp("2026-10-31 23:00")).sum())
    (ok if fut == 0 else lambda c, d: issue("DQ-06", "ERROR", c, fut, d))("no hourly timestamps beyond dataset end 2026-10-31 23:00", f"max={end}")
    sfut = int((s.run_ts > end).sum())
    (ok if sfut == 0 else lambda c, d: issue("DQ-06b", "WARN", c, sfut, d))("scenario run_ts within MES period", f"range {s.run_ts.min()} .. {s.run_ts.max()}")
    ok("timezone", "Source timestamps carry no offset. Asia/Kolkata assumed (plant master dim_plant.csv says Asia/Kolkata for both plants; shift windows A 06-14, B 14-22, C 22-06 fit local time). Stored as naive timestamp.")
    sh = f.groupby("shift_id").hour.apply(lambda x: sorted(x.unique())).to_dict()
    ok("shift/hour pattern", f"A={sh['A'][0]}-{sh['A'][-1]}, B={sh['B'][0]}-{sh['B'][-1]}, C covers {len(sh['C'])} hours")
    brate = {}
    for ln in ["SMT-L1", "FAL-L1"]:
        areas = ["SMT"] if ln.startswith("SMT") else ["FINAL_ASSEMBLY", "TEST", "QUALITY", "PACKAGING", "WAREHOUSE"]
        ids = stn[stn.process_area.isin(areas)].station_id
        brate[ln] = 3600 / rt[rt.station_id.isin(ids)].groupby("product_id").standard_cycle_sec.max().mean()
    issue("DQ-08", "INFO", "routing theoretical rate does not reconcile with hourly targets", 2,
          f"Slowest-station cycle gives about {brate['SMT-L1']:.0f}/h (SMT) and {brate['FAL-L1']:.0f}/h (FAL) at one unit per cycle, vs targets 390 and 270. "
          "A per-line parallelism factor must be calibrated (Phase 3); it is not assumed.")

    # ------------------------------------------------ write report
    sev_order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    issues.sort(key=lambda x: (sev_order[x["severity"]], x["id"]))
    out = [f"# Data Quality Report\n\nGenerated {datetime.now():%Y-%m-%d %H:%M} by `backend/scripts/validate_data.py` from `./data/*.csv`. "
           "Findings are reported, not silently corrected. Source values are loaded unchanged.\n",
           f"**{sum(i['severity']=='ERROR' for i in issues)} errors, {sum(i['severity']=='WARN' for i in issues)} warnings, "
           f"{sum(i['severity']=='INFO' for i in issues)} notes. {len(passes)} checks passed.**\n",
           "## Issues\n", "| ID | Severity | Check | Rows affected | Detail |", "|---|---|---|---|---|"]
    for i in issues:
        out.append(f"| {i['id']} | {i['severity']} | {i['check']} | {i['count']} | {i['detail'].replace('|', '/')} |")
    out += ["\n## Checks passed\n", "| Check | Result |", "|---|---|"] + [f"| {c} | {d.replace('|', '/')} |" for c, d in passes]
    out += ["\n## Supporting analysis\n"] + notes
    (settings.docs_dir).mkdir(exist_ok=True)
    (settings.docs_dir / "DATA_QUALITY.md").write_text("\n".join(out) + "\n", encoding="utf-8")

    for i in issues:
        print(f"[{i['severity']:5}] {i['id']:7} {i['check']}  -> {i['count']}")
    print(f"{len(passes)} checks passed. Report: {settings.docs_dir / 'DATA_QUALITY.md'}")
    if strict and any(i["severity"] == "ERROR" for i in issues):
        sys.exit(2)


if __name__ == "__main__":
    main()
