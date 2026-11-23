"""Apply versioned migrations (backend/migrations/V*.sql) on the DIRECT Neon URL. Safe on an empty database."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import direct_connection  # noqa: E402

MIG = Path(__file__).resolve().parents[1] / "migrations"


def main():
    with direct_connection() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS twin")
        conn.execute("CREATE TABLE IF NOT EXISTS twin.schema_migrations "
                     "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT version FROM twin.schema_migrations")}
        for f in sorted(MIG.glob("V*.sql")):
            if f.stem in done:
                print(f"skip  {f.name} (already applied)")
                continue
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO twin.schema_migrations(version) VALUES (%s)", (f.stem,))
            print(f"apply {f.name}")
        conn.commit()
    print("db-init OK")


if __name__ == "__main__":
    main()
