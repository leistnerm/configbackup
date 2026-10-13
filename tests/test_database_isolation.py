"""Failure, secrecy and preservation contracts; all databases below are fixtures."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('pg_isolation_fixture', ROOT/'collectors/postgresql/collect_postgresql.py')
pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)
from completeness import Coverage
from secret_scan import scan_bytes
import configbackup as cb

class Fixture:
    def __init__(self, args=None, *, fail_globals=False, fail_query='', bad_schema='', option=None):
        self.args = args or pg.parse_args([])
        self.collection_scopes = []; self.telemetry_failures = []
        self.fail_globals = fail_globals; self.fail_query = fail_query; self.bad_schema = bad_schema
        self.option = option if option is not None else ['ldapserver=example.invalid', 'ldapbindpasswd=SYNTHETIC_PASSWORD', 'radiussecrets=SYNTHETIC_RADIUS']
        self.calls = []
    def version(self, name): return 'fixture'
    def psql_scalar(self, database, query): return 'false'
    def globals_dump(self):
        if self.fail_globals: raise RuntimeError('fixture globals unavailable')
        return '-- fixture globals\n'
    def schema_dump(self, database):
        if database == self.bad_schema: raise RuntimeError('fixture schema unavailable')
        return 'CREATE TABLE sample(id int);\n'
    def psql_rows(self, database, query):
        self.calls.append((database, query))
        if self.fail_query and self.fail_query in query: raise RuntimeError('fixture catalog unavailable')
        if 'SELECT rolsuper' in query: return [{'rolsuper': True}]
        if 'current_database() AS maintenance_database' in query: return [{'server_version_num':170000, 'server_version':'17 fixture'}]
        if 'FROM pg_database d' in query: return [{'database_name':db,'is_template':False,'allow_connections':True} for db in ('alpha','beta')]
        if 'FROM pg_hba_file_rules' in query: return [{'line_number':5, 'auth_method':'ldap', 'options':self.option, 'error':None}]
        return []

class DatabaseIsolation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'CONFIGBACKUP_SECTIONS':'{}'}); self.env.start()
    def tearDown(self): self.env.stop(); self.temp.cleanup()
    def collect(self, **fixture_args):
        target = self.root / ('capture-' + str(len(list(self.root.iterdir()))))
        fixture = Fixture(**fixture_args)
        with patch.object(pg, 'PgTools', return_value=fixture), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = pg.main(['--output', str(target), '--skip-object-files'])
        return code, target, Coverage(target), fixture
    def test_hba_options_redacted_before_disk_and_scan(self):
        code, root, coverage, _ = self.collect(); self.assertEqual(code, 0)
        text = (root/'config/pg-hba-file-rules.csv').read_bytes()
        self.assertNotIn(b'SYNTHETIC_PASSWORD', text); self.assertNotIn(b'SYNTHETIC_RADIUS', text)
        self.assertIn(b'ldapserver=example.invalid', text); self.assertIn(b'ldapbindpasswd=<REDACTED>', text)
        self.assertEqual(scan_bytes(text, 'hba.csv'), [])
    def test_gate_catches_auth_options_even_short_and_case_variants(self):
        for value in (b'ldapbindpasswd=x', b'RADIUSSECRETS="ab"', b'radiussecret=rawvalue', b"OPTIONS(password 'fixture')", b"sslpassword 'keypass'"): self.assertTrue(scan_bytes(value, 'fixture'))
        self.assertTrue(scan_bytes(b'''"password" 'fixture' ''', 'fixture'))
        self.assertEqual(scan_bytes(b'ldapbindpasswd=<REDACTED>', 'fixture'), [])
    def test_hba_parse_error_does_not_echo_secret(self):
        result = pg.sanitize_hba_rows([{'options':None, 'error':'bad ldapbindpasswd=SYNTHETIC_PASSWORD'}])
        self.assertNotIn('SYNTHETIC_PASSWORD', json.dumps(result)); self.assertIn('PARSE ERROR', result[0]['error'])
    def test_hba_nonstructured_options_fail_only_hba_scope(self):
        code, root, coverage, _ = self.collect(option='{ldapbindpasswd=unsafe}'); self.assertEqual(code, 6)
        self.assertEqual(next(s['status'] for s in coverage.sections if s['path']=='config/pg-hba-file-rules.csv'), 'failed')
        self.assertIn('databases/alpha/schema.sql', coverage.files)
    def test_globals_failure_does_not_stop_databases_or_other_cluster_queries(self):
        code, root, coverage, _ = self.collect(fail_globals=True); self.assertEqual(code, 6)
        self.assertIn('databases/alpha/schema.sql', coverage.files); self.assertIn('config/pg-hba-file-rules.csv', coverage.files)
        self.assertFalse(coverage.permits_deletion('cluster/globals.sql'))
    def test_failed_catalog_does_not_freeze_other_catalogs(self):
        code, root, coverage, _ = self.collect(fail_query='FROM pg_sequences'); self.assertEqual(code, 6)
        self.assertIn('databases/alpha/inventory/columns.csv', coverage.files)
        self.assertFalse(coverage.permits_deletion('databases/alpha/inventory/sequences.csv')); self.assertIn('databases/beta/schema.sql', coverage.files)
    def test_schema_failure_preserves_whole_database_but_other_database_collects(self):
        code, root, coverage, _ = self.collect(bad_schema='alpha'); self.assertEqual(code, 6)
        self.assertFalse(any(x.startswith('databases/alpha/') for x in coverage.files))
        self.assertFalse(coverage.permits_deletion('databases/alpha/old.sql')); self.assertIn('databases/beta/schema.sql', coverage.files)
    def test_disabled_schema_and_catalog_are_not_queried_or_certified(self):
        with patch.dict(os.environ, {'CONFIGBACKUP_SECTIONS':json.dumps({'schema':False, 'databases/alpha/inventory/sequences.csv':False})}):
            code, root, coverage, fixture = self.collect(bad_schema='alpha')
        self.assertEqual(code, 0); self.assertFalse(coverage.permits_deletion('databases/alpha/schema.sql'))
        self.assertFalse(any(db=='alpha' and 'FROM pg_sequences' in q for db,q in fixture.calls))
    def test_repeated_partial_capture_preserves_git_and_archive(self):
        config = {'backup':{'root':str(self.root/'archive')}, 'git':{'repository':str(self.root/'git')}, 'options':{'log_level':'CRITICAL'},
                  'deletion':{'missing_runs':1,'max_percent_per_run':100}, 'tasks':[{'name':'pg','type':'directory','source':'','destination':'pg','collection_manifest':True,'storage':'both'}]}
        def archive(source):
            config['tasks'][0]['source']=str(source); engine=cb.BackupEngine(cb.ConfigLoader(self.root/'unused')._resolve(config))
            with contextlib.redirect_stdout(io.StringIO()): engine.run()
            return engine
        _, root, _, _ = self.collect(); first=archive(root)
        names=['pg/cluster/globals.sql','pg/databases/alpha/inventory/sequences.csv']
        before={key:copy.deepcopy(first.state.task('pg')['files'][key]) for key in names}
        for _ in range(3):
            _, root, _, _=self.collect(fail_globals=True,fail_query='FROM pg_sequences'); latest=archive(root)
            for key in names:
                self.assertEqual(before[key], latest.state.task('pg')['files'][key]); self.assertTrue((self.root/'git'/key).exists())
    def test_repeat_capture_has_identical_certified_configuration(self):
        _, first, a, _=self.collect(); _, second, b, _=self.collect(); self.assertEqual(a.hashes,b.hashes)


class ExtensionAdapters(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0,str(ROOT/'collectors/postgresql'))
        import extension_adapters
        self.module=extension_adapters
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':'{}'});self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def test_provider_allowlist_and_identifier_quoting(self):
        with self.assertRaises(ValueError):self.module.queries('arbitrary_sql','public')
        queries=self.module.queries('pg_partman','strange"; DROP TABLE important;--')
        self.assertIn('"strange""; DROP TABLE important;--".part_config',queries['partition-sets'])
    def test_adapter_failure_does_not_freeze_siblings(self):
        class Tools(Fixture):
            def psql_rows(self,db,query):
                if 'FROM pg_extension e' in query:return [{'name':'postgres_fdw','version':'1.1','schema_name':'public'}]
                if 'FROM pg_user_mapping' in query:raise RuntimeError('fixture denied')
                return [{'option_name':'host','option_value':'local.invalid'}]
        tools=Tools();failures=[]
        self.module.collect(tools,'example',self.root,'databases/example',['postgres_fdw'],failures,pg.capture_file,pg.redact_text)
        from completeness import publish
        publish(self.root,tools.collection_scopes);coverage=Coverage(self.root)
        self.assertTrue(failures);self.assertIn('databases/example/extension-adapters/postgres_fdw/servers.csv',coverage.files)
        self.assertFalse(coverage.permits_deletion('databases/example/extension-adapters/postgres_fdw/user-mappings.csv'))
    def test_absent_extension_preserves_previous_files(self):
        tools=Fixture();failures=[]
        self.module.collect(tools,'example',self.root,'databases/example',['postgis'],failures,pg.capture_file,pg.redact_text)
        self.assertEqual(tools.collection_scopes[0]['status'],'not_applicable');self.assertEqual(failures,[])
    def test_disabled_adapter_never_queries(self):
        tools=Fixture();failures=[]
        with patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':'{"extension-adapters":false}'}):
            self.module.collect(tools,'example',self.root,'databases/example',['postgres_fdw'],failures,pg.capture_file,pg.redact_text)
        self.assertEqual(tools.calls,[]);self.assertEqual(tools.collection_scopes[0]['status'],'disabled')
    def test_profile_new_options_validate_and_map(self):
        import database_diagnostics as d
        p={'engine':'postgresql','extension_adapters':['postgres_fdw','pg_partman']};d.validate(p)
        self.assertEqual(d.pg_arguments(p).count('--extension-adapter'),2)
        with self.assertRaises(ValueError):d.validate({'engine':'postgresql','extension_adapters':['arbitrary']})
        d.validate({'engine':'sqlserver','server':'localhost','include_operational_inventory':True})

class SqlFinalizedScopes(unittest.TestCase):
    def test_actual_powershell_finalizer_and_legacy_parent_archive_migration(self):
        import shutil
        import subprocess
        from completeness import publish,section
        ps=shutil.which('pwsh') or '/usr/local/microsoft/powershell/7-preview/pwsh'
        if not Path(ps).is_file():self.skipTest('PowerShell unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);output=root/'scopes.json'
            cp=subprocess.run([ps,'-NoProfile','-File',str(ROOT/'tests/integration/Test-DatabaseScopes.ps1'),'-OutputFile',str(output)],capture_output=True,text=True)
            self.assertEqual(cp.returncode,0,cp.stdout+cp.stderr)
            result=json.loads(output.read_text(encoding='utf-8-sig'))
            source=root/'source';db=source/'databases/demo';(db/'schema').mkdir(parents=True);(db/'catalog').mkdir()
            for name in ('schema/table.sql','database.json','catalog/good.csv','catalog/denied.csv'):(db/name).write_text('original')
            cfg={'backup':{'root':str(root/'archive')},'git':{'repository':str(root/'git')},'options':{'log_level':'CRITICAL'},
                 'deletion':{'missing_runs':1,'max_percent_per_run':100},'tasks':[{'name':'sql','type':'directory','source':str(source),'destination':'sql','collection_manifest':True,'storage':'both'}]}
            def archive():
                engine=cb.BackupEngine(cb.ConfigLoader(root/'unused')._resolve(cfg))
                with contextlib.redirect_stdout(io.StringIO()):engine.run()
                return engine
            publish(source,[section(source,'databases/demo')]);first=archive()
            frozen={k:copy.deepcopy(v) for k,v in first.state.task('sql')['files'].items() if '/schema/' in k or k.endswith('denied.csv')}
            (db/'schema/table.sql').unlink();(db/'catalog/denied.csv').unlink();(db/'catalog/good.csv').write_text('updated')
            for _ in range(3):
                publish(source,[section(source,s['path'],s['status'],s['error']) for s in result['disabled']]);latest=archive()
                for k,v in frozen.items():self.assertEqual(v,latest.state.task('sql')['files'][k]);self.assertEqual((root/'git'/k).read_text(),'original')
            self.assertEqual((root/'git/sql/databases/demo/catalog/good.csv').read_text(),'updated')

if __name__=='__main__': unittest.main()
