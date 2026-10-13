import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
import os
import types

ROOT=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('setup',ROOT/'prerequisite_setup.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.events=[];self.calls=[]
    def tearDown(self):self.tmp.cleanup()
    def run_case(self,answers=(),missing=True,git=True,winget=True,fail=None):
        answers=iter(answers); installed={'git':git}
        def locate(name):return '/mock/'+name if installed['git'] and name=='git' or winget and name=='winget' else None
        def inspect(exe):return {'checks':[{'status':'INCOMPLETE' if missing and exe=='base-python' else 'PASS'}]}
        def execute(args,log):
            self.calls.append((args,log))
            if '--id' in args:installed['git']=True
            return 9 if fail and fail in args else 0
        with contextlib.redirect_stdout(io.StringIO()):
            return m.prepare(self.root,inspect,self.events.append,python='base-python',windows=True,
                ask=lambda _:next(answers),execute=execute,locate=locate,refresh=lambda:None)
    def test_decline_never_creates_environment_or_installs(self):
        self.assertEqual(self.run_case(['n'],git=True),'base-python');self.assertEqual(self.calls,[]);self.assertFalse((self.root/'test-environment').exists())
    def test_online_environment_selected_only_after_fresh_probe(self):
        selected=self.run_case(['o']);self.assertIn('test-environment',selected)
        self.assertEqual(len(self.calls),2);self.assertIn('--only-binary=:all:',self.calls[1][0]);self.assertEqual(self.events[-1]['status'],'VERIFIED')
    def test_offline_wheels_are_argument_safe_and_disable_indexes(self):
        wheels=self.root/'wheels with spaces & quote';wheels.mkdir()
        self.run_case(['l',str(wheels)]);args=self.calls[1][0]
        self.assertIn('--no-index',args);self.assertIn(str(wheels.resolve()),args)
    def test_bad_wheel_folder_does_not_create_venv(self):
        self.run_case(['l',str(self.root/'missing')]);self.assertEqual(self.calls,[]);self.assertFalse((self.root/'test-environment').exists())
    def test_existing_environment_is_never_overwritten(self):
        (self.root/'test-environment').mkdir();(self.root/'test-environment/keep').write_text('untouched')
        self.run_case(['o']);self.assertEqual(self.calls,[]);self.assertEqual((self.root/'test-environment/keep').read_text(),'untouched')
    def test_venv_failure_stops_pip_and_keeps_base(self):
        self.assertEqual(self.run_case(['o'],fail='venv'),'base-python');self.assertEqual(len(self.calls),1);self.assertEqual(self.events[-1]['status'],'FAILED')
    def test_pip_failure_keeps_base(self):
        self.assertEqual(self.run_case(['o'],fail='pip'),'base-python');self.assertEqual(self.events[-1]['status'],'FAILED')
    def test_git_decline_starts_no_installer(self):
        self.run_case(['n'],missing=False,git=False);self.assertEqual(self.calls,[])
    def test_git_install_then_probe_no_security_bypasses(self):
        self.run_case(['y'],missing=False,git=False);self.assertEqual(len(self.calls),2)
        args=self.calls[0][0];self.assertIn('Git.Git',args);self.assertIn('--exact',args);self.assertIn('--no-upgrade',args)
        self.assertFalse(any(x in args for x in ('--silent','--force','--allow-reboot','--ignore-security-hash','--accept-package-agreements')))
        self.assertEqual(self.calls[1][0][-1],'--version')
    def test_missing_winget_runs_no_installer(self):
        self.run_case(missing=False,git=False,winget=False);self.assertEqual(self.calls,[]);self.assertEqual(self.events[-1]['status'],'UNAVAILABLE')
    def test_eof_and_interrupt_are_not_consent(self):
        for exc in (EOFError,KeyboardInterrupt):
            def ask(_):raise exc()
            self.assertEqual(m.choice('?',{'y'},ask),'n')
    def test_non_windows_cannot_install(self):
        chosen=m.prepare(self.root,lambda _:self.fail('probe not expected'),self.events.append,windows=False,execute=lambda *a:self.fail('install not expected'))
        self.assertEqual(self.events[-1]['status'],'NOT RUN')
    def test_pip_target_overrides_are_removed(self):
        calls=[]
        def fake(args,**kw):calls.append((args,kw));return types.SimpleNamespace(returncode=9)
        with patch.dict(os.environ,{'PIP_TARGET':'outside','PIP_PREFIX':'outside','PIP_USER':'1','PIP_CONFIG_FILE':'evil.ini','PIP_INDEX_URL':'https://mirror.example/simple'}),patch.object(m.subprocess,'run',side_effect=fake),contextlib.redirect_stdout(io.StringIO()):
            m.prepare(self.root,lambda _: {'checks':[{'status':'INCOMPLETE'}]},self.events.append,windows=True,ask=lambda _:'o',locate=lambda _: '/git')
        env=calls[0][1]['env'];self.assertEqual(env['PIP_CONFIG_FILE'],os.devnull);self.assertNotIn('PIP_TARGET',env);self.assertNotIn('PIP_PREFIX',env);self.assertNotIn('PIP_USER',env);self.assertEqual(env['PIP_INDEX_URL'],'https://mirror.example/simple')

    def test_comment_preserving_yaml_dependency_is_bounded_in_both_install_modes(self):
        for wheels in (None,self.root/'approved wheels'):
            args=m.pip_arguments('fixture-python',wheels)
            self.assertIn('ruamel.yaml>=0.18.6,<0.19',args)
            self.assertIn('PyYAML>=6.0,<7',args)
            self.assertIn('tzdata>=2024.1',args)
            self.assertIn('--only-binary=:all:',args)
            self.assertEqual('--no-index' in args,wheels is not None)

    def test_successful_install_with_failed_fresh_probe_keeps_old_interpreter(self):
        with contextlib.redirect_stdout(io.StringIO()):
            selected=m.prepare(self.root,lambda _: {'checks':[{'status':'INCOMPLETE'}]},
                self.events.append,python='base-python',windows=True,ask=lambda _:'o',
                execute=lambda *args:0,locate=lambda _: '/fixture/git')
        self.assertEqual(selected,'base-python')
        self.assertEqual(self.events[-1]['status'],'FAILED')
        self.assertIn('fresh dependency checks',self.events[-1]['detail'])

if __name__=='__main__':unittest.main(verbosity=2)
