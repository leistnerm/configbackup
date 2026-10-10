# Integration tests

Run from the release root against disposable databases only. Use fresh output directories. These tests create schemas, jobs and backups, take databases OFFLINE/RESTORING, and acquire blocking locks. They create unique `cbtest_*` objects and clean up their own databases/jobs. SQL's native `.bak` fixture remains in the specified server directory for inspection; remove it manually after testing. Inspect test logs if cleanup fails.

Use Python 3.10+, PyYAML, Git, PostgreSQL client tools, PowerShell, dbatools, and SqlPackage as applicable. Credentials come from the process environment or normal libpq facilities; never put real credentials in the examples or release archive.

```sh
python tests/integration/postgresql_live.py --bin-dir /path/to/postgresql/bin \
  --host 127.0.0.1 --port 55439 --user postgres --output /fresh/pg-test

# Set CONFIGBACKUP_TEST_SQL_PASSWORD through your shell or secret provider first.
python tests/integration/sqlserver_live.py --pwsh /path/to/pwsh \
  --module-path /path/to/powershell/modules --sqlpackage /path/to/sqlpackage \
  --server 127.0.0.1,51439 --user sa --backup-directory /var/opt/mssql/data \
  --output /fresh/sql-test

python tests/integration/multi_instance_live.py --pwsh /path/to/pwsh \
  --module-path /path/to/powershell/modules --sqlpackage /path/to/sqlpackage \
  --server-one 127.0.0.1,51439 --server-two 127.0.0.1,51440 --user sa \
  --output /fresh/multi-instance-test

python tests/integration/sql_services_fixture.py --pwsh /path/to/pwsh \
  --output /fresh/ssrs-protocol-test
```

The multi-instance test expects both endpoints to accept the same test credential. The SSRS test starts an unauthenticated loopback SOAP fixture; it validates the adapter protocol/failure behavior, not an actual SSRS installation. `Test-Sections.ps1` executes the real SQL collector's section wrapper with controlled successes/failures. Missing-module tests for the optional service adapter should yield exit 6 and failed scopes, with no eligible failed files.

## Advanced fixtures

Files in `fixtures/` are manual **templates**, deliberately not ready-to-run production scripts. Replace the visibly marked placeholders after creating an empty disposable database. Select that database explicitly for every batch; do not rely on a previous connection's `USE` state.

- SQL template: replace `{{DATABASE}}` and `{{MEMORY_FILE_PATH}}`, run on SQL Server 2022 Developer with In-Memory OLTP and Full-Text installed. It creates a memory filegroup/file, RLS, temporal and partitioned/columnstore tables, full-text objects, a native procedure, and fixed-name ConfigBackup test Agent jobs/schedules in msdb. The Agent job needs `sp_add_jobserver` before execution. Use an isolated instance to avoid fixed-name conflicts; review/drop those test jobs separately. Replication distribution/publication setup is not automated by this template because it changes instance-wide replication settings.
- PostgreSQL advanced template: replace `{{DATABASE}}` and `{{READER_ROLE}}`; create the reader role first. It adds quoted names, types, RLS, partitions, routines, indexes, grants and FDW metadata. Use an empty disposable database.
- PostgreSQL scheduler template additionally requires pg_cron/pgAgent packages and `shared_preload_libraries`/`cron.database_name` setup. Start pgAgent separately. Those server changes are not performed by ConfigBackup.

Collect twice without changing settings and run:

```sh
python collectors/common/compare_snapshots.py --before /snapshot/one --after /snapshot/two --output /results/diff.json
python collectors/common/snapshot_manifest.py /snapshot/one /results/hashes.json
```

Separate telemetry/manifest changes and native PostgreSQL safety-key changes from configuration changes. Deliberately alter a partition boundary and RLS policy in the disposable database, collect again, verify meaningful catalog/schema differences, then revert the fixture. Restore native PG schema dumps only into a separate disposable cluster: dumps contain `CREATE DATABASE` and reconnect commands. File recovery with `configbackup.py --restore` is distinct from restoring a live database.


## Read-access profile and 2.0 host tests

The read-access test creates a new uniquely named login/role and database, applies the generated grant scripts, verifies ordinary writes are rejected, collects a real schema and removes its test objects. SQL Server can return partial status because protected native service exports remain unavailable. PostgreSQL grants include broad cluster-wide reads and BYPASSRLS; use only a disposable cluster. PostgreSQL admin authentication uses normal libpq settings; the temporary role receives a generated password in the child environment. Its connection must be allowed by your test pg_hba.conf.

```sh
python tests/integration/read_only_access_live.py --engine postgresql \
  --bin-dir /path/to/postgresql/bin --host 127.0.0.1 --port 55439 --user postgres \
  --output /fresh/pg-access --confirm-disposable-server

# Set CONFIGBACKUP_TEST_SQL_PASSWORD through a secret provider first.
python tests/integration/read_only_access_live.py --engine sqlserver \
  --server 127.0.0.1,51439 --pwsh /path/to/pwsh \
  --module-path /path/to/modules --sqlpackage /path/to/sqlpackage \
  --output /fresh/sql-access --confirm-disposable-server
```

These probes are not a proof against every extension, inherited permission or privileged routine. See [read-only access](../../docs/read-only-access.md). The earlier richer fixture tests also caught unsafe inherited permissions and failed only the affected databases.

The [host coverage guide](../../docs/host-coverage.md) gives commands for the image-only AppleRAID fault lab, disposable Linux LVM/mdraid/XFS/ext4 lab, and isolated nftables counter test. Do not run Linux disk/firewall labs on a production host or shared network namespace.

For optional database health, populate the templates below in NEW disposable databases, then run SQL `-IncludeHealthMetrics -IncludeIndexHealth` or PostgreSQL `--include-health --bloat-table public.health_fixture`. Compare repeated configuration separately from changing `telemetry/`; verify SQL index page/density values and PG `bloat:public.health_fixture` results. Drop only the disposable databases afterwards.

- [SQL health fixture](fixtures/sqlserver-health.sql.template)
- [PostgreSQL health fixture](fixtures/postgresql-health.sql.template)
