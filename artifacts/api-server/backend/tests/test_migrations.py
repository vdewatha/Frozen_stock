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
        self.assertIn("stock_paper_promotion_readiness_reports", inspector.get_table_names())
        with self.engine.connect() as connection:
            trigger_names = {
                row[0]
                for row in connection.execute(text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = "
                    "'stock_paper_promotion_readiness_reports'"
                ))
            }
        self.assertIn(
            "stock_paper_promotion_readiness_reports_immutable_update",
            trigger_names,
        )
        self.assertIn(
            "stock_paper_promotion_readiness_reports_immutable_delete",
            trigger_names,
        )

    def test_scheduled_trial_handoff_accepts_publish_preapplied_schema(self):
        command.upgrade(self.config, "0033_promotion_decision_retries")
        with self.engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE stock_learning_cycles ADD COLUMN binding_id INTEGER "
                "REFERENCES stock_paper_model_bindings(id)"
            ))
            connection.execute(text(
                "CREATE UNIQUE INDEX uq_stock_learning_cycle_binding "
                "ON stock_learning_cycles(binding_id)"
            ))
            connection.execute(text(
                "ALTER TABLE stock_paper_model_bindings ADD COLUMN source_cycle_id VARCHAR(64)"
            ))
            connection.execute(text(
                "CREATE UNIQUE INDEX uq_stock_binding_source_cycle "
                "ON stock_paper_model_bindings(source_cycle_id)"
            ))
            connection.execute(text(
                "ALTER TABLE stock_paper_trials ADD COLUMN source_cycle_id VARCHAR(64)"
            ))
            connection.execute(text(
                "CREATE UNIQUE INDEX uq_stock_trial_source_cycle "
                "ON stock_paper_trials(source_cycle_id)"
            ))

        command.upgrade(self.config, "0034_scheduled_trial_handoff")
        with self.engine.connect() as connection:
            self.assertIn(
                "binding_id",
                {column["name"] for column in inspect(connection).get_columns("stock_learning_cycles")},
            )
            self.assertIn(
                "source_cycle_id",
                {column["name"] for column in inspect(connection).get_columns("stock_paper_model_bindings")},
            )
            self.assertIn(
                "source_cycle_id",
                {column["name"] for column in inspect(connection).get_columns("stock_paper_trials")},
            )
            check = next(
                item
                for item in inspect(connection).get_check_constraints("stock_learning_cycles")
                if item["name"] == "ck_stock_learning_cycle_status"
            )
            self.assertIn("awaiting_admission", check["sqltext"])

        # A retry after a stale revision marker must tolerate the already-applied shape.
        command.stamp(self.config, "0033_promotion_decision_retries")
        command.upgrade(self.config, "0034_scheduled_trial_handoff")

    def test_isolated_live_broker_migration_accepts_publish_precreated_tables(self):
        command.upgrade(self.config, "0038_stock_accuracy")
        from app.db.base import Base

        live_tables = [
            table
            for table in Base.metadata.sorted_tables
            if table.name.startswith("live_broker_")
        ]
        self.assertEqual(len(live_tables), 7)
        Base.metadata.create_all(self.engine, tables=live_tables)

        command.upgrade(self.config, "0039_isolated_live_broker")

        inspector = inspect(self.engine)
        for table in live_tables:
            self.assertIn(table.name, inspector.get_table_names())
        self.assertIn(
            "ix_live_broker_accounts_status",
            {index["name"] for index in inspector.get_indexes("live_broker_accounts")},
        )

    def test_controlled_live_pilot_migration_accepts_publish_precreated_tables(self):
        command.upgrade(self.config, "0039_isolated_live_broker")
        from app.db.base import Base

        pilot_tables = [
            Base.metadata.tables[name]
            for name in ("live_pilot", "live_pilot_events")
        ]
        Base.metadata.create_all(self.engine, tables=pilot_tables)

        command.upgrade(self.config, "0040_controlled_live_pilot")
        command.stamp(self.config, "0039_isolated_live_broker")
        command.upgrade(self.config, "0040_controlled_live_pilot")

        inspector = inspect(self.engine)
        self.assertIn("live_pilot", inspector.get_table_names())
        self.assertIn("live_pilot_events", inspector.get_table_names())
        self.assertIn(
            "ix_live_pilot_status",
            {index["name"] for index in inspector.get_indexes("live_pilot")},
        )
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT COUNT(*) FROM live_pilot WHERE id = 1")).scalar(),
                1,
            )

    def test_paper_graduation_migration_accepts_publish_precreated_tables(self):
        command.upgrade(self.config, "0040_controlled_live_pilot")
        from app.db.base import Base

        graduation_table = Base.metadata.tables["stock_paper_graduation_packages"]
        Base.metadata.create_all(self.engine, tables=[graduation_table])

        command.upgrade(self.config, "0041_paper_graduation_packages")
        command.stamp(self.config, "0040_controlled_live_pilot")
        command.upgrade(self.config, "0041_paper_graduation_packages")

        inspector = inspect(self.engine)
        self.assertIn("stock_paper_graduation_packages", inspector.get_table_names())
        self.assertIn(
            "ix_stock_paper_graduation_packages_decision",
            {
                index["name"]
                for index in inspector.get_indexes("stock_paper_graduation_packages")
            },
        )
        with self.engine.connect() as connection:
            trigger_names = {
                row[0]
                for row in connection.execute(text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = "
                    "'stock_paper_graduation_packages'"
                ))
            }
        self.assertIn("stock_paper_graduation_packages_immutable_update", trigger_names)
        self.assertIn("stock_paper_graduation_packages_immutable_delete", trigger_names)

    def test_intraday_provider_migration_accepts_missing_legacy_constraint(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                """
                CREATE TABLE intraday_bars (
                    id INTEGER PRIMARY KEY,
                    symbol VARCHAR(16) NOT NULL,
                    timeframe VARCHAR(8) NOT NULL,
                    opened_at DATETIME NOT NULL,
                    open NUMERIC NOT NULL,
                    high NUMERIC NOT NULL,
                    low NUMERIC NOT NULL,
                    close NUMERIC NOT NULL,
                    volume BIGINT NOT NULL,
                    provider VARCHAR(32) NOT NULL,
                    feed_class VARCHAR(32) NOT NULL,
                    exchange_timestamp DATETIME NOT NULL,
                    ingested_at DATETIME NOT NULL
                )
                """
            )
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES ('0041_paper_graduation_packages')"
            )

        command.upgrade(self.config, "0042_intraday_provider")
        inspector = inspect(self.engine)
        self.assertIn(
            "uq_intraday_bar_provider",
            {item["name"] for item in inspector.get_unique_constraints("intraday_bars")},
        )

    def test_publish_managed_schema_allows_stale_revision_marker(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as connection:
            connection.execute(text(
                "UPDATE alembic_version SET version_num = '0020_trial_baseline_equity'"
            ))

        with self.assertRaisesRegex(RuntimeError, "Database migration required"):
            assert_schema_current(self.engine)
        assert_schema_current(self.engine, require_migration_head=False)

    def test_promotion_readiness_reports_are_append_only(self):
        command.upgrade(self.config, "0023_stock_model_lifecycle")
        with self.engine.begin() as connection:
            connection.execute(text(
                "DROP TRIGGER stock_paper_promotion_readiness_reports_immutable_update"
            ))
            connection.execute(text(
                "DROP TRIGGER stock_paper_promotion_readiness_reports_immutable_delete"
            ))
        command.upgrade(self.config, "head")
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO stock_paper_promotion_readiness_reports "
                "(trial_id,report_hash,decision,gates,lineage,policy) "
                "VALUES ('trial-1','hash-1','unknown','{}','{}','{}')"
            ))

        with self.engine.connect() as connection:
            with self.assertRaisesRegex(Exception, "immutable"):
                connection.execute(text(
                    "UPDATE stock_paper_promotion_readiness_reports "
                    "SET decision = 'pass' WHERE report_hash = 'hash-1'"
                ))
            connection.rollback()

            with self.assertRaisesRegex(Exception, "immutable"):
                connection.execute(text(
                    "DELETE FROM stock_paper_promotion_readiness_reports "
                    "WHERE report_hash = 'hash-1'"
                ))
            connection.rollback()

        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text(
                    "SELECT decision FROM stock_paper_promotion_readiness_reports "
                    "WHERE report_hash = 'hash-1'"
                )).scalar(),
                "unknown",
            )

    def test_agent_comparison_reports_are_append_only(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO agent_research_reports (report_id,content_sha256,report) "
                "VALUES ('report-1','digest-1','{}')"
            ))
        with self.engine.connect() as connection:
            for statement in (
                "UPDATE agent_research_reports SET report = '{\"changed\":true}'",
                "DELETE FROM agent_research_reports",
            ):
                with self.assertRaisesRegex(Exception, "immutable"):
                    connection.exec_driver_sql(statement)
                connection.rollback()
        assert_schema_current(self.engine)

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
