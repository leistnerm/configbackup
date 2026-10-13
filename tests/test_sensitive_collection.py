"""Explicit sensitive-section consent, publication boundaries and failure isolation."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import configbackup as cb
import sensitive_data as policy
from completeness import publish, section, Coverage
from secret_scan import scan_bytes
from test_database_isolation import Fixture, pg
import reporting

SCOPE='instance/sensitive-ssis/environment-variables.csv'
SECRET='SYNTHETIC-PRIVATE-RECOVERY-VALUE'

class SensitiveCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.env=patch.dict(os.environ,{policy.ENVIRONMENT:'[]','CONFIGBACKUP_SECTIONS':'{}'});self.env.start()
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def config(self, approve=False):
        git={'repository':str(self.root/'git')}
        if approve:git['sensitive_data']={'enabled':True,'private_repository':True,'sections':[{'task':'snapshot','section':SCOPE}]}
        return {'backup':{'root':str(self.root/'archive'),'include_configuration':False},'options':{'log_level':'CRITICAL'},'git':git,
                'tasks':[{'name':'snapshot','type':'directory','source':str(self.root/'source'),'destination':'server','storage':'both','collection_manifest':True}]}
    def capture(self, secret=SECRET, status='complete', healthy='normal'):
        root=self.root/'source';(root/SCOPE).parent.mkdir(parents=True,exist_ok=True)
        (root/SCOPE).write_text('password='+secret+'\n')
        good=root/'instance/ssis/catalog.json';good.parent.mkdir(parents=True,exist_ok=True);good.write_text(json.dumps({'state':healthy}))
        entry=section(root,SCOPE,status,('failure '+SECRET) if status=='failed' else '');entry['sensitive']=True
        publish(root,[entry,section(root,'instance/ssis')])
    def run_engine(self, config):
        engine=cb.BackupEngine(cb.ConfigLoader(self.root/'config.yaml')._resolve(config))
        try:
            with contextlib.redirect_stdout(io.StringIO()):return engine.run()
        finally:engine._close_logging()
    def test_exact_opt_in_and_strict_validation(self):
        self.assertFalse(policy.requested('cluster/globals.sql'))
        for value in (True,'cluster/globals.sql',['cluster/*'],['cluster'],['cluster/globals.sql']*2):
            with self.assertRaises(ValueError):policy.validate_sections(value)
        with patch.dict(os.environ,{policy.ENVIRONMENT:'["cluster/globals.sql"]'}):
            self.assertTrue(policy.requested('cluster/globals.sql'));self.assertFalse(policy.requested('cluster/roles.csv'))
    def test_git_requires_independent_explicit_approval(self):
        for value in ({'enabled':'true'},{'enabled':True},{'enabled':True,'private_repository':True},{'sections':[{'task':'snapshot','section':'instance/*'}]}):
            with self.assertRaises(ValueError):policy.validate_git({'sensitive_data':value})
        self.assertFalse(policy.git_approved({},'snapshot',SCOPE))
    def test_collection_without_git_approval_preserves_sensitive_scope_and_collects_normal(self):
        self.capture();self.assertNotEqual(self.run_engine(self.config()),0)
        self.assertFalse((self.root/'git/server'/SCOPE).exists())
        self.assertTrue((self.root/'git/server/instance/ssis/catalog.json').exists())
    def test_approved_sensitive_snapshot_repeat_and_failed_recovery_leave_normal_ssis_working(self):
        self.capture();cfg=self.config(True);self.assertEqual(self.run_engine(cfg),0)
        path=self.root/'git/server'/SCOPE;before=path.read_bytes()
        head=lambda:subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root/'git')
        first=head();self.assertEqual(self.run_engine(cfg),0);self.assertEqual(first,head())
        self.capture('SHOULD-NOT-REPLACE',status='failed',healthy='new normal SSIS')
        self.assertNotEqual(self.run_engine(cfg),0);self.assertEqual(before,path.read_bytes())
        self.assertIn('new normal SSIS',(self.root/'git/server/instance/ssis/catalog.json').read_text());self.assertNotEqual(first,head())
    def test_revoking_git_approval_does_not_publish_retained_sensitive_bytes(self):
        self.capture();cfg=self.config(True);self.assertEqual(self.run_engine(cfg),0)
        before=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root/'git')
        self.capture(status='failed',healthy='changed');self.assertNotEqual(self.run_engine(self.config()),0)
        self.assertEqual(before,subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root/'git'))
    def test_unrelated_secret_is_still_blocked(self):
        self.capture();root=self.root/'source';(root/'other.sql').write_text("PASSWORD = 'UNAPPROVED-SYNTHETIC'")
        data=json.loads((root/'collection-manifest.json').read_text());publish(root,data['sections']+[section(root,'other.sql')])
        self.assertNotEqual(self.run_engine(self.config(True)),0)
        result=subprocess.run(['git','rev-parse','HEAD'],cwd=self.root/'git',capture_output=True);self.assertNotEqual(result.returncode,0)
    def test_sensitive_report_preview_never_exposes_opaque_values(self):
        self.capture();result=reporting.capture({'id':'demo','path':str(self.root/'source'),'include_values':True},{'include_values':True})
        record=result['files'][SCOPE];self.assertEqual(record['preview_status'],'sensitive_section');self.assertNotIn(SECRET,json.dumps(result))
    def test_sensitive_manifests_require_new_consumer_version(self):
        self.capture();root=self.root/'source'
        self.assertEqual(json.loads((root/'collection-manifest.json').read_text())['schema_version'],2)
        self.assertIn(SCOPE,Coverage(root).complete)
        publish(root,[section(root,'instance/ssis')])
        self.assertEqual(json.loads((root/'collection-manifest.json').read_text())['schema_version'],1)

    def test_nonboolean_manifest_marker_rejected(self):
        self.capture();root=self.root/'source';data=json.loads((root/'collection-manifest.json').read_text());data['sections'][0]['sensitive']='false';(root/'collection-manifest.json').write_text(json.dumps(data))
        with self.assertRaises(ValueError):Coverage(root)
    def test_allow_sensitive_keeps_archive_scan_failure_closed(self):
        data=b"password='SYNTHETIC-SECRET'"
        self.assertTrue(scan_bytes(data,'fixture'));self.assertFalse(scan_bytes(data,'fixture',allow_sensitive=True))
        self.assertTrue(scan_bytes(b'PK\x03\x04broken','fixture',allow_sensitive=True))
        for data in (b'PASSWORD = 0x0200123456789abcdef0',b"SCRAM-SHA-256$4096:fixture"):
            self.assertTrue(scan_bytes(data,'hash'))
    def test_git_destination_checks_fetch_push_urls_and_github_privacy(self):
        from types import SimpleNamespace
        class Engine:
            git_cfg={'sensitive_data':{'enabled':True,'private_repository':True,'sections':[{'task':'snapshot','section':SCOPE}],'allowed_remotes':['https://github.com/example/private.git']}}
            git_mode='direct'
            def _git_command(self,args):return SimpleNamespace(stdout='origin\n' if args==['remote'] else 'https://github.com/example/private.git\n')
            def _gh_command(self,args):return SimpleNamespace(stdout='{"private":true}')
        engine=Engine();policy.check_git_destination(engine)
        with patch.object(engine,'_gh_command',return_value=SimpleNamespace(stdout='{"private":false}')):
            with self.assertRaisesRegex(RuntimeError,'private'):policy.check_git_destination(engine)
        with patch.object(engine,'_git_command',side_effect=lambda args:SimpleNamespace(stdout='origin\n' if args==['remote'] else 'ssh://unapproved/repo\n')):
            with self.assertRaisesRegex(RuntimeError,'unapproved'):policy.check_git_destination(engine)
    def test_postgres_hash_flag_default_and_explicit(self):
        tools=pg.PgTools(pg.parse_args([]));commands=[]
        def run(name,args,**kw):
            commands.append(args);return subprocess.CompletedProcess([],0,'-- fixture','')
        with patch.object(tools,'help',return_value='--no-role-passwords'),patch.object(tools,'run',side_effect=run):
            tools.globals_dump();self.assertIn('--no-role-passwords',commands[-1])
            with patch.dict(os.environ,{policy.ENVIRONMENT:'["cluster/globals.sql"]'}):tools.globals_dump()
            self.assertNotIn('--no-role-passwords',commands[-1])
    def test_postgres_opt_in_is_leaf_scoped_and_failures_mask_values(self):
        root=self.root/'pg';root.mkdir();fixture=Fixture();failures=[]
        with patch.dict(os.environ,{policy.ENVIRONMENT:'["config/pg-hba-file-rules.csv"]'}):
            pg.collect_cluster(fixture,root,'postgres',failures,fixture.args)
        self.assertIn('SYNTHETIC_PASSWORD',(root/'config/pg-hba-file-rules.csv').read_text())
        self.assertTrue(next(s for s in fixture.collection_scopes if s['path']=='config/pg-hba-file-rules.csv')['sensitive'])
        self.assertNotIn('sensitive',next(s for s in fixture.collection_scopes if s['path']=='cluster/globals.sql'))
        def fail():raise ValueError(SECRET)
        with patch.dict(os.environ,{policy.ENVIRONMENT:'["cluster/globals.sql"]'}):pg.capture_file(fixture,root,'cluster/globals.sql',failures,'globals',fail)
        self.assertNotIn(SECRET,json.dumps(failures))
    def test_child_environment_does_not_inherit_ambient_sensitive_permissions(self):
        from database_connections import environment
        env=environment({'engine':'postgresql'},{policy.ENVIRONMENT:'["cluster/globals.sql"]'})
        self.assertEqual(env[policy.ENVIRONMENT],'[]')
    def test_sensitive_permissions_cannot_be_inherited_from_defaults_or_connections(self):
        from config_schema import validate_types
        from database_connections import validate_connections
        with self.assertRaises(ValueError):validate_types({'defaults':{'sensitive_sections':['cluster/globals.sql']}})
        with self.assertRaises(ValueError):validate_connections({'connections':{'pg':{'engine':'postgresql','sensitive_sections':['cluster/globals.sql']}}})
    def test_sensitive_failed_report_drops_cached_value_properties(self):
        self.capture(status='failed')
        previous={'files':{SCOPE:{'path':SCOPE,'scope':SCOPE,'properties':{'password':SECRET}}}}
        result=reporting.capture({'id':'demo','path':str(self.root/'source')},{'include_values':True},previous)
        self.assertNotIn(SECRET,json.dumps(result))
    def test_guided_opt_in_requires_confirmation_and_supports_disable(self):
        config={'tasks':[{'name':'collector','type':'execute'}]}
        with patch('configure.choice',return_value='collector'),patch('configure_features.select_many',return_value=['cluster/globals.sql']):
            policy.collection_wizard(config,lambda _: 'no')
            self.assertNotIn('sensitive_sections',config['tasks'][0])
            policy.collection_wizard(config,lambda _: 'ENABLE SENSITIVE COLLECTION')
            self.assertEqual(config['tasks'][0]['sensitive_sections'],['cluster/globals.sql'])
        config['git']={'sensitive_data':{'enabled':True}}
        with patch('configure.choice',return_value='Disable sensitive publication'):policy.git_wizard(config,lambda _: '')
        self.assertFalse(config['git']['sensitive_data']['enabled'])
    @unittest.skipUnless(shutil.which('pwsh'),'PowerShell unavailable')
    def test_actual_powershell_sensitive_recovery_isolation(self):
        script=Path(__file__).parent/'integration/Test-SensitiveCollection.ps1'
        result=subprocess.run(['pwsh','-NoProfile','-File',str(script)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
