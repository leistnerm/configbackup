# Real PowerShell execution of bounded transformation and regression cases.
param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../../collectors/sqlserver/Remove-DacFxPlaceholderPasswords.ps1')
$root=[IO.Path]::GetFullPath($OutputDirectory)
if(Test-Path -LiteralPath $root){throw 'Use a fresh test output directory'}
[IO.Directory]::CreateDirectory((Join-Path $root 'Security'))|Out-Null
[IO.Directory]::CreateDirectory((Join-Path $root 'dbo/Stored Procedures'))|Out-Null
$fixtures=[ordered]@{
    'Security/login.sql'="CREATE LOGIN [read]]er]`n    WITH PASSWORD = N'first''random';`n`nGO`n"
    'Security/user.sql'="CREATE USER [contained] WITH PASSWORD = 'random two', DEFAULT_SCHEMA = [dbo];`nGO`n"
    'Security/role.sql'="CREATE ROLE [password_label];`nGO`n"
    'Security/comment.sql'="-- CREATE LOGIN [x] WITH PASSWORD = N'leave alone';`n"
    'dbo/Stored Procedures/test.sql'="CREATE PROCEDURE dbo.test AS SELECT N'PASSWORD=secret_literal';`nGO`n"
}
foreach($name in $fixtures.Keys){[IO.File]::WriteAllText((Join-Path $root $name),$fixtures[$name])}
if((Remove-DacFxPlaceholderPasswords $root) -ne 2){throw 'Expected exactly login and contained-user replacement'}
foreach($name in @('Security/login.sql','Security/user.sql')){
    $text=[IO.File]::ReadAllText((Join-Path $root $name))
    if($text -notmatch 'PASSWORD\s*=\s*<CONFIGBACKUP_PASSWORD_REMOVED>' -or $text -match 'first|random two'){throw 'Generated password was retained or replaced by executable literal'}
}
foreach($name in @('Security/role.sql','Security/comment.sql','dbo/Stored Procedures/test.sql')){
    if([IO.File]::ReadAllText((Join-Path $root $name)) -cne $fixtures[$name]){throw 'Unrelated SQL text changed'}
}
$first=[IO.File]::ReadAllBytes((Join-Path $root 'Security/login.sql'))
if((Remove-DacFxPlaceholderPasswords $root) -ne 0){throw 'Transformation is not idempotent'}
$second=[IO.File]::ReadAllBytes((Join-Path $root 'Security/login.sql'))
if([Convert]::ToBase64String($first) -ne [Convert]::ToBase64String($second)){throw 'Repeated transformation changed bytes'}
$bad=Join-Path $root 'Security/unknown.sql'
[IO.File]::WriteAllText($bad,"CREATE LOGIN [unknown] WITH PASSWORD=N'value', UNSUPPORTED_OPTION=ON;`nGO`n")
$refused=$false
try{Remove-DacFxPlaceholderPasswords $root|Out-Null}catch{$refused=$true}
if(-not $refused){throw 'Unrecognized native login syntax was certified'}
[ordered]@{status='passed';cases=@('login and contained-user placeholders removed','escaped identifiers and literals','non-executable restore marker','unrelated comments/procedures unchanged','byte-stable repeated transformation','unknown native password syntax fails closed')} | ConvertTo-Json
