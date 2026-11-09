"""Short-lived, single-flight cache for expensive read-only endpoints.

On the hosted deployment a few report endpoints take 15-60s each (scorecard
bootstrap, weekly-report preview, deployment monitor, live operations). The
dashboard polls them, and several open tabs or one impatient user could pile
identical requests onto the small threadpool and database pool until the
whole API stopped answering (2026-11-09 11:07 ET).

``cached_read`` makes at most one computation per key run at a time. While
one runs, every other caller gets the last stored value (if any) at once
instead of starting another computation. Values expire after ``ttl_seconds``.

The cache is active only in production: local and test runs keep computing
fresh results, so tests that mutate state and re-read are unaffected.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from app.core.config import settings

_entries: dict[str, tuple[float, Any]] = {}
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def cached_read(key: str, ttl_seconds: float, compute: Callable[[], Any]) -> Any:
    if settings.environment != "production":
        return compute()
    now = time.monotonic()
    entry = _entries.get(key)
    if entry and now - entry[0] < ttl_seconds:
        return entry[1]
    lock = _lock_for(key)
    if not lock.acquire(blocking=entry is None):
        # Another request is already recomputing; serve the stale value.
        return entry[1]
    try:
        # A caller that waited for the first computation finds it stored.
        entry = _entries.get(key)
        if entry and time.monotonic() - entry[0] < ttl_seconds:
            return entry[1]
        value = compute()
        _entries[key] = (time.monotonic(), value)
        return value
    finally:
        lock.release()


def invalidate(key: str) -> None:
    """Drop one key after a write, so the next read is computed fresh."""
    _entries.pop(key, None)


def clear() -> None:
    with _guard:
        _entries.clear()
