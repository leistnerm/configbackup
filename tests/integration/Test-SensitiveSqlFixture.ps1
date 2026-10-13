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
        'CREATE TABLE internal.folders(folder_id bigint,name nvarchar(128))',
        'CREATE TABLE internal.projects(project_id bigint,folder_id bigint,name nvarchar(128),object_version_lsn bigint)',
        'CREATE TABLE internal.object_parameters(project_id bigint,project_version_lsn bigint,object_type smallint,object_name nvarchar(128),parameter_name nvarchar(128),parameter_data_type nvarchar(128),description nvarchar(1024),required bit,sensitive bit,value_set bit,value_type char(1),referenced_variable_name nvarchar(128),sensitive_default_value varbinary(max))',
        "CREATE CERTIFICATE MS_Cert_Proj_7 WITH SUBJECT='Synthetic parameter fixture'",
        'CREATE SYMMETRIC KEY fixture_project_key WITH ALGORITHM=AES_256 ENCRYPTION BY CERTIFICATE MS_Cert_Proj_7',
        "INSERT internal.folders VALUES(1,N'fixture'); INSERT internal.projects VALUES(7,1,N'project',100); OPEN SYMMETRIC KEY fixture_project_key DECRYPTION BY CERTIFICATE MS_Cert_Proj_7; INSERT internal.object_parameters VALUES(7,100,20,N'project',N'project-secret',N'String',N'fixture',1,1,1,'V',NULL,ENCRYPTBYKEY(KEY_GUID(N'fixture_project_key'),CONVERT(varbinary(max),N'SYNTHETIC-PROJECT-秘密'))),(7,100,30,N'package.dtsx',N'package-secret',N'String',N'fixture',1,1,1,'V',NULL,ENCRYPTBYKEY(KEY_GUID(N'fixture_project_key'),CONVERT(varbinary(max),N'SYNTHETIC-PACKAGE'))),(7,100,30,N'package.dtsx',N'empty-secret',N'String',N'fixture',0,1,1,'V',NULL,ENCRYPTBYKEY(KEY_GUID(N'fixture_project_key'),CONVERT(varbinary(max),N''))); CLOSE SYMMETRIC KEY fixture_project_key; INSERT internal.object_parameters VALUES(7,100,20,N'project',N'environment-secret',N'String',N'fixture',1,1,1,'R',N'ENV_SECRET',NULL),(7,100,30,N'package.dtsx',N'design-secret',N'String',N'fixture',0,1,0,'V',NULL,NULL),(7,99,20,N'old-version',N'old-secret',N'String',N'fixture',0,1,1,'V',NULL,0x1234),(7,100,20,N'project',N'not-sensitive',N'String',N'fixture',0,0,1,'V',NULL,NULL);",
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
    $projectFile=Join-Path $OutputDirectory 'project.csv';$packageFile=Join-Path $OutputDirectory 'package.csv'
    Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $projectFile -ObjectType 20
    Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $packageFile -ObjectType 30
    $projects=@(Import-Csv $projectFile);$packages=@(Import-Csv $packageFile)
    if($projects.Count -ne 2 -or $packages.Count -ne 3){throw 'Parameter filtering failed'}
    if(($projects | Where-Object parameter_name -eq 'project-secret').value -cne 'SYNTHETIC-PROJECT-秘密'){throw 'Project decryption mismatch'}
    if(($packages | Where-Object parameter_name -eq 'package-secret').value -cne 'SYNTHETIC-PACKAGE'){throw 'Package decryption mismatch'}
    if(($packages | Where-Object parameter_name -eq 'empty-secret').recovery_status -ne 'server_literal'){throw 'Empty literal not recovered'}
    if(($projects | Where-Object parameter_name -eq 'environment-secret').recovery_status -ne 'environment_reference'){throw 'Reference not recorded'}
    if(($packages | Where-Object parameter_name -eq 'design-secret').recovery_status -ne 'design_default_unrecovered'){throw 'Design-only value misrepresented'}
    $projectHash=(Get-FileHash $projectFile).Hash;$packageHash=(Get-FileHash $packageFile).Hash
    Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $projectFile -ObjectType 20
    Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $packageFile -ObjectType 30
    if((Get-FileHash $projectFile).Hash -ne $projectHash -or (Get-FileHash $packageFile).Hash -ne $packageHash){throw 'Parameter repeat differed'}
    Query "UPDATE internal.object_parameters SET sensitive_default_value=0x1234 WHERE parameter_name=N'package-secret'" $database | Out-Null
    $failed=$false
    try{Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $packageFile -ObjectType 30}catch{$failed=$true}
    if(-not $failed -or (Get-FileHash $packageFile).Hash -ne $packageHash){throw 'Corrupt package value replaced previous bytes'}
    Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $projectFile -ObjectType 20
    if((Get-FileHash $projectFile).Hash -ne $projectHash){throw 'Package failure changed project output'}
    Query 'DELETE internal.folders' $database | Out-Null
    $failed=$false
    try{Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $projectFile -ObjectType 20}catch{$failed=$true}
    if(-not $failed -or (Get-FileHash $projectFile).Hash -ne $projectHash){throw 'Incomplete parameter inventory replaced previous bytes'}
    Query "INSERT internal.folders VALUES(1,N'fixture'); DROP SYMMETRIC KEY fixture_project_key" $database | Out-Null
    $failed=$false
    try{Export-SsisSensitiveParameters -ServerObject $serverObject -TargetPath $projectFile -ObjectType 20}catch{$failed=$true}
    if(-not $failed -or (Get-FileHash $projectFile).Hash -ne $projectHash){throw 'Missing project key replaced previous bytes'}
    Query 'DROP SYMMETRIC KEY fixture_env_key' $database | Out-Null
    $failed=$false
    try{Export-SsisSensitiveEnvironment -ServerObject $serverObject -TargetPath $file}catch{$failed=$true}
    if(-not $failed -or (Get-FileHash $file).Hash -ne $first){throw 'Missing-key failure did not preserve prior bytes'}
    @{status='PASS';fixture='Synthetic SQL catalog and certificate/key layout';live_ssis_verified=$false;checks=@('AES decryption roundtrip','repeat determinism','missing key failure preserves bytes','project/package literal recovery','current-version filtering','references and unrecovered design defaults','corrupt package preserves project','incomplete joins preserve bytes')} | ConvertTo-Json | Set-Content (Join-Path $OutputDirectory 'result.json')
    'PASS: disposable cryptographic fixture; live Windows SSIS remains unverified.'
} finally {
    if($created){Query "ALTER DATABASE [$database] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [$database];" | Out-Null}
}
