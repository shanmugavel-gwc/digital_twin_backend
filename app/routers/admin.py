"""Supporting sections: users and roles, agent configuration (settings) and the audit log viewer."""
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..deps import envelope, query, require_api_key
from ..factory import agent_config
from ..factory import notify
from ..factory.users import ROLES, current_user, list_users, require, upsert_user

router = APIRouter(tags=["admin"], dependencies=[Depends(require_api_key)])


@router.get("/users")
def users():
    return envelope("LIVE", validation_status="NOT_APPLICABLE", items=list_users(), roles=list(ROLES),
                    assumptions=[{"id": "U-1", "text": "Identity selection, not authentication: there are no passwords. Add SSO before production use (A-27)."}])


@router.get("/me")
def me(user=Depends(current_user)):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", user=user)


@router.post("/users")
def save_user(body: dict = Body(...), user=Depends(current_user)):
    """Create or update a user. The very first user must be an ADMIN (bootstrap); after that only an ADMIN can manage users."""
    return envelope("LIVE", validation_status="NOT_APPLICABLE", user=upsert_user(body, user))


@router.get("/settings/agent")
def get_settings():
    return envelope("LIVE", validation_status="NOT_APPLICABLE", config=agent_config.load(), defaults=agent_config.DEFAULTS, meta=agent_config.meta(),
                    channels=notify.channel_status())


@router.post("/settings/agent")
def save_settings(body: dict = Body(...), user=Depends(require("ADMIN"))):
    try:
        cfg = agent_config.save(body, user["user_id"])
    except (agent_config.ConfigError, ValueError, TypeError) as e:
        raise HTTPException(422, str(e))
    return envelope("LIVE", validation_status="NOT_APPLICABLE", config=cfg, meta=agent_config.meta())


@router.get("/audit-logs")
def audit_logs(actor: str | None = None, action: str | None = None, entity: str | None = None, q: str | None = None,
               limit: int = Query(default=100, ge=1, le=500), before_id: int | None = None):
    """Append-only audit log (DB trigger blocks UPDATE/DELETE). Filter by actor, action, entity or free text in details."""
    rows = query("SELECT id, ts, actor, action, entity, entity_id, details FROM twin.audit_log WHERE "
                 "(CAST(:a AS text) IS NULL OR actor = :a) AND (CAST(:ac AS text) IS NULL OR action ILIKE '%' || :ac || '%') "
                 "AND (CAST(:e AS text) IS NULL OR entity = :e) AND (CAST(:q AS text) IS NULL OR details::text ILIKE '%' || :q || '%' OR entity_id ILIKE '%' || :q || '%') "
                 "AND (CAST(:b AS bigint) IS NULL OR id < :b) ORDER BY id DESC LIMIT :n",
                 a=actor, ac=action, e=entity, q=q, b=before_id, n=limit)
    facets = query("SELECT 'actor' AS k, actor AS v, count(*) AS n FROM twin.audit_log GROUP BY 2 UNION ALL "
                   "SELECT 'entity', entity, count(*) FROM twin.audit_log GROUP BY 2 ORDER BY 1, 3 DESC")
    return envelope("LIVE", validation_status="NOT_APPLICABLE", items=rows, facets=facets)
