from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("freqtrade_smoke", Path(__file__).parents[1] / "scripts" / "smoke_freqtrade.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeBoundaryTests(unittest.TestCase):
    def test_disposable_nonroot_boundary_and_cleanup(self):
        calls = []
        def fake(args, **kwargs):
            calls.append((args, kwargs))
            output = "true\n" if args[1] == "inspect" else '{"ok":true}'
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        with patch.object(smoke, "command", side_effect=fake), redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(), 0)
        args, options = calls[0]
        self.assertIn(smoke.IMAGE, args)
        self.assertIn("@sha256:", smoke.IMAGE)
        self.assertNotIn("--user", args)
        self.assertNotIn("-p", args)
        self.assertNotIn("--publish", args)
        self.assertEqual(args[args.index("--env") + 1], "PAPER_PROBE_CONFIG")
        config = json.loads(options["env"]["PAPER_PROBE_CONFIG"])
        self.assertIs(config["dry_run"], True)
        self.assertEqual(config["initial_state"], "stopped")
        self.assertEqual(config["exchange"]["key"], "")
        self.assertEqual(config["exchange"]["secret"], "")
        self.assertIn("'--dry-run'", smoke.BOOTSTRAP)
        self.assertEqual(calls[-1][0][1:4], ["rm", "--force", "--volumes"])
        self.assertEqual(calls[-1][0][-1], args[args.index("--name") + 1])

    def test_start_failure_still_cleans_exact_container(self):
        with patch.object(smoke, "command", return_value=SimpleNamespace(returncode=1, stdout="", stderr="failed")) as command, redirect_stdout(io.StringIO()):
            self.assertEqual(smoke.main(), 1)
        self.assertEqual(command.call_args_list[-1].args[0][1], "rm")
