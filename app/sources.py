"""External source connectors: PostgreSQL, MySQL, SharePoint. READ-ONLY.

Safety design (CLAUDE.md hard rules):
* Credentials come only from backend/.env (config.py). They are never returned, logged or sent to the frontend; error text is scrubbed.
* The app never runs user-supplied SQL against these sources. It runs two fixed statements only: table introspection and
  `SELECT * FROM <table> LIMIT n`, where <table> must be one of the source's own listed tables (allow-list) and is quoted by the dialect.
* Database sessions are set READ ONLY with a short statement timeout (defence in depth). The real safeguard is the database
  account you provide: give it SELECT only.
* SharePoint uses Microsoft Graph with GET reads (app permission Sites.Read.All or Files.Read.All). The access token is kept in memory for
  one call and is never stored.
* No method here writes, sends or changes anything in a source system.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal

import httpx
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

from .config import settings
from .connectors import Connector, Info, NotConnected

MAX_PREVIEW_ROWS = 100
_SAFE_PATH = re.compile(r"^[\w .()&-]+(/[\w .()&-]+)*$")
MAX_OBJECTS = 500
_SENSITIVE = re.compile(r"pass(word)?|secret|token|api[_-]?key|credential|ssn|card", re.I)


class SourceError(Exception):
    """A connection or read failure with a message that is safe to show (no credentials)."""


class InvalidInput(SourceError):
    """The submitted connection details are malformed (HTTP 422). The message never contains the submitted values."""


def _scrub(msg: str, *secrets: str) -> str:
    msg = re.sub(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"]+", "<url>", msg)   # any connection URL
    for s in secrets:
        if s:
            msg = msg.replace(s, "***")
    return msg.strip().splitlines()[0][:300] if msg.strip() else "error"


def _jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return str(v)


def _clean_rows(columns: list[str], rows: list) -> list[dict]:
    hide = {c for c in columns if _SENSITIVE.search(c)}
    return [{c: ("***" if c in hide else _jsonable(v)) for c, v in zip(columns, r)} for r in rows]



# ----------------------------------------------------------------------------------------------- session connections (added in the UI)
# Held in process memory only: never written to disk, the database or logs, never returned by the API, lost on restart.
# For a permanent connection put the values in backend/.env instead.
_RUNTIME: dict[str, dict] = {}
_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_NAME = re.compile(r"^[\w.$@-]{1,64}$")
_BLOCKED_HOST = re.compile(r"^(169\.254\.|metadata\.|fd00:)", re.I)             # cloud metadata endpoints
_GUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_SITE = re.compile(r"^[\w.-]+:/[\w /.-]+$")


def _check_host(v: str) -> str:
    v = v.strip()
    if not _HOST.match(v) or _BLOCKED_HOST.match(v):
        raise ValueError("invalid or blocked host")
    return v


class SqlConn(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    username: str
    password: SecretStr
    ssl: bool = True

    @field_validator("host")
    @classmethod
    def _h(cls, v): return _check_host(v)

    @field_validator("database", "username")
    @classmethod
    def _n(cls, v):
        if not _NAME.match(v.strip()):
            raise ValueError("invalid name (letters, digits and . _ $ @ - only, up to 64 characters)")
        return v.strip()


class SpConn(BaseModel):
    tenant_id: str
    client_id: str
    client_secret: SecretStr
    site: str
    folder: str

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _g(cls, v):
        if not _GUID.match(v.strip()):
            raise ValueError("must be a GUID, e.g. 00000000-0000-0000-0000-000000000000")
        return v.strip()

    @field_validator("site")
    @classmethod
    def _s(cls, v):
        if not _SITE.match(v.strip()):
            raise ValueError("must look like contoso.sharepoint.com:/sites/Plant")
        return v.strip()

    @field_validator("folder")
    @classmethod
    def _f(cls, v):
        v = v.strip().strip("/")
        if not _SAFE_PATH.match(v) or ".." in v.split("/"):
            raise ValueError("folder contains characters that are not allowed")
        return v


def _validate(model, body: dict):
    try:
        return model.model_validate(body)
    except ValidationError as e:        # message only: never the submitted value
        raise InvalidInput("; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())) from None


class _External(Connector):
    category = "External source"
    source_system = ""
    used_for = ""
    config_keys: tuple[str, ...] = ()
    needed = ""
    setup = ""

    # ---- to implement
    def configured(self) -> bool: ...
    def test(self) -> dict: ...
    def objects(self) -> list[dict]: ...
    def preview(self, obj: str, limit: int = 20) -> dict: ...

    def _parse(self, body: dict) -> dict: ...

    def connect(self, body: dict) -> dict:
        """Validate, keep in memory for this session, test, and roll back if the test fails. Never persists or returns the secret."""
        conf = self._parse(body)
        prev = _RUNTIME.get(self.id)
        _RUNTIME[self.id] = conf
        try:
            return self.test()
        except BaseException:
            if prev is None:
                _RUNTIME.pop(self.id, None)
            else:
                _RUNTIME[self.id] = prev
            raise

    def disconnect(self) -> bool:
        return _RUNTIME.pop(self.id, None) is not None

    def describe(self, facts: dict) -> Info:
        ok = self.configured()
        session = self.id in _RUNTIME
        where = f" at {_RUNTIME[self.id]['public']}" if session else ""
        if session:
            msg = ("Added in the UI for this session and tested OK. Credentials are held in memory only and are lost when the backend restarts "
                   "(set them in backend/.env to keep them). Data is read on demand and is not used by calibration or scenarios.")
        elif ok:
            msg = "Credentials are set in backend/.env. Not tested yet: use Test connection. Data is read on demand and is not used by calibration or scenarios."
        else:
            msg = "Not configured. Nothing from this source is used or shown."
        return Info(id=self.id, name=self.name, source_system=self.source_system + where, category=self.category,
                    status="CONNECTED" if session else ("CONFIGURED" if ok else "NOT_CONNECTED"), mode="ON_DEMAND" if ok else "NONE",
                    state_type="LIVE" if ok else "NOT_CONNECTED", used_for=self.used_for, message=msg, config_keys=list(self.config_keys),
                    needed_to_connect=None if ok else self.setup, config_origin="session" if session else ("env" if ok else None))

    def read_latest(self, facts: dict) -> list[dict]:
        if not self.configured():
            raise NotConnected(f"{self.name} is not configured")
        objs = self.objects()
        if not objs:
            return []
        return self.preview(objs[0]["name"], 20)["rows"]


# ----------------------------------------------------------------------------------------------- SQL databases
class _SqlSource(_External):
    category = "Database"
    driver_hint = ""
    _engines: dict[str, Engine] = {}

    driver = ""
    default_port = 0

    def _env_url(self) -> str: ...

    def _url(self) -> str:
        rt = _RUNTIME.get(self.id)
        return rt["url"] if rt else self._env_url()

    def _extra_query(self, c: SqlConn) -> dict:
        return {}

    def _parse(self, body: dict) -> dict:
        c = _validate(SqlConn, body)
        url = URL.create(self.driver, username=c.username, password=c.password.get_secret_value(), host=c.host, port=c.port, database=c.database,
                         query=self._extra_query(c))
        return {"url": url.render_as_string(hide_password=False), "public": f"{c.host}:{c.port}/{c.database}"}

    def disconnect(self) -> bool:
        rt = _RUNTIME.get(self.id)
        if rt:
            eng = self._engines.pop(rt["url"], None)
            if eng is not None:
                eng.dispose()
        return super().disconnect()

    def _readonly_sql(self) -> list[str]: ...

    def configured(self) -> bool:
        return bool(self._url().strip())

    def _engine(self) -> Engine:
        url = self._url().strip()
        if not url:
            raise NotConnected(f"{self.name} is not configured")
        eng = self._engines.get(url)
        if eng is None:
            try:
                eng = create_engine(url, pool_pre_ping=True, pool_recycle=240, pool_size=1, max_overflow=1, connect_args=self._connect_args())
            except Exception as e:                      # bad URL or missing driver
                raise SourceError(_scrub(str(e), url)) from None
            self._engines[url] = eng
        return eng

    def _connect_args(self) -> dict:
        return {}

    def _session(self):
        """Context manager: a connection whose transaction is READ ONLY with a short statement timeout."""
        eng = self._engine()
        url = self._url()
        outer = self

        class _Ctx:
            def __enter__(self_):
                try:
                    self_.c = eng.connect()
                    for stmt in outer._readonly_sql():
                        self_.c.execute(text(stmt))
                except Exception as e:
                    raise SourceError(_scrub(str(e), url, make_url(url).password or "")) from None
                return self_.c

            def __exit__(self_, *a):
                try:
                    self_.c.rollback()
                finally:
                    self_.c.close()
        return _Ctx()

    def test(self) -> dict:
        url = self._url()
        try:
            with self._session() as c:
                version = c.execute(text("SELECT version()")).scalar()
            u = make_url(url)
            return {"ok": True, "server": str(version)[:120], "database": u.database, "host": u.host}      # never the password or user
        except (SourceError, NotConnected):
            raise
        except Exception as e:
            raise SourceError(_scrub(str(e), url, make_url(url).password or "")) from None

    def _tables(self, c) -> list[tuple[str | None, str]]:
        raise NotImplementedError

    def objects(self) -> list[dict]:
        try:
            with self._session() as c:
                return [{"name": f"{s}.{t}" if s else t, "kind": "table"} for s, t in self._tables(c)[:MAX_OBJECTS]]
        except (SourceError, NotConnected):
            raise
        except Exception as e:
            raise SourceError(_scrub(str(e), self._url())) from None

    def preview(self, obj: str, limit: int = 20) -> dict:
        limit = max(1, min(int(limit), MAX_PREVIEW_ROWS))
        try:
            with self._session() as c:
                tables = {(f"{s}.{t}" if s else t): (s, t) for s, t in self._tables(c)}
                if obj not in tables:
                    raise SourceError("Unknown table. Choose one from the objects list.")
                s, t = tables[obj]
                q = c.dialect.identifier_preparer
                ident = f"{q.quote_schema(s)}.{q.quote(t)}" if s else q.quote(t)
                res = c.execute(text(f"SELECT * FROM {ident} LIMIT {limit}"))      # identifier from the allow-list, limit is an int
                cols = list(res.keys())
                rows = _clean_rows(cols, res.fetchall())
            return {"object": obj, "columns": cols, "rows": rows, "row_count": len(rows), "limit": limit,
                    "redacted_columns": [c for c in cols if _SENSITIVE.search(c)]}
        except (SourceError, NotConnected):
            raise
        except Exception as e:
            raise SourceError(_scrub(str(e), self._url())) from None


class PostgresSource(_SqlSource):
    id, name = "postgres", "PostgreSQL database"
    source_system = "PostgreSQL (read-only account)"
    used_for = "Read tables from another PostgreSQL database, for example an ERP or quality database."
    config_keys = ("PG_SOURCE_URL",)
    setup = "Set PG_SOURCE_URL in backend/.env, e.g. postgresql+psycopg://readonly_user:***@host:5432/dbname?sslmode=require. Use an account with SELECT only."

    driver = "postgresql+psycopg"
    default_port = 5432

    def _env_url(self) -> str:
        return settings.pg_source_url.get_secret_value()

    def _extra_query(self, c: SqlConn) -> dict:
        return {"sslmode": "require"} if c.ssl else {}

    def _connect_args(self) -> dict:
        return {"connect_timeout": 10}

    def _readonly_sql(self) -> list[str]:
        return ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 8000"]

    def _tables(self, c):
        rows = c.execute(text("SELECT table_schema, table_name FROM information_schema.tables WHERE table_type IN ('BASE TABLE','VIEW') "
                              "AND table_schema NOT IN ('pg_catalog','information_schema') ORDER BY 1, 2")).fetchall()
        return [(r[0], r[1]) for r in rows]


class MysqlSource(_SqlSource):
    id, name = "mysql", "MySQL database"
    source_system = "MySQL / MariaDB (read-only account)"
    used_for = "Read tables from a MySQL database, for example a plant or maintenance system."
    config_keys = ("MYSQL_SOURCE_URL",)
    setup = "Set MYSQL_SOURCE_URL in backend/.env, e.g. mysql+pymysql://readonly_user:***@host:3306/dbname. Use an account with SELECT only."

    driver = "mysql+pymysql"
    default_port = 3306

    def _env_url(self) -> str:
        return settings.mysql_source_url.get_secret_value()

    def _connect_args(self) -> dict:
        return {"connect_timeout": 10, "read_timeout": 15}

    def _readonly_sql(self) -> list[str]:
        return ["SET TRANSACTION READ ONLY"]      # statement time is bounded by read_timeout (MariaDB has no MAX_EXECUTION_TIME)

    def _tables(self, c):
        db = c.execute(text("SELECT DATABASE()")).scalar()
        rows = c.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = :d ORDER BY 1"), {"d": db}).fetchall()
        return [(None, r[0]) for r in rows]


# ----------------------------------------------------------------------------------------------- SharePoint (Microsoft Graph)
_GRAPH = "https://graph.microsoft.com/v1.0"


class SharePointSource(_External):
    id, name = "sharepoint", "SharePoint document library"
    category = "Files"
    source_system = "SharePoint Online via Microsoft Graph (read-only)"
    used_for = "Read CSV exports from a SharePoint folder, for example cycle-time studies, BOM extracts or layout tables."
    config_keys = ("SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET", "SHAREPOINT_SITE", "SHAREPOINT_FOLDER")
    setup = ("Register an Entra ID app with the read-only application permission Sites.Read.All (or Files.Read.All) and set the five SHAREPOINT_* "
             "values in backend/.env. SHAREPOINT_SITE looks like contoso.sharepoint.com:/sites/Plant and SHAREPOINT_FOLDER like Shared Documents/Twin.")

    def _parse(self, body: dict) -> dict:
        c = _validate(SpConn, body)
        return {"cfg": (c.tenant_id, c.client_id, c.client_secret.get_secret_value(), c.site, c.folder), "public": f"{c.site} / {c.folder}"}

    def _cfg(self):
        rt = _RUNTIME.get(self.id)
        if rt:
            return rt["cfg"]
        s = settings
        return (s.sharepoint_tenant_id.strip(), s.sharepoint_client_id.strip(), s.sharepoint_client_secret.get_secret_value().strip(),
                s.sharepoint_site.strip(), s.sharepoint_folder.strip().strip("/"))

    def configured(self) -> bool:
        return all(self._cfg())

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=20.0)

    def _acquire_token(self, cl: httpx.Client) -> str:
        tenant, cid, secret, _, _ = self._cfg()
        r = cl.request("POST", f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                       data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret, "scope": "https://graph.microsoft.com/.default"})
        if r.status_code != 200:
            raise SourceError(f"Microsoft sign-in failed (HTTP {r.status_code}). Check tenant, client id and secret.")
        return r.json()["access_token"]               # kept in memory for this call only

    def _get(self, cl: httpx.Client, token: str, path: str) -> httpx.Response:
        r = cl.get(f"{_GRAPH}{path}", headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 403:
            raise SourceError("Access denied (HTTP 403). The app needs the read-only permission Sites.Read.All or Files.Read.All with admin consent.")
        if r.status_code == 404:
            raise SourceError("Site or folder not found (HTTP 404). Check SHAREPOINT_SITE and SHAREPOINT_FOLDER.")
        if r.status_code >= 400:
            raise SourceError(f"SharePoint request failed (HTTP {r.status_code}).")
        return r

    def _folder_items(self, cl: httpx.Client, token: str) -> list[dict]:
        _, _, secret, site, folder = self._cfg()
        if not _SAFE_PATH.match(folder) or ".." in folder.split("/"):
            raise SourceError("SHAREPOINT_FOLDER contains characters that are not allowed.")
        if not re.match(r"^[\w.-]+:/[\w /.-]+$", site):
            raise SourceError("SHAREPOINT_SITE must look like contoso.sharepoint.com:/sites/Plant.")
        site_id = self._get(cl, token, f"/sites/{site}").json()["id"]
        items = self._get(cl, token, f"/sites/{site_id}/drive/root:/{folder}:/children?$top={MAX_OBJECTS}").json().get("value", [])
        return [{"id": i["id"], "name": i["name"], "size": i.get("size"), "modified": i.get("lastModifiedDateTime"), "site_id": site_id}
                for i in items if "file" in i]

    def test(self) -> dict:
        if not self.configured():
            raise NotConnected("SharePoint is not configured")
        try:
            with self._client() as cl:
                files = self._folder_items(cl, self._acquire_token(cl))
            return {"ok": True, "folder": self._cfg()[4], "files": len(files)}
        except (SourceError, NotConnected):
            raise
        except Exception as e:
            raise SourceError(_scrub(str(e), self._cfg()[2])) from None

    def objects(self) -> list[dict]:
        if not self.configured():
            raise NotConnected("SharePoint is not configured")
        try:
            with self._client() as cl:
                files = self._folder_items(cl, self._acquire_token(cl))
            return [{"name": f["name"], "kind": "csv file" if f["name"].lower().endswith(".csv") else "file", "size": f["size"], "modified": f["modified"]} for f in files]
        except (SourceError, NotConnected):
            raise
        except Exception as e:
            raise SourceError(_scrub(str(e), self._cfg()[2])) from None

    def preview(self, obj: str, limit: int = 20) -> dict:
        if not self.configured():
            raise NotConnected("SharePoint is not configured")
        limit = max(1, min(int(limit), MAX_PREVIEW_ROWS))
        if not obj.lower().endswith(".csv"):
            raise SourceError("Only CSV files can be previewed.")
        try:
            with self._client() as cl:
                token = self._acquire_token(cl)
                files = {f["name"]: f for f in self._folder_items(cl, token)}
                if obj not in files:
                    raise SourceError("Unknown file. Choose one from the objects list.")
                f = files[obj]
                r = self._get(cl, token, f"/sites/{f['site_id']}/drive/items/{f['id']}/content")
                if len(r.content) > 5_000_000:
                    raise SourceError("File is larger than 5 MB; preview is limited.")
            rd = csv.reader(io.StringIO(r.content.decode("utf-8-sig", errors="replace")))
            cols = next(rd, [])
            rows = [row for _, row in zip(range(limit), rd)]
            return {"object": obj, "columns": cols, "rows": _clean_rows(cols, rows), "row_count": len(rows), "limit": limit,
                    "redacted_columns": [c for c in cols if _SENSITIVE.search(c)]}
        except (SourceError, NotConnected):
            raise
        except Exception as e:
            raise SourceError(_scrub(str(e), self._cfg()[2])) from None


# Register with the shared connector list (done here, after the classes exist, to avoid a circular import).
from . import connectors as _connectors  # noqa: E402

for _c in (PostgresSource(), MysqlSource(), SharePointSource()):
    if all(_c.id != x.id for x in _connectors.REGISTRY):
        _connectors.REGISTRY.append(_c)
