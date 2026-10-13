import contextlib
import copy
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
import configbackup_lib.recovery.recovery_assets as assets
import configbackup_lib.recovery.setup_recovery as r
from configbackup_lib.engine import startup_launcher

class SetupRecoveryToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.source=self.root/'source';self.source.mkdir()
        self.config=self.source/'config.yaml';self.cfg={'backup':{'root':str(self.source/'old-archive')},'tasks':[]}
        self.config.write_text(yaml.safe_dump(self.cfg));self.bundle=self.root/'bundle'
    def tearDown(self):self.tmp.cleanup()
    def export(self):return assets.export_recovery(self.cfg,self.config,self.bundle)
    def manifest(self,update):
        p=self.bundle/'recovery-inventory.json';v=json.loads(p.read_text());update(v);p.write_text(json.dumps(v))
    def test_verification_repeat_and_tamper_missing_extra(self):
        inv=self.export();first=r.verify(self.bundle)[0];self.assertEqual(first['status'],'passed');self.assertEqual(first,r.verify(self.bundle)[0])
        p=self.bundle/inv['assets'][0]['logical'];old=p.read_bytes();p.write_text('corrupt');self.assertEqual(r.verify(self.bundle)[0]['status'],'failed');p.write_bytes(old)
        p.unlink();self.assertEqual(r.verify(self.bundle)[0]['status'],'failed');p.write_bytes(old)
        (self.bundle/'extra.py').write_text('raise RuntimeError()');self.assertEqual(r.verify(self.bundle)[0]['status'],'failed')
    def test_paths_duplicates_partial_manifests_fail_before_copy(self):
        original=self.export();p=self.bundle/'recovery-inventory.json'
        for mutate in (lambda v:v['assets'][0].update(logical='../escape'),lambda v:v['assets'].append(v['assets'][0]),lambda v:v.update(status='partial'),lambda v:v['application_files'][0].update(path='application/../escape')):
            p.write_text(json.dumps(original));self.manifest(mutate)
            with self.assertRaises(ValueError):r.relocate(self.bundle,self.root/'candidate')
            self.assertFalse((self.root/'candidate').exists())
    def test_symlink_rejected(self):
        inv=self.export();p=self.bundle/inv['assets'][0]['logical'];p.unlink();p.symlink_to(self.config)
        with self.assertRaises(ValueError):r.verify(self.bundle)
    def test_checklist_contains_references_not_values(self):
        self.cfg['connections']={'pg':{'engine':'postgresql','password_env':'PG_PASSWORD','password':'synthetic-secret-value'}};self.config.write_text(yaml.safe_dump(self.cfg));self.export()
        result=r.checklist(self.bundle);text=json.dumps(result);self.assertIn('PG_PASSWORD',text);self.assertNotIn('synthetic-secret-value',text);self.assertEqual(result['connections'][0]['engine'],'postgresql')
    def test_relocate_disables_actions_maps_assets_regenerates_launcher(self):
        extra=self.source/'rules.yaml';extra.write_text('sources: []\n');self.cfg['backup']['configuration_files']=[str(extra)]
        self.cfg['variables']={'APP':str(Path(r.__file__).resolve().parent),'DATA':'/old/data'}
        self.cfg['git']={'push':True,'repository':str(self.source/'git')};self.cfg['tasks']=[{'name':'script','type':'execute','executable':sys.executable,'arguments':[str(extra)],'output_directory':'/old/data/output'}]
        self.config.write_text(yaml.safe_dump(self.cfg));system={'Darwin':'macos','Windows':'windows'}.get(platform.system(),'linux')
        startup_launcher.generate(self.config,self.source/'launch',system,sys.executable);before=self.config.read_bytes();self.export()
        dest=self.root/'restored';report=r.relocate(self.bundle,dest,{'/old/data':str(dest/'data')})
        self.assertEqual(report['configuration_validation'],'passed');self.assertEqual(report['launchers'][0]['status'],'generated');self.assertEqual(self.config.read_bytes(),before)
        cfg=yaml.safe_load((dest/'configbackup.yaml').read_text());self.assertFalse(cfg['tasks'][0]['enabled']);self.assertEqual(cfg['git'],{});self.assertFalse(cfg['monitoring']['enabled']);self.assertEqual(cfg['backup']['root'],str(dest/'archive'));self.assertEqual(cfg['tasks'][0]['output_directory'],str(dest/'data/output'))
        launch=next((dest/'launchers/0').glob('run-configbackup.ps1' if system=='windows' else 'run-configbackup.sh'));self.assertIn(str(dest/'application/configbackup.py'),launch.read_text());self.assertFalse((dest/'archive').exists())
        self.assertTrue(any('run-configbackup' in p for p in cfg['backup']['configuration_files']))
        with self.assertRaises(ValueError):r.relocate(self.bundle,dest)
        with self.assertRaises(ValueError):r.relocate(self.bundle,self.source/'child')
        with patch('configbackup_lib.engine.configbackup.ConfigLoader.load',side_effect=ValueError('synthetic invalid config')):
            bad=r.relocate(self.bundle,self.root/'invalid-candidate')
        self.assertEqual(bad['status'],'failed');self.assertEqual(bad['configuration_validation'],'failed')
    def test_prefix_mapping_does_not_change_embedded_commands(self):
        changes=[];value={'path':'/old/config/x','similar':'/older/x','command':'python /old/config/x','secret':'/old/secret'}
        new=r.replace_paths(value,{'/old':'/new'},changes);self.assertEqual(new['path'],str(Path('/new')/'config/x'));self.assertEqual(new['secret'],value['secret']);self.assertEqual(new['similar'],value['similar']);self.assertEqual(new['command'],value['command']);self.assertEqual(len(changes),1)
    def test_change_during_copy_fails_without_publish(self):
        self.export();real=r.shutil.copyfile
        def corrupt(source,target):
            real(source,target);Path(target).write_text('changed')
        with patch.object(r.shutil,'copyfile',side_effect=corrupt),self.assertRaises(ValueError):r.relocate(self.bundle,self.root/'candidate')
        self.assertFalse((self.root/'candidate').exists())
    def test_dependency_copy_hashes_no_install_and_offline_check(self):
        wheels=self.root/'wheels';wheels.mkdir();(wheels/'example-1-py3-none-any.whl').write_bytes(b'fixture')
        dest=self.root/'dependencies';r.dependencies(dest,wheels);calls=[]
        def run(args,**kw):calls.append((args,kw));return subprocess.CompletedProcess(args,0)
        self.assertEqual(r.check_dependencies(dest,run)['status'],'passed');self.assertIn('--no-index',calls[0][0]);self.assertNotIn('install',calls[0][0]);self.assertFalse(calls[0][1]['shell'])
        (dest/'wheels/example-1-py3-none-any.whl').write_bytes(b'changed')
        with self.assertRaises(ValueError):r.check_dependencies(dest,lambda *a,**kw:self.fail('No process after corruption'))
    def test_dependency_untrusted_requirements_never_execute(self):
        wheels=self.root/'wheels';wheels.mkdir();(wheels/'fixture.whl').write_bytes(b'x');dest=self.root/'deps';r.dependencies(dest,wheels)
        req=dest/'requirements.txt';req.write_text('pkg @ https://example.invalid/secret');manifest=dest/'dependencies.json';data=json.loads(manifest.read_text())
        next(row for row in data['files'] if row['path']=='requirements.txt')['sha256']=r.digest(req);manifest.write_text(json.dumps(data))
        with self.assertRaises(ValueError):r.check_dependencies(dest,lambda *a,**kw:self.fail('No untrusted requirements'))
    def test_download_failure_and_empty_wheelhouse_not_published(self):
        dest=self.root/'deps'
        with self.assertRaises(ValueError):r.dependencies(dest,download=True,execute=lambda *a,**kw:subprocess.CompletedProcess(a,2))
        self.assertFalse(dest.exists());empty=self.root/'empty';empty.mkdir()
        with self.assertRaises(ValueError):r.dependencies(dest,empty)
        self.assertFalse(dest.exists())
    def test_drill_pass_due_failure_retention_and_metrics(self):
        self.export();history=self.root/'history';result=r.drill(self.bundle,history,keep=1);self.assertEqual(result['status'],'passed')
        self.assertEqual(r.drill(self.bundle,history)['status'],'not_due')
        (self.bundle/'application/configbackup.py').write_text('changed');self.assertEqual(r.drill(self.bundle,history,force=True,keep=1)['status'],'failed')
        self.assertEqual(len(list(history.glob('drill-*.json'))),1)
        from configbackup_lib.reporting.monitoring import flatten
        metrics=flatten(json.loads((history/'telemetry.json').read_text()));self.assertEqual(next(m['value'] for m in metrics if m['metric']=='configbackup.setup_recovery.failed_count'),1)
        self.assertIn('failed',(history/'report.html').read_text());self.assertTrue((history/'.drill.lock').exists())
    def test_drill_lock_interval_and_history_guards(self):
        self.export();history=self.root/'history';history.mkdir();(history/'.drill.lock').write_text('stale lock file')
        from configbackup_lib.engine.configbackup import single_instance_lock, LockError
        with single_instance_lock(history/'.drill.lock'):
            with self.assertRaises(LockError):r.drill(self.bundle,history)
        with self.assertRaises(ValueError):r.drill(self.bundle,self.bundle/'history')
        with self.assertRaises(ValueError):r.drill(self.bundle,history,interval_days=float('nan'))
    def test_cli_corruption_failure_code(self):
        self.export();(self.bundle/'application/configbackup.py').unlink()
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(r.main(['verify','--bundle',str(self.bundle)]),6)

if __name__=='__main__':unittest.main()
