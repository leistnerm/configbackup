# Included collectors

ConfigBackup keeps application-specific discovery outside the Python backup engine. Included collectors write deterministic current-state trees that can be stored with dated filesystem history, Git history, or both.

## `system/collect_system.py`

Cross-platform Windows/Linux guest inventory: CPU/memory, OS, disks/partitions/volumes, Storage Spaces or Linux storage topology, network configuration, shares, services, scheduled tasks/timers/cron, installed software/packages, patches, roles/features, drivers, accounts/groups, firewall, pagefile/swap-related state, and other rebuild-relevant data.

See `../docs/system-collector.md`.

## `sqlserver/Collect-SqlServerConfiguration.ps1`

SQL Server collector using dbatools/SMO plus SqlPackage: instance configuration, database metadata/files/filegroups, per-object schema, SQL Server Agent, SSISDB projects/packages/environments/permissions, and optional legacy MSDB SSIS packages.

See `../docs/sql-server-collector.md`.

## `postgresql/collect_postgresql.py`

Cross-platform PostgreSQL configuration/schema collector using native `psql`, `pg_dump`, and `pg_dumpall`: password-safe cluster globals, per-database schema dumps, catalog/configuration inventory, logical replication metadata, Git-friendly per-object definitions, and pg_cron/pgAgent discovery.

See `../docs/postgresql-collector.md`.

