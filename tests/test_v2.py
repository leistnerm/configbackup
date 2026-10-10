import contextlib,copy,datetime as dt,io,json,os,socketserver,subprocess,sys,tempfile,threading,types,unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch
import configbackup as cb
import configure
from completeness import Coverage,publish,section
import monitoring as mon
import notifications as notify
import telemetry
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'collectors/system'));sys.path.insert(0,str(ROOT/'collectors/schedule'))
import storage_inventory as storage
import host_details as host
import windows_settings as win
import analyze_schedules as schedules
import schedule_insights as insights


def engine(base,**changes):
    cfg={'backup':{'root':str(base/'archive')},'options':{'log_level':'CRITICAL'},'git':{'repository':str(base/'git')},
         'deletion':{'missing_runs':1,'max_percent_per_run':100},
         'tasks':[{'name':'source','type':'directory','source':str(base/'source'),'destination':'source','collection_manifest':True,'storage':'both'}]}
    cfg.update(changes);return cb.BackupEngine(cb.ConfigLoader(base/'unused')._resolve(cfg))

def run(e):
    with contextlib.redirect_stdout(io.StringIO()):return e.run()

def seed(base):
    source=base/'source'
    for name in ('a','b'):
        (source/name).mkdir(parents=True);(source/name/'value').write_text('original '+name)
    publish(source,[section(source,'a'),section(source,'b')]);return source

class SafetyV2(unittest.TestCase):
    def test_disabled_dependency_chain_does_not_initialize_git(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td)
            tasks=[{'name':'collector','type':'execute','executable':'missing-tool','enabled':False},
                   {'name':'prepare','type':'execute','executable':'missing-tool','depends_on':['collector']},
                   {'name':'snapshot','type':'directory','source':str(base/'missing'),'storage':'git','depends_on':['prepare'],'run_on_failure':True}]
            e=engine(base,tasks=tasks)
            self.assertFalse(e._uses_git);self.assertEqual(run(e),0)
            self.assertTrue(all(result.status=='disabled' for result in e.results.values()))
            self.assertFalse((base/'git').exists())
    def test_mutation_during_seal_preserves_only_failed_scope(self):
        for mutation in ('replace','remove'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as td:
                base=Path(td);source=seed(base);e=engine(base);self.assertEqual(run(e),0)
                before=copy.deepcopy(e.state.task('source')['files']['source/b/value'])
                (source/'a/value').write_text('new a');publish(source,[section(source,'a'),section(source,'b')]);real=Coverage.seal
                def racing(coverage,destination):
                    if mutation=='remove':(source/'b/value').unlink()
                    else:(source/'b/value').write_text('uncertified')
                    return real(coverage,destination)
                with patch.object(Coverage,'seal',racing):e=engine(base);self.assertEqual(run(e),4)
                self.assertEqual((base/'git/source/a/value').read_text(),'new a');self.assertEqual((base/'git/source/b/value').read_text(),'original b')
                self.assertEqual(e.state.task('source')['files']['source/b/value'],before)
    def test_changes_after_seal_cannot_change_committed_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);source=seed(base);real=Coverage.seal
            def racing(coverage,destination):
                result=real(coverage,destination);(source/'b/value').write_text('raced');return result
            with patch.object(Coverage,'seal',racing):self.assertEqual(run(engine(base)),0)
            self.assertEqual((base/'git/source/b/value').read_text(),'original b')
    def test_staged_secret_scanned_after_worktree_unlinked(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);seed(base);e=engine(base);self.assertEqual(run(e),0)
            file=base/'git/source/a/value';file.write_text('password=non_placeholder_credential')
            subprocess.run(['git','-C',str(base/'git'),'add','.'],check=True,capture_output=True);file.unlink()
            with self.assertRaisesRegex(RuntimeError,'Secret scan'):e._scan_git_secrets()
    def test_failed_push_retried_when_snapshot_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);seed(base);remote=base/'remote.git';subprocess.run(['git','init','--bare',str(remote)],check=True,capture_output=True)
            cfg={'repository':str(base/'git'),'remote_url':str(remote),'push':True,'branch':'main'};e=engine(base,git=cfg);real=e._git_command
            def reject(args,*a,**kw):
                if args[0]=='push':raise RuntimeError('simulated offline remote')
                return real(args,*a,**kw)
            with patch.object(e,'_git_command',side_effect=reject):self.assertEqual(run(e),4)
            commit=subprocess.check_output(['git','-C',str(base/'git'),'rev-parse','HEAD'],text=True).strip()
            self.assertEqual(run(engine(base,git=cfg)),0)
            self.assertEqual(subprocess.check_output(['git','--git-dir',str(remote),'rev-parse','refs/heads/main'],text=True).strip(),commit)
    def test_disabled_scope_preserves_config_and_state(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);source=seed(base);e=engine(base);run(e);before=copy.deepcopy(e.state.task('source')['files']['source/b/value']);(source/'b/value').unlink()
            publish(source,[section(source,'a'),section(source,'b','disabled')]);e=engine(base)
            self.assertEqual(run(e),0);self.assertEqual(e.state.task('source')['files']['source/b/value'],before);self.assertTrue((base/'git/source/b/value').exists())

class MonitorV2(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=mon.Store(Path(self.tmp.name)/'history.db');self.now=1800000000
        self.cfg={'default_rules':False,'consecutive':2,'rules':[{'id':'space','metric':'free','op':'lt','warning':20,'critical':5,'clear':25}]}
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def sample(self,value,offset=0):return mon.metric('free',value,{'host':'test'},self.now+offset,'percent')
    def test_debounce_hysteresis_recovery_and_duplicate_observation(self):
        for offset in (0,1):
            a,e=mon.evaluate(self.store,self.cfg,[self.sample(10)],self.now+offset);self.assertFalse(e)
        a,e=mon.evaluate(self.store,self.cfg,[self.sample(10,10)],self.now+10);self.assertEqual(e[0]['status'],'warning')
        a,e=mon.evaluate(self.store,self.cfg,[self.sample(22,20)],self.now+20);self.assertEqual(a[0]['status'],'warning')
        a,e=mon.evaluate(self.store,self.cfg,[self.sample(30,30)],self.now+30);self.assertEqual(e[0]['event'],'recovered')
    def test_stale_does_not_recover_and_disable_does_not_alert(self):
        cfg={**self.cfg,'consecutive':1};mon.evaluate(self.store,cfg,[self.sample(1)],self.now)
        a,e=mon.evaluate(self.store,cfg,[self.sample(1)],self.now+100000)
        self.assertTrue(a[0]['unknown']);self.assertEqual(a[0]['status'],'critical');self.assertFalse(e)
        cfg['rules']=[{**cfg['rules'][0],'enabled':False}];a,e=mon.evaluate(self.store,cfg,[],self.now+100001);self.assertEqual(a[0]['status'],'disabled');self.assertFalse(e)
    def test_maintenance_releases_alert_when_window_ends(self):
        cfg={**self.cfg,'consecutive':1,'maintenance':[{'start':self.now-5,'end':self.now+10}]}
        a,e=mon.evaluate(self.store,cfg,[self.sample(1)],self.now);self.assertFalse(e);self.assertTrue(a[0]['suppressed'])
        a,e=mon.evaluate(self.store,cfg,[self.sample(1)],self.now+11);self.assertEqual(e[0]['status'],'critical')
    def test_counter_reset_and_growth_minimum(self):
        def count(v,o,reset):return mon.metric('count',v,{},self.now+o,'count','counter',reset)
        self.store.add([count(10,0,'a'),count(20,10,'a')]);self.assertEqual(self.store.derived([count(20,10,'a')],self.now+10)[0]['value'],1)
        self.store.add([count(30,20,'b')]);self.assertFalse(self.store.derived([count(30,20,'b')],self.now+20))
        def size(v,o):return mon.metric('disk.free_bytes',v,{},self.now+o,'bytes')
        self.store.add([size(100000,0),size(80000,10800)]);self.assertFalse(self.store.derived([size(80000,10800)],self.now+10800))
        latest=size(60000,21600);self.store.add([latest]);derived=self.store.derived([latest],self.now+21600)
        self.assertAlmostEqual(next(x['value'] for x in derived if x['metric'].endswith('days_until_full')),.375)
    def test_outbox_persists_failure_and_retries_sanitized(self):
        cfg={'notifications':{'channels':[{'id':'test','type':'webhook','url':'https://example.invalid','immediate':False}]}}
        mon.enqueue(self.store,'test',{'text':'fixture'},self.now)
        with patch.object(mon,'send',side_effect=RuntimeError('token=must_not_be_saved')):errors=mon.notifications(self.store,cfg,[],{},self.now)
        self.assertEqual(errors,[{'channel':'test','error':'RuntimeError'}]);row=self.store.db.execute('SELECT * FROM outbox').fetchone();self.assertEqual(row['status'],'pending');self.assertEqual(row['error'],'RuntimeError')
        with patch.object(mon,'send') as send:mon.notifications(self.store,cfg,[],{},self.now+61);self.assertEqual(send.call_count,1)
        self.assertEqual(self.store.db.execute('SELECT status FROM outbox').fetchone()[0],'sent')
    def test_disabled_source_dismisses_previous_alert_without_recovery(self):
        folder=Path(self.tmp.name);source=folder/'sample.json';data=telemetry.envelope('system','test');data['observed_at']=dt.datetime.fromtimestamp(self.now,dt.timezone.utc).isoformat()
        telemetry.dataset(data,'disk',[{'free_percent':1}],[],{'free_percent':'percent'});telemetry.write_envelope(source,data);cfg={'sources':[{'path':str(source)}],'consecutive':1}
        self.assertTrue(mon.run_monitor(cfg,folder/'report',now=self.now,deliver=False)['alerts']);cfg['sources'][0]['enabled']=False
        summary=mon.run_monitor(cfg,folder/'report',now=self.now+1,deliver=False);self.assertFalse(summary['alerts']);self.assertFalse(summary['events'])
    def test_future_telemetry_rejected(self):
        source=Path(self.tmp.name)/'sample.json';data=telemetry.envelope('system');data['observed_at']=dt.datetime.fromtimestamp(self.now+1000,dt.timezone.utc).isoformat();telemetry.write_envelope(source,data)
        samples,errors=mon.ingest(self.store,{'sources':[str(source)]},self.now);self.assertTrue(errors);self.assertEqual(next(x['value'] for x in samples if x['metric']=='monitor.source.unavailable_count'),1)
    def test_missing_mount_never_substitutes_parent(self):
        rows,failures=telemetry.disk_rows([Path(self.tmp.name)/'missing-volume']);self.assertFalse(rows);self.assertTrue(failures)
    def test_disabled_monitor_never_sends_or_changes_files(self):
        folder=Path(self.tmp.name)/'report'
        with patch.object(mon,'send') as send:self.assertEqual(mon.run_monitor({'enabled':False},folder)['status'],'disabled');send.assert_not_called()
        self.assertFalse(folder.exists())

class NotificationV2(unittest.TestCase):
    def test_summary_escapes_bounds_and_section_switches(self):
        summary={'status':'warning','alerts':[{'value':'<script>bad</script>'}]*200,'capacity':[{'free':'hide'}]}
        plain,rich=notify.render_summary(summary,{'alerts':True,'capacity':{'enabled':False}},max_bytes=2048)
        self.assertNotIn('<script>',rich);self.assertNotIn('hide',rich);self.assertLessEqual(len(rich.encode()),2048)
    def test_insecure_remote_rejected(self):
        with self.assertRaises(ValueError):notify.send({'type':'webhook','url':'http://example.invalid'}, {})
        with self.assertRaises(ValueError):notify.send({'type':'smtp','host':'example.invalid','from':'a@example.invalid','to':['b@example.invalid'],'tls':'none'}, {})
    def test_real_smtp_multipart_and_recovery_message(self):
        messages=[]
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'220 fixture ready\r\n')
                while True:
                    line=self.rfile.readline()
                    if not line:return
                    cmd=line.split()[0].upper()
                    if cmd==b'DATA':
                        self.wfile.write(b'354 send mail\r\n');chunks=[]
                        while True:
                            line=self.rfile.readline()
                            if line==b'.\r\n':break
                            chunks.append(line)
                        messages.append(b''.join(chunks));self.wfile.write(b'250 queued\r\n')
                    elif cmd==b'QUIT':self.wfile.write(b'221 bye\r\n');return
                    else:self.wfile.write(b'250 OK\r\n')
        with socketserver.TCPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:notify.send({'type':'smtp','host':'127.0.0.1','port':server.server_address[1],'tls':'none','allow_insecure_localhost':True,'from':'test@example.invalid','to':['recipient@example.invalid']}, {'text':'recovered space','summary':{'status':'healthy','alerts':[]},'subject':'Fixture'})
            finally:server.shutdown();thread.join()
        msg=BytesParser(policy=policy.default).parsebytes(messages[0]);self.assertEqual(msg.get_content_type(),'multipart/alternative')
        for kind in ('plain','html'):self.assertIn('recovered space',msg.get_body(preferencelist=(kind,)).get_content())

class ScheduleV2(unittest.TestCase):
    def test_running_sql_agent_start_time_is_observed(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td);(path/'telemetry/agent').mkdir(parents=True)
            (path/'telemetry/agent/running-jobs.csv').write_text('job_name,start_time\none,2026-10-09T12:00:00\n')
            job=self.job();job.name='one'
            rows=schedules.load_observations(path,job)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'running');self.assertEqual(rows[0]['start'],'2026-10-09T12:00:00')
    def test_disabled_analysis_preserves_prior_reports(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td);report=path/'execution-analysis.csv';report.write_text('prior result')
            config=path/'config.yaml';config.write_text('analysis:\n  enabled: false\n')
            args=types.SimpleNamespace(output=str(path),config=str(config),start='2026-10-09',days=1,system=None,sql=None,postgresql=None)
            with patch.object(schedules,'analyze_insights') as analyze:
                result=schedules.report(args);analyze.assert_not_called()
            self.assertEqual(result['analysis_status'],'disabled');self.assertEqual(report.read_text(),'prior result')
            self.assertIn('execution-analysis',json.loads((path/'report-status.json').read_text())['disabled'])
    def job(self,key='host|sql|one'):return types.SimpleNamespace(key=key,source='sql_agent',durations=[10,20],override_duration=None,observed=[],host='host',timezone='UTC',heartbeats=[])
    def test_missed_requires_complete_history(self):
        job=self.job();timeline=[{'job_id':job.key,'start':'2026-10-09T12:00:00'}];now=dt.datetime(2026,10,9,14)
        self.assertEqual(insights.executions(timeline,{job.key:job},{},now)[0]['status'],'unobserved')
        cfg={'history_coverage':{'*':{'complete':True,'start':'2026-10-09T00:00:00','end':'2026-10-10T00:00:00'}}}
        self.assertEqual(insights.executions(timeline,{job.key:job},cfg,now)[0]['status'],'missed')
        job.observed=[{'start':'2026-10-09T12:10:00','end':'2026-10-09T12:20:00','status':'1'}];self.assertEqual(insights.executions(timeline,{job.key:job},cfg,now)[0]['status'],'late')
    def test_failed_runs_not_successful(self):
        self.assertTrue(insights.success('sql_agent','1'));self.assertFalse(insights.success('sql_agent','0'));self.assertFalse(insights.success('sql_agent','3'));self.assertTrue(insights.success('pgagent','s'))
    def test_resource_dependency_deadline_watchdog(self):
        a=self.job('first');b=self.job('second');b.host='another';jobs={'first':a,'second':b};rows=[{'job_id':'first','start':'2026-10-09T12:00:00'},{'job_id':'second','start':'2026-10-09T12:05:00'}]
        cfg={'job_resources':{'*':['disk:shared']},'resource_capacity':{'disk:shared':1},'dependencies':[{'job':'second','requires':['first']}],'deadlines':{'second':'12:15'}}
        conflicts,truncated=insights.conflicts(rows,jobs,cfg);self.assertEqual(conflicts[0]['status'],'capacity_exceeded');self.assertFalse(truncated)
        self.assertEqual(insights.dependencies(rows,jobs,cfg)[0]['status'],'predicted_conflict');self.assertEqual(insights.deadlines(rows,jobs,cfg)[0]['status'],'predicted_late')
        cfg.update(watchdogs=['first'],watchdog_max_silence_minutes={'first':5});self.assertEqual(insights.watchdogs(jobs,cfg,dt.datetime(2026,10,9,13))[0]['status'],'unknown')
        a.heartbeats=[{'observed_at':'2026-10-09T12:00:00','status':'success'}];self.assertEqual(insights.watchdogs(jobs,cfg,dt.datetime(2026,10,9,13))[0]['status'],'stale')
    def test_disabled_report_preserves_prior_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'daily.csv';path.write_text('old snapshot')
            with patch.object(schedules,'REPORT_SETTINGS',{'daily':{'enabled':False}}):schedules.write_csv(path,[{'a':1}],['a'])
            self.assertEqual(path.read_text(),'old snapshot')

class StorageV2(unittest.TestCase):
    def test_zfs_order_and_runtime_fields(self):
        data={'pools':{'tank':{'name':'tank','guid':1,'state':'DEGRADED','txg':5,'vdevs':{'mirror':{'vdev_type':'mirror','children':[{'path':'/dev/z','guid':2,'state':'ONLINE'},{'path':'/dev/a','guid':3,'errors':3}]}}}}}
        actual=storage.zfs_topology(data);self.assertEqual(actual['pools']['tank']['vdevs']['mirror']['children'][0]['path'],'/dev/z')
        for name in ('txg','errors','state'):self.assertNotIn(name,json.dumps(actual))
        with self.assertRaises(ValueError):storage.zfs_topology({'pools':{'tank':{'guid':1}}})
    def test_apfs_usage_excluded_quota_retained(self):
        data={'Containers':[{'ContainerReference':'disk3','CapacityCeiling':100,'CapacityFree':20,'Volumes':[{'DeviceIdentifier':'disk3s1','CapacityQuota':50,'CapacityInUse':10,'Roles':['Data']}]}]}
        actual=storage.mac_config(data);self.assertNotIn('CapacityInUse',json.dumps(actual));self.assertEqual(actual['Containers'][0]['Volumes'][0]['CapacityQuota'],50)
    def test_raid_member_roles_and_ext_geometry(self):
        actual=storage.raid_export('MD_UUID=abc\nMD_LEVEL=raid5\nMD_DEVICES=3\nMD_DEVICE_dev_sda_DEV=/dev/sda\nMD_DEVICE_dev_sda_ROLE=2\nMD_STATE=clean\n')
        self.assertEqual(actual['MD_DEVICE_dev_sda_ROLE'],'2');self.assertNotIn('MD_STATE',actual)
        self.assertEqual(storage.ext_geometry('Filesystem UUID: abc\nBlock count: 100\nFree blocks: 20\nLast mount time: today\n'),{'Filesystem UUID':'abc','Block count':'100'})
    def test_probe_failure_is_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            def fake(cmd):
                if cmd[0]=='zfs':return 'tank\tcompression\tlz4\tlocal\n'
                if cmd[0]=='zpool':raise RuntimeError('permission denied')
                raise storage.Unavailable('missing tool')
            with patch.object(storage,'text',side_effect=fake):scopes,failures=storage.collect(Path(td),'linux')
            by={s['path']:s for s in scopes};self.assertEqual(by['storage/zfs-properties']['status'],'complete');self.assertEqual(by['storage/zfs-topology']['status'],'failed');self.assertEqual(by['storage/lvm']['status'],'not_applicable');self.assertTrue(failures)
            publish(Path(td),scopes);Coverage(td)
    def test_kernel_params_redaction_and_order(self):
        self.assertEqual(host.boot_arguments('a=1 a=2 password=unsafe'),['a=1','a=2','password=<REDACTED>'])
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);(base/'vm').mkdir();(base/'vm/swappiness').write_text('5\n');self.assertEqual(host.sysctls(base=base),{'vm.swappiness':'5'})
            with self.assertRaises(ValueError):host.sysctls(['../../secret'],base)
    def test_mac_module_addresses_removed_versions_retained(self):
        a=' 1 0 0xffff8000 0x5000 0x4000 com.example.driver (1.0) 11111111-2222-3333-4444-555555555555 <4>';b=a.replace('0xffff8000','0xff000000').replace(' 1 0 ',' 8 3 ')
        self.assertEqual(host.mac_extensions(a),host.mac_extensions(b));self.assertNotEqual(host.mac_extensions(a),host.mac_extensions(b.replace('(1.0)','(2.0)')))
        with self.assertRaises(ValueError):host.mac_extensions('unexpected format')

class WindowsAndEditorV2(unittest.TestCase):
    def test_autoruns_both_views(self):
        runs=[x for x in win.selections() if x['path'].endswith('\\Run')];self.assertEqual(len(runs),4);self.assertEqual({x['view'] for x in runs},{'32','64'})
    def test_custom_registry_validation_and_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'keys.yaml';path.write_text('include_defaults: false\nregistry:\n- id: app\n  path: HKLM\\SOFTWARE\\Example\\App\n  values: [Mode]\n  enabled: false\n')
            with patch.object(win.subprocess,'run') as call:
                scopes,failures=win.collect_registry(Path(td)/'out','pwsh',path);call.assert_not_called();self.assertEqual(scopes[0]['status'],'disabled')
            path.write_text('include_defaults: false\nregistry:\n- id: secret\n  path: HKLM\\SECURITY\\Policy\n')
            with self.assertRaises(ValueError):win.selections(path)
    def test_rsop_narrow_timestamp_normalization(self):
        a='<Rsop xmlns="http://www.microsoft.com/GroupPolicy/Rsop"><CreationTime>now</CreationTime><ComputerResults><GPO><ModifiedTime>old</ModifiedTime><Name>Policy</Name></GPO></ComputerResults></Rsop>'
        self.assertEqual(win.normalize_rsop(a),win.normalize_rsop(a.replace('now','later')));self.assertNotEqual(win.normalize_rsop(a),win.normalize_rsop(a.replace('old','changed')))
        with self.assertRaises(ValueError):win.normalize_rsop('<error/>')
    def test_editor_backup_validation_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'monitor.yaml';original=b'# preserve comments\nenabled: false\n';path.write_bytes(original);backup=configure.save({'enabled':True},path,'monitor');self.assertEqual(backup.read_bytes(),original);saved=path.read_bytes()
            with self.assertRaises(ValueError):configure.save({'enabled':'false'},path,'monitor')
            self.assertEqual(path.read_bytes(),saved)
    def test_editor_toggle_list_sections(self):
        item={'sections':['alerts','capacity'],'host':'unchanged'};answers=iter(['3','capacity','4'])
        with contextlib.redirect_stdout(io.StringIO()):configure.edit_mapping(item,lambda _:next(answers))
        self.assertEqual(item['host'],'unchanged');self.assertEqual(item['sections']['capacity'],{'enabled':False})

if __name__=='__main__':unittest.main()
