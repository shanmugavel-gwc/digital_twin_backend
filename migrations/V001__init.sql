-- Digital Twin Agent: schema V001. Run via scripts/db_init.py on the DIRECT Neon URL (never the pooler).
-- All objects are schema-qualified: Neon's pooler (PgBouncer, transaction mode) cannot rely on search_path.
CREATE SCHEMA IF NOT EXISTS twin;

-- ---------------------------------------------------------------- reference data (section 4.2)
CREATE TABLE IF NOT EXISTS twin.dim_plant (
  plant_id   text PRIMARY KEY,
  plant_name text NOT NULL
);

CREATE TABLE IF NOT EXISTS twin.dim_line (
  line_id        text PRIMARY KEY,
  plant_id       text NOT NULL REFERENCES twin.dim_plant(plant_id),
  process_family text NOT NULL CHECK (process_family IN ('SMT','FAL'))
);

-- ---------------------------------------------------------------- source tables (1:1 with CSVs)
CREATE TABLE IF NOT EXISTS twin.dim_machine (
  machine_id                       text PRIMARY KEY,
  machine_name                     text NOT NULL,
  machine_type                     text NOT NULL,
  line_id                          text NOT NULL REFERENCES twin.dim_line(line_id),
  plant_id                         text NOT NULL REFERENCES twin.dim_plant(plant_id),
  criticality                      text NOT NULL CHECK (criticality IN ('A','B')),
  commissioned_date                date,
  planned_maintenance_interval_days integer
);

CREATE TABLE IF NOT EXISTS twin.dim_station (
  station_id              text PRIMARY KEY,
  station_name            text NOT NULL,
  process_area            text NOT NULL CHECK (process_area IN ('SMT','FINAL_ASSEMBLY','TEST','QUALITY','PACKAGING','WAREHOUSE')),
  sequence_no             integer NOT NULL,
  critical_control_point  boolean NOT NULL
);

-- NOTE: the brief proposed PK (routing_id, operation_seq). operation_seq restarts within each process
-- area in the supplied data (39 duplicate pairs), so it is NOT unique. (routing_id, station_id) is.
CREATE TABLE IF NOT EXISTS twin.dim_product_routing (
  routing_id         text NOT NULL,
  operation_seq      integer NOT NULL,
  product_id         text NOT NULL,
  station_id         text NOT NULL REFERENCES twin.dim_station(station_id),
  standard_cycle_sec integer NOT NULL CHECK (standard_cycle_sec > 0),
  setup_min          integer NOT NULL CHECK (setup_min >= 0),
  quality_gate       boolean NOT NULL,
  revision           text NOT NULL,
  PRIMARY KEY (routing_id, station_id)
);

CREATE TABLE IF NOT EXISTS twin.fact_hourly_production (
  event_id             text PRIMARY KEY,
  event_ts             timestamp NOT NULL,          -- plant-local time (Asia/Kolkata), no tz in source
  event_date           date NOT NULL,
  year                 integer, month integer, week integer, day integer, hour integer,
  plant_id             text NOT NULL REFERENCES twin.dim_plant(plant_id),
  line_id              text NOT NULL REFERENCES twin.dim_line(line_id),
  shift_id             text NOT NULL CHECK (shift_id IN ('A','B','C')),
  product_id           text NOT NULL,
  production_order_id  text,
  target_qty           integer NOT NULL,
  actual_qty           integer NOT NULL,
  good_qty             integer NOT NULL,
  reject_qty           integer NOT NULL,
  rework_qty           integer NOT NULL,
  downtime_min         integer NOT NULL CHECK (downtime_min BETWEEN 0 AND 60),
  downtime_reason      text NOT NULL CHECK (downtime_reason IN ('NONE','CHANGEOVER','EQUIPMENT_FAULT','FEEDER_JAM','MICRO_STOP','NO_MATERIAL')),
  operator_team        text,
  source_system        text NOT NULL,
  data_quality         text NOT NULL,
  -- tolerance 0: the supplied data has 0 violations; the loader reports violations and aborts rather than dropping rows
  CONSTRAINT ck_good_reject_le_actual CHECK (good_qty + reject_qty <= actual_qty + 0)
);
CREATE INDEX IF NOT EXISTS ix_fhp_line_ts      ON twin.fact_hourly_production (line_id, event_ts);
CREATE INDEX IF NOT EXISTS ix_fhp_plant_date   ON twin.fact_hourly_production (plant_id, event_date);
CREATE INDEX IF NOT EXISTS ix_fhp_product      ON twin.fact_hourly_production (product_id);
CREATE INDEX IF NOT EXISTS ix_fhp_downtime     ON twin.fact_hourly_production (downtime_reason);

CREATE TABLE IF NOT EXISTS twin.fact_digital_twin_scenario (
  scenario_id                     text PRIMARY KEY,
  scenario_name                   text NOT NULL,
  plant_id                        text NOT NULL REFERENCES twin.dim_plant(plant_id),  -- see DATA_QUALITY: contradicts line->plant in 158 rows
  line_id                         text NOT NULL REFERENCES twin.dim_line(line_id),
  model_version                   text NOT NULL,
  run_ts                          timestamp NOT NULL,
  demand_change_pct               numeric,
  capacity_change_pct             numeric,
  predicted_output_units          integer,
  predicted_wip_units             integer,
  predicted_utilization_pct       numeric,
  actual_output_comparison_units  integer,
  prediction_error_pct            numeric,   -- stored as supplied; never overwritten (see v_scenario_accuracy)
  scenario_status                 text NOT NULL,
  approval_required               boolean NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fdts_line_name ON twin.fact_digital_twin_scenario (line_id, scenario_name);
CREATE INDEX IF NOT EXISTS ix_fdts_run_ts    ON twin.fact_digital_twin_scenario (run_ts);

-- ---------------------------------------------------------------- derived / assumption-driven mapping tables (editable)
CREATE TABLE IF NOT EXISTS twin.station_line_map (
  station_id text NOT NULL REFERENCES twin.dim_station(station_id),
  line_id    text NOT NULL REFERENCES twin.dim_line(line_id),
  role       text NOT NULL,
  PRIMARY KEY (station_id, line_id)
);

CREATE TABLE IF NOT EXISTS twin.machine_station_map (
  machine_id text PRIMARY KEY REFERENCES twin.dim_machine(machine_id),
  station_id text NOT NULL REFERENCES twin.dim_station(station_id)
);

CREATE TABLE IF NOT EXISTS twin.buffer_config (
  line_id         text NOT NULL REFERENCES twin.dim_line(line_id),
  from_station_id text NOT NULL REFERENCES twin.dim_station(station_id),
  to_station_id   text NOT NULL REFERENCES twin.dim_station(station_id),
  capacity_units  integer NOT NULL CHECK (capacity_units >= 0),
  PRIMARY KEY (line_id, from_station_id, to_station_id)
);

-- ---------------------------------------------------------------- twin-owned tables (section 4.3)
CREATE TABLE IF NOT EXISTS twin.twin_model_version (
  model_version      text PRIMARY KEY,
  created_at         timestamptz NOT NULL DEFAULT now(),
  description        text,
  calibration_run_id bigint,
  is_active          boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS twin.twin_calibration_run (
  run_id        bigserial PRIMARY KEY,
  model_version text NOT NULL REFERENCES twin.twin_model_version(model_version),
  period_start  timestamp,
  period_end    timestamp,
  line_id       text REFERENCES twin.dim_line(line_id),
  params        jsonb,
  mape_pct      numeric, bias_pct numeric, rmse numeric,
  n_hours       integer,
  status        text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS twin.twin_scenario_run (
  run_id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario_name             text NOT NULL,
  line_id                   text NOT NULL REFERENCES twin.dim_line(line_id),
  plant_id                  text NOT NULL REFERENCES twin.dim_plant(plant_id),
  model_version             text NOT NULL REFERENCES twin.twin_model_version(model_version),
  requested_by              text,
  params                    jsonb NOT NULL DEFAULT '{}'::jsonb,
  assumptions               jsonb NOT NULL DEFAULT '[]'::jsonb,
  state_type                text NOT NULL DEFAULT 'SIMULATED' CHECK (state_type = 'SIMULATED'),
  status                    text NOT NULL DEFAULT 'DRAFT'
                              CHECK (status IN ('DRAFT','SIMULATED','PENDING_REVIEW','APPROVED','REJECTED')),
  approval_required         boolean NOT NULL DEFAULT true CHECK (approval_required),
  predicted_output_units    numeric,
  predicted_wip_units       numeric,
  predicted_utilization_pct numeric,
  bottleneck_station_id     text REFERENCES twin.dim_station(station_id),
  confidence_score          numeric,
  confidence_band_low       numeric,
  confidence_band_high      numeric,
  validation_status         text,
  turnaround_ms             integer,
  run_ts                    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tsr_line_name ON twin.twin_scenario_run (line_id, scenario_name);
CREATE INDEX IF NOT EXISTS ix_tsr_status    ON twin.twin_scenario_run (status);

CREATE TABLE IF NOT EXISTS twin.twin_station_result (
  run_id         uuid NOT NULL REFERENCES twin.twin_scenario_run(run_id) ON DELETE CASCADE,
  station_id     text NOT NULL REFERENCES twin.dim_station(station_id),
  utilization_pct numeric, avg_queue_units numeric, avg_wait_sec numeric,
  blocked_pct numeric, starved_pct numeric,
  is_bottleneck  boolean NOT NULL DEFAULT false,
  PRIMARY KEY (run_id, station_id)
);

CREATE TABLE IF NOT EXISTS twin.scenario_review (
  id         bigserial PRIMARY KEY,
  run_id     uuid NOT NULL REFERENCES twin.twin_scenario_run(run_id),
  reviewer   text NOT NULL CHECK (length(btrim(reviewer)) > 0),
  decision   text NOT NULL CHECK (decision IN ('APPROVED','REJECTED')),
  comment    text NOT NULL CHECK (length(btrim(comment)) > 0),
  decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS twin.audit_log (
  id        bigserial PRIMARY KEY,
  ts        timestamptz NOT NULL DEFAULT now(),
  actor     text NOT NULL,
  action    text NOT NULL,
  entity    text,
  entity_id text,
  details   jsonb
);
-- append-only: block UPDATE / DELETE (TRUNCATE is revoked from the app role in create_roles.sql)
CREATE OR REPLACE FUNCTION twin.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'twin.audit_log is append-only'; END $$;
DROP TRIGGER IF EXISTS trg_audit_immutable ON twin.audit_log;
CREATE TRIGGER trg_audit_immutable BEFORE UPDATE OR DELETE ON twin.audit_log
  FOR EACH ROW EXECUTE FUNCTION twin.audit_log_immutable();

CREATE TABLE IF NOT EXISTS twin.conversations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title      text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS twin.messages (
  id              bigserial PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES twin.conversations(id) ON DELETE CASCADE,
  role            text NOT NULL CHECK (role IN ('user','assistant')),
  content         text NOT NULL,
  steps           jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);

INSERT INTO twin.twin_model_version (model_version, description, is_active) VALUES
  ('TWIN-1.7', 'Baseline model version recorded in fact_digital_twin_scenario (historical runs; not reproducible here).', false),
  ('TWIN-2.0', 'SimPy discrete-event line model built by this agent; calibrated on Aug-Oct 2026 MES data.', true)
ON CONFLICT (model_version) DO NOTHING;

-- ---------------------------------------------------------------- views
-- Performance is NOT capped at 100%: actual_qty exceeds target_qty in some line-hours (see DATA_QUALITY.md).
CREATE OR REPLACE VIEW twin.v_line_hourly_oee AS
SELECT event_id, event_ts, event_date, plant_id, line_id, shift_id, product_id,
       target_qty, actual_qty, good_qty, downtime_min, downtime_reason,
       1 - downtime_min / 60.0                                                          AS availability,
       CASE WHEN target_qty * (1 - downtime_min / 60.0) > 0
            THEN actual_qty / (target_qty * (1 - downtime_min / 60.0)) END              AS performance,
       CASE WHEN actual_qty > 0 THEN good_qty::numeric / actual_qty END                 AS quality,
       CASE WHEN target_qty * (1 - downtime_min / 60.0) > 0 AND actual_qty > 0
            THEN (1 - downtime_min / 60.0)
               * (actual_qty / (target_qty * (1 - downtime_min / 60.0)))
               * (good_qty::numeric / actual_qty) END                                   AS oee,
       'LIVE'::text AS state_type
FROM twin.fact_hourly_production;

CREATE OR REPLACE VIEW twin.v_line_daily_actuals AS
SELECT line_id, plant_id, event_date,
       sum(target_qty) AS target_qty, sum(actual_qty) AS actual_qty, sum(good_qty) AS good_qty,
       sum(reject_qty) AS reject_qty, sum(rework_qty) AS rework_qty, sum(downtime_min) AS downtime_min,
       1 - sum(downtime_min) / (60.0 * count(*))                                       AS availability,
       sum(actual_qty)::numeric / NULLIF(sum(target_qty),0)                            AS plan_attainment,
       sum(good_qty)::numeric / NULLIF(sum(actual_qty),0)                              AS quality,
       count(*) AS hours_reported,
       'LIVE'::text AS state_type
FROM twin.fact_hourly_production
GROUP BY line_id, plant_id, event_date;

CREATE OR REPLACE VIEW twin.v_scenario_accuracy AS
SELECT s.scenario_id, s.scenario_name, s.line_id, s.plant_id AS plant_id_as_stored,
       l.plant_id AS plant_id_from_line, (s.plant_id = l.plant_id) AS plant_matches_line,
       s.model_version, s.run_ts, s.predicted_output_units, s.actual_output_comparison_units,
       s.prediction_error_pct AS stored_error_pct,
       round(abs(s.predicted_output_units - s.actual_output_comparison_units)::numeric
             / NULLIF(s.actual_output_comparison_units,0) * 100, 2)                    AS recomputed_error_pct,
       round((s.predicted_output_units - s.actual_output_comparison_units)::numeric
             / NULLIF(s.actual_output_comparison_units,0) * 100, 2)                    AS signed_error_pct,
       round(abs(s.predicted_output_units - s.actual_output_comparison_units)::numeric
             / NULLIF(s.actual_output_comparison_units,0) * 100, 2) - s.prediction_error_pct AS error_gap_pts,
       s.scenario_status, s.approval_required,
       'SIMULATED'::text AS state_type
FROM twin.fact_digital_twin_scenario s
JOIN twin.dim_line l USING (line_id);

CREATE OR REPLACE VIEW twin.v_data_freshness AS
SELECT 'MES:fact_hourly_production'::text AS source, max(event_ts) AS max_ts,
       round((extract(epoch FROM ((now() AT TIME ZONE 'Asia/Kolkata') - max(event_ts))) / 3600.0)::numeric, 1) AS age_hours
FROM twin.fact_hourly_production
UNION ALL
SELECT 'TWIN:fact_digital_twin_scenario', max(run_ts),
       round((extract(epoch FROM ((now() AT TIME ZONE 'Asia/Kolkata') - max(run_ts))) / 3600.0)::numeric, 1)
FROM twin.fact_digital_twin_scenario;

CREATE TABLE IF NOT EXISTS twin.schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
