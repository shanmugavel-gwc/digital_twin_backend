"""Hour-by-hour REPLAY of the loaded plant data for the campus twin.

The supplied data is a historical export, not a stream. To make the twin move like a live view without inventing anything, the UI
plays back the recorded hours on a clock. Every frame returned here is one real recorded hour: machine states and sensor readings,
line output, inspections, energy by area, plus the day's stock, shipments and attendance. Nothing is interpolated or generated."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import HTTPException

from ..deps import query, query_many

MAX_HOURS = 48


def _f(v):
    return float(v) if v is not None else None


def frames(plant_id: str, start: str | None, hours: int) -> dict:
    hours = max(1, min(int(hours), MAX_HOURS))
    lines = [r["line_id"] for r in query("SELECT line_id FROM twin.dim_line WHERE plant_id = :p ORDER BY 1", p=plant_id)]
    if not lines:
        raise HTTPException(404, "unknown plant")
    b = query('SELECT min("timestamp") AS lo, max("timestamp") AS hi FROM twin.fact_machine_telemetry_hourly')[0]
    lo, hi = b["lo"], b["hi"]
    if start:
        try:
            t0 = datetime.fromisoformat(start).replace(minute=0, second=0, microsecond=0)
        except ValueError:
            raise HTTPException(422, "start must be an ISO date-time, e.g. 2026-10-31T00:00")
    else:
        t0 = hi - timedelta(hours=hours - 1)          # default: the last recorded hours
    t0 = max(lo, min(t0, hi))
    t1 = min(hi, t0 + timedelta(hours=hours - 1))
    d0, d1 = t0.date(), t1.date()
    p = {"p": plant_id, "l": lines, "a": t0, "b": t1, "d0": d0, "d1": d1}
    R = query_many({
        "tel": ('SELECT t."timestamp" AS ts, t.machine_id, t.machine_state, t.temperature_c, t.vibration_mm_s, t.motor_current_a, t.alarm_code, t.is_anomaly '
                'FROM twin.fact_machine_telemetry_hourly t JOIN twin.dim_machine m USING (machine_id) '
                'WHERE m.line_id = ANY(:l) AND t."timestamp" BETWEEN :a AND :b ORDER BY 1, 2', p),
        "prod": ("SELECT event_ts AS ts, line_id, shift_id, target_qty, actual_qty, good_qty, reject_qty, downtime_min, downtime_reason "
                 "FROM twin.fact_hourly_production WHERE line_id = ANY(:l) AND event_ts BETWEEN :a AND :b ORDER BY 1, 2", p),
        "qual": ("SELECT date_trunc('hour', inspection_ts) AS ts, count(*) AS n, count(*) FILTER (WHERE result = 'FAIL') AS fails, "
                 "max(defect_code) FILTER (WHERE result = 'FAIL') AS defect FROM twin.fact_quality_inspection WHERE line_id = ANY(:l) "
                 "AND inspection_ts >= :a AND inspection_ts < CAST(:b AS timestamp) + interval '1 hour' GROUP BY 1 ORDER BY 1", p),
        "energy": ('SELECT "timestamp" AS ts, area, sum(consumption_kwh) AS kwh, sum(demand_kw) AS kw, avg(power_factor) AS pf FROM twin.fact_energy_meter_hourly '
                   'WHERE plant_id = :p AND "timestamp" BETWEEN :a AND :b GROUP BY 1, 2 ORDER BY 1, 2', p),
        "stock": ("SELECT snapshot_date AS d, warehouse_id, sum(on_hand_qty) AS on_hand, sum(quality_hold_qty) AS hold FROM twin.fact_inventory_daily_snapshot "
                  "WHERE plant_id = :p AND snapshot_date BETWEEN :d0 AND :d1 GROUP BY 1, 2 ORDER BY 1, 2", p),
        "ship": ("SELECT dispatch_date AS d, count(*) AS n, sum(shipment_qty) AS qty, count(*) FILTER (WHERE shipment_status = 'DELAYED') AS delayed "
                 "FROM twin.fact_shipment_milestone WHERE plant_id = :p AND dispatch_date BETWEEN :d0 AND :d1 GROUP BY 1", p),
        "staff": ("SELECT work_date AS d, shift_id, count(*) FILTER (WHERE attendance_status IN ('PRESENT', 'LATE')) AS present, count(*) AS rostered "
                  "FROM twin.fact_workforce_attendance WHERE plant_id = :p AND work_date BETWEEN :d0 AND :d1 GROUP BY 1, 2", p),
        "dt": ("SELECT date_trunc('hour', start_ts) AS ts, machine_id, reason_code, duration_min FROM twin.fact_downtime_event d JOIN twin.dim_machine m USING (machine_id) "
               "WHERE m.line_id = ANY(:l) AND start_ts BETWEEN :a AND :b", p),
    }, workers=8)
    by = {}
    n_hours = int((t1 - t0).total_seconds() // 3600) + 1
    for i in range(n_hours):
        ts = t0 + timedelta(hours=i)
        by[ts] = {"ts": ts.isoformat(), "machines": {}, "lines": {}, "quality": {"n": 0, "fails": 0, "defect": None}, "energy": {}, "downtime_events": []}
    for r in R["tel"]:
        f = by.get(r["ts"])
        if f:
            f["machines"][r["machine_id"]] = {"state": r["machine_state"], "temp": _f(r["temperature_c"]), "vib": _f(r["vibration_mm_s"]),
                                              "current": _f(r["motor_current_a"]), "alarm": r["alarm_code"], "anomaly": bool(r["is_anomaly"])}
    for r in R["prod"]:
        f = by.get(r["ts"])
        if f:
            f["lines"][r["line_id"]] = {"shift": r["shift_id"], "target": _f(r["target_qty"]), "actual": _f(r["actual_qty"]), "good": _f(r["good_qty"]),
                                        "reject": _f(r["reject_qty"]), "downtime_min": _f(r["downtime_min"]), "reason": r["downtime_reason"]}
    for r in R["qual"]:
        f = by.get(r["ts"])
        if f:
            f["quality"] = {"n": r["n"], "fails": r["fails"], "defect": r["defect"]}
    for r in R["energy"]:
        f = by.get(r["ts"])
        if f:
            f["energy"][r["area"]] = {"kwh": _f(r["kwh"]), "kw": _f(r["kw"]), "pf": _f(r["pf"])}
    for r in R["dt"]:
        f = by.get(r["ts"])
        if f:
            f["downtime_events"].append({"machine_id": r["machine_id"], "reason": r["reason_code"], "minutes": _f(r["duration_min"])})
    days = {}
    for r in R["stock"]:
        days.setdefault(r["d"].isoformat(), {"stock": {}, "shipments": None, "staff": {}})["stock"][r["warehouse_id"]] = {"on_hand": _f(r["on_hand"]), "hold": _f(r["hold"])}
    for r in R["ship"]:
        days.setdefault(r["d"].isoformat(), {"stock": {}, "shipments": None, "staff": {}})["shipments"] = {"n": r["n"], "qty": _f(r["qty"]), "delayed": r["delayed"]}
    for r in R["staff"]:
        days.setdefault(r["d"].isoformat(), {"stock": {}, "shipments": None, "staff": {}})["staff"][r["shift_id"]] = {"present": r["present"], "rostered": r["rostered"]}
    return {"plant_id": plant_id, "lines": lines, "start": t0.isoformat(), "end": t1.isoformat(), "bounds": {"from": lo.isoformat(), "to": hi.isoformat()},
            "frames": list(by.values()), "days": days,
            "note": "Replay of recorded hours from the historical export (Aug-Oct 2026). Each frame is one real hour; nothing is interpolated."}
