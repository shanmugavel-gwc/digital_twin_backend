# syntax=docker/dockerfile:1.7
# Digital Twin Agent API (FastAPI). Build from the REPOSITORY ROOT (the image also needs data/static):
#   docker build -f backend/Dockerfile -t digital-twin-api .
# Run with keys taken from the Docker environment (never baked into the image):
#   docker run --rm -p 8000:8000 --env-file backend/.env digital-twin-api
#   docker run --rm -p 8000:8000 -e NEON_DATABASE_URL -e NEON_READONLY_URL -e API_KEY -e DOMO_TOKEN digital-twin-api
# or: docker compose up --build   (see docker-compose.yml)

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/backend

# dependencies first (cached layer)
COPY backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt

# application code + the connector templates the API reads (header-only CSVs, no data)
COPY backend/ /srv/backend/
COPY data/static/ /srv/data/static/

# non-root user; writable folders for report/integration exports
RUN useradd --create-home --uid 10001 twin \
 && mkdir -p /srv/data/exports /srv/data/integrations /srv/docs \
 && chown -R twin:twin /srv/data /srv/docs
USER twin

# ---- Configuration: NON-SECRET defaults only. Override any of them with -e at run time.
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
#   optional : NEON_DIRECT_URL              only for migrations / data loading scripts
#   optional : SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, TEAMS_WEBHOOK_URL   notifications
#   optional : PG_SOURCE_URL, MYSQL_SOURCE_URL, SHAREPOINT_*                       read-only data sources
#   optional : INTEG_<CONNECTOR>_<FIELD>   enterprise connector secrets, e.g. INTEG_SAP_PASSWORD
#   optional : TWIN_RW_PASSWORD, TWIN_RO_PASSWORD (only scripts/apply_roles.py)
# scripts/container_start.py stops the container with a clear message if a required key is missing.

EXPOSE 8000

# /api/health also wakes the Neon compute, so allow it time
HEALTHCHECK --interval=30s --timeout=20s --start-period=40s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/health', timeout=15)" || exit 1

CMD ["python", "scripts/container_start.py"]
