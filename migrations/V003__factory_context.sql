-- V003: plant-context tables from data_factory/ (machine telemetry, downtime events, maintenance, quality inspections,
-- workforce, inventory, shipments, energy). Loaded AS SUPPLIED by scripts/load_factory_data.py: no rows dropped or "fixed".
-- Deliberately no foreign keys: the supplied files contain known cross-references that disagree (docs/DATA_QUALITY.md), and the
-- loader must report them, not fail or alter them. All objects schema-qualified (PgBouncer).
CREATE TABLE IF NOT EXISTS twin.dim_production_line (
  line_id text PRIMARY KEY, plant_id text, line_type text, nominal_capacity_per_hour numeric, takt_time_sec numeric, area text
);
CREATE TABLE IF NOT EXISTS twin.fact_machine_telemetry_hourly (
  telemetry_id text PRIMARY KEY, "timestamp" timestamp NOT NULL, date date, machine_id text NOT NULL, plant_id text, line_id text, shift_id text,
  temperature_c numeric, vibration_mm_s numeric, motor_current_a numeric, pressure_bar numeric, rpm numeric,
  machine_state text, alarm_code text, sensor_health text, is_anomaly boolean
);
CREATE INDEX IF NOT EXISTS ix_fmt_machine_ts ON twin.fact_machine_telemetry_hourly (machine_id, "timestamp");
CREATE TABLE IF NOT EXISTS twin.fact_downtime_event (
  downtime_id text PRIMARY KEY, machine_id text, plant_id text, line_id text, start_ts timestamp, duration_min numeric, reason_code text,
  impact_units numeric, maintenance_required boolean, acknowledged_by text, closure_status text
);
CREATE INDEX IF NOT EXISTS ix_fde_machine ON twin.fact_downtime_event (machine_id);
CREATE TABLE IF NOT EXISTS twin.fact_maintenance_work_order (
  work_order_id text PRIMARY KEY, machine_id text, plant_id text, created_ts timestamp, maintenance_type text, failure_mode text, priority text,
  planned_start timestamp, actual_start timestamp, downtime_avoided_estimate_min numeric, status text, technician_id text
);
CREATE INDEX IF NOT EXISTS ix_fmwo_machine ON twin.fact_maintenance_work_order (machine_id, status);
CREATE TABLE IF NOT EXISTS twin.fact_quality_inspection (
  inspection_id text PRIMARY KEY, inspection_ts timestamp, plant_id text, line_id text, station_id text, product_id text, serial_number text,
  production_order_id text, inspection_stage text, defect_code text, result text, severity text, measurement_value numeric, spec_min numeric,
  spec_max numeric, inspector_id text, disposition text, spec_revision text, source_system text
);
CREATE INDEX IF NOT EXISTS ix_fqi_station ON twin.fact_quality_inspection (plant_id, station_id);
CREATE TABLE IF NOT EXISTS twin.fact_inventory_daily_snapshot (
  snapshot_date date, plant_id text, material_id text, warehouse_id text, lot_id text, on_hand_qty numeric, allocated_qty numeric, blocked_qty numeric,
  quality_hold_qty numeric, in_transit_qty numeric, safety_stock_qty numeric, inventory_status text, unit_cost_inr numeric, expiry_date date
);
CREATE INDEX IF NOT EXISTS ix_fids_plant_date ON twin.fact_inventory_daily_snapshot (plant_id, snapshot_date);
CREATE TABLE IF NOT EXISTS twin.fact_shipment_milestone (
  shipment_id text PRIMARY KEY, customer_order_id text, plant_id text, warehouse_id text, carrier text, vehicle_type text, dispatch_date date,
  planned_eta timestamp, actual_delivery_ts timestamp, shipment_qty numeric, freight_cost_inr numeric, vehicle_utilization_pct numeric,
  pod_status text, shipment_status text
);
CREATE TABLE IF NOT EXISTS twin.fact_energy_meter_hourly (
  meter_reading_id text PRIMARY KEY, "timestamp" timestamp NOT NULL, date date, plant_id text, meter_id text, area text, energy_source text,
  consumption_kwh numeric, demand_kw numeric, power_factor numeric, tariff_inr_per_kwh numeric, cost_inr numeric, co2e_kg numeric, reading_quality text
);
CREATE INDEX IF NOT EXISTS ix_femh_plant_ts ON twin.fact_energy_meter_hourly (plant_id, "timestamp");
CREATE TABLE IF NOT EXISTS twin.dim_workforce (
  employee_id text PRIMARY KEY, team_id text, role text, skill_grade text, certification text, home_shift text, active_status text
);
CREATE TABLE IF NOT EXISTS twin.fact_workforce_attendance (
  attendance_id text PRIMARY KEY, work_date date, employee_id text, plant_id text, shift_id text, scheduled_hours numeric, actual_hours numeric,
  attendance_status text, overtime_hours numeric, skill_match text, training_compliance text
);
CREATE INDEX IF NOT EXISTS ix_fwa_plant_date ON twin.fact_workforce_attendance (plant_id, work_date);
