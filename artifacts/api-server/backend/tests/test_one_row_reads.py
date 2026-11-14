"""2026-11-13: "the latest row" was read by fetching the whole table.

``db.scalars(select(StockMonitoringSnapshot).order_by(...)).first()`` has no
LIMIT, so the database sent every monitoring snapshot ever recorded (20,424
that day, about 1,000 more a day, each with its checks as JSON) and the caller
kept one.  The live-safety check did it twice per call and runs in the API's
readiness build every five minutes and in the drift-retrain,
parameter-application and strategy-readmission jobs every fifteen: 45 to 80
seconds and up to 1.8 GiB a call.  The memory guard killed paper-core 15 times
that day, each about 45 seconds into those jobs.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.session import OneRowScalarSession, SessionLocal, first_row_only
from app.models.models import Base, StockMonitoringSnapshot
from app.services import drift_retraining, live_pilot, live_safety

APP = Path(__file__).resolve().parents[1] / "app"
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_and_sql():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda _c, _cur, statement, *_rest: statements.append(" ".join(statement.split())))
    with OneRowScalarSession(engine) as session:
        for minutes in (30, 20, 10):          # the newest is added last
            session.add(StockMonitoringSnapshot(
                monitor_key=drift_retraining.MONITOR_KEY, status="clear", generated_at=NOW - timedelta(minutes=minutes),
                checks=[{"key": "feed", "status": "clear"}], actions=[], source="test"))
        session.commit()
        statements.clear()
        yield session, statements


def _snapshot_reads(statements):
    return [s for s in statements if "FROM stock_monitoring_snapshots" in s]


def test_every_latest_snapshot_read_asks_for_one_row(db_and_sql):
    db, statements = db_and_sql
    newest = db.scalar(select(func.max(StockMonitoringSnapshot.id)))
    statements.clear()

    assert live_safety._current_data_gate(db, NOW)["evidence"]["snapshot_id"] == newest
    assert live_safety._monitoring_gate(db, NOW)["evidence"]["snapshot_id"] == newest
    live_pilot._monitoring_gate(db)
    assert drift_retraining.evaluate_drift_trigger(db, now=NOW)["snapshot_id"] == newest

    reads = _snapshot_reads(statements)
    assert len(reads) == 4
    assert all("LIMIT" in s for s in reads), reads


def test_scalar_limits_a_plain_select_and_returns_the_same_row(db_and_sql):
    db, statements = db_and_sql
    latest = select(StockMonitoringSnapshot).order_by(StockMonitoringSnapshot.generated_at.desc())
    with Session(db.get_bind()) as plain:
        expected = plain.scalar(latest).id
    statements.clear()
    assert db.scalar(latest).id == expected
    assert "LIMIT" in _snapshot_reads(statements)[-1]
    assert db.scalar(select(func.count(StockMonitoringSnapshot.id))) == 3   # aggregates are unaffected


def test_statements_that_page_or_lock_are_left_alone():
    plain = select(StockMonitoringSnapshot)
    assert first_row_only(plain)._limit_clause is not None
    for statement in (plain.limit(5), plain.offset(2), plain.with_for_update()):
        assert first_row_only(statement) is statement
    assert first_row_only("SELECT 1") == "SELECT 1"


def test_the_app_session_factory_uses_the_guard():
    assert issubclass(SessionLocal.class_, OneRowScalarSession)   # sessionmaker subclasses it


def _called(node: ast.AST) -> set[str]:
    return {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(node) if isinstance(n, ast.Call)}


def test_no_first_row_read_is_written_without_a_limit():
    """``.scalars(select(...)).first()`` and ``.execute(select(...)).first()`` fetch every matching row."""
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "first"):
                continue
            inner = node.func.value
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr in {"scalars", "execute"} and inner.args):
                called = _called(inner.args[0])
                if "select" in called and "limit" not in called:
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, f"add .limit(1): {offenders}"
