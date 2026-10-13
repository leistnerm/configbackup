import copy,datetime as dt,json,os,shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import configbackup_lib.engine.configbackup as cb; import configbackup_lib.reporting.performance_report as perf; import configbackup_lib.engine.native_recording as native; import configbackup_lib.engine.completeness as completeness
from test_performance_and_recording import rows,jobs,BASE
class ProfileAndReportWorkflowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.cfg={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'options':{'log_level':'CRITICAL'},'tasks':[]}
 def tearDown(self):self.tmp.cleanup()
 def run_engine(self):
  e=cb.BackupEngine(cb.ConfigLoader(self.root/'config.yaml')._resolve(copy.deepcopy(self.cfg)))
  try:return e,e.run()
  finally:e._close_logging()
 def file_task(self):
  src=self.root/'x.ini';src.write_text('x=1');self.cfg['tasks']=[{'name':'save','type':'file','source':str(src),'destination':'x.ini'}];return src
 def source(self):
  path=self.root/'good.json';path.write_text(json.dumps({'samples':rows()}));return {'id':'good','type':'normalized_json','path':str(path)}
 def test_preview_secrets_immutability(self):
  from configbackup_lib.configuration import config_editor
  secret='SYNTHETIC-PREVIEW-SENTINEL';self.cfg['tasks']=[{'name':'custom','type':'execute','executable':sys.executable,'arguments':['--password','${CB_PREVIEW_PASSWORD}','token='+secret]}];before=copy.deepcopy(self.cfg)
  with patch.dict(os.environ,{'CB_PREVIEW_PASSWORD':secret}):value=config_editor.effective_preview(self.cfg,self.root/'preview.yaml')
  self.assertNotIn(secret,json.dumps(value));self.assertEqual(before,self.cfg);self.assertEqual(cb.redact_for_display({'safe':'','password':''})['safe'],'')
 def test_profile_budget_archive_preservation(self):
  source=self.root/'staging/collect';self.cfg.update(internal={'staging_root':str(self.root/'staging')},connections={'pg':{'engine':'postgresql','host':'localhost','user':'test'}},collection_profiles={'small':{'engine':'postgresql','max_scope_bytes':20}})
  self.cfg['tasks']=[{'name':'collect','type':'execute','connection':'pg','profile':'small','output_directory':str(source)},{'name':'archive','type':'directory','source':str(source),'destination':'db','depends_on':['collect'],'collection_manifest':True}];content=['x=1']
  def collect(profile,output,env,cwd):
   p=Path(output);(p/'one').mkdir(parents=True,exist_ok=True);(p/'one/x.ini').write_text(content[0])
   with patch.dict(os.environ,env):completeness.publish(p,[completeness.section(p,'one')])
   return subprocess.CompletedProcess([],0,b'',b'')
  with patch('configbackup_lib.configuration.database_connections.collect',side_effect=collect):
   e,code=self.run_engine();self.assertEqual(code,0);before=copy.deepcopy(e.state.task('archive')['files']);content[0]='x'*100;e,code=self.run_engine();self.assertEqual(code,4);self.assertEqual(before,e.state.task('archive')['files']);self.assertEqual(e.cfg['tasks'][1]['max_scope_bytes'],20)
   self.cfg['tasks'][1]['max_scope_bytes']=200;self.assertEqual(self.run_engine()[1],0)
 def test_malformed_source_isolated_deterministic(self):
  bad=self.root/'bad.json';bad.write_text(json.dumps({'sysstat':{'hosts':[{'statistics':[{'cpu-load':[None]}]}]}}));cfg={'sources':[{'id':'bad','type':'sysstat_json','path':str(bad)},self.source()]};out=self.root/'report';first=perf.run(cfg,out);prior=(out/'performance.json').read_bytes();perf.run(cfg,out)
  self.assertEqual(first['source_status'][0]['status'],'failed');self.assertEqual(first['source_status'][1]['status'],'complete');self.assertEqual(prior,(out/'performance.json').read_bytes())
 def test_missing_counter_partial(self):
  path=self.root/'perf.csv';path.write_text('"date","\\\\H\\Processor(_Total)\\% Processor Time","\\\\H\\PhysicalDisk(0 C:)\\Avg. Disk sec/Read"\n"10/01/2026 00:00:00","0","0"\n"10/01/2026 00:00:15","-1","0.025"\n');value=perf.run({'sources':[{'id':'p','host':'h','type':'perfmon_csv','path':str(path),'timezone':'UTC'}]},self.root/'out')
  self.assertEqual(value['status'],'partial');self.assertEqual(value['source_status'][0]['coverage_warnings'][0]['metric'],'cpu.busy_percent')
 def test_expected_metric_missing(self):
  spec=self.source();spec['expected_metrics']=['memory.used_percent'];value=perf.run({'sources':[spec]},self.root/'out');self.assertEqual(value['source_status'][0]['coverage_warnings'][0]['kind'],'missing_metric')
 def test_interrupted_capture_cleanup(self):
  name='sample-20261001T000000000000Z.sa';other='sample-foreign.sa';(self.root/name).write_text('partial');(self.root/other).write_text('foreign');(self.root/'capture-pending.json').write_text(json.dumps({'schema':1,'files':[name]}));native.reconcile_capture(self.root);self.assertFalse((self.root/name).exists());self.assertEqual((self.root/other).read_text(),'foreign')
 def test_committed_capture_preserved(self):
  name='sample-20261001T000000000000Z.json';(self.root/name).write_text('committed');(self.root/'history-index.json').write_text(json.dumps([{'created':1,'files':[name]}]));(self.root/'capture-pending.json').write_text(json.dumps({'schema':1,'files':[name]}));native.reconcile_capture(self.root);self.assertEqual((self.root/name).read_text(),'committed')
 def test_invalid_pending_never_deletes(self):
  name='sample-20261001T000000000000Z.sa';(self.root/name).write_text('partial');(self.root/'capture-pending.json').write_text(json.dumps({'schema':1,'files':[name,'../foreign']}))
  with self.assertRaises(ValueError):native.reconcile_capture(self.root)
  self.assertTrue((self.root/name).exists())
 def test_git_event_time_preserved(self):
  self.file_task();self.cfg['git']={'repository':str(self.root/'git')};self.cfg['tasks'][0]['storage']='both'
  with patch.object(cb,'iso_now',return_value='2026-10-01T00:00:00+00:00'):e,code=self.run_engine()
  before=copy.deepcopy(e.state.task('save')['files']['x.ini']['publication'])
  with patch.object(cb,'iso_now',return_value='2026-10-02T00:00:00+00:00'):e,code=self.run_engine()
  after=e.state.task('save')['files']['x.ini']['publication'];self.assertEqual(before['committed_at'],after['committed_at']);self.assertNotEqual(before['last_checked_at'],after['last_checked_at'])
 def test_required_integrity_overdue_visible(self):
  self.file_task();self.cfg['integrity']={'enabled':True,'required':True,'max_bytes':1};e,code=self.run_engine();self.assertEqual(code,4);self.assertEqual(e.results['integrity'].status,'partial');self.assertIn('overdue 1',e.results['integrity'].message)
 def test_optional_integrity_visible_nonfatal(self):
  self.file_task();self.cfg['integrity']={'enabled':True,'required':False,'max_bytes':1};e,code=self.run_engine();self.assertEqual(code,0);self.assertEqual(e.results['integrity'].status,'partial')
 def test_profile_impacts_clone_used_delete(self):
  import configbackup_lib.configuration.configuration_profiles as p
  self.cfg.update(collection_profiles={'p':{'engine':'postgresql','max_scope_bytes':100}},tasks=[{'name':'collect','profile':'p'},{'name':'archive','depends_on':'collect'}]);self.assertEqual(len(p.affected(self.cfg,'p')),2)
  answers=iter(['1','clone']);p.manage(self.cfg,'Clone profile',lambda _:next(answers));self.assertIn('clone',self.cfg['collection_profiles']);answers=iter(['2']);p.manage(self.cfg,'Delete profile',lambda _:next(answers));self.assertIn('p',self.cfg['collection_profiles'])
 def test_duration_confidence_determinism_unknown(self):
  from configbackup_lib.reporting.performance_incidents import duration_comparisons
  data=[]
  for i in range(12):
   start=BASE+dt.timedelta(minutes=i*10);end=start+dt.timedelta(seconds=60 if i<6 else 120);data.append({'host':'h','job_id':'one','start':start.isoformat(),'end':end.isoformat(),'status':'success'})
   if i>=6:data.append({**data[-1],'job_id':'two'})
  value=duration_comparisons(data);self.assertEqual(value,duration_comparisons(list(reversed(data))));self.assertEqual(value[0]['median_ratio_95pct_bootstrap'],[2,2]);data[0]['status']='unknown';self.assertEqual(duration_comparisons(data)[0]['excluded_unknown_or_failed'],1)
 def test_workflow_report_email_staleness(self):
  from configbackup_lib.reporting.monitoring import run_monitor
  from configbackup_lib.reporting.notifications import render_summary
  from configbackup_lib.reporting.performance_summary import read
  spec=self.source();history=self.root/'jobs.json';history.write_text(json.dumps([{**r,'status':'success'} for r in jobs()]));out=self.root/'perf';value=perf.run({'sources':[spec],'job_history':[str(history)]},out);self.assertIn('incident-select',(out/'performance.html').read_text());self.assertEqual(len(value['observed_runs']),2)
  config={'performance_reports':[{'path':str(out/'performance.json')}],'channels':[]};summary=run_monitor(config,self.root/'monitor',now=(BASE+dt.timedelta(minutes=5)).timestamp(),deliver=False);plain,rich=render_summary(summary,['performance']);self.assertIn('cpu.busy_percent',rich);self.assertNotIn('<script',rich)
  facts,_,_=read(config,(BASE+dt.timedelta(days=2)).timestamp());self.assertEqual(facts[0]['status'],'stale');self.assertEqual(len(facts),1)
 def test_recovery_copy_corruption_budget(self):
  import configbackup_lib.recovery.archive_integrity as a; import configbackup_lib.recovery.recovery_readiness as r
  self.file_task();e,code=self.run_engine();a.scrub(e,force=True);mirror=self.root/'mirror';shutil.copytree(e.root,mirror);cfg={'copy_roots':[str(mirror)],'targets':[{'task':'save','min_verified_copies':2}]};value=r.evaluate(e,cfg);self.assertEqual(value['status'],'met',value)
  version=e.state.task('save')['files']['x.ini']['versions'][-1];(mirror/version['path']).write_text('bad');value=r.evaluate(e,cfg);self.assertEqual(value['status'],'unmet');self.assertEqual(value['targets'][0]['verified_copies'],1);cfg['max_bytes']=1;self.assertEqual(r.evaluate(e,cfg)['targets'][0]['verified_copies'],0)
 def test_recovery_duplicate_roots(self):
  import configbackup_lib.recovery.archive_integrity as a; import configbackup_lib.recovery.recovery_readiness as r
  self.file_task();e,code=self.run_engine();a.scrub(e,force=True);value=r.evaluate(e,{'copy_roots':[str(e.root),str(e.root)],'targets':[{'min_verified_copies':2}]});self.assertEqual(value['targets'][0]['verified_copies'],1);self.assertEqual(value['status'],'unmet')
 def test_recovery_required_keeps_healthy_backup(self):
  self.file_task();self.cfg['recovery_readiness']={'enabled':True,'required':True,'targets':[{'min_verified_copies':2}]};e,code=self.run_engine();self.assertEqual(code,4);self.assertTrue(e.state.task('save')['files']['x.ini']['versions']);self.assertEqual(e.results['recovery_readiness'].status,'partial')
 def test_recorder_health_running_stale_unknown(self):
  import configbackup_lib.engine.recorder_health as h
  path=self.root/'config.yaml';path.write_text('tasks: []');history=self.root/'history';history.mkdir();cfg={'performance':{'recording':{'directory':str(history)}}};value=native.settings(cfg,path,'Linux');value['backend']='psutil';name='sample-20261001T000000000000Z.json';(history/name).write_text(json.dumps({'samples':rows()}));(history/'history-index.json').write_text(json.dumps([{'created':1,'files':[name]}]));native.state_path(path).write_text(json.dumps({'config':str(path),'system':'Linux','name':'ConfigBackup-Performance-0123456789ab','settings':value,'status':'enabled'}))
  execute=lambda *a,**k:subprocess.CompletedProcess(a,0,'ActiveState=active\nSubState=running\n');result=h.inspect(cfg,path,system='Linux',execute=execute,now=(BASE+dt.timedelta(minutes=4)).timestamp());self.assertEqual(result['status'],'healthy');result=h.inspect(cfg,path,system='Linux',execute=execute,now=(BASE+dt.timedelta(days=1)).timestamp());self.assertEqual(result['capture_status'],'stale');self.assertEqual(result['status'],'attention');execute=lambda *a,**k:subprocess.CompletedProcess(a,1,'');self.assertEqual(h.inspect(cfg,path,system='Linux',execute=execute,now=BASE.timestamp())['native_status'],'unknown')
 def test_profile_edit_and_unused_delete(self):
  import configbackup_lib.configuration.configuration_profiles as p
  self.cfg['collection_profiles']={'p':{'engine':'postgresql','max_scope_bytes':100}};answers=iter(['1','3','200','y']);p.manage(self.cfg,'Edit profile',lambda _:next(answers));self.assertEqual(self.cfg['collection_profiles']['p']['max_scope_bytes'],200)
  answers=iter(['1','y']);p.manage(self.cfg,'Delete profile',lambda _:next(answers));self.assertFalse(self.cfg['collection_profiles'])
 def test_profile_clear_override_menu(self):
  import configbackup_lib.configuration.configuration_profiles as p
  self.cfg.update(connections={'pg':{'engine':'postgresql'}},tasks=[{'name':'one','connection':'pg','max_scope_bytes':3}]);answers=iter(['8','1','1','y','9']);p.wizard(self.cfg,lambda _:next(answers));self.assertNotIn('max_scope_bytes',self.cfg['tasks'][0])
 def test_performance_unavailable_removes_old_pressure_metrics(self):
  from configbackup_lib.reporting.monitoring import run_monitor
  out=self.root/'perf';perf.run({'sources':[self.source()]},out);config={'performance_reports':[{'path':str(out/'performance.json')}]};now=(BASE+dt.timedelta(minutes=5)).timestamp();run_monitor(config,self.root/'monitor',now=now,deliver=False)
  (out/'performance.json').write_text('bad');value=run_monitor(config,self.root/'monitor',now=now+1,deliver=False);self.assertEqual(value['performance'][0]['status'],'unavailable');self.assertFalse(any(r['metric']=='performance.pressure_minutes' for r in value['metrics']))
 def test_conflicting_job_status_is_unknown_deterministic(self):
  facts=[{**jobs()[0],'status':'success'},{**jobs()[0],'status':'failed'}];path=self.root/'jobs.json';cfg={'sources':[self.source()],'job_history':[str(path)]};path.write_text(json.dumps(facts));a=perf.run(cfg,self.root/'a');path.write_text(json.dumps(list(reversed(facts))));b=perf.run(cfg,self.root/'b');self.assertEqual(a,b);self.assertEqual(a['observed_runs'][0]['status'],'unknown')
 def test_readiness_missing_target_and_invalid_limits(self):
  import configbackup_lib.recovery.recovery_readiness as r
  e,_=self.run_engine();self.assertEqual(r.evaluate(e,{'targets':[{'task':'missing'}]})['status'],'unmet')
  for cfg in ({'targets':[{'min_verified_copies':True}]},{'copy_roots':['relative']},{'max_bytes':float('nan')}):
   with self.assertRaises(ValueError):r.validate(cfg)
