import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import configbackup as cb
import journal_recovery as recovery
import recovery_maintenance as maintenance
import retention_planning
import review_html
from transaction_journal import Journal


class RetentionRecoveryAndSchedulePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.config=self.root/'config.yaml';self.cfg={'backup':{'root':str(self.root/'archive'),'include_configuration':False},'tasks':[]}
        self.config.write_text('backup: {root: archive}\ntasks: []\n')
    def tearDown(self):self.tmp.cleanup()
    def test_journal_plan_is_bound_to_current_bytes(self):
        path=self.root/'data';path.write_text('before');j=Journal(self.root/'journal',[self.root]);j.begin();j.remember(path);path.write_text('after')
        plan=recovery.inspect(j);self.assertTrue(plan['can_reconcile']);path.write_text('different')
        with self.assertRaises(ValueError):recovery.reconcile(j,plan['plan_id'])
        plan=recovery.inspect(j);self.assertEqual(recovery.reconcile(j,plan['plan_id'])['status'],'rolled_back');self.assertEqual(path.read_text(),'before')
    def test_changed_head_and_corrupt_before_image_are_blocked(self):
        path=self.root/'data';path.write_text('before');j=Journal(self.root/'journal',[self.root]);j.begin();j.remember(path)
        with patch('journal_recovery.head',return_value='different'):
            report=recovery.inspect(j);self.assertFalse(report['can_reconcile']);self.assertIn('Git HEAD changed',report['guidance'])
        (j.directory/'before-00000000').write_text('corrupt');self.assertEqual(recovery.inspect(j)['status'],'invalid');self.assertEqual(path.read_text(),'before')
    def test_orphan_journal_never_deleted_by_inspection(self):
        j=Journal(self.root/'journal',[self.root]);j.directory.mkdir();path=j.directory/'before-00000000';path.write_text('old')
        self.assertEqual(recovery.inspect(j)['status'],'orphaned');self.assertTrue(path.exists())
    def test_retention_preview_matches_engine_and_never_mutates(self):
        source=self.root/'source';source.write_text('one');self.cfg['tasks']=[{'name':'file','type':'file','source':str(source),'destination':'file','retention':{'active':{'mode':'simple','min_versions':1,'max_versions':3}}}]
        def engine():return cb.BackupEngine(cb.ConfigLoader(self.config)._resolve(copy.deepcopy(self.cfg)))
        for i in range(3):source.write_text(str(i));e=engine();self.assertEqual(e.run(),0)
        self.cfg['tasks'][0]['retention']['active']['max_versions']=1;e=engine();before=e.state.path.read_bytes()
        report=retention_planning.report(e,100,50);self.assertEqual(sum(r['action']=='delete' for r in report['items']),2)
        self.assertEqual(report['space']['additional_peak_estimate_bytes'],402);self.assertEqual(e.state.path.read_bytes(),before)
        e._close_logging();e=engine();self.assertEqual(e.run(),0);self.assertEqual(len(next(iter(e.state.task('file')['files'].values()))['versions']),1)
    def test_retention_preview_protects_disabled_and_pending(self):
        e=cb.BackupEngine(cb.ConfigLoader(self.config)._resolve(copy.deepcopy(self.cfg)),dry_run=True)
        (e.internal_root/'transaction').mkdir(parents=True);(e.internal_root/'transaction/pending.json').write_text('{}')
        self.assertEqual(retention_planning.report(e)['status'],'blocked');e._close_logging()
    def test_html_is_deterministic_escaped_and_searchable(self):
        value={'items':[{'application':'<script>alert(1)</script>','status':'unknown','dependencies':['a','b']}],'limits':'unknown is not complete'}
        one=review_html.render(value,'<report>');self.assertEqual(one,review_html.render(value,'<report>'))
        self.assertNotIn('<script>alert',one);self.assertIn('&lt;script&gt;',one);self.assertIn('type="search"',one);self.assertIn('scope="col"',one)
    def test_encrypted_secondary_is_verified_and_repeat_is_unchanged(self):
        try:import cryptography
        except ImportError:self.skipTest('Optional cryptography dependency not installed')
        password='synthetic test passphrase';managed=self.root/'managed';secondary=self.root/'secondary'
        one=maintenance.refresh(self.cfg,self.config,managed,secondary,password=password);self.assertEqual(one['secondary']['status'],'verified')
        copy_path=Path(one['secondary']['path']);before=copy_path.read_bytes()
        two=maintenance.refresh(self.cfg,self.config,managed,secondary,password=password);self.assertEqual(two['status'],'unchanged');self.assertEqual(before,copy_path.read_bytes())
        self.assertEqual(maintenance.status(managed)['secondary']['current_verification'],'not_checked_passphrase_required')
        self.assertEqual(maintenance.status(managed,password)['secondary']['current_verification'],'passed')
        bad=maintenance.refresh(self.cfg,self.config,managed,secondary,password='wrong test passphrase');self.assertEqual(bad['secondary']['status'],'failed');self.assertEqual(before,copy_path.read_bytes())
    def test_recovery_retention_protects_last_valid_and_corrupt_copy(self):
        managed=self.root/'managed';secondary=self.root/'secondary'
        first=maintenance.refresh(self.cfg,self.config,managed,secondary)
        self.config.write_text(self.config.read_text()+'# revision two\n');second=maintenance.refresh(self.cfg,self.config,managed,secondary)
        corrupt=secondary/('a'*64);corrupt.mkdir();(corrupt/'bad').write_text('unrecognized')
        preview=maintenance.retain(secondary,1);self.assertEqual(sum(r['action']=='delete' for r in preview['copies']),1)
        result=maintenance.retain(secondary,1,apply=True);self.assertEqual(result['status'],'applied');self.assertTrue(Path(second['secondary']['path']).exists());self.assertFalse(Path(first['secondary']['path']).exists());self.assertTrue(corrupt.exists())
        with self.assertRaises(ValueError):maintenance.retain(secondary,0,apply=True)
    def test_recovery_retention_unverifiable_encrypted_copy_is_protected(self):
        root=self.root/'secondary';root.mkdir();(root/('b'*64+'.cbrec')).write_bytes(b'unknown')
        result=maintenance.retain(root,1,apply=True);self.assertEqual(result['copies'][0]['action'],'protected');self.assertEqual(result['verified_copies'],0)
    def test_secondary_failed_creation_preserves_local_latest(self):
        original=maintenance.check_copy
        def fail_secondary(path,*args,**kwargs):
            if Path(path).is_relative_to(self.root/'secondary'):raise ValueError('synthetic copy error')
            return original(path,*args,**kwargs)
        with patch('recovery_maintenance.check_copy',side_effect=fail_secondary):
            result=maintenance.refresh(self.cfg,self.config,self.root/'managed',self.root/'secondary')
        self.assertEqual(result['secondary']['status'],'failed');self.assertTrue(Path(result['bundle']).is_dir());self.assertEqual(list((self.root/'secondary').glob('[0-9a-f]'*64)),[])

    def test_schedule_visual_shift_conflicts_exclusions_and_determinism(self):
        import sys
        from types import SimpleNamespace
        sys.path.insert(0,str(Path(cb.__file__).parent/'collectors/schedule'))
        import schedule_insights as insights
        jobs={k:SimpleNamespace(key=k,host='host',timezone='UTC',override_duration=20,durations=[],observed=[],source='test',heartbeats=[]) for k in ['a','b','watchdog']}
        timeline=[{'job_id':k,'start':'2026-01-01T01:00:00'} for k in jobs]
        settings={'watchdogs':['watchdog'],'what_if':[{'name':'Move b <later>','shifts_minutes':{'b':30}}],'_what_if_exclude':['watchdog']}
        insights.analyze(self.root,timeline,jobs,settings,lambda *args:None,'2026-01-01T00:00:00')
        value=json.loads((self.root/'what-if.json').read_text());self.assertEqual(len(value['cases'][0]['conflicts']),1);self.assertEqual(value['cases'][1]['conflicts'],[])
        self.assertEqual({r['job_id'] for r in value['cases'][0]['timeline']},{'a','b'})
        before=(self.root/'what-if.html').read_bytes();self.assertIn(b'Move b &lt;later&gt;',before)
        insights.analyze(self.root,timeline,jobs,settings,lambda *args:None,'2026-01-01T00:00:00');self.assertEqual(before,(self.root/'what-if.html').read_bytes())
    def test_schedule_visual_unknown_duration_and_bounded_rows(self):
        import sys
        from types import SimpleNamespace
        sys.path.insert(0,str(Path(cb.__file__).parent/'collectors/schedule'))
        import schedule_visual as visual
        job=SimpleNamespace(timezone='UTC',override_duration=None,durations=[])
        visual.write_scenarios(self.root,[{'job_id':'a','start':'2026-01-01T00:00:00'},{'job_id':'a','start':'2026-01-01T01:00:00'}],{'a':job},[],[],[],[],{'max_visual_occurrences':1},False)
        data=json.loads((self.root/'what-if.json').read_text());self.assertTrue(data['cases'][0]['visual_truncated']);self.assertIsNone(data['cases'][0]['timeline'][0]['duration_minutes'])
        self.assertIn('TRUNCATED',(self.root/'what-if.html').read_text())

if __name__=='__main__':unittest.main()
