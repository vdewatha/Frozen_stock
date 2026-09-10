import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("trainer_launcher", Path(__file__).parents[1] / "scripts/start_continuous_training.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class TrainingLauncherTests(unittest.TestCase):
    def test_isolated_read_only_source_and_no_credentials(self):
        args = launcher.launch_arguments(Path("/private/research.sqlite"), Path("/training-output"))
        self.assertIn("type=bind,src=/private/research.sqlite,dst=/source/research.sqlite,readonly", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--restart") + 1], "unless-stopped")
        self.assertIn("--read-only", args)
        self.assertNotIn("provider.json", " ".join(args))
        self.assertNotIn("--execute-paper", args)
        self.assertIn("--continuous", args)
