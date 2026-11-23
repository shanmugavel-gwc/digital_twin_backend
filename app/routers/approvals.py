from fastapi import APIRouter, Depends, Query

from ..deps import envelope, query, require_api_key

router = APIRouter(tags=["audit"], dependencies=[Depends(require_api_key)])


@router.get("/audit")
def audit(limit: int = Query(default=100, ge=1, le=500)):
    """Read-only view of the append-only audit log."""
    return envelope("LIVE", items=query(
        "SELECT id, ts, actor, action, entity, entity_id, details FROM twin.audit_log ORDER BY id DESC LIMIT :n", n=limit))
