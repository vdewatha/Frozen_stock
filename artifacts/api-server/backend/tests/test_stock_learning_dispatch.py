from app.models import StockLearningWorkerDispatch
from app.tasks.jobs import _claim_learning_dispatch


class FakeDispatchSession:
    def __init__(self):
        self.row = StockLearningWorkerDispatch(
            id=1,
            batch_id="batch-1",
            symbol="AAPL",
            strategy_slug="ensemble",
            status="published",
            attempt_count=0,
        )
        self.commits = 0

    def scalar(self, _query):
        return self.row

    def commit(self):
        self.commits += 1


def test_learning_dispatch_claim_fences_concurrent_delivery():
    db = FakeDispatchSession()

    claimed, status = _claim_learning_dispatch(db, 1, "task-a")
    assert claimed is db.row
    assert status == "claimed"
    assert db.row.status == "running"
    assert db.row.attempt_count == 1
    assert db.commits == 1

    duplicate, status = _claim_learning_dispatch(db, 1, "task-b")
    assert duplicate is db.row
    assert status == "in_progress"
    assert db.row.attempt_count == 1
    assert db.commits == 1


def test_learning_dispatch_same_task_can_reclaim_after_worker_loss():
    db = FakeDispatchSession()
    _claim_learning_dispatch(db, 1, "task-a")

    reclaimed, status = _claim_learning_dispatch(db, 1, "task-a")
    assert reclaimed is db.row
    assert status == "claimed"
    assert db.row.attempt_count == 2
    assert db.commits == 2

