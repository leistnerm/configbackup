import copy
import hashlib
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
import configbackup_lib.engine.configbackup as cb
import configbackup_lib.engine.completeness as c
from configbackup_lib.engine import determinism
import configbackup_lib.recovery.recovery_assets as ra
import configbackup_lib.recovery.recovery_maintenance as rm
import configbackup_lib.engine.schedule_discovery as sd
import configbackup_lib.engine.task_lifecycle as tl
from configbackup_lib.recovery.transaction_journal import Journal


class ScopeRollbackAndStateMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.src=self.root/'source';self.src.mkdir()
        self.cfg={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'options':{'log_level':'CRITICAL'},'tasks':[{'name':'archive','type':'directory','source':str(self.src),'destination':'snap','retention':{'active':{'mode':'simple','min_versions':1,'max_versions':10}}}]}
    def tearDown(self):self.tmp.cleanup()
    def run_engine(self):
        e=cb.BackupEngine(cb.ConfigLoader(self.root/'unused')._resolve(copy.deepcopy(self.cfg)));code=e.run();return e,code
    def prepare(self,scopes=False):
        for i in range(3):
            for name in ['one','two']:
                (self.src/name).mkdir(exist_ok=True);(self.src/name/'file').write_text(str(i))
            if scopes:c.publish(self.src,[c.section(self.src,'one'),c.section(self.src,'two')])
            self.assertEqual(self.run_engine()[1],0)
    def test_stable_identity_unreported_scope_history_is_frozen(self):
        self.cfg['tasks'][0].update(state_id='stable',collection_manifest=True);self.prepare(True)
        c.publish(self.src,[c.section(self.src,'one')]);self.cfg['tasks'][0]['retention']['active']['max_versions']=1
        e,code=self.run_engine();self.assertEqual(code,0)
        self.assertEqual(len(e.state.task('archive')['files']['snap/two/file']['versions']),3)
        self.assertEqual(len(e.state.task('archive')['files']['snap/one/file']['versions']),1)
        self.assertEqual(e.state.task('archive')['scopes']['two']['status'],'not_discovered')
    def test_failed_retention_scope_rolls_back_and_healthy_scope_prunes(self):
        self.cfg['tasks'][0]['collection_manifest']=True;self.prepare(True)
        e,_=self.run_engine();before=copy.deepcopy(e.state.task('archive')['files']['snap/one/file'])
        targets=[e.root/v['path'] for v in before['versions']];original=Path.unlink;failed=[]
        def unlink(path,*a,**kw):
            if path==targets[1] and not failed:failed.append(True);raise PermissionError('synthetic retention failure')
            return original(path,*a,**kw)
        self.cfg['tasks'][0]['retention']['active']['max_versions']=1
        # Collection succeeds before retention starts. Its observation timestamp must
        # advance, while a failed retention transaction preserves all versions.
        observed = '2030-01-02T03:04:05+00:00'
        with patch.object(Path,'unlink',unlink), patch.object(cb,'iso_now',return_value=observed):
            e,code=self.run_engine()
        self.assertEqual(code,4);self.assertTrue(failed)
        expected=copy.deepcopy(before);expected['last_seen']=observed
        self.assertNotEqual(before['last_seen'],observed)
        self.assertEqual(e.state.task('archive')['files']['snap/one/file'],expected)
        self.assertTrue(all(p.exists() for p in targets));self.assertEqual(len(e.state.task('archive')['files']['snap/two/file']['versions']),1)
        saved=json.loads(e.state.path.read_text())
        self.assertTrue(all((e.root/v['path']).is_file() for record in saved['tasks']['archive']['files'].values() for v in record['versions']))
    def test_backup_retry_overrides_successful_collector_cadence(self):
        script="import os,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from configbackup_lib.engine.completeness import publish,section;p=Path(os.environ['CONFIGBACKUP_OUTPUT']);p.mkdir(exist_ok=True,parents=True);(p/'db').mkdir(exist_ok=True);(p/'db/schema').write_text('12345');publish(p,[section(p,'db')])"
        self.cfg['tasks']=[{'name':'collect','type':'execute','executable':sys.executable,'arguments':['-c',script,str(__import__('configbackup_lib').APPLICATION_ROOT)],'output_directory':str(self.src),'collection_interval_hours':24},{'name':'archive','type':'directory','source':str(self.src),'destination':'dbs','depends_on':['collect'],'collection_manifest':True,'max_scope_bytes':1}]
        self.assertEqual(self.run_engine()[1],4);self.cfg['tasks'][1]['max_scope_bytes']=10
        e,code=self.run_engine();self.assertEqual(code,0);self.assertEqual(e.results['archive'].status,'success');self.assertTrue(e.state.task('archive')['files'])
        e,code=self.run_engine();self.assertEqual(e.results['collect'].status,'not_due');self.assertEqual(e.results['archive'].status,'not_due')
    def test_nested_destination_migration_preserves_mapping_and_version(self):
        (self.src/'database').mkdir();(self.src/'database/schema.sql').write_text('schema');e,_=self.run_engine()
        old=copy.deepcopy(e.state.task('archive')['files']['snap/database/schema.sql']['versions'])
        report,updated=tl.plan(e.state.data,'archive','new','new-prefix');self.assertEqual(report['changes'][0]['after'],'new-prefix/database/schema.sql')
        e.state.data=updated;e.state.save();self.cfg['tasks'][0].update(state_id='new',destination='new-prefix')
        e,code=self.run_engine();self.assertEqual(code,0);self.assertEqual(list(e.state.task('archive')['files']),['new-prefix/database/schema.sql']);self.assertEqual(e.state.task('archive')['files']['new-prefix/database/schema.sql']['versions'],old)
    def test_legacy_destination_move_requires_explicit_prefix(self):
        with self.assertRaises(ValueError):tl.plan({'tasks':{'a':{'files':{'old/nested/a':{}}}}},'a','b','new')
    def test_sql_literals_and_line_separators_never_normalized(self):
        captures=[]
        for i,body in enumerate(["SELECT 'alpha\u2028beta';","SELECT 'alpha\nbeta';"]):
            r=self.root/str(i);r.mkdir();(r/'schema.sql').write_text('\\restrict Key'+str(i)+'\n'+body+'\n\\unrestrict Key'+str(i)+'\n');c.publish(r,[c.section(r,'schema.sql')]);captures.append(r)
        self.assertEqual(determinism.compare(captures)['status'],'changed')
        from collectors.common.canonicalize import canonical_pg
        a="\\restrict Abc\r\nSELECT 'a\r\nb';\r\n\\unrestrict Abc\r\n"
        self.assertNotEqual(canonical_pg(a),canonical_pg(a.replace('\r\n','\n')))
        self.assertEqual(canonical_pg(a),canonical_pg(a.replace('Abc','Def')))
    def test_journal_source_read_failure_does_not_poison_sibling(self):
        a=self.root/'a';b=self.root/'b';a.write_text('a');b.write_text('b');j=Journal(self.root/'journal',[self.root]);j.begin()
        with patch('configbackup_lib.recovery.transaction_journal.shutil.copyfileobj',side_effect=OSError('synthetic read error')):
            with self.assertRaises(OSError):j.remember(a)
        j.remember(b);b.write_text('changed');Journal(j.directory,[self.root]).recover();self.assertEqual(b.read_text(),'b')
    def test_journal_metadata_is_linear_and_missing_entry_fails_closed(self):
        j=Journal(self.root/'journal',[self.root]);j.begin();written=[]
        import configbackup_lib.recovery.transaction_journal as tj
        real=tj.atomic
        def capture(path,data):written.append(len(data));return real(path,data)
        with patch('configbackup_lib.recovery.transaction_journal.atomic',capture):
            for i in range(100):j.remember(self.root/f'file-{i}')
        self.assertLess(sum(written),100_000)
        (j.directory/'entry-00000020.json').unlink()
        with self.assertRaises(ValueError):Journal(j.directory,[self.root]).recover()
    def test_legacy_pending_journal_still_recovers(self):
        a=self.root/'a';a.write_text('old');d=self.root/'journal';d.mkdir();(d/'before-00000000').write_text('old')
        (d/'pending.json').write_text(json.dumps({'schema':1,'status':'pending','repo':None,'head':None,'entries':[{'path':str(a),'backup':'before-00000000','mode':0o600,'sha256':hashlib.sha256(b'old').hexdigest()}]}));a.write_text('new')
        Journal(d,[self.root]).recover();self.assertEqual(a.read_text(),'old')
    def test_task_rollback_removes_phantom_changes(self):
        for name in ['a','b']:(self.src/name).write_text('old')
        self.run_engine()
        for name in ['a','b']:(self.src/name).write_text('new')
        original=cb.BackupEngine._store_one_file
        def fail(self,task,source,logical,**kw):
            if source.name=='b':raise PermissionError('synthetic source failure')
            return original(self,task,source,logical,**kw)
        with patch.object(cb.BackupEngine,'_store_one_file',fail):e,code=self.run_engine()
        self.assertEqual(code,4);self.assertEqual(e.changes,[])
    def test_code_only_change_invalidates_export_freshness(self):
        config=self.src/'config.yaml';config.write_text('tasks: []\n');clone=self.root/'application';clone.mkdir()
        (clone/'configbackup.py').write_text('# version remains unchanged');(clone/'requirements.txt').write_text('PyYAML')
        with patch.object(ra,'APPLICATION_ROOT',clone):
            first=rm.refresh({},config,self.root/'exports');self.assertEqual(rm.freshness({},config,first['bundle'])['status'],'current')
            (clone/'configbackup.py').write_text('# same-version correction')
            self.assertEqual(rm.freshness({},config,first['bundle'])['status'],'stale_or_invalid')
            second=rm.refresh({},config,self.root/'exports');self.assertNotEqual(first['fingerprint'],second['fingerprint'])
    def test_malformed_windows_mapping_keeps_later_task(self):
        good={'name':'healthy','xml':'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>C:\\Jobs\\job.exe</Command></Exec></Actions></Task>'}
        def execute(*a,**kw):return subprocess.CompletedProcess(a,0,json.dumps([{'name':'missing xml'},{'unavailable':True},good]),'')
        value=sd.discover(system='windows',execute=execute,which=lambda _: 'pwsh');self.assertEqual(value['jobs_inspected'],1);self.assertEqual(len(value['probe_errors']),2)

    def test_prune_only_failure_commits_healthy_scope(self):
        self.cfg['tasks'][0]['collection_manifest']=True;self.prepare(True)
        e,_=self.run_engine();record=copy.deepcopy(e.state.task('archive')['files']['snap/one/file']);target=e.root/record['versions'][1]['path'];original=Path.unlink;failed=[]
        def unlink(path,*args,**kw):
            if path==target and not failed:failed.append(True);raise PermissionError('synthetic prune failure')
            return original(path,*args,**kw)
        self.cfg['tasks'][0]['retention']['active']['max_versions']=1
        e=cb.BackupEngine(cb.ConfigLoader(self.root/'unused')._resolve(copy.deepcopy(self.cfg)),prune_only=True)
        with patch.object(Path,'unlink',unlink):self.assertEqual(e.run(),4)
        self.assertEqual(e.state.task('archive')['files']['snap/one/file'],record);self.assertEqual(len(e.state.task('archive')['files']['snap/two/file']['versions']),1)
        self.assertFalse((e.internal_root/'transaction/pending.json').exists())
    def test_prune_only_disabled_collector_preserves_dependent_archive(self):
        self.prepare();self.cfg['tasks'].insert(0,{'name':'collector','type':'execute','executable':sys.executable,'arguments':['-c','pass'],'enabled':False})
        self.cfg['tasks'][1]['depends_on']=['collector'];self.cfg['tasks'][1]['retention']['active']['max_versions']=1
        e=cb.BackupEngine(cb.ConfigLoader(self.root/'unused')._resolve(copy.deepcopy(self.cfg)),prune_only=True);self.assertEqual(e.run(),0)
        self.assertEqual(len(e.state.task('archive')['files']['snap/one/file']['versions']),3)

if __name__=='__main__':unittest.main()
