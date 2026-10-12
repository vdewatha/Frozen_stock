"""Durably associate every trial exit attempt with its owned lot."""
import hashlib

from alembic import op
import sqlalchemy as sa

revision = "0021_trial_exit_order_link"
down_revision = "0020_trial_baseline_equity"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    order_columns = {column["name"] for column in inspector.get_columns("stock_paper_orders")}
    foreign_keys = {
        constraint.get("name") for constraint in inspector.get_foreign_keys("stock_paper_orders")
    }
    indexes = {index.get("name") for index in inspector.get_indexes("stock_paper_orders")}

    with op.batch_alter_table("stock_paper_orders") as batch:
        # Replit's hosted database can contain this column from a partially
        # applied release while its Alembic marker still points earlier.
        if "trial_lot_id" not in order_columns:
            batch.add_column(sa.Column("trial_lot_id", sa.Integer(), nullable=True))
        if "fk_stock_paper_orders_trial_lot" not in foreign_keys:
            batch.create_foreign_key(
                "fk_stock_paper_orders_trial_lot",
                "stock_paper_trial_lots",
                ["trial_lot_id"],
                ["id"],
            )
        if "ix_stock_paper_orders_trial_lot_id" not in indexes:
            batch.create_index("ix_stock_paper_orders_trial_lot_id", ["trial_lot_id"])

    lots = bind.execute(sa.text("SELECT id FROM stock_paper_trial_lots")).mappings().all()
    orders = bind.execute(sa.text(
        "SELECT id, client_order_id FROM stock_paper_orders "
        "WHERE side = 'sell' AND trial_lot_id IS NULL"
    )).mappings().all()
    # The shared ledger hashes idempotency keys before persisting them. Match
    # every existing sell against all feasible historical attempt keys.
    max_attempt = len(orders) + 1
    expected = {}
    for lot in lots:
        for attempt in range(1, max_attempt + 1):
            raw = f"trial-exit:{lot['id']}:attempt-{attempt}"
            expected["sp-" + hashlib.sha256(raw.encode()).hexdigest()[:45]] = lot["id"]
    for order in orders:
        lot_id = expected.get(order["client_order_id"])
        if lot_id is not None:
            bind.execute(
                sa.text("UPDATE stock_paper_orders SET trial_lot_id = :lot_id WHERE id = :order_id"),
                {"lot_id": lot_id, "order_id": order["id"]},
            )

def downgrade() -> None:
    with op.batch_alter_table("stock_paper_orders") as batch:
        batch.drop_index("ix_stock_paper_orders_trial_lot_id")
        batch.drop_constraint("fk_stock_paper_orders_trial_lot", type_="foreignkey")
        batch.drop_column("trial_lot_id")
