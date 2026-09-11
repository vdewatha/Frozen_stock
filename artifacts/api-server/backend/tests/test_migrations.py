import hashlib
import tempfile
import logging
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

from app.db.schema import assert_schema_current, migration_config


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'isolated.db'}")
        self.config = migration_config()
        self.config.set_main_option("sqlalchemy.url", str(self.engine.url))

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def test_fresh_upgrade_and_repeat(self):
        security_logger = logging.getLogger("trading.security")
        security_logger.disabled = False
        with self.assertRaises(RuntimeError):
            assert_schema_current(self.engine)
        command.upgrade(self.config, "head")
        self.assertFalse(security_logger.disabled)
        command.upgrade(self.config, "head")
        assert_schema_current(self.engine)
        inspector = inspect(self.engine)
        self.assertIn("candidate_decision_journal", inspector.get_table_names())
        columns = {c["name"]: c for c in inspector.get_columns("market_prices")}
        self.assertFalse(columns["source"]["nullable"])
        self.assertFalse(columns["imported_at"]["nullable"])

    def test_trial_exit_order_link_backfills_hashed_legacy_attempt(self):
        command.upgrade(self.config, "0020_trial_baseline_equity")
        raw_key = "trial-exit:7:attempt-1"
        client_order_id = "sp-" + hashlib.sha256(raw_key.encode()).hexdigest()[:45]
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO stock_paper_trials "
                "(id,binding_id,status,actor,policy,lineage) "
                "VALUES ('trial-1',1,'stopped','test','{}','{}')"
            ))
            connection.execute(text(
                "INSERT INTO stock_paper_trial_decisions "
                "(id,trial_id,symbol,bar_timestamp,decision_timestamp,action,qualifying,lineage) "
                "VALUES (11,'trial-1','SPY','2025-01-02','2025-01-02','buy',1,'{}')"
            ))
            connection.execute(text(
                "INSERT INTO stock_paper_trial_lots "
                "(id,trial_id,symbol,entry_decision_id,quantity,entry_session,"
                "planned_horizon_sessions,stop_fraction) "
                "VALUES (7,'trial-1','SPY',11,1,'2025-01-02',5,0.02)"
            ))
            connection.execute(text(
                "INSERT INTO stock_paper_orders "
                "(id,account_id,client_order_id,symbol,side,quantity,order_type,time_in_force,"
                "reserved_cash,status,uncertain_submission,source) "
                "VALUES (13,1,:client_id,'SPY','sell',1,'market','day',0,'filled',0,'manual_control_room')"
            ), {"client_id": client_order_id})
        command.upgrade(self.config, "head")
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text(
                    "SELECT trial_lot_id FROM stock_paper_orders WHERE id = 13"
                )).scalar(),
                7,
            )

    def test_legacy_provenance_absent_preserves_prices(self):
        command.upgrade(self.config, "0001_initial_schema")
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE market_prices DROP COLUMN source"))
            connection.execute(text("ALTER TABLE market_prices DROP COLUMN imported_at"))
            connection.execute(text("INSERT INTO market_prices(id,symbol,price_date,close) VALUES (1,'SPY','2024-01-02',400)"))
        command.upgrade(self.config, "head")
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT close,source,imported_at FROM market_prices")).one()
            self.assertEqual(row[0], 400)
            self.assertEqual(row[1], "unknown")
            self.assertIsNotNone(row[2])
        assert_schema_current(self.engine)

    def test_existing_provenance_and_create_all_tables(self):
        from app.db.base import Base
        command.upgrade(self.config, "0001_initial_schema")
        Base.metadata.create_all(self.engine, tables=[Base.metadata.tables[name] for name in ("candidate_decision_journal", "scanner_refresh_jobs")])
        with self.engine.begin() as connection:
            connection.execute(text("INSERT INTO market_prices(id,symbol,price_date,source) VALUES (1,'SPY','2024-01-02','yahoo')"))
        command.upgrade(self.config, "head")
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT source FROM market_prices")).scalar(), "yahoo")
        assert_schema_current(self.engine)

    def test_old_nullable_sqlite_revision_0002(self):
        command.upgrade(self.config, "0002_market_price_provenance")
        with self.engine.begin() as connection:
            connection.execute(text("ALTER TABLE market_prices DROP COLUMN source"))
            connection.execute(text("ALTER TABLE market_prices DROP COLUMN imported_at"))
            connection.execute(text("ALTER TABLE market_prices ADD COLUMN source VARCHAR(64)"))
            connection.execute(text("ALTER TABLE market_prices ADD COLUMN imported_at DATETIME"))
            connection.execute(text("INSERT INTO market_prices(id,symbol,price_date) VALUES (1,'SPY','2024-01-02')"))
        command.upgrade(self.config, "head")
        assert_schema_current(self.engine)
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT source FROM market_prices")).scalar(), "unknown")

    def test_missing_table_even_if_stamped_is_rejected(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE scanner_refresh_jobs"))
        with self.assertRaisesRegex(RuntimeError, "missing table"):
            assert_schema_current(self.engine)

    def test_wrong_existing_unique_index_fails_without_dropping_constraint(self):
        command.upgrade(self.config, "0002_market_price_provenance")
        with self.engine.begin() as connection:
            connection.execute(text("CREATE INDEX ix_assets_symbol ON assets (symbol)"))
        with self.assertRaisesRegex(RuntimeError, "must be a unique index"):
            command.upgrade(self.config, "head")
        self.assertTrue(any(c["column_names"] == ["symbol"] for c in inspect(self.engine).get_unique_constraints("assets")))

    def test_partial_unique_index_cannot_replace_full_uniqueness(self):
        command.upgrade(self.config, "0002_market_price_provenance")
        with self.engine.begin() as connection:
            connection.execute(text("CREATE UNIQUE INDEX ix_assets_symbol ON assets (symbol) WHERE is_active = 1"))
        with self.assertRaisesRegex(RuntimeError, "must be a unique index"):
            command.upgrade(self.config, "head")

    def test_safe_provenance_downgrade_and_forward_only_journal(self):
        command.upgrade(self.config, "0002_market_price_provenance")
        command.downgrade(self.config, "0001_initial_schema")
        self.assertIn("source", {c["name"] for c in inspect(self.engine).get_columns("market_prices")})
        command.upgrade(self.config, "head")
        with self.assertRaisesRegex(RuntimeError, "forward-only"):
            command.downgrade(self.config, "0002_market_price_provenance")
