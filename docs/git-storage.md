# Git / GitHub snapshot storage

ConfigBackup can use Git as the history engine for individual archive tasks. This is useful for text-heavy configuration, SQL schema, SSIS package XML, system inventory, and other artifacts where normal Git diffs are more useful than dated filenames.

Git support is deliberately **provider-neutral**. A repository can be local-only or have a remote hosted by GitHub, GitLab, Azure DevOps, Bitbucket, or another Git server.

## Storage modes

Every artifact-producing task supports:

```yaml
storage: filesystem   # default
storage: git
storage: both
```

- `filesystem` uses ConfigBackup's dated filenames, deletion archive, and retention engine.
- `git` keeps only the current working-tree snapshot. Git commits provide history, diffs, and deleted-file history.
- `both` writes both forms.

`execute` tasks do not archive artifacts themselves, so their `storage` remains `filesystem`; use a dependent file/directory/glob task to archive collector output.

## Basic Git configuration

```yaml
git:
  repository: /srv/configbackup-repo
  branch: main
  include_hostname: true
  path_prefix: snapshots
  author_name: ConfigBackup
  author_email: configbackup@example.invalid
  push: false
```

Windows example:

```yaml
git:
  repository: 'C:\ConfigBackupGit'
  branch: main
```

ConfigBackup requires a **clean Git working tree at the start of every run**. This prevents unrelated manual edits from being committed or accidentally discarded. A dedicated repository is strongly recommended. ConfigBackup also excludes its configured Git repository from source traversal so it cannot recursively ingest its own snapshot tree.

## GitHub remote

ConfigBackup does not store GitHub credentials or tokens in YAML. Configure authentication using normal Git mechanisms such as SSH keys/agent, Git Credential Manager, or an approved deploy/service identity. HTTP(S) remote URLs containing user-info/credentials are rejected during validation.

For an empty/new remote:

```yaml
git:
  repository: 'C:\ConfigBackupGit'
  remote_name: origin
  remote_url: 'git@github.com:example/server-config-history.git'
  branch: main
  push: true
```

When `remote_url` is present and the named remote does not exist, ConfigBackup adds it. If the remote already exists with a different URL, the run fails rather than silently changing it.

If the remote repository already contains commits, clone it normally first and point `git.repository` at that clone. ConfigBackup does not automatically fetch, pull, merge, rebase, or force-push.

## Commit behavior

ConfigBackup stages snapshot changes after all tasks complete successfully, then creates **one commit per successful run that actually changed Git-backed content**. An unchanged run does not create an empty commit.

Default commit message:

```text
ConfigBackup HOSTNAME RUN_ID
```

Optional template:

```yaml
git:
  commit_message: 'ConfigBackup {hostname} {date} ({run_id})'
```

Supported placeholders are `{hostname}`, `{date}`, and `{run_id}`.

If a required task fails, ConfigBackup does not commit a partial snapshot. Because the repository was required to be clean before the run, ConfigBackup restores the repository to its pre-run `HEAD`.

## Deletions

Git-backed tasks still use ConfigBackup's missing-run and mass-deletion guards. A missing object is not removed from the working tree until the configured missing threshold is reached. Once confirmed, its current file is removed and the next successful Git commit records a normal Git deletion.

The filesystem `_deleted` tree is not needed for a Git-only task because the Git history already retains the deleted object's prior versions. A `storage: both` task gets both behaviors.

## Retention

ConfigBackup retention rules apply only to filesystem history. Git-backed history is governed by Git and the hosting platform. ConfigBackup does not rewrite or garbage-collect Git commit history.

For Git-only tasks, retention configuration is therefore ignored for the Git copy.

## Binary files

Git can store binary files, but repeated binary versions can grow a repository quickly and do not provide useful line-level diffs. For SQL/SSIS collection, consider `-SkipIspac` when using a Git-focused repository; the collector will still export the expanded `.dtsx` and other project files for useful diffs.

If exact deployable `.ispac` artifacts are required as well, use `storage: both`, a separate artifact archive, or Git LFS according to your organization's policy.

## Example

```yaml
git:
  repository: /srv/config-history
  remote_url: git@github.com:example/config-history.git
  push: true
  include_hostname: true

tasks: []
```

A practical task pair:

```yaml
- name: collect-system
  type: execute
  phase: pre_backup
  executable: python3
  arguments:
    - /opt/configbackup/collectors/system/collect_system.py
  output_directory: ${CONFIGBACKUP_STAGING}/system
  clean_output: true

- name: system-snapshot
  type: directory
  source: ${CONFIGBACKUP_STAGING}/system
  destination: system
  depends_on: [collect-system]
  storage: git
```

## Security of collected content

Git authentication secrets are not stored by ConfigBackup, but the files being collected may themselves contain operationally sensitive data or embedded credentials. SQL Agent command text, service command lines, scheduled-task arguments, application configuration, SSIS package XML, and network/share configuration are common examples. Use a private repository with access controls and review collector output before enabling a remote push.
