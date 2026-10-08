"""Allow provider-specific daily market-price observations to coexist."""

from alembic import op


revision = "0058_market_price_provider_key"
down_revision = "0057_learning_worker_leases"
branch_labels = None
depends_on = None


def upgrade():
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
