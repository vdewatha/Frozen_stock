"""Schema verification must not depend on which service was imported first."""
import subprocess
import sys


def test_standalone_schema_check_registers_lazy_broker_models():
    subprocess.run(
        [sys.executable, "-c",
         "from app.db.schema import Base; "
         "assert {'stock_paper_accounts', 'live_broker_accounts', 'live_pilot', 'live_mandates'} "
         "<= set(Base.metadata.tables)"],
        check=True, capture_output=True, text=True, timeout=15,
    )
