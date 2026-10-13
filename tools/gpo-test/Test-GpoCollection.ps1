#requires -Version 5.1
<#
.SYNOPSIS
Read existing local GPO results twice and write a local repeatability report.
.DESCRIPTION
Requires Python 3.10+. Does not install software, change execution policy,
refresh/change Group Policy, register tasks, contact a reporting service, or
upload results. Run from an elevated PowerShell window for computer results.
#>
[CmdletBinding()]
param(
    [ValidateRange(2, 5)][int]$Runs = 2,
    [ValidateRange(0, 120)][int]$IntervalSeconds = 15,
    [string[]]$TargetUser = @(),
    [string]$OutputDirectory,
    [string]$PythonExe
)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Live GPO collection requires Windows.'
}
$pythonPrefix = @()
if ($PythonExe) {
    $pythonCommand = (Get-Command -Name $PythonExe -CommandType Application -ErrorAction Stop).Source
} else {
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($launcher) {
        $pythonCommand = $launcher.Source
        $pythonPrefix = @('-3')
    } else {
        $python = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
        if (-not $python) {
            throw 'Python 3.10+ is required. Use a compatible Python installation, or pass -PythonExe with its full path. No software has been installed.'
        }
        $pythonCommand = $python.Source
    }
}
$probeArgs = $pythonPrefix + @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)')
& $pythonCommand @probeArgs
if ($LASTEXITCODE -ne 0) { throw 'The selected Python must be version 3.10 or newer.' }
$scriptArgs = $pythonPrefix + @((Join-Path $PSScriptRoot 'test_gpo_collection.py'), '--runs', [string]$Runs, '--interval', [string]$IntervalSeconds)
foreach ($account in $TargetUser) { $scriptArgs += @('--user', $account) }
if ($OutputDirectory) { $scriptArgs += @('--output', $OutputDirectory) }
& $pythonCommand @scriptArgs
$resultCode = $LASTEXITCODE
switch ($resultCode) {
    0 { Write-Host 'PASS: the successfully collected configuration files matched. Review coverage and identity in report.html.' }
    2 { Write-Host 'REVIEW or command-line error: read the final Python message. Changed results have an HTML report and local diffs.' }
    3 { Write-Host 'INCOMPLETE: at least one scope failed, was disabled, or could not be validated. See report.html.' }
    default { Write-Warning "The test stopped with exit code $resultCode. See the preceding message and any report folder printed above." }
}
exit $resultCode
