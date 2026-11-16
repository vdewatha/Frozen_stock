"""PostgreSQL row-lock races on the stock-paper account, recovery and order rows.

Broker reconciliation locks the account row for its whole broker sync (40-85 s
in production). These tests hold a reconciliation inside that sync on a real
PostgreSQL server and run the recovery watchdog, the automatic resume checks
and order dispatch against it. SQLite has no row locks, so none of this is
exercised by the SQLite suite.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RiskRule, StockPaperRecoveryEvent, StockPaperRecoveryState
from app.models.stock_paper import StockPaperAccount, StockPaperLedgerEvent, StockPaperOrder
from app.services import auto_revalidation
from app.services.stock_paper_ledger import (
    RECOVERY_ROW_DEFERRED_REASON,
    StockPaperError,
    dispatch_reserved_order,
    initialize_stock_paper_account,
    reconcile_stock_paper_account,
    reserve_stock_paper_order,
)
from app.services.stock_recovery import (
    ACCOUNT_BUSY_REASON,
    RecoveryAccountBusy,
    _account_after_state,
    _state,
    attempt_explained_halt_auto_resume,
    resume_stock_paper_after_revalidation,
    enter_stock_recovery,
    run_stock_watchdog,
)
from tests.postgres_support import (
    WAIT_SECONDS,
    backend_pid,
    outcomes_or_known_deadlock,
    result_or_fail,
    wait_until_blocked,
    wait_until_blocking,
)
from tests.test_stock_paper_ledger import FakeAlpaca, session_window_around

pytestmark = pytest.mark.postgres

POSITION = {"symbol": "SPY", "qty": "1", "market_value": "100"}
UNANSWERED_ORDER_HALT = "Nonterminal client order has no broker lookup result"


class HeldSync(FakeAlpaca):
    """A broker sync that stops in its first request, while the caller holds the account row."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.holding, self.release = Event(), Event()

    def account(self):
        self.holding.set()
        if not self.release.wait(WAIT_SECONDS * 4):
            raise RuntimeError("The test never released the held broker sync")
        return super().account()


class AcceptingBroker(FakeAlpaca):
    """Accepts every order; ``hold`` keeps the POST in flight until ``finish`` is set."""

    def __init__(self, *, hold: bool = False):
        super().__init__()
        self.hold = hold
        self.posting, self.finish = Event(), Event()
        self.accepted: list[dict] = []

    def submit_order(self, payload):
        self.calls += 1
        if self.hold:
            self.posting.set()
            if not self.finish.wait(WAIT_SECONDS * 4):
                raise RuntimeError("The test never let the held order POST return")
        raw = {"id": f"broker-{self.calls}", "status": "accepted", "type": payload["type"],
               **{key: payload[key] for key in ("client_order_id", "symbol", "side", "qty", "time_in_force", "limit_price")}}
        self.accepted.append(raw)
        return raw


@pytest.fixture()
def ledger(postgres_schema):
    with patch.object(settings, "active_paper_broker", "alpaca_paper"), \
         patch.object(settings, "paper_broker_account_id", ""), \
         patch.object(settings, "allow_live_trading", False):
        with Session(postgres_schema.engine) as db:
            db.add(RiskRule(name="postgres-locks", value={"kill_switch_enabled": False, "paper_only": True}))
            db.commit()
        yield postgres_schema


def _reconcile(engine, gateway, pids):
    with Session(engine) as db:
        pids["reconcile"] = backend_pid(db)
        return reconcile_stock_paper_account(db, gateway)["status"]


def _pid(pids: dict, name: str) -> int:
    deadline = time.monotonic() + WAIT_SECONDS
    while name not in pids:
        if time.monotonic() > deadline:
            pytest.fail(f"The {name} thread never opened its transaction")
        time.sleep(0.01)
    return pids[name]


# --- (a) reconciliation against the recovery watchdog and the resume checks ---


def _watchdog_pause(engine):
    """The production setup of 2026-11-10: a stale reconciliation heartbeat paused a flat, armed account."""
    with Session(engine) as db:
        initialize_stock_paper_account(db, FakeAlpaca(), activity_contract="alpaca-activities-v2")
        account = db.query(StockPaperAccount).one()
        state = _state(db)
        now = datetime.now(timezone.utc)
        state.status = "resumable"
        state.last_monitor_heartbeat_at = now
        account.last_reconciled_at = now - timedelta(minutes=15)
        db.commit()
        assert run_stock_watchdog(db)["reasons"] == ["broker reconciliation heartbeat is stale"]
        state = db.query(StockPaperRecoveryState).one()
        assert state.status == "cooldown"
        state.last_monitor_heartbeat_at = datetime.now(timezone.utc)
        state.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()


def _recovery_row_is_free(engine) -> bool:
    with Session(engine) as db:
        db.execute(text("SELECT id FROM stock_paper_recovery_state WHERE id = 1 FOR UPDATE NOWAIT"))
        db.rollback()
    return True


def test_resume_checks_give_way_while_reconciliation_holds_the_account(ledger):
    engine = ledger.engine
    _watchdog_pause(engine)
    gateway, pids = HeldSync(), {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        reconciliation = pool.submit(_reconcile, engine, gateway, pids)
        try:
            assert gateway.holding.wait(WAIT_SECONDS), "Reconciliation never reached the broker sync"

            # The lock helper itself: recovery row first, then the account without waiting.
            with Session(engine) as db:
                _state(db)
                with pytest.raises(RecoveryAccountBusy, match="Broker reconciliation holds the account"):
                    _account_after_state(db, wait=False)
                # Giving way also gave up the recovery row; holding it while
                # reconciliation finished was one edge of the deadlock.
                assert _recovery_row_is_free(engine)

            # The three scheduled callers report "busy" and leave no row locked.
            with Session(engine) as db:
                assert attempt_explained_halt_auto_resume(db) == {"status": "busy", "reason": ACCOUNT_BUSY_REASON}
                assert _recovery_row_is_free(engine)
            with Session(engine) as db:
                with pytest.raises(RecoveryAccountBusy):
                    resume_stock_paper_after_revalidation(
                        db, actor=auto_revalidation.AUTO_REVALIDATION_ACTOR,
                        reason="Automatic revalidation after system pause", wait_for_account=False,
                    )
                assert _recovery_row_is_free(engine)
            with Session(engine) as db, \
                 patch.object(auto_revalidation, "_fresh_monitoring_is_clear", return_value=(True, "test monitoring")):
                assert auto_revalidation.run_auto_revalidation(db) == {"status": "busy", "reason": ACCOUNT_BUSY_REASON}
                assert _recovery_row_is_free(engine)

            # Nothing above waited: reconciliation still holds the account, untouched.
            assert not reconciliation.done()
            with Session(engine) as db:
                assert db.query(StockPaperRecoveryEvent).filter_by(action="resume").count() == 0
        finally:
            gateway.release.set()
        assert result_or_fail(reconciliation, "Broker reconciliation") == "reconciled"
    with Session(engine) as db:
        # The fresh reconciliation undid the heartbeat-only pause, as it does unopposed.
        assert db.query(StockPaperRecoveryState).one().status == "resumable"
        assert db.query(StockPaperAccount).one().status == "reconciled"


def test_reconciliation_watchdog_and_resume_check_never_deadlock(ledger):
    """2026-11-10: the watchdog was the deadlock victim twice ("watchdog evaluation failed").

    Reconciliation held the account and wrote the recovery row at its end; a
    resume check held the recovery row while queued for the account; the
    watchdog queued on the recovery row in between. Run all three for real,
    with the watchdog and the resume check looping across reconciliation's
    final write.
    """
    engine = ledger.engine
    _watchdog_pause(engine)
    gateway, pids = HeldSync(), {}
    finished = Event()
    outcomes = {"watchdog": [], "resume": []}

    def loop(name, call):
        while not finished.is_set():
            with Session(engine) as db:
                outcomes[name].append(call(db)["status"])
        return len(outcomes[name])

    with ThreadPoolExecutor(max_workers=3) as pool:
        reconciliation = pool.submit(_reconcile, engine, gateway, pids)
        try:
            assert gateway.holding.wait(WAIT_SECONDS), "Reconciliation never reached the broker sync"
            watchdog = pool.submit(loop, "watchdog", run_stock_watchdog)
            resume = pool.submit(loop, "resume", attempt_explained_halt_auto_resume)
            # Both must make progress while reconciliation holds the account:
            # neither may queue behind the account row.
            deadline = time.monotonic() + WAIT_SECONDS
            while len(outcomes["watchdog"]) < 3 or len(outcomes["resume"]) < 3:
                assert time.monotonic() < deadline, f"A recovery check queued behind reconciliation: {outcomes}"
                time.sleep(0.01)
            held = {name: list(values) for name, values in outcomes.items()}
            gateway.release.set()
            status = result_or_fail(reconciliation, "Broker reconciliation")
            time.sleep(0.2)  # a few more passes over the committed result
        finally:
            gateway.release.set()
            finished.set()
        assert result_or_fail(watchdog, "Recovery watchdog") >= 3
        assert result_or_fail(resume, "Automatic resume check") >= 3

    assert status == "reconciled"
    # While the account was held: the watchdog saw the already-halted account
    # and only recorded its heartbeat; the resume check gave way every time.
    assert set(held["watchdog"]) == {"clear"}
    assert set(held["resume"]) == {"busy"}
    assert set(outcomes["watchdog"]) == {"clear"}
    assert set(outcomes["resume"]) <= {"busy", "not_halted"}
    with Session(engine) as db:
        state = db.query(StockPaperRecoveryState).one()
        assert state.status == "resumable" and state.cooldown_until is None
        assert db.query(StockPaperAccount).one().status == "reconciled"
        assert db.query(RiskRule).one().value["kill_switch_enabled"] is False
        # One pause, one restore: the looping checks changed nothing.
        assert db.query(StockPaperRecoveryEvent).filter_by(action="pause").count() == 1
        assert db.query(StockPaperRecoveryEvent).filter_by(action="automatic_restore").count() == 1


def test_pause_queued_behind_a_restoring_reconciliation_is_applied(ledger):
    """The same two rows as 2026-11-10, on the paths that still wait for the account.

    Every pause (monitor, watchdog, operator) goes through ``enter_stock_recovery``:
    recovery row, then the account row, waiting. A reconciliation that restores
    a heartbeat-only pause writes the recovery row at its end. On 2026-11-16 this
    test showed PostgreSQL killing the pause, leaving trading restored with the
    monitor's pause dropped until its next run.

    Reconciliation no longer waits for the recovery row while it holds the
    account (``_recovery_row_without_waiting``). It finds the row held, leaves
    the watchdog's halt in place for its next pass, and commits; the pause then
    gets the account and is applied.
    """
    engine = ledger.engine
    _watchdog_pause(engine)
    gateway, pids = HeldSync(), {}
    reason = "Automatic stock-paper pause after persistent monitoring breach"

    def pause():
        with Session(engine) as db:
            pids["pause"] = backend_pid(db)
            enter_stock_recovery(db, reason=reason, actor="stock_monitor")
            db.commit()
            return "paused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        reconciliation = pool.submit(_reconcile, engine, gateway, pids)
        try:
            assert gateway.holding.wait(WAIT_SECONDS), "Reconciliation never reached the broker sync"
            monitor = pool.submit(pause)
            wait_until_blocked(ledger.owner, _pid(pids, "pause"), _pid(pids, "reconcile"))
        finally:
            gateway.release.set()
        outcomes = outcomes_or_known_deadlock({"reconciliation": reconciliation, "pause": monitor})
    # Nobody was killed. Reconciliation gave way on the recovery row, so its
    # own result is the halt it left; the pause, which came last, stands.
    assert outcomes == {"reconciliation": "halted", "pause": "paused"}
    with Session(engine) as db:
        state, account = db.query(StockPaperRecoveryState).one(), db.query(StockPaperAccount).one()
        assert (state.status, state.pause_reason) == ("cooldown", reason)
        assert (account.status, account.halt_reason) == ("halted", reason)
        assert db.query(RiskRule).one().value["kill_switch_enabled"] is True
        deferred = db.query(StockPaperLedgerEvent).filter_by(event_type="reconcile", status="recovery_deferred").one()
        assert deferred.reason == RECOVERY_ROW_DEFERRED_REASON
        # The pause was never undone: no restore was written at any point.
        assert db.query(StockPaperRecoveryEvent).filter_by(action="automatic_restore").count() == 0
        assert db.query(StockPaperRecoveryEvent).filter_by(action="pause", actor="stock_monitor").count() == 1


def _hold_recovery_row(engine, held: Event, release: Event):
    """Another process holding the recovery row without waiting for anything."""
    with Session(engine) as db:
        db.execute(text("SELECT id FROM stock_paper_recovery_state WHERE id = 1 FOR UPDATE"))
        held.set()
        release.wait(WAIT_SECONDS * 4)
        db.rollback()


def _lock_timeout(engine) -> str:
    with Session(engine) as db:
        return db.execute(text("SELECT current_setting('lock_timeout')")).scalar_one()


def test_reconciliation_never_waits_for_a_held_recovery_row_and_the_next_one_restores(ledger):
    engine = ledger.engine
    _watchdog_pause(engine)
    held, release = Event(), Event()
    usual = _lock_timeout(engine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_hold_recovery_row, engine, held, release)
        try:
            assert held.wait(WAIT_SECONDS), "The holder never locked the recovery row"
            started = time.monotonic()
            with Session(engine) as db:
                assert reconcile_stock_paper_account(db, FakeAlpaca())["status"] == "halted"
            # It queued for RECOVERY_ROW_WAIT_MS and gave way; the holder is still there.
            assert time.monotonic() - started < WAIT_SECONDS / 2 and not holder.done()
            # The short timeout was that one statement's: sessions still have their own setting.
            assert _lock_timeout(engine) == usual != "500ms"
        finally:
            release.set()
        result_or_fail(holder, "Recovery row holder")
    with Session(engine) as db:
        state, account = db.query(StockPaperRecoveryState).one(), db.query(StockPaperAccount).one()
        # Everything the watchdog set is still in place: nothing was half-restored.
        assert state.status == "cooldown" and account.status == "halted"
        assert (account.halt_reason or "").startswith("Independent watchdog:")
        assert db.query(RiskRule).one().value["kill_switch_enabled"] is True
        assert db.query(StockPaperLedgerEvent).filter_by(status="recovery_deferred").count() == 1
    # With the row free, the next reconciliation does what the first one left.
    with Session(engine) as db:
        assert reconcile_stock_paper_account(db, FakeAlpaca())["status"] == "reconciled"
    with Session(engine) as db:
        state = db.query(StockPaperRecoveryState).one()
        assert state.status == "resumable" and db.query(StockPaperAccount).one().status == "reconciled"
        assert db.query(RiskRule).one().value["kill_switch_enabled"] is False
        assert db.query(StockPaperRecoveryEvent).filter_by(action="automatic_restore").count() == 1
        assert db.query(StockPaperLedgerEvent).filter_by(status="recovery_deferred").count() == 1


def test_a_recovery_row_held_for_a_moment_does_not_defer_the_restore(ledger):
    """A holder that is not waiting for the account (a heartbeat write) is gone within the retries."""
    engine = ledger.engine
    _watchdog_pause(engine)
    held, release = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(_hold_recovery_row, engine, held, release)
        try:
            assert held.wait(WAIT_SECONDS), "The holder never locked the recovery row"
            pids = {}
            reconciliation = pool.submit(_reconcile, engine, FakeAlpaca(), pids)
            time.sleep(0.2)  # well inside RECOVERY_ROW_WAIT_MS
        finally:
            release.set()
        assert result_or_fail(reconciliation, "Broker reconciliation") == "reconciled"
        result_or_fail(holder, "Recovery row holder")
    with Session(engine) as db:
        assert db.query(StockPaperRecoveryState).one().status == "resumable"
        assert db.query(StockPaperLedgerEvent).filter_by(status="recovery_deferred").count() == 0


# --- (b) dispatch_reserved_order against reconciliation ---


@pytest.fixture()
def reserved_sell(ledger):
    """An initialized account holding one SPY share and a committed sell reservation for it."""
    now = datetime.now(timezone.utc)
    with patch("app.services.stock_paper_ledger.session_bounds", return_value=session_window_around(now)), \
         patch("app.services.stock_paper_ledger.feed_status", return_value={"status": "ready"}), \
         patch("app.services.stock_paper_ledger._validate_reference_price"):
        with Session(ledger.engine) as db:
            initialize_stock_paper_account(db, FakeAlpaca(positions=[POSITION]))
            order = reserve_stock_paper_order(
                db, symbol="SPY", side="sell", quantity=Decimal("1"), reference_price=Decimal("100"),
                idempotency_key="postgres-dispatch-race", source="manual_close",
            )
            order_id, client_order_id = order.id, order.client_order_id
        yield ledger, order_id, client_order_id


def _dispatch(engine, order_id, broker, pids):
    with Session(engine) as db:
        pids["dispatch"] = backend_pid(db)
        return dispatch_reserved_order(db, order_id, broker).status


def _order_and_account(engine, order_id):
    with Session(engine) as db:
        order, account = db.get(StockPaperOrder, order_id), db.query(StockPaperAccount).one()
        return (order.status, order.broker_order_id, order.uncertain_submission), (account.status, account.halt_reason)


def test_dispatch_waits_for_reconciliation_then_submits_once(reserved_sell):
    ledger, order_id, _ = reserved_sell
    engine = ledger.engine
    gateway, broker, pids = HeldSync(positions=[POSITION]), AcceptingBroker(), {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        reconciliation = pool.submit(_reconcile, engine, gateway, pids)
        try:
            assert gateway.holding.wait(WAIT_SECONDS), "Reconciliation never reached the broker sync"
            dispatch = pool.submit(_dispatch, engine, order_id, broker, pids)
            # Dispatch holds its order row and queues for the account row.
            wait_until_blocked(ledger.owner, _pid(pids, "dispatch"), _pid(pids, "reconcile"))
            assert broker.calls == 0, "An order was POSTed while reconciliation held the account"
        finally:
            gateway.release.set()
        assert result_or_fail(reconciliation, "Broker reconciliation") == "reconciled"
        assert result_or_fail(dispatch, "Order dispatch") == "accepted"
    assert broker.calls == 1
    assert _order_and_account(engine, order_id) == (("accepted", "broker-1", False), ("reconciled", None))
    with Session(engine) as db:
        assert db.query(StockPaperLedgerEvent).filter_by(event_type="order_submission", status="accepted").count() == 1


def test_dispatch_queued_behind_a_halting_reconciliation_never_posts(reserved_sell):
    ledger, order_id, _ = reserved_sell
    engine = ledger.engine
    with Session(engine) as db:
        account = db.query(StockPaperAccount).one()
        # An earlier submission the broker cannot answer for: this reconciliation halts the account.
        db.add(StockPaperOrder(
            account_id=account.id, client_order_id="unanswered-1", symbol="QQQ", side="buy", quantity=Decimal("1"),
            order_type="limit", time_in_force="day", limit_price=Decimal("10"), reserved_cash=Decimal("10"),
            status="submitting", source="manual_control_room", submission_attempted_at=datetime.now(timezone.utc),
        ))
        db.commit()
    gateway, broker, pids = HeldSync(positions=[POSITION]), AcceptingBroker(), {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        reconciliation = pool.submit(_reconcile, engine, gateway, pids)
        try:
            assert gateway.holding.wait(WAIT_SECONDS), "Reconciliation never reached the broker sync"
            dispatch = pool.submit(_dispatch, engine, order_id, broker, pids)
            wait_until_blocked(ledger.owner, _pid(pids, "dispatch"), _pid(pids, "reconcile"))
        finally:
            gateway.release.set()
        assert result_or_fail(reconciliation, "Broker reconciliation") == "halted"
        # Dispatch decided on the account row as reconciliation committed it,
        # not on the reconciled row it could see before it queued.
        with pytest.raises(StockPaperError, match="cannot dispatch until stock paper account is reconciled"):
            result_or_fail(dispatch, "Order dispatch")
    assert broker.calls == 0
    assert _order_and_account(engine, order_id) == (("reserved", None, False), ("halted", UNANSWERED_ORDER_HALT))


def test_reconciliation_during_an_order_post_fails_closed_and_the_next_one_clears(reserved_sell):
    ledger, order_id, client_order_id = reserved_sell
    engine = ledger.engine
    broker, pids = AcceptingBroker(hold=True), {}
    with ThreadPoolExecutor(max_workers=1) as pool:
        dispatch = pool.submit(_dispatch, engine, order_id, broker, pids)
        try:
            assert broker.posting.wait(WAIT_SECONDS), "Dispatch never reached the broker POST"
            # The POST is in flight and no row is locked: a whole reconciliation
            # runs. The broker does not list the order yet, so it must halt,
            # never cancel, release or re-send the order.
            during = FakeAlpaca(positions=[POSITION])
            with Session(engine) as db:
                assert reconcile_stock_paper_account(db, during)["status"] == "halted"
            assert during.lookups == 1
            assert _order_and_account(engine, order_id) == (("submitting", None, False), ("halted", UNANSWERED_ORDER_HALT))
        finally:
            broker.finish.set()
        assert result_or_fail(dispatch, "Order dispatch") == "accepted"
    assert broker.calls == 1
    assert _order_and_account(engine, order_id) == (("accepted", "broker-1", False), ("halted", UNANSWERED_ORDER_HALT))
    # The next pass sees the order at the broker and clears the uncertain-order halt.
    with Session(engine) as db:
        after = FakeAlpaca(positions=[POSITION], orders=list(broker.accepted))
        assert reconcile_stock_paper_account(db, after)["status"] == "reconciled"
    assert _order_and_account(engine, order_id) == (("accepted", "broker-1", False), ("reconciled", None))
    with Session(engine) as db:
        assert db.query(StockPaperOrder).filter_by(client_order_id=client_order_id).count() == 1


def test_order_post_returning_during_reconciliation_keeps_one_accepted_order(reserved_sell):
    """Reconciliation takes the account while an order POST is in flight, then imports that order.

    Dispatch commits "submitting" and releases its locks before the POST, so a
    queued reconciliation is granted the account at that moment. The POST
    returns while reconciliation is still in its broker sync, and both then
    write the same order row. On 2026-11-16 this test showed PostgreSQL
    killing dispatch: the accepted order was recorded as "unknown" and the
    account halted until the next reconciliation looked the order up.

    Dispatch now takes the account row before the order row once the broker
    has replied (``_account_then_order``), so it waits for the reconciliation
    and then records the order that reconciliation already imported.
    """
    ledger, order_id, client_order_id = reserved_sell
    engine = ledger.engine
    broker, pids = AcceptingBroker(hold=True), {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        dispatch = pool.submit(_dispatch, engine, order_id, broker, pids)
        gateway = HeldSync(positions=[POSITION])
        try:
            assert broker.posting.wait(WAIT_SECONDS), "Dispatch never reached the broker POST"
            # By the time reconciliation asks, the broker has the order.
            gateway.lookup_rows = {client_order_id: {
                "id": "broker-1", "status": "accepted", "client_order_id": client_order_id, "symbol": "SPY",
                "side": "sell", "qty": "1", "type": "limit", "time_in_force": "day", "limit_price": "100",
            }}
            reconciliation = pool.submit(_reconcile, engine, gateway, pids)
            assert gateway.holding.wait(WAIT_SECONDS), "Reconciliation never reached the broker sync"
            broker.finish.set()
            # Dispatch records the accepted order; its ledger event references
            # the account row reconciliation holds, so it waits here.
            wait_until_blocking(ledger.owner, _pid(pids, "reconcile"))
        finally:
            broker.finish.set()
            gateway.release.set()
        outcomes = outcomes_or_known_deadlock({"reconciliation": reconciliation, "dispatch": dispatch})
    assert outcomes == {"reconciliation": "reconciled", "dispatch": "accepted"}
    assert broker.calls == 1
    assert _order_and_account(engine, order_id) == (("accepted", "broker-1", False), ("reconciled", None))
