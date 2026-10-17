from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.services import iex_research_runner as runner


NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)


def test_runner_skips_outside_market_window_without_touching_redis():
    client = Mock()
    with patch.object(runner.settings, "iex_research_enabled", True):
        result = runner.run_iex_collection_once(
            now=datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc),
            redis_client=client,
        )
    assert result == {"status": "outside_market_hours", "ran": False, "execution_eligible": False}
    client.ping.assert_called_once_with()
    client.set.assert_called_once_with(runner.HEARTBEAT_KEY, "2026-10-17T15:00:00+00:00", ex=runner.LOCK_TTL_SECONDS)


def test_runner_does_not_collect_when_another_instance_holds_lease():
    client = Mock()
    lock = Mock()
    lock.acquire.return_value = False
    client.lock.return_value = lock
    with patch.object(runner.settings, "iex_research_enabled", True):
        result = runner.run_iex_collection_once(now=NOW, redis_client=client)
    assert result["status"] == "lease_busy"
    assert result["execution_eligible"] is False
    lock.release.assert_not_called()


def test_runner_commits_observation_and_releases_lease_without_execution_imports():
    client = Mock()
    lock = Mock()
    lock.acquire.return_value = True
    client.lock.return_value = lock
    db = Mock()
    collected = {"status": "observed", "execution_eligible": False, "results": []}
    learned = {"status": "advanced"}
    with patch.object(runner.settings, "iex_research_enabled", True), patch(
        "app.services.alpaca_research_data.collect_iex_research", return_value=collected
    ) as collect, patch(
        "app.services.online_research.advance_online_research", return_value=learned
    ) as advance:
        result = runner.run_iex_collection_once(now=NOW, redis_client=client, db_factory=lambda: db)
    assert result["execution_eligible"] is False
    assert result["learning"] == learned
    collect.assert_called_once_with(db, now=NOW)
    advance.assert_called_once_with(db)
    db.commit.assert_called_once_with()
    db.close.assert_called_once_with()
    lock.release.assert_called_once_with()


def test_runner_refuses_live_enabled_configuration():
    client = Mock()
    with patch.object(runner.settings, "allow_live_trading", True):
        result = runner.run_iex_collection_once(now=NOW, redis_client=client)
    assert result == {"status": "blocked_live_configuration", "ran": False, "execution_eligible": False}
    client.ping.assert_not_called()


def test_runner_status_reports_fresh_heartbeat_without_exposing_configuration():
    client = Mock()
    client.get.return_value = NOW.isoformat().encode()
    with patch.object(runner.settings, "iex_research_enabled", True), patch(
        "redis.Redis.from_url", return_value=client
    ):
        status = runner.research_runner_status(now=NOW)
    assert status == {"enabled": True, "alive": True, "heartbeat_at": NOW.isoformat()}
    client.close.assert_called_once_with()
