from datetime import datetime, timedelta, timezone

from app.services.scalp_research import (
    _metrics,
    _session_quality,
    _bar_lineage,
    _validation_metrics,
    create_scalp_research_run,
    expire_stale_scalp_research_runs,
)


def _research_bars(start, count, *, provider="alpaca_iex"):
    from app.models import IntradayBar

    return [IntradayBar(
        symbol="SPY", timeframe="1m", provider=provider, feed_class="iex",
        opened_at=start + timedelta(minutes=index),
        exchange_timestamp=start + timedelta(minutes=index),
        open=100, high=101, low=99, close=100, volume=10,
        last_verified_at=start + timedelta(minutes=index),
    ) for index in range(count)]


def test_scalp_quality_rejects_missing_current_session_minute():
    start = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
    bars = _research_bars(start, 29)
    bars.pop(10)
    quality = _session_quality(bars, now=datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc))
    assert quality["status"] == "blocked"
    assert quality["missing_count"] == 1
    assert quality["reason"].startswith("trusted intraday session is incomplete")


def test_scalp_quality_rejects_stale_session_and_non_provider_data():
    start = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
    bars = _research_bars(start, 390)
    stale = _session_quality(bars, now=datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc))
    assert stale["status"] == "blocked"
    assert stale["reason"] == "latest trusted session is stale"
    synthetic = _session_quality(
        _research_bars(start, 390, provider="synthetic_fallback"),
        now=datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc),
    )
    assert synthetic["status"] == "blocked"
    assert "provenance" in synthetic["reason"]


def test_scalp_quality_accepts_only_contiguous_current_real_session():
    start = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
    quality = _session_quality(
        _research_bars(start, 29),
        now=datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc),
    )
    assert quality["status"] == "ready"
    assert quality["expected_bars"] == 29
    assert quality["authenticated_verification_at"] == "2026-09-28T13:58:00+00:00"


def test_scalp_quality_rejects_unverified_current_session():
    start = datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc)
    bars = _research_bars(start, 29)
    bars[-1].last_verified_at = None
    quality = _session_quality(bars, now=datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc))
    assert quality["status"] == "blocked"
    assert "authenticated feed verification" in quality["reason"]


def test_scalp_quality_rejects_incomplete_older_session():
    first = _research_bars(datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc), 390)
    second = _research_bars(datetime(2026, 9, 29, 13, 30, tzinfo=timezone.utc), 390)
    first.pop(100)
    quality = _session_quality(
        first + second,
        now=datetime(2026, 9, 29, 20, 0, tzinfo=timezone.utc),
    )
    assert quality["status"] == "blocked"
    assert "incomplete regular session" in quality["reason"]
    assert quality["incomplete_sessions"][0]["session_day"] == "2026-09-28"


def test_scalp_metrics_are_separate_and_cost_adjusted():
    values = [100 + index * 0.05 for index in range(80)]
    result = _metrics(values)
    assert set(result) == {
        "ema_momentum",
        "intraday_mean_reversion",
        "rsi_reversion",
        "range_breakout",
        "short_term_reversal",
    }
    assert result["ema_momentum"]["trades"] > 0
    assert result["ema_momentum"]["average_trade"] < 0.01
    assert "profitable_after_costs" in result["ema_momentum"]
    assert result["ema_momentum"]["execution_model"] == "next_close_single_bar_with_modeled_costs"
    assert set(result["ema_momentum"]["modeled_cost_sensitivity"]) == {"5.5bps", "10bps", "20bps"}
    assert "max_drawdown" in result["ema_momentum"]


def test_scalp_holdout_does_not_promote_a_non_unique_winner():
    values = [100 + index * 0.05 for index in range(180)]
    result = _validation_metrics(values)
    assert result["status"] == "complete"
    assert result["holdout_bars"] == 55
    assert result["paper_candidate"] is None
    assert "candidate_reason" in result


def test_scalp_run_rejects_unapproved_symbols():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.models import ScalpResearchRun

    engine = create_engine("sqlite://")
    ScalpResearchRun.__table__.create(engine)
    with Session(engine) as db:
        try:
            create_scalp_research_run(db, symbols=["TSLA"], actor="test")
        except ValueError as exc:
            assert "supports only" in str(exc)
        else:
            raise AssertionError("unapproved symbol was accepted")


def test_stale_scalp_queue_rows_are_blocked_without_replay():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import ScalpResearchRun

    engine = create_engine("sqlite://")
    ScalpResearchRun.__table__.create(engine)
    with Session(engine) as db:
        row = ScalpResearchRun(
            run_id="stale-run", requested_by="test", symbols=["AAPL"],
            timeframe="1m", status="queued", data_snapshot={}, metrics={}, assumptions={},
            created_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(row)
        db.commit()
        result = expire_stale_scalp_research_runs(db)
        db.refresh(row)
        assert result["expired_run_ids"] == ["stale-run"]
        assert row.status == "blocked"
        assert "expired" in row.error


def test_stale_running_scalp_rows_are_blocked_so_scheduler_can_recover():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import ScalpResearchRun

    engine = create_engine("sqlite://")
    ScalpResearchRun.__table__.create(engine)
    with Session(engine) as db:
        row = ScalpResearchRun(
            run_id="crashed-run", requested_by="worker", symbols=["AAPL"],
            timeframe="1m", status="running", data_snapshot={}, metrics={}, assumptions={},
            started_at=datetime.now(timezone.utc) - timedelta(minutes=20),
        )
        db.add(row)
        db.commit()
        result = expire_stale_scalp_research_runs(db)
        db.refresh(row)
        assert result["expired_running_count"] == 1
        assert row.status == "blocked"
        assert "liveness" in row.error


def test_lineage_fingerprint_changes_when_a_bar_changes():
    from app.models import IntradayBar
    from datetime import datetime, timezone

    first = IntradayBar(
        symbol="AAPL", timeframe="1m", provider="alpaca_iex", feed_class="iex",
        opened_at=datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc),
        exchange_timestamp=datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc),
        open=100, high=101, low=99, close=100, volume=10,
    )
    second = IntradayBar(
        symbol="AAPL", timeframe="1m", provider="alpaca_iex", feed_class="iex",
        opened_at=datetime(2026, 9, 28, 13, 31, tzinfo=timezone.utc),
        exchange_timestamp=datetime(2026, 9, 28, 13, 31, tzinfo=timezone.utc),
        open=100, high=101, low=99, close=100, volume=10,
    )
    original = _bar_lineage([first, second])
    second.close = 100.25
    changed = _bar_lineage([first, second])
    assert original["bar_count"] == 2
    assert original["intraday_gap_count"] == 0
    assert original["bars_sha256"] != changed["bars_sha256"]


def test_lineage_records_material_in_session_gap():
    from app.models import IntradayBar
    from datetime import datetime, timezone

    first = IntradayBar(
        symbol="SPY", timeframe="1m", provider="alpaca_iex", feed_class="iex",
        opened_at=datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc),
        exchange_timestamp=datetime(2026, 9, 28, 13, 30, tzinfo=timezone.utc),
        open=100, high=101, low=99, close=100, volume=10,
    )
    second = IntradayBar(
        symbol="SPY", timeframe="1m", provider="alpaca_iex", feed_class="iex",
        opened_at=datetime(2026, 9, 28, 13, 36, tzinfo=timezone.utc),
        exchange_timestamp=datetime(2026, 9, 28, 13, 36, tzinfo=timezone.utc),
        open=100, high=101, low=99, close=100, volume=10,
    )
    lineage = _bar_lineage([first, second])
    assert lineage["intraday_gap_count"] == 1
    assert lineage["intraday_gaps"][0]["gap_seconds"] == 360


def test_scheduled_scalp_research_queues_one_paper_batch_when_idle():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from unittest.mock import Mock, patch
    from app.models import ScalpResearchRun
    from app.tasks import jobs

    engine = create_engine("sqlite://")
    ScalpResearchRun.__table__.create(engine)
    with Session(engine) as db, patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(db)):
        queued = Mock(id="task-1")
        with patch.object(jobs.scalp_research_job, "apply_async", return_value=queued) as apply_async:
            result = jobs.scheduled_scalp_research_job()

        assert result["status"] == "queued"
        assert result["paper_only"] is True
        assert result["eligible_for_trading"] is False
        assert result["symbols"] == ["AAPL", "MSFT", "SPY", "QQQ"]
        assert apply_async.call_count == 4
        assert all(call.kwargs["queue"] == "scalp_research" for call in apply_async.call_args_list)


def test_scheduled_scalp_research_does_not_overlap_active_run():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from unittest.mock import patch
    from app.models import ScalpResearchRun
    from app.tasks import jobs

    engine = create_engine("sqlite://")
    ScalpResearchRun.__table__.create(engine)
    with Session(engine) as db:
        db.add(ScalpResearchRun(
            run_id="active-run", requested_by="scheduler", symbols=["AAPL"],
            timeframe="1m", status="running", data_snapshot={}, metrics={}, assumptions={},
        ))
        db.commit()
        with patch.object(jobs, "_run_job", side_effect=lambda _name, work: work(db)):
            result = jobs.scheduled_scalp_research_job()

        assert result["status"] == "skipped"
        assert result["run_id"] == "active-run"
        assert result["eligible_for_trading"] is False
