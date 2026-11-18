"""Runtime settings. Everything comes from environment variables (see .env.example).

Secrets (API keys, Entra client secret) are NOT read from here in production:
they live in Azure Key Vault and are fetched through prism.secrets.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRISM_", env_file=".env", extra="ignore")

    # Database. Production: Azure SQL (shared Farview DB, own schema).
    # e.g. mssql+pyodbc://@<server>.database.windows.net/<db>?driver=ODBC+Driver+17+for+SQL+Server&Authentication=ActiveDirectoryMsi
    database_url: str = "sqlite:///./prism.db"
    db_schema: str = "prism"  # only applied on SQL Server

    # Web
    base_path: str = "/prism"  # served at jarvis.farviewequity.com/prism
    session_secret: str = "change-me"
    public_url: str = "http://localhost:8040/prism"

    # Auth: "entra" in production, "dev" locally (no login, you are admin)
    auth_mode: str = "dev"
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret_name: str = "prism-entra-client-secret"  # Key Vault secret name
    entra_client_secret: str = ""  # fallback when the secret isn't in Key Vault
    allowed_email_domain: str = "farviewequity.com"
    admin_emails: str = ""  # comma separated; admins can use the Command Centre

    # Secrets backend: "keyvault" (production), "file" (fallback, chmod 600 JSON), "env" (local/tests)
    secret_backend: str = "env"
    key_vault_url: str = ""  # https://<vault>.vault.azure.net/
    secrets_file: str = "data/secrets.json"

    # Alerts (Teams incoming webhook), optional
    teams_webhook_secret_name: str = "prism-teams-webhook"

    # Ports (used by the MCP server; the web port is set in the systemd unit)
    port: int = 8040
    mcp_port: int = 8041

    # Collector
    max_workers: int = 6
    request_timeout_s: int = 120

    @property
    def admin_list(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
