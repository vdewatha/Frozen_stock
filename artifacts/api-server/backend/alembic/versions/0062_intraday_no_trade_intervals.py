"""Record provider-verified no-trade minutes for partial-venue intraday feeds.

Alpaca IEX publishes a one-minute bar only when IEX traded in that minute.
Gap accounting needs explicit evidence that a closed minute was re-queried
and still had no bar, without fabricating a price row in ``intraday_bars``.
"""
from alembic import op
import sqlalchemy as sa

revision = "0062_intraday_no_trade"
down_revision = "0061_scalp_timezone_reconciled"
branch_labels = None
depends_on = None

TABLE = "intraday_no_trade_intervals"


def _create_index_if_missing(bind, name: str, columns: list[str]) -> None:
    if name not in {index["name"] for index in sa.inspect(bind).get_indexes(TABLE)}:
        op.create_index(name, TABLE, columns)


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(TABLE):
        table = sa.Table(
            TABLE,
            sa.MetaData(),
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("timeframe", sa.String(length=8), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("feed_class", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.String(length=64), nullable=False),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("query_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("query_end", sa.DateTime(timezone=True), nullable=False),
            sa.Column("previous_bar_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("next_bar_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_minutes", sa.Integer(), nullable=False),
            sa.UniqueConstraint(
                "symbol", "timeframe", "opened_at", "provider",
                name="uq_intraday_no_trade_provider",
            ),
        )
        bind.execute(sa.schema.CreateTable(table, if_not_exists=True))
    _create_index_if_missing(bind, "ix_intraday_no_trade_intervals_symbol", ["symbol"])
    _create_index_if_missing(bind, "ix_intraday_no_trade_intervals_opened_at", ["opened_at"])


def downgrade():
    raise RuntimeError("0062 is forward-only; restore a verified backup to roll back")
