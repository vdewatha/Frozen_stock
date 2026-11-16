"""Every queue a task is routed to is declared and has a local consumer.

``learning_fast`` was routed to but missing from ``task_queues``, and the local
launcher started no worker for ``learning_fast``, ``broker_reconciliation`` or
``research_market_data``: locally those tasks were published and never ran.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.tasks.celery_app import (
    BROKER_RECONCILIATION_QUEUE,
    LEARNING_FAST_QUEUE,
    MONITORING_QUEUE,
    RESEARCH_MARKET_DATA_QUEUE,
    celery_app,
)

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "run_local_stack.sh"


def _declared_queues() -> set[str]:
    return {queue.name for queue in celery_app.conf.task_queues}


def _routed_queues() -> set[str]:
    routed = {route["queue"] for route in celery_app.conf.task_routes.values()}
    for entry in celery_app.conf.beat_schedule.values():
        queue = (entry.get("options") or {}).get("queue")
        if queue:
            routed.add(queue)
    return routed


def _launcher_queues(*, direct_monitor_loop: bool) -> set[str]:
    """Queues the launcher's workers consume, read from the script text."""
    source = LAUNCHER.read_text()
    consumed: set[str] = set()
    for literal in re.findall(r"--queues=([a-z_,]+)", source):
        consumed.update(literal.split(","))
    general = re.findall(r'^\s*general_queues="([a-z_,]+)"$', source, flags=re.MULTILINE)
    # One assignment per monitor mode: the direct loop first, beat second.
    assert len(general) == 2, general
    assert MONITORING_QUEUE not in general[0] and MONITORING_QUEUE in general[1]
    consumed.update(general[0 if direct_monitor_loop else 1].split(","))
    extra = re.findall(r'^\s*routed_only_queues="([a-z_,]+)"$', source, flags=re.MULTILINE)
    assert len(extra) == 1, extra
    # Appended in both monitor modes, and the general worker uses the result.
    assert source.count('general_queues+=",${routed_only_queues}"') == 2
    assert '--queues="$general_queues"' in source
    consumed.update(extra[0].split(","))
    return consumed


def test_learning_fast_is_a_declared_queue():
    queue = next(queue for queue in celery_app.conf.task_queues if queue.name == LEARNING_FAST_QUEUE)
    assert queue.exchange.name == LEARNING_FAST_QUEUE
    assert queue.routing_key == LEARNING_FAST_QUEUE


def test_every_routed_queue_is_declared():
    assert _routed_queues() <= _declared_queues()
    assert celery_app.conf.task_default_queue in _declared_queues()


def test_local_launcher_consumes_the_three_queues_it_used_to_miss():
    for direct in (True, False):
        consumed = _launcher_queues(direct_monitor_loop=direct)
        assert {LEARNING_FAST_QUEUE, BROKER_RECONCILIATION_QUEUE, RESEARCH_MARKET_DATA_QUEUE} <= consumed


def test_local_launcher_consumes_every_declared_queue():
    # With the direct monitor loop the launcher runs monitoring as its own
    # process and beat does not schedule stock-monitoring-job.
    assert _launcher_queues(direct_monitor_loop=False) == _declared_queues()
    assert _launcher_queues(direct_monitor_loop=True) == _declared_queues() - {MONITORING_QUEUE}
