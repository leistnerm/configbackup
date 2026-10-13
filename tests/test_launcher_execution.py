"""Execute generated launchers against a harmless fixture, never a real backup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from configbackup_lib.engine.startup_launcher import generate


class LauncherExecutionTests(unittest.TestCase):
    def setup_fixture(self, root, shell):
        source=root/'配置 with spaces';source.mkdir()
        config=source/'config.yaml';config.write_text('tasks: []',encoding='utf-8')
        engine=source/'fixture.py';engine.write_text("import json,os,sys\nfrom pathlib import Path\nPath('observed.json').write_text(json.dumps({'arguments':sys.argv[1:],'cwd':os.getcwd(),'policy':os.environ.get('PSExecutionPolicyPreference'),'mode':os.environ.get('CONFIGBACKUP_LAUNCH_MODE')}),encoding='utf-8')\nsys.exit(23)\n",encoding='utf-8')
        out=root/'launcher with spaces'
        generate(config,out,'windows',sys.executable,shell=shell,engine_path=engine,execution_policy='Bypass')
        return source,config,out

    @unittest.skipUnless(shutil.which('pwsh'), 'PowerShell 7 is not installed')
    def test_generated_ps1_unicode_paths_working_directory_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,config,out=self.setup_fixture(Path(tmp),'pwsh')
            cp=subprocess.run(['pwsh','-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',str(out/'run-configbackup.ps1')],cwd=tmp,capture_output=True,text=True,timeout=30)
            self.assertEqual(cp.returncode,23,cp.stderr)
            result=json.loads((source/'observed.json').read_text(encoding='utf-8'))
            self.assertEqual(result['arguments'],['--config',str(config)])
            self.assertEqual(Path(result['cwd']).resolve(),source.resolve())
            self.assertEqual(result['mode'],'backup')

    @unittest.skipUnless(os.name=='nt', 'Native CMD execution requires Windows')
    def test_native_bat_launcher_exits_and_inherits_process_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,config,out=self.setup_fixture(Path(tmp),'powershell.exe')
            cp=subprocess.run(['cmd.exe','/d','/c',str(out/'run-configbackup.bat')],cwd=tmp,capture_output=True,timeout=30)
            self.assertEqual(cp.returncode,23,'Native launcher blocked or failed; inspect policy and shell availability')
            result=json.loads((source/'observed.json').read_text(encoding='utf-8'))
            self.assertEqual(result['policy'],'Bypass')
            self.assertEqual(result['arguments'],['--config',str(config)])
