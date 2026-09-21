# SQL Server collector

`Collect-SqlServerConfiguration.ps1` creates a current-state SQL Server snapshot for ConfigBackup using SqlPackage, dbatools and SMO.

It covers database schema/inventory, instance configuration, SQL Server Agent jobs/schedules/alerts/operators/proxies, and SSISDB projects/packages/environments/parameters. Sensitive SSIS values and dbatools-exported password material are excluded/redacted.

See `../../docs/sql-server-collector.md` for prerequisites, switches, output layout, Git guidance, and YAML examples.

Typical direct test:

```powershell
$env:CONFIGBACKUP_OUTPUT = 'C:\Temp\sql-collector-test'
.\Collect-SqlServerConfiguration.ps1 -SqlInstance SQL01
```

For a Git-focused snapshot, consider `-SkipIspac` to avoid storing binary `.ispac` files while still expanding their `.dtsx`/project contents.
