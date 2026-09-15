"""Durable point-in-time forward accuracy and outcome evidence."""
from alembic import op
import sqlalchemy as sa


revision = "0038_stock_accuracy"
down_revision = "0037_live_safety_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    legacy_interim = False
    if "stock_dataset_snapshots" in tables:
        legacy_interim = bool(bind.execute(sa.text(
            "SELECT 1 FROM stock_dataset_snapshots "
            "WHERE CAST(metadata_json AS TEXT) LIKE '%legacy_interim_schema%' LIMIT 1"
        )).scalar())
    if "stock_paper_trial_outcomes" not in tables:
        op.create_table(
            "stock_paper_trial_outcomes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("trial_id", sa.String(length=36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
            sa.Column("decision_id", sa.Integer(), sa.ForeignKey("stock_paper_trial_decisions.id"), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("label_status", sa.String(length=24), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column("label_end", sa.DateTime(timezone=True)),
            sa.Column("realized_return", sa.Numeric(precision=18, scale=8)),
            sa.Column("cost_adjusted_return", sa.Numeric(precision=18, scale=8)),
            sa.Column("realized_up", sa.Boolean()),
            sa.Column("costs_known", sa.Boolean()),
            sa.Column("label_lineage", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("decision_id", name="uq_stock_trial_outcome_decision"),
            sa.CheckConstraint(
                "label_status IN ('resolved','unknown','blocked','restated')",
                name="ck_stock_trial_outcome_label_status",
            ),
        )
        op.create_index("ix_stock_paper_trial_outcomes_trial_id", "stock_paper_trial_outcomes", ["trial_id"])
    if "stock_paper_accuracy_reports" not in tables:
        op.create_table(
            "stock_paper_accuracy_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("trial_id", sa.String(length=36), sa.ForeignKey("stock_paper_trials.id"), nullable=False),
            sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
            sa.Column("classification", sa.String(length=24), nullable=False),
            sa.Column("report_hash", sa.String(length=64), nullable=False),
            sa.Column("lineage", sa.JSON(), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("report_hash", name="uq_stock_accuracy_report_hash"),
        )
        op.create_index("ix_stock_paper_accuracy_reports_trial_id", "stock_paper_accuracy_reports", ["trial_id"])
        op.create_index("ix_stock_accuracy_report_trial_asof", "stock_paper_accuracy_reports", ["trial_id", "as_of"])


def downgrade() -> None:
    raise RuntimeError("0038 is forward-only; restore a verified backup to roll back")