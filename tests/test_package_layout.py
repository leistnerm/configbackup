import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from configbackup_lib import APPLICATION_ROOT
from configbackup_lib.recovery.recovery_assets import distribution_files


class PackageLayoutTests(unittest.TestCase):
    def test_all_compatibility_commands_help_outside_install_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in sorted(APPLICATION_ROOT.glob('*.py')):
                with self.subTest(command=path.name):
                    cp=subprocess.run([sys.executable,str(path),'--help'],cwd=tmp,capture_output=True,text=True,timeout=30)
                    self.assertEqual(cp.returncode,0,cp.stderr)

    def test_recovery_distribution_contains_every_internal_module(self):
        files={str(relative) for relative,_ in distribution_files()}
        for path in (APPLICATION_ROOT/'configbackup_lib').rglob('*.py'):
            self.assertIn(str(path.relative_to(APPLICATION_ROOT)),files)
        self.assertIn('requirements-console.txt',files)

    def test_root_import_alias_is_same_engine_for_compatibility(self):
        import configbackup
        from configbackup_lib.engine import configbackup as implementation
        self.assertIs(configbackup,implementation)
