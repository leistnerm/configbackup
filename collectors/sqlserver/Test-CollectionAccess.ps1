#Requires -Version 5.1
<# Runs only built-in probes/collector in a private diagnostic directory. No grant or backup-engine commands. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ProfileFile,
      [Parameter(Mandatory=$true)][string]$ScratchDirectory)
$ErrorActionPreference='Stop'
$p=Get-Content -LiteralPath $ProfileFile -Raw | ConvertFrom-Json
$report=[ordered]@{status='not_tested'}
try {
    Import-Module dbatools -ErrorAction Stop
    $connect=@{SqlInstance=[string]$p.server;ConnectTimeout=10;ClientName='ConfigBackup.AccessDiagnostic'}
    if($p.connect_timeout){$connect.ConnectTimeout=[int]$p.connect_timeout}
    if($p.trust_server_certificate){$connect.TrustServerCertificate=$true}
    $credential=$null
    if($p.user){
        if(-not $env:CONFIGBACKUP_DIAGNOSTIC_PASSWORD){throw 'Diagnostic credential environment variable unavailable'}
        $credential=[pscredential]::new([string]$p.user,(ConvertTo-SecureString $env:CONFIGBACKUP_DIAGNOSTIC_PASSWORD -AsPlainText -Force))
        $connect.SqlCredential=$credential
    }
    $server=Connect-DbaInstance @connect
    $report=[ordered]@{status='connected'}
    $row=Invoke-DbaQuery -SqlInstance $server -Database master -Query "SELECT ORIGINAL_LOGIN() AS identity_name,IS_SRVROLEMEMBER(N'sysadmin') AS sysadmin,HAS_PERMS_BY_NAME(NULL,NULL,'VIEW ANY DEFINITION') AS view_any_definition,HAS_PERMS_BY_NAME(NULL,NULL,'VIEW ANY DATABASE') AS view_any_database,HAS_PERMS_BY_NAME(NULL,NULL,'VIEW SERVER STATE') AS view_server_state;" -EnableException | Select-Object -First 1
    $report=[ordered]@{status='connected';identity=[string]$row.identity_name;permissions=[ordered]@{sysadmin=([int]$row.sysadmin -eq 1);view_any_definition=([int]$row.view_any_definition -eq 1);view_any_database=([int]$row.view_any_database -eq 1);view_server_state=([int]$row.view_server_state -eq 1)}}
    $server.ConnectionContext.Disconnect()
} catch {
    $reason=[string]$_.Exception.Message
    if($env:CONFIGBACKUP_DIAGNOSTIC_PASSWORD){$reason=$reason.Replace($env:CONFIGBACKUP_DIAGNOSTIC_PASSWORD,'[redacted]')}
    if($report.status -eq 'connected'){$report.permission_error=$reason}
    else{$report=[ordered]@{status='failed';reason=$reason}}
}
$report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $ScratchDirectory 'connection.json') -Encoding UTF8
if($report.status -ne 'connected'){exit 1}
$argsForCollector=@{SqlInstance=[string]$p.server;OutputDirectory=(Join-Path $ScratchDirectory 'snapshot');SkipHostConfiguration=$true;ConnectTimeout=$connect.ConnectTimeout}
if($credential){$argsForCollector.SqlCredential=$credential}
if($p.trust_server_certificate){$argsForCollector.TrustServerCertificate=$true}
if($p.databases){$argsForCollector.Database=@($p.databases)}
if($p.sqlpackage){$argsForCollector.SqlPackagePath=[string]$p.sqlpackage}
if(-not $p.access_profile -or $p.access_profile -eq 'read-only'){$argsForCollector.ReadOnlyAccess=$true}
if($p.PSObject.Properties.Name -contains 'schema' -and -not $p.schema){$argsForCollector.SkipSchema=$true}
if($p.include_health){$argsForCollector.IncludeHealthMetrics=$true}
if($p.include_history){$argsForCollector.IncludeAgentHistory=$true}
& (Join-Path $PSScriptRoot 'Collect-SqlServerConfiguration.ps1') @argsForCollector
exit $LASTEXITCODE
