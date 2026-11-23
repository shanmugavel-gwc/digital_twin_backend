"""Container entrypoint: check that the required keys came in through the Docker environment, then start the API.

Secrets are never baked into the image (backend/.env is excluded from the build). They are passed at run time, e.g.
`docker run --env-file backend/.env ...` or the `environment:` block of docker-compose.yml. This script prints key NAMES only,
never values."""
import os
import sys

REQUIRED = {
    "NEON_DATABASE_URL": "pooled Neon URL (twin_rw role) used by the API",
}
REQUIRED_OUTSIDE_DEV = {
    "API_KEY": "X-API-Key guard for every /api route (without it the API is open)",
}
RECOMMENDED = {
    "NEON_READONLY_URL": "read-only Neon URL (twin_ro role) for the agent's SQL tool; without it chat SQL fails",
    "DOMO_TOKEN": "LLM for the agent summary and the chatbot; without it the chatbot uses keyword routing",
}


def main() -> None:
    env = os.environ
    app_env = env.get("APP_ENV", "production")
    missing = [k for k in REQUIRED if not env.get(k)]
    if app_env != "dev":
        missing += [k for k in REQUIRED_OUTSIDE_DEV if not env.get(k)]
    if missing:
        print("FATAL: missing required environment variables (pass them with -e / --env-file / compose 'environment'):", file=sys.stderr)
        for k in missing:
            print(f"  - {k}: {REQUIRED.get(k) or REQUIRED_OUTSIDE_DEV.get(k)}", file=sys.stderr)
        sys.exit(1)
    for k, why in RECOMMENDED.items():
        if not env.get(k):
            print(f"WARNING: {k} is not set ({why}).", file=sys.stderr)
    print(f"Starting Digital Twin Agent API (APP_ENV={app_env}). Keys present: "
          + ", ".join(sorted(k for k in env if k.isupper() and env.get(k) and k in {*REQUIRED, *REQUIRED_OUTSIDE_DEV, *RECOMMENDED,
                     "NEON_DIRECT_URL", "SMTP_HOST", "TEAMS_WEBHOOK_URL", "PG_SOURCE_URL", "MYSQL_SOURCE_URL"})), flush=True)
    port = env.get("PORT", "8000")
    os.execvp("uvicorn", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port,
                          "--proxy-headers", "--forwarded-allow-ips", env.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
                          "--workers", env.get("WEB_CONCURRENCY", "1")])


if __name__ == "__main__":
    main()
