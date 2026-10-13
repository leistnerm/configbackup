import hashlib,json,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import deployment_pack as pack
class DeploymentPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.bundle=self.root/'bundle';self.bundle.mkdir();self.files={}
        for name in ['install.py','requirements.lock','application/configbackup.py','application/requirements.txt']:
            path=self.bundle/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# fixture\n');self.files[name]=pack.digest(path)
        self.value={'schema':1,'platform':pack.fingerprint(),'files':self.files};self.save()
    def tearDown(self):self.tmp.cleanup()
    def save(self):(self.bundle/'deployment.json').write_text(json.dumps(self.value))
    def test_every_file_hashed_and_no_unlisted_payload(self):
        pack.verify(self.bundle,True);(self.bundle/'extra').write_text('extra')
        with self.assertRaises(ValueError):pack.verify(self.bundle)
        (self.bundle/'extra').unlink();(self.bundle/'application/configbackup.py').write_text('modified')
        with self.assertRaises(ValueError):pack.verify(self.bundle)
    def test_wrong_platform_never_creates_environment(self):
        self.value['platform']['machine']='wrong';self.save();destination=self.root/'venv'
        with self.assertRaises(ValueError):pack.install(self.bundle,destination)
        self.assertFalse(destination.exists())
    def test_existing_environment_never_changed(self):
        destination=self.root/'venv';destination.mkdir();(destination/'marker').write_text('keep')
        with self.assertRaises(ValueError):pack.install(self.bundle,destination)
        self.assertEqual((destination/'marker').read_text(),'keep')
    def test_symlink_payload_refused(self):
        path=self.bundle/'install.py';path.unlink();source=self.root/'outside';source.write_text('# fixture\n');path.symlink_to(source)
        with self.assertRaises(ValueError):pack.verify(self.bundle)
    def test_dependency_closure_is_exact_and_deterministic(self):
        first=pack.dependencies([]);self.assertIn('pyyaml',first);self.assertIn('ruamel-yaml',first);self.assertEqual(first,pack.dependencies([]))
if __name__=='__main__':unittest.main()
