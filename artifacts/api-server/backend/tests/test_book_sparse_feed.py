"""2026-11-10: the passive arm never bought VOO/SGOV.

SGOV prints on IEX only every 20+ minutes, so its feed was "incomplete"
(gaps that long are never verified as no-trade) and the book's 15-minute price
rule found no price: no tranche was ever planned, and the ledger would have
refused the orders anyway. Book orders are collared limits, so a same-session
bar up to an hour old is now accepted for them only.
"""
from datetime import datetime, timedelta, timezone

from app.services import stock_paper_ledger as ledger
from app.services import target_weight_book as twb
from tests.test_target_weight_book import REBALANCE_NOW, _bar, db  # noqa: F401  (fixture)

NOW = datetime(2026, 11, 10, 16, 30, tzinfo=timezone.utc)  # 11:30 ET


def market(latest, status="incomplete", execution_status="stale"):
    return {"status": status, "execution_status": execution_status, "exchange_timestamp": latest}


def test_sparse_feed_within_an_hour_is_acceptable_for_book_orders():
    assert ledger._late_feed_acceptable_for_book_order(market(NOW - timedelta(minutes=40)), NOW)
    assert ledger._late_feed_acceptable_for_book_order(market(NOW - timedelta(minutes=10), status="stale"), NOW)


def test_book_allowance_has_limits():
    assert not ledger._late_feed_acceptable_for_book_order(market(NOW - timedelta(minutes=75)), NOW)
    # Yesterday's last bar is never today's reference.
    assert not ledger._late_feed_acceptable_for_book_order(market(NOW - timedelta(hours=20)), NOW)
    # Outage-class states stay blocked.
    assert not ledger._late_feed_acceptable_for_book_order(
        market(NOW - timedelta(minutes=5), status="unavailable", execution_status="unavailable"), NOW)
    assert not ledger._late_feed_acceptable_for_book_order(market(None), NOW)
    # A bar stamped in the future is not trusted.
    assert not ledger._late_feed_acceptable_for_book_order(market(NOW + timedelta(minutes=5)), NOW)


def test_book_prices_from_a_bar_under_an_hour_old(db):  # noqa: F811
    _bar(db, "SGOV", "100.55", at=REBALANCE_NOW - timedelta(minutes=38))
    assert twb.latest_price(db, "SGOV", now=REBALANCE_NOW) is not None
    _bar(db, "VOO", "600", at=REBALANCE_NOW - timedelta(minutes=75))
    assert twb.latest_price(db, "VOO", now=REBALANCE_NOW) is None


def test_only_the_book_gets_the_allowance():
    source = (ledger.__file__)
    text = open(source).read()
    assert text.count("source == TARGET_WEIGHT_SOURCE and _late_feed_acceptable_for_book_order") == 2
