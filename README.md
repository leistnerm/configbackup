# ConfigBackup

ConfigBackup is a Python 3 utility for preserving configuration files, generated configuration snapshots, database schema dumps, and other text/binary artifacts over time.

It is designed to run once and exit, making it suitable for **Windows Task Scheduler**, **cron**, or a **systemd timer**. It does not contain its own scheduler and does not connect to SQL Server or any other database directly. External collector scripts can do that work and ConfigBackup can archive their output.

## Key features

- Windows and Linux support.
- Individual files, directories, and recursive glob patterns (`**`).
- Preserves the source directory hierarchy when no explicit destination is supplied.
- Optional hostname segment in the backup path.
- SHA-256 change detection by default; unchanged files are not duplicated.
- Version names use `filename.YYYYMMDD.ext`; additional distinct versions on the same day use `filename.YYYYMMDD-HHMMSS.ext`.
- Collector/preparation tasks can run before backups.
- Optional cross-platform SQL Server collector using Microsoft SqlPackage + dbatools, including SQL Agent, SSIS inventory/export, conditional Availability Groups, and local SQL Server on Linux host configuration (`mssql.conf`, packages, systemd metadata).
- Optional PostgreSQL collector using native `psql`/`pg_dump`/`pg_dumpall`, including safe cluster globals, schema/configuration inventory, logical replication metadata, and pg_cron/pgAgent.
- Optional cross-platform guest-system collector for hardware, CPU/memory, storage layout, networking, services, scheduled tasks/timers, installed software/packages, patches, roles/features, drivers, accounts/groups, firewall configuration, and related operating-system state.
- Optional Git storage backend: keep only the current snapshot in the working tree and use Git commits for history/diffs; works with GitHub, GitLab, Azure DevOps, local/bare remotes, or other Git servers.
- Per-task `storage: filesystem | git | both`, so dated-file retention and Git history can be mixed within one configuration.
- Command tasks can capture stdout directly into a versioned backup artifact.
- Per-task working directory, environment, timeout, and stderr behavior.
- Task dependencies and execution phases.
- Deleted-source tracking with a configurable consecutive-missing threshold.
- Deleted histories move under `_deleted/YYYYMMDD/...` rather than remaining mixed with active objects.
- Mass-deletion guard for failed/incomplete collector output.
- Named retention policy templates plus per-task overrides or fully task-local retention.
- Indefinite, simple, and tiered retention.
- Tiered retention can keep all recent versions, then thin to daily/weekly/monthly/yearly representatives.
- `min_versions` is a safety floor that retention will not violate.
- Optional maximum age, maximum versions, and maximum task size.
- Dry-run, validation, resolved-config display, and prune-only modes.
- Atomic file copies and a single-instance lock.
- JSON state and per-run manifests for auditing.

## Requirements

- Python 3.10+
- PyYAML 6.x

Install the dependency:

```bash
python3 -m pip install -r requirements.txt
```

On Windows:

```powershell
py -3 -m pip install -r requirements.txt
```

Optional collectors have their own external prerequisites:

- SQL Server: PowerShell, dbatools, and Microsoft SqlPackage.
- PostgreSQL: native PostgreSQL client tools `psql`, `pg_dump`, and `pg_dumpall` (no Python PostgreSQL driver is required).

See the collector-specific documentation for authentication and privileges.

For Git/GitHub setup, including Windows Git Credential Manager, SSH/deploy keys, Linux credential stores, and unattended scheduling, see `docs/git-storage.md`.

## Quick start

> **Recommended first-run safety:** begin with indefinite retention, run `--validate`, then `--dry-run`. Before enabling or changing retention, inspect `--prune --dry-run` output.

1. Copy `configbackup.example.yaml` to `configbackup.yaml`.
2. Set `backup.root`.
3. Remove or modify the example tasks.
4. Validate the file:

```bash
python3 configbackup.py --config configbackup.yaml --validate
```

5. Preview a run without writing files or executing collector commands:

```bash
python3 configbackup.py --config configbackup.yaml --dry-run
```

6. Run the backup:

```bash
python3 configbackup.py --config configbackup.yaml
```

## Included examples

- `examples/windows.yaml` — basic Windows file/directory backup.
- `examples/linux.yaml` — basic Linux file/directory backup.
- `examples/full-stack-windows.yaml` — Windows guest inventory plus SQL Server collection.
- `examples/full-stack-linux.yaml` — Linux guest inventory with Git/filesystem examples.
- `examples/sql-schema-collector.yaml` — SQL Server schema, Agent, and SSIS collection on Windows.
- `examples/sql-schema-collector-linux.yaml` — SQL Server on Linux schema/instance collection plus local `mssql.conf`, packages, and systemd metadata.
- `examples/postgresql-collector.yaml` — PostgreSQL configuration/schema collection with Git/filesystem history.
- `examples/full-stack-postgresql-linux.yaml` — Linux guest inventory plus PostgreSQL configuration/schema history.
- `examples/system-git.yaml` — guest-system inventory stored as a current Git snapshot.
- `examples/git-existing-repo-pr.yaml` — existing/shared repository using an isolated automation branch, linked worktree, GitHub PR, and Git-only `.ispac` exclusion.

## Typical archive layout

A Windows source such as:

```text
D:\SQL Server\someconfig.ini
```

with `backup.root: C:\ConfigBackup` and no explicit destination becomes approximately:

```text
C:\ConfigBackup\D\SQL Server\someconfig.20260920.ini
```

A Linux source such as:

```text
/etc/systemd/system/myservice.service
```

with `backup.root: /backup/configbackup` becomes:

```text
/backup/configbackup/etc/systemd/system/myservice.20260920.service
```

When `backup.include_hostname: true`, the hostname is inserted immediately below `backup.root`. ConfigBackup's internal state/log/lock directory is also kept beneath that hostname path, so multiple hosts can safely use the same higher-level root as long as hostname separation is enabled.

## Task types

### `file`
Back up one or more explicit files.

### `directory`
Recursively back up a directory. A directory source may also contain glob metacharacters.

### `glob`
Back up files/directories matched by a glob such as:

```text
/path/**/config/**/*.yaml
```

### `execute`
Run a program/script, normally during `pre_backup`, to prepare files for later backup tasks. ConfigBackup does not archive stdout for an `execute` task; stdout is logged. Use `command` when stdout itself is the artifact.

### `command`
Run a program/script and archive stdout as the configured `output` file.

See `docs/commands-and-collectors.md`. For SQL Server, see `docs/sql-server-collector.md`; for PostgreSQL, see `docs/postgresql-collector.md`; for guest inventory, see `docs/system-collector.md`.


## Git-backed history

A task can use `storage: git` or `storage: both`. For an existing/shared repository, 1.4.0 adds `git.mode: pull_request`, which uses an isolated linked worktree and automation branch instead of touching the normal checkout:

```yaml
git:
  repository: 'S:\Repos\Infrastructure'
  mode: pull_request
  base_branch: auto
  branch: 'configbackup/{hostname}'
  remote_name: origin
  push: true
  path_prefix: configbackup
  include_hostname: true
  ignore:
    - '**/*.ispac'
  pull_request:
    enabled: true
    provider: github

tasks:
  - name: sql-history
    type: directory
    source: ${SQL_STAGING}
    destination: sql-server
    storage: both
```

This keeps exact `.ispac` artifacts in the filesystem archive while excluding them from Git; expanded SSIS `.dtsx` and metadata remain diffable. Pull-request mode can auto-detect the remote default branch, push the ConfigBackup branch, and create/update a GitHub PR through the `gh` CLI. The main working tree can contain unrelated local changes because ConfigBackup operates in its own worktree.

See `docs/git-storage.md` for Git/GitHub CLI authentication, Git Credential Manager, SSH/deploy keys, Task Scheduler/cron identity requirements, worktree/branch behavior, and PR setup.

## Retention

No retention block is required. The safe default is indefinite retention with a minimum of three versions.

Named policies can be defined once and referenced by many tasks. A task may:

- use a named policy,
- use a named policy and override selected values, or
- define its entire retention policy locally.

Example tiered policy:

```yaml
retention_policies:
  standard:
    active:
      mode: tiered
      min_versions: 3
      tiers:
        - interval: all
          duration_days: 7
        - interval: weekly
          duration_days: 90
        - interval: monthly
          forever: true
```

This keeps every distinct version for seven days, then one version per ISO week for the next 90 days, then one version per calendar month indefinitely.

See `docs/retention.md` for full semantics.

## Deleted sources

For successful file/directory/glob scans, ConfigBackup tracks expected files. By default a file must be missing from two consecutive successful scans before it is classified as deleted.

When deletion is confirmed, the existing history is moved beneath:

```text
_deleted/YYYYMMDD/<task-name>/<original-logical-path>/...
```

A uniquely named deletion metadata JSON sidecar is also written for each deletion event.

A failed collector dependency does **not** cause its dependent backup task to scan its output, so an incomplete/failed collector cannot advance missing/deleted state.

The mass-deletion guard can suspend deletion-state advancement if an implausibly large set disappears at once.

## Useful commands

Validate configuration:

```bash
python3 configbackup.py -c configbackup.yaml --validate
```

Show resolved configuration (obvious secret-key fields are redacted):

```bash
python3 configbackup.py -c configbackup.yaml --show-config
```

Dry-run:

```bash
python3 configbackup.py -c configbackup.yaml --dry-run
```

Run retention only:

```bash
python3 configbackup.py -c configbackup.yaml --prune
```

Preview retention only:

```bash
python3 configbackup.py -c configbackup.yaml --prune --dry-run
```

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | Configuration/validation error |
| 3 | Another instance already holds the lock |
| 4 | One or more required tasks failed |
| 5 | Unexpected fatal error |
| 130 | Interrupted by the user |

## Internal data

By default ConfigBackup maintains this under the backup root:

```text
_configbackup/
  configbackup.log
  configbackup.lock
  state.json
  state.json.bak
  runs/
    YYYYMMDD-HHMMSS-ffffff.json
```

The state file is operational metadata, not the primary backup content. The previous state is retained as `state.json.bak` before each state update.

By default collector staging is kept **outside** the backup root in an isolated directory beneath the operating system temporary directory. This prevents staging output from colliding with archive-recursion protection. Set `internal.staging_root` if you want a fixed staging location.

Retention runs automatically only when required tasks complete successfully, and only successful tasks are eligible for automatic pruning. `--prune` is the explicit exception and runs retention without performing backups.

## Documentation

- `docs/configuration.md` — full YAML reference and path behavior.
- `docs/retention.md` — retention modes, templates, deletion history, and pruning.
- `docs/commands-and-collectors.md` — executing collectors and capturing command output.
- `docs/scheduling.md` — Windows Task Scheduler, cron, and systemd examples.
- `docs/state-and-recovery.md` — state files, run manifests, interrupted runs, and recovery behavior.
- `SECURITY.md` — credential handling, sensitive collected content, and destructive-operation safeguards.
- `docs/sql-server-collector.md` — SqlPackage + dbatools SQL Server collector, SQL Agent, SSISDB, and optional legacy SSIS.
- `docs/system-collector.md` — Windows/Linux guest hardware, storage, software, patch, service, networking, and configuration inventory.
- `docs/git-storage.md` — Git/GitHub/GitLab/Azure DevOps storage, commits, remotes, authentication, and binary-file guidance.

## Design notes

- ConfigBackup intentionally does not embed SQL Server logic in the Python engine. The optional PowerShell collector under `collectors/sqlserver/` uses SqlPackage + dbatools to produce a current-state tree including schema, SQL Agent, and SSIS configuration.
- The optional system collector under `collectors/system/` is Python-stdlib-only and emits stable JSON/CSV/text snapshots suitable for either dated-file retention or Git diffs.
- Passwords and secrets should not be placed directly in YAML. Prefer integrated authentication, service identities, environment variables, or the platform's credential facilities.
- Symlinks are followed; the target contents are backed up rather than preserving the link object itself.
- Automatic compression is intentionally not enabled because configuration/schema backups are generally more useful when directly browsable and diffable.

## Operational limitations / behavior

- ConfigBackup preserves file contents and basic timestamps via `copy2`; it is **not** a full filesystem image and does not promise to preserve Windows ACLs, Unix ownership, extended ACLs, alternate data streams, or every extended attribute.
- If the state file is lost, existing archive filenames are still protected from overwrite, but ConfigBackup may create an extra version because it no longer has the previous hash history. Keep `_configbackup/state.json` with the archive.
- A source file that changes while being copied fails verification rather than committing a mismatched version. A later scheduled run can retry it.
- Task renames leave the prior task's state/history untouched. This is intentionally conservative; old history is not silently reassigned or deleted.
- The date used in filenames is the local date/time of the machine running ConfigBackup.
