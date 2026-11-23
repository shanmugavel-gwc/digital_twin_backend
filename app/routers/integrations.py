"""Enterprise integrations (10 connectors). Configuration needs ADMIN; sending a record needs PLANNER (and an APPROVED recommendation);
test/sync need ENGINEER. KPIs come from twin.integration_log. Secrets are never returned."""
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..deps import envelope, require_api_key
from ..factory import integrations as integ
from ..factory.users import require

router = APIRouter(prefix="/integrations", tags=["integrations"], dependencies=[Depends(require_api_key)])
NOTE = [{"id": "I-1", "text": "Statuses come from real connection tests. Nothing is shown as connected or sent unless the external system accepted it."},
        {"id": "I-2", "text": "Secrets are held in backend memory (UI) or backend/.env only; never stored in the database, returned or logged."},
        {"id": "I-3", "text": "Database connectors and Siemens Opcenter MES are read-only. A recommendation can be sent only after a planner approved it."}]


def _cid(cid: str) -> str:
    if cid not in integ.CONNECTORS:
        raise HTTPException(404, "unknown connector")
    return cid


def _wrap(fn, *a):
    try:
        return fn(*a)
    except integ.IntegrationError as e:
        raise HTTPException(e.status, e.message)


@router.get("")
def list_integrations():
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, **integ.kpis())


@router.get("/activity")
def activity(limit: int = Query(default=100, ge=1, le=500)):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", items=integ.activity(limit))


@router.get("/records")
def records():
    return envelope("LIVE", validation_status="NOT_APPLICABLE", items=integ.sendable_records())


@router.get("/{cid}")
def one(cid: str):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, connector=integ.describe(_cid(cid)))


@router.post("/{cid}/configure")
def configure(cid: str, body: dict = Body(...), user=Depends(require("ADMIN"))):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector=_wrap(integ.configure, _cid(cid), body, user["user_id"]))


@router.post("/{cid}/test")
def test(cid: str, user=Depends(require("ENGINEER"))):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", result=_wrap(integ.test, _cid(cid), user["user_id"]))


@router.post("/{cid}/connect")
def connect(cid: str, user=Depends(require("ADMIN"))):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector=_wrap(integ.set_enabled, _cid(cid), True, user["user_id"]))


@router.post("/{cid}/disconnect")
def disconnect(cid: str, user=Depends(require("ADMIN"))):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", connector=_wrap(integ.set_enabled, _cid(cid), False, user["user_id"]))


@router.post("/{cid}/sync")
def sync(cid: str, user=Depends(require("ENGINEER"))):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", result=_wrap(integ.sync, _cid(cid), user["user_id"]))


@router.post("/{cid}/preview")
def preview(cid: str, body: dict = Body(...), user=Depends(require("VIEWER"))):
    """Payload that Send would post (no secrets in it)."""
    out = _wrap(integ.build_payload, _cid(cid), str(body.get("record_type", "")), str(body.get("record_id", "")), body.get("note"), user["user_id"])
    return envelope("LIVE", validation_status="NOT_APPLICABLE", **out, can_send=integ.CONNECTORS[cid].get("send", False),
                    blocked_reason=("Only an APPROVED recommendation can be sent." if out["record"]["record_type"] == "recommendation" and out["record"]["status"] != "APPROVED" else None))


@router.post("/{cid}/send")
def send(cid: str, body: dict = Body(...), user=Depends(require("PLANNER"))):
    note = (body.get("note") or "").strip()[:1000] or None
    return envelope("LIVE", validation_status="NOT_APPLICABLE",
                    result=_wrap(integ.send, _cid(cid), str(body.get("record_type", "")), str(body.get("record_id", "")), note, user["user_id"]))
