"""Exercise real batch control flow against a fake py command in a temp folder."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == 'nt', 'Windows batch launchers')
class DesktopLaunchTests(unittest.TestCase):
    def run_batch(self, name, answer, binding=None):
        with tempfile.TemporaryDirectory(prefix='anaxi-launch-proof-') as tmp:
            work = Path(tmp)
            shutil.copyfile(ROOT / name, work / name)
            (work / 'py.cmd').write_text(
                '@echo off\n@echo %*>>"%~dp0invoked.txt"\nexit /b 0\n', encoding='ascii')
            env = dict(os.environ)
            env['PATH'] = str(work) + os.pathsep + env.get('PATH', '')
            if binding is None:
                env.pop('ANAXI_BOUND_HUMAN_ACTOR_ID', None)
            else:
                env['ANAXI_BOUND_HUMAN_ACTOR_ID'] = binding
            result = subprocess.run(['cmd.exe', '/d', '/c', name], cwd=work,
                                    env=env, input=answer, text=True, capture_output=True, timeout=10)
            marker = work / 'invoked.txt'
            return result, marker.read_text() if marker.exists() else ''

    def test_sleep_decline_never_imports_or_calls_sleep(self):
        result, invocation = self.run_batch('Run Anaxi Sleep Cycle.bat', 'N\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invocation, '')

    def test_sleep_explicit_yes_calls_one_authoritative_route(self):
        result, invocation = self.run_batch('Run Anaxi Sleep Cycle.bat', 'Y\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(invocation.splitlines()), 1)
        self.assertIn('run_sleep_cycle_with_production_defaults()', invocation)
        self.assertNotIn('llama_sleep.py', invocation)

    def test_waking_requires_the_configured_human_binding(self):
        for binding in (None, 'wrong-actor'):
            result, invocation = self.run_batch('Launch Anaxi.bat', '\n', binding)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(invocation, '')
        result, invocation = self.run_batch('Launch Anaxi.bat', '\n', 'human-actor-c8feddc1b4bb')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('llama_launch.py --conversation', invocation)


if __name__ == '__main__':
    unittest.main()
