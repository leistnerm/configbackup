#Requires -Version 5.1
<#
.SYNOPSIS
    Collects SQL Server instance configuration, database inventory, and per-object schema files.

.DESCRIPTION
    Designed to run as a ConfigBackup "execute" collector. The script uses dbatools for
    SQL Server discovery/instance scripting and SqlPackage for deterministic per-object
    schema extraction (SchemaObjectType). It writes a current-state tree; ConfigBackup
    is responsible for change detection, versioning, deletion tracking, and retention.

    By default the current process identity is used for SQL authentication (Windows/
    integrated authentication where supported). Password material is excluded from
    dbatools instance scripts by default.

.NOTES
    Collector version: 1.3.1
    Requires:
      - PowerShell 5.1+ (PowerShell 7+ recommended)
      - dbatools PowerShell module
      - Microsoft SqlPackage CLI (unless -SkipSchema is used)
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SqlInstance,

    [string]$OutputDirectory = $env:CONFIGBACKUP_OUTPUT,

    # Comma/semicolon-delimited values are accepted, which is convenient when the
    # collector is launched by an external process/YAML task.
    [string[]]$Database = @(),
    [string[]]$ExcludeDatabase = @(),

    [switch]$IncludeSystemDatabases,
    [switch]$IncludeTempdb,

    [string]$SqlPackagePath = 'sqlpackage',

    [switch]$SkipSchema,
    [switch]$SkipInstanceExport,
    [switch]$SkipInventory,
    [switch]$SkipAgent,
    [switch]$SkipSsis,
    [switch]$SkipIspac,
    [switch]$IncludeLegacySsis,

    # Additional Export-DbaInstance categories to skip. "Databases" is always
    # excluded because this collector inventories/extracts databases separately.
    [string[]]$InstanceExclude = @(),

    [switch]$TrustServerCertificate,

    [ValidateRange(1, 600)]
    [int]$ConnectTimeout = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$CollectorVersion = '1.3.1'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-CollectorMessage {
    param([string]$Message)
    [Console]::Out.WriteLine("[sql-collector] $Message")
}

function Write-CollectorError {
    param([string]$Message)
    [Console]::Error.WriteLine("[sql-collector] ERROR: $Message")
}

function Write-Utf8Text {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$Text
    )
    $parent = Split-Path -Parent $Path
    if ($parent) {
        [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    }
    [System.IO.File]::WriteAllText($Path, $Text, $script:Utf8NoBom)
}

function Write-StableJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value,
        [int]$Depth = 8
    )
    $json = $Value | ConvertTo-Json -Depth $Depth
    Write-Utf8Text -Path $Path -Text ($json + "`n")
}

function Write-StableCsv {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyCollection()][object[]]$Rows
    )
    $rowsArray = @($Rows)
    if ($rowsArray.Count -eq 0) {
        Write-Utf8Text -Path $Path -Text ''
        return
    }
    $text = (@($rowsArray | ConvertTo-Csv -NoTypeInformation) -join "`n") + "`n"
    Write-Utf8Text -Path $Path -Text $text
}

function Get-ObjectPropertyValue {
    param(
        [AllowNull()]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Convert-ToStableString {
    param([AllowNull()]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) {
        return $Value.ToString('o', [System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -is [bool]) {
        return $Value.ToString().ToLowerInvariant()
    }
    if ($Value -is [IFormattable]) {
        return $Value.ToString($null, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    return [string]$Value
}

function Convert-ToInvariantNumber {
    param(
        [AllowNull()]$Value,
        [string]$Format = '0.###'
    )
    if ($null -eq $Value) { return $null }
    try {
        return ([double]$Value).ToString($Format, [System.Globalization.CultureInfo]::InvariantCulture)
    }
    catch {
        return (Convert-ToStableString -Value $Value)
    }
}

function Get-SizeMegabytes {
    param([AllowNull()]$SizeObject)
    if ($null -eq $SizeObject) { return $null }
    foreach ($propertyName in @('Megabytes', 'Megabyte', 'MB')) {
        $property = $SizeObject.PSObject.Properties[$propertyName]
        if ($null -ne $property -and $null -ne $property.Value) {
            return (Convert-ToInvariantNumber -Value $property.Value -Format '0.###')
        }
    }
    return $null
}

function Expand-NameList {
    param([string[]]$Values)
    $expanded = foreach ($value in @($Values)) {
        if ([string]::IsNullOrWhiteSpace($value)) { continue }
        foreach ($part in ($value -split '[,;]')) {
            $trimmed = $part.Trim()
            if ($trimmed) { $trimmed }
        }
    }
    return @($expanded | Select-Object -Unique)
}

function Get-ShortHash {
    param([Parameter(Mandatory = $true)][string]$Value)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
        $hash = [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
        return $hash.Substring(0, 8)
    }
    finally {
        $sha.Dispose()
    }
}

function Get-SafePathSegment {
    param([Parameter(Mandatory = $true)][string]$Value)

    $safe = [regex]::Replace($Value, '[\x00-\x1f<>:"/\\|?*]', '_')
    $safe = $safe.Trim().TrimEnd([char[]]' .')
    if ([string]::IsNullOrWhiteSpace($safe)) { $safe = '_' }

    $reserved = $safe -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$'
    $changed = ($safe -ne $Value) -or $reserved

    if ($safe.Length -gt 100) {
        $safe = $safe.Substring(0, 90)
        $changed = $true
    }

    if ($changed) {
        $safe = "${safe}__$(Get-ShortHash -Value $Value)"
    }
    return $safe
}

function Get-DatabaseMetadata {
    param([Parameter(Mandatory = $true)]$DatabaseObject)

    return [ordered]@{
        Name                       = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'Name')
        Status                     = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'Status')
        IsSystemObject             = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'IsSystemObject')
        RecoveryModel              = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'RecoveryModel')
        CompatibilityLevel         = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'CompatibilityLevel')
        Collation                  = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'Collation')
        Owner                      = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'Owner')
        CreateDate                 = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'CreateDate')
        SizeMB                     = Convert-ToInvariantNumber (Get-ObjectPropertyValue $DatabaseObject 'Size')
        UserAccess                 = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'UserAccess')
        ReadOnly                   = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'ReadOnly')
        AutoClose                  = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'AutoClose')
        AutoShrink                 = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'AutoShrink')
        PageVerify                 = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'PageVerify')
        BrokerEnabled              = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'BrokerEnabled')
        Trustworthy                = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'Trustworthy')
        DatabaseOwnershipChaining  = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'DatabaseOwnershipChaining')
        IsEncrypted                = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'IsEncrypted')
        TargetRecoveryTime         = Convert-ToStableString (Get-ObjectPropertyValue $DatabaseObject 'TargetRecoveryTime')
    }
}

function Get-FileGroupRows {
    param([Parameter(Mandatory = $true)]$DatabaseObject)

    $rows = foreach ($fileGroup in @($DatabaseObject.FileGroups)) {
        $fileNames = @($fileGroup.Files | Sort-Object Name | ForEach-Object { $_.Name })
        [pscustomobject][ordered]@{
            Database   = $DatabaseObject.Name
            FileGroup  = $fileGroup.Name
            IsDefault  = Convert-ToStableString (Get-ObjectPropertyValue $fileGroup 'IsDefault')
            IsReadOnly = Convert-ToStableString (Get-ObjectPropertyValue $fileGroup 'IsReadOnly')
            Type       = Convert-ToStableString (Get-ObjectPropertyValue $fileGroup 'FileGroupType')
            FileCount  = Convert-ToStableString $fileNames.Count
            Files      = ($fileNames -join ';')
        }
    }
    return @($rows | Sort-Object Database, FileGroup)
}

function Convert-SpaceRows {
    param([object[]]$SpaceObjects)

    $rows = foreach ($space in @($SpaceObjects)) {
        [pscustomobject][ordered]@{
            Database               = Convert-ToStableString (Get-ObjectPropertyValue $space 'Database')
            FileName               = Convert-ToStableString (Get-ObjectPropertyValue $space 'FileName')
            FileGroup              = Convert-ToStableString (Get-ObjectPropertyValue $space 'FileGroup')
            PhysicalName           = Convert-ToStableString (Get-ObjectPropertyValue $space 'PhysicalName')
            FileType               = Convert-ToStableString (Get-ObjectPropertyValue $space 'FileType')
            FileSizeMB             = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'FileSize')
            UsedSpaceMB            = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'UsedSpace')
            FreeSpaceMB            = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'FreeSpace')
            PercentUsed            = Convert-ToInvariantNumber (Get-ObjectPropertyValue $space 'PercentUsed')
            AutoGrowthType         = Convert-ToStableString (Get-ObjectPropertyValue $space 'AutoGrowType')
            AutoGrowthMB           = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'AutoGrowth')
            AutoGrowthDisplay      = Convert-ToStableString (Get-ObjectPropertyValue $space 'AutoGrowth')
            SpaceUntilMaxSizeMB    = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'SpaceUntilMaxSize')
            AutoGrowthPossibleMB   = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'AutoGrowthPossible')
            UnusableSpaceMB        = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'UnusableSpace')
        }
    }
    return @($rows | Sort-Object Database, FileType, FileName)
}

function Invoke-SqlPackageExtract {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$ServerName,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [int]$TimeoutSeconds,
        [switch]$TrustCertificate
    )

    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null

    $arguments = @(
        '/Action:Extract',
        "/SourceServerName:$ServerName",
        "/SourceDatabaseName:$DatabaseName",
        "/SourceTimeout:$TimeoutSeconds",
        "/TargetFile:$TargetDirectory",
        '/p:ExtractTarget=SchemaObjectType',
        '/p:ExtractAllTableData=False',
        '/p:VerifyExtraction=True',
        '/Quiet:True'
    )
    if ($TrustCertificate) {
        $arguments += '/SourceTrustServerCertificate:True'
    }

    Write-CollectorMessage "Extracting schema: $DatabaseName"
    & $Executable @arguments
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "SqlPackage failed for database '$DatabaseName' with exit code $exitCode."
    }
}

function Export-InstanceConfiguration {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [string[]]$AdditionalExcludes
    )

    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("configbackup-dbatools-" + [guid]::NewGuid().ToString('N'))
    [System.IO.Directory]::CreateDirectory($tempRoot) | Out-Null

    try {
        $excludes = @('Databases') + @(Expand-NameList $AdditionalExcludes)
        $excludes = @($excludes | Select-Object -Unique)

        Write-CollectorMessage 'Exporting SQL Server instance configuration with dbatools'
        $exportArgs = @{
            SqlInstance    = $ServerObject
            Path           = $tempRoot
            Force          = $true
            NoPrefix       = $true
            ExcludePassword = $true
            Exclude        = $excludes
            EnableException = $true
        }
        $files = @(Export-DbaInstance @exportArgs)

        $fileInfos = @($files | Where-Object { $_ -is [System.IO.FileInfo] -and $_.Exists } | Sort-Object Name, FullName)
        foreach ($file in $fileInfos) {
            $targetName = $file.Name
            $targetPath = Join-Path $TargetDirectory $targetName
            if (Test-Path -LiteralPath $targetPath) {
                $relativeSource = $file.FullName.Substring($tempRoot.Length).TrimStart([char[]]'\/')
                $targetName = "{0}.{1}{2}" -f $file.BaseName, (Get-ShortHash -Value $relativeSource), $file.Extension
                $targetPath = Join-Path $TargetDirectory $targetName
            }
            Copy-Item -LiteralPath $file.FullName -Destination $targetPath -Force
        }

        Write-CollectorMessage ("Instance export created {0} file(s)" -f $fileInfos.Count)
    }
    finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}


function Get-SqlLiteral {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return 'NULL' }
    return "N'" + $Value.Replace("'", "''") + "'"
}

function Convert-DataTableRows {
    param([Parameter(Mandatory = $true)]$Table)
    $rows = foreach ($row in $Table.Rows) {
        $ordered = [ordered]@{}
        foreach ($column in $Table.Columns) {
            $value = $row[$column.ColumnName]
            if ($value -is [System.DBNull]) { $value = $null }
            elseif ($value -is [byte[]]) { $value = [Convert]::ToBase64String($value) }
            elseif ($value -is [datetime]) { $value = $value.ToString('o') }
            $ordered[$column.ColumnName] = $value
        }
        [pscustomobject]$ordered
    }
    return @($rows)
}

function Invoke-QueryTable {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [Parameter(Mandatory = $true)][string]$Query
    )
    $safeDb = $DatabaseName.Replace(']', ']]')
    $ds = $ServerObject.ConnectionContext.ExecuteWithResults("USE [$safeDb];`n$Query")
    if ($null -eq $ds -or $ds.Tables.Count -eq 0) { return $null }
    return $ds.Tables[0]
}

function Export-SqlAgentConfiguration {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$TargetDirectory
    )
    Write-CollectorMessage 'Collecting SQL Server Agent configuration'
    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null
    $jobsDir = Join-Path $TargetDirectory 'jobs'
    [System.IO.Directory]::CreateDirectory($jobsDir) | Out-Null

    $jobServer = $ServerObject.JobServer
    $agentSettings = [ordered]@{
        AgentMailType = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'AgentMailType')
        DatabaseMailProfile = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'DatabaseMailProfile')
        ErrorLogFile = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'ErrorLogFile')
        IdleCpuDuration = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'IdleCpuDuration')
        IdleCpuPercentage = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'IdleCpuPercentage')
        MaximumHistoryRows = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'MaximumHistoryRows')
        MaximumJobHistoryRows = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'MaximumJobHistoryRows')
        NetSendRecipient = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'NetSendRecipient')
        ReplaceAlertTokensEnabled = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'ReplaceAlertTokensEnabled')
        SaveInSentFolder = Convert-ToStableString (Get-ObjectPropertyValue $jobServer 'SaveInSentFolder')
    }
    Write-StableJson -Path (Join-Path $TargetDirectory 'agent-settings.json') -Value $agentSettings

    $jobIndex = @()
    foreach ($job in @($ServerObject.JobServer.Jobs | Sort-Object Name)) {
        $safe = Get-SafePathSegment -Value $job.Name
        $steps = foreach ($step in @($job.JobSteps | Sort-Object ID)) {
            [pscustomobject][ordered]@{
                ID = $step.ID
                Name = $step.Name
                SubSystem = Convert-ToStableString $step.SubSystem
                DatabaseName = Convert-ToStableString $step.DatabaseName
                Command = Convert-ToStableString $step.Command
                CommandExecutionSuccessCode = Convert-ToStableString $step.CommandExecutionSuccessCode
                OnSuccessAction = Convert-ToStableString $step.OnSuccessAction
                OnSuccessStep = Convert-ToStableString $step.OnSuccessStep
                OnFailAction = Convert-ToStableString $step.OnFailAction
                OnFailStep = Convert-ToStableString $step.OnFailStep
                RetryAttempts = Convert-ToStableString $step.RetryAttempts
                RetryInterval = Convert-ToStableString $step.RetryInterval
                OutputFileName = Convert-ToStableString $step.OutputFileName
                ProxyName = Convert-ToStableString (Get-ObjectPropertyValue $step 'ProxyName')
            }
        }
        $schedules = foreach ($schedule in @($job.JobSchedules | Sort-Object Name)) {
            [pscustomobject][ordered]@{
                Name = $schedule.Name
                IsEnabled = Convert-ToStableString $schedule.IsEnabled
                FrequencyTypes = Convert-ToStableString $schedule.FrequencyTypes
                FrequencyInterval = Convert-ToStableString $schedule.FrequencyInterval
                FrequencySubDayTypes = Convert-ToStableString $schedule.FrequencySubDayTypes
                FrequencySubDayInterval = Convert-ToStableString $schedule.FrequencySubDayInterval
                FrequencyRelativeIntervals = Convert-ToStableString $schedule.FrequencyRelativeIntervals
                FrequencyRecurrenceFactor = Convert-ToStableString $schedule.FrequencyRecurrenceFactor
                ActiveStartDate = Convert-ToStableString $schedule.ActiveStartDate
                ActiveEndDate = Convert-ToStableString $schedule.ActiveEndDate
                ActiveStartTimeOfDay = Convert-ToStableString $schedule.ActiveStartTimeOfDay
                ActiveEndTimeOfDay = Convert-ToStableString $schedule.ActiveEndTimeOfDay
            }
        }
        $jobData = [ordered]@{
            Name = $job.Name
            IsEnabled = Convert-ToStableString $job.IsEnabled
            OwnerLoginName = Convert-ToStableString $job.OwnerLoginName
            Category = Convert-ToStableString $job.Category
            Description = Convert-ToStableString $job.Description
            StartStepID = Convert-ToStableString $job.StartStepID
            EmailLevel = Convert-ToStableString $job.EmailLevel
            OperatorToEmail = Convert-ToStableString $job.OperatorToEmail
            NetsendLevel = Convert-ToStableString $job.NetsendLevel
            OperatorToNetSend = Convert-ToStableString $job.OperatorToNetSend
            PageLevel = Convert-ToStableString $job.PageLevel
            OperatorToPage = Convert-ToStableString $job.OperatorToPage
            DeleteLevel = Convert-ToStableString $job.DeleteLevel
            Steps = @($steps)
            Schedules = @($schedules)
        }
        Write-StableJson -Path (Join-Path $jobsDir ($safe + '.json')) -Value $jobData -Depth 12
        try {
            $scriptText = (@($job.Script()) -join "`n")
            if (-not [string]::IsNullOrWhiteSpace($scriptText)) {
                Write-Utf8Text -Path (Join-Path $jobsDir ($safe + '.sql')) -Text ($scriptText + "`n")
            }
        }
        catch {
            Write-CollectorError "Unable to script SQL Agent job '$($job.Name)': $($_.Exception.Message)"
        }
        $jobIndex += [pscustomobject][ordered]@{
            Name = $job.Name
            Enabled = Convert-ToStableString $job.IsEnabled
            Owner = Convert-ToStableString $job.OwnerLoginName
            Category = Convert-ToStableString $job.Category
            StepCount = @($steps).Count
            ScheduleCount = @($schedules).Count
        }
    }
    Write-StableCsv -Path (Join-Path $TargetDirectory 'jobs.csv') -Rows $jobIndex

    foreach ($spec in @(
        @{Name='operators'; Query='SELECT name, enabled, email_address, pager_address, weekday_pager_start_time, weekday_pager_end_time, saturday_pager_start_time, saturday_pager_end_time, sunday_pager_start_time, sunday_pager_end_time FROM msdb.dbo.sysoperators ORDER BY name;'},
        @{Name='alerts'; Query='SELECT name, event_source, event_category_id, event_id, message_id, severity, enabled, delay_between_responses, include_event_description, database_name, notification_message, job_id, has_notification FROM msdb.dbo.sysalerts ORDER BY name;'},
        @{Name='notifications'; Query='SELECT a.name AS alert_name,o.name AS operator_name,n.notification_method FROM msdb.dbo.sysnotifications n JOIN msdb.dbo.sysalerts a ON n.alert_id=a.id JOIN msdb.dbo.sysoperators o ON n.operator_id=o.id ORDER BY a.name,o.name,n.notification_method;'},
        @{Name='schedules'; Query='SELECT schedule_id,schedule_uid,name,enabled,freq_type,freq_interval,freq_subday_type,freq_subday_interval,freq_relative_interval,freq_recurrence_factor,active_start_date,active_end_date,active_start_time,active_end_time,date_created,date_modified,version_number FROM msdb.dbo.sysschedules ORDER BY name,schedule_id;'},
        @{Name='schedule-jobs'; Query='SELECT s.name AS schedule_name,j.name AS job_name FROM msdb.dbo.sysjobschedules js JOIN msdb.dbo.sysschedules s ON js.schedule_id=s.schedule_id JOIN msdb.dbo.sysjobs j ON js.job_id=j.job_id ORDER BY s.name,j.name;'},
        @{Name='categories'; Query='SELECT category_id,category_class,category_type,name FROM msdb.dbo.syscategories ORDER BY category_class,category_type,name;'},
        @{Name='proxies'; Query='SELECT p.name AS proxy_name, p.enabled, p.description, c.name AS credential_name FROM msdb.dbo.sysproxies p LEFT JOIN master.sys.credentials c ON p.credential_id=c.credential_id ORDER BY p.name;'},
        @{Name='proxy-subsystems'; Query='SELECT p.name AS proxy_name, s.subsystem, s.subsystem_id FROM msdb.dbo.sysproxysubsystem ps JOIN msdb.dbo.sysproxies p ON ps.proxy_id=p.proxy_id JOIN msdb.dbo.syssubsystems s ON ps.subsystem_id=s.subsystem_id ORDER BY p.name,s.subsystem;'}
    )) {
        try {
            $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'msdb' -Query $spec.Query
            if ($null -ne $table) {
                Write-StableCsv -Path (Join-Path $TargetDirectory ($spec.Name + '.csv')) -Rows (Convert-DataTableRows $table)
            }
        }
        catch {
            Write-CollectorError "Unable to collect SQL Agent $($spec.Name): $($_.Exception.Message)"
        }
    }
}

function Export-SsisConfiguration {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [switch]$SkipIspacFiles,
        [switch]$IncludeLegacy
    )
    if ($null -eq $ServerObject.Databases['SSISDB']) {
        Write-CollectorMessage 'SSISDB is not present; skipping project-deployment SSIS collection'
    }
    else {
        Write-CollectorMessage 'Collecting SSISDB projects, packages, parameters, environments, and references'
        [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null
        $queries = [ordered]@{
            'catalog-properties.csv' = 'SELECT property_name, property_value FROM catalog.catalog_properties ORDER BY property_name;'
            'folders.csv' = 'SELECT id AS folder_id,name,description,created_by_name,created_time FROM catalog.folders ORDER BY name;'
            'projects.csv' = 'SELECT project_id,folder_id,name,description,project_format_version,deployed_by_name,last_deployed_time,created_time,object_version_lsn FROM catalog.projects ORDER BY folder_id,name;'
            'packages.csv' = 'SELECT p.package_id,p.project_id,p.name,p.package_guid,p.description,p.package_format_version,p.version_major,p.version_minor,p.version_build,p.version_comments FROM catalog.packages p ORDER BY p.project_id,p.name;'
            'environment-references.csv' = 'SELECT reference_id,project_id,reference_type,environment_folder_name,environment_name FROM catalog.environment_references ORDER BY project_id,reference_id;'
            'environments.csv' = 'SELECT environment_id,folder_id,name,description,created_by_name,created_time FROM catalog.environments ORDER BY folder_id,name;'
            'environment-variables.csv' = "SELECT environment_id,name,description,type,sensitive,CASE WHEN sensitive=1 THEN N'<REDACTED>' ELSE CONVERT(nvarchar(max),value) END AS value FROM catalog.environment_variables ORDER BY environment_id,name;"
            'object-parameters.csv' = "SELECT project_id,object_type,object_name,parameter_name,data_type,required,sensitive,description,CASE WHEN sensitive=1 THEN N'<REDACTED>' ELSE CONVERT(nvarchar(max),design_default_value) END AS design_default_value,CASE WHEN sensitive=1 THEN N'<REDACTED>' ELSE CONVERT(nvarchar(max),default_value) END AS default_value,value_type,value_set,referenced_variable_name FROM catalog.object_parameters ORDER BY project_id,object_type,object_name,parameter_name;"
            'explicit-object-permissions.csv' = 'SELECT e.object_type,e.object_id,e.principal_id,p.name AS principal_name,p.type_desc AS principal_type,e.permission_type,e.is_deny,e.grantor_id,g.name AS grantor_name FROM catalog.explicit_object_permissions e LEFT JOIN sys.database_principals p ON e.principal_id=p.principal_id LEFT JOIN sys.database_principals g ON e.grantor_id=g.principal_id ORDER BY e.object_type,e.object_id,p.name,e.permission_type;'
            'database-role-memberships.csv' = 'SELECT rp.name AS role_name,mp.name AS member_name,mp.type_desc AS member_type FROM sys.database_role_members drm JOIN sys.database_principals rp ON drm.role_principal_id=rp.principal_id JOIN sys.database_principals mp ON drm.member_principal_id=mp.principal_id ORDER BY rp.name,mp.name;'
        }
        foreach ($entry in $queries.GetEnumerator()) {
            $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query $entry.Value
            if ($null -ne $table) {
                Write-StableCsv -Path (Join-Path $TargetDirectory $entry.Key) -Rows (Convert-DataTableRows $table)
            }
        }

        $projectTable = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query @'
SELECT p.project_id,p.name AS project_name,f.name AS folder_name
FROM catalog.projects p JOIN catalog.folders f ON p.folder_id=f.folder_id
ORDER BY f.name,p.name;
'@
        foreach ($row in @($projectTable.Rows)) {
            $folderName = [string]$row['folder_name']
            $projectName = [string]$row['project_name']
            $folderDir = Get-SafePathSegment -Value $folderName
            $projectDir = Get-SafePathSegment -Value $projectName
            $targetProject = Join-Path (Join-Path (Join-Path $TargetDirectory 'projects') $folderDir) $projectDir
            [System.IO.Directory]::CreateDirectory($targetProject) | Out-Null
            $folderLit = Get-SqlLiteral $folderName
            $projectLit = Get-SqlLiteral $projectName
            $streamTable = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query "EXEC catalog.get_project @folder_name=$folderLit, @project_name=$projectLit;"
            if ($null -eq $streamTable -or $streamTable.Rows.Count -eq 0) { throw "SSISDB returned no project stream for $folderName/$projectName" }
            $projectBytes = $streamTable.Rows[0][0]
            if ($projectBytes -isnot [byte[]]) { throw "Unexpected SSIS project stream type for $folderName/$projectName" }
            $tempIspac = Join-Path ([System.IO.Path]::GetTempPath()) ("configbackup-" + [guid]::NewGuid().ToString('N') + '.ispac')
            [System.IO.File]::WriteAllBytes($tempIspac, $projectBytes)
            try {
                if (-not $SkipIspacFiles) {
                    Copy-Item -LiteralPath $tempIspac -Destination (Join-Path $targetProject ($projectDir + '.ispac')) -Force
                }
                Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
                $expanded = Join-Path $targetProject 'expanded'
                if (Test-Path -LiteralPath $expanded) { Remove-Item -LiteralPath $expanded -Recurse -Force }
                [System.IO.Compression.ZipFile]::ExtractToDirectory($tempIspac, $expanded)
            }
            finally {
                Remove-Item -LiteralPath $tempIspac -Force -ErrorAction SilentlyContinue
            }
        }
    }

    if ($IncludeLegacy) {
        Write-CollectorMessage 'Collecting legacy MSDB SSIS package metadata and package data'
        $legacyDir = Join-Path $TargetDirectory 'legacy-msdb'
        [System.IO.Directory]::CreateDirectory($legacyDir) | Out-Null
        $legacy = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'msdb' -Query 'SELECT id,name,description,folderid,ownersid,packagedata,packageformat FROM dbo.sysssispackages ORDER BY name,id;'
        $metadata = @()
        foreach ($row in @($legacy.Rows)) {
            $name = [string]$row['name']
            $id = [string]$row['id']
            $safe = (Get-SafePathSegment -Value $name) + '__' + (Get-ShortHash -Value $id)
            $metadata += [pscustomobject][ordered]@{
                id=$id; name=$name; description=[string]$row['description']; folderid=[string]$row['folderid']; packageformat=[string]$row['packageformat']
            }
            $bytes = $row['packagedata']
            if ($bytes -is [byte[]]) {
                [System.IO.File]::WriteAllBytes((Join-Path $legacyDir ($safe + '.dtsx')), $bytes)
            }
        }
        Write-StableCsv -Path (Join-Path $legacyDir 'packages.csv') -Rows $metadata
    }
}

try {
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        throw 'OutputDirectory was not specified and CONFIGBACKUP_OUTPUT is not set.'
    }

    $OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
    [System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null

    Write-CollectorMessage "Collector version $CollectorVersion"
    Write-CollectorMessage "SQL instance: $SqlInstance"
    Write-CollectorMessage "Output: $OutputDirectory"

    $dbatoolsModule = Get-Module -ListAvailable -Name dbatools | Sort-Object Version -Descending | Select-Object -First 1
    if ($null -eq $dbatoolsModule) {
        throw "The dbatools PowerShell module is required. Install it with: Install-Module dbatools -Scope CurrentUser"
    }
    Import-Module dbatools -ErrorAction Stop
    $loadedDbatools = Get-Module dbatools | Sort-Object Version -Descending | Select-Object -First 1

    $resolvedSqlPackage = $null
    $sqlPackageVersion = $null
    if (-not $SkipSchema) {
        if (Test-Path -LiteralPath $SqlPackagePath) {
            $resolvedSqlPackage = (Resolve-Path -LiteralPath $SqlPackagePath).Path
        }
        else {
            $sqlPackageCommand = Get-Command $SqlPackagePath -ErrorAction Stop
            $resolvedSqlPackage = $sqlPackageCommand.Source
        }
        $versionOutput = (& $resolvedSqlPackage /Version 2>&1 | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to execute SqlPackage at '$resolvedSqlPackage'."
        }
        $sqlPackageVersion = $versionOutput
    }

    $connectArgs = @{
        SqlInstance    = $SqlInstance
        ClientName     = 'ConfigBackup.SqlCollector'
        ConnectTimeout = $ConnectTimeout
    }
    if ($TrustServerCertificate) {
        $connectArgs.TrustServerCertificate = $true
    }

    Write-CollectorMessage 'Connecting with dbatools'
    $server = Connect-DbaInstance @connectArgs

    $requestedDatabases = @(Expand-NameList $Database)
    $excludedDatabases = @(Expand-NameList $ExcludeDatabase)

    $databaseArgs = @{
        SqlInstance     = $server
        OnlyAccessible  = $true
        EnableException = $true
    }
    if ($requestedDatabases.Count -gt 0) {
        $databaseArgs.Database = $requestedDatabases
    }
    elseif (-not $IncludeSystemDatabases) {
        $databaseArgs.ExcludeSystem = $true
    }
    if ($excludedDatabases.Count -gt 0) {
        $databaseArgs.ExcludeDatabase = $excludedDatabases
    }

    $databases = @(Get-DbaDatabase @databaseArgs | Where-Object {
        $IncludeTempdb -or $_.Name -ne 'tempdb'
    } | Sort-Object Name)

    Write-CollectorMessage ("Selected {0} database(s)" -f $databases.Count)

    $instanceDirectory = Join-Path $OutputDirectory 'instance'
    $databaseRoot = Join-Path $OutputDirectory 'databases'
    [System.IO.Directory]::CreateDirectory($instanceDirectory) | Out-Null
    [System.IO.Directory]::CreateDirectory($databaseRoot) | Out-Null

    $serverInfo = [ordered]@{
        SqlInstanceInput          = $SqlInstance
        Name                      = Convert-ToStableString (Get-ObjectPropertyValue $server 'Name')
        DomainInstanceName        = Convert-ToStableString (Get-ObjectPropertyValue $server 'DomainInstanceName')
        ComputerNamePhysicalNetBIOS = Convert-ToStableString (Get-ObjectPropertyValue $server 'ComputerNamePhysicalNetBIOS')
        InstanceName              = Convert-ToStableString (Get-ObjectPropertyValue $server 'InstanceName')
        VersionString             = Convert-ToStableString (Get-ObjectPropertyValue $server 'VersionString')
        Edition                   = Convert-ToStableString (Get-ObjectPropertyValue $server 'Edition')
        ProductLevel              = Convert-ToStableString (Get-ObjectPropertyValue $server 'ProductLevel')
        EngineEdition             = Convert-ToStableString (Get-ObjectPropertyValue $server 'EngineEdition')
        Collation                 = Convert-ToStableString (Get-ObjectPropertyValue $server 'Collation')
        IsClustered               = Convert-ToStableString (Get-ObjectPropertyValue $server 'IsClustered')
        HostPlatform              = Convert-ToStableString (Get-ObjectPropertyValue $server 'HostPlatform')
    }
    Write-StableJson -Path (Join-Path $instanceDirectory 'server.json') -Value $serverInfo

    $collectorInfo = [ordered]@{
        Collector             = 'Collect-SqlServerConfiguration.ps1'
        CollectorVersion      = $CollectorVersion
        PowerShellVersion     = $PSVersionTable.PSVersion.ToString()
        PowerShellEdition     = Convert-ToStableString $PSVersionTable.PSEdition
        DbatoolsVersion       = if ($null -ne $loadedDbatools) { $loadedDbatools.Version.ToString() } else { $null }
        SqlPackageVersion     = $sqlPackageVersion
        SchemaExtraction      = (-not $SkipSchema)
        InstanceExport        = (-not $SkipInstanceExport)
        Inventory             = (-not $SkipInventory)
        PasswordMaterial      = 'excluded'
        SqlAgent               = (-not $SkipAgent)
        Ssis                   = (-not $SkipSsis)
        IspacFiles             = (-not $SkipIspac)
        LegacySsis             = [bool]$IncludeLegacySsis
    }
    Write-StableJson -Path (Join-Path $OutputDirectory 'collector.json') -Value $collectorInfo

    $databaseMap = @()
    $usedDatabaseDirectories = @{}
    $databaseInventory = @()
    $allFileGroupRows = @()
    $allSpaceRows = @()

    if (-not $SkipInventory) {
        Write-CollectorMessage 'Collecting database and file inventory'
        $spaceObjects = if ($databases.Count -gt 0) {
            @($databases | Get-DbaDbSpace -EnableException)
        }
        else {
            @()
        }
        $allSpaceRows = @(Convert-SpaceRows -SpaceObjects $spaceObjects)

        foreach ($db in $databases) {
            $metadata = Get-DatabaseMetadata -DatabaseObject $db
            $databaseInventory += [pscustomobject]$metadata
            $allFileGroupRows += @(Get-FileGroupRows -DatabaseObject $db)
        }

        Write-StableCsv -Path (Join-Path $instanceDirectory 'databases.csv') -Rows $databaseInventory
        Write-StableCsv -Path (Join-Path $instanceDirectory 'database-files.csv') -Rows $allSpaceRows
        Write-StableCsv -Path (Join-Path $instanceDirectory 'filegroups.csv') -Rows $allFileGroupRows
    }

    if (-not $SkipInstanceExport) {
        $instanceExportArgs = @{
            ServerObject       = $server
            TargetDirectory    = (Join-Path $instanceDirectory 'scripts')
            AdditionalExcludes = $InstanceExclude
        }
        Export-InstanceConfiguration @instanceExportArgs
    }

    if (-not $SkipAgent) {
        Export-SqlAgentConfiguration -ServerObject $server -TargetDirectory (Join-Path $instanceDirectory 'agent')
    }

    if (-not $SkipSsis) {
        Export-SsisConfiguration -ServerObject $server -TargetDirectory (Join-Path $instanceDirectory 'ssis') -SkipIspacFiles:$SkipIspac -IncludeLegacy:$IncludeLegacySsis
    }

    foreach ($db in $databases) {
        $safeName = Get-SafePathSegment -Value $db.Name
        if ($usedDatabaseDirectories.ContainsKey($safeName) -and $usedDatabaseDirectories[$safeName] -ne $db.Name) {
            $safeName = "${safeName}__$(Get-ShortHash -Value $db.Name)"
        }
        $usedDatabaseDirectories[$safeName] = $db.Name
        $databaseMap += [pscustomobject][ordered]@{
            Database  = $db.Name
            Directory = $safeName
        }

        $dbDirectory = Join-Path $databaseRoot $safeName
        [System.IO.Directory]::CreateDirectory($dbDirectory) | Out-Null

        if (-not $SkipInventory) {
            $metadata = Get-DatabaseMetadata -DatabaseObject $db
            Write-StableJson -Path (Join-Path $dbDirectory 'database.json') -Value $metadata

            $dbSpaceRows = @($allSpaceRows | Where-Object { $_.Database -eq $db.Name })
            $dbFileGroupRows = @($allFileGroupRows | Where-Object { $_.Database -eq $db.Name })
            Write-StableCsv -Path (Join-Path $dbDirectory 'files.csv') -Rows $dbSpaceRows
            Write-StableCsv -Path (Join-Path $dbDirectory 'filegroups.csv') -Rows $dbFileGroupRows
        }

        if (-not $SkipSchema) {
            $extractArgs = @{
                Executable      = $resolvedSqlPackage
                ServerName      = $SqlInstance
                DatabaseName    = $db.Name
                TargetDirectory = (Join-Path $dbDirectory 'schema')
                TimeoutSeconds  = $ConnectTimeout
                TrustCertificate = [bool]$TrustServerCertificate
            }
            Invoke-SqlPackageExtract @extractArgs
        }
    }

    Write-StableCsv -Path (Join-Path $OutputDirectory 'database-map.csv') -Rows $databaseMap

    Write-CollectorMessage 'SQL Server collection completed successfully'
    exit 0
}
catch {
    Write-CollectorError $_.Exception.Message
    Write-CollectorError $_.ScriptStackTrace
    exit 1
}
