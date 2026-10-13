import copy, datetime as dt, json, plistlib, subprocess, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
import performance_report as p
import native_recording as n
import configuration_profiles as profiles
import recovery_status
import configbackup as cb
BASE=dt.datetime(2026,10,1,tzinfo=dt.timezone.utc)
def rows(count=12,host='h'):
    return [p.sample(host,'host','cpu.busy_percent','percent',BASE+dt.timedelta(seconds=i*15),BASE+dt.timedelta(seconds=(i+1)*15),20 if i<4 else 95) for i in range(count)]
def jobs():
    return [{'host':'h','job_id':name,'start':(BASE+dt.timedelta(seconds=offset)).isoformat(),'end':(BASE+dt.timedelta(seconds=180)).isoformat()} for name,offset in [('one',0),('two',60)]]
class PerformanceReportTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
    def tearDown(self):self.tmp.cleanup()
    def source(self,name='one',data=None):
        path=self.root/(name+'.json');path.write_text(json.dumps({'samples':data if data is not None else rows()}));return {'id':name,'path':str(path),'type':'normalized_json'}
    def test_overlap_solo_deterministic_chart(self):
        value=p.analyze(rows(),jobs());r=value['overlap_comparisons'][0];self.assertEqual(r['status'],'observed_overlap_with_pressure');self.assertEqual(r['solo_sample_median'],20);self.assertEqual(r['coverage_fraction'],1)
        self.assertEqual(value,p.analyze(list(reversed(rows())),jobs()));doc=p.render_charts(value);self.assertIn('<svg',doc);self.assertIn('performance.json',doc);self.assertEqual(doc,p.render_charts(value))
    def test_half_open_intervals(self):
        facts=jobs();facts[0]['end']=facts[1]['start'];self.assertFalse(p.analyze(rows(),facts)['overlap_comparisons'])
    def test_gaps_coarse_and_unknown_host(self):
        data=[rows()[0],p.sample('h','host','cpu.busy_percent','percent',BASE+dt.timedelta(seconds=60),BASE+dt.timedelta(seconds=500),99)]
        value=p.analyze(data,jobs()+[{**jobs()[0],'host':'unknown'}]);self.assertEqual(len(value['gaps']),2);self.assertTrue(all(r['status']=='insufficient_evidence' for r in value['overlap_comparisons']));self.assertEqual(value['unmatched_job_hosts'],['unknown'])
    def test_duplicate_units_and_overlapping_samples(self):
        self.assertEqual(p.analyze(rows()+rows(),[])['summary'][0]['samples'],12)
        for other in ({**rows()[0],'value':99},{**rows()[0],'unit':'ms'},{**rows()[0],'end':rows()[1]['end']}):
            with self.assertRaises(ValueError):p.analyze(rows()+[other],[])
    def test_perfmon_localized_utf16_clock_offset(self):
        path=self.root/'perf.csv';counter=r'\\HOST\Procesador(_Total)\Uso';path.write_text('"date","'+counter+'"\n"10/01/2026 00:00:00.000","0"\n"10/01/2026 00:00:15.000","50"\n',encoding='utf-16')
        spec={'path':str(path),'type':'perfmon_csv','host':'h','timezone':'UTC','clock_offset_seconds':30,'counters':{counter:{'metric':'cpu.busy_percent','unit':'percent'}}};data=p.load_source(spec);self.assertEqual(data[0]['value'],50);self.assertEqual(p.timestamp(data[0]['start']),BASE+dt.timedelta(seconds=30))
    def test_perfmon_disk_units_missing_counter(self):
        data='"date","\\\\H\\Processor(_Total)\\% Processor Time","\\\\H\\PhysicalDisk(0 C:)\\Avg. Disk sec/Read"\n"10/01/2026 00:00:00","0","0"\n"10/01/2026 00:00:15","-1","0.025"\n'
        values=p.perfmon(data,{'host':'h','timezone':'UTC'});self.assertEqual(len(values),1);self.assertEqual(values[0]['value'],25);self.assertEqual(values[0]['metric'],'disk.read_latency_ms')
    def test_dst_invalid_or_missing_zone(self):
        for text,zone in [('2026-11-01T01:30:00','America/New_York'),('2026-03-08T02:30:00','America/New_York'),('2026-10-01T00:00:00',None)]:
            with self.assertRaises(ValueError):p.timestamp(text,zone)
    def test_sysstat_cpu_memory_disk_units(self):
        event={'timestamp':{'date':'2026-10-01','time':'00:00:15','utc':1,'interval':15},'cpu-load':[{'cpu':'all','idle':20,'iowait':10}],'memory':{'avail':1024,'memused-percent':80},'disk':[{'disk-device':'sda','await':25,'rkB':2,'aqu-sz':3}]}
        values=p.sysstat({'sysstat':{'hosts':[{'nodename':'h','statistics':[event]}]}},{});actual={r['metric']:r['value'] for r in values};self.assertEqual(actual['cpu.busy_percent'],70);self.assertEqual(actual['memory.available_mb'],1);self.assertEqual(actual['disk.read_bytes_per_sec'],2048)
        event['timestamp'].pop('utc');event['timestamp']['tz']='UTC';modern=p.sysstat({'sysstat':{'hosts':[{'nodename':'h','statistics':[event]}]}},{});self.assertEqual(values,modern)
    def test_failed_source_preserves_cache_other_host_advances(self):
        specs=[self.source('one',rows(host='one')),self.source('two',rows(host='two'))];out=self.root/'report';p.run({'sources':specs},out);prior=(out/'.source-cache/one.json').read_bytes();Path(specs[0]['path']).write_text('broken');self.source('two',rows(15,host='two'))
        value=p.run({'sources':specs},out);self.assertEqual(value['status'],'partial');self.assertEqual((out/'.source-cache/one.json').read_bytes(),prior);self.assertEqual(value['summary'][0]['samples'],15)
    def test_conflicting_source_preserves_prior_cache(self):
        specs=[self.source('one'),self.source('two')];out=self.root/'report';p.run({'sources':specs},out);before=(out/'.source-cache/two.json').read_bytes();data=rows();data[0]['value']=88;self.source('two',data);value=p.run({'sources':specs},out)
        self.assertEqual(value['source_status'][1]['status'],'failed');self.assertEqual((out/'.source-cache/two.json').read_bytes(),before);self.assertEqual(value['summary'][0]['samples'],12)
    def test_caps_and_invalid_numbers(self):
        value=p.analyze(rows(),jobs(),{'max_details':2});self.assertEqual(len(value['findings']),2);self.assertEqual(value['truncated']['findings'],6)
        for cfg in ({'max_rows':True},{'minimum_coverage':1.1},{'thresholds':{'cpu.busy_percent':float('nan')}}):
            with self.assertRaises(ValueError):p.validate(cfg)
        spec=self.source()
        with self.assertRaises(ValueError):p.load_source(spec,max_rows=1)
        with self.assertRaises(ValueError):p.load_source(spec,max_bytes=20)
    def test_unknown_job_end_exclusion_and_html_escape(self):
        path=self.root/'jobs.json';path.write_text(json.dumps(jobs()+[{**jobs()[0],'end':None}]))
        value=p.run({'sources':[self.source()],'job_history':[str(path)],'exclude_jobs':['two']},self.root/'report');self.assertFalse(value['overlap_comparisons']);self.assertEqual(value['status'],'partial')
        doc=p.render_charts(p.analyze(rows(host='<script>alert(1)</script>'),[]));self.assertIn('&lt;script&gt;',doc);self.assertNotIn('<h2><script>',doc)
    def test_native_history_ignores_metadata_and_rejects_traversal(self):
        name='sample-one.json';(self.root/name).write_text(json.dumps({'samples':rows()}));(self.root/'recorder.json').write_text('not samples');index=self.root/'history-index.json';index.write_text(json.dumps([{'created':time.time(),'files':[name]}]));spec={'type':'native_history','format':'normalized_json','path':str(index)};self.assertEqual(len(p.load_source(spec)),12)
        index.write_text(json.dumps([{'created':time.time(),'files':['../elsewhere']}]))
        with self.assertRaises(ValueError):p.load_source(spec)
    def test_schedule_history_compatible_watchdogs_excluded(self):
        from collectors.schedule import analyze_schedules as schedule
        from types import SimpleNamespace
        out=self.root/'schedule';out.mkdir();job=SimpleNamespace(observed=[{**r,'duration_minutes':(p.timestamp(r['end'])-p.timestamp(r['start'])).total_seconds()/60,'status':'success'} for r in jobs()],host='h',durations=[1],duration=lambda *a:1)
        schedule.workload_reports(out,[],{'a':job},{'watchdogs':['two']},{},[]);facts,warnings=p.load_jobs(out/'observed-runs.csv');self.assertEqual([r['job_id'] for r in facts],['one']);self.assertFalse(warnings)
class NativeRecordingControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.path=self.root/'config.yaml';self.path.write_text('tasks: []\n');self.config={'performance':{'recording':{'directory':str(self.root/'history')}}};self.commands=[]
    def tearDown(self):self.tmp.cleanup()
    def execute(self,args,**kwargs):self.commands.append(args);return subprocess.CompletedProcess(args,0,'','')
    def apply(self,action,system):
        plan=n.plan(self.config,self.path,action,system,self.root/'home')
        with patch.object(n,'prerequisites',return_value={'missing':[]}):return n.apply(self.config,self.path,action,plan['plan_id'],execute=self.execute,system=system,home=self.root/'home')
    def test_linux_enable_disable_reenable_preserves_history(self):
        self.apply('enable','Linux');path=self.root/'history/sample-keep.json';path.write_text('keep');self.apply('disable','Linux');self.apply('enable','Linux');self.assertEqual(path.read_text(),'keep');self.assertTrue(any(c[:3]==['systemctl','--user','restart'] for c in self.commands));self.assertFalse(any(c[-1]=='sysstat.service' for c in self.commands))
    def test_mac_disable_persists_next_login(self):
        self.apply('enable','Darwin');self.apply('disable','Darwin');path=next((self.root/'home/Library/LaunchAgents').glob('*.plist'));data=plistlib.loads(path.read_bytes());self.assertTrue(data['Disabled']);self.assertFalse(data['RunAtLoad']);self.assertTrue(any(c[:2]==['launchctl','disable'] for c in self.commands));self.apply('enable','Darwin');self.assertFalse(plistlib.loads(path.read_bytes())['Disabled'])
    def test_windows_only_owned_set_and_startup(self):
        value=self.apply('enable','Windows');self.assertEqual(set(value['components']),{'counter','startup'});self.apply('disable','Windows');self.apply('enable','Windows');self.assertTrue(any(c[:2]==['schtasks','/Change'] and c[-1]=='/Disable' for c in self.commands));self.assertTrue(any(c[:3]==['logman','update','counter'] for c in self.commands));self.assertTrue(any(c[0]=='powershell.exe' and '$s.Status' in c[-1] for c in self.commands))
    def test_foreign_new_directory_files_are_not_overwritten(self):
        self.apply('enable','Linux');root=self.root/'changed';root.mkdir();path=root/'recorder.json';path.write_text('foreign');self.config['performance']['recording']['directory']=str(root)
        with self.assertRaises(ValueError):self.apply('enable','Linux')
        self.assertEqual(path.read_text(),'foreign')
    def test_unowned_disable_stale_plan(self):
        with self.assertRaises(ValueError):n.plan(self.config,self.path,'disable','Linux',self.root)
        plan=n.plan(self.config,self.path,'enable','Linux',self.root);self.config['performance']['recording']['interval_seconds']=30
        with self.assertRaises(ValueError):n.apply(self.config,self.path,'enable',plan['plan_id'],system='Linux',home=self.root)
    def test_failed_create_does_not_claim_foreign_native_counter(self):
        plan=n.plan(self.config,self.path,'enable','Windows',self.root)
        with patch.object(n,'prerequisites',return_value={'missing':[]}):
            with self.assertRaises(RuntimeError):n.apply(self.config,self.path,'enable',plan['plan_id'],execute=lambda args,**kw:subprocess.CompletedProcess(args,1),system='Windows',home=self.root)
        saved=json.loads(n.state_path(self.path).read_text());self.assertFalse(saved['owned']);self.assertFalse(saved['components']);self.assertEqual(saved['status'],'failed');self.apply('enable','Windows')
    def test_bad_export_retains_previous_csv(self):
        root=self.root/'history';root.mkdir();prior=root/'perfmon-export.csv';prior.write_text('old')
        def export(args,**kw):(root/'perfmon-export.partial.csv').write_text('bad');return subprocess.CompletedProcess(args,0)
        with patch.object(n.subprocess,'run',side_effect=export):
            with self.assertRaises(ValueError):n.export_perfmon({'directory':str(root),'keep_days':7,'timezone':'UTC'})
        self.assertEqual(prior.read_text(),'old')
    def test_retention_requires_valid_newest_survivor(self):
        records=[]
        for i in range(2):
            name=f'sample-{i}.json';(self.root/name).write_text(json.dumps({'samples':rows()}));records.append({'created':time.time()-864000+i,'files':[name]})
        (self.root/'history-index.json').write_text(json.dumps(records));(self.root/'sample-1.json').write_text('bad');value={'backend':'psutil','keep_days':1,'max_mb':1}
        with self.assertRaises(ValueError):n.retain_history(self.root,value)
        self.assertTrue((self.root/'sample-0.json').exists());(self.root/'sample-1.json').write_text(json.dumps({'samples':rows()}));n.retain_history(self.root,value);self.assertFalse((self.root/'sample-0.json').exists());self.assertTrue((self.root/'sample-1.json').exists())
    def test_systemd_escaping_and_saved_disable_settings(self):
        self.config['performance']['recording']['directory']=str(self.root/'space % $VALUE');plan=n.plan(self.config,self.path,'enable','Linux',self.root);unit=next(v for k,v in plan['files'].items() if k.endswith('.service'));self.assertIn('%% $$VALUE',unit)
        saved=self.apply('enable','Linux');self.config['performance']['recording']['directory']=str(self.root/'edited');plan=n.plan(self.config,self.path,'disable','Linux',self.root/'home');self.assertEqual(plan['settings'],saved['settings'])
class ProfileEvidenceTests(unittest.TestCase):
    def test_profiles_inheritance_origins_and_engine(self):
        cfg={'connections':{'pg':{'engine':'postgresql'}},'collection_profiles':{'enabled':{'engine':'postgresql','collection':{'include_history':False,'history_days':7},'max_scope_bytes':1000}}};task={'connection':'pg','profile':'enabled','collection':{'history_days':2}};runtime=profiles.resolve(task,cfg)
        from config_schema import validate_types
        validate_types(cfg);profiles.validate(cfg);self.assertFalse(runtime['collection']['include_history']);self.assertEqual(runtime['collection']['history_days'],2);origins=profiles.origins(task,runtime,cfg);self.assertEqual(origins['collection.history_days'],'task');self.assertEqual(origins['collection.include_history'],'profile:enabled');cfg['collection_profiles']['enabled']['engine']='sqlserver'
        with self.assertRaises(ValueError):profiles.resolve(task,cfg)
    def test_unused_invalid_profile_rejected(self):
        with self.assertRaises(ValueError):profiles.validate({'collection_profiles':{'unused':{'engine':'postgresql','max_scope_bytes':False}}})
    def test_recovery_evidence_separate_and_hash_bound(self):
        version={'path':'arch/a','hash':'abc','created':'2026-10-01T00:00:00+00:00'};state={'tasks':{'collect':{'scopes':{'database/a':{'logical_scope':'a','status':'failed','last_success':'2026-10-01'}},'files':{'a/schema.sql':{'active':True,'versions':[version],'git_hash':'abc','publication':{'hash':'abc','committed_at':'2026-10-01','published_at':None}}}}}}
        evidence={'files':{'arch/a':{'expected_hash':'abc','status':'verified','checked_at':'2026-10-02'}}};r=recovery_status.rows(state,evidence)[0];self.assertEqual(r['collection_status'],'failed');self.assertEqual(r['git_status'],'committed_locally');self.assertEqual(r['integrity_status'],'verified');self.assertEqual(r['file_restore_status'],'unknown')
        state['tasks']['collect']['files']['a/schema.sql']['versions'].insert(0,{'path':'old','hash':'old','created':'2026-10-01T00:00:00+00:00'});self.assertEqual(recovery_status.rows(state,evidence)[0]['integrity_status'],'verified')
        state['recovery']={'restored_versions':{'arch/a':{'hash':'abc','at':'2026-10-03'}}};self.assertEqual(recovery_status.rows(state)[0]['file_restore_status'],'verified');version['hash']='new';self.assertEqual(recovery_status.rows(state)[0]['file_restore_status'],'unknown')
    def test_optional_report_failure_preserves_backup_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'config.ini';source.write_text('value=1');cfg={'backup':{'root':str(root/'archive'),'include_configuration':False},'options':{'log_level':'CRITICAL'},'tasks':[{'name':'one','type':'file','source':str(source),'destination':'config.ini'}],'performance':{'enabled':True,'sources':[{'id':'bad','type':'normalized_json','path':str(root/'missing')}]}}
            engine=cb.BackupEngine(cb.ConfigLoader(root/'config.yaml')._resolve(cfg));self.assertEqual(engine.run(),0);self.assertEqual(engine.results['performance'].status,'failed');self.assertEqual(engine.results['one'].status,'success')
if __name__=='__main__':unittest.main()
