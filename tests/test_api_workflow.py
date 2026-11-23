"""API contract + approval workflow tests against Neon. They WRITE a few rows (scenario runs, audit entries, one conversation),
so they only run when RUN_DB_WRITE_TESTS=1 (runs are finished as APPROVED/REJECTED, never left PENDING)."""
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.deps import get_engine
from app.main import app
from app.routers.chat import get_llm

pytestmark = pytest.mark.skipif(os.environ.get("RUN_DB_WRITE_TESTS") != "1" or not settings.neon_database_url,
                                reason="set RUN_DB_WRITE_TESTS=1 (writes to Neon)")
H = {"X-API-Key": settings.api_key.get_secret_value()}
c = TestClient(app)
FAST = {"replications": 6, "horizon_hours": 6, "requested_by": "pytest-runner"}


def run(name, params=None, line="SMT-L1", **kw):
    return c.post("/api/scenarios/run", headers=H, json={"scenario_name": name, "line_id": line, "params": params or {}, **FAST, **kw})


def finish(rid):
    """Leave no PENDING runs behind."""
    c.post(f"/api/scenarios/{rid}/submit-review", headers=H, json={})
    c.post(f"/api/scenarios/{rid}/decision", headers=H, json={"decision": "REJECTED", "reviewer": "Bo Li", "comment": "test cleanup"})


def test_envelope_on_every_response():
    for path in ["/api/twin/model", "/api/kpis", "/api/calibration/report", "/api/scenarios", "/api/scenarios/history",
                 "/api/twin/live-state?line_id=SMT-L1", "/api/twin/bottleneck?line_id=SMT-L1",
                 "/api/forecast/throughput-wip?line_id=SMT-L1&horizon_hours=6", "/api/audit", "/api/conversations"]:
        j = c.get(path, headers=H).json()
        assert j["state_type"] in ("LIVE", "SIMULATED") and "model_version" in j and "validation_status" in j and isinstance(j["assumptions"], list), path


def test_scenario_run_contract_and_baseline_delta():
    r = run("Buffer increase", {"buffer_factor": 2})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["state_type"] == "SIMULATED" and j["status"] == "SIMULATED" and j["approval_required"] is True and j["persisted"] is True
    assert j["model_version"] == "TWIN-2.0" and j["validation_status"] in ("VALIDATED", "NEEDS_REVIEW", "NOT_VALIDATED")
    assert j["assumptions"] and {"score", "band_low", "band_high", "components"} <= set(j["confidence"])
    assert {"output_units", "throughput_improvement_pct", "wip_units", "utilization_pct", "bottleneck_shift"} <= set(j["baseline_delta"])
    assert j["turnaround_ms"] > 0 and len(j["station_results"]) == 8
    assert c.get(f"/api/scenarios/{j['run_id']}", headers=H).json()["run_id"] == j["run_id"]
    finish(j["run_id"])


@pytest.mark.parametrize("body", [
    {"scenario_name": "Custom", "params": {"demand_change_pct": 99}}, {"scenario_name": "Custom", "params": {"bogus": 1}},
    {"scenario_name": "Nope"}, {"scenario_name": "SMT machine outage", "line_id": "FAL-L1"},
    {"scenario_name": "Baseline", "replications": 0}, {"scenario_name": "Baseline", "line_id": "XXX-L9"}])
def test_bad_parameters_return_422_with_a_message(body):
    r = c.post("/api/scenarios/run", headers=H, json={"line_id": "SMT-L1", **body})
    assert r.status_code == 422 and r.json().get("detail")


def test_simulate_endpoint_is_not_persisted_and_cannot_be_reviewed():
    j = c.post("/api/twin/simulate", headers=H, json={"scenario_name": "Baseline", "line_id": "FAL-L1", **FAST}).json()
    assert j["persisted"] is False and j["run_id"] is None and j["status"] == "DRAFT" and j["state_type"] == "SIMULATED"


def test_full_review_workflow_and_audit_trail():
    rid = run("Cycle time improvement", {"cycle_improvement_pct": 10}).json()["run_id"]
    dec = lambda who, comment="ok", d="APPROVED": c.post(f"/api/scenarios/{rid}/decision", headers=H, json={"decision": d, "reviewer": who, "comment": comment})
    assert dec("Asha Rao").status_code == 409                               # cannot decide before review is requested
    sub = c.post(f"/api/scenarios/{rid}/submit-review", headers=H, json={"actor": "agent"})
    assert sub.status_code == 200 and sub.json()["status"] == "PENDING_REVIEW"
    assert c.post(f"/api/scenarios/{rid}/submit-review", headers=H, json={}).status_code == 409
    assert rid in [p["run_id"] for p in c.get("/api/scenarios?status=PENDING_REVIEW", headers=H).json()["items"]]
    assert dec("agent").status_code == 403 and dec("Digital Twin").status_code == 403       # automated identities
    assert dec("pytest-runner").status_code == 403                                           # requester cannot self-approve
    assert dec("Asha Rao", comment="").status_code == 422 and dec(" ").status_code == 422     # name + comment required
    ok = dec("Asha Rao", "Fine for the change-control board")
    assert ok.status_code == 200 and ok.json()["status"] == "APPROVED"
    assert any("recommendation only" in a["text"] for a in ok.json()["assumptions"])
    assert dec("Bo Li", "later", "REJECTED").status_code == 409                              # terminal
    acts = {(a["action"], a["actor"]) for a in c.get("/api/audit?limit=300", headers=H).json()["items"] if a["entity_id"] == rid}
    assert {("SCENARIO_RUN", "pytest-runner"), ("SUBMIT_FOR_REVIEW", "agent"), ("REVIEW_DECISION", "Asha Rao")} <= acts


def test_database_itself_blocks_skipping_review():
    rid = run("Baseline").json()["run_id"]
    for target in ("APPROVED", "REJECTED"):
        with pytest.raises(Exception):                         # SIMULATED -> APPROVED/REJECTED straight from SQL as the app role
            with get_engine().begin() as conn:
                conn.execute(text("UPDATE twin.twin_scenario_run SET status = :s WHERE run_id = CAST(:r AS uuid)"), dict(s=target, r=rid))
    with pytest.raises(Exception):                             # audit_log is append-only
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE twin.audit_log SET actor = 'x'"))
    with pytest.raises(Exception):                             # approval_required cannot be switched off
        with get_engine().begin() as conn:
            conn.execute(text("UPDATE twin.twin_scenario_run SET approval_required = false WHERE run_id = CAST(:r AS uuid)"), dict(r=rid))
    finish(rid)


def test_compare_two_to_six_runs():
    a, b = run("Baseline").json(), run("Demand +15%").json()
    r = c.post("/api/scenarios/compare", headers=H, json={"run_ids": [a["run_id"], b["run_id"]]})
    assert r.status_code == 200 and len(r.json()["runs"]) == 2
    assert c.post("/api/scenarios/compare", headers=H, json={"run_ids": [a["run_id"]]}).status_code == 422
    finish(a["run_id"]); finish(b["run_id"])


def test_bottleneck_map_forecast_and_marginal():
    b = c.get("/api/twin/bottleneck?line_id=SMT-L1&marginal=true", headers=H).json()
    assert b["stations"] and b["ranking_by_utilization"] and len(b["what_to_fix_first"]) == 8
    g = [x["gain_units_per_pct_cycle_reduction"] for x in b["what_to_fix_first"]]
    assert g == sorted(g, reverse=True)
    f = c.get("/api/forecast/throughput-wip?line_id=FAL-L1&horizon_hours=8", headers=H).json()
    assert len(f["series"]) == 8 and f["state_type"] == "SIMULATED"


def test_chat_endpoint_with_mock_llm_and_rate_limit():
    class L:
        def invoke(self, prompt, **kw):
            return json.dumps({"action": "final", "answer": "Mock answer."})
    from app.routers import chat as chat_mod
    app.dependency_overrides[get_llm] = lambda: L()
    chat_mod._HITS.clear()
    try:
        r = c.post("/api/chat", headers=H, json={"message": "hello"})
        assert r.status_code == 200 and r.json()["answer"].startswith("Mock") and r.json()["conversation_id"]
        cid = r.json()["conversation_id"]
        assert [x["role"] for x in c.get(f"/api/conversations/{cid}", headers=H).json()["messages"]] == ["user", "assistant"]
        codes = [c.post("/api/chat", headers=H, json={"message": "x", "conversation_id": cid}).status_code for _ in range(14)]
        assert 429 in codes
    finally:
        app.dependency_overrides.clear()
        chat_mod._HITS.clear()


def test_api_key_required():
    if settings.api_key.get_secret_value():
        assert TestClient(app).post("/api/scenarios/run", json={"scenario_name": "Baseline", "line_id": "SMT-L1"}).status_code == 401
