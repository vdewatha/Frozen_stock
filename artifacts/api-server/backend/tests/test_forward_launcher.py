from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.start_forward_experiment import launch, NAME


class ForwardLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'experiment'
        self.root.mkdir()
        (self.root / 'evidence').mkdir(mode=0o700)
        (self.root / 'experiment.json').write_text('{}')
        state = base / 'state'
        state.mkdir()
        self.source = state / 'research.sqlite'
        with sqlite3.connect(self.source) as db:
            db.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY)')
        self.contract = {'experiment_id': 'frozen-test', 'runtime_image_id': 'sha256:' + 'a' * 64, 'policy': {
            'start_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, 'arms': {}}
        for name in ('champion_baseline', 'challenger_baseline', 'champion_stress', 'challenger_stress'):
            model = base / ('model-' + name)
            model.mkdir()
            self.contract['arms'][name] = {'model_directory': str(model), 'model_run_id': 'run-' + name}
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 1))
        self.loader = patch('scripts.start_forward_experiment.load_contract', return_value=self.contract)
        self.load = self.loader.start()

    def tearDown(self):
        self.loader.stop()
        self.temp.cleanup()

    def test_isolated_read_only_launch_with_only_evidence_writable(self):
        result = launch(self.root, self.source, runner=self.runner)
        self.assertEqual(result['status'], 'started')
        self.assertFalse(result['live_authorized'])
        self.load.assert_called_once_with(self.root.resolve(), check_code=True)
        self.assertEqual(self.runner.call_args_list[0].args[0], ['docker', 'inspect', NAME])
        call = self.runner.call_args_list[1]
        command = call.args[0]
        self.assertTrue(call.kwargs['check'])
        self.assertEqual(command[command.index('--network') + 1], 'none')
        self.assertEqual(command[command.index('--restart') + 1], 'unless-stopped')
        self.assertIn('--read-only', command)
        self.assertIn('ALLOW_LIVE_TRADING=false', command)
        self.assertIn(self.contract['runtime_image_id'], command)
        mounts = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
        self.assertEqual(len(mounts), 7)
        writable = [mount for mount in mounts if not mount.endswith(',readonly')]
        self.assertEqual(writable, [f'type=bind,src={self.root.resolve() / "evidence"},dst=/experiment/evidence'])
        self.assertIn(f'type=bind,src={self.source.resolve()},dst=/source/research.sqlite,readonly', mounts)
        self.assertIn(f'type=bind,src={self.root.resolve() / "experiment.json"},dst=/experiment/experiment.json,readonly', mounts)
        for name, arm in self.contract['arms'].items():
            self.assertIn(f'type=bind,src={arm["model_directory"]},dst=/models/{name}/{arm["model_run_id"]},readonly', mounts)
        for forbidden in ('provider.json', 'docker.sock', '--env-file', 'API_KEY', 'API_SECRET', '--privileged'):
            self.assertNotIn(forbidden, ' '.join(command))

    def test_duplicate_container_fails_without_run(self):
        self.runner.return_value = subprocess.CompletedProcess([], 0)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            launch(self.root, self.source, runner=self.runner)
        self.assertEqual(self.runner.call_count, 1)
        self.assertEqual(self.runner.call_args.args[0], ['docker', 'inspect', NAME])

    def test_late_start_fails_before_docker(self):
        self.contract['policy']['start_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, 'before frozen start'):
            launch(self.root, self.source, runner=self.runner)
        self.runner.assert_not_called()

    def test_nonempty_evidence_fails_before_docker(self):
        (self.root / 'evidence' / 'existing.sqlite').touch()
        with self.assertRaisesRegex(ValueError, 'fresh private'):
            launch(self.root, self.source, runner=self.runner)
        self.runner.assert_not_called()

    def test_wal_source_fails_before_docker(self):
        with sqlite3.connect(self.source) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        with self.assertRaisesRegex(ValueError, 'DELETE journal mode'):
            launch(self.root, self.source, runner=self.runner)
        self.runner.assert_not_called()
