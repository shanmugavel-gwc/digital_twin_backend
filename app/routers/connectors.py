from dataclasses import asdict

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..connectors import REGISTRY, gather_facts
from ..connectors import NotConnected
from ..db import run_with_retry
from ..deps import envelope, get_engine, query, require_api_key
from ..config import settings
from ..sources import _RUNTIME, InvalidInput, SourceError, _External
from ..twin.scenarios import audit

router = APIRouter(prefix="/connectors", tags=["connectors"], dependencies=[Depends(require_api_key)])

NOTES = [
    {"id": "C-1", "text": "Connectors are read-only. No connector can write, send a command or change a setpoint in any plant system."},
    {"id": "C-2", "text": "CONNECTED means data is loaded and queryable. MES and master data are batch loads from exports, not streaming feeds; check the age shown."},
    {"id": "C-3", "text": "NOT_CONNECTED sources contribute nothing to results. They are listed so the gap is visible."},
]


@router.get("")
def list_connectors():
    """Status of every data source. Each item carries its own state_type (LIVE, SIMULATED or NOT_CONNECTED)."""
    facts = gather_facts(query)
    items = [asdict(c.describe(facts)) for c in REGISTRY]
    summary = {"connected": sum(i["status"] == "CONNECTED" for i in items), "configured": sum(i["status"] == "CONFIGURED" for i in items), "not_connected": sum(i["status"] == "NOT_CONNECTED" for i in items),
               "stale": sum(bool(i["stale"]) for i in items)}
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTES, summary=summary, connectors=items)


@router.get("/{connector_id}")
def one_connector(connector_id: str):
    facts = gather_facts(query)
    for c in REGISTRY:
        if c.id == connector_id:
            return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTES, connector=asdict(c.describe(facts)))
    raise HTTPException(404, f"Unknown connector '{connector_id}'")


def _source(connector_id: str) -> _External:
    for c in REGISTRY:
        if c.id == connector_id and isinstance(c, _External):
            return c
    raise HTTPException(404, f"'{connector_id}' is not an external source connector")


def _audited(connector_id: str, action: str, details: dict):
    def go(engine):
        with engine.begin() as conn:
            audit(conn, "ui", action, "connector", connector_id, details)    # names and counts only, never row values or credentials
    try:
        run_with_retry(get_engine(), go)
    except Exception:
        pass                                                                   # auditing must not break a read; failures are visible in logs


def _run(connector_id: str, fn, action: str, details_fn):
    src = _source(connector_id)
    try:
        out = fn(src)
    except NotConnected as e:
        raise HTTPException(409, str(e))
    except SourceError as e:
        raise HTTPException(502, str(e))
    _audited(connector_id, action, details_fn(out))
    return out


@router.get("/{connector_id}/test")
def test_connection(connector_id: str):
    """Opens a read-only session (or Graph token) and reports ok/server info. Never returns credentials."""
    out = _run(connector_id, lambda s: s.test(), "CONNECTOR_TEST", lambda o: {"ok": o.get("ok")})
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector_id=connector_id, result=out)


@router.get("/{connector_id}/objects")
def list_objects(connector_id: str):
    out = _run(connector_id, lambda s: s.objects(), "CONNECTOR_LIST", lambda o: {"objects": len(o)})
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector_id=connector_id, objects=out)


@router.get("/{connector_id}/preview")
def preview(connector_id: str, object: str = Query(min_length=1, max_length=200), limit: int = Query(default=20, ge=1, le=100)):
    """First rows of one table or CSV file. Fixed read; the object must be in the source's own listing. Sensitive-looking columns are masked."""
    out = _run(connector_id, lambda s: s.preview(object, limit), "CONNECTOR_PREVIEW", lambda o: {"object": o["object"], "rows": o["row_count"]})
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector_id=connector_id, preview=out,
                    assumptions=[{"id": "C-4", "text": "Preview is data read on demand from the external source. It is not used by calibration or scenarios."}])


def _runtime_allowed():
    if settings.app_env != "dev" and not settings.allow_runtime_connections:
        raise HTTPException(403, "Adding connections from the UI is disabled outside dev. Set the values in backend/.env, or ALLOW_RUNTIME_CONNECTIONS=true.")


@router.post("/{connector_id}/connection")
def add_connection(connector_id: str, body: dict = Body(...)):
    """Add a connection for this session. The secret is held in backend memory only (never saved, logged or returned) and the connection
    is tested before it is kept; a failed test leaves nothing behind. Persistent connections belong in backend/.env."""
    _runtime_allowed()
    src = _source(connector_id)
    try:
        out = src.connect(body)
    except InvalidInput as e:
        raise HTTPException(422, str(e))
    except SourceError as e:
        raise HTTPException(502, str(e))
    _audited(connector_id, "CONNECTOR_CONNECT", {"target": _RUNTIME.get(connector_id, {}).get("public"), "scope": "session"})
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector_id=connector_id, result=out, connector=asdict(src.describe({})),
                    assumptions=[{"id": "C-5", "text": "Session connection: credentials are in backend memory only and are lost on restart. Use backend/.env to keep them."}])


@router.post("/{connector_id}/disconnect")
def remove_connection(connector_id: str):
    _runtime_allowed()
    src = _source(connector_id)
    removed = src.disconnect()
    _audited(connector_id, "CONNECTOR_DISCONNECT", {"scope": "session", "removed": removed})
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector_id=connector_id, removed=removed, connector=asdict(src.describe({})))
