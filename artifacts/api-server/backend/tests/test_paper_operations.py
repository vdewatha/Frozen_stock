import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import yaml

from app.core.config import Settings

BACKEND = Path(__file__).parents[1]
ROOT = BACKEND.parents[2]
sys.path.insert(0, str(BACKEND / "scripts"))
from check_paper_progress import collect, main, summarize, INFRASTRUCTURE_CHECKS
from prepare_paper_deployment import prepare
import compare_paper_brokers as brokers


def test_private_configuration_is_random_and_never_overwritten(tmp_path):
    path = tmp_path / "paper.env"
    prepare(path)
    original = path.read_bytes()
    values = dict(line.split("=", 1) for line in path.read_text().splitlines() if not line.startswith("#"))
    keys = [values[f"AUTH_{role}_KEY"] for role in ("VIEWER", "RESEARCHER", "OPERATOR", "ADMIN")]
    assert len(set(keys)) == 4
    assert all(len(key) == 64 for key in keys)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert values["ALLOW_LIVE_TRADING"] == "false"
    with pytest.raises(FileExistsError):
        prepare(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("symbols", [[], [""], ["SPY;command"], ["/tmp"]])
def test_empty_or_invalid_universe_is_rejected(symbols):
    with pytest.raises(ValueError):
        Settings(_env_file=None, stock_learning_default_symbols=symbols)


def test_universe_is_normalized_and_deduplicated():
    assert Settings(_env_file=None, stock_learning_default_symbols=[" spy ", "AAPL", "SPY"]).stock_learning_default_symbols == ["AAPL", "SPY"]


def _ready_runtime():
    return {
        "checks": [{"name": name, "status": "ready"} for name in (*INFRASTRUCTURE_CHECKS, "Live trading safety")],
        "live_trading_allowed": False,
    }


def test_healthy_runtime_does_not_claim_session_or_learning_success():
    report = summarize(_ready_runtime(), {"status": "ready", "eligible_for_approval": False}, {"paused": False}, [])
    assert report["infrastructure_ready"]
    assert not report["launch"]["eligible_for_new_approval"]
    assert report["observed_cycles"] == []
    assert not report["profitability_proven"]
    assert not report["live_trading_authorized"]


def test_approval_readiness_does_not_grant_authority():
    preflight = {"cycle_id": "a" * 64, "gates": {"feed": {"status": "pass"}}, "status": "ready", "eligible_for_approval": True}
    report = summarize(_ready_runtime(), preflight, {"paused": False}, [])
    assert report["launch"]["eligible_for_new_approval"]
    assert not report["live_trading_authorized"]
    assert not report["profitability_proven"]
    preflight["gates"]["feed"]["status"] = "unknown"
    assert not summarize(_ready_runtime(), preflight, {"paused": False}, [])["launch"]["eligible_for_new_approval"]
    preflight["gates"]["feed"]["status"] = "pass"
    assert not summarize(_ready_runtime(), preflight, {"paused": True}, [])["launch"]["eligible_for_new_approval"]


def test_progress_only_fetches_read_only_paths_and_exact_cycle():
    cycle = "a" * 64
    with patch("check_paper_progress._load_json", side_effect=[
        _ready_runtime(), {"paused": False}, {"cycle_id": cycle}, [], {"items": []},
    ]) as load:
        collect("https://example.com", 10, cycle)
    paths = [call.args[1] for call in load.call_args_list]
    assert paths == [
        "/api/system/deployment-monitor", "/api/stock/learning-cycles/schedule-control",
        f"/api/stock/learning-cycles/launch-prerequisites?cycle_id={cycle}",
        "/api/stock/learning-cycles?limit=100",
        "/api/stock/training/jobs?limit=100",
    ]


def test_invalid_cycle_never_uses_network():
    with patch("check_paper_progress._load_json") as load, pytest.raises(ValueError):
        collect("https://example.com", 10, "bad&token=secret")
    load.assert_not_called()


def test_unavailable_progress_writes_fail_closed_report_without_error_secrets(tmp_path):
    output = tmp_path / "report.json"
    with patch("check_paper_progress.collect", side_effect=ValueError("secret-credential")):
        assert main(["--output", str(output)]) == 2
    report = json.loads(output.read_text())
    assert report["status"] == "unavailable"
    assert "secret-credential" not in output.read_text()


def test_persistent_stack_is_private_paper_only_and_has_isolated_queues():
    config = yaml.safe_load((ROOT / "deploy/paper/compose.yaml").read_text())
    services = config["services"]
    assert services["backend"]["ports"] == ["127.0.0.1:${PAPER_API_PORT:-8010}:8000"]
    assert "ports" not in services["redis"] and "ports" not in services["postgres"]
    assert services["redis"]["command"][1:3] == ["--appendonly", "yes"]
    for service in ("backend", "learning", "risk", "execution", "scheduler", "watchdog", "intraday", "market-data"):
        assert services[service]["environment"]["ALLOW_LIVE_TRADING"] == "false"
        assert services[service]["environment"]["ACTIVE_PAPER_BROKER"] == "alpaca_paper"
        assert services[service]["environment"]["LIVE_ALPACA_API_KEY"] == ""
        assert services[service]["environment"]["LIVE_ALPACA_API_SECRET"] == ""
        assert services[service]["restart"] == "unless-stopped"
    for service, queue in (("learning", "learning"), ("risk", "risk"), ("execution", "paper_trading"), ("intraday", "intraday_market_data")):
        assert f"--queues={queue}" in services[service]["command"]
    assert services["migrate"]["restart"] == "no"


def test_beat_child_stops_before_lease_release():
    spec = importlib.util.spec_from_file_location("paper_beat_test", BACKEND / "scripts/run_beat_with_lease.py")
    beat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(beat)
    events = []
    client = MagicMock()
    client.set.return_value = True
    refresh = MagicMock(return_value=0)
    release = MagicMock(side_effect=lambda **kwargs: events.append("release"))
    client.register_script.side_effect = [refresh, release]
    child = MagicMock()
    child.poll.side_effect = [None, None]
    waits = iter([True, False])
    def wait(*args, **kwargs):
        if next(waits):
            raise subprocess.TimeoutExpired("beat", 10)
        events.append("wait")
    child.wait.side_effect = wait
    child.kill.side_effect = lambda: events.append("kill")
    with patch.dict("os.environ", {"REDIS_URL": "redis://test"}), patch.object(beat.redis.Redis, "from_url", return_value=client), patch.object(beat.celery_app, "send_task"), patch.object(beat.subprocess, "Popen", return_value=child), patch.object(beat.signal, "signal"), patch.object(beat.time, "sleep"):
        assert beat.main() == 1
    assert events == ["kill", "wait", "release"]
    assert child.wait.call_count == 2


def test_startup_refresh_is_opt_in():
    spec = importlib.util.spec_from_file_location("paper_beat_opt_in_test", BACKEND / "scripts/run_beat_with_lease.py")
    beat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(beat)
    with patch.dict("os.environ", {"PAPER_STARTUP_REFRESH": "false"}, clear=False):
        assert beat._startup_refresh_enabled() is False
    with patch.dict("os.environ", {"PAPER_STARTUP_REFRESH": "true"}, clear=False):
        assert beat._startup_refresh_enabled() is True


def test_startup_learning_recovery_is_deduplicated_and_paper_only():
    spec = importlib.util.spec_from_file_location("paper_beat_recovery_test", BACKEND / "scripts/run_beat_with_lease.py")
    beat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(beat)
    client = MagicMock()
    client.set.return_value = True
    with patch.object(beat.celery_app, "send_task") as send_task:
        beat._queue_startup_learning_recovery(client)
    client.set.assert_called_once_with(
        beat.STARTUP_LEARNING_RECOVERY_KEY,
        "queued",
        nx=True,
        ex=beat.STARTUP_LEARNING_RECOVERY_TTL_SECONDS,
    )
    send_task.assert_called_once_with(
        "app.tasks.jobs.retry_failed_strategy_learning_scopes_job",
        queue="learning",
        expires=15 * 60,
    )


def test_startup_learning_recovery_tolerates_transient_redis_restart():
    import redis

    spec = importlib.util.spec_from_file_location("paper_beat_redis_test", BACKEND / "scripts/run_beat_with_lease.py")
    beat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(beat)
    client = MagicMock()
    client.set.side_effect = redis.RedisError("redis restarting")
    with patch.object(beat.celery_app, "send_task") as send_task:
        beat._queue_startup_learning_recovery(client)
    send_task.assert_not_called()


def test_both_broker_probes_are_read_only_and_redacted():
    from pydantic import SecretStr

    configuration = MagicMock()
    configuration.paper_broker_credentials.return_value = ("key", "secret")
    configuration.tradier_api_key = SecretStr("tradier-secret")
    configuration.tradier_account_id = "private-account"
    configuration.active_paper_broker = "tradier_sandbox"
    alpaca, tradier = MagicMock(), MagicMock()
    for client in (alpaca, tradier):
        client.account.return_value = {"id": "private-account", "cash": "12345"}
        client.positions.return_value = []
    with patch.object(brokers, "settings", configuration), patch.object(brokers, "AlpacaPaperClient", return_value=alpaca), patch.object(brokers, "TradierPaperClient", return_value=tradier):
        report = brokers.compare()
    assert all(row["status"] == "reachable_not_qualified" for row in report["candidates"])
    assert "private-account" not in json.dumps(report)
    assert "12345" not in json.dumps(report)
    for client in (alpaca, tradier):
        client.submit_order.assert_not_called()
        client.cancel_order.assert_not_called()
    assert not report["provider_changed"]


def test_missing_broker_credentials_never_make_network_requests():
    with patch.object(brokers, "settings", Settings(_env_file=None)), patch.object(brokers, "AlpacaPaperClient") as alpaca, patch.object(brokers, "TradierPaperClient") as tradier:
        report = brokers.compare()
    assert all(row["status"] == "blocked" for row in report["candidates"])
    alpaca.assert_not_called()
    tradier.assert_not_called()
