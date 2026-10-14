"""State identity, guided fields, validation navigation and keyboard parity."""
import copy
import contextlib
import io
import unittest
from unittest.mock import patch
from configbackup_lib.ui import configure
from configbackup_lib.ui.editor_forms import EditCancelled
from configbackup_lib.ui.navigation import field_context,validation_targets
from configbackup_lib.configuration.config_editor import duplicate_group
from configbackup_lib.configuration.configuration_links import rename
import test_global_navigation as nav
import test_console_ui as console

class ConfigurationValueTests(unittest.TestCase):
    def test_duplicate_renamed_task_and_consumers_have_independent_identities(self):
        config={'tasks':[{'name':'collect','type':'execute','output_directory':'/capture'}, {'name':'archive','type':'directory','depends_on':['collect'],'source':'/capture','destination':'old','state_id':'archive-original'}]}
        rename(config,'task','collect','renamed')
        clones=duplicate_group(config['tasks'],0,'copy')
        all_tasks=config['tasks']+clones
        ids=[t.get('state_id',t['name']) for t in all_tasks]
        self.assertEqual(len(ids),len(set(ids)));self.assertNotIn('state_id',clones[1])
        self.assertEqual(clones[1]['depends_on'],['copy'])
        self.assertEqual(config['tasks'][0]['state_id'],'collect')
        self.assertNotIn('state_id',duplicate_group(config['tasks'],1,'copy-directory')[0])

    def test_reserved_connection_names_open_as_items(self):
        for name in ('Back','Add connection'):
            config={'connections':{name:{'engine':'postgresql'}}}
            with patch.object(configure,'choice',side_effect=['1: '+name,'Edit connection','Back']) as choose,patch.object(configure,'edit_mapping') as edit:
                configure.shared_connections_wizard(config,'unused')
            edit.assert_called_once();self.assertEqual(choose.call_args_list[1].args[0],'Connection / '+name)

    def test_cancel_reporting_does_not_insert_section(self):
        from configbackup_lib.ui.configure_features import reporting_wizard
        config={}
        with patch.object(configure,'boolean',side_effect=EditCancelled()):
            with self.assertRaises(EditCancelled):reporting_wizard(config)
        self.assertEqual(config,{})

    def test_context_resolves_nested_profile_defaults_and_connection(self):
        cfg={'defaults':{'collection':{'include_health':False}},'connections':{'db':{'engine':'postgresql','schema':True}},'collection_profiles':{'p':{'engine':'postgresql','collection':{'include_health':True}}},'tasks':[{'name':'one','type':'execute','connection':'db','profile':'p','collection':{'include_health':False,'schema':False}}]}
        original=copy.deepcopy(cfg)
        text=field_context(cfg,('tasks',0,'collection'),'include_health',False)
        self.assertIn('Origin: Task override',text);self.assertIn('Inherited/default: true',text);self.assertIn('After reset origin: Profile: p',text)
        text=field_context(cfg,('tasks',0,'collection'),'schema',False)
        self.assertIn('Inherited/default: true',text);self.assertIn('Shared connection: db',text)
        del cfg['collection_profiles']['p']['collection']['include_health']
        text=field_context(cfg,('tasks',0,'collection'),'include_health',False)
        self.assertIn('After reset origin: Task defaults',text)
        self.assertEqual(original['tasks'],cfg['tasks'])
        cfg['tasks'][0]['enabled']=True;cfg['connections']['db']['enabled']=False
        text=field_context(cfg,('tasks',0),'enabled',True)
        self.assertIn('Current: true',text);self.assertIn('Effective: false',text)
        self.assertIn('Shared connection: db (disabled)',text)

    def test_list_item_context_does_not_attempt_mapping_reset(self):
        config={'tasks':[{'name':'one','arguments':['--output','/capture']}]}
        text=field_context(config,('tasks',0,'arguments'),'0','--output')
        self.assertIn('Explicit list item',text)
        self.assertEqual(config['tasks'][0]['arguments'],['--output','/capture'])

    def test_validation_targets_paths_without_secret_values(self):
        cfg={'reporting':{'timezone':'Bad/Zone'},'tasks':[{'enabled':'secret-value'}]}
        self.assertEqual(validation_targets(cfg,ValueError('tasks[0].enabled must be true or false')),{'Open tasks[0].enabled':('validation',('tasks',0,'enabled'))})
        self.assertEqual(validation_targets(cfg,ValueError('reporting.timezone must be an available IANA time zone name')),{'Open reporting.timezone':('validation',('reporting','timezone'))})
        self.assertNotIn('secret-value',str(validation_targets(cfg,ValueError('something failed'))))

@unittest.skipUnless(console.AVAILABLE,'Console dependencies unavailable')
class KeyboardMatrix(unittest.TestCase):
    drive=console.ConsoleTests.drive
    run_editor=nav.GlobalNavigationTests.run_editor
    browser=nav.GlobalNavigationTests.browser
    exit=nav.GlobalNavigationTests.exit
    cases=[('label','old','label','\x01new\x0b','new'),('enabled',False,'enabled','\x1b[A',True),('storage','filesystem','storage (configured: filesystem)','\x1b[B','git'),('timezone','UTC','IANA timezone','\x1b[B','America/New_York'),('count',3,'count','\x0142\x0b',42),('fraction',1.0,'fraction','\x012.5\x0b',2.5),('object',None,'object (JSON)','\x01{"x":1}\x0b',{'x':1}),('array',None,'array (JSON)','\x01[1,2]\x0b',[1,2])]
    def steps(self,name):return self.browser()+[('Current configuration','custom\r'),('Current configuration/custom',name+'\r')]

    def test_save_and_cancel_across_scalar_and_json_fields(self):
        for name,initial,title,keys,expected in self.cases:
            with self.subTest(field=name,action='save'):
                result,_=self.run_editor({'sources':[],'custom':{name:initial}},self.steps(name)+[(title,keys+'\x13')]+self.exit('Current configuration/custom'))
                self.assertEqual(result['custom'][name],expected)
            with self.subTest(field=name,action='cancel'):
                # Text escape unwinds the form; selector escape returns unchanged.
                destination='Current configuration/custom' if name in ('enabled','storage','timezone') else 'Advanced configuration'
                result,_=self.run_editor({'sources':[],'custom':{name:initial}},self.steps(name)+[(title,keys+'\x1b')]+self.exit(destination))
                self.assertEqual(result['custom'][name],initial)

    def test_exit_decline_preserves_every_unfinished_field(self):
        for name,initial,title,keys,expected in self.cases:
            with self.subTest(field=name):
                steps=self.steps(name)+[(title,keys+'\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','\r'),(title,'\x13')]+self.exit('Current configuration/custom')
                result,_=self.run_editor({'sources':[],'custom':{name:initial}},steps)
                self.assertEqual(result['custom'][name],expected)

    def test_undo_decline_preserves_every_unfinished_field(self):
        for name,initial,title,keys,expected in self.cases:
            with self.subTest(field=name):
                steps=self.steps(name)+[(title,keys+'\x1b[15~'),('Discard the current unfinished field/item and return to the main menu?','\r'),(title,'\x13')]+self.exit('Current configuration/custom')
                result,_=self.run_editor({'sources':[],'custom':{name:initial}},steps)
                self.assertEqual(result['custom'][name],expected)

    def test_search_decline_preserves_every_unfinished_field(self):
        for name,initial,title,keys,expected in self.cases:
            with self.subTest(field=name):
                steps=self.steps(name)+[(title,keys+'\x1b[19~'),('Find command or setting','setting / custom\r'),('Leave unfinished input to open the selected location?','\r'),(title,'\x13')]+self.exit('Current configuration/custom')
                result,_=self.run_editor({'sources':[],'custom':{name:initial}},steps)
                self.assertEqual(result['custom'][name],expected)

    def test_invalid_timezone_stays_in_guided_reporting_field(self):
        from configbackup_lib.ui.configure_features import reporting_wizard
        cfg={}
        def check(ui):
            self.assertTrue(ui.field_error);self.assertNotIn('timezone',cfg['reporting']);return '\x01UTC\x0b\r'
        self.drive(lambda ui:reporting_wizard(cfg,ui),[('Generate the offline HTML review portal','\r'),('Report directory','\r'),('Display time zone (IANA)','\x01Bad/Zone\x0b\r'),('Display time zone (IANA)',check),('Include sanitized configuration values in the local report','\x03\x03'),('Session complete','\r')])
        self.assertEqual(cfg['reporting']['timezone'],'UTC')

    def test_validation_issue_jumps_to_setting(self):
        cfg={'sources':[],'custom':{'enabled':'invalid'}}
        steps=[('ConfigBackup configuration editor','\x1b[13~'),('Validation issues','open custom.enabled\r'),('enabled','false\x13')]+self.exit('Current configuration/custom')
        result,_=self.run_editor(cfg,steps);self.assertIs(result['custom']['enabled'],False)

    def test_validate_help_diff_and_redo_decline_preserve_all_field_types(self):
        for name,initial,title,keys,expected in self.cases:
            with self.subTest(field=name):
                steps=self.steps(name)+[(title,keys+'\x1b[13~'),(title,'\x1bOP'),(title,'\x1b[14~'),(title,'\x1b[17~'),('Discard the current unfinished field/item and return to the main menu?','\r'),(title,'\x13')]+self.exit('Current configuration/custom')
                result,_=self.run_editor({'sources':[],'custom':{name:initial}},steps)
                self.assertEqual(result['custom'][name],expected)

    def test_dependencies_refresh_after_return_even_via_shortcut(self):
        missing=[{'name':'tzdata','status':'missing','detail':'fixture'}]
        ready=[{'name':'tzdata','status':'ready','detail':'fixture'}]
        steps=[('ConfigBackup configuration editor','setup\r'),('Setup & launchers','python dependencies\r'),('Python dependencies','\x1b[19~'),('Find command or setting','overview / configuration summary\r')]+self.exit('ConfigBackup configuration editor')
        with patch('configbackup_lib.ui.config_dependencies.inspect',side_effect=[missing,ready,ready]):
            _,ui=self.run_editor({'sources':[]},steps)
        self.assertIn('Dependency attention: none detected',ui.log_text)

    def test_checklist_shortcuts_and_cancellation_use_session_protection(self):
        from configbackup_lib.ui.configure_features import select_many
        from configbackup_lib.ui import config_browser
        # The actual browser imports this shared field adapter dynamically.
        from configbackup_lib.ui import editor_forms
        original=editor_forms.scalar
        def field(name,value,input_fn,**kwargs):
            if name=='pages':return select_many('Pages',['A','B'],['A'],input_fn)
            return original(name,value,input_fn,**kwargs)
        with patch.object(editor_forms,'scalar',side_effect=field):
            steps=self.steps('pages')+[('Pages','\x1b[B \x1b[13~'),('Pages','\x1b[14~'),('Pages','\x1bOP'),('Pages','\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','\r'),('Pages','\x1b[15~'),('Discard the current unfinished field/item and return to the main menu?','\r'),('Pages','\x1b[17~'),('Discard the current unfinished field/item and return to the main menu?','\r'),('Pages','\x1b[19~'),('Find command or setting','setting / custom\r'),('Leave unfinished input to open the selected location?','\r'),('Pages','\x13')]+self.exit('Current configuration/custom')
            result,_=self.run_editor({'sources':[],'custom':{'pages':None}},steps)
            self.assertEqual(result['custom']['pages'],['A','B'])
            steps=self.steps('pages')+[('Pages','\x1b[B \x1b'),('Current configuration/custom','\x13')]+self.exit('Current configuration/custom')
            result,_=self.run_editor({'sources':[],'custom':{'pages':None}},steps)
            self.assertEqual(result['custom']['pages'],['A'])

    def test_accepted_edit_survives_cancelling_next_field(self):
        steps=self.browser()+[('Current configuration','custom\r'),('Current configuration/custom','first\r'),('first','\x01accepted\x0b\r'),('Current configuration/custom','second\r'),('second','\x01discard\x0b\x1b'),('Advanced configuration','\x13')]+self.exit('Advanced configuration')
        result,_=self.run_editor({'sources':[],'custom':{'first':'old','second':'keep'}},steps)
        self.assertEqual(result['custom'],{'first':'accepted','second':'keep'})

    def test_escape_command_confirmation_restores_unfinished_text(self):
        steps=self.steps('label')+[('label','\x01unfinished\x0b\x1b[21~'),('Discard unsaved configuration changes and unfinished input?','\x1b'),('label','\x13')]+self.exit('Current configuration/custom')
        result,_=self.run_editor({'sources':[],'custom':{'label':'old'}},steps)
        self.assertEqual(result['custom']['label'],'unfinished')
