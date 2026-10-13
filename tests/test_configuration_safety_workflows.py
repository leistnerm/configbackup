import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import configbackup_lib.engine.configbackup as cb
from configbackup_lib.ui import configure
from configbackup_lib.ui.config_browser import browse
from configbackup_lib.recovery.transaction_journal import Journal,for_engine
from configbackup_lib.recovery import journal_recovery
from configbackup_lib.engine.startup_launcher import generate
from configbackup_lib.engine.launcher_manager import inventory,regenerate
from configbackup_lib.configuration.configuration_links import rename,references
from configbackup_lib.configuration.configuration_repair import load
from configbackup_lib.ui.editor_forms import EditHistory
from configbackup_lib.engine.windows_readiness import inspect as windows_inspect


class ConfigurationSafetyTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
    def tearDown(self):self.temp.cleanup()

    def test_cancel_boolean_preserves_value(self):
        class UI:
            console_ui=True
            n=0
            def choose(self,prompt,values):
                self.n+=1
                return values[0] if self.n==1 else None
        config={'enabled':True};browse(config,UI());self.assertTrue(config['enabled'])

    def test_hidden_mapping_never_becomes_prompt_default(self):
        prompts=[];answers=iter(['1','environment','4'])
        def inp(prompt):prompts.append(prompt);return next(answers)
        configure.edit_mapping({'environment':{'API_TOKEN':'dummy-hidden-token'}},inp)
        self.assertFalse(any('dummy-hidden-token' in p for p in prompts))

    def test_symlink_journal_is_refused_without_moving_target(self):
        target=self.root/'target';target.mkdir();(target/'keep').write_text('x')
        link=self.root/'transaction'
        try:link.symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('Symlink creation unavailable')
        with self.assertRaises(ValueError):Journal(link,[self.root])
        self.assertEqual((target/'keep').read_text(),'x')

    def test_committed_missing_checkout_has_same_inspection_and_recovery(self):
        j=Journal(self.root/'journal',[self.root],self.root/'absent');j.directory.mkdir()
        j.manifest.write_text(json.dumps({'schema':2,'status':'committed','repo':j.repo,'head':'x','entry_count':0}))
        self.assertTrue(journal_recovery.inspect(j)['can_reconcile'])
        self.assertEqual(j.recover()['status'],'cleaned_committed')

    def test_prune_only_needs_no_git_checkout(self):
        source=self.root/'source';source.write_text('x')
        config={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'git':{'repository':str(self.root/'absent-repo'),'mode':'pull_request','push':True,'worktree_root':str(self.root/'trees')},'tasks':[{'name':'file','type':'file','source':str(source),'destination':'data','storage':'both'}]}
        engine=cb.BackupEngine(cb.ConfigLoader(self.root/'config.yaml')._resolve(config),prune_only=True)
        try:
            with patch.object(engine,'_prepare_git_repo',side_effect=AssertionError('Git preparation forbidden')):self.assertEqual(engine.run(),0)
        finally:engine._close_logging()

    def test_archive_only_pending_recovers_in_normal_run_context(self):
        source=self.root/'source';source.write_text('x')
        config={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'tasks':[]}
        e=cb.BackupEngine(cb.ConfigLoader(self.root/'config.yaml')._resolve(config),prune_only=True)
        try:
            e.internal_root.mkdir(parents=True,exist_ok=True)
            target=e.root/'file';target.write_text('old');j=for_engine(e,new=True);j.begin();j.remember(target);target.write_text('new')
            e.prune_only=False;for_engine(e).recover();self.assertEqual(target.read_text(),'old')
        finally:e._close_logging()

    def test_unicode_windows_launcher_has_bom_and_policy_metadata(self):
        config=self.root/'配置.yaml';config.write_text('tasks: []')
        out=self.root/'launch';generate(config,out,'windows','python',execution_policy='Bypass')
        self.assertTrue((out/'run-configbackup.ps1').read_bytes().startswith(b'\xef\xbb\xbf'))
        self.assertIn(str(config),(out/'run-configbackup.ps1').read_text(encoding='utf-8-sig'))
        row=inventory(config,{'options':{'powershell_execution_policy':'Bypass'}})[0]
        self.assertEqual(row['status'],'current');self.assertEqual(row['metadata']['execution_policy'],'Bypass')

    def test_failed_launcher_write_leaves_no_partial_file(self):
        config=self.root/'config.yaml';config.write_text('tasks: []')
        real=os.fdopen
        class Bad:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def write(self,value):self.stream.write(value[:10]);raise OSError('disk full')
            def __exit__(self,*args):self.stream.close()
        with patch('configbackup_lib.engine.startup_launcher.os.fdopen',side_effect=lambda *a,**kw:Bad(real(*a,**kw))):
            with self.assertRaises(OSError):generate(config,self.root/'launch','windows','python')
        self.assertEqual(list((self.root/'launch').iterdir()),[])

    def test_launcher_regeneration_retains_previous_files(self):
        config=self.root/'config.yaml';config.write_text('tasks: []')
        out=self.root/'launch';generate(config,out,'windows','python')
        original=(out/'run-configbackup.bat').read_bytes();new={'tasks':[],'options':{'powershell_execution_policy':'Bypass'}}
        row=inventory(config,new)[0];self.assertEqual(row['status'],'review')
        config.write_text('tasks: []\noptions:\n  powershell_execution_policy: Bypass\n')
        backup=Path(regenerate(config,new,row['metadata']))
        self.assertEqual((backup/'run-configbackup.bat').read_bytes(),original)
        self.assertIn('-ExecutionPolicy Bypass',(out/'run-configbackup.bat').read_text())

    def test_launcher_registration_failure_rolls_back_files(self):
        config=self.root/'config.yaml';config.write_text('tasks: []')
        out=self.root/'launch';files=generate(config,out,'windows','python');before={p.name:p.read_bytes() for p in files}
        config.write_text('tasks: []\noptions:\n  powershell_execution_policy: Bypass\n')
        with patch('configbackup_lib.engine.launcher_manager.register_launcher',side_effect=OSError('registry unavailable')):
            with self.assertRaises(OSError):regenerate(config,{'tasks':[],'options':{'powershell_execution_policy':'Bypass'}},inventory(config,{})[0]['metadata'])
        self.assertEqual({p.name:p.read_bytes() for p in files},before)

    def test_rename_updates_references_and_state_identity(self):
        cfg={'connections':{'old':{}},'tasks':[{'name':'collect','connection':'old'},{'name':'archive','depends_on':['collect']}]}
        rename(cfg,'connection','old','new');self.assertEqual(cfg['tasks'][0]['connection'],'new')
        rename(cfg,'task','collect','collect-new');self.assertEqual(cfg['tasks'][0]['state_id'],'collect');self.assertEqual(cfg['tasks'][1]['depends_on'],['collect-new'])
        before=copy.deepcopy(cfg)
        with self.assertRaises(ValueError):rename(cfg,'task','collect-new','archive')
        self.assertEqual(cfg,before)

    def test_undo_redo_and_yaml_error_hide_source(self):
        h=EditHistory();c={'a':1};before=copy.deepcopy(c);c['a']=2;h.record(before,c)
        h.undo(c);self.assertEqual(c,{'a':1});h.redo(c);self.assertEqual(c,{'a':2})
        path=self.root/'bad.yaml';path.write_text('password: [DO_NOT_SHOW')
        with self.assertRaises(ValueError) as error:load(path)
        self.assertIn('line',str(error.exception));self.assertNotIn('DO_NOT_SHOW',str(error.exception))

    def test_windows_readiness_is_read_only_and_masks_probe_errors(self):
        calls=[]
        def run(command,**kwargs):
            calls.append((command,kwargs));return subprocess.CompletedProcess(command,0,json.dumps({'effectivePolicy':'RemoteSigned','policies':[]}), '')
        result=windows_inspect({'options':{'powershell_execution_policy':'RemoteSigned'}},platform='nt',execute=run)
        self.assertEqual(result['status'],'complete')
        self.assertTrue(all('Set-ExecutionPolicy' not in ' '.join(c[0]) for c in calls))
        self.assertTrue(all(c[1]['env']['PSExecutionPolicyPreference']=='RemoteSigned' for c in calls))
