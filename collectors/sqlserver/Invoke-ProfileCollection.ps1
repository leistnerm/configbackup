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
if($p.include_history){$argsForCollector.IncludeAgentHistory=$true}
& (Join-Path $PSScriptRoot 'Collect-SqlServerConfiguration.ps1') @argsForCollector
exit $LASTEXITCODE
