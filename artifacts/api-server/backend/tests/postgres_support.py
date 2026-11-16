"""Helpers for the tests marked ``postgres`` (real row and advisory locks)."""
import time
from concurrent.futures import Future

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

# Generous: PostgreSQL reports a deadlock after one second whatever this is,
# so it only bounds a hang, and a loaded runner can stall a statement for seconds.
WAIT_SECONDS = 30


def backend_pid(db) -> int:
    """The server process serving this session's current transaction."""
    return db.scalar(text("SELECT pg_backend_pid()"))


def wait_until_blocked(owner, waiting_pid: int, blocking_pid: int | None = None, *, timeout: float = WAIT_SECONDS):
    """Return once ``waiting_pid`` waits on a lock (held by ``blocking_pid`` when given)."""
    deadline = time.monotonic() + timeout
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        while time.monotonic() < deadline:
            blockers = connection.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}) or []
            if (blocking_pid in blockers) if blocking_pid is not None else blockers:
                return list(blockers)
            time.sleep(0.01)
    pytest.fail(f"Backend {waiting_pid} never waited on a lock held by {blocking_pid or 'another backend'}")


def wait_until_blocking(owner, blocking_pid: int, *, timeout: float = WAIT_SECONDS) -> list[int]:
    """Return the backends waiting on a lock ``blocking_pid`` holds, once there is one.

    For a waiter whose pid is not known: a session takes a new pooled
    connection after each commit.
    """
    deadline = time.monotonic() + timeout
    with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        while time.monotonic() < deadline:
            waiting = connection.scalars(text(
                "SELECT pid FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND :pid = ANY(pg_blocking_pids(pid))"
            ), {"pid": blocking_pid}).all()
            if waiting:
                return list(waiting)
            time.sleep(0.01)
    pytest.fail(f"No backend ever waited on a lock held by backend {blocking_pid}")


def is_deadlock(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    return isinstance(exc, DBAPIError) and (
        getattr(orig, "sqlstate", None) == "40P01" or type(orig).__name__ == "DeadlockDetected"
    )


class KnownDeadlock(AssertionError):
    """A deadlock a test documents as an open defect (``xfail(strict=True, raises=KnownDeadlock)``)."""


def outcomes_or_known_deadlock(futures: dict[str, Future], *, timeout: float = WAIT_SECONDS) -> dict:
    """Each thread's result by name; raise ``KnownDeadlock`` naming the victim if PostgreSQL chose one."""
    outcomes, victims = {}, []
    for name, future in futures.items():
        try:
            outcomes[name] = future.result(timeout=timeout)
        except TimeoutError:
            pytest.fail(f"{name} did not finish within {timeout} s (still waiting on a lock?)")
        except DBAPIError as exc:
            if not is_deadlock(exc):
                raise
            outcomes[name] = "deadlock victim"
            victims.append(f"{name}: {' '.join(str(exc.orig).split())[:300]}")
    if victims:
        raise KnownDeadlock(f"{outcomes}; {'; '.join(victims)}")
    return outcomes


def result_or_fail(future: Future, what: str, *, timeout: float = WAIT_SECONDS):
    """The thread's result; a deadlock or a hang fails the test naming the thread."""
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        pytest.fail(f"{what} did not finish within {timeout} s (still waiting on a lock?)")
    except DBAPIError as exc:
        if is_deadlock(exc):
            pytest.fail(f"{what} was chosen as a PostgreSQL deadlock victim: {exc.orig}")
        raise
