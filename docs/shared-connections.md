# Shared connections and scheduled-account readiness (2.1.0)

Managed database tasks now use **one connection definition** for both normal collection and diagnostics. Start with [the example](../examples/shared-connections.yaml). In `configure.py`, use **Shared connections → Add connection**, then **Tasks → SQL Server/PostgreSQL** to reference it. Editing that connection updates both paths. Existing arbitrary execute commands and legacy `database_diagnostics` profiles continue to work; their custom wrappers are not inferred or migrated automatically.

Each `connections` entry supports the existing [diagnostic profile fields](database-diagnostics.md). A managed `type: execute` task supplies `connection`, optional `collection` selection overrides, section switches, and `required_sections`. It cannot also specify an executable, arguments or task timeout. Use `collection.timeout` for a bound on the collector process tree. PostgreSQL uses the same argument builder in both paths; SQL Server uses `Invoke-ProfileCollection.ps1` in both paths. Managed SQL tasks intentionally skip OS host collection; configure a separate system/service adapter task. Advanced physical-index scans, pgstattuple targets, raw config and custom SQL connection-string options still require standalone collector tasks and separate validation.

Task environment, runtime variables and working directory are resolved by the backup engine in diagnostic dry-run context. No backup directory is created by that resolution. The selected password is passed in the child environment, never in executable arguments or profile JSON. Use explicit connection endpoint/user fields and absolute client paths for reproducibility. Normal shell/libpq/vault access may differ under a scheduler. Never put actual passwords in YAML task environment fields; managed shared profiles accept references only.

```sh
python configure.py --config configbackup.yaml --diagnose-task collect-sql --diagnostic-report sql-check.json
python configure.py --config configbackup.yaml --diagnose-task collect-pg --database application --metadata-only
python configure.py --config configbackup.yaml --diagnose-task collect-sql --skip-section instance/ssis
```

`--database` is repeatable and changes only this test's selection. `--skip-section` is repeatable and uses the collector's existing section switches. Common connection, visibility and discovery queries still run. This is not an arbitrary query filter. `--metadata-only` does not certify native schema extraction. Reports distinguish available, unavailable, disabled, not applicable and not tested. Reports certify only finalized hash-verified scope files. Raw exports are removed after diagnosis.

## Required sections and suggested fixes

`required_sections` uses case-sensitive glob patterns against manifest scope paths and verified file paths. Use names from a diagnostic report (encoded database paths can differ from display names). Every matched scope must be available; an unavailable parent also fails a descendant requirement. Zero matches, disabled, not applicable and untested do not pass. A glob proves coverage of matches reported by the collector, not the existence of an externally expected database: list explicit database paths when every named database must be present.

Both normal collection and diagnostics enforce these rules. A normal required task with missing requirements marks the run incomplete (engine exit **4**) while manifest-protected archive tasks can still preserve successful independent sections. Failed, disabled or unreported sections retain their previous snapshots. Optional task failures remain governed by `required: false` as before. Do not remove `collection_manifest: true` from an archive task.

Readiness is `ready`, `ready_with_warnings` (optional section failures), or `not_ready` (connection/incomplete/required-section failure). CLI exit codes: **0**, **6**, **1**, respectively; **2** is a configuration/usage error. A partial SQL read-only profile can legitimately be ready with warnings because protected native Agent/SSIS/instance exports require additional access. It does not mean those exports were captured.

Guidance uses observed permission indicators and actual failed sections. It suggests client installation/path fixes, certificate/endpoint checks, reviewed grants and retesting; it never applies grants or disables TLS verification. To generate the established read-access scripts:

```sh
python configure.py --config configbackup.yaml --diagnose-task collect-sql \
  --generate-access-fix review-sql-access --principal configbackup_reader --database ApplicationDB
```

The output directory must be fresh, the principal must already exist, and database names must be literal. Generated scripts are a **baseline read-access profile**, not an exact minimal patch for each error. PostgreSQL `pg_read_all_data` plus `BYPASSRLS` gives broad cluster-wide reads. Read [the access documentation](read-only-access.md) and review scripts before an administrator applies anything.

## Scheduled account and capability history

**Generate startup launcher → diagnostic** asks for a managed task and private report directory. It uses the same existing/Keychain/Secret Service/PowerShell vault references as a backup launcher. Generate launchers on the target OS with its actual absolute paths; cross-generating a Windows launcher on macOS does not translate filesystem paths. Each run writes a unique JSON file and returns its readiness status. The report records the effective OS identity (UID on POSIX, SID on Windows), host, Python path, working directory and observed database login. Report values never include passwords. Protect report directories; Windows needs a suitable NTFS ACL. Raw diagnostic reports have no automatic retention: place them under managed runtime retention or prune them deliberately.

A manual launcher run does **not** establish Task Scheduler/cron/launchd access. `scheduler_proven` stays false in the report because a process cannot reliably prove its scheduler provenance. Run the launcher through the actual scheduler, inspect the scheduler's result and compare its configured identity with the report. The [Windows lab helper](../tests/integration/Test-ScheduledLauncher.ps1) correlates a new report with an already registered diagnostic task. It does not create a task or elevate its account.

```sh
python configure.py --config configbackup.yaml --diagnose-task collect-sql \
  --report-directory /srv/configbackup/runtime/reports \
  --capability-history /srv/configbackup/runtime/readiness
```

Capability state is stored privately in SQLite. It is partitioned by effective profile, host and OS account; database selection, metadata-only and section switches start separate baselines. A failed attempt does not erase prior successful capabilities. Explicit failures produce `lost`; connection failure/omitted evidence produces `unverified`; later successful checks produce `recovered`. Gained capabilities are recorded too. A change from the baseline database login remains flagged until the original identity returns or an operator intentionally starts a new baseline directory. This state is evidence of collection access, not proof of the cause (permission revocation versus service/server failure).

The companion JSON telemetry contains aggregate counts only. Add its `*.json` files as monitoring sources to use new default alerts for lost/unverified capabilities, changed database identity and failed readiness. Existing notification cooldown/recovery/outbox behavior applies. No notification is sent just by running diagnostics. The latest telemetry is replaced atomically; each distinct profile/account retains one baseline and one current telemetry file. Remove obsolete baselines deliberately; this small state does not expire automatically. Keep the entire runtime directory outside archive/Git source trees.

## Setup and delivery checks

```sh
python configure.py --config configbackup.yaml --setup-check --diagnostic-report setup.json
python configure.py --config configbackup.yaml --setup-check --probe-notifications
# Explicitly sends one harmless message, only when requested:
python configure.py --config configbackup.yaml --test-notification operations-email
```

Setup checks create/remove an owned temporary file in existing archive/staging/Git directories, record free space, locate database executables and check credential-reference presence. They do not create missing backup directories. Git uses bounded, noninteractive `ls-remote`: read access does not prove push/PR permissions or bypass branch protection. SSH uses `BatchMode=yes`; custom `GIT_SSH_COMMAND` wrappers are not exercised by this probe. Notification settings are validated; optional SMTP EHLO/TLS/login sends **no MAIL/RCPT/DATA**. Recipient acceptance and delivery require the explicit send action. Webhooks/ntfy/heartbeat are not called by setup checks. A complete setup check does not claim that untested checks passed; inspect every row and then run the managed database diagnostic.

## Sanitizing generated SQL password placeholders

After a successful SqlPackage extraction, ConfigBackup inspects the new extraction's `Security` directory. Recognized standalone `CREATE LOGIN` and password-bearing `CREATE USER` scripts have only their generated password literal replaced with **`<CONFIGBACKUP_PASSWORD_REMOVED>`**, plus a stable explanatory comment. This is intentionally invalid executable SQL: supplying a new secure password is a required manual restore step. No known working password is substituted and no source-server password is changed.

This transformation occurs before manifest hashing, so filesystem/Git snapshots and comparisons use the same sanitized bytes. It does not keep the randomly generated literal; that literal was not the original source password and has no recovery value. Other schema files, real job/procedure/string contents and arbitrary external SQL are not rewritten. Generic comparison normalization and the secret gate remain in place. Unrecognized password-bearing native creation syntax fails that database collection, preserving the previous archived/Git database snapshot while independent databases continue.

The rule recognizes a complete creation script, with quoted/bracketed names, escaped literal quotes, and selected generated default-schema/database/language or policy options. It is not a universal T-SQL parser. New SqlPackage output formats need a reviewed rule and tests rather than a broad password regex. Review the generated comments before using these files for reconstruction. The code is in [Remove-DacFxPlaceholderPasswords.ps1](../collectors/sqlserver/Remove-DacFxPlaceholderPasswords.ps1); the [PowerShell regression script](../tests/integration/Test-DacFxSanitizer.ps1) checks bounded replacement, idempotence and refusal of unknown syntax.
