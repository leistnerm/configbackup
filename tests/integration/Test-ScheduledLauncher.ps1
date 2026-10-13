#Requires -Version 5.1
<# Run an already registered DIAGNOSTIC task, correlate a new report and identity.
Does not register tasks, store credentials or change privileges. Windows-only live validation pending. #>
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$TaskName,
      [string]$TaskPath='\',
      [Parameter(Mandatory=$true)][string]$LauncherPath,
      [Parameter(Mandatory=$true)][string]$ReportDirectory,
      [ValidateRange(10,14400)][int]$TimeoutSeconds=900)
$ErrorActionPreference='Stop'
if($env:OS -ne 'Windows_NT'){throw 'Windows Task Scheduler is required'}
$launcher=(Resolve-Path -LiteralPath $LauncherPath).Path
$source=Get-Content -LiteralPath $launcher -Raw
if($source -notmatch "CONFIGBACKUP_LAUNCH_MODE\s*=\s*'diagnostic'"){throw 'Select a generated diagnostic launcher, not a backup launcher'}
$task=Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
if(@($task.Actions).Count -ne 1){throw 'A lab diagnostic task must have exactly one action'}
$action=$task.Actions[0]
if([IO.Path]::GetFileNameWithoutExtension($action.Execute) -notin @('pwsh','powershell')){throw 'Expected a PowerShell task action'}
$expectedArguments='^\s*(?:-NoLogo\s+)?-NoProfile\s+-NonInteractive\s+-File\s+"'+[regex]::Escape($launcher)+'"\s*$'
if($action.Arguments -notmatch $expectedArguments){throw 'Expected only -NoProfile -NonInteractive -File followed by the quoted selected launcher path; extra actions/arguments are refused'}
if($task.State -eq 'Running'){throw 'Task is already running; wait for it to finish'}
$expected=$task.Principal.UserId
if(-not $expected){throw 'Task has no explicit user identity'}
$expectedSid=if($expected -match '^S-1-'){ $expected } else { ([Security.Principal.NTAccount]::new($expected)).Translate([Security.Principal.SecurityIdentifier]).Value }
$before=@(Get-ChildItem -LiteralPath $ReportDirectory -Filter 'readiness-*.json' -ErrorAction Stop | ForEach-Object FullName)
$started=Get-Date
Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
$deadline=$started.AddSeconds($TimeoutSeconds)
$reports=@()
do {
    Start-Sleep -Seconds 2
    $reports=@(Get-ChildItem -LiteralPath $ReportDirectory -Filter 'readiness-*.json' | Where-Object { $_.FullName -notin $before -and $_.LastWriteTime -ge $started })
    $current=Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
    $info=Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
    if($reports.Count -gt 0 -and $current.State -ne 'Running' -and $info.LastRunTime -ge $started.AddSeconds(-2)){break}
} while((Get-Date) -lt $deadline)
if($reports.Count -ne 1 -or $current.State -eq 'Running'){throw 'No unique completed report correlated; inspect task history. The helper does not stop the task.'}
$report=Get-Content -LiteralPath $reports[0].FullName -Raw | ConvertFrom-Json
if($report.execution.sid -ne $expectedSid){throw 'Report OS identity does not match the registered scheduled account'}
if($report.execution.launcher_mode -ne 'diagnostic'){throw 'Unexpected launcher mode'}
if($info.LastTaskResult -notin @(0,1,6)){throw "Task failed before a recognized diagnostic result: $($info.LastTaskResult)"}
[ordered]@{task=$TaskName;task_path=$TaskPath;scheduled_account=$expected;sid=$expectedSid;
    task_result=$info.LastTaskResult;report=$reports[0].FullName;readiness=$report.readiness;
    identity_and_run_correlated=$true;note='Readiness failures still require fixes. Correlation is valid only with an isolated report directory and one scheduled run.'} | ConvertTo-Json
if($report.readiness -eq 'not_ready'){exit 1}
if($report.readiness -eq 'ready_with_warnings'){exit 6}
exit 0
