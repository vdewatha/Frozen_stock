from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import StockPaperAccount, StockPaperBrokerActivity, StockPaperFill
from app.services.paper_venue_qualification import (
    SELECTED_PAPER_PROVIDER,
    assess_paper_venue_evidence,
    authorize_paper_venue_activation,
    paper_venue_qualification_status,
    record_paper_venue_qualification,
)
from app.services.stock_paper_ledger import _upsert_fills


def _evidence(**overrides):
    value = {
        "account_identity": {"present": True, "matches_configured_binding": True},
        "orders": {"complete": True},
        "executions": {"complete": True},
        "commissions": {"complete": True},
        "cash_activities": {"complete": True},
        "timestamps": {"precise_utc": True},
        "pagination": {"orders_complete": True, "activities_complete": True},
        "report_period": {"start": "2026-01-01T00:00:00Z", "end": "2026-09-21T00:00:00Z"},
        "delayed_events": {"captured": True},
        "restart_replay": {"activities_preserved": True, "no_duplicates": True},
        "session_expiry_replay": {"activities_preserved": True, "no_duplicates": True},
        "provider_contract": {"name": "alpaca-paper-api", "read_paths": ["account", "orders", "activities"]},
        "counts": {"orders": 1, "executions": 1, "commissions": 1, "cash_activities": 1},
    }
    for section, fields in overrides.items():
        value[section] = fields
    return value


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine


def test_missing_or_unknown_evidence_is_fail_closed_and_redacted():
    evidence = _evidence(commissions={"complete": None})
    evidence["cash_activities"]["raw_payload"] = {"account_id": "private-account", "net_amount": "100"}
    report = assess_paper_venue_evidence(
        provider=SELECTED_PAPER_PROVIDER,
        account_id="private-account",
        evidence=evidence,
    )
    assert report["status"] == "blocked"
    assert "commissions.complete" in " ".join(report["blockers"])
    assert "private-account" not in str(report)
    assert "net_amount" not in str(report)
    assert report["paper_only"] is True
    assert report["live_authorized"] is False


def test_replaying_identical_qualification_evidence_is_idempotent():
    engine = _db()
    with Session(engine) as db:
        first = record_paper_venue_qualification(
            db, provider=SELECTED_PAPER_PROVIDER, account_id="private-account",
            evidence=_evidence(), reviewer="reviewer-a",
        )
        db.commit()
        second = record_paper_venue_qualification(
            db, provider=SELECTED_PAPER_PROVIDER, account_id="private-account",
            evidence=_evidence(), reviewer="reviewer-b",
        )
        assert second.id == first.id
        assert second.report_sha256 == first.report_sha256
        assert db.query(type(first)).count() == 1
    engine.dispose()


def test_activation_requires_separate_authorization_and_exact_passing_package():
    engine = _db()
    with Session(engine) as db:
        db.add(StockPaperAccount(broker=SELECTED_PAPER_PROVIDER, broker_account_id="private-account",
            currency="USD", cash=1000, buying_power=1000, equity=1000, raw_payload={}))
        db.flush()
        qualification = record_paper_venue_qualification(
            db, provider=SELECTED_PAPER_PROVIDER, account_id="private-account",
            evidence=_evidence(), reviewer="reviewer-a",
        )
        db.commit()
        assert paper_venue_qualification_status(db, SELECTED_PAPER_PROVIDER)["ready_for_paper_admission"] is False
        with pytest.raises(ValueError, match="separate authorizer"):
            authorize_paper_venue_activation(
                db, qualification_id=qualification.id, provider=SELECTED_PAPER_PROVIDER,
                authorizer="reviewer-a", reason="not independent",
            )
        authorization = authorize_paper_venue_activation(
            db, qualification_id=qualification.id, provider=SELECTED_PAPER_PROVIDER,
            authorizer="operator-b", reason="Reviewed the redacted package",
        )
        db.commit()
        state = paper_venue_qualification_status(db, SELECTED_PAPER_PROVIDER)
        assert state["ready_for_paper_admission"] is True
        assert state["report_sha256"] == qualification.report_sha256
        assert authorization.live_authorized is False
    engine.dispose()


@pytest.mark.parametrize("corruption", ["account", "report", "report_hash", "reviewer", "authorizer", "authorization_hash", "reason"])
def test_admission_revalidates_account_and_both_evidence_packages(corruption):
    engine = _db()
    with Session(engine) as db:
        account = StockPaperAccount(broker=SELECTED_PAPER_PROVIDER, broker_account_id="private-account",
            currency="USD", cash=1000, buying_power=1000, equity=1000, raw_payload={})
        db.add(account)
        db.flush()
        qualification = record_paper_venue_qualification(db, provider=SELECTED_PAPER_PROVIDER,
            account_id="private-account", evidence=_evidence(), reviewer="reviewer-a")
        authorization = authorize_paper_venue_activation(db, qualification_id=qualification.id,
            provider=SELECTED_PAPER_PROVIDER, authorizer="operator-b", reason="Reviewed package")
        db.commit()
        assert paper_venue_qualification_status(db, SELECTED_PAPER_PROVIDER)["ready_for_paper_admission"]
        if corruption == "account":
            account.broker_account_id = "replacement-account"
        elif corruption == "report":
            qualification.report = {**qualification.report, "status": "blocked"}
        elif corruption == "report_hash":
            qualification.report_sha256 = "0" * 64
        elif corruption == "reviewer":
            qualification.reviewed_by = "operator-b"
        elif corruption == "authorizer":
            authorization.authorized_by = " "
        elif corruption == "authorization_hash":
            authorization.authorization_sha256 = "0" * 64
        elif corruption == "reason":
            authorization.reason = "Modified after approval"
        db.flush()
        result = paper_venue_qualification_status(db, SELECTED_PAPER_PROVIDER)
        assert not result["ready_for_paper_admission"]
        assert not result["activation_authorized"]
    engine.dispose()


@pytest.mark.parametrize("authorizer", ["", " "])
def test_blank_authorizer_rejected(authorizer):
    engine = _db()
    with Session(engine) as db:
        db.add(StockPaperAccount(broker=SELECTED_PAPER_PROVIDER, broker_account_id="private-account",
            currency="USD", cash=1000, buying_power=1000, equity=1000, raw_payload={}))
        db.flush()
        qualification = record_paper_venue_qualification(db, provider=SELECTED_PAPER_PROVIDER,
            account_id="private-account", evidence=_evidence(), reviewer="reviewer-a")
        with pytest.raises(ValueError):
            authorize_paper_venue_activation(db, qualification_id=qualification.id,
                provider=SELECTED_PAPER_PROVIDER, authorizer=authorizer, reason="Reviewed")
    engine.dispose()


def test_activation_requires_a_matching_initialized_account():
    engine = _db()
    with Session(engine) as db:
        qualification = record_paper_venue_qualification(db, provider=SELECTED_PAPER_PROVIDER,
            account_id="private-account", evidence=_evidence(), reviewer="reviewer-a")
        with pytest.raises(ValueError):
            authorize_paper_venue_activation(db, qualification_id=qualification.id,
                provider=SELECTED_PAPER_PROVIDER, authorizer="operator-b", reason="Reviewed")
    engine.dispose()


@pytest.mark.parametrize("field,value", [("paper_only", False), ("live_authorized", True)])
def test_invalid_newest_authorization_cannot_fall_back_to_older_approval(field, value):
    engine = _db()
    with Session(engine) as db:
        db.add(StockPaperAccount(broker=SELECTED_PAPER_PROVIDER, broker_account_id="private-account",
            currency="USD", cash=1000, buying_power=1000, equity=1000, raw_payload={}))
        db.flush()
        qualification = record_paper_venue_qualification(db, provider=SELECTED_PAPER_PROVIDER,
            account_id="private-account", evidence=_evidence(), reviewer="reviewer-a")
        authorize_paper_venue_activation(db, qualification_id=qualification.id,
            provider=SELECTED_PAPER_PROVIDER, authorizer="operator-b", reason="Initial approval")
        newest = authorize_paper_venue_activation(db, qualification_id=qualification.id,
            provider=SELECTED_PAPER_PROVIDER, authorizer="operator-b", reason="Latest approval")
        db.commit()
        # DB constraints already reject these flags; also reject a dirty identity-map object.
        with db.no_autoflush:
            setattr(newest, field, value)
            assert not paper_venue_qualification_status(db, SELECTED_PAPER_PROVIDER)["ready_for_paper_admission"]
    engine.dispose()


def test_restart_and_expiry_replay_flags_are_required():
    for section in ("restart_replay", "session_expiry_replay"):
        evidence = _evidence(**{section: {"activities_preserved": True, "no_duplicates": False}})
        report = assess_paper_venue_evidence(
            provider=SELECTED_PAPER_PROVIDER, account_id="paper-account", evidence=evidence
        )
        assert report["status"] == "blocked"


def test_restart_and_session_expiry_replays_store_each_activity_once():
    engine = _db()
    activity = {
        "id": "execution-1",
        "activity_type": "FILL",
        "order_id": "order-1",
        "symbol": "SPY",
        "side": "buy",
        "qty": "1",
        "price": "100",
        "commission": "0",
        "transaction_time": "2026-09-21T14:31:00Z",
    }
    with Session(engine) as db:
        account = StockPaperAccount(
            id=1, broker=SELECTED_PAPER_PROVIDER, broker_account_id="paper",
            currency="USD", cash=1000, buying_power=1000, equity=1000,
            raw_payload={},
        )
        db.add(account)
        db.flush()
        for _replay in ("restart", "session_expiry"):
            _upsert_fills(
                db,
                account,
                [activity],
                datetime.fromisoformat("2026-09-21T14:32:00+00:00"),
            )
            db.flush()
        assert db.query(StockPaperBrokerActivity).count() == 1
        assert db.query(StockPaperFill).count() == 1
    engine.dispose()
