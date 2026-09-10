"""Journal, scanner jobs and missing lookup indexes.

Existing create_all deployments may already contain these tables. Validate the
result with the schema checker before serving traffic.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_journal_scanner"
down_revision = "0002_market_price_provenance"
branch_labels = None
depends_on = None

INDEXES = {
    "assets": ["symbol"], "strategies": ["name"],
    "market_prices": ["symbol", "price_date"], "news_articles": ["symbol"],
    "paper_trades": ["symbol"], "strategy_backtests": ["symbol"],
    "strategy_signals": ["symbol"],
    "candidate_decision_journal": ["symbol", "strategy_id", "strategy_type", "decision", "status", "paper_trade_id", "realized_status", "created_at"],
    "scanner_refresh_jobs": ["trigger", "status", "created_at"],
}


def upgrade():
    # Earlier SQLite 0002 deployments left these nullable. Repair them even
    # when Alembic will not execute the corrected 0002 again.
    provenance = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("market_prices")}
    if provenance["source"]["nullable"] or provenance["imported_at"]["nullable"]:
        op.execute("UPDATE market_prices SET source = 'unknown' WHERE source IS NULL")
        op.execute("UPDATE market_prices SET imported_at = CURRENT_TIMESTAMP WHERE imported_at IS NULL")
        with op.batch_alter_table("market_prices") as batch:
            batch.alter_column("source", existing_type=sa.String(64), nullable=False, server_default="unknown")
            batch.alter_column("imported_at", existing_type=sa.DateTime(), nullable=False, server_default=sa.func.now())
    tables = sa.inspect(op.get_bind()).get_table_names()
    if "candidate_decision_journal" not in tables:
        op.create_table(
            "candidate_decision_journal",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id")),
            sa.Column("strategy_type", sa.String(64), nullable=False),
            sa.Column("decision", sa.String(64), nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("reason", sa.Text()),
            sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
            sa.Column("paper_trade_id", sa.Integer(), sa.ForeignKey("paper_trades.id")),
            sa.Column("realized_return", sa.Numeric(12, 6)),
            sa.Column("realized_status", sa.String(64)),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    if "scanner_refresh_jobs" not in tables:
        op.create_table(
            "scanner_refresh_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("trigger", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("message", sa.Text()), sa.Column("payload", sa.JSON()),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("started_at", sa.DateTime()), sa.Column("completed_at", sa.DateTime()),
        )
    for table, columns in INDEXES.items():
        existing = {i["name"]: i for i in sa.inspect(op.get_bind()).get_indexes(table)}
        for column in columns:
            name = f"ix_{table}_{column}"
            if name not in existing:
                op.create_index(name, table, [column], unique=table in {"assets", "strategies"})
            elif table in {"assets", "strategies"}:
                index = existing[name]
                options = index.get("dialect_options") or {}
                partial = options.get("postgresql_where") is not None or options.get("sqlite_where") is not None
                if not index.get("unique") or index.get("column_names") != [column] or partial:
                    raise RuntimeError(f"Refusing migration: {name} must be a unique index on {column}")
    # The ORM represents these as unique indexes. 0001 used unique constraints;
    # PostgreSQL reports redundant constraints as schema drift after adding the
    # indexes. Keep uniqueness via the new index before removing the duplicate.
    if op.get_bind().dialect.name == "postgresql":
        for table, column in (("assets", "symbol"), ("strategies", "name")):
            for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table):
                if constraint["column_names"] == [column]:
                    op.drop_constraint(constraint["name"], table, type_="unique")


def downgrade():
    # New journal/job rows are audit evidence. A backup restore is the explicit
    # rollback; never erase that evidence silently via an automatic downgrade.
    raise RuntimeError("0003 is forward-only; restore a verified backup to roll back")
