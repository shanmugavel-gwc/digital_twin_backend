import json
import time
import uuid
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..agent import run_agent
from ..deps import envelope, get_engine, query, require_api_key
from ..domo_llm import LLMError, default_llm

router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])
_HITS: dict[str, deque] = defaultdict(deque)
RATE = (12, 60.0)                                           # 12 chat requests per 60 s per client


def get_llm():
    """The configured LLM, or None (the chat then answers by deterministic keyword routing from the factory tools)."""
    try:
        return default_llm()
    except LLMError:
        return None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


def _rate_limit(key: str):
    now, q = time.time(), _HITS[key]
    while q and now - q[0] > RATE[1]:
        q.popleft()
    if len(q) >= RATE[0]:
        raise HTTPException(429, "Too many chat requests; please wait a minute.")
    q.append(now)


@router.get("/conversations")
def conversations():
    return envelope("SIMULATED", items=query("SELECT id, title, created_at FROM twin.conversations ORDER BY created_at DESC LIMIT 100"))


@router.get("/conversations/{cid}")
def conversation(cid: str):
    try:
        uuid.UUID(cid)
    except ValueError:
        raise HTTPException(404, "conversation not found")
    msgs = query("SELECT role, content, steps, created_at FROM twin.messages WHERE conversation_id = CAST(:c AS uuid) ORDER BY id", c=cid)
    return envelope("SIMULATED", messages=msgs)


def _attachments(steps: list[dict]) -> list[dict]:
    """Tables / charts returned by factory tools, shown under the answer as supporting data."""
    out = []
    for s in steps:
        o = s.get("output")
        if isinstance(o, dict):
            for k in ("table", "chart"):
                if isinstance(o.get(k), dict):
                    out.append({"kind": k, "tool": s["tool"], **o[k]})
    return out[:6]


def _fallback(message: str) -> dict:
    """No LLM available: deterministic intent routing to one factory tool, answer templated from its output only."""
    from ..factory.chat_tools import fallback_answer, route
    from ..tools import TOOL_MAP
    name, args = route(message)
    try:
        out = TOOL_MAP[name].invoke(args)
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"answer": fallback_answer(name, out) + "\n\nModel: TWIN-2.0 | State: LIVE | Confidence: n/a | Validation: n/a | Key assumptions: answered by keyword routing (LLM unavailable)",
            "steps": [{"tool": name, "input": args, "output": out}], "model_version": "TWIN-2.0", "validation_status": None, "mode": "FALLBACK_NO_LLM"}


@router.post("/chat")
def chat(req: ChatRequest, request: Request, llm=Depends(get_llm)):
    _rate_limit(request.headers.get("x-api-key") or (request.client.host if request.client else "anon"))
    cid = req.conversation_id
    history: list[dict] = []
    if cid:
        try:
            uuid.UUID(cid)
        except ValueError:
            raise HTTPException(404, "conversation not found")
        history = query("SELECT role, content FROM twin.messages WHERE conversation_id = CAST(:c AS uuid) ORDER BY id", c=cid)
        if not history and not query("SELECT 1 FROM twin.conversations WHERE id = CAST(:c AS uuid)", c=cid):
            raise HTTPException(404, "conversation not found")
    if llm is None:
        out = _fallback(req.message)
    else:
        try:
            out = run_agent(llm, req.message, history)
            out["mode"] = "LLM_AGENT"
        except LLMError:
            out = _fallback(req.message)
    with get_engine().begin() as c:
        if not cid:
            cid = str(c.execute(text("INSERT INTO twin.conversations (title) VALUES (:t) RETURNING id"), dict(t=req.message[:60])).scalar_one())
        c.execute(text("INSERT INTO twin.messages (conversation_id, role, content) VALUES (CAST(:c AS uuid), 'user', :m)"), dict(c=cid, m=req.message))
        c.execute(text("INSERT INTO twin.messages (conversation_id, role, content, steps) VALUES (CAST(:c AS uuid), 'assistant', :m, CAST(:s AS jsonb))"),
                  dict(c=cid, m=out["answer"], s=json.dumps(out["steps"], default=str)))
    from ..factory.chat_tools import FOLLOW_UPS
    used = [s["tool"] for s in out["steps"]]
    follow = next((FOLLOW_UPS[t] for t in reversed(used) if t in FOLLOW_UPS), FOLLOW_UPS["get_factory_status"])
    any_sim = any(t in ("run_scenario", "compare_scenarios", "get_bottleneck_map", "get_calibration_report") for t in used)
    return envelope("SIMULATED" if any_sim else "LIVE", model_version=out["model_version"], validation_status=out["validation_status"] or "n/a",
                    conversation_id=cid, answer=out["answer"], steps=out["steps"], attachments=_attachments(out["steps"]), follow_ups=follow,
                    mode=out.get("mode"),
                    assumptions=[{"id": "CHAT", "text": "The agent can only read, simulate and recommend. Recommendations go to engineering review under change control."}])
