from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models
from app.core.config import settings
from app.core.security import required_role
from app.db.base import Base
from app.models import AuditLog, IntradayBar
from app.services import delayed_sip_research as sip
from app.services.intraday_data import feed_status, upsert_intraday_bars
from app.services.online_research import advance_online_research
from app.services.trusted_data import UntrustedMarketData, trusted_intraday_observation

UTC = timezone.utc
START = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
END = START + timedelta(minutes=10)
NOW = END + timedelta(minutes=16)


def bar(at=START, **changes):
    return {"t": at.isoformat(), "o": 100, "h": 102, "l": 99, "c": 100, "v": 20, **changes}


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_window_is_delayed_and_prior_session_is_bounded():
    assert sip.delayed_window(NOW) == (START, END)
    assert sip.delayed_window(START + timedelta(minutes=16))[1] == datetime(2026, 9, 25, 20, tzinfo=UTC)
    assert sip.delayed_window(START + timedelta(minutes=17))[1] == START + timedelta(minutes=1)
    assert sip.delayed_window(datetime(2025, 7, 3, 20, tzinfo=UTC))[1] == datetime(2025, 7, 3, 17, tzinfo=UTC)
    with pytest.raises(ValueError):
        sip.delayed_window(NOW.replace(tzinfo=None))


def test_sdk_feed_raw_and_pagination_are_explicit():
    client = Mock()
    client.get.side_effect = [{"bars": {"SPY": [bar()]}, "next_page_token": "page2"},
                              {"bars": {"SPY": [bar(START + timedelta(minutes=1))]}, "next_page_token": None}]
    rows = sip.fetch_delayed_sip_bars(["SPY"], START, END, now=NOW, client=client)
    assert len(rows["SPY"]) == 2
    assert client.get.call_count == 2
    params = client.get.call_args.kwargs["data"]
    assert params["feed"] == "sip" and params["adjustment"] == "raw"
    assert params["page_token"] == "page2"
    assert datetime.fromisoformat(params["end"].replace("Z", "+00:00")) < END


@pytest.mark.parametrize("end,now", [(END + timedelta(microseconds=1), NOW), (END, NOW.replace(tzinfo=None)), (END.replace(tzinfo=None), NOW)])
def test_recent_or_naive_cutoff_rejected_before_request(end, now):
    client = Mock()
    with pytest.raises(ValueError):
        sip.fetch_delayed_sip_bars(["SPY"], START, end, now=now, client=client)
    client.get.assert_not_called()


@pytest.mark.parametrize("payload", [{}, {"bars": []}, {"bars": {"BAD": []}}, {"bars": {"SPY": [bar(), bar()]}},
                                      {"bars": {"SPY": [bar(END)]}}, {"bars": {"SPY": [bar(c="nan")]}},
                                      {"bars": {"SPY": [bar(t="2026-09-28T13:30:00")]}}])
def test_bad_response_rejected(payload):
    client = Mock()
    client.get.return_value = payload
    with pytest.raises(ValueError):
        sip.fetch_delayed_sip_bars(["SPY"], START, END, now=NOW, client=client)


def test_pagination_exhaustion_never_persists_partial_data(db):
    client = Mock()
    client.get.side_effect = [{"bars": {"SPY": [bar(START + timedelta(minutes=n))]}, "next_page_token": str(n)} for n in range(4)]
    with patch("app.services.alpaca_research_data.BoundedStockDataClient", return_value=client):
        report = sip.collect_delayed_sip(db, now=NOW)
    assert report["status"] == "unavailable"
    assert db.query(IntradayBar).count() == 0
    client.close.assert_called_once()


@pytest.mark.parametrize("code,classification", [(401, "authentication"), (403, "entitlement"), (422, "invalid_request_or_entitlement"), (429, "rate_limit"), (500, "unavailable_or_invalid")])
def test_failure_is_redacted(db, code, classification):
    error = RuntimeError("credential-in-provider-response")
    error.status_code = code
    with patch.object(sip, "fetch_delayed_sip_bars", side_effect=error):
        result = sip.collect_delayed_sip(db, now=NOW)
    db.commit()
    assert result["status"] == "unavailable" and result["failure_class"] == classification
    assert "credential-in-provider-response" not in str(db.query(AuditLog).one().payload)
    assert db.query(IntradayBar).count() == 0


def test_idempotent_provenance_comparison_and_execution_isolation(db):
    rows = {symbol: [bar()] for symbol in sip.ALLOWED_SYMBOLS}
    with patch.object(sip, "fetch_delayed_sip_bars", return_value=rows):
        for _ in range(2):
            assert sip.collect_delayed_sip(db, now=NOW)["status"] == "observed"
            db.commit()
    assert db.query(IntradayBar).count() == 4
    assert {(r.provider, r.feed_class) for r in db.query(IntradayBar)} == {(sip.PROVIDER, sip.FEED)}
    assert feed_status(db, "SPY", now=NOW)["status"] != "ready"
    with pytest.raises(UntrustedMarketData):
        trusted_intraday_observation(db, "SPY", now=NOW)
    # No IEX rows means the forward IEX learner cannot train on delayed SIP.
    outcome = advance_online_research(db, now=NOW)
    assert all(row["status"] == "waiting_for_fresh_contiguous_bars" and row["scored"] == 0 for row in outcome["results"])
    upsert_intraday_bars(db, "SPY", [bar(c=101)], ingested_at=NOW, provider="alpaca_iex", feed_class="iex")
    db.commit()
    status = sip.delayed_sip_status(db, now=NOW)
    spy = next(row for row in status["symbols"] if row["symbol"] == "SPY")
    assert spy["matched_minutes"] == 1 and spy["mean_abs_close_gap_bps"] == 100
    assert spy["window_bars"] == 1 and spy["iex_window_bars"] == 1
    assert status["poll_fresh"] and not status["execution_eligible"]
    assert not sip.delayed_sip_status(db, now=NOW + timedelta(minutes=11))["poll_fresh"]


def test_storage_boundary_also_rejects_recent_delayed_rows(db):
    with pytest.raises(ValueError, match="cutoff"):
        upsert_intraday_bars(db, "SPY", [bar(END)], ingested_at=NOW, provider=sip.PROVIDER, feed_class=sip.FEED)
    assert db.query(IntradayBar).count() == 0
    with pytest.raises(ValueError, match="provenance"):
        upsert_intraday_bars(db, "SPY", [bar()], ingested_at=NOW, provider=sip.PROVIDER, feed_class="sip")


def test_empty_feed_does_not_claim_observations(db):
    with patch.object(sip, "fetch_delayed_sip_bars", return_value={symbol: [] for symbol in sip.ALLOWED_SYMBOLS}):
        assert sip.collect_delayed_sip(db, now=NOW)["status"] == "sparse_or_empty"
    assert all(row["mean_abs_close_gap_bps"] is None for row in sip.delayed_sip_status(db, now=NOW)["symbols"])


def test_worker_disabled_and_shared_coordination(monkeypatch):
    from app.tasks import jobs
    from app.tasks.celery_app import celery_app
    monkeypatch.setattr(settings, "delayed_sip_research_enabled", False)
    with patch.object(jobs, "_run_job") as run:
        assert jobs.delayed_sip_collection_job()["status"] == "disabled"
        run.assert_not_called()
    monkeypatch.setattr(settings, "delayed_sip_research_enabled", True)
    with patch.object(jobs, "_run_job", return_value={"status": "test"}) as run:
        assert jobs.delayed_sip_collection_job()["status"] == "test"
        assert run.call_args.args[0] in jobs.REDIS_LOCKED_JOBS
    assert celery_app.conf.task_routes["app.tasks.jobs.delayed_sip_collection_job"]["queue"] == "market_data"
    assert celery_app.conf.beat_schedule["delayed-sip-collection"]["schedule"] == 300


def test_api_disable_queue_and_roles(monkeypatch):
    from app.api.routes import router
    from app.tasks.jobs import delayed_sip_collection_job
    assert required_role("GET", "/market-data/delayed-sip/status") == "viewer"
    assert required_role("POST", "/market-data/delayed-sip/collect") == "researcher"
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client, patch.object(delayed_sip_collection_job, "apply_async", return_value=Mock(id="test-task")) as queue:
        monkeypatch.setattr(settings, "delayed_sip_research_enabled", False)
        assert client.post("/market-data/delayed-sip/collect").status_code == 409
        queue.assert_not_called()
        monkeypatch.setattr(settings, "delayed_sip_research_enabled", True)
        response = client.post("/market-data/delayed-sip/collect")
        assert response.status_code == 202 and not response.json()["execution_eligible"]
        queue.assert_called_once_with(expires=240)
