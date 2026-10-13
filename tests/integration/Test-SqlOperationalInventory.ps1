# Disposable database only. Metadata query validation; no production settings changed.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Server,
      [Parameter(Mandatory=$true)][string]$ModulePath,
      [Parameter(Mandatory=$true)][string]$OutputDirectory,
      [string]$User='sa', [switch]$ConfirmDisposableServer)
$ErrorActionPreference='Stop'
$script:Utf8NoBom=[Text.UTF8Encoding]::new($false)
if(-not $ConfirmDisposableServer){throw 'Explicit disposable-server confirmation required'}
if(-not $env:CONFIGBACKUP_TEST_SQL_PASSWORD){throw 'Test credential environment variable required'}
if(Test-Path -LiteralPath $OutputDirectory){throw 'Use a fresh test output directory'}
$null=New-Item -ItemType Directory -Path $OutputDirectory
$env:PSModulePath=$ModulePath+[IO.Path]::PathSeparator+$env:PSModulePath
Import-Module dbatools -ErrorAction Stop
$credential=[pscredential]::new($User,(ConvertTo-SecureString $env:CONFIGBACKUP_TEST_SQL_PASSWORD -AsPlainText -Force))
$connection=Connect-DbaInstance -SqlInstance $Server -SqlCredential $credential -TrustServerCertificate
$collector=Join-Path $PSScriptRoot '../../collectors/sqlserver/Collect-SqlServerConfiguration.ps1'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $collector),[ref]$tokens,[ref]$errors)
foreach($fn in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
 if($fn.Name -in @('Get-OperationalCatalogQueries','Write-StableCsv','Write-Utf8Text','Convert-DataTableRows','Convert-ToStableString')){Invoke-Expression $fn.Extent.Text}
}
$name='cbtest_ops_'+[guid]::NewGuid().ToString('N').Substring(0,10)
$created=$false;$cdc=$false
function Query([string]$Database,[string]$Sql){Invoke-DbaQuery -SqlInstance $connection -Database $Database -Query $Sql -EnableException | Out-Null}
try {
 Query 'master' ('CREATE DATABASE ['+$name+']');$created=$true
 Query $name @'
CREATE TABLE dbo.source_table(id int NOT NULL PRIMARY KEY, value nvarchar(50));
CREATE MESSAGE TYPE [ConfigBackupMessage] VALIDATION=NONE;
CREATE CONTRACT [ConfigBackupContract] ([ConfigBackupMessage] SENT BY INITIATOR);
CREATE QUEUE dbo.ConfigBackupQueue;
CREATE SERVICE [ConfigBackupService] ON QUEUE dbo.ConfigBackupQueue ([ConfigBackupContract]);
CREATE ROUTE [ConfigBackupRoute] WITH SERVICE_NAME=N'ConfigBackupService', ADDRESS=N'LOCAL';
CREATE CERTIFICATE [ConfigBackupCertificate] ENCRYPTION BY PASSWORD='SYNTHETIC_fixture_password_1!' WITH SUBJECT='Disposable ConfigBackup test certificate', EXPIRY_DATE='2035-01-01';
EXEC sys.sp_cdc_enable_db;
EXEC sys.sp_cdc_enable_table @source_schema=N'dbo',@source_name=N'source_table',@role_name=NULL,@supports_net_changes=1;
'@
 $cdc=$true;$queries=Get-OperationalCatalogQueries;$failures=@();$hashes=@{}
 foreach($pass in @('first','second')) {
  $target=Join-Path $OutputDirectory $pass;$null=New-Item -ItemType Directory -Path $target
  foreach($key in @($queries.Keys|Sort-Object)) {
   try {
    $result=Invoke-DbaQuery -SqlInstance $connection -Database $name -Query $queries[$key] -As DataSet -EnableException
    $rows=if($result -and $result.Tables.Count){@(Convert-DataTableRows $result.Tables[0])}else{@()}
    $file=Join-Path $target ($key+'.csv');Write-StableCsv -Path $file -Rows $rows
    if($pass -eq 'first'){$hashes[$key]=(Get-FileHash $file).Hash}
    elseif((Get-FileHash $file).Hash -ne $hashes[$key]){throw 'Repeated configuration changed'}
   } catch {$failures+=@{pass=$pass;query=$key;error=$_.Exception.Message}}
  }
 }
 foreach($key in @('broker-queues','broker-services','broker-contracts','cdc-capture-instances','cdc-captured-columns','certificates')) {
  $file=Join-Path $OutputDirectory ('first/'+$key+'.csv')
  if(-not (Test-Path $file) -or (Get-Item $file).Length -eq 0){$failures+=@{query=$key;error='Expected nonempty fixture inventory'}}
 }
 $fullTarget=Join-Path $OutputDirectory 'collector'
 & $collector -SqlInstance $Server -SqlCredential $credential -Database @($name) -OutputDirectory $fullTarget -TrustServerCertificate -SkipSchema -SkipInstanceExport -SkipAgent -SkipSsis -SkipHostConfiguration -IncludeOperationalInventory
 if($LASTEXITCODE -ne 0){$failures+=@{query='collector';error='Full collector reported partial/failure'}}
 $manifest=Get-Content (Join-Path $fullTarget 'collection-manifest.json') -Raw|ConvertFrom-Json
 $operational=@($manifest.sections|Where-Object {$_.path -like ('databases/'+$name+'/operational/*')})
 if($operational.Count -ne $queries.Count -or @($operational|Where-Object status -ne 'complete').Count){$failures+=@{query='manifest';error='Operational scopes not certified independently'}}
 $report=@{status=if($failures.Count){'FAILED'}else{'PASS'};queries=$queries.Count;failures=$failures;live_sql=$true;full_collector_operational_scopes=$operational.Count;cleanup='pending'}
} finally {
 if($created){
  Query $name 'IF (SELECT is_cdc_enabled FROM sys.databases WHERE database_id=DB_ID())=1 EXEC sys.sp_cdc_disable_db;'
  Query 'master' ('ALTER DATABASE ['+$name+'] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE ['+$name+'];')
 }
}
$report.cleanup='PASS';$report|ConvertTo-Json -Depth 10|Set-Content (Join-Path $OutputDirectory 'report.json') -Encoding utf8
if($report.status -ne 'PASS'){throw 'Operational query tests failed; review local report'}
'PASS: live SQL operational inventory, repeated captures and cleanup'
