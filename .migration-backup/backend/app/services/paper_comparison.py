"""Isolated forward-only simulated strategy comparison. No broker imports.

Execution uses the next on-time observed candle-close proxy, never a historical
next-open fill. This is an explicit coarse simulation, not executable quotes.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import sqlite3

from app.services.crypto_collection import _check
from app.services.instruments import utc_timestamp

STRATEGIES = ("trend", "mean_reversion", "cost_ml", "cash", "buy_hold")
UNIT = Decimal("0.00000001")


def number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not an amount")
    result = Decimal(str(value))
    if not result.is_finite() or abs(result) > Decimal("1000000000000"):
        raise ValueError("Invalid finite amount")
    return result


def money(value):
    return value.quantize(UNIT, rounding=ROUND_HALF_EVEN)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ComparisonConfig:
    ml_model_id: str = "unavailable"
    fee_rate: str = "0.008"
    slippage_rate: str = "0.001"
    starting_cash: str = "10000"
    order_budget: str = "100"
    maximum_drawdown: str = "0.10"
    ml_entry_probability: str = "0.60"
    ml_exit_probability: str = "0.40"
    ml_expected_net_margin: str = "0"
    ml_horizon_hours: int = 24
    fee_profile: str = "baseline"

    def validated(self):
        for field in ("fee_rate", "slippage_rate"):
            if not 0 <= number(getattr(self, field)) <= Decimal("0.05"):
                raise ValueError("Per-side costs must be 0..5%")
        if not 0 < number(self.order_budget) <= number(self.starting_cash) <= Decimal("1000000000"):
            raise ValueError("Invalid account budget")
        if not 0 < number(self.maximum_drawdown) <= 1:
            raise ValueError("Invalid drawdown limit")
        if not 0 <= number(self.ml_exit_probability) < number(self.ml_entry_probability) <= 1:
            raise ValueError("Invalid ML thresholds")
        if not 0 <= number(self.ml_expected_net_margin) <= 1:
            raise ValueError("Invalid expected net margin")
        if type(self.ml_horizon_hours) is not int or not 1 <= self.ml_horizon_hours <= 168:
            raise ValueError("Invalid ML horizon")
        if not isinstance(self.ml_model_id, str) or not 1 <= len(self.ml_model_id) <= 256:
            raise ValueError("Explicit model identity or unavailable required")
        if self.fee_profile not in ("baseline", "stress", "custom"):
            raise ValueError("Explicit cost profile required")
        return self

    @property
    def identity(self):
        return dict(asdict(self.validated()), version="paper-comparison-v1",
                    instrument="crypto_spot:KRAKEN:BTC:USD", timeframe="1h",
                    execution="next_observed_hourly_close_proxy", policies="trend20_50-rsi14-ml-net-v1")

    @property
    def sha256(self):
        return hashlib.sha256(canonical(self.identity).encode()).hexdigest()


def _initial_state(config):
    return dict(cash=str(money(number(config.starting_cash))), quantity="0", peak_equity=str(money(number(config.starting_cash))),
                maximum_drawdown="0", fees_paid="0", fills=0, pending=None, entered_at=None, halted=False)


def _connect(path, config):
    path = Path(path)
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise ValueError("Standalone comparison database must be a regular file")
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Refuse unrelated databases instead of adding tables to a live DB.
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"comparison_config", "comparison_accounts", "comparison_steps", "comparison_audits"}
        if tables - expected:
            raise ValueError("Refusing a non-comparison database")
        connection.execute("CREATE TABLE IF NOT EXISTS comparison_config (id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL, payload TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS comparison_accounts (strategy TEXT PRIMARY KEY, state TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS comparison_steps (bar_close TEXT PRIMARY KEY, observed_at TEXT NOT NULL, data_sha256 TEXT NOT NULL, report TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS comparison_audits (id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL)")
        existing = connection.execute("SELECT fingerprint,payload FROM comparison_config WHERE id=1").fetchone()
        if existing and (existing[0] != config.sha256 or existing[1] != canonical(config.identity)):
            raise ValueError("Comparison policy/model/cost configuration is frozen; use a new database")
        if not existing:
            connection.execute("INSERT INTO comparison_config VALUES (1,?,?)", (config.sha256, canonical(config.identity)))
            for strategy in STRATEGIES:
                state = _initial_state(config)
                connection.execute("INSERT INTO comparison_accounts VALUES (?,?)", (strategy, canonical(state)))
        accounts = connection.execute("SELECT strategy,state FROM comparison_accounts").fetchall()
        if {row[0] for row in accounts} != set(STRATEGIES):
            raise ValueError("Comparison strategy accounts are incomplete")
        latest = connection.execute("SELECT report FROM comparison_steps ORDER BY bar_close DESC LIMIT 1").fetchone()
        previous = {row["strategy"]:row["state"] for row in json.loads(latest[0])["accounts"]} if latest else {name:_initial_state(config) for name in STRATEGIES}
        if set(previous) != set(STRATEGIES):
            raise ValueError("Incomplete comparison history")
        for row in accounts:
            state = json.loads(row[1])
            if state != previous[row[0]]:
                raise ValueError("Account state differs from committed comparison ledger")
            if any(number(state[key]) < 0 for key in ("cash","quantity","peak_equity","fees_paid","maximum_drawdown")):
                raise ValueError("Invalid negative comparison account")
            if type(state["fills"]) is not int or state["fills"] < 0 or type(state["halted"]) is not bool:
                raise ValueError("Invalid comparison account state")
        return connection
    except BaseException:
        connection.rollback()
        connection.close()
        raise


def _signal(strategy, state, closes, config, probability, expected_net, bar_close):
    held = number(state["quantity"]) > 0
    if state["halted"]:
        return ("sell" if held else "hold"), "drawdown_halt"
    if strategy == "cash":
        return "hold", "cash_benchmark"
    if strategy == "buy_hold":
        return ("hold" if held else "buy"), "fixed_budget_buy_hold_benchmark"
    if strategy == "cost_ml":
        if held and state["entered_at"] and bar_close+timedelta(hours=1)-datetime.fromisoformat(state["entered_at"]) >= timedelta(hours=config.ml_horizon_hours):
            return "sell", "model_horizon_exit"
        if probability is None or expected_net is None or config.ml_model_id == "unavailable":
            return "hold", "model_unavailable"
        if held:
            return ("sell", "model_exit_probability") if probability <= number(config.ml_exit_probability) else ("hold", "model_holding")
        if probability >= number(config.ml_entry_probability) and expected_net > number(config.ml_expected_net_margin):
            return "buy", "model_probability_and_expected_net_pass"
        return "hold", "model_entry_gate_not_met"
    if strategy == "trend":
        trend = sum(closes[-20:])/20 > sum(closes[-50:])/50 and closes[-1] > closes[-21]
        return ("buy", "positive_20_50_trend") if trend and not held else (("sell", "trend_exit") if held and not trend else ("hold", "trend_unchanged"))
    changes = [b-a for a,b in zip(closes[-15:-1],closes[-14:])]
    gain, loss = sum(max(v,0) for v in changes)/14, sum(max(-v,0) for v in changes)/14
    rsi = Decimal(100) if loss == 0 and gain else (Decimal(50) if loss == 0 else 100-100/(1+gain/loss))
    return ("buy", "rsi_below_30") if rsi < 30 and not held else (("sell", "rsi_above_55") if rsi > 55 and held else ("hold", "rsi_no_transition"))


def advance_comparison(path, candles, *, observed_at, ml_probability=None, ml_expected_net=None,
                       config=ComparisonConfig()):
    """Record one forward step; input model values must be externally verified.

    The clock is explicit for deterministic tests. Production callers must pass
    wall time, never historic timestamps. All results remain nonqualifying.
    """
    clock = utc_timestamp(observed_at)
    config.validated()
    rows = list(candles)
    connection = _connect(path, config)
    try:
        try:
            _check(rows, clock, 51)
            if any(r.instrument_id != "crypto_spot:KRAKEN:BTC:USD" or r.timeframe != "1h" for r in rows):
                raise ValueError("identity")
            bar = datetime.fromisoformat(rows[-1].opened_at)+timedelta(hours=1)
            if not 0 <= (clock-bar).total_seconds() <= 300:
                raise ValueError("late")
            probability = number(ml_probability) if ml_probability is not None else None
            expected_net = number(ml_expected_net) if ml_expected_net is not None else None
            if probability is not None and not 0 <= probability <= 1:
                raise ValueError("probability")
        except (ValueError, TypeError, ArithmeticError):
            connection.execute("INSERT INTO comparison_audits(observed_at,status,reason) VALUES (?,?,?)", (clock.isoformat(),"blocked","invalid_or_late_observation"))
            connection.commit()
            return dict(status="blocked", reason="invalid_or_late_observation", eligible_for_qualification=False)
        stamp = bar.isoformat()
        digest = hashlib.sha256(canonical([r.content_sha256 for r in rows]).encode()).hexdigest()
        existing = connection.execute("SELECT report,data_sha256 FROM comparison_steps WHERE bar_close=?",(stamp,)).fetchone()
        if existing:
            if existing[1] != digest:
                raise ValueError("Observed comparison history cannot be revised")
            connection.commit()
            return json.loads(existing[0])
        previous = connection.execute("SELECT bar_close,observed_at FROM comparison_steps ORDER BY bar_close DESC LIMIT 1").fetchone()
        if previous and (bar <= datetime.fromisoformat(previous[0]) or clock <= datetime.fromisoformat(previous[1])):
            raise ValueError("Retroactive observations cannot advance paper comparison")
        consecutive = previous is not None and bar-datetime.fromisoformat(previous[0]) == timedelta(hours=1)
        price = number(rows[-1].close)
        closes = [number(r.close) for r in rows]
        fee, slip = number(config.fee_rate), number(config.slippage_rate)
        results = []
        for stored in connection.execute("SELECT strategy,state FROM comparison_accounts ORDER BY strategy").fetchall():
            strategy, state = stored[0], json.loads(stored[1])
            cash, quantity = number(state["cash"]), number(state["quantity"])
            fill = None
            pending = state["pending"]
            if pending and consecutive:
                # Missing/invalid current model evidence cancels queued ML entry.
                available = strategy != "cost_ml" or pending["side"] != "buy" or (probability is not None and expected_net is not None and config.ml_model_id != "unavailable")
                if available and pending["side"] == "buy" and quantity == 0 and not state["halted"]:
                    fill_price = price*(1+slip)
                    qty = (min(number(config.order_budget),cash)/(fill_price*(1+fee))).quantize(UNIT,rounding=ROUND_DOWN)
                    notional = money(qty*fill_price)
                    cost = money(notional*fee)
                    if qty > 0 and notional+cost <= cash:
                        cash -= notional+cost
                        quantity += qty
                        state["entered_at"] = stamp
                        fill = dict(side="buy",quantity=str(qty),price=str(fill_price),fee=str(cost),decision_bar_close=pending["bar_close"])
                elif available and pending["side"] == "sell" and quantity > 0:
                    fill_price = price*(1-slip)
                    notional = money(quantity*fill_price)
                    cost = money(notional*fee)
                    cash += notional-cost
                    fill = dict(side="sell",quantity=str(quantity),price=str(fill_price),fee=str(cost),decision_bar_close=pending["bar_close"])
                    quantity = Decimal(0)
                    state["entered_at"] = None
            state["pending"] = None
            state.update(cash=str(cash),quantity=str(quantity))
            if fill:
                state["fees_paid"] = str(number(state["fees_paid"])+number(fill["fee"]))
                state["fills"] += 1
            equity = money(cash+quantity*price)
            peak = max(number(state["peak_equity"]),equity)
            drawdown = (peak-equity)/peak
            state["peak_equity"] = str(peak)
            state["maximum_drawdown"] = str(max(number(state["maximum_drawdown"]),drawdown))
            state["halted"] = state["halted"] or drawdown >= number(config.maximum_drawdown)
            action, reason = _signal(strategy,state,closes,config,probability,expected_net,bar)
            if action in ("buy","sell"):
                state["pending"] = dict(side=action,bar_close=stamp)
            connection.execute("UPDATE comparison_accounts SET state=? WHERE strategy=?",(canonical(state),strategy))
            results.append(dict(strategy=strategy,action=action,reason=reason,fill=fill,state=state,equity=str(equity),
                pending_cancelled_for_gap=bool(pending and not consecutive),
                ml_probability=str(probability) if strategy=="cost_ml" and probability is not None else None,
                ml_expected_net=str(expected_net) if strategy=="cost_ml" and expected_net is not None else None))
        report = dict(status="observed",bar_close=stamp,observed_at=clock.isoformat(),config_sha256=config.sha256,
            ml_model_id=config.ml_model_id,data_sha256=digest,accounts=results,eligible_for_qualification=False,
            execution="next_observed_hourly_close_proxy",profit_is_simulated=True)
        connection.execute("INSERT INTO comparison_steps VALUES (?,?,?,?)",(stamp,clock.isoformat(),digest,canonical(report)))
        connection.commit()
        return report
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def comparison_report(path):
    """Read the latest committed report without creating or mutating files."""
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError("An existing comparison database is required")
    connection = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
    try:
        config = connection.execute("SELECT fingerprint,payload FROM comparison_config WHERE id=1").fetchone()
        row = connection.execute("SELECT report FROM comparison_steps ORDER BY bar_close DESC LIMIT 1").fetchone()
        return dict(config_sha256=config[0], config=json.loads(config[1]),
                    latest=json.loads(row[0]) if row else None, eligible_for_qualification=False)
    finally:
        connection.close()
