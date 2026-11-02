"""Autonomous paper workflow tests: never contact a real broker."""
from datetime import timedelta
from contextlib import nullcontext
from decimal import Decimal
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import subprocess
from threading import Event

import numpy as np
import pandas as pd
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine

from test_paper_short_pilot import ACCOUNT as ACCOUNT_ID, setup, Broker, NOW, authorization, order
from app.models import IntradayBar, RiskRule, StockPaperRecoveryState
from app.models.paper_short_pilot import PaperShortPilot
from app.services import paper_short_automation as auto, paper_short_pilot as pilot
from app.services.exchange_sessions import is_nyse_session

OPEN = NOW.replace(hour=14, minute=40)
REAL_DAILY_SIGNAL = auto.daily_signal


def signal(enter=True, cover=False):
    return {"enter": enter, "cover": cover, "rsi2": 95, "sma200": 150,
            "daily_session": "2026-10-30", "dataset_sha256": "a" * 64}


@pytest.fixture
def automated(setup, monkeypatch):
    db, broker = setup
    monkeypatch.setattr(pilot.settings, "dev_paper_shorts_automation_enabled", True)
    pilot.authorize_pilot(db, authorization(symbols=["AAPL"], automated=True,
        expires_at=OPEN + timedelta(days=30)), actor="admin:test", gateway=broker, now=OPEN)
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: signal())
    return db, broker


def bar(db, now, symbol="AAPL", price=100, high=None):
    opened = now - timedelta(minutes=2)
    row = db.query(IntradayBar).filter_by(symbol=symbol, opened_at=opened, provider=auto.MARKET_DATA_PROVIDER).first()
    if row is None:
        row = IntradayBar(symbol=symbol, timeframe="1m", opened_at=opened, open=price,
            high=high or price + .1, low=price - .1, close=price, volume=100,
            provider=auto.MARKET_DATA_PROVIDER, feed_class=auto.MARKET_DATA_FEED_CLASS,
            exchange_timestamp=opened)
        db.add(row)
    db.commit()
    return row


def cycle(db, broker, now=OPEN, **kwargs):
    broker.raw_clock["timestamp"] = now.isoformat()
    bar(db, now)
    return auto.run_short_automation_cycle(db, gateway=broker, now=now, refresh=False, **kwargs)


def test_automatic_entry_fill_cover_no_operator_calls(automated, monkeypatch):
    db, broker = automated
    first = cycle(db, broker)
    assert first["action"] == "open_short", first
    assert first["order"]["quantity"] == "2.000000000" or Decimal(first["order"]["quantity"]) == 2
    assert len(broker.posts) == 1
    broker.fill(first["order"]["client_order_id"])
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: signal(cover=True))
    covered = cycle(db, broker, OPEN + timedelta(minutes=1))
    assert covered["action"] == "cover_short", covered
    assert covered["reason"] == "rsi_cover"
    assert len(broker.posts) == 2
    broker.fill(covered["order"]["client_order_id"], price="90")
    again = cycle(db, broker, OPEN + timedelta(minutes=5))
    assert any(d["action"] == "daily_entry_limit" for d in again["decisions"])
    assert len(broker.posts) == 2
    status = pilot.pilot_status(db)
    assert status["autonomous"] and status["automation_last_cycle"]["checked_at"]


def test_stale_order_is_canceled_automatically(automated):
    db, broker = automated
    first = cycle(db, broker)
    waiting = cycle(db, broker, OPEN + timedelta(minutes=1))
    assert waiting["action"] == "wait_for_fill"
    canceled = cycle(db, broker, OPEN + timedelta(minutes=2))
    assert canceled["action"] == "cancel_pending"
    assert len(broker.posts) == 1 and len(broker.cancels) == 1
    # A cancel request itself never frees the account-wide reservation.
    assert pilot.pilot_status(db)["orders"][0]["unresolved"]
    broker.book[first["order"]["client_order_id"]]["status"] = "canceled"
    retried = cycle(db, broker, OPEN + timedelta(minutes=4))
    assert retried["action"] == "open_short"
    assert len(broker.posts) == 2


def test_partial_entry_is_automatically_canceled_then_bounded_cover(automated, monkeypatch):
    db, broker = automated
    first = cycle(db, broker)
    client_id = first["order"]["client_order_id"]
    broker.fill(client_id, qty="1", status="partially_filled")
    assert cycle(db, broker, OPEN + timedelta(minutes=2))["action"] == "cancel_pending"
    assert len(broker.posts) == 1
    broker.book[client_id]["status"] = "canceled"
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: signal(cover=True))
    result = cycle(db, broker, OPEN + timedelta(minutes=3))
    assert result["action"] == "cover_short", result
    assert broker.posts[-1]["qty"] == "1" and broker.posts[-1]["side"] == "buy"


@pytest.mark.parametrize("gate", ["kill", "pause", "expiry", "entry_flag"])
def test_automatic_exits_survive_entry_blocks(automated, monkeypatch, gate):
    db, broker = automated
    first = cycle(db, broker)
    broker.fill(first["order"]["client_order_id"])
    if gate == "kill":
        db.query(RiskRule).one().value = {"kill_switch_enabled": True}
    elif gate == "pause":
        db.get(PaperShortPilot, 1).status = "paused"
    elif gate == "expiry":
        db.get(PaperShortPilot, 1).expires_at = OPEN
    else:
        monkeypatch.setattr(pilot.settings, "dev_paper_shorts_enabled", False)
    db.commit()
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: signal(cover=True))
    covered = cycle(db, broker, OPEN + timedelta(minutes=1))
    assert covered["action"] == "cover_short", covered
    assert len(broker.posts) == 2


def test_stop_exit_does_not_require_daily_history(automated, monkeypatch):
    db, broker = automated
    first = cycle(db, broker)
    broker.fill(first["order"]["client_order_id"])
    later = OPEN + timedelta(minutes=2)
    bar(db, later, price=110)
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: (_ for _ in ()).throw(ValueError("daily unavailable")))
    result = cycle(db, broker, later)
    assert result["action"] == "cover_short" and result["reason"] == "stop_loss", result


def test_time_stop_is_session_based_and_automatic(automated, monkeypatch):
    db, broker = automated
    first = cycle(db, broker)
    broker.fill(first["order"]["client_order_id"])
    later = OPEN + timedelta(days=7)
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: (_ for _ in ()).throw(ValueError("daily unavailable")))
    result = cycle(db, broker, later)
    assert result["action"] == "cover_short" and result["reason"] == "time_stop", result


def test_uncertain_submission_is_never_blindly_retried(automated):
    db, broker = automated
    broker.failure = TimeoutError()
    first = cycle(db, broker)
    assert len(broker.posts) == 1
    for minutes in (1, 2, 5, 10):
        result = cycle(db, broker, OPEN + timedelta(minutes=minutes))
        assert result["status"] == "blocked"
    assert len(broker.posts) == 1


@pytest.mark.parametrize("gate", ["kill", "recovery", "stale_feed", "history", "identity", "outside_market_session"])
def test_automation_never_overrides_readiness(automated, monkeypatch, gate):
    db, broker = automated
    now = OPEN
    if gate == "kill":
        db.query(RiskRule).one().value = {"kill_switch_enabled": True}
    elif gate == "recovery":
        db.add(StockPaperRecoveryState(id=1, status="paused", flatten_policy="none", updated_by="test"))
    elif gate == "stale_feed":
        monkeypatch.setattr(pilot, "feed_status", lambda *a, **kw: {"status": "stale"})
    elif gate == "history":
        monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: (_ for _ in ()).throw(ValueError("incomplete history")))
    elif gate == "identity":
        broker.raw_account["id"] = "other-account"
    else:
        now = OPEN.replace(hour=21, minute=0)
    db.commit()
    cycle(db, broker, now)
    assert not broker.posts


def test_unfilled_entry_canceled_immediately_after_pause(automated):
    db, broker = automated
    cycle(db, broker)
    pilot.pause_pilot(db, actor="admin:test", reason="Emergency pause of paper entries")
    result = cycle(db, broker, OPEN + timedelta(seconds=30))
    assert result["action"] == "cancel_pending"
    assert len(broker.cancels) == 1


def test_manual_grant_is_never_auto_promoted(setup):
    db, broker = setup
    pilot.authorize_pilot(db, authorization(), actor="admin:test", gateway=broker, now=NOW)
    assert cycle(db, broker)["status"] == "disabled"
    assert not broker.posts


def test_manual_entry_cannot_override_automated_mode(automated):
    db, broker = automated
    broker.raw_clock["timestamp"] = OPEN.isoformat()
    with pytest.raises(pilot.StockPaperError, match="reserved for the scheduled strategy"):
        pilot.submit_pilot_order(db, order(), actor="operator:test", gateway=broker, now=OPEN)
    assert not broker.posts


def test_three_zero_fill_entry_attempts_stop_retry_storm(automated):
    db, broker = automated
    broker.failure = pilot.StockPaperUnavailable("rejected", status_code=422)
    for minute in (0, 4, 8, 12, 16):
        cycle(db, broker, OPEN + timedelta(minutes=minute))
    assert len(broker.posts) == 3


def test_below_whole_share_budget_is_automatic_no_trade(automated):
    db, broker = automated
    db.get(PaperShortPilot, 1).max_order_notional = Decimal("50")
    db.commit()
    result = cycle(db, broker)
    assert result["decisions"][0]["action"] == "below_whole_share_budget"
    assert not broker.posts


def test_automatic_backfill_pins_provider_and_does_not_fabricate_data(automated, monkeypatch):
    db, _ = automated
    calls = []
    monkeypatch.setattr(auto, "collect_scheduled_intraday", lambda *a, **kw: calls.append(("intraday", kw)))
    monkeypatch.setattr(auto, "get_price_history", lambda *a, **kw: (pd.DataFrame(), auto.SOURCE))
    monkeypatch.setattr(auto, "import_market_prices", lambda *a, **kw: calls.append(("daily", kw)))
    auto.refresh_inputs(db, db.get(PaperShortPilot, 1), OPEN)
    assert calls == [("intraday", {"symbols": ["AAPL"], "now": OPEN}),
                     ("daily", {"period": "2y", "provider": "alpaca_iex_daily"})]


def test_refresh_failure_cannot_suppress_stop_exit(automated, monkeypatch):
    db, broker = automated
    first = cycle(db, broker)
    broker.fill(first["order"]["client_order_id"])
    later = OPEN + timedelta(minutes=2)
    bar(db, later, price=110)
    broker.raw_clock["timestamp"] = later.isoformat()
    monkeypatch.setattr(auto, "refresh_inputs", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("provider down")))
    monkeypatch.setattr(auto, "refresh_daily_inputs", lambda *a, **kw: pytest.fail("Daily backfill delayed an urgent exit"))
    result = auto.run_short_automation_cycle(db, gateway=broker, now=later, refresh=True)
    assert result["action"] == "cover_short" and result["reason"] == "stop_loss"


def daily_frame():
    dates = [d for d in pd.date_range("2025-01-02", "2026-10-30") if is_nyse_session(d.date())][-260:]
    prices = np.linspace(200, 100, len(dates))
    prices[-3:] = [100, 108, 116]
    return pd.DataFrame({"date": dates, "open": prices, "high": prices + .1, "low": prices - .1, "close": prices})


def test_real_indicator_path_only_uses_previous_completed_session(setup, monkeypatch):
    db, _ = setup
    frame = daily_frame()
    monkeypatch.setattr(auto, "get_price_history", lambda *a, **kw: (frame, auto.SOURCE))
    result = auto.daily_signal(db, "AAPL", OPEN)
    assert result["enter"] and not result["cover"]
    assert result["daily_session"] == "2026-10-30"
    current = frame.iloc[-1:].copy()
    current["date"] = pd.Timestamp("2026-11-02")
    current[["open", "high", "low", "close"]] = [1000, 1001, 999, 1000]
    monkeypatch.setattr(auto, "get_price_history", lambda *a, **kw: (pd.concat([frame, current]), auto.SOURCE))
    assert auto.daily_signal(db, "AAPL", OPEN) == result


def test_real_daily_indicator_drives_automatic_broker_intent(automated, monkeypatch):
    db, broker = automated
    monkeypatch.setattr(auto, "daily_signal", REAL_DAILY_SIGNAL)
    monkeypatch.setattr(auto, "get_price_history", lambda *a, **kw: (daily_frame(), auto.SOURCE))
    result = cycle(db, broker)
    assert result["action"] == "open_short", result
    assert result["evidence"]["rsi2"] > 90
    assert len(result["evidence"]["dataset_sha256"]) == 64
    assert len(broker.posts) == 1 and broker.posts[0]["position_intent"] == "sell_to_open"


def test_database_provenance_label_works_without_mocked_history(setup):
    from app.services.market_data import upsert_prices, get_price_history
    db, _ = setup
    frame = daily_frame().assign(adjusted_close=lambda x: x.close, volume=1000)
    upsert_prices(db, "AAPL", frame, auto.SOURCE)
    _, provenance = get_price_history(db, "AAPL", limit=260, auto_seed=False, source_filter=auto.SOURCE)
    assert provenance == "database:alpaca_iex_daily"
    result = auto.daily_signal(db, "AAPL", OPEN)
    assert result["enter"] is True and result["daily_session"] == "2026-10-30"


def test_after_old_cutoff_eligible_signal_can_open_short(automated):
    db, broker = automated
    result = cycle(db, broker, OPEN + timedelta(hours=1))
    assert result["action"] == "open_short", result
    assert result["evidence"]["rsi2"] == 95
    assert result["entry_policy"] == "regular_session"
    assert result["strategy"] == "rsi2_short_bounce_full_session_v2"
    assert len(broker.posts) == 1


def test_missing_signal_after_old_cutoff_cannot_open_short(automated, monkeypatch):
    db, broker = automated
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: (_ for _ in ()).throw(ValueError("missing history")))
    result = cycle(db, broker, OPEN + timedelta(hours=1))
    assert result["decisions"][0]["action"] == "entry_blocked"
    assert not broker.posts


@pytest.mark.parametrize("stamp,allowed", [
    ("2026-11-02T14:29:59+00:00", False),
    ("2026-11-02T14:30:00+00:00", True),
    ("2026-11-02T15:16:00+00:00", True),
    ("2026-11-02T18:00:00+00:00", True),
    ("2026-11-02T20:59:59+00:00", True),
    ("2026-11-02T21:00:00+00:00", False),
    ("2026-11-02T21:30:00+00:00", False),
    ("2026-11-07T18:00:00+00:00", False),
    ("2026-11-27T17:59:59+00:00", True),
    ("2026-11-27T18:00:00+00:00", False),
])
def test_full_regular_session_boundaries_and_exchange_early_close(automated, stamp, allowed):
    from datetime import datetime
    db, broker = automated
    now = datetime.fromisoformat(stamp)
    # The standing policy already exists before each tested market boundary.
    db.get(PaperShortPilot, 1).authorized_at = OPEN.replace(hour=14, minute=0)
    db.commit()
    result = cycle(db, broker, now)
    assert bool(broker.posts) is allowed, result
    assert result["action"] == ("open_short" if allowed else "market_closed")


def test_no_signal_at_midday_remains_flat(automated, monkeypatch):
    db, broker = automated
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: signal(enter=False))
    result = cycle(db, broker, OPEN.replace(hour=18, minute=0))
    assert result["action"] == "hold"
    assert result["decisions"][0]["action"] == "hold_flat"
    assert result["decisions"][0]["execution_feed"] == "ready"
    assert not broker.posts


def test_preflight_crossing_regular_close_never_posts(automated, monkeypatch):
    db, broker = automated
    ticks = iter([10, 12])
    monkeypatch.setattr(pilot, "monotonic", lambda: next(ticks))
    result = cycle(db, broker, OPEN.replace(hour=20, minute=59, second=59))
    assert not broker.posts
    assert "ended during paper preflight" in result["decisions"][0]["reason"]


def test_afternoon_eligible_signal_still_respects_kill_switch(automated):
    db, broker = automated
    db.query(RiskRule).one().value = {"kill_switch_enabled": True}
    db.commit()
    result = cycle(db, broker, OPEN.replace(hour=18, minute=0))
    assert not broker.posts
    assert "kill switch" in result["decisions"][0]["reason"]


def test_cycle_that_crosses_close_during_refresh_cannot_open_entry(automated, monkeypatch):
    from datetime import datetime
    db, broker = automated
    clocks = iter([OPEN.replace(hour=20, minute=59, second=50),
                   OPEN.replace(hour=21, minute=0), OPEN.replace(hour=21, minute=0, second=2)])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(clocks)

    monkeypatch.setattr(auto, "datetime", Clock)
    monkeypatch.setattr(auto, "refresh_inputs", lambda *a, **kw: None)
    monkeypatch.setattr(auto, "refresh_daily_inputs", lambda *a, **kw: None)
    result = auto.run_short_automation_cycle(db, gateway=broker)
    assert result["action"] == "no_new_entries" and result["reason"] == "market_closed", result
    assert not broker.posts


def test_worker_module_launch_does_not_depend_on_pythonpath():
    root = Path(__file__).parents[1]
    result = subprocess.run([__import__("sys").executable, "-m", "scripts.run_paper_short_automation"],
        cwd=root, capture_output=True, text=True, timeout=15,
        env={k: v for k, v in {**os.environ, "DEV_PAPER_SHORTS_AUTOMATION_ENABLED": "false"}.items() if k != "PYTHONPATH"})
    assert result.returncode == 0, result.stderr
    assert "disabled by deployment policy" in result.stderr


@pytest.mark.parametrize("bad", ["stale", "gap", "future", "synthetic", "duplicate"])
def test_strategy_rejects_untrustworthy_history(setup, monkeypatch, bad):
    db, _ = setup
    frame, source = daily_frame(), auto.SOURCE
    if bad == "stale":
        frame = frame.iloc[:-1]
    elif bad == "gap":
        frame = frame.drop(index=100)
    elif bad == "future":
        frame.loc[259, "date"] = pd.Timestamp("2026-11-03")
    elif bad == "synthetic":
        source = "synthetic_fallback"
    else:
        frame.loc[258, "date"] = frame.loc[259, "date"]
    monkeypatch.setattr(auto, "get_price_history", lambda *a, **kw: (frame, source))
    with pytest.raises((ValueError, pilot.StockPaperError)):
        auto.daily_signal(db, "AAPL", OPEN)


def runner():
    path = Path(__file__).parents[1] / "scripts/run_paper_short_automation.py"
    spec = importlib.util.spec_from_file_location("short_runner_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_holds_singleton_leadership_and_releases_on_exit(tmp_path):
    module = runner()
    engine = create_engine(f"sqlite:///{tmp_path / 'pilot.db'}")
    try:
        with module.leader(engine) as check:
            check()
            with pytest.raises(BlockingIOError):
                with module.leader(engine):
                    pytest.fail("Duplicate runner acquired leadership")
        with module.leader(engine):
            pass
    finally:
        engine.dispose()


def test_runner_retries_transient_failure_without_user_action():
    module = runner()
    stop = Event()
    calls = []

    def once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary outage")
        stop.set()
        return {"status": "observed", "action": "hold"}

    module.run_loop(stop, lambda: None, once=once, interval=0)
    assert len(calls) == 2


def test_runner_never_ignores_lost_leadership():
    module = runner()
    with pytest.raises(RuntimeError, match="lost"):
        module.run_loop(Event(), lambda: (_ for _ in ()).throw(RuntimeError("lost")),
                        once=lambda: pytest.fail("Ran without leadership"), interval=0)


def test_launcher_rejects_conflicting_workers_before_starting_any_process():
    script = Path(__file__).parents[1] / "scripts/run_local_stack.sh"
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=5,
        env={**os.environ, "ENVIRONMENT": "local", "PAPER_WORKERS_ENABLED": "true",
             "DEV_PAPER_SHORTS_ENABLED": "true", "DEV_PAPER_SHORTS_AUTOMATION_ENABLED": "true"})
    assert result.returncode == 1
    assert "shared workers disabled" in result.stderr
    assert result.stdout == ""


def test_runner_restarts_in_cover_only_mode_when_entry_flag_is_off(setup, monkeypatch):
    module = runner()
    monkeypatch.setattr(module.settings, "dev_paper_shorts_automation_enabled", True)
    monkeypatch.setattr(module.settings, "dev_paper_shorts_enabled", False)
    monkeypatch.setattr(module, "assert_schema_current", lambda *a: None)
    monkeypatch.setattr(module, "leader", lambda *a: nullcontext(lambda: None))
    monkeypatch.setattr(module.signal, "signal", lambda *a: None)
    calls = []
    monkeypatch.setattr(module, "run_loop", lambda *a: calls.append("started"))
    module.main()
    assert calls == ["started"]


def test_automated_authority_cannot_extend_beyond_thirty_days(setup):
    db, broker = setup
    with pytest.raises(pilot.StockPaperError, match="validity window"):
        pilot.authorize_pilot(db, authorization(automated=True, expires_at=NOW + timedelta(days=31)),
                              actor="admin:test", gateway=broker, now=NOW)
    assert not broker.posts


@pytest.fixture
def hosted(setup, monkeypatch):
    """Production-like settings: shared workers on, shorts on their own account."""
    db, broker = setup
    for name, value in {
        "environment": "production", "paper_binding_source": "shared", "paper_workers_enabled": True,
        "paper_broker_account_id": "long-lane-account", "dev_paper_broker_account_id": SecretStr(""),
        "short_paper_alpaca_api_key": SecretStr("key"), "short_paper_alpaca_api_secret": SecretStr("secret"),
        "short_paper_broker_account_id": SecretStr(ACCOUNT_ID),
        "dev_paper_shorts_automation_enabled": True, "short_pilot_auto_authorize": True,
        "short_pilot_symbols": ["AAPL"], "short_pilot_max_order_notional": Decimal("250"),
        "short_pilot_max_gross_notional": Decimal("1000"),
    }.items():
        monkeypatch.setattr(pilot.settings, name, value)
    monkeypatch.setattr(auto, "daily_signal", lambda *a, **kw: signal())
    return db, broker


def test_hosted_binding_uses_its_own_account_beside_long_workers(hosted, monkeypatch):
    db, broker = hosted
    assert pilot._binding(broker) == sha256(ACCOUNT_ID.encode()).hexdigest()
    assert pilot.ShortPilotGateway()._headers()["APCA-API-KEY-ID"] == "key"
    monkeypatch.setattr(pilot.settings, "paper_broker_account_id", ACCOUNT_ID)
    with pytest.raises(pilot.StockPaperError, match="its own dedicated paper account"):
        pilot._binding(broker)
    monkeypatch.setattr(pilot.settings, "short_paper_broker_account_id", SecretStr(""))
    with pytest.raises(pilot.StockPaperError, match="exclusive local development"):
        pilot._binding(broker)


def test_hosted_worker_grants_its_own_policy_and_trades_without_clicks(hosted):
    db, broker = hosted
    result = cycle(db, broker)
    grant = db.get(PaperShortPilot, 1)
    assert grant.automated and grant.authorized_by == auto.POLICY_ACTOR and grant.symbols == ["AAPL"]
    assert result["action"] == "open_short" and broker.posts[0]["position_intent"] == "sell_to_open"


def test_standing_policy_renews_only_its_own_active_flat_grant(hosted):
    db, broker = hosted
    auto.ensure_standing_policy(db, broker, OPEN)
    first = pilot._utc(db.get(PaperShortPilot, 1).expires_at)
    later = OPEN + timedelta(days=auto.POLICY_DAYS) - timedelta(days=1)
    auto.ensure_standing_policy(db, broker, later)
    assert pilot._utc(db.get(PaperShortPilot, 1).expires_at) > first
    pilot.pause_pilot(db, actor="admin:test", reason="Operator pause of paper short entries")
    auto.ensure_standing_policy(db, broker, later + timedelta(days=28))
    assert db.get(PaperShortPilot, 1).status == "paused"


def test_standing_policy_never_replaces_a_manual_grant(hosted):
    db, broker = hosted
    pilot.authorize_pilot(db, authorization(), actor="admin:test", gateway=broker, now=NOW)
    auto.ensure_standing_policy(db, broker, NOW + timedelta(hours=3))
    grant = db.get(PaperShortPilot, 1)
    assert not grant.automated and grant.authorized_by == "admin:test"


def test_standing_policy_off_by_default(setup, monkeypatch):
    db, broker = setup
    monkeypatch.setattr(pilot.settings, "dev_paper_shorts_automation_enabled", True)
    assert auto.ensure_standing_policy(db, broker, OPEN) is None
