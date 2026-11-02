from datetime import datetime, timezone

import pytest

from scripts.run_local_short_session import collection_needs_refresh


@pytest.mark.parametrize("checked,now,expected", [
    ("2026-10-30T19:01:59+00:00", "2026-10-30T19:01:59.9+00:00", False),
    ("2026-10-30T19:01:59+00:00", "2026-10-30T19:02:00+00:00", True),
    ("2026-10-30T15:01:59-04:00", "2026-10-30T19:01:59.9+00:00", False),
    ("2026-10-30T19:02:00+00:00", "2026-10-30T19:01:59+00:00", True),
    (None, "2026-10-30T19:01:59+00:00", True),
    ("invalid", "2026-10-30T19:01:59+00:00", True),
    ("2026-10-30T19:01:00", "2026-10-30T19:01:59+00:00", True),
])
def test_refresh_real_data_when_batch_crosses_minute_boundary(checked, now, expected):
    assert collection_needs_refresh(checked, datetime.fromisoformat(now)) is expected
