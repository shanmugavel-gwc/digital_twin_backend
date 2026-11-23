"""External source connectors: read-only, allow-listed objects, no credential leaks."""
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from app import sources
from app.config import settings
from app.main import app

client = TestClient(app)
H = {"X-API-Key": settings.api_key.get_secret_value()}
needs_db = pytest.mark.skipif(not settings.neon_readonly_url, reason="NEON_READONLY_URL not set")


# ------------------------------------------------------------------------------------------------ not configured
def test_unconfigured_sources_are_not_connected(monkeypatch):
    for k in ("pg_source_url", "mysql_source_url", "sharepoint_client_secret"):
        monkeypatch.setattr(settings, k, SecretStr(""))
    for c in (sources.PostgresSource(), sources.MysqlSource(), sources.SharePointSource()):
        i = c.describe({})
        assert i.status == "NOT_CONNECTED" and i.state_type == "NOT_CONNECTED" and i.direction == "READ_ONLY"
        with pytest.raises(sources.NotConnected):
            c.read_latest({})
    r = client.get("/api/connectors/postgres/preview", params={"object": "x"}, headers=H)
    assert r.status_code == 409


def test_registry_has_the_three_sources():
    ids = {c["id"] for c in client.get("/api/connectors", headers=H).json()["connectors"]}
    assert {"postgres", "mysql", "sharepoint"} <= ids


# ------------------------------------------------------------------------------------------------ PostgreSQL (real, via the twin_ro role)
@pytest.fixture
def pg(monkeypatch):
    monkeypatch.setattr(settings, "pg_source_url", SecretStr(settings.neon_readonly_url))
    sources._SqlSource._engines.clear()
    return sources.PostgresSource()


@needs_db
def test_postgres_test_list_preview(pg):
    t = pg.test()
    assert t["ok"] and "password" not in str(t).lower()
    names = [o["name"] for o in pg.objects()]
    assert "twin.dim_machine" in names
    p = pg.preview("twin.dim_machine", 5)
    assert p["row_count"] == 5 and "machine_id" in p["columns"]


@needs_db
def test_postgres_rejects_objects_outside_the_listing_and_injection(pg):
    for bad in ("twin.dim_machine; DROP TABLE twin.dim_machine", "pg_catalog.pg_user", "twin.audit_log", "x\" OR 1=1 --"):
        with pytest.raises(sources.SourceError):
            pg.preview(bad, 5)


@needs_db
def test_postgres_session_is_read_only(pg):
    with pg._session() as c:
        with pytest.raises(Exception) as e:
            c.execute(text("CREATE TEMP TABLE should_fail (a int)"))
        assert "read-only" in str(e.value).lower() or "permission" in str(e.value).lower()


@needs_db
def test_postgres_api_and_audit(pg):
    r = client.get("/api/connectors/postgres/preview", params={"object": "twin.dim_machine", "limit": 3}, headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state_type"] == "LIVE" and d["preview"]["row_count"] == 3
    assert settings.neon_readonly_url.split("@")[0].split("//")[1].split(":")[1] not in r.text         # the password is never in a response
    assert client.get("/api/connectors/postgres/preview", params={"object": "nope.nope"}, headers=H).status_code == 502


def test_sensitive_columns_are_masked():
    rows = sources._clean_rows(["id", "api_key", "Password"], [(1, "abc", "pw")])
    assert rows == [{"id": 1, "api_key": "***", "Password": "***"}]


# ------------------------------------------------------------------------------------------------ MySQL (no server: error must not leak the password)
def test_mysql_connection_error_does_not_leak_credentials(monkeypatch):
    monkeypatch.setattr(settings, "mysql_source_url", SecretStr("mysql+pymysql://reader:SuperSecretPw1@127.0.0.1:1/plantdb"))
    sources._SqlSource._engines.clear()
    with pytest.raises(sources.SourceError) as e:
        sources.MysqlSource().test()
    assert "SuperSecretPw1" not in str(e.value) and "mysql+pymysql://" not in str(e.value)
    r = client.get("/api/connectors/mysql/test", headers=H)
    assert r.status_code == 502 and "SuperSecretPw1" not in r.text


# ------------------------------------------------------------------------------------------------ SharePoint (mocked Microsoft Graph)
CSV = b"station_id,measured_cycle_sec,api_key\nST-SMT-040,9.8,leak\nST-SMT-050,41.0,leak\n"


def _graph(handler_overrides=None):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, str(req.url)))
        u = str(req.url)
        if handler_overrides and handler_overrides(u):
            return handler_overrides(u)
        if "oauth2/v2.0/token" in u:
            return httpx.Response(200, json={"access_token": "tok-123"})
        assert req.headers.get("authorization") == "Bearer tok-123"
        if u.endswith("/sites/contoso.sharepoint.com:/sites/Plant"):
            return httpx.Response(200, json={"id": "site1"})
        if "/children" in u:
            return httpx.Response(200, json={"value": [{"id": "f1", "name": "cycle.csv", "size": 60, "file": {}, "lastModifiedDateTime": "2026-10-01T00:00:00Z"},
                                                       {"id": "d1", "name": "subfolder", "folder": {}}]})
        if u.endswith("/drive/items/f1/content"):
            return httpx.Response(200, content=CSV)
        return httpx.Response(404)
    return handler, calls


@pytest.fixture
def sp(monkeypatch):
    monkeypatch.setattr(settings, "sharepoint_tenant_id", "tenant")
    monkeypatch.setattr(settings, "sharepoint_client_id", "client")
    monkeypatch.setattr(settings, "sharepoint_client_secret", SecretStr("S3cretValue"))
    monkeypatch.setattr(settings, "sharepoint_site", "contoso.sharepoint.com:/sites/Plant")
    monkeypatch.setattr(settings, "sharepoint_folder", "Shared Documents/Twin")
    return sources.SharePointSource()


def test_sharepoint_list_and_preview_use_only_reads(sp, monkeypatch):
    handler, calls = _graph()
    monkeypatch.setattr(sources.SharePointSource, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    assert sp.test() == {"ok": True, "folder": "Shared Documents/Twin", "files": 1}
    assert [o["name"] for o in sp.objects()] == ["cycle.csv"]                         # folders are not listed
    p = sp.preview("cycle.csv", 10)
    assert p["row_count"] == 2 and p["rows"][0]["api_key"] == "***" and p["rows"][0]["station_id"] == "ST-SMT-040"
    assert all(m == "GET" or "oauth2/v2.0/token" in u for m, u in calls)             # the only POST is the sign-in
    with pytest.raises(sources.SourceError):
        sp.preview("../../secrets.csv", 5)
    with pytest.raises(sources.SourceError):
        sp.preview("other.csv", 5)                                                     # not in the folder listing


def test_sharepoint_errors_are_clear_and_leak_nothing(sp, monkeypatch):
    handler, _ = _graph(lambda u: httpx.Response(403) if "/sites/" in u and "oauth2" not in u else None)
    monkeypatch.setattr(sources.SharePointSource, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(sources.SourceError) as e:
        sp.test()
    assert "Sites.Read.All" in str(e.value) and "S3cretValue" not in str(e.value)


def test_sharepoint_rejects_unsafe_folder_and_site(sp, monkeypatch):
    handler, _ = _graph()
    monkeypatch.setattr(sources.SharePointSource, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(settings, "sharepoint_folder", "Shared Documents/../Private")
    with pytest.raises(sources.SourceError):
        sp.test()
    monkeypatch.setattr(settings, "sharepoint_folder", "Docs")
    monkeypatch.setattr(settings, "sharepoint_site", "evil.example.com/../x")
    with pytest.raises(sources.SourceError):
        sp.test()


# ------------------------------------------------------------------------------------------------ session connections added from the UI
@pytest.fixture(autouse=True)
def _clean_runtime():
    yield
    sources._RUNTIME.clear()
    sources._SqlSource._engines.clear()


def _pg_body():
    from sqlalchemy.engine import make_url
    u = make_url(settings.neon_readonly_url)
    return u, {"host": u.host, "port": u.port or 5432, "database": u.database, "username": u.username, "password": u.password, "ssl": True}


@needs_db
def test_add_postgres_connection_from_ui_keeps_secret_in_memory_only(monkeypatch):
    monkeypatch.setattr(settings, "pg_source_url", SecretStr(""))
    u, body = _pg_body()
    r = client.post("/api/connectors/postgres/connection", json=body, headers=H)
    assert r.status_code == 200, r.text
    assert u.password not in r.text and "connector" in r.json()
    c = r.json()["connector"]
    assert c["status"] == "CONNECTED" and c["config_origin"] == "session" and c["state_type"] == "LIVE"
    listing = client.get("/api/connectors", headers=H)
    assert u.password not in listing.text
    pv = client.get("/api/connectors/postgres/preview", params={"object": "twin.dim_machine", "limit": 2}, headers=H)
    assert pv.status_code == 200 and u.password not in pv.text
    d = client.post("/api/connectors/postgres/disconnect", headers=H).json()
    assert d["removed"] is True and d["connector"]["status"] == "NOT_CONNECTED"
    assert client.get("/api/connectors/postgres/preview", params={"object": "twin.dim_machine"}, headers=H).status_code == 409


def test_bad_connection_details_are_rejected_without_echoing_values():
    secret = "Hunter2-Not-Echoed"
    for bad in ({"host": "169.254.169.254", "port": 5432, "database": "d", "username": "u", "password": secret},
                {"host": "db.example.com", "port": 99999, "database": "d", "username": "u", "password": secret},
                {"host": "db.example.com; rm -rf", "port": 5432, "database": "d", "username": "u", "password": secret},
                {"host": "db.example.com", "port": 5432, "database": "d;drop", "username": "u", "password": secret},
                {"host": "db.example.com", "port": 5432}):
        r = client.post("/api/connectors/postgres/connection", json=bad, headers=H)
        assert r.status_code == 422, r.text
        assert secret not in r.text and "postgres" not in sources._RUNTIME
    # the global 422 handler must not echo the submitted input either
    r = client.get("/api/twin/bottleneck", params={"line_id": secret}, headers=H)
    assert r.status_code == 422 and secret not in r.text


def test_failed_connection_leaves_nothing_behind():
    secret = "Hunter2-Not-Echoed"
    r = client.post("/api/connectors/mysql/connection", json={"host": "127.0.0.1", "port": 1, "database": "d", "username": "u", "password": secret}, headers=H)
    assert r.status_code == 502 and secret not in r.text
    assert "mysql" not in sources._RUNTIME
    assert client.get("/api/connectors/mysql", headers=H).json()["connector"]["status"] == "NOT_CONNECTED"


def test_ui_connections_are_disabled_outside_dev(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "allow_runtime_connections", False)
    r = client.post("/api/connectors/postgres/connection", json={"host": "h", "port": 5432, "database": "d", "username": "u", "password": "p"}, headers=H)
    assert r.status_code == 403
    assert client.post("/api/connectors/postgres/disconnect", headers=H).status_code == 403


def test_sharepoint_session_connection_with_mocked_graph(monkeypatch):
    monkeypatch.setattr(settings, "sharepoint_client_secret", SecretStr(""))
    handler, _ = _graph()
    monkeypatch.setattr(sources.SharePointSource, "_client", lambda self: httpx.Client(transport=httpx.MockTransport(handler)))
    body = {"tenant_id": "11111111-1111-1111-1111-111111111111", "client_id": "22222222-2222-2222-2222-222222222222",
            "client_secret": "S3cretValue", "site": "contoso.sharepoint.com:/sites/Plant", "folder": "Shared Documents/Twin"}
    r = client.post("/api/connectors/sharepoint/connection", json=body, headers=H)
    assert r.status_code == 200, r.text
    assert "S3cretValue" not in r.text and r.json()["connector"]["status"] == "CONNECTED"
    pv = client.get("/api/connectors/sharepoint/preview", params={"object": "cycle.csv"}, headers=H)
    assert pv.status_code == 200 and pv.json()["preview"]["row_count"] == 2 and "S3cretValue" not in pv.text
    bad = dict(body, tenant_id="not-a-guid", client_secret="S3cretValue")
    r2 = client.post("/api/connectors/sharepoint/connection", json=bad, headers=H)
    assert r2.status_code == 422 and "S3cretValue" not in r2.text
    assert client.post("/api/connectors/sharepoint/disconnect", headers=H).json()["removed"] is True


def test_static_connectors_cannot_take_a_connection():
    r = client.post("/api/connectors/telemetry/connection", json={"host": "h"}, headers=H)
    assert r.status_code == 404
