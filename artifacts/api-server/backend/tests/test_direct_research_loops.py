from __future__ import annotations

from unittest.mock import patch


def test_direct_scalp_research_once_does_not_run_invalid_cleanup(monkeypatch, capsys):
    from app import main

    with patch(
        "app.services.scalp_research_runner.run_scalp_research_once",
        return_value={"status": "outside_market_hours", "execution_eligible": False},
    ) as run_once:
        main._run_direct_scalp_research_once("AAPL")

    run_once.assert_called_once()
    assert "symbol=AAPL status=outside_market_hours" in capsys.readouterr().out

def test_direct_scalp_research_starts_one_loop_per_configured_symbol(monkeypatch):
    from app import main

    started: list[tuple[str, str]] = []

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            assert daemon is True
            started.append((name, args[0]))

        def start(self):
            return None

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main.settings, "iex_research_enabled", True)
    monkeypatch.setenv("PAPER_DIRECT_SCALP_RESEARCH_LOOP", "true")
    monkeypatch.setenv("PAPER_SCALP_RESEARCH_SYMBOLS", "SPY, aapl, SPY, QQQ")

    main._start_direct_scalp_research_loops()

    assert started == [
        ("direct-scalp-research-aapl", "AAPL"),
        ("direct-scalp-research-qqq", "QQQ"),
        ("direct-scalp-research-spy", "SPY"),
    ]


def test_direct_scalp_research_stays_disabled_without_explicit_flag(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main.settings, "iex_research_enabled", True)
    monkeypatch.delenv("PAPER_DIRECT_SCALP_RESEARCH_LOOP", raising=False)

    class FailThread:
        def __init__(self, **_kwargs):
            raise AssertionError("research loop must remain disabled")

    monkeypatch.setattr(main.threading, "Thread", FailThread)
    main._start_direct_scalp_research_loops()


def test_direct_delayed_sip_research_starts_with_shared_research_flag(monkeypatch):
    from app import main

    started = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            assert target is main._direct_delayed_sip_research_loop
            assert daemon is True
            started.append(name)

        def start(self):
            return None

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    monkeypatch.setattr(main.settings, "environment", "production")
    monkeypatch.setattr(main.settings, "delayed_sip_research_enabled", True)
    monkeypatch.setenv("PAPER_DIRECT_RESEARCH_LOOP", "true")

    main._start_direct_delayed_sip_research_loop()

    assert started == ["direct-delayed-sip-research-loop"]
