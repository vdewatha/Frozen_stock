from __future__ import annotations

import json
import hmac
import re
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
    auth_identity_principal_types: dict[str, str] = {}
    auth_identity_issuer: str = ""
    auth_identity_audience: str = ""
    auth_identity_revoked_before: int = 0
    auth_identity_max_token_seconds: int = 900
    internal_auth_secret: SecretStr = SecretStr("")
    clerk_secret_key: SecretStr = SecretStr("")
    clerk_publishable_key: str = ""
    database_url: str = "sqlite:///./trading_app.db"
    redis_url: str = "redis://localhost:6379/0"
    allow_live_trading: bool = False
    # Public paper deployments may expose viewer-only GET pages without a
    # bearer key. Mutations and elevated research routes remain authenticated.
    allow_anonymous_viewer: bool = False
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
    paper_broker_account_id: str = ""
    live_broker_account_id: str = ""
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_feed: str = "sip"
    # Alpaca paper equity trades are commission-free, but this must be an
    # explicit provider-contract choice. It never authorizes live trading and
    # does not suppress separately reported account-level cash activities.
    alpaca_paper_zero_commission_contract: bool = False
    tradier_market_data_api_key: SecretStr = SecretStr("")
    tradier_api_key: SecretStr = SecretStr("")
    tradier_account_id: str = ""
    tradier_market_data_url: str = "https://api.tradier.com/v1"
    tradier_sandbox_url: str = "https://sandbox.tradier.com/v1"
    active_market_data_provider: str = "tradier"
    active_paper_broker: str = "tradier_sandbox"
    intraday_enabled: bool = True
    iex_research_enabled: bool = False
    delayed_sip_research_enabled: bool = False
    research_observation_only: bool = False
    stock_training_artifact_root: str = "./stock_training_artifacts"
    # Read-only launch preflight defaults are deliberately limited to the
    # supported paper-learning universe.  A cycle request always overrides
    # these values with its immutable symbols and provider.
    stock_learning_default_symbols: list[str] = ["AAPL", "MSFT", "QQQ", "SPY"]
    stock_learning_default_provider: str = "yfinance"
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # A shared workspace .env also contains frontend and provider-specific
    # values. The backend should consume only its declared settings rather
    # than refusing to start because another service added a valid variable.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("stock_learning_default_symbols")
    @classmethod
    def validate_learning_universe(cls, values: list[str]) -> list[str]:
        symbols = sorted({value.strip().upper() for value in values})
        if not symbols or len(symbols) > 25 or any(
            not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) for symbol in symbols
        ):
            raise ValueError("Learning universe requires 1 to 25 valid stock symbols")
        return symbols

    @field_validator("database_url", mode="before")
    @classmethod
    def use_psycopg_v3(cls, value: str) -> str:
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        return value

    @field_validator("auth_identity_roles", "auth_identity_principal_types", mode="before")
    @classmethod
    def parse_identity_roles(cls, value):
        if value is None or value == "":
            return {}
        if isinstance(value, str):
            return json.loads(value)
        return value

    @model_validator(mode="after")
    def validate_auth_mode(self) -> "Settings":
        if self.auth_mode not in {"local_role_keys", "production_identity", "clerk_gateway"}:
            raise ValueError("auth_mode must be local_role_keys, production_identity, or clerk_gateway")
        if self.active_market_data_provider not in {"tradier"}:
            raise ValueError("active_market_data_provider must be tradier")
        if self.active_paper_broker not in {"tradier_sandbox", "alpaca_paper"}:
            raise ValueError("active_paper_broker must be tradier_sandbox or alpaca_paper")
        return self

    def validate_production_configuration(self) -> list[str]:
        """Return non-secret production blockers without exposing secret values."""
        blockers: list[str] = []
        if self.production_identity_required:
            if self.auth_mode == "clerk_gateway":
                if len(self.internal_auth_secret.get_secret_value()) < 32:
                    blockers.append("internal Clerk gateway authentication is unavailable")
                if not self.clerk_secret_key.get_secret_value() or not self.clerk_publishable_key:
                    blockers.append("Clerk production authentication is not configured")
            elif self.auth_mode == "production_identity":
                signing_secret = self.auth_identity_signing_secret.get_secret_value()
                if len(signing_secret) < 32:
                    blockers.append("identity signing secret is missing or too short")
                if not self.auth_identity_roles:
                    blockers.append("identity-to-role mapping is missing")
                elif any(role not in {"viewer", "researcher", "operator", "admin"} for role in self.auth_identity_roles.values()):
                    blockers.append("identity-to-role mapping contains an unknown role")
                principal_types = set(self.auth_identity_principal_types.values())
                required_types = {"operator", "reviewer", "service", "worker", "scheduler", "emergency"}
                if set(self.auth_identity_principal_types) != set(self.auth_identity_roles):
                    blockers.append("identity principal inventory does not match the role mapping")
                elif not required_types.issubset(principal_types):
                    blockers.append("identity principal inventory is missing a required production class")
                if not self.auth_identity_issuer:
                    blockers.append("identity issuer is missing")
                if not self.auth_identity_audience:
                    blockers.append("identity audience is missing")
                if self.auth_identity_max_token_seconds < 60 or self.auth_identity_max_token_seconds > 3600:
                    blockers.append("identity token lifetime limit is invalid")
            else:
                blockers.append("production identity authentication is not enabled")
            if not self.paper_credentials_configured:
                blockers.append("explicit paper broker credentials are missing")
            if not self.paper_account_binding:
                blockers.append("explicit paper broker account binding is missing")
            live_pair = bool(
                self.live_alpaca_api_key.get_secret_value()
                or self.live_alpaca_api_secret.get_secret_value()
            )
            if live_pair and not (
                self.live_alpaca_api_key.get_secret_value()
                and self.live_alpaca_api_secret.get_secret_value()
            ):
                blockers.append("live broker credentials must be provided as a complete pair")
            if live_pair and not self.live_broker_account_id.strip():
                blockers.append("explicit live broker account binding is missing")
            if live_pair and (
                hmac.compare_digest(
                    self.paper_alpaca_api_key.get_secret_value(),
                    self.live_alpaca_api_key.get_secret_value(),
                )
                or hmac.compare_digest(
                    self.paper_alpaca_api_secret.get_secret_value(),
                    self.live_alpaca_api_secret.get_secret_value(),
                )
            ):
                blockers.append("paper and live broker credentials must be distinct")
            if (
                self.paper_broker_account_id.strip()
                and self.live_broker_account_id.strip()
                and hmac.compare_digest(
                    self.paper_broker_account_id.strip(),
                    self.live_broker_account_id.strip(),
                )
            ):
                blockers.append("paper and live broker account bindings must be distinct")
            if self.allow_live_trading:
                admin_count = sum(role == "admin" for role in self.auth_identity_roles.values())
                if self.environment != self.live_environment_name or self.live_environment_name != "approved-live":
                    blockers.append("live trading requires the approved-live runtime environment")
                if not self.live_broker_name.strip():
                    blockers.append("live trading requires an explicit live broker")
                if not self.live_credentials_configured:
                    blockers.append("live trading requires explicit live broker credentials")
                if admin_count < 2:
                    blockers.append("live trading requires two distinct mapped admin identities")
        return blockers

    @property
    def paper_credentials_configured(self) -> bool:
        if self.active_paper_broker == "tradier_sandbox":
            return bool(
                self.tradier_api_key.get_secret_value()
                and self.tradier_account_id.strip()
            )
        if self.environment == "local":
            return bool(
                (self.paper_alpaca_api_key.get_secret_value() or self.alpaca_api_key.get_secret_value())
                and (self.paper_alpaca_api_secret.get_secret_value() or self.alpaca_api_secret.get_secret_value())
            )
        return bool(
            self.paper_alpaca_api_key.get_secret_value()
            and self.paper_alpaca_api_secret.get_secret_value()
        )

    @property
    def paper_account_binding(self) -> str:
        if self.paper_broker_account_id.strip():
            return self.paper_broker_account_id.strip()
        if self.active_paper_broker == "tradier_sandbox":
            return self.tradier_account_id.strip()
        return ""

    @property
    def production_identity_required(self) -> bool:
        return self.environment == "production" or self.environment == self.live_environment_name

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

    def research_alpaca_credentials(self) -> tuple[str, str]:
        """Return credentials scoped to read-only Alpaca market-data research.

        A legacy ``ALPACA_API_*`` pair may be used for the data API only. It is
        never used by the paper broker adapter or any order endpoint.
        """
        paper_key = self.paper_alpaca_api_key.get_secret_value()
        paper_secret = self.paper_alpaca_api_secret.get_secret_value()
        if paper_key and paper_secret:
            return paper_key, paper_secret
        return self.alpaca_api_key.get_secret_value(), self.alpaca_api_secret.get_secret_value()

    @property
    def live_credentials_configured(self) -> bool:
        return bool(
            self.live_alpaca_api_key.get_secret_value()
            and self.live_alpaca_api_secret.get_secret_value()
        )

    def live_broker_credentials(self) -> tuple[str, str]:
        """Return only the explicitly configured live credential pair."""
        return (
            self.live_alpaca_api_key.get_secret_value(),
            self.live_alpaca_api_secret.get_secret_value(),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
