"""Allow provider-specific daily market-price observations to coexist."""

from alembic import op
import sqlalchemy as sa


revision = "0058_market_price_provider_key"
down_revision = "0057_learning_worker_leases"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    unique_constraints = {
        item.get("name")
        for item in inspector.get_unique_constraints("market_prices")
    }
    indexes = {
        item.get("name")
        for item in inspector.get_indexes("market_prices")
    }
    if "uq_market_prices_symbol_date_source" in unique_constraints | indexes:
        return
    if "uq_market_prices_symbol_date" not in unique_constraints | indexes:
        return
    # Batch mode is required for SQLite, which cannot drop a named unique
    # constraint directly. It also keeps the operation portable to Postgres.
    with op.batch_alter_table("market_prices", recreate="auto") as batch:
        batch.drop_constraint("uq_market_prices_symbol_date", type_="unique")
        batch.create_unique_constraint(
            "uq_market_prices_symbol_date_source",
            ["symbol", "price_date", "source"],
        )


def downgrade():
    raise RuntimeError("0058 is forward-only; restore a verified backup to roll back")
