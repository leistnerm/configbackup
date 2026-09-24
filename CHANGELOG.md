# Changelog

## 1.4.4 - 2026-09-24

- Fixed GitHub pull-request automation so `gh pr list` / `gh pr create` no longer depend on the process current directory or on GitHub CLI inferring the repository from a temporary linked worktree.
- ConfigBackup now derives the GitHub `[HOST/]OWNER/REPO` selector from the configured Git remote and supplies it to GitHub CLI through `GH_REPO`.
- Added optional `git.pull_request.repository` override (`OWNER/REPO` or `HOST/OWNER/REPO`) for unusual remote layouts or explicit configuration.
- Added regression coverage for HTTPS GitHub remotes, SCP-style SSH/GitHub Enterprise remotes, and PR automation with an explicit repository selector.

## 1.4.3 - 2026-09-24

- Enabled SqlPackage `ScriptSortElementsByName=True` by default for SQL Server schema extraction. This reduces non-semantic Git/hash churn when DacFx returns child elements (including extended properties) in a different order across otherwise identical extracts.
- Added SQL collector switch `-DisableSchemaElementSorting` for troubleshooting or compatibility; normal collection should leave deterministic sorting enabled.
- SQL collector logs and `collector.json` now record whether schema element sorting is enabled.

## 1.4.2 - 2026-09-23

- Pull-request mode now detects a stale registered worktree that still has the ConfigBackup automation branch checked out and removes it before resetting/reusing the branch.
- Automatic stale-worktree cleanup is safety-scoped to recognized ConfigBackup worktree roots: the current configured/default worktree root, `${TEMP}/cbwt`, and the legacy 1.4.0 `${TEMP}/configbackup/.../git-worktrees/...` layout.
- If the automation branch is checked out in an unrelated/user-owned worktree, ConfigBackup refuses to remove it and reports the path instead.
- Added regression coverage for upgrading from the legacy 1.4.0 worktree layout to the short 1.4.1+ layout.

## 1.4.1 - 2026-09-23

- Shortened pull-request-mode Git worktree paths. The default is now `${TEMP}/cbwt/<short-id>` (for example `%TEMP%\cbwt\a1b2c3d4e5f6` on Windows) instead of nesting worktrees under ConfigBackup staging paths.
- Added/retained `git.worktree_root` as an explicit override; ConfigBackup creates the short per-repository/branch worktree ID beneath that root (for example `C:\cbwt\a1b2c3d4e5f6`).
- On Windows, ConfigBackup passes `-c core.longpaths=true` to its Git commands so long-path checkout support does not depend on global Git configuration.
- Added regression coverage for default/overridden worktree roots and Windows Git long-path command construction.

## 1.4.0 - 2026-09-23

- Added `git.mode: pull_request` for safely using an existing/shared repository without switching or modifying the user's normal checkout.
- Pull-request mode fetches the configured remote, auto-detects its default branch when `base_branch: auto`, maintains a ConfigBackup automation branch, and uses an isolated linked Git worktree.
- Added optional GitHub pull-request automation through the `gh` CLI; existing open PRs are reused and updated by later runs.
- Added configurable PR title/body, draft mode, labels, and reviewers.
- Added `git.ignore` for Git-only exclusion patterns. ConfigBackup writes a managed `.gitignore` block inside its snapshot subtree, preserves unrelated `.gitignore` content, and untracks matching files that were previously committed.
- Recommended `**/*.ispac` Git exclusion so `storage: both` retains deployable SSIS artifacts in filesystem history while Git tracks expanded `.dtsx`/metadata.
- Added isolated-worktree regression coverage proving dirty files in the normal checkout remain untouched.
- Added GitHub CLI PR-creation integration coverage and Git-ignore filesystem/Git split-storage coverage.
- Expanded Git documentation for existing repositories, worktrees, automation branches, PR workflows, Git Credential Manager, SSH/deploy keys, GitHub CLI authentication, and unattended Task Scheduler/cron operation.

## 1.3.15 - 2026-09-23

- Fixed SqlPackage `SchemaObjectType` extraction to let SqlPackage create the target schema directory instead of pre-creating it.
- Removes any stale per-database schema target before extraction, matching the manually validated SqlPackage workflow.
- SqlPackage stdout/stderr now flow directly to ConfigBackup logging instead of being redirected inside PowerShell.

## 1.3.14

- Changed SqlPackage execution back to PowerShell's native invocation operator with native-error promotion temporarily disabled, matching the standalone SqlPackage command path validated on Windows.
- Added `/Diagnostics:True` and verbose SqlPackage diagnostic logging while preserving explicit exit-code handling.
- Added optional `-AppendConnectionString` for non-secret advanced SQL connection properties such as `MultiSubnetFailover=True` and `ApplicationIntent=ReadOnly`; credential/authentication/endpoint/database/TLS keys are rejected.
- Applies appended non-secret connection properties consistently to dbatools and SqlPackage connections.
- Updated application and collector version metadata to 1.3.14.

## 1.3.13

- Fixed a PowerShell parser error in the SQL Server collector caused by a trailing comma in the SqlPackage argument array.
- Added static regression coverage for trailing-comma parser hazards in multiline PowerShell array literals used by the collector.
- Updated application and collector version metadata to 1.3.13.

## 1.3.12

- Fixed SqlPackage schema-extraction diagnostics on PowerShell versions where a non-zero native process exit can honor `$ErrorActionPreference = 'Stop'` and throw before the collector can inspect `$LASTEXITCODE` or captured output.
- SqlPackage is now launched with `System.Diagnostics.ProcessStartInfo`, capturing exit code, stdout, stderr, and the diagnostics file independently of PowerShell native-command error behavior.
- Removed `/Quiet:True`; SqlPackage output is captured silently on success and emitted only on failure.
- Added regression coverage requiring the process wrapper and stderr/stdout capture.
- Updated application and collector version metadata to 1.3.12.

## 1.3.11

- SQL Server collector: SqlPackage schema extraction now emits per-database error diagnostics on failure instead of suppressing the useful native error output.
- SQL Server collector: added a SqlPackage diagnostics file with `Error`-level tracing; the last diagnostic lines are copied to ConfigBackup stderr when extraction fails.
- SQL Server collector: schema-model verification is now opt-in with `-VerifySchemaExtraction`, matching SqlPackage's documented default and source-control extraction examples.
- SQL Server collector: explicitly records/uses encrypted SqlPackage source connections and the configured `TrustServerCertificate` choice in extraction logging.
- Updated application and collector version metadata to 1.3.11.

## 1.3.10

- Fixed SQL Server/SSIS collection when a catalog query returns zero rows. PowerShell can collapse an empty converted row set to `$null`; `Write-StableCsv` now treats null/empty collections as a valid empty CSV result.
- Added SSIS metadata row-count logging so each catalog section reports the number of rows returned before serialization.
- Updated application and collector version metadata to 1.3.10.

## 1.3.9

- Fixed SQL Server collector `Invoke-QueryTable` result handling under PowerShell: `DataTable` is enumerable and could be unwrapped into `DataRow` pipeline output when returned from the helper.
- The query helper now uses `Write-Output -NoEnumerate` so callers reliably receive the `DataTable` and can safely use `.Rows` / `.Columns`.
- This fixes the SSISDB preflight failure observed immediately after a successful `Invoke-DbaQuery -As DataSet` execution.
- Added regression coverage to require non-enumerating `DataTable` return behavior.
- Updated application and collector version metadata to 1.3.9.

## 1.3.8

- Changed SQL Server collector query execution from direct SMO `ConnectionContext.ExecuteWithResults` calls to dbatools `Invoke-DbaQuery -As DataSet -EnableException`.
- Fixes SSISDB collection on environments where direct SMO database-context switching fails even though `Invoke-DbaQuery -Database SSISDB` succeeds.
- The same supported dbatools query path is now used for SQL Agent metadata and optional legacy SSIS queries as well.
- Preserves the existing DataSet/DataTable handling, including binary `.ispac` project streams returned by `catalog.get_project`.
- Updated application and collector version metadata to 1.3.8.

## 1.3.7

- Reworked the SSISDB preflight to avoid the failing `master` / `HAS_DBACCESS` query. The collector now uses the already-discovered SMO SSISDB object for status metadata and proves access by executing a direct query in `SSISDB`.
- Combined SSISDB access and role-membership validation into one direct query that records the effective login/database user and `sysadmin` / `ssis_admin` state.
- Added richer exception diagnostics around the SSISDB direct-access probe.
- Retained the 1.3.6 runtime-adaptive `catalog.folders` identifier handling, SQL Server on Linux host collection, and Git credential/setup documentation.
- Updated collector/application version metadata to 1.3.7.

## 1.3.6

- Made SSISDB folder-key handling runtime-adaptive. The collector now inspects the actual `catalog.folders` view and supports either `folder_id` or `id` instead of hard-coding one schema shape. This corrects the 1.3.5 assumption after SQL Server 2022 installations were observed exposing `folder_id`.
- Added detailed SSISDB preflight sub-step logging and contextual errors for status/access and role-membership queries.
- Added SQL Server on Linux host collection when the collector runs locally on the Linux SQL guest: `/var/opt/mssql/mssql.conf`, parsed settings, SQL-related package versions, stable `systemd` unit/drop-in metadata, and host paths.
- Added `-SkipHostConfiguration` and `-CollectLocalHostConfiguration` switches for Linux-host collection control.
- Added a SQL Server on Linux example configuration.
- Expanded Git documentation into a command-line setup runbook covering Windows Git Credential Manager/Windows Credential Manager, HTTPS, SSH keys/deploy keys, Linux GCM credential stores, credential-helper security, remote verification, and Task Scheduler/cron identity requirements.
- Updated collector/application version metadata to 1.3.6.

## 1.3.5

- Fixed SSISDB project discovery: `catalog.folders` exposes its key as `id`, so project discovery now joins `catalog.projects.folder_id` to `catalog.folders.id`.
- Added SSISDB preflight checks for ONLINE state, database access, `sysadmin`, and `ssis_admin` membership.
- Complete SSIS snapshots now require full catalog visibility by default to avoid row-level-security changes being misinterpreted as object deletions.
- Added `-AllowPartialSsis` as an explicit opt-in for visibility-limited SSIS snapshots.
- Added step-level SSIS logging and contextual errors for every metadata query, project discovery, and individual `.ispac` export.
- Added regression coverage for the corrected SSIS folder join and SSIS permission preflight.

## 1.3.4

- Fixed SQL Server collection on instances where Always On Availability Groups (HADR) is not enabled.
- `Export-DbaInstance` now always excludes `AvailabilityGroups`; the collector queries `SERVERPROPERTY('IsHadrEnabled')` and exports AG definitions separately only when HADR is enabled.
- Added `IsHadrEnabled` to SQL Server `instance/server.json` and explicit logging when AG collection is skipped or excluded.
- Added a regression test for non-HADR SQL Server instances.

## 1.3.3

- Expanded Windows Firewall inventory to capture actual rule semantics: local/remote addresses, protocol, local/remote ports, ICMP type, application/package, service, interface/interface type, security conditions, direction, action, profile, and policy source.
- Windows firewall collection now queries `ActiveStore`, representing the effective resultant policy including applicable Group Policy and local rules.
- Added `network/firewall-rules.csv` with a concise `Summary` column such as `Inbound Block TCP local-port=445 remote-address=192.0.2.10`; the complete structured data remains in `firewall-rules.json`.

## 1.3.2

- SQL Server collector: exclude `AgentServer` from the broad `Export-DbaInstance` pass because SQL Agent is already collected separately in a granular form.
- SQL Server collector: enable dbatools verbose progress for instance export so the last component attempted is visible in ConfigBackup logs.
- SQL Server collector: emit detailed PowerShell/dbatools error metadata when `Export-DbaInstance` fails, making permission/unsupported-component failures diagnosable without editing the script.

## 1.3.1

- Fixed SQL Server collector startup failure under PowerShell `Set-StrictMode` when `-Database` and/or `-ExcludeDatabase` were omitted or expanded to zero/one value.
- Database include/exclude selections are now explicitly materialized as arrays before `.Count` is evaluated.

## 1.3.0

- Added cross-platform PostgreSQL configuration/schema collector using native `psql`, `pg_dump`, and `pg_dumpall`.
- Added password-safe cluster globals (`--no-role-passwords`), role/membership/tablespace/database inventory, effective settings, parsed config/HBA/ident views, database-role settings, and replication-slot inventory.
- Added authoritative per-database native schema dumps with deterministic dump output and subscription connection strings excluded where supported.
- Added per-database extension/schema/table/column/partition/sequence/RLS/event-trigger/language/FDW/logical-replication inventories.
- Added Git-friendly per-object SQL files for views, materialized views, routines, indexes, constraints, and triggers, plus optional native per-table pre-data DDL.
- Added pg_cron and pgAgent scheduler discovery/export.
- Added optional size inventories and explicit opt-in raw PostgreSQL config-file copying.
- Added PostgreSQL collector documentation, standalone example, Linux full-stack example, security guidance, and collector unit/smoke coverage.

## 1.2.0

- Added provider-neutral Git storage with per-task `storage: filesystem`, `git`, or `both`.
- Added automatic Git initialization, clean-worktree protection, branch/remote configuration, change-only commits, optional push, rollback on required-task failure, and Git-aware deletion handling.
- Added documentation and examples for GitHub/GitLab/Azure DevOps/local Git remotes without storing credentials in YAML.
- Added cross-platform guest-system collector for Windows and Linux hardware, CPU/memory, storage/volume layout, network configuration, services, scheduled tasks/timers, software/packages, patches, roles/features, drivers, accounts/groups, firewall state, swap/pagefile, and related configuration.
- Added Windows Storage Spaces inventory and Linux mount/LVM/mdraid/multipath inventory where available.
- Expanded the SQL Server collector with SQL Server Agent jobs/steps/schedules/operators/alerts/proxies and individual recreatable job scripts where SMO supports scripting.
- Added SSISDB collection: folders, projects, packages, parameters, environments, references, redacted sensitive values, deployable `.ispac` export, and expanded project/package files.
- Added optional legacy MSDB SSIS package inventory.
- Added full-stack Windows/Linux and Git-backed example configurations plus dedicated system/Git documentation.
- Hardened recursion protection so configured Git repositories cannot be accidentally ingested as backup sources.

## 1.1.0

- Added optional SQL Server PowerShell collector using Microsoft SqlPackage and dbatools.
- Added deterministic per-database `SchemaObjectType` extraction for schema-change history.
- Added instance-level dbatools exports with volatile headers and password material excluded by default.
- Added database, file-space, and filegroup inventory output.
- Added SQL Server collector documentation and ready-to-run YAML example.

## 1.0.0

Initial implementation.

- Cross-platform Windows/Linux file, directory, and recursive glob backup.
- SHA-256 change detection with same-day timestamp disambiguation.
- Collector (`execute`) and captured-output (`command`) tasks.
- Task phases, dependencies, timeouts, working directories, environment variables, and stderr policies.
- External staging by default with configurable fixed staging root.
- Deleted-object detection, consecutive-missing thresholds, `_deleted` history, metadata sidecars, and mass-deletion guard.
- Named retention templates and task-local overrides.
- Indefinite, simple, and tiered retention with minimum-version safety floors.
- Active/deleted retention lifecycle, grace periods, size limits, and optional deleted-generation purge.
- `--validate`, `--show-config`, `--dry-run`, and `--prune` modes.
- Single-instance locking, rotating logs, state backup, and per-run JSON manifests.
- Unit/integration test coverage for naming, path mapping, policy resolution, retention bucketing, unchanged detection, same-day changes, and deletion transitions.
