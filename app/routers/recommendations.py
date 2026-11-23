"""Approval queue for agent recommendations. Decisions only by a named PLANNER/ADMIN with a comment (audit-logged, DB-enforced).
A decision triggers no plant change; approved actions are tracked by people (work order reference, status)."""
from fastapi import APIRouter, Body, Depends, Query

from ..deps import envelope, require_api_key
from ..factory import approvals
from ..factory.users import require

router = APIRouter(prefix="/recommendations", tags=["recommendations"], dependencies=[Depends(require_api_key)])
NOTE = [{"id": "R-1", "text": "approval_required is always true. The agent can only submit (PENDING_REVIEW); approval, rejection and change requests come from a named planner."},
        {"id": "R-2", "text": "Approving records a decision. No work order or external action is created automatically; the planner records the work order reference."}]


@router.get("")
def list_recommendations(status: str | None = Query(default=None, pattern="^(PENDING_REVIEW|APPROVED|REJECTED|CHANGES_REQUESTED|SUPERSEDED)$")):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, items=approvals.list_recs(status), counts=approvals.counts())


@router.get("/{rec_id}")
def get_recommendation(rec_id: str):
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE, recommendation=approvals.detail(rec_id))


@router.post("/{rec_id}/decision")
def decide(rec_id: str, body: dict = Body(...), user=Depends(require("ENGINEER"))):
    """{decision: APPROVED|REJECTED|CHANGES_REQUESTED|COMMENT, comment}. Decisions need PLANNER/ADMIN; COMMENT needs ENGINEER."""
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE,
                    recommendation=approvals.decide(rec_id, body.get("decision", ""), body.get("comment", ""), user))


@router.post("/{rec_id}/action")
def track(rec_id: str, body: dict = Body(...), user=Depends(require("PLANNER"))):
    """{action_status: NOT_STARTED|IN_PROGRESS|DONE|CANCELLED, work_order_ref?, note?} on an APPROVED recommendation."""
    return envelope("LIVE", validation_status="NOT_APPLICABLE", assumptions=NOTE,
                    recommendation=approvals.track(rec_id, body.get("action_status", ""), body.get("work_order_ref"), body.get("note"), user))
