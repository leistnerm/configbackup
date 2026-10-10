# ConfigBackup documentation

Start with the project-level `README.md` and `configbackup.example.yaml`, then use these references as needed:

- `configuration.md` — complete YAML structure, task types, variables, storage modes, and path behavior.
- `retention.md` — indefinite/simple/tiered retention, templates, minimum-version floors, deleted-object lifecycle, and pruning.
- `git-storage.md` — Git history, isolated worktree/automation-branch mode, GitHub PR automation, Git-only ignores, and credential setup — Git-backed current snapshots, commits, remotes/GitHub, push behavior, deletion semantics, and binary guidance.
- `commands-and-collectors.md` — collector execution, staging, dependencies, stdout/stderr capture, environment, and safety.
- `system-collector.md` — Windows/Linux guest hardware/storage/network/service/task/software/patch/share inventory.
- `sql-server-collector.md` — SQL Server databases/schema, instance configuration, SQL Agent, SSISDB, and optional legacy SSIS.
- `postgresql-collector.md` — PostgreSQL cluster globals, schema dumps, configuration/catalog inventory, replication, pg_cron, and pgAgent.
- `scheduling.md` — running ConfigBackup from Windows Task Scheduler, cron, and systemd timer.
- `schedule-analysis.md` — cross-system task timelines, historical run durations, potential overlaps, and per-report exclusions.
- `state-and-recovery.md` — internal state, run manifests, interruption/recovery behavior, and backup-state considerations.
- `../SECURITY.md` — credentials, sensitive collected content, and destructive-operation safeguards.

## 2.0 additions

- [Release/migration](release-2.0.0.md)
- [Operations, email and runtime history](operations.md)
- [Guided configuration and authentication launchers](configuration-tools.md)
- [Read-only access and permission audits](read-only-access.md)
- [Host/storage/firewall/SMART coverage](host-coverage.md)

- [Database connection/access diagnostics](database-diagnostics.md)
