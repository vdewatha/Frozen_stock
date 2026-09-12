"""Add tamper-evident, append-only audit event chaining."""
from alembic import op
import sqlalchemy as sa
from hashlib import sha256
import json

revision = "0027_audit_event_chain"
down_revision = "0026_stock_paper_recovery_merge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("previous_event_sha256", sa.String(length=64)))
    op.add_column("audit_logs", sa.Column("event_sha256", sa.String(length=64)))
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, event_type, entity_type, entity_id, action, status, message, payload "
            "FROM audit_logs ORDER BY id"
        )
    ).mappings().all()
    previous = None
    for row in rows:
        payload = row["payload"] or {}
        digest_payload = {
            "event_type": row["event_type"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "action": row["action"],
            "status": row["status"],
            "message": row["message"],
            "payload": payload,
            "previous_event_sha256": previous,
        }
        digest = sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE audit_logs SET previous_event_sha256 = :previous, event_sha256 = :digest "
                "WHERE id = :id"
            ),
            {"previous": previous, "digest": digest, "id": row["id"]},
        )
        previous = digest
    with op.batch_alter_table("audit_logs") as batch:
        batch.alter_column("event_sha256", existing_type=sa.String(length=64), nullable=False)
    op.create_index("ix_audit_logs_previous_event_sha256", "audit_logs", ["previous_event_sha256"])
    op.create_index("ix_audit_logs_event_sha256", "audit_logs", ["event_sha256"], unique=True)
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE OR REPLACE FUNCTION audit_logs_immutable_row() "
            "RETURNS trigger AS $$ BEGIN RAISE EXCEPTION "
            "'audit_logs is append-only'; END; $$ LANGUAGE plpgsql"
        )
        op.execute(
            "CREATE TRIGGER audit_logs_immutable "
            "BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW "
            "EXECUTE FUNCTION audit_logs_immutable_row()"
        )
    elif bind.dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER audit_logs_immutable_update BEFORE UPDATE ON audit_logs "
            "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END"
        )
        op.execute(
            "CREATE TRIGGER audit_logs_immutable_delete BEFORE DELETE ON audit_logs "
            "BEGIN SELECT RAISE(ABORT, 'audit_logs is append-only'); END"
        )


def downgrade() -> None:
    raise RuntimeError("0027 is forward-only; restore a verified backup to roll back")