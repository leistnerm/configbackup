import contextlib, copy, datetime as dt, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import configbackup as cb
from completeness import Coverage, publish, section
from recovery import recover
from secret_scan import scan_bytes
ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value);return value
sa=module('schedule160','collectors/schedule/analyze_schedules.py')
canon=module('canon160','collectors/common/canonicalize.py')
pg=module('pg160','collectors/postgresql/collect_postgresql.py')
class SafetyTests(unittest.TestCase):
    def setup_engine(self,base,storage='both'):
        config={'backup':{'root':str(base/'archive')},'options':{'log_level':'CRITICAL'},
          'git':{'repository':str(base/'git')},'deletion':{'missing_runs':1,'max_percent_per_run':100},
          'tasks':[{'name':'sql','type':'directory','source':str(base/'source'),'destination':'sql','collection_manifest':True,'storage':storage}]}
        return cb.BackupEngine(cb.ConfigLoader(base/'unused')._resolve(config))
    def run_engine(self,engine):
        with contextlib.redirect_stdout(io.StringIO()):return engine.run()
    def test_repeated_partial_runs_preserve_archive_git_and_missing_counters(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'source'
            for db in ('healthy','restoring'):
                (root/'databases'/db).mkdir(parents=True);(root/'databases'/db/'schema.sql').write_text('original')
            publish(root,[section(root,'databases/healthy'),section(root,'databases/restoring')])
            engine=self.setup_engine(base);self.assertEqual(self.run_engine(engine),0)
            before=copy.deepcopy(engine.state.task('sql')['files']['sql/databases/restoring/schema.sql'])
            (root/'databases/restoring/schema.sql').write_text('BROKEN HALF EXPORT')
            for i in range(3):
                (root/'databases/healthy/schema.sql').write_text('healthy '+str(i))
                publish(root,[section(root,'databases/healthy'),section(root,'databases/restoring','failed','RESTORING')])
                engine=self.setup_engine(base);self.assertEqual(self.run_engine(engine),4)
                self.assertEqual(engine.state.task('sql')['files']['sql/databases/restoring/schema.sql'],before)
                self.assertEqual((base/'git/sql/databases/restoring/schema.sql').read_text(),'original')
                self.assertEqual((base/'git/sql/databases/healthy/schema.sql').read_text(),'healthy '+str(i))
            (root/'databases/healthy/schema.sql').unlink()
            publish(root,[section(root,'databases/healthy'),section(root,'databases/restoring','failed')])
            self.run_engine(self.setup_engine(base));self.assertFalse((base/'git/sql/databases/healthy/schema.sql').exists())
            self.assertTrue((base/'git/sql/databases/restoring/schema.sql').exists())
            (root/'databases/restoring/schema.sql').write_text('recovered')
            publish(root,[section(root,'databases/healthy'),section(root,'databases/restoring')])
            self.assertEqual(self.run_engine(self.setup_engine(base)),0)
            self.assertEqual((base/'git/sql/databases/restoring/schema.sql').read_text(),'recovered')
    def test_missing_tampered_stale_and_overlapping_manifests(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'db').mkdir();(root/'db/a').write_text('a');publish(root,[section(root,'db')])
            with self.assertRaises(ValueError):Coverage(root,'different-run')
            (root/'db/a').write_text('tampered')
            with self.assertRaises(ValueError):Coverage(root)
            publish(root,[section(root,'db'),section(root,'db/a')])
            with self.assertRaises(ValueError):Coverage(root)
            publish(root,[],False)
            with self.assertRaises(ValueError):Coverage(root)
    def test_portable_case_collisions_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'a').write_text('old')
            publish(root,[section(root,'a'),{'path':'A','status':'failed','files':{}}])
            with self.assertRaises(ValueError):Coverage(root)
            pg.stable_text(root/'Mixed.sql','original')
            with self.assertRaises(pg.CollectorError):pg.stable_text(root/'mixed.sql','replacement')
            self.assertEqual((root/'Mixed.sql').read_text(),'original\n')
    def test_omitted_schema_protected(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'source';(root/'db/schema').mkdir(parents=True)
            (root/'db/schema/a.sql').write_text('ddl');(root/'db/meta.json').write_text('{}')
            publish(root,[section(root,'db')]);self.run_engine(self.setup_engine(base))
            (root/'db/schema/a.sql').unlink();publish(root,[section(root,'db/meta.json')]);engine=self.setup_engine(base);self.run_engine(engine)
            self.assertTrue((base/'git/sql/db/schema/a.sql').exists())
            self.assertEqual(engine.state.task('sql')['files']['sql/db/schema/a.sql']['missing_runs'],0)
    def test_symlink_and_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'a').write_text('a');(root/'link').symlink_to(root/'a')
            with self.assertRaises(ValueError):section(root,'link')
            with self.assertRaises(ValueError):section(root,'../escape')
    def test_verify_restore_corruption(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'source';root.mkdir();(root/'config').write_text('safe config')
            publish(root,[section(root,'config')]);engine=self.setup_engine(base,'filesystem');self.run_engine(engine)
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(recover(engine,str(base/'restore')),0)
            self.assertEqual((base/'restore/sql/config').read_text(),'safe config')
            with self.assertRaises(ValueError):recover(engine,str(base/'restore'))
            version=engine.state.task('sql')['files']['sql/config']['versions'][0];(engine.root/version['path']).write_text('corrupt')
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(recover(engine),4)
    def test_historical_restore_respects_deletion_and_resurrection_gap(self):
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);engine=self.setup_engine(base,'filesystem');engine.root.mkdir(parents=True,exist_ok=True)
            old=engine.root/'old.version';new=engine.root/'new.version';old.write_text('old');new.write_text('new')
            def version(path,date):return {'path':path.name,'hash':hashlib.sha256(path.read_bytes()).hexdigest(),'created':date}
            engine.state.task('sql')['files']['sql/config']={'active':True,
                'versions':[version(new,'2026-01-05T00:00:00+00:00')],
                'deleted_generations':[{'deleted_at':'2026-01-02T00:00:00+00:00','versions':[version(old,'2026-01-01T00:00:00+00:00')]}]}
            for label,date,expected in [('before','2026-01-01T12:00:00+00:00','old'),('gap','2026-01-03T00:00:00+00:00',None),('after','2026-01-06T00:00:00+00:00','new')]:
                with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(recover(engine,str(base/label),date),0)
                target=base/label/'sql/config'
                if expected is None:self.assertFalse(target.exists())
                else:self.assertEqual(target.read_text(),expected)
    def test_secret_gate_blocks_commit(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'source';root.mkdir();(root/'a.sql').write_text("password = 'a_real_credential_value'")
            publish(root,[section(root,'a.sql')]);self.assertEqual(self.run_engine(self.setup_engine(base)),4)
            self.assertNotEqual(subprocess.run(['git','-C',str(base/'git'),'rev-parse','--verify','HEAD'],capture_output=True).returncode,0)
    def test_nested_zip_and_allowlist(self):
        import zipfile,hashlib
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:z.writestr('package.dtsx',b'pwd=credential_value')
        self.assertTrue(scan_bytes(buf.getvalue(),'p.ispac'));data=b'pwd=credential_value'
        self.assertFalse(scan_bytes(data,'x',[hashlib.sha256(data).hexdigest()]));self.assertFalse(scan_bytes(b'pwd=<REDACTED>','x'))
    def test_archive_io_failure_rolls_back_only_its_task(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'source';root.mkdir()
            for name in ('a','b'):(root/name).write_text('old '+name)
            publish(root,[section(root,'a'),section(root,'b')]);self.run_engine(self.setup_engine(base))
            engine=self.setup_engine(base);before=copy.deepcopy(engine.state.task('sql'))
            (root/'a').write_text('new a');(root/'b').write_text('new b')
            publish(root,[section(root,'a'),section(root,'b')])
            original=engine._store_one_file
            def failing(task,src,logical,*args,**kwargs):
                if src.name=='b':raise OSError('simulated disk error after first write')
                return original(task,src,logical,*args,**kwargs)
            with patch.object(engine,'_store_one_file',side_effect=failing):
                self.assertEqual(self.run_engine(engine),4)
            self.assertEqual(engine.state.task('sql'),before)
            self.assertEqual((base/'git/sql/a').read_text(),'old a')
            self.assertEqual((base/'git/sql/b').read_text(),'old b')
            versions=[v['path'] for f in before['files'].values() for v in f['versions']]
            archived=[p.relative_to(engine.root).as_posix() for p in engine.archive_root.rglob('*') if p.is_file() and not p.is_relative_to(engine.internal_root)]
            self.assertCountEqual(versions,archived)
    def test_secret_failure_can_be_fixed_next_run(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);root=base/'source';root.mkdir();(root/'a').write_text('password=real_secret')
            publish(root,[section(root,'a')]);self.assertEqual(self.run_engine(self.setup_engine(base)),4)
            (root/'a').write_text('redacted configuration');publish(root,[section(root,'a')])
            self.assertEqual(self.run_engine(self.setup_engine(base)),0)
            self.assertEqual((base/'git/sql/a').read_text(),'redacted configuration')
    def test_named_instances_keep_identity_and_share_host_concurrency(self):
        import csv,types,yaml
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);agent=root/'sql/instance/agent';agent.mkdir(parents=True)
            def put(name,rows):
                with (agent/name).open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            put('jobs.csv',[{'Name':'SameJob','Enabled':'true'}])
            put('schedules.csv',[{'name':'daily','enabled':'1','freq_type':'4','freq_interval':'1','freq_subday_type':'1','active_start_time':'10000','active_end_time':'235959','active_start_date':'20260101','active_end_date':'99991231'}])
            put('schedule-jobs.csv',[{'schedule_name':'daily','job_name':'SameJob'}])
            config={'sources':[{'type':'sql','host':'SERVER','instance':i,'timezone':'UTC','path':str(root/'sql')} for i in ('ONE','TWO')],
                    'analysis':{'duration_overrides_minutes':{'sql_agent:*':60}}}
            config_path=root/'config.yaml';config_path.write_text(yaml.safe_dump(config))
            args=types.SimpleNamespace(config=str(config_path),output=str(root/'report'),system=None,sql=None,postgresql=None,start='2026-10-09',days=1)
            summary=sa.report(args);self.assertEqual(summary['jobs'],2)
            rows=sa.read_csv(root/'report/timeline.csv');self.assertEqual(len({r['job_id'] for r in rows}),2)
            self.assertEqual({r['host'] for r in rows},{'SERVER'})
            self.assertEqual(max(int(r['concurrent_jobs']) for r in sa.read_csv(root/'report/concurrency.csv') if r['host']=='SERVER'),2)
            config['reports']={'concurrency':{'exclude':['*ONE*']}}
            config_path.write_text(yaml.safe_dump(config));sa.report(args)
            self.assertEqual(max(int(r['concurrent_jobs']) for r in sa.read_csv(root/'report/concurrency.csv')),1)
            self.assertTrue(sa.read_csv(root/'report/overlaps-p95.csv'))
    def test_launchd_calendar_and_unanchored_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);directory=root/'scheduling/launchd';directory.mkdir(parents=True)
            (directory/'a.json').write_text(json.dumps({'path':'/Library/LaunchDaemons/test.plist','definition':{'Label':'test','StartCalendarInterval':{'Weekday':5,'Hour':9,'Minute':30},'KeepAlive':True}}))
            jobs,warnings=sa.load_launchd(root,dt.date(2026,10,9),7)
            self.assertEqual(jobs[0].expand(None,None),[dt.datetime(2026,10,9,9,30)])
            self.assertTrue(warnings)
    def test_windows_repetition_old_anchor_and_midnight(self):
        t={'CimClass':'MSFT_TaskTimeTrigger','StartBoundary':'2020-01-01T23:55:00','Repetition':{'Interval':'PT5M'}}
        self.assertEqual(len(sa.expand_windows_trigger(t,dt.date(2026,10,9),1)[0]),288)
        t['StartBoundary']='2026-10-08T23:55:00';t['Repetition']['Duration']='PT20M'
        self.assertEqual([x.minute for x in sa.expand_windows_trigger(t,dt.date(2026,10,9),1)[0]],[0,5,10])
    def test_windows_weekly_anchor_and_end(self):
        t={'CimClass':'MSFT_TaskWeeklyTrigger','StartBoundary':'2026-10-05T12:00:00','WeeksInterval':2,'DaysOfWeek':1}
        self.assertEqual([e.date().isoformat() for e in sa.expand_windows_trigger(t,dt.date(2026,10,5),21)[0]],['2026-10-18'])
        t={'CimClass':'MSFT_TaskTimeTrigger','StartBoundary':'2026-10-09T00:00:00','EndBoundary':'2026-10-09T00:10:00','Repetition':{'Interval':'PT5M'}}
        self.assertEqual(len(sa.expand_windows_trigger(t,dt.date(2026,10,9),1)[0]),2)
    def test_pgagent_last_day_and_exception(self):
        t={'jscenabled':'true','jscstart':'2020-01-01','jscminutes':[True]+[False]*59,'jschours':[True]+[False]*23,'jscweekdays':[False]*7,'jscmonthdays':[False]*31+[True],'jscmonths':[False]*12}
        self.assertEqual([e.date().isoformat() for e in sa.expand_pgagent(t,[],dt.date(2024,2,1),60)[0]],['2024-02-29','2024-03-31'])
        self.assertEqual(len(sa.expand_pgagent(t,[{'jexdate':'2024-02-29'}],dt.date(2024,2,1),60)[0]),1)
        t['jschours']=[];self.assertTrue(sa.expand_pgagent(t,[],dt.date(2024,2,1),60)[2])
    def test_timezones_dst(self):
        w=[];self.assertEqual(sa.wall_to_utc(dt.datetime(2026,10,9,9),'America/New_York',w,'j'),dt.datetime(2026,10,9,13))
        self.assertIsNone(sa.wall_to_utc(dt.datetime(2026,11,1,1,30),'America/New_York',w,'j'))
        self.assertIsNone(sa.wall_to_utc(dt.datetime(2026,3,8,2,30),'America/New_York',w,'j'));self.assertEqual(len(w),2)
    def test_concurrency_half_open(self):
        rows=[{'host':'h','start':'2026-01-01T01:00:00','end':'2026-01-01T02:00:00'},{'host':'h','start':'2026-01-01T02:00:00','end':'2026-01-01T03:00:00'}]
        self.assertEqual(max(x['concurrent_jobs'] for x in sa.concurrency(rows,'observed')),1)
    def test_narrow_normalizer(self):
        a="EXEC sys.sp_addextendedproperty @name=N'z', @value=N'hello';\nGO\n";b="EXEC sys.sp_addextendedproperty @name=N'a', @value=N'world';\nGO\n"
        self.assertEqual(canon.canonical_sql(a+b),canon.canonical_sql(b+a))
        unsafe='CREATE PROCEDURE p AS BEGIN\n'+a+b+'END;';self.assertEqual(canon.canonical_sql(unsafe),unsafe)
        self.assertEqual(canon.canonical_sql(a+a),a+a);self.assertIn('[\n    2,\n    1\n  ]',canon.canonical_json({'steps':[2,1]}))
        self.assertIn('<COMPARISON-ONLY>',canon.canonical_pg('-- PostgreSQL\n\\restrict key\nCREATE TABLE x(i int);\n\\unrestrict key\n'))
if __name__=='__main__':unittest.main()
