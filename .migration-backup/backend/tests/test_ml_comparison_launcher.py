import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('ml_launcher', Path(__file__).parents[1] / 'scripts/start_ml_comparison.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_isolated_mounts_and_content_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            database = source / 'research.sqlite'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE marker(id INTEGER)')
            model = root / ('a' * 64)
            model.mkdir()
            (model / 'manifest.json').write_text('{}')
            args = ['launcher', '--database', str(database), '--output', str(root / 'output'),
                    '--baseline-model', str(model), '--stress-model', str(model), '--model-source-claim', 'test']
            with patch('sys.argv', args), patch.object(launcher.subprocess, 'run',
                    side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)]) as run:
                launcher.main()
            command = run.call_args_list[-1].args[0]
            self.assertEqual(command[command.index('--network') + 1], 'none')
            self.assertIn('ALLOW_LIVE_TRADING=false', command)
            self.assertIn('/models/baseline/' + model.name, command)
            self.assertIn('--baseline-manifest-sha256', command)
            self.assertTrue(any('dst=/source/research.sqlite,readonly' in part for part in command))
            self.assertFalse(any('provider.json' in part for part in command))


if __name__ == '__main__':
    unittest.main()
