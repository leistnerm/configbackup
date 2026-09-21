# Changelog

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
