"""Validated runtime configuration.

Names follow architecture A13. Validation failures report field names only, never values.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised with field names only; values may be secrets."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    trace_env: Literal["development", "test", "production"] = "development"

    # Identity: 'supabase' (production), 'local' (dev HS256 tokens) or 'off' (dev: no login, fixed dev user)
    auth_mode: Literal["supabase", "local", "off"] = "supabase"
    local_auth_secret: str = "dev-only-change-me-dev-only-change-me"

    # Control plane (any Postgres; Supabase Postgres in production)
    database_url: str | None = None
    supabase_url: str | None = None
    supabase_jwks_url: str | None = None
    supabase_jwt_audience: str = "authenticated"
    supabase_service_role_key: str | None = None

    # Evidence graph
    neo4j_uri: str | None = None
    neo4j_database: str = "neo4j"
    neo4j_username: str | None = None
    neo4j_password: str | None = None

    # Model
    openai_api_key: str | None = None
    trace_model: str = "gpt-4o-mini"

    # Credential vault (authenticated encryption; key never stored beside ciphertext)
    trace_secret_key: str | None = None
    trace_secret_key_version: int = 1
    # Older keys kept only to decrypt existing ciphertext during manual re-encryption: "1:<b64>,2:<b64>"
    trace_secret_keys_previous: str | None = None

    # Demo environment built by a collaborator (separate repos). Optional placeholders: TRACE works without them and
    # reports them as "not configured". TRACE never hardcodes these and never hands evaluator data to the investigator.
    pulselab_url: str | None = None          # browser-only traffic generator; its JSON export feeds ticket intake (T-010)
    shopflow_web_url: str | None = None
    shopflow_api_url: str | None = None      # the backend TRACE's connected Vercel project/GitHub repo describe
    shopflow_repository: str | None = None   # e.g. owner/shopflow-backend (informational; selection still goes through discovery)

    trace_public_origin: str = "http://localhost:8000"
    trace_allowed_origins: str = "http://localhost:3000"

    # GitHub: read-plane and remediation-plane are separate apps (A13)
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: str | None = None   # easier than a multi-line env value; file stays out of git
    github_webhook_secret: str | None = None
    github_remediation_app_id: str | None = None
    github_remediation_private_key: str | None = None

    # Budgets and caps (A7, A9) - starting values, not performance promises
    run_max_model_calls: int = Field(20, ge=1)
    run_max_tool_calls: int = Field(60, ge=1)
    run_max_active_seconds: int = Field(600, ge=10)
    graph_default_hops: int = Field(2, ge=1, le=4)
    graph_max_hops: int = Field(4, ge=1, le=4)
    graph_max_nodes: int = Field(100, ge=1)
    graph_max_edges: int = Field(200, ge=1)
    repo_max_fetch_bytes: int = 100 * 1024 * 1024
    repo_max_files: int = 10_000
    repo_max_file_bytes: int = 2 * 1024 * 1024
    job_lease_seconds: int = Field(60, ge=5)
    job_heartbeat_seconds: int = Field(15, ge=1)
    job_max_attempts: int = Field(3, ge=1)
    ingest_max_body_bytes: int = 1024 * 1024

    @model_validator(mode="after")
    def _load_key_file(self) -> Settings:
        if self.github_app_private_key_path and not self.github_app_private_key:
            from pathlib import Path

            try:
                self.github_app_private_key = Path(self.github_app_private_key_path).read_text(encoding="utf-8")
            except OSError:
                raise ValueError("github_app_private_key_path is not readable") from None
        return self

    @field_validator("pulselab_url", "shopflow_web_url", "shopflow_api_url", mode="before")
    @classmethod
    def _http_url(cls, v):
        if v in (None, ""):
            return None
        v = str(v).strip().rstrip("/")
        if not v.startswith(("https://", "http://")) or " " in v:
            raise ValueError("must be an http(s) URL")
        return v

    @field_validator("trace_secret_key")
    @classmethod
    def _key_shape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        import base64

        try:
            raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("must be urlsafe base64") from exc
        if len(raw) != 32:
            raise ValueError("must decode to 32 bytes")
        return v

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        if self.trace_env == "production":
            missing = [
                n
                for n in (
                    "database_url",
                    "supabase_url",
                    "supabase_jwks_url",
                    "neo4j_uri",
                    "neo4j_username",
                    "neo4j_password",
                    "openai_api_key",
                    "trace_secret_key",
                )
                if not getattr(self, n)
            ]
            if missing:
                raise ValueError("missing required settings: " + ", ".join(m.upper() for m in missing))
        if self.auth_mode in ("local", "off") and self.trace_env == "production":
            raise ValueError("auth_mode local/off is not allowed in production")
        if self.graph_default_hops > self.graph_max_hops:
            raise ValueError("graph_default_hops exceeds graph_max_hops")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        origins = [o.strip() for o in self.trace_allowed_origins.split(",") if o.strip()]
        # PulseLab is a browser app that will POST symptom tickets to the intake API, so its origin must pass CORS.
        if self.pulselab_url and self.pulselab_url not in origins:
            origins.append(self.pulselab_url)
        return origins

    def capability_report(self) -> dict[str, bool]:
        """Names and on/off only - safe to log at startup."""
        return {
            "database": bool(self.database_url),
            "auth": self.auth_mode != "supabase" or bool(self.supabase_jwks_url),
            "auth_mode": self.auth_mode,
            "neo4j": bool(self.neo4j_uri),
            "model": bool(self.openai_api_key),
            "secret_vault": bool(self.trace_secret_key),
            "github_read_app": bool(self.github_app_id and self.github_app_private_key),
            "github_remediation_app": bool(self.github_remediation_app_id and self.github_remediation_private_key),
        }


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        # Field-level errors: name only. Cross-field errors carry our own messages (names, never values).
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or e["msg"].removeprefix("Value error, ") for e in exc.errors()}
        )
        raise ConfigError("invalid configuration: " + ", ".join(fields)) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
