from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import SecretStr, field_validator


class Settings(BaseSettings):
    app_name: str = "Paper Trading Research API"
    environment: str = "local"
    database_url: str = "sqlite:///./trading_app.db"
    redis_url: str = "redis://localhost:6379/0"
    allow_live_trading: bool = False
    freqtrade_paper_execution_enabled: bool = False
    freqtrade_url: str = "http://127.0.0.1:8080"
    freqtrade_username: str = ""
    freqtrade_password: SecretStr = SecretStr("")
    auth_viewer_key: SecretStr = SecretStr("")
    auth_researcher_key: SecretStr = SecretStr("")
    auth_operator_key: SecretStr = SecretStr("")
    auth_admin_key: SecretStr = SecretStr("")
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_feed: str = "sip"
    intraday_enabled: bool = True
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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
