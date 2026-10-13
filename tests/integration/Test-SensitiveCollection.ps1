# Synthetic checks of the actual sensitive export and collection-scope functions.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../../collectors/sqlserver/SensitiveCollection.ps1')
$source=Join-Path $PSScriptRoot '../../collectors/sqlserver/Collect-SqlServerConfiguration.ps1'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $source),[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Collector parse failure'}
$exportFunction=$ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Export-InstanceConfiguration'},$true)[0]
if('IncludeSensitive' -notin @($exportFunction.Body.ParamBlock.Parameters | ForEach-Object {$_.Name.VariablePath.UserPath})){throw 'Instance export missing sensitive parameter'}
$names=@('Test-SectionEnabled','Invoke-CollectionSection','Write-StableCsv','Write-Utf8Text','Convert-DataTableRows','Convert-ToStableString')
foreach($fn in $ast.FindAll({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) {
    if($fn.Name -in $names){Invoke-Expression $fn.Extent.Text}
}
function Save-CollectionManifest { }
function Write-CollectorError([string]$Message) { if($Message -match 'SECRET-SENTINEL'){throw 'Secret leaked to log'} }
function Get-SsisFolderIdentifierColumn { return 'folder_id' }
function Assert($Condition,[string]$Message){if(-not $Condition){throw $Message}}
function Invoke-QueryTable {
    param($ServerObject,[string]$DatabaseName,[string]$Query)
    Assert ($Query -notmatch '(?im)^\s*(UPDATE|INSERT|DELETE|ALTER|CREATE|DROP|EXEC|GRANT)\b') 'Mutation query'
    $table=New-Object System.Data.DataTable
    if($Query -match 'AS compatible') {
        $null=$table.Columns.Add('compatible',[int]);$null=$table.Rows.Add([int]$script:Compatible)
    } elseif($Query -match 'AS expected_count') {
        $null=$table.Columns.Add('expected_count',[long]);$null=$table.Rows.Add([long]$script:ExpectedCount)
    } else {
        if($Query -match 'internal.object_parameters') {
            $null=$table.Columns.Add('recovery_status',[string]);$null=$table.Columns.Add('referenced_variable_name',[string])
            $null=$table.Columns.Add('value',[string]);$row=$table.NewRow();$row['recovery_status']='server_literal';$row['value']=if($Query -match 'a.object_type=20' -and $script:FailProject){[DBNull]::Value}else{'SECRET-SENTINEL'};$table.Rows.Add($row)
            return ,$table
        }
        $null=$table.Columns.Add('value',[string]);$null=$table.Rows.Add($(if($script:Decrypt){'SECRET-SENTINEL'}else{[DBNull]::Value}))
    }
    return ,$table
}
$old=$env:CONFIGBACKUP_SENSITIVE_SECTIONS;$oldSections=$env:CONFIGBACKUP_SECTIONS
$OutputDirectory=Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString('N'))
$null=[IO.Directory]::CreateDirectory($OutputDirectory)
try {
    $script:Utf8NoBom=New-Object System.Text.UTF8Encoding($false)
    $env:CONFIGBACKUP_SECTIONS='{}';$env:CONFIGBACKUP_SENSITIVE_SECTIONS='[]'
    Assert (-not (Test-SensitiveSection 'instance/scripts/Logins')) 'Default enabled sensitive capture'
    $env:CONFIGBACKUP_SENSITIVE_SECTIONS='["instance/sensitive-ssis/environment-variables.csv"]'
    Assert (-not (Test-SensitiveSection 'instance/ssis')) 'Parent inherited sensitive approval'
    $script:RequireServerVisibility=$false;$script:CollectionSections=@();$script:Compatible=$false;$script:Decrypt=$false
    $scope='instance/sensitive-ssis/environment-variables.csv'
    $action={Export-SsisSensitiveEnvironment -ServerObject $null -TargetPath (Join-Path $OutputDirectory $scope)}
    Invoke-CollectionSection 'instance/ssis' { $null=[IO.Directory]::CreateDirectory((Join-Path $OutputDirectory 'instance/ssis'));[IO.File]::WriteAllText((Join-Path $OutputDirectory 'instance/ssis/normal.txt'),'normal') }
    Invoke-CollectionSection $scope $action
    Assert (($script:CollectionSections | Where-Object path -eq $scope).status -eq 'failed') 'Denied recovery certified'
    Assert (($script:CollectionSections | Where-Object path -eq 'instance/ssis').status -eq 'complete') 'Normal SSIS failed with sensitive recovery'
    $script:Compatible=$true;$script:ExpectedCount=1;$script:CollectionSections=@()
    Invoke-CollectionSection $scope $action
    Assert ($script:CollectionSections[0].status -eq 'failed') 'NULL decryption certified'
    Assert (-not (Test-Path (Join-Path $OutputDirectory $scope))) 'Partial sensitive output written'
    $script:Decrypt=$true;$script:CollectionSections=@()
    Invoke-CollectionSection $scope $action
    Assert ($script:CollectionSections[0].status -eq 'complete') 'Synthetic compatible recovery failed'
    Assert ($script:CollectionSections[0].sensitive -eq $true) 'Sensitive classification missing'
    $first=(Get-FileHash (Join-Path $OutputDirectory $scope)).Hash
    Invoke-CollectionSection $scope $action
    Assert ((Get-FileHash (Join-Path $OutputDirectory $scope)).Hash -eq $first) 'Recovery output not deterministic'
    $script:ExpectedCount=2;$script:CollectionSections=@()
    Invoke-CollectionSection $scope $action
    Assert ($script:CollectionSections[0].status -eq 'failed') 'Incomplete joined inventory certified'
    Assert ((Get-FileHash (Join-Path $OutputDirectory $scope)).Hash -eq $first) 'Incomplete inventory changed previous bytes'
    $projectScope='instance/sensitive-ssis/project-parameters.csv';$packageScope='instance/sensitive-ssis/package-parameters.csv'
    $env:CONFIGBACKUP_SENSITIVE_SECTIONS='[]';$script:CollectionSections=@()
    Invoke-SsisSensitiveCollections -ServerObject $null -OutputDirectory $OutputDirectory
    Assert ($script:CollectionSections.Count -eq 0) 'Recovery ran without approval'
    $env:CONFIGBACKUP_SENSITIVE_SECTIONS=ConvertTo-Json -Compress -InputObject @($projectScope,$packageScope,$scope)
    $script:ExpectedCount=1;$script:FailProject=$true;$script:CollectionSections=@()
    Invoke-SsisSensitiveCollections -ServerObject $null -OutputDirectory $OutputDirectory
    Assert (($script:CollectionSections | Where-Object path -eq $projectScope).status -eq 'failed') 'Failed project certified'
    Assert (($script:CollectionSections | Where-Object path -eq $packageScope).status -eq 'complete') 'Project failure stopped package recovery'
    Assert (($script:CollectionSections | Where-Object path -eq $scope).status -eq 'complete') 'Project failure stopped environment recovery'
    $env:CONFIGBACKUP_SENSITIVE_SECTIONS=ConvertTo-Json -Compress -InputObject @($packageScope)
    $script:CollectionSections=@();Invoke-SsisSensitiveCollections -ServerObject $null -OutputDirectory $OutputDirectory
    Assert ($script:CollectionSections.Count -eq 1 -and $script:CollectionSections[0].path -eq $packageScope) 'Approval inherited by sibling'
    $script:CollectionSections=@();Invoke-SsisSensitiveCollections -ServerObject $null -OutputDirectory $OutputDirectory -SkipSsis $true
    Assert ($script:CollectionSections[0].status -eq 'disabled') 'SkipSsis did not apply to parameter recovery'
    $env:CONFIGBACKUP_SECTIONS=ConvertTo-Json -Compress -InputObject @{$packageScope=$false}
    $script:CollectionSections=@();Invoke-SsisSensitiveCollections -ServerObject $null -OutputDirectory $OutputDirectory
    Assert ($script:CollectionSections[0].status -eq 'disabled') 'Section disable did not apply'
    'PASS: explicit consent, read-only queries, denied/null recovery isolation, normal SSIS continuity and deterministic synthetic output.'
} finally {
    $env:CONFIGBACKUP_SENSITIVE_SECTIONS=$old;$env:CONFIGBACKUP_SECTIONS=$oldSections
    Remove-Item -LiteralPath $OutputDirectory -Recurse -Force
}
