import json
from pathlib import Path
import stat
import tempfile
import unittest

from app.integrations.paper_venue import kraken_observation_config, write_config


class PaperVenueTests(unittest.TestCase):
    def test_fixed_observation_boundaries(self):
        config = kraken_observation_config("observer", "p" * 32, "j" * 32)
        self.assertIs(config["dry_run"], True)
        self.assertEqual(config["trading_mode"], "spot")
        self.assertEqual(config["exchange"]["pair_whitelist"], ["BTC/USD"])
        self.assertFalse(config["exchange"]["key"])
        self.assertFalse(config["exchange"]["secret"])
        self.assertFalse(config["force_entry_enable"])
        self.assertEqual(config["initial_state"], "stopped")
        self.assertEqual(config["api_server"]["listen_ip_address"], "127.0.0.1")

    def test_secrets_and_exclusive_permissions(self):
        for args in (("", "p" * 32, "j" * 32), ("obs", "short", "j" * 32), ("obs", "x" * 32, "x" * 32)):
            with self.assertRaises(ValueError):
                kraken_observation_config(*args)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            config = kraken_observation_config("observer", "p" * 32, "j" * 32)
            write_config(path, config)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), config)
            with self.assertRaises(FileExistsError):
                write_config(path, config)
