import contextlib
import copy
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
import configbackup as cb
import configure
from config_editor import duplicate_group, effective_preview
from completeness import publish,section,Coverage
from recovery import recover,plan_restore


class ConfigurationAndRestoreSafetyTests(unittest.TestCase):
    def config(self,root,tasks):
        return cb.ConfigLoader(root/'c.yaml')._resolve({'backup':{'root':str(root/'archive')},'options':{'log_level':'CRITICAL'},'tasks':tasks})

    def test_quoted_switches_rejected_before_coercion(self):
        for place in ('task','defaults','connection','section','report'):
            with self.subTest(place=place),tempfile.TemporaryDirectory() as td:
                root=Path(td);cfg={'backup':{'root':str(root/'archive')},'tasks':[{'name':'a','type':'directory','source':str(root/'src')}]}
                if place=='task':cfg['tasks'][0]['enabled']='false'
                if place=='defaults':cfg['defaults']={'required':'false'}
                if place=='connection':cfg['connections']={'pg':{'engine':'postgresql','schema':'false'}}
                if place=='section':cfg['tasks'][0]['sections']={'schema':{'enabled':'false'}}
                if place=='report':cfg['reporting']={'enabled':'false'}
                with self.assertRaises(cb.ConfigError):cb.ConfigLoader(root/'c.yaml')._resolve(cfg)
        for key in ('reports','analysis','reporting'):
            with self.assertRaises(cb.ConfigError):cb.ConfigLoader(Path('c'))._resolve({'backup':{'root':'archive'},key:False})

    def test_missing_boolean_field_edit_is_typed(self):
        replies=iter(['1','clean_output','no','4']);item={'name':'a'}
        with contextlib.redirect_stdout(io.StringIO()):configure.edit_mapping(item,lambda _:next(replies))
        self.assertIs(item['clean_output'],False)

    def test_duplicate_collector_group_is_independent(self):
        tasks=[{'name':'first','type':'execute','executable':sys.executable,'output_directory':'${CONFIGBACKUP_STAGING}/first'},
               {'name':'first-archive','type':'directory','source':'${CONFIGBACKUP_STAGING}/first','destination':'first','depends_on':['first'],'collection_manifest':True}]
        cloned=duplicate_group(tasks,0,'second');self.assertEqual(len(cloned),2)
        self.assertEqual(cloned[1]['source'],cloned[0]['output_directory'])
        self.assertEqual(cloned[1]['depends_on'],['second']);self.assertEqual(cloned[1]['destination'],'second')
        self.assertEqual(tasks[1]['depends_on'],['first'])
        default_copy=duplicate_group(tasks,0,'first-copy')
        self.assertEqual(default_copy[0]['output_directory'],'${CONFIGBACKUP_STAGING}/first-copy')
        self.assertEqual(default_copy[1]['source'],default_copy[0]['output_directory'])
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);preview=effective_preview({'backup':{'root':str(root/'archive')},'tasks':tasks+cloned},root/'c')
            self.assertNotEqual(preview['tasks'][0]['output_directory'],preview['tasks'][2]['output_directory'])
            self.assertFalse((root/'archive').exists())

    def test_overlap_rejected_before_clean_or_archive_creation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);out=root/'out';out.mkdir();(out/'keep').write_text('original')
            for second in (out,out/'child',root/'OUT'):
                tasks=[{'name':n,'type':'execute','executable':sys.executable,'output_directory':str(p),'clean_output':True} for n,p in [('one',out),('two',second)]]
                with self.assertRaises(cb.ConfigError):cb.BackupEngine(self.config(root,tasks))
            self.assertEqual((out/'keep').read_text(),'original');self.assertFalse((root/'archive').exists())

    def test_manifest_producer_rejected(self):
        with tempfile.TemporaryDirectory() as td,patch.dict(os.environ,{'CONFIGBACKUP_TASK_NAME':'wrong','CONFIGBACKUP_RUN_ID':'r'}):
            root=Path(td);(root/'file').write_text('value');publish(root,[section(root,'file')])
            with self.assertRaisesRegex(ValueError,'producer'):Coverage(root,'r',expected_producer='expected')

    def test_restore_dry_run_no_files_or_state_changes_and_glob_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source';source.mkdir();(source/'a.conf').write_text('original')
            cfg=self.config(root,[{'name':'files','type':'glob','source':str(source/'**/*.conf'),'destination':'config'}])
            engine=cb.BackupEngine(cfg)
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(engine.run(),0)
            before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
            dry=cb.BackupEngine(cfg,dry_run=True)
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(recover(dry,root/'restore'),0)
            self.assertFalse((root/'restore').exists())
            after={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            with self.assertRaisesRegex(ValueError,'live sources'):recover(dry,source/'nested')
            dry._close_logging()
            real=cb.BackupEngine(cfg)
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(recover(real,root/'restore'),0)
            real._close_logging();self.assertEqual((root/'restore/config/a.conf').read_text(),'original')

    def test_restore_plan_pure_and_collision_safe(self):
        item={'active':True,'versions':[{'path':'a','hash':'h','created':'2026-01-01T00:00:00+00:00'}]}
        state={'tasks':{'one':{'files':{'a':item}},'two':{'files':{'A':item}}}};before=copy.deepcopy(state)
        plan=plan_restore(state);self.assertTrue(plan['errors']);self.assertFalse(plan['verified']);self.assertEqual(state,before)

    def test_restore_same_second_selects_latest_collected_version(self):
        stamp='2026-10-11T12:00:00+00:00'
        versions=[{'path':'old','hash':'a','created':stamp},{'path':'new','hash':'b','created':stamp}]
        item={'active':True,'versions':versions,'deleted_generations':[{'deleted_at':'2026-10-12T00:00:00+00:00','versions':[{'path':'deleted','hash':'c','created':stamp}]}]}
        state={'tasks':{'config':{'files':{'config.yaml':item}}}}
        for cutoff in (None,stamp):
            with self.subTest(cutoff=cutoff):self.assertEqual(plan_restore(state,cutoff)['records'][0]['version']['path'],'new')

    def test_same_second_revert_is_archived_and_newest_survives_retention(self):
        stamp='2026-10-11T12:00:00+00:00'
        with tempfile.TemporaryDirectory() as td,patch.object(cb,'iso_now',return_value=stamp):
            root=Path(td);source=root/'source';source.write_text('A')
            cfg=self.config(root,[{'name':'files','type':'file','source':str(source),'retention':{'active':{'mode':'simple','min_versions':1,'max_versions':1}}}])
            # First test ordinary change detection with multiple equal timestamps.
            cfg['tasks'][0]['retention']['active']={'mode':'simple','min_versions':3,'max_versions':30}
            for content in ('A','B','A'):
                source.write_text(content);engine=cb.BackupEngine(cfg)
                with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(engine.run(),0)
            records=plan_restore(engine.state)['records'];self.assertEqual(len(records),1)
            self.assertEqual(engine._archive_state_path(records[0]['version']['path']).read_text(),'A')
            item=next(iter(engine.state.data['tasks']['files']['files'].values()))
            self.assertEqual(len(item['versions']),3)
            versions=item['versions']
            for policy in ({'mode':'simple','min_versions':1,'max_versions':1},{'mode':'tiered','min_versions':0,'tiers':[{'interval':'daily','forever':True}]}):
                self.assertEqual({v['path'] for v in engine._retention_candidates({'files':versions},policy)},{v['path'] for v in versions[:-1]})
            pruned=engine._apply_task_size_limit({'files':versions},{'min_versions':1,'max_size_bytes':1},[])
            self.assertEqual({v['path'] for v in pruned},{v['path'] for v in versions[:-1]})

    def test_comment_preservation_atomic_save_and_race(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.yaml';original='# Main comment\nbackup:\n  root: "archive" # keep this\ntasks: []\n';path.write_text(original)
            data=yaml.safe_load(original);data['backup']['include_hostname']=True
            backup=configure.save(data,path);self.assertEqual(backup.read_text(),original)
            self.assertIn('# Main comment',path.read_text());self.assertIn('# keep this',path.read_text());self.assertIn('"archive"',path.read_text())
            with self.assertRaisesRegex(ValueError,'changed on disk'):configure.save(data,path,expected_hash='stale')
            with self.assertRaisesRegex(ValueError,'changed on disk'):configure.save(data,path,expected_hash=None)
            self.assertTrue(yaml.safe_load(path.read_text())['backup']['include_hostname'])

    def test_configuration_file_backed_up_by_default_with_versions(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'configbackup.yaml'
            cfg={'backup':{'root':str(root/'archive')},'options':{'log_level':'CRITICAL'},'tasks':[]}
            path.write_text(yaml.safe_dump(cfg));original=path.read_bytes()
            for _ in range(2):
                engine=cb.BackupEngine(cb.ConfigLoader(path).load())
                with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(engine.run(),0)
            task=engine.state.data['tasks']['configbackup-settings'];item=task['files']['_configuration/configbackup.yaml']
            self.assertEqual(len(item['versions']),1)
            self.assertEqual(engine._archive_state_path(item['versions'][0]['path']).read_bytes(),original)
            self.assertIsNone(item.get('git_hash'))
            cfg['backup']['include_hostname']=False;path.write_text(yaml.safe_dump(cfg))
            engine=cb.BackupEngine(cb.ConfigLoader(path).load())
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(engine.run(),0)
            self.assertEqual(len(engine.state.data['tasks']['configbackup-settings']['files']['_configuration/configbackup.yaml']['versions']),2)
            self.assertIn('_configuration/configbackup.yaml',[r['logical'] for r in plan_restore(engine.state)['records']])

    def test_configuration_backup_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'config.yaml';path.write_text(yaml.safe_dump({'backup':{'root':str(Path(td)/'archive'),'include_configuration':False},'tasks':[]}))
            self.assertEqual(cb.ConfigLoader(path).load()['tasks'],[])

    def test_advanced_profiles_map_without_credential_arguments(self):
        from database_diagnostics import validate,pg_arguments
        from database_connections import prepare
        pg={'engine':'postgresql','exclude_databases':['template*'],'split_table_ddl':True,'bloat_tables':['public.a'],'command_timeout':32,'dump_timeout':33,'lock_wait_timeout':'4s'}
        validate(pg);args=pg_arguments(pg)
        self.assertIn('--split-table-ddl',args);self.assertEqual(args[args.index('--dump-timeout')+1],'33')
        self.assertIn('--bloat-table',args)
        sql={'engine':'sqlserver','server':'example,1433','include_index_health':True,'health_index_limit':10,'append_connection_string':'MultiSubnetFailover=True;ApplicationIntent=ReadOnly'}
        validate(sql);task=prepare({'name':'a','type':'execute','connection':'sql','collection':{'skip_inventory':True}},{'connections':{'sql':sql}})
        self.assertTrue(task['_database_profile']['skip_inventory'])
        with self.assertRaises(ValueError):validate({**sql,'append_connection_string':'Password=nope'})
        with self.assertRaises(ValueError):validate({**pg,'split_table_ddl':'false'})

    def test_email_sections_keep_their_list_schema(self):
        from config_schema import validate_types
        validate_types({'monitoring':{'notifications':{'channels':[{'id':'email','type':'smtp','sections':['alerts','changes']}]}}})
        validate_types({'monitoring':{'notifications':{'channels':[{'id':'email','type':'smtp','sections':{'alerts':{'enabled':True}}}]}}})
        with self.assertRaises(ValueError):validate_types({'tasks':[{'sections':['schema']}]})
        for key in ('reports','analysis'):
            with self.assertRaises(ValueError):configure.validate({'sources':[],key:123},'schedule')

if __name__=='__main__':unittest.main()
