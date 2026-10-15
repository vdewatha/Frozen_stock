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
    allowed_statuses = (
        "blocked", "deferred", "queued", "running", "awaiting_admission",
        "awaiting_preflight", "running_forward_trial", "awaiting_forward_evidence",
        "operator_review", "complete", "promoted", "demoted", "rolled_back", "failed",
    )
    status_check = (
        "status IN ('blocked', 'deferred', 'queued', 'running', "
        "'awaiting_admission', 'awaiting_preflight', 'running_forward_trial', "
        "'awaiting_forward_evidence', 'operator_review', 'complete', 'promoted', "
        "'demoted', 'rolled_back', 'failed')"
    )

    def columns(table: str) -> set[str]:
        return {column["name"] for column in sa.inspect(bind).get_columns(table)}

    def unique_exists(table: str, name: str, column: str) -> bool:
        current = sa.inspect(bind)
        if any(
            constraint.get("name") == name
            or constraint.get("column_names") == [column]
            for constraint in current.get_unique_constraints(table)
        ):
            return True
        return any(
            index.get("unique")
            and index.get("column_names") == [column]
            and not any(key.endswith("_where") and value for key, value in index.get("dialect_options", {}).items())
            for index in current.get_indexes(table)
        )

    def foreign_key_exists(table: str, column: str, referred_table: str, referred_column: str) -> bool:
        return any(
            foreign_key.get("referred_table") == referred_table
            and foreign_key.get("constrained_columns") == [column]
            and foreign_key.get("referred_columns") == [referred_column]
            for foreign_key in sa.inspect(bind).get_foreign_keys(table)
        )

    def status_check_is_current() -> bool:
        check = next(
            (
                constraint
                for constraint in sa.inspect(bind).get_check_constraints("stock_learning_cycles")
                if constraint.get("name") == check_name
            ),
            None,
        )
        clause = (check or {}).get("sqltext", "").lower()
        return check is not None and all(f"'{status}'" in clause for status in allowed_statuses)

    if bind.dialect.name == "sqlite":
        cycle_columns = columns("stock_learning_cycles")
        cycle_fk_exists = foreign_key_exists(
            "stock_learning_cycles", "binding_id", "stock_paper_model_bindings", "id"
        )
        cycle_unique_exists = unique_exists(
            "stock_learning_cycles", "uq_stock_learning_cycle_binding", "binding_id"
        )
        cycle_checks = {
            check.get("name")
            for check in sa.inspect(bind).get_check_constraints("stock_learning_cycles")
        }
        cycle_check_current = status_check_is_current()
        if (
            "binding_id" not in cycle_columns
            or not cycle_fk_exists
            or not cycle_unique_exists
            or not cycle_check_current
        ):
            with op.batch_alter_table("stock_learning_cycles", recreate="always") as batch:
                if "binding_id" not in cycle_columns:
                    batch.add_column(sa.Column("binding_id", sa.Integer()))
                if not cycle_fk_exists:
                    batch.create_foreign_key(
                        "fk_learning_cycle_binding",
                        "stock_paper_model_bindings",
                        ["binding_id"],
                        ["id"],
                    )
                if not cycle_unique_exists:
                    batch.create_unique_constraint(
                        "uq_stock_learning_cycle_binding", ["binding_id"]
                    )
                if not cycle_check_current:
                    if check_name in cycle_checks:
                        batch.drop_constraint(check_name, type_="check")
                    batch.create_check_constraint(check_name, status_check)

        for table in ("stock_paper_model_bindings", "stock_paper_trials"):
            existing_columns = columns(table)
            unique_name = (
                "uq_stock_binding_source_cycle"
                if table == "stock_paper_model_bindings"
                else "uq_stock_trial_source_cycle"
            )
            if "source_cycle_id" not in existing_columns or not unique_exists(
                table, unique_name, "source_cycle_id"
            ):
                with op.batch_alter_table(table, recreate="always") as batch:
                    if "source_cycle_id" not in existing_columns:
                        batch.add_column(sa.Column("source_cycle_id", sa.String(length=64)))
                    if not unique_exists(table, unique_name, "source_cycle_id"):
                        batch.create_unique_constraint(unique_name, ["source_cycle_id"])
    else:
        cycle_columns = columns("stock_learning_cycles")
        if "binding_id" not in cycle_columns:
            op.add_column("stock_learning_cycles", sa.Column("binding_id", sa.Integer()))
        if not foreign_key_exists(
            "stock_learning_cycles", "binding_id", "stock_paper_model_bindings", "id"
        ):
            op.create_foreign_key(
                "fk_learning_cycle_binding",
                "stock_learning_cycles",
                "stock_paper_model_bindings",
                ["binding_id"],
                ["id"],
            )
        if not unique_exists(
            "stock_learning_cycles", "uq_stock_learning_cycle_binding", "binding_id"
        ):
            op.create_unique_constraint(
                "uq_stock_learning_cycle_binding", "stock_learning_cycles", ["binding_id"]
            )

        for table, unique_name in (
            ("stock_paper_model_bindings", "uq_stock_binding_source_cycle"),
            ("stock_paper_trials", "uq_stock_trial_source_cycle"),
        ):
            if "source_cycle_id" not in columns(table):
                op.add_column(table, sa.Column("source_cycle_id", sa.String(length=64)))
            if not unique_exists(table, unique_name, "source_cycle_id"):
                op.create_unique_constraint(unique_name, table, ["source_cycle_id"])

        if not status_check_is_current():
            checks = {
                check.get("name")
                for check in inspector.get_check_constraints("stock_learning_cycles")
            }
            if check_name in checks:
                op.drop_constraint(check_name, "stock_learning_cycles", type_="check")
            op.create_check_constraint(check_name, "stock_learning_cycles", status_check)


def downgrade() -> None:
    raise RuntimeError("0034 is forward-only; restore a verified backup to roll back")