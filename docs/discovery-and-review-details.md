# Discovery and review details

Recovery and discovery workflows use independent failure handling and journaled local writes. Upgrade the entire distribution together. Preserve the archive and matching state before upgrading; rollback after new writes requires reconciling that matching set, not simply running old code on newer state.

## Behavior

- Windows scheduled-job arguments recognize quoted option values such as `--config="C:\Job Files\settings.yaml"`, escaped quotes and empty arguments.
- Unsupported launchd structures and malformed Windows task/service records fail their individual discovery record. Healthy definitions continue.
- Every recovery-drill invocation verifies the bundle, even when a full relocation rehearsal is not due. Missing/corrupt bundles fail immediately. A changed inventory invalidates the previous drill result. OS-backed locking releases on process termination; a leftover lock file is harmless.
- PostgreSQL native dumps retain random restriction keys. Only SQL-aware comparison representations normalize paired top-level keys. Never restore comparison copies.

## Discovery and coverage

The main editor's **Discover files from scheduled jobs** menu remembers accepted, ignored and disabled decisions under `discovery.decisions`. New-only review hides already decided paths. Rows explain direct references versus nearby files whose usage is unproven, and group by directory. Re-enable a selected file without deleting it:

```yaml
backup:
  configuration_files:
    - path: /opt/jobs/settings.ini
      enabled: false
```

The legacy string list remains supported. **Recovery files and export** can toggle individual additional files. Choices are unsaved until the main editor saves. Ignoring an already selected file is rejected; disable it explicitly instead.

`python schedule_discovery.py --services --user-timers` additionally reads local Windows service command paths and Linux system/user service and timer definitions. These probes never run the jobs. Existing limits of 500 jobs, 50 directories and 200 entries per directory remain. It does not enumerate every user's scheduler, expand environment values or infer an unquoted ambiguous Windows executable path.

**Recovery and review workflows** can follow recognized `include`, `include_file` and `!include` directives from selected files. Allowed roots, depth and file counts are explicit; cycles are deduplicated. Symlinks, wildcards, dynamic expressions, remote paths and unknown include languages are not followed. This is a bounded suggestion parser, not an effective-configuration parser for every application.

```sh
python discovery_review.py --config configbackup.yaml --output /private/review/coverage
# Optional: --discovery saved-discovery.json --state /archive/_configbackup/state.json
```

The JSON/HTML report connects configured task dependencies, shared-connection names, selected files, discovery evidence and credential environment-variable names. It distinguishes selected with no archive evidence, matching recorded backup hash, inaccessible, disabled, excluded, discovered but unselected and unknown. Archive-state matches are not a new integrity verification. Scheduler registration, grants, credentials, tools and service data remain explicit unknown requirements. No claim of complete application dependency discovery is made.

## Interrupted writes

The engine records original local archive/Git working-file bytes and state in a durable undo journal under `_configbackup/transaction`. Every recorded backup and manifest is synced before mutation. Retention deletions participate too. A committed marker is written only after state and final recorded files are synced. The engine's normal run lock serializes reconciliation.

At the next normal run, an unfinished transaction is validated completely before rollback. If Git HEAD is unchanged, recorded files/state and the Git index return to the prior local state, then collection resumes. Failed database/section preservation during ordinary partial runs remains independent. An interrupted uncommitted run may roll back all its recorded local changes, including healthy siblings; the next run recollects them.

If Git HEAD advanced, a journal is corrupt, or storage identity changed, startup stops for review without undoing a published commit. Preserve the archive, state, repository and transaction directory as a set. This release does **not** promise a distributed transaction with Git remotes, undo collector side effects, recover from arbitrary hardware loss, or prove durability on network filesystems. Filesystem fsync/rename guarantees and sufficient space for undo copies are required. Do not edit storage while a transaction is pending. `--verify`/`--restore` refuse pending transactions; normal startup reconciliation must happen first.

## Stable identities and retirement

A task may set `state_id: inventory-host-a`, which remains constant when its display `name` changes. Existing tasks without it continue using their names. Preserve explicit destination, output directory and dependency names when renaming; changing a name-derived default path is a separate change. Duplicate identities are rejected.

Preview a legacy identity migration or retirement:

```sh
python task_lifecycle.py --config candidate.yaml --source old-task --target inventory-host-a
python task_lifecycle.py --config candidate.yaml --source inventory-host-a --retire
```

Save the matching `state_id` in the candidate configuration, then repeat with `--apply` to change state under the engine lock and durable journal. Retirement requires the configured task to be disabled, preserves versions and prevents retention for that identity. A filesystem-only destination migration can add `--destination new-prefix`; the preview lists every logical key. Retained physical versions stay in their original locations. **Git destination moves are rejected and need a separate reviewed Git migration.** No task or directory is retired automatically merely because discovery stops reporting it.

## Recovery package maintenance and encryption

```sh
python recovery_maintenance.py refresh --config configbackup.yaml --directory /private/setup-exports
python recovery_maintenance.py freshness --config configbackup.yaml --bundle /private/setup-exports/FINGERPRINT
python recovery_maintenance.py status --directory /private/setup-exports --output /private/review/recovery
```

Refresh publishes a new immutable verified export when selected inputs or packaged code change; unchanged input reuses the existing export. Incomplete selected inputs preserve the previous pointer/export. Add it explicitly to a scheduled run using `examples/recovery-maintenance.yaml`. No scheduler is installed. Exports are retained; there is no automatic pruning of these managed bundles.

Optional `--secondary /mounted/protected-copy` copies and verifies independently. Local export success survives secondary-copy failure, which returns exit 6. Status shows last secondary verification age and current readability/integrity. A separate path is not proof of a separate physical host or immutable storage. Cloud APIs and automated remote-host validation are not provided.

Optional encrypted packages use the cryptography library's AES-256-GCM with random salt/nonce and scrypt. Install the separate optional requirements in the selected environment:

```sh
python -m pip install -r requirements-encryption.txt
python recovery_crypto.py encrypt --source /private/setup-exports/FINGERPRINT --destination /private/offhost/setup.cbrec
python recovery_crypto.py decrypt --source /private/offhost/setup.cbrec --destination /private/new-decrypted-bundle
```

The command prompts without echo, or accepts `--password-env ENVIRONMENT_VARIABLE_NAME`. Never supply the passphrase itself in argv/YAML. Use a long unique passphrase and preserve it separately; losing it prevents recovery. Decryption authenticates before extraction, bounds total size to 256 MiB, checks paths and verifies the restored manifest before publishing a new directory. Native encrypted bytes deliberately differ on every run. Encryption does not encrypt the existing plaintext archive or export and is not automatically applied to `--secondary` copies. No external cryptographic audit is claimed.

Implementation references: [cryptography authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/) and [scrypt key derivation](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#scrypt).

## Attention rules and collection costs

The review portal includes **Changes needing attention**. `reporting.change_policy` configures rules over source, task, path, property, change kind and optional exact before/after values. Default rules flag selected security, firewall, listener and database paths and observed `enabled: true` to `false` changes for review. A filename match is not proof of privilege expansion or external reachability. Approved fingerprints require before/after content hashes; generic hashless change notifications cannot be permanently approved. Approved and maintenance-window findings remain visible. This is a local review view; no new notification is sent automatically.

Task `collection_interval_hours` separates collection cadences within one scheduled engine invocation. When a collector is not due, its dependent archive is also not due; old output cannot advance deletion counters. Failed/partial attempts remain due. `options.run_budget_seconds` stops dispatching further collectors after the elapsed budget; it does not terminate a running collector or skip independent file backups. Existing task/managed `collection.timeout` controls subprocess timeouts.

Archive tasks may set `max_scope_bytes`; an oversized certified scope fails independently while other databases/scopes continue. This limits publication, not bytes already collected by the external tool. Runtime state retains up to 90 task cost observations and latest per-scope archive time, file count and bytes. The portal quality view shows these and failure counts. Query timing remains explicitly unknown where a collector does not provide it. This release does not measure per-query costs or automatically tune database maintenance.

## Determinism command

```sh
python determinism.py --snapshot /captures/first --snapshot /captures/second --output /private/review/determinism
python determinism.py --config configbackup.yaml --task collect-postgres --repeat 3 --output /private/review/pg-repeat
```

The second command explicitly repeats a managed database collector in disposable scratch using its shared connection/settings. It never archives, commits, sends messages or runs a saved launcher. It still uses database resources and schema locks; run on a suitable target. Arbitrary command tasks are not executed by this tool.

Results are `identical`, `representation_only`, `changed` or `incomplete`. Failed/unreported/corrupt scopes cannot pass as identical. Disabled/not-applicable scopes remain visible as not collected. Telemetry is excluded by certified manifests. PostgreSQL native random keys are reported as representation-only using existing narrow SQL-aware comparison; other byte changes require review. Exit 0 means identical or explicitly reported representation-only, not guaranteed semantic equivalence or complete feature coverage.

Read `VALIDATION.md` for platform limitations. Windows fixture success is not proof of live GPO, NTFS, role, scheduler-account or database-provider behavior.
