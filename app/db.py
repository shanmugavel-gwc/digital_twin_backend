"""Neon connectivity. Neon scales to zero, so the first connect can be slow: retry with backoff.
All SQL is schema-qualified (twin.*): the pooler is PgBouncer in transaction mode, so search_path / startup
`options` cannot be relied on."""
import time

import psycopg
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url

from .config import settings


def _need(url: str, name: str) -> str:
    if not url:
        raise RuntimeError(f"{name} is not set. Copy backend/.env.example to backend/.env and fill in the Neon URLs.")
    if "sslmode=require" not in url:
        raise RuntimeError(f"{name} must include sslmode=require")
    return url


def make_engine(url: str, name: str = "database url") -> Engine:
    # Neon drops idle connections (scale-to-zero / pooler idle timeout): keepalives + short recycle + pre_ping,
    # and callers use run_with_retry() for drops that happen mid-query (pre_ping cannot catch those).
    return create_engine(_need(url, name), pool_pre_ping=True, pool_recycle=240, pool_size=5, max_overflow=5,
                         connect_args={"connect_timeout": 15, "keepalives": 1, "keepalives_idle": 30,
                                       "keepalives_interval": 10, "keepalives_count": 3})


def pooled_engine() -> Engine:
    return make_engine(settings.neon_database_url, "NEON_DATABASE_URL")


def readonly_engine() -> Engine:
    return make_engine(settings.neon_readonly_url, "NEON_READONLY_URL")


def psycopg_conninfo(url: str) -> str:
    """SQLAlchemy URL -> libpq conninfo for raw psycopg (COPY / DDL on the direct URL)."""
    return make_url(_need(url, "NEON_DIRECT_URL")).set(drivername="postgresql").render_as_string(hide_password=False)


def connect_with_retry(conninfo: str, tries: int = 3, base_delay: float = 2.0) -> psycopg.Connection:
    last = None
    for i in range(tries):
        try:
            return psycopg.connect(conninfo, connect_timeout=20)
        except psycopg.OperationalError as e:      # compute may be waking up
            last = e
            time.sleep(base_delay * (2 ** i))
    raise RuntimeError(f"Could not connect to Neon after {tries} tries: {last}")


def direct_connection() -> psycopg.Connection:
    return connect_with_retry(psycopg_conninfo(settings.neon_direct_url))


def run_with_retry(engine: Engine, fn, tries: int = 3, base_delay: float = 0.5):
    """Run fn(engine); on a dropped/unavailable connection discard the pool and retry with backoff.
    Only for idempotent reads (writes must not be blindly retried)."""
    from sqlalchemy.exc import DBAPIError, OperationalError
    last = None
    for i in range(tries):
        try:
            return fn(engine)
        except (OperationalError, DBAPIError) as e:
            if isinstance(e, DBAPIError) and not (e.connection_invalidated or isinstance(e, OperationalError)):
                raise
            last = e
            engine.dispose()
            time.sleep(base_delay * (2 ** i))
    raise last
