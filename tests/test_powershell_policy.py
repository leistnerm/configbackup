import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import powershell_policy as policy
from startup_launcher import generate
import configbackup as cb


class PowerShellPolicyTests(unittest.TestCase):
    def test_inherit_preserves_environment_and_does_not_mutate_parent(self):
        source = {'PSExecutionPolicyPreference': 'AllSigned'}
        self.assertEqual(policy.environment({}, source, platform='nt'), source)
        result = policy.environment({'options': {'powershell_execution_policy': 'Bypass'}}, source, platform='nt')
        self.assertEqual(result['PSExecutionPolicyPreference'], 'Bypass')
        self.assertEqual(source['PSExecutionPolicyPreference'], 'AllSigned')

    def test_non_windows_unchanged_and_invalid_values_rejected(self):
        cfg = {'options': {'powershell_execution_policy': 'Bypass'}}
        self.assertEqual(policy.environment(cfg, {}, platform='posix'), {})
        for value in ('Bypass & whoami', True, None, 'bogus'):
            with self.assertRaises(ValueError):policy.validate(value)
            with self.assertRaises(cb.ConfigError):
                cb.ConfigLoader(Path('unused'))._resolve({'tasks': [], 'options': {'powershell_execution_policy': value}})

    def test_launcher_policy_is_before_file_and_inherit_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for value in ('inherit', 'RemoteSigned', 'Bypass'):
                out = root / value
                generate(root/'config.yaml', out, 'windows', 'python', execution_policy=value)
                text = (out/'run-configbackup.bat').read_text()
                if value == 'inherit':self.assertNotIn('-ExecutionPolicy', text)
                else:
                    self.assertIn('-ExecutionPolicy '+value, text)
                    self.assertLess(text.index('-ExecutionPolicy'), text.index('-File'))
                self.assertNotIn('Set-ExecutionPolicy', text)

    def test_menu_edit_and_cancel(self):
        config = {}
        with patch('configure.choice', return_value='Bypass'):policy.wizard(config)
        self.assertEqual(config['options']['powershell_execution_policy'], 'Bypass')
        with patch('configure.choice', return_value=None):policy.wizard(config)
        self.assertEqual(config['options']['powershell_execution_policy'], 'Bypass')

    def test_task_child_receives_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cb.ConfigLoader(Path('unused'))._resolve({'backup': {'root': tmp, 'include_configuration': False},
                 'options': {'powershell_execution_policy': 'Bypass'}, 'tasks': []})
            engine = cb.BackupEngine(cfg)
            original = policy.environment
            try:
                with patch('powershell_policy.environment', side_effect=lambda config, env: original(config, env, platform='nt')), patch('configbackup.subprocess.run') as run:
                    engine._run_subprocess({'name': 'test', 'executable': 'powershell.exe', 'arguments': ['-File', 'test.ps1']}, True)
                self.assertEqual(run.call_args.kwargs['env']['PSExecutionPolicyPreference'], 'Bypass')
            finally:engine._close_logging()
