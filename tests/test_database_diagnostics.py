import contextlib,io,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import database_diagnostics as diag
import configure
from completeness import publish,section

class DiagnosticTests(unittest.TestCase):
    def test_permission_probe_failure_keeps_connection_success(self):
        from collectors.postgresql.collect_postgresql import PgTools
        with tempfile.TemporaryDirectory() as folder,patch.object(PgTools,'exe',return_value='psql'),patch.object(diag,'process',side_effect=[(0,'{"status":"connected"}'),(1,''),(1,'')]):
            root=Path(folder);diag.postgres(self.profile(),root,{})
            value=json.loads((root/'connection.json').read_text());self.assertEqual(value['status'],'connected');self.assertIn('permission_error',value)
    def profile(self,**extra):return {'name':'pg','engine':'postgresql','host':'localhost','databases':['fixture'],**extra}
    def result(self):return {'status':'incomplete','connection':{'status':'not_tested'},'sections':[],'collector_exit_code':6}
    def test_manifest_results_keep_independent_failures_and_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'snapshot';root.mkdir();(root/'good').mkdir();(root/'good/file').write_text('ok')
            publish(root,[section(root,'good'),section(root,'bad','failed','permission denied'),section(root,'off','disabled'),section(root,'absent','not_applicable')]);(base/'connection.json').write_text('{"status":"connected"}')
            result=self.result();diag.summarize(base,result,{})
            self.assertEqual([r['status'] for r in result['sections']],['available','unavailable','disabled','not_applicable']);self.assertEqual(result['status'],'partial')
    def test_unfinalized_or_corrupt_manifest_never_claims_availability(self):
        for finalized in (True,False):
            with tempfile.TemporaryDirectory() as folder:
                base=Path(folder);root=base/'snapshot';root.mkdir();(root/'value').write_text('before');publish(root,[section(root,'value')],finalized=finalized);(root/'value').write_text('uncertified')
                result=self.result();diag.summarize(base,result,{})
                self.assertEqual(result['status'],'incomplete');self.assertFalse(any(s['status']=='available' for s in result['sections']))
    def test_missing_secret_and_disabled_profile_never_connect(self):
        with patch.object(diag,'process') as process,patch.dict(os.environ,{},clear=True):
            result=diag.diagnose(self.profile(password_env='MISSING_TEST_SECRET'));self.assertEqual(result['connection']['status'],'not_tested')
            result=diag.diagnose(self.profile(enabled=False));self.assertEqual(result['status'],'disabled');process.assert_not_called()
    def test_reject_secret_values_unsafe_profile_and_unknown_options(self):
        for extra in ({'password':'not-allowed'},{'timeout':False},{'sections':{1:False}},{'maintenance_db':'postgresql://user:pass@host/db'},{'password_env':'INVALID-NAME'},{'databases':'all'}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):diag.validate(self.profile(**extra))
    def test_temp_exports_removed_and_secret_values_redacted(self):
        locations=[]
        def collector(profile,scratch,env):
            locations.append(scratch);root=scratch/'snapshot';root.mkdir();publish(root,[section(root,'protected','failed','denied fixture-private-value password=accidental-leak')]);(scratch/'connection.json').write_text('{"status":"connected"}');return 6
        with patch.dict(os.environ,{'TEST_DIAGNOSTIC_PASSWORD':'fixture-private-value'}),patch.object(diag,'postgres',side_effect=collector):result=diag.diagnose(self.profile(password_env='TEST_DIAGNOSTIC_PASSWORD',schema=False))
        self.assertFalse(locations[0].exists());self.assertNotIn('fixture-private-value',json.dumps(result));self.assertNotIn('accidental-leak',json.dumps(result));self.assertTrue(any(r['section']=='native schema extraction' and r['status']=='not_tested' for r in result['sections']))
    def test_runtime_failure_is_not_hidden_by_configuration_success(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'snapshot';(root/'telemetry').mkdir(parents=True);publish(root,[section(root,'empty')]);(root/'telemetry/health.json').write_text(json.dumps({'datasets':{'files':{'rows':[]}},'failures':[{'query':'backup-history','error':'denied'}]}))
            result=self.result();result['collector_exit_code']=0;diag.summarize(base,result,{})
            self.assertEqual(result['status'],'partial');self.assertEqual(sum(x['status']=='unavailable' for x in result['sections']),1)
    def test_timeout_no_partial_manifest_certification_and_cleanup(self):
        locations=[]
        def blocked(profile,scratch,env):locations.append(scratch);raise subprocess.TimeoutExpired(['collector'],1)
        with patch.object(diag,'postgres',side_effect=blocked):result=diag.diagnose(self.profile())
        self.assertEqual(result['status'],'incomplete');self.assertIn('timed out',result['error']);self.assertFalse(locations[0].exists())
    def test_real_timeout_terminates_child_and_returns(self):
        with self.assertRaises(subprocess.TimeoutExpired):diag.process([sys.executable,'-c','import time; time.sleep(30)'],os.environ.copy(),.1)
    def test_report_never_overwrites_and_does_not_contain_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'report.json';diag.write_report({'status':'partial'},path)
            with self.assertRaises(FileExistsError):diag.write_report({'changed':True},path)
            self.assertEqual(json.loads(path.read_text()),{'status':'partial'})
    def test_cli_named_profile_preserves_config_and_returns_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.yaml';path.write_text('database_diagnostics:\n  - name: pg\n    engine: postgresql\n');before=path.read_bytes();result={**self.result(),'access_profile':'read-only','scope_note':'fixture','status':'partial'}
            with patch.object(diag,'diagnose',return_value=result) as run,contextlib.redirect_stdout(io.StringIO()):code=configure.main(['--config',str(path),'--diagnose-database','pg','--diagnostic-report',str(Path(folder)/'report.json')])
            self.assertEqual(code,6);self.assertEqual(path.read_bytes(),before);run.assert_called_once()
    def test_command_arguments_do_not_contain_password(self):
        args=diag.pg_arguments(self.profile(password_env='SECRET',schema=False,include_health=True));self.assertNotIn('SECRET',args);self.assertIn('--skip-schema-dump',args);self.assertIn('--include-health',args)
    def test_interrupted_connection_report_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'connection.json').write_text('{"status":');result=self.result();diag.summarize(root,result,{})
            self.assertEqual(result['connection']['status'],'not_tested');self.assertEqual(result['status'],'incomplete')
