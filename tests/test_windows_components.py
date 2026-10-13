import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'collectors/system'))
import windows_components as wc
from configbackup_lib.engine.completeness import Coverage,publish
import configbackup_lib.engine.configbackup as cb

class ComponentsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':'{}'});self.env.start()
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def record(self,component='iis',key='sites/example/settings',data=None,status='complete'):
        return {'scope':'windows-components/'+component+'/'+key,'status':status,'data':{'name':'example'} if data is None else data}
    def collect(self,records,component='iis'):
        def run(args,**kw):
            self.assertEqual(kw,{'capture_output':True,'timeout':300,'shell':False})
            self.assertIn('-File',args);self.assertEqual(args[-2:],['-Component',component])
            return subprocess.CompletedProcess(args,0,json.dumps({'schema_version':1,'component':component,'records':records}).encode(),b'')
        return wc.collect(self.root,[component],windows=True,powershell='fake',execute=run)
    def test_stable_top_level_rows_preserve_nested_order(self):
        a=self.record(data=[{'id':'z','options':[2,1]},{'id':'a','options':[1,2]}]);a['unordered_rows']=True
        wc.write_records(self.root,'iis',[a]);p=self.root/a['scope']/'configuration.json';before=p.read_bytes()
        a['data'].reverse();wc.write_records(self.root,'iis',[a]);self.assertEqual(p.read_bytes(),before)
        a['data'][0]['options'].reverse();wc.write_records(self.root,'iis',[a]);self.assertNotEqual(p.read_bytes(),before)
    def test_one_failed_query_does_not_hide_healthy_query(self):
        a=self.record();b=self.record(key='sites/other/settings',status='failed');b['error']='Access denied'
        scopes,failures=self.collect([a,b]);publish(self.root,scopes);coverage=Coverage(self.root)
        self.assertEqual(len(failures),1);self.assertEqual(len(coverage.files),1);self.assertFalse(coverage.permits_deletion(b['scope']+'/old.json'))
    def test_component_failure_isolated_from_next_component(self):
        def run(args,**kw):
            if args[-1]=='iis':raise subprocess.TimeoutExpired(args,300)
            return subprocess.CompletedProcess(args,0,json.dumps({'schema_version':1,'component':'dns','records':[self.record(component='dns')]}).encode(),b'')
        scopes,failures=wc.collect(self.root,['iis','dns'],windows=True,powershell='fake',execute=run)
        self.assertEqual([r['status'] for r in scopes],['failed','complete']);publish(self.root,scopes);Coverage(self.root)
    def test_disabled_parent_never_invokes_reader(self):
        with patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':'{"windows-components/iis":false}'}):
            scopes,failures=wc.collect(self.root,['iis'],windows=True,execute=lambda *a,**k:self.fail('invoked'))
        self.assertEqual(scopes[0]['status'],'disabled');self.assertFalse(failures)
    def test_disabled_child_not_certified(self):
        a=self.record()
        with patch.dict(os.environ,{'CONFIGBACKUP_SECTIONS':json.dumps({a['scope']:False})}):
            scopes,failures=self.collect([a])
        self.assertEqual(scopes[0]['status'],'disabled');self.assertFalse((self.root/a['scope']).exists())
    def test_non_windows_fails_selected_only(self):
        scopes,failures=wc.collect(self.root,['dns'],windows=False)
        self.assertEqual(scopes[0]['status'],'failed');self.assertEqual(len(failures),1)
    def test_empty_selection_does_nothing(self):
        scopes,failures=wc.collect(self.root,[]);self.assertEqual((scopes,failures),([],[]));self.assertEqual(list(self.root.iterdir()),[])
    def test_invalid_envelopes_fail_closed(self):
        for payload in ({}, {'schema_version':1,'component':'other','records':[]}, {'schema_version':1,'component':'iis','records':[]}):
            def run(*a,**k):return subprocess.CompletedProcess([],0,json.dumps(payload).encode(),b'')
            scopes,failures=wc.collect(self.root,['iis'],windows=True,powershell='fake',execute=run)
            self.assertTrue(failures);self.assertEqual(scopes[0]['status'],'failed')
    def test_unsafe_duplicate_overlapping_scopes_rejected(self):
        cases=[[self.record(key='../escape')],[self.record(),self.record()],[self.record(key='sites'),self.record(key='sites/child')],[self.record(component='dns')]]
        for records in cases:
            with self.assertRaises(ValueError):wc.write_records(self.root,'iis',records)
        self.assertEqual(list(self.root.iterdir()),[])
    def test_null_payload_and_empty_rows_remain_explicit(self):
        a=self.record(data=[]);a['unordered_rows']=True
        scopes,failures=self.collect([a]);self.assertFalse(failures)
        self.assertEqual(json.loads((self.root/a['scope']/'configuration.json').read_text()),[])
    def test_serialization_failure_is_per_scope(self):
        scopes,failures=wc.write_records(self.root,'iis',[self.record(data=float('nan')),self.record(key='sites/good')])
        self.assertEqual([s['status'] for s in scopes],['failed','complete']);self.assertEqual(len(failures),1)
    def test_archive_preserves_failed_and_disappeared_objects(self):
        source=self.root/'source';source.mkdir()
        config={'backup':{'root':str(self.root/'archive')},'git':{'repository':str(self.root/'git')},'options':{'log_level':'CRITICAL'},'deletion':{'missing_runs':1,'max_percent_per_run':100},'tasks':[{'name':'components','type':'directory','source':str(source),'destination':'host','collection_manifest':True,'storage':'both'}]}
        def engine():return cb.BackupEngine(cb.ConfigLoader(self.root/'unused')._resolve(config))
        def run(records):
            scopes,failures=wc.write_records(source,'iis',records);publish(source,scopes);e=engine()
            with contextlib.redirect_stdout(io.StringIO()):code=e.run()
            return e,code
        a=self.record(key='sites/healthy');b=self.record(key='sites/failed');c=self.record(key='sites/disappeared')
        first,code=run([a,b,c]);self.assertEqual(code,0)
        state=json.loads(json.dumps(first.state.task('components')['files']))
        a['data']={'name':'updated'};b['status']='failed';b['error']='denied'
        second,code=run([a,b]);self.assertEqual(code,4)
        for item in (b,c):
            name='host/'+item['scope']+'/configuration.json'
            self.assertEqual(second.state.task('components')['files'][name],state[name])
            self.assertEqual(json.loads((self.root/'git'/name).read_text()),{'name':'example'})
        self.assertNotEqual(second.state.task('components')['files']['host/'+a['scope']+'/configuration.json'],state['host/'+a['scope']+'/configuration.json'])
        self.assertEqual(json.loads((self.root/'git/host'/a['scope']/'configuration.json').read_text()),{'name':'updated'})
    def test_wizard_only_enables_selected_components(self):
        from configbackup_lib.ui import configure
        answers=iter(['host','python','yes','no','yes','no'])
        with contextlib.redirect_stdout(io.StringIO()):tasks=configure.template_tasks('System',input_fn=lambda _:next(answers))
        self.assertIn('iis',tasks[0]['arguments']);self.assertIn('dhcp',tasks[0]['arguments']);self.assertNotIn('dns',tasks[0]['arguments']);self.assertNotIn('hyperv',tasks[0]['arguments'])
        self.assertEqual(tasks[0]['sections'],{'windows-components/iis':{'enabled':True},'windows-components/dhcp':{'enabled':True}})

if __name__=='__main__':unittest.main(verbosity=2)
