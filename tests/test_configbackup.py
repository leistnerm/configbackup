import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import configbackup as cb


class ConfigBackupTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(cb.parse_size("5GB"), 5_000_000_000)
        self.assertEqual(cb.parse_size("1GiB"), 1024**3)

    def test_version_filename_first_and_second_same_day(self):
        when = dt.datetime(2026, 9, 20, 14, 15, 16).astimezone()
        existing = set()
        first = cb.make_versioned_filename("config.ini", when, existing)
        self.assertEqual(first, "config.20260920.ini")
        existing.add(first)
        second = cb.make_versioned_filename("config.ini", when, existing)
        self.assertEqual(second, "config.20260920-141516.ini")

    def test_compound_extension_uses_final_suffix(self):
        when = dt.datetime(2026, 9, 20, 1, 2, 3).astimezone()
        name = cb.make_versioned_filename("schema.sql.gz", when, set())
        self.assertEqual(name, "schema.sql.20260920.gz")

    def test_source_to_logical_unix(self):
        logical = cb.source_to_logical(Path("/etc/ssh/sshd_config"))
        self.assertEqual(logical.as_posix(), "etc/ssh/sshd_config")

    def test_path_matching_root_and_recursive(self):
        self.assertTrue(cb.path_matches("a.conf", ["**/*.conf"]))
        self.assertTrue(cb.path_matches("sub/a.conf", ["**/*.conf"]))


    def test_windows_source_to_logical(self):
        logical = cb.source_to_logical(Path(r"D:\SQL Server\config.ini"))
        self.assertEqual(logical.as_posix(), "D/SQL Server/config.ini")

    def test_task_local_retention_opts_out_of_default_policy(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {td}/backup
retention_policies:
  standard:
    active:
      mode: tiered
      min_versions: 3
      tiers:
        - interval: monthly
          forever: true
defaults:
  retention_policy: standard
tasks:
  - name: local
    type: file
    source: {td}/x.txt
    retention:
      active:
        mode: simple
        min_versions: 7
        max_versions: 9
""",
                encoding="utf-8",
            )
            cfg = cb.ConfigLoader(cfg_path).load()
            task = cfg["tasks"][0]
            self.assertNotIn("retention_policy", task)
            self.assertEqual(task["retention"]["active"]["mode"], "simple")
            self.assertEqual(task["retention"]["active"]["min_versions"], 7)


    def test_end_to_end_unchanged_change_and_delete(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            target = source / "app.conf"
            target.write_text("alpha\n", encoding="utf-8")
            cfg_path = base / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {base / 'archive'}
options:
  log_level: CRITICAL
deletion:
  missing_runs: 2
  max_percent_per_run: 90
  min_items_for_percent: 10
  max_items_per_run: 500
tasks:
  - name: configs
    type: directory
    source: {source}
    destination: copied
""",
                encoding="utf-8",
            )

            def run_once():
                cfg = cb.ConfigLoader(cfg_path).load()
                with contextlib.redirect_stdout(io.StringIO()):
                    return cb.BackupEngine(cfg).run()

            self.assertEqual(run_once(), 0)
            self.assertEqual(run_once(), 0)
            target.write_text("beta\n", encoding="utf-8")
            self.assertEqual(run_once(), 0)

            active = list((base / "archive" / "copied").glob("app.*.conf"))
            self.assertEqual(len(active), 2)

            target.unlink()
            self.assertEqual(run_once(), 0)
            self.assertEqual(run_once(), 0)

            state = json.loads((base / "archive" / "_configbackup" / "state.json").read_text())
            fs = state["tasks"]["configs"]["files"]["copied/app.conf"]
            self.assertFalse(fs["active"])
            self.assertEqual(len(fs["deleted_generations"]), 1)
            self.assertEqual(len(fs["deleted_generations"][0]["versions"]), 2)

    def test_tiered_retention_keeps_newest_per_bucket(self):
        cfg = {
            "backup": {"root": tempfile.mkdtemp(), "include_hostname": False, "hostname": "host", "deleted_directory": "_deleted"},
            "internal": {"directory": "_configbackup", "staging_directory": "staging"},
            "options": {"hash_algorithm": "sha256", "log_level": "CRITICAL"},
            "logging": {"max_bytes": 100000, "backup_count": 1},
            "variables": {},
            "tasks": [],
        }
        engine = cb.BackupEngine(cfg, dry_run=True)
        now = cb.now_local()
        versions = []
        for days in [10, 11, 12, 20]:
            created = now - dt.timedelta(days=days)
            versions.append({"path": f"x{days}", "hash": str(days), "size": 1, "created": created.isoformat()})
        policy = cb.normalize_retention({
            "active": {
                "mode": "tiered",
                "min_versions": 1,
                "tiers": [
                    {"interval": "all", "duration_days": 7},
                    {"interval": "weekly", "duration_days": 90},
                ],
            }
        })["active"]
        candidates = engine._retention_candidates({"x": versions}, policy)
        # At least one older version in a shared weekly bucket should be removable.
        self.assertGreaterEqual(len(candidates), 1)

    def test_git_storage_tracks_current_snapshot_and_deletion(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            target = source / "app.conf"
            target.write_text("alpha\n", encoding="utf-8")
            cfg_path = base / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {base / 'state-root'}
git:
  repository: {base / 'repo'}
  branch: main
  push: false
options:
  log_level: CRITICAL
deletion:
  missing_runs: 1
  max_percent_per_run: 90
  min_items_for_percent: 10
tasks:
  - name: configs
    type: directory
    source: {source}
    destination: copied
    storage: git
""",
                encoding="utf-8",
            )

            def run_once():
                cfg = cb.ConfigLoader(cfg_path).load()
                with contextlib.redirect_stdout(io.StringIO()):
                    return cb.BackupEngine(cfg).run()

            self.assertEqual(run_once(), 0)
            repo_file = base / "repo" / "copied" / "app.conf"
            self.assertEqual(repo_file.read_text(), "alpha\n")
            count1 = int(subprocess.check_output(["git", "-C", str(base / "repo"), "rev-list", "--count", "HEAD"], text=True).strip())
            self.assertEqual(run_once(), 0)
            count2 = int(subprocess.check_output(["git", "-C", str(base / "repo"), "rev-list", "--count", "HEAD"], text=True).strip())
            self.assertEqual(count1, count2)
            target.write_text("beta\n", encoding="utf-8")
            self.assertEqual(run_once(), 0)
            self.assertEqual(repo_file.read_text(), "beta\n")
            count3 = int(subprocess.check_output(["git", "-C", str(base / "repo"), "rev-list", "--count", "HEAD"], text=True).strip())
            self.assertEqual(count3, count2 + 1)
            target.unlink()
            self.assertEqual(run_once(), 0)
            self.assertFalse(repo_file.exists())
            deleted = subprocess.check_output(["git", "-C", str(base / "repo"), "show", "--name-status", "--format=", "HEAD"], text=True)
            self.assertIn("D", deleted)

    def test_git_snapshot_rolls_back_when_required_task_fails(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            target = source / "app.conf"
            target.write_text("alpha\n", encoding="utf-8")
            cfg_path = base / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {base / 'state-root'}
git:
  repository: {base / 'repo'}
  branch: main
  push: false
options:
  log_level: CRITICAL
tasks:
  - name: configs
    type: directory
    source: {source}
    destination: copied
    storage: git
  - name: required-failure
    type: command
    executable: {sys.executable}
    arguments: [-c, 'import sys; sys.exit(9)']
    output: reports/fail.txt
    required: true
""",
                encoding="utf-8",
            )
            cfg = cb.ConfigLoader(cfg_path).load()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cb.BackupEngine(cfg).run()
            self.assertEqual(rc, 4)
            repo = base / "repo"
            self.assertFalse((repo / "copied" / "app.conf").exists())
            status = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip()
            self.assertEqual(status, "")
            self.assertNotEqual(subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode, 0)

    def test_git_repository_is_excluded_from_source_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            cfg_path = base / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {base / 'archive'}
git:
  repository: {source / 'git-history'}
options:
  log_level: CRITICAL
tasks:
  - name: source-tree
    type: directory
    source: {source}
    destination: tree
    storage: both
""",
                encoding="utf-8",
            )
            (source / "config.ini").write_text("x=1\n", encoding="utf-8")
            cfg = cb.ConfigLoader(cfg_path).load()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cb.BackupEngine(cfg).run()
            self.assertEqual(rc, 0)
            self.assertTrue((source / "git-history" / "tree" / "config.ini").exists())
            self.assertFalse((source / "git-history" / "tree" / "git-history").exists())


    def test_git_push_to_local_bare_remote(self):
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            remote = base / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            source = base / "source.txt"
            source.write_text("snapshot\n", encoding="utf-8")
            cfg_path = base / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {base / 'state'}
git:
  repository: {base / 'repo'}
  remote_url: {remote}
  branch: main
  push: true
options:
  log_level: CRITICAL
tasks:
  - name: one-file
    type: file
    source: {source}
    destination: current/source.txt
    storage: git
""",
                encoding="utf-8",
            )
            cfg = cb.ConfigLoader(cfg_path).load()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = cb.BackupEngine(cfg).run()
            self.assertEqual(rc, 0)
            remote_head = subprocess.check_output(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], text=True).strip()
            local_head = subprocess.check_output(["git", "-C", str(base / "repo"), "rev-parse", "HEAD"], text=True).strip()
            self.assertEqual(remote_head, local_head)


    def test_git_remote_rejects_embedded_http_credentials(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {td}/state
git:
  repository: {td}/repo
  remote_url: https://user:secret@example.invalid/repo.git
tasks:
  - name: x
    type: file
    source: {td}/x.txt
    storage: git
""",
                encoding="utf-8",
            )
            with self.assertRaises(cb.ConfigError):
                cb.ConfigLoader(cfg_path).load()


    def test_git_storage_validation_requires_repository(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            cfg_path.write_text(
                f"""
backup:
  root: {td}/state
tasks:
  - name: x
    type: file
    source: {td}/x.txt
    storage: git
""",
                encoding="utf-8",
            )
            with self.assertRaises(cb.ConfigError):
                cb.ConfigLoader(cfg_path).load()


if __name__ == "__main__":
    unittest.main()
