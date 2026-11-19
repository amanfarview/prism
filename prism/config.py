"""Runtime settings. Everything comes from environment variables (see .env.example).

Production (Jarvis VM, since Nov 2026): systemd loads /etc/prism/prism.env (root-only, 600) into the
services' environment. There is no .env in the app folder; the .env fallback below is for a laptop.

Secrets (provider API keys, Entra client secret, Teams webhook) are fetched through prism.secrets.
On the VM they live in /var/lib/prism/secrets.json (600, owned by the prism service user).

Fail closed: if the settings file goes missing, Prism starts in sign-in mode and refuses to start at all
without a real session secret, rather than coming up as an open admin console behind Caddy.
"""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}
DEFAULT_SESSION_SECRET = "change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRISM_", env_file=".env", extra="ignore")

    # production | staging | dev. Staging switches off everything that costs money or leaves the box.
    environment: str = "production"

    # Database. Production: Azure SQL (shared Farview DB "jarvis", own schema "prism", login prismapp).
    # e.g. mssql+pyodbc://prismapp:<pw>@farview.database.windows.net/jarvis?driver=ODBC+Driver+18+for+SQL+Server
    database_url: str = "sqlite:///./prism.db"
    db_schema: str = "prism"  # only applied on SQL Server

    # Web
    base_path: str = "/prism"  # served at jarvis.farviewequity.com/prism
    session_secret: str = DEFAULT_SESSION_SECRET
    public_url: str = "http://localhost:8040/prism"
    max_body_mb: int = 25  # largest request body (crawler log uploads, CSV imports)

    # Auth: "entra" (default, production) or "dev" (laptop only: no login, you are admin)
    auth_mode: str = "entra"
    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret_name: str = "prism-entra-client-secret"  # name in the secret store
    entra_client_secret: str = ""  # fallback when the secret isn't in the secret store
    allowed_email_domain: str = "farviewequity.com"
    admin_emails: str = ""  # comma separated; admins can use the Command Centre

    # Secrets backend: "file" (production: chmod 600 JSON owned by the service user),
    # "keyvault" (VM managed identity; not used on Jarvis, see DEPLOY.md), "env" (laptop/tests)
    secret_backend: str = "env"
    key_vault_url: str = ""  # https://<vault>.vault.azure.net/
    secrets_file: str = "data/secrets.json"

    # Alerts (Teams / Power Automate webhook), optional
    teams_webhook_secret_name: str = "prism-teams-webhook"

    # Ports (used by the MCP server; the web port is set in the systemd unit)
    port: int = 8040
    mcp_port: int = 8041

    # Collector
    max_workers: int = 6
    request_timeout_s: int = 120

    # Outbound fetches of other people's websites (site audit): refuse private, loopback and cloud metadata
    # addresses. Only tests switch this off.
    block_private_fetch: bool = True

    @model_validator(mode="after")
    def _fail_closed(self):
        self.auth_mode = (self.auth_mode or "").strip().lower()
        self.environment = (self.environment or "").strip().lower()
        if self.auth_mode not in ("entra", "dev"):
            raise ValueError(f"PRISM_AUTH_MODE must be 'entra' or 'dev', not {self.auth_mode!r}")
        if self.auth_mode == "dev":
            host = (urlparse(self.public_url).hostname or "").lower()
            if host not in LOCAL_HOSTS:
                raise ValueError(
                    f"PRISM_AUTH_MODE=dev (no sign-in, everyone is admin) is only allowed on localhost, "
                    f"but PRISM_PUBLIC_URL is {self.public_url}. Refusing to start.")
        else:
            if self.session_secret == DEFAULT_SESSION_SECRET or len(self.session_secret) < 32:
                raise ValueError("PRISM_SESSION_SECRET is missing or shorter than 32 characters. Refusing to start.")
        return self

    @property
    def admin_list(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def is_staging(self) -> bool:
        return self.environment == "staging"

    @property
    def public_host(self) -> str:
        return (urlparse(self.public_url).netloc or "").lower()


@lru_cache
def get_settings() -> Settings:
    return Settings()
