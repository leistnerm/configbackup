# PostgreSQL collector

`collect_postgresql.py` creates a deterministic PostgreSQL configuration/schema snapshot for ConfigBackup.

It intentionally uses PostgreSQL's native clients (`psql`, `pg_dump`, and `pg_dumpall`) instead of a Python database driver. Authentication is therefore handled by normal libpq mechanisms such as `.pgpass`, `PGSERVICE`, GSSAPI/Kerberos, certificates, or environment configured outside ConfigBackup YAML.

Highlights:

- password-safe cluster globals via `pg_dumpall --globals-only --no-role-passwords`;
- one authoritative `pg_dump --schema-only --create` file per selected database;
- role/membership/tablespace/database inventory;
- effective server settings plus parsed config/HBA/ident rules;
- extensions, schemas, tables, columns, partitions, sequences, RLS policies, event triggers, languages, FDWs, publications, subscriptions, and replication slots;
- Git-friendly per-object SQL files for views, materialized views, routines, indexes, constraints, and triggers;
- optional per-table pre-data DDL using native `pg_dump` (more expensive);
- pg_cron and pgAgent discovery/export where installed;
- optional database/relation size inventory;
- optional raw config-file copying only with explicit opt-in because raw files can contain secrets.

See `../../docs/postgresql-collector.md` and `../../examples/postgresql-collector.yaml`.
