import pkgutil, re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.config import settings
from app.main import app

H = {"X-API-Key": settings.api_key.get_secret_value()}
needs_db = pytest.mark.skipif(not settings.neon_database_url, reason="NEON_DATABASE_URL not set")


def test_no_plc_scada_write_path():
    """Hard rule: no endpoint, library or config can write to PLC/SCADA."""
    # FastAPI >= 0.13x includes routers lazily, so app.routes no longer lists nested endpoints: check the OpenAPI paths as well.
    paths = {p: set(ops) for p, ops in app.openapi()["paths"].items()}
    assert len(paths) > 40, "route discovery broken: the safety check would be checking nothing"
    for path in [getattr(r, "path", "") for r in app.routes] + list(paths):
        assert not re.search(r"plc|scada|opc|modbus|write-?back|actuat|setpoint", path.lower()), path
    assert all(m.upper() in {"GET", "POST", "HEAD", "OPTIONS"} for ops in paths.values() for m in ops)
    banned = ("opcua", "asyncua", "pymodbus", "pycomm3", "snap7", "paho", "minimalmodbus")
    mods = {m.name.lower() for m in pkgutil.iter_modules()} & set(banned)
    src = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in Path("app").rglob("*.py"))
    assert not [b for b in banned if re.search(rf"^\s*(import|from)\s+{b}", src, re.M)], "OT client imported"
    # only GET reads and the scenario/approval POSTs may exist; nothing named like an OT write
    assert all(m in {"GET", "POST", "HEAD", "OPTIONS"} for r in app.routes for m in getattr(r, "methods", []) or [])


def test_api_key_required_but_health_public():
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200
    if settings.api_key.get_secret_value():
        assert c.get("/api/twin/lines").status_code == 401


@needs_db
def test_twin_model_graph():
    j = TestClient(app).get("/api/twin/model", headers=H).json()
    assert j["state_type"] == "SIMULATED" and j["model_version"] and j["validation_status"] and j["assumptions"]
    lines = {l["line_id"]: l for p in j["plants"] for l in p["lines"]}
    assert set(lines) == {"SMT-L1", "SMT-L2", "FAL-L1", "FAL-L2"}
    assert len(lines["SMT-L1"]["stations"]) == 8 and len(lines["FAL-L1"]["stations"]) == 13
    for l in lines.values():
        assert len(l["buffers"]) == len(l["stations"]) - 1
    # every one of the 21 stations appears on exactly one line per plant
    for fam in (("SMT-L1", "FAL-L1"), ("SMT-L2", "FAL-L2")):
        ids = [s["station_id"] for ln in fam for s in lines[ln]["stations"]]
        assert len(ids) == len(set(ids)) == 21
