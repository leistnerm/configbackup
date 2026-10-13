import copy
import contextlib
import io
import json
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import schedule_discovery as s

class ScheduleDiscoveryTests(unittest.TestCase):
    def test_windows_static_quoted_arguments_and_dynamic_rejection(self):
        xml='''<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>pwsh.exe</Command><Arguments>-File "C:\\Jobs\\backup.ps1" --config "C:\\Jobs\\private config.yaml"</Arguments><WorkingDirectory>C:\\Jobs</WorkingDirectory></Exec></Actions></Task>'''
        rows=s.candidates(s.windows_xml(xml,'job'))['candidates'];self.assertEqual({r['path'] for r in rows},{r'C:\Jobs\backup.ps1',r'C:\Jobs\private config.yaml'});self.assertFalse(any(r['selected'] for r in rows))
        jobs=[{'name':'dynamic','platform':'windows','arguments':['pwsh','-File','%APPDATA%\\job.ps1']}];self.assertFalse(s.candidates(jobs)['candidates']);self.assertTrue(s.candidates(jobs)['unresolved'])
    def test_inline_shell_commands_never_analyzed_or_executed(self):
        for arguments in (['sh','-c','python /job.py'],['powershell','-Command','Get-Content secret'],['cmd','/c','job.cmd']):
            report=s.candidates([{'name':'inline','arguments':arguments}]);self.assertFalse(report['candidates']);self.assertTrue(report['unresolved'])
    def test_cron_system_and_user_fields(self):
        jobs=s.cron('PATH=/bin\n# comment\n*/5 * * * * /usr/bin/python /jobs/job.py --config=/etc/job.yaml\n@reboot /jobs/restart.sh','user')
        self.assertEqual(len(jobs),2);self.assertEqual(len(s.candidates(jobs)['candidates']),3)
        system=s.cron('0 1 * * * root /jobs/job.sh','system',True);self.assertEqual(system[0]['arguments'],['/jobs/job.sh'])
    def test_launchd_working_directory_relative_paths_deduplicated(self):
        data=plistlib.dumps({'ProgramArguments':['python','job.py','--config','settings.yaml'],'WorkingDirectory':'/jobs'})
        jobs=s.launchd(data,'agent');report=s.candidates(jobs+jobs,exists=lambda p:True);self.assertEqual([r['path'] for r in report['candidates']],['/jobs/job.py','/jobs/settings.yaml']);self.assertTrue(all(len(r['jobs'])==1 for r in report['candidates']))
    def test_relative_without_workdir_and_traversal_unresolved(self):
        report=s.candidates([{'name':'unknown','arguments':['python','job.py','--config','../secret.yaml']}]);self.assertFalse(report['candidates']);self.assertEqual(len(report['unresolved']),2)
    def test_systemd_reset_and_timer_unit_mapping(self):
        jobs=s.systemd('[Service]\nWorkingDirectory=/jobs\nExecStart=/old.py\nExecStart=\nExecStart=/usr/bin/python new.py --config settings.yaml','timer')
        self.assertEqual(len(jobs),1);self.assertEqual(len(s.candidates(jobs)['candidates']),2)
        calls=[]
        def execute(args,**kwargs):
            calls.append(args)
            out='nightly.timer enabled\n' if 'list-unit-files' in args else '[Timer]\nUnit=custom.service\n' if 'nightly.timer' in args else '[Service]\nExecStart=/jobs/job.sh\n'
            return subprocess.CompletedProcess(args,0,out,'')
        result=s.discover(system='linux',roots=[],which=lambda n:'/bin/systemctl' if n=='systemctl' else None,execute=execute)
        self.assertTrue(any('custom.service' in args for args in calls));self.assertEqual(result['candidates'][0]['path'],'/jobs/job.sh');self.assertFalse(any('start' in args for args in calls))
    def test_definitions_parse_failure_isolated_and_no_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);bad=root/'bad.xml';bad.write_text('<broken');good=root/'cron.txt';good.write_text('@reboot /jobs/job.sh')
            report=s.discover(definitions=[bad,good],execute=lambda *a,**k:self.fail('Definitions never execute'))
            self.assertEqual(len(report['candidates']),1);self.assertEqual(len(report['probe_errors']),1)
    def test_windows_probe_exports_only_and_individual_errors(self):
        xml='<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec><Command>C:\\job.ps1</Command></Exec></Actions></Task>'
        calls=[]
        def execute(args,**kw):calls.append(args);return subprocess.CompletedProcess(args,0,json.dumps([{'name':'ok','xml':xml},{'name':'denied','unavailable':True}]),'')
        report=s.discover(system='windows',which=lambda _:'pwsh',execute=execute);self.assertEqual(len(report['candidates']),1);self.assertEqual(len(report['probe_errors']),1);self.assertIn('Export-ScheduledTask',calls[0][-1]);self.assertNotIn('Start-ScheduledTask',calls[0][-1])
    def test_selection_explicit_deduplicated_and_atomic(self):
        report={'candidates':[{'path':'/job.py','status':'available'},{'path':'/missing.py','status':'missing or not a regular file'}]};cfg={}
        s.select(cfg,report,[]);self.assertEqual(cfg['backup']['configuration_files'],[]);s.select(cfg,report,['/job.py']);s.select(cfg,report,['/job.py']);self.assertEqual(cfg['backup']['configuration_files'],['/job.py'])
        old=copy.deepcopy(cfg)
        with self.assertRaises(ValueError):s.select(cfg,report,['/job.py','/missing.py'])
        self.assertEqual(cfg,old)
        answers=iter(['1','2']);selected={}
        with patch.object(s,'discover',return_value=report),contextlib.redirect_stdout(io.StringIO()):s.wizard(selected,lambda _:next(answers))
        self.assertEqual(selected['backup']['configuration_files'],['/job.py'])
    def test_bad_cron_line_does_not_hide_good_line(self):
        jobs=s.cron('@reboot python "bad\n@reboot /jobs/good.sh','mixed')
        report=s.candidates(jobs);self.assertEqual(len(report['unresolved']),1);self.assertEqual(report['candidates'][0]['path'],'/jobs/good.sh')
    def test_working_and_script_directories_suggest_only_top_level_configs(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td).resolve();work=base/'working';work.mkdir();scripts=base/'scripts';scripts.mkdir()
            for name in ('app.ini','app.exe.config','app.json','app.yaml','app.yml','app.toml','app.conf','app.properties','app.xml'):(work/name).write_text('synthetic content never read')
            (work/'ignore.txt').write_text('x');(work/'nested').mkdir();(work/'nested/hidden.yaml').write_text('x');(scripts/'settings.json').write_text('x')
            jobs=[{'name':'job','platform':'windows' if __import__('os').name=='nt' else 'unix','working_directory':str(work),'arguments':['python',str(scripts/'job.py')]}]
            report=s.candidates(jobs);paths={r['path'] for r in report['candidates']};self.assertIn(str(scripts/'settings.json'),paths);self.assertIn(str(work/'app.exe.config'),paths);self.assertNotIn(str(work/'nested/hidden.yaml'),paths);self.assertNotIn(str(work/'ignore.txt'),paths);self.assertFalse(any(r['selected'] for r in report['candidates']))
            jobs[0]['arguments']=['sh','-c','do not execute'];inline=s.candidates(jobs);self.assertTrue(inline['unresolved']);self.assertIn(str(work/'app.ini'),{r['path'] for r in inline['candidates']})
    def test_directory_limits_and_symlink_do_not_guess(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/'app.yaml').write_text('x');(root/'other.txt').write_text('x')
            jobs=[{'name':'job','platform':'windows' if __import__('os').name=='nt' else 'unix','working_directory':str(root),'arguments':['app']}]
            with patch.object(s,'MAX_DIRECTORY_ENTRIES',1):report=s.candidates(jobs)
            self.assertFalse(report['candidates']);self.assertIn('limit',report['directory_scans'][0]['status'])
            link=root/'link';link.symlink_to(root,target_is_directory=True);jobs[0]['working_directory']=str(link);self.assertIn('symlink',s.candidates(jobs)['directory_scans'][0]['status'])
    def test_shared_executable_directory_skipped(self):
        import sys,os
        report=s.candidates([{'name':'job','platform':'windows' if os.name=='nt' else 'unix','arguments':[sys.executable]}])
        self.assertTrue(any('shared' in r['status'] for r in report['directory_scans']))
    def test_probe_timeout_and_large_definition(self):
        report=s.discover(system='windows',which=lambda _:'pwsh',execute=lambda *a,**k:(_ for _ in ()).throw(subprocess.TimeoutExpired('pwsh',30)));self.assertTrue(report['probe_errors'])
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'large.xml';p.write_text('x'*(s.MAX_BYTES+1));self.assertTrue(s.discover(definitions=[p])['probe_errors'])

if __name__=='__main__':unittest.main()
