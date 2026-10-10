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
