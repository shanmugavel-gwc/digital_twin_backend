"""Plant-context signals for the 3D factory view. LIVE = measured values from the loaded data_factory export (hourly files, not a stream).
Read-only. Nothing here commands, acknowledges or writes to any plant system."""
from fastapi import APIRouter, Depends, Query

from ..deps import envelope, query, require_api_key

router = APIRouter(prefix="/twin", tags=["signals"], dependencies=[Depends(require_api_key)])

ASSUMPTIONS = [
    {"id": "SIG-1", "text": "LIVE values come from the loaded data_factory export (hourly files). They are anchored on the latest hour in the file, not wall-clock time, and the data age is reported."},
    {"id": "SIG-2", "text": "Telemetry is per machine. Machines are placed at stations by machine type (assumption A-03); the supplied files give no machine-to-station key."},
    {"id": "SIG-3", "text": "Quality pass rate = share of inspections with result PASS per station (inspections, not units: it is not a first-pass yield)."},
    {"id": "SIG-4", "text": "Downtime events and work orders cover the whole supplied period (Aug to Oct 2026); machine and plant ids are used as supplied (see docs/DATA_QUALITY.md)."},
]


@router.get("/machine-signals")
def machine_signals():
    """LIVE: latest telemetry hour, 24 h state mix and anomalies, downtime events and work orders, per machine."""
    anchor = query('SELECT max("timestamp") AS ts FROM twin.fact_machine_telemetry_hourly')[0]["ts"]
    latest = query('SELECT DISTINCT ON (machine_id) machine_id, "timestamp" AS ts, temperature_c, vibration_mm_s, motor_current_a, pressure_bar, rpm, '
                   "machine_state, alarm_code, sensor_health, is_anomaly FROM twin.fact_machine_telemetry_hourly "
                   'ORDER BY machine_id, "timestamp" DESC')
    mix = query('SELECT machine_id, machine_state, count(*) AS n, sum(is_anomaly::int) AS anomalies FROM twin.fact_machine_telemetry_hourly '
                'WHERE "timestamp" > :t - interval \'24 hours\' GROUP BY 1, 2', t=anchor)
    dt = query("SELECT machine_id, count(*) AS events, count(*) FILTER (WHERE closure_status = 'OPEN') AS open_events, sum(duration_min) AS minutes "
               "FROM twin.fact_downtime_event GROUP BY 1")
    wo = query("SELECT machine_id, status, count(*) AS n FROM twin.fact_maintenance_work_order GROUP BY 1, 2")
    out: dict[str, dict] = {}
    for r in latest:
        out[r["machine_id"]] = {**r, "ts": r["ts"].isoformat(), "state_mix_24h": {}, "anomalies_24h": 0, "downtime": {"events": 0, "open_events": 0, "minutes": 0}, "work_orders": {}}
    for r in mix:
        m = out.get(r["machine_id"])
        if m:
            m["state_mix_24h"][r["machine_state"]] = r["n"]
            m["anomalies_24h"] += int(r["anomalies"] or 0)
    for r in dt:
        if r["machine_id"] in out:
            out[r["machine_id"]]["downtime"] = {"events": r["events"], "open_events": r["open_events"], "minutes": float(r["minutes"] or 0)}
    for r in wo:
        if r["machine_id"] in out:
            out[r["machine_id"]]["work_orders"][r["status"]] = r["n"]
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=ASSUMPTIONS, as_of=anchor.isoformat() if anchor else None, machines=out)


@router.get("/plant-context")
def plant_context(plant_id: str = Query(pattern="^IN-(TN|KA)-01$")):
    """LIVE: workforce, warehouse stock, shipments, energy and station quality for one plant."""
    p = {"p": plant_id}
    wf_date = query("SELECT max(work_date) AS d FROM twin.fact_workforce_attendance WHERE plant_id = :p", **p)[0]["d"]
    workforce = query("SELECT shift_id, attendance_status, count(*) AS n, sum(actual_hours) AS hours FROM twin.fact_workforce_attendance "
                      "WHERE plant_id = :p AND work_date = :d GROUP BY 1, 2 ORDER BY 1, 2", d=wf_date, **p) if wf_date else []
    inv_date = query("SELECT max(snapshot_date) AS d FROM twin.fact_inventory_daily_snapshot WHERE plant_id = :p", **p)[0]["d"]
    inventory = query("SELECT warehouse_id, count(*) AS lots, sum(on_hand_qty) AS on_hand, sum(quality_hold_qty) AS quality_hold, sum(blocked_qty) AS blocked, "
                      "sum(in_transit_qty) AS in_transit, sum(safety_stock_qty) AS safety_stock FROM twin.fact_inventory_daily_snapshot "
                      "WHERE plant_id = :p AND snapshot_date = :d GROUP BY 1 ORDER BY 1", d=inv_date, **p) if inv_date else []
    shipments = query("SELECT shipment_status, count(*) AS n, sum(shipment_qty) AS qty, max(dispatch_date) AS last_dispatch FROM twin.fact_shipment_milestone "
                      "WHERE plant_id = :p GROUP BY 1 ORDER BY 1", **p)
    en_ts = query('SELECT max("timestamp") AS ts FROM twin.fact_energy_meter_hourly WHERE plant_id = :p', **p)[0]["ts"]
    energy = query('SELECT area, sum(consumption_kwh) AS kwh, avg(demand_kw) AS avg_kw FROM twin.fact_energy_meter_hourly '
                   'WHERE plant_id = :p AND "timestamp" > :t - interval \'24 hours\' GROUP BY 1 ORDER BY 1', t=en_ts, **p) if en_ts else []
    quality = query("SELECT station_id, count(*) AS inspections, count(*) FILTER (WHERE result = 'PASS') AS passed FROM twin.fact_quality_inspection "
                    "WHERE plant_id = :p GROUP BY 1 ORDER BY 1", **p)
    iso = lambda d: d.isoformat() if d else None
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=ASSUMPTIONS, plant_id=plant_id,
                    workforce={"work_date": iso(wf_date), "rows": workforce}, inventory={"snapshot_date": iso(inv_date), "warehouses": inventory},
                    shipments=[{**s, "last_dispatch": iso(s["last_dispatch"])} for s in shipments],
                    energy={"as_of": iso(en_ts), "areas": energy}, quality_by_station=quality)
