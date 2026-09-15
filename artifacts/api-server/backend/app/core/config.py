from __future__ import annotations

import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, field_validator, model_validator


class Settings(BaseSettings):
    app_name: str = "Paper Trading Research API"
    environment: str = "local"
    # Local paper development intentionally uses deployment role keys. Production
    # must use an attributable identity assertion/JWT issued by the identity
    # boundary configured below.
    auth_mode: str = "local_role_keys"
    auth_identity_signing_secret: SecretStr = SecretStr("")
    auth_identity_previous_signing_secret: SecretStr = SecretStr("")
    auth_identity_roles: dict[str, str] = {}
    auth_identity_issuer: str = ""
    auth_identity_audience: str = ""
    auth_identity_revoked_before: int = 0
    database_url: str = "sqlite:///./trading_app.db"
    redis_url: str = "redis://localhost:6379/0"
    allow_live_trading: bool = False
    live_environment_name: str = "approved-live"
    live_broker_name: str = ""
    freqtrade_paper_execution_enabled: bool = False
    freqtrade_url: str = "http://127.0.0.1:8080"
    freqtrade_username: str = ""
    freqtrade_password: SecretStr = SecretStr("")
    auth_viewer_key: SecretStr = SecretStr("")
    auth_researcher_key: SecretStr = SecretStr("")
    auth_operator_key: SecretStr = SecretStr("")
    auth_admin_key: SecretStr = SecretStr("")
    # The legacy Alpaca pair remains the local-paper compatibility name. In
    # production it is intentionally ignored; the explicit paper pair must be
    # configured and live credentials are a separate pair.
    paper_alpaca_api_key: SecretStr = SecretStr("")
    paper_alpaca_api_secret: SecretStr = SecretStr("")
    live_alpaca_api_key: SecretStr = SecretStr("")
    live_alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_feed: str = "sip"
    intraday_enabled: bool = True
    stock_training_artifact_root: str = "./stock_training_artifacts"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg_v3(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        return value

    @field_validator("auth_identity_roles", mode="before")
    @classmethod
    def parse_identity_roles(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return json.loads(value)
        return value

    @model_validator(mode="after")
    def validate_auth_mode(self) -> "Settings":
        if self.auth_mode not in {"local_role_keys", "production_identity"}:
            raise ValueError("auth_mode must be local_role_keys or production_identity")
        return self

    def validate_production_configuration(self) -> list[str]:
        """Return non-secret production blockers without exposing secret values."""
        blockers: list[str] = []
        if self.environment == "production":
            signing_secret = self.auth_identity_signing_secret.get_secret_value()
            if self.auth_mode != "production_identity":
                blockers.append("production identity authentication is not enabled")
            if len(signing_secret) < 32:
                blockers.append("identity signing secret is missing or too short")
            if not self.auth_identity_roles:
                blockers.append("identity-to-role mapping is missing")
            elif any(role not in {"viewer", "researcher", "operator", "admin"} for role in self.auth_identity_roles.values()):
                blockers.append("identity-to-role mapping contains an unknown role")
            if not self.paper_credentials_configured:
                blockers.append("explicit paper broker credentials are missing")
            live_pair = bool(
                self.live_alpaca_api_key.get_secret_value()
                or self.live_alpaca_api_secret.get_secret_value()
            )
            if live_pair and not (
                self.live_alpaca_api_key.get_secret_value()
                and self.live_alpaca_api_secret.get_secret_value()
            ):
                blockers.append("live broker credentials must be provided as a complete pair")
        return blockers

    @property
    def paper_credentials_configured(self) -> bool:
        if self.environment == "local":
            return bool(
                (self.paper_alpaca_api_key.get_secret_value() or self.alpaca_api_key.get_secret_value())
                and (self.paper_alpaca_api_secret.get_secret_value() or self.alpaca_api_secret.get_secret_value())
            )
        return bool(
            self.paper_alpaca_api_key.get_secret_value()
            and self.paper_alpaca_api_secret.get_secret_value()
        )

    def paper_broker_credentials(self) -> tuple[str, str]:
        """Return paper credentials without ever falling back across environments."""
        if self.environment == "local":
            return (
                self.paper_alpaca_api_key.get_secret_value() or self.alpaca_api_key.get_secret_value(),
                self.paper_alpaca_api_secret.get_secret_value() or self.alpaca_api_secret.get_secret_value(),
            )
        return (
            self.paper_alpaca_api_key.get_secret_value(),
            self.paper_alpaca_api_secret.get_secret_value(),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
