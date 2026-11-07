"""Exploration lane: liquidity screen, the pre-registered selection, admission/expiry, lane slots and caps."""
import math
import tempfile
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401  register all tables
from app.core.config import settings
from app.db.base import Base
from app.models import Asset, AuditLog, RiskRule, Strategy, StrategySignal
from app.models.stock_paper import StockPaperAccount, StockPaperPosition
from app.services import exploration_universe as lane
from app.services import point_in_time_universe as pit
from app.services.entry_protections import ExplorationDateCaps, load_exploration_date_caps
from app.services.stock_paper_ledger import EXPLORATION_SOURCE, StockPaperError, reserve_stock_paper_order

UTC = timezone.utc
NOW = datetime(2026, 11, 9, 14, 0, tzinfo=UTC)


def bars(n=60, *, price=100.0, ratio=1.005, volume=1_000_000, start="2026-06-01", drift=0.0, seed=None):
    """Daily bars with a fixed high/low ratio (no overnight gaps: open = previous close)."""
    dates = pd.bdate_range(start, periods=n)
    rng = np.random.default_rng(seed) if seed is not None else None
    closes = price * np.cumprod(1 + (rng.normal(drift, 0.01, n) if rng is not None else np.full(n, drift)))
    root = math.sqrt(ratio)
    return pd.DataFrame({"date": dates, "open": closes, "high": closes * root, "low": closes / root,
                         "close": closes, "volume": np.full(n, float(volume))})


# --------------------------------------------------------------- liquidity

def test_corwin_schultz_matches_the_closed_form_for_identical_bars():
    frame = bars(5, ratio=1.02)
    x = math.log(1.02)
    expected = 2 * (math.exp(x) - 1) / (1 + math.exp(x))
    estimate = pit.corwin_schultz(frame)
    assert np.isnan(estimate[0])
    assert estimate[1:] == pytest.approx([expected] * 4, rel=1e-9)


def test_corwin_schultz_floors_negative_estimates_and_shifts_gaps():
    frame = bars(3, ratio=1.0)            # zero range: zero spread
    assert pit.corwin_schultz(frame)[1:] == pytest.approx([0.0, 0.0])
    gap = bars(2, ratio=1.02)
    gap.loc[1, ["high", "low", "close", "open"]] *= 1.10   # a 10% overnight gap
    estimate = pit.corwin_schultz(gap)[1]
    assert math.isfinite(estimate) and 0.0 <= estimate < 0.02  # the gap is not read as spread


def test_liquidity_frame_uses_a_full_window_median():
    frame = bars(25, volume=1_000)
    frame.loc[24, "volume"] = 10**9  # one spike does not move the median
    liquid = pit.liquidity_frame(frame, window=20)
    assert liquid["dollar_volume"].iloc[:19].isna().all()
    assert liquid["dollar_volume"].iloc[-1] == pytest.approx(1_000 * frame["close"].iloc[-1], rel=0.05)
    assert liquid["spread"].iloc[-1] > 0


def test_terciles_rank_members_only_and_never_etfs():
    dates = pd.bdate_range("2026-01-01", periods=3)
    dollar = pd.DataFrame({"A": 6.0, "B": 5.0, "C": 4.0, "D": 3.0, "E": 2.0, "F": 1.0, "SPY": 99.0, "G": np.nan},
                          index=dates)
    terciles = pit.tercile_panel(dollar)
    assert terciles.loc[dates[0], ["A", "B", "C", "D", "E", "F"]].tolist() == [1, 1, 2, 2, 3, 3]
    assert "SPY" not in terciles.columns and terciles.loc[dates[0], "G"] == 0
    members = pit.Membership.from_frame(pd.DataFrame({
        "ticker": ["A", "B", "C", "D", "E", "F"], "start": pd.Timestamp("2020-01-01"),
        "end": [pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.NaT, pd.Timestamp("2026-01-02")]}))
    masked = pit.tercile_panel(dollar, members)
    assert masked.loc[dates[0], "F"] == 3 and masked.loc[dates[1], "F"] == 0   # out from its end date
    assert masked.loc[dates[1], ["A", "B", "C", "D", "E"]].tolist() == [1, 2, 2, 3, 3]
    assert pit.tercile_on(masked, "A", [dates[1]]).tolist() == [1]
    assert pit.tercile_on(masked, "ZZZ", [dates[0]]).tolist() == [0]


# ---------------------------------------------------------- the rule

def _row(e15, e20, spread=0.005):
    return {"expectancy_bps": {"2015+": e15, "2020+": e20}, "spread": spread}


def test_select_universe_takes_the_largest_run_of_qualifying_terciles():
    core = _row(10.0, 8.0)
    picked = lane.select_universe({1: _row(6.0, 4.0), 2: _row(5.5, 3.5), 3: _row(-20.0, 3.0)}, core, max_spread=0.01)
    assert picked["terciles"] == 2 and picked["exploration_universe"] == "liquid_pit"
    assert [d["qualifies"] for d in picked["decisions"]] == [True, True, False]


def test_select_universe_needs_both_windows_the_ceiling_and_the_top_tercile():
    core = _row(10.0, 8.0)
    # T1 fails 2020+ by more than 5 bps: nothing is admitted even though T2 qualifies.
    closed = lane.select_universe({1: _row(9.0, 2.9), 2: _row(10.0, 8.0), 3: _row(10.0, 8.0)}, core, max_spread=0.01)
    assert closed["terciles"] == 0 and closed["exploration_universe"] == "core"
    assert "2020+" in closed["decisions"][0]["reason"]
    wide = lane.select_universe({1: _row(12.0, 9.0, spread=0.0101)}, core, max_spread=0.01)
    assert wide["terciles"] == 0 and "ceiling" in wide["decisions"][0]["reason"]
    missing = lane.select_universe({1: _row(None, 9.0)}, core, max_spread=0.01)
    assert missing["terciles"] == 0 and "no trades" in missing["decisions"][0]["reason"]
    edge = lane.select_universe({1: _row(5.0, 3.0)}, core, max_spread=0.01)  # exactly 5 bps below: qualifies
    assert edge["terciles"] == 1


# -------------------------------------------------------------- screen

def test_screen_admits_liquid_tradable_names_and_logs_the_one_share_skip():
    # Dollar volume: AAPL 9.9B, BIG 900M, WIDE 800M, MID 700M, DEAR 600M, LOW 100M -> terciles 1,1,2,2,3,3.
    frames = {
        "AAPL": bars(30, volume=99_000_000), "BIG": bars(30, volume=9_000_000),
        "WIDE": bars(30, volume=8_000_000, ratio=1.05), "MID": bars(30, volume=7_000_000),
        "DEAR": bars(30, price=2_000, volume=300_000), "LOW": bars(30, volume=1_000_000),
    }
    rows = {row["symbol"]: row for row in lane.screen_names(
        [*frames, "GONE", "SPY"], frames, core={"AAPL"}, terciles=3, max_spread=0.01, max_price=1_500, min_price=5)}
    assert [rows[s]["tercile"] for s in frames] == [1, 1, 2, 2, 3, 3]
    assert rows["BIG"]["admitted"] and rows["MID"]["admitted"] and rows["LOW"]["admitted"]
    assert rows["AAPL"]["reason"] == "core list" and not rows["AAPL"]["admitted"]
    assert "spread proxy" in rows["WIDE"]["reason"]
    assert rows["DEAR"]["reason"].startswith("one share") and not rows["DEAR"]["admitted"]
    assert rows["GONE"]["reason"] == "no daily history in the cache" and "SPY" not in rows
    counts = lane._reason_counts(rows.values())
    assert counts["one share above the learning floor"] == 1 and counts["spread above the ceiling"] == 1
    top = {row["symbol"]: row for row in lane.screen_names(
        list(frames), frames, core={"AAPL"}, terciles=1, max_spread=0.01, max_price=1_500, min_price=5)}
    assert [s for s, row in top.items() if row["admitted"]] == ["BIG"]
    assert "outside the admitted top 1" in top["MID"]["reason"]


def test_assign_sectors_follows_the_residual_correlation():
    rng = np.random.default_rng(7)
    n = 300
    dates = pd.bdate_range("2025-01-01", periods=n)
    market = rng.normal(0, 0.01, n)
    tech, energy = rng.normal(0, 0.01, n), rng.normal(0, 0.01, n)

    def frame(returns):
        return pd.DataFrame({"date": dates, "close": 100 * np.cumprod(1 + returns)})

    spy = frame(market)
    etfs = {"XLK": frame(market + tech), "XLE": frame(market + energy), "XLF": None}
    names = {"NEWTECH": frame(market + tech + rng.normal(0, 0.005, n)),
             "DRILLER": frame(market + energy + rng.normal(0, 0.005, n)),
             "SHORT": frame(market)[:50], "NVDA": frame(market + energy)}
    sectors = lane.assign_sectors(names, etfs, spy, known={"NVDA": "XLK"})
    assert sectors == {"NEWTECH": "XLK", "DRILLER": "XLE", "SHORT": "SHORT", "NVDA": "XLK"}
    assert lane.sector_for("NEWTECH", sectors) == "XLK" and lane.sector_for("JPM") == "XLF"
    assert lane.sector_for("UNKNOWN", sectors) == "UNKNOWN"


def test_buy_candidates_need_a_last_bar_buy_and_the_market_gate():
    frames = {"UP": bars(80), "FLAT": bars(80)}

    def entries(strategy, frame, replay):
        out = np.zeros(len(frame), dtype=bool)
        out[-1] = frame is frames["UP"]
        return out

    with patch("app.services.meta_label_primary.replay_signals", return_value={}), \
            patch("app.services.gate_replay.entries", side_effect=entries):
        assert lane.buy_candidates(frames, ["macd_momentum", "rsi2_pullback"]) == {"UP": ["macd_momentum"]}
        falling = bars(80, drift=-0.01)
        gated = lane.buy_candidates(frames, ["macd_momentum"], spy=falling,
                                    setups={"macd_momentum": {"market_gate_sma": 50}})
        assert gated == {}


# ------------------------------------------------------- admission / expiry

@pytest.fixture
def engine():
    tmp = tempfile.TemporaryDirectory()
    eng = create_engine("sqlite:///" + str(Path(tmp.name) / "lane.sqlite"))
    Base.metadata.create_all(eng)
    lane.clear_cache()
    yield eng
    eng.dispose()
    tmp.cleanup()
    lane.clear_cache()


def _membership(symbols):
    return pit.Membership.from_frame(pd.DataFrame({"ticker": list(symbols), "start": pd.Timestamp("2020-01-01"),
                                                   "end": pd.NaT}))


def _run(db, *, now, firing, frames):
    with patch.object(lane, "buy_candidates", return_value=firing), \
            patch("app.services.news_discovery._max_price", return_value=1_500.0):
        return lane.run_exploration_universe(
            db, now=now, loader=lambda symbol: frames[symbol], membership=_membership(
                [s for s in frames if s not in ("SPY",)]),
            strategies=["macd_momentum"], setups={}, import_prices=lambda _db, _symbol: None)


def test_admission_activates_candidates_and_expiry_keeps_held_names_for_exits(engine):
    frames = {"AAA": bars(40, volume=9_000_000), "BBB": bars(40, volume=8_000_000),
              "CCC": bars(40, volume=7_000_000), "DDD": bars(40, volume=100_000), "SPY": bars(40)}
    with Session(engine) as db, patch.object(settings, "exploration_universe", "liquid_pit"), \
            patch.object(settings, "exploration_universe_terciles", 2), \
            patch.object(settings, "exploration_universe_max_candidates", 1), \
            patch.object(settings, "paper_execution_symbols", ["SPY"]):
        payload = _run(db, now=NOW, firing={"AAA": ["macd_momentum"], "BBB": ["macd_momentum"]}, frames=frames)
        assert [row["symbol"] for row in payload["candidates"]] == ["AAA"]   # the minute-bar cap keeps the most liquid
        assert set(payload["admitted"]) >= {"AAA", "BBB"} and "DDD" not in payload["admitted"]
        assert payload["managed"] == ["AAA"]
        asset = db.query(Asset).filter_by(symbol="AAA").one()
        assert asset.is_active and asset.sector == lane.ASSET_SECTOR
        admitted, exits = lane.lane_universe(db, NOW + timedelta(hours=1))
        assert set(admitted) == {"AAA"} and exits == set()
        assert lane.lane_book(db) == {"AAA"}
        # The admission lapses after admit_hours; a held name stays for exits only.
        later = NOW + timedelta(hours=settings.exploration_universe_admit_hours + 1)
        admitted, exits = lane.lane_universe(db, later)
        assert admitted == {} and exits == {"AAA"}
        from app.services.stock_paper_ledger import initialize_stock_paper_account
        from tests.test_stock_paper_ledger import FakeAlpaca

        initialize_stock_paper_account(db, FakeAlpaca())
        account = db.query(StockPaperAccount).one()
        db.add(StockPaperPosition(account_id=account.id, symbol="AAA", quantity=Decimal("2"), observed_at=NOW,
                                  raw_payload={}))
        db.commit()
        payload = _run(db, now=later, firing={"BBB": ["macd_momentum"]}, frames=frames)
        assert [row["symbol"] for row in payload["candidates"]] == ["BBB"]
        assert payload["holding"] == ["AAA"] and set(payload["managed"]) == {"AAA", "BBB"}
        db.query(StockPaperPosition).delete()
        db.commit()
        payload = _run(db, now=later + timedelta(days=1), firing={}, frames=frames)
        assert payload["candidates"] == [] and payload["holding"] == [] and payload["managed"] == []
        assert not db.query(Asset).filter_by(symbol="AAA").one().is_active


def test_lane_closed_on_core_but_held_names_keep_their_exits(engine):
    with Session(engine) as db:
        assert lane.run_exploration_universe(db, now=NOW)["status"] == "skipped"
        assert db.query(AuditLog).count() == 0
        from app.services.audit import write_audit_log

        write_audit_log(db, event_type=lane.EVENT_TYPE, entity_type="asset", action=lane.ACTION, status="complete",
                        message="x", payload={"candidates": [{"symbol": "AAA", "expires_at": (NOW + timedelta(days=1)).isoformat()}],
                                              "holding": ["OLD"], "managed": ["AAA"]})
        db.commit()
        admitted, exits = lane.lane_universe(db, NOW)   # setting "core": no candidate, both exits-only
        assert admitted == {} and exits == {"AAA", "OLD"}


def test_intraday_universe_includes_lane_names_only_through_the_cache():
    from app.services import trading_universe

    with patch("app.services.exploration_universe.lane_symbols", return_value=frozenset({"LANE1"})), \
            patch("app.services.news_discovery.discovered_symbols", return_value=frozenset()):
        assert "LANE1" in trading_universe.intraday_symbols()
    with patch("app.services.exploration_universe.lane_symbols", return_value=frozenset()), \
            patch("app.services.news_discovery.discovered_symbols", return_value=frozenset()):
        assert "LANE1" not in trading_universe.intraday_symbols()


# ------------------------------------------------------------- date caps

def test_exploration_date_caps_per_strategy_and_sector():
    caps = ExplorationDateCaps(per_strategy=2, per_sector=3)
    caps.record("macd_momentum", "XLK")
    assert caps.check("macd_momentum", "XLK") is None
    caps.record("macd_momentum", "XLK")
    blocked = caps.check("macd_momentum", "XLF")
    assert blocked["protection"] == "exploration_date_cap" and "per strategy" in blocked["reason"]
    caps.record("channel_breakout", "XLK")
    assert "sector XLK" in caps.check("trend_pullback", "XLK")["reason"]
    assert caps.check("trend_pullback", "XLE") is None
    assert ExplorationDateCaps().check("x", "y") is None            # 0 disables both
    assert ExplorationDateCaps(load_error="boom").check("x", "y")["protection"] == "exploration_date_cap"


def test_load_exploration_date_caps_counts_this_sessions_live_exploration_buys():
    opened = NOW - timedelta(hours=1)
    orders = [
        SimpleNamespace(strategy_id=1, symbol="AAA", status="accepted", created_at=NOW - timedelta(minutes=5)),
        SimpleNamespace(strategy_id=1, symbol="BBB", status="cancelled", created_at=NOW - timedelta(minutes=5)),
        SimpleNamespace(strategy_id=2, symbol="AAPL", status="filled", created_at=NOW - timedelta(minutes=5)),
        SimpleNamespace(strategy_id=1, symbol="CCC", status="filled", created_at=opened - timedelta(hours=2)),
    ]

    class Query:
        def filter(self, *args):
            return self

        def all(self):
            return orders

    db = SimpleNamespace(query=lambda model: Query(), rollback=lambda: None)
    caps = load_exploration_date_caps(db, SimpleNamespace(id=1), now=NOW, type_by_id={1: "macd", 2: "trend"},
                                      sector_of=lambda s: {"AAA": "XLK", "AAPL": "XLK"}.get(s, s),
                                      bounds_for=lambda day: (opened, NOW + timedelta(hours=5)),
                                      per_strategy=3, per_sector=3)
    assert caps.by_strategy == {"macd": 1, "trend": 1} and caps.by_sector == {"XLK": 2}


# ---------------------------------------------------------- ledger slots

LANE_STATE = {"candidates": [{"symbol": "LANE", "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
                             {"symbol": "LANE2", "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}],
              "holding": [], "managed": ["LANE", "LANE2"]}


class TestLedgerLane:
    @pytest.fixture(autouse=True)
    def _setup(self, engine):
        from tests.test_paper_exploration import session_window_around

        self.engine = engine
        self.now = datetime.now(UTC)
        self.stack = ExitStack()
        for item in (
            patch.object(settings, "active_paper_broker", "alpaca_paper"),
            patch.object(settings, "paper_broker_account_id", ""),
            patch.object(settings, "paper_exploration_enabled", True),
            patch.object(settings, "paper_exploration_max_positions", 1),
            patch.object(settings, "exploration_universe", "liquid_pit"),
            patch.object(settings, "exploration_max_open", 1),
            patch("app.services.exploration_universe.latest_state", return_value=LANE_STATE),
            patch("app.services.stock_paper_ledger.session_bounds", return_value=session_window_around(self.now)),
            patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}),
            patch("app.services.stock_paper_ledger._validate_reference_price"),
            patch("app.services.stock_learning_cycle.evaluate_launch_admission_prerequisites",
                  return_value={"all": {"status": "pass"}}),
        ):
            self.stack.enter_context(item)
        yield
        self.stack.close()

    def seed(self, db, symbol, status="paper_trading_active", strategy_type="macd_momentum"):
        from app.services.stock_paper_ledger import initialize_stock_paper_account
        from tests.test_stock_paper_ledger import FakeAlpaca

        if not db.query(StockPaperAccount).first():
            initialize_stock_paper_account(db, FakeAlpaca())
            db.add(RiskRule(name="lane", value={"max_open_positions": 1}))
        strategy = Strategy(name=f"{strategy_type} {status} {symbol}", strategy_type=f"{strategy_type}_{symbol}",
                            parameters={}, is_active=True, current_status=status)
        db.add(strategy)
        db.flush()
        signal = StrategySignal(strategy_id=strategy.id, symbol=symbol, signal_time=self.now, action="BUY",
                                probability_up=Decimal("0.8"), probability_down=Decimal("0.2"),
                                confidence=Decimal("0.8"), features={})
        db.add(signal)
        db.commit()
        return db.query(StockPaperAccount).one(), signal

    def reserve(self, db, signal, symbol, key):
        account = db.query(StockPaperAccount).one()
        quantity = (account.equity * Decimal(str(settings.paper_exploration_risk_fraction)) / Decimal("100")).quantize(
            Decimal("0.000001"))
        return reserve_stock_paper_order(db, symbol=symbol, side="buy", quantity=quantity,
                                         reference_price=Decimal("100"), idempotency_key=key,
                                         source=EXPLORATION_SOURCE, signal_id=signal.id)

    def test_active_strategy_explores_a_lane_candidate_in_its_own_slots(self):
        with Session(self.engine) as db:
            account, core_signal = self.seed(db, "SPY", status="paper_trading_candidate")
            self.reserve(db, core_signal, "SPY", "core-1")               # the core's only exploration lot
            db.add(StockPaperPosition(account_id=account.id, symbol="QQQ", quantity=Decimal("1"),
                                      observed_at=self.now, raw_payload={}))  # the core's only open slot
            db.commit()
            _a, lane_signal = self.seed(db, "LANE")
            order = self.reserve(db, lane_signal, "LANE", "lane-1")        # core caps full; lane slot free
            assert order.source == EXPLORATION_SOURCE and order.evidence_id is None
            _a, second = self.seed(db, "LANE2")
            with pytest.raises(StockPaperError, match="lane slots are full"):
                self.reserve(db, second, "LANE2", "lane-2")

    def test_active_strategy_still_cannot_explore_off_the_lane(self):
        with Session(self.engine) as db:
            _account, signal = self.seed(db, "MSFT")
            with pytest.raises(StockPaperError, match="candidate or research"):
                self.reserve(db, signal, "MSFT", "off-lane")

    def test_lane_closed_by_the_setting_refuses_active_strategies(self):
        with Session(self.engine) as db, patch.object(settings, "exploration_universe", "core"):
            _account, signal = self.seed(db, "LANE")
            with pytest.raises(StockPaperError, match="candidate or research"):
                self.reserve(db, signal, "LANE", "closed")


# ---------------------------------------------------------------- cycle

class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None

    def one_or_none(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)


class _Db:
    def __init__(self, strategies):
        self.strategies = strategies

    def query(self, model):
        return _Query(self.strategies if getattr(model, "__name__", "") == "Strategy" else [])

    def rollback(self):
        pass

    def commit(self):
        pass


def _cycle(state, *, strategies, prices, exploration=True, per_strategy=3, per_sector=10):
    from app.services.stock_paper_autotrader import run_stock_paper_signal_cycle
    from tests.test_stock_paper_autotrader import signal_source

    account = SimpleNamespace(id=1, status="reconciled", reconciliation_required=False,
                              unexplained_residual=False, equity=Decimal("100000"), cash=Decimal("100000"))
    order = SimpleNamespace(id=9, status="accepted")
    research = strategies[0].current_status != "paper_trading_active"
    with patch.object(settings, "paper_execution_symbols", ["AAPL"]), \
         patch.object(settings, "paper_exploration_enabled", exploration), \
         patch.object(settings, "exploration_universe", "liquid_pit"), \
         patch.object(settings, "exploration_max_entries_per_strategy_per_date", per_strategy), \
         patch.object(settings, "exploration_max_entries_per_sector_per_date", per_sector), \
         patch.object(settings, "paper_one_position_per_symbol", True), \
         patch("app.services.exploration_universe.latest_state", return_value=state), \
         patch("app.services.stock_paper_autotrader.active_paper_account", return_value=account), \
         patch("app.services.stock_paper_autotrader._is_regular_session", return_value=True), \
         signal_source(research, side_effect=lambda db, symbol, strategy_type: {
             "signal_id": hash((symbol, strategy_type)) % 10**6, "signal_action": "BUY",
             "reference_price": str(prices.get(symbol, 100))}), \
         patch("app.services.stock_paper_autotrader.reserve_stock_paper_order", return_value=order) as reserve, \
         patch("app.services.stock_paper_autotrader.dispatch_reserved_order", return_value=order):
        result = run_stock_paper_signal_cycle(_Db(strategies))
    return result, reserve


def _state(symbols, sectors=None, holding=()):
    expires = (datetime.now(UTC) + timedelta(hours=10)).isoformat()
    return {"candidates": [{"symbol": s, "expires_at": expires, "tercile": 1} for s in symbols],
            "holding": list(holding), "managed": list(symbols), "sectors": dict(sectors or {})}


def test_cycle_sizes_lane_entries_learning_sized_and_caps_them_per_date():
    active = SimpleNamespace(id=3, strategy_type="macd_momentum", current_status="paper_trading_active", parameters={})
    names = ["LA", "LB", "LC", "LD"]
    result, reserve = _cycle(_state(names, holding=["OLD"]), strategies=[active], prices={}, per_strategy=3)
    lane_calls = [call.kwargs for call in reserve.call_args_list if call.kwargs["symbol"] in names]
    assert len(lane_calls) == 3                                   # 3 per strategy per entry date
    for kwargs in lane_calls:
        assert kwargs["source"] == EXPLORATION_SOURCE
        assert kwargs["idempotency_key"].startswith("explore-paper:")
        assert kwargs["quantity"] == Decimal("2")                  # 0.25% of 100k at 100, whole shares
    rows = {row["symbol"]: row for row in result["results"] if row.get("strategy") == "macd_momentum"}
    assert rows["LD"]["protection"] == "exploration_date_cap"
    assert "OLD" in result["symbols"] and "exits only" in rows["OLD"]["reason"]
    assert result["exploration_universe"] == {"candidates": names, "exits_only": ["OLD"]}
    assert result["exploration_date_caps"]["by_strategy"] == {"macd_momentum": 3}


def test_cycle_caps_lane_entries_per_sector_and_skips_a_one_share_name():
    one = SimpleNamespace(id=3, strategy_type="macd_momentum", current_status="paper_trading_active", parameters={})
    two = SimpleNamespace(id=4, strategy_type="trend_pullback", current_status="paper_trading_active", parameters={})
    names = ["SA", "SB", "SC", "DEAR"]
    sectors = {"SA": "XLK", "SB": "XLK", "SC": "XLK", "DEAR": "XLE"}
    result, reserve = _cycle(_state(names, sectors), strategies=[one, two], prices={"DEAR": 2_000}, per_sector=2)
    bought = [call.kwargs["symbol"] for call in reserve.call_args_list if call.kwargs["symbol"] in names]
    assert sorted(bought) == ["SA", "SB"]                          # 2 per sector per date
    rows = [row for row in result["results"] if row.get("symbol") == "SC"]
    assert any(row.get("protection") == "exploration_date_cap" and "sector XLK" in row["reason"] for row in rows)
    dear = [row for row in result["results"] if row.get("symbol") == "DEAR"]
    assert dear and all("below one whole share" in row["reason"] for row in dear)


def test_cycle_never_sizes_a_lane_name_full_when_exploration_is_off():
    active = SimpleNamespace(id=3, strategy_type="macd_momentum", current_status="paper_trading_active", parameters={})
    result, reserve = _cycle(_state(["LA"]), strategies=[active], prices={}, exploration=False)
    assert all(call.kwargs["symbol"] != "LA" for call in reserve.call_args_list)
    row = next(row for row in result["results"] if row.get("symbol") == "LA")
    assert row["status"] == "skipped" and "learning-sized exploration entries only" in row["reason"]


# ------------------------------------------------------- the check's book

def test_book_simulation_applies_the_lane_caps_and_counts_effective_trades():
    from scripts.run_liquid_universe_check import simulate_books

    day = "2026-01-05"
    rows = []
    for i in range(6):  # six lane candidates of one strategy on one date, two sectors
        rows.append({"strategy": "macd_momentum", "symbol": f"L{i}", "decision_date": day, "exit_date": "2026-01-20",
                     "group": "T1"})
    rows.append({"strategy": "macd_momentum", "symbol": "AAPL", "decision_date": day, "exit_date": "2026-01-20",
                 "group": "core"})
    trades = pd.DataFrame(rows)
    sectors = {f"L{i}": ("XLK" if i < 3 else "XLE") for i in range(6)}
    out = simulate_books(trades, lane_groups=("T1",), slots=40, rho=0.5, start="2026-01-01", sectors=sectors,
                         lots_per_session=4, weeks=1.0, technical=("macd_momentum",))
    row = out["strategies"]["macd_momentum"]
    assert row["trades"] == 4 and row["lane_trades"] == 3        # core 1 + lane capped at 3 per strategy per date
    assert row["effective_per_week"] == pytest.approx(4 / (1 + 3 * 0.5))
    tight = simulate_books(trades, lane_groups=("T1",), slots=2, rho=0.0, start="2026-01-01", sectors=sectors,
                           lots_per_session=4, weeks=1.0, technical=("macd_momentum",))
    assert tight["strategies"]["macd_momentum"]["lane_trades"] == 2 and tight["lane_open_max"] == 2
    closed = simulate_books(trades, lane_groups=(), slots=40, rho=0.0, start="2026-01-01", sectors=sectors,
                            lots_per_session=4, weeks=1.0, technical=("macd_momentum",))
    assert closed["strategies"]["macd_momentum"]["lane_trades"] == 0


def test_news_found_names_and_cheap_names_stay_out_of_the_lane(engine):
    frames = {"AAA": bars(40, volume=9_000_000), "NEWS": bars(40, volume=9_500_000),
              "PENNY": bars(40, price=8.0, volume=900_000_000), "SPY": bars(40)}
    with Session(engine) as db, patch.object(settings, "exploration_universe", "liquid_pit"), \
            patch.object(settings, "exploration_universe_terciles", 3), \
            patch.object(settings, "paper_execution_symbols", ["SPY"]), \
            patch("app.services.news_discovery.news_universe", return_value=({"NEWS": {}}, set())):
        payload = _run(db, now=NOW, firing={s: ["macd_momentum"] for s in frames}, frames=frames)
    assert [row["symbol"] for row in payload["candidates"]] == ["AAA"]
    assert "NEWS" not in payload["admitted"] and "PENNY" not in payload["admitted"]
    assert payload["rejected"].get("price below the minimum") == 1
