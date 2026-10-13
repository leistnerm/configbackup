# Execute the actual SQL collector's scope finalization without connecting to SQL.
[CmdletBinding()]
param([string]$OutputFile)
$ErrorActionPreference='Stop'
$path=Join-Path $PSScriptRoot '../../collectors/sqlserver/Collect-SqlServerConfiguration.ps1'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $path),[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Collector parse failure'}
$names=@('Test-SectionEnabled','Invoke-DatabaseDetail','Expand-DatabaseScopes','Get-OperationalCatalogQueries')
foreach($fn in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    if($fn.Name -in $names){Invoke-Expression $fn.Extent.Text}
}
function Write-CollectorError([string]$Message) { }
function Assert($Condition,[string]$Message){if(-not $Condition){throw $Message}}
$original=$env:CONFIGBACKUP_SECTIONS
try {
    $env:CONFIGBACKUP_SECTIONS='{}';$SkipSchema=$false;$SkipInventory=$false
    $script:DatabaseDetailScopes=@{}
    Invoke-DatabaseDetail 'databases/demo' 'database.json' { }
    Invoke-DatabaseDetail 'databases/demo' 'catalog/good.csv' { }
    Invoke-DatabaseDetail 'databases/demo' 'catalog/denied.csv' { throw 'fixture denied' }
    $entry=[ordered]@{path='databases/demo';status='complete';error='';files=[ordered]@{'databases/demo/schema/table.sql'='schemahash';'databases/demo/database.json'='identityhash';'databases/demo/catalog/good.csv'='goodhash';'databases/demo/catalog/denied.csv'='partialhash'}}
    $normal=@(Expand-DatabaseScopes @($entry))
    Assert (@($normal | Where-Object path -eq 'databases/demo').Count -eq 0) 'Broad database scope was retained'
    Assert (($normal | Where-Object path -eq 'databases/demo/catalog/denied.csv').status -eq 'failed') 'Failed child certified'
    Assert (($normal | Where-Object path -eq 'databases/demo/catalog/good.csv').files.Count -eq 1) 'Healthy child omitted'
    $SkipSchema=$true;$disabled=@(Expand-DatabaseScopes @($entry))
    Assert (($disabled | Where-Object path -eq 'databases/demo/schema').status -eq 'disabled') 'SkipSchema failed to preserve'
    $SkipSchema=$false;$env:CONFIGBACKUP_SECTIONS='{"schema":false}'
    $disabled=@(Expand-DatabaseScopes @($entry))
    Assert (($disabled | Where-Object path -eq 'databases/demo/schema').status -eq 'disabled') 'Schema switch failed to preserve'
    $env:CONFIGBACKUP_SECTIONS='{"databases/demo/schema":false}'
    Assert ((@(Expand-DatabaseScopes @($entry)) | Where-Object path -eq 'databases/demo/schema').status -eq 'disabled') 'Exact schema switch failed'
    $env:CONFIGBACKUP_SECTIONS='{"databases/demo/catalog/denied.csv":false}'
    Invoke-DatabaseDetail 'databases/demo' 'catalog/denied.csv' { throw 'Must not execute' }
    Assert ($script:DatabaseDetailScopes['databases/demo/catalog/denied.csv'].status -eq 'disabled') 'Disabled query executed'
    $entry.status='failed';$entry.files=@{}
    $failed=@(Expand-DatabaseScopes @($entry))
    Assert ($failed.Count -eq 1 -and $failed[0].path -eq 'databases/demo' -and $failed[0].status -eq 'failed') 'Schema failure must preserve whole database'
    $SkipInventory=$true;$SkipSchema=$true;$script:DatabaseDetailScopes=@{};$entry.status='complete'
    $both=@(Expand-DatabaseScopes @($entry))
    Assert (@($both | Where-Object status -ne 'disabled').Count -eq 0) 'Skipped groups were certified'
    $queries=Get-OperationalCatalogQueries
    Assert ($queries.Count -ge 15) 'Operational inventory queries missing'
    foreach($query in $queries.Values){Assert ($query -notmatch '(?im)^\s*(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|EXEC)\b') 'Unexpected mutation query'}
    $result=@{status='PASS';live_sql=$false;normal=$normal;disabled=$disabled;failed=$failed}
    if($OutputFile){$result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $OutputFile -Encoding utf8}
    'PASS: actual SQL finalization preserves disabled/failed siblings, and schema failure is atomic.'
} finally {$env:CONFIGBACKUP_SECTIONS=$original}
