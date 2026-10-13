import errno,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import yaml
import configbackup as cb
import archive_integrity as integrity
from transaction_journal import Journal
class FailureInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.source=self.root/'source';self.source.mkdir()
        for name in ('a','b'):(self.source/name).write_text('source-'+name)
        self.path=self.root/'config.yaml';self.path.write_text(yaml.safe_dump({'backup':{'root':str(self.root/'archive'),'include_configuration':False},'options':{'log_level':'CRITICAL'},'tasks':[{'name':'data','type':'directory','source':str(self.source),'destination':'data'}]}))
        self.engine=cb.BackupEngine(cb.ConfigLoader(self.path).load());self.assertEqual(self.engine.run(),0)
        self.targets=[self.engine.root/r['versions'][-1]['path'] for r in self.engine.state.task('data')['files'].values()]
        for target in self.targets:target.write_text('corrupt')
    def tearDown(self):self.engine._close_logging();self.tmp.cleanup()
    def test_real_repair_crash_reconciles_before_retry(self):
        plan=integrity.repair_plan(self.engine);script="""import os,sys
from pathlib import Path
import configbackup as cb,archive_integrity as integrity
engine=cb.BackupEngine(cb.ConfigLoader(Path(sys.argv[1])).load());original=cb.atomic_copy
def crash(source,target,*args,**kwargs):
 result=original(source,target,*args,**kwargs)
 if str(target)==sys.argv[3]:os._exit(99)
 return result
cb.atomic_copy=crash
integrity.repair(engine,sys.argv[2])
"""
        result=subprocess.run([sys.executable,'-c',script,str(self.path),plan['plan_id'],str(self.targets[0])],cwd=Path(cb.__file__).parent,capture_output=True);self.assertEqual(result.returncode,99)
        journal=Journal(self.engine.internal_root/'transaction',[self.engine.root,self.engine.git_repo],None);self.assertEqual(journal.recover()['status'],'rolled_back');self.assertEqual(self.targets[0].read_text(),'corrupt');self.assertEqual(integrity.repair(self.engine,integrity.repair_plan(self.engine)['plan_id'])['status'],'passed')
    def test_disk_full_after_write_restores_failed_file_and_repairs_sibling(self):
        original=cb.atomic_copy
        def fail(source,target,*args,**kwargs):
            result=original(source,target,*args,**kwargs)
            if target==self.targets[0]:raise OSError(errno.ENOSPC,'Injected disk full')
            return result
        with patch.object(cb,'atomic_copy',side_effect=fail):result=integrity.repair(self.engine,integrity.repair_plan(self.engine)['plan_id'])
        self.assertEqual([r['result'] for r in result['repairs']],['failed','repaired']);self.assertEqual(self.targets[0].read_text(),'corrupt');self.assertNotEqual(self.targets[1].read_text(),'corrupt')
    def test_live_process_lock_prevents_engine_mutation(self):
        before=self.engine.state.path.read_bytes()
        with cb.single_instance_lock(self.engine.internal_root/'configbackup.lock'):
            result=subprocess.run([sys.executable,str(Path(cb.__file__)), '--config',str(self.path)],capture_output=True)
        self.assertNotEqual(result.returncode,0);self.assertEqual(before,self.engine.state.path.read_bytes());self.assertTrue(all(p.read_text()=='corrupt' for p in self.targets))
if __name__=='__main__':unittest.main()
