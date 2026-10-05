from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import Notification
from app.tasks import jobs


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with patch.object(jobs, "SessionLocal", factory):
        yield factory
    engine.dispose()


@pytest.mark.parametrize("flush", [False, True])
def test_failure_rolls_back_pending_work_but_records_notification(sessions, flush):
    def work(db):
        jobs.create_notification(db, category="fixture", severity="info", source="committed",
                                 title="fixture", message="previous committed evidence")
        db.commit()
        jobs.create_notification(db, category="fixture", severity="info", source="partial",
                                 title="fixture", message="must not survive")
        if flush:
            db.flush()
        raise ValueError("work failed")

    with pytest.raises(ValueError, match="work failed"):
        jobs._run_job("transaction_fixture", work)
    with sessions() as db:
        assert db.query(Notification).filter_by(source="partial").count() == 0
        assert db.query(Notification).filter_by(source="committed").count() == 1
        assert db.query(Notification).filter_by(source="transaction_fixture", message="work failed").count() == 1


def test_notification_failure_preserves_original_and_redacts_log(sessions, caplog):
    original = ValueError("private-original-detail")

    def work(db):
        raise original

    with patch.object(jobs, "create_notification", side_effect=RuntimeError("private-provider-detail")):
        with pytest.raises(ValueError) as caught:
            jobs._run_job("notification_fixture", work)
    assert caught.value is original
    assert "notification unavailable" in caplog.text
    assert "private-original-detail" not in caplog.text
    assert "private-provider-detail" not in caplog.text
