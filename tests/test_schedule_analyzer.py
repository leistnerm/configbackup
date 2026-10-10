import csv
import importlib.util
import json
import tempfile
import types
import unittest
from datetime import date, datetime
from pathlib import Path

import yaml

P=Path(__file__).resolve().parents[1]/'collectors/schedule/analyze_schedules.py'
spec=importlib.util.spec_from_file_location('configbackup_schedule',P)
schedule=importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name]=schedule
spec.loader.exec_module(schedule)

class ScheduleTests(unittest.TestCase):
    def test_cron_every_6_hours_is_four_per_day(self):
        events,cadence,error=schedule.expand_cron('0 */6 * * *',date(2026,10,8),2)
        self.assertEqual((len(events),cadence,error),(8,'daily',''))
        self.assertEqual([e.hour for e in events[:4]],[0,6,12,18])

    def test_sql_agent_every_five_minutes_and_weekly(self):
        base={'freq_type':'4','freq_interval':'1','freq_subday_type':'4','freq_subday_interval':'5',
            'active_start_time':'10000','active_end_time':'11500','active_start_date':'20260101'}
        events,cadence,error=schedule.expand_sql_schedule(base,date(2026,10,8),1)
        self.assertEqual([e.strftime('%H:%M') for e in events],['01:00','01:05','01:10','01:15'])
        self.assertEqual(cadence,'daily')
        base.update(freq_type='8',freq_interval='2',freq_subday_type='1')
        events,cadence,error=schedule.expand_sql_schedule(base,date(2026,10,5),7)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].date(),date(2026,10,5))

    def test_sql_agent_duration_over_day(self):
        self.assertEqual(schedule.sql_duration_minutes('250130'),1501.5)

    def test_windows_repetition(self):
        trigger={'Enabled':True,'StartBoundary':'2026-10-08T00:00:00','CimClass':'MSFT_TaskDailyTrigger',
                 'DaysInterval':1,'Repetition':{'Interval':'PT5M','Duration':'PT20M'}}
        events,cadence,error=schedule.expand_windows_trigger(trigger,date(2026,10,8),1)
        self.assertEqual(len(events),4)
        self.assertEqual([e.minute for e in events],[0,5,10,15])

    def test_pg_cron_and_systemd(self):
        events,cadence,error=schedule.expand_cron('0 1 * * 1',date(2026,10,5),7)
        self.assertEqual(len(events),1)
        events,cadence,error=schedule.expand_systemd_calendar('Mon *-*-* 03:00:00',date(2026,10,5),7)
        self.assertEqual((len(events),cadence,error),(1,'weekly',''))

    def test_deterministic_report_and_file_only_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            system=root/'system'/'scheduling';system.mkdir(parents=True)
            windows=[{'TaskPath':'\\Automation\\','TaskName':'Watchdog','Settings':{'Enabled':True},'Triggers':[
                {'Enabled':True,'StartBoundary':'2026-10-08T00:00:00','CimClass':'MSFT_TaskDailyTrigger',
                 'DaysInterval':1,'Repetition':{'Interval':'PT5M','Duration':'PT15M'}}]}]
            (system/'scheduled-tasks.json').write_text(json.dumps(windows))
            sql=root/'sql'/'instance'/'agent';sql.mkdir(parents=True)
            def put(name,items):
                with (sql/name).open('w',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=list(items[0]));writer.writeheader();writer.writerows(items)
            put('jobs.csv',[{'Name':'HeavyETL','Enabled':'true'}])
            put('schedules.csv',[{'name':'daily','enabled':'1','freq_type':'4','freq_interval':'1','freq_subday_type':'1',
                                  'active_start_time':'0','active_end_time':'235959','active_start_date':'20260101','active_end_date':'99991231'}])
            put('schedule-jobs.csv',[{'schedule_name':'daily','job_name':'HeavyETL'}])
            put('job-runs.csv',[{'job_name':'HeavyETL','run_date':'20261007','run_time':'0','run_duration':'003000','run_status':'1'}])
            cfg={'reports':{'daily':{'exclude':['windows:*Watchdog']},'overlaps':{'exclude':['windows:*Watchdog']}},
                 'analysis':{'duration_overrides_minutes':{'windows:*Watchdog':120}}}
            config=root/'report.yaml';config.write_text(yaml.safe_dump(cfg))
            args=types.SimpleNamespace(output=str(root/'out'),system=str(root/'system'),sql=str(root/'sql'),postgresql=None,
                                       sql_host='SQL1',pg_host='PG1',config=str(config),start='2026-10-08',days=35)
            summary=schedule.report(args)
            self.assertEqual(summary['jobs'],2)
            daily=schedule.read_csv(root/'out'/'daily.csv')
            self.assertEqual(len(daily),1)
            self.assertEqual(daily[0]['job'],'HeavyETL')
            events=schedule.read_csv(root/'out'/'timeline.csv')
            self.assertEqual(len(events),140) # 35 days, watchdog 3/day + SQL 1/day
            self.assertEqual(schedule.read_csv(root/'out'/'overlaps.csv'),[])
            self.assertEqual(summary['known_duration_jobs'],2)
            exclusions=schedule.read_csv(root/'out'/'exclusions.csv')
            self.assertTrue(any(r['report']=='daily' and 'Watchdog' in r['job_id'] for r in exclusions))
            before=(root/'out'/'jobs.csv').read_bytes()
            schedule.report(args)
            self.assertEqual(before,(root/'out'/'jobs.csv').read_bytes())

    def test_pg_csv_sort_canonical(self):
        import importlib.util
        p=Path(__file__).resolve().parents[1]/'collectors/postgresql/collect_postgresql.py'
        spec=importlib.util.spec_from_file_location('configbackup_pg_for_schedules',p)
        pg=importlib.util.module_from_spec(spec);spec.loader.exec_module(pg)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'inventory.csv'
            rows=[{'name':'z','value':1},{'name':'a','value':2}]
            pg.stable_csv(path,rows)
            first=path.read_bytes()
            pg.stable_csv(path,list(reversed(rows)))
            self.assertEqual(path.read_bytes(),first)

if __name__=='__main__':unittest.main()

class SnapshotAuditTests(unittest.TestCase):
    def test_csv_order_churn_is_distinguished_from_real_change(self):
        p=Path(__file__).resolve().parents[1]/'collectors/common/compare_snapshots.py'
        spec=importlib.util.spec_from_file_location('cb_compare',p)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as path:
            base=Path(path);a=base/'a';b=base/'b';a.mkdir();b.mkdir()
            (a/'jobs.csv').write_text('job,schedule\na,1\nb,2\n')
            (b/'jobs.csv').write_text('job,schedule\nb,2\na,1\n')
            (a/'schema.sql').write_text('CREATE TABLE a (id int);')
            (b/'schema.sql').write_text('CREATE TABLE a (id bigint);')
            data=module.audit(a,b)
            self.assertEqual(data['csv_row_order_only'],['jobs.csv'])
            self.assertEqual(data['changed'],['schema.sql'])
