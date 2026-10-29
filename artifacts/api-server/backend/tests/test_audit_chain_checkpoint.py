"""Incremental audit-chain verification through a Redis checkpoint."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import AuditLog
from app.services import operational_hardening as hardening
from app.services.audit import write_audit_log


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex
        return True

    def close(self):
        pass


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def fake_redis():
    client = FakeRedis()
    with patch.object(hardening, "_checkpoint_redis", return_value=client):
        yield client


def _write(db: Session, count: int, *, start: int = 0) -> None:
    for index in range(start, start + count):
        write_audit_log(db, event_type="operations_probe", action="checkpoint_scan",
                        status="complete", message="Checkpoint verification",
                        payload={"sequence": index})
        db.flush()
    db.commit()


def _rows(db: Session) -> list[AuditLog]:
    return db.scalars(select(AuditLog).order_by(AuditLog.id)).all()


def test_missing_checkpoint_scans_full_chain_then_resumes_incrementally(db, fake_redis):
    _write(db, 20)
    first = hardening._audit_chain_check(db, checkpoint=True)
    assert first["status"] == "clear"
    assert first["details"]["checkpoint_status"] == "missing"
    assert first["details"]["scope"] == "full"
    assert first["details"]["event_count"] == 20
    assert first["details"]["checkpoint_saved"] is True
    stored = json.loads(fake_redis.values[hardening._audit_chain_checkpoint_key()])
    last = _rows(db)[-1]
    assert stored["row_id"] == last.id and stored["event_sha256"] == last.event_sha256
    assert fake_redis.ttls[hardening._audit_chain_checkpoint_key()] > 0

    _write(db, 5, start=20)
    second = hardening._audit_chain_check(db, checkpoint=True)
    assert second["status"] == "clear"
    assert second["details"]["checkpoint_status"] == "used"
    assert second["details"]["scope"] == "incremental"
    assert second["details"]["checkpoint_row_id"] == last.id
    assert second["details"]["event_count"] == 5
    assert second["details"]["verified_through_row_id"] == _rows(db)[-1].id

    # Nothing new: the checkpoint row is still re-checked, zero links hashed.
    third = hardening._audit_chain_check(db, checkpoint=True)
    assert third["status"] == "clear" and third["details"]["event_count"] == 0


def test_tampering_after_checkpoint_is_detected(db, fake_redis):
    _write(db, 10)
    assert hardening._audit_chain_check(db, checkpoint=True)["status"] == "clear"
    _write(db, 5, start=10)
    target = _rows(db)[12]
    target.payload = {"sequence": "tampered"}
    db.commit()
    result = hardening._audit_chain_check(db, checkpoint=True)
    assert result["status"] == "breach"
    assert result["details"]["first_broken_row_id"] == target.id
    assert result["details"]["classification"] == "event_digest_mismatch"
    assert result["details"]["checkpoint_status"] == "used"


def test_breach_does_not_advance_checkpoint(db, fake_redis):
    _write(db, 10)
    hardening._audit_chain_check(db, checkpoint=True)
    before = fake_redis.values[hardening._audit_chain_checkpoint_key()]
    _write(db, 3, start=10)
    _rows(db)[-1].payload = {"sequence": "tampered"}
    db.commit()
    assert hardening._audit_chain_check(db, checkpoint=True)["status"] == "breach"
    assert fake_redis.values[hardening._audit_chain_checkpoint_key()] == before


def test_tampered_checkpoint_row_falls_back_to_full_scan(db, fake_redis):
    _write(db, 10)
    hardening._audit_chain_check(db, checkpoint=True)
    checkpoint_row = _rows(db)[-1]
    checkpoint_row.payload = {"sequence": "tampered"}
    db.commit()
    result = hardening._audit_chain_check(db, checkpoint=True)
    assert result["status"] == "breach"
    assert result["details"]["checkpoint_status"] == "digest_mismatch"
    assert result["details"]["first_broken_row_id"] == checkpoint_row.id


def test_checkpoint_with_foreign_digest_falls_back_to_full_scan(db, fake_redis):
    _write(db, 10)
    rows = _rows(db)
    rows[3].payload = {"sequence": "tampered"}
    db.commit()
    # A checkpoint from another chain (or a forged one) names a real row id
    # but a digest the row does not carry; the full scan must still run.
    fake_redis.values[hardening._audit_chain_checkpoint_key()] = json.dumps(
        {"row_id": rows[-1].id, "event_sha256": "0" * 64}
    )
    result = hardening._audit_chain_check(db, checkpoint=True)
    assert result["status"] == "breach"
    assert result["details"]["checkpoint_status"] == "digest_mismatch"
    assert result["details"]["first_broken_row_id"] == rows[3].id


def test_checkpoint_row_missing_falls_back_to_full_scan(db, fake_redis):
    _write(db, 4)
    fake_redis.values[hardening._audit_chain_checkpoint_key()] = json.dumps(
        {"row_id": 9999, "event_sha256": "a" * 64}
    )
    result = hardening._audit_chain_check(db, checkpoint=True)
    assert result["status"] == "clear"
    assert result["details"]["checkpoint_status"] == "row_missing"
    assert result["details"]["scope"] == "full"
    assert result["details"]["event_count"] == 4


def test_malformed_checkpoint_is_treated_as_missing(db, fake_redis):
    _write(db, 3)
    fake_redis.values[hardening._audit_chain_checkpoint_key()] = "not-json"
    result = hardening._audit_chain_check(db, checkpoint=True)
    assert result["details"]["checkpoint_status"] == "missing"
    assert result["details"]["event_count"] == 3


def test_redis_unavailable_falls_back_to_full_scan(db):
    _write(db, 6)
    with patch.object(hardening, "_checkpoint_redis", side_effect=ConnectionError("down")):
        result = hardening._audit_chain_check(db, checkpoint=True)
    assert result["status"] == "clear"
    assert result["details"]["checkpoint_status"] == "missing"
    assert result["details"]["checkpoint_saved"] is False
    assert result["details"]["event_count"] == 6


def test_default_and_recent_checks_ignore_checkpoint(db, fake_redis):
    _write(db, 10)
    hardening._audit_chain_check(db, checkpoint=True)
    _rows(db)[2].payload = {"sequence": "tampered"}
    db.commit()
    # Default (no checkpoint) still re-hashes from row 1.
    assert hardening._audit_chain_check(db)["status"] == "breach"
    recent = hardening._audit_chain_check(db, recent=3)
    assert recent["details"]["checkpoint_status"] == "not_used"


def test_checkpoint_key_is_namespaced_by_database(fake_redis):
    with patch.object(hardening.settings, "database_url", "postgresql://a/db1"):
        first = hardening._audit_chain_checkpoint_key()
    with patch.object(hardening.settings, "database_url", "postgresql://a/db2"):
        second = hardening._audit_chain_checkpoint_key()
    assert first != second
    assert "db1" not in first


def test_operator_runs_use_checkpoint_but_drills_and_full_rescan_do_not(db):
    calls = []

    def fake_check(_db, **kwargs):
        calls.append(kwargs)
        return {"key": "audit_chain", "status": "clear", "message": "", "details": {}}

    with patch.object(hardening, "_audit_chain_check", side_effect=fake_check), patch.object(
        hardening, "_scheduler_check",
        return_value={"key": "scheduler_ownership", "status": "unknown", "message": "", "details": {}},
    ):
        hardening.run_operational_hardening(db, source="manual_api", notify=False)
        hardening.run_operational_hardening(db, source="acceptance_drill", notify=False)
        hardening.run_operational_hardening(db, source="deployment_probe", notify=False, full_rescan=True)
        hardening.run_operational_hardening(db, source="read", notify=False)
    assert calls == [
        {"checkpoint": True},
        {"checkpoint": False},
        {"checkpoint": False},
        {"recent": hardening.AUDIT_CHAIN_RECENT_WINDOW},
    ]
