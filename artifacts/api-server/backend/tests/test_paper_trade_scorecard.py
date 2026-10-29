import hashlib
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from celery.schedules import crontab
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.api.stock_paper import router
from app.core.config import Settings, settings
from app.core.security import AuthenticationMiddleware, required_role
from app.db.base import Base
from app.db.session import get_db
from app.models import MarketPrice, Strategy, StrategySignal
from app.models.stock_paper import (
    StockPaperAccount,
    StockPaperEquitySnapshot,
    StockPaperFill,
    StockPaperLedgerEvent,
    StockPaperOrder,
    StockPaperPosition,
)
from app.services import paper_trade_scorecard as scorecard_module
from app.services.paper_trade_scorecard import (
    SNAPSHOT_EVENT_TYPE,
    build_scorecard,
    goal_progress,
    record_scorecard_snapshot,
    render_markdown,
    scorecard_history,
    source_label,
    summarize,
    time_bucket,
)
from app.services.trade_mistakes import classify_round_trip
from app.tasks import jobs
from app.tasks.celery_app import celery_app

UTC = timezone.utc
DAY = "2026-10-29"
NOW = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)


def client_id(key: str) -> str:
    return "sp-" + hashlib.sha256(key.encode()).hexdigest()[:45]


def at(hour: int, minute: int = 0, day: int = 29, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


class Ledger:
    """Tiny synthetic ledger builder for orders and fills."""

    def __init__(self, db: Session, account: StockPaperAccount):
        self.db = db
        self.account = account
        self.counter = 0

    def order(self, *, symbol, side, quantity, source="scheduled_paper_signal", strategy=None, client=None,
              order_class="simple", parent=None, leg_role=None, stop=None, take=None, created=None, signal=None):
        self.counter += 1
        row = StockPaperOrder(
            account_id=self.account.id, strategy_id=strategy.id if strategy else None,
            client_order_id=client or f"order-{self.counter}", broker_order_id=f"b-{self.counter}",
            symbol=symbol, side=side, quantity=Decimal(str(quantity)), order_type="limit", status="filled",
            source=source, order_class=order_class, parent_order_id=parent.id if parent else None, leg_role=leg_role,
            stop_price=Decimal(str(stop)) if stop is not None else None,
            take_profit_price=Decimal(str(take)) if take is not None else None,
            created_at=created or at(14), signal_id=signal.id if signal else None,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def fill(self, order, quantity, price, when, fee=None):
        self.counter += 1
        row = StockPaperFill(
            account_id=self.account.id, order_id=order.id, broker_activity_id=f"act-{self.counter}",
            broker_order_id=order.broker_order_id, symbol=order.symbol, side=order.side,
            quantity=Decimal(str(quantity)), price=Decimal(str(price)),
            fee=Decimal(str(fee)) if fee is not None else None, cost_known=fee is not None,
            filled_at=when, raw_payload={},
        )
        self.db.add(row)
        self.db.flush()
        return row


class ScorecardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "scorecard.sqlite"),
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.account = StockPaperAccount(broker="alpaca_paper", broker_account_id="paper", cash=Decimal("9000"),
                                         buying_power=Decimal("9000"), equity=Decimal("10000"), status="reconciled",
                                         raw_payload={})
        self.db.add(self.account)
        self.db.flush()
        self.db.add(StockPaperEquitySnapshot(account_id=self.account.id, cash=Decimal("10000"), equity=Decimal("10000"),
                                             buying_power=Decimal("10000"), observed_at=at(13), raw_payload={}))
        self.trend = Strategy(name="Trend A", strategy_type="trend_a", parameters={}, current_status="paper_trading_active")
        self.probe = Strategy(name="Probe B", strategy_type="probe_b", parameters={}, current_status="research")
        self.swing = Strategy(name="Swing C", strategy_type="swing_c", parameters={}, current_status="paper_trading_active")
        self.db.add_all([self.trend, self.probe, self.swing])
        self.db.flush()
        self.ledger = Ledger(self.db, self.account)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def build_book(self):
        L = self.ledger
        # 1. Scheduled bracket, entry filled in two partials, take-profit leg exits.
        signal = StrategySignal(strategy_id=self.trend.id, symbol="AAPL", signal_time=at(14), action="BUY",
                                probability_up=Decimal("0.7"))
        self.db.add(signal)
        self.db.flush()
        aapl = L.order(symbol="AAPL", side="buy", quantity=10, strategy=self.trend, order_class="bracket",
                       stop=97, take=103, signal=signal)
        L.fill(aapl, 6, 100, at(14, 1), fee="0.05")
        L.fill(aapl, 4, 100, at(14, 2), fee="0.05")
        tp = L.order(symbol="AAPL", side="sell", quantity=10, strategy=self.trend, order_class="bracket",
                     parent=aapl, leg_role="take_profit")
        L.order(symbol="AAPL", side="sell", quantity=10, strategy=self.trend, order_class="bracket",
                parent=aapl, leg_role="stop_loss")
        L.fill(tp, 10, 103, at(17), fee="0.10")
        # 2. Exploration bracket, stop-loss leg exits in two partial fills.
        msft = L.order(symbol="MSFT", side="buy", quantity=5, strategy=self.probe, source="paper_exploration",
                       order_class="bracket", stop=194, take=206)
        L.fill(msft, 5, 200, at(15, 45), fee="0")
        stop = L.order(symbol="MSFT", side="sell", quantity=5, strategy=self.probe, source="paper_exploration",
                       order_class="bracket", parent=msft, leg_role="stop_loss")
        L.fill(stop, 3, 194, at(18), fee="0")
        L.fill(stop, 2, 193.5, at(18, 1), fee="0")
        # 3. Two strategies in one symbol: an unattributed sell-signal order
        #    (strategy recovered from its deterministic client id) must close
        #    Swing C's lot, and the protective stop then closes Trend A's lot.
        nvda_a = L.order(symbol="NVDA", side="buy", quantity=4, strategy=self.trend)
        L.fill(nvda_a, 4, 50, at(13, 35), fee="0")
        nvda_c = L.order(symbol="NVDA", side="buy", quantity=6, strategy=self.swing)
        L.fill(nvda_c, 6, 52, at(16), fee="0")
        sell_c = L.order(symbol="NVDA", side="sell", quantity=6,
                         client=client_id(f"scheduled-paper:{DAY}:NVDA:swing_c:sell"), created=at(19))
        L.fill(sell_c, 6, 55, at(19, 1), fee="0")
        stop_a = L.order(symbol="NVDA", side="sell", quantity=4,
                         client=client_id(f"scheduled-paper:{DAY}:NVDA:protective_stop:sell"), created=at(19, 30))
        L.fill(stop_a, 4, 48, at(19, 31), fee="0")
        # 4. Open exploration lot, marked to the broker position price.
        tsla = L.order(symbol="TSLA", side="buy", quantity=2, strategy=self.probe, source="paper_exploration")
        L.fill(tsla, 2, 300, at(14, 30), fee="0")
        self.db.add(StockPaperPosition(account_id=self.account.id, symbol="TSLA", quantity=Decimal("2"),
                                       average_entry_price=Decimal("300"), current_price=Decimal("310"),
                                       observed_at=at(20), raw_payload={}))
        # 5. A trade that closed long before the window.
        old = L.order(symbol="AMD", side="buy", quantity=1, strategy=self.trend, created=at(14, day=15, month=9))
        L.fill(old, 1, 10, at(14, day=15, month=9), fee="0")
        old_exit = L.order(symbol="AMD", side="sell", quantity=1, strategy=self.trend, order_class="bracket",
                           parent=old, leg_role="take_profit")
        L.fill(old_exit, 1, 11, at(15, day=15, month=9), fee="0")
        # 6. A manual close of a manual buy (not a bot trade).
        man = L.order(symbol="KO", side="buy", quantity=1, source="manual_control_room")
        L.fill(man, 1, 60, at(14), fee="0")
        man_exit = L.order(symbol="KO", side="sell", quantity=1, source="manual_close")
        L.fill(man_exit, 1, 61, at(15), fee="0")
        for day, close in ((date(2026, 9, 30), 500), (date(2026, 10, 30), 510)):
            self.db.add(MarketPrice(symbol="SPY", price_date=day, close=Decimal(str(close)), source="test"))
        self.db.commit()


class RoundTripTests(ScorecardFixture):
    def test_reconstructs_brackets_partials_fifo_and_sources(self):
        self.build_book()
        card = build_scorecard(self.db, days=30, now=NOW, account_id=self.account.id)
        self.assertEqual(card["status"], "available")
        trades = {trade["symbol"] + ":" + str(trade["strategy_id"]): trade for trade in card["trades"]}
        self.assertEqual(card["trade_count_in_window"], 5)  # 4 bot + 1 manual; AMD is outside the window
        self.assertEqual(card["non_bot_trades"], 1)

        aapl = trades[f"AAPL:{self.trend.id}"]
        self.assertEqual(aapl["exit_reason"], "take_profit_leg")
        self.assertEqual(aapl["entry_fills"], 2)
        self.assertEqual(aapl["source"], "scheduled")
        self.assertAlmostEqual(aapl["gross_pnl"], 30.0)
        self.assertAlmostEqual(aapl["fees"], 0.20)
        self.assertAlmostEqual(aapl["net_pnl"], 29.80)
        self.assertEqual(aapl["entry_time_bucket"], "morning_1000_1130")
        self.assertAlmostEqual(aapl["holding_minutes"], 179.0)
        self.assertAlmostEqual(aapl["probability"], 0.7)
        self.assertNotIn("mistakes", aapl)

        msft = trades[f"MSFT:{self.probe.id}"]
        self.assertEqual(msft["exit_reason"], "stop_loss_leg")
        self.assertEqual(msft["source"], "exploration")
        self.assertEqual(msft["exit_fills"], 2)
        self.assertAlmostEqual(msft["net_pnl"], -31.0)
        self.assertEqual(msft["mistakes"]["status"], "loss")
        self.assertTrue(msft["mistakes"]["mistakes"])

        swing = trades[f"NVDA:{self.swing.id}"]
        self.assertEqual(swing["exit_reason"], "sell_signal")
        self.assertAlmostEqual(swing["net_pnl"], 18.0)
        trend_nvda = trades[f"NVDA:{self.trend.id}"]
        self.assertEqual(trend_nvda["exit_reason"], "protective_stop")
        self.assertAlmostEqual(trend_nvda["net_pnl"], -8.0)
        self.assertEqual(trend_nvda["entry_time_bucket"], "open_0930_1000")

        manual = next(trade for trade in card["trades"] if trade["symbol"] == "KO")
        self.assertEqual((manual["source"], manual["exit_reason"]), ("manual", "manual"))
        self.assertEqual(card["data_quality"]["unmatched_exit_count"], 0)

    def test_aggregates_goal_benchmark_and_open_marks(self):
        self.build_book()
        card = build_scorecard(self.db, days=30, now=NOW, account_id=self.account.id)
        head = card["headline"]
        self.assertEqual((head["trades"], head["wins"], head["losses"]), (4, 2, 2))
        self.assertAlmostEqual(head["win_rate"], 0.5)
        self.assertAlmostEqual(head["total_pnl"], 29.8 - 31 + 18 - 8)
        self.assertAlmostEqual(head["avg_win"], (29.8 + 18) / 2)
        self.assertAlmostEqual(head["avg_loss"], (-31 - 8) / 2)
        self.assertAlmostEqual(head["profit_factor"], (29.8 + 18) / 39)
        self.assertAlmostEqual(head["expectancy"], head["total_pnl"] / 4)
        self.assertIsNone(head["bootstrap_ci"])
        sources = {row["key"]: row for row in card["by_source"]}
        self.assertEqual(sources["exploration"]["trades"], 1)
        self.assertEqual(sources["scheduled"]["trades"], 3)
        self.assertEqual(sources["manual"]["trades"], 1)
        reasons = {row["key"] for row in card["by_exit_reason"]}
        self.assertEqual(reasons, {"take_profit_leg", "stop_loss_leg", "sell_signal", "protective_stop"})
        strategies = {row["key"]: row for row in card["by_strategy"]}
        self.assertEqual(strategies[self.trend.id]["trades"], 2)
        self.assertEqual(strategies[self.trend.id]["strategy_name"], "Trend A")
        self.assertEqual(strategies[self.probe.id]["sources"], ["exploration"])
        self.assertEqual([row["key"] for row in card["by_day"]], [DAY])
        self.assertEqual(card["goal"]["last_window_trades"], 4)
        self.assertFalse(card["goal"]["met_last_window"])
        self.assertEqual(card["mistakes"]["losing_trades"], 2)
        self.assertEqual(sum(card["mistakes"]["primary_counts"].values()), 2)
        self.assertEqual(card["open_mark_to_market"]["positions"], 1)
        self.assertAlmostEqual(card["open_mark_to_market"]["unrealized_pnl"], 20.0)
        bench = card["benchmark"]
        self.assertEqual(bench["status"], "available")
        self.assertAlmostEqual(bench["benchmark_return"], 0.02)
        self.assertAlmostEqual(bench["benchmark_pnl_on_start_equity"], 200.0)
        self.assertTrue(card["paper_only"])
        self.assertFalse(card["live_authorized"])
        markdown = render_markdown(card)
        self.assertIn("# Paper trade scorecard (30 days)", markdown)
        self.assertIn("Trend A", markdown)
        self.assertIn("protective_stop", markdown)

    def test_scorecard_is_read_only(self):
        self.build_book()
        before = (self.db.query(StockPaperOrder).count(), self.db.query(StockPaperFill).count(),
                  self.db.query(StockPaperLedgerEvent).count())
        build_scorecard(self.db, days=30, now=NOW, account_id=self.account.id)
        self.db.rollback()
        after = (self.db.query(StockPaperOrder).count(), self.db.query(StockPaperFill).count(),
                 self.db.query(StockPaperLedgerEvent).count())
        self.assertEqual(before, after)

    def test_partial_exit_stays_open_with_realized_part(self):
        L = self.ledger
        entry = L.order(symbol="AAPL", side="buy", quantity=10, strategy=self.trend, order_class="bracket", stop=97, take=103)
        L.fill(entry, 10, 100, at(14), fee="0")
        tp = L.order(symbol="AAPL", side="sell", quantity=10, strategy=self.trend, parent=entry, leg_role="take_profit")
        L.fill(tp, 4, 103, at(15), fee="0")
        self.db.commit()
        card = build_scorecard(self.db, days=30, now=NOW, account_id=self.account.id)
        self.assertEqual(card["headline"]["trades"], 0)
        open_lot = card["open_positions"][0]
        self.assertAlmostEqual(open_lot["quantity_open"], 6)
        self.assertAlmostEqual(open_lot["realized_partial_pnl"], 12.0)
        self.assertIsNone(open_lot["unrealized_pnl"])
        self.assertEqual(card["open_mark_to_market"]["unmarked_positions"], 1)

    def test_no_account_is_unavailable(self):
        with patch.object(settings, "active_paper_broker", "alpaca_paper"):
            self.db.delete(self.account)
            self.db.commit()
            card = build_scorecard(self.db, days=7, now=NOW)
        self.assertEqual(card["status"], "unavailable")
        self.assertEqual(card["headline"]["trades"], 0)

    def test_snapshot_once_per_new_york_date(self):
        self.build_book()
        with patch.object(scorecard_module, "_account", return_value=self.account):
            first = record_scorecard_snapshot(self.db, days=30, now=NOW)
            second = record_scorecard_snapshot(self.db, days=30, now=NOW + timedelta(minutes=30))
        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        rows = self.db.query(StockPaperLedgerEvent).filter_by(event_type=SNAPSHOT_EVENT_TYPE).all()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].account_id)
        self.assertEqual(rows[0].payload["as_of_date"], "2026-10-30")
        history = scorecard_history(self.db)
        self.assertEqual(history[0]["trades"], 4)


class ScorecardCliTests(ScorecardFixture):
    def test_cli_prints_markdown_without_writing(self):
        import io
        from contextlib import redirect_stdout

        from scripts import report_paper_scorecard

        self.build_book()
        engine = self.engine

        class Factory:
            def __call__(self):
                return Session(engine)

        out = io.StringIO()
        with patch("app.db.session.SessionLocal", Factory()), patch.object(
            scorecard_module, "_account", return_value=self.account,
        ), redirect_stdout(out):
            self.assertEqual(report_paper_scorecard.main(["--days", "30"]), 0)
        self.assertIn("## Headline", out.getvalue())
        self.assertEqual(self.db.query(StockPaperLedgerEvent).filter_by(event_type=SNAPSHOT_EVENT_TYPE).count(), 0)


class StatisticsTests(unittest.TestCase):
    @staticmethod
    def trades(pnls):
        start = datetime(2026, 11, 2, 15, tzinfo=UTC)
        return [{"net_pnl": value, "net_return": value / 1000, "fees": 0.0, "holding_minutes": 30.0,
                 "costs_known": True, "exited_at": (start + timedelta(minutes=i)).isoformat()}
                for i, value in enumerate(pnls)]

    def test_bootstrap_ci_from_twenty_trades(self):
        stats = summarize(self.trades([10.0] * 16 + [-5.0] * 4))
        self.assertAlmostEqual(stats["win_rate"], 0.8)
        self.assertAlmostEqual(stats["payoff_ratio"], 2.0)
        self.assertAlmostEqual(stats["profit_factor"], 8.0)
        ci = stats["bootstrap_ci"]
        self.assertLessEqual(ci["win_rate"][0], 0.8)
        self.assertGreaterEqual(ci["win_rate"][1], 0.8)
        self.assertLessEqual(ci["expectancy"][0], stats["expectancy"])
        self.assertEqual(summarize(self.trades([1.0] * 19))["bootstrap_ci"], None)

    def test_headline_leads_with_expectancy_and_flags_small_samples(self):
        trades = self.trades([10.0] * 16 + [-5.0] * 4)
        trades[0]["costs_known"] = False
        stats = summarize(trades)
        self.assertEqual(next(iter(stats)), "expectancy")
        self.assertAlmostEqual(stats["expectancy"], 7.0)
        self.assertAlmostEqual(stats["expectancy_known_costs"], (150.0 - 20.0) / 19, places=5)
        self.assertAlmostEqual(stats["avg_win"], 10.0)
        self.assertAlmostEqual(stats["avg_loss"], -5.0)
        # Wilson 95% for 16/20 (reference: 0.5840 .. 0.9193).
        low, high = stats["win_rate_wilson_ci"]
        self.assertAlmostEqual(low, 0.5840, places=3)
        self.assertAlmostEqual(high, 0.9193, places=3)
        self.assertTrue(stats["insufficient_data"])
        self.assertFalse(summarize(self.trades([1.0] * 30))["insufficient_data"])
        empty = summarize([])
        self.assertTrue(empty["insufficient_data"])
        self.assertIsNone(empty["win_rate_wilson_ci"])
        self.assertIsNone(empty["expectancy_known_costs"])

    def test_goal_progress_and_rolling_windows(self):
        goal = goal_progress(self.trades([-1.0] * 5 + [5.0] * 8 + [-1.0] * 2))
        self.assertEqual(goal["last_window_wins"], 8)
        self.assertTrue(goal["met_last_window"])
        self.assertEqual(goal["rolling"]["last_20"]["trades"], 15)
        self.assertFalse(goal["rolling"]["last_20"]["complete"])
        losing = goal_progress(self.trades([1.0] * 8 + [-10.0] * 2))
        self.assertFalse(losing["met_last_window"])  # 8/10 but not making money

    def test_source_labels_keep_forward_trials_apart(self):
        self.assertEqual(source_label("paper_exploration"), "exploration")
        self.assertEqual(source_label("scheduled_paper_signal"), "scheduled")
        self.assertEqual(source_label("manual_control_room", "trial-1"), "forward_trial")
        self.assertEqual(source_label("manual_close"), "manual")
        self.assertEqual(source_label("broker_import"), "other")

    def test_time_buckets_use_new_york_clock(self):
        self.assertEqual(time_bucket(datetime(2026, 11, 2, 14, 35, tzinfo=UTC)), "open_0930_1000")
        self.assertEqual(time_bucket(datetime(2026, 11, 2, 20, 45, tzinfo=UTC)), "close_1530_1600")
        self.assertEqual(time_bucket(datetime(2026, 11, 2, 22, 0, tzinfo=UTC)), "extended_hours")


class MistakeAdapterTests(unittest.TestCase):
    def test_stop_exit_with_slippage_is_labelled(self):
        db = MagicMock()
        db.scalars.return_value.all.return_value = []
        result = classify_round_trip(db, {
            "symbol": "MSFT", "entered_at": "2026-10-29T15:45:00+00:00", "exited_at": "2026-10-29T18:00:00+00:00",
            "entry_price": 200.0, "exit_price": 190.0, "quantity": 5, "fees": 0.0, "stop_fraction": 0.03,
            "exit_reason": "stop_loss_leg",
        })
        self.assertEqual(result["status"], "loss")
        self.assertIn("stop_slippage", result["mistakes"])


class ScorecardApiAndScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.tmp.name) / "api.sqlite"),
                                    connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        app = FastAPI()
        app.add_middleware(AuthenticationMiddleware, configuration=Settings(
            _env_file=None, auth_viewer_key="v" * 32, auth_researcher_key="r" * 32,
            auth_operator_key="o" * 32, auth_admin_key="a" * 32))
        app.include_router(router, prefix="/api")

        def db_session():
            with Session(self.engine) as db:
                yield db

        app.dependency_overrides[get_db] = db_session
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def test_scorecard_route_is_viewer_read_only(self):
        self.assertEqual(required_role("GET", "/stock-paper/scorecard"), "viewer")
        self.assertEqual(required_role("POST", "/stock-paper/scorecard"), "admin")
        response = self.client.get("/api/stock-paper/scorecard?days=7", headers={"Authorization": "Bearer " + "v" * 32})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "unavailable")
        self.assertEqual(body["history"], [])
        self.assertTrue(body["paper_only"])
        self.assertIn("paused_session_minutes", body["operations_today"])
        self.assertIn("watchdog_pauses", body["operations_today"])
        self.assertEqual(self.client.get("/api/stock-paper/scorecard").status_code, 401)
        self.assertEqual(self.client.get("/api/stock-paper/scorecard?days=0",
                                         headers={"Authorization": "Bearer " + "v" * 32}).status_code, 422)

    def test_daily_snapshot_is_a_weekday_wall_clock_crontab(self):
        entry = celery_app.conf.beat_schedule["paper-trade-scorecard-snapshot"]
        self.assertEqual(entry["task"], "app.tasks.jobs.paper_trade_scorecard_snapshot_job")
        self.assertIsInstance(entry["schedule"], crontab)
        self.assertEqual(entry["schedule"].hour, {16})
        self.assertEqual(entry["schedule"].minute, {20})
        self.assertEqual(entry["schedule"].day_of_week, {1, 2, 3, 4, 5})
        self.assertEqual(celery_app.conf.timezone, "America/New_York")
        self.assertEqual(celery_app.conf.task_routes["app.tasks.jobs.paper_trade_scorecard_snapshot_job"],
                         {"queue": "learning"})

    def test_snapshot_job_records_through_run_job(self):
        sentinel = object()
        with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(sentinel)), patch(
            "app.services.paper_trade_scorecard.record_scorecard_snapshot",
            return_value={"status": "complete", "recorded": True},
        ) as record:
            result = jobs.paper_trade_scorecard_snapshot_job()
        record.assert_called_once_with(sentinel, days=30)
        self.assertEqual(result["status"], "complete")


if __name__ == "__main__":
    unittest.main()
