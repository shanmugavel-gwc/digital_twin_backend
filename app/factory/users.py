"""Users and roles. IMPORTANT: this is identity SELECTION, not authentication (no passwords, no SSO): the UI sends the chosen user id in
X-User and the backend checks that user's role. It gives role separation and a named audit trail; real sign-in (SSO/OIDC) is required
before production use (docs/ASSUMPTIONS.md A-27). The API key guard still applies to every route.

Roles: VIEWER (read), ENGINEER (run the agent, comment), PLANNER (approve / reject / request changes, track actions, send records),
ADMIN (everything, users, settings, integration configuration)."""
from __future__ import annotations

import re

from fastapi import Header, HTTPException
from sqlalchemy import text

from ..deps import get_engine, query
from ..twin.scenarios import AUTOMATED_IDENTITIES, audit

ROLES = ("VIEWER", "ENGINEER", "PLANNER", "ADMIN")
RANK = {r: i for i, r in enumerate(ROLES)}
_ID = re.compile(r"^[a-z0-9._-]{2,40}$")


def list_users() -> list[dict]:
    return query("SELECT user_id, display_name, role, active, created_at FROM twin.app_user ORDER BY role DESC, user_id")


def get_user(uid: str | None) -> dict | None:
    if not uid or not _ID.match(uid):
        return None
    r = query("SELECT user_id, display_name, role, active FROM twin.app_user WHERE user_id = :u", u=uid)
    return r[0] if r and r[0]["active"] else None


def current_user(x_user: str | None = Header(default=None)) -> dict | None:
    return get_user((x_user or "").strip().lower())


def require(min_role: str):
    def dep(x_user: str | None = Header(default=None)) -> dict:
        u = get_user((x_user or "").strip().lower())
        if u is None:
            raise HTTPException(401, "Select an active user (top bar) to do this. Identity selection is not authentication.")
        if RANK[u["role"]] < RANK[min_role]:
            raise HTTPException(403, f"This needs role {min_role} or higher; {u['display_name']} is {u['role']}.")
        return u
    return dep


def upsert_user(body: dict, actor: dict | None) -> dict:
    uid = str(body.get("user_id", "")).strip().lower()
    name = str(body.get("display_name", "")).strip()
    role = str(body.get("role", "")).strip().upper()
    active = bool(body.get("active", True))
    if not _ID.match(uid):
        raise HTTPException(422, "user_id: 2-40 characters, lower-case letters, digits, . _ - only")
    if uid in AUTOMATED_IDENTITIES or name.lower() in AUTOMATED_IDENTITIES:
        raise HTTPException(422, "automated identities (agent, system, ...) cannot be users")
    if not name or len(name) > 80:
        raise HTTPException(422, "display_name is required (max 80 characters)")
    if role not in ROLES:
        raise HTTPException(422, f"role must be one of {', '.join(ROLES)}")
    first = not query("SELECT 1 FROM twin.app_user LIMIT 1")
    if first:
        if role != "ADMIN":
            raise HTTPException(422, "the first user must be an ADMIN")
    elif actor is None or actor["role"] != "ADMIN":
        raise HTTPException(403, "only an ADMIN can manage users")
    if actor and actor["user_id"] == uid and (role != "ADMIN" or not active):
        raise HTTPException(422, "you cannot remove your own ADMIN role or deactivate yourself")
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO twin.app_user (user_id, display_name, role, active) VALUES (:u, :n, :r, :a) "
                       "ON CONFLICT (user_id) DO UPDATE SET display_name = :n, role = :r, active = :a"), dict(u=uid, n=name, r=role, a=active))
        audit(c, actor["user_id"] if actor else uid, "USER_UPSERT", "app_user", uid, {"role": role, "active": active, "bootstrap": first})
    return {"user_id": uid, "display_name": name, "role": role, "active": active}
