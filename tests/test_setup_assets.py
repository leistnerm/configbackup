import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
import configbackup as cb
import recovery_assets as ra
import startup_launcher
from recovery import plan_restore,recover


class SetupAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.config=self.root/'configbackup.yaml'
        self.cfg={'backup':{'root':str(self.root/'archive')},'options':{'log_level':'CRITICAL'},'tasks':[]}
        self.save()
    def tearDown(self):self.tmp.cleanup()
    def save(self):self.config.write_text(yaml.safe_dump(self.cfg),encoding='utf-8')
    def run_backup(self):
        engine=cb.BackupEngine(cb.ConfigLoader(self.config).load())
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(engine.run(),0)
        return engine
    def contents(self,engine):return {r['logical']:engine._archive_state_path(r['version']['path']).read_bytes() for r in plan_restore(engine.state)['records']}

    def test_registered_launchers_additional_files_repeat_and_restore(self):
        extra=self.root/'schedule.yaml';extra.write_text('watchdogs: []\n')
        self.cfg['backup']['configuration_files']=['schedule.yaml'];self.save()
        files=startup_launcher.generate(self.config,self.root/'launchers','linux',sys.executable)
        self.assertEqual(set(ra.registry(self.config)['files']),set(str(p.resolve()) for p in files))
        engine=self.run_backup();before=self.contents(engine);engine=self.run_backup()
        self.assertEqual(before,self.contents(engine))
        self.assertTrue(any(k.endswith('/run-configbackup.sh') for k in before))
        self.assertTrue(any(k.endswith('/AUTH-SETUP.txt') for k in before))
        self.assertTrue(any(k.endswith('/schedule.yaml') for k in before))
        self.assertIn('_configuration/recovery-inventory.json',before)
        for task in engine.state.data['tasks'].values():
            for item in task['files'].values():self.assertEqual(len(item['versions']),1);self.assertFalse(item.get('git_hash'))
        restore=cb.BackupEngine(cb.ConfigLoader(self.config).load())
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(recover(restore,self.root/'restored'),0)
        restore._close_logging()
        for key,data in before.items():self.assertEqual((self.root/'restored'/key).read_bytes(),data)

    def test_missing_asset_preserves_own_history_and_complete_inventory(self):
        good=self.root/'good.sh';bad=self.root/'bad.yaml';good.write_text('old');bad.write_text('retained')
        self.cfg['backup']['configuration_files']=['good.sh','bad.yaml'];self.save();engine=self.run_backup();before=self.contents(engine)
        bad.unlink();good.write_text('new');engine=self.run_backup();after=self.contents(engine)
        badkey=next(k for k in before if k.endswith('/bad.yaml'));goodkey=next(k for k in before if k.endswith('/good.sh'))
        self.assertEqual(after[badkey],b'retained');self.assertEqual(after[goodkey],b'new')
        self.assertEqual(after['_configuration/recovery-inventory.json'],before['_configuration/recovery-inventory.json'])
        self.assertEqual(engine.results['configbackup-recovery-inventory'].status,'failed')
        self.assertEqual(ra.inventory(self.cfg,self.config)['status'],'partial')

    def test_reference_allowlist_working_directory_and_equal_flags(self):
        inputs=self.root/'inputs';inputs.mkdir();p=inputs/'schedule.yaml';p.write_text('sources: []')
        cfg=copy.deepcopy(self.cfg);cfg['tasks']=[{'name':'reports','type':'execute','working_directory':str(inputs),'arguments':['analyze_schedules.py','--config=schedule.yaml']},
           {'name':'other','type':'execute','arguments':['unknown.py','--config','never-read.yaml']}]
        rows,errors=ra.assets(cfg,self.config);self.assertFalse(errors)
        self.assertEqual({Path(r['source']) for r in rows},{self.config,p})
        cfg['tasks'][0]={'arguments':['collect_system.py','--smart-config',str(p),'--registry-config',str(p)]}
        self.assertEqual(len(ra.assets(cfg,self.config)[0]),2)
        cfg['tasks'][0]={'arguments':['INVOKE-PROFILECOLLECTION.PS1','-ProfileFile',str(p)]}
        self.assertEqual(len(ra.assets(cfg,self.config)[0]),2)

    def test_corrupt_registry_does_not_veto_main_or_explicit_asset(self):
        x=self.root/'x.ini';x.write_text('data');self.cfg['backup']['configuration_files']=['x.ini'];self.save()
        ra.registry_path(self.config).write_text('{broken')
        rows,errors=ra.assets(self.cfg,self.config);self.assertTrue(errors);self.assertTrue(any(r['source']==str(x) for r in rows))
        engine=self.run_backup();self.assertEqual(engine.results['configbackup-settings'].status,'success');self.assertEqual(engine.results['configbackup-recovery-inventory'].status,'failed')

    def test_individual_optout_and_no_implicit_directory_capture(self):
        files=startup_launcher.generate(self.config,self.root/'launch','linux',sys.executable)
        self.cfg['backup']['include_related_configuration']=False
        rows,errors=ra.assets(self.cfg,self.config);self.assertEqual(len(rows),1)
        self.cfg['backup']['configuration_files']=[str(self.root/'launch')]
        self.assertEqual(ra.inventory(self.cfg,self.config)['status'],'partial')
        self.cfg['backup']['include_configuration']=False;self.save()
        self.assertEqual(cb.ConfigLoader(self.config).load()['tasks'],[])

    def test_export_complete_hashes_private_permissions_and_source_independence(self):
        x=self.root/'custom.sh';x.write_text('#!/bin/sh\nexit 0\n');self.cfg['backup']['configuration_files']=[str(x)];self.save()
        startup_launcher.generate(self.config,self.root/'launch','windows','python')
        before=self.config.read_bytes();result=ra.export_recovery(self.cfg,self.config,self.root/'export')
        self.assertEqual(self.config.read_bytes(),before)
        for row in result['assets']:self.assertEqual((self.root/'export'/row['logical']).read_bytes(),Path(row['source']).read_bytes())
        self.assertTrue((self.root/'export/application/configbackup.py').is_file())
        self.assertTrue((self.root/'export/application/requirements.txt').is_file())
        self.assertFalse((self.root/'export/archive').exists())
        if os.name!='nt':self.assertEqual((self.root/'export').stat().st_mode&0o777,0o700)
        with self.assertRaises(ValueError):ra.export_recovery(self.cfg,self.config,self.root/'export')
        result=subprocess.run([sys.executable,str(self.root/'export/application/configbackup.py'),'--version'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0);self.assertIn(cb.APP_VERSION,result.stdout)

    def test_export_failure_never_publishes_partial_directory(self):
        self.cfg['backup']['configuration_files']=['missing.sh']
        with self.assertRaises(ValueError):ra.export_recovery(self.cfg,self.config,self.root/'bad-export')
        self.assertFalse((self.root/'bad-export').exists())
        self.cfg['backup']['configuration_files']=[]
        original=Path.read_bytes
        def changed(p):return b'changed' if p==self.config else original(p)
        # Inventory/second read mismatch, not a simulated filesystem-only write failure.
        snapshot=ra.inventory(self.cfg,self.config)
        with patch.object(ra,'inventory',return_value=snapshot),patch.object(Path,'read_bytes',changed):
            with self.assertRaises(ValueError):ra.export_recovery(self.cfg,self.config,self.root/'changed-export')
        self.assertFalse((self.root/'changed-export').exists());self.assertFalse(list(self.root.glob('.configbackup-recovery-*')))

    def test_existing_launchers_register_repeatably_without_execution(self):
        folder=self.root/'old launchers';folder.mkdir();script=folder/'run-configbackup.bat';script.write_text('@echo off\nexit /b 0\n')
        (folder/'unrelated.txt').write_text('not selected')
        files=ra.register_existing(self.config,folder);before=ra.registry_path(self.config).read_bytes()
        ra.register_existing(self.config,folder);self.assertEqual(ra.registry_path(self.config).read_bytes(),before)
        self.assertEqual(files,[script]);self.assertTrue(any(r['source']==str(script) for r in ra.assets(self.cfg,self.config)[0]))

    def test_registration_failure_rolls_back_generated_files_and_keeps_registry(self):
        registry=ra.registry_path(self.config);registry.write_text('{invalid')
        with self.assertRaises(ValueError):startup_launcher.generate(self.config,self.root/'launch','linux','python')
        self.assertFalse(list((self.root/'launch').iterdir()));self.assertEqual(registry.read_text(),'{invalid')
        self.assertFalse(registry.with_name(registry.name+'.lock').exists())

    def test_duplicate_filenames_distinct_sources_and_unresolved_variable(self):
        a=self.root/'one';b=self.root/'two';a.mkdir();b.mkdir();(a/'x.yaml').write_text('one');(b/'x.yaml').write_text('two')
        self.cfg['backup']['configuration_files']=[str(a/'x.yaml'),str(b/'x.yaml'),'${MISSING_CONFIGBACKUP_TEST_VAR}/x']
        with patch.dict(os.environ,{},clear=True):rows,errors=ra.assets(self.cfg,self.config)
        self.assertEqual(len(rows),3);self.assertEqual(len({r['logical'] for r in rows}),3);self.assertTrue(errors)

    def test_secret_values_not_in_inventory_but_original_files_preserved(self):
        self.cfg['monitoring']={'notifications':{'channels':[{'password_env':'SMTP_PASSWORD','password':'private-inline-value'}]}}
        inv=ra.inventory(self.cfg,self.config);encoded=json.dumps(inv)
        self.assertNotIn('private-inline-value',encoded);self.assertIn('SMTP_PASSWORD',inv['environment_references'])
        self.cfg['backup']['configuration_files']='not-list'
        with self.assertRaises(ValueError):ra.assets(self.cfg,self.config)

if __name__=='__main__':unittest.main()
