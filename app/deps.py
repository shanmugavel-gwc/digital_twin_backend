"""Shared dependencies: DB engine (pooled, cached) and the X-API-Key guard."""
import hmac
from functools import lru_cache

from fastapi import Header, HTTPException
from sqlalchemy.engine import Engine

from .config import settings
from .db import pooled_engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return pooled_engine()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Applied to every non-health route. If API_KEY is unset (dev only) the guard is open and a warning is logged at boot."""
    expected = settings.api_key.get_secret_value()
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def query(sql: str, **params) -> list[dict]:
    """Idempotent read with retry on dropped Neon connections. Parameterised only."""
    from sqlalchemy import text

    from .db import run_with_retry

    def go(e):
        # AUTOCOMMIT: a single read needs no transaction, which saves the BEGIN and ROLLBACK round-trips to Neon.
        with e.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            return [dict(r) for r in c.execute(text(sql), params).mappings().all()]
    return run_with_retry(get_engine(), go)


def envelope(state_type: str, **payload) -> dict:
    """Every response carries state_type, model_version, validation_status and an assumptions array."""
    base = {"state_type": state_type, "model_version": "TWIN-2.0", "validation_status": "NOT_CALIBRATED", "assumptions": []}
    base.update(payload)
    return base


def query_many(jobs: dict[str, tuple[str, dict]], workers: int = 6) -> dict[str, list[dict]]:
    """Run several independent reads concurrently (Neon round-trips dominate). jobs = {name: (sql, params)}."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs) or 1)) as ex:
        futs = {k: ex.submit(query, sql, **params) for k, (sql, params) in jobs.items()}
        return {k: f.result() for k, f in futs.items()}
