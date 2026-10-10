# Read-only database access and protected exports

The normal collector profile requires full visibility: SQL sysadmin for protected instance exports and dbo/sysadmin for database certification; PostgreSQL superuser. The optional read-only profile permits checked metadata access while preserving sections that cannot be fully observed. It never retries with a more privileged identity.

Generate administrator-reviewable scripts from the configuration editor's **Generate database access scripts** menu. Use an existing, newly dedicated login/role. No password, connection or permission change occurs in the generator. Applying the scripts is an administrator action. Read every generated warning before applying them.

## SQL Server

`grant-read-access.sql` adds server metadata/state access, database metadata/state access and explicit SELECT on SQL Agent/history/backup catalog tables. SQL Server 2022 additionally uses the separate performance/security-definition permissions. Replication catalog SELECT is granted only where those catalog objects exist in selected databases. It does not grant SELECT on application tables.

No sysadmin, db_owner, SQLAgentUserRole/ReaderRole/OperatorRole, application DML, CONTROL or general EXECUTE permission is granted. The Agent reader role is deliberately avoided because it can create/manage jobs owned by that account. Native database schema extraction still needs its own permissions to succeed; any extraction failure preserves that database.

Run the collector with `-ReadOnlyAccess`. It checks metadata visibility, rejects explicit metadata-hiding DENYs and audits common write/application-procedure execution rights in each selected database. Direct Agent CSV reads use individual file scopes; existing native job scripts and Agent settings are marked failed and preserved. Privileged native instance exports and protected SSIS catalogs can remain unavailable. `-SkipInstanceExport` can avoid attempted protected script exports without deleting earlier files.

The permission audit is conservative: an executable application procedure is rejected even if it only reads. Inherited permissions or replication-installed procedures can therefore make an otherwise useful database unavailable under this profile. Review the account's effective permissions; do not add administrative roles merely to make a report green.

## PostgreSQL

Requires PostgreSQL 14+ for `pg_read_all_data`. `grant-cluster-read-access.sql` adds `pg_monitor`, `pg_read_all_data`, BYPASSRLS and selected CONNECT grants. **pg_read_all_data and BYPASSRLS are cluster-wide read privileges, broader than the selected collection databases.** They are needed by this profile to avoid silently hidden table/scheduler metadata and support native schema dump locks. Do not apply this profile if that access is unacceptable.

The role gains no CREATE ROLE/DATABASE, DML, replication, server-file write or administrative role. The script rejects an already privileged role instead of silently downgrading it. `default_transaction_read_only=on` is a guardrail; the role can change this default, so it is not the security boundary.

The optional `grant-file-settings-read.sql` grants execution on three specific built-in functions exposing parsed server/HBA/ident settings in the maintenance database. It does not grant `pg_read_server_files`. Without these grants, affected settings fail and remain preserved. Cluster collection currently treats optional cluster-query failures conservatively across its top-level cluster/config/replication scopes.

Run `audit-database-access.sql` in every selected database before using `--read-only-access`. The collector also rejects common inherited table/sequence write rights, schema creation and executable SECURITY DEFINER functions per database. PostgreSQL has no per-user DENY that overrides PUBLIC. Existing PUBLIC privileges must be reviewed by the database administrator; the generated scripts do not remove them from everyone.

A live pg_cron fixture had a PUBLIC DELETE grant on its history table. The audit caught it, and the read-only collector rejected that database while collecting another clean database. This is a real limitation, not an empty successful snapshot.

## What the checks establish

Tests created temporary identities, applied generated grants, verified reads and native schema extraction, and rejected INSERT/ALTER/CREATE operations. SQL Agent job creation was also rejected. The audit caught inherited permissions and preserved affected sections.

Neither the scripts nor the audit formally prove that every arbitrary installed extension, C function, ownership chain or publicly executable procedure is harmless. Existing identities may have rights unrelated to these scripts. Run audits under your access-control process; use an isolated reporting/replica environment when stronger enforcement is required. Filesystem, Windows service/GPO, SSIS/SSRS/SSAS and cluster privileges are separate from database grants. No Windows integrated authentication or enterprise service read-only profile was tested here.

## Test the account before scheduling

The configuration editor now provides [Database connection tests](database-diagnostics.md). It shows actual per-section collector outcomes, including protected exports that remain unavailable, without changing backups or applying permissions. Repeat after grants change.
