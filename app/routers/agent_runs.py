"""Run the central Digital Twin Agent and follow it live. The stream is Server-Sent Events built from rows the backend writes as each
step really progresses (twin.agent_run_event); the UI never simulates progress."""
import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ..deps import envelope, query, require_api_key
from ..factory import agent_runner
from ..factory.users import require

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(require_api_key)])


def _uuid(s: str) -> str:
    try:
        return str(uuid.UUID(s))
    except ValueError:
        raise HTTPException(404, "run not found")


def _run(rid: str) -> dict:
    r = query("SELECT run_id, agent_name, status, triggered_by, current_step, records_processed, data_as_of, summary, error, state_type, started_at, finished_at, "
              "extract(epoch FROM (coalesce(finished_at, now()) - started_at)) AS duration_s FROM twin.agent_run WHERE run_id = CAST(:r AS uuid)", r=rid)
    if not r:
        raise HTTPException(404, "run not found")
    return {**r[0], "run_id": str(r[0]["run_id"]), "duration_s": float(r[0]["duration_s"])}


@router.post("/runs")
def start_run(user=Depends(require("ENGINEER"))):
    """RUN DIGITAL TWIN AGENT. Needs role ENGINEER or higher. Returns immediately; follow /runs/{id}/stream."""
    try:
        rid = agent_runner.start(user["user_id"])
    except agent_runner.RunConflict as e:
        raise HTTPException(409, str(e))
    return envelope("LIVE", validation_status="NOT_APPLICABLE", run=_run(rid), steps=[{"step": n, "name": name, "substeps": s} for n, name, s in agent_runner.STEPS])


@router.get("/runs")
def list_runs(limit: int = Query(default=30, ge=1, le=200)):
    rows = query("SELECT run_id, agent_name, status, triggered_by, current_step, records_processed, data_as_of, started_at, finished_at, "
                 "extract(epoch FROM (coalesce(finished_at, now()) - started_at)) AS duration_s, summary->>'findings' AS findings, "
                 "summary->>'recommendations' AS recommendations, error FROM twin.agent_run ORDER BY started_at DESC LIMIT :n", n=limit)
    return envelope("LIVE", validation_status="NOT_APPLICABLE", items=[{**r, "run_id": str(r["run_id"]), "duration_s": float(r["duration_s"])} for r in rows],
                    steps=[{"step": n, "name": name, "substeps": s} for n, name, s in agent_runner.STEPS])


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    rid = _uuid(run_id)
    run = _run(rid)
    ev = query("SELECT id, ts, step, kind, status, message, data FROM twin.agent_run_event WHERE run_id = CAST(:r AS uuid) ORDER BY id", r=rid)
    findings = query("SELECT finding_id, rank, category, severity, machine_id, line_id, station_id, title, explanation, root_causes, evidence, value_label, state_type "
                     "FROM twin.agent_finding WHERE run_id = CAST(:r AS uuid) ORDER BY rank", r=rid)
    return envelope("LIVE", validation_status="NOT_APPLICABLE", run=run, events=ev, findings=[{**f, "finding_id": str(f["finding_id"])} for f in findings],
                    steps=[{"step": n, "name": name, "substeps": s} for n, name, s in agent_runner.STEPS],
                    assumptions=[{"id": "AG-1", "text": "Findings are CALCULATED by stated rules or PREDICTED by the calibrated twin; each carries its label and evidence."}])


@router.get("/runs/{run_id}/stream")
async def stream(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
    """SSE: one event per persisted row (event: STEP|LOG|WARN|ERROR|FINDING), then `event: done` with the final run row."""
    rid = _uuid(run_id)
    await run_in_threadpool(_run, rid)
    last = int(request.headers.get("last-event-id") or after)

    async def gen():
        nonlocal last
        idle = 0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            rows = await run_in_threadpool(query, "SELECT id, ts, step, kind, status, message, data FROM twin.agent_run_event "
                                                  "WHERE run_id = CAST(:r AS uuid) AND id > :a ORDER BY id LIMIT 200", r=rid, a=last)
            for r in rows:
                last = r["id"]
                yield f"id: {r['id']}\nevent: {r['kind']}\ndata: {json.dumps(r, default=str)}\n\n"
            if not rows:
                run = await run_in_threadpool(_run, rid)
                if run["status"] != "RUNNING":
                    yield f"event: done\ndata: {json.dumps(run, default=str)}\n\n"
                    return
                idle += 1
                if idle % 20 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.4)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
