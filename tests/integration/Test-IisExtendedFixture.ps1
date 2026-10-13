[CmdletBinding()]
param([string]$ReaderPath=(Join-Path $PSScriptRoot '../../collectors/system/Read-WindowsComponents.ps1'))
$ErrorActionPreference='Stop';$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $ReaderPath),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors.Message -join '; ') }
foreach ($node in $ast.EndBlock.Statements) { if ($node -is [Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($node.Extent.Text)) } }
$script:Records=New-Object 'System.Collections.Generic.List[object]'
$script:Selection=[pscustomobject]@{};$script:Details=@('iis-extended');$Component='iis'
$script:Calls=New-Object 'System.Collections.Generic.List[object]';$script:Disposed=$false
$script:Element=[pscustomobject]@{ElementTagName='fixture';Attributes=@([pscustomobject]@{Name='enabled';Value=$true});ChildElements=@();Schema=[pscustomobject]@{CollectionSchema=$null}}
$script:Element|Add-Member ScriptMethod GetCollection { @() }
$script:Element|Add-Member ScriptMethod GetChildElement { param($name);$script:Element }
$script:Config=[pscustomobject]@{}
$script:Config|Add-Member ScriptMethod GetSection {
    param($name,$location)
    $script:Calls.Add(@{name=$name;location=$location})
    if ($name -eq 'system.webServer/rewrite/outboundRules') { throw 'Unsupported section fixture' }
    $script:Element
}
$script:Config|Add-Member ScriptMethod GetLocationPaths { @('fixture/nested') }
$script:Manager=[pscustomobject]@{Sites=@([pscustomobject]@{Name='Fixture';Bindings=@();Applications=@([pscustomobject]@{Path='/';ApplicationPoolName='FixturePool'})})}
$script:Manager|Add-Member ScriptMethod GetApplicationHostConfiguration { $script:Config }
$script:Manager|Add-Member ScriptMethod GetWebConfiguration { param($site,$path);$script:Config }
$script:Manager|Add-Member ScriptMethod Dispose { $script:Disposed=$true }
$script:Manager|Add-Member ScriptMethod CommitChanges { throw 'MUTATION MUST NEVER BE CALLED' }
function Add-Type { param($Path) }
function Join-Path { param($Path,$ChildPath);'fixture/Microsoft.Web.Administration.dll' }
function New-Object { param($TypeName);if ($TypeName -eq 'Microsoft.Web.Administration.ServerManager') { return $script:Manager };throw 'Unexpected object construction' }
Read-Iis
if (-not $script:Disposed) { throw 'Manager was not disposed' }
if (-not @($script:Calls|Where-Object name -eq 'webFarms').Count) { throw 'ARR farms not queried' }
if (-not @($script:Calls|Where-Object name -like 'system.ftpServer/*').Count) { throw 'FTP sections not queried' }
if (-not @($script:Calls|Where-Object location -eq 'fixture/nested').Count) { throw 'Configured nested locations not queried' }
if (-not @($script:Records|Where-Object { $_.scope -like '*/extended/applications/*/locations/*' -and $_.status -eq 'complete' }).Count) { throw 'Application-local location settings missing' }
if (-not @($script:Records|Where-Object status -eq 'failed').Count) { throw 'Unsupported section did not fail independently' }
if (-not @($script:Records|Where-Object { $_.scope -like '*/server/effective-*' -and $_.status -eq 'complete' }).Count) { throw 'Healthy base settings did not survive extension failure' }
$script:Details=@();$script:Records.Clear();$script:Calls.Clear();Read-Iis
if (@($script:Calls|Where-Object { $_.name -like '*rewrite*' -or $_.name -eq 'webFarms' -or $_.location }).Count) { throw 'Optional IIS expansion ran without selection' }
@{status='PASS';live_iis=$false;fixture='Actual extended IIS traversal against fake MWA objects, nested locations, extension failure isolation, no mutation, default-off'}|ConvertTo-Json -Compress
