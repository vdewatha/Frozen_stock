from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.services import scalp_research_runner as runner


NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)


def test_configured_scalp_symbols_normalizes_and_deduplicates():
    assert runner.configured_scalp_symbols(" spy, AAPL,SPY ") == ["AAPL", "SPY"]


def test_configured_scalp_symbols_rejects_empty_or_unsupported_lanes():
    for configured in (" , ", "AAPL,TSLA"):
        try:
            runner.configured_scalp_symbols(configured)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid symbol configuration accepted: {configured!r}")


def test_scalp_runner_rejects_unknown_symbols_before_redis():
    client = Mock()
    result = runner.run_scalp_research_once("TSLA", now=NOW, redis_client=client)
    assert result == {"status": "unsupported_symbol", "symbol": "TSLA", "execution_eligible": False}
    client.ping.assert_not_called()


def test_scalp_runner_uses_symbol_lease_and_never_grants_execution():
    client = Mock()
    lock = Mock()
    lock.acquire.return_value = False
    client.lock.return_value = lock
    with patch.object(runner.settings, "iex_research_enabled", True):
        result = runner.run_scalp_research_once("AAPL", now=NOW, redis_client=client)
    assert result["status"] == "lease_busy"
    assert result["execution_eligible"] is False
    assert client.set.call_args.kwargs["ex"] == runner.HEARTBEAT_TTL_SECONDS
    lock.release.assert_not_called()


def test_scalp_runner_commits_a_paper_research_batch():
    client = Mock()
    lock = Mock()
    lock.acquire.return_value = True
    client.lock.return_value = lock
    db = Mock()
    row = Mock(run_id="research-1")
    result = {"status": "completed", "execution_eligible": False}
    with patch.object(runner.settings, "iex_research_enabled", True), patch(
        "app.services.scalp_research.create_scalp_research_run", return_value=row
    ) as create, patch(
        "app.services.scalp_research.run_scalp_research", return_value=result
    ) as run:
        observed = runner.run_scalp_research_once("AAPL", now=NOW, redis_client=client, db_factory=lambda: db)
    assert observed["symbol"] == "AAPL"
    assert observed["execution_eligible"] is False
    create.assert_called_once_with(db, symbols=["AAPL"], actor="hosted-research-worker")
    run.assert_called_once_with(db, "research-1")
    db.commit.assert_called_once_with()
    db.close.assert_called_once_with()
    lock.release.assert_called_once_with()


def test_scalp_runner_status_reports_each_symbol_heartbeat():
    client = Mock()
    client.get.side_effect = sum(([NOW.isoformat().encode(), None] for _ in range(4)), [])
    with patch.object(runner.settings, "iex_research_enabled", True), patch(
        "redis.Redis.from_url", return_value=client
    ):
        status = runner.scalp_research_runner_status(now=NOW, symbols=["AAPL", "MSFT", "QQQ", "SPY"])
    assert status["alive"] is True
    assert set(status["symbols"]) == {"AAPL", "MSFT", "QQQ", "SPY"}
    assert all(item["alive"] for item in status["symbols"].values())
    assert all(item["last_poll"] is None for item in status["symbols"].values())
