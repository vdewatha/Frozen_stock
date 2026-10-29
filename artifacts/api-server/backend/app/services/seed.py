from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Asset, AuditLog, RiskRule, Strategy
from app.services.risk import DEFAULT_RISK_RULES

SCORECARD_REPORT = "reports/strategy-scorecard-20261028.md"
SCORECARD_DEMOTION_ACTION = "scorecard_demotion"

# Short-term reversion candidates from the 2026-10-28 scorecard.  Seeded as
# ``paper_trading_candidate``: with PAPER_EXPLORATION_ENABLED they place only
# small, tagged exploration orders that never count as promotion evidence;
# promotion still needs the normal forward-trial / reactivation evidence.
# ``symbol_scope`` and the exit keys mirror the strategy class defaults and
# can be edited per row.
REVERSION_CANDIDATES = (
    {
        "name": "RSI(2) Pullback",
        "strategy_type": "rsi2_pullback",
        "description": (
            "Connors RSI(2) pullback: close above the 200-day SMA and RSI(2) below 10; exit on a close above "
            "the 5-day SMA, a 10-session time stop or a 10% broker-side catastrophe stop. Index ETFs only."
        ),
        "parameters": {
            "rsi_period": 2, "entry_threshold": 10, "trend_window": 200, "exit_rule": "sma",
            "exit_sma_window": 5, "stop_loss_pct": 0.10, "take_profit_pct": 0, "max_holding_sessions": 10,
            "symbol_scope": ["SPY", "QQQ", "IWM", "DIA"],
        },
    },
    {
        "name": "IBS Mean Reversion",
        "strategy_type": "ibs_mean_reversion",
        "description": (
            "Internal bar strength below 0.1 with close above the 200-day SMA; exit when IBS exceeds 0.8, a "
            "10-session time stop or a 10% broker-side catastrophe stop. Index ETFs only."
        ),
        "parameters": {
            "entry_threshold": 0.1, "exit_threshold": 0.8, "trend_window": 200, "exit_rule": "ibs",
            "stop_loss_pct": 0.10, "take_profit_pct": 0, "max_holding_sessions": 10,
            "symbol_scope": ["SPY", "QQQ", "IWM", "DIA"],
        },
    },
)

MOVING_AVERAGE_DEMOTION_REASON = (
    "Strategy scorecard 2026-10-28: moving_average_crossover has negative or flat net expectancy in every "
    "window (all history -10.6 bps [-22.3, 1.2], post-2015 +4.8 [-15.8, 26.2], post-2020 -2.6 [-31.8, 24.1]; "
    "37-40% win rate). Demoted from paper_trading_active to paper_trading_candidate: it continues only as "
    "tagged exploration; re-activation requires the normal reactivation review."
)


def ensure_paper_execution_assets(db: Session) -> list[str]:
    """Create missing Asset rows for the configured paper execution universe.

    Only missing rows are added; an operator-deactivated asset is left alone
    (readiness reports it). Daily import then fills trusted history for every
    active asset, so a newly configured symbol becomes tradable once its data
    is present and fresh. Returns the symbols that were created.
    """
    from app.services.trading_universe import (
        ETF_SYMBOLS, SECTOR_NAMES, execution_symbols, sector_group,
    )

    wanted = execution_symbols()
    existing = {
        row[0] for row in db.query(Asset.symbol).filter(Asset.symbol.in_(wanted)).all()
    }
    created = [symbol for symbol in wanted if symbol not in existing]
    for symbol in created:
        group = sector_group(symbol)
        db.add(Asset(
            symbol=symbol,
            name=symbol,
            asset_type="stock",
            sector=SECTOR_NAMES.get(group),
            industry="ETF" if symbol in ETF_SYMBOLS else None,
            is_active=True,
        ))
    if created:
        db.flush()
    return created


def ensure_reversion_candidates(db: Session) -> list[str]:
    """Create missing RSI(2)/IBS candidate rows; never touches existing rows.

    An operator-edited, paused or retired row is left exactly as it is.
    Returns the strategy types that were created.
    """
    existing_types = {row[0] for row in db.query(Strategy.strategy_type).all()}
    existing_names = {row[0] for row in db.query(Strategy.name).all()}
    created = []
    for spec in REVERSION_CANDIDATES:
        if spec["strategy_type"] in existing_types or spec["name"] in existing_names:
            continue
        db.add(Strategy(
            name=spec["name"], strategy_type=spec["strategy_type"], description=spec["description"],
            parameters=dict(spec["parameters"]), is_active=True, current_status="paper_trading_candidate",
        ))
        created.append(spec["strategy_type"])
    if created:
        db.flush()
    return created


def demote_moving_average_crossover(db: Session) -> bool:
    """One-time governance demotion of the crossover from active to candidate.

    Idempotent and one-shot: it only acts while the row is
    ``paper_trading_active`` *and* no previous scorecard-demotion audit event
    exists for it, so a later re-activation through the governed
    reactivation review is never undone here.  Records a hash-chained
    ``strategy_governance`` audit event with the scorecard reason.
    """
    from app.services.audit import write_audit_log

    changed = False
    rows = db.query(Strategy).filter(Strategy.strategy_type == "moving_average_crossover").order_by(Strategy.id).all()
    for row in rows:
        if row.current_status != "paper_trading_active":
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == SCORECARD_DEMOTION_ACTION,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        row.current_status = "paper_trading_candidate"
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=SCORECARD_DEMOTION_ACTION,
            status="complete",
            message=MOVING_AVERAGE_DEMOTION_REASON,
            payload={
                "old_status": "paper_trading_active",
                "new_status": "paper_trading_candidate",
                "source": SCORECARD_REPORT,
                "expectancy_bps": {"all": -10.6, "post2015": 4.8, "post2020": -2.6},
                "expectancy_ci_bps": {"all": [-22.3, 1.2], "post2015": [-15.8, 26.2], "post2020": [-31.8, 24.1]},
                "paper_only": True,
            },
        )
        changed = True
    if changed:
        db.flush()
    return changed


def ensure_strategy_lineup(db: Session) -> dict:
    """Idempotent strategy lineup reconciliation run by every seed pass."""
    return {
        "created": ensure_reversion_candidates(db),
        "demoted_moving_average_crossover": demote_moving_average_crossover(db),
    }


def seed_defaults(db: Session) -> None:
    if not db.query(Asset).first():
        db.add_all(
            [
                Asset(symbol="SPY", name="SPDR S&P 500 ETF Trust", sector="Broad Market"),
                Asset(symbol="QQQ", name="Invesco QQQ Trust", sector="Technology"),
                Asset(symbol="AAPL", name="Apple Inc.", sector="Technology"),
                Asset(symbol="MSFT", name="Microsoft Corporation", sector="Technology"),
            ]
        )
    if not db.query(Strategy).first():
        db.add_all(
            [
                Strategy(
                    name="Moving Average Crossover",
                    strategy_type="moving_average_crossover",
                    description="Trend-following strategy using short and long moving average distance.",
                    parameters={"short_window": 20, "long_window": 50},
                    # Scorecard 2026-10-28: negative/flat in every window; a
                    # fresh database starts it as an exploration candidate.
                    current_status="paper_trading_candidate",
                ),
                Strategy(
                    name="RSI Mean Reversion",
                    strategy_type="rsi_mean_reversion",
                    description="Contrarian strategy looking for oversold and overbought RSI conditions.",
                    parameters={"buy_threshold": 30, "sell_threshold": 70},
                    current_status="research",
                ),
                Strategy(
                    name="MACD Momentum",
                    strategy_type="macd_momentum",
                    description="Momentum strategy based on MACD histogram direction.",
                    parameters={},
                    current_status="paper_trading_candidate",
                ),
                Strategy(
                    name="Ensemble Strategy",
                    strategy_type="ensemble",
                    description="Confidence-weighted vote across the technical strategy library.",
                    parameters={},
                    current_status="research",
                ),
                Strategy(
                    name="Model Predictive Long",
                    strategy_type="model_predictive_long",
                    description="Long-only paper strategy driven by probabilistic expected-return evidence.",
                    parameters={"min_probability_up": 0.56, "min_expected_return": 0.003},
                    current_status="paper_trading_candidate",
                ),
                Strategy(
                    name="Bollinger Mean Reversion",
                    strategy_type="bollinger_mean_reversion",
                    description="Mean-reversion strategy that looks for lower Bollinger Band touches with oversold RSI confirmation.",
                    parameters={"window": 20, "std_dev": 2.0, "rsi_buy": 35},
                    current_status="research",
                ),
                Strategy(
                    name="Channel Breakout",
                    strategy_type="channel_breakout",
                    description="Trend-following breakout strategy using prior channel highs and volume confirmation.",
                    parameters={"lookback": 55, "volume_window": 20},
                    current_status="research",
                ),
                Strategy(
                    name="Trend Pullback",
                    strategy_type="trend_pullback",
                    description="Trend-continuation strategy that buys controlled pullbacks inside an EMA uptrend.",
                    parameters={"fast_ema": 20, "slow_ema": 100},
                    current_status="research",
                ),
            ]
        )
    db.flush()
    ensure_strategy_lineup(db)
    ensure_paper_execution_assets(db)
    if not db.query(RiskRule).first():
        db.add(RiskRule(name="default_paper_risk", value=DEFAULT_RISK_RULES, is_active=True))
    db.commit()
