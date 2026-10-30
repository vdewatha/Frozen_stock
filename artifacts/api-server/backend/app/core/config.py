from __future__ import annotations

import json
import hmac
import re
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr, field_validator, model_validator

# Liquid paper execution universe: broad/sector ETFs plus US megacaps. Kept in
# sync with PAPER_EXECUTION_SYMBOLS in .replit (see trading_universe.py).
DEFAULT_PAPER_EXECUTION_SYMBOLS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC", "SMH",
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "V", "MA",
    "UNH", "XOM", "LLY", "COST", "HD", "PG", "JNJ", "ABBV", "MRK", "PEP", "KO", "BAC",
    "WMT", "NFLX", "AMD", "CRM", "ORCL", "ADBE",
)
MAX_PAPER_EXECUTION_SYMBOLS = 60


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
    # Development-only paper binding. Replit secrets are shared between the
    # workspace and the published deployment, so the development database
    # cannot bind its own paper account by overwriting the PAPER_* names
    # without also rebinding production. These values replace the paper pair
    # and account binding only when ``environment == "local"``; every other
    # environment ignores them. Each database must be the sole owner of the
    # broker account it reconciles.
    dev_paper_alpaca_api_key: SecretStr = SecretStr("")
    dev_paper_alpaca_api_secret: SecretStr = SecretStr("")
    dev_paper_broker_account_id: SecretStr = SecretStr("")
    # Derived by ``apply_development_paper_binding``; never read from the env.
    paper_binding_source: str = "shared"
    alpaca_api_key: SecretStr = SecretStr("")
    alpaca_api_secret: SecretStr = SecretStr("")
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_feed: str = "sip"
    # Alpaca paper equity trades are commission-free, but this must be an
    # explicit provider-contract choice. It never authorizes live trading and
    # does not suppress separately reported account-level cash activities.
    alpaca_paper_zero_commission_contract: bool = False
    # The paper Activity SSE provides bounded historical replay and structured
    # cash-impact evidence. It does not turn missing commissions into zero.
    alpaca_activity_sse_enabled: bool = False
    # Bounded warm-up after a supervised restart (see stock_restart_grace).
    # Only stale-heartbeat / stale-data findings that predate the recorded
    # worker start are deferred, new entries stay blocked while warming up,
    # and any other breach still pauses. Requires the gateway boot markers
    # (PAPER_BOOT_ID / PAPER_WORKERS_STARTED_AT); without them there is no grace.
    stock_restart_grace_enabled: bool = True
    stock_restart_grace_seconds: int = Field(default=300, ge=0, le=900)
    # The previous process generation must have produced a watchdog heartbeat
    # within this long before the restart; longer gaps are outages, not deploys.
    stock_restart_grace_max_downtime_seconds: int = Field(default=1800, ge=60, le=7200)
    # Restart-storm guard: more grace windows than this per rolling hour are denied.
    stock_restart_grace_max_boots_per_hour: int = Field(default=3, ge=1, le=12)
    tradier_market_data_api_key: SecretStr = SecretStr("")
    tradier_api_key: SecretStr = SecretStr("")
    tradier_account_id: str = ""
    tradier_market_data_url: str = "https://api.tradier.com/v1"
    tradier_sandbox_url: str = "https://sandbox.tradier.com/v1"
    active_market_data_provider: str = "tradier"
    active_paper_broker: str = "tradier_sandbox"
    intraday_enabled: bool = True
    intraday_backfill_chunks_per_cycle: int = Field(default=1, ge=1, le=8)
    # Longest run of consecutive bar-less minutes, bracketed by real bars, that a
    # partial-venue feed (Alpaca IEX) may resolve as provider-verified no-trade
    # minutes. Longer runs stay missing and keep readiness fail-closed. 0 disables.
    intraday_no_trade_max_run_minutes: int = Field(default=5, ge=0, le=10)
    # Provider publication lag tolerated by feed_status, on top of the 60-second
    # late-trade allowance. Alpaca IEX publishes a 1-minute bar ~1-2 minutes
    # after the minute closes, so with no extra lag readiness flipped between
    # ready and stale every minute. With 120s, a bar is required once its
    # minute closed more than 3 minutes ago. A feed that stops updating is still
    # marked stale after that. 0 restores the strict boundary.
    intraday_publication_lag_seconds: int = Field(default=120, ge=0, le=300)
    # Broker-side exit re-arm (app/services/stock_exit_rearm.py): place an
    # Alpaca OCO (or a stop-only GTC order) for whole shares of a long position
    # that no live broker stop leg covers. It only adds protective sell orders,
    # and only ever for shares the broker reports as available.
    paper_exit_rearm_enabled: bool = True
    # Re-arm may also run while the account is halted or the kill switch is on.
    # Protective exits reduce risk. Recovery cancel/flatten always blocks it.
    paper_exit_rearm_while_halted: bool = True
    # A bracket entry that stays partially filled this long is cancelled, so its
    # filled shares can be covered by an OCO. Bracket legs only activate on a
    # complete fill.
    paper_exit_rearm_partial_entry_grace_minutes: int = Field(default=5, ge=1, le=390)
    iex_research_enabled: bool = False
    # A successful IEX research observation may queue a full feature refresh
    # (at most one per throttle window). Each refresh retrains every active
    # symbol; on a small VM that pegs the CPU all session and starves the
    # minutely intraday import, so small hosts turn this off and rely on the
    # off-hours feature-generation crontab instead.
    iex_feature_refresh_enabled: bool = True
    delayed_sip_research_enabled: bool = False
    research_observation_only: bool = False
    stock_training_artifact_root: str = "./stock_training_artifacts"
    # Read-only launch preflight defaults are deliberately limited to the
    # supported paper-learning universe.  A cycle request always overrides
    # these values with its immutable symbols and provider.
    stock_learning_default_symbols: list[str] = ["AAPL", "MSFT", "QQQ", "SPY"]
    stock_learning_default_provider: str = "yfinance"
    # Research can cover a wider universe than the symbols admitted to paper
    # decisions. A symbol with incomplete execution data stays research-only.
    # The default is a liquid ~45-name list (index/sector ETFs + megacaps);
    # PAPER_EXECUTION_SYMBOLS in .replit carries the same list.
    paper_execution_symbols: list[str] = list(DEFAULT_PAPER_EXECUTION_SYMBOLS)
    # Stale market data blocks only the affected symbol unless more than this
    # fraction of the execution universe (or SPY/QQQ) is stale: a broad outage
    # still blocks everything.
    paper_feed_broad_outage_fraction: float = Field(default=0.2, gt=0, le=1)
    # Correlation guard: max open (or pending-entry) positions per sector ETF
    # group for scheduled paper entries. The risk rule key
    # ``max_positions_per_sector`` overrides it; 0 disables the cap.
    paper_max_positions_per_sector: int = Field(default=3, ge=0, le=50)
    # Upper bound on extra (repair/backfill) intraday provider requests one
    # ingestion cycle may make across all symbols. The newest window for all
    # symbols is fetched with multi-symbol requests and does not count here.
    intraday_backfill_request_budget: int = Field(default=40, ge=0, le=150)
    # Server-side limit for sessions left idle inside a transaction (0 disables).
    database_idle_in_transaction_timeout_seconds: int = Field(default=600, ge=0, le=3600)
    # Wall-clock cap on repair requests per minutely import. Repairs run before
    # the import reports, so 40 sequential repair requests (~160 s on the VM)
    # made every live minute arrive late; leftover gaps wait for a later cycle.
    intraday_backfill_time_budget_seconds: float = Field(default=25.0, ge=0, le=120)
    # Paper exploration lets candidate/research strategies place small, tagged
    # paper orders to generate learning data before they earn trial evidence.
    # Exploration fills never count as promotion evidence. Off by default.
    paper_exploration_enabled: bool = False
    paper_exploration_risk_fraction: float = Field(default=0.0025, gt=0, le=0.01)
    paper_exploration_max_positions: int = Field(default=12, ge=1, le=40)
    # Volatility-scaled sizing for daily-bar strategies (``daily_bar_execution``):
    # the risk-sized notional is multiplied by target / realized volatility
    # (annualized, from the last ``lookback`` daily close-to-close returns),
    # capped at 1.0 (never larger than the unscaled size) and floored at
    # ``floor``. When volatility cannot be computed the floor is used (fail
    # closed). A target of 0 disables scaling.
    paper_vol_target_annual: float = Field(default=0.15, ge=0, le=2)
    paper_vol_lookback_days: int = Field(default=20, ge=5, le=252)
    paper_vol_scale_floor: float = Field(default=0.25, gt=0, le=1)
    # Meta-label filter for BUY signals (app/services/meta_label.py).
    # "shadow" records the calibrated probability, threshold and the take/skip
    # decision it would make on every actionable BUY without changing orders;
    # it degrades to "off" when the model artifact is missing or invalid.
    # "enforce" skips BUYs the filter rejects and fails closed (skips every
    # BUY) when the artifact is unavailable. Never enable enforce without a
    # forward paper review of the shadow records.
    paper_meta_label_mode: str = "shadow"
    # Empty = <backend>/stock_training_artifacts/meta_label/meta_label_model.joblib
    # (produced by scripts/train_meta_label.py).
    paper_meta_label_model_path: str = ""
    # Entry protections (app/services/entry_protections.py): entry-only
    # circuit breakers for scheduled and exploration paper BUYs.  They never
    # block sells/exits.  A risk-rule key with the same name minus the
    # ``paper_`` prefix overrides each value; 0 disables that protection.
    paper_stoploss_guard_count: int = Field(default=3, ge=0, le=100)
    paper_stoploss_guard_global_count: int = Field(default=0, ge=0, le=100)
    paper_stoploss_guard_lookback_minutes: int = Field(default=360, ge=0, le=10080)
    paper_stoploss_guard_pause_minutes: int = Field(default=120, ge=0, le=10080)
    paper_exit_cooldown_minutes: int = Field(default=30, ge=0, le=10080)
    paper_low_profit_trades: int = Field(default=4, ge=0, le=100)
    paper_low_profit_lock_minutes: int = Field(default=1440, ge=0, le=43200)
    paper_session_drawdown_lock_pct: float = Field(default=0.02, ge=0, le=1)
    paper_entry_open_buffer_minutes: int = Field(default=15, ge=0, le=390)
    paper_entry_close_buffer_minutes: int = Field(default=10, ge=0, le=390)
    # Per-strategy drawdown lock (entry_protections #6): realized drawdown over
    # the strategy's last N closed trades >= pct x (allocation fraction x
    # equity) blocks that strategy's new entries for the rest of the session
    # plus N sessions.  Exits are never blocked.
    paper_strategy_drawdown_trades: int = Field(default=10, ge=0, le=500)
    paper_strategy_drawdown_lock_sessions: int = Field(default=3, ge=0, le=60)
    paper_strategy_drawdown_lock_pct: float = Field(default=0.05, ge=0, le=1)
    paper_strategy_allocation_fraction: float = Field(default=0.10, ge=0, le=1)
    # Autonomous BUY entries with no fill this long after submission (same
    # session) are cancelled; 0 disables.  Sells/exit legs are never touched.
    paper_stale_entry_cancel_minutes: int = Field(default=30, ge=0, le=390)
    # Broker-rejected stock paper orders in 24h that raise one warning
    # notification (never critical, so it cannot gate exits); 0 disables.
    paper_order_reject_alert_count: int = Field(default=3, ge=0, le=1000)
    # Entry window for daily-bar strategies (``daily_bar_execution``, e.g.
    # RSI(2) Pullback / IBS Mean Reversion), in minutes after the session open
    # from ``exchange_sessions.session_bounds``.  The backtester fills these
    # entries at the next session's open, so paper BUYs are allowed only in
    # [open + start, open + end) (clamped to the session close); outside it
    # they open no new positions.  Exits are not affected.  An empty window
    # (end <= start) disables their entries.  The time-of-day entry protection
    # (``paper_entry_open_buffer_minutes``) still applies on top of this.
    paper_daily_bar_entry_window_start_minutes: int = Field(default=5, ge=0, le=390)
    paper_daily_bar_entry_window_end_minutes: int = Field(default=45, ge=0, le=390)
    # Unattended paper operation.  Explained technical halts auto-resume only
    # after cooldown plus fresh clean evidence; 0 disables the auto-resume.
    paper_auto_resume_enabled: bool = True
    # Edge-decay re-admission: quarantine before the first governed retry,
    # exponential backoff base on repeated failures, retire after N failures.
    strategy_readmission_quarantine_hours: int = Field(default=72, ge=1, le=24 * 90)
    strategy_readmission_backoff_factor: int = Field(default=2, ge=1, le=10)
    strategy_readmission_max_attempts: int = Field(default=3, ge=1, le=20)
    # Paper-only loss feedback loop (app/services/loss_feedback.py).
    # Attribution window feeding the research agenda and weights.
    loss_feedback_lookback_days: int = Field(default=30, ge=1, le=365)
    loss_agenda_max_strategies: int = Field(default=3, ge=1, le=25)
    loss_agenda_max_symbols: int = Field(default=4, ge=1, le=25)
    # Passing experiments become versioned parameter sets that must clear a
    # governed cycle (fresh holdout, challenger, forward trial, promotion).
    paper_parameter_application_enabled: bool = True
    # Meta-label auto-enforce bar (only when paper_meta_label_mode="shadow").
    meta_label_auto_enforce_enabled: bool = True
    meta_label_enforce_min_samples: int = Field(default=30, ge=5, le=10000)
    meta_label_enforce_min_precision: float = Field(default=0.55, ge=0, le=1)
    meta_label_enforce_min_recall: float = Field(default=0.5, ge=0, le=1)
    meta_label_enforce_min_lift: float = Field(default=0.05, ge=0, le=1)
    meta_label_demote_min_samples: int = Field(default=20, ge=5, le=10000)
    meta_label_demote_precision: float = Field(default=0.5, ge=0, le=1)
    # Performance-weighted paper risk fractions (bounded, shrink-first).
    paper_allocation_weighting_enabled: bool = True
    paper_allocation_floor: float = Field(default=0.5, gt=0, le=1)
    paper_allocation_ceiling: float = Field(default=1.25, ge=1, le=2)
    paper_allocation_min_trades: int = Field(default=5, ge=1, le=500)
    paper_allocation_lookback_trades: int = Field(default=20, ge=1, le=500)
    paper_allocation_lookback_days: int = Field(default=60, ge=1, le=365)
    paper_allocation_return_scale: float = Field(default=0.02, gt=0, le=1)
    paper_allocation_shrinkage_trades: int = Field(default=10, ge=0, le=500)
    paper_allocation_max_share: float = Field(default=0.5, gt=0, le=1)
    # Warn this long before a standing paper mandate expires.
    paper_mandate_expiry_warning_hours: int = Field(default=48, ge=1, le=24 * 30)
    # External alert delivery.  Channels are enabled only when configured.
    alert_min_severity: str = "warning"
    alert_dedupe_window_minutes: int = Field(default=60, ge=1, le=24 * 60)
    alert_max_attempts: int = Field(default=5, ge=1, le=20)
    alert_webhook_url: SecretStr = SecretStr("")
    alert_webhook_signing_secret: SecretStr = SecretStr("")
    alert_telegram_bot_token: SecretStr = SecretStr("")
    alert_telegram_chat_id: str = ""
    alert_email_smtp_host: str = ""
    alert_email_smtp_port: int = Field(default=587, ge=1, le=65535)
    alert_email_smtp_username: str = ""
    alert_email_smtp_password: SecretStr = SecretStr("")
    alert_email_from: str = ""
    alert_email_to: list[str] = []
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

    @field_validator("paper_execution_symbols")
    @classmethod
    def validate_paper_execution_universe(cls, values: list[str]) -> list[str]:
        symbols = sorted({value.strip().upper() for value in values})
        if not symbols or len(symbols) > MAX_PAPER_EXECUTION_SYMBOLS or any(
            not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) for symbol in symbols
        ):
            raise ValueError(
                f"Paper execution universe requires 1 to {MAX_PAPER_EXECUTION_SYMBOLS} valid stock symbols"
            )
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
        if self.active_market_data_provider not in {"tradier", "alpaca_iex", "alpaca_sip"}:
            raise ValueError("active_market_data_provider must be tradier, alpaca_iex, or alpaca_sip")
        if self.active_market_data_provider in {"alpaca_iex", "alpaca_sip"} and (
            self.allow_live_trading or self.active_paper_broker != "alpaca_paper"
        ):
            raise ValueError("Alpaca intraday feeds are available only for live-disabled Alpaca paper operation")
        if self.active_paper_broker not in {"tradier_sandbox", "alpaca_paper"}:
            raise ValueError("active_paper_broker must be tradier_sandbox or alpaca_paper")
        if self.paper_meta_label_mode not in {"off", "shadow", "enforce"}:
            raise ValueError("paper_meta_label_mode must be off, shadow, or enforce")
        return self

    @model_validator(mode="after")
    def apply_development_paper_binding(self) -> "Settings":
        """Bind the local database to its own paper account when configured.

        All three development values are required together; a partial set
        fails closed rather than mixing one account's keys with another
        account's binding. Non-local environments never read them.
        """
        dev_values = (
            self.dev_paper_alpaca_api_key.get_secret_value().strip(),
            self.dev_paper_alpaca_api_secret.get_secret_value().strip(),
            self.dev_paper_broker_account_id.get_secret_value().strip(),
        )
        self.paper_binding_source = "shared"
        if self.environment != "local" or not any(dev_values):
            return self
        if not all(dev_values):
            raise ValueError(
                "Development paper binding requires DEV_PAPER_ALPACA_API_KEY, "
                "DEV_PAPER_ALPACA_API_SECRET, and DEV_PAPER_BROKER_ACCOUNT_ID together"
            )
        self.paper_alpaca_api_key = SecretStr(dev_values[0])
        self.paper_alpaca_api_secret = SecretStr(dev_values[1])
        self.paper_broker_account_id = dev_values[2]
        self.paper_binding_source = "development"
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
