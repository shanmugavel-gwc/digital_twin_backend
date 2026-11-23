# syntax=docker/dockerfile:1.7
# Digital Twin Agent API (FastAPI). The build context is THIS folder (the backend), so it works both for the standalone backend
# repository (e.g. Render: repo root = backend) and for the monorepo:
#   docker build -t digital-twin-api backend            (from the monorepo root)
#   docker build -t digital-twin-api .                  (from inside backend/ or the backend repo)
# Run with keys taken from the Docker environment (never baked into the image):
#   docker run --rm -p 8000:8000 --env-file backend/.env digital-twin-api
# On Render: set the keys under Environment; Render provides PORT. Health check path: /api/health

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/backend

# dependencies first (cached layer)
COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# application code (backend/.env is excluded by .dockerignore: secrets never enter the image)
COPY . /srv/backend/

# Header-only connector templates (data/static/*.csv) generated from the connector definitions: no data, so the
# static-file connectors honestly report NOT_CONNECTED. Plus writable folders for report / integration exports.
RUN useradd --create-home --uid 10001 twin \
 && mkdir -p /srv/data/static /srv/data/exports /srv/data/integrations /srv/docs \
 && python -c "from app.connectors import REGISTRY, STATIC_DIR, _Static; [ (STATIC_DIR / c.filename).write_text(','.join(c.columns) + '\n', encoding='utf-8') for c in REGISTRY if isinstance(c, _Static) ]" \
 && rm -f /srv/backend/.env \
 && chown -R twin:twin /srv/data /srv/docs
USER twin

# ---- Configuration: NON-SECRET defaults only. Override any of them with -e / the platform's environment settings.
ENV APP_ENV=production \
    PORT=8000 \
    WEB_CONCURRENCY=1 \
    MACHINE_PLANT_SOURCE=data \
    DOMO_SUBDOMAIN=gwcteq-partner \
    SMTP_PORT=587 \
    SMTP_STARTTLS=true \
    CORS_ORIGINS=http://localhost:5173

# ---- Keys taken from the Docker environment at run time (NOT set here, never stored in the image):
#   required : NEON_DATABASE_URL            pooled Neon URL, twin_rw role (the API's database)
#   required : API_KEY                      X-API-Key guard (required unless APP_ENV=dev)
#   recommended: NEON_READONLY_URL          pooled Neon URL, twin_ro role (agent SQL tool)
#   recommended: DOMO_TOKEN, DOMO_MODEL     LLM for the agent summary and the chatbot
#   recommended: CORS_ORIGINS               comma-separated URL(s) of the deployed UI
#   optional : NEON_DIRECT_URL              only for migrations / data loading scripts
#   optional : SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, TEAMS_WEBHOOK_URL   notifications
#   optional : PG_SOURCE_URL, MYSQL_SOURCE_URL, SHAREPOINT_*                       read-only data sources
#   optional : INTEG_<CONNECTOR>_<FIELD>   enterprise connector secrets, e.g. INTEG_SAP_PASSWORD
#   optional : FORWARDED_ALLOW_IPS          set to * behind a platform proxy (Render, etc.)
# scripts/container_start.py stops the container with a clear message if a required key is missing.

EXPOSE 8000

# /api/health also wakes the Neon compute, so allow it time
HEALTHCHECK --interval=30s --timeout=20s --start-period=40s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/health', timeout=15)" || exit 1

CMD ["python", "scripts/container_start.py"]
