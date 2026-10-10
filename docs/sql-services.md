# SQL platform coverage and platform limits

Use `Collect-SqlServerConfiguration.ps1` for each Database Engine instance. Use the separate, optional `Collect-SqlServices.ps1` adapter for Reporting Services, Analysis Services, Windows service inventory, explicitly named configuration files, and Windows Failover Clustering. Give every instance/service its own staging directory, task name, and archive destination. Neither collector changes source settings.

## Database Engine

The normal collector records server configuration values, identity/version/features, services, endpoints, linked-server metadata, credential identities (not passwords), logins/roles/permissions, SQL Agent configuration/jobs/steps/schedules, SSIS catalog/package output, and database metadata/schema. Instance script categories have separate manifest scopes; failure of Credentials, for example, does not stop Logins, Agent, or databases. `sp_configure.sql` is generated from read-only catalog queries; the collector does not call dbatools' exporter that temporarily changes `show advanced options`.

Explicit database inventories include:

- Tables, columns, computed/identity definitions, constraints, indexes, index columns, and scoped settings.
- Partition functions and typed boundaries, schemes/filegroup destinations, index placement and partitioning columns, compression per partition; rowstore and columnstore indexes.
- In-Memory OLTP table durability, hash-index bucket counts, memory filegroups, and native module flags. SqlPackage supplies the actual definitions.
- Temporal table/history relationships; RLS policies and filter/block predicates; masking; principals, role membership and grants/denies; column encryption key/provider metadata.
- Full-text catalogs, indexes, indexed columns/languages, stopwords, and registered search properties.
- Transactional replication publication, article, subscription metadata; merge publication/article queries when those catalogs exist. Server replication scripting is a separate, failure-protected component.
- Availability groups, replicas, database membership, listeners/addresses, read-only routing, and the existing availability-group script export. Server metadata distinguishes clustered virtual identity, physical node, and instance.

Tests exercised SQL Server 2022. Newer catalog columns are not silently removed to make older engines appear complete: an unsupported query fails its scope and preserves previous files. Older SQL versions need live validation. Catalogs are documentation, not a complete disaster-recovery backup: private keys, decrypted credentials, database data and external binaries are not included. DacFx support limits still apply.

Full instance certification requires `sysadmin`. Per-database certification requires `dbo` or `sysadmin`; ordinary `VIEW DEFINITION` grants can be overridden on individual objects. Encrypted/unreadable T-SQL modules fail the database when schema collection is requested. These checks deliberately avoid certifying an empty/partially visible schema as a deletion. Explicitly skipping schema preserves earlier schema files.

On the tested macOS ARM/dbatools combination, credential-secret scripting, linked-server scripting, Policy Management and the replication management assembly are unavailable. Their catalog inventories still collect, while unsupported script scopes are failed and preserved. Linux/x64 may have different module coverage; do not treat macOS results as Linux/x64 verification. SSISDB was absent in the test server; known absence is `not_applicable`, preserving older SSIS output. No live SSIS package round-trip, AG failover, Windows cluster, merge replication, TDE recovery, Always Encrypted client round-trip, FILESTREAM or FileTable test was performed.

## Named instances on the same server

Use `-SqlInstance 'SERVER\INSTANCE1'` or an explicit endpoint such as `-SqlInstance 'tcp:server.example,51433'`. Each configured endpoint gets its own task and destination, e.g. `sql/SERVER/INSTANCE1`. The collector does not automatically discover every instance on a machine. Windows SQL Browser, network/firewall rules, TLS and authentication must allow name resolution, or configure the instance's TCP port explicitly.

Report sources can share `host: SERVER` and use distinct `instance: INSTANCE1` / `instance: INSTANCE2`. Job IDs include both; concurrency is grouped by physical host. See `examples/multi-instance-sql.yaml` and `examples/multi-host-schedules.yaml`. Two live Linux SQL containers with different TCP ports were tested; Windows named-instance discovery was not.

## Optional services adapter

The adapter requires PowerShell 7.2+. Invoke it as its own execute task, then archive its output using `collection_manifest: true`. It publishes the same manifest contract and exit 6 for partial collection. Each SSRS item, subscription group, SSAS database, cluster, and named configuration file is isolated. Discovery failures leave previously known objects untouched. Missing discovery items are never assumed deleted.

```powershell
pwsh -NoProfile -File ./collectors/sqlserver/Collect-SqlServices.ps1 `
  -OutputDirectory /fresh/staging/services `
  -ReportServerUri https://reports.example/ReportServer/ReportService2010.asmx
```

SSRS uses only read operations in the ReportService2010 SOAP API: system properties/policies, shared schedules, recursive item discovery, native definitions, item policies, linked-report targets, and standard/data-driven subscription properties. It uses the process identity or an in-memory `-ReportCredential`. HTTPS is required; unauthenticated loopback HTTP is reserved for protocol tests. Report definitions are preserved as returned. Known direct runtime status fields are omitted from schedule/subscription comparison output; embedded queries and nested settings are untouched. Array order and other native XML order remain significant; no arbitrary XML sorting is attempted.

**SSRS validation is limited to a local SOAP protocol fixture**, including repeated bytes and failed-item archive/Git preservation. A live SSRS service, actual authentication, delivery extensions, data-driven subscriptions, custom security extensions and Power BI Report Server were not available. Read permissions must cover the intended catalog and subscriptions. API-hidden credentials, encryption keys, ReportServer data, service installation and configuration files require separate backup. Supply selected local `RSReportServer.config`, web.config or other configuration files through `-ConfigurationFile` when running on their host; files may contain secrets and are subject to the Git scan gate.

```powershell
pwsh -NoProfile -File ./collectors/sqlserver/Collect-SqlServices.ps1 `
  -OutputDirectory C:\FreshStaging\analysis-cluster `
  -AnalysisServer 'SERVER\ANALYSIS' -ClusterName SQLCLUSTER `
  -IncludeLocalServiceInventory `
  -ConfigurationFile 'C:\YourActualSSASPath\Config\msmdsrv.ini'
```

SSAS imports the Microsoft `SqlServer` module and uses read-only `DBSCHEMA_CATALOGS` / `DISCOVER_XML_METADATA` requests per catalog. The output is metadata documentation, **not a verified deployable model backup**. Live tabular/multidimensional servers, all compatibility levels, providers, and name/ID differences remain unverified; failed metadata requests preserve the model. Native `.abf` backups, TOM/TMSL export and model restore testing remain outside this adapter. `msmdsrv.ini` must be explicitly supplied to capture host settings.

WSFC imports `FailoverClusters` on Windows and reads cluster identity/settings, nodes, quorum, networks, resources, resource parameters and possible owners. Windows service inventory records SQL-related service names, startup mode, account and executable path. This is configuration inventory, not a cluster/system-state backup. Both adapters have syntax/failure-path checks only; no live cluster or SSAS validation is claimed.

Sources: [Microsoft SSRS GetItemDefinition](https://learn.microsoft.com/en-us/dotnet/api/reportservice2010.reportingservice2010.getitemdefinition?view=sqlserver-2016), [SSRS API implementation](https://github.com/microsoft/Reporting-Services-LoadTest/blob/master/src/RSAccessor/Implementation/ReportingService2010.cs), [Analysis Services PowerShell](https://learn.microsoft.com/en-us/analysis-services/powershell/analysis-services-powershell-reference?view=sql-analysis-services-2025), [SQL containers and multiple instances](https://learn.microsoft.com/en-us/sql/linux/containers/deploy?view=sql-server-ver16).
