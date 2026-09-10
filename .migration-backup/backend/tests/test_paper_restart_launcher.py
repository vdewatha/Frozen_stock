import subprocess
import unittest
from unittest.mock import patch, Mock
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import accept_paper_restart as acceptance
from start_local_paper_research import worker_arguments
from run_paper_research import run_cycle


class RestartLauncherTests(unittest.TestCase):
    def test_acceptance_never_retries_first_mutation_and_cleans_owned_volume(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if acceptance.BEFORE in args else 0, "", "")
        with patch.object(acceptance, "command", side_effect=command):
            self.assertEqual(acceptance.main(), 1)
        self.assertEqual(sum(acceptance.BEFORE in args for args in calls), 1)
        self.assertFalse(any(acceptance.AFTER in args for args in calls))
        self.assertEqual(calls[-1][:3], ["docker", "volume", "rm"])
        self.assertEqual(calls[-1][-1], calls[0][-1])

    def test_worker_is_local_image_only_explicit_paper_and_private_network(self):
        args = worker_arguments(Path("/private/project/.paper-venue"), 1)
        self.assertEqual(args[args.index("--pull") + 1], "never")
        self.assertIn("ALLOW_LIVE_TRADING=false", args)
        self.assertIn("--execute-paper", args)
        self.assertIn("container:trading-paper-kraken", args)
        self.assertNotIn("--publish", args)
        self.assertIn("--read-only", args)

    def test_failed_data_stage_still_reconciles_and_never_submits(self):
        client = Mock()
        with patch("app.services.crypto_pipeline.run_crypto_cycle", side_effect=RuntimeError("private details")), \
             patch("app.services.paper_recovery.run_recovery_cycle", return_value={"status": "success"}) as recovery, \
             patch("app.services.paper_trial_cycle.run_paper_trial_cycle") as trial:
            result = run_cycle(Mock(), client, 1)
            self.assertEqual(result["data"], {"status": "error", "reason": "data_stage_failed"})
            recovery.assert_called_once()
            trial.assert_not_called()
