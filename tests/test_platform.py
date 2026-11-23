"""Central agent platform: rules, approval guard (DB trigger), roles, integrations safety, chatbot routing. DB tests run inside transactions
that are rolled back, so they leave no fake decisions behind."""
import copy
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.factory import agent_config, integrations as integ, rules
from app.factory.chat_tools import route
from app.main import app

H = {"X-API-Key": settings.api_key.get_secret_value()}
needs_db = pytest.mark.skipif(not settings.neon_database_url, reason="NEON_DATABASE_URL not set")


def _metrics(**over):
    m = {"machine_id": "M-X", "machine_name": "X", "line_id": "SMT-L1", "station_id": "ST-SMT-040", "pm_interval_days": 30,
         "window": {"days": 7}, "latest": {"machine_state": "RUNNING"}, "anomalies": 0,
         "sensor_excursions": {"temperature": 0, "vibration": 0, "motor_current": 0}, "state_hours": {"DOWN": 0},
         "downtime": {"events": 0, "minutes": 0.0, "impact_units": 0.0, "top_reason": None, "by_reason": []},
         "maintenance": {"days_since_pm": 1.0, "pm_overdue": False, "corrective_failure_modes_90d": []},
         "station_quality": {"fail_rate_pct": None, "plant_fail_rate_pct": None}, "temp_slope_per_day": 0.0}
    m.update(over)
    return m


# ------------------------------------------------------------------------------------------------ rules (offline)
def test_healthy_machine_is_normal_and_risk_is_bounded():
    s = rules.score(_metrics(), agent_config.DEFAULTS)
    assert s["status"] == "NORMAL" and 0 <= s["risk_score"] < 5 and s["health_score"] == pytest.approx(100 - s["risk_score"])
    worst = _metrics(anomalies=99, sensor_excursions={"temperature": 50, "vibration": 50, "motor_current": 50}, state_hours={"DOWN": 99},
                     downtime={"events": 9, "minutes": 9999.0, "impact_units": 1.0, "top_reason": "X", "by_reason": []},
                     maintenance={"days_since_pm": 90.0, "pm_overdue": True, "corrective_failure_modes_90d": []},
                     station_quality={"fail_rate_pct": 20.0, "plant_fail_rate_pct": 2.0})
    s = rules.score(worst, agent_config.DEFAULTS)
    assert s["risk_score"] == pytest.approx(100.0) and s["status"] == "CRITICAL"


def test_down_in_latest_hour_is_offline_and_explanation_uses_only_metrics():
    m = _metrics(latest={"machine_state": "DOWN"}, anomalies=3)
    s = rules.score(m, agent_config.DEFAULTS)
    assert s["status"] == "OFFLINE"
    e = rules.explain(m, s)
    assert "3 hourly readings flagged" in e and "rule-based" in e


def test_root_causes_come_only_from_records():
    m = _metrics(maintenance={"days_since_pm": 1.0, "pm_overdue": False, "corrective_failure_modes_90d": [{"failure_mode": "NOZZLE_CLOG", "n": 2}]})
    rc = rules.root_causes(m)
    assert rc and all(c["source"].endswith("(actual)") or "calculated" in c["source"] for c in rc)
    assert rules.root_causes(_metrics()) == []                        # no records -> no invented cause


def test_config_validation_rejects_bad_weights_and_bands():
    bad = copy.deepcopy(agent_config.DEFAULTS)
    bad["risk_weights"]["anomalies"] = 50
    with pytest.raises(agent_config.ConfigError):
        agent_config.validate(bad)
    bad = copy.deepcopy(agent_config.DEFAULTS)
    bad["status_thresholds"] = {"WARNING": 50, "ATTENTION": 40, "CRITICAL": 60}
    with pytest.raises(agent_config.ConfigError):
        agent_config.validate(bad)


# ------------------------------------------------------------------------------------------------ safety (offline)
def test_no_ot_client_in_the_platform_and_no_approve_tool():
    src = "\n".join(p.read_text(encoding="utf-8") for p in Path("app/factory").rglob("*.py"))
    assert not re.search(r"^\s*(import|from)\s+(opcua|asyncua|pymodbus|pycomm3|snap7|paho)", src, re.M)
    from app.tools import TOOL_MAP
    assert not [t for t in TOOL_MAP if re.search(r"approve|reject|decide|plc|scada|setpoint|write", t)]


def test_integration_urls_and_paths_are_guarded():
    for bad in ("http://example.com/x", "https://169.254.169.254/latest", "ftp://x", "https://metadata/x"):
        with pytest.raises(integ.IntegrationError):
            integ._check_url(bad)
    assert integ._check_url("https://api.example.com/v1") == "https://api.example.com/v1"
    for bad in ("../backend/.env", "..\\x.xlsx", "a.csv", "/etc/passwd.xlsx"):
        with pytest.raises(integ.IntegrationError):
            integ._excel_path(bad)


def test_secrets_are_never_described(monkeypatch):
    monkeypatch.setitem(integ._SECRETS, "rest", {"api_key": "SUPER-SECRET-123"})
    monkeypatch.setattr(integ, "_row", lambda cid, rows=None: integ._EMPTY)
    d = integ.describe("rest")
    assert "SUPER-SECRET-123" not in repr(d) and d["secrets_set"]["api_key"] == "session"


def test_opcenter_and_databases_cannot_send(monkeypatch):
    monkeypatch.setattr(integ, "_log", lambda *a, **k: None)
    for cid in ("opcenter", "oracle", "mysql", "sap_hana", "snowflake"):
        with pytest.raises(integ.IntegrationError) as e:
            integ.send(cid, "report", "RPT-X", None, "tester")
        assert e.value.status == 403


def test_unapproved_recommendation_cannot_be_sent(monkeypatch):
    logged = []
    monkeypatch.setattr(integ, "_log", lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(integ, "describe", lambda cid, rows=None: {"status": "CONNECTED"})
    monkeypatch.setattr(integ, "build_payload", lambda *a: {"record": {"record_type": "recommendation", "status": "PENDING_REVIEW"}, "payload": {}})
    with pytest.raises(integ.IntegrationError) as e:
        integ.send("rest", "recommendation", "x", None, "tester")
    assert e.value.status == 403 and logged and logged[-1][0][2] == "BLOCKED"


@pytest.mark.parametrize("q,tool", [
    ("What is the current factory status?", "get_factory_status"), ("Which machine has abnormal sensor readings?", "get_abnormal_machines"),
    ("Explain why machine M-101 is showing a warning.", "get_machine_health"), ("Compare SMT-L1 and FAL-L2", "compare_lines"),
    ("What recommendations are pending approval?", "get_pending_recommendations"), ("What are the major quality issues?", "get_quality_issues"),
    ("Summarize the latest Digital Twin Agent execution.", "get_latest_agent_run"), ("What are the main production bottlenecks?", "get_agent_findings"),
])
def test_chat_fallback_routes_spec_questions(q, tool):
    assert route(q)[0] == tool


# ------------------------------------------------------------------------------------------------ database guard (rolled back)
@needs_db
def test_db_refuses_approval_without_a_human_review_row():
    from app.deps import get_engine
    with get_engine().connect() as c:
        tx = c.begin()
        try:
            rid = c.execute(text("INSERT INTO twin.agent_run (triggered_by) VALUES ('pytest') RETURNING run_id")).scalar_one()
            rec = c.execute(text("INSERT INTO twin.recommendation (run_id, category, issue, explanation, risk_level, action) "
                                 "VALUES (:r, 'TEST', 'i', 'e', 'LOW', 'a') RETURNING rec_id"), dict(r=rid)).scalar_one()
            for sql in ("UPDATE twin.recommendation SET status = 'APPROVED' WHERE rec_id = :i",
                        "UPDATE twin.recommendation SET action_status = 'DONE' WHERE rec_id = :i",
                        "UPDATE twin.recommendation SET work_order_ref = 'WO-1' WHERE rec_id = :i"):
                sp = c.begin_nested()
                with pytest.raises(Exception):
                    c.execute(text(sql), dict(i=rec))
                sp.rollback()
            sp = c.begin_nested()
            with pytest.raises(Exception):                                   # cannot be created already approved
                c.execute(text("INSERT INTO twin.recommendation (run_id, category, issue, explanation, risk_level, action, status) "
                               "VALUES (:r, 'TEST', 'i', 'e', 'LOW', 'a', 'APPROVED')"), dict(r=rid))
            sp.rollback()
            c.execute(text("INSERT INTO twin.recommendation_review (rec_id, reviewer, role, decision, comment) VALUES (:i, 'Test Planner', 'PLANNER', 'APPROVED', 'ok')"), dict(i=rec))
            c.execute(text("UPDATE twin.recommendation SET status = 'APPROVED' WHERE rec_id = :i"), dict(i=rec))
            c.execute(text("UPDATE twin.recommendation SET action_status = 'IN_PROGRESS', work_order_ref = 'WO-1' WHERE rec_id = :i"), dict(i=rec))
            sp = c.begin_nested()
            with pytest.raises(Exception):                                   # terminal: no way back to pending
                c.execute(text("UPDATE twin.recommendation SET status = 'PENDING_REVIEW' WHERE rec_id = :i"), dict(i=rec))
            sp.rollback()
        finally:
            tx.rollback()


@needs_db
def test_decisions_and_runs_need_a_user_with_the_right_role():
    c = TestClient(app)
    assert c.post("/api/agent/runs", headers=H).status_code == 401                                   # no user selected
    assert c.post("/api/recommendations/00000000-0000-0000-0000-000000000000/decision", headers=H,
                  json={"decision": "APPROVED", "comment": "x"}).status_code == 401
    assert c.post("/api/settings/agent", headers=H, json={}).status_code == 401
    r = c.post("/api/users", headers={**H, "X-User": "nobody-here"}, json={"user_id": "agent", "display_name": "Agent", "role": "ADMIN"})
    assert r.status_code in (403, 422)                                                                # automated identity / not admin


@needs_db
def test_platform_read_endpoints_carry_state_type():
    c = TestClient(app)
    for path in ("/api/factory/overview", "/api/factory/machines", "/api/factory/architecture", "/api/factory/machines/M-SPI-02/history", "/api/factory/replay?plant_id=IN-KA-01&hours=2", "/api/factory/home", "/api/agent/runs", "/api/recommendations", "/api/reports", "/api/integrations",
                 "/api/integrations/activity", "/api/settings/agent", "/api/users", "/api/audit-logs?limit=5"):
        r = c.get(path, headers=H)
        assert r.status_code == 200, (path, r.text[:200])
        assert r.json()["state_type"] in ("LIVE", "SIMULATED"), path
    assert c.get("/api/factory/overview?date_from=2026-13-01", headers=H).status_code == 422
