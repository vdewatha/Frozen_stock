"""Deduplicate and constrain persisted model prediction identities."""
from alembic import op


revision = "0053_model_prediction_identity"
down_revision = "0052_paper_research_qual"
branch_labels = None
depends_on = None


def upgrade():
    # Earlier refreshes could write the same symbol/date/horizon/source more
    # than once. Keep the oldest row as the canonical evidence record.
    op.execute(
        """
        DELETE FROM model_predictions
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM model_predictions
            GROUP BY symbol, prediction_date, horizon_days, source
        )
        """
    )
    # SQLite cannot ALTER a table to add a constraint. Alembic's batch mode
    # rebuilds that table on SQLite and uses a normal ALTER on databases that
    # support it.
    with op.batch_alter_table("model_predictions", recreate="auto") as batch_op:
        batch_op.create_unique_constraint(
            "uq_model_prediction_identity",
            ["symbol", "prediction_date", "horizon_days", "source"],
        )


def downgrade():
    raise RuntimeError("0053 is forward-only; restore a verified backup to roll back")
