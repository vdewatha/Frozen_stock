"""Durable delivery and governance around the verified stock training pipeline."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    StockDatasetSnapshot, StockHoldoutConsumption, StockHoldoutReservation,
    StockModelLifecycleEvent, StockModelLifecycleState, StockModelRegistry, StockPaperBindingState,
    StockPaperModelBinding, StockTrainingJob,
)
from app.services.stock_dataset import (
    StockDataset,
    UnverifiedStockDataset,
    create_stock_dataset,
    persist_stock_dataset_snapshot,
    publish_stock_dataset,
    verify_stock_dataset_artifact,
)
from app.services.stock_training import (
    TrainingCancelled, train_stock_model, validate_stock_model_artifact,
)

MAX_ATTEMPTS = 3
MAX_DELIVERY_ATTEMPTS = 3
MAX_ACTIVE_JOBS = 4
LEASE_SECONDS = 15 * 60
STOCK_MODEL_LIFECYCLE_STATES = (
    "challenger", "eligible", "paper_canary", "champion", "demoted", "retired",
)
STOCK_MODEL_LIFECYCLE_ACTIONS = {
    "mark_eligible": {"challenger": "eligible", "demoted": "eligible"},
    "start_canary": {"challenger": "paper_canary", "eligible": "paper_canary", "demoted": "paper_canary"},
    "promote": {"paper_canary": "champion"},
    "demote": {"paper_canary": "demoted", "champion": "demoted"},
    "retire": {"challenger": "retired", "eligible": "retired", "paper_canary": "retired", "demoted": "retired"},
}


class StockTrainingError(ValueError):
    pass


class HoldoutAlreadyReserved(StockTrainingError):
    pass


class AmbiguousHoldoutConsumption(StockTrainingError):
    """A final holdout was durably consumed but has no publishable result."""


def _lock_lifecycle_admission(db: Session) -> None:
    """Serialize lifecycle transitions and active-binding replacement."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(781928344202)"))
    elif dialect == "sqlite" and not db.in_transaction():
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _active_binding_state(db: Session, *, for_update: bool = True) -> StockPaperBindingState | None:
    statement = select(StockPaperBindingState).where(StockPaperBindingState.id == 1)
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def _current_lifecycle(
    db: Session, model: StockModelRegistry, *, for_update: bool = True,
) -> StockModelLifecycleState:
    statement = select(StockModelLifecycleState).where(
        StockModelLifecycleState.model_run_id == model.run_id
    )
    if for_update:
        statement = statement.with_for_update()
    state = db.scalar(statement)
    if state is None:
        state = StockModelLifecycleState(
            model_run_id=model.run_id,
            lifecycle_state=model.lifecycle_state or "challenger",
            updated_by="system",
            reason="Initialized from immutable model registry state",
        )
        db.add(state)
        db.flush()
    return state


def get_stock_model_lifecycle_state(db: Session, model_run_id: str) -> str:
    model = db.get(StockModelRegistry, model_run_id)
    if model is None:
        raise StockTrainingError("Stock model not found")
    state = db.scalar(
        select(StockModelLifecycleState.lifecycle_state).where(
            StockModelLifecycleState.model_run_id == model_run_id
        )
    )
    return state or model.lifecycle_state or "challenger"


def _lifecycle_event(
    db: Session, *, model: StockModelRegistry, from_state: str | None, to_state: str,
    action: str, actor: str, reason: str, binding_id: int | None = None,
) -> StockModelLifecycleEvent:
    if to_state not in STOCK_MODEL_LIFECYCLE_STATES:
        raise StockTrainingError("Unsupported stock model lifecycle state")
    event_identity = {
        "event_nonce": uuid4().hex,
        "model_run_id": model.run_id,
        "binding_id": binding_id,
        "from_state": from_state,
        "to_state": to_state,
        "action": action,
        "actor": actor,
        "reason": reason.strip(),
        "created_at": _utc(_now()).isoformat(),
    }
    event = StockModelLifecycleEvent(
        model_run_id=model.run_id, binding_id=binding_id, from_state=from_state,
        to_state=to_state, action=action, actor=actor, reason=reason.strip(),
        event_sha256=hashlib.sha256(_canonical(event_identity)).hexdigest(),
    )
    db.add(event)
    return event


def _transition_model(
    db: Session, *, model: StockModelRegistry, action: str, actor: str, reason: str,
    binding_id: int | None = None,
) -> StockModelLifecycleEvent:
    if not reason.strip():
        raise StockTrainingError("A lifecycle transition reason is required")
    allowed = STOCK_MODEL_LIFECYCLE_ACTIONS.get(action)
    current = _current_lifecycle(db, model)
    if allowed is None or current.lifecycle_state not in allowed:
        raise StockTrainingError(
            f"Invalid lifecycle transition: {current.lifecycle_state} cannot perform {action}"
        )
    from_state = current.lifecycle_state
    to_state = allowed[from_state]
    current.lifecycle_state = to_state
    current.updated_by, current.reason, current.updated_at = actor, reason.strip(), _now()
    return _lifecycle_event(
        db, model=model, from_state=from_state, to_state=to_state,
        action=action, actor=actor, reason=reason, binding_id=binding_id,
    )


def transition_stock_model_lifecycle(
    db: Session, *, model_run_id: str, action: str, actor: str, reason: str,
) -> StockModelRegistry:
    """Apply one explicit, auditable lifecycle transition.

    Promotion is intentionally not inferred from training, binding, or a
    scheduled job. It requires the model to be the active paper canary.
    """
    if action not in STOCK_MODEL_LIFECYCLE_ACTIONS:
        raise StockTrainingError("Unsupported stock model lifecycle action")
    _lock_lifecycle_admission(db)
    model = db.scalar(
        select(StockModelRegistry)
        .where(StockModelRegistry.run_id == model_run_id)
        .with_for_update()
    )
    if model is None:
        raise StockTrainingError("Stock model not found")
    current = _current_lifecycle(db, model)
    state = _active_binding_state(db)
    active_binding = db.get(StockPaperModelBinding, state.active_binding_id) if state else None
    if action == "start_canary" and (
        active_binding is None or active_binding.model_run_id != model.run_id
    ):
        raise StockTrainingError(
            "A paper canary must be created through an active paper binding"
        )
    if action == "promote":
        if active_binding is None or active_binding.model_run_id != model.run_id:
            raise StockTrainingError("Only the active paper canary may be promoted")
        champions = db.scalars(
            select(StockModelLifecycleState)
            .where(
                StockModelLifecycleState.lifecycle_state == "champion",
                StockModelLifecycleState.model_run_id != model.run_id,
            )
            .with_for_update()
        ).all()
        if champions:
            raise StockTrainingError("An existing champion must be explicitly demoted before promotion")
        _transition_model(
            db, model=model, action=action, actor=actor, reason=reason,
            binding_id=active_binding.id,
        )
    else:
        _transition_model(db, model=model, action=action, actor=actor, reason=reason)
        if action in {"demote", "retire"} and active_binding and active_binding.model_run_id == model.run_id:
            db.delete(state)
            db.flush()
    return model


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes despite timezone=True columns."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:1000] or exc.__class__.__name__


def _root() -> Path:
    root = Path(settings.stock_training_artifact_root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise StockTrainingError("Configured stock training artifact root is invalid")
    return root


def _lock_training_admission(db: Session) -> None:
    """Serialize new snapshots/jobs and overlapping-holdout checks.

    PostgreSQL's transaction-scoped advisory lock is authoritative in
    production and covers the check/insert race across web workers. SQLite
    has no advisory locks; BEGIN IMMEDIATE obtains its corresponding
    single-writer transaction lock for focused tests and local deployments.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # Stable, application-specific signed bigint; no user input is passed
        # to the lock primitive.
        db.execute(text("SELECT pg_advisory_xact_lock(781928344201)"))
    elif dialect == "sqlite":
        # A Session has not issued a statement at admission call sites. If a
        # caller already opened its transaction, the Python mutex still makes
        # check/insert deterministic in this process.
        if not db.in_transaction():
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _adopt_snapshot_artifact(dataset: StockDataset, output: Path) -> StockDataset:
    """Validate a previously atomically-published orphan before adopting it.

    This covers a process crash after `os.rename` but before the surrounding
    database transaction commits. No current MarketPrice rows are trusted
    beyond the already-created deterministic dataset passed to this function.
    """
    root = Path(output) / dataset.snapshot_id
    manifest_path = root / "snapshot.json"
    if root.is_symlink() or not manifest_path.is_file():
        raise StockTrainingError("Existing stock snapshot artifact cannot be safely adopted")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StockTrainingError("Existing stock snapshot artifact has an invalid manifest") from exc
    identity = ("snapshot_id", "dataset_sha256", "identity_sha256", "provider", "universe", "cutoff_date", "feature_config_id", "horizon_days")
    if any(manifest.get(key) != dataset.metadata.get(key) for key in identity):
        raise StockTrainingError("Existing stock snapshot artifact has a different immutable identity")
    metadata = dataset.metadata | {
        "artifact_path": str(root.absolute()),
        "artifact_data_path": manifest.get("artifact_data_path"),
        "artifact_sha256": dataset.dataset_sha256,
        "snapshot_manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }
    adopted = StockDataset(dataset.snapshot_id, dataset.dataset_sha256, dataset.identity_sha256, dataset.observations, metadata)
    verify_stock_dataset_artifact(adopted)
    return adopted


def _dataset_from_record(row: StockDatasetSnapshot) -> StockDataset:
    metadata = dict(row.metadata_json)
    required = {
        "snapshot_id": row.snapshot_id, "dataset_sha256": row.dataset_sha256,
        "artifact_path": row.artifact_path, "artifact_sha256": row.artifact_sha256,
        "provider": row.provider, "universe": row.universe, "horizon_days": row.horizon_days,
        "feature_config_id": row.feature_config_id,
    }
    if any(metadata.get(key) != value for key, value in required.items()):
        raise StockTrainingError("Durable dataset snapshot metadata does not match its immutable registry row")
    try:
        dataset_path = verify_stock_dataset_artifact(
            StockDataset(row.snapshot_id, row.dataset_sha256, metadata["identity_sha256"], pd.DataFrame(), metadata)
        )
        # The snapshot serializer uses 17-significant-digit floats. Pandas'
        # default fast parser may choose a neighboring IEEE value, so request
        # round-trip parsing before the trainer rechecks the immutable hash.
        observations = pd.read_csv(
            dataset_path,
            dtype={"symbol": str, "date": str, "provider": str, "imported_at": str},
            float_precision="round_trip",
        )
    except (KeyError, OSError, UnverifiedStockDataset, ValueError) as exc:
        raise StockTrainingError(f"Verified dataset snapshot is unavailable or invalid: {_error(exc)}") from exc
    return StockDataset(row.snapshot_id, row.dataset_sha256, metadata["identity_sha256"], observations, metadata)


def _reserve_holdout(db: Session, dataset: StockDataset, job_id: str) -> StockHoldoutReservation:
    dates = pd.to_datetime(dataset.observations["date"], utc=True, errors="raise").sort_values().unique()
    if len(dates) < 30:
        raise StockTrainingError("Verified dataset has too few distinct dates for a controlled holdout")
    start = pd.Timestamp(dates[int(len(dates) * .8)]).to_pydatetime()
    end = (pd.Timestamp(dates[-1]) + timedelta(days=int(dataset.metadata["horizon_days"]) + 1)).to_pydatetime()
    universe_key = dataset.metadata["universe_sha256"]
    horizon = int(dataset.metadata["horizon_days"])
    existing = db.scalars(select(StockHoldoutReservation).with_for_update()).all()
    symbols = set(dataset.metadata["universe"])
    if any(
        symbols.intersection(set(row.universe))
        and _utc(row.period_start) < end and start < _utc(row.period_end)
        for row in existing
    ):
        raise HoldoutAlreadyReserved("Holdout interval overlaps an immutable reservation for at least one symbol")
    row = StockHoldoutReservation(
        universe_key=universe_key, universe=sorted(symbols), horizon_days=horizon, period_start=start, period_end=end,
        snapshot_id=dataset.snapshot_id, reserved_by_job_id=job_id, purpose="model_selection",
    )
    db.add(row)
    db.flush()
    return row


def create_stock_training_job(
    db: Session, *, symbols: list[str], horizon_bars: int, actor: str, cutoff_at: date,
    provider: str = "yfinance", trigger: str = "manual", seed: int = 42,
) -> tuple[StockTrainingJob, bool]:
    if type(horizon_bars) is not int or not 1 <= horizon_bars <= 252 or type(seed) is not int:
        raise StockTrainingError("A supported integer horizon and seed are required")
    if trigger not in {"manual", "scheduled"}:
        raise StockTrainingError("Only manual or scheduled challenger triggers are allowed")
    _lock_training_admission(db)
    try:
        dataset = create_stock_dataset(
            db, cutoff=cutoff_at, universe=sorted({str(symbol).strip().upper() for symbol in symbols}),
            horizon_days=horizon_bars, provider=provider,
        )
        existing_snapshot = db.get(StockDatasetSnapshot, dataset.snapshot_id)
        if existing_snapshot is None:
            captured_at = _now()
            historical = cutoff_at < captured_at.date()
            # This first-publication timestamp is part of the immutable
            # manifest. Binding eligibility is therefore not re-evaluated
            # against the wall clock when an operator later binds a model.
            dataset = StockDataset(
                dataset.snapshot_id, dataset.dataset_sha256, dataset.identity_sha256, dataset.observations,
                dataset.metadata | {
                    "captured_at": captured_at.isoformat(),
                    "binding_eligible": not historical,
                    "binding_eligibility_reason": (
                        "Historical cutoff precedes immutable capture date; research-only, nonbinding artifact"
                        if historical else
                        "Current-cutoff paper-only artifact; adjusted prices are not point-in-time corporate-action data"
                    ),
                    "adjustments_point_in_time": False,
                },
            )
            try:
                dataset = publish_stock_dataset(dataset, _root() / "snapshots")
            except FileExistsError:
                dataset = _adopt_snapshot_artifact(dataset, _root() / "snapshots")
            snapshot = persist_stock_dataset_snapshot(db, dataset, snapshot_model=StockDatasetSnapshot)
        else:
            snapshot = existing_snapshot
            dataset = _dataset_from_record(snapshot)
    except (UnverifiedStockDataset, OSError, ValueError) as exc:
        raise StockTrainingError(f"Verified dataset snapshot rejected: {_error(exc)}") from exc
    dedupe_key = hashlib.sha256(_canonical({
        "snapshot_id": snapshot.snapshot_id, "horizon_bars": horizon_bars, "seed": seed,
    })).hexdigest()
    duplicate = db.scalar(select(StockTrainingJob).where(StockTrainingJob.dedupe_key == dedupe_key).order_by(StockTrainingJob.created_at.desc()))
    if duplicate is not None:
        return duplicate, True
    # A global admission ceiling protects the learning queue. A scheduler
    # defers rather than treating capacity exhaustion as a model failure.
    active_count = len(db.scalars(select(StockTrainingJob.id).where(
        StockTrainingJob.status.in_(("queued", "running", "cancel_requested"))
    ).with_for_update()).all())
    if active_count >= MAX_ACTIVE_JOBS:
        if trigger != "scheduled":
            raise StockTrainingError("Stock training admission is full; retry after an active job completes")
        reservation = None
        status, deferred_reason = "deferred", "Scheduled challenger deferred: bounded training admission is full"
    else:
        reservation = None
        status, deferred_reason = "queued", None
    job_id = str(uuid4())
    if status == "queued":
        try:
            reservation = _reserve_holdout(db, dataset, job_id)
        except HoldoutAlreadyReserved:
            if trigger != "scheduled":
                raise
            # A rolling challenger never reuses an evaluation holdout. The
            # core trainer requires one final untouched holdout, so defer it
            # until a sufficiently new, disjoint interval exists.
            status = "deferred"
            deferred_reason = "Scheduled challenger deferred: no fresh untouched holdout interval is available"
    job = StockTrainingJob(
        id=job_id, dedupe_key=dedupe_key, trigger=trigger, status=status, requested_by=actor,
        request_payload={
            "symbols": list(dataset.metadata["universe"]), "horizon_bars": horizon_bars, "seed": seed,
            "provider": provider, "cutoff_at": dataset.metadata["cutoff_date"], "paper_only": True,
        },
        snapshot_id=snapshot.snapshot_id, holdout_reservation_id=reservation.id if reservation else None,
        queue_error=deferred_reason,
    )
    db.add(job)
    try:
        db.flush()
    except IntegrityError as exc:
        # The named dedupe constraint is the final admission guard when an
        # external transaction races this process.
        db.rollback()
        duplicate = db.scalar(select(StockTrainingJob).where(StockTrainingJob.dedupe_key == dedupe_key))
        if duplicate is not None:
            return duplicate, True
        raise StockTrainingError("Stock training admission could not be serialized") from exc
    return job, False


def enqueue_stock_training_job(db: Session, job: StockTrainingJob) -> StockTrainingJob:
    if job.status != "queued" or job.delivery_attempts >= MAX_DELIVERY_ATTEMPTS or job.celery_task_id:
        return job
    # Count delivery requests, including broker errors, so a permanently
    # unavailable broker cannot create unbounded delivery churn.
    job.delivery_attempts += 1
    job.recovery_attempted_at = _now()
    try:
        from app.tasks.jobs import stock_training_job
        task = stock_training_job.apply_async(args=[job.id], queue="learning")
        job.celery_task_id, job.queue_error = task.id, None
    except Exception as exc:
        job.queue_error = f"Queue delivery unavailable: {exc.__class__.__name__}: {_error(exc)}"
    return job


def request_stock_training_cancel(db: Session, job_id: str) -> StockTrainingJob:
    row = db.get(StockTrainingJob, job_id)
    if row is None:
        raise StockTrainingError("Stock training job not found")
    if row.status == "queued":
        row.status, row.completed_at = "cancelled", _now()
    elif row.status == "running":
        row.status = "cancel_requested"
    elif row.status == "deferred":
        row.status, row.completed_at = "cancelled", _now()
    elif row.status not in {"cancelled", "succeeded", "failed"}:
        raise StockTrainingError("Stock training job cannot be cancelled")
    return row


def _register_stock_model(db: Session, dataset: StockDataset, manifest: dict, output: Path) -> StockModelRegistry:
    run_id = manifest["run_id"]
    location = output / run_id
    manifest_path = location / "manifest.json"
    if location.is_symlink() or not manifest_path.is_file():
        raise StockTrainingError("Immutable model artifact is missing")
    stored = manifest_path.read_bytes()
    digest = hashlib.sha256(stored).hexdigest()
    if json.loads(stored) != manifest or manifest.get("dataset_snapshot_id") != dataset.snapshot_id:
        raise StockTrainingError("Immutable model manifest does not match its verified dataset")
    if any(hashlib.sha256((location / name).read_bytes()).hexdigest() != value for name, value in manifest.get("files", {}).items()):
        raise StockTrainingError("Immutable model artifact file digest mismatch")
    existing = db.get(StockModelRegistry, run_id)
    if existing is not None:
        if existing.manifest_sha256 != digest or existing.snapshot_id != dataset.snapshot_id:
            raise StockTrainingError("Stock model registry identity cannot be overwritten")
        return existing
    row = StockModelRegistry(
        run_id=run_id, snapshot_id=dataset.snapshot_id, manifest_sha256=digest,
        artifact_path=str(location), training_metadata=manifest,
    )
    db.add(row)
    db.flush()
    db.add(StockModelLifecycleState(
        model_run_id=run_id, lifecycle_state="challenger", updated_by="training",
        reason="Model publication creates a challenger only",
    ))
    return row


def validate_registered_stock_model(model: StockModelRegistry, dataset: StockDataset) -> dict:
    """Revalidate immutable registry evidence before it is reported or bound."""
    location = Path(model.artifact_path)
    models_root = (_root() / "models").resolve()
    try:
        resolved = location.resolve(strict=True)
    except OSError as exc:
        raise StockTrainingError("Registered model artifact is missing") from exc
    if models_root not in resolved.parents or resolved == models_root:
        raise StockTrainingError("Registered model artifact path is outside the immutable model root")
    manifest_path = resolved / "manifest.json"
    try:
        raw = manifest_path.read_bytes()
        stored = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StockTrainingError("Registered model manifest is unavailable or invalid") from exc
    if hashlib.sha256(raw).hexdigest() != model.manifest_sha256:
        raise StockTrainingError("Registered model manifest hash mismatch")
    if stored != model.training_metadata:
        raise StockTrainingError("Registered model manifest differs from immutable registry metadata")
    result_fields = {
        "selected_model", "walkforward_metrics", "calibration_metrics",
        "final_holdout_metrics", "final_holdout_baseline", "development_rows",
        "calibration_rows", "final_holdout_rows", "final_holdout_start",
        "final_holdout_end", "purged_expanding_walkforward", "limitations",
        "final_holdout_evaluation_count", "final_holdout_consumption",
    }
    expected = {key: value for key, value in stored.items() if key not in result_fields | {"files"}}
    try:
        verified = validate_stock_model_artifact(resolved, expected_manifest=expected)
    except (OSError, ValueError) as exc:
        raise StockTrainingError(f"Registered model artifact integrity check failed: {_error(exc)}") from exc
    if verified != stored or model.snapshot_id != dataset.snapshot_id:
        raise StockTrainingError("Registered model does not match the verified dataset snapshot")
    return verified


def _adopt_model_artifact(dataset: StockDataset, output: Path, seed: int) -> dict:
    """Safely adopt a completed orphan model artifact after worker crash.

    The trainer publishes an immutable directory last. We accept only one
    manifest whose verified dataset binding and seed exactly match this job;
    `_register_stock_model` subsequently verifies every listed artifact hash.
    """
    candidates: list[dict] = []
    if not Path(output).is_dir():
        raise StockTrainingError("No immutable model artifact is available for adoption")
    for location in Path(output).iterdir():
        manifest_path = location / "manifest.json"
        if location.is_symlink() or not location.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        if (
            manifest.get("run_id") == location.name
            and manifest.get("dataset_snapshot_id") == dataset.snapshot_id
            and manifest.get("dataset_sha256") == dataset.dataset_sha256
            and manifest.get("seed") == seed
        ):
            candidates.append(manifest)
    if len(candidates) != 1:
        raise StockTrainingError("Immutable model artifact adoption is ambiguous or unavailable")
    return candidates[0]


def _owned_update(db: Session, job_id: str, attempt: int, statuses: tuple[str, ...], **values) -> bool:
    """Fence a worker mutation to the attempt generation it successfully claimed."""
    return bool(db.execute(update(StockTrainingJob).where(
        StockTrainingJob.id == job_id,
        StockTrainingJob.attempts == attempt,
        StockTrainingJob.status.in_(statuses),
    ).values(**values)).rowcount)


def _consume_final_holdout(db: Session, *, job_id: str, attempt: int) -> dict:
    """Fence and commit the one permitted final-holdout evaluation before it runs."""
    db.expire_all()
    job = db.get(StockTrainingJob, job_id)
    if (
        job is None or job.attempts != attempt or job.status != "running"
        or job.holdout_reservation_id is None
    ):
        db.rollback()
        raise TrainingCancelled("Stock training worker no longer owns its holdout reservation")
    consumed_at = _now()
    claim_sha256 = hashlib.sha256(_canonical({
        "reservation_id": job.holdout_reservation_id, "job_id": job_id,
        "attempt": attempt, "consumed_at": _utc(consumed_at).isoformat(),
    })).hexdigest()
    # This update and the unique-reservation insertion are one transaction.
    # A reclaimed/stale worker cannot create a consumption record, and a
    # second owner cannot create a second one for the same reserved interval.
    if not _owned_update(
        db, job_id, attempt, ("running",),
        heartbeat_at=consumed_at, lease_expires_at=consumed_at + timedelta(seconds=LEASE_SECONDS),
    ):
        db.rollback()
        raise TrainingCancelled("Stock training worker lost ownership before holdout consumption")
    db.add(StockHoldoutConsumption(
        reservation_id=job.holdout_reservation_id, job_id=job_id, attempt=attempt,
        claim_sha256=claim_sha256, consumed_at=consumed_at,
    ))
    try:
        db.flush()
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise AmbiguousHoldoutConsumption(
            "ambiguous_holdout_consumption: reserved final holdout was already consumed without a reusable artifact"
        ) from error
    return {
        "count": 1,
        "claim_sha256": claim_sha256,
        "reservation_id": job.holdout_reservation_id,
        "job_id": job_id,
        "attempt": attempt,
        "consumed_at": _utc(consumed_at).isoformat(),
    }


def _validate_holdout_consumption(
    db: Session, *, job: StockTrainingJob, manifest: dict,
) -> None:
    """Bind a completed model artifact to the durable one-use ledger entry."""
    evidence = manifest.get("final_holdout_consumption")
    if not isinstance(evidence, dict) or type(evidence.get("count")) is not int or evidence["count"] != 1:
        raise StockTrainingError("Immutable model artifact lacks one-use final holdout evidence")
    if job.holdout_reservation_id is None:
        raise StockTrainingError("Training job has no durable final holdout reservation")
    consumed = db.scalar(select(StockHoldoutConsumption).where(
        StockHoldoutConsumption.reservation_id == job.holdout_reservation_id
    ))
    if consumed is None:
        raise StockTrainingError("Immutable model artifact has no durable final holdout consumption record")
    expected = {
        "count": 1,
        "claim_sha256": consumed.claim_sha256,
        "reservation_id": consumed.reservation_id,
        "job_id": consumed.job_id,
        "attempt": consumed.attempt,
        "consumed_at": _utc(consumed.consumed_at).isoformat(),
    }
    if evidence != expected or manifest.get("final_holdout_evaluation_count") != 1:
        raise StockTrainingError("Immutable model artifact final holdout evidence does not match its reservation")


def run_stock_training_job(db: Session, job_id: str) -> dict:
    row = db.get(StockTrainingJob, job_id)
    if row is None:
        return {"status": "missing", "job_id": job_id}
    if row.status in {"cancelled", "cancel_requested"}:
        if row.status == "cancel_requested":
            row.status, row.completed_at, row.lease_expires_at = "cancelled", _now(), None
            db.commit()
        return {"status": "cancelled", "job_id": job_id}
    if row.status != "queued":
        return {"status": row.status, "job_id": job_id}
    now = _now()
    # Compare-and-swap claim makes duplicate Celery delivery harmless. Exactly
    # one worker can transition queued -> running and own the lease.
    owned_attempt = db.execute(update(StockTrainingJob).where(
        StockTrainingJob.id == job_id,
        StockTrainingJob.status == "queued",
        StockTrainingJob.attempts < MAX_ATTEMPTS,
    ).values(
        status="running", started_at=now, heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
        queue_error=None, attempts=StockTrainingJob.attempts + 1,
    ).returning(StockTrainingJob.attempts)).scalar_one_or_none()
    db.commit()
    if owned_attempt is None:
        row = db.get(StockTrainingJob, job_id)
        if row is not None and row.status == "queued" and row.attempts >= MAX_ATTEMPTS:
            row.status, row.failure_code, row.failure_detail, row.completed_at = "failed", "attempt_limit", "Bounded retry limit reached", _now()
            db.commit()
        return {"status": row.status if row else "missing", "job_id": job_id}
    try:
        snapshot = db.get(StockDatasetSnapshot, row.snapshot_id)
        if snapshot is None:
            raise StockTrainingError("Training job references no durable snapshot")
        dataset = _dataset_from_record(snapshot)
        output = _root() / "models"
        def cancel_requested() -> bool:
            # A worker keeps its own SQLAlchemy session, so expire cached state
            # before every cooperative checkpoint and observe a cancellation
            # committed by the API in another transaction.
            db.expire_all()
            current = db.get(StockTrainingJob, job_id)
            if current is None or current.attempts != owned_attempt or current.status == "cancel_requested":
                return True
            beat = _now()
            if not _owned_update(
                db, job_id, owned_attempt, ("running",),
                heartbeat_at=beat, lease_expires_at=beat + timedelta(seconds=LEASE_SECONDS),
            ):
                return True
            db.commit()
            return False
        try:
            manifest = train_stock_model(
                dataset, output, seed=row.request_payload["seed"],
                cancel_requested=cancel_requested,
                before_holdout=lambda: _consume_final_holdout(
                    db, job_id=job_id, attempt=owned_attempt,
                ),
            )
        except FileExistsError:
            manifest = _adopt_model_artifact(dataset, output, row.request_payload["seed"])
        db.expire_all()
        row = db.get(StockTrainingJob, job_id)
        if row is None or row.attempts != owned_attempt:
            return {"status": "superseded", "job_id": job_id}
        if row.status == "cancel_requested":
            if _owned_update(
                db, job_id, owned_attempt, ("cancel_requested",),
                status="cancelled", completed_at=_now(), lease_expires_at=None,
            ):
                db.commit()
            return {"status": "cancelled", "job_id": job_id}
        if row.status != "running":
            return {"status": "superseded", "job_id": job_id}
        _validate_holdout_consumption(db, job=row, manifest=manifest)
        model = _register_stock_model(db, dataset, manifest, output)
        if not _owned_update(
            db, job_id, owned_attempt, ("running",),
            result_run_id=model.run_id, status="succeeded", completed_at=_now(), lease_expires_at=None,
        ):
            db.rollback()  # Do not leave a stale worker's registry insert.
            return {"status": "superseded", "job_id": job_id}
        db.commit()
        return {"status": "succeeded", "job_id": job_id, "run_id": model.run_id, "paper_only": True, "live_authorized": False}
    except TrainingCancelled:
        db.rollback()
        if _owned_update(
            db, job_id, owned_attempt, ("running", "cancel_requested"),
            status="cancelled", completed_at=_now(), lease_expires_at=None,
        ):
            db.commit()
            return {"status": "cancelled", "job_id": job_id}
        db.rollback()
        return {"status": "superseded", "job_id": job_id}
    except Exception as exc:
        db.rollback()
        failure_code = (
            "ambiguous_holdout_consumption"
            if isinstance(exc, AmbiguousHoldoutConsumption) else exc.__class__.__name__
        )
        if _owned_update(
            db, job_id, owned_attempt, ("running",),
            status="failed", failure_code=failure_code, failure_detail=_error(exc),
            completed_at=_now(), lease_expires_at=None,
        ):
            db.commit()
            return {"status": "failed", "job_id": job_id, "failure_code": failure_code}
        db.rollback()
        return {"status": "superseded", "job_id": job_id}


def recover_stock_training_jobs(db: Session, *, stale_after_minutes: int = 30) -> list[StockTrainingJob]:
    """Persist recovery before broker delivery; database state is authoritative.

    This function commits its recovery transition before enqueueing. Callers
    may safely begin a new transaction afterward for audit logging.
    """
    now, recovered = _now(), []
    rows = db.scalars(select(StockTrainingJob).where(StockTrainingJob.status.in_(("queued", "running", "cancel_requested")))).all()
    for row in rows:
        if row.status == "cancel_requested":
            row.status, row.completed_at, row.lease_expires_at = "cancelled", now, None
        elif row.status == "running" and row.lease_expires_at and _utc(row.lease_expires_at) < now:
            expired = (
                StockTrainingJob.id == row.id,
                StockTrainingJob.status == "running",
                StockTrainingJob.attempts == row.attempts,
                StockTrainingJob.lease_expires_at < now,
            )
            if row.attempts >= MAX_ATTEMPTS:
                db.execute(update(StockTrainingJob).where(*expired).values(
                    status="failed", failure_code="worker_lost",
                    failure_detail="Worker vanished after bounded retries",
                    completed_at=now, lease_expires_at=None,
                ))
            else:
                recovered_claim = db.execute(update(StockTrainingJob).where(*expired).values(
                    status="queued", celery_task_id=None,
                    queue_error="Recovered after expired worker lease",
                    recovery_attempted_at=now, lease_expires_at=None,
                )).rowcount
                if recovered_claim:
                    db.expire(row)
                    db.refresh(row)
        elif row.status == "running" and row.started_at and _utc(row.started_at) < now - timedelta(minutes=stale_after_minutes) and row.lease_expires_at is None:
            # Compatibility for rows created before lease support.
            row.status, row.celery_task_id, row.queue_error, row.recovery_attempted_at = "queued", None, "Recovered legacy stale worker", now
        if row.status == "queued" and row.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
            row.status, row.failure_code, row.failure_detail, row.completed_at = (
                "failed", "delivery_limit", "Bounded broker delivery limit reached", now
            )
        elif row.status == "queued":
            # A queued record with a broker id is retried only after the
            # recovery interval; duplicate delivery cannot claim the job.
            if row.celery_task_id and row.recovery_attempted_at and _utc(row.recovery_attempted_at) > now - timedelta(minutes=stale_after_minutes):
                continue
            row.celery_task_id = None
            row.recovery_attempted_at = now
            recovered.append(row)
    recovered_ids = [row.id for row in recovered]
    # A fast worker can consume the broker message immediately. It must see
    # queued state in an independent DB session, never the old running lease.
    db.commit()
    delivered: list[StockTrainingJob] = []
    for job_id in recovered_ids:
        row = db.get(StockTrainingJob, job_id)
        if row is None or row.status != "queued":
            continue
        enqueue_stock_training_job(db, row)
        db.commit()
        delivered.append(row)
    return delivered


def create_stock_paper_binding(
    db: Session, *, model_run_id: str, snapshot_id: str, actor: str, purpose: str, reason: str
) -> StockPaperModelBinding:
    _lock_lifecycle_admission(db)
    model = db.scalar(
        select(StockModelRegistry)
        .where(StockModelRegistry.run_id == model_run_id)
        .with_for_update()
    )
    snapshot = db.get(StockDatasetSnapshot, snapshot_id)
    if model is None or snapshot is None or model.snapshot_id != snapshot.snapshot_id:
        raise StockTrainingError("A completed immutable model and its exact verified snapshot are required")
    if snapshot.metadata_json.get("binding_eligible") is not True:
        reason = snapshot.metadata_json.get("binding_eligibility_reason", "snapshot is research-only")
        raise StockTrainingError(f"Snapshot is not eligible for paper binding: {reason}")
    # Registry data alone is never sufficient authority: verify the immutable
    # snapshot and exact registered model inventory at binding time.
    dataset = _dataset_from_record(snapshot)
    manifest = validate_registered_stock_model(model, dataset)
    evidence = manifest.get("final_holdout_consumption", {})
    consumed_job = db.get(StockTrainingJob, evidence.get("job_id")) if isinstance(evidence, dict) else None
    if consumed_job is None or consumed_job.snapshot_id != snapshot_id:
        raise StockTrainingError("Registered model has no durable final holdout consumption job")
    _validate_holdout_consumption(db, job=consumed_job, manifest=manifest)
    if not purpose.strip() or not reason.strip():
        raise StockTrainingError("A paper evaluation purpose and binding reason are required")
    state = _active_binding_state(db)
    active_binding = db.get(StockPaperModelBinding, state.active_binding_id) if state else None
    if active_binding and active_binding.model_run_id != model_run_id:
        active_model = db.scalar(
            select(StockModelRegistry)
            .where(StockModelRegistry.run_id == active_binding.model_run_id)
            .with_for_update()
        )
        active_lifecycle = _current_lifecycle(db, active_model) if active_model else None
        if active_lifecycle and active_lifecycle.lifecycle_state == "champion":
            raise StockTrainingError(
                "The active champion cannot be replaced; explicitly demote it before binding a challenger"
            )
        if active_lifecycle and active_lifecycle.lifecycle_state == "paper_canary":
            _transition_model(
                db, model=active_model, action="demote", actor=actor,
                reason=f"Replaced by explicit paper canary binding: {reason}",
                binding_id=active_binding.id,
            )
    # A binding is an append-only activation event, not a mutable association.
    # In particular, A -> B -> A must record the final reactivation rather
    # than returning A's old event and leaving B selected by the latest-event
    # read policy.  The nonce is deliberately part of the immutable digest;
    # actor, reason, and activation timestamp are stored alongside it for
    # auditable evidence.
    activated_at = _now()
    identity = {
        "event_nonce": uuid4().hex,
        "activated_at": _utc(activated_at).isoformat(),
        "actor": actor,
        "reason": reason.strip(),
        "model_run_id": model_run_id,
        "snapshot_id": snapshot_id,
        "purpose": purpose.strip(),
        "paper_only": True,
        "live_authorized": False,
    }
    digest = hashlib.sha256(_canonical(identity)).hexdigest()
    row = StockPaperModelBinding(
        model_run_id=model_run_id, snapshot_id=snapshot_id, binding_sha256=digest, purpose=purpose.strip(),
        paper_only=True, live_authorized=False, bound_by=actor, reason=reason.strip(), created_at=activated_at,
    )
    db.add(row)
    db.flush()
    target_lifecycle = _current_lifecycle(db, model)
    if target_lifecycle.lifecycle_state in {"challenger", "demoted"}:
        _transition_model(
            db, model=model, action="mark_eligible", actor=actor,
            reason=f"Eligibility established for paper canary: {reason}",
        )
    if target_lifecycle.lifecycle_state == "eligible":
        _transition_model(
            db, model=model, action="start_canary", actor=actor,
            reason=reason, binding_id=row.id,
        )
    elif target_lifecycle.lifecycle_state == "paper_canary":
        # Re-activation of the same canary is represented by the new immutable
        # binding event and pointer update; no fake state transition is needed.
        pass
    else:
        raise StockTrainingError(
            f"Model lifecycle state {target_lifecycle.lifecycle_state} cannot create a paper binding"
        )
    if state is None:
        state = StockPaperBindingState(
            id=1, active_binding_id=row.id, changed_by=actor, reason=reason.strip(),
            changed_at=activated_at,
        )
        db.add(state)
    else:
        state.active_binding_id = row.id
        state.changed_by, state.reason, state.changed_at = actor, reason.strip(), activated_at
    db.flush()
    return row


def snapshot_projection(row: StockDatasetSnapshot) -> dict:
    return {
        "snapshot_id": row.snapshot_id, "verified": True, "provider": row.provider, "cutoff_at": row.cutoff_date.isoformat(),
        "universe": row.universe, "features": [row.feature_config_id], "rows": row.metadata_json.get("row_count"),
        "sha256": row.dataset_sha256, "provenance": {"provider": row.provider, "feature_config_id": row.feature_config_id},
        "captured_at": row.metadata_json.get("captured_at"),
        "binding_eligible": row.metadata_json.get("binding_eligible") is True,
        "binding_eligibility_reason": row.metadata_json.get("binding_eligibility_reason", "Legacy snapshot is research-only"),
        "adjustments_point_in_time": row.metadata_json.get("adjustments_point_in_time") is True,
        "created_at": row.created_at,
    }


def model_projection(
    row: StockModelRegistry,
    snapshot: StockDatasetSnapshot | None = None,
    lifecycle_state: str | None = None,
) -> dict:
    state = lifecycle_state or row.lifecycle_state or "challenger"
    return {
        "model_id": row.run_id, "model_version": row.training_metadata.get("selected_model", "unknown"),
        "status": state, "lifecycle_state": state,
        "sha256": row.manifest_sha256, "artifact_hash": row.manifest_sha256,
        "created_at": row.created_at, "scheduled": False,
        "eligible_for_binding": bool(snapshot and snapshot.metadata_json.get("binding_eligible") is True),
        "binding_eligibility_reason": (
            snapshot.metadata_json.get("binding_eligibility_reason", "Legacy snapshot is research-only")
            if snapshot else "Snapshot eligibility was not resolved"
        ),
        "binding_blockers": (
            [] if snapshot and snapshot.metadata_json.get("binding_eligible") is True
            else [snapshot.metadata_json.get("binding_eligibility_reason", "Snapshot is research-only") if snapshot else "Snapshot eligibility was not resolved"]
        ),
        "temporal_caveat": (
            "Adjusted prices are not point-in-time corporate-action data"
            if snapshot and snapshot.metadata_json.get("adjustments_point_in_time") is not True else None
        ),
    }


def report_projection(
    job: StockTrainingJob,
    snapshot: StockDatasetSnapshot | None,
    model: StockModelRegistry | None,
    lifecycle_state: str | None = None,
) -> dict:
    manifest = model.training_metadata if model else {}
    holdout_evaluation_count = manifest.get("final_holdout_evaluation_count", 0)
    if type(holdout_evaluation_count) is not int or holdout_evaluation_count < 0:
        holdout_evaluation_count = 0
    model_detail = model_projection(model, snapshot, lifecycle_state) | {"scheduled": job.trigger == "scheduled"} if model else None
    comparisons, walkforward, calibration = [], [], []
    for name, metrics in manifest.get("walkforward_metrics", {}).items():
        outcome = metrics.get("cost_aware_nonoverlapping_returns", {})
        item = {"name": name, "phase": "purged_walkforward", "kind": "model", "brier_score": metrics.get("brier_score"), "log_loss": metrics.get("log_loss"), "calibration": metrics.get("brier_score"), "sample_count": metrics.get("sample_count"), "losses": outcome.get("losses"), "net_return": outcome.get("total_return"), "max_drawdown": outcome.get("max_drawdown")}
        walkforward.append(item)
        comparisons.append(item)
    for name, metrics in manifest.get("calibration_metrics", {}).items():
        outcome = metrics.get("cost_aware_nonoverlapping_returns", {})
        item = {"name": name, "phase": "calibration", "kind": "baseline" if name == "training_prevalence_baseline" else "model", "brier_score": metrics.get("brier_score"), "log_loss": metrics.get("log_loss"), "calibration": metrics.get("brier_score"), "sample_count": metrics.get("sample_count"), "losses": outcome.get("losses"), "net_return": outcome.get("total_return"), "max_drawdown": outcome.get("max_drawdown")}
        calibration.append(item)
        comparisons.append(item)
    chosen = manifest.get("selected_model")
    holdout_model = manifest.get("final_holdout_metrics", {})
    holdout_baseline = manifest.get("final_holdout_baseline", {})
    holdout_rows = []
    for name, metrics, kind in (
        (chosen or "selected_model", holdout_model, "model"),
        ("training_prevalence_baseline", holdout_baseline, "baseline"),
    ):
        if metrics:
            outcome = metrics.get("cost_aware_nonoverlapping_returns", {})
            item = {"name": name, "phase": "final_untouched_holdout", "kind": kind, "brier_score": metrics.get("brier_score"), "log_loss": metrics.get("log_loss"), "calibration": metrics.get("brier_score"), "sample_count": metrics.get("sample_count"), "losses": outcome.get("losses"), "net_return": outcome.get("total_return"), "max_drawdown": outcome.get("max_drawdown")}
            holdout_rows.append(item)
            comparisons.append(item)
    return {
        "run_id": job.result_run_id, "job_id": job.id, "status": job.status, "trigger": job.trigger,
        "dataset_snapshot": snapshot_projection(snapshot) if snapshot else None, "model": model_detail,
        "report_hash": model.manifest_sha256 if model else None,
        # These immutable manifest fields let the UI display the exact runtime
        # inventory and modeling contract, rather than falling back to n/a.
        "versions": manifest.get("versions", {}),
        "code_sha256": manifest.get("code_sha256"),
        "assumptions": {
            key: manifest.get(key)
            for key in (
                "format", "horizon_days", "fee_rate_per_side", "slippage_rate_per_side",
                "folds", "embargo_days", "seed", "candidate_model_families",
                "selection_policy", "eligible_for_trading",
            )
        },
        "provenance": {"server_verified": bool(snapshot), "paper_only": True, "live_authorized": False},
        "validation": {"status": "passed" if model else "failed", "phase": "purged_walkforward_and_calibration", "selected_model": chosen, "folds": len(manifest.get("purged_expanding_walkforward", [])), "validation_rows": manifest.get("development_rows"), "walkforward_comparisons": walkforward, "calibration_comparisons": calibration, "failures": [job.failure_detail] if job.failure_detail else []},
        "holdout": {"status": "passed" if model else "failed", "phase": "final_untouched_holdout", "chosen_model": chosen, "holdout_rows": manifest.get("final_holdout_rows"), "holdout_start": manifest.get("final_holdout_start"), "holdout_end": manifest.get("final_holdout_end"), "chosen_model_metrics": holdout_rows[0] if holdout_rows else None, "baseline_metrics": holdout_rows[1] if len(holdout_rows) > 1 else None, "repeated_holdout_uses": holdout_evaluation_count, "failures": [job.failure_detail] if job.failure_detail else []},
        "comparisons": comparisons, "failures": [job.failure_detail] if job.failure_detail else [], "completed_at": job.completed_at,
    }


def summarize_job(job: StockTrainingJob, db: Session | None = None, detail: bool = False) -> dict:
    answer = {
        "id": job.id, "job_id": job.id, "status": job.status, "trigger": job.trigger, "requested_by": job.requested_by,
        "request": job.request_payload, "snapshot_id": job.snapshot_id, "dataset_snapshot_id": job.snapshot_id,
        "holdout_reservation_id": job.holdout_reservation_id, "attempts": job.attempts, "queue_error": job.queue_error,
        "failure_code": job.failure_code, "failure_detail": job.failure_detail, "error": job.failure_detail,
        "result_run_id": job.result_run_id, "run_id": job.result_run_id, "created_at": job.created_at,
        "started_at": job.started_at, "completed_at": job.completed_at, "paper_only": True, "live_authorized": False,
        "report_available": bool(job.result_run_id or job.status in {"failed", "cancelled"}),
    }
    if detail and db is not None:
        snapshot = db.get(StockDatasetSnapshot, job.snapshot_id)
        model = db.get(StockModelRegistry, job.result_run_id) if job.result_run_id else None
        answer["snapshot"] = snapshot_projection(snapshot) if snapshot else None
        lifecycle_state = get_stock_model_lifecycle_state(db, model.run_id) if model else None
        answer["model"] = (model_projection(model, snapshot, lifecycle_state) | {"scheduled": job.trigger == "scheduled"}) if model else None
        answer["report"] = report_projection(job, snapshot, model, lifecycle_state)
    return answer