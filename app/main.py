"""FastAPI entrypoint. No endpoint, client or config anywhere writes to PLC/SCADA (asserted by tests)."""
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .config import settings
from .db import run_with_retry
from .deps import get_engine
from .routers import (admin, agent_runs, approvals, calibration, chat, connectors, factory, forecast, integrations, kpis, recommendations,
                      reports, scenarios, signals, twin)

log = logging.getLogger("twin")
@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm, daemon=True).start()      # spawn the simulation worker pool so the first run is not slow
    yield


def _warm():
    try:
        from .twin.simulator import LineParams, RunConfig, run_many
        p = LineParams("W", ["S"], [False], [{"X": 10.0}], [20], ["X"], [1.0], 100.0)
        run_many([(p, RunConfig(horizon_hours=1, stops=False), i) for i in range(12)])
    except Exception:
        log.warning("worker pool warm-up skipped")


app = FastAPI(title="Digital Twin Agent", version="0.3.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()], allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-API-Key", "X-User", "Last-Event-ID"])

if not settings.api_key.get_secret_value():
    log.warning("API_KEY is not set: X-API-Key guard is open (dev only).")

api = [twin.router, calibration.router, scenarios.router, scenarios.sim_router, forecast.router, kpis.router, approvals.router, chat.router, connectors.router, signals.router,
       factory.router, agent_runs.router, recommendations.router, reports.router, integrations.router, admin.router]
for r in api:
    app.include_router(r, prefix="/api")


@app.get("/api/health")
def health():
    """Public. Also checks Neon connectivity (and wakes the scaled-to-zero compute)."""
    try:
        def ping(e):
            with e.connect() as c:
                c.execute(text("SELECT 1"))
        run_with_retry(get_engine(), ping)
        db = "ok"
    except Exception as e:  # report class only: never echo connection strings
        db = f"error: {type(e).__name__}"
    return {"status": "ok" if db == "ok" else "degraded", "database": db, "app_env": settings.app_env,
            "state_type": "LIVE", "model_version": "TWIN-2.0"}


@app.exception_handler(RequestValidationError)
async def validation_handler(_: Request, exc: RequestValidationError):
    # Drop the submitted value ("input") and "ctx": a rejected request can carry a password and must never be echoed back.
    safe = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "validation_error", "detail": safe})


@app.exception_handler(Exception)
async def unhandled(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": type(exc).__name__})
