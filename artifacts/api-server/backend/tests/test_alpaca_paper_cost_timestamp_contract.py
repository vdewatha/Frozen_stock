"""Provider-shaped fixtures are contract tests, not observed broker certification."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401 -- register ledger foreign-key targets
from app.db.base import Base
from app.models.stock_paper import StockPaperAccount, StockPaperFill
from app.services.stock_paper_ledger import StockPaperError, _upsert_fills
from scripts.alpaca_paper_contract import assess_cost_timestamp_contract, precise_activity_time


@pytest.fixture
def shapes():
    return json.loads((Path(__file__).parent / "fixtures/alpaca_paper_cost_timestamp.json").read_text())


def test_documented_shapes_do_not_qualify(shapes):
    result = assess_cost_timestamp_contract([shapes["documented_fill"], shapes["documented_nontrade"]])
    assert result["result"] == "not_established"
    assert result["cost_evidence"] == result["activity_timestamp_evidence"] == "unknown"
    assert result["fills_without_valid_commission"] == 1
    assert result["activities_without_precise_broker_time"] == 1
    assert result["launch_authorized"] is False
    assert precise_activity_time(shapes["documented_nontrade"]) is None


@pytest.mark.parametrize("rows", [None, []])
def test_empty_or_unavailable_evidence_cannot_pass(rows):
    result = assess_cost_timestamp_contract(rows)
    assert result["result"] == "not_established"
    assert result["cost_evidence"] == result["activity_timestamp_evidence"] == "unknown"


@pytest.mark.parametrize("commission", [None, "", "NaN", "Infinity", "bad"])
def test_absent_or_invalid_commission_is_unknown(shapes, commission):
    fill = {**shapes["documented_fill"], "commission": commission}
    assert assess_cost_timestamp_contract([fill])["cost_evidence"] == "unknown"


@pytest.mark.parametrize("timestamp", [None, "", "2026-09-15", "2026-09-15T14:30:00", "bad"])
def test_no_date_midnight_or_observation_time_inference(shapes, timestamp):
    row = {**shapes["documented_nontrade"], "transaction_time": timestamp}
    assert precise_activity_time(row) is None


@pytest.mark.parametrize("extension", [
    "explicit_zero_commission_extension", "explicit_nonzero_commission_extension",
])
def test_explicit_cost_and_precise_time_only_satisfy_field_contract(shapes, extension):
    fill = {**shapes["documented_fill"], **shapes[extension]}
    journal = {**shapes["documented_nontrade"], **shapes["precise_created_at_extension"]}
    result = assess_cost_timestamp_contract([fill, journal])
    assert result["result"] == "field_contract_satisfied_only"
    assert result["launch_authorized"] is False
    assert precise_activity_time(journal).isoformat() == "2026-09-15T14:30:00+00:00"


def test_ledger_preserves_unknown_cost_then_exact_enrichment_and_immutable_time(shapes):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 16, tzinfo=UTC)
    fill = shapes["documented_fill"]
    try:
        with Session(engine) as db:
            # This focused fixture invokes only the persistence boundary, not
            # initialization, reconciliation, a broker client, or launch gates.
            account = StockPaperAccount(id=1)
            _upsert_fills(db, account, [fill], now)
            db.flush()
            persisted = db.query(StockPaperFill).one()
            assert persisted.fee is None
            assert persisted.cost_known is False
            original_time = persisted.filled_at
            _upsert_fills(db, account, [fill], now + timedelta(days=1))
            assert persisted.filled_at == original_time
            enriched = {**fill, **shapes["explicit_zero_commission_extension"]}
            assert _upsert_fills(db, account, [enriched], now) == {fill["id"]}
            assert persisted.fee == Decimal("0")
            assert persisted.cost_known is True
            with pytest.raises(StockPaperError, match="immutable fields changed"):
                _upsert_fills(db, account, [{**enriched, "transaction_time": "2026-09-15T14:32:00Z"}], now)
    finally:
        engine.dispose()