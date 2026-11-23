"""Connectors are read-only, stubs never fabricate data, and the API labels every source."""
import inspect
import re

import pytest
from fastapi.testclient import TestClient

from app import connectors
from app.config import settings
from app.main import app

client = TestClient(app)
H = {"X-API-Key": settings.api_key.get_secret_value()}
WRITE_WORDS = re.compile(r"write|send|post|put|push|command|setpoint|actuat|publish|set_|delete|update", re.I)


def test_connectors_have_no_write_methods():
    for c in connectors.REGISTRY:
        for name, _ in inspect.getmembers(type(c), predicate=inspect.isfunction):
            assert not WRITE_WORDS.search(name), f"{type(c).__name__}.{name}"


def test_empty_templates_report_not_connected_and_never_return_data():
    stubs = [c for c in connectors.REGISTRY if isinstance(c, connectors._Static)]
    assert {s.id for s in stubs} == {"telemetry", "erp-bom", "cad-layout", "cycle-studies"}
    for s in stubs:
        info = s.describe({})
        assert info.status == "NOT_CONNECTED" and info.state_type == "NOT_CONNECTED" and info.direction == "READ_ONLY"
        with pytest.raises(connectors.NotConnected):
            s.read_latest({})


def test_static_file_with_rows_becomes_connected_and_is_labelled_static(tmp_path, monkeypatch):
    monkeypatch.setattr(connectors, "STATIC_DIR", tmp_path)
    c = connectors.CycleStudies()
    (tmp_path / c.filename).write_text(",".join(c.columns) + "\n", encoding="utf-8")
    assert c.describe({}).status == "NOT_CONNECTED"
    (tmp_path / c.filename).write_text(",".join(c.columns) + "\nSPH-A15-128,ST-SMT-040,9.8,30,2026-09-01\n", encoding="utf-8")
    i = c.describe({})
    assert i.status == "CONNECTED" and i.mode == "STATIC_FILE" and i.rows == 1 and i.latest_ts == "2026-09-01"
    assert "not a live feed" in i.message and c.read_latest({})[0]["station_id"] == "ST-SMT-040"
    (tmp_path / c.filename).write_text("wrong,header\n1,2\n", encoding="utf-8")
    assert c.describe({}).status == "NOT_CONNECTED" and "missing columns" in c.describe({}).message


def test_config_is_names_only():
    for c in connectors.REGISTRY:
        for k in c.describe({}).config_keys:
            assert "://" not in k and "=" not in k


@pytest.mark.skipif(not settings.neon_database_url, reason="NEON_DATABASE_URL not set")
def test_api_lists_all_sources_with_labels():
    r = client.get("/api/connectors", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state_type"] and len(d["connectors"]) == len(connectors.REGISTRY)
    assert all(c["state_type"] in {"LIVE", "SIMULATED", "NOT_CONNECTED"} and c["direction"] == "READ_ONLY" for c in d["connectors"])
    by = {c["id"]: c for c in d["connectors"]}
    assert by["mes"]["status"] == "CONNECTED" and by["mes"]["state_type"] == "LIVE" and by["mes"]["rows"] == 8832
    assert by["telemetry"]["status"] == "NOT_CONNECTED"
    assert client.get("/api/connectors/nope", headers=H).status_code == 404
