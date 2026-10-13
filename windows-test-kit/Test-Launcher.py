import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile

KIT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('launcher', KIT / 'Run-WorkstationTests.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "result with spaces München 東京 & quote'"
        self.output.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def fake(self, args, output, log, env, timeout=240):
        self.calls.append((args, env, timeout))
        (output / log).write_text('SIMULATED CHILD PROCESS\n')
        if log == 'native-launcher.txt':
            return self.transform(log, {'status':'PASS'})[1] if self.transform else 0
        if log == 'offline-console.txt':
            (output / 'offline-checks').mkdir()
            data = {'status': 'PASS', 'results': [{'check': str(n), 'status': 'PASS'} for n in range(w.EXPECTED_CHECKS)]}
            path = output / 'offline-checks/results.json'
        elif log.startswith('component-run-'):
            root=Path(args[args.index('--output')+1]);root.mkdir()
            relative='windows-components/iis/server/settings/configuration.json'
            content={'name':'Fixture site', 'ordered_handlers':['a','b']}
            path=root/relative;path.parent.mkdir(parents=True);path.write_text(json.dumps(content))
            data={'schema_version':1,'finalized':True,'sections':[{'path':'windows-components/iis/server/settings','status':'complete','files':{relative:hashlib.sha256(path.read_bytes()).hexdigest()}}]}
            path=root/'collection-manifest.json'
        elif log == 'gpo-fixtures.log':
            data = {'status': 'PASS', 'passed': [str(n) for n in range(42)], 'skipped': [], 'tests_run': 42, 'failures': [], 'errors': []}
            path = output / 'gpo-fixtures.json'
        else:
            (output / 'gpo').mkdir()
            data = {'status': 'PASS', 'machine': {'account': 'SYNTHETIC ONLY'}, 'runs': [], 'comparisons': []}
            path = output / 'gpo/report.json'
        code = 0
        if self.transform:
            data, code = self.transform(log, data)
        w.write_json(path, data)
        return code

    def run_case(self, transform=None, missing_deps=False, offline=False, windows=True, gpresult='C:/Windows/System32/gpresult.exe', kit=KIT, components=(), details=(), offer_install=False):
        self.calls = []
        self.transform = transform
        checks = [{'name': 'PyYAML', 'status': 'INCOMPLETE' if missing_deps else 'PASS', 'detail': 'fixture'}]
        with patch.object(w, 'package_check', return_value=checks), patch.object(w, 'windows_details', return_value={'windows': windows, 'elevated': False, 'gpresult': gpresult}), patch.object(w.shutil, 'which', return_value='/fixture/git'), contextlib.redirect_stdout(io.StringIO()):
            return w.run_suite(kit, self.output, offline_only=offline, users=[r'EXAMPLE\normal user'], execute=self.fake, components=components, details=details, offer_install=offer_install)

    def test_native_launcher_failure_does_not_stop_gpo(self):
        def transform(log,data):return data,1 if log=='native-launcher.txt' else 0
        data=self.run_case(transform=transform)
        self.assertEqual(data['status'],'FAIL')
        self.assertEqual(data['stages'][2]['status'],'FAIL')
        self.assertEqual(data['stages'][3]['status'],'PASS')

    def test_components_in_same_report(self):
        data=self.run_case(components=['iis'])
        self.assertEqual(data['status'],'PASS');self.assertEqual(len(self.calls),6)
        self.assertEqual(data['components']['scopes'][0]['status'],'PASS')
        self.assertIn('windows-components/iis/server/settings',(self.output/'report.html').read_text())

    def test_component_failure_preserves_gpo_results(self):
        def transform(log,data):
            if log.startswith('component-run-'):
                data['sections'][0].update(status='failed',files={},error='Fixture denied')
                return data,6
            return data,0
        data=self.run_case(transform,components=['iis'])
        self.assertEqual(data['status'],'INCOMPLETE')
        self.assertEqual(data['stages'][3]['status'],'PASS')
        self.assertEqual(len(self.calls),6)
        self.assertIn('Fixture denied',(self.output/'report.html').read_text())

    def test_component_corrupt_manifest_fails_after_other_checks(self):
        def transform(log,data):
            if log=='component-run-2.log':data['finalized']=False
            return data,0
        data=self.run_case(transform,components=['iis'])
        self.assertEqual(data['status'],'FAIL');self.assertEqual(data['stages'][3]['status'],'PASS')

    def test_offline_components_are_explicitly_not_run(self):
        data=self.run_case(offline=True,components=['iis'],details=['iis-extended'])
        self.assertEqual(data['requested_details'],['iis-extended'])
        self.assertIn('iis-extended',(self.output/'report.html').read_text())
        self.assertEqual(len(data['stages']),5);self.assertEqual(data['stages'][-1]['status'],'NOT RUN')
        self.assertEqual(data['status'],'INCOMPLETE');self.assertEqual(len(self.calls),3)

    def test_new_setup_interpreter_is_rechecked_and_used_for_all_children(self):
        selected='/fixture/new environment/python'
        def prepare(output,inspect,record,**kwargs):
            record(dict(name='Fixture setup',status='VERIFIED',detail='Simulated environment'))
            return selected
        fake_spec=types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda module:None))
        fake_module=types.SimpleNamespace(prepare=prepare)
        probe={'checks':[dict(name='PyYAML',status='PASS',detail='Fresh fixture')], 'executable':selected,'python':'3.14 fixture'}
        with patch.object(w.importlib.util,'spec_from_file_location',return_value=fake_spec), patch.object(w.importlib.util,'module_from_spec',return_value=fake_module), patch.object(w,'probe_interpreter',return_value=probe) as inspected:
            data=self.run_case(offer_install=True)
        self.assertEqual(data['status'],'PASS');inspected.assert_called_once_with(selected)
        self.assertTrue(all(call[0][0]==selected for call in self.calls))
        self.assertEqual(data['machine']['test_python_executable'],selected)
        self.assertIn('Simulated environment',(self.output/'report.html').read_text())

    def test_full_simulated_run_and_argument_boundaries(self):
        data = self.run_case()
        self.assertEqual(data['status'], 'PASS')
        self.assertEqual(len(self.calls), 4)
        live = self.calls[-1][0]
        self.assertIn('--runs', live)
        self.assertEqual(live[-2:], ['--user', r'EXAMPLE\normal user'])
        self.assertIn('3', live)
        self.assertIn('30', live)
        self.assertIsNone(self.calls[-1][2])
        self.assertTrue((self.output / 'report.html').is_file())

    def test_missing_dependencies_still_runs_both_gpo_stages(self):
        data = self.run_case(missing_deps=True)
        self.assertEqual(data['status'], 'INCOMPLETE')
        self.assertEqual(data['stages'][0]['status'], 'NOT RUN')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(data['stages'][-1]['status'], 'PASS')

    def test_offline_skip_never_claims_pass(self):
        data = self.run_case(offline=True)
        self.assertEqual(data['status'], 'INCOMPLETE')
        self.assertEqual(len(self.calls), 3)

    def test_non_windows_runs_fixtures_only(self):
        data = self.run_case(windows=False)
        self.assertEqual(data['status'], 'INCOMPLETE')
        self.assertEqual(len(self.calls), 2)

    def test_missing_gpresult_is_incomplete(self):
        data = self.run_case(gpresult=None)
        self.assertEqual(data['status'], 'INCOMPLETE')
        self.assertEqual(len(self.calls), 3)

    def test_application_failure_does_not_stop_gpo(self):
        def transform(log, data):
            if log == 'offline-console.txt':
                data['status'] = 'FAIL'
                return data, 1
            return data, 0
        data = self.run_case(transform)
        self.assertEqual(data['status'], 'FAIL')
        self.assertEqual(data['stages'][-1]['status'], 'PASS')
        self.assertEqual(len(self.calls), 4)

    def test_unexpected_child_exit_cannot_pass(self):
        data = self.run_case(lambda log, data: (data, 9 if log == 'offline-console.txt' else 0))
        self.assertEqual(data['status'], 'FAIL')
        self.assertIn('disagrees', data['stages'][0]['detail'])
        self.assertEqual(len(self.calls), 4)

    def test_missing_result_is_failure_and_later_stages_continue(self):
        def transform(log, data):
            if log == 'offline-console.txt':
                raise FileNotFoundError('simulated missing result')
            return data, 0
        data = self.run_case(transform)
        self.assertEqual(data['status'], 'FAIL')
        self.assertEqual(data['stages'][-1]['status'], 'PASS')

    def test_short_application_result_cannot_pass(self):
        def transform(log, data):
            if log == 'offline-console.txt':
                data['results'].pop()
            return data, 0
        data = self.run_case(transform)
        self.assertEqual(data['status'], 'FAIL')
        self.assertIn('fixture list', data['stages'][0]['detail'])

    def test_denied_scope_is_incomplete_and_inline(self):
        def transform(log, data):
            if log == 'gpo-console.txt':
                data.update(status='INCOMPLETE', comparisons=[{'scope': 'policy/rsop/computer', 'status': 'INCOMPLETE', 'pairs': []}, {'scope': 'policy/rsop/user', 'status': 'PASS', 'pairs': []}], runs=[{'number': 1, 'scopes': [{'path': 'policy/rsop/computer', 'usable': False, 'status': 'failed', 'error': 'Access denied <scope>'}], 'commands': []}])
                return data, 3
            return data, 0
        data = self.run_case(transform)
        self.assertEqual(data['status'], 'INCOMPLETE')
        report = (self.output / 'report.html').read_text()
        self.assertIn('Access denied &lt;scope&gt;', report)
        self.assertIn('policy/rsop/user', report)

    def test_changed_policy_review_escaped_and_diff_inline(self):
        def transform(log, data):
            if log == 'gpo-console.txt':
                (self.output / 'gpo/diffs').mkdir()
                (self.output / 'gpo/diffs/test.diff').write_text('-1\n+2')
                data.update(status='REVIEW', comparisons=[{'scope': '<script>bad</script>', 'status': 'REVIEW', 'pairs': [{'compared_run': 2, 'config_identical': False, 'raw_identical': False, 'diff_preview': '<script>alert(1)</script>', 'diff_file': 'diffs/test.diff'}]}])
                return data, 2
            return data, 0
        data = self.run_case(transform)
        report = (self.output / 'report.html').read_text()
        self.assertEqual(data['status'], 'REVIEW')
        self.assertNotIn('<script>', report)
        self.assertIn('&lt;script&gt;', report)
        self.assertIn('gpo/diffs/test.diff', report)
        self.assertIn("default-src 'none'", report)

    def test_checksum_tamper_stops_before_any_tests(self):
        kit = self.root / 'kit'
        shutil.copytree(KIT, kit)
        with (kit / 'Run-OfflineChecks.py').open('a') as stream:
            stream.write('\n# modified\n')
        data = self.run_case(kit=kit)
        self.assertEqual(data['status'], 'FAIL')
        self.assertEqual(self.calls, [])
        self.assertIn('checksum mismatch', data['stages'][0]['detail'])
        self.assertTrue((self.output / 'report.html').exists())

    def test_invalid_checksum_manifest_paths_rejected(self):
        kit = self.root / 'kit'
        kit.mkdir()
        (kit / 'CHECKSUMS.sha256').write_text('0'*64 + '  ../escape\n')
        with self.assertRaisesRegex(ValueError, 'checksum entry'):
            w.verify_kit(kit)

    def test_unsafe_zip_paths_links_collisions_rejected_before_writes(self):
        cases = ['../escape', '/absolute', 'release/../../escape', 'release/file:ads', 'release/a\\b', 'release/trailing.']
        for number, name in enumerate(cases):
            with self.subTest(name=name):
                archive = self.root / 'release.zip'
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr(name, 'bad')
                dest = self.root / ('dest' + str(number)); dest.mkdir()
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    w.extract_release(archive, dest)
                self.assertEqual(list(dest.iterdir()), [])
        with zipfile.ZipFile(archive, 'w') as z:
            i = zipfile.ZipInfo('release/link'); i.create_system = 3; i.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(i, '../../outside')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            w.extract_release(archive, self.root)
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('release/A', 'one'); z.writestr('release/a', 'two')
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            w.extract_release(archive, self.root)

    def test_existing_output_refused_and_unchanged(self):
        sentinel = self.output / 'sentinel'; sentinel.write_text('keep')
        with self.assertRaises(FileExistsError):
            w.new_output(self.output)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_unc_output_rejected(self):
        with self.assertRaisesRegex(ValueError, 'local'):
            w.local_path('//server/share/results')

    def test_child_environment_isolated_without_changing_parent(self):
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.hooksPath', 'CONFIGBACKUP_SECTIONS': '{}', 'CONFIGBACKUP_TEST_USER': 'private', 'PYTHONPATH': '/outside'}):
            env = w.child_env(self.output)
            self.assertNotIn('GIT_CONFIG_COUNT', env)
            self.assertNotIn('CONFIGBACKUP_SECTIONS', env)
            self.assertNotIn('CONFIGBACKUP_TEST_USER', env)
            self.assertNotIn('PYTHONPATH', env)
            self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
            self.assertEqual(os.environ['GIT_CONFIG_COUNT'], '1')
            self.assertTrue(Path(env['TEMP']).is_relative_to(self.output))

    def test_links_outside_report_are_not_rendered(self):
        secret = self.root / 'outside.txt'; secret.write_text('sensitive configuration data')
        self.assertNotIn('href=', w.safe_link(self.output, '../outside.txt', 'secret'))
        self.assertNotIn('href=', w.safe_link(self.output, 'https://example.com', 'outside'))

    def test_expected_windows_skip_and_other_skips(self):
        t = types.SimpleNamespace(id=lambda: 'HarnessTests.test_live_cli_refuses_non_windows')
        result = types.SimpleNamespace(skipped=[(t, 'non-Windows guard')], testsRun=42, passed=['ok']*41, failures=[], errors=[], wasSuccessful=lambda: True)
        self.assertEqual(w.fixture_summary(result, True)['status'], 'PASS')
        self.assertEqual(w.fixture_summary(result, False)['status'], 'INCOMPLETE')
        t.id = lambda: 'HarnessTests.test_real_coverage'
        self.assertEqual(w.fixture_summary(result, True)['status'], 'INCOMPLETE')
        result.skipped = []; result.testsRun = 0
        self.assertEqual(w.fixture_summary(result, True)['status'], 'INCOMPLETE')

    def test_unfinished_report_never_passes(self):
        self.assertEqual(w.overall({'prerequisites': [], 'stages': []}), 'INCOMPLETE')
        for result in ('NOT RUN', 'INCOMPLETE', 'FAIL', 'REVIEW'):
            data = {'prerequisites': [], 'stages': [{'status': result}, {'status': 'PASS'}, {'status': 'PASS'}, {'status': 'PASS'}]}
            self.assertEqual(w.overall(data), 'INCOMPLETE' if result == 'NOT RUN' else result)

    def test_real_child_command_is_shell_free_and_handles_unicode_paths(self):
        code = w.command([sys.executable, '-I', '-c', 'import sys;print(sys.argv[1])', "München 東京 & quote'"], self.output, 'unicode.log', w.child_env(self.output))
        self.assertEqual(code, 0)
        self.assertIn('東京', (self.output / 'unicode.log').read_text())

    def test_real_gpo_bootstrap_imports_sibling_then_platform_guard(self):
        gpo = self.root / 'staged'; gpo.mkdir()
        root = w.extract_release(KIT / 'configbackup-gpo-test-1.0.0.zip', gpo)
        if os.name == 'nt':
            self.skipTest('Non-Windows guard integration only')
        args = [sys.executable, '-I', '-B', '-c', "import runpy,sys; from pathlib import Path; p=sys.argv.pop(1); sys.path.insert(0,str(Path(p).parent)); runpy.run_path(p,run_name='__main__')", str(root / 'test_gpo_collection.py'), '--runs', '3', '--interval', '30', '--output', str(self.output / 'gpo')]
        code = w.command(args, self.output, 'guard.log', w.child_env(self.output))
        self.assertEqual(code, 2)
        self.assertIn('requires Windows', (self.output / 'guard.log').read_text())
        self.assertNotIn('ModuleNotFoundError', (self.output / 'guard.log').read_text())

class ComponentArgumentTests(unittest.TestCase):
    """Argument validation must finish before creating outputs or running a stage."""

    def run_main(self, arguments):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            with patch.object(w, 'new_output', return_value=output) as create, \
                 patch.object(w, 'run_suite', return_value={'status': 'PASS'}) as suite, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(w.main(['--no-open', *arguments]), 0)
            create.assert_called_once()
            return suite.call_args.kwargs

    def test_optional_components_and_details_default_off(self):
        options = self.run_main([])
        self.assertEqual(options['components'], [])
        self.assertEqual(options['details'], [])

    def test_details_imply_components_and_duplicates_are_removed(self):
        options = self.run_main(['--windows-component', 'hyperv',
                                 '--windows-detail', 'hyperv-network',
                                 '--windows-detail', 'hyperv-replication',
                                 '--windows-detail', 'hyperv-network',
                                 '--windows-detail', 'dns-policies'])
        self.assertEqual(options['components'], ['hyperv', 'dns'])
        self.assertEqual(options['details'], ['hyperv-network', 'hyperv-replication', 'dns-policies'])

    def test_all_ten_component_choices_are_accepted(self):
        wanted = ['iis', 'dns', 'dhcp', 'hyperv', 'certificates', 'dfs', 'nps', 'wef', 'wsus', 'adcs']
        options = self.run_main([value for name in wanted for value in ('--windows-component', name)])
        self.assertEqual(options['components'], wanted)

    def test_all_five_detail_choices_map_to_their_base_component(self):
        wanted = ['iis-extended', 'dns-policies', 'dhcp-reservation-options', 'hyperv-network', 'hyperv-replication']
        options = self.run_main([value for name in wanted for value in ('--windows-detail', name)])
        self.assertEqual(options['details'], wanted)
        self.assertEqual(options['components'], ['iis', 'dns', 'dhcp', 'hyperv'])

    def test_invalid_selection_fails_before_output_or_stages(self):
        for flag, value in [('--windows-component', 'unknown'), ('--windows-detail', 'unknown'),
                            ('--windows-detail', 'dns-policies; Stop-Service DNS')]:
            with self.subTest(flag=flag, value=value), \
                 patch.object(w, 'new_output') as create, patch.object(w, 'run_suite') as suite, \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                w.main([flag, value, '--no-open'])
            self.assertEqual(raised.exception.code, 2)
            create.assert_not_called()
            suite.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
