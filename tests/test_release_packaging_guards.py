"""Repository-only tooling checks; release application omits build tooling."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

BUILDER=Path(__file__).resolve().parents[1]/'tools/build_release.py'
@unittest.skipUnless(BUILDER.exists(),'Release tooling is only in the Git checkout')
class PackagingGuards(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('release_builder',BUILDER)
        self.builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.builder)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        subprocess.run(['git','init','-q',str(self.root)],check=True)
    def tearDown(self):self.temp.cleanup()
    def track(self,name,data):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(data)
        subprocess.run(['git','-C',str(self.root),'add','--',name],check=True)
        return path
    def test_untracked_private_inputs_are_not_packaged(self):
        self.track('app.py','print("hello")');self.track('sub/included.txt','public')
        (self.root/'.env').write_text('PRIVATE_SYNTHETIC_FIXTURE')
        (self.root/'configbackup.yaml').write_text('PRIVATE_SYNTHETIC_FIXTURE')
        self.assertEqual(set(self.builder.files(self.root)),{'app.py','sub/included.txt'})
        self.assertEqual(set(self.builder.files(self.root/'sub')),{'included.txt'})
    def test_missing_tracked_input_fails_instead_of_silent_omission(self):
        self.track('app.py','hello').unlink()
        with self.assertRaisesRegex(ValueError,'missing'):self.builder.files(self.root)
    def test_version_is_read_without_executing_engine(self):
        self.track('configbackup_lib/engine/configbackup.py','raise RuntimeError("must not execute")\nAPP_VERSION = "1.6.0"\n')
        self.assertEqual(self.builder.application_version(self.root),'1.6.0')
        (self.root/'configbackup_lib/engine/configbackup.py').write_text('APP_VERSION = "bad"\n')
        with self.assertRaises(ValueError):self.builder.application_version(self.root)
