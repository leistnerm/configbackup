import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import configbackup_lib.engine.configbackup as cb
import configbackup_lib.engine.completeness as c
import configbackup_lib.recovery.archive_integrity as integrity
import configbackup_lib.recovery.recovery_assets as assets
import configbackup_lib.recovery.recovery_maintenance as maintenance
from configbackup_lib.recovery import journal_recovery
from configbackup_lib.recovery.transaction_journal import Journal


class ArchiveAndRecoveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.source=self.root/'source';self.source.mkdir()
        self.config={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'internal':{'staging_root':str(self.root/'staging')},'options':{'log_level':'CRITICAL'},
            'tasks':[{'name':'save','type':'directory','source':str(self.source),'destination':'data','collection_manifest':True,'retention':{'active':{'mode':'simple','min_versions':1,'max_versions':3}}}]}
    def tearDown(self):self.tmp.cleanup()
    def engine(self,prune=False):return cb.BackupEngine(cb.ConfigLoader(self.root/'config.yaml')._resolve(copy.deepcopy(self.config)),prune_only=prune)
    def run_engine(self,prune=False):
        e=self.engine(prune)
        try:return e,e.run()
        finally:e._close_logging()
    def capture(self,text):
        for name in ('one','two','three'):
            (self.source/name).mkdir(exist_ok=True);(self.source/name/'schema.sql').write_text(text)
        c.publish(self.source,[c.section(self.source,n) for n in ('one','two','three')])
    def versions(self,e,scope='two'):return e.state.task('save')['files']['data/'+scope+'/schema.sql']['versions']
    def test_missing_current_freezes_only_bad_scope_and_retention(self):
        for text in ('SELECT 1;','SELECT 2;'):self.capture(text);self.assertEqual(self.run_engine()[1],0)
        e=self.engine();versions=copy.deepcopy(self.versions(e));e._close_logging();(e.root/versions[-1]['path']).unlink()
        self.config['tasks'][0]['retention']['active']['max_versions']=1
        e,code=self.run_engine();self.assertEqual(code,4);self.assertEqual(self.versions(e),versions)
        self.assertTrue((e.root/versions[0]['path']).exists());self.assertEqual(len(self.versions(e,'one')),1);self.assertEqual(len(self.versions(e,'three')),1)
    def test_prune_checks_survivors_without_updating_failed_scope(self):
        for text in ('SELECT 1;','SELECT 2;'):self.capture(text);self.run_engine()
        e=self.engine();before=copy.deepcopy(self.versions(e));e._close_logging();(e.root/before[-1]['path']).write_text('damaged')
        self.config['tasks'][0]['retention']['active']['max_versions']=1
        e,code=self.run_engine(True);self.assertEqual(code,4);self.assertEqual(self.versions(e),before)
        self.assertTrue((e.root/before[0]['path']).exists());self.assertEqual(len(self.versions(e,'one')),1)
    def test_bad_manifest_bytes_keep_healthy_archive_and_git_updates(self):
        self.config['git']={'repository':str(self.root/'git')};self.config['tasks'][0]['storage']='both'
        self.capture('SELECT 1;');self.run_engine();self.capture('SELECT 2;');(self.source/'two/schema.sql').write_text('damaged')
        e,code=self.run_engine();self.assertEqual(code,4)
        for scope,expected in [('one','SELECT 2;'),('two','SELECT 1;'),('three','SELECT 2;')]:
            self.assertEqual((e.root/self.versions(e,scope)[-1]['path']).read_text(),expected)
            self.assertEqual((self.root/'git/data'/scope/'schema.sql').read_text(),expected)
    def test_global_manifest_overlap_still_refused(self):
        self.capture('SELECT 1;');c.publish(self.source,[c.section(self.source,'one'),c.section(self.source,'one/schema.sql')])
        with self.assertRaises(ValueError):c.Coverage(self.source)
    def test_failed_git_commit_overrides_cadence_until_recovered(self):
        script="import os;from pathlib import Path;from configbackup_lib.engine.completeness import publish,section;p=Path(os.environ['CONFIGBACKUP_OUTPUT']);(p/'one').mkdir(parents=True,exist_ok=True);(p/'one/schema.sql').write_text('SELECT 1;');publish(p,[section(p,'one')])"
        self.config['git']={'repository':str(self.root/'git')};archive=self.config['tasks'][0];archive.update(storage='both',depends_on=['collect'])
        self.config['tasks'].insert(0,{'name':'collect','type':'execute','executable':sys.executable,'arguments':['-c',script], 'environment':{'PYTHONPATH':str(__import__('configbackup_lib').APPLICATION_ROOT)},'output_directory':str(self.source),'collection_interval_hours':24})
        with patch.object(cb.BackupEngine,'_finalize_git_repo',side_effect=OSError('commit failed')):self.assertEqual(self.run_engine()[1],4)
        e,code=self.run_engine();self.assertEqual(code,0);self.assertEqual(e.results['collect'].status,'success');self.assertFalse(e.state.data['publication']['pending'])
        self.assertEqual((self.root/'git/data/one/schema.sql').read_text(),'SELECT 1;')
        self.assertEqual(self.run_engine()[0].results['collect'].status,'not_due')
    def test_secondary_lock_failure_publishes_valid_local_status(self):
        config=self.root/'config.yaml';config.write_text('tasks: []\n');secondary=self.root/'secondary'
        with cb.single_instance_lock(secondary/'.refresh.lock'):result=maintenance.refresh({},config,self.root/'managed',secondary)
        self.assertEqual(result['secondary']['error_type'],'LockError');self.assertTrue((self.root/'managed/latest.json').is_file())
        self.assertEqual(maintenance.status(self.root/'managed')['local']['status'],'passed')
    def test_invalid_new_export_keeps_previous_pointer(self):
        config=self.root/'config.yaml';config.write_text('tasks: []\n');managed=self.root/'managed';maintenance.refresh({},config,managed)
        before=(managed/'latest.json').read_bytes();clone=self.root/'app';clone.mkdir();(clone/'configbackup.py').write_text('# incomplete')
        with patch.object(assets,'APPLICATION_ROOT',clone):
            with self.assertRaises(ValueError):maintenance.refresh({},config,managed)
        self.assertEqual(before,(managed/'latest.json').read_bytes());self.assertEqual(len(list(managed.glob('[0-9a-f]'*64))),1)
    def test_bad_journal_modes_never_approve_or_partially_restore(self):
        a=self.root/'a';b=self.root/'b';a.write_text('old');b.write_text('old');j=Journal(self.root/'journal',[self.root]);j.begin();j.remember(a);j.remember(b);a.write_text('new');b.write_text('new')
        entry=j.directory/'entry-00000000.json';valid=json.loads(entry.read_text())
        for mode in ('invalid',None,True,-1,0o1000):
            entry.write_text(json.dumps({**valid,'mode':mode}));report=journal_recovery.inspect(Journal(j.directory,[self.root]));self.assertEqual(report['status'],'invalid')
            with self.assertRaises(ValueError):Journal(j.directory,[self.root]).recover()
            self.assertEqual([a.read_text(),b.read_text()],['new','new'])
    def test_capacity_single_job_and_boundaries(self):
        sys.path.insert(0,str(__import__('configbackup_lib').APPLICATION_ROOT/'collectors/schedule'));import schedule_insights as si
        from types import SimpleNamespace
        jobs={k:SimpleNamespace(key=k,host='h',override_duration=10,durations=[]) for k in ('a','b')}
        timeline=[{'job_id':'a','start':'2026-01-01T00:00:00'},{'job_id':'b','start':'2026-01-01T00:10:00'}]
        cfg={'resource_capacity':{'host:h':1},'resource_weights':{'host:h':{'a':2,'b':1}}}
        rows,truncated=si.conflicts(timeline,jobs,cfg);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'capacity_exceeded')
        value=si.capacity_intervals(timeline,jobs,cfg);self.assertEqual(value['summary'][0]['peak_weight'],2);self.assertEqual(value['summary'][0]['overload_minutes'],10)
        self.assertEqual(value,si.capacity_intervals(timeline,jobs,cfg))
    def test_scrub_bounds_repair_plan_binding_quarantine_and_siblings(self):
        self.capture('SELECT 1;');e,_=self.run_engine();now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        limited=integrity.scrub(e,{'max_files':1},force=True,now=now);self.assertEqual(len(limited['checked']),1);self.assertEqual(len(limited['deferred']),2)
        target=e.root/self.versions(e)[-1]['path'];target.write_text('damaged');plan=integrity.repair_plan(e)
        target.write_text('different damage')
        with self.assertRaises(ValueError):integrity.repair(e,plan['plan_id'])
        plan=integrity.repair_plan(e);result=integrity.repair(e,plan['plan_id']);self.assertEqual(result['status'],'passed');self.assertEqual(target.read_text(),'SELECT 1;')
        self.assertEqual(next((e.internal_root/'integrity-quarantine').iterdir()).read_text(),'different damage')
        self.assertEqual(integrity.scrub(e,force=True,now=now)['status'],'passed')
    def test_repair_copy_failure_rolls_back_and_keeps_next_healthy(self):
        self.capture('SELECT 1;');e,_=self.run_engine()
        for scope in ('one','two'):(e.root/self.versions(e,scope)[-1]['path']).write_text('damaged')
        original=cb.atomic_copy
        def fail(source,target):
            if source==self.source/'one/schema.sql':raise OSError('read failure')
            return original(source,target)
        plan=integrity.repair_plan(e)
        with patch.object(cb,'atomic_copy',fail):result=integrity.repair(e,plan['plan_id'])
        self.assertEqual(result['status'],'partial');self.assertEqual((e.root/self.versions(e,'one')[-1]['path']).read_text(),'damaged');self.assertEqual((e.root/self.versions(e,'two')[-1]['path']).read_text(),'SELECT 1;')

if __name__=='__main__':unittest.main()
