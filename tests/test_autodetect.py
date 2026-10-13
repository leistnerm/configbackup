import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
import autodetect as a
import configure


def response(out='',code=0):return types.SimpleNamespace(stdout=out,returncode=code)


class AutodetectTests(unittest.TestCase):
    def report(self,system='linux',present=()):
        names=('git','psql','pg_dump','pg_dumpall','pwsh','powershell.exe','sqlpackage','dotnet','docker','podman','openssl','brew','winget','apt-get','sudo')
        return {'platform':system,'tools':{n:{'path':'/tools/'+n if n in present else None,'status':'present' if n in present else 'missing','version':'1.0' if n in present else ''} for n in names},'powershell_modules':[],'suggestions':[]}
    def test_client_presence_does_not_connect_or_imply_server(self):
        calls=[]
        def run(args,**kwargs):calls.append(args);return response('tool version')
        r=a.discover(system='linux',which=lambda n:'/tools/'+n if n in ('git','psql','pg_dump','pg_dumpall') else None,execute=run,roots=[])
        self.assertFalse(r['services']);self.assertTrue(any(s['id']=='postgresql' and 'no server' in s['reason'] for s in r['suggestions']))
        self.assertTrue(all(c[-1]=='--version' for c in calls))
    def test_windows_named_instances_and_independent_module_failure(self):
        def run(args,**kwargs):
            if args[-1]=='--version':return response('PowerShell 7')
            if 'Get-Service' in args[-1]:return response(json.dumps([{'name':'MSSQL$TEST','state':'Running'},{'name':'postgresql-x64-17','state':'Stopped'}]))
            return response('private failure text must not appear',1)
        r=a.discover(system='windows',which=lambda n:'C:/Tools/pwsh.exe' if n=='pwsh' else None,execute=run,roots=[])
        self.assertEqual({s['kind'] for s in r['services']},{'sqlserver','postgresql'});self.assertTrue(r['probe_errors']);self.assertNotIn('private failure',json.dumps(r))
    def test_container_hint_reads_only_image_names(self):
        calls=[]
        def run(args,**kwargs):
            calls.append(args)
            return response('"unix:///fixture/docker.sock"' if 'inspect' in args else 'mcr.microsoft.com/mssql/server:2022-latest\npostgres:17\n')
        with patch.dict(os.environ,{},clear=True):r=a.discover(system='linux',which=lambda n:'/tools/docker' if n=='docker' else None,execute=run,roots=[])
        self.assertEqual({s['kind'] for s in r['services']},{'sqlserver','postgresql'});self.assertEqual(calls[-1],['/tools/docker','ps','--format','{{.Image}}']);self.assertEqual(len(calls),2)
    def test_remote_docker_context_is_not_contacted(self):
        calls=[]
        def run(args,**kwargs):calls.append(args);return response('"ssh://remote.example"')
        with patch.dict(os.environ,{},clear=True):r=a.discover(system='linux',which=lambda n:'/tools/docker' if n=='docker' else None,execute=run,roots=[])
        self.assertEqual(len(calls),1);self.assertIn('inspect',calls[0]);self.assertFalse(r['services']);self.assertTrue(r['probe_errors'])
    def test_timeout_is_unavailable_and_raw_errors_not_leaked(self):
        def run(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],15)
        r=a.discover(system='windows',which=lambda n:'/tools/pwsh' if n=='pwsh' else None,execute=run,roots=[])
        self.assertEqual(r['tools']['pwsh']['status'],'unverified');self.assertTrue(r['probe_errors']);self.assertFalse(r['services'])
    def test_bounded_standard_paths(self):
        with tempfile.TemporaryDirectory() as td:
            file=Path(td)/'pg_dump';file.write_text('fixture');file.chmod(0o700)
            r=a.discover(system='linux',which=lambda n:None,roots=[td],execute=lambda *args,**kw:response('pg_dump 17'))
            self.assertEqual(r['tools']['pg_dump']['path'],str(file));self.assertIsNone(r['tools']['psql']['path'])
    def test_fixed_package_plans_exclude_server_installers(self):
        mac=self.report('macos',('brew',));self.assertEqual(a.installation_plan('postgresql-client',mac)['commands'],[['/tools/brew','install','libpq']])
        win=self.report('windows',('winget',));self.assertFalse(a.installation_plan('postgresql-client',win)['commands']);self.assertIn('Microsoft.PowerShell',a.installation_plan('powershell',win)['commands'][0])
        linux=self.report('linux',('apt-get','sudo'));self.assertEqual(a.installation_plan('postgresql-client',linux)['commands'][0][-1],'postgresql-client')
        with self.assertRaises(ValueError):a.installation_plan('sqlserver-server',mac)
    def test_sql_sdk_and_module_trust_prompts(self):
        r=self.report('macos',('dotnet','pwsh'));self.assertEqual(a.installation_plan('sqlpackage',r)['commands'][0][-1],'Microsoft.SqlPackage');self.assertEqual(a.installation_plan('powershell',r)['commands'][0][-1],'PowerShell')
        r['tools']['dotnet']['version']='';self.assertFalse(a.installation_plan('sqlpackage',r)['commands'])
        cmd=a.installation_plan('dbatools',r)['commands'][0][-1];self.assertIn('-Scope CurrentUser',cmd);self.assertNotIn('-Force',cmd);self.assertNotIn('SkipPublisher',cmd)
    def test_decline_never_executes(self):
        calls=[]
        with contextlib.redirect_stdout(io.StringIO()):result=a.install('git',input_fn=lambda _:'no',execute=lambda *args,**kw:calls.append(args),discover_fn=lambda:self.report('macos',('brew',)))
        self.assertEqual(result['status'],'declined');self.assertFalse(calls)
    def test_already_installed_never_executes(self):
        result=a.install('git',input_fn=lambda _:self.fail('No confirmation needed'),execute=lambda *args,**kw:self.fail('No install'),discover_fn=lambda:self.report(present=('git',)))
        self.assertEqual(result['status'],'verified');self.assertTrue(result['already_present'])
    def test_failure_and_fresh_post_install_probe(self):
        before=self.report('macos',('brew',));after=self.report('macos',('brew','git'));calls=[]
        def run(args,**kwargs):calls.append((args,kwargs));return response()
        sequence=iter([before,after])
        with contextlib.redirect_stdout(io.StringIO()):result=a.install('git',input_fn=lambda _:'INSTALL',execute=run,discover_fn=lambda:next(sequence))
        self.assertEqual(result['status'],'verified');self.assertFalse(calls[0][1]['shell'])
        with contextlib.redirect_stdout(io.StringIO()):result=a.install('git',input_fn=lambda _:'INSTALL',execute=lambda *args,**kw:response(code=2),discover_fn=lambda:before)
        self.assertEqual(result['status'],'failed')
        with contextlib.redirect_stdout(io.StringIO()):result=a.install('git',input_fn=lambda _:'INSTALL',execute=run,discover_fn=lambda:before)
        self.assertEqual(result['status'],'unverified')
    def test_venv_is_new_and_supports_offline_wheels(self):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);wheels=base/'wheels';wheels.mkdir();plan=a.installation_plan('python-packages',self.report(),venv=base/'venv space',wheelhouse=wheels)
            self.assertEqual(plan['commands'][0][1:3],['-m','venv']);self.assertIn('--no-index',plan['commands'][1]);self.assertIn('ruamel.yaml',plan['postcheck'][-1])
            with self.assertRaises(ValueError):a.installation_plan('python-packages',self.report(),venv=base)
            with contextlib.redirect_stdout(io.StringIO()):result=a.install('python-packages',venv=base/'venv',input_fn=lambda _:'INSTALL',execute=lambda *args,**kw:response(),discover_fn=self.report)
            self.assertEqual(result['status'],'verified')
    def test_guided_pg_addition_is_unsaved_and_collision_safe(self):
        cfg={'backup':{'root':'archive'},'tasks':[]};replies=iter(['pg','connection','127.0.0.1','5432','reader','/client/bin','PG_PASSWORD'])
        with contextlib.redirect_stdout(io.StringIO()):a.add_suggestion(cfg,'postgresql',self.report(present=('pg_dump','psql','pg_dumpall')),lambda _:next(replies))
        self.assertEqual(cfg['connections']['connection']['access_profile'],'read-only');self.assertEqual(len(cfg['tasks']),2);self.assertTrue(cfg['tasks'][1]['collection_manifest']);before=copy.deepcopy(cfg)
        with self.assertRaises(ValueError):a.add_suggestion(cfg,'postgresql',self.report(),lambda _:'pg')
        self.assertEqual(before,cfg)
    def test_cli_detect_does_not_create_config(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'missing.yaml'
            with patch.object(a,'discover',return_value={'status':'complete'}),contextlib.redirect_stdout(io.StringIO()):self.assertEqual(configure.main(['--config',str(path),'--autodetect']),0)
            self.assertEqual(list(Path(td).iterdir()),[])

if __name__=='__main__':unittest.main()
