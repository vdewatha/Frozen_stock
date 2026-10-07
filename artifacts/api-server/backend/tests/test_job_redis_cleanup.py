from unittest.mock import Mock, patch

import pytest

from app.tasks import jobs


@pytest.mark.parametrize("failure", [None, "ping", "acquire"])
def test_unused_lease_client_is_closed_and_work_cannot_run(failure):
    client = Mock()
    client.lock.return_value.acquire.return_value = False
    if failure == "ping":
        client.ping.side_effect = jobs.redis.ConnectionError("private-connection")
    elif failure == "acquire":
        client.lock.return_value.acquire.side_effect = jobs.redis.ConnectionError("private-connection")
    with patch.object(jobs.redis.Redis, "from_url", return_value=client), patch.object(jobs.time, "sleep"):
        if failure:
            with pytest.raises(RuntimeError, match="coordination is unavailable"):
                jobs._acquire_job_lock("stock_monitoring_job")
        else:
            assert jobs._acquire_job_lock("stock_monitoring_job") is None
    client.close.assert_called_once_with()
    client.lock.return_value.release.assert_not_called()


@pytest.mark.parametrize("work_fails", [False, True])
@pytest.mark.parametrize("release_fails", [False, True])
@pytest.mark.parametrize("close_fails", [False, True])
def test_owned_lease_cleanup_preserves_result_and_reports_failures(
    work_fails, release_fails, close_fails, caplog,
):
    client = Mock()
    lock = client.lock.return_value
    lock.redis = client
    lock.acquire.return_value = True
    if release_fails:
        lock.release.side_effect = jobs.redis.lock.LockNotOwnedError("private-owner-token")
    if close_fails:
        client.close.side_effect = RuntimeError("private-close-detail")
    db = Mock()
    db.get_bind.return_value.dialect.name = "sqlite"
    original = ValueError("private-work-detail")

    def work(_db):
        # The client must stay alive until the owner releases the lease.
        client.close.assert_not_called()
        if work_fails:
            raise original
        return {"status": "complete"}

    with patch.object(jobs.redis.Redis, "from_url", return_value=client), patch.object(
        jobs, "SessionLocal", return_value=db
    ), patch.object(jobs, "create_notification"), patch.object(
        jobs, "resolve_successful_job_notifications", return_value=False
    ):
        if work_fails:
            with pytest.raises(ValueError) as caught:
                jobs._run_job("stock_monitoring_job", work)
            assert caught.value is original
        else:
            assert jobs._run_job("stock_monitoring_job", work) == {"status": "complete"}
    lock.release.assert_called_once_with()
    client.close.assert_called_once_with()
    client.delete.assert_not_called()
    db.close.assert_called_once_with()
    assert ("Redis lease cleanup failed" in caplog.text) == release_fails
    assert ("Redis client cleanup failed" in caplog.text) == close_fails
    assert "private-" not in caplog.text
