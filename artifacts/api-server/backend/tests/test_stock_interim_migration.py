"""PostgreSQL integration coverage for repairing the populated interim 0010."""
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.session import engine


def test_populated_interim_stock_schema_repairs_through_head_with_audit_triggers():
    if engine.dialect.name != "postgresql":
        pytest.skip("interim-schema repair is PostgreSQL-specific")
    schema = f"stock_interim_{uuid4().hex}"
    quoted = f'"{schema}"'
    raw = make_url(settings.database_url)
    scoped_url = raw.update_query_dict({"options": f"-csearch_path={schema}"})
    isolated = create_engine(scoped_url)
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {quoted}"))
        with isolated.begin() as connection:
            # This reproduces the earlier applied 0010 names and the
            # append-only triggers that made populated repair fail.
            connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('0010_stock_training_jobs')"))
            connection.execute(text("CREATE TABLE research_model_runs (run_id varchar(64) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO research_model_runs VALUES ('cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc')"))
            connection.execute(text("""
                CREATE TABLE stock_dataset_snapshots (
                  id varchar(64) PRIMARY KEY, dataset_sha256 varchar(64) UNIQUE NOT NULL,
                  universe json NOT NULL, provider_provenance json NOT NULL, cutoff_at timestamp NOT NULL,
                  feature_config_id varchar(128) NOT NULL, artifact_path text NOT NULL,
                  row_count integer NOT NULL, created_at timestamp NOT NULL DEFAULT now()
                )
            """))
            connection.execute(text("""
                CREATE TABLE stock_holdout_reservations (
                  id serial PRIMARY KEY, universe_key varchar(64) NOT NULL, horizon_bars integer NOT NULL,
                  period_start timestamp NOT NULL, period_end timestamp NOT NULL,
                  dataset_snapshot_id varchar(64) REFERENCES stock_dataset_snapshots(id) NOT NULL,
                  reserved_by_job_id varchar(36) UNIQUE, purpose varchar(32) NOT NULL, created_at timestamp NOT NULL DEFAULT now(),
                  CONSTRAINT uq_stock_holdout_snapshot_horizon UNIQUE(dataset_snapshot_id, horizon_bars)
                )
            """))
            connection.execute(text("""
                CREATE TABLE stock_training_jobs (
                  id varchar(36) PRIMARY KEY, dedupe_key varchar(64) NOT NULL, trigger varchar(32) NOT NULL,
                  status varchar(32) NOT NULL, requested_by varchar(32) NOT NULL, request_payload json NOT NULL,
                  dataset_snapshot_id varchar(64) REFERENCES stock_dataset_snapshots(id) NOT NULL,
                  holdout_reservation_id integer REFERENCES stock_holdout_reservations(id) NOT NULL,
                  celery_task_id varchar(64) UNIQUE, attempts integer NOT NULL DEFAULT 0, queue_error text,
                  failure_code varchar(96), failure_detail text,
                  result_run_id varchar(64) REFERENCES research_model_runs(run_id),
                  created_at timestamp NOT NULL DEFAULT now(), started_at timestamp, completed_at timestamp,
                  recovery_attempted_at timestamp,
                  CONSTRAINT ck_stock_training_job_status CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed')),
                  CONSTRAINT ck_stock_training_job_attempts CHECK(attempts >= 0 AND attempts <= 3)
                )
            """))
            connection.execute(text("""
                CREATE TABLE stock_paper_model_bindings (
                  id serial PRIMARY KEY, model_run_id varchar(64) REFERENCES research_model_runs(run_id) NOT NULL,
                  dataset_snapshot_id varchar(64) REFERENCES stock_dataset_snapshots(id) NOT NULL,
                  binding_sha256 varchar(64) UNIQUE NOT NULL, purpose varchar(64) NOT NULL,
                  paper_only boolean NOT NULL, live_authorized boolean NOT NULL, bound_by varchar(32) NOT NULL,
                  reason text NOT NULL, created_at timestamp NOT NULL DEFAULT now()
                )
            """))
            connection.execute(text("""
                CREATE FUNCTION stock_training_immutable_row() RETURNS trigger AS
                $$ BEGIN RAISE EXCEPTION 'stock training audit rows are immutable'; END; $$ LANGUAGE plpgsql
            """))
            for table in ("stock_dataset_snapshots", "stock_holdout_reservations", "stock_paper_model_bindings"):
                connection.execute(text(
                    f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                    "FOR EACH ROW EXECUTE FUNCTION stock_training_immutable_row()"
                ))
            connection.execute(text("""
                INSERT INTO stock_dataset_snapshots
                  (id,dataset_sha256,universe,provider_provenance,cutoff_at,feature_config_id,artifact_path,row_count)
                VALUES
                  ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                   'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                   '{"symbols":["SPY"]}', '{"provider":"yfinance"}', '2024-01-01',
                   'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd', '/legacy/snapshot', 10)
            """))
            connection.execute(text("""
                INSERT INTO stock_holdout_reservations
                  (universe_key,horizon_bars,period_start,period_end,dataset_snapshot_id,reserved_by_job_id,purpose)
                VALUES ('eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',5,'2023-01-01','2024-01-02',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','12345678-1234-1234-1234-123456789abc','model_selection')
            """))
            connection.execute(text("""
                INSERT INTO stock_training_jobs
                  (id,dedupe_key,trigger,status,requested_by,request_payload,dataset_snapshot_id,holdout_reservation_id,result_run_id)
                VALUES ('12345678-1234-1234-1234-123456789abc',
                        'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff',
                        'manual','succeeded','researcher','{}',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1,
                        'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc')
            """))
            connection.execute(text("""
                INSERT INTO stock_paper_model_bindings
                  (model_run_id,dataset_snapshot_id,binding_sha256,purpose,paper_only,live_authorized,bound_by,reason)
                VALUES ('cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
                        'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                        '1111111111111111111111111111111111111111111111111111111111111111',
                        'paper',true,false,'operator','legacy row')
            """))

        backend = Path(__file__).resolve().parents[1]
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "alembic"))
        # Deliberately never log this URL: it can contain credentials.
        # ConfigParser treats '%' as interpolation. This string is kept
        # internal to Alembic and is never logged by this test.
        config.set_main_option(
            "sqlalchemy.url",
            scoped_url.render_as_string(hide_password=False).replace("%", "%%"),
        )
        command.upgrade(config, "head")

        with isolated.connect() as connection:
            inspector = inspect(connection)
            assert MigrationContext.configure(connection).get_current_heads() == ("0014_stock_holdout_consumption",)
            assert connection.execute(text("SELECT count(*) FROM stock_dataset_snapshots")).scalar() == 1
            assert connection.execute(text("SELECT count(*) FROM stock_holdout_reservations")).scalar() == 1
            assert connection.execute(text("SELECT count(*) FROM stock_training_jobs")).scalar() == 1
            assert connection.execute(text("SELECT count(*) FROM stock_paper_model_bindings")).scalar() == 1
            assert connection.execute(text("SELECT count(*) FROM stock_model_registry")).scalar() == 1
            assert {"snapshot_id", "cutoff_date", "provider", "horizon_days", "artifact_sha256", "metadata_json"} <= {
                column["name"] for column in inspector.get_columns("stock_dataset_snapshots")
            }
            for table in (
                "stock_dataset_snapshots",
                "stock_holdout_reservations",
                "stock_paper_model_bindings",
                "stock_model_registry",
            ):
                triggers = connection.execute(text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = to_regclass(:table) AND NOT tgisinternal"
                ), {"table": table}).scalars().all()
                assert f"{table}_immutable" in triggers
    finally:
        isolated.dispose()
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))