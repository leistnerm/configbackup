import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from configbackup_lib.recovery.transaction_journal import Journal
import configbackup_lib.recovery.journal_recovery as recovery


@unittest.skipUnless(shutil.which('git'), 'Git required')
class MissingPrWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.repo / 'file').write_text('original')
        self.git('add', '.')
        self.git('commit', '-m', 'initial')
        self.worktree = self.root / 'worktree'
        self.git('worktree', 'add', '-b', 'backup', str(self.worktree))
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.state = self.archive / 'state.json'
        self.state.write_text('before')
        self.journal = Journal(self.archive / 'transaction', [self.archive, self.worktree], self.worktree)
        self.journal.begin()
        self.journal.remember(self.state)
        self.journal.remember(self.worktree / 'file')
        self.state.write_text('after')
        (self.worktree / 'file').write_text('modified')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True, check=True).stdout.strip()

    def test_missing_worktree_restores_archive_without_rewriting_branch(self):
        before = self.git('rev-parse', 'backup')
        shutil.rmtree(self.worktree)
        result = self.journal.recover_missing_pr_worktree(self.repo, 'backup')
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(self.state.read_text(), 'before')
        self.assertEqual(self.git('rev-parse', 'backup'), before)
        self.assertFalse(self.worktree.exists())
        self.assertFalse(self.journal.manifest.exists())
        # Normal Git preparation can recreate the missing checkout afterwards.
        self.git('worktree', 'prune')
        self.git('worktree', 'add', str(self.worktree), 'backup')
        self.assertEqual((self.worktree / 'file').read_text(), 'original')

    def test_advanced_branch_blocks_automatic_undo_but_reset_preserves_evidence(self):
        subprocess.run(['git', '-C', str(self.worktree), 'commit', '-am', 'advanced'], capture_output=True, check=True)
        before = self.git('rev-parse', 'backup')
        shutil.rmtree(self.worktree)
        with self.assertRaisesRegex(ValueError, 'changed or unavailable'):
            self.journal.recover_missing_pr_worktree(self.repo, 'backup')
        self.assertEqual(self.state.read_text(), 'after')
        manifest = self.journal.manifest.read_bytes()
        result = recovery.reset(self.journal, [self.state])
        evidence = Path(result['evidence'])
        self.assertEqual((evidence / 'journal/pending.json').read_bytes(), manifest)
        self.assertEqual((evidence / 'state-0-state.json').read_text(), 'after')
        self.assertEqual(self.state.read_text(), 'after')
        self.assertEqual(self.git('rev-parse', 'backup'), before)
        self.assertFalse(self.journal.directory.exists())

    def test_corrupt_evidence_never_partially_restores(self):
        shutil.rmtree(self.worktree)
        (self.journal.directory / 'before-00000001').write_text('corrupt')
        with self.assertRaises(ValueError):
            self.journal.recover_missing_pr_worktree(self.repo, 'backup')
        self.assertEqual(self.state.read_text(), 'after')
        self.assertTrue(self.journal.manifest.exists())

    def test_reset_copy_failure_keeps_active_journal(self):
        with patch('configbackup_lib.recovery.journal_recovery.shutil.copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                recovery.reset(self.journal, [self.state])
        self.assertTrue(self.journal.manifest.exists())
        self.assertEqual(self.state.read_text(), 'after')

    def test_existing_worktree_uses_normal_rollback(self):
        self.journal.recover_missing_pr_worktree(self.repo, 'backup')
        self.assertEqual((self.worktree / 'file').read_text(), 'original')
        self.assertEqual(self.state.read_text(), 'before')

    def test_reset_requires_acknowledgement_before_loading_config(self):
        with self.assertRaises(SystemExit) as result:
            recovery.main(['--config', 'absent.yaml', '--output', 'unused', '--reset'])
        self.assertEqual(result.exception.code, 2)

    def test_engine_keeps_pr_checkout_until_journal_commit(self):
        self.engine_run()

    def test_failed_git_publication_retains_worktree_until_journal_commit(self):
        self.engine_run(fail=True)

    def engine_run(self, fail=False):
        import configbackup_lib.engine.configbackup as cb
        source = self.root / 'source'
        source.write_text('configuration')
        cfg = {'backup': {'root': str(self.root / 'run-archive'), 'include_configuration': False},
               'git': {'repository': str(self.repo), 'mode': 'pull_request', 'push': True,
                       'branch': 'run-backup', 'worktree_root': str(self.root / 'trees')},
               'tasks': [{'name': 'file', 'type': 'file', 'source': str(source),
                          'destination': 'saved', 'storage': 'both'}]}
        resolved = cb.ConfigLoader(Path('unused'))._resolve(cfg)
        resolved['git']['push'] = False  # Exercise real local commits without a network remote.
        engine = cb.BackupEngine(resolved)
        def prepare():
            engine.git_repo.parent.mkdir(parents=True, exist_ok=True)
            self.git('worktree', 'add', '-b', 'run-backup', str(engine.git_repo))
            engine.git_branch = 'run-backup'
        original = Journal.commit
        observed = []
        def commit(journal):
            observed.append(engine.git_repo.exists())
            return original(journal)
        from contextlib import nullcontext
        failure = patch.object(engine, '_scan_git_secrets', side_effect=RuntimeError('fixture failure')) if fail else nullcontext()
        with patch.object(engine, '_prepare_git_repo', side_effect=prepare), patch.object(Journal, 'commit', commit), failure:
            self.assertEqual(engine.run(), 4 if fail else 0)
        self.assertEqual(observed, [True])
        self.assertFalse(engine.git_repo.exists())
        self.assertFalse((engine.internal_root / 'transaction/pending.json').exists())
        if not fail:
            self.assertEqual(self.git('show', 'run-backup:saved/source'), 'configuration')
        else:
            self.assertTrue(engine.state.data['publication']['pending'])

    def test_reset_cli_preserves_corrupt_journal_and_writes_report(self):
        import yaml
        config = self.root / 'config.yaml'
        config.write_text(yaml.safe_dump({'backup': {'root': str(self.archive), 'include_configuration': False}, 'tasks': []}))
        directory = self.archive / '_configbackup/transaction'
        directory.mkdir(parents=True)
        (directory / 'pending.json').write_text('incomplete metadata')
        output = self.root / 'reset-report'
        self.assertEqual(recovery.main(['--config', str(config), '--output', str(output),
                                        '--reset', '--acknowledge-incomplete-run']), 0)
        evidence = list(directory.parent.glob('transaction-abandoned-*'))
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0] / 'journal/pending.json').read_text(), 'incomplete metadata')
        self.assertFalse(directory.exists())
        self.assertTrue(output.exists())
