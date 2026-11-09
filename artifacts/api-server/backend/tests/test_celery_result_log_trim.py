"""Celery's per-task success log line is trimmed (see celery_app.TASK_RESULT_LOG_CHARS)."""
from celery.utils.saferepr import saferepr

from app.tasks import jobs  # noqa: F401  (registers the tasks on the app)
from app.tasks.celery_app import TASK_RESULT_LOG_CHARS, celery_app


def test_every_registered_task_trims_its_logged_result():
    tasks = [task for name, task in celery_app.tasks.items() if not name.startswith("celery.")]
    assert tasks, "no application tasks registered"
    assert all(task.resultrepr_maxsize == TASK_RESULT_LOG_CHARS for task in tasks)


def test_trimmed_repr_stays_short_for_a_large_result():
    result = {"results": [{"symbol": f"S{i}", "rows_imported": i, "status": "complete"} for i in range(500)]}
    assert len(saferepr(result, TASK_RESULT_LOG_CHARS)) <= TASK_RESULT_LOG_CHARS + 16
