"""Allow intraday providers to coexist without relabeling history."""
from alembic import op
import sqlalchemy as sa


revision = "0042_intraday_provider"
down_revision = "0041_paper_graduation_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("intraday_bars")
        if constraint.get("name")
    }
    indexes = {
        index["name"]
        for index in inspector.get_indexes("intraday_bars")
        if index.get("name")
    }

    # Published databases can have the table from the ORM metadata without
    # the legacy constraint, or can have a partially-applied retry with the
    # replacement already present. Make the migration converge in either case.
    with op.batch_alter_table("intraday_bars") as batch:
        if "uq_intraday_bar" in constraints:
            batch.drop_constraint("uq_intraday_bar", type_="unique")
        if "uq_intraday_bar_provider" not in constraints and "uq_intraday_bar_provider" not in indexes:
            batch.create_unique_constraint(
                "uq_intraday_bar_provider",
                ["symbol", "timeframe", "opened_at", "provider"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("intraday_bars")
        if constraint.get("name")
    }
    indexes = {
        index["name"]
        for index in inspector.get_indexes("intraday_bars")
        if index.get("name")
    }
    with op.batch_alter_table("intraday_bars") as batch:
        if "uq_intraday_bar_provider" in constraints:
            batch.drop_constraint("uq_intraday_bar_provider", type_="unique")
        if "uq_intraday_bar" not in constraints and "uq_intraday_bar" not in indexes:
            batch.create_unique_constraint(
                "uq_intraday_bar",
                ["symbol", "timeframe", "opened_at"],
            )
