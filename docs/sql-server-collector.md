# SQL Server / Agent / SSIS collector

`collectors/sqlserver/Collect-SqlServerConfiguration.ps1` builds a deterministic current-state SQL Server configuration tree. SQL logic remains outside the Python ConfigBackup engine; ConfigBackup only archives or Git-commits the collector's output.

## What it collects

### SQL Server instance

Using dbatools and SMO:

- Server/version/edition/collation/platform metadata.
- `Export-DbaInstance` configuration scripts such as `sp_configure`, logins, linked servers, endpoints, Extended Events, audits, Resource Governor, server roles and other supported instance categories.
- Availability Groups are handled separately: the collector records `IsHadrEnabled`, skips AG discovery cleanly when HADR is disabled, and exports `AvailabilityGroups.sql` only when HADR is enabled and AG collection has not been explicitly excluded.
- Password material is excluded from `Export-DbaInstance`.

### Databases

- Database metadata and configuration.
- Database files, sizes/free space/growth data.
- Filegroups and files.
- Per-object schema extraction using Microsoft SqlPackage `ExtractTarget=SchemaObjectType`.

SqlPackage's schema-object layout makes individual tables, views, procedures, functions, constraints and other objects independently diffable/versionable.

### SQL Server Agent

The collector now captures SQL Server Agent separately from the general instance export so scheduled automation is easy to inspect and diff:

- One JSON description per job.
- Best-effort SMO creation script per job.
- Job steps, subsystem, database, command, success/failure actions, retries, output file, proxy reference.
- Attached job schedules and recurrence settings.
- Job owner/category/enablement/notification settings.
- Agent-level settings.
- Operators.
- Alerts and operator notification mappings.
- Shared schedules, schedule-to-job mappings, and Agent categories.
- Proxy metadata and credential **names** (not credential secret values).
- Proxy-to-subsystem mappings.

Job execution/history data is intentionally excluded; it is operational telemetry rather than configuration and would create constant backup churn.

### SSISDB / project deployment model

If `SSISDB` exists, the collector first verifies that the database is online and accessible. By default it also requires the collecting principal to be either `sysadmin` or a member of the SSISDB `ssis_admin` database role. This is intentional: SSIS catalog views enforce row-level security, so a visibility-limited snapshot can make objects merely hidden by permissions look deleted. Use `-AllowPartialSsis` only when that tradeoff is explicitly acceptable.

The collector captures:

- Catalog properties.
- Folders.
- Projects and project metadata.
- Packages and package metadata.
- Project/package object parameters.
- Environments.
- Environment variables.
- Project-to-environment references.
- Parameter-to-environment-variable references.
- Explicit SSIS object permissions and SSISDB database-role membership.
- The deployed `.ispac` project stream unless `-SkipIspac` is used.
- An expanded copy of every `.ispac`, including individual `.dtsx` package files and project metadata, for useful text/XML diffs.

Sensitive SSIS environment variables and sensitive parameter default values are emitted as `<REDACTED>`. The public SSISDB catalog views already return `NULL` for sensitive environment-variable values; the collector explicitly emits a redaction marker rather than treating that as an ordinary empty value.

### Legacy SSIS

`-IncludeLegacySsis` optionally exports packages stored in `msdb.dbo.sysssispackages`, plus metadata. This is disabled by default because legacy package-deployment configurations can contain embedded/encrypted sensitive package content and are less uniform than SSISDB projects.

## Typical output

```text
sql-server/
├── collector.json
├── database-map.csv
├── instance/
│   ├── server.json
│   ├── databases.csv
│   ├── database-files.csv
│   ├── filegroups.csv
│   ├── scripts/
│   │   ├── sp_configure.sql
│   │   ├── logins.sql
│   │   ├── linkedservers.sql
│   │   └── ...
│   ├── agent/
│   │   ├── agent-settings.json
│   │   ├── jobs.csv
│   │   ├── operators.csv
│   │   ├── alerts.csv
│   │   ├── notifications.csv
│   │   ├── schedules.csv
│   │   ├── schedule-jobs.csv
│   │   ├── categories.csv
│   │   ├── proxies.csv
│   │   ├── proxy-subsystems.csv
│   │   └── jobs/
│   │       ├── Nightly_Backup.json
│   │       ├── Nightly_Backup.sql
│   │       └── ...
│   └── ssis/
│       ├── catalog-properties.csv
│       ├── folders.csv
│       ├── projects.csv
│       ├── packages.csv
│       ├── object-parameters.csv
│       ├── environments.csv
│       ├── environment-variables.csv
│       ├── environment-references.csv
│       ├── explicit-object-permissions.csv
│       ├── database-role-memberships.csv
│       └── projects/
│           └── Folder/
│               └── Project/
│                   ├── Project.ispac
│                   └── expanded/
│                       ├── Package1.dtsx
│                       ├── Package2.dtsx
│                       ├── @Project.params
│                       └── ...
└── databases/
    └── AppDb/
        ├── database.json
        ├── files.csv
        ├── filegroups.csv
        └── schema/
            └── ...
```

## Prerequisites

- PowerShell 5.1+; PowerShell 7 is recommended.
- dbatools PowerShell module.
- Microsoft SqlPackage unless `-SkipSchema` is used.
- Permissions sufficient to read the desired SQL Server, Agent and SSISDB objects.

Install dbatools:

```powershell
Install-Module dbatools -Scope CurrentUser
```

A convenient current SqlPackage installation is the .NET tool:

```text
dotnet tool install -g Microsoft.SqlPackage
```

Verify:

```text
sqlpackage /Version
```

Useful upstream documentation:

- https://learn.microsoft.com/sql/tools/sqlpackage/sqlpackage-extract
- https://docs.dbatools.io/Export-DbaInstance.html
- https://learn.microsoft.com/sql/ssms/agent/sql-server-agent
- https://learn.microsoft.com/sql/integration-services/catalog/ssis-catalog
- https://learn.microsoft.com/sql/integration-services/system-stored-procedures/catalog-get-project-ssisdb-database

## Authentication

By default the collector uses the identity running PowerShell/ConfigBackup. Integrated authentication/service identities are preferred for unattended operation.

Do not place SQL passwords in ConfigBackup YAML. If an alternate authentication mechanism is required, adapt the collector to use your approved secret store/identity mechanism.

## Database selection

Default: accessible user databases only.

```powershell
-Database 'AppDb,ReportingDb'
-ExcludeDatabase 'ScratchDb,Test*'
-IncludeSystemDatabases
-IncludeTempdb
```

## Optional switches

```text
-SkipSchema          Skip SqlPackage database schema extraction
-SkipInstanceExport  Skip dbatools Export-DbaInstance scripting
-SkipInventory       Skip database/file inventory
-SkipAgent           Skip dedicated SQL Agent collection
-SkipSsis            Skip SSISDB/legacy SSIS collection
-SkipIspac           Do not retain binary .ispac files; expanded contents remain
-IncludeLegacySsis   Also export legacy MSDB SSIS packages
-AllowPartialSsis     Permit a visibility-limited SSIS snapshot when not sysadmin/ssis_admin
-SkipHostConfiguration       Skip automatic local SQL Server on Linux host collection
-CollectLocalHostConfiguration Force local Linux host collection when using an alias/CNAME
-TrustServerCertificate
-AppendConnectionString 'MultiSubnetFailover=True;ApplicationIntent=ReadOnly'
```

`-SkipIspac` is useful for Git-only history because `.ispac` files are ZIP/binary artifacts and can cause repository growth while providing poor text diffs. The expanded `.dtsx`/project files remain available.

When using `storage: both`, a better option is usually to keep the exact deployable `.ispac` in the filesystem archive while excluding it only from Git:

```yaml
git:
  ignore:
    - '**/*.ispac'

- name: archive-sql-server
  type: directory
  source: ${SQL_STAGING}
  destination: sql-server
  storage: both
```

This preserves the binary deployment artifact in ConfigBackup's dated filesystem history while Git tracks the expanded `.dtsx` and metadata files that produce useful diffs.

### SSIS permissions and diagnostics

A complete SSIS snapshot requires `sysadmin` or SSISDB `ssis_admin` by default. This is stricter than the minimum permission needed to read one project because SSIS catalog views are row-level secured. For change/deletion tracking, completeness is more important than silently collecting only what the current principal happens to see.

The collector logs each SSIS phase individually (`catalog-properties`, `folders`, `projects`, `packages`, environments/parameters/permissions, project discovery, and each project export). A failure therefore identifies the exact query or project rather than only reporting that SSIS collection failed. `catalog.get_project` also requires READ permission on the project (or `ssis_admin`/`sysadmin`).

SQL/SSIS metadata queries are executed through dbatools `Invoke-DbaQuery -As DataSet -EnableException`, using the connected dbatools server object and an explicit `-Database` context. This avoids relying on SMO `ConnectionContext.ExecuteWithResults` for database-context switching and preserves binary `catalog.get_project` results as `byte[]` data.

## ConfigBackup integration

See `examples/sql-schema-collector.yaml` and `examples/full-stack-windows.yaml`.

```yaml
- name: collect-sql-server
  type: execute
  phase: pre_backup
  executable: pwsh
  arguments:
    - -NoProfile
    - -NonInteractive
    - -File
    - /opt/configbackup/collectors/sqlserver/Collect-SqlServerConfiguration.ps1
    - -SqlInstance
    - SQL01
  output_directory: ${CONFIGBACKUP_STAGING}/sql-server
  clean_output: true
  required: true

- name: archive-sql-server
  type: directory
  source: ${CONFIGBACKUP_STAGING}/sql-server
  destination: sql/SQL01
  depends_on: [collect-sql-server]
  storage: filesystem
```

Change the final task to `storage: git` or `storage: both` after configuring the top-level `git` section.

## Failure semantics

The core collector is designed to fail when required SQL/database extraction fails. ConfigBackup then skips the dependent snapshot task, preventing an incomplete generated tree from being interpreted as a successful scan with mass deletions.

Some secondary scripting (for example an individual SMO job script) is best-effort; the structured job/configuration record remains the primary change-tracking artifact.

## Stable-output design

The collector avoids run timestamps in generated current-state files. ConfigBackup/Git supply the history timestamp. This keeps SHA-256 comparison and Git diffs meaningful.

Inventory values that genuinely change, such as database file size/free space, naturally create changes in the corresponding inventory files without forcing unrelated schema/job/package files to change.

## Security

Even with dbatools password export disabled and catalog-marked sensitive SSIS values redacted, this output is operationally sensitive. SQL Agent commands, SSIS package/project contents, and other ordinary configuration may themselves contain embedded secrets that cannot be reliably identified or redacted generically. It can reveal server names, login names, job commands, file paths, linked-server definitions, SSIS connection/configuration metadata, environment names, package logic, and database/application names. Store it in protected filesystem locations and private Git repositories.
## Instance export diagnostics

The collector uses `Export-DbaInstance` for broad instance-level configuration, but always excludes `Databases`, `AgentServer`, and `AvailabilityGroups`. Databases are handled by the collector's inventory/SqlPackage flow, SQL Agent is exported separately into the granular `instance/agent` tree, and Availability Groups are conditionally exported only after `SERVERPROPERTY('IsHadrEnabled')` confirms that HADR is enabled. This avoids treating a normal non-HADR SQL Server instance as an export failure.

The dbatools instance-export phase runs with verbose progress enabled. If a dbatools component fails, ConfigBackup logs show the last component attempted plus the PowerShell error category, fully-qualified error ID, invocation position, and stack trace. Additional dbatools component types can be skipped with `-InstanceExclude`.


## SQL Server on Linux

The SQL collector is supported under PowerShell 7 on Linux. The database-facing collection remains the same: dbatools/SMO inventory and scripting, SQL Agent metadata, conditional Availability Group collection, and SqlPackage schema extraction.

When all of the following are true:

- SQL Server reports `HostPlatform = Linux`,
- the collector itself is running on Linux, and
- the SQL Server host resolves as the local machine,

ConfigBackup also writes a focused host-level tree under:

```text
instance/host-linux/
```

It includes:

```text
instance/host-linux/
├── mssql.conf
├── mssql-settings.csv
├── packages.csv
├── mssql-server.service.txt
├── service.json
└── paths.json
```

The Linux host collector records:

- `/var/opt/mssql/mssql.conf` when present.
- A parsed/stable CSV view of configured `mssql.conf` settings. Keys that look password/secret/token related are redacted in the parsed CSV.
- Installed SQL-related packages from `dpkg-query` or `rpm` (`mssql-*`, `mssql-tools*`, `msodbcsql*`, etc.).
- The effective `systemd` unit/drop-in text from `systemctl cat mssql-server.service`.
- Stable service metadata such as unit-file state, fragment/drop-in paths, configured user/group, `ExecStart`, and environment-file references. Volatile runtime fields such as PID/start time are intentionally omitted.
- Common SQL Server on Linux filesystem paths.

Microsoft documents `/var/opt/mssql/mssql.conf` as the SQL Server on Linux configuration file managed by `/opt/mssql/bin/mssql-conf`.

Host-level collection is automatic when locality can be verified. Disable it with:

```text
-SkipHostConfiguration
```

If you intentionally connect through a local alias/CNAME that prevents the collector from recognizing the local machine, use:

```text
-CollectLocalHostConfiguration
```

Only use that switch when the PowerShell process is actually running on the SQL Server host; otherwise it would describe the wrong Linux machine.

If SQL Server is remote, database/instance collection still works, but ConfigBackup cannot read that remote machine's `/var/opt/mssql` or `systemd` configuration. Run the system collector and/or SQL collector locally on that guest if host configuration is required.

SSISDB is normally absent on SQL Server on Linux; that is treated as not applicable and skipped cleanly.

### Linux prerequisites

Typical prerequisites are:

```bash
# PowerShell 7 installed from Microsoft's package repository or your approved source
pwsh --version

# dbatools, installed from PowerShell
pwsh -NoProfile -Command 'Install-Module dbatools -Scope CurrentUser'

# SqlPackage (one supported option)
dotnet tool install -g Microsoft.SqlPackage
```

Make sure the account running ConfigBackup can read `/var/opt/mssql/mssql.conf` and execute the non-mutating `systemctl show/cat` commands if you want host-level artifacts.

## SSISDB schema compatibility

The collector does not hard-code the identifier column exposed by `SSISDB.catalog.folders`. At runtime it inspects the catalog view and accepts either `folder_id` or `id`, then uses that detected column for folder export and project-to-folder joins. This is intentional because deployed SSISDB catalogs can expose different column names across builds/environments; the actual catalog schema is authoritative.
## SqlPackage schema extraction diagnostics

Per-database schema files are generated with SqlPackage using `ExtractTarget=SchemaObjectType`. The collector uses an encrypted source connection. Certificate validation remains enabled unless the collector is explicitly launched with `-TrustServerCertificate`.

Schema-model verification is **disabled by default**, matching SqlPackage's documented default and Microsoft's source-control extraction examples. To request DacFx model verification, add:

```powershell
-VerifySchemaExtraction
```

Verification can fail for databases with unresolved/external references even when the object definitions themselves can be extracted successfully, so it is not required for ConfigBackup history collection.

If SqlPackage exits nonzero, the collector writes the native console output plus verbose SqlPackage diagnostics to ConfigBackup stderr. The collector intentionally invokes SqlPackage through PowerShell's native invocation operator so its argument semantics match a command that can be reproduced directly at an administrator console. A certificate-chain error usually means either the SQL Server certificate must be trusted by the collector host or, only when intentionally accepted, `-TrustServerCertificate` should be used.

For advanced **non-secret** connection properties, use `-AppendConnectionString`. Example:

```powershell
-AppendConnectionString 'MultiSubnetFailover=True;ApplicationIntent=ReadOnly'
```

Those properties are appended to the dbatools connection and, for SqlPackage extraction, the collector switches to `/SourceConnectionString`. Endpoint, database, authentication, timeout, encryption, certificate-trust, and credential-bearing keys are rejected; use the collector's explicit parameters for those settings and never put passwords/tokens in YAML.

