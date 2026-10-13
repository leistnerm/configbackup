# Disposable SQL cryptographic fixture, not a live SSIS compatibility test.
[CmdletBinding()]
param([string]$Server='127.0.0.1,51439', [string]$User='sa',
      [string]$PasswordEnvironment='CONFIGBACKUP_TEST_SQL_PASSWORD',
      [Parameter(Mandatory=$true)][string]$OutputDirectory,
      [Parameter(Mandatory=$true)][switch]$ConfirmDisposable)
$ErrorActionPreference='Stop'
if(-not $ConfirmDisposable){throw 'Disposable database consent required'}
$secret=[Environment]::GetEnvironmentVariable($PasswordEnvironment)
if(-not $secret){throw 'Test credential environment variable unavailable'}
Import-Module dbatools -ErrorAction Stop
$serverObject=Connect-DbaInstance -SqlInstance $Server -SqlCredential ([pscredential]::new($User,(ConvertTo-SecureString $secret -AsPlainText -Force))) -TrustServerCertificate
$secret=$null
$database='cbtest_sensitive_'+[guid]::NewGuid().ToString('N').Substring(0,12)
function Query([string]$Sql,[string]$Database='master') {
    Invoke-DbaQuery -SqlInstance $serverObject -Database $Database -Query $Sql -EnableException -As DataTable
}
. (Join-Path $PSScriptRoot '../../collectors/sqlserver/SensitiveCollection.ps1')
$source=Join-Path $PSScriptRoot '../../collectors/sqlserver/Collect-SqlServerConfiguration.ps1'
$tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $source),[ref]$tokens,[ref]$errors)
foreach($fn in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    if($fn.Name -in @('Write-StableCsv','Write-Utf8Text','Convert-DataTableRows','Convert-ToStableString')){Invoke-Expression $fn.Extent.Text}
}
function Invoke-QueryTable { param($ServerObject,[string]$DatabaseName,[string]$Query)
    # Only the connection target is substituted; the recovery queries are unchanged.
    $result=Query $Query $script:database
    return ,$result
}
function Get-SsisFolderIdentifierColumn { return 'folder_id' }
$script:Utf8NoBom=New-Object System.Text.UTF8Encoding($false)
$created=$false
try {
    Query "CREATE DATABASE [$database]" | Out-Null;$created=$true
    foreach($sql in @(
        'CREATE SCHEMA internal', 'CREATE SCHEMA catalog',
        "CREATE MASTER KEY ENCRYPTION BY PASSWORD = 'SYNTHETIC-TEST-KEY-Only!42'",
        "CREATE CERTIFICATE MS_Cert_Env_1 WITH SUBJECT='Synthetic environment fixture'",
        'CREATE SYMMETRIC KEY fixture_env_key WITH ALGORITHM=AES_256 ENCRYPTION BY CERTIFICATE MS_Cert_Env_1',
        'CREATE TABLE catalog.folders(folder_id bigint,name nvarchar(128))',
        'CREATE TABLE catalog.environments(environment_id bigint,folder_id bigint,name nvarchar(128))',
        'CREATE TABLE internal.environment_variables(environment_id bigint,name nvarchar(128),description nvarchar(1024),type nvarchar(128),sensitive bit,sensitive_value varbinary(max))',
        'CREATE FUNCTION internal.get_value_by_data_type(@value varbinary(max),@type nvarchar(128)) RETURNS sql_variant AS BEGIN RETURN CONVERT(nvarchar(4000),@value); END',
        "INSERT catalog.folders VALUES(1,N'fixture'); INSERT catalog.environments VALUES(1,1,N'fixture'); OPEN SYMMETRIC KEY fixture_env_key DECRYPTION BY CERTIFICATE MS_Cert_Env_1; INSERT internal.environment_variables VALUES(1,N'password',N'synthetic only',N'String',1,ENCRYPTBYKEY(KEY_GUID(N'fixture_env_key'),CONVERT(varbinary(max),N'SYNTHETIC-DECRYPT-ROUNDTRIP'))); CLOSE SYMMETRIC KEY fixture_env_key;"
    )) {Query $sql $database | Out-Null}
    if(Test-Path -LiteralPath $OutputDirectory){throw 'Choose a new output folder'}
    $null=[IO.Directory]::CreateDirectory($OutputDirectory)
    $file=Join-Path $OutputDirectory 'environment.csv'
    Export-SsisSensitiveEnvironment -ServerObject $serverObject -TargetPath $file
    if((Get-Content $file -Raw) -notmatch 'SYNTHETIC-DECRYPT-ROUNDTRIP'){throw 'Decryption mismatch'}
    $first=(Get-FileHash $file).Hash
    Export-SsisSensitiveEnvironment -ServerObject $serverObject -TargetPath $file
    if((Get-FileHash $file).Hash -ne $first){throw 'Repeated output differed'}
    Query 'DROP SYMMETRIC KEY fixture_env_key' $database | Out-Null
    $failed=$false
    try{Export-SsisSensitiveEnvironment -ServerObject $serverObject -TargetPath $file}catch{$failed=$true}
    if(-not $failed -or (Get-FileHash $file).Hash -ne $first){throw 'Missing-key failure did not preserve prior bytes'}
    @{status='PASS';fixture='Synthetic SQL catalog and certificate/key layout';live_ssis_verified=$false;checks=@('AES decryption roundtrip','repeat determinism','missing key failure preserves bytes')} | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory 'result.json')
    'PASS: disposable cryptographic fixture; live Windows SSIS remains unverified.'
} finally {
    if($created){Query "ALTER DATABASE [$database] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [$database];" | Out-Null}
}
