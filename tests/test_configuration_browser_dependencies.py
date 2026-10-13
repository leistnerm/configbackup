"""Existing config editing and actionable, explicitly approved dependency repairs."""
import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError
from configbackup_lib.ui import config_browser
import configbackup_lib.ui.config_dependencies as deps
from configbackup_lib.reporting import reporting

class BrowserDependencyTests(unittest.TestCase):
    def test_browser_edits_existing_nested_item_and_preserves_unknown_fields(self):
        config={'tasks':[{'name':'existing','enabled':True,'source':'old','extension_setting':{'keep':7}}],'reporting':{'timezone':'UTC'}}
        choices=iter(['tasks: (1 items)','0: existing (4 settings)','source: old','Back','Back','reporting: (1 settings)','timezone: UTC','America/New_York','Back','Back'])
        def choose(prompt,values,*args):
            answer=next(choices)
            # Counts include an optional leading space for unnamed containers.
            match=next((v for v in values if v.strip()==answer),None)
            self.assertIsNotNone(match,(answer,values));return match
        with patch('configbackup_lib.ui.configure.choice',side_effect=choose),patch('configbackup_lib.ui.configure.ask',side_effect=['new','America/New_York']),contextlib.redirect_stdout(io.StringIO()):config_browser.browse(config)
        self.assertEqual(config['tasks'][0]['source'],'new');self.assertEqual(config['tasks'][0]['extension_setting'],{'keep':7})
        self.assertEqual(config['reporting']['timezone'],'America/New_York')

    def test_sensitive_container_and_argument_values_are_never_displayed(self):
        config={'secret_settings':{'name':'PRIVATE-SENTINEL'},'arguments':['--password','PRIVATE-SENTINEL']}
        before=copy.deepcopy(config);output=io.StringIO()
        def choose(prompt,values,*args):
            self.assertNotIn('PRIVATE-SENTINEL',str(values))
            if prompt=='Current configuration':return values[0] if not hasattr(choose,'seen') else 'Back'
            choose.seen=True;return 'Back'
        # Sensitive container returns without a nested menu.
        calls=iter([0,-1])
        with patch('configbackup_lib.ui.configure.choice',side_effect=lambda p,v,*a:v[next(calls)]),contextlib.redirect_stdout(output):config_browser.browse(config)
        self.assertNotIn('PRIVATE-SENTINEL',output.getvalue());self.assertEqual(before,config)

    def test_timezone_database_failure_has_repair_instruction(self):
        with patch('configbackup_lib.reporting.reporting.ZoneInfo',side_effect=ZoneInfoNotFoundError('fixture')):
            with self.assertRaisesRegex(ValueError,'pip install tzdata'):reporting.validate({})
        with self.assertRaisesRegex(ValueError,'IANA time zone name'):reporting.validate({'timezone':'Invalid/Zone/Name'})

    def test_dependency_check_identifies_missing_timezone_data(self):
        with patch.object(deps,'ZoneInfo',side_effect=ZoneInfoNotFoundError('fixture')):
            row=next(r for r in deps.inspect() if r['name']=='tzdata')
        self.assertEqual(row['status'],'timezone data unavailable')
        self.assertIn('this Python environment',row['detail'])

    def test_install_is_explicit_uses_current_python_and_supported_constraints(self):
        output=io.StringIO()
        with patch.object(deps.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run,contextlib.redirect_stdout(output):
            self.assertFalse(deps.install(['tzdata'],input_fn=lambda p:'no'));run.assert_not_called()
            self.assertTrue(deps.install(['tzdata','prompt_toolkit'],update=True,input_fn=lambda p:'INSTALL'))
        args=run.call_args.args[0]
        self.assertEqual(args[:4],[deps.sys.executable,'-m','pip','install']);self.assertIn('--upgrade',args)
        self.assertIn('prompt_toolkit>=3.0.52,<4',args);self.assertIn('Restart',output.getvalue())
        with self.assertRaises(ValueError):deps.command(['arbitrary-package'])
