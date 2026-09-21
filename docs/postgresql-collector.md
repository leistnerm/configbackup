# PostgreSQL collector

ConfigBackup includes `collectors/postgresql/collect_postgresql.py`, a cross-platform PostgreSQL configuration/schema collector designed for Windows and Linux.

The collector does **not** back up table data and is not a replacement for a PostgreSQL disaster-recovery strategy. It is intended to preserve database/cluster configuration and schema history so changes can be diffed through ConfigBackup's filesystem history, Git history, or both.

## Requirements

- Python 3.10+
- PostgreSQL client programs available in `PATH`, or specify `--bin-dir`:
  - `psql`
  - `pg_dump`
  - `pg_dumpall`
- A PostgreSQL account with enough metadata access for the objects you want to collect.

PostgreSQL 10+ is the practical target. Newer matching client tools are recommended. `pg_dump` itself enforces client/server compatibility rules.

No Python PostgreSQL driver is required.

## Authentication

There is deliberately no password argument.

Use normal libpq authentication, for example:

- `.pgpass` / `pgpass.conf`;
- `PGSERVICE` and a service file;
- GSSAPI/Kerberos;
- client certificates;
- a service identity;
- PostgreSQL environment variables supplied outside the YAML.

Do not place database passwords in ConfigBackup YAML or Git URLs.

The collector invokes PostgreSQL clients with non-interactive password prompting disabled so an unattended scheduled run fails rather than hanging on a prompt.

## Output layout

A typical snapshot looks like:

```text
postgresql/
├── collector.json
├── database-map.csv
├── cluster/
│   ├── server.json
│   ├── globals.sql
│   ├── databases.csv
│   ├── roles.csv
│   ├── role-memberships.csv
│   ├── tablespaces.csv
│   └── database-role-settings.csv
├── config/
│   ├── pg-settings.csv
│   ├── pg-file-settings.csv
│   ├── pg-hba-file-rules.csv
│   └── pg-ident-file-mappings.csv
├── replication/
│   └── slots.csv
└── databases/
    └── appdb/
        ├── database.json
        ├── schema.sql
        ├── inventory/
        │   ├── extensions.csv
        │   ├── schemas.csv
        │   ├── tables.csv
        │   ├── columns.csv
        │   ├── partitions.csv
        │   ├── sequences.csv
        │   ├── policies.csv
        │   ├── event-triggers.csv
        │   ├── languages.csv
        │   ├── foreign-data-wrappers.csv
        │   ├── foreign-servers.csv
        │   └── user-mapping-options.csv
        ├── replication/
        │   ├── publications.csv
        │   └── subscriptions.csv
        ├── schedulers/
        │   ├── pg-cron-jobs.csv
        │   └── pgagent/
        └── objects/
            ├── views/
            ├── materialized-views/
            ├── routines/
            ├── indexes/
            ├── constraints/
            └── triggers/
```

`schema.sql` is the authoritative native schema artifact for a database, except that logical-replication subscriptions are excluded when the installed `pg_dump` supports `--no-subscriptions`; subscription connection strings can contain credentials, so a separate redacted subscription inventory is written instead. The `objects/` tree is a diff-oriented representation intended to make individual object changes easy to review in Git. `schema.sql` remains authoritative for the schema content it includes because some PostgreSQL object properties are not fully represented by the normalized per-object files.

## Cluster globals and passwords

`cluster/globals.sql` is generated with:

```text
pg_dumpall --globals-only --no-role-passwords
```

This retains roles/tablespaces/global configuration without writing PostgreSQL role password hashes into the archive.

The collector refuses to create `globals.sql` if the installed `pg_dumpall` does not support `--no-role-passwords`.

Role inventory uses `pg_roles`, which does not expose password hashes.

## Deterministic pg_dump output

On PostgreSQL clients that support it, the collector supplies a fixed `--restrict-key` because PostgreSQL otherwise generates a random psql restriction key in plain dumps. PostgreSQL documents `--restrict-key` specifically for repeatable/comparable output.

The collector also removes dump banner `Started on` / `Completed on` timestamp comments if present. Schema content, server/client version banners, ownership, privileges, comments, and database definitions remain intact because changes to those are meaningful configuration changes.

## Per-object files

By default the collector emits individual diff-oriented SQL files for:

- views;
- materialized views;
- functions/procedures;
- indexes;
- constraints;
- triggers.

Overloaded routines are disambiguated with a stable hash of their identity arguments.

Tables are different because PostgreSQL does not expose a single catalog function equivalent to `pg_get_functiondef()` for complete `CREATE TABLE` DDL. The authoritative `schema.sql` already contains table definitions. If you want individual native table DDL files as well, enable:

```text
--split-table-ddl
```

That invokes `pg_dump --section=pre-data --table=...` once per table. It is more expensive on databases with many tables, so it is opt-in.

## Configuration capture

The collector captures effective and parsed configuration through PostgreSQL catalogs/views:

- `pg_settings` — effective runtime settings and their source;
- `pg_file_settings` — parsed configuration-file entries;
- `pg_hba_file_rules` — parsed client-authentication rules;
- `pg_ident_file_mappings` — parsed identity mappings where supported.

Known credential-bearing setting names and common `password=`, token, secret, API-key, and URI credential patterns are redacted.

### Raw config files

Raw config files are **not copied by default**. Files such as `postgresql.auto.conf` can contain connection strings or credentials.

If you explicitly want locally readable source configuration files, use:

```text
--include-raw-config-files
```

This should only be used when the destination is protected as secret-bearing operational data. It works only when the collector is running on a system that can read the server-reported file paths.

## Database selection

Default behavior is all connectable, non-template databases. `postgres` is included unless excluded.

Examples:

```text
--database appdb
--database 'app*'
--database appdb,reporting
--exclude-database scratch
--exclude-database 'test*'
--include-template-databases
```

Patterns use shell-style wildcard matching in the collector; they are not SQL patterns.

## Logical replication

Where available, the collector captures:

- replication slot identity/type/plugin/database (not runtime LSN/counter state);
- publications;
- subscriptions without the subscription connection string.

Subscription connection information is deliberately represented as `<REDACTED>`.

## FDW/user mappings

Foreign data wrapper/server metadata is captured. User-mapping option values whose option names look credential-bearing are redacted.

## Scheduled jobs

The collector auto-detects:

### pg_cron

`cron.job` definitions are exported to `schedulers/pg-cron-jobs.csv`.

The scheduled command itself is retained because it is the configuration being tracked. A command can itself contain a credential; protect the archive/Git repository accordingly.

### pgAgent

Where a `pgagent` schema exists, job, step, schedule, and job-class definitions are exported. Runtime fields such as last/next-run timestamps are omitted. Connection-string-like fields are redacted.

## Size inventory

Database and relation sizes change continuously and can create a Git commit every day even when configuration has not changed. They are therefore disabled by default.

Enable them with:

```text
--include-sizes
```

This adds cluster database-size and per-database relation-size inventories.

## Recommended ConfigBackup task

```yaml
variables:
  CONFIGBACKUP_HOME: /opt/configbackup
  PG_STAGING: ${CONFIGBACKUP_STAGING}/postgresql

tasks:
  - name: collect-postgresql
    type: execute
    phase: pre_backup
    executable: python3
    arguments:
      - ${CONFIGBACKUP_HOME}/collectors/postgresql/collect_postgresql.py
      - --host
      - localhost
      - --user
      - configbackup
    output_directory: ${PG_STAGING}
    clean_output: true
    timeout: 7200
    required: true

  - name: postgresql-history
    type: directory
    source: ${PG_STAGING}
    destination: postgresql
    depends_on: [collect-postgresql]
    storage: both
    retention_policy: important
```

`clean_output: true` is important: every collector run should start from an empty staging tree so an object removed from PostgreSQL disappears from the current snapshot and can be detected correctly by ConfigBackup/Git.

## Snapshot safety

Failure of a required native schema dump or core database inventory causes the collector to exit nonzero. A dependent ConfigBackup task is then skipped, which prevents a partial PostgreSQL snapshot from being interpreted as mass deletion.

Optional metadata sections are best-effort because visibility and available catalog views differ by PostgreSQL version and privileges. Optional failures are recorded in `collector.json` and on stderr.

## Useful standalone commands

Help:

```bash
python3 collectors/postgresql/collect_postgresql.py --help
```

Collect locally using libpq defaults:

```bash
CONFIGBACKUP_OUTPUT=/tmp/pg-config \
python3 collectors/postgresql/collect_postgresql.py
```

Collect selected databases:

```bash
python3 collectors/postgresql/collect_postgresql.py \
  --output /tmp/pg-config \
  --host pg01 \
  --user configbackup \
  --database appdb \
  --database reporting
```

Use a libpq service:

```bash
python3 collectors/postgresql/collect_postgresql.py \
  --output /tmp/pg-config \
  --service production
```

## What this is not

This collector contains configuration/schema definitions, not database table data. Continue to use an appropriate PostgreSQL data/DR strategy such as physical/base backups with WAL archiving, managed-service backups, or another tested recovery design.
