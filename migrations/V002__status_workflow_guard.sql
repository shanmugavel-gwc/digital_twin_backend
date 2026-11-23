-- Enforce the scenario status workflow in the database, so no code path (agent, API bug, manual SQL by twin_rw)
-- can skip human review:  DRAFT -> SIMULATED -> PENDING_REVIEW -> APPROVED | REJECTED.
-- APPROVED / REJECTED additionally require a matching scenario_review row (reviewer + comment) written first.
CREATE OR REPLACE FUNCTION twin.enforce_scenario_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.status IS DISTINCT FROM OLD.status THEN
    IF NOT (   (OLD.status = 'DRAFT'          AND NEW.status = 'SIMULATED')
            OR (OLD.status = 'SIMULATED'      AND NEW.status = 'PENDING_REVIEW')
            OR (OLD.status = 'PENDING_REVIEW' AND NEW.status IN ('APPROVED', 'REJECTED'))) THEN
      RAISE EXCEPTION 'illegal scenario status transition % -> %', OLD.status, NEW.status;
    END IF;
    IF NEW.status IN ('APPROVED', 'REJECTED') AND NOT EXISTS (
         SELECT 1 FROM twin.scenario_review r WHERE r.run_id = NEW.run_id AND r.decision = NEW.status) THEN
      RAISE EXCEPTION 'status % requires a scenario_review row (reviewer + comment) for run %', NEW.status, NEW.run_id;
    END IF;
  END IF;
  IF TG_OP = 'INSERT' AND NEW.status <> 'DRAFT' THEN
    RAISE EXCEPTION 'a scenario run must be created as DRAFT';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_scenario_transition ON twin.twin_scenario_run;
CREATE TRIGGER trg_scenario_transition BEFORE INSERT OR UPDATE ON twin.twin_scenario_run
  FOR EACH ROW EXECUTE FUNCTION twin.enforce_scenario_transition();
