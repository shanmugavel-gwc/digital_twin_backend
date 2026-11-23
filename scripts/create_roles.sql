-- Run by the OWNER on the DIRECT Neon URL AFTER db_init.py (tables must exist). Use scripts/apply_roles.py,
-- which substitutes {{RW_PASSWORD}} / {{RO_PASSWORD}} from the environment. Idempotent.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'twin_rw') THEN CREATE ROLE twin_rw LOGIN PASSWORD {{RW_PASSWORD}}; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'twin_ro') THEN CREATE ROLE twin_ro LOGIN PASSWORD {{RO_PASSWORD}}; END IF;
END $$;
ALTER ROLE twin_rw PASSWORD {{RW_PASSWORD}};
ALTER ROLE twin_ro PASSWORD {{RO_PASSWORD}};
ALTER ROLE twin_ro SET default_transaction_read_only = on;
ALTER ROLE twin_ro SET statement_timeout = '8s';

-- Start from nothing on both roles.
REVOKE ALL ON ALL TABLES IN SCHEMA twin FROM twin_rw, twin_ro;
REVOKE ALL ON SCHEMA public FROM twin_ro;
GRANT USAGE ON SCHEMA twin TO twin_rw, twin_ro;

-- twin_rw (the app): DML on everything. No DDL, no TRUNCATE. audit_log is INSERT/SELECT only (also trigger-protected).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA twin TO twin_rw;
REVOKE UPDATE, DELETE ON twin.audit_log FROM twin_rw;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA twin TO twin_rw;

-- twin_ro (agent SQL tool ONLY): explicit allow-list of analytical objects. NOT conversations, messages,
-- audit_log, scenario_review or schema_migrations.
GRANT SELECT ON
  twin.dim_plant, twin.dim_line, twin.dim_machine, twin.dim_station, twin.dim_product_routing,
  twin.fact_hourly_production, twin.fact_digital_twin_scenario,
  twin.station_line_map, twin.machine_station_map, twin.buffer_config,
  twin.twin_model_version, twin.twin_calibration_run, twin.twin_scenario_run, twin.twin_station_result,
  twin.v_line_hourly_oee, twin.v_line_daily_actuals, twin.v_scenario_accuracy, twin.v_data_freshness,
  twin.dim_production_line, twin.dim_workforce, twin.fact_machine_telemetry_hourly, twin.fact_downtime_event, twin.fact_maintenance_work_order,
  twin.fact_quality_inspection, twin.fact_inventory_daily_snapshot, twin.fact_shipment_milestone, twin.fact_energy_meter_hourly,
  twin.fact_workforce_attendance,
  twin.dim_defect_code, twin.fact_ncr_capa, twin.agent_run, twin.agent_finding, twin.agent_machine_state, twin.recommendation,
  twin.v_machine_3d_mapping
TO twin_ro;

-- Default privileges: only twin_rw gets auto-grants on future tables. twin_ro gets none: new analytical
-- tables must be added to the explicit allow-list above.
ALTER DEFAULT PRIVILEGES IN SCHEMA twin REVOKE ALL ON TABLES FROM twin_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA twin GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO twin_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA twin GRANT USAGE, SELECT ON SEQUENCES TO twin_rw;
