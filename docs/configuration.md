# Configuration reference

## Top-level sections

```yaml
backup: {}
git: {}
internal: {}
options: {}
logging: {}
deletion: {}
variables: {}
retention_policies: {}
defaults: {}
tasks: []
```

## `backup`

```yaml
backup:
  root: /var/backups/configbackup
  include_hostname: false
  hostname: my-server
  deleted_directory: _deleted
```

`root` is required even for Git-only artifact tasks because ConfigBackup keeps operational state, logs, run manifests, and locking data there. If `include_hostname` is true, the hostname is inserted below the filesystem archive root.

## `git`

Required only when at least one artifact-producing task has `storage: git` or `storage: both`.

For an existing/shared repository, the recommended configuration is pull-request mode:

```yaml
git:
  repository: 'S:\Repos\Infrastructure'
  mode: pull_request
  remote_name: origin
  base_branch: auto
  branch: 'configbackup/{hostname}'
  push: true
  include_hostname: true
  path_prefix: configbackup
  ignore:
    - '**/*.ispac'
  author_name: ConfigBackup
  author_email: configbackup@example.invalid
  commit_message: 'ConfigBackup {hostname} {date} ({run_id})'
  pull_request:
    enabled: true
    provider: github
    draft: false
    title: 'ConfigBackup: {hostname}'
    body: 'Automated configuration snapshot for {hostname}.'
    reviewers: []
    labels: []
```

`mode: pull_request` uses a temporary linked Git worktree and an automation branch, so the repository's normal checkout may contain unrelated staged or unstaged user work without ConfigBackup modifying or committing it. `base_branch: auto` follows the configured remote's default branch. Built-in PR creation currently uses the GitHub CLI (`gh`); set `pull_request.enabled: false` if another scheduled process will create the PR.

`git.ignore` is a list of Git-only glob exclusions relative to the ConfigBackup snapshot root. ConfigBackup writes a managed block into a `.gitignore` under that root. Ignored files are still retained by the filesystem side of `storage: both`. This is particularly useful for `**/*.ispac`. Negation patterns are not supported.

`mode: direct` remains available for dedicated ConfigBackup repositories; direct mode requires a clean Git working tree.

Remote authentication is handled by Git/SSH/Git Credential Manager or another approved credential mechanism. PR automation is authenticated separately through `gh`. Do not put access tokens/passwords in YAML; HTTP(S) remote URLs containing embedded user-info are rejected.

See `git-storage.md`.

## `internal`

```yaml
internal:
  directory: _configbackup
  staging_directory: staging
  # staging_root: /var/tmp/configbackup
```

If `staging_root` is omitted, ConfigBackup creates an isolated staging tree beneath the operating-system temporary directory. It must remain outside `backup.root` to prevent recursive backup behavior.

## `options`

```yaml
options:
  hash_algorithm: sha256
  stop_on_error: false
  log_level: INFO
```

`stop_on_error: false` is recommended for unattended use: unrelated tasks continue, dependent tasks are skipped, and the process still exits nonzero if a required task failed.

## `logging`

```yaml
logging:
  max_bytes: 5000000
  backup_count: 5
```

## `deletion`

```yaml
deletion:
  enabled: true
  missing_runs: 2
  max_percent_per_run: 20
  min_items_for_percent: 10
  max_items_per_run: 500
```

A file is considered missing only after a successful scan of its task. Failed/skipped collector-dependent tasks do not advance missing/deleted state.

The percentage guard only activates when at least `min_items_for_percent` items are missing, which avoids treating a single-file task as a mass deletion. `max_items_per_run` is an independent absolute guard.

Git-backed tasks use the same confirmation/guard logic before a file is removed from the current Git snapshot.

## Variables

```yaml
variables:
  SQL_SERVER: SQL01
  SCRIPT_ROOT: /opt/configbackup
```

`${NAME}` expansion can reference configured variables or environment variables.

ConfigBackup automatically makes these runtime variables available to task strings and child processes:

```text
CONFIGBACKUP_ROOT
CONFIGBACKUP_ARCHIVE_ROOT
CONFIGBACKUP_STAGING
CONFIGBACKUP_OUTPUT
CONFIGBACKUP_TASK_NAME
CONFIGBACKUP_RUN_ID
CONFIGBACKUP_DATE
CONFIGBACKUP_HOSTNAME
CONFIGBACKUP_GIT_ROOT
```

`CONFIGBACKUP_OUTPUT` defaults to an isolated per-task staging directory unless `output_directory` is set.

## Defaults

```yaml
defaults:
  retention_policy: standard
  storage: filesystem
```

Task-local values override defaults.

## Artifact storage mode

File, directory, glob, and command tasks support:

```yaml
storage: filesystem  # default
storage: git
storage: both
```

- `filesystem`: dated versions such as `config.20260921.ini`; ConfigBackup retention applies.
- `git`: current file only in the configured Git working tree; Git provides history/deletions/diffs.
- `both`: both mechanisms.

`execute` tasks create/prep data but do not archive output directly; leave their storage at the default and archive their output with a dependent task.

## Retention policies

Named retention templates are reusable:

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
    deleted:
      mode: tiered
      min_versions: 3
      grace_days: 30
      tiers:
        - interval: monthly
          duration_days: 1825
        - interval: yearly
          forever: true
```

A task can use a named policy, use one plus local overrides, or define retention entirely locally. Retention applies only to the filesystem-history portion of `storage: filesystem/both`; ConfigBackup does not rewrite Git commit history.

See `retention.md`.

## File task

```yaml
- name: hosts
  type: file
  source: /etc/hosts
```

Multiple explicit files may be given as a list.

With no destination, absolute source hierarchy is preserved in a platform-neutral form. For example, `D:\SQL Server\config.ini` maps beneath `D/SQL Server/config.ini`.

## Directory task

```yaml
- name: nginx
  type: directory
  source: /etc/nginx
  destination: etc/nginx
```

Directories are recursive and symbolic links are followed to their target contents. ConfigBackup prevents traversal into its own backup root and configured Git snapshot repository.

A directory source may also contain glob metacharacters.

## Glob task

```yaml
- name: generated-config
  type: glob
  source: /srv/**/config/**/*.yaml
```

Python recursive glob semantics are used. `**` is supported. Zero matches are valid as long as the non-glob base directory exists and is readable. A missing/inaccessible base path fails the task rather than being interpreted as mass deletion.

Directory matches are recursively traversed.

## `include` / `exclude`

Filters are applied to paths relative to each source root:

```yaml
include:
  - '**/*.conf'
  - '*.ini'
exclude:
  - '**/.git/**'
  - '**/cache/**'
```

## Execute tasks

```yaml
- name: collect-current-state
  type: execute
  phase: pre_backup
  executable: python3
  arguments:
    - collector.py
  output_directory: ${CONFIGBACKUP_STAGING}/collector
  clean_output: true
  timeout: 600
```

`execute` is intended for collector/preparation scripts. Stdout is logged; it is not itself the archived artifact.

With `clean_output: true`, cleaning is restricted to `CONFIGBACKUP_STAGING` unless `allow_clean_outside_staging: true` is explicitly set.

## Command tasks

```yaml
- name: report
  type: command
  executable: some-tool
  arguments: [--current-config]
  output: reports/config.txt
  stderr: log
  storage: filesystem
```

A command task archives stdout directly. It can use filesystem, Git, or both storage modes.

## Common execution fields

```yaml
working_directory: /opt/scripts
timeout: 600
environment:
  MODE: production
stderr: log
```

`stderr` values:

- `log` — capture and write stderr to ConfigBackup's log; default.
- `discard` — discard stderr.
- `capture` — for a command task, archive stderr separately.
- `merge` — merge stderr into stdout.

For `command` plus `stderr: capture`, optionally set `stderr_output`; otherwise `<output>.stderr` is used.

## Dependencies

```yaml
depends_on:
  - collect-sql
```

A dependent task does not run when its prerequisite failed. This is particularly important for collector trees: an incomplete collector run must not advance deletion state.

## Execution phases

Phases run in this order:

1. `pre_run`
2. `pre_backup`
3. `backup`
4. `post_backup`
5. `post_run`

Dependencies may refer to tasks in the same or an earlier phase, never a later phase.

## Dry-run semantics

`--dry-run`:

- scans normal file/directory/glob sources,
- hashes sources where necessary,
- reports storage/deletion/retention decisions,
- does not write/delete filesystem or Git snapshot data,
- does not persist state,
- does not execute external commands/collectors,
- does not commit/push Git.

A backup task dependent on a suppressed `execute` collector is skipped in dry-run because its would-be output does not exist.
