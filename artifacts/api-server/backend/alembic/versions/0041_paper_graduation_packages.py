"""Add immutable paper graduation packages."""
from alembic import op
import sqlalchemy as sa


revision = "0041_paper_graduation_packages"
down_revision = "0040_controlled_live_pilot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_paper_graduation_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trial_id", sa.String(length=36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
        sa.Column("cycle_id", sa.String(length=64), sa.ForeignKey("stock_learning_cycles.cycle_id")),
        sa.Column(
            "readiness_report_id",
            sa.Integer(),
            sa.ForeignKey("stock_paper_promotion_readiness_reports.id"),
            nullable=False,
        ),
        sa.Column("accuracy_report_id", sa.Integer(), sa.ForeignKey("stock_paper_accuracy_reports.id")),
        sa.Column("package_hash", sa.String(length=64), nullable=False),
        sa.Column("readiness_report_hash", sa.String(length=64), nullable=False),
        sa.Column("soak_report_hash", sa.String(length=64)),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("reviewer_actor", sa.String(length=128), nullable=False),
        sa.Column("reviewer_reason", sa.Text(), nullable=False),
        sa.Column("authorization", sa.JSON(), nullable=False),
        sa.Column("paper_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("live_authorized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="ck_stock_graduation_decision"),
        sa.CheckConstraint("paper_only = true", name="ck_stock_graduation_paper_only"),
        sa.CheckConstraint("live_authorized = false", name="ck_stock_graduation_live_disabled"),
        sa.UniqueConstraint("package_hash", name="uq_stock_graduation_package_hash"),
    )
    op.create_index(
        "ix_stock_graduation_trial_created",
        "stock_paper_graduation_packages",
        ["trial_id", "created_at"],
    )
    op.create_index(
        "ix_stock_paper_graduation_packages_trial_id",
        "stock_paper_graduation_packages",
        ["trial_id"],
    )
    op.create_index(
        "ix_stock_paper_graduation_packages_cycle_id",
        "stock_paper_graduation_packages",
        ["cycle_id"],
    )
    op.create_index(
        "ix_stock_paper_graduation_packages_readiness_report_id",
        "stock_paper_graduation_packages",
        ["readiness_report_id"],
    )
    op.create_index(
        "ix_stock_paper_graduation_packages_accuracy_report_id",
        "stock_paper_graduation_packages",
        ["accuracy_report_id"],
    )
    op.create_index(
        "ix_stock_paper_graduation_packages_decision",
        "stock_paper_graduation_packages",
        ["decision"],
    )
    op.create_index(
        "ix_stock_paper_graduation_packages_created_at",
        "stock_paper_graduation_packages",
        ["created_at"],
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(
            """
            CREATE OR REPLACE FUNCTION reject_stock_paper_graduation_package_mutation()
            RETURNS trigger AS $$
            BEGIN
              RAISE EXCEPTION 'stock paper graduation packages are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        ))
        op.execute(sa.text(
            """
            CREATE TRIGGER stock_paper_graduation_packages_immutable
            BEFORE UPDATE OR DELETE ON stock_paper_graduation_packages
            FOR EACH ROW EXECUTE FUNCTION reject_stock_paper_graduation_package_mutation()
            """
        ))
    elif bind.dialect.name == "sqlite":
        op.execute(sa.text(
            """
            CREATE TRIGGER stock_paper_graduation_packages_immutable_update
            BEFORE UPDATE ON stock_paper_graduation_packages
            BEGIN
              SELECT RAISE(ABORT, 'stock paper graduation packages are immutable');
            END
            """
        ))
        op.execute(sa.text(
            """
            CREATE TRIGGER stock_paper_graduation_packages_immutable_delete
            BEFORE DELETE ON stock_paper_graduation_packages
            BEGIN
              SELECT RAISE(ABORT, 'stock paper graduation packages are immutable');
            END
            """
        ))


def downgrade() -> None:
    raise RuntimeError("0041 is forward-only; restore a verified backup to roll back")