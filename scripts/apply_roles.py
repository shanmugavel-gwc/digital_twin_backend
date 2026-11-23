"""Run create_roles.sql on the DIRECT URL, substituting passwords from env (never stored in the SQL file)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psycopg import sql  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import direct_connection  # noqa: E402


def main():
    rw = settings.twin_rw_password.get_secret_value()
    ro = settings.twin_ro_password.get_secret_value()
    if not rw or not ro:
        sys.exit("Set TWIN_RW_PASSWORD and TWIN_RO_PASSWORD in backend/.env first.")
    text = Path(__file__).with_name("create_roles.sql").read_text(encoding="utf-8")
    with direct_connection() as conn:
        text = (text.replace("{{RW_PASSWORD}}", sql.Literal(rw).as_string(conn))
                    .replace("{{RO_PASSWORD}}", sql.Literal(ro).as_string(conn)))
        conn.execute(text)
        conn.commit()
    print("roles OK (twin_rw, twin_ro)")


if __name__ == "__main__":
    main()
