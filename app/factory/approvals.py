"""Human approval of agent recommendations and tracking of approved actions.

A decision needs a named, active user with role PLANNER or ADMIN and a comment. The status change and the review row are written in one
transaction; the database trigger (V004) refuses any status change without a matching review row, so the agent cannot approve anything.
Approval records a decision only: it triggers no plant change and creates no work order by itself. A planner records the work order
reference and progress afterwards (action tracking)."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import text

from ..deps import get_engine, query, query_many
from ..twin.scenarios import AUTOMATED_IDENTITIES, audit
from .users import RANK

DECISIONS = ("APPROVED", "REJECTED", "CHANGES_REQUESTED", "COMMENT")
ACTION_STATES = ("NOT_STARTED", "IN_PROGRESS", "DONE", "CANCELLED")


def _rec(rec_id: str) -> dict:
    r = query("SELECT * FROM twin.recommendation WHERE CAST(rec_id AS text) = :i", i=rec_id)
    if not r:
        raise HTTPException(404, "recommendation not found")
    return r[0]


def detail(rec_id: str) -> dict:
    r = _rec(rec_id)
    J = {
        "reviews": ("SELECT reviewer, role, decision, comment, decided_at FROM twin.recommendation_review WHERE rec_id = CAST(:i AS uuid) ORDER BY id", {"i": rec_id}),
        "actions": ("SELECT ts, actor, action_status, work_order_ref, note FROM twin.recommendation_action WHERE rec_id = CAST(:i AS uuid) ORDER BY id", {"i": rec_id}),
        "reports": ("SELECT report_id, title, category FROM twin.report WHERE run_id = :r AND category IN ('MAINTENANCE_RECOMMENDATIONS', 'MACHINE_HEALTH', "
                    "'PRODUCTION_PERFORMANCE', 'QUALITY_ANALYSIS', 'FACTORY_OVERVIEW') ORDER BY category", {"r": r["run_id"]}),
    }
    if r["finding_id"]:
        J["finding"] = ("SELECT finding_id, rank, category, severity, title, explanation, root_causes, evidence, value_label, state_type FROM twin.agent_finding "
                        "WHERE finding_id = :f", {"f": r["finding_id"]})
    if r["machine_id"]:
        J["machine"] = ("SELECT m.*, mp.station_id, mp.zone FROM twin.dim_machine m LEFT JOIN twin.v_machine_3d_mapping mp USING (machine_id) WHERE m.machine_id = :m",
                        {"m": r["machine_id"]})
        J["state"] = ("SELECT status, health_score, risk_score FROM twin.agent_machine_state WHERE run_id = :r AND machine_id = :m", {"r": r["run_id"], "m": r["machine_id"]})
    R = query_many(J)
    reviews, actions, reports = R["reviews"], R["actions"], R["reports"]
    finding, machine, state = R.get("finding", []), R.get("machine", []), R.get("state", [])
    return {**r, "rec_id": str(r["rec_id"]), "run_id": str(r["run_id"]), "finding_id": str(r["finding_id"]) if r["finding_id"] else None,
            "reviews": reviews, "actions": actions, "finding": ({**finding[0], "finding_id": str(finding[0]["finding_id"])} if finding else None),
            "machine": machine[0] if machine else None, "machine_state": state[0] if state else None, "reports": reports}


def list_recs(status: str | None, limit: int = 200) -> list[dict]:
    rows = query("SELECT r.rec_id, r.run_id, r.machine_id, r.line_id, r.category, r.issue, r.risk_level, r.action, r.expected_impact, r.confidence, r.status, "
                 "r.action_status, r.work_order_ref, r.created_at, r.updated_at, "
                 "(SELECT row_to_json(v) FROM (SELECT reviewer, role, decision, comment, decided_at FROM twin.recommendation_review x WHERE x.rec_id = r.rec_id "
                 "AND x.decision <> 'COMMENT' ORDER BY id DESC LIMIT 1) v) AS decision "
                 "FROM twin.recommendation r WHERE (CAST(:s AS text) IS NULL OR r.status = :s) "
                 "ORDER BY CASE r.status WHEN 'PENDING_REVIEW' THEN 0 ELSE 1 END, "
                 "CASE r.risk_level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, r.created_at DESC LIMIT :n", s=status, n=limit)
    return [{**r, "rec_id": str(r["rec_id"]), "run_id": str(r["run_id"])} for r in rows]


def decide(rec_id: str, decision: str, comment: str, user: dict) -> dict:
    decision = (decision or "").upper()
    comment = (comment or "").strip()
    if decision not in DECISIONS:
        raise HTTPException(422, f"decision must be one of {', '.join(DECISIONS)}")
    if not comment:
        raise HTTPException(422, "a comment is required")
    if len(comment) > 2000:
        raise HTTPException(422, "comment too long (max 2000 characters)")
    if user["user_id"] in AUTOMATED_IDENTITIES or user["display_name"].lower() in AUTOMATED_IDENTITIES:
        raise HTTPException(403, "decisions must come from a named human user")
    if decision != "COMMENT" and RANK[user["role"]] < RANK["PLANNER"]:
        raise HTTPException(403, f"approving, rejecting or requesting changes needs role PLANNER or ADMIN; {user['display_name']} is {user['role']}")
    r = _rec(rec_id)
    if decision != "COMMENT" and r["status"] != "PENDING_REVIEW":
        raise HTTPException(409, f"only a PENDING_REVIEW recommendation can be decided (current status: {r['status']})")
    who = f"{user['display_name']} ({user['user_id']})"
    with get_engine().begin() as c:
        c.execute(text("INSERT INTO twin.recommendation_review (rec_id, reviewer, role, decision, comment) VALUES (CAST(:i AS uuid), :w, :r, :d, :c)"),
                  dict(i=rec_id, w=who, r=user["role"], d=decision, c=comment))
        if decision != "COMMENT":
            c.execute(text("UPDATE twin.recommendation SET status = :d WHERE rec_id = CAST(:i AS uuid)"), dict(d=decision, i=rec_id))
        audit(c, user["user_id"], f"RECOMMENDATION_{decision}", "recommendation", rec_id,
              {"reviewer": who, "role": user["role"], "comment": comment, "plant_change": "none (decision recorded only)"})
    return detail(rec_id)


def track(rec_id: str, action_status: str, work_order_ref: str | None, note: str | None, user: dict) -> dict:
    action_status = (action_status or "").upper()
    if action_status not in ACTION_STATES:
        raise HTTPException(422, f"action_status must be one of {', '.join(ACTION_STATES)}")
    if RANK[user["role"]] < RANK["PLANNER"]:
        raise HTTPException(403, "action tracking needs role PLANNER or ADMIN")
    r = _rec(rec_id)
    if r["status"] != "APPROVED":
        raise HTTPException(409, "only an APPROVED recommendation can be tracked")
    wo = (work_order_ref or "").strip()[:60] or r["work_order_ref"]
    with get_engine().begin() as c:
        c.execute(text("UPDATE twin.recommendation SET action_status = :s, work_order_ref = :w WHERE rec_id = CAST(:i AS uuid)"), dict(s=action_status, w=wo, i=rec_id))
        c.execute(text("INSERT INTO twin.recommendation_action (rec_id, actor, action_status, work_order_ref, note) VALUES (CAST(:i AS uuid), :a, :s, :w, :n)"),
                  dict(i=rec_id, a=f"{user['display_name']} ({user['user_id']})", s=action_status, w=wo, n=(note or "").strip()[:1000] or None))
        audit(c, user["user_id"], "RECOMMENDATION_ACTION_TRACKED", "recommendation", rec_id, {"action_status": action_status, "work_order_ref": wo})
    return detail(rec_id)


def counts() -> dict:
    rows = query("SELECT status, count(*) AS n FROM twin.recommendation GROUP BY 1")
    acts = query("SELECT action_status, count(*) AS n FROM twin.recommendation WHERE status = 'APPROVED' GROUP BY 1")
    return {"by_status": {r["status"]: r["n"] for r in rows}, "approved_actions": {r["action_status"]: r["n"] for r in acts}}
