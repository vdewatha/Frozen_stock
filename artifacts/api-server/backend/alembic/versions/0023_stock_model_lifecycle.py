"""Explicit stock model lifecycle and atomic active paper binding."""
from alembic import op
import sqlalchemy as sa
import hashlib

revision = "0023_stock_model_lifecycle"
down_revision = "0022_promotion_readiness_report"
branch_labels = None
depends_on = None


LIFECYCLE_CHECK = (
    "lifecycle_state IN "
    "('challenger', 'eligible', 'paper_canary', 'champion', 'demoted', 'retired')"
)
INITIAL_CANARY_EVENT_HASH = hashlib.sha256(
    b"stock-model-lifecycle-0023-initialize-canary"
).hexdigest()


def upgrade() -> None:
    with op.batch_alter_table("stock_model_registry") as batch:
        batch.add_column(
            sa.Column(
                "lifecycle_state",
                sa.String(length=24),
                nullable=False,
                server_default="challenger",
            )
        )
        batch.create_check_constraint("ck_stock_model_lifecycle_state", LIFECYCLE_CHECK)

    op.create_index(
        "ix_stock_model_registry_lifecycle_state",
        "stock_model_registry",
        ["lifecycle_state"],
    )
    op.create_table(
        "stock_model_lifecycle_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_run_id", sa.String(length=64), sa.ForeignKey("stock_model_registry.run_id"), nullable=False),
        sa.Column("binding_id", sa.Integer(), sa.ForeignKey("stock_paper_model_bindings.id"), nullable=True),
        sa.Column("from_state", sa.String(length=24), nullable=True),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("event_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(LIFECYCLE_CHECK.replace("lifecycle_state", "to_state"), name="ck_stock_model_lifecycle_event_to_state"),
        sa.CheckConstraint(
            "from_state IS NULL OR " + LIFECYCLE_CHECK.replace("lifecycle_state", "from_state"),
            name="ck_stock_model_lifecycle_event_from_state",
        ),
        sa.UniqueConstraint("event_sha256", name="uq_stock_model_lifecycle_event_digest"),
    )
    op.create_index(
        "ix_stock_model_lifecycle_events_model_run_id",
        "stock_model_lifecycle_events",
        ["model_run_id"],
    )
    op.create_index(
        "ix_stock_model_lifecycle_events_binding_id",
        "stock_model_lifecycle_events",
        ["binding_id"],
    )

    op.create_table(
        "stock_model_lifecycle_state",
        sa.Column(
            "model_run_id",
            sa.String(length=64),
            sa.ForeignKey("stock_model_registry.run_id"),
            primary_key=True,
        ),
        sa.Column("lifecycle_state", sa.String(length=24), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(LIFECYCLE_CHECK, name="ck_stock_model_current_lifecycle_state"),
    )
    op.create_index(
        "ix_stock_model_lifecycle_state_lifecycle_state",
        "stock_model_lifecycle_state",
        ["lifecycle_state"],
    )
    op.create_index(
        "uq_stock_model_one_champion",
        "stock_model_lifecycle_state",
        ["lifecycle_state"],
        unique=True,
        sqlite_where=sa.text("lifecycle_state = 'champion'"),
        postgresql_where=sa.text("lifecycle_state = 'champion'"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO stock_model_lifecycle_state
                (model_run_id, lifecycle_state, updated_by, reason)
            SELECT run_id, 'challenger', 'migration',
                'Initialized from immutable stock model registry'
            FROM stock_model_registry
            """
        )
    )

    op.create_table(
        "stock_paper_binding_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "active_binding_id",
            sa.Integer(),
            sa.ForeignKey("stock_paper_model_bindings.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("changed_by", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_stock_paper_binding_state_singleton"),
    )

    # Preserve the existing latest-event read policy as the initial explicit
    # pointer. Existing paper models become canaries rather than champions:
    # no historical evidence is upgraded into a promotion. The registry row
    # remains immutable; only the current-state table is changed.
    op.execute(
        sa.text(
            """
            INSERT INTO stock_paper_binding_state
                (id, active_binding_id, changed_by, reason)
            SELECT 1, id, 'migration',
                'Initialized from the pre-lifecycle latest paper binding'
            FROM stock_paper_model_bindings
            ORDER BY id DESC
            LIMIT 1
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE stock_model_lifecycle_state
            SET lifecycle_state = 'paper_canary',
                updated_by = 'migration',
                reason = 'Initialized from the pre-lifecycle latest paper binding'
            WHERE model_run_id = (
                SELECT binding.model_run_id
                FROM stock_paper_binding_state AS state
                JOIN stock_paper_model_bindings AS binding
                  ON binding.id = state.active_binding_id
                WHERE state.id = 1
            )
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            INSERT INTO stock_model_lifecycle_events
                (model_run_id, binding_id, from_state, to_state, action,
                 actor, reason, event_sha256)
            SELECT binding.model_run_id, binding.id, 'challenger', 'paper_canary',
                'migration_initialize_canary', 'migration',
                'Initialized from the pre-lifecycle latest paper binding',
                '{INITIAL_CANARY_EVENT_HASH}'
            FROM stock_paper_binding_state AS state
            JOIN stock_paper_model_bindings AS binding
              ON binding.id = state.active_binding_id
            JOIN stock_model_lifecycle_state AS current
              ON current.model_run_id = binding.model_run_id
            WHERE state.id = 1 AND current.lifecycle_state = 'paper_canary'
            """
        )
    )


def downgrade() -> None:
    op.drop_table("stock_paper_binding_state")
    op.drop_index(
        "ix_stock_model_lifecycle_events_binding_id",
        table_name="stock_model_lifecycle_events",
    )
    op.drop_index(
        "ix_stock_model_lifecycle_events_model_run_id",
        table_name="stock_model_lifecycle_events",
    )
    op.drop_table("stock_model_lifecycle_events")
    op.drop_index("uq_stock_model_one_champion", table_name="stock_model_lifecycle_state")
    op.drop_index(
        "ix_stock_model_lifecycle_state_lifecycle_state",
        table_name="stock_model_lifecycle_state",
    )
    op.drop_table("stock_model_lifecycle_state")
    op.drop_index(
        "ix_stock_model_registry_lifecycle_state",
        table_name="stock_model_registry",
    )
    with op.batch_alter_table("stock_model_registry") as batch:
        batch.drop_constraint("ck_stock_model_lifecycle_state", type_="check")
        batch.drop_column("lifecycle_state")