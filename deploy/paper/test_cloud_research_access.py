import os
from pathlib import Path
import tempfile
import unittest

from dotenv import dotenv_values

from prepare_cloud_research_access import prepare


class CloudResearchAccessTests(unittest.TestCase):
    def test_allowlist_permissions_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source", Path(folder) / "output"
            source.write_text("ALPACA_API_KEY=paper-key\nALPACA_API_SECRET=paper-secret\n"
                              "LIVE_ALPACA_API_KEY=forbidden\nAUTH_ADMIN_KEY=forbidden\n"
                              "TRADIER_API_KEY=forbidden\n"
                              "TRADIER_MARKET_DATA_API_KEY=data-key\n")
            prepare(source, output)
            self.assertEqual(dotenv_values(output), {
                "PAPER_ALPACA_API_KEY": "paper-key",
                "PAPER_ALPACA_API_SECRET": "paper-secret",
                "TRADIER_MARKET_DATA_API_KEY": "data-key",
            })
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                prepare(source, output)

    def test_explicit_paper_keys_take_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source", Path(folder) / "output"
            source.write_text("PAPER_ALPACA_API_KEY=explicit\n"
                              "PAPER_ALPACA_API_SECRET=explicit-secret\n"
                              "ALPACA_API_KEY=legacy\nALPACA_API_SECRET=legacy-secret\n")
            prepare(source, output)
            self.assertEqual(dotenv_values(output)["PAPER_ALPACA_API_KEY"], "explicit")

    def test_incomplete_credentials_do_not_create_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "source", Path(folder) / "output"
            source.write_text("ALPACA_API_KEY=key\n")
            with self.assertRaises(ValueError):
                prepare(source, output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
