"""add market price provenance

Revision ID: 0002_market_price_provenance
Revises: 0001_initial_schema
Create Date: 2026-06-10
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_market_price_provenance"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("market_prices")}
    if "source" not in columns:
        op.add_column("market_prices", sa.Column("source", sa.String(length=64), nullable=True))
    if "imported_at" not in columns:
        op.add_column("market_prices", sa.Column("imported_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE market_prices SET source = 'unknown' WHERE source IS NULL")
    op.execute("UPDATE market_prices SET imported_at = CURRENT_TIMESTAMP WHERE imported_at IS NULL")

    with op.batch_alter_table("market_prices") as batch:
        batch.alter_column("source", existing_type=sa.String(64), nullable=False, server_default="unknown")
        batch.alter_column("imported_at", existing_type=sa.DateTime(), nullable=False, server_default=sa.func.now())


def downgrade() -> None:
    # 0001 already includes these columns. Preserve data and that revision's contract.
    pass
