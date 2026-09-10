"""Safety/lifecycle checks for the opt-in real dry-run acceptance harness."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
import accept_freqtrade as acceptance


class AcceptanceSafetyTests(unittest.TestCase):
    def test_single_mutating_attempt_and_cleanup_on_failure(self):
        calls=[]
        def command(args,**kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args,7 if acceptance.ROUNDTRIP in args else 0,'','')
        with patch.object(acceptance,'command',side_effect=command):
            self.assertEqual(acceptance.main(),7)
        self.assertEqual(sum(acceptance.ROUNDTRIP in args for args in calls),1)
        self.assertEqual(calls[-1][:4],['docker','rm','--force','--volumes'])
        launch=calls[0]
        self.assertNotIn('-p',launch)
        self.assertNotIn('--privileged',launch)
        self.assertEqual(launch.count('--env'),1)
        self.assertIn('PAPER_PROBE_CONFIG',launch)
        self.assertTrue(all('trading_app.db' not in arg for arg in launch))
        self.assertTrue(all('src=' not in arg or arg.endswith('readonly') for arg in launch))

    def test_cleanup_after_subprocess_timeout(self):
        calls=[]
        def command(args,**kwargs):
            calls.append(args)
            if acceptance.ROUNDTRIP in args:
                raise subprocess.TimeoutExpired(args,240)
            return subprocess.CompletedProcess(args,0,'','')
        with patch.object(acceptance,'command',side_effect=command):
            with self.assertRaises(subprocess.TimeoutExpired):
                acceptance.main()
        self.assertEqual(calls[-1][:4],['docker','rm','--force','--volumes'])
