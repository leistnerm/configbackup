import copy,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from configbackup import ConfigLoader,BackupEngine,ConfigError
from completeness import publish,section
import database_connections as shared
import database_diagnostics as diag
import readiness
import setup_checks
import startup_launcher

class ReadinessTests(unittest.TestCase):
    def config(self,base):
        return {'backup':{'root':str(base/'archive')},'connections':{'pg':{'engine':'postgresql','host':'localhost','port':5432,'databases':['one'],'sections':{'objects':False}}},'tasks':[{'name':'collect','type':'execute','connection':'pg','collection':{'databases':['two']},'sections':{'schedulers':False},'required_sections':['databases/two/schema.sql']}]}
    def result(self,rows,status='complete'):
        return {'name':'test','observed_at':'2026-10-09T12:00:00+00:00','status':status,'connection':{'status':'connected','identity':'reader'},'sections':rows,'execution':{'platform':'fixture','effective_uid':123,'account':'reader'}}
    def test_sql_unicode_password_literal_is_blocked_without_exposing_value(self):
        from secret_scan import scan_bytes
        for value in (b"CREATE LOGIN reader WITH PASSWORD=N'unicode-secret';",b"ALTER LOGIN reader WITH PASSWORD='a b c d';",b"CREATE LOGIN reader WITH PASSWORD=N'abc''d';"):
            findings=scan_bytes(value,'fixture.sql');self.assertTrue(findings);self.assertNotIn('unicode-secret',str(findings))
        self.assertFalse(scan_bytes(b"CREATE LOGIN reader WITH PASSWORD=@password;",'fixture.sql'))
    def test_shared_task_resolution_environment_and_selection(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);config=self.config(base);config['tasks'][0]['environment']={'PGAPPNAME':'test-app'}
            task,env=shared.task_context(config,base/'config.yaml','collect')
            self.assertEqual(task['_database_profile']['databases'],['two'])
            self.assertEqual(task['_database_profile']['sections'],{'objects':False,'schedulers':False})
            self.assertEqual(env['PGAPPNAME'],'test-app');self.assertFalse((base/'archive').exists())
            self.assertEqual(config['connections']['pg']['databases'],['one'])
    def test_shared_task_conflicts_and_unknown_fields_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            for extra in ({'arguments':['custom']},{'executable':'wrapper'},{'collection':{'password':'bad'}},{'timeout':10},{'connection':'missing'},{'required_sections':'schema'}):
                config=self.config(Path(td));config['tasks'][0].update(extra)
                with self.subTest(extra=extra),self.assertRaises(ConfigError):ConfigLoader(Path(td)/'config')._resolve(config)
    def test_disabled_connection_disables_task_and_no_probe(self):
        with tempfile.TemporaryDirectory() as td,patch.object(diag,'process') as probe:
            base=Path(td);config=self.config(base);config['connections']['pg']['enabled']=False
            result=readiness.run_task(config,base/'config','collect');self.assertEqual(result['readiness'],'not_ready');probe.assert_not_called()
    def test_required_scope_not_satisfied_by_sibling_or_failed_parent(self):
        rows=[{'section':'databases/good','status':'available','verified_paths':['databases/good/schema.sql']},{'section':'databases/bad','status':'unavailable'}]
        result=self.result(rows,'partial');readiness.requirements(result,['databases/*/schema.sql'])
        self.assertEqual(result['readiness'],'not_ready')
        self.assertIn('databases/bad',result['requirements'][0]['unavailable'])
        for status in ('disabled','not_applicable','not_tested'):
            value=self.result([{'section':'required','status':status}]);readiness.requirements(value,['required']);self.assertEqual(value['readiness'],'not_ready')
    def test_requirements_use_verified_paths_and_optional_failures_warn(self):
        result=self.result([{'section':'databases/db','status':'available','verified_paths':['databases/db/schema.sql']},{'section':'optional','status':'unavailable'}],'partial')
        readiness.requirements(result,['databases/db/schema.sql']);self.assertEqual(result['readiness'],'ready_with_warnings')
        readiness.requirements(result,['databases/db/native.sql']);self.assertEqual(result['readiness'],'not_ready')
    def test_capability_baseline_survives_failure_recovery_and_setting_change(self):
        with tempfile.TemporaryDirectory() as td:
            profile={'engine':'postgresql','name':'test'}
            def record(rows,status='complete',p=profile):
                result=self.result(rows,status);readiness.requirements(result,[]);path=readiness.record_capabilities(result,p,td)
                self.assertEqual(path.stat().st_mode&0o777,0o600);return result
            first=record([{'section':'db/schema','status':'available'}]);self.assertTrue(first['capabilities']['initialized'])
            lost=record([{'section':'db','status':'unavailable'}],'partial');self.assertEqual(lost['capabilities']['lost'],['db/schema'])
            again=record([{'section':'db','status':'unavailable'}],'partial');self.assertEqual(again['capabilities']['lost'],['db/schema'])
            unknown=record([],'incomplete');self.assertEqual(unknown['capabilities']['unverified'],['db/schema'])
            recovered=record([{'section':'db/schema','status':'available'}]);self.assertEqual(recovered['capabilities']['recovered'],['db/schema'])
            changed=record([],p={**profile,'schema':False});self.assertTrue(changed['capabilities']['initialized']);self.assertEqual(changed['capabilities']['lost'],[])
    def test_guidance_no_automatic_grants_or_tls_bypass(self):
        result=self.result([],'incomplete');result['connection']={'status':'connected','permissions':{'pg_monitor':False,'pg_read_all_data':False,'bypass_rls':False}}
        readiness.requirements(result,['schema']);readiness.guided_fixes(result,{'engine':'postgresql'})
        self.assertTrue(any(f['id']=='postgres-read-access' for f in result['guided_fixes']))
        self.assertTrue(all(f['applied'] is False for f in result['guided_fixes']))
    def test_broken_capability_state_does_not_discard_database_results(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);config=self.config(base)
            with patch.object(diag,'diagnose',return_value=self.result([{'section':'databases/two/schema.sql','status':'available'}])),patch.object(readiness,'record_capabilities',side_effect=OSError('private state inaccessible')):
                result=readiness.run_task(config,base/'config','collect',history=base/'history')
            self.assertEqual(result['sections'][0]['status'],'available');self.assertEqual(result['capability_history']['status'],'unavailable');self.assertEqual(result['readiness'],'not_ready')
    def test_metadata_selective_test_does_not_change_config(self):
        with tempfile.TemporaryDirectory() as td:
            config=self.config(Path(td));original=copy.deepcopy(config)
            with patch.object(diag,'diagnose',return_value=self.result([])) as probe:
                readiness.run_task(config,Path(td)/'config','collect',['chosen'],['inventory'],True)
                profile=probe.call_args.args[0];self.assertFalse(profile['schema']);self.assertEqual(profile['databases'],['chosen']);self.assertFalse(profile['sections']['inventory'])
            self.assertEqual(config,original)
    def test_real_generated_launcher_records_account_and_missing_secret(self):
        with tempfile.TemporaryDirectory(prefix='cb space ') as td:
            base=Path(td);config=self.config(base);config['connections']['pg']['password_env']='CB_TEST_MISSING_PASSWORD'
            path=base/'configuration.json';path.write_text(json.dumps(config))
            startup_launcher.generate(path,base/'launch','linux',sys.executable,mode='diagnostic',task='collect',report_directory=str(base/'reports'))
            env={k:v for k,v in os.environ.items() if k!='CB_TEST_MISSING_PASSWORD'}
            cp=subprocess.run(['/bin/sh',str(base/'launch/run-configbackup.sh')],env=env,capture_output=True,text=True)
            self.assertEqual(cp.returncode,1,cp.stdout+cp.stderr)
            reports=list((base/'reports').glob('*.json'));self.assertEqual(len(reports),1)
            report=json.loads(reports[0].read_text());self.assertEqual(report['execution']['launcher_mode'],'diagnostic');self.assertFalse(report['execution']['scheduler_proven']);self.assertTrue(report['execution']['account']);self.assertIn('missing',report['connection']['reason'])
            self.assertFalse((base/'archive').exists())
    def test_setup_write_probe_cleanup_missing_path_and_no_implicit_send(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);before=set(base.iterdir());result=setup_checks.check_directory(base)
            self.assertEqual(result['status'],'available');self.assertEqual(set(base.iterdir()),before)
            self.assertEqual(setup_checks.check_directory(base/'missing')['status'],'unavailable')
    def test_smtp_probe_never_submits_mail(self):
        with patch('smtplib.SMTP') as factory:
            smtp=factory.return_value.__enter__.return_value;smtp.ehlo.return_value=(250,b'ok')
            setup_checks.smtp_check({'host':'localhost','tls':'none','allow_insecure_localhost':True},{})
            smtp.send_message.assert_not_called();smtp.sendmail.assert_not_called();smtp.login.assert_not_called()
    def test_identity_change_stays_flagged_until_original_identity_returns(self):
        with tempfile.TemporaryDirectory() as td:
            def record(identity):
                result=self.result([{'section':'db','status':'available'}]);result['connection']['identity']=identity
                readiness.requirements(result,[]);readiness.record_capabilities(result,{'engine':'postgresql'},td);return result
            record('original')
            for _ in range(2):
                changed=record('unexpected');self.assertEqual(changed['readiness'],'not_ready');self.assertTrue(changed['capabilities']['database_identity_changed'])
            self.assertFalse(record('original')['capabilities']['database_identity_changed'])
    def test_setup_git_remote_read_and_real_smtp_probe_send_separation(self):
        import socketserver,threading
        commands=[];messages=[]
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'220 fixture ready\r\n')
                while True:
                    line=self.rfile.readline()
                    if not line:return
                    cmd=line.split()[0].upper();commands.append(cmd)
                    if cmd==b'DATA':
                        self.wfile.write(b'354 send\r\n');chunks=[]
                        while True:
                            line=self.rfile.readline()
                            if line==b'.\r\n':break
                            chunks.append(line)
                        messages.append(b''.join(chunks));self.wfile.write(b'250 accepted\r\n')
                    elif cmd==b'QUIT':self.wfile.write(b'221 bye\r\n');return
                    else:self.wfile.write(b'250 OK\r\n')
        with tempfile.TemporaryDirectory() as td,socketserver.TCPServer(('127.0.0.1',0),Handler) as server:
            base=Path(td);repo=base/'git';remote=base/'remote.git';archive=base/'archive';stage=base/'stage';archive.mkdir();stage.mkdir()
            for cmd in (['git','init',str(repo)],['git','init','--bare',str(remote)],['git','-C',str(repo),'remote','add','origin',str(remote)]):subprocess.run(cmd,check=True,capture_output=True)
            channel={'id':'fixture','type':'smtp','host':'127.0.0.1','port':server.server_address[1],'tls':'none','allow_insecure_localhost':True,'from':'fixture@example.invalid','to':['fixture@example.invalid']}
            config={'backup':{'root':str(archive)},'internal':{'staging_root':str(stage)},'git':{'repository':str(repo)},'tasks':[],'monitoring':{'notifications':{'channels':[channel]}}}
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                result=setup_checks.run(config,base/'config',probe_notifications=True)
                self.assertEqual(result['status'],'complete');self.assertTrue(any(r['section']=='git:remote-read' and r['status']=='available' for r in result['checks']))
                self.assertNotIn(b'MAIL',commands);self.assertEqual(messages,[])
                setup_checks.test_notification(config,'fixture');self.assertEqual(len(messages),1)
                self.assertIn(b'multipart/alternative',messages[0]);self.assertNotIn(str(archive).encode(),messages[0])
            finally:server.shutdown();thread.join()
    def test_required_collection_failure_still_archives_successful_scopes(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);config=self.config(base);config['tasks'][0].update(output_directory=str(base/'stage'))
            config['tasks'].append({'name':'archive','type':'directory','source':str(base/'stage'),'destination':'db','collection_manifest':True,'depends_on':['collect']})
            engine=BackupEngine(ConfigLoader(base/'config')._resolve(config))
            def collect(profile,output,env,cwd):
                root=Path(output);(root/'good').mkdir(parents=True,exist_ok=True);(root/'good/file').write_text('verified')
                with patch.dict(os.environ,{'CONFIGBACKUP_RUN_ID':env['CONFIGBACKUP_RUN_ID']}):publish(root,[section(root,'good')])
                return subprocess.CompletedProcess([],0,b'',b'')
            with patch.object(shared,'collect',side_effect=collect):code=engine.run()
            self.assertEqual(code,4);self.assertEqual(engine.results['collect'].status,'partial');self.assertEqual(engine.results['archive'].status,'success')
            self.assertTrue(list((base/'archive/db/good').glob('*')))

if __name__=='__main__':unittest.main()
