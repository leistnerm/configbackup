import copy
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from configbackup_lib.git import change_policy
import configbackup_lib.engine.configbackup as cb
from configbackup_lib.engine import determinism
import configbackup_lib.engine.discovery_review as discovery
from configbackup_lib.recovery import recovery_assets
import configbackup_lib.recovery.recovery_maintenance as maintenance
import configbackup_lib.engine.schedule_discovery as sd
from configbackup_lib.recovery import setup_recovery
from configbackup_lib.engine import task_lifecycle
from configbackup_lib.engine.completeness import publish as manifest_write

class RecoveryAndDiscoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.source=self.root/'source';self.source.mkdir()
        self.config=self.source/'config.yaml';self.cfg={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'tasks':[]};self.config.write_text(yaml.safe_dump(self.cfg))
    def tearDown(self):self.tmp.cleanup()
    def bundle(self):
        target=self.root/'bundle';recovery_assets.export_recovery(self.cfg,self.config,target);return target
    def engine(self,cfg=None):return cb.BackupEngine(cb.ConfigLoader(self.config)._resolve(copy.deepcopy(cfg or self.cfg)))
    def capture(self,name,content=b'abc',status='complete'):
        path=self.root/name;path.mkdir();(path/'db').mkdir();(path/'db/config').write_bytes(content)
        import hashlib
        manifest_write(path,[{'path':'db','status':status,'files':{'db/config':hashlib.sha256(content).hexdigest()} if status=='complete' else {}}])
        return path
    def test_windows_quoted_equals_and_backslash(self):
        args=sd.split_command(r'--config="C:\Job Files\settings.yaml" --name "a\"b"',True)
        self.assertEqual(args,[r'--config=C:\Job Files\settings.yaml','--name','a"b'])
        row=sd.candidates([{'name':'job','platform':'windows','arguments':['x',*args]}],scan_directories=False)['candidates'][0]
        self.assertEqual(row['path'],r'C:\Job Files\settings.yaml')
    def test_bad_plist_does_not_hide_sibling(self):
        bad=self.root/'bad.plist';bad.write_bytes(plistlib.dumps(['bad']))
        good=self.root/'good.plist';good.write_bytes(plistlib.dumps({'ProgramArguments':['/app/job.py']}))
        report=sd.discover(definitions=[bad,good]);self.assertEqual(report['jobs_inspected'],1);self.assertEqual(len(report['probe_errors']),1)
        self.assertEqual(report,sd.discover(definitions=[bad,good]))
    def test_bad_working_directory_is_isolated(self):
        bad=self.root/'bad.plist';bad.write_bytes(plistlib.dumps({'ProgramArguments':['/app/job.py'],'WorkingDirectory':[]}))
        self.assertEqual(sd.discover(definitions=[bad])['jobs_inspected'],0)
    def test_deleted_bundle_invalidates_drill(self):
        bundle=self.bundle();history=self.root/'history';self.assertEqual(setup_recovery.drill(bundle,history)['status'],'passed');shutil.rmtree(bundle)
        self.assertEqual(setup_recovery.drill(bundle,history)['status'],'failed')
    def test_modified_bundle_invalidates_drill(self):
        bundle=self.bundle();history=self.root/'history';setup_recovery.drill(bundle,history);(bundle/'application/configbackup.py').write_text('changed')
        self.assertEqual(setup_recovery.drill(bundle,history)['status'],'failed')
    def test_unchanged_drill_interval_still_checks_hashes(self):
        bundle=self.bundle();history=self.root/'history';setup_recovery.drill(bundle,history)
        self.assertEqual(setup_recovery.drill(bundle,history)['status'],'not_due')
    def test_persistent_decisions_and_file_switch(self):
        file=self.source/'job.ini';file.write_text('x=1')
        report={'candidates':[{'path':str(file),'status':'available','jobs':['job'],'reasons':['explicit configuration argument']}]}
        discovery.decide(self.cfg,report,str(file),'accepted');self.assertEqual(discovery.review(self.cfg,report,True),[])
        discovery.decide(self.cfg,report,str(file),'disabled');assets,_=recovery_assets.assets(self.cfg,self.config)
        self.assertNotIn(str(file),[r['source'] for r in assets]);self.assertEqual(discovery.review(self.cfg,report)[0]['decision'],'disabled')
        with self.assertRaises(ValueError):discovery.decide(self.cfg,report,str(file),'ignored')
    def test_ignored_decisions_do_not_select_files(self):
        report={'candidates':[{'path':'/x','status':'missing','jobs':[],'reasons':[]}]};discovery.decide(self.cfg,report,'/x','ignored')
        self.assertEqual(discovery.review(self.cfg,report,True),[]);self.assertEqual(self.cfg['backup']['configuration_files'],[])
    def test_include_cycle_bounds_and_missing_sibling(self):
        a=self.source/'a.conf';b=self.source/'b.conf';a.write_text('include b.conf;\ninclude missing.conf;\ninclude ../outside.conf;\ninclude *.conf;');b.write_text('include a.conf;')
        result=discovery.includes([a],[self.source]);self.assertEqual(result,discovery.includes([a],[self.source]));self.assertEqual(len(result['candidates']),3);self.assertTrue(result['probe_errors'])
        self.assertTrue(discovery.includes([a],[self.source],max_depth=0)['probe_errors'])
    def test_include_symlink_not_followed(self):
        outside=self.root/'out';outside.write_text('x');link=self.source/'link';link.symlink_to(outside);a=self.source/'a.conf';a.write_text('include link;')
        self.assertEqual(discovery.includes([a],[self.source])['candidates'],[])
    def test_coverage_unknown_not_success_and_html_escaping(self):
        report=discovery.coverage(self.cfg,self.config);self.assertTrue(any(r['status']=='unknown' for r in report['items']))
        self.assertEqual(report,discovery.coverage(self.cfg,self.config));report['test']='<script>x</script>'
        discovery.write_report(report,self.root/'report');text=(self.root/'report/report.html').read_text();self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text)
    def test_refresh_idempotent_and_current_config_drift(self):
        managed=self.root/'managed';secondary=self.root/'secondary';first=maintenance.refresh(self.cfg,self.config,managed,secondary);second=maintenance.refresh(self.cfg,self.config,managed,secondary)
        self.assertEqual(first['status'],'created');self.assertEqual(second['status'],'unchanged');self.assertEqual(first['fingerprint'],second['fingerprint'])
        self.assertEqual(maintenance.freshness(self.cfg,self.config,first['bundle'])['status'],'current')
        self.config.write_text(yaml.safe_dump({**self.cfg,'variables':{'x':'changed'}}));self.assertEqual(maintenance.freshness(self.cfg,self.config,first['bundle'])['status'],'stale_or_invalid')
        third=maintenance.refresh(self.cfg,self.config,managed);self.assertNotEqual(first['fingerprint'],third['fingerprint']);self.assertTrue(Path(first['bundle']).is_dir())
    def test_refresh_secondary_failure_keeps_valid_local(self):
        secondary=self.root/'blocked';secondary.write_text('file')
        result=maintenance.refresh(self.cfg,self.config,self.root/'managed',secondary);self.assertEqual(result['secondary']['status'],'failed');self.assertEqual(setup_recovery.verify(result['bundle'])[0]['status'],'passed')
    def test_refresh_missing_input_preserves_latest(self):
        managed=self.root/'managed';maintenance.refresh(self.cfg,self.config,managed);before=(managed/'latest.json').read_bytes();self.cfg['backup']['configuration_files']=['missing']
        with self.assertRaises(ValueError):maintenance.refresh(self.cfg,self.config,managed)
        self.assertEqual(before,(managed/'latest.json').read_bytes())
    def test_encryption_roundtrip_tamper_wrong_password_and_nondeterminism(self):
        try:import cryptography
        except ImportError:self.skipTest('Optional cryptography dependency not installed')
        import configbackup_lib.recovery.recovery_crypto as crypto
        bundle=self.bundle();password='synthetic recovery passphrase';one=self.root/'one.cbrec';two=self.root/'two.cbrec'
        crypto.encrypt(bundle,one,password);crypto.encrypt(bundle,two,password);self.assertNotEqual(one.read_bytes(),two.read_bytes())
        with self.assertRaises(ValueError):crypto.decrypt(one,self.root/'bad','wrong recovery password')
        self.assertFalse((self.root/'bad').exists());crypto.decrypt(one,self.root/'restored',password);self.assertEqual(setup_recovery.verify(self.root/'restored')[0]['status'],'passed')
        data=bytearray(two.read_bytes());data[-1]^=1;two.write_bytes(data)
        with self.assertRaises(ValueError):crypto.decrypt(two,self.root/'tampered',password)
        self.assertFalse((self.root/'tampered').exists())
    def test_state_rename_preserves_versions(self):
        file=self.source/'x';file.write_text('one');self.cfg['tasks']=[{'name':'old','state_id':'identity','type':'file','source':str(file),'destination':'fixed/x'}]
        engine=self.engine();self.assertEqual(engine.run(),0);before=copy.deepcopy(engine.state.data['tasks']['identity']['files'])
        self.cfg['tasks'][0]['name']='new';engine=self.engine();self.assertEqual(engine.run(),0);self.assertEqual(len(engine.state.data['tasks']['identity']['files']['fixed/x']['versions']),1)
        self.assertEqual(set(engine.state.data['tasks']),{'identity'});self.assertEqual(before['fixed/x']['versions'],engine.state.data['tasks']['identity']['files']['fixed/x']['versions'])
    def test_state_migration_retirement_and_collision(self):
        state={'tasks':{'old':{'files':{'old/x':{'versions':[{'path':'old/x.1'}]}}}}}
        report,updated=task_lifecycle.plan(state,'old','new','new-prefix',from_destination='old');self.assertIn('new-prefix/x',updated['tasks']['new']['files']);self.assertIn('old',state['tasks'])
        _,retired=task_lifecycle.plan(updated,'new',retire=True);self.assertTrue(retired['tasks']['new']['retired'])
        state['tasks']['taken']={}
        with self.assertRaises(ValueError):task_lifecycle.plan(state,'old','taken')
    def test_git_destination_migration_refuses_without_modification(self):
        state={'tasks':{'old':{'files':{'old/x':{'git_path':'old/x'}}}}}
        with self.assertRaises(ValueError):task_lifecycle.plan(state,'old','new','dest')
        self.assertEqual(state['tasks']['old']['files']['old/x']['git_path'],'old/x')
    def test_cadence_skips_collector_and_archive(self):
        self.cfg['tasks']=[{'name':'collector','type':'execute','executable':sys.executable,'arguments':['-c','raise RuntimeError()'],'collection_interval_hours':24},{'name':'archive','type':'directory','source':str(self.source),'destination':'data','depends_on':['collector']}]
        engine=self.engine();engine.state.task('collector')['last_success']=cb.iso_now();engine.state.task('archive')['last_success']=cb.iso_now();engine.state.save();engine._close_logging();engine=self.engine();self.assertEqual(engine.run(),0)
        self.assertEqual(engine.results['collector'].status,'not_due');self.assertEqual(engine.results['archive'].status,'not_due')
    def test_budget_skips_only_collection_preserves_independent_files(self):
        file=self.source/'x';file.write_text('x');self.cfg['options']={'run_budget_seconds':0.00000001};self.cfg['tasks']=[{'name':'collector','type':'execute','executable':sys.executable,'arguments':['-c','raise RuntimeError()']},{'name':'file','type':'file','source':str(file),'destination':'x'}]
        engine=self.engine();self.assertEqual(engine.run(),4);self.assertEqual(engine.results['collector'].status,'skipped');self.assertEqual(engine.results['file'].status,'success')
    def test_policy_approval_maintenance_and_determinism(self):
        changes=[{'source':'host','path':'jobs.json','property':'$/enabled','change':'modified','before':True,'after':False,'after_sha256':'a'*64}]
        now='2026-01-02T00:00:00+00:00';rows=change_policy.evaluate(changes,as_of=now);self.assertEqual(rows,change_policy.evaluate(changes,as_of=now));self.assertEqual(rows[0]['attention'],'high')
        self.assertEqual(change_policy.evaluate(changes,{'approved_fingerprints':[rows[0]['fingerprint']]},now)[0]['disposition'],'approved')
        policy={'maintenance_windows':[{'start':'2026-01-01T00:00:00+00:00','end':'2026-01-03T00:00:00+00:00'}]};self.assertEqual(change_policy.evaluate(changes,policy,now)[0]['disposition'],'maintenance')
        self.assertEqual(change_policy.evaluate([{**changes[0],'change':'unverified_preserved'}],as_of=now),[])
    def test_determinism_identical_changed_and_partial(self):
        a=self.capture('a');b=self.capture('b');c=self.capture('c',b'changed');bad=self.capture('bad',status='failed')
        self.assertEqual(determinism.compare([a,b])['status'],'identical');self.assertEqual(determinism.compare([a,c])['status'],'changed');self.assertEqual(determinism.compare([a,bad])['status'],'incomplete')
        self.assertEqual(determinism.compare([a,b]),determinism.compare([a,b]))
    def test_determinism_corruption_never_identical(self):
        a=self.capture('a');b=self.capture('b');(b/'db/config').write_text('tamper')
        self.assertEqual(determinism.compare([a,b])['status'],'incomplete')

if __name__=='__main__':unittest.main()
