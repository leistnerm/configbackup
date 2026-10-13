import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
import configbackup as cb
from transaction_journal import Journal,sha
from completeness import publish,section

class TransactionJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.archive=self.root/'archive';self.archive.mkdir();self.directory=self.archive/'_configbackup/transaction'
    def tearDown(self):self.tmp.cleanup()
    def journal(self,repo=None):return Journal(self.directory,[self.archive,repo],repo)
    def test_restore_changed_deleted_new_and_state(self):
        old=self.archive/'old';old.write_text('original');deleted=self.archive/'deleted';deleted.write_text('retained');new=self.archive/'new'
        journal=self.journal();journal.begin()
        for p in (old,deleted,new):journal.remember(p)
        old.write_text('changed');deleted.unlink();new.write_text('new')
        self.assertEqual(self.journal().recover()['status'],'rolled_back');self.assertEqual(old.read_text(),'original');self.assertEqual(deleted.read_text(),'retained');self.assertFalse(new.exists())
        self.assertEqual(self.journal().recover()['status'],'clean')
    def test_corrupt_backup_validates_all_before_mutation(self):
        a=self.archive/'a';b=self.archive/'b';a.write_text('a');b.write_text('b');j=self.journal();j.begin();j.remember(a);j.remember(b);a.write_text('new a');b.write_text('new b')
        (self.directory/'before-00000000').write_text('corrupt')
        with self.assertRaises(ValueError):self.journal().recover()
        self.assertEqual(a.read_text(),'new a');self.assertEqual(b.read_text(),'new b')
    def test_symlink_and_path_escape_refused(self):
        outside=self.root/'outside';outside.write_text('outside');link=self.archive/'link';link.symlink_to(outside);j=self.journal();j.begin()
        with self.assertRaises(ValueError):j.remember(link)
        with self.assertRaises(ValueError):j.remember(outside)
        self.assertEqual(outside.read_text(),'outside')
    def test_disk_full_before_journal_publish_does_not_authorize_mutation(self):
        target=self.archive/'x';target.write_text('old');j=self.journal();j.begin()
        with patch('transaction_journal.atomic',side_effect=OSError(28,'No space')):
            with self.assertRaises(OSError):j.remember(target)
        self.assertEqual(target.read_text(),'old');self.assertEqual(self.journal().recover()['status'],'rolled_back')
    def test_committed_marker_keeps_final_files(self):
        target=self.archive/'x';target.write_text('old');j=self.journal();j.begin();j.remember(target);target.write_text('new')
        j.data['status']='committed';j.save();self.assertEqual(self.journal().recover()['status'],'cleaned_committed');self.assertEqual(target.read_text(),'new')
    @unittest.skipUnless(shutil.which('git'),'Git required')
    def test_advanced_git_head_never_rolled_back(self):
        repo=self.root/'git';repo.mkdir()
        def git(*args):return subprocess.run(['git','-C',str(repo),*args],capture_output=True,check=True)
        git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture');(repo/'file').write_text('one');git('add','.');git('commit','-m','one')
        j=self.journal(repo);j.begin();j.remember(repo/'file');(repo/'file').write_text('two');git('add','.');git('commit','-m','two');before=git('rev-parse','HEAD').stdout
        with self.assertRaises(ValueError):self.journal(repo).recover()
        self.assertEqual(git('rev-parse','HEAD').stdout,before);self.assertEqual((repo/'file').read_text(),'two')
    @unittest.skipUnless(shutil.which('git'),'Git required')
    def test_interrupted_staging_restores_index_and_worktree(self):
        repo=self.root/'git';repo.mkdir()
        def git(*args):return subprocess.run(['git','-C',str(repo),*args],capture_output=True,check=True)
        git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture');(repo/'file').write_text('one');git('add','.');git('commit','-m','one')
        j=self.journal(repo);j.begin();j.remember(repo/'file');(repo/'file').write_text('two');git('add','.')
        self.journal(repo).recover();self.assertEqual(git('status','--porcelain').stdout,b'');self.assertEqual((repo/'file').read_text(),'one')
    def test_real_process_exit_reconciles_before_next_run(self):
        source=self.root/'source';source.mkdir();(source/'a').write_text('before');(source/'b').write_text('before')
        config=self.root/'config.yaml';cfg={'backup':{'root':str(self.archive),'include_configuration':False},'tasks':[{'name':'files','type':'directory','source':str(source),'destination':'files'}]};config.write_text(yaml.safe_dump(cfg))
        engine=cb.BackupEngine(cb.ConfigLoader(config).load());self.assertEqual(engine.run(),0)
        state=engine.state.path.read_bytes();paths={p.name:p.read_bytes() for p in (self.archive/'files').iterdir()};(source/'a').write_text('after')
        script="""import os,sys
from pathlib import Path
import configbackup as cb
original=cb.BackupEngine._store_one_file
def crash(self,*a,**kw):
 result=original(self,*a,**kw)
 os._exit(97)
cb.BackupEngine._store_one_file=crash
cb.main(['--config',sys.argv[1]])
"""
        cp=subprocess.run([sys.executable,'-c',script,str(config)],cwd=Path(cb.__file__).parent,capture_output=True)
        self.assertEqual(cp.returncode,97);self.assertTrue((self.directory/'pending.json').exists())
        # Reconciliation itself restores exact pre-run bytes; next collection can then advance.
        self.journal().recover();self.assertEqual(engine.state.path.read_bytes(),state);self.assertEqual({p.name:p.read_bytes() for p in (self.archive/'files').iterdir()},paths)
        engine=cb.BackupEngine(cb.ConfigLoader(config).load());self.assertEqual(engine.run(),0);self.assertEqual(len(engine.state.task('files')['files']['files/a']['versions']),2)
    def test_scope_size_failure_preserves_one_database_and_updates_sibling(self):
        source=self.root/'source';source.mkdir()
        for name in ('one','two'):(source/name).mkdir();(source/name/'file').write_text('old')
        def certify():publish(source,[section(source,'one'),section(source,'two')])
        certify();cfg={'backup':{'root':str(self.archive),'include_configuration':False},'tasks':[{'name':'db','type':'directory','source':str(source),'destination':'db','collection_manifest':True,'max_scope_bytes':10}]}
        def run():
            engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(copy.deepcopy(cfg)));return engine,engine.run()
        engine,code=run();self.assertEqual(code,0);before=copy.deepcopy(engine.state.task('db')['files']['db/one/file'])
        (source/'one/file').write_text('x'*11);(source/'two/file').write_text('healthy');certify();engine,code=run();self.assertEqual(code,4)
        self.assertEqual(engine.state.task('db')['files']['db/one/file'],before);self.assertEqual(len(engine.state.task('db')['files']['db/two/file']['versions']),2);self.assertEqual(engine.state.task('db')['scopes']['one']['status'],'failed')
    def test_retention_deleted_file_is_journaled(self):
        target=self.archive/'x';target.write_text('retained');cfg={'backup':{'root':str(self.archive),'include_configuration':False},'tasks':[]};engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(cfg))
        j=self.journal();j.begin();engine._durable_journal=j;engine._delete_version_file({'path':'x','size':8});engine._close_logging();self.journal().recover();self.assertEqual(target.read_text(),'retained')

    def test_automatic_startup_recovery_after_state_save_process_exit(self):
        source=self.root/'source';source.write_text('old');config=self.root/'config.yaml'
        cfg={'backup':{'root':str(self.archive),'include_configuration':False},'tasks':[{'name':'file','type':'file','source':str(source),'destination':'file'}]};config.write_text(yaml.safe_dump(cfg))
        engine=cb.BackupEngine(cb.ConfigLoader(config).load());self.assertEqual(engine.run(),0);before=copy.deepcopy(engine.state.task('file')['files']);source.write_text('new')
        script="import os,sys;from pathlib import Path;import configbackup as cb;original=cb.StateStore.save\ndef crash(self):\n original(self)\n os._exit(98)\ncb.StateStore.save=crash\ncb.main(['--config',sys.argv[1]])"
        cp=subprocess.run([sys.executable,'-c',script,str(config)],cwd=Path(cb.__file__).parent,capture_output=True);self.assertEqual(cp.returncode,98)
        source.write_text('old');engine=cb.BackupEngine(cb.ConfigLoader(config).load());self.assertEqual(engine.run(),0)
        logical=next(iter(before));self.assertEqual(engine.state.task('file')['files'][logical]['versions'],before[logical]['versions']);self.assertFalse((self.directory/'pending.json').exists())

if __name__=='__main__':unittest.main()
