"""Combined editor sequences and malformed configuration repair regressions."""
import contextlib
import io
import unittest
from unittest.mock import patch
from configbackup_lib.ui.editor_forms import scalar
import test_console_ui as console
import test_global_navigation as nav

class TypedRepairTests(unittest.TestCase):
    def test_wrong_types_use_structured_input_and_retry(self):
        for name,value,replies,expected in [('reporting','bad',['[]','{"enabled":false}'],{'enabled':False}),('sources',42,['{}','[]'],[]),('reporting',[],['{}'],{})]:
            with self.subTest(name=name,value=value),patch('configbackup_lib.ui.configure.ask',side_effect=replies),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(scalar(name,value),expected)

@unittest.skipUnless(console.AVAILABLE,'Console dependencies unavailable')
class CombinedKeyTests(unittest.TestCase):
    drive=console.ConsoleTests.drive
    run_editor=nav.GlobalNavigationTests.run_editor
    browser=nav.GlobalNavigationTests.browser
    exit=nav.GlobalNavigationTests.exit
    def test_no_match_save_then_cancel_never_writes_prior_edits(self):
        for name,initial,title in [('enabled',False,'enabled'),('storage','filesystem','storage (configured: filesystem)'),('timezone','UTC','IANA timezone')]:
            with self.subTest(field=name):
                steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01accepted\x0b\r'),('Current configuration/custom',name+'\r'),(title,'zzzz\x13'),(title,'\x1b'),(title,'\x1b'),('Current configuration/custom','\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','yes\r'),('Session complete','\r')]
                result,ui=self.run_editor({'sources':[],'custom':{'label':'old',name:initial}},steps,lambda p,b:self.assertEqual(list(p.parent.glob('*.bak')),[]))
                self.assertEqual(result['custom']['label'],'old');self.assertNotIn('Saved ',ui.log_text)

    def test_wrong_mapping_validation_link_repairs_and_saves(self):
        for initial in ('bad',[]):
            with self.subTest(initial=initial):
                steps=[('ConfigBackup configuration editor','\x1b[13~'),('Validation issues','open reporting\r'),('reporting (JSON)','\x01[]\x0b\x13'),('reporting (JSON)','\x01{"enabled":false}\x0b\x13')]+self.exit('Current configuration/')
                result,ui=self.run_editor({'sources':[],'reporting':initial},steps)
                self.assertEqual(result['reporting'],{'enabled':False});self.assertIn('Save deferred',ui.log_text)

    def test_no_match_then_valid_selection_saves_only_after_acceptance(self):
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','enabled\r'),('enabled','zzzz\x13'),('enabled','\x1b'),('enabled','true\x13')]+self.exit('Current configuration/custom')
        result,_=self.run_editor({'sources':[],'custom':{'enabled':False}},steps)
        self.assertIs(result['custom']['enabled'],True)
