from alembic import op
import sqlalchemy as sa

revision = "0009_intraday_alpaca"
down_revision = "0008_external_paper_fills"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("intraday_bars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False, index=True),
        sa.Column("timeframe", sa.String(8), nullable=False, server_default="1m"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("open", sa.Numeric(18, 6), nullable=False), sa.Column("high", sa.Numeric(18, 6), nullable=False),
        sa.Column("low", sa.Numeric(18, 6), nullable=False), sa.Column("close", sa.Numeric(18, 6), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False), sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("feed_class", sa.String(32), nullable=False), sa.Column("exchange_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "timeframe", "opened_at", name="uq_intraday_bar"))
    op.create_table("corporate_actions",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("symbol", sa.String(16), nullable=False, index=True),
        sa.Column("action_type", sa.String(32), nullable=False), sa.Column("ex_date", sa.Date(), nullable=False, index=True),
        sa.Column("value", sa.Numeric(18, 8), nullable=False), sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("raw_payload", sa.JSON()), sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("symbol", "action_type", "ex_date", "value", name="uq_corporate_action"))

def downgrade():
    op.drop_table("corporate_actions")
    op.drop_table("intraday_bars")