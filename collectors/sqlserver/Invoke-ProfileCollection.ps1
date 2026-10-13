#Requires -Version 5.1
<# Shared parameter mapping for normal collection and disposable diagnostics. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProfileFile,
      [Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
$p=Get-Content -LiteralPath $ProfileFile -Raw | ConvertFrom-Json
$argsForCollector=@{SqlInstance=[string]$p.server;OutputDirectory=$OutputDirectory;SkipHostConfiguration=$true;ConnectTimeout=10}
if($p.connect_timeout){$argsForCollector.ConnectTimeout=[int]$p.connect_timeout}
if($p.user){
    if(-not $env:CONFIGBACKUP_DIAGNOSTIC_PASSWORD){throw 'Credential environment variable unavailable'}
    $argsForCollector.SqlCredential=[pscredential]::new([string]$p.user,(ConvertTo-SecureString $env:CONFIGBACKUP_DIAGNOSTIC_PASSWORD -AsPlainText -Force))
}
if($p.trust_server_certificate){$argsForCollector.TrustServerCertificate=$true}
if($p.databases){$argsForCollector.Database=@($p.databases)}
if($p.sqlpackage){$argsForCollector.SqlPackagePath=[string]$p.sqlpackage}
if(-not $p.access_profile -or $p.access_profile -eq 'read-only'){$argsForCollector.ReadOnlyAccess=$true}
if($p.PSObject.Properties.Name -contains 'schema' -and -not $p.schema){$argsForCollector.SkipSchema=$true}
if($p.include_health){$argsForCollector.IncludeHealthMetrics=$true}
if($p.include_operational_inventory){$argsForCollector.IncludeOperationalInventory=$true}
if($p.include_history){$argsForCollector.IncludeAgentHistory=$true}
if($p.PSObject.Properties.Name -contains 'include_system_databases'){$argsForCollector.IncludeSystemDatabases=$p.include_system_databases}
if($p.PSObject.Properties.Name -contains 'include_tempdb'){$argsForCollector.IncludeTempdb=$p.include_tempdb}
if($p.PSObject.Properties.Name -contains 'verify_schema_extraction'){$argsForCollector.VerifySchemaExtraction=$p.verify_schema_extraction}
if($p.PSObject.Properties.Name -contains 'include_capacity'){$argsForCollector.IncludeCapacityMetrics=$p.include_capacity}
if($p.PSObject.Properties.Name -contains 'include_performance'){$argsForCollector.IncludePerformanceMetrics=$p.include_performance}
if($p.PSObject.Properties.Name -contains 'include_index_health'){$argsForCollector.IncludeIndexHealth=$p.include_index_health}
if($p.PSObject.Properties.Name -contains 'skip_inventory'){$argsForCollector.SkipInventory=$p.skip_inventory}
if($p.PSObject.Properties.Name -contains 'skip_agent'){$argsForCollector.SkipAgent=$p.skip_agent}
if($p.PSObject.Properties.Name -contains 'skip_ssis'){$argsForCollector.SkipSsis=$p.skip_ssis}
if($p.PSObject.Properties.Name -contains 'skip_ispac'){$argsForCollector.SkipIspac=$p.skip_ispac}
if($p.PSObject.Properties.Name -contains 'include_legacy_ssis'){$argsForCollector.IncludeLegacySsis=$p.include_legacy_ssis}
if($p.PSObject.Properties.Name -contains 'health_query_timeout'){$argsForCollector.HealthQueryTimeout=$p.health_query_timeout}
if($p.PSObject.Properties.Name -contains 'health_index_limit'){$argsForCollector.HealthIndexLimit=$p.health_index_limit}
if($p.PSObject.Properties.Name -contains 'agent_history_days'){$argsForCollector.AgentHistoryDays=$p.agent_history_days}
if($p.PSObject.Properties.Name -contains 'exclude_databases'){$argsForCollector.ExcludeDatabase=$p.exclude_databases}
if($p.PSObject.Properties.Name -contains 'instance_exclude'){$argsForCollector.InstanceExclude=$p.instance_exclude}
if($p.PSObject.Properties.Name -contains 'append_connection_string'){$argsForCollector.AppendConnectionString=$p.append_connection_string}
if($p.collect_local_host_configuration){$argsForCollector.SkipHostConfiguration=$false;$argsForCollector.CollectLocalHostConfiguration=$true}
& (Join-Path $PSScriptRoot 'Collect-SqlServerConfiguration.ps1') @argsForCollector
exit $LASTEXITCODE
