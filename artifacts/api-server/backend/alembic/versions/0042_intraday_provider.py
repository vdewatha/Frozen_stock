"""Allow intraday providers to coexist without relabeling history."""
from alembic import op


revision = "0042_intraday_provider"
down_revision = "0041_paper_graduation_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("intraday_bars") as batch:
        batch.drop_constraint("uq_intraday_bar", type_="unique")
        batch.create_unique_constraint(
            "uq_intraday_bar_provider",
            ["symbol", "timeframe", "opened_at", "provider"],
        )


def downgrade() -> None:
    with op.batch_alter_table("intraday_bars") as batch:
        batch.drop_constraint("uq_intraday_bar_provider", type_="unique")
        batch.create_unique_constraint(
            "uq_intraday_bar",
            ["symbol", "timeframe", "opened_at"],
        )