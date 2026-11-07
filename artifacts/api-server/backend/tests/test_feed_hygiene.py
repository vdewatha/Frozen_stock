"""Feed and benchmark hygiene: SIP references, the delay guard, fill and signal feed tags,
the IEX-vs-SIP touch log, paper auction exclusion and gapped-stop fills."""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.integrations import alpaca_market_data as amd
from app.models import AuditLog, IntradayBar
from app.models.stock_paper import StockPaperAccount, StockPaperFill, StockPaperOrder, StockPaperPosition
from app.services import feed_hygiene as fh
from app.services import gate_cost as gc
from app.services import market_data
from app.services import paper_cost_calibration as pcc
from app.services import trade_attribution as ta
from app.services.triple_barrier import BarrierConfig, gapped_stop_fill, label_events
from app.tasks.celery_app import celery_app
from tests.test_loss_feedback_loop import db  # noqa: F401  (fixture)

UTC = timezone.utc
ET = ZoneInfo("America/New_York")
SESSION = date(2026, 11, 5)                          # Thursday
OPEN = datetime(2026, 11, 5, 14, 30, tzinfo=UTC)     # 09:30 ET
NOW = datetime(2026, 11, 7, 18, 30, tzinfo=UTC)      # Saturday


def _bar(minute: int, open_, high=None, low=None, close=None, vwap=None, start=OPEN):
    close = open_ if close is None else close
    return {"opened_at": start + timedelta(minutes=minute), "open": open_, "high": high or max(open_, close),
            "low": low or min(open_, close), "close": close, "volume": 1000.0, "vwap": vwap}


def _tape(prices, *, start=OPEN, vwap_offset=0.01):
    return [_bar(i, p, vwap=round(p + vwap_offset, 4), start=start) for i, p in enumerate(prices)]


class _FakeClient:
    """Stands in for ``AlpacaBarsClient``; records every request."""

    def __init__(self, tapes: dict, fail: dict | None = None):
        self.tapes, self.fail, self.calls, self.requests = tapes, fail or {}, [], 0

    def bars(self, symbols, start, end, *, feed, timeframe="1Min"):
        self.calls.append((tuple(symbols), start, end, feed))
        self.requests += 1
        if feed in self.fail:
            raise self.fail[feed]
        return {symbol: [bar for bar in self.tapes.get((feed, symbol), []) if start <= bar["opened_at"] < end]
                for symbol in symbols}


# ------------------------------------------------------------ reference choice

def test_entry_at_the_open_takes_the_official_sip_open():
    bars = _tape([100.00, 100.20, 100.40, 100.60])
    ref = fh.select_sip_reference(filled_at=OPEN + timedelta(minutes=2, seconds=10), bars=bars, at_open=True)
    assert ref["kind"] == "official_open" and ref["price"] == 100.00
    assert ref["official_open"]["at_session_open"] is True
    assert ref["minute_bar"]["opened_at"] == (OPEN + timedelta(minutes=2)).isoformat()   # both are kept
    assert ref["feed"] == "sip" and ref["delay_minutes"] == 16 and ref["status"] == "referenced"


def test_intraday_fill_takes_the_minute_bar_containing_it():
    bars = _tape([100.00, 100.20, 100.40, 100.60])
    ref = fh.select_sip_reference(filled_at=OPEN + timedelta(minutes=2, seconds=59), bars=bars, at_open=False)
    assert ref["kind"] == "minute_vwap" and ref["price"] == pytest.approx(100.41)
    no_vwap = [dict(bar, vwap=None) for bar in bars]
    ref = fh.select_sip_reference(filled_at=OPEN + timedelta(minutes=3), bars=no_vwap, at_open=False)
    assert ref["kind"] == "minute_open" and ref["price"] == 100.60
    # A minute without a consolidated print has no reference; the open entry still has the official open.
    gap = fh.select_sip_reference(filled_at=OPEN + timedelta(minutes=30), bars=bars, at_open=False)
    assert gap["kind"] is None and gap["price"] is None and gap["status"] == "no_sip_bar"
    late = [bar for bar in bars if bar["opened_at"] > OPEN]
    opening = fh.official_open(late, SESSION)
    assert opening["price"] == 100.20 and opening["at_session_open"] is False
    # Pre-market bars are never the official open.
    pre = [_bar(-5, 99.0)] + bars
    assert fh.official_open(pre, SESSION)["price"] == 100.00


def test_open_entry_definition():
    filled = OPEN + timedelta(minutes=12)
    assert fh.is_open_entry(side="buy", filled_at=filled)
    assert not fh.is_open_entry(side="buy", filled_at=OPEN + timedelta(minutes=95))
    assert not fh.is_open_entry(side="sell", filled_at=filled)
    stop_leg = SimpleNamespace(leg_role="stop_loss", order_type="stop", time_in_force="gtc")
    assert not fh.is_open_entry(side="buy", filled_at=filled, order=stop_leg)
    opg = SimpleNamespace(leg_role=None, order_type="market", time_in_force="OPG")
    assert fh.is_open_entry(side="buy", filled_at=OPEN + timedelta(minutes=95), order=opg)
    assert not fh.is_open_entry(side="buy", filled_at=datetime(2026, 11, 8, 15, tzinfo=UTC))   # Sunday


# ------------------------------------------------------------------ delay guard

def test_delay_guard_refuses_recent_sip_queries():
    now = datetime(2026, 11, 5, 21, 10, tzinfo=UTC)            # 16:10 ET
    close = datetime(2026, 11, 5, 21, 0, tzinfo=UTC)
    with pytest.raises(amd.DelayedSipTooRecent):
        amd.assert_sip_delay(close, now)
    amd.assert_sip_delay(close, close + timedelta(minutes=16))
    with pytest.raises(ValueError):
        amd.assert_sip_delay(close.replace(tzinfo=None), now)
    assert amd.latest_sip_end(now) == datetime(2026, 11, 5, 20, 54, tzinfo=UTC)
    assert not fh.sip_ready(SESSION, now) and fh.sip_ready(SESSION, close + timedelta(minutes=16))
    assert fh.last_sip_ready_session(now) == date(2026, 11, 4)


def test_client_refuses_sip_inside_the_delay_without_a_request_and_paces_iex():
    urls = []

    def transport(url, headers):
        urls.append((url, headers))
        return {"bars": {"SPY": [{"t": "2026-11-05T14:30:00Z", "o": 1, "h": 1.2, "l": 0.9, "c": 1.1, "v": 10,
                                  "vw": 1.05}]}, "next_page_token": None}

    clock = iter(range(100))
    slept = []
    now = datetime(2026, 11, 5, 21, 10, tzinfo=UTC)
    client = amd.AlpacaBarsClient(credentials=("key-id", "secret-key"), transport=transport,
                                  sleep=slept.append, clock=lambda: next(clock) * 0.1, now=lambda: now)
    with pytest.raises(amd.DelayedSipTooRecent):
        client.bars(["SPY"], OPEN, datetime(2026, 11, 5, 21, 0, tzinfo=UTC), feed="sip")
    assert urls == []
    out = client.bars(["spy"], OPEN, datetime(2026, 11, 5, 21, 0, tzinfo=UTC), feed="iex")
    client.bars(["SPY"], OPEN, datetime(2026, 11, 5, 20, 50, tzinfo=UTC), feed="sip")
    assert out["SPY"][0]["vwap"] == 1.05 and out["SPY"][0]["opened_at"] == OPEN
    assert "feed=iex" in urls[0][0] and "feed=sip" in urls[1][0]
    assert all("secret" not in url and "key-id" not in url for url, _ in urls)
    assert urls[0][1]["APCA-API-KEY-ID"] == "key-id"
    assert slept and slept[0] == pytest.approx(0.25)          # 0.35 s spacing, 0.1 s already elapsed


def test_client_backs_off_on_rate_limit_and_stops_at_its_budget():
    calls = []

    def transport(url, headers):
        calls.append(url)
        if len(calls) == 1:
            raise amd.MarketDataUnavailable("HTTP 429", status_code=429)
        return {"bars": {}, "next_page_token": None}

    slept = []
    client = amd.AlpacaBarsClient(credentials=("k", "s"), transport=transport, sleep=slept.append,
                                  min_interval=0, max_requests=2, now=lambda: NOW)
    assert client.bars(["AAPL"], OPEN, OPEN + timedelta(hours=1), feed="iex") == {"AAPL": []}
    assert client.retries == 1 and 2.0 in slept
    with pytest.raises(amd.MarketDataUnavailable):
        client.bars(["AAPL"], OPEN, OPEN + timedelta(hours=1), feed="iex")
    unconfigured = amd.AlpacaBarsClient(credentials=("", ""), transport=transport)
    assert not unconfigured.configured
    with pytest.raises(amd.MarketDataUnavailable):
        unconfigured.bars(["AAPL"], OPEN, OPEN + timedelta(hours=1), feed="iex")


# -------------------------------------------------------------------- tagging

def test_fill_tags_mark_paper_auction_fills():
    opg = SimpleNamespace(time_in_force="opg", leg_role=None, order_type="market")
    assert fh.fill_tags(None, opg, broker="alpaca_paper") == {
        "time_in_force": "opg", "paper_fill": True, "auction": True, "excluded": "paper_auction"}
    assert fh.fill_tags(None, SimpleNamespace(time_in_force="cls"), broker=None)["excluded"] == "paper_auction"
    assert fh.fill_tags(None, SimpleNamespace(time_in_force="day"), broker="alpaca_paper")["excluded"] is None
    assert fh.fill_tags(None, opg, broker="alpaca_live")["excluded"] is None           # a real auction fill
    fill = SimpleNamespace(raw_payload={"time_in_force": "GTC"})
    assert fh.fill_tags(fill, None)["time_in_force"] == "gtc"
    assert fh.fill_tags(None, None)["time_in_force"] is None


def test_signal_feed_tags_each_input():
    observation = {"provider": "alpaca_iex", "feed_class": "iex", "close": 101.0}
    tags = fh.signal_feed("database:yfinance", observation, now=OPEN + timedelta(minutes=10))
    assert tags == {"daily_bars": "consolidated", "daily_source": "database:yfinance", "reference_price": "iex",
                    "reference_provider": "alpaca_iex", "decision_phase": "session"}
    assert fh.signal_feed("database:alpaca_iex_daily", {}, now=OPEN + timedelta(hours=7))["daily_bars"] == "iex"
    assert fh.signal_feed("database:alpaca_iex_daily", {}, now=OPEN + timedelta(hours=7))["decision_phase"] == \
        "after_close"
    assert fh.daily_source_feed("alpaca_sip_daily") == "sip"
    assert fh.daily_source_feed("database:mixed(yfinance,yahoo_chart)") == "consolidated"
    assert fh.daily_source_feed("database:mixed(yfinance,alpaca_iex_daily)") == "mixed"
    assert fh.daily_source_feed(None) == "unknown"


def test_stock_paper_signals_record_their_feed():
    from app.services import stock_paper_ledger as ledger

    generated = SimpleNamespace(action="BUY", probability_up=0.6, probability_down=0.4, confidence=0.6,
                                reason="test", features={"rsi": 20})
    strategy = SimpleNamespace(id=3)
    observation = {"provider": "alpaca_iex", "feed_class": "iex", "close": 50.0,
                   "opened_at": OPEN + timedelta(minutes=9)}
    signal = ledger._build_stock_paper_signal(strategy, "AAPL", generated, "database:yfinance", observation,
                                              False, OPEN + timedelta(minutes=10))
    assert signal.features["feed"]["daily_bars"] == "consolidated"
    assert signal.features["feed"]["reference_price"] == "iex"
    assert signal.features["rsi"] == 20 and signal.features["source"] == "database:yfinance"


# ----------------------------------------------------------- divergence detector

def test_divergence_detector_on_synthetic_bars():
    # IEX prints a thin low that SIP does not have; SIP carries a high IEX never printed.
    sip = _tape([100.0, 99.6, 99.4, 100.8, 101.6])
    iex = _tape([100.0, 99.7, 98.9, 100.7, 101.0])
    levels = [
        {"symbol": "AAPL", "kind": "stop", "price": 99.0, "source": "app_protective_stop"},     # IEX only
        {"symbol": "AAPL", "kind": "target", "price": 101.5, "source": "broker_take_profit"},  # SIP only
        {"symbol": "AAPL", "kind": "stop", "price": 99.7, "source": "broker_stop"},            # both
        {"symbol": "AAPL", "kind": "target", "price": 105.0, "source": "broker_take_profit"},  # neither
        {"symbol": "MSFT", "kind": "stop", "price": 50.0, "source": "broker_stop"},            # no IEX bars
    ]
    out = fh.detect_divergence(levels, {"AAPL": iex}, {"AAPL": sip, "MSFT": _tape([51.0, 49.0])}, SESSION)
    assert [row["touched_by"] for row in out] == ["iex_only", "sip_only", "both", "neither", "missing_iex"]
    assert [row["divergent"] for row in out] == [True, True, False, False, False]
    assert out[0]["iex"]["first_touch_at"] == (OPEN + timedelta(minutes=2)).isoformat()
    assert out[0]["sip"]["low"] == pytest.approx(99.4) and out[0]["sip_distance_bps"] > 0
    assert out[1]["sip"]["first_touch_at"] == (OPEN + timedelta(minutes=4)).isoformat()
    counts = fh.divergence_counts(out)
    assert counts == {"levels": 5, "divergent": 2, "iex_only": 1, "sip_only": 1, "both": 1, "neither": 1,
                      "missing": 1, "stops_divergent": 1, "targets_divergent": 1}
    # After-hours prints never count as a session touch.
    after = sip + [_bar(400, 90.0)]
    assert fh.compare_level(levels[2], iex, after, SESSION)["sip"]["low"] == pytest.approx(99.4)


def _account(db):  # noqa: F811
    account = StockPaperAccount(broker="alpaca_paper", broker_account_id="paper-1", cash=100_000, equity=100_000,
                                buying_power=100_000, raw_payload={})
    db.add(account)
    db.flush()
    return account


_ORDER_IDS = iter(range(1, 1_000_000))


def _order(db, account, **values):  # noqa: F811
    base = dict(account_id=account.id, client_order_id=f"o-{next(_ORDER_IDS)}",
                symbol="AAPL", side="buy", quantity=Decimal("10"), order_type="limit", reserved_cash=Decimal("0"),
                status="filled", source="scheduled_signal", time_in_force="day")
    base.update(values)
    order = StockPaperOrder(**base)
    db.add(order)
    db.flush()
    return order


def test_feed_divergence_job_logs_one_row_per_session(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    account = _account(db)
    db.add(StockPaperPosition(account_id=account.id, symbol="AAPL", quantity=Decimal("10"),
                              average_entry_price=Decimal("100"), observed_at=OPEN, raw_payload={}))
    created = OPEN - timedelta(days=1)
    _order(db, account, side="sell", order_type="stop", leg_role="stop_loss", stop_price=Decimal("99.5"),
           status="new", time_in_force="gtc", created_at=created, updated_at=created)
    _order(db, account, side="sell", order_type="limit", leg_role="take_profit", limit_price=Decimal("101.5"),
           status="new", time_in_force="gtc", created_at=created, updated_at=created)
    _order(db, account, side="sell", order_type="stop", leg_role="stop_loss", stop_price=Decimal("80"),
           status="canceled", time_in_force="gtc", created_at=created, updated_at=created)      # dead before
    db.flush()
    sip = _tape([100.0, 99.6, 99.4, 100.8, 101.6])
    iex = _tape([100.0, 99.7, 99.6, 100.7, 101.0])
    client = _FakeClient({("sip", "AAPL"): sip, ("iex", "AAPL"): iex})
    friday = datetime(2026, 11, 6, 21, 20, tzinfo=UTC)   # Friday 16:20 ET; Thursday is the session asked for
    early = fh.record_feed_divergence(db, now=datetime(2026, 11, 5, 21, 5, tzinfo=UTC), session=SESSION,
                                      client=client)
    assert early["status"] == "waiting" and client.calls == []
    result = fh.record_feed_divergence(db, now=friday, session=SESSION, client=client)
    assert result["recorded"] and result["status"] == "divergent"
    assert result["levels"] == 3 and result["sip_only"] == 2 and result["divergent"] == 2
    # 99.5 broker stop: SIP low 99.4, IEX low 99.6 -> SIP only; 101.5 target: SIP only; app stop 97: neither.
    row = db.query(AuditLog).filter_by(action=fh.DIVERGENCE_ACTION).one()
    assert row.payload["session"] == "2026-11-05" and len(row.payload["divergent"]) == 2
    assert {item["source"] for item in row.payload["comparisons"]} == {
        "broker_stop", "broker_take_profit", "app_protective_stop"}
    again = fh.record_feed_divergence(db, now=friday, session=SESSION, client=client)
    assert again["recorded"] is False and len(client.calls) == 2      # one IEX and one SIP request, once
    summary = fh.divergence_summary(db, now=datetime.now(UTC))
    assert summary["sessions_checked"] == 1 and summary["divergent_sessions"] == 1
    assert summary["sip_only"] == 2 and summary["examples"][0]["symbol"] == "AAPL"
    section = pcc.weekly_report_section(db)["feed_hygiene"]
    assert section["divergence"]["divergent_sessions"] == 1 and "1 with an IEX/SIP touch disagreement" in \
        section["detail"]


def test_feed_divergence_reports_unavailable_feeds_without_a_row(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr("app.services.stock_paper_ledger.active_paper_broker_name", lambda: "alpaca_paper")
    account = _account(db)
    db.add(StockPaperPosition(account_id=account.id, symbol="AAPL", quantity=Decimal("10"),
                              average_entry_price=Decimal("100"), observed_at=OPEN, raw_payload={}))
    db.flush()
    client = _FakeClient({}, fail={"sip": amd.MarketDataUnavailable("HTTP 403", status_code=403)})
    result = fh.record_feed_divergence(db, now=NOW, session=SESSION, client=client)
    assert result["status"] == "unavailable" and not db.query(AuditLog).filter_by(action=fh.DIVERGENCE_ACTION).count()


# ------------------------------------------------- SIP references and calibration

def _seed_reference_fills(db):  # noqa: F811
    account = _account(db)
    entry = _order(db, account, limit_price=Decimal("100.30"), order_class="bracket", time_in_force="gtc")
    opg = _order(db, account, symbol="MSFT", order_type="market", time_in_force="opg")
    exit_ = _order(db, account, side="sell", order_type="market", limit_price=Decimal("101.00"))
    stop = _order(db, account, side="sell", order_type="stop", leg_role="stop_loss", stop_price=Decimal("99.0"),
                  time_in_force="gtc")
    fills = [
        StockPaperFill(account_id=account.id, order_id=entry.id, broker_activity_id="a1", symbol="AAPL", side="buy",
                       quantity=Decimal("10"), price=Decimal("100.40"), fee=Decimal("0"), cost_known=True,
                       filled_at=OPEN + timedelta(minutes=7, seconds=5), raw_payload={}),
        StockPaperFill(account_id=account.id, order_id=opg.id, broker_activity_id="a2", symbol="MSFT", side="buy",
                       quantity=Decimal("5"), price=Decimal("50.30"), fee=Decimal("0"), cost_known=True,
                       filled_at=OPEN + timedelta(seconds=20), raw_payload={}),
        StockPaperFill(account_id=account.id, order_id=exit_.id, broker_activity_id="a3", symbol="AAPL",
                       side="sell", quantity=Decimal("10"), price=Decimal("100.95"), fee=Decimal("0"),
                       cost_known=True, filled_at=OPEN + timedelta(hours=3, seconds=30), raw_payload={}),
        StockPaperFill(account_id=account.id, order_id=stop.id, broker_activity_id="a4", symbol="AAPL",
                       side="sell", quantity=Decimal("10"), price=Decimal("98.90"), fee=Decimal("0"),
                       cost_known=True, filled_at=OPEN + timedelta(hours=4, seconds=30), raw_payload={}),
    ]
    db.add_all(fills)
    db.flush()
    aapl = [_bar(i, 100.0 + 0.04 * i, vwap=100.0 + 0.04 * i + 0.01) for i in range(10)]
    aapl += [_bar(180, 101.05, vwap=101.04), _bar(240, 99.0, vwap=99.02)]
    msft = [_bar(0, 50.10, vwap=50.12), _bar(1, 50.20, vwap=50.21)]
    return account, {"entry": entry, "opg": opg, "exit": exit_, "stop": stop}, fills, \
        _FakeClient({("sip", "AAPL"): aapl, ("sip", "MSFT"): msft})


def test_sip_references_are_recorded_once_and_respect_the_delay(db):  # noqa: F811
    _account_, orders, fills, client = _seed_reference_fills(db)
    inside = fh.record_sip_references(db, now=datetime(2026, 11, 5, 21, 10, tzinfo=UTC), client=client)
    assert inside["recorded"] == 0 and client.calls == []           # Thursday's tape is still inside the delay
    result = fh.record_sip_references(db, now=NOW, client=client)
    assert result["recorded"] == 4 and result["priced"] == 4
    assert len(client.calls) == 1 and client.calls[0][3] == "sip" and client.calls[0][0] == ("AAPL", "MSFT")
    refs = fh.sip_references(db)
    entry = refs[fills[0].id]
    assert entry["kind"] == "official_open" and entry["price"] == 100.0 and entry["sip_bps"] == pytest.approx(40.0)
    assert entry["minute_bar"]["vwap"] == pytest.approx(100.29)
    assert entry["time_in_force"] == "gtc" and entry["paper_fill"] and not entry["auction"]
    exit_ = refs[fills[2].id]
    assert exit_["kind"] == "minute_vwap" and exit_["price"] == 101.04 and exit_["sip_bps"] == pytest.approx(8.9074, abs=1e-3)
    assert refs[fills[1].id]["excluded"] == "paper_auction"
    assert fh.record_sip_references(db, now=NOW, client=client)["recorded"] == 0 and len(client.calls) == 1
    by_order = fh.order_sip_references(refs, [orders["entry"].id, orders["exit"].id, None])
    assert by_order[orders["entry"].id]["price"] == 100.0 and by_order[orders["exit"].id]["fills"] == 1


def test_calibration_prices_entries_against_sip_and_excludes_paper_auctions(db):  # noqa: F811
    _account_, orders, fills, client = _seed_reference_fills(db)
    fh.record_sip_references(db, now=NOW, client=client)
    frame = pd.DataFrame({"date": pd.bdate_range("2026-08-03", periods=60).strftime("%Y-%m-%d"),
                          "open": 100.0, "high": 100.5, "low": 99.5, "close": np.linspace(99, 101, 60),
                          "volume": 2_000_000.0})
    report = pcc.build_calibration(db, now=NOW, loader=lambda symbol: frame)
    rows = {row["fill_id"]: row for row in report["fills"]["rows"]}
    entry, opg, exit_, stop = (rows[fill.id] for fill in fills)
    assert entry["reference_feed"] == "sip" and entry["slippage_bps"] == pytest.approx(40.0)
    assert entry["decision_slippage_bps"] == pytest.approx(9.9701, abs=1e-3)       # vs the IEX limit 100.30
    assert entry["sip_reference_kind"] == "official_open" and entry["usable"]
    assert opg["excluded"] == "paper_auction" and opg["time_in_force"] == "opg" and not opg["usable"]
    assert exit_["reference_feed"] == "sip"
    assert stop["reference_feed"] == "level" and stop["slippage_bps"] == pytest.approx(10.101, abs=1e-3)
    assert stop["sip_slippage_bps"] is not None                                    # kept for the view
    assert report["fills"]["excluded_paper_auction"] == 1 and report["fills"]["sip_referenced"] == 2
    overall = report["tables"]["overall"]
    assert overall["excluded_paper_auction"] == 1 and overall["priced"] == 3
    feed = report["feed"]
    assert feed["excluded_by_time_in_force"] == {"opg": 1} and feed["both_benchmarks"] == 3
    assert feed["by_order_kind"]["limit"]["difference_bps_mean"] == pytest.approx(30.03, abs=0.01)
    assert {row["time_in_force"] for row in report["tables"]["by_time_in_force"]} == {"gtc", "opg", "day"}


def test_observe_fill_without_sip_keeps_the_decision_price():
    order = SimpleNamespace(order_type="limit", leg_role=None, limit_price=Decimal("100"), stop_price=None,
                            strategy_id=1, time_in_force="day")
    fill = SimpleNamespace(id=1, order_id=1, symbol="SPY", side="buy", quantity=Decimal("1"),
                           price=Decimal("100.10"), fee=None, filled_at=OPEN + timedelta(hours=2), raw_payload={})
    row = pcc.observe_fill(fill, order, None)
    assert row["reference_feed"] == "iex" and row["slippage_bps"] == pytest.approx(10.0)
    assert row["sip_slippage_bps"] is None and row["excluded"] is None and row["usable"]
    orphan = pcc.observe_fill(SimpleNamespace(**{**fill.__dict__, "order_id": None}), None, None,
                              sip={"price": 100.05, "kind": "minute_vwap"})
    assert orphan["reference_feed"] == "sip" and orphan["slippage_bps"] == pytest.approx(4.9975, abs=1e-3)


def test_attribution_carries_sip_reference_next_to_the_iex_number():
    references = {
        1: {"order_id": 10, "price": 100.0, "quantity": 10, "fill_price": 100.4, "kind": "official_open",
            "time_in_force": "gtc", "auction": False, "paper_fill": True},
        2: {"order_id": 11, "price": 101.0, "quantity": 4, "fill_price": 100.9, "kind": "minute_vwap"},
        3: {"order_id": 12, "price": 102.0, "quantity": 6, "fill_price": 101.9, "kind": "minute_vwap"},
    }
    entry_order = SimpleNamespace(id=10, time_in_force="GTC")
    out = ta.sip_slippage(references, entry_order=entry_order, exit_order_ids=[11, 12, None],
                          entry_price=100.4, exit_price=101.5)
    assert out["reference_feed"] == "iex" and out["entry_time_in_force"] == "gtc" and not out["auction"]
    assert out["sip_reference"]["entry"]["price"] == 100.0
    assert out["sip_reference"]["exit"]["price"] == pytest.approx(101.6) and out["sip_reference"]["exit"]["fills"] == 2
    assert out["sip_bps"]["entry"] == pytest.approx(40.0)
    assert out["sip_bps"]["exit"] == pytest.approx(9.8425, abs=1e-3)
    empty = ta.sip_slippage({}, entry_order=SimpleNamespace(id=1, time_in_force="opg"), exit_order_ids=[],
                            entry_price=1.0, exit_price=1.0)
    assert empty["sip_reference"] == {"entry": None, "exit": None} and empty["auction"] is True


# ----------------------------------------------------------- daily bars / opens

def test_daily_fallback_prefers_delayed_sip_over_iex(db):  # noqa: F811
    calls = []

    def fake(feed, rows):
        def fetch(symbol, period):
            calls.append(feed)
            return rows
        return fetch

    prices = pd.DataFrame({"date": [date(2026, 11, 4)], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                           "adjusted_close": [1.0], "volume": [10]})
    with patch.object(market_data, "fetch_yfinance_prices", return_value=pd.DataFrame()), \
            patch.object(market_data, "fetch_yahoo_chart_prices", return_value=pd.DataFrame()), \
            patch.object(type(market_data.settings), "research_alpaca_credentials", return_value=("k", "s")), \
            patch.object(market_data, "fetch_alpaca_sip_daily_prices", fake("sip", prices)), \
            patch.object(market_data, "fetch_alpaca_iex_daily_prices", fake("iex", prices)):
        result = market_data.import_market_prices(db, "SPY")
    assert result["source"] == "alpaca_sip_daily" and result["trusted"] and calls == ["sip"]
    with patch.object(market_data, "fetch_yfinance_prices", return_value=pd.DataFrame()), \
            patch.object(market_data, "fetch_yahoo_chart_prices", return_value=pd.DataFrame()), \
            patch.object(type(market_data.settings), "research_alpaca_credentials", return_value=("k", "s")), \
            patch.object(market_data, "fetch_alpaca_sip_daily_prices", fake("sip", pd.DataFrame())), \
            patch.object(market_data, "fetch_alpaca_iex_daily_prices", fake("iex", prices)):
        result = market_data.import_market_prices(db, "QQQ")
    assert result["source"] == "alpaca_iex_daily" and [a["provider"] for a in result["provider_attempts"]][-2:] == \
        ["alpaca_sip_daily", "alpaca_iex_daily"]


def test_sip_daily_query_ends_outside_the_delay():
    payload = {"bars": {"SPY": [{"t": "2026-10-02T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 10}]},
               "next_page_token": None}
    with patch.object(type(market_data.settings), "research_alpaca_credentials", return_value=("key", "secret")), \
            patch.object(market_data, "urlopen", return_value=io.StringIO(json.dumps(payload))) as network:
        before = datetime.now(UTC)
        frame = market_data.fetch_alpaca_sip_daily_prices("SPY")
    url = network.call_args.args[0].full_url
    assert "feed=sip" in url and "key" not in url and "secret" not in url
    end = datetime.fromisoformat(dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))["end"]
                                 .replace("%3A", ":").replace("Z", "+00:00"))
    assert end <= before - timedelta(minutes=16) + timedelta(seconds=1)
    assert list(frame.date) == [date(2026, 10, 2)]


def test_gate_cost_opening_print_ignores_iex_bars(db):  # noqa: F811
    db.add(IntradayBar(symbol="AAPL", timeframe="1m", opened_at=OPEN, open=101.75, high=101.8, low=101.7,
                       close=101.8, volume=10, provider="alpaca_iex", feed_class="iex", exchange_timestamp=OPEN))
    db.flush()
    assert gc.intraday_open(db, "AAPL", SESSION) is None          # the consolidated daily open stands
    db.add(IntradayBar(symbol="AAPL", timeframe="1m", opened_at=OPEN, open=101.25, high=101.5, low=101.0,
                       close=101.4, volume=1000, provider="alpaca_delayed_sip", feed_class="sip_delayed",
                       exchange_timestamp=OPEN))
    db.flush()
    assert gc.intraday_open(db, "AAPL", SESSION) == 101.25


# ------------------------------------------------------------------ gapped stops

def test_gapped_stop_fills_at_min_of_stop_and_open():
    assert gapped_stop_fill(97.0, 95.0) == 95.0 and gapped_stop_fill(97.0, 97.0) == 97.0
    rows = 30
    frame = pd.DataFrame({"date": pd.bdate_range("2024-01-01", periods=rows).strftime("%Y-%m-%d"),
                          "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e6})
    frame.loc[12, ["open", "high", "low", "close"]] = [94.0, 95.0, 93.5, 94.5]    # opens 6% down
    config = BarrierConfig(cost_model="flat", slippage_bps=0, fees_bps=0, stop_loss_pct=0.03,
                           take_profit_pct=0.10, max_holding_days=20)
    row = label_events(frame, [10], config).iloc[0]
    assert row.barrier == "stop_loss_gap" and row.exit_idx == 12
    assert row.exit_price == pytest.approx(94.0)                     # the open, not the 97 stop
    assert row.net_return == pytest.approx(-0.06)
    # An intrabar touch without a gap still fills at the stop level.
    frame.loc[12, ["open", "high", "low", "close"]] = [99.0, 99.2, 96.0, 98.0]
    row = label_events(frame, [10], config).iloc[0]
    assert row.barrier == "stop_loss" and row.exit_price == pytest.approx(97.0)


def test_report_script_counts_gapped_stop_overstatement():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "feed_hygiene_report.py"
    spec = importlib.util.spec_from_file_location("feed_hygiene_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    frame = pd.DataFrame({"open": [100.0, 100.0, 94.0]})
    trades = pd.DataFrame([
        {"symbol": "X", "barrier": "stop_loss_gap", "exit_idx": 2, "entry_price": 100.0, "stop_loss_pct": 0.03,
         "net_return": -0.06},
        {"symbol": "X", "barrier": "stop_loss", "exit_idx": 1, "entry_price": 100.0, "stop_loss_pct": 0.03,
         "net_return": -0.03},
        {"symbol": "X", "barrier": "take_profit", "exit_idx": 1, "entry_price": 100.0, "stop_loss_pct": 0.03,
         "net_return": 0.06},
    ])
    out = module.gapped_stop_impact(trades, {"X": frame})
    assert out["gapped_stops"] == 1 and out["stop_exits"] == 2 and out["gapped_share_of_stops"] == 0.5
    assert out["overstatement_per_gapped_trade_bps"] == pytest.approx(300.0)       # -3% at the stop vs -6%
    assert out["expectancy_bps"] == pytest.approx(-100.0)
    assert out["expectancy_if_filled_at_stop_bps"] == pytest.approx(0.0)


def test_feed_hygiene_job_is_scheduled_after_the_sip_delay():
    from app.tasks import jobs

    entry = celery_app.conf.beat_schedule["feed-hygiene"]
    assert entry["task"] == "app.tasks.jobs.feed_hygiene_job"
    schedule = entry["schedule"]
    assert schedule.hour == {16} and schedule.minute == {40} and schedule.day_of_week == {1, 2, 3, 4, 5}
    assert celery_app.conf.task_routes["app.tasks.jobs.feed_hygiene_job"] == {"queue": "market_data"}
    assert callable(jobs.feed_hygiene_job)
