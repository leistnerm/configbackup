"""Real filesystem/Git engine integration for optional review reports.

Uses local temporary repositories and synthetic files; no remote publication,
provider delivery, OS role changes or browser access.
"""
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import configbackup_lib.engine.configbackup as cb
from configbackup_lib.ui import configure_features
from configbackup_lib.configuration import config_schema
from configbackup_lib.reporting import reporting
from configbackup_lib.recovery import recovery


class ReportingEngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.source.mkdir();self.file=self.source/'app.conf';self.file.write_text('setting=one\n')
        self.config_path=self.root/'config.yaml'
        self.config={'backup':{'root':str(self.root/'archive')},'options':{'log_level':'CRITICAL'},'git':{'repository':str(self.root/'git'),'push':False},'tasks':[{'name':'settings','type':'directory','source':str(self.source),'destination':'settings','storage':'both'}], 'reporting':{'enabled':True,'required':False,'output':'${CONFIGBACKUP_ARCHIVE_ROOT}/_configbackup/review'}}
    def run_engine(self):
        self.config_path.write_text(yaml.safe_dump(self.config,sort_keys=False))
        engine=cb.BackupEngine(cb.ConfigLoader(self.config_path).load())
        with contextlib.redirect_stdout(io.StringIO()):result=engine.run()
        return result,engine
    def git(self,*arguments):return subprocess.check_output(['git','-C',str(self.root/'git'),*arguments],text=True)
    def assert_good_archives(self,engine,expected):
        self.assertEqual(engine.results['settings'].status,'success')
        self.assertEqual(engine.results['configbackup-settings'].status,'success')
        self.assertEqual((self.root/'git/settings/app.conf').read_text(),expected)
        self.assertEqual(self.git('show','HEAD:settings/app.conf'),expected)
        records=engine.state.data['tasks']['settings']['files']['settings/app.conf']['versions']
        latest=max(enumerate(records),key=lambda pair:(pair[1]['created'],pair[0]))[1];self.assertEqual(engine._archive_state_path(latest['path']).read_text(),expected)
        own=engine.state.data['tasks']['configbackup-settings']['files']['_configuration/config.yaml']['versions']
        self.assertTrue(any(engine._archive_state_path(version['path']).read_bytes()==self.config_path.read_bytes() for version in own))
        self.assertFalse(any(name.startswith('_configuration/') for name in self.git('ls-tree','-r','--name-only','HEAD').splitlines()))
    @unittest.skipUnless(shutil.which('git'),'Local Git required')
    def test_real_missing_report_source_does_not_block_archive_git_or_config(self):
        code,engine=self.run_engine();self.assertEqual(code,0);self.assert_good_archives(engine,'setting=one\n');first=self.git('rev-parse','HEAD')
        self.file.write_text('setting=two\n');self.config['reporting']['snapshots']=[{'id':'unavailable-db','path':str(self.root/'missing')}]
        code,engine=self.run_engine();self.assertEqual(code,0);self.assert_good_archives(engine,'setting=two\n');self.assertNotEqual(self.git('rev-parse','HEAD'),first)
        self.assertEqual(engine.results['reporting'].status,'failed');self.assertFalse(engine.results['reporting'].required)
        portal=json.loads((self.root/'archive/_configbackup/review/report.json').read_text());self.assertEqual(portal['status'],'partial')
        manifest=json.loads((engine.internal_root/'runs'/f'{engine.run_id}.json').read_text());self.assertEqual(manifest['tasks']['reporting']['status'],'failed')
    @unittest.skipUnless(shutil.which('git'),'Local Git required')
    def test_required_report_size_failure_preserves_portal_and_committed_work(self):
        code,engine=self.run_engine();self.assertEqual(code,0)
        output=self.root/'archive/_configbackup/review';before={name:(output/name).read_bytes() for name in ('dashboard.html','report.json')}
        self.file.write_text('setting=two\n');self.config['reporting'].update(required=True,max_report_bytes=1)
        code,engine=self.run_engine();self.assertEqual(code,4);self.assert_good_archives(engine,'setting=two\n')
        self.assertEqual(before,{name:(output/name).read_bytes() for name in before});self.assertTrue(engine.results['reporting'].required)
    @unittest.skipUnless(shutil.which('git'),'Local Git required')
    def test_monitor_failure_and_report_exception_are_independent_of_archive(self):
        self.config['monitoring']={'enabled':True,'required':False}
        with patch('configbackup_lib.reporting.monitoring.run_monitor',side_effect=RuntimeError('synthetic monitor failure')),patch('configbackup_lib.reporting.reporting.run_reports',side_effect=OSError('synthetic report failure')) as builder:
            code,engine=self.run_engine()
        builder.assert_called_once();self.assertEqual(code,0);self.assert_good_archives(engine,'setting=one\n')
        self.assertEqual(engine.results['monitoring'].status,'failed');self.assertEqual(engine.results['reporting'].status,'failed')
    @unittest.skipUnless(shutil.which('git'),'Local Git required')
    def test_null_monitoring_does_not_prevent_optional_reporting(self):
        self.config['monitoring']=None
        code,engine=self.run_engine();self.assertEqual(code,0);self.assert_good_archives(engine,'setting=one\n')
        self.assertNotIn('reporting',engine.results)
        data=json.loads((self.root/'archive/_configbackup/review/report.json').read_text())
        self.assertEqual(data['status'],'complete');self.assertTrue(data['restore_preview'])
    @unittest.skipUnless(shutil.which('git'),'Local Git required')
    def test_change_report_failure_keeps_archive_git_monitoring_and_portal_working(self):
        self.config['monitoring']={'enabled':True,'required':False}
        with patch('configbackup_lib.reporting.changes.write_report',side_effect=OSError('synthetic report write failure')):
            code,engine=self.run_engine()
        self.assertEqual(code,0);self.assert_good_archives(engine,'setting=one\n')
        self.assertEqual(engine.results['change-report'].status,'failed');self.assertFalse(engine.results['change-report'].required)
        self.assertNotIn('monitoring',engine.results);self.assertNotIn('reporting',engine.results)
        self.assertTrue((engine.internal_root/'monitoring/summary.json').is_file())
        self.assertTrue((self.root/'archive/_configbackup/review/dashboard.html').is_file())
        manifest=json.loads((engine.internal_root/'runs'/f'{engine.run_id}.json').read_text())
        self.assertEqual(manifest['tasks']['change-report']['status'],'failed')
    def test_restore_plan_tied_timestamp_prefers_latest_active_append(self):
        created='2026-10-11T00:00:00Z'
        old={'path':'old','created':created,'hash':'old'};new={'path':'new','created':created,'hash':'new'}
        state={'tasks':{'settings':{'files':{'settings/app.conf':{'versions':[old,new],'deleted_generations':[{'deleted_at':'2026-10-12T00:00:00Z','versions':[old]}]}}}}}
        before=copy.deepcopy(state)
        for as_of in (None,created):
            plan=recovery.plan_restore(state,as_of=as_of)
            self.assertFalse(plan['verified']);self.assertFalse(plan['errors']);self.assertEqual(plan['records'][0]['version'],new)
        self.assertEqual(state,before)
    def test_dry_run_never_invokes_reporting_or_writes_outputs(self):
        self.config_path.write_text(yaml.safe_dump(self.config));engine=cb.BackupEngine(cb.ConfigLoader(self.config_path).load(),dry_run=True)
        with patch('configbackup_lib.reporting.reporting.run_reports') as builder,contextlib.redirect_stdout(io.StringIO()):self.assertEqual(engine.run(),0)
        builder.assert_not_called();self.assertFalse((self.root/'archive/_configbackup/review').exists())
    def test_reporting_wizard_retains_source_switches_and_selects_exposure(self):
        config={'reporting':{'enabled':False,'snapshots':[{'id':'host','path':'C:/collector','enabled':False}],'schedule_paths':['C:/schedules'],'runtime_paths':[{'id':'host','path':'C:/runtime.json','enabled':False}],'sections':{'exposure':False}}}
        selections=[True,False,False]
        with patch('configbackup_lib.ui.configure.boolean',side_effect=selections),patch('configbackup_lib.ui.configure.ask',side_effect=lambda prompt,default=None,input_fn=None:default or ''),patch('configbackup_lib.ui.configure_features.select_many',return_value=['coverage','exposure']),patch('configbackup_lib.ui.configure.manage'):
            configure_features.reporting_wizard(config)
        value=config['reporting'];reporting.validate(value)
        self.assertTrue(value['enabled']);self.assertEqual(value['output'],'${CONFIGBACKUP_ARCHIVE_ROOT}/_configbackup/review')
        self.assertTrue(value['sections']['exposure']['enabled']);self.assertFalse(value['snapshots'][0]['enabled']);self.assertFalse(value['runtime_paths'][0]['enabled']);self.assertEqual(value['schedule_paths'],[{'path':'C:/schedules','enabled':True}])
    def test_nested_wrong_boolean_report_settings_are_rejected_before_execution(self):
        for report in ({'enabled':'false'},{'include_values':'false'},{'snapshots':[{'id':'x','path':'x','enabled':'false'}]},{'runtime_paths':[{'id':'x','path':'x','enabled':'false'}]},{'sections':{'exposure':{'enabled':'false'}}}):
            config=copy.deepcopy(self.config);config['reporting']=report
            with self.subTest(report=report),self.assertRaises((cb.ConfigError,ValueError)):cb.ConfigLoader(self.config_path)._resolve(config)
    def test_reporting_numeric_limits_reject_strings_and_boolean_counts(self):
        for key in ('max_files','max_rows','max_file_bytes','max_report_bytes','history_days','history_points'):
            for value in ('200',True):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):reporting.validate({key:value})
    def test_reporting_primitive_path_and_timezone_errors_use_configerror(self):
        for report in ({'output':7},{'state':True},{'monitoring_path':['path']},{'snapshots':[{'id':'x','path':{}}]},{'snapshots':[{'id':'x','path':'x','baseline':123}]},{'runtime_paths':[{'id':'x','path':['x']}]},{'timezone':True}):
            config=copy.deepcopy(self.config);config['reporting']=report
            with self.subTest(report=report),self.assertRaises(cb.ConfigError):cb.ConfigLoader(self.config_path)._resolve(config)
    def test_reporting_editor_schema_matches_validator_types(self):
        for name,kind in {'runtime_paths':list,'max_files':int,'max_rows':int,'max_file_bytes':int,'max_report_bytes':int,'history_days':int,'history_points':int,'max_source_age_hours':float}.items():
            with self.subTest(name=name):self.assertIs(config_schema.field_type(name),kind)

if __name__=='__main__':unittest.main()
