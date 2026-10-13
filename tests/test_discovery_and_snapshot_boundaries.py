import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from configbackup_lib.git import change_policy
import configbackup_lib.engine.configbackup as cb
from configbackup_lib.engine import discovery_review
from configbackup_lib.recovery import recovery_assets
from configbackup_lib.recovery import recovery_maintenance
from configbackup_lib.reporting import reporting
import configbackup_lib.engine.schedule_discovery as sd
from configbackup_lib.recovery.transaction_journal import Journal

class DiscoveryAndSnapshotBoundaryTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
    def tearDown(self):self.tmp.cleanup()
    def test_windows_services_probe_and_malformed_task_row_continue(self):
        xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>C:\\Tools\\job.exe</Command><Arguments>--config="C:\\Job Files\\x.ini"</Arguments></Exec></Actions></Task>'
        def execute(args,**kw):
            script=args[-1]
            data=[{'Name':'Fixture','PathName':'"C:\\App\\worker.exe" --config "C:\\App\\x.yaml"'}] if 'Win32_Service' in script else [7,{'name':'good','xml':xml}]
            return subprocess.CompletedProcess(args,0,json.dumps(data),'')
        report=sd.discover(system='windows',execute=execute,which=lambda _: 'pwsh',include_services=True)
        self.assertEqual(report['jobs_inspected'],2);self.assertEqual(len(report['candidates']),2);self.assertTrue(report['probe_errors'])
    def test_systemd_user_manager_failure_keeps_system_service(self):
        calls=[]
        def execute(args,**kw):
            calls.append(args)
            if '--user' in args:return subprocess.CompletedProcess(args,1,'','unavailable')
            if '--type=service' in args:return subprocess.CompletedProcess(args,0,'fixture.service enabled\n','')
            if 'cat' in args:return subprocess.CompletedProcess(args,0,'[Service]\nWorkingDirectory=/opt/app\nExecStart=/opt/app/run --config /opt/app/config.yaml\n','')
            return subprocess.CompletedProcess(args,0,'','')
        report=sd.discover(system='linux',execute=execute,which=lambda n:'systemctl' if n=='systemctl' else None,roots=[],include_services=True,user_timers=True)
        self.assertEqual(report['jobs_inspected'],1);self.assertTrue(any('--user' in c for c in calls));self.assertTrue(report['probe_errors'])
    def test_saved_definitions_never_invoke_service_commands(self):
        file=self.root/'cron';file.write_text('* * * * * /opt/app/job.py')
        report=sd.discover(definitions=[file],system='windows',include_services=True,execute=lambda *a,**kw:self.fail('No live probing'))
        self.assertEqual(report['jobs_inspected'],1)
    def test_quote_empty_unbalanced_and_trailing_slashes(self):
        self.assertEqual(sd.split_command('"" "abc"',True),['','abc'])
        with self.assertRaises(ValueError):sd.split_command('"unfinished',True)
        self.assertEqual(sd.split_command(r'--dir "C:\Data\\"',True),['--dir','C:\\Data\\'])
    def test_fileswitch_string_boolean_rejected(self):
        config=self.root/'config.yaml';config.write_text('tasks: []')
        with self.assertRaises(ValueError):recovery_assets.assets({'backup':{'configuration_files':[{'path':'x','enabled':'false'}]}},config)
    def test_inventory_malformed_registry_does_not_hide_main(self):
        config=self.root/'config.yaml';config.write_text('tasks: []');Path(str(config)+'.assets.json').write_text('{bad')
        inv=recovery_assets.inventory({},config);self.assertEqual(inv['status'],'partial');self.assertTrue(any(r['kind']=='main configuration' and r['status']=='available' for r in inv['assets']))
    def test_freshness_current_selected_inputs_missing(self):
        source=self.root/'source';source.mkdir();config=source/'config.yaml';extra=source/'rules';extra.write_text('x');cfg={'backup':{'root':str(self.root/'archive'),'configuration_files':[str(extra)]},'tasks':[]};config.write_text(yaml.safe_dump(cfg))
        result=recovery_maintenance.refresh(cfg,config,self.root/'exports');extra.unlink()
        self.assertEqual(recovery_maintenance.freshness(cfg,config,result['bundle'])['status'],'stale_or_invalid')
    def test_secondary_current_verification_detects_loss(self):
        source=self.root/'source';source.mkdir();config=source/'config.yaml';cfg={'backup':{'root':str(self.root/'archive')},'tasks':[]};config.write_text(yaml.safe_dump(cfg))
        result=recovery_maintenance.refresh(cfg,config,self.root/'exports',self.root/'secondary');(Path(result['secondary']['path'])/'application/configbackup.py').unlink()
        status=recovery_maintenance.status(self.root/'exports');self.assertEqual(status['local']['status'],'passed');self.assertEqual(status['secondary']['current_verification'],'failed')
    def test_coverage_backed_up_and_disabled_group(self):
        source=self.root/'source';source.mkdir();config=source/'config.yaml';cfg={'backup':{'root':str(self.root/'archive')},'tasks':[]};config.write_text(yaml.safe_dump(cfg));engine=cb.BackupEngine(cb.ConfigLoader(config).load());self.assertEqual(engine.run(),0)
        report=discovery_review.coverage(cfg,config,state=engine.state.data);row=next(r for r in report['items'] if r['item']==str(config));self.assertEqual(row['status'],'backed up (recorded hash matches)')
        cfg['backup']['include_configuration']=False;report=discovery_review.coverage(cfg,config,state=engine.state.data);self.assertEqual(next(r for r in report['items'] if r['item']==str(config))['status'],'disabled')
    def test_policy_hashless_change_cannot_be_permanently_approved(self):
        changes=[{'path':'security.json','change':'modified'}];rows=change_policy.evaluate(changes);policy={'approved_fingerprints':[rows[0]['fingerprint']]}
        self.assertEqual(change_policy.evaluate(changes,policy)[0]['disposition'],'review')
    def test_invalid_policy_rejected_before_report_cache_updates(self):
        config={'change_policy':{'rules':[{'id':'x','enabled':'false'}]}}
        with self.assertRaises(ValueError):reporting.run_reports(config,self.root/'reports')
        self.assertFalse((self.root/'reports').exists())
    def test_report_attention_and_cost_fixed_time_deterministic(self):
        config={'as_of':'2026-01-01T00:00:00+00:00'};state={'tasks':{'id':{'display_name':'job','files':{},'cost_history':[{'status':'failed','duration_seconds':2}], 'scopes':{'db':{'status':'failed','cost':{'bytes':10,'query_seconds':None}}}}}}
        changes=[{'path':'security.json','change':'modified'}];a=reporting.run_reports(config,self.root/'one',state,changes);b=reporting.run_reports(config,self.root/'two',state,changes)
        self.assertEqual((self.root/'one/report.json').read_bytes(),(self.root/'two/report.json').read_bytes());self.assertEqual((self.root/'one/dashboard.html').read_bytes(),(self.root/'two/dashboard.html').read_bytes());data=json.loads((self.root/'one/report.json').read_text());self.assertTrue(data['attention']);self.assertTrue(data['quality'])
    def test_duplicate_stable_ids_and_invalid_budgets_rejected(self):
        base={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'tasks':[{'name':'a','state_id':'same','type':'file','source':'x'},{'name':'b','state_id':'same','type':'file','source':'y'}]}
        with self.assertRaises(cb.ConfigError):cb.ConfigLoader(Path('unused'))._resolve(base)
        base['tasks']=[];base['options']={'run_budget_seconds':float('nan')}
        with self.assertRaises(cb.ConfigError):cb.ConfigLoader(Path('unused'))._resolve(base)
    def test_retired_identity_does_not_collect_or_prune(self):
        source=self.root/'source';source.write_text('x');cfg={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'tasks':[{'name':'x','type':'file','source':str(source)}]};engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(cfg));engine.state.task('x')['retired']=True;engine.state.save();self.assertEqual(engine.run(),0);self.assertEqual(engine.results['x'].status,'disabled')
    def test_poisoned_journal_cannot_silently_skip_persistence(self):
        archive=self.root/'archive';archive.mkdir();target=archive/'x';target.write_text('x');j=Journal(archive/'journal',[archive]);j.begin()
        with patch('configbackup_lib.recovery.transaction_journal.atomic',side_effect=OSError(28,'full')):
            with self.assertRaises(OSError):j.remember(target)
        with self.assertRaises(OSError):j.remember(target)
        with self.assertRaises(OSError):j.commit()
        self.assertEqual(target.read_text(),'x')

    def test_postgres_restriction_keys_are_comparison_only(self):
        from configbackup_lib.engine.completeness import publish,section
        from configbackup_lib.engine import determinism
        paths=[]
        for name,key in [('one','ABC'),('two','DEF')]:
            path=self.root/name;path.mkdir();(path/'schema.sql').write_text('\\restrict '+key+'\nCREATE TABLE x(a int);\n\\unrestrict '+key+'\n');publish(path,[section(path,'schema.sql')]);paths.append(path)
        report=determinism.compare(paths);self.assertEqual(report['status'],'representation_only');self.assertNotEqual((paths[0]/'schema.sql').read_bytes(),(paths[1]/'schema.sql').read_bytes())
        (paths[1]/'schema.sql').write_text('\\restrict DEF\nCREATE TABLE x(a text);\n\\unrestrict DEF\n');publish(paths[1],[section(paths[1],'schema.sql')]);self.assertEqual(determinism.compare(paths)['status'],'changed')
    def test_fully_disabled_capture_is_not_a_determinism_pass(self):
        from configbackup_lib.engine.completeness import publish
        from configbackup_lib.engine import determinism
        paths=[]
        for name in ('one','two'):
            path=self.root/name;path.mkdir();publish(path,[{'path':'db','status':'disabled','files':{}}]);paths.append(path)
        self.assertEqual(determinism.compare(paths)['status'],'incomplete')

if __name__=='__main__':unittest.main()
