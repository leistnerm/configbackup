# State, manifests, and recovery

ConfigBackup stores operational state beneath the effective archive root:

```text
_configbackup/
  configbackup.log
  configbackup.lock
  state.json
  state.json.bak
  runs/
    YYYYMMDD-HHMMSS-ffffff.json
```

If `backup.include_hostname: true`, this internal directory is beneath that hostname's archive path, so each host has independent state and locking.

## `state.json`

The state file records the logical files known to each task, hashes and paths of archived versions, consecutive-missing counters, and deleted generations. It is what allows ConfigBackup to efficiently determine whether the current content is identical to the last archived content.

Before replacing `state.json`, ConfigBackup copies the previous state to `state.json.bak`.

## Run manifests

Each normal run creates a JSON manifest under `_configbackup/runs/`. It records task outcomes and aggregate counts such as new, changed, unchanged, missing, deleted, stored, and pruned versions.

These manifests are intended for audit/troubleshooting; they are not required to restore the actual configuration files.

## If state is lost

The archive data remains ordinary files and is not made unreadable by loss of state. ConfigBackup also checks physical archive filenames before selecting a new version name, so loss of state should not silently overwrite an existing dated backup.

However, without the historical hash metadata, ConfigBackup may create an additional version the next time it sees a source because it cannot prove that the current content matches the previous archived version.

Recommended recovery order:

1. Restore `_configbackup/state.json` if you have it.
2. If the primary state file is damaged, inspect/restore `_configbackup/state.json.bak`.
3. If neither is usable, preserve the existing archive and start with fresh state. Expect some duplicate versions until new state history is established.

There is intentionally no automatic state rebuild in version 1.0 because guessing logical ownership from arbitrary historical filenames can be ambiguous, especially after task/destination changes.

## Failed/interrupted runs

Backup files are written through a temporary file and atomically renamed into place. A source copy is hashed again after storage to verify it matches the hash observed before the copy. If the source changes during the operation, the candidate stored copy is removed and the task fails rather than committing inconsistent state.

State is saved at the end of the run. If the process is terminated after an archive file was committed but before state was saved, that file remains safe; a later run may create an extra dated version because the state file does not yet know about it.

## Task renames and destination changes

Task names are state identities. Renaming a task does not migrate its old state automatically. Likewise, changing a destination may cause the old logical path to be treated as missing/deleted while the new destination begins a new history.

For major configuration reorganizations, use `--dry-run` first and retain the old archive/state until the new layout is verified.
