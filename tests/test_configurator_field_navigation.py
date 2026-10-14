"""Configuration field and navigation behavior using real key dispatch."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch
import test_global_navigation as navigation_tests
import test_console_ui as console_tests
from configbackup_lib.ui import config_browser,configure

@unittest.skipUnless(console_tests.AVAILABLE,'Optional console dependencies unavailable')
class FieldNavigationTests(unittest.TestCase):
    drive=navigation_tests.GlobalNavigationTests.drive
    run_editor=navigation_tests.GlobalNavigationTests.run_editor
    browser=navigation_tests.GlobalNavigationTests.browser
    exit=navigation_tests.GlobalNavigationTests.exit

    def test_save_without_moving_false_enum_timezone_keeps_current(self):
        config={'sources':[],'custom':{'enabled':False,'storage':'git','timezone':'America/New_York'}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','enabled\r'),('enabled','\x13'),('Current configuration/custom','storage\r'),('storage (configured: git)','\x13'),('Current configuration/custom','timezone\r'),('IANA timezone','\x13')]+self.exit('Current configuration/custom')
        before=copy.deepcopy(config);result,_=self.run_editor(config,steps);self.assertEqual(before,result)

    def test_abandoning_deferred_save_with_undo_does_not_write(self):
        config={'sources':[]}
        steps=[('ConfigBackup configuration editor','monitoring\r'),('Monitoring & reports','sources\r'),('Schedule sources','add\r'),('Snapshot directory','/capture\x13'),('An item is still being created','\r'),('Host label','\x1b[15~'),('Discard the current unfinished field/item and return to the main menu?','yes\r')]+self.exit('ConfigBackup configuration editor')
        result,_=self.run_editor(config,steps,lambda p,b:self.assertEqual(list(p.parent.glob('*.bak')),[]));self.assertEqual(result,config)

    def test_browser_undo_uses_session_history_and_subsequent_edit_saves(self):
        config={'sources':[],'custom':{'inner':{'label':'old'},'outer':'a'}}
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','inner\r'),('Current configuration/custom/inner','label\r'),('label','\x01new\x0b\r'),('Current configuration/custom/inner','undo last\r')]+self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','outer\r'),('outer','\x01newouter\x0b\x13')]+self.exit('Current configuration/custom')
        result,_=self.run_editor(config,steps);self.assertEqual(result['custom'],{'inner':{'label':'old'},'outer':'newouter'})

    def test_help_keeps_draft_and_palette_jumps_to_setting(self):
        config={'sources':[],'reporting':{'timezone':'UTC'},'custom':{'label':'old'}}
        def inspect(ui):self.assertEqual(ui.text.text,'draft');return '\x13'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','label\r'),('label','\x01draft\x0b\x1bOP'),('label',inspect),('Current configuration/custom','\x1b[19~'),('Find command or setting','setting / reporting / timezone\r'),('Current configuration/reporting','timezone\r'),('IANA timezone','\x13')]+self.exit('Current configuration/reporting')
        result,ui=self.run_editor(config,steps);self.assertEqual(result['custom']['label'],'draft');self.assertIn('Context: label',ui.log_text)

    def test_invalid_custom_timezone_stays_open_and_shows_inline_error(self):
        config={'sources':[],'custom':{'timezone':'UTC'}}
        def inspect(ui):self.assertIn('IANA',ui.field_error);return '\x01Europe/Paris\x0b\x13'
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','timezone\r'),('IANA timezone','custom\r'),('IANA timezone','\x01Bad/Zone\x0b\x13'),('IANA timezone',inspect)]+self.exit('Current configuration/custom')
        result,_=self.run_editor(config,steps);self.assertEqual(result['custom']['timezone'],'Europe/Paris')

    def test_item_first_sources_enable_disable_and_save(self):
        config={'sources':[{'path':'/capture','host':'one','type':'sql','timezone':'UTC','enabled':True}]}
        steps=[('ConfigBackup configuration editor','monitoring\r'),('Monitoring & reports','sources\r'),('Schedule sources','one\r'),('Schedule sources / 1: one (enabled)','enable/disable\r'),('Schedule sources','\x13')]+self.exit('Schedule sources')
        result,_=self.run_editor(config,steps);self.assertFalse(result['sources'][0]['enabled'])

class FieldStateTests(unittest.TestCase):
    def test_malformed_inheritance_does_not_block_field_repair(self):
        from configbackup_lib.ui.navigation import field_context
        for extra in ({'connections':[]},{'collection_profiles':False},{'tasks':[{'enabled':True,'profile':['bad']}]},{'tasks':[{'enabled':True,'connection':{'bad':'value'}}]}):
            config={'tasks':[{'enabled':True}],**extra}
            with self.subTest(config=config):
                before=copy.deepcopy(config)
                self.assertIn('Unresolved',field_context(config,('tasks',0),'enabled',True))
                self.assertNotIn('NEVER-PRINT-THIS',field_context(config,('tasks',0),'password','NEVER-PRINT-THIS'))
                self.assertEqual(config,before)
                choices=iter(['Back'])
                def choose(prompt,values,*args,**kwargs):
                    prefix=next(choices);return next(x for x in values if x.startswith(prefix))
                with patch.object(configure,'choice',side_effect=choose),patch.object(configure,'ask',return_value='no'),contextlib.redirect_stdout(io.StringIO()):
                    config_browser.browse(config,start=('tasks',0,'enabled'),edit_start=True)
                self.assertFalse(config['tasks'][0]['enabled'])
                before['tasks'][0]['enabled']=False;self.assertEqual(config,before)

    def test_opening_missing_sections_and_back_does_not_modify(self):
        config={}
        for key in ('git','backup','defaults','monitoring','retention_policies'):
            with patch.object(configure,'choice',return_value='Back'),contextlib.redirect_stdout(io.StringIO()):config_browser.browse(config,section=key)
        self.assertEqual(config,{})

    def test_plain_nested_undo_restarts_navigation_without_detached_edits(self):
        config={'custom':{'inner':{'label':'old'},'outer':'a'}}
        choices=iter(['custom:','inner:','label:','Undo last edit','custom:','outer:','Back','Back'])
        def choose(prompt,values,*args,**kwargs):
            prefix=next(choices);return next(x for x in values if x.startswith(prefix))
        with patch.object(configure,'choice',side_effect=choose),patch.object(configure,'ask',side_effect=['new','newouter']),contextlib.redirect_stdout(io.StringIO()):config_browser.browse(config)
        self.assertEqual(config['custom'],{'inner':{'label':'old'},'outer':'newouter'})

    def test_enum_phases_match_engine(self):
        from configbackup_lib.ui.editor_forms import ENUMS
        from configbackup_lib.engine.configbackup import PHASES
        self.assertEqual(ENUMS['phase'],PHASES)

    def test_palette_uses_paths_and_commands_never_sensitive_values(self):
        from configbackup_lib.ui.navigation import palette_entries
        entries=palette_entries({'credentials':{'password':'NEVER-PRINT-THIS'}},['Sensitive collection','Recovery status and reset'])
        self.assertNotIn('NEVER-PRINT-THIS',repr(entries));self.assertTrue(any('ssis' in x for x in entries));self.assertTrue(any('journal' in x for x in entries))

    def test_failed_save_blocks_save_and_test(self):
        from types import SimpleNamespace
        ui=SimpleNamespace(console_ui=True,save_configuration=lambda:False,choose=lambda *args:'Save and test')
        with contextlib.redirect_stdout(io.StringIO()):self.assertFalse(configure.test_mode(ui))

    def test_reset_task_override_exposes_inherited_default(self):
        config={'defaults':{'storage':'git'},'tasks':[{'name':'one','storage':'filesystem'}]}
        choices=iter(['tasks:','0:','Reset to inherited/default','storage:','Back','Back','Back'])
        def choose(prompt,values,*args,**kwargs):
            prefix=next(choices);return next(x for x in values if x.startswith(prefix))
        with patch.object(configure,'choice',side_effect=choose),patch.object(configure,'boolean',return_value=True),contextlib.redirect_stdout(io.StringIO()) as out:config_browser.browse(config)
        self.assertNotIn('storage',config['tasks'][0]);self.assertIn('Inherited/default: "git"',out.getvalue())

    def test_connections_view_does_not_insert_empty_section(self):
        config={}
        with patch.object(configure,'choice',return_value='Back'):configure.shared_connections_wizard(config,'unused.yaml',input)
        self.assertEqual(config,{})

    def test_successful_save_and_test_calls_save_once(self):
        from types import SimpleNamespace
        saved=[]
        ui=SimpleNamespace(console_ui=True,save_configuration=lambda:saved.append('saved') or True,choose=lambda *args:'Save and test')
        with contextlib.redirect_stdout(io.StringIO()):self.assertTrue(configure.test_mode(ui))
        self.assertEqual(saved,['saved'])

    def test_read_only_overview_counts_and_boundary_labels(self):
        from configbackup_lib.ui.navigation import changed_count,operation_boundary
        self.assertEqual(changed_count({'tasks':[{'enabled':False}]},{'tasks':[{'enabled':True}]}),1)
        self.assertIn('saved configuration',operation_boundary('Recovery status and reset'))
        self.assertIn('current accepted',operation_boundary('Performance recording and reports'))

@unittest.skipUnless(console_tests.AVAILABLE,'Optional console dependencies unavailable')
class RecorderFieldTests(unittest.TestCase):
    drive=console_tests.ConsoleTests.drive

    def test_recorder_numeric_retry_and_save_after_completing_form(self):
        from configbackup_lib.engine import native_recording as recording
        from configbackup_lib.ui.navigation import Navigate
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            config={'performance':{'recording':{'backend':'sysstat','directory':folder,'interval_seconds':60,'keep_days':7,'max_mb':64}}}
            captured=[];real_settings=recording.settings
            def work(ui):
                def command(action,mode,draft):
                    if action=='Save' and draft:ui.save_after_draft=True
                    elif action=='Save':captured.append(copy.deepcopy(config))
                ui.command_handler=command
                with patch.object(recording,'settings',side_effect=lambda cfg,path:real_settings(cfg,path,system='Linux')),patch.object(recording,'prerequisites',return_value={'guidance':'Fixture only; no recorder activated'}):recording.wizard(config,Path(folder)/'config.yaml',ui)
            def invalid(ui):self.assertIn('integer',ui.field_error);return '\x0160\x0b\x13'
            self.drive(work,[('Performance recording','configure recording\r'),('Private runtime history directory','\r'),('Sample interval seconds (5..3600)','\x01wrong\x0b\x13'),('Sample interval seconds (5..3600)',invalid),('History/export days (1..365)','\r'),('History size budget MiB (1..4096)','\r'),('Performance recording','back\r'),('Session complete','\r')])
            self.assertEqual(len(captured),1);self.assertEqual(captured[0]['performance']['recording']['interval_seconds'],60)
