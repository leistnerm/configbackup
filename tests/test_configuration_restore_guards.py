"""Reviewed configuration bytes and quoted credential gates."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from configbackup_lib.configuration import configuration_repair as repair
from configbackup_lib.git.secret_scan import scan_bytes

class ConfigurationRestoreGuards(unittest.TestCase):
    def test_falsey_nonmapping_yaml_is_rejected(self):
        for value in (b'false',b'0',b'[]',b'""'):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'must be a mapping'):repair.parse(value)
        self.assertEqual(repair.parse(b''),{});self.assertEqual(repair.parse(b'{}'),{})

    def test_changed_candidate_or_destination_is_never_restored(self):
        for target in ('candidate','destination'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as td:
                root=Path(td);path=root/'config.yaml';candidate=root/'config.yaml.saved.bak'
                path.write_bytes(b'original-invalid');candidate.write_bytes(b'sources: []\n')
                def confirm(*args,**kwargs):
                    (candidate if target=='candidate' else path).write_bytes(b'changed-during-confirmation')
                    return 'RESTORE'
                with patch('configbackup_lib.ui.configure.choice',return_value=str(candidate)),patch('configbackup_lib.ui.configure.ask',side_effect=confirm):
                    with self.assertRaisesRegex(ValueError,'changed'):repair.repair(path,'schedule')
                self.assertEqual(path.read_bytes(),b'original-invalid' if target=='candidate' else b'changed-during-confirmation')
                self.assertEqual(list(root.glob('*.invalid.bak')),[])

    def test_restore_validated_bytes_and_preserve_original(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);path=root/'config.yaml';candidate=root/'config.yaml.saved.bak'
            path.write_bytes(b'original-invalid');payload=b'\xef\xbb\xbfsources: []\r\n';candidate.write_bytes(payload)
            with patch('configbackup_lib.ui.configure.choice',return_value=str(candidate)),patch('configbackup_lib.ui.configure.ask',return_value='RESTORE'):
                self.assertTrue(repair.repair(path,'schedule'))
            self.assertEqual(path.read_bytes(),payload);self.assertEqual(candidate.read_bytes(),payload)
            self.assertEqual(next(root.glob('*.invalid.bak')).read_bytes(),b'original-invalid')

    def test_quoted_credentials_are_detected_without_disclosing_values(self):
        for payload in (b'{"password": "SYNTHETIC-PRIVATE-VALUE"}',b"'api_key': 'SYNTHETIC-PRIVATE-VALUE'",b'{"client_secret":"SYNTHETIC-PRIVATE-VALUE"}'):
            for encoded in (payload,payload.decode().encode('utf-16')):
                findings=scan_bytes(encoded,'fixture')
                self.assertIn('fixture: credential-assignment',findings);self.assertNotIn('SYNTHETIC-PRIVATE-VALUE',str(findings))
                self.assertEqual(scan_bytes(encoded,'fixture',allow_sensitive=True),[])
        self.assertEqual(scan_bytes(b'{"password":"${PASSWORD_ENV}"}','fixture'),[])

    def test_archive_quoted_credentials_follow_sensitive_gate(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr('config.json','{"password":"SYNTHETIC-PRIVATE-VALUE"}')
        self.assertTrue(scan_bytes(stream.getvalue(),'fixture.zip'))
        self.assertEqual(scan_bytes(stream.getvalue(),'fixture.zip',allow_sensitive=True),[])
