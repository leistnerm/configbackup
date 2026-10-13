# Executes the actual collector section wrapper with controlled success/failure.
param([Parameter(Mandatory=$true)][string]$OutputDirectory)
$ErrorActionPreference='Stop'
$source=Join-Path $PSScriptRoot '../../collectors/sqlserver/Collect-SqlServerConfiguration.ps1'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path $source),[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
foreach($fn in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]},$false)){
    Invoke-Expression $fn.Extent.Text
}
$script:Utf8NoBom=New-Object System.Text.UTF8Encoding($false)
$script:CollectionSections=@()
$OutputDirectory=[System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -ErrorAction Stop|Out-Null
Invoke-CollectionSection 'databases/first' { Write-Utf8Text (Join-Path $OutputDirectory 'databases/first/schema.sql') 'healthy before' }
Invoke-CollectionSection 'databases/restoring' { throw 'RESTORING' }
Invoke-CollectionSection 'databases/failed-extract' {
    Write-Utf8Text (Join-Path $OutputDirectory 'databases/failed-extract/schema.sql') 'partial output'
    throw 'SqlPackage failed'
}
Invoke-CollectionSection 'databases/last' { Write-Utf8Text (Join-Path $OutputDirectory 'databases/last/schema.sql') 'healthy after' }
Save-CollectionManifest -Finalized $true
$data=Get-Content (Join-Path $OutputDirectory 'collection-manifest.json') -Raw|ConvertFrom-Json
if($data.sections.Count -ne 4){throw 'Wrong section count'}
if(($data.sections |Where-Object status -eq 'complete').Count -ne 2){throw 'Healthy sections not completed'}
if(($data.sections |Where-Object status -eq 'failed').Count -ne 2){throw 'Failure isolation broken'}
foreach($entry in $data.sections|Where-Object status -eq 'failed'){
    if(@($entry.files.PSObject.Properties).Count -ne 0){throw 'Failed section contains eligible files'}
}
Write-Output 'PASS: actual PowerShell section wrapper isolates failures and continues.'
