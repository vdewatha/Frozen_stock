from __future__ import annotations

from typing import Callable, Optional

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
            "rsi_period": 2, "entry_threshold": 10, "trend_filter_sma": 200, "exit_rule": "sma",
            "exit_sma_window": 5, "stop_loss_pct": 0.10, "take_profit_pct": 0, "max_holding_sessions": 10,
            "symbol_scope": ["SPY", "QQQ", "IWM", "DIA"],
        },
    },
    {
        "name": "IBS Mean Reversion",
        "strategy_type": "ibs_mean_reversion",
        "description": (
            "Internal bar strength below 0.1 (no 200-day trend filter; see reports/regime-filter-20261029.md); "
            "exit when IBS exceeds 0.8, a "
            "10-session time stop or a 10% broker-side catastrophe stop. Index ETFs only."
        ),
        "parameters": {
            "entry_threshold": 0.1, "exit_threshold": 0.8, "trend_filter_sma": 0, "exit_rule": "ibs",
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


IBS_TREND_FILTER_ACTION = "regime_filter_update"
IBS_TREND_FILTER_REASON = (
    "reports/regime-filter-20261029.md: the 200-day trend filter lowered IBS expectancy in every "
    "window (2001+: +3.3 vs +15.3 bps/trade), so the seeded IBS row drops it."
)


def drop_ibs_trend_filter(db: Session) -> bool:
    """One-time parameter correction for IBS rows seeded with the 200-day filter.

    Acts only on a row still carrying the originally seeded ``trend_window:
    200`` without an explicit ``trend_filter_sma`` (an operator choice is never
    overridden) and with no previous correction audit event.
    """
    from app.services.audit import write_audit_log

    changed = False
    rows = db.query(Strategy).filter(Strategy.strategy_type == "ibs_mean_reversion").order_by(Strategy.id).all()
    for row in rows:
        params = dict(row.parameters or {})
        if "trend_filter_sma" in params or params.get("trend_window") != 200:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == IBS_TREND_FILTER_ACTION,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        row.parameters = params | {"trend_filter_sma": 0}
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=IBS_TREND_FILTER_ACTION,
            status="complete",
            message=IBS_TREND_FILTER_REASON,
            payload={"old_trend_filter_sma": 200, "new_trend_filter_sma": 0, "paper_only": True},
        )
        changed = True
    if changed:
        db.flush()
    return changed


SCORECARD_RETIREMENT_ACTION = "scorecard_retirement"
# Strategy scorecard 2026-10-28, best variant per family, net bps per trade
# [95% CI] over 45 symbols: both lose on the full history and their 37% win
# rate is the 3%/6% bracket's break-even, so exploring them only costs money.
SCORECARD_RETIREMENTS = {
    "macd_momentum": {"all": [-15.6, -30.0, -1.0], "post2015": [14.3, -10.8, 40.6], "post2020": [12.6, -20.2, 42.4]},
    "moving_average_crossover": {"all": [-10.6, -22.3, 1.2], "post2015": [4.8, -15.8, 26.2],
                                 "post2020": [-2.6, -31.8, 24.1]},
}


def retire_scorecard_losers(db: Session) -> list[str]:
    """One-time retirement of families with negative full-history expectancy.

    Acts only on exploration rows (candidate/research) with no previous
    retirement audit event, so an operator's later choice, or a strategy that
    re-earned a status through a new governed research cycle, is never
    overridden. Retired rows leave the signal cycle; open lots keep their
    broker exit legs and the protective stop. Returns the retired types.
    """
    from app.services.audit import write_audit_log

    retired = []
    rows = db.query(Strategy).filter(Strategy.strategy_type.in_(tuple(SCORECARD_RETIREMENTS))).order_by(Strategy.id).all()
    for row in rows:
        if row.current_status not in {"paper_trading_candidate", "research"}:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == SCORECARD_RETIREMENT_ACTION,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        old_status, row.current_status = row.current_status, "retired"
        evidence = SCORECARD_RETIREMENTS[row.strategy_type]
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=SCORECARD_RETIREMENT_ACTION,
            status="complete",
            message=(f"{SCORECARD_REPORT}: {row.strategy_type} loses money on the full history "
                     f"({evidence['all'][0]:+.1f} bps per trade); retired from paper exploration."),
            payload={
                "old_status": old_status, "new_status": "retired", "source": SCORECARD_REPORT,
                "expectancy_bps": {window: values[0] for window, values in evidence.items()},
                "expectancy_ci_bps": {window: values[1:] for window, values in evidence.items()},
                "paper_only": True,
            },
        )
        retired.append(row.strategy_type)
    if retired:
        db.flush()
    return retired


RSI2_SCOPE_REPORT = "reports/rsi2-scope-20261102.md"
RSI2_SCOPE_ACTION = "rsi2_scope_update"
RSI2_SEEDED_SCOPE = ("SPY", "QQQ", "IWM", "DIA")
# Index ETFs plus the single stocks of the paper universe. Sector ETFs stay
# out: with production settings they earn +1 to +11 bps per trade.
RSI2_WIDENED_SCOPE = RSI2_SEEDED_SCOPE + (
    "AAPL", "ABBV", "ADBE", "AMD", "AMZN", "AVGO", "BAC", "COST", "CRM", "GOOGL", "HD", "JNJ", "JPM",
    "KO", "LLY", "MA", "META", "MRK", "MSFT", "NFLX", "NVDA", "ORCL", "PEP", "PG", "TSLA", "UNH",
    "V", "WMT", "XOM",
)


def widen_rsi2_scope(db: Session) -> bool:
    """One-time widening of RSI(2) Pullback from the index ETFs to single stocks.

    Acts only on a row whose scope is still the seeded index list and with no
    previous update event, so an operator's own scope is never overridden.
    Stocks keep the RSI < 10 entry: RSI < 5 did not improve the recent windows.
    """
    from app.services.audit import write_audit_log

    changed = False
    for row in db.query(Strategy).filter(Strategy.strategy_type == "rsi2_pullback").order_by(Strategy.id).all():
        params = dict(row.parameters or {})
        scope = params.get("symbol_scope")
        if scope is not None and sorted(scope) != sorted(RSI2_SEEDED_SCOPE):
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == RSI2_SCOPE_ACTION,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        row.parameters = params | {"symbol_scope": list(RSI2_WIDENED_SCOPE)}
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=RSI2_SCOPE_ACTION,
            status="complete",
            message=(f"{RSI2_SCOPE_REPORT}: RSI(2) < 10 on single stocks earns +16.6 bps per trade since 2015 "
                     "(index ETFs +13.7) on 6.6x the trades; scope widened from the index ETFs."),
            payload={"old_symbol_scope": list(RSI2_SEEDED_SCOPE), "new_symbol_scope": list(RSI2_WIDENED_SCOPE),
                     "source": RSI2_SCOPE_REPORT, "paper_only": True},
        )
        changed = True
    if changed:
        db.flush()
    return changed


PAPER_EVIDENCE_PAUSE_ACTION = "paper_evidence_pause"
# Weekly report 2026-W45 (2026-11-06): 11 closed paper trades, 2 wins (18%),
# -184.3 bps per trade against +26.8 bps in its backtest; 2 or fewer wins in
# 11 has about a 3% chance at a 50% win rate. Its monitor check
# (model.realized_performance) was in breach, and retraining it per symbol was
# the slowest part of the signal cycle.
PAPER_EVIDENCE_PAUSES = {
    "model_predictive_long": {"trades": 11, "wins": 2, "paper_bps": -184.3, "backtest_bps": 26.8,
                              "source": "weekly report 2026-W45"},
}


def pause_paper_evidence_losers(db: Session) -> list[str]:
    """One-time pause of strategies whose paper results contradict their backtest.

    ``paused`` leaves the signal cycle (no new entries, no signal compute) but
    keeps open lots' broker exit legs and the protective stop, and the governed
    re-admission path can bring the strategy back. Acts only on a candidate or
    research row with no earlier pause event, so an operator's later choice
    stands.
    """
    from app.services.audit import write_audit_log

    paused: list[str] = []
    rows = db.query(Strategy).filter(Strategy.strategy_type.in_(tuple(PAPER_EVIDENCE_PAUSES))).order_by(Strategy.id).all()
    for row in rows:
        if row.current_status not in {"paper_trading_candidate", "research", "paper_trading_active"}:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == PAPER_EVIDENCE_PAUSE_ACTION,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        evidence = PAPER_EVIDENCE_PAUSES[row.strategy_type]
        old_status, row.current_status = row.current_status, "paused"
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=PAPER_EVIDENCE_PAUSE_ACTION,
            status="complete",
            message=(f"{evidence['source']}: {evidence['wins']} wins in {evidence['trades']} closed paper trades, "
                     f"{evidence['paper_bps']:+.1f} bps per trade against {evidence['backtest_bps']:+.1f} in its "
                     "backtest; paused from paper trading. Open lots keep their exits."),
            payload={"old_status": old_status, "new_status": "paused", **evidence, "paper_only": True},
        )
        paused.append(row.strategy_type)
    if paused:
        db.flush()
    return paused


REGIME_GATE_ACTION = "regime_gate_update"
REGIME_GATE_REPORT = "reports/regime-gate-20261106.md"
# The ADX ``range`` gate kept or raised expectancy and cut drawdown for these
# two in the 2026-11-06 backtest; it lowered expectancy for RSI(2), IBS and
# Channel Breakout, which stay ungated.
REGIME_GATED_STRATEGIES = {"rsi_mean_reversion": "range", "bollinger_mean_reversion": "range"}


def _enable_gate_once(db: Session, gates: dict[str, dict], *, key: str, action: str,
                      message: Callable[[dict], str], source: str) -> list[str]:
    """Merge ``gates[strategy_type]`` into each matching row's parameters, once.

    Acts only on a row with no ``key`` parameter and no previous ``action``
    event, so an operator who sets or removes the gate later is never
    overridden.
    """
    from app.services.audit import write_audit_log

    changed: list[str] = []
    rows = db.query(Strategy).filter(Strategy.strategy_type.in_(gates)).order_by(Strategy.id).all()
    for row in rows:
        params = dict(row.parameters or {})
        if key in params:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == action,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        gate = gates[row.strategy_type]
        row.parameters = params | gate
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=action,
            status="complete",
            message=message(gate),
            payload={**gate, "source": source, "paper_only": True},
        )
        changed.append(row.strategy_type)
    if changed:
        db.flush()
    return changed


def enable_regime_gates(db: Session) -> list[str]:
    """One-time ``regime_gate`` for the strategies the ADX backtest supports."""
    return _enable_gate_once(
        db, {slug: {"regime_gate": gate} for slug, gate in REGIME_GATED_STRATEGIES.items()},
        key="regime_gate", action=REGIME_GATE_ACTION, source=REGIME_GATE_REPORT,
        message=lambda gate: (f"{REGIME_GATE_REPORT}: BUY only when ADX(14) < 20 (sideways market); the gate kept or "
                              "raised expectancy and cut portfolio drawdown in the backtest. Sells and exits are unchanged."),
    )


VIX_GATE_ACTION = "vix_gate_update"
VIX_GATE_REPORT = "reports/vix-gate-20261106.md"
# Checked on each strategy's live configuration (paper scope plus the SPY-trend
# gate) in the 2026-11-06 backtest: VIX < 20 on top of Channel Breakout's
# SPY-20 gate raised net expectancy and total profit in all three windows
# without raising portfolio drawdown.  IBS with VIX >= 20 raised both too but
# worsened drawdown since 2015; RSI(2) with VIX >= 20 earned more per trade but
# less in total; RSI(14) and Bollinger failed.  Those stay ungated.
VIX_GATED_STRATEGIES = {
    "channel_breakout": {"vix_gate": "below", "vix_level": 20},
}


def enable_vix_gates(db: Session) -> list[str]:
    """One-time ``vix_gate`` for the strategies the VIX backtest supports."""
    def message(gate: dict) -> str:
        when = "at or above" if gate["vix_gate"] == "above" else "below"
        return (f"{VIX_GATE_REPORT}: BUY only when the VIX close is {when} {gate['vix_level']:g}; the gate kept or "
                "raised expectancy without raising portfolio drawdown in the backtest. Sells and exits are unchanged.")

    return _enable_gate_once(db, VIX_GATED_STRATEGIES, key="vix_gate", action=VIX_GATE_ACTION,
                             source=VIX_GATE_REPORT, message=message)


OPEN_POSITION_LIMIT_ACTION = "open_position_limit_update"
PREVIOUS_MAX_OPEN_POSITIONS = 10


SIZING_MODE_ACTION = "sizing_mode_update"
SIZING_MODE_REPORT = "reports/position-sizing-20261106.md"
# The 2026-11-06 sizing backtest adopted no mode: every mode is capped at the
# fixed-fraction notional, so it can only shrink entries, and the cases where
# it bound (the 10% stop strategies at 0.25% risk, quarter Kelly where the
# payoff ratio is low) lowered book profit in at least one window.  An
# adopted mode is written here as ``{slug: {"sizing_mode": ..., "risk_per_trade": ...}}``.
SIZED_STRATEGIES: dict[str, dict] = {}


def apply_sizing_modes(db: Session) -> list[str]:
    """One-time ``sizing_mode`` / ``risk_per_trade`` for the strategies the sizing backtest adopted."""
    def message(mode: dict) -> str:
        risk = mode.get("risk_per_trade")
        budget = f" with {float(risk):.2%} of equity at risk per trade" if risk is not None else ""
        return (f"{SIZING_MODE_REPORT}: size scheduled BUYs by {mode['sizing_mode']}{budget}; the mode kept or raised "
                "book profit without a worse drawdown in every backtest window. Capped at the fixed fraction; "
                "exits and gates are unchanged.")

    return _enable_gate_once(db, SIZED_STRATEGIES, key="sizing_mode", action=SIZING_MODE_ACTION,
                             source=SIZING_MODE_REPORT, message=message)


def raise_open_position_limit(db: Session) -> bool:
    """One-time raise of ``max_open_positions`` from the old default to the new one.

    The 2026-11-04 exploration cap raise (12 -> 20 lots) was blocked by this
    risk rule at 10 open positions. Acts only on an active rule still at the
    old default with no previous update event, so an operator's own value is
    never overridden. Every other limit is unchanged.
    """
    from app.services.audit import write_audit_log

    new_limit = DEFAULT_RISK_RULES["max_open_positions"]
    changed = False
    for rule in db.query(RiskRule).filter(RiskRule.is_active.is_(True)).order_by(RiskRule.id).all():
        value = dict(rule.value or {})
        if value.get("max_open_positions") != PREVIOUS_MAX_OPEN_POSITIONS or new_limit <= PREVIOUS_MAX_OPEN_POSITIONS:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "risk_governance", AuditLog.action == OPEN_POSITION_LIMIT_ACTION,
            AuditLog.entity_type == "risk_rule", AuditLog.entity_id == rule.id,
        ).first()
        if already:
            continue
        rule.value = value | {"max_open_positions": new_limit}
        write_audit_log(
            db, event_type="risk_governance", entity_type="risk_rule", entity_id=rule.id,
            action=OPEN_POSITION_LIMIT_ACTION, status="complete",
            message=(f"Open-position limit raised from {PREVIOUS_MAX_OPEN_POSITIONS} to {new_limit} to match the "
                     "exploration lot cap; per-strategy, exposure and risk-per-trade limits are unchanged."),
            payload={"old_max_open_positions": PREVIOUS_MAX_OPEN_POSITIONS, "new_max_open_positions": new_limit,
                     "paper_only": True},
        )
        changed = True
    if changed:
        db.flush()
    return changed


MARKET_GATE_ACTION = "market_gate_update"
MARKET_GATE_REPORT = "reports/market-gate-20261106.md"
# SPY-trend gate N (no new longs while SPY <= its N-day average) for the
# strategies where it kept or raised expectancy in all three windows without
# worsening drawdown in the 2026-11-06 backtest.  The others stay ungated.
MARKET_GATED_STRATEGIES = {"macd_momentum": 50, "channel_breakout": 20, "trend_pullback": 10, "rsi2_pullback": 50}


def enable_market_gates(db: Session) -> list[str]:
    """One-time ``market_gate_sma`` for the strategies the backtest supports.

    Same contract as ``enable_regime_gates``: a row that already has the key,
    or had it set by an earlier update event, is left to the operator.
    """
    from app.services.audit import write_audit_log
    from app.services.market_gate import MARKET_GATE_KEY

    changed: list[str] = []
    rows = db.query(Strategy).filter(Strategy.strategy_type.in_(MARKET_GATED_STRATEGIES)).order_by(Strategy.id).all()
    for row in rows:
        params = dict(row.parameters or {})
        if MARKET_GATE_KEY in params:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance",
            AuditLog.action == MARKET_GATE_ACTION,
            AuditLog.entity_type == "strategy",
            AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        window = MARKET_GATED_STRATEGIES[row.strategy_type]
        row.parameters = params | {MARKET_GATE_KEY: window}
        write_audit_log(
            db,
            event_type="strategy_governance",
            entity_type="strategy",
            entity_id=row.id,
            action=MARKET_GATE_ACTION,
            status="complete",
            message=(f"{MARKET_GATE_REPORT}: no new longs while SPY is at or below its {window}-day average; "
                     "the gate kept or raised expectancy without worsening drawdown in the backtest. "
                     "Sells and exits are unchanged."),
            payload={MARKET_GATE_KEY: window, "source": MARKET_GATE_REPORT, "paper_only": True},
        )
        changed.append(row.strategy_type)
    if changed:
        db.flush()
    return changed


SECTOR_GATE_ACTION = "sector_gate_update"
BREADTH_GATE_ACTION = "breadth_gate_update"
ENTRY_CAP_ACTION = "entry_cap_update"
# Sector / breadth gates and the session entry cap are adopted by the
# experiment engine (``sector_gate`` / ``breadth_gate`` / ``entry_cluster_cap``
# templates); no hand-run report has adopted a value yet, so these ship
# empty.  A report that does fills them in and the steps below apply them
# once, under the same contract as ``enable_market_gates``: a row (or the
# risk rule) that already carries the key, or had it set by an earlier
# update event (the engine's adoption or revert included), is left alone.
SECTOR_GATED_STRATEGIES: dict[str, int] = {}
BREADTH_GATED_STRATEGIES: dict[str, float] = {}
ENTRY_CAP_PER_SESSION: int = 0
LESSON_TEMPLATES_REPORT = "reports/lesson-templates-20261106.md"


def apply_governed_parameter(db: Session, *, key: str, action: str, values: dict, report: str,
                             describe) -> list[str]:
    """One-time strategy-row parameter under a governance action; ``describe(value)`` words the audit message."""
    from app.services.audit import write_audit_log

    changed: list[str] = []
    if not values:
        return changed
    rows = db.query(Strategy).filter(Strategy.strategy_type.in_(values)).order_by(Strategy.id).all()
    for row in rows:
        params = dict(row.parameters or {})
        if key in params:
            continue
        already = db.query(AuditLog.id).filter(
            AuditLog.event_type == "strategy_governance", AuditLog.action == action,
            AuditLog.entity_type == "strategy", AuditLog.entity_id == row.id,
        ).first()
        if already:
            continue
        value = values[row.strategy_type]
        row.parameters = params | {key: value}
        write_audit_log(
            db, event_type="strategy_governance", entity_type="strategy", entity_id=row.id, action=action,
            status="complete", message=f"{report}: {describe(value)}. Sells and exits are unchanged.",
            payload={key: value, "source": report, "paper_only": True},
        )
        changed.append(row.strategy_type)
    if changed:
        db.flush()
    return changed


def apply_sector_gates(db: Session) -> list[str]:
    return apply_governed_parameter(
        db, key="sector_gate_sma", action=SECTOR_GATE_ACTION, values=SECTOR_GATED_STRATEGIES,
        report=LESSON_TEMPLATES_REPORT,
        describe=lambda n: f"no new longs in a symbol while its sector ETF is at or below its {n}-day average")


def apply_breadth_gates(db: Session) -> list[str]:
    return apply_governed_parameter(
        db, key="breadth_gate_min", action=BREADTH_GATE_ACTION, values=BREADTH_GATED_STRATEGIES,
        report=LESSON_TEMPLATES_REPORT,
        describe=lambda p: f"no new longs while under {p:g}% of S&P 500 members are above their 200-day average")


def apply_entry_cap(db: Session) -> bool:
    """One-time ``max_new_entries_per_session`` on the active risk rule (same contract, risk-rule entity)."""
    from app.services.audit import write_audit_log
    from app.services.risk_settings import get_active_risk_rule, update_risk_settings

    if not ENTRY_CAP_PER_SESSION:
        return False
    rule = get_active_risk_rule(db)
    if "max_new_entries_per_session" in (rule.value or {}):
        return False
    already = db.query(AuditLog.id).filter(
        AuditLog.event_type == "strategy_governance", AuditLog.action == ENTRY_CAP_ACTION,
        AuditLog.entity_type == "risk_rule",
    ).first()
    if already:
        return False
    message = f"{LESSON_TEMPLATES_REPORT}: at most {ENTRY_CAP_PER_SESSION} new entries per session across the book."
    update_risk_settings(db, {"max_new_entries_per_session": ENTRY_CAP_PER_SESSION}, reason=message, actor="seed")
    write_audit_log(db, event_type="strategy_governance", entity_type="risk_rule", action=ENTRY_CAP_ACTION,
                    status="complete", message=message,
                    payload={"max_new_entries_per_session": ENTRY_CAP_PER_SESSION,
                             "source": LESSON_TEMPLATES_REPORT, "paper_only": True})
    return True


HEDGE_ADMISSION_ACTION = "hedge_admission"
HEDGE_STRATEGY_NAME = "Market Hedge"


def admit_market_hedge(db: Session) -> Optional[str]:
    """One-time admission of the ``market_hedge`` row (``reports/hedge-20261106.md``).

    Long SH while SPY trades below a falling 50-day average, through the
    normal paper long path.  The row is created ``paper_trading_candidate``
    (learning-sized exploration, like a discovered rule) only when the hedge
    backtest adopted a size (``market_hedge.ADOPTED_HEDGE_FRACTION``);
    otherwise it is created ``paused`` so the forward loop can admit it later
    through its normal re-admission gates.  Acts once: a row of this type that
    already exists, or an earlier ``hedge_admission`` event (the operator
    deleted or re-statused the row), means the operator's choice stands.
    Returns the status it created, or None.
    """
    from app.services.audit import write_audit_log
    from app.services.strategies.market_hedge import (
        ADOPTED_HEDGE_FRACTION, FAST_WINDOW, HEDGE_EXIT_POLICY, HEDGE_REPORT, HEDGE_SYMBOL, TREND_WINDOW,
        MarketHedgeStrategy,
    )

    slug = MarketHedgeStrategy.slug
    exists = db.query(Strategy.id).filter(
        (Strategy.strategy_type == slug) | (Strategy.name == HEDGE_STRATEGY_NAME)).first()
    already = db.query(AuditLog.id).filter(
        AuditLog.event_type == "strategy_governance", AuditLog.action == HEDGE_ADMISSION_ACTION).first()
    if exists or already:
        return None
    adopted = ADOPTED_HEDGE_FRACTION is not None
    status = "paper_trading_candidate" if adopted else "paused"
    row = Strategy(
        name=HEDGE_STRATEGY_NAME, strategy_type=slug,
        description=(f"Long {HEDGE_SYMBOL} (inverse S&P 500) while SPY closes below its {TREND_WINDOW}-day average "
                     f"with the {FAST_WINDOW}-day average below it; exit when SPY closes back above the "
                     f"{TREND_WINDOW}-day average or on the {HEDGE_EXIT_POLICY.stop_loss_pct:.0%} catastrophe stop. "
                     "The mirror image of the SPY market gate, placed through the ordinary long path."),
        parameters={
            "trend_sma": TREND_WINDOW, "fast_sma": FAST_WINDOW,
            "hedge_fraction": ADOPTED_HEDGE_FRACTION if adopted else 0,
            "stop_loss_pct": HEDGE_EXIT_POLICY.stop_loss_pct, "take_profit_pct": HEDGE_EXIT_POLICY.take_profit_pct,
            "max_holding_sessions": HEDGE_EXIT_POLICY.max_holding_sessions, "symbol_scope": [HEDGE_SYMBOL],
        },
        is_active=True, current_status=status,
    )
    db.add(row)
    db.flush()
    if adopted:
        message = (f"{HEDGE_REPORT}: a {ADOPTED_HEDGE_FRACTION:.0%} {HEDGE_SYMBOL} hedge while the market gate is closed "
                   "kept the book's total profit and lowered its max drawdown in every window; admitted as a "
                   "learning-sized paper candidate. Promotion needs forward evidence like any other candidate.")
    else:
        message = (f"{HEDGE_REPORT}: no hedge size kept the book's total profit while lowering its max drawdown in "
                   f"every window; the {HEDGE_SYMBOL} hedge is created paused. The forward loop's re-admission gates "
                   f"decide whether it trades; admitting it also means PAPER_HEDGE_SYMBOLS={HEDGE_SYMBOL}.")
    write_audit_log(
        db, event_type="strategy_governance", entity_type="strategy", entity_id=row.id,
        action=HEDGE_ADMISSION_ACTION, status="complete", message=message,
        payload={"new_status": status, "adopted": adopted, "hedge_fraction": ADOPTED_HEDGE_FRACTION,
                 "hedge_symbol": HEDGE_SYMBOL, "source": HEDGE_REPORT, "paper_only": True},
    )
    db.flush()
    return status


SECTOR_REVERSAL_ACTION = "sector_reversal_admission"
SECTOR_REVERSAL_NAME = "Sector Reversal"
SECTOR_RANK_ACTION = "sector_rank_update"
# Strategies for which ``scripts/run_sector_reversal_backtest.py`` adopted
# sector-relative ranking of same-day candidates (``sector_ranking``):
# expectancy not lower and drawdown not worse in every window.  Written as
# ``{slug: {"rank_by": "rel_z_5"}}``.
SECTOR_RANKED_STRATEGIES: dict[str, dict] = {}


def admit_sector_reversal(db: Session) -> Optional[str]:
    """One-time admission of the ``sector_reversal`` row (``reports/sector-reversal-20261107.md``).

    Created ``paper_trading_candidate`` (learning-sized exploration, like a
    discovered rule) only when the pre-registered backtest admitted it
    (``strategies.sector_reversal.ADMITTED``: the default rule reached the
    discovery gate's ``explore`` tier on its own); otherwise ``paused``, so
    the forward loop's re-admission gates or an operator decide later.  Acts
    once: an existing row of this type or name, or an earlier
    ``sector_reversal_admission`` event (the operator deleted or re-statused
    the row), means the operator's choice stands.  Returns the status it
    created, or None.
    """
    from app.core.config import DEFAULT_PAPER_EXECUTION_SYMBOLS
    from app.services.audit import write_audit_log
    from app.services.sector_relative import static_sector
    from app.services.strategies.sector_reversal import (
        ADMISSION, ADMITTED, DEFAULT_RULE, REPORT, SECTOR_REVERSAL_EXIT_POLICY, SectorReversalStrategy,
    )

    slug = SectorReversalStrategy.slug
    exists = db.query(Strategy.id).filter(
        (Strategy.strategy_type == slug) | (Strategy.name == SECTOR_REVERSAL_NAME)).first()
    already = db.query(AuditLog.id).filter(
        AuditLog.event_type == "strategy_governance", AuditLog.action == SECTOR_REVERSAL_ACTION).first()
    if exists or already:
        return None
    status = "paper_trading_candidate" if ADMITTED else "paused"
    scope = [s for s in dict.fromkeys(str(x).upper() for x in DEFAULT_PAPER_EXECUTION_SYMBOLS) if static_sector(s)]
    row = Strategy(
        name=SECTOR_REVERSAL_NAME, strategy_type=slug,
        description=("Sector-relative short-term reversal: close above the 200-day SMA and the 5-day return "
                     "relative to the stock's sector ETF more than two standard deviations under its 60-day norm; "
                     "exit when the relative RSI(2) recovers above 70 or the close is back above its 5-day "
                     "average, a 10-session time stop or a 10% catastrophe stop. Single stocks with a sector ETF."),
        parameters={"rule": DEFAULT_RULE, "stop_loss_pct": SECTOR_REVERSAL_EXIT_POLICY.stop_loss_pct,
                    "take_profit_pct": SECTOR_REVERSAL_EXIT_POLICY.take_profit_pct,
                    "max_holding_sessions": SECTOR_REVERSAL_EXIT_POLICY.max_holding_sessions,
                    "symbol_scope": scope, "rank_by": "rel_z_5"},
        is_active=True, current_status=status,
    )
    db.add(row)
    db.flush()
    message = (f"{REPORT}: {ADMISSION}" + (" Admitted as a learning-sized paper candidate; promotion needs forward "
                                           "evidence like any other candidate." if ADMITTED else
                                           " Created paused; the forward loop's re-admission gates decide whether it "
                                           "trades."))
    write_audit_log(
        db, event_type="strategy_governance", entity_type="strategy", entity_id=row.id,
        action=SECTOR_REVERSAL_ACTION, status="complete", message=message,
        payload={"new_status": status, "admitted": ADMITTED, "rule": DEFAULT_RULE, "symbol_scope": scope,
                 "source": REPORT, "paper_only": True},
    )
    db.flush()
    return status


def apply_sector_ranking(db: Session) -> list[str]:
    """One-time ``rank_by = rel_z_5`` for the strategies the sector-reversal backtest adopted it for."""
    from app.services.strategies.sector_reversal import REPORT

    return _enable_gate_once(
        db, SECTOR_RANKED_STRATEGIES, key="rank_by", action=SECTOR_RANK_ACTION, source=REPORT,
        message=lambda gate: (f"{REPORT}: when several symbols signal this strategy on the same day, take them by "
                              "the 5-day return relative to their sector (most negative first); the ranking kept "
                              "expectancy and drawdown in every backtest window. Signals and exits are unchanged."),
    )


def ensure_strategy_lineup(db: Session) -> dict:
    """Idempotent strategy lineup reconciliation run by every seed pass."""
    return {
        "created": ensure_reversion_candidates(db),
        "demoted_moving_average_crossover": demote_moving_average_crossover(db),
        "dropped_ibs_trend_filter": drop_ibs_trend_filter(db),
        "retired_scorecard_losers": retire_scorecard_losers(db),
        "widened_rsi2_scope": widen_rsi2_scope(db),
        "regime_gates_enabled": enable_regime_gates(db),
        "market_gates_enabled": enable_market_gates(db),
        "vix_gates_enabled": enable_vix_gates(db),
        "paused_paper_losers": pause_paper_evidence_losers(db),
        "sizing_modes_applied": apply_sizing_modes(db),
        "hedge_admission": admit_market_hedge(db),
        "sector_reversal_admission": admit_sector_reversal(db),
        "sector_ranking_applied": apply_sector_ranking(db),
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
    db.flush()
    raise_open_position_limit(db)
    db.commit()
