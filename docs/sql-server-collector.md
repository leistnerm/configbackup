# SQL Server / Agent / SSIS collector

`collectors/sqlserver/Collect-SqlServerConfiguration.ps1` builds a deterministic current-state SQL Server configuration tree. SQL logic remains outside the Python ConfigBackup engine; ConfigBackup only archives or Git-commits the collector's output.

## What it collects

### SQL Server instance

Using dbatools and SMO:

- Server/version/edition/collation/platform metadata.
- `Export-DbaInstance` configuration scripts such as `sp_configure`, logins, linked servers, endpoints, Extended Events, audits, Resource Governor, server roles, Availability Groups and other supported instance categories.
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

If `SSISDB` exists, the collector captures:

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
-TrustServerCertificate
```

`-SkipIspac` is useful for Git-focused history because `.ispac` files are ZIP/binary artifacts and can cause repository growth while providing poor text diffs. The expanded `.dtsx`/project files remain available.

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
