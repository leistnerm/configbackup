"""Isolation when sealing snapshots or preparing launcher replacement fails."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from configbackup_lib.engine.completeness import Coverage,publish,section
from configbackup_lib.engine.startup_launcher import generate
from configbackup_lib.engine.launcher_manager import regenerate,inventory

class FailurePathTests(unittest.TestCase):
    def test_seal_preserves_existing_destination_and_continues_healthy_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source';destination=root/'sealed'
            for scope in ('bad','healthy'):
                (source/scope).mkdir(parents=True);(source/scope/'config').write_text('new')
            publish(source,[section(source,'bad'),section(source,'healthy')])
            (destination/'bad').mkdir(parents=True);(destination/'bad/config').write_text('preserve-existing')
            coverage=Coverage(source);coverage.seal(destination)
            self.assertEqual((destination/'bad/config').read_text(),'preserve-existing')
            self.assertEqual((destination/'healthy/config').read_text(),'new')
            self.assertFalse(coverage.permits_deletion('bad/old'));self.assertTrue(coverage.permits_deletion('healthy/old'))

    def test_backup_directory_failure_releases_launcher_lock_for_retry(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);config=root/'config.yaml';config.write_text('tasks: []\n');out=root/'launcher'
            files=generate(config,out,'windows','python');metadata=inventory(config,{'tasks':[]})[0]['metadata']
            before={p.name:p.read_bytes() for p in files};mkdir=Path.mkdir
            def fail_backup(path,*args,**kwargs):
                if path.name.startswith('backup-'):raise OSError('synthetic backup creation failure')
                return mkdir(path,*args,**kwargs)
            with patch.object(Path,'mkdir',fail_backup):
                with self.assertRaises(OSError):regenerate(config,{'tasks':[]},metadata)
            self.assertFalse((out/'.launcher-update.lock').exists());self.assertEqual(before,{p.name:p.read_bytes() for p in files})
            self.assertTrue(Path(regenerate(config,{'tasks':[]},metadata)).is_dir())
