"""Protect existing promotion-readiness evidence from edits."""
from alembic import op
import sqlalchemy as sa


revision = "0025_promo_immutable"
down_revision = "0024_stock_continuous_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        exists = bind.execute(sa.text(
            "SELECT 1 FROM pg_trigger "
            "WHERE tgrelid = to_regclass('stock_paper_promotion_readiness_reports') "
            "AND tgname = 'stock_paper_promotion_readiness_reports_immutable' "
            "AND NOT tgisinternal"
        )).scalar()
        if not exists:
            op.execute(
                "CREATE OR REPLACE FUNCTION stock_promotion_readiness_immutable_row() "
                "RETURNS trigger AS "
                "$$ BEGIN RAISE EXCEPTION "
                "'stock_paper_promotion_readiness_reports is immutable'; "
                "END; $$ LANGUAGE plpgsql"
            )
            op.execute(
                "CREATE TRIGGER stock_paper_promotion_readiness_reports_immutable "
                "BEFORE UPDATE OR DELETE ON stock_paper_promotion_readiness_reports "
                "FOR EACH ROW EXECUTE FUNCTION stock_promotion_readiness_immutable_row()"
            )
    elif bind.dialect.name == "sqlite":
        exists = bind.execute(sa.text(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'trigger' "
            "AND name = 'stock_paper_promotion_readiness_reports_immutable_update'"
        )).scalar()
        if not exists:
            op.execute(
                "CREATE TRIGGER stock_paper_promotion_readiness_reports_immutable_update "
                "BEFORE UPDATE ON stock_paper_promotion_readiness_reports "
                "BEGIN SELECT RAISE(ABORT, "
                "'stock_paper_promotion_readiness_reports is immutable'); END"
            )
            op.execute(
                "CREATE TRIGGER stock_paper_promotion_readiness_reports_immutable_delete "
                "BEFORE DELETE ON stock_paper_promotion_readiness_reports "
                "BEGIN SELECT RAISE(ABORT, "
                "'stock_paper_promotion_readiness_reports is immutable'); END"
            )


def downgrade() -> None:
    raise RuntimeError("0025 is forward-only; restore a verified backup to roll back")