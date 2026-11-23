-- V004: central Digital Twin Agent platform tables: runs, execution events, findings, per-machine status, recommendations with a
-- human approval workflow (enforced in the database), reports, notifications, enterprise integrations, users/roles, agent config,
-- plus two supplied quality datasets (defect codes, NCR/CAPA) loaded AS SUPPLIED by scripts/load_factory_data.py.
-- All objects schema-qualified (PgBouncer). Nothing here can reach PLC/SCADA.

-- ------------------------------------------------------------------ supplied quality master/fact data (loaded as supplied, no FKs)
CREATE TABLE IF NOT EXISTS twin.dim_defect_code (
  defect_code text PRIMARY KEY, defect_name text, defect_category text, severity text, disposition_default text
);
CREATE TABLE IF NOT EXISTS twin.fact_ncr_capa (
  ncr_id text PRIMARY KEY, inspection_id text, opened_ts timestamp, defect_code text, root_cause_category text,
  containment_action text, owner_id text, capa_due_date date, capa_status text, effectiveness_check text
);

-- ------------------------------------------------------------------ users and roles (identity selection, NOT authentication: see ASSUMPTIONS A-20)
CREATE TABLE IF NOT EXISTS twin.app_user (
  user_id      text PRIMARY KEY CHECK (user_id ~ '^[a-z0-9._-]{2,40}$'),
  display_name text NOT NULL CHECK (length(btrim(display_name)) > 0),
  role         text NOT NULL CHECK (role IN ('VIEWER','ENGINEER','PLANNER','ADMIN')),
  active       boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------ agent configuration (single row, versioned by updated_at)
CREATE TABLE IF NOT EXISTS twin.agent_config (
  id         integer PRIMARY KEY CHECK (id = 1),
  config     jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  updated_by text
);

-- ------------------------------------------------------------------ agent runs and their streamed execution events
CREATE TABLE IF NOT EXISTS twin.agent_run (
  run_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_name        text NOT NULL DEFAULT 'Manufacturing Digital Twin Agent',
  status            text NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','COMPLETED','FAILED')),
  triggered_by      text NOT NULL,
  config            jsonb NOT NULL DEFAULT '{}'::jsonb,
  current_step      integer NOT NULL DEFAULT 0,
  records_processed bigint NOT NULL DEFAULT 0,
  data_as_of        timestamp,
  summary           jsonb,
  error             text,
  state_type        text NOT NULL DEFAULT 'LIVE' CHECK (state_type IN ('LIVE','SIMULATED')),
  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz
);
CREATE INDEX IF NOT EXISTS ix_agent_run_started ON twin.agent_run (started_at DESC);

CREATE TABLE IF NOT EXISTS twin.agent_run_event (
  id       bigserial PRIMARY KEY,
  run_id   uuid NOT NULL REFERENCES twin.agent_run(run_id) ON DELETE CASCADE,
  ts       timestamptz NOT NULL DEFAULT clock_timestamp(),
  step     integer NOT NULL,
  kind     text NOT NULL CHECK (kind IN ('STEP','LOG','WARN','ERROR','FINDING')),
  status   text CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','SKIPPED')),
  message  text NOT NULL,
  data     jsonb
);
CREATE INDEX IF NOT EXISTS ix_agent_run_event ON twin.agent_run_event (run_id, id);

-- ------------------------------------------------------------------ findings and per-machine status computed by a run
CREATE TABLE IF NOT EXISTS twin.agent_finding (
  finding_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id      uuid NOT NULL REFERENCES twin.agent_run(run_id) ON DELETE CASCADE,
  rank        integer,
  category    text NOT NULL,           -- MACHINE_HEALTH | ANOMALY | DOWNTIME | MAINTENANCE | QUALITY | PRODUCTION | BOTTLENECK | DATA_QUALITY
  severity    text NOT NULL CHECK (severity IN ('NORMAL','WARNING','ATTENTION','CRITICAL','OFFLINE')),
  machine_id  text,
  line_id     text,
  station_id  text,
  title       text NOT NULL,
  explanation text NOT NULL,
  root_causes jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence    jsonb NOT NULL DEFAULT '{}'::jsonb,
  value_label text NOT NULL CHECK (value_label IN ('ACTUAL','CALCULATED','PREDICTED')),
  state_type  text NOT NULL DEFAULT 'LIVE' CHECK (state_type IN ('LIVE','SIMULATED')),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_agent_finding_run ON twin.agent_finding (run_id, rank);

CREATE TABLE IF NOT EXISTS twin.agent_machine_state (
  run_id       uuid NOT NULL REFERENCES twin.agent_run(run_id) ON DELETE CASCADE,
  machine_id   text NOT NULL,
  status       text NOT NULL CHECK (status IN ('NORMAL','WARNING','ATTENTION','CRITICAL','OFFLINE')),
  health_score numeric,
  risk_score   numeric,
  metrics      jsonb NOT NULL,
  PRIMARY KEY (run_id, machine_id)
);

-- 3D mapping: real machine id -> 3D asset key and zone (placement basis is stated, see ASSUMPTIONS A-03)
CREATE OR REPLACE VIEW twin.v_machine_3d_mapping AS
SELECT m.machine_id, m.machine_name, m.machine_type, m.line_id, ms.station_id, s.process_area AS zone,
       'machine:' || m.machine_id AS asset_key, 'machine type -> station (A-03)'::text AS placement_basis
FROM twin.dim_machine m
LEFT JOIN twin.machine_station_map ms USING (machine_id)
LEFT JOIN twin.dim_station s ON s.station_id = ms.station_id;

-- ------------------------------------------------------------------ recommendations + human approval
CREATE TABLE IF NOT EXISTS twin.recommendation (
  rec_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          uuid NOT NULL REFERENCES twin.agent_run(run_id),
  finding_id      uuid REFERENCES twin.agent_finding(finding_id),
  machine_id      text,
  line_id         text,
  category        text NOT NULL,
  issue           text NOT NULL,
  explanation     text NOT NULL,
  evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
  risk_level      text NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  action          text NOT NULL,
  expected_impact text,
  confidence      numeric CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  approval_required boolean NOT NULL DEFAULT true CHECK (approval_required),
  status          text NOT NULL DEFAULT 'PENDING_REVIEW'
                    CHECK (status IN ('PENDING_REVIEW','APPROVED','REJECTED','CHANGES_REQUESTED','SUPERSEDED')),
  action_status   text NOT NULL DEFAULT 'NOT_STARTED' CHECK (action_status IN ('NOT_STARTED','IN_PROGRESS','DONE','CANCELLED')),
  work_order_ref  text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rec_status ON twin.recommendation (status, created_at DESC);

CREATE TABLE IF NOT EXISTS twin.recommendation_review (
  id         bigserial PRIMARY KEY,
  rec_id     uuid NOT NULL REFERENCES twin.recommendation(rec_id),
  reviewer   text NOT NULL CHECK (length(btrim(reviewer)) > 0),
  role       text NOT NULL,
  decision   text NOT NULL CHECK (decision IN ('APPROVED','REJECTED','CHANGES_REQUESTED','COMMENT')),
  comment    text NOT NULL CHECK (length(btrim(comment)) > 0),
  decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS twin.recommendation_action (
  id            bigserial PRIMARY KEY,
  rec_id        uuid NOT NULL REFERENCES twin.recommendation(rec_id),
  ts            timestamptz NOT NULL DEFAULT now(),
  actor         text NOT NULL,
  action_status text NOT NULL,
  work_order_ref text,
  note          text
);

-- Workflow guard in the database: created PENDING_REVIEW; a decision needs a matching review row by a human; the action can only be
-- tracked after APPROVED. SUPERSEDED (a newer run replaced a pending item) is the only transition without a review row.
CREATE OR REPLACE FUNCTION twin.enforce_recommendation_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'PENDING_REVIEW' OR NEW.action_status <> 'NOT_STARTED' THEN
      RAISE EXCEPTION 'a recommendation must be created as PENDING_REVIEW / NOT_STARTED';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    IF NOT (OLD.status = 'PENDING_REVIEW' AND NEW.status IN ('APPROVED','REJECTED','CHANGES_REQUESTED','SUPERSEDED')) THEN
      RAISE EXCEPTION 'illegal recommendation status transition % -> %', OLD.status, NEW.status;
    END IF;
    IF NEW.status IN ('APPROVED','REJECTED','CHANGES_REQUESTED') AND NOT EXISTS (
         SELECT 1 FROM twin.recommendation_review r WHERE r.rec_id = NEW.rec_id AND r.decision = NEW.status) THEN
      RAISE EXCEPTION 'status % requires a recommendation_review row (reviewer + comment)', NEW.status;
    END IF;
  END IF;
  IF NEW.action_status IS DISTINCT FROM OLD.action_status AND NEW.status <> 'APPROVED' THEN
    RAISE EXCEPTION 'action tracking is only allowed on an APPROVED recommendation';
  END IF;
  IF NEW.work_order_ref IS DISTINCT FROM OLD.work_order_ref AND NEW.status <> 'APPROVED' THEN
    RAISE EXCEPTION 'a work order reference can only be recorded on an APPROVED recommendation';
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_recommendation_transition ON twin.recommendation;
CREATE TRIGGER trg_recommendation_transition BEFORE INSERT OR UPDATE ON twin.recommendation
  FOR EACH ROW EXECUTE FUNCTION twin.enforce_recommendation_transition();

-- ------------------------------------------------------------------ reports and notifications
CREATE TABLE IF NOT EXISTS twin.report (
  report_id    text PRIMARY KEY,
  run_id       uuid REFERENCES twin.agent_run(run_id),
  category     text NOT NULL,
  title        text NOT NULL,
  audience     text NOT NULL,
  scope        text NOT NULL,
  priority     text NOT NULL CHECK (priority IN ('LOW','MEDIUM','HIGH','CRITICAL')),
  status       text NOT NULL DEFAULT 'GENERATED' CHECK (status IN ('GENERATED','SUBMITTED')),
  content      jsonb NOT NULL,
  generated_by text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_report_created ON twin.report (created_at DESC);

CREATE TABLE IF NOT EXISTS twin.notification (
  id         bigserial PRIMARY KEY,
  run_id     uuid REFERENCES twin.agent_run(run_id),
  report_id  text REFERENCES twin.report(report_id),
  channel    text NOT NULL CHECK (channel IN ('EMAIL','TEAMS')),
  recipients text,
  subject    text NOT NULL,
  body       text NOT NULL,
  status     text NOT NULL CHECK (status IN ('PREPARED','SENT','FAILED','NOT_CONFIGURED')),
  error      text,
  actor      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at    timestamptz
);

-- ------------------------------------------------------------------ enterprise integrations (non-secret config only; secrets never stored here)
CREATE TABLE IF NOT EXISTS twin.integration_connector (
  connector_id text PRIMARY KEY,
  config       jsonb NOT NULL DEFAULT '{}'::jsonb,
  enabled      boolean NOT NULL DEFAULT false,
  last_test_ok boolean,
  last_test_at timestamptz,
  last_sync_at timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  updated_by   text
);
CREATE TABLE IF NOT EXISTS twin.integration_log (
  id           bigserial PRIMARY KEY,
  ts           timestamptz NOT NULL DEFAULT now(),
  connector_id text NOT NULL,
  action       text NOT NULL,           -- CONFIGURE | TEST | SYNC | SEND | CONNECT | DISCONNECT
  record_ref   text,
  status       text NOT NULL CHECK (status IN ('OK','FAILED','BLOCKED','NOT_CONFIGURED')),
  records      integer NOT NULL DEFAULT 0,
  actor        text NOT NULL,
  error        text,
  details      jsonb
);
CREATE INDEX IF NOT EXISTS ix_integration_log_ts ON twin.integration_log (ts DESC);
