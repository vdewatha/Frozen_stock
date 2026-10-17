from datetime import datetime, timezone
from unittest.mock import Mock, patch

from app.services import delayed_sip_research_runner as runner


NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)


def test_runner_collects_and_commits_without_execution():
    client = Mock()
    lock = Mock()
    lock.acquire.return_value = True
    client.lock.return_value = lock
    db = Mock()
    collected = {"status": "observed", "results": []}
    with patch.object(runner.settings, "delayed_sip_research_enabled", True), patch(
        "app.services.delayed_sip_research.collect_delayed_sip", return_value=collected
    ) as collect:
        result = runner.run_delayed_sip_collection_once(
            now=NOW, redis_client=client, db_factory=lambda: db
        )
    assert result["execution_eligible"] is False
    collect.assert_called_once_with(db, now=NOW)
    db.commit.assert_called_once_with()
    db.close.assert_called_once_with()
    lock.release.assert_called_once_with()
    client.set.assert_called_once_with(runner.HEARTBEAT_KEY, NOW.isoformat(), ex=runner.LOCK_TTL_SECONDS)


def test_runner_refuses_live_configuration():
    client = Mock()
    with patch.object(runner.settings, "allow_live_trading", True):
        result = runner.run_delayed_sip_collection_once(now=NOW, redis_client=client)
    assert result == {"status": "blocked_live_configuration", "ran": False, "execution_eligible": False}
    client.ping.assert_not_called()


def test_runner_status_reports_fresh_heartbeat():
    client = Mock()
    client.get.return_value = NOW.isoformat().encode()
    with patch.object(runner.settings, "delayed_sip_research_enabled", True), patch(
        "redis.Redis.from_url", return_value=client
    ):
        status = runner.delayed_sip_runner_status(now=NOW)
    assert status == {"enabled": True, "alive": True, "heartbeat_at": NOW.isoformat()}
    client.close.assert_called_once_with()
