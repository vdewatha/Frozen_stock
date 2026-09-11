"""Repair the previously shipped interim stock-training 0010 schema.

The first 0010 revision was applied while its ORM contract was still being
renamed.  This migration is deliberately additive/transformative: it keeps
all prior rows and their original provenance in metadata rather than dropping
tables or rebuilding user data.
"""
from alembic import op
import sqlalchemy as sa


# alembic_version.version_num is VARCHAR(32) in the initial schema.
revision = "0011_stock_training_repair"
down_revision = "0010_stock_training_jobs"
branch_labels = None
depends_on = None


def _columns(bind, table):
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _unique(bind, table):
    return {item["name"] for item in sa.inspect(bind).get_unique_constraints(table) if item["name"]}


def _drop_unique_if_present(bind, table, name):
    if name in _unique(bind, table):
        op.drop_constraint(name, table, type_="unique")


def _drop_unique_for_column(bind, table, column):
    for item in sa.inspect(bind).get_unique_constraints(table):
        if item["name"] and item.get("column_names") == [column]:
            op.drop_constraint(item["name"], table, type_="unique")


def _add_column_if_missing(bind, table, column):
    if column.name not in _columns(bind, table):
        op.add_column(table, column)


def _disable_interim_immutable_triggers(bind):
    """Disable only named stock audit triggers while repair backfills rows.

    PostgreSQL DDL is transactional, so a repair exception rolls this
    temporary state back. The caller also restores triggers in `finally` for
    the normal success path.
    """
    disabled = []
    for table in (
        "stock_dataset_snapshots",
        "stock_holdout_reservations",
        "stock_paper_model_bindings",
        "stock_model_registry",
    ):
        rows = bind.execute(sa.text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = to_regclass(:table) AND NOT tgisinternal "
            "AND tgname = :name"
        ), {"table": table, "name": f"{table}_immutable"}).scalars()
        for name in rows:
            op.execute(sa.text(f'ALTER TABLE "{table}" DISABLE TRIGGER "{name}"'))
            disabled.append((table, name))
    return disabled


def _restore_immutable_triggers(disabled):
    for table, name in disabled:
        op.execute(sa.text(f'ALTER TABLE "{table}" ENABLE TRIGGER "{name}"'))


def upgrade():
    bind = op.get_bind()
    snapshot = "stock_dataset_snapshots"
    # A fresh database has the final 0010 shape already. The repair remains a
    # no-op there (except model registry creation if an interrupted fresh
    # migration somehow omitted it).
    interim = "id" in _columns(bind, snapshot)
    if not interim:
        if "stock_model_registry" not in sa.inspect(bind).get_table_names():
            op.create_table(
                "stock_model_registry",
                sa.Column("run_id", sa.String(64), primary_key=True),
                sa.Column("snapshot_id", sa.String(64), sa.ForeignKey(f"{snapshot}.snapshot_id"), nullable=False),
                sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
                sa.Column("artifact_path", sa.Text(), nullable=False),
                sa.Column("training_metadata", sa.JSON(), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            )
        return
    # The known interim form was PostgreSQL. SQLite fresh migrations never
    # have this shape; fail loudly rather than risk a lossy emulation.
    if bind.dialect.name != "postgresql":
        raise RuntimeError("Interim stock-training schema repair requires PostgreSQL")
    disabled = _disable_interim_immutable_triggers(bind)
    try:
        _repair_interim(bind)
    finally:
        _restore_immutable_triggers(disabled)


def _repair_interim(bind):
    snapshot = "stock_dataset_snapshots"

    # Rename FK-side columns first; PostgreSQL propagates referenced-column
    # renames, and all transformations below preserve the old values.
    op.alter_column("stock_holdout_reservations", "dataset_snapshot_id", new_column_name="snapshot_id")
    op.alter_column("stock_holdout_reservations", "horizon_bars", new_column_name="horizon_days")
    op.alter_column("stock_training_jobs", "dataset_snapshot_id", new_column_name="snapshot_id")
    op.alter_column("stock_paper_model_bindings", "dataset_snapshot_id", new_column_name="snapshot_id")
    op.alter_column(snapshot, "id", new_column_name="snapshot_id")

    # Preserve interim-only values in metadata before removing their columns.
    _add_column_if_missing(bind, snapshot, sa.Column("cutoff_date", sa.Date()))
    _add_column_if_missing(bind, snapshot, sa.Column("provider", sa.String(64)))
    _add_column_if_missing(bind, snapshot, sa.Column("horizon_days", sa.Integer()))
    _add_column_if_missing(bind, snapshot, sa.Column("artifact_sha256", sa.String(64)))
    _add_column_if_missing(bind, snapshot, sa.Column("metadata_json", sa.JSON()))
    op.execute(
        """
        UPDATE stock_dataset_snapshots
        SET cutoff_date = cutoff_at::date,
            provider = COALESCE(provider_provenance->>'provider',
                                provider_provenance->>'source',
                                'legacy_unverified'),
            horizon_days = COALESCE(
                (SELECT MAX(h.horizon_days) FROM stock_holdout_reservations h
                 WHERE h.snapshot_id = stock_dataset_snapshots.snapshot_id),
                1
            ),
            artifact_sha256 = dataset_sha256,
            universe = CASE
                WHEN json_typeof(universe) = 'object' THEN COALESCE(universe->'symbols', '[]'::json)
                ELSE universe
            END,
            metadata_json = json_build_object(
                'legacy_interim_schema', true,
                'legacy_provider_provenance', provider_provenance,
                'legacy_cutoff_at', cutoff_at,
                'legacy_row_count', row_count,
                'snapshot_id', snapshot_id,
                'dataset_sha256', dataset_sha256,
                'artifact_path', artifact_path,
                'artifact_sha256', dataset_sha256,
                'provider', COALESCE(provider_provenance->>'provider',
                                     provider_provenance->>'source',
                                     'legacy_unverified'),
                'horizon_days', COALESCE(
                    (SELECT MAX(h.horizon_days) FROM stock_holdout_reservations h
                     WHERE h.snapshot_id = stock_dataset_snapshots.snapshot_id),
                    1
                ),
                'feature_config_id', feature_config_id
            )
        """
    )
    for name in ("cutoff_date", "provider", "horizon_days", "artifact_sha256", "metadata_json"):
        op.alter_column(snapshot, name, nullable=False)
    _drop_unique_for_column(bind, snapshot, "dataset_sha256")
    if "ix_stock_dataset_snapshots_dataset_sha256" not in {x["name"] for x in sa.inspect(bind).get_indexes(snapshot)}:
        op.create_index("ix_stock_dataset_snapshots_dataset_sha256", snapshot, ["dataset_sha256"])
    op.drop_column(snapshot, "provider_provenance")
    op.drop_column(snapshot, "cutoff_at")
    op.drop_column(snapshot, "row_count")

    # Holdout intervals retain their original range and gain the explicit
    # symbol set used for cross-universe intersection checks.
    _add_column_if_missing(bind, "stock_holdout_reservations", sa.Column("universe", sa.JSON()))
    op.execute(
        """
        UPDATE stock_holdout_reservations h
        SET universe = s.universe
        FROM stock_dataset_snapshots s
        WHERE h.snapshot_id = s.snapshot_id AND h.universe IS NULL
        """
    )
    op.alter_column("stock_holdout_reservations", "universe", nullable=False)

    # Registry rows are created for historical completed jobs/bindings. Their
    # metadata explicitly marks them legacy; absent artifacts remain visible
    # and cannot be silently promoted by current binding validation.
    op.create_table(
        "stock_model_registry",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey(f"{snapshot}.snapshot_id"), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("training_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        """
        INSERT INTO stock_model_registry
            (run_id, snapshot_id, manifest_sha256, artifact_path, training_metadata)
        SELECT DISTINCT ON (run_id)
            run_id, snapshot_id, lpad(md5(run_id), 64, '0'), '',
            json_build_object('legacy_interim_schema', true, 'run_id', run_id)
        FROM (
            SELECT result_run_id AS run_id, snapshot_id FROM stock_training_jobs WHERE result_run_id IS NOT NULL
            UNION ALL
            SELECT model_run_id AS run_id, snapshot_id FROM stock_paper_model_bindings
        ) legacy
        WHERE run_id IS NOT NULL
        ORDER BY run_id
        ON CONFLICT (run_id) DO NOTHING
        """
    )

    _add_column_if_missing(bind, "stock_training_jobs", sa.Column("delivery_attempts", sa.Integer(), server_default="0", nullable=False))
    _add_column_if_missing(bind, "stock_training_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True)))
    _add_column_if_missing(bind, "stock_training_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.alter_column("stock_training_jobs", "holdout_reservation_id", nullable=True)
    # Preserve every duplicate record while making the durable dedupe key
    # unique for future admissions.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY dedupe_key ORDER BY created_at, id) AS n
            FROM stock_training_jobs
        )
        UPDATE stock_training_jobs j
        SET dedupe_key = left(j.dedupe_key, 31) || md5(j.id)
        FROM ranked r WHERE j.id = r.id AND r.n > 1
        """
    )
    _drop_unique_if_present(bind, "stock_training_jobs", "stock_training_jobs_celery_task_id_key")
    # Re-add with the ORM's unnamed convention: PostgreSQL's previous name
    # may differ, but the column-level unique property is equivalent.
    op.create_unique_constraint("stock_training_jobs_celery_task_id_key", "stock_training_jobs", ["celery_task_id"])
    op.create_unique_constraint("uq_stock_training_job_dedupe", "stock_training_jobs", ["dedupe_key"])
    op.drop_constraint("ck_stock_training_job_status", "stock_training_jobs", type_="check")
    op.create_check_constraint(
        "ck_stock_training_job_status", "stock_training_jobs",
        "status IN ('queued', 'running', 'cancel_requested', 'cancelled', 'succeeded', 'failed', 'deferred')",
    )
    op.create_check_constraint("ck_stock_training_job_deliveries", "stock_training_jobs", "delivery_attempts >= 0 AND delivery_attempts <= 3")

    _drop_unique_if_present(bind, "stock_paper_model_bindings", "stock_paper_model_bindings_binding_sha256_key")
    op.create_unique_constraint("uq_stock_binding_digest", "stock_paper_model_bindings", ["binding_sha256"])
    existing_fks = {
        (item["referred_table"], tuple(item["constrained_columns"]))
        for item in sa.inspect(bind).get_foreign_keys("stock_training_jobs")
    }
    if ("stock_model_registry", ("result_run_id",)) not in existing_fks:
        op.create_foreign_key(
            "fk_stock_training_jobs_result_run_id_stock_model_registry",
            "stock_training_jobs", "stock_model_registry", ["result_run_id"], ["run_id"],
        )
    binding_fks = {
        (item["referred_table"], tuple(item["constrained_columns"]))
        for item in sa.inspect(bind).get_foreign_keys("stock_paper_model_bindings")
    }
    if ("stock_model_registry", ("model_run_id",)) not in binding_fks:
        op.create_foreign_key(
            "fk_stock_paper_bindings_model_run_id_stock_model_registry",
            "stock_paper_model_bindings", "stock_model_registry", ["model_run_id"], ["run_id"],
        )


def downgrade():
    raise RuntimeError("0011 is forward-only; restore a verified backup to roll back")