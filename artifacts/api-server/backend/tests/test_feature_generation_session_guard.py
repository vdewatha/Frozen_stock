from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.tasks import jobs


def test_feature_generation_is_skipped_during_the_session():
    now = datetime.now(timezone.utc)
    with patch("app.services.intraday_data.session_bounds", return_value=(now - timedelta(hours=1), now + timedelta(hours=5))), \
         patch.object(jobs, "_run_job") as run_job:
        result = jobs.daily_feature_generation()
    assert result["status"] == "skipped"
    run_job.assert_not_called()


def test_feature_generation_runs_outside_the_session():
    now = datetime.now(timezone.utc)
    with patch("app.services.intraday_data.session_bounds", return_value=(now + timedelta(hours=3), now + timedelta(hours=9))), \
         patch.object(jobs, "_run_job", return_value={"status": "complete"}) as run_job:
        result = jobs.daily_feature_generation()
    assert result == {"status": "complete"}
    run_job.assert_called_once()
