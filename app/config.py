"""Settings from backend/.env. Secrets live only here (never in code or the frontend)."""
from pathlib import Path
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    neon_database_url: str = ""      # pooled (-pooler): running app
    neon_direct_url: str = ""        # direct: DDL / migrations / bulk load
    neon_readonly_url: str = ""      # pooled, twin_ro: agent SQL tool only
    domo_subdomain: str = "gwcteq-partner"
    domo_token: SecretStr = SecretStr("")
    domo_model: str = ""
    app_env: str = "dev"
    twin_rw_password: SecretStr = SecretStr("")
    twin_ro_password: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    # External read-only sources (app/sources.py). Empty = not configured. Never returned by the API.
    pg_source_url: SecretStr = SecretStr("")
    mysql_source_url: SecretStr = SecretStr("")
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    sharepoint_client_secret: SecretStr = SecretStr("")
    sharepoint_site: str = ""        # e.g. contoso.sharepoint.com:/sites/Plant
    sharepoint_folder: str = ""      # e.g. Shared Documents/Twin
    allow_runtime_connections: bool = False   # UI "Add connection" is always on when APP_ENV=dev; set true to allow it elsewhere
    # Notifications (app/factory/notify.py). Empty = NOT_CONFIGURED: messages are prepared and shown, never faked as sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_starttls: bool = True
    teams_webhook_url: SecretStr = SecretStr("")     # Microsoft Teams incoming webhook / Workflows URL
    machine_plant_source: str = "data"   # "data" | "line"  (see docs/ASSUMPTIONS.md A-03)
    data_dir: Path = ROOT_DIR / "data"
    docs_dir: Path = ROOT_DIR / "docs"
    # calibration verdict thresholds (MAPE % on hold-out), used from Phase 3
    mape_validated_max: float = 8.0
    mape_needs_review_max: float = 15.0


settings = Settings()
