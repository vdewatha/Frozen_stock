import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

from app.integrations.paper_venue import kraken_execution_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_paper_venue import launch_arguments, BOOTSTRAP


class PaperLauncherTests(unittest.TestCase):
    def test_pinned_private_persistent_dryrun(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / ".paper-venue"
            state.mkdir(mode=0o700)
            command = launch_arguments(state, 8080)
            self.assertIn("127.0.0.1:8080:8080", command)
            self.assertIn("--read-only", command)
            self.assertIn("--dry-run", BOOTSTRAP)
            self.assertTrue(any("@sha256:" in arg for arg in command))
            self.assertIn(f"type=bind,src={state.resolve()},dst=/paper-state", command)
            os.chmod(state, 0o755)
            with self.assertRaises(ValueError):
                launch_arguments(state, 8080)

    def test_execution_profile_never_includes_exchange_credentials(self):
        config = kraken_execution_config("paper", "p" * 40, "j" * 40)
        self.assertIs(config["dry_run"], True)
        self.assertIs(config["force_entry_enable"], True)
        self.assertEqual(config["exchange"]["key"], "")
        self.assertEqual(config["exchange"]["secret"], "")
        self.assertEqual(config["bot_name"], "kraken-paper-execution")
