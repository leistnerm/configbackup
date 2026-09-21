# Commands and collectors

ConfigBackup deliberately keeps application/database-specific logic outside the core program.

Ready-to-use collectors are included:

- `collectors/sqlserver/Collect-SqlServerConfiguration.ps1` — SQL instance/database schema, SQL Agent, and SSIS; see `docs/sql-server-collector.md`.
- `collectors/system/collect_system.py` — Windows/Linux guest hardware, storage, network, services/tasks, software/patches, shares, and related configuration; see `docs/system-collector.md`.

For SQL Server, for example, a collector might use PowerShell/SMO, Python/ODBC, or `sqlcmd` to create files representing instance configuration, database settings, filegroups, files, tables, partitions, indexes, constraints, triggers, stored procedures, views, functions, and other objects. ConfigBackup then versions those files.

## `execute`: prepare a tree, then back it up

```yaml
variables:
  SQL_SCHEMA_STAGING: ${CONFIGBACKUP_STAGING}/sql-schema

tasks:
  - name: collect-sql-schema
    type: execute
    phase: pre_backup
    executable: python3
    output_directory: ${SQL_SCHEMA_STAGING}
    arguments:
      - /opt/scripts/dump_sql_schema.py
      - --server
      - SQL01
      - --output
      - ${CONFIGBACKUP_OUTPUT}
    clean_output: true
    timeout: 1800
    stderr: log

  - name: sql-schema
    type: directory
    source: ${SQL_SCHEMA_STAGING}
    destination: sql/SQL01
    depends_on:
      - collect-sql-schema
```

This model is preferred for schema objects because each generated file is independently versioned. If only one stored procedure changes, only that object needs a new archive version.

## Staging safety

Every task receives a `CONFIGBACKUP_OUTPUT` variable pointing to a per-task staging directory. If `output_directory` is set on an execute task, `CONFIGBACKUP_OUTPUT` points to that directory instead. A dependent task has its own `CONFIGBACKUP_OUTPUT`, so use a shared variable/path (as above) when the collector and backup task must reference the same staging tree.

With:

```yaml
clean_output: true
```

an `execute` task removes and recreates its output directory before running. This prevents stale files from a prior collector run from being mistaken for objects that still exist. For safety, recursive cleaning is restricted to a directory beneath `CONFIGBACKUP_STAGING` by default.

If you intentionally point a collector at some other disposable directory, opt in explicitly:

```yaml
output_directory: /var/tmp/my-dedicated-schema-dump
clean_output: true
allow_clean_outside_staging: true
```

Do not enable this for a directory that contains unrelated data. ConfigBackup refuses to clean the filesystem root, backup root, internal archive directories, the staging root itself, or a symlinked output directory.

A collector may ignore this facility and write to any configured location instead.

## Dependency safety

A dependent backup task is skipped if its collector did not succeed.

That is important for deletion tracking: a collector failure does not produce a successful scan of an incomplete tree, so missing/deleted counters for that backup task do not advance.

## `command`: stdout is the artifact

```yaml
- name: instance-configuration
  type: command
  executable: python3
  arguments:
    - /opt/scripts/dump_instance_configuration.py
    - --server
    - SQL01
  output: sql/SQL01/instance/configuration.txt
  timeout: 300
  stderr: log
```

stdout is written to a temporary file, hashed, compared to the previous archived version, and only stored if its content changed.

A nonzero process exit code prevents the artifact from being committed.

## stderr modes

### `log` (default)

stderr is written to the ConfigBackup application log. A nonzero exit code marks the command failed.

### `discard`

stderr is discarded.

### `merge`

stderr is merged into stdout. For a `command` task this means merged output becomes part of the archived artifact.

### `capture`

For a `command` task, stderr is archived separately. Specify an output path if desired:

```yaml
stderr: capture
stderr_output: sql/SQL01/instance/configuration-errors.txt
```

Otherwise ConfigBackup uses `<output>.stderr`.

## Working directory and environment

```yaml
working_directory: /opt/scripts
environment:
  SQL_SERVER: SQL01
  OUTPUT_FORMAT: sql
```

The child also receives ConfigBackup's automatic environment variables.

## Avoid credentials in YAML

Prefer:

- Windows Integrated Authentication,
- Kerberos/service identities,
- managed identities where applicable,
- environment variables injected by the scheduler/service environment,
- OS credential stores/secrets facilities.

Avoid embedding database passwords or API secrets directly in YAML or command-line arguments.

## Suggested SQL schema layout

A collector could produce:

```text
SQL01/
  instance/
    configuration.txt
    databases.csv
  MyDatabase/
    database.sql
    files.sql
    filegroups.sql
    tables/
      Customer.sql
      Orders.sql
    indexes/
      IX_Customer_Name.sql
    views/
      CurrentOrders.sql
    procedures/
      GetCustomer.sql
    functions/
    triggers/
    constraints/
    partitions/
```

This is only an example; ConfigBackup has no SQL-specific assumptions.
