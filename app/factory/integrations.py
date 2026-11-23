"""Enterprise integrations: 10 connectors (Oracle, MySQL, SAP, SAP HANA, Excel, Snowflake, REST API, Siemens Opcenter MES, Microsoft Teams,
Microsoft Power BI). Every status shown comes from a real test against the configured endpoint; nothing is faked as connected or sent.

Safety (CLAUDE.md):
* Secrets (passwords, API keys, webhook URLs) are never stored in the database, never returned by the API and never logged. They are read
  from backend/.env (INTEG_<CONNECTOR>_<FIELD>) or entered in the UI and held in backend memory only (lost on restart).
* Database connectors are READ-ONLY (test + table listing). Siemens Opcenter MES is READ-ONLY: the twin never writes to a shop-floor system.
* Outbound sends (SAP, REST API, Teams, Power BI, Excel export) carry informational records only, and a recommendation can be sent only after
  a human APPROVED it. No connector can reach PLC/SCADA.
* Every configure / test / connect / sync / send is written to twin.integration_log and twin.audit_log (names and outcomes, never values)."""
from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values
from sqlalchemy import text

from ..config import BACKEND_DIR, ROOT_DIR, settings
from ..deps import get_engine, query
from ..sources import _BLOCKED_HOST, _HOST, _scrub
from ..twin.scenarios import audit

EXCEL_DIR = ROOT_DIR / "data" / "integrations"
EXPORT_DIR = ROOT_DIR / "data" / "exports"
_SECRETS: dict[str, dict[str, str]] = {}          # session secrets: memory only

F = lambda name, label, typ="text", required=False, secret=False, default=None, options=None, help=None: {
    "name": name, "label": label, "type": typ, "required": required, "secret": secret, "default": default, "options": options, "help": help}
COMMON = [F("sync_frequency", "Sync frequency", "select", default="manual", options=["manual", "hourly", "daily"],
            help="Recorded with the configuration. Syncs run when a person clicks Sync; no background scheduler is running."),
          F("data_mapping", "Data mapping (JSON: target field -> record field)", "textarea", help='e.g. {"NotificationText": "issue", "TechnicalObject": "machine_id"}')]
DB_FIELDS = lambda port, dbl="Database": [F("host", "Host", required=True), F("port", "Port", "number", True, default=port), F("database", dbl, required=True),
                                          F("username", "Username", required=True), F("password", "Password", "password", True, True)]
HTTP_FIELDS = [F("api_url", "API base URL (https)", required=True), F("auth_method", "Authentication", "select", default="none", options=["none", "basic", "bearer", "api_key"]),
               F("username", "Username (basic)"), F("password", "Password (basic)", "password", secret=True), F("api_key", "API key / bearer token", "password", secret=True),
               F("api_key_header", "API key header", default="X-API-Key"), F("test_path", "Test / read path", default="/")]

CONNECTORS: dict[str, dict] = {
    "oracle": {"name": "Oracle Database", "kind": "sql", "dialect": "oracle+oracledb", "driver": "oracledb", "probe": "SELECT 1 FROM DUAL",
               "fields": DB_FIELDS(1521, "Service name"), "events": ["read: tables (on demand)"], "direction": "READ_ONLY"},
    "mysql": {"name": "MySQL", "kind": "sql", "dialect": "mysql+pymysql", "driver": "pymysql", "probe": "SELECT 1",
              "fields": DB_FIELDS(3306), "events": ["read: tables (on demand)"], "direction": "READ_ONLY"},
    "sap": {"name": "SAP S/4HANA (OData)", "kind": "http", "send": True, "csrf": True,
            "fields": [*HTTP_FIELDS[:-1], F("test_path", "Service path", default="/sap/opu/odata/sap/API_MAINTNOTIFICATION"),
                       F("send_path", "Create path", default="/sap/opu/odata/sap/API_MAINTNOTIFICATION/MaintenanceNotification"), F("sap_client", "SAP client (sap-client)")],
            "default_mapping": {"NotificationText": "issue40", "MaintNotifLongText": "action", "TechnicalObject": "machine_id", "MaintPriority": "sap_priority",
                                "NotificationType": "=M1"},
            "events": ["send: approved recommendation as maintenance notification", "read: service metadata"], "direction": "OUTBOUND_AFTER_APPROVAL"},
    "sap_hana": {"name": "SAP HANA", "kind": "sql", "dialect": "hana+hdbcli", "driver": "hdbcli", "probe": "SELECT 1 FROM DUMMY",
                 "fields": DB_FIELDS(443, "Database / tenant"), "events": ["read: tables (on demand)"], "direction": "READ_ONLY"},
    "excel": {"name": "Microsoft Excel", "kind": "excel", "send": True,
              "fields": [F("file_path", "Workbook (.xlsx) in data/integrations/", required=True, help="Read on Sync. Send writes a new export file to data/exports/."),
                         F("sheet", "Sheet (optional)")],
              "events": ["read: workbook sheets", "send: export record to data/exports/*.xlsx"], "direction": "READ_AND_EXPORT"},
    "snowflake": {"name": "Snowflake", "kind": "snowflake", "driver": "snowflake.connector",
                  "fields": [F("account", "Account identifier", required=True), F("warehouse", "Warehouse"), F("database", "Database", required=True), F("schema", "Schema", default="PUBLIC"),
                             F("username", "Username", required=True), F("password", "Password", "password", True, True)],
                  "events": ["read: tables (on demand)"], "direction": "READ_ONLY"},
    "rest": {"name": "REST API", "kind": "http", "send": True, "fields": [*HTTP_FIELDS, F("send_path", "Send path (POST)", default="/")],
             "events": ["send: approved recommendation / report (POST JSON)", "read: GET test path"], "direction": "OUTBOUND_AFTER_APPROVAL"},
    "opcenter": {"name": "Siemens Opcenter MES", "kind": "http", "send": False, "fields": [*HTTP_FIELDS[:-1], F("test_path", "Read path", default="/")],
                 "events": ["read: MES data (GET)"], "direction": "READ_ONLY",
                 "note": "Read-only by design: the twin never writes to a shop-floor system (CLAUDE.md)."},
    "teams": {"name": "Microsoft Teams", "kind": "teams", "send": True,
              "fields": [F("webhook_url", "Incoming webhook / Workflows URL", "password", True, True), F("channel_label", "Channel label (display only)")],
              "events": ["send: report / approved recommendation card", "send: notification"], "direction": "OUTBOUND_AFTER_APPROVAL"},
    "powerbi": {"name": "Microsoft Power BI", "kind": "powerbi", "send": True,
                "fields": [F("push_url", "Push dataset rows URL (with key)", "password", True, True), F("dataset_label", "Dataset label (display only)")],
                "events": ["send: rows to a push/streaming dataset"], "direction": "OUTBOUND_AFTER_APPROVAL"},
}
for _c in CONNECTORS.values():
    _c["fields"] = _c["fields"] + COMMON


class IntegrationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ------------------------------------------------------------------------------------------------ config + secrets
def _env_secrets() -> dict[str, str]:
    vals = {k: v for k, v in dotenv_values(BACKEND_DIR / ".env").items() if v}
    vals.update({k: v for k, v in os.environ.items() if k.startswith("INTEG_") and v})
    return vals


def _env_key(cid: str, field: str) -> str:
    return f"INTEG_{cid.upper()}_{field.upper()}"


def secret_value(cid: str, field: str) -> str:
    return _SECRETS.get(cid, {}).get(field) or _env_secrets().get(_env_key(cid, field), "")


_EMPTY = {"config": {}, "enabled": False, "last_test_ok": None, "last_test_at": None, "last_sync_at": None, "updated_at": None, "updated_by": None}


def _all_rows() -> dict[str, dict]:
    return {r["connector_id"]: r for r in query("SELECT connector_id, config, enabled, last_test_ok, last_test_at, last_sync_at, updated_at, updated_by "
                                                "FROM twin.integration_connector")}


def _row(cid: str, rows: dict | None = None) -> dict:
    if rows is not None:
        return rows.get(cid, _EMPTY)
    r = query("SELECT config, enabled, last_test_ok, last_test_at, last_sync_at, updated_at, updated_by FROM twin.integration_connector WHERE connector_id = :c", c=cid)
    return r[0] if r else {"config": {}, "enabled": False, "last_test_ok": None, "last_test_at": None, "last_sync_at": None, "updated_at": None, "updated_by": None}


def _conf(cid: str, row: dict | None = None) -> dict:
    spec = CONNECTORS[cid]
    cfg = {f["name"]: f["default"] for f in spec["fields"] if not f["secret"] and f["default"] is not None}
    cfg.update((row or _row(cid))["config"] or {})
    for f in spec["fields"]:
        if f["secret"]:
            cfg[f["name"]] = secret_value(cid, f["name"])
    return cfg


def _driver_ok(spec: dict) -> bool:
    import importlib.util
    d = spec.get("driver")
    try:
        return not d or importlib.util.find_spec(d) is not None
    except ModuleNotFoundError:
        return False


def describe(cid: str, rows: dict | None = None) -> dict:
    spec = CONNECTORS[cid]
    row = _row(cid, rows)
    cfg = _conf(cid, row)
    missing = [f["label"] for f in spec["fields"] if f["required"] and not str(cfg.get(f["name"]) or "").strip()]
    if spec["kind"] == "http" and cfg.get("auth_method") in ("bearer", "api_key") and not cfg.get("api_key"):
        missing.append("API key / bearer token")
    if spec["kind"] == "http" and cfg.get("auth_method") == "basic" and not (cfg.get("username") and cfg.get("password")):
        missing.append("Username and password")
    driver = _driver_ok(spec)
    if missing:
        status = "NOT_CONFIGURED"
    elif not driver:
        status = "DRIVER_MISSING"
    elif row["enabled"] and row["last_test_ok"]:
        status = "CONNECTED"
    elif row["last_test_ok"] is False:
        status = "ERROR"
    elif row["enabled"] is False and row["last_test_ok"]:
        status = "DISCONNECTED"
    else:
        status = "CONFIGURED"
    secrets = {f["name"]: ("session" if _SECRETS.get(cid, {}).get(f["name"]) else "env" if _env_secrets().get(_env_key(cid, f["name"])) else None)
               for f in spec["fields"] if f["secret"]}
    public = {f["name"]: cfg.get(f["name"]) for f in spec["fields"] if not f["secret"]}
    return {"id": cid, "name": spec["name"], "kind": spec["kind"], "direction": spec["direction"], "events": spec["events"], "status": status,
            "missing": missing, "driver_installed": driver, "driver": spec.get("driver"), "can_send": bool(spec.get("send")), "note": spec.get("note"),
            "fields": spec["fields"], "config": public, "secrets_set": secrets, "enabled": row["enabled"], "last_test_ok": row["last_test_ok"],
            "last_test_at": row["last_test_at"], "last_sync_at": row["last_sync_at"], "updated_at": row["updated_at"], "updated_by": row["updated_by"],
            "default_mapping": spec.get("default_mapping")}


def _log(cid: str, action: str, status: str, actor: str, record_ref: str | None = None, records: int = 0, error: str | None = None, details: dict | None = None):
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO twin.integration_log (connector_id, action, record_ref, status, records, actor, error, details) "
                       "VALUES (:c, :a, :r, :s, :n, :u, :e, CAST(:d AS jsonb))"),
                  dict(c=cid, a=action, r=record_ref, s=status, n=records, u=actor, e=error, d=json.dumps(details or {}, default=str)))
        audit(c, actor, f"INTEGRATION_{action}", "integration", cid, {"status": status, "record": record_ref, "error": error})


def configure(cid: str, body: dict, actor: str) -> dict:
    spec = CONNECTORS[cid]
    names = {f["name"]: f for f in spec["fields"]}
    unknown = set(body) - set(names)
    if unknown:
        raise IntegrationError(422, f"unknown fields: {', '.join(sorted(unknown))}")
    row = _row(cid)
    cfg = dict(row["config"] or {})
    for k, v in body.items():
        f = names[k]
        v = "" if v is None else str(v).strip()
        if len(v) > 2000:
            raise IntegrationError(422, f"{f['label']}: too long")
        if f["secret"]:
            if v:                                          # empty = keep the current secret
                _SECRETS.setdefault(cid, {})[k] = v
            continue
        if f["type"] == "number" and v and not v.isdigit():
            raise IntegrationError(422, f"{f['label']}: must be a number")
        if f["type"] == "select" and v and f["options"] and v not in f["options"]:
            raise IntegrationError(422, f"{f['label']}: must be one of {', '.join(f['options'])}")
        if k == "data_mapping" and v:
            try:
                m = json.loads(v)
                assert isinstance(m, dict) and all(isinstance(x, str) for x in m.values())
            except Exception:
                raise IntegrationError(422, "Data mapping must be a JSON object of target field -> record field")
        if k in ("host",) and v and (not _HOST.match(v) or _BLOCKED_HOST.match(v)):
            raise IntegrationError(422, f"{f['label']}: invalid or blocked host")
        if k == "api_url" and v:
            _check_url(v)
        if k == "file_path" and v:
            _excel_path(v)
        cfg[k] = v
    with get_engine().begin() as c:                    # a config change invalidates the previous test
        c.execute(text("INSERT INTO twin.integration_connector (connector_id, config, enabled, last_test_ok, updated_by) VALUES (:c, CAST(:j AS jsonb), false, NULL, :u) "
                       "ON CONFLICT (connector_id) DO UPDATE SET config = EXCLUDED.config, enabled = false, last_test_ok = NULL, updated_at = now(), updated_by = EXCLUDED.updated_by"),
                  dict(c=cid, j=json.dumps(cfg), u=actor))
    _log(cid, "CONFIGURE", "OK", actor, details={"fields": sorted(k for k in body if not names[k]["secret"]), "secrets_updated": sorted(k for k in body if names[k]["secret"] and body[k])})
    return describe(cid)


# ------------------------------------------------------------------------------------------------ safety helpers
def _check_url(url: str) -> str:
    m = re.match(r"^(https?)://([^/:?#]+)(:\d+)?(/.*)?$", url.strip())
    if not m:
        raise IntegrationError(422, "URL must look like https://host/path")
    scheme, host = m.group(1), m.group(2).lower()
    if _BLOCKED_HOST.match(host) or host in ("metadata", "169.254.169.254"):
        raise IntegrationError(422, "URL host is blocked")
    if scheme == "http" and not (settings.app_env == "dev" and host in ("localhost", "127.0.0.1")):
        raise IntegrationError(422, "URL must use https (http only for localhost in dev)")
    return url.strip()


def _excel_path(p: str) -> Path:
    path = (EXCEL_DIR / p).resolve()
    if EXCEL_DIR.resolve() not in path.parents or path.suffix.lower() != ".xlsx":
        raise IntegrationError(422, "Workbook must be an .xlsx file inside data/integrations/")
    return path


def _secrets_of(cid: str) -> list[str]:
    return [secret_value(cid, f["name"]) for f in CONNECTORS[cid]["fields"] if f["secret"]]


def _safe_err(cid: str, e: Exception) -> str:
    return _scrub(f"{type(e).__name__}: {e}", *_secrets_of(cid))


def _require_ready(cid: str) -> tuple[dict, dict]:
    d = describe(cid)
    if d["status"] == "NOT_CONFIGURED":
        raise IntegrationError(409, f"{d['name']} is not configured: missing {', '.join(d['missing'])}")
    if d["status"] == "DRIVER_MISSING":
        raise IntegrationError(409, f"{d['name']}: Python driver '{d['driver']}' is not installed on the backend")
    return d, _conf(cid)


# ------------------------------------------------------------------------------------------------ per-kind operations
def _sql_engine(spec: dict, cfg: dict):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    q = {"service_name": cfg["database"]} if spec["dialect"].startswith("oracle") else {}
    url = URL.create(spec["dialect"], username=cfg["username"], password=cfg["password"], host=cfg["host"], port=int(cfg["port"]),
                     database=None if q else cfg["database"], query=q)
    args = {"connect_timeout": 10} if "mysql" in spec["dialect"] else {}
    return create_engine(url, pool_pre_ping=False, connect_args=args)


def _http(cid: str, cfg: dict) -> tuple[httpx.Client, str]:
    base = _check_url(cfg["api_url"]).rstrip("/")
    headers = {"Accept": "application/json"}
    auth = None
    if cfg.get("auth_method") == "basic":
        auth = (cfg.get("username") or "", cfg.get("password") or "")
    elif cfg.get("auth_method") == "bearer":
        headers["Authorization"] = f"Bearer {cfg.get('api_key')}"
    elif cfg.get("auth_method") == "api_key":
        headers[cfg.get("api_key_header") or "X-API-Key"] = cfg.get("api_key") or ""
    params = {"sap-client": cfg["sap_client"]} if cfg.get("sap_client") else None
    return httpx.Client(timeout=20.0, headers=headers, auth=auth, params=params, follow_redirects=False), base


def _do_test(cid: str) -> dict:
    spec = CONNECTORS[cid]
    _, cfg = _require_ready(cid)
    k = spec["kind"]
    t0 = time.perf_counter()
    if k == "sql":
        eng = _sql_engine(spec, cfg)
        try:
            with eng.connect() as c:
                c.execute(text(spec["probe"])).scalar()
        finally:
            eng.dispose()
        info = {"server": f"{cfg['host']}:{cfg['port']}/{cfg['database']}"}
    elif k == "snowflake":
        import snowflake.connector
        con = snowflake.connector.connect(account=cfg["account"], user=cfg["username"], password=cfg["password"], warehouse=cfg.get("warehouse") or None,
                                          database=cfg["database"], schema=cfg.get("schema") or None, login_timeout=15, network_timeout=20)
        try:
            ver = con.cursor().execute("SELECT CURRENT_VERSION()").fetchone()[0]
        finally:
            con.close()
        info = {"server": f"Snowflake {ver}"}
    elif k == "excel":
        from openpyxl import load_workbook
        path = _excel_path(cfg["file_path"])
        if not path.exists():
            raise IntegrationError(409, f"{cfg['file_path']} not found in data/integrations/")
        wb = load_workbook(path, read_only=True)
        info = {"sheets": wb.sheetnames}
        wb.close()
    elif k == "http":
        cl, base = _http(cid, cfg)
        with cl:
            r = cl.get(base + (cfg.get("test_path") or "/"))
        if r.status_code >= 400:
            raise IntegrationError(502, f"HTTP {r.status_code} from {spec['name']}")
        info = {"http_status": r.status_code}
    elif k == "teams":
        url = _check_url(cfg["webhook_url"])
        from .notify import teams_card
        r = httpx.post(url, json=teams_card("Connection test", "Digital Twin Agent connection test. No action required."), timeout=20)
        if r.status_code >= 300:
            raise IntegrationError(502, f"Teams returned HTTP {r.status_code}")
        info = {"http_status": r.status_code, "note": "A test card was posted to the channel."}
    elif k == "powerbi":
        url = _check_url(cfg["push_url"])
        r = httpx.post(url, json={"rows": []}, timeout=20)
        if r.status_code >= 300:
            raise IntegrationError(502, f"Power BI returned HTTP {r.status_code}")
        info = {"http_status": r.status_code}
    else:
        raise IntegrationError(400, "unsupported connector")
    return {"ok": True, "latency_ms": round((time.perf_counter() - t0) * 1000), **info}


def test(cid: str, actor: str) -> dict:
    try:
        out = _do_test(cid)
        ok, err = True, None
    except IntegrationError as e:
        if e.status == 409:
            _log(cid, "TEST", "NOT_CONFIGURED", actor, error=e.message)
            raise
        out, ok, err = {"ok": False, "error": e.message}, False, e.message
    except Exception as e:
        err = _safe_err(cid, e)
        out, ok = {"ok": False, "error": err}, False
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO twin.integration_connector (connector_id, last_test_ok, last_test_at) VALUES (:c, :ok, now()) "
                       "ON CONFLICT (connector_id) DO UPDATE SET last_test_ok = :ok, last_test_at = now(), enabled = CASE WHEN :ok THEN twin.integration_connector.enabled ELSE false END"),
                  dict(c=cid, ok=ok))
    _log(cid, "TEST", "OK" if ok else "FAILED", actor, error=err, details={k: v for k, v in out.items() if k != "error"})
    return {**out, "connector": describe(cid)}


def set_enabled(cid: str, enabled: bool, actor: str) -> dict:
    if enabled:
        res = test(cid, actor)
        if not res["ok"]:
            raise IntegrationError(502, f"Connection test failed, not connected: {res['error']}")
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO twin.integration_connector (connector_id, enabled) VALUES (:c, :e) "
                       "ON CONFLICT (connector_id) DO UPDATE SET enabled = :e, updated_at = now(), updated_by = :u"), dict(c=cid, e=enabled, u=actor))
    if not enabled:
        _SECRETS.pop(cid, None)                                # disconnect also forgets session secrets
    _log(cid, "CONNECT" if enabled else "DISCONNECT", "OK", actor)
    return describe(cid)


def sync(cid: str, actor: str) -> dict:
    """Read-only pull: lists what the source exposes and counts it. Nothing is written to the source."""
    spec = CONNECTORS[cid]
    d = describe(cid)
    if d["status"] != "CONNECTED":
        _log(cid, "SYNC", "BLOCKED", actor, error=f"connector is {d['status']}")
        raise IntegrationError(409, f"{spec['name']} is {d['status']}: connect it first (Test connection, then Connect).")
    cfg = _conf(cid)
    k = spec["kind"]
    try:
        if k == "sql":
            from sqlalchemy import inspect
            eng = _sql_engine(spec, cfg)
            try:
                names = inspect(eng).get_table_names()
            finally:
                eng.dispose()
            res = {"objects": len(names), "sample": names[:20]}
            n = len(names)
        elif k == "snowflake":
            import snowflake.connector
            con = snowflake.connector.connect(account=cfg["account"], user=cfg["username"], password=cfg["password"], warehouse=cfg.get("warehouse") or None,
                                              database=cfg["database"], schema=cfg.get("schema") or None, login_timeout=15)
            try:
                rows = con.cursor().execute("SELECT table_name FROM information_schema.tables WHERE table_schema = CURRENT_SCHEMA() LIMIT 500").fetchall()
            finally:
                con.close()
            res, n = {"objects": len(rows), "sample": [r[0] for r in rows[:20]]}, len(rows)
        elif k == "excel":
            from openpyxl import load_workbook
            wb = load_workbook(_excel_path(cfg["file_path"]), read_only=True)
            sheets = {s: wb[s].max_row for s in wb.sheetnames if not cfg.get("sheet") or s == cfg["sheet"]}
            wb.close()
            res, n = {"sheets": sheets}, sum(max(0, v - 1) for v in sheets.values())
        elif k == "http":
            cl, base = _http(cid, cfg)
            with cl:
                r = cl.get(base + (cfg.get("test_path") or "/"))
            if r.status_code >= 400:
                raise IntegrationError(502, f"HTTP {r.status_code}")
            try:
                body = r.json()
                items = body if isinstance(body, list) else (body.get("value") or body.get("d", {}).get("results") or body.get("items") or [])
                n = len(items) if isinstance(items, list) else 0
            except ValueError:
                n = 0
            res = {"http_status": r.status_code, "items": n}
        else:
            _log(cid, "SYNC", "BLOCKED", actor, error="outbound-only connector")
            raise IntegrationError(400, f"{spec['name']} is outbound only: there is nothing to read. Use Send.")
    except IntegrationError as e:
        if e.status != 400:
            _log(cid, "SYNC", "FAILED", actor, error=e.message)
        raise
    except Exception as e:
        err = _safe_err(cid, e)
        _log(cid, "SYNC", "FAILED", actor, error=err)
        raise IntegrationError(502, err)
    with get_engine().begin() as c:
        c.execute(text("UPDATE twin.integration_connector SET last_sync_at = now() WHERE connector_id = :c"), dict(c=cid))
    _log(cid, "SYNC", "OK", actor, records=n, details=res)
    return {"ok": True, "records": n, **res, "connector": describe(cid)}


# ------------------------------------------------------------------------------------------------ records + send
SAP_PRIO = {"CRITICAL": "1", "HIGH": "2", "MEDIUM": "3", "LOW": "4"}


def sendable_records() -> list[dict]:
    recs = query("SELECT r.rec_id, r.category, r.machine_id, r.line_id, r.issue, r.risk_level, r.status, r.action_status, r.created_at, "
                 "(SELECT reviewer FROM twin.recommendation_review v WHERE v.rec_id = r.rec_id AND v.decision = 'APPROVED' ORDER BY id DESC LIMIT 1) AS approved_by "
                 "FROM twin.recommendation r WHERE r.status IN ('APPROVED', 'PENDING_REVIEW') ORDER BY r.created_at DESC LIMIT 60")
    reps = query("SELECT report_id, category, title, priority, status, created_at FROM twin.report ORDER BY created_at DESC LIMIT 30")
    return ([{"type": "recommendation", "id": str(r["rec_id"]), "label": f"[{r['status']}] {r['risk_level']} {r['machine_id'] or r['line_id'] or ''}: {r['issue']}",
              "sendable": r["status"] == "APPROVED", "status": r["status"], "approved_by": r["approved_by"]} for r in recs] +
            [{"type": "report", "id": r["report_id"], "label": f"{r['report_id']} {r['title']}", "sendable": True, "status": r["status"]} for r in reps])


def _record(rtype: str, rid: str) -> dict:
    if rtype == "recommendation":
        r = query("SELECT * FROM twin.recommendation WHERE CAST(rec_id AS text) = :i", i=rid)
        if not r:
            raise IntegrationError(404, "recommendation not found")
        r = r[0]
        rev = query("SELECT reviewer, role, decision, comment, decided_at FROM twin.recommendation_review WHERE rec_id = CAST(:i AS uuid) ORDER BY id", i=rid)
        return {"record_type": "recommendation", "record_id": rid, "status": r["status"], "category": r["category"], "machine_id": r["machine_id"],
                "line_id": r["line_id"], "issue": r["issue"], "issue40": r["issue"][:40], "action": r["action"], "risk_level": r["risk_level"],
                "sap_priority": SAP_PRIO.get(r["risk_level"], "3"), "expected_impact": r["expected_impact"], "explanation": r["explanation"],
                "action_status": r["action_status"], "work_order_ref": r["work_order_ref"], "run_id": str(r["run_id"]), "created_at": r["created_at"].isoformat(),
                "approvals": [{**x, "decided_at": x["decided_at"].isoformat()} for x in rev if x["decision"] == "APPROVED"]}
    if rtype == "report":
        r = query("SELECT report_id, run_id, category, title, audience, scope, priority, status, generated_by, created_at, content FROM twin.report WHERE report_id = :i", i=rid)
        if not r:
            raise IntegrationError(404, "report not found")
        r = r[0]
        c = r["content"]
        return {"record_type": "report", "record_id": rid, "status": r["status"], "title": r["title"], "category": r["category"], "audience": r["audience"],
                "priority": r["priority"], "summary": c.get("summary"), "data_as_of": c.get("data_as_of"), "issue": r["title"], "issue40": r["title"][:40],
                "action": c.get("summary"), "findings": [{k: f.get(k) for k in ("rank", "severity", "title", "value_label")} for f in c.get("findings", [])[:10]],
                "created_at": r["created_at"].isoformat()}
    raise IntegrationError(422, "record type must be recommendation or report")


def build_payload(cid: str, rtype: str, rid: str, note: str | None, actor: str) -> dict:
    spec = CONNECTORS[cid]
    rec = _record(rtype, rid)
    cfg = _conf(cid)
    mapping = None
    if cfg.get("data_mapping"):
        mapping = json.loads(cfg["data_mapping"])
    elif spec.get("default_mapping"):
        mapping = spec["default_mapping"]
    if mapping:
        data = {t: (src[1:] if src.startswith("=") else rec.get(src)) for t, src in mapping.items()}
        if note:
            data["Note"] = note
    elif spec["kind"] == "teams":
        from .notify import teams_card
        body = f"{rec.get('issue')}\n\nAction: {rec.get('action')}\nRisk: {rec.get('risk_level', rec.get('priority'))}\nStatus: {rec['status']}" + (f"\nNote: {note}" if note else "")
        data = teams_card(f"[Digital Twin] {rec['record_type']} {rid[:8]}", body)
    elif spec["kind"] == "powerbi":
        flat = {k: v for k, v in rec.items() if isinstance(v, (str, int, float)) or v is None}
        data = {"rows": [{**flat, "note": note, "sent_by": actor, "sent_at": datetime.utcnow().isoformat() + "Z"}]}
    else:
        data = {"source": "Manufacturing Digital Twin Agent", "state_type": "LIVE", "approval_required": True, "record": rec, "note": note,
                "sent_by": actor, "sent_at": datetime.utcnow().isoformat() + "Z"}
    return {"connector": cid, "record": rec, "payload": data}


def send(cid: str, rtype: str, rid: str, note: str | None, actor: str) -> dict:
    spec = CONNECTORS[cid]
    ref = f"{rtype}:{rid}"
    if not spec.get("send"):
        _log(cid, "SEND", "BLOCKED", actor, ref, error="connector is read-only")
        raise IntegrationError(403, f"{spec['name']} is read-only. {spec.get('note') or ''}".strip())
    d = describe(cid)
    if d["status"] != "CONNECTED":
        _log(cid, "SEND", "BLOCKED", actor, ref, error=f"connector is {d['status']}")
        raise IntegrationError(409, f"{spec['name']} is {d['status']}: connect it first.")
    pl = build_payload(cid, rtype, rid, note, actor)
    if rtype == "recommendation" and pl["record"]["status"] != "APPROVED":
        _log(cid, "SEND", "BLOCKED", actor, ref, error=f"recommendation is {pl['record']['status']}, not APPROVED")
        raise IntegrationError(403, "Only an APPROVED recommendation can be sent to an external system. A planner must approve it first.")
    cfg = _conf(cid)
    k = spec["kind"]
    try:
        if k == "excel":
            from openpyxl import Workbook
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            wb = Workbook()
            ws = wb.active
            ws.append(["field", "value"])
            for key, v in (pl["payload"].get("record") or pl["payload"]).items():
                ws.append([key, json.dumps(v, default=str) if isinstance(v, (dict, list)) else v])
            name = f"{rtype}_{rid[:12]}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            wb.save(EXPORT_DIR / name)
            resp = {"file": f"data/exports/{name}"}
        elif k == "http":
            cl, base = _http(cid, cfg)
            with cl:
                headers = {}
                if spec.get("csrf"):
                    t = cl.get(base + (cfg.get("test_path") or "/"), headers={"x-csrf-token": "fetch"})
                    headers["x-csrf-token"] = t.headers.get("x-csrf-token", "")
                r = cl.post(base + (cfg.get("send_path") or "/"), json=pl["payload"], headers=headers)
            resp = {"http_status": r.status_code, "body": _scrub(r.text[:300], *_secrets_of(cid)) if r.text else ""}
            if r.status_code >= 300:
                raise IntegrationError(502, f"HTTP {r.status_code} from {spec['name']}")
        elif k in ("teams", "powerbi"):
            url = _check_url(cfg["webhook_url" if k == "teams" else "push_url"])
            r = httpx.post(url, json=pl["payload"], timeout=20)
            resp = {"http_status": r.status_code}
            if r.status_code >= 300:
                raise IntegrationError(502, f"HTTP {r.status_code} from {spec['name']}")
        else:
            raise IntegrationError(400, "unsupported")
    except IntegrationError as e:
        _log(cid, "SEND", "FAILED", actor, ref, error=e.message)
        raise
    except Exception as e:
        err = _safe_err(cid, e)
        _log(cid, "SEND", "FAILED", actor, ref, error=err)
        raise IntegrationError(502, err)
    _log(cid, "SEND", "OK", actor, ref, records=1, details=resp)
    return {"ok": True, "response": resp, "payload": pl["payload"]}


def kpis() -> dict:
    r = query("SELECT count(*) FILTER (WHERE action = 'SEND' AND status = 'OK') AS sends, coalesce(sum(records) FILTER (WHERE action = 'SEND' AND status = 'OK'), 0) AS records_sent, "
              "count(*) FILTER (WHERE status = 'FAILED') AS failed, max(ts) FILTER (WHERE action = 'SYNC' AND status = 'OK') AS last_sync FROM twin.integration_log")[0]
    rows = _all_rows()
    items = [describe(c, rows) for c in CONNECTORS]
    return {"total": len(items), "active": sum(1 for i in items if i["status"] == "CONNECTED"), "records_sent": int(r["records_sent"]),
            "failed_calls": int(r["failed"]), "last_sync": r["last_sync"], "items": items}


def activity(limit: int = 100) -> list[dict]:
    return query("SELECT id, ts, connector_id, action, record_ref, status, records, actor, error FROM twin.integration_log ORDER BY id DESC LIMIT :n", n=limit)
