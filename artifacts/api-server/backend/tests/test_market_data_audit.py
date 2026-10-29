"""Compact audit payloads for high-volume market-data writers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import AuditLog
from app.services import alpaca_research_data, intraday_data
from app.services.market_data_audit import canonical_sha256

UTC = timezone.utc
NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _db() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _preflight_result(symbols: list[str]) -> dict:
    results = []
    for index, symbol in enumerate(symbols):
        ready = index not in {3, 7}
        results.append({
            "symbol": symbol,
            "status": "ready" if ready else "stale",
            "execution_status": "ready" if ready else "blocked",
            "failure_class": None if ready else "timing",
            "entitlement_state": "verified",
            "exchange_timestamp": NOW.isoformat(),
            "checked_at": NOW.isoformat(),
            "latency_seconds": 60.0,
            "missing_intervals": [] if ready else [f"2026-09-11T14:{m:02d}:00+00:00" for m in range(30)],
            "unavailable_reason": None if ready else "Latest bar is stale " + "x" * 400,
            "rows_imported": 2,
            "historical_repair_status": "incomplete" if index == 5 else None,
            "raw_payload": {"secret": "never"},
        })
    return {
        "provider": "alpaca_iex", "status": "blocked", "ready": False,
        "checked_at": NOW.isoformat(), "symbols": symbols, "reason": "Two symbols stale",
        "results": results,
    }


def test_preflight_audit_stores_compact_summary_with_full_digest():
    db = _db()
    symbols = [f"S{index:02d}" for index in range(45)]
    result = _preflight_result(symbols)
    returned = intraday_data._record_preflight_audit(db, result)
    db.commit()
    assert returned is result and len(returned["results"]) == 45

    audit = db.query(AuditLog).filter_by(action="sip_preflight").one()
    payload = audit.payload
    assert payload["status"] == "blocked"
    assert payload["provider"] == "alpaca_iex"
    assert payload["checked_at"] == NOW.isoformat()
    assert payload["symbols"] == symbols
    assert payload["symbol_count"] == 45
    assert payload["status_counts"] == {"ready": 43, "stale": 2}
    assert payload["rows_imported"] == 90
    assert payload["results_scope"] == "non_ready_only"
    assert [row["symbol"] for row in payload["results"]] == ["S03", "S05", "S07"]
    stale = payload["results"][0]
    assert stale["status"] == "stale" and stale["failure_class"] == "timing"
    assert len(stale["unavailable_reason"]) <= 160
    assert "missing_intervals" not in stale
    assert payload["results"][1]["historical_repair_status"] == "incomplete"
    assert payload["non_ready_count"] == 3
    assert "raw_payload" not in json.dumps(payload)
    assert len(payload["full_result_sha256"]) == 64
    # Much smaller than the full per-symbol result.
    assert len(json.dumps(payload)) * 4 < len(json.dumps(result, default=str))


def test_preflight_full_result_digest_is_stable_and_content_sensitive():
    db = _db()
    symbols = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META"]
    intraday_data._record_preflight_audit(db, _preflight_result(symbols))
    intraday_data._record_preflight_audit(db, _preflight_result(symbols))
    changed = _preflight_result(symbols)
    changed["results"][0]["latency_seconds"] = 61.0
    intraday_data._record_preflight_audit(db, changed)
    db.commit()
    digests = [row.payload["full_result_sha256"] for row in db.query(AuditLog).order_by(AuditLog.id)]
    assert digests[0] == digests[1] != digests[2]


def test_iex_research_audit_is_compact_and_status_readers_still_work():
    db = _db()
    symbols = sorted(alpaca_research_data.ALLOWED_SYMBOLS)
    result = {
        "provider": "alpaca_iex", "feed_class": "iex", "status": "sparse_or_empty",
        "failure_class": None, "checked_at": NOW.isoformat(),
        "window_start": NOW.isoformat(), "window_end": NOW.isoformat(),
        "results": [
            {"symbol": symbol, "rows_imported": 0 if index == 0 else 3, "duplicate_bars": 0, "out_of_order": 0}
            for index, symbol in enumerate(symbols)
        ],
    }
    payload = alpaca_research_data.compact_research_audit_payload(result)
    assert payload["status"] == "sparse_or_empty"
    assert payload["failure_class"] is None
    assert payload["checked_at"] == NOW.isoformat()
    assert payload["window_start"] == NOW.isoformat()
    assert payload["symbol_count"] == len(symbols)
    assert payload["rows_imported"] == 3 * (len(symbols) - 1)
    assert [row["symbol"] for row in payload["results"]] == [symbols[0]]
    assert payload["full_result_sha256"] == canonical_sha256(result)

    with patch.object(alpaca_research_data, "fetch_iex_bars", return_value={s: [] for s in symbols}):
        alpaca_research_data.collect_iex_research(db, now=NOW)
    db.commit()
    audit = db.query(AuditLog).filter_by(action="iex_research_collection").one()
    assert audit.payload["results_scope"] == "non_ready_only"
    assert audit.payload["non_ready_count"] == len(symbols)
