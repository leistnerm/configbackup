import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch,MagicMock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'collectors/schedule')]
import reporting
import monitoring as mon
import notifications
import schedule_insights as insights
import analyze_schedules as schedules
from completeness import publish,section


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.current=self.root/'current';self.current.mkdir();self.out=self.root/'out'
        self.cfg={'as_of':'2026-10-11T12:00:00Z','snapshots':[{'id':'server','path':str(self.current)}]}
    def capture(self,content,failed=False):
        folder=self.current/'scope';folder.mkdir(exist_ok=True)
        (folder/'configuration.json').write_text(json.dumps(content))
        publish(self.current,[section(self.current,'scope','failed' if failed else 'complete','password=do-not-expose')])
    def data(self):return json.loads((self.out/'report.json').read_text())
    def test_semantic_baseline_diff_and_secret_redaction(self):
        self.capture({'a':1,'password':'never-show-secret','name':'private-value'})
        reporting.run_reports(self.cfg,self.out)
        self.capture({'a':2,'password':'changed-secret','name':'different-value'})
        reporting.run_reports(self.cfg,self.out);data=self.data();text=(self.out/'report.json').read_text()
        self.assertTrue(any(r.get('property')=='$/a' and r['before']==1 and r['after']==2 for r in data['changes']))
        for secret in ('never-show-secret','changed-secret','private-value','different-value','password=do-not-expose'):self.assertNotIn(secret,text)
    def test_failed_and_undiscovered_scope_preserves_old_content(self):
        self.capture({'a':1});reporting.run_reports(self.cfg,self.out)
        before=self.data()['explorer'][0]['sha256'];self.capture({'a':99},failed=True)
        reporting.run_reports(self.cfg,self.out);data=self.data()
        self.assertEqual(data['status'],'partial');self.assertEqual(data['explorer'][0]['sha256'],before);self.assertTrue(data['explorer'][0]['preserved'])
        publish(self.current,[]);reporting.run_reports(self.cfg,self.out)
        self.assertEqual(self.data()['explorer'][0]['preservation_reason'],'scope_not_discovered')
    def test_entire_source_integrity_failure_preserves_cached_source(self):
        self.capture({'a':1});reporting.run_reports(self.cfg,self.out)
        before=self.data()['explorer'];(self.current/'scope/configuration.json').write_text('changed without manifest')
        reporting.run_reports(self.cfg,self.out);data=self.data()
        self.assertEqual(data['explorer'][0]['sha256'],before[0]['sha256']);self.assertEqual(data['explorer'][0]['properties'],before[0]['properties'])
        self.assertTrue(data['explorer'][0]['preserved']);self.assertEqual(data['explorer'][0]['preservation_reason'],'failed');self.assertEqual(data['status'],'partial')
    def test_bad_baseline_does_not_replace_previous_comparison(self):
        self.capture({'a':1});reporting.run_reports(self.cfg,self.out)
        cache=(self.out/'.report-cache/snapshot-server.json').read_bytes()
        cfg={**self.cfg,'snapshots':[{**self.cfg['snapshots'][0],'baseline':str(self.root/'absent')}]}
        reporting.run_reports(cfg,self.out);self.assertEqual((self.out/'.report-cache/snapshot-server.json').read_bytes(),cache)
    def test_fixed_baseline_repeat_is_byte_deterministic(self):
        baseline=self.root/'base';baseline.mkdir();(baseline/'scope').mkdir();(baseline/'scope/configuration.json').write_text('{"a":1}')
        publish(baseline,[section(baseline,'scope')]);self.capture({'a':2})
        self.cfg['snapshots'][0]['baseline']=str(baseline)
        reporting.run_reports(self.cfg,self.out);first={p.name:p.read_bytes() for p in self.out.iterdir() if p.is_file()}
        reporting.run_reports(self.cfg,self.out);self.assertEqual(first,{p.name:p.read_bytes() for p in self.out.iterdir() if p.is_file()})
    def test_html_embedded_payload_and_preview_hardening(self):
        self.capture({'tag':'</script><script>alert(1)</script>','private_key':'hidden'})
        cfg={**self.cfg,'include_values':True};reporting.run_reports(cfg,self.out)
        html=(self.out/'dashboard.html').read_text();self.assertNotIn('</script><script>alert',html);self.assertIn('\\u003c/script',html);self.assertNotIn('"private_key": "hidden"',html)
        reporting.run_reports(self.cfg,self.out);self.assertNotIn('alert(1)',(self.out/'report.json').read_text())
    def test_parent_symlink_rejected_without_output(self):
        self.capture({'a':1});(self.current/'scope/configuration.json').unlink();(self.current/'scope/configuration.json').symlink_to(self.root/'outside')
        reporting.run_reports(self.cfg,self.out);self.assertEqual(self.data()['status'],'partial');self.assertFalse(self.data()['explorer'])
    def test_section_and_source_switches(self):
        self.capture({'a':1});cfg={**self.cfg,'sections':{'explorer':False}}
        reporting.run_reports(cfg,self.out);self.assertFalse(self.data()['explorer']);self.assertEqual(self.data()['section_status']['explorer'],'disabled')
        cfg['enabled']=False;before=(self.out/'report.json').read_bytes();reporting.run_reports(cfg,self.out);self.assertEqual((self.out/'report.json').read_bytes(),before)
    def test_csv_keys_ignore_only_declared_row_order(self):
        root=self.current/'scope';root.mkdir();file=root/'rows.csv';file.write_text('id,value\nb,2\na,1\n');publish(self.current,[section(self.current,'scope')])
        self.cfg['csv_keys']={'scope/rows.csv':['id']};reporting.run_reports(self.cfg,self.out)
        file.write_text('id,value\na,1\nb,2\n');publish(self.current,[section(self.current,'scope')]);reporting.run_reports(self.cfg,self.out)
        self.assertFalse(any('property' in row for row in self.data()['changes']))
        self.assertEqual(self.data()['changes'][0]['reason'],'Representation/order changed or preview is incomplete; inspect native diff')
    def test_monitor_history_read_only_and_bounded(self):
        monitor=self.root/'monitor';monitor.mkdir();store=mon.Store(monitor/'history.sqlite3')
        base=mon.timestamp('2026-10-11T12:00:00Z');store.add([mon.metric('test.free_bytes',x,{'host':'a'},base-x,'bytes') for x in range(5)])
        mon.enqueue(store,'channel',{'text':'secret-not-for-report'},base);store.close();(monitor/'summary.json').write_text('{"observed_at":"2026-10-11T12:00:00Z","capacity":[]}')
        cfg={**self.cfg,'snapshots':[],'monitoring_path':str(monitor),'history_points':3};reporting.run_reports(cfg,self.out)
        data=self.data();self.assertEqual(len(data['history']),3);self.assertTrue(data['limits']['monitoring']['history_truncated']);self.assertNotIn('secret-not-for-report',(self.out/'report.json').read_text())
    def test_missing_schedule_preserves_old_report(self):
        folder=self.root/'schedule';folder.mkdir();(folder/'summary.json').write_text('{"analysis_as_of":"2026-10-11T12:00:00Z"}')
        (folder/'timeline.csv').write_text('job_id,start\njob,2026-10-11T13:00:00\n')
        cfg={**self.cfg,'snapshots':[],'schedule_paths':[str(folder)]};reporting.run_reports(cfg,self.out);before=self.data()['schedules']
        (folder/'summary.json').write_text('{broken');reporting.run_reports(cfg,self.out)
        self.assertEqual(self.data()['schedules'],before);self.assertEqual(self.data()['status'],'partial')
    def test_recovery_preview_never_copies_files(self):
        state={'tasks':{'task':{'files':{'scope/a.txt':{'versions':[{'path':'missing-on-purpose','created':'2026-10-10T00:00:00Z','hash':'abc'}]}}}}}
        reporting.run_reports({**self.cfg,'snapshots':[]},self.out,state=state)
        data=self.data();self.assertTrue(data['restore_preview']);self.assertEqual(data['recovery'][0]['retained_versions'],1)
        self.assertFalse((self.root/'missing-on-purpose').exists())
    def test_recovery_inventory_fallback_preserves_latest_append_order(self):
        state={'tasks':{'task':{'files':{'a':{'versions':[{'path':name,'created':'2026-10-11T00:00:00Z'} for name in ('old','new')]}}}}}
        with patch.dict(sys.modules,{'recovery':None}):_,preview,_=reporting.recovery_rows(state,{})
        self.assertEqual(preview[0]['version']['path'],'new');self.assertIn('no restore validation',preview[0]['status'])


class MonitoringDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.store=mon.Store(self.root/'history.sqlite3');self.addCleanup(self.store.close);self.now=mon.timestamp('2026-10-11T12:00:00Z')
    def test_full_queue_drains_and_other_disabled_channel_cannot_starve(self):
        for n in range(1000):mon.enqueue(self.store,'disabled',{'text':'old'},self.now-100)
        for n in range(1000):mon.enqueue(self.store,'live',{'text':'old'},self.now-100)
        cfg={'notifications':{'channels':[{'id':'live','type':'heartbeat','url':'https://example.invalid'}]}}
        with patch.object(mon,'send') as send:
            errors=mon.notifications(self.store,cfg,[],{'status':'healthy','observed_at':'2026-10-11T12:00:00Z'},self.now)
        self.assertGreater(send.call_count,0);self.assertFalse(errors)
    def test_heartbeat_queue_coalesces_without_delivery(self):
        cfg={'notifications':{'channels':[{'id':'live','type':'heartbeat','url':'https://example.invalid'}]}}
        for n in range(3):mon.notifications(self.store,cfg,[],{'status':'healthy','observed_at':str(n)},self.now+n,deliver=False)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0],1)
    def test_queue_failure_does_not_abort_dashboard(self):
        with patch.object(mon,'enqueue',side_effect=RuntimeError('secret-must-not-leak')):
            result=mon.run_monitor({'notifications':{'channels':[{'id':'beat','type':'heartbeat','url':'https://example.invalid'}]}},self.root/'out',now=self.now,deliver=False)
        self.assertTrue(result['delivery_errors']);self.assertTrue((self.root/'out/dashboard.html').is_file());self.assertNotIn('secret-must-not-leak',json.dumps(result))
    def test_schedule_unavailable_malformed_stale_isolated(self):
        bad=self.root/'bad';bad.mkdir();(bad/'summary.json').write_text('{no')
        good=self.root/'good';good.mkdir();(good/'summary.json').write_text('{"analysis_as_of":"2026-10-11T11:00:00"}')
        (good/'execution-analysis.csv').write_text('job_id,status\njob,missed\n')
        cfg={'schedule_reports':[str(self.root/'absent'),str(bad),{'path':str(good),'alerts':True}]}
        result=mon.run_monitor(cfg,self.root/'out',now=self.now,deliver=False)
        self.assertEqual(result['status'],'critical');self.assertEqual(len(result['schedules']),1);self.assertEqual(len(result['collection_errors']),2)
        self.assertTrue(any(e['rule']=='schedule-findings' for e in result['events']))
        result=mon.run_monitor({'schedule_reports':[{'path':str(good),'max_age_hours':.5}]},self.root/'another',now=self.now,deliver=False)
        self.assertEqual(result['schedule_sources'][0]['status'],'stale');self.assertTrue(result['alerts'])
    def test_disabled_schedule_source_clears_without_false_recovery(self):
        cfg={'schedule_reports':[{'path':str(self.root/'missing')}]};mon.run_monitor(cfg,self.root/'out',now=self.now,deliver=False)
        cfg['schedule_reports'][0]['enabled']=False;result=mon.run_monitor(cfg,self.root/'out',now=self.now+1,deliver=False)
        self.assertFalse(result['alerts']);self.assertFalse(result['events'])
    def test_smtp_final_byte_limit_with_multibyte_escaped_event_text(self):
        client=MagicMock();client.__enter__.return_value=client;client.send_message.return_value={}
        with patch.object(notifications.smtplib,'SMTP',return_value=client):notifications.send({'type':'smtp','host':'localhost','tls':'none','allow_insecure_localhost':True,'from':'a@local','to':['b@local'],'max_html_bytes':1024},{'summary':{'status':'healthy'},'text':'<&é'*10000})
        message=client.send_message.call_args.args[0];html=message.get_body(preferencelist=('html',)).get_content()
        self.assertLessEqual(len(html.encode()),1024);self.assertIn('shortened',html)
    def test_acknowledgement_expires_without_marking_recovery(self):
        sample=mon.metric('test',5,{},self.now)
        cfg={'default_rules':False,'rules':[{'id':'test','metric':'test','warning':0,'consecutive':1}], 'acknowledgements':[{'rule':'test','until':'2026-10-11T12:01:00Z','reason':'Investigating'}]}
        alerts,events=mon.evaluate(self.store,cfg,[sample],self.now);self.assertFalse(events);self.assertTrue(alerts[0]['acknowledged'])
        alerts,events=mon.evaluate(self.store,cfg,[sample],self.now+61);self.assertEqual(events[0]['event'],'opened')
    def test_channel_event_routing(self):
        cfg={'notifications':{'channels':[{'id':'only-db','type':'webhook','url':'https://example.invalid','where':{'host':'db-*'}}]}}
        event={'id':'id','rule':'rule','status':'critical','labels':{'host':'web-1'},'event':'opened','value':1,'unit':'count'}
        with patch.object(mon,'send') as send:mon.notifications(self.store,cfg,[event],{'status':'critical'},self.now);send.assert_not_called()


class ScheduleObservationTests(unittest.TestCase):
    def test_nearest_match_not_stolen_by_earlier_occurrence(self):
        job=types.SimpleNamespace(key='job',source='sql_agent',durations=[1],override_duration=None,observed=[{'start':'2026-10-11T00:05:00','end':'2026-10-11T00:06:00','status':'1'}])
        rows=[{'job_id':'job','start':'2026-10-11T00:'+minute+':00'} for minute in ('00','05')]
        result=insights.executions(rows,{'job':job},{'history_coverage':{'job':{'complete':True,'start':'2026-10-11','end':'2026-10-12'}}},dt.datetime(2026,10,11,2))
        self.assertEqual([r['status'] for r in result],['missed','matched']);self.assertEqual(result[1]['delay_minutes'],0)
    def test_explicit_match_wins_and_observation_order_does_not_change_result(self):
        observations=[{'start':'2026-10-11T00:05:00','scheduled_start':'2026-10-11T00:00:00','end':'2026-10-11T00:06:00','status':'1'},{'start':'2026-10-11T00:04:00','end':'2026-10-11T00:05:00','status':'1'}]
        job=types.SimpleNamespace(key='job',source='sql_agent',durations=[1],override_duration=None,observed=observations)
        rows=[{'job_id':'job','start':'2026-10-11T00:'+minute+':00'} for minute in ('00','05')]
        first=insights.executions(rows,{'job':job},{},dt.datetime(2026,10,11,2));job.observed=list(reversed(observations));second=insights.executions(rows,{'job':job},{},dt.datetime(2026,10,11,2))
        self.assertEqual(first,second);self.assertEqual(first[0]['actual_start'],'2026-10-11T00:05:00')
    def test_bad_source_keeps_all_existing_aggregate_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);out=root/'out';out.mkdir();(out/'jobs.csv').write_text('prior bytes')
            args=types.SimpleNamespace(output=str(out),config=None,start='2026-10-11',days=1,system=str(root/'missing'),sql=None,postgresql=None)
            with self.assertRaises(FileNotFoundError):schedules.report(args)
            self.assertEqual((out/'jobs.csv').read_text(),'prior bytes');self.assertEqual(json.loads((out/'generation-status.json').read_text())['status'],'failed')
    def test_static_html_discloses_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);schedules.render_dashboard(root,dt.date(2026,10,11),[],[{'job_id':str(n)} for n in range(201)],[],['warning']*101)
            text=(root/'dashboard.html').read_text();self.assertIn('Showing 200 of 201',text);self.assertIn('Showing 100 of 101',text)


class ExposureCorrelationTests(unittest.TestCase):
    def test_iis_windows_listener_service_and_certificate_correlate(self):
        from exposure_review import evidence,runtime,correlate
        configured=evidence('windows-components/certificates/stores/x/configuration.json',[{'thumbprint':'A'*40,'not_after':'2026-10-10T00:00:00Z'}])
        configured+=evidence('windows-components/certificates/bindings/iis/configuration.json',[{'site':'web','binding':'*:443:example.invalid','certificate_hash':'A'*40}])
        configured+=evidence('services/services.json',[{'Name':'W3SVC','PathName':'"C:\\Windows\\server.exe" -password secret-never-read'}])
        facts=runtime({'platform':'win32','observed_at':'2026-10-11T00:00:00Z','datasets':{'tcp-listeners':[{'LocalAddress':'0.0.0.0','LocalPort':443,'OwningProcess':42}],'processes':[{'ProcessId':42,'Name':'server.exe','ExecutablePath':'C:\\Windows\\server.exe'}]}})
        rows=correlate('host',configured,facts,mon.timestamp('2026-10-11T00:00:00Z'))
        self.assertEqual(rows[0]['certificate_status'],'expired');self.assertEqual(rows[0]['service_candidates'],['W3SVC']);self.assertEqual(rows[0]['listener_evidence'],'observed');self.assertIn('unknown',rows[0]['reachability']);self.assertNotIn('secret-never-read',json.dumps(rows))
    def test_cross_source_unmatched_no_ownership_or_reachability_claim(self):
        from exposure_review import correlate
        rows=correlate('host',[],[{'kind':'observed_endpoint','address':'127.0.0.1','port':8080,'protocol':'tcp'}],0)
        self.assertEqual(rows[0]['binding_scope'],'loopback');self.assertEqual(rows[0]['entity'],'unmatched listener')
    def test_linux_and_macos_runtime_field_parsing(self):
        from exposure_review import runtime
        linux=runtime({'platform':'linux','datasets':{'listeners':'tcp LISTEN 0 128 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=12,fd=3))'}})
        self.assertEqual(linux[0]['port'],5432);self.assertEqual(linux[0]['process'],'postgres')
        mac=runtime({'platform':'darwin','datasets':{'tcp-listeners':'p42\ncservice\nf3\nn*:8443\n'}})
        self.assertEqual(mac[0]['port'],8443);self.assertEqual(mac[0]['pid'],'42')
        udp=runtime({'platform':'darwin','datasets':{'udp-endpoints':'p42\ncservice\nn127.0.0.1:1234->127.0.0.1:5678\n'}})
        self.assertEqual(udp[0]['port'],1234);self.assertEqual(udp[0]['address'],'127.0.0.1')
    def test_container_and_web_bindings_have_no_fabricated_listener(self):
        from exposure_review import evidence,correlate
        values=evidence('containers/docker/container/objects/a/configuration.json',{'Name':'web','host':{'PortBindings':{'80/tcp':[{'HostIp':'127.0.0.1','HostPort':'8080'}]}}})
        values+=evidence('web-servers/nginx/a/configuration.json',{'source':'/etc/nginx.conf','directives':[{'directive':'listen','arguments':['443','ssl'],'values_collected':True}]})
        rows=correlate('host',values,[],0)
        self.assertEqual(len(rows),2);self.assertTrue(all('not observed' in r['listener_evidence'] for r in rows))
    def test_runtime_only_portal_and_failed_runtime_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);file=root/'runtime.json';file.write_text(json.dumps({'platform':'win32','observed_at':'2026-10-11T12:00:00Z','datasets':{'tcp-listeners':[{'LocalAddress':'::','LocalPort':443,'OwningProcess':1}]}}))
            config={'as_of':'2026-10-11T12:00:00Z','runtime_paths':[{'id':'host','path':str(file)}]};reporting.run_reports(config,root/'out')
            first=json.loads((root/'out/report.json').read_text())['exposure'];self.assertEqual(first[0]['port'],443)
            file.write_text('broken');result=reporting.run_reports(config,root/'out');self.assertEqual(result['status'],'partial')
            self.assertEqual(json.loads((root/'out/report.json').read_text())['exposure'],first)


class PortalFailureIsolationTests(unittest.TestCase):
    def test_report_paths_reject_invalid_primitive_types_before_io(self):
        values=(7,True,{},['path'],'','   ','a\x00b')
        for value in values:
            configs=[{key:value} for key in ('output','state','monitoring_path')]
            configs.extend(({'snapshots':[{'id':'host','path':value}]},{'snapshots':[{'id':'host','path':'current','baseline':value}]},{'runtime_paths':[{'id':'host','path':value}]},{'schedule_paths':[value]},{'schedule_paths':[{'path':value}]}))
            for config in configs:
                with self.subTest(config=config),self.assertRaises(ValueError):reporting.validate(config)
        with self.assertRaises(ValueError):reporting.validate({'output':None})
        reporting.validate({'output':'${CONFIGBACKUP_ARCHIVE_ROOT}/review','state':None,'monitoring_path':None,'snapshots':[{'id':'host','path':'C:/captures','baseline':None}],'runtime_paths':[{'id':'host','path':'/var/tmp/runtime.json'}]})
    def test_report_timezone_errors_are_consistent_and_do_not_touch_output(self):
        for value in (True,None,1,{},[],'','Not/A_Time_Zone','/UTC','UTC\x00'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'reporting.timezone'):reporting.validate({'timezone':value})
        reporting.validate({'timezone':'UTC'})
    def test_source_ids_are_strings_and_runtime_cache_ids_are_unique(self):
        for key in ('snapshots','runtime_paths'):
            for value in (123,True,{},['host']):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):reporting.validate({key:[{'id':value,'path':'source'}]})
            with self.subTest(key=key),self.assertRaises(ValueError):reporting.validate({key:[{'id':'host','path':'first'},{'id':'host','path':'second'}]})
    def test_csv_identity_schema_rejects_scalar_or_ambiguous_keys(self):
        for value in ([],{'x.csv':'id'},{'x.csv':[]},{'x.csv':['id','id']},{'x.csv':[{}]},{1:['id']},{'x.csv':['']}):
            for config in ({'csv_keys':value},{'snapshots':[{'id':'host','path':'source','csv_keys':value}]}):
                with self.subTest(config=config),self.assertRaises(ValueError):reporting.validate(config)
        reporting.validate({'csv_keys':{'x.csv':['host','id']}})
    def test_missing_state_keeps_prior_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);file=root/'state.json';file.write_text(json.dumps({'tasks':{'task':{'files':{'a':{'versions':[{'path':'old','created':'2026-10-10T00:00:00Z','hash':'abc'}]}}}}}))
            cfg={'state':str(file),'as_of':'2026-10-11T00:00:00Z'};reporting.run_reports(cfg,root/'out')
            before=json.loads((root/'out/report.json').read_text())['recovery'];file.unlink();reporting.run_reports(cfg,root/'out')
            data=json.loads((root/'out/report.json').read_text());self.assertEqual(data['recovery'],before);self.assertEqual(data['status'],'partial')
    def test_report_size_failure_preserves_previous_portal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg={'as_of':'2026-10-11T00:00:00Z'};reporting.run_reports(cfg,root)
            before={p.name:p.read_bytes() for p in root.iterdir() if p.is_file()}
            with self.assertRaises(ValueError):reporting.run_reports({**cfg,'max_report_bytes':1},root)
            self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir() if p.is_file()})
    def test_fixed_matching_guard_is_explicit(self):
        job=types.SimpleNamespace(key='job',source='sql_agent',durations=[],override_duration=None,observed=[{'start':'2026-10-11T00:00:00','end':'2026-10-11T00:01:00','status':'1'}])
        with self.assertRaisesRegex(ValueError,'matching limit'):insights.executions([{'job_id':'job','start':'2026-10-11T00:00:00'}],{'job':job},{'max_match_pairs':0},dt.datetime(2026,10,11,2))
    def test_disabled_exposure_does_not_read_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg={'sections':{'exposure':False},'runtime_paths':[{'id':'host','path':'nonexistent-fixture'}]}
            result=reporting.run_reports(cfg,tmp);self.assertNotEqual(result['status'],'partial')
            self.assertEqual(result['section_status']['exposure'],'disabled')

if __name__=='__main__':unittest.main()
