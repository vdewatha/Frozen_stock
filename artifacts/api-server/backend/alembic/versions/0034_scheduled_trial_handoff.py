"""Persist scheduled learning-cycle paper handoff ownership."""
from alembic import op
import sqlalchemy as sa


revision = "0034_scheduled_trial_handoff"
down_revision = "0033_promotion_decision_retries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    check_name = "ck_stock_learning_cycle_status"
    status_check = (
        "status IN ('blocked', 'deferred', 'queued', 'running', "
        "'awaiting_admission', 'awaiting_preflight', 'running_forward_trial', "
        "'awaiting_forward_evidence', 'operator_review', 'complete', 'promoted', "
        "'demoted', 'rolled_back', 'failed')"
    )
    def has_column(table: str, column: str) -> bool:
        return column in {item["name"] for item in inspector.get_columns(table)}

    def has_unique(table: str, name: str) -> bool:
        return any(item.get("name") == name for item in inspector.get_unique_constraints(table))

    def has_check(table: str, name: str) -> bool:
        return any(item.get("name") == name for item in inspector.get_check_constraints(table))

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("stock_learning_cycles", recreate="always") as batch:
            if not has_column("stock_learning_cycles", "binding_id"):
                batch.add_column(
                    sa.Column(
                        "binding_id",
                        sa.Integer(),
                        sa.ForeignKey("stock_paper_model_bindings.id", name="fk_learning_cycle_binding"),
                    )
                )
            if not has_unique("stock_learning_cycles", "uq_stock_learning_cycle_binding"):
                batch.create_unique_constraint("uq_stock_learning_cycle_binding", ["binding_id"])
            if has_check("stock_learning_cycles", check_name):
                batch.drop_constraint(check_name, type_="check")
            batch.create_check_constraint(check_name, status_check)
        with op.batch_alter_table("stock_paper_model_bindings", recreate="always") as batch:
            if not has_column("stock_paper_model_bindings", "source_cycle_id"):
                batch.add_column(sa.Column("source_cycle_id", sa.String(length=64)))
            if not has_unique("stock_paper_model_bindings", "uq_stock_binding_source_cycle"):
                batch.create_unique_constraint("uq_stock_binding_source_cycle", ["source_cycle_id"])
        with op.batch_alter_table("stock_paper_trials", recreate="always") as batch:
            if not has_column("stock_paper_trials", "source_cycle_id"):
                batch.add_column(sa.Column("source_cycle_id", sa.String(length=64)))
            if not has_unique("stock_paper_trials", "uq_stock_trial_source_cycle"):
                batch.create_unique_constraint("uq_stock_trial_source_cycle", ["source_cycle_id"])
    else:
        if not has_column("stock_learning_cycles", "binding_id"):
            op.add_column(
                "stock_learning_cycles",
                sa.Column(
                    "binding_id",
                    sa.Integer(),
                    sa.ForeignKey("stock_paper_model_bindings.id", name="fk_learning_cycle_binding"),
                ),
            )
        if not has_unique("stock_learning_cycles", "uq_stock_learning_cycle_binding"):
            op.create_unique_constraint(
                "uq_stock_learning_cycle_binding", "stock_learning_cycles", ["binding_id"]
            )
        if not has_column("stock_paper_model_bindings", "source_cycle_id"):
            op.add_column(
                "stock_paper_model_bindings",
                sa.Column("source_cycle_id", sa.String(64)),
            )
        if not has_unique("stock_paper_model_bindings", "uq_stock_binding_source_cycle"):
            op.create_unique_constraint(
                "uq_stock_binding_source_cycle", "stock_paper_model_bindings", ["source_cycle_id"]
            )
        if not has_column("stock_paper_trials", "source_cycle_id"):
            op.add_column("stock_paper_trials", sa.Column("source_cycle_id", sa.String(64)))
        if not has_unique("stock_paper_trials", "uq_stock_trial_source_cycle"):
            op.create_unique_constraint(
                "uq_stock_trial_source_cycle", "stock_paper_trials", ["source_cycle_id"]
            )
        if has_check("stock_learning_cycles", check_name):
            op.drop_constraint(check_name, "stock_learning_cycles", type_="check")
        op.create_check_constraint(check_name, "stock_learning_cycles", status_check)


def downgrade() -> None:
    raise RuntimeError("0034 is forward-only; restore a verified backup to roll back")
