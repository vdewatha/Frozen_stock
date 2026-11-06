"""Same-day re-entries get a fresh order key instead of a duplicate-key refusal.

On 2026-11-05 DIA, GOOGL and TSLA BUYs were refused on every cycle all
afternoon ("Idempotency key was already used for a different order"): an
earlier entry that day had ended, and every retry reused its key at a new
price.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.stock_paper import StockPaperAccount
from app.services.stock_paper_autotrader import EXPLORATION_SOURCE, EntryRetryBlocked, _buy_key
from tests.test_loss_feedback_loop import _strategy, db  # noqa: F401  (fixture)
from tests.test_paper_trade_scorecard import Ledger

UTC = timezone.utc
NOW = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)  # 13:00 ET
BASE = "explore-paper:2026-11-06:DIA:model_predictive_long:buy"


@pytest.fixture()
def book(db):  # noqa: F811
    account = StockPaperAccount(broker="alpaca_paper", broker_account_id="paper", cash=Decimal("9000"),
                                buying_power=Decimal("9000"), equity=Decimal("10000"), status="reconciled",
                                raw_payload={})
    db.add(account)
    db.flush()
    strategy = _strategy(db, "model_predictive_long")
    ledger = Ledger(db, account)
    ledger.counter = 100
    return db, account, strategy, ledger


def _entry(ledger, strategy, status, at, symbol="DIA"):
    row = ledger.order(symbol=symbol, side="buy", quantity=1, strategy=strategy, source=EXPLORATION_SOURCE,
                       created=at)
    row.status = status
    row.updated_at = at
    ledger.db.flush()
    return row


def _key(db, account, strategy, now=NOW):
    return _buy_key(db, account.id, "DIA", BASE, {strategy.id}, now)


def test_the_first_entry_of_the_day_keeps_the_base_key(book):
    db, account, strategy, _ = book
    assert _key(db, account, strategy) == BASE


def test_an_ended_entry_advances_the_key(book):
    db, account, strategy, ledger = book
    _entry(ledger, strategy, "filled", NOW - timedelta(hours=2))
    assert _key(db, account, strategy) == f"{BASE}:retry1"
    _entry(ledger, strategy, "cancelled", NOW - timedelta(hours=1))
    assert _key(db, account, strategy) == f"{BASE}:retry2"


def test_an_entry_in_flight_keeps_its_key_so_a_retry_finds_it(book):
    db, account, strategy, ledger = book
    _entry(ledger, strategy, "filled", NOW - timedelta(hours=2))
    _entry(ledger, strategy, "reserved", NOW - timedelta(minutes=1))
    assert _key(db, account, strategy) == f"{BASE}:retry1"
    _entry(ledger, strategy, "uncertain_submission", NOW)  # may be live at the broker
    assert _key(db, account, strategy) == f"{BASE}:retry1"


def test_yesterdays_entries_and_other_symbols_do_not_count(book):
    db, account, strategy, ledger = book
    _entry(ledger, strategy, "filled", NOW - timedelta(days=1))
    _entry(ledger, strategy, "filled", NOW - timedelta(hours=1), symbol="SPY")
    assert _key(db, account, strategy) == BASE


def test_re_entries_are_capped_per_session(book, monkeypatch):
    db, account, strategy, ledger = book
    monkeypatch.setattr(settings, "paper_max_entries_per_symbol_per_day", 2)
    _entry(ledger, strategy, "filled", NOW - timedelta(hours=3))
    _entry(ledger, strategy, "filled", NOW - timedelta(hours=2))
    with pytest.raises(EntryRetryBlocked, match="2 entries in DIA already ended"):
        _key(db, account, strategy)


def test_a_broker_reject_waits_before_retrying(book):
    db, account, strategy, ledger = book
    _entry(ledger, strategy, "rejected", NOW - timedelta(minutes=10))
    with pytest.raises(EntryRetryBlocked, match="rejected the last entry in DIA at 12:50 ET"):
        _key(db, account, strategy)
    assert _key(db, account, strategy, now=NOW + timedelta(minutes=25)) == f"{BASE}:retry1"

