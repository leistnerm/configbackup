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
    Collector version: 2.0.0
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

    # Optional in-memory credential for SQL authentication (never store in YAML).
    [System.Management.Automation.PSCredential]$SqlCredential,

    # Comma/semicolon-delimited values are accepted, which is convenient when the
    # collector is launched by an external process/YAML task.
    [string[]]$Database = @(),
    [string[]]$ExcludeDatabase = @(),

    [switch]$IncludeSystemDatabases,
    [switch]$IncludeTempdb,

    [string]$SqlPackagePath = 'sqlpackage',

    # SqlPackage schema-model verification is intentionally opt-in. Extraction can
    # succeed for source-control/history purposes even when model verification finds
    # unresolved external references.
    [switch]$VerifySchemaExtraction,

    # SqlPackage can otherwise emit child elements in unstable order between
    # identical extracts. Keep DacFx name-sorting enabled by default so Git/hash
    # history reflects semantic changes instead of collection-order churn.
    [switch]$DisableSchemaElementSorting,

    [switch]$SkipSchema,
    [switch]$SkipInstanceExport,
    [switch]$ReadOnlyAccess,
    # Include frequently-changing size/usage counters in an inventory-only file.
    # Disabled by default to keep SQL configuration diffs meaningful.
    [switch]$IncludeCapacityMetrics,
    [switch]$IncludePerformanceMetrics,
    [switch]$IncludeHealthMetrics,
    [switch]$IncludeIndexHealth,
    [ValidateRange(1,300)][int]$HealthQueryTimeout = 15,
    [ValidateRange(1,1000)][int]$HealthIndexLimit = 25,

    [switch]$SkipInventory,
    [switch]$IncludeAgentHistory,

    [ValidateRange(1, 365)]
    [int]$AgentHistoryDays = 60,

    [switch]$SkipAgent,
    [switch]$SkipSsis,
    [switch]$SkipIspac,
    [switch]$IncludeLegacySsis,
    [switch]$AllowPartialSsis,

    # When the collector itself is running on the same Linux host as SQL Server,
    # capture mssql.conf, stable systemd unit metadata, package versions, and
    # parsed SQL Server host settings. This is automatic unless skipped.
    [switch]$SkipHostConfiguration,

    # Use only when SQLInstance is an alias that prevents automatic local-host
    # detection. This never collects a remote host; it explicitly says the local
    # Linux machine running this collector is the SQL Server host.
    [switch]$CollectLocalHostConfiguration,

    # Additional Export-DbaInstance categories to skip. "Databases", "AgentServer",
    # and "AvailabilityGroups" are always excluded from the broad export because this
    # collector handles those areas separately (AGs are conditionally exported only
    # when HADR is enabled).
    [string[]]$InstanceExclude = @(),

    [switch]$TrustServerCertificate,

    # Optional non-secret SQL connection-string properties appended to dbatools and
    # SqlPackage connections, for example:
    #   'MultiSubnetFailover=True;ApplicationIntent=ReadOnly'
    # Endpoint, authentication, database, timeout, encryption, certificate-trust, and
    # credential-bearing keys are rejected because they are controlled explicitly.
    [string]$AppendConnectionString = '',

    # Export-DbaInstance/SMO can fail transiently while a database is changing state
    # (for example ONLINE/OFFLINE/RESTORE transitions). Retry only that specific
    # error; all other instance-export failures remain immediately fatal.
    [ValidateRange(0, 10)]
    [int]$InstanceExportTransitionRetries = 3,

    [ValidateRange(0, 300)]
    [int]$InstanceExportTransitionDelaySeconds = 15,

    [ValidateRange(1, 600)]
    [int]$ConnectTimeout = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$CollectorVersion = '2.0.0'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-CollectorMessage {
    param([string]$Message)
    if ($null -ne $SqlCredential) { $Message = $Message.Replace($SqlCredential.GetNetworkCredential().Password, '<REDACTED>') }
    [Console]::Out.WriteLine("[sql-collector] $Message")
}

function Write-CollectorError {
    param([string]$Message)
    if ($null -ne $SqlCredential) { $Message = $Message.Replace($SqlCredential.GetNetworkCredential().Password, '<REDACTED>') }
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
        [AllowNull()][AllowEmptyCollection()][object[]]$Rows
    )

    # PowerShell functions emit no pipeline object for an empty array. Callers that
    # convert a zero-row DataTable can therefore arrive here with $null rather than
    # @(). Treat both forms as a valid empty result and write an empty CSV artifact.
    if ($null -eq $Rows) {
        Write-Utf8Text -Path $Path -Text ''
        return
    }

    $rowsArray = @($Rows)
    if ($rowsArray.Count -eq 0) {
        Write-Utf8Text -Path $Path -Text ''
        return
    }
    # Sort complete serialized rows, not object-property enumeration order.
    # SQL metadata views and SMO collections may return equivalent sets in
    # arbitrary orders on successive runs. Do NOT reorder T-SQL scripts.
    $csvLines = @($rowsArray | ConvertTo-Csv -NoTypeInformation)
    if ($csvLines.Count -lt 2) { return Write-Utf8Text -Path $Path -Text (($csvLines -join "`n") + "`n") }
    $header = $csvLines[0]
    $body = @($csvLines | Select-Object -Skip 1)
    [array]::Sort($body, [System.StringComparer]::Ordinal)
    $sortedCsv = @($header) + $body
    Write-Utf8Text -Path $Path -Text (($sortedCsv -join "`n") + "`n")
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
            AutoGrowthType         = Convert-ToStableString (Get-ObjectPropertyValue $space 'AutoGrowType')
            AutoGrowthMB           = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'AutoGrowth')
            AutoGrowthDisplay      = Convert-ToStableString (Get-ObjectPropertyValue $space 'AutoGrowth')
        }
    }
    return @($rows | Sort-Object Database, FileType, FileName)
}

function Assert-SafeAppendConnectionString {
    param([AllowEmptyString()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return }

    # Keep endpoint/authentication/secrets and settings already controlled by explicit
    # collector parameters out of YAML. The option is intended for non-secret transport
    # and routing properties such as MultiSubnetFailover or ApplicationIntent.
    $forbiddenPattern = '(?i)(?:^|;)\s*(?:password|pwd|user\s*id|uid|access\s*token|authentication|integrated\s*security|trusted_connection|data\s*source|server|address|addr|network\s*address|initial\s*catalog|database|encrypt|trustservercertificate|connect\s*timeout|connection\s*timeout)\s*='
    if ($Value -match $forbiddenPattern) {
        throw 'AppendConnectionString contains a forbidden endpoint, authentication, credential, database, timeout, encryption, or certificate-trust property. Use the collector parameters for those settings and keep secrets out of YAML.'
    }
}

function Get-SqlPackageSourceConnectionString {
    param(
        [Parameter(Mandatory = $true)][string]$ServerName,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [int]$TimeoutSeconds,
        [switch]$TrustCertificate,
        [AllowEmptyString()][string]$AdditionalOptions
    )

    $builder = New-Object System.Data.SqlClient.SqlConnectionStringBuilder
    $builder['Data Source'] = $ServerName
    $builder['Initial Catalog'] = $DatabaseName
    $builder['Integrated Security'] = ($null -eq $SqlCredential)
    if ($null -ne $SqlCredential) {
        $builder['User ID'] = $SqlCredential.UserName
        $builder['Password'] = $SqlCredential.GetNetworkCredential().Password
    }
    $builder['Encrypt'] = $true
    $builder['TrustServerCertificate'] = [bool]$TrustCertificate
    $builder['Connect Timeout'] = $TimeoutSeconds
    $builder['Application Name'] = 'ConfigBackup.SqlCollector'

    $connectionString = $builder.ConnectionString.TrimEnd(';')
    if (-not [string]::IsNullOrWhiteSpace($AdditionalOptions)) {
        $connectionString += ';' + $AdditionalOptions.Trim().Trim(';')
    }
    return $connectionString + ';'
}

function Invoke-SqlPackageExtract {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string]$ServerName,
        [Parameter(Mandatory = $true)][string]$DatabaseName,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [int]$TimeoutSeconds,
        [switch]$TrustCertificate,
        [switch]$VerifyExtraction,
        [bool]$SortElementsByName = $true,
        [AllowEmptyString()][string]$AdditionalConnectionOptions
    )

    # SqlPackage creates the SchemaObjectType target tree itself. Ensure only the
    # parent exists and remove any stale target from a prior/partial extraction.
    # ConfigBackup staging is intentionally disposable current-state output, so this
    # cleanup is safe and prevents SqlPackage target-collision failures.
    $targetParent = Split-Path -Parent $TargetDirectory
    if (-not [string]::IsNullOrWhiteSpace($targetParent)) {
        [System.IO.Directory]::CreateDirectory($targetParent) | Out-Null
    }
    if (Test-Path -LiteralPath $TargetDirectory) {
        Remove-Item -LiteralPath $TargetDirectory -Recurse -Force
    }

    $diagnosticsFile = Join-Path ([System.IO.Path]::GetTempPath()) ("configbackup-sqlpackage-" + [guid]::NewGuid().ToString('N') + '.log')
    $verifyValue = if ($VerifyExtraction) { 'True' } else { 'False' }
    $trustValue = if ($TrustCertificate) { 'True' } else { 'False' }
    $sortElementsValue = if ($SortElementsByName) { 'True' } else { 'False' }

    if ([string]::IsNullOrWhiteSpace($AdditionalConnectionOptions) -and $null -eq $SqlCredential) {
        # Keep the default invocation identical to a normal SqlPackage CLI command. This
        # path is intentionally simple because it is also easy to reproduce manually.
        $arguments = @(
            '/Action:Extract',
            "/SourceServerName:$ServerName",
            "/SourceDatabaseName:$DatabaseName",
            "/SourceTimeout:$TimeoutSeconds",
            '/SourceEncryptConnection:True',
            "/SourceTrustServerCertificate:$trustValue",
            "/TargetFile:$TargetDirectory",
            '/p:ExtractTarget=SchemaObjectType',
            '/p:ExtractAllTableData=False',
            '/p:IgnorePermissions=False',
            "/p:ScriptSortElementsByName=$sortElementsValue",
            "/p:VerifyExtraction=$verifyValue",
            '/Diagnostics:True',
            "/DiagnosticsFile:$diagnosticsFile",
            '/DiagnosticsLevel:Verbose'
        )
    }
    else {
        $sourceConnectionString = Get-SqlPackageSourceConnectionString `
            -ServerName $ServerName `
            -DatabaseName $DatabaseName `
            -TimeoutSeconds $TimeoutSeconds `
            -TrustCertificate:$TrustCertificate `
            -AdditionalOptions $AdditionalConnectionOptions

        $arguments = @(
            '/Action:Extract',
            "/SourceConnectionString:$sourceConnectionString",
            "/TargetFile:$TargetDirectory",
            '/p:ExtractTarget=SchemaObjectType',
            '/p:ExtractAllTableData=False',
            '/p:IgnorePermissions=False',
            "/p:ScriptSortElementsByName=$sortElementsValue",
            "/p:VerifyExtraction=$verifyValue",
            '/Diagnostics:True',
            "/DiagnosticsFile:$diagnosticsFile",
            '/DiagnosticsLevel:Verbose'
        )
    }

    Write-CollectorMessage "Extracting schema: $DatabaseName (verify=$verifyValue trust_server_certificate=$trustValue sort_elements_by_name=$sortElementsValue)"
    try {
        # Use PowerShell's native invocation operator so argument semantics match the
        # standalone command administrators can reproduce at a console. Temporarily
        # prevent a non-zero native exit from becoming a terminating PowerShell error;
        # the collector handles the exit code and diagnostics explicitly below.
        $savedErrorActionPreference = $ErrorActionPreference
        $nativePreferenceExists = Test-Path variable:PSNativeCommandUseErrorActionPreference
        if ($nativePreferenceExists) {
            $savedNativePreference = $PSNativeCommandUseErrorActionPreference
        }
        try {
            $ErrorActionPreference = 'Continue'
            if ($nativePreferenceExists) {
                $PSNativeCommandUseErrorActionPreference = $false
            }
            # Do not redirect/capture SqlPackage output here. Let stdout/stderr flow
            # directly to ConfigBackup, which already captures and logs both streams.
            # This also keeps invocation semantics identical to a manually tested CLI
            # command and avoids PowerShell NativeCommandError wrapping.
            & $Executable @arguments
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorActionPreference
            if ($nativePreferenceExists) {
                $PSNativeCommandUseErrorActionPreference = $savedNativePreference
            }
        }

        if ($exitCode -ne 0) {
            Write-CollectorError "SqlPackage failed for database '$DatabaseName' with exit code $exitCode."
            if (Test-Path -LiteralPath $diagnosticsFile) {
                $diagnosticLines = @(Get-Content -LiteralPath $diagnosticsFile -ErrorAction SilentlyContinue)
                if ($diagnosticLines.Count -gt 0) {
                    Write-CollectorError 'SqlPackage diagnostics:'
                    foreach ($line in @($diagnosticLines | Select-Object -Last 300)) {
                        Write-CollectorError ("SqlPackage diagnostic: " + [string]$line)
                    }
                }
            }
            throw "SqlPackage failed for database '$DatabaseName' with exit code $exitCode."
        }
    }
    finally {
        Remove-Item -LiteralPath $diagnosticsFile -Force -ErrorAction SilentlyContinue
    }
}

function Get-HadrEnabled {
    param(
        [Parameter(Mandatory = $true)]$ServerObject
    )

    try {
        $dataSet = $ServerObject.ConnectionContext.ExecuteWithResults("SELECT CAST(SERVERPROPERTY('IsHadrEnabled') AS int) AS IsHadrEnabled;")
        if ($null -eq $dataSet -or $dataSet.Tables.Count -eq 0 -or $dataSet.Tables[0].Rows.Count -eq 0) {
            return $false
        }
        $value = $dataSet.Tables[0].Rows[0]['IsHadrEnabled']
        if ($value -eq [DBNull]::Value -or $null -eq $value) {
            return $false
        }
        return ([int]$value -eq 1)
    }
    catch {
        $script:HadrDetectionError = $_.Exception.Message
        Write-CollectorMessage ("Unable to determine HADR status; treating Availability Groups as unavailable: {0}" -f $_.Exception.Message)
        return $false
    }
}

function Export-AvailabilityGroupConfiguration {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [Parameter(Mandatory = $true)][bool]$HadrEnabled
    )

    if (-not $HadrEnabled) {
        Write-CollectorMessage 'Skipping Availability Groups: HADR is not enabled on this instance'
        return
    }

    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null
    $targetPath = Join-Path $TargetDirectory 'AvailabilityGroups.sql'
    Write-CollectorMessage 'Exporting Availability Groups'

    try {
        $groups = @(Get-DbaAvailabilityGroup -SqlInstance $ServerObject -EnableException | Sort-Object Name)
        if ($groups.Count -eq 0) {
            Write-CollectorMessage 'HADR is enabled, but no Availability Groups are configured'
            return
        }

        $scriptingOptions = New-DbaScriptingOption
        $null = $groups | Export-DbaScript -FilePath $targetPath -NoPrefix -ScriptingOptionsObject $scriptingOptions -EnableException
        Write-CollectorMessage ("Availability Group export created {0} definition(s)" -f $groups.Count)
    }
    catch {
        Write-CollectorError ("Availability Group export failed: {0}" -f $_.Exception.Message)
        throw
    }
}

function Get-TransitionDatabaseName {
    param([Parameter(Mandatory = $true)]$ErrorRecord)

    $parts = @()
    if ($null -ne $ErrorRecord.Exception) {
        $parts += [string]$ErrorRecord.Exception.Message
        $parts += [string]$ErrorRecord.Exception.ToString()
    }
    $parts += [string]$ErrorRecord
    $text = $parts -join "`n"
    $match = [regex]::Match(
        $text,
        "(?i)Database\s+'([^']+)'\s+is in transition\.\s*Try the statement later\."
    )
    if (-not $match.Success) { return $null }
    return $match.Groups[1].Value
}

function Write-TransitionDatabaseState {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$DatabaseName
    )

    try {
        $databaseLiteral = Get-SqlLiteral $DatabaseName
        $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'master' -Query @"
SELECT
    name,
    state_desc,
    user_access_desc,
    is_read_only,
    is_in_standby
FROM sys.databases
WHERE name = $databaseLiteral;
"@
        if ($null -eq $table -or $table.Rows.Count -eq 0) {
            Write-CollectorMessage "Transitioning database '$DatabaseName' is not currently visible in sys.databases"
            return
        }
        $row = $table.Rows[0]
        Write-CollectorMessage (
            "Transitioning database state: name={0} state={1} user_access={2} read_only={3} standby={4}" -f
            [string]$row['name'],
            [string]$row['state_desc'],
            [string]$row['user_access_desc'],
            [string]$row['is_read_only'],
            [string]$row['is_in_standby']
        )
    }
    catch {
        Write-CollectorMessage "Unable to query current state for transitioning database '$DatabaseName': $($_.Exception.Message)"
    }
}

function Clear-InstanceExportTempRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

function Export-InstanceConfiguration {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [string[]]$AdditionalExcludes,
        [Parameter(Mandatory = $true)][bool]$HadrEnabled
    )

    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("configbackup-dbatools-" + [guid]::NewGuid().ToString('N'))
    [System.IO.Directory]::CreateDirectory($tempRoot) | Out-Null

    $previousExportPath = Get-DbatoolsConfigValue -FullName 'Path.DbatoolsExport'
    try {
        Set-DbatoolsConfig -FullName 'Path.DbatoolsExport' -Value $tempRoot | Out-Null
        # Databases are handled by the inventory/SqlPackage path below. SQL Agent is
        # exported separately by Export-SqlAgentConfiguration. Availability Groups are
        # also excluded from the broad Export-DbaInstance pass because dbatools treats
        # "HADR not configured" as an exception when -EnableException is used. We
        # conditionally export AGs below only when SERVERPROPERTY('IsHadrEnabled') = 1.
        $requestedExcludes = @(Expand-NameList $AdditionalExcludes)
        $skipAvailabilityGroups = ($requestedExcludes -contains 'AvailabilityGroups')
        $excludes = @('Databases', 'AgentServer', 'AvailabilityGroups', 'SpConfigure') + $requestedExcludes
        $excludes = @($excludes | Select-Object -Unique)

        Write-CollectorMessage 'Exporting SQL Server instance configuration with dbatools'
        Write-CollectorMessage ("dbatools instance export excludes: {0}" -f ($excludes -join ', '))
        $exportArgs = @{
            SqlInstance     = $ServerObject
            Path            = $tempRoot
            Force           = $true
            NoPrefix        = $true
            ExcludePassword = $true
            Exclude         = $excludes
            EnableException = $true
            Verbose         = $true
        }

        $files = @()
        $maxAttempts = 1 + $InstanceExportTransitionRetries
        for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
            Clear-InstanceExportTempRoot -Path $tempRoot
            try {
                if ($maxAttempts -gt 1) {
                    Write-CollectorMessage "Export-DbaInstance attempt $attempt of $maxAttempts"
                }
                $files = @(Export-DbaInstance @exportArgs)
                break
            }
            catch {
                $transitionDatabase = Get-TransitionDatabaseName -ErrorRecord $_
                $canRetryTransition = (
                    -not [string]::IsNullOrWhiteSpace($transitionDatabase) -and
                    $attempt -lt $maxAttempts
                )

                if ($canRetryTransition) {
                    Write-CollectorMessage (
                        "Transient database transition detected during instance export: '{0}'. " +
                        "Discarding partial output and retrying in {1} second(s) ({2}/{3})." -f
                        $transitionDatabase,
                        $InstanceExportTransitionDelaySeconds,
                        $attempt,
                        $maxAttempts
                    )
                    Write-TransitionDatabaseState -ServerObject $ServerObject -DatabaseName $transitionDatabase
                    try { $ServerObject.Databases.Refresh() } catch { }
                    if ($InstanceExportTransitionDelaySeconds -gt 0) {
                        Start-Sleep -Seconds $InstanceExportTransitionDelaySeconds
                    }
                    continue
                }

                Write-CollectorError ("Export-DbaInstance failed: {0}" -f $_.Exception.Message)
                if (-not [string]::IsNullOrWhiteSpace($transitionDatabase)) {
                    Write-CollectorError (
                        "Database '{0}' remained in transition after {1} attempt(s)." -f
                        $transitionDatabase,
                        $attempt
                    )
                    Write-TransitionDatabaseState -ServerObject $ServerObject -DatabaseName $transitionDatabase
                }
                if ($null -ne $_.CategoryInfo) {
                    Write-CollectorError ("Category: {0}" -f $_.CategoryInfo.ToString())
                }
                if (-not [string]::IsNullOrWhiteSpace($_.FullyQualifiedErrorId)) {
                    Write-CollectorError ("FullyQualifiedErrorId: {0}" -f $_.FullyQualifiedErrorId)
                }
                if ($null -ne $_.InvocationInfo -and -not [string]::IsNullOrWhiteSpace($_.InvocationInfo.PositionMessage)) {
                    Write-CollectorError $_.InvocationInfo.PositionMessage.Trim()
                }
                if (-not [string]::IsNullOrWhiteSpace($_.ScriptStackTrace)) {
                    Write-CollectorError $_.ScriptStackTrace
                }
                throw
            }
        }

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

        # Export-DbaSpConfigure temporarily changes 'show advanced options'.
        # Read the catalog instead: collection must never reconfigure the source.
        if ($requestedExcludes -notcontains 'SpConfigure') {
            $settings = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'master' -Query 'SELECT name,value FROM sys.configurations ORDER BY name;'
            $lines = @('-- Configured values; review before applying to another instance.', "EXEC sys.sp_configure N'show advanced options', 1;", 'RECONFIGURE;')
            foreach ($setting in $settings.Rows) {
                if ($setting.name -ne 'show advanced options') {
                    $lines += ('EXEC sys.sp_configure {0}, {1};' -f (Get-SqlLiteral $setting.name), ([string]$setting.value))
                }
            }
            $lines += 'RECONFIGURE;'
            $advanced = @($settings.Rows | Where-Object { $_.name -eq 'show advanced options' })[0].value
            $lines += "EXEC sys.sp_configure N'show advanced options', $advanced;"
            $lines += 'RECONFIGURE;'
            Write-Utf8Text -Path (Join-Path $TargetDirectory 'sp_configure.sql') -Text (($lines -join "`n") + "`n")
        }
        Write-CollectorMessage ("Instance export created {0} file(s)" -f $fileInfos.Count)

        if (-not $skipAvailabilityGroups) {
            Export-AvailabilityGroupConfiguration -ServerObject $ServerObject -TargetDirectory $TargetDirectory -HadrEnabled:$HadrEnabled
        }
        else {
            Write-CollectorMessage 'Skipping Availability Groups because InstanceExclude contains AvailabilityGroups'
        }
    }
    finally {
        Set-DbatoolsConfig -FullName 'Path.DbatoolsExport' -Value $previousExportPath | Out-Null
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}


function Test-IsLinuxRuntime {
    try {
        return [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Linux)
    }
    catch {
        return $false
    }
}

function Get-ComparableHostName {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $v = $Value.Trim().TrimEnd('.').ToLowerInvariant()
    if ($v.Contains('\')) { $v = $v.Split('\')[0] }
    if ($v.Contains(',')) { $v = $v.Split(',')[0] }
    if ($v.StartsWith('[') -and $v.Contains(']')) { $v = $v.Trim([char[]]'[]') }
    return $v
}

function Test-IsLocalSqlHost {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$SqlInstanceInput
    )

    if ($CollectLocalHostConfiguration) { return $true }
    if (-not (Test-IsLinuxRuntime)) { return $false }

    $localNames = New-Object -TypeName 'System.Collections.Generic.HashSet[string]' -ArgumentList ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($name in @('localhost', '127.0.0.1', '::1', [System.Net.Dns]::GetHostName())) {
        $candidate = Get-ComparableHostName $name
        if ($candidate) {
            $null = $localNames.Add($candidate)
            if ($candidate.Contains('.')) { $null = $localNames.Add($candidate.Split('.')[0]) }
        }
    }
    try {
        $fqdn = [System.Net.Dns]::GetHostEntry([System.Net.Dns]::GetHostName()).HostName
        $candidate = Get-ComparableHostName $fqdn
        if ($candidate) {
            $null = $localNames.Add($candidate)
            if ($candidate.Contains('.')) { $null = $localNames.Add($candidate.Split('.')[0]) }
        }
    }
    catch { }

    foreach ($value in @(
        $SqlInstanceInput,
        (Get-ObjectPropertyValue $ServerObject 'Name'),
        (Get-ObjectPropertyValue $ServerObject 'NetName'),
        (Get-ObjectPropertyValue $ServerObject 'ComputerNamePhysicalNetBIOS'),
        (Get-ObjectPropertyValue $ServerObject 'DomainInstanceName')
    )) {
        $candidate = Get-ComparableHostName ([string]$value)
        if (-not $candidate) { continue }
        if ($localNames.Contains($candidate)) { return $true }
        if ($candidate.Contains('.') -and $localNames.Contains($candidate.Split('.')[0])) { return $true }
    }
    return $false
}

function Convert-MssqlConfToRows {
    param([Parameter(Mandatory = $true)][string]$Path)
    $rows = @()
    $section = ''
    foreach ($raw in [System.IO.File]::ReadAllLines($Path)) {
        $line = $raw.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#') -or $line.StartsWith(';')) { continue }
        if ($line -match '^\[(.+)\]$') {
            $section = $Matches[1].Trim()
            continue
        }
        $idx = $line.IndexOf('=')
        if ($idx -lt 0) { continue }
        $key = $line.Substring(0, $idx).Trim()
        $value = $line.Substring($idx + 1).Trim()
        $sensitive = ($key -match '(?i)(password|passwd|secret|token)')
        if ($sensitive) { $value = '<REDACTED>' }
        $rows += [pscustomobject][ordered]@{
            Section = $section
            Key = $key
            Value = $value
            SensitiveValueRedacted = $sensitive
        }
    }
    return @($rows | Sort-Object Section, Key)
}

function Get-LinuxSqlPackageRows {
    $rows = @()
    $dpkg = Get-Command dpkg-query -ErrorAction SilentlyContinue
    if ($null -ne $dpkg) {
        try {
            $lines = @(& $dpkg.Source -W '-f=${Package}\t${Version}\t${Architecture}\n' 2>$null)
            foreach ($line in $lines) {
                $parts = [string]$line -split "`t", 3
                if ($parts.Count -lt 2) { continue }
                if ($parts[0] -notmatch '^(mssql|msodbcsql)') { continue }
                $rows += [pscustomobject][ordered]@{ Manager='dpkg'; Name=$parts[0]; Version=$parts[1]; Architecture=if ($parts.Count -gt 2) {$parts[2]} else {$null} }
            }
            return @($rows | Sort-Object Name, Version)
        }
        catch { }
    }

    $rpm = Get-Command rpm -ErrorAction SilentlyContinue
    if ($null -ne $rpm) {
        try {
            $lines = @(& $rpm.Source -qa --qf "%{NAME}`t%{VERSION}-%{RELEASE}`t%{ARCH}`n" 2>$null)
            foreach ($line in $lines) {
                $parts = [string]$line -split "`t", 3
                if ($parts.Count -lt 2) { continue }
                if ($parts[0] -notmatch '^(mssql|msodbcsql)') { continue }
                $rows += [pscustomobject][ordered]@{ Manager='rpm'; Name=$parts[0]; Version=$parts[1]; Architecture=if ($parts.Count -gt 2) {$parts[2]} else {$null} }
            }
        }
        catch { }
    }
    return @($rows | Sort-Object Name, Version)
}

function Invoke-ExternalTextCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    $cmd = Get-Command $Executable -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { return $null }
    try {
        $output = @(& $cmd.Source @Arguments 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($output | ForEach-Object { [string]$_ }) -join "`n").TrimEnd() + "`n"
    }
    catch {
        return $null
    }
}

function Export-LinuxSqlHostConfiguration {
    param([Parameter(Mandatory = $true)][string]$TargetDirectory)

    Write-CollectorMessage 'Collecting local SQL Server on Linux host configuration'
    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null

    $configPath = '/var/opt/mssql/mssql.conf'
    if (Test-Path -LiteralPath $configPath) {
        Copy-Item -LiteralPath $configPath -Destination (Join-Path $TargetDirectory 'mssql.conf') -Force
        Write-StableCsv -Path (Join-Path $TargetDirectory 'mssql-settings.csv') -Rows (Convert-MssqlConfToRows -Path $configPath)
    }
    else {
        Write-CollectorMessage 'Linux SQL host: /var/opt/mssql/mssql.conf not found'
        Write-StableCsv -Path (Join-Path $TargetDirectory 'mssql-settings.csv') -Rows @()
    }

    Write-StableCsv -Path (Join-Path $TargetDirectory 'packages.csv') -Rows (Get-LinuxSqlPackageRows)

    $systemdCat = Invoke-ExternalTextCommand -Executable 'systemctl' -Arguments @('cat', 'mssql-server.service')
    if ($null -ne $systemdCat) {
        Write-Utf8Text -Path (Join-Path $TargetDirectory 'mssql-server.service.txt') -Text $systemdCat
    }

    $showText = Invoke-ExternalTextCommand -Executable 'systemctl' -Arguments @(
        'show', 'mssql-server.service',
        '--property=LoadState,UnitFileState,FragmentPath,DropInPaths,User,Group,ExecStart,EnvironmentFiles'
    )
    $service = [ordered]@{}
    if ($null -ne $showText) {
        foreach ($line in ($showText -split "`n")) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $idx = $line.IndexOf('=')
            if ($idx -lt 0) { continue }
            $service[$line.Substring(0,$idx)] = $line.Substring($idx+1)
        }
    }
    Write-StableJson -Path (Join-Path $TargetDirectory 'service.json') -Value $service

    $paths = [ordered]@{
        MssqlRoot = '/var/opt/mssql'
        ConfigFile = $configPath
        DefaultDataDirectory = '/var/opt/mssql/data'
        DefaultLogDirectory = '/var/opt/mssql/log'
        DefaultBackupDirectory = '/var/opt/mssql/data'
        MssqlConfExecutable = '/opt/mssql/bin/mssql-conf'
    }
    Write-StableJson -Path (Join-Path $TargetDirectory 'paths.json') -Value $paths
}

function Get-SsisFolderIdentifierColumn {
    param([Parameter(Mandatory = $true)]$ServerObject)
    Write-CollectorMessage 'SSISDB preflight: detecting catalog.folders identifier column'
    $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query @'
SELECT c.name
FROM sys.columns c
JOIN sys.objects o ON c.object_id = o.object_id
JOIN sys.schemas s ON o.schema_id = s.schema_id
WHERE s.name = N'catalog'
  AND o.name = N'folders'
  AND c.name IN (N'folder_id', N'id')
ORDER BY CASE c.name WHEN N'folder_id' THEN 0 ELSE 1 END;
'@
    if ($null -eq $table -or $table.Rows.Count -eq 0) {
        throw 'Unable to determine the identifier column exposed by SSISDB catalog.folders (expected folder_id or id).'
    }
    $name = [string]$table.Rows[0]['name']
    if ($name -notin @('folder_id','id')) { throw "Unexpected SSISDB catalog.folders identifier column '$name'." }
    Write-CollectorMessage "SSISDB catalog.folders identifier column: $name"
    return $name
}


function Get-SqlLiteral {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return 'NULL' }
    return "N'" + $Value.Replace("'", "''") + "'"
}

function Convert-DataTableRows {
    param([Parameter(Mandatory = $true)][AllowNull()]$Table)
    if ($null -eq $Table) { return @() }
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

    # Use dbatools' supported query execution path rather than calling SMO
    # SMO ExecuteWithResults directly.  Some environments can query
    # SSISDB successfully through Invoke-DbaQuery while the reused SMO connection
    # fails when changing database context.  -As DataSet preserves the DataTable
    # shape expected by the rest of this collector, including varbinary(max)
    # project streams returned by SSISDB catalog.get_project.
    $ds = Invoke-DbaQuery `
        -SqlInstance $ServerObject `
        -Database $DatabaseName `
        -Query $Query `
        -As DataSet `
        -EnableException

    if ($null -eq $ds -or $ds.Tables.Count -eq 0) { return $null }

    # A DataTable is enumerable. Returning it normally through the PowerShell
    # pipeline can unwrap it into DataRow objects, which breaks callers that
    # expect .Rows/.Columns. Preserve the DataTable as a single object.
    Write-Output -NoEnumerate $ds.Tables[0]
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
            throw "Unable to script SQL Agent job '$($job.Name)': $($_.Exception.Message)"
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
            throw "Unable to collect SQL Agent $($spec.Name): $($_.Exception.Message)"
        }
    }
}

function Export-SsisConfiguration {
    param(
        [Parameter(Mandatory = $true)]$ServerObject,
        [Parameter(Mandatory = $true)][string]$TargetDirectory,
        [switch]$SkipIspacFiles,
        [switch]$IncludeLegacy,
        [switch]$AllowPartial
    )

    if ($null -eq $ServerObject.Databases['SSISDB']) {
        throw 'SSISDB is absent or not visible; previous SSIS snapshot preserved'
    }
    else {
        Write-CollectorMessage 'Collecting SSISDB projects, packages, parameters, environments, and references'
        [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null

        # SSISDB catalog views enforce row-level security. A complete, deletion-safe
        # snapshot can only be guaranteed for sysadmin or SSISDB ssis_admin. Do the
        # preflight in SSISDB itself. If USE [SSISDB] succeeds, database access is
        # proven directly; this avoids an unnecessary master/HAS_DBACCESS dependency.
        Write-CollectorMessage 'SSISDB preflight: checking database status and access'
        $ssisDatabase = $ServerObject.Databases['SSISDB']
        $databaseStatus = Convert-ToStableString (Get-ObjectPropertyValue $ssisDatabase 'Status')
        Write-CollectorMessage "SSISDB preflight: SMO status=$databaseStatus"
        Write-CollectorMessage 'SSISDB preflight: probing SSISDB and checking sysadmin/ssis_admin membership'
        try {
            $roleTable = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query @'
SELECT
    DB_NAME() AS database_name,
    IS_SRVROLEMEMBER(N'sysadmin') AS is_sysadmin,
    IS_ROLEMEMBER(N'ssis_admin') AS is_ssis_admin,
    ORIGINAL_LOGIN() AS original_login,
    SUSER_SNAME() AS login_name,
    USER_NAME() AS database_user;
'@
        }
        catch {
            Write-CollectorError ("SSISDB preflight direct-access query failed: {0}" -f $_.Exception.Message)
            if ($null -ne $_.CategoryInfo) {
                Write-CollectorError ("Category: {0}" -f $_.CategoryInfo.ToString())
            }
            if (-not [string]::IsNullOrWhiteSpace($_.FullyQualifiedErrorId)) {
                Write-CollectorError ("FullyQualifiedErrorId: {0}" -f $_.FullyQualifiedErrorId)
            }
            if ($null -ne $_.InvocationInfo -and -not [string]::IsNullOrWhiteSpace($_.InvocationInfo.PositionMessage)) {
                Write-CollectorError $_.InvocationInfo.PositionMessage.Trim()
            }
            if (-not [string]::IsNullOrWhiteSpace($_.ScriptStackTrace)) {
                Write-CollectorError $_.ScriptStackTrace
            }
            throw "SSISDB exists but the current SQL principal could not query it directly: $($_.Exception.Message)"
        }
        if ($null -eq $roleTable -or $roleTable.Rows.Count -eq 0) {
            throw 'SSISDB preflight direct-access query returned no row.'
        }

        $databaseName = [string]$roleTable.Rows[0]['database_name']
        if ($databaseName -ne 'SSISDB') {
            throw "SSISDB preflight unexpectedly executed in database '$databaseName'."
        }

        $isSysadmin = 0
        $isSsisAdmin = 0
        if ($roleTable.Rows[0]['is_sysadmin'] -isnot [System.DBNull] -and $null -ne $roleTable.Rows[0]['is_sysadmin']) {
            $isSysadmin = [int]$roleTable.Rows[0]['is_sysadmin']
        }
        if ($roleTable.Rows[0]['is_ssis_admin'] -isnot [System.DBNull] -and $null -ne $roleTable.Rows[0]['is_ssis_admin']) {
            $isSsisAdmin = [int]$roleTable.Rows[0]['is_ssis_admin']
        }
        $fullCatalogAccess = ($isSysadmin -eq 1 -or $isSsisAdmin -eq 1)
        $originalLogin = [string]$roleTable.Rows[0]['original_login']
        $loginName = [string]$roleTable.Rows[0]['login_name']
        $databaseUser = [string]$roleTable.Rows[0]['database_user']
        Write-CollectorMessage "SSISDB preflight: database=$databaseName login=$loginName database_user=$databaseUser is_sysadmin=$isSysadmin is_ssis_admin=$isSsisAdmin full_catalog_visibility=$fullCatalogAccess"
        Write-StableJson -Path (Join-Path $TargetDirectory 'access.json') -Value ([ordered]@{
            DatabaseStatus = $databaseStatus
            HasDatabaseAccess = $true
            OriginalLogin = $originalLogin
            LoginName = $loginName
            DatabaseUser = $databaseUser
            IsSysadmin = ($isSysadmin -eq 1)
            IsSsisAdmin = ($isSsisAdmin -eq 1)
            FullCatalogVisibility = $fullCatalogAccess
            PartialMode = [bool]$AllowPartial
        })

        if (-not $fullCatalogAccess -and -not $AllowPartial) {
            throw 'SSISDB is accessible, but the current principal is neither sysadmin nor a member of SSISDB role ssis_admin. A complete SSIS snapshot cannot be guaranteed because SSIS catalog views use row-level security. Grant appropriate SSISDB access, use -SkipSsis, or explicitly use -AllowPartialSsis if a visibility-limited snapshot is acceptable.'
        }
        if (-not $fullCatalogAccess -and $AllowPartial) {
            Write-CollectorMessage 'WARNING: SSIS partial mode is enabled; only objects visible to the current principal will be collected. Permission changes can look like deletions.'
        }

        $folderIdColumn = Get-SsisFolderIdentifierColumn -ServerObject $ServerObject
        $folderIdSql = '[' + $folderIdColumn + ']'

        $querySpecs = @(
            [pscustomobject]@{ Name='catalog-properties'; File='catalog-properties.csv'; RequiresFull=$true; Query='SELECT property_name, property_value FROM catalog.catalog_properties ORDER BY property_name;' },
            [pscustomobject]@{ Name='folders'; File='folders.csv'; RequiresFull=$false; Query="SELECT $folderIdSql AS folder_id,name,description,created_by_name,created_time FROM catalog.folders ORDER BY name;" },
            [pscustomobject]@{ Name='projects'; File='projects.csv'; RequiresFull=$false; Query='SELECT project_id,folder_id,name,description,project_format_version,deployed_by_name,last_deployed_time,created_time,object_version_lsn FROM catalog.projects ORDER BY folder_id,name;' },
            [pscustomobject]@{ Name='packages'; File='packages.csv'; RequiresFull=$false; Query='SELECT p.package_id,p.project_id,p.name,p.package_guid,p.description,p.package_format_version,p.version_major,p.version_minor,p.version_build,p.version_comments FROM catalog.packages p ORDER BY p.project_id,p.name;' },
            [pscustomobject]@{ Name='environment-references'; File='environment-references.csv'; RequiresFull=$false; Query='SELECT reference_id,project_id,reference_type,environment_folder_name,environment_name FROM catalog.environment_references ORDER BY project_id,reference_id;' },
            [pscustomobject]@{ Name='environments'; File='environments.csv'; RequiresFull=$false; Query='SELECT environment_id,folder_id,name,description,created_by_name,created_time FROM catalog.environments ORDER BY folder_id,name;' },
            [pscustomobject]@{ Name='environment-variables'; File='environment-variables.csv'; RequiresFull=$false; Query="SELECT environment_id,name,description,type,sensitive,CASE WHEN sensitive=1 THEN N'<REDACTED>' ELSE CONVERT(nvarchar(max),value) END AS value FROM catalog.environment_variables ORDER BY environment_id,name;" },
            [pscustomobject]@{ Name='object-parameters'; File='object-parameters.csv'; RequiresFull=$false; Query="SELECT project_id,object_type,object_name,parameter_name,data_type,required,sensitive,description,CASE WHEN sensitive=1 THEN N'<REDACTED>' ELSE CONVERT(nvarchar(max),design_default_value) END AS design_default_value,CASE WHEN sensitive=1 THEN N'<REDACTED>' ELSE CONVERT(nvarchar(max),default_value) END AS default_value,value_type,value_set,referenced_variable_name FROM catalog.object_parameters ORDER BY project_id,object_type,object_name,parameter_name;" },
            [pscustomobject]@{ Name='explicit-object-permissions'; File='explicit-object-permissions.csv'; RequiresFull=$false; Query='SELECT e.object_type,e.object_id,e.principal_id,p.name AS principal_name,p.type_desc AS principal_type,e.permission_type,e.is_deny,e.grantor_id,g.name AS grantor_name FROM catalog.explicit_object_permissions e LEFT JOIN sys.database_principals p ON e.principal_id=p.principal_id LEFT JOIN sys.database_principals g ON e.grantor_id=g.principal_id ORDER BY e.object_type,e.object_id,p.name,e.permission_type;' },
            [pscustomobject]@{ Name='database-role-memberships'; File='database-role-memberships.csv'; RequiresFull=$false; Query='SELECT rp.name AS role_name,mp.name AS member_name,mp.type_desc AS member_type FROM sys.database_role_members drm JOIN sys.database_principals rp ON drm.role_principal_id=rp.principal_id JOIN sys.database_principals mp ON drm.member_principal_id=mp.principal_id ORDER BY rp.name,mp.name;' }
        )

        foreach ($spec in $querySpecs) {
            if ($spec.RequiresFull -and -not $fullCatalogAccess) {
                Write-CollectorMessage "SSIS metadata: skipping $($spec.Name) because full SSIS catalog visibility is unavailable"
                continue
            }
            Write-CollectorMessage "SSIS metadata: $($spec.Name)"
            try {
                $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query $spec.Query
                $rowCount = if ($null -eq $table) { 0 } else { $table.Rows.Count }
                Write-CollectorMessage "SSIS metadata: $($spec.Name) -> $rowCount row(s)"
                if ($rowCount -gt 0) {
                    Write-StableCsv -Path (Join-Path $TargetDirectory $spec.File) -Rows (Convert-DataTableRows $table)
                }
                else {
                    Write-StableCsv -Path (Join-Path $TargetDirectory $spec.File) -Rows @()
                }
            }
            catch {
                throw "SSIS metadata query '$($spec.Name)' failed: $($_.Exception.Message)"
            }
        }

        Write-CollectorMessage 'SSIS projects: discovering visible deployed projects'
        try {
            $projectTable = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query @"
SELECT p.project_id,p.name AS project_name,f.name AS folder_name
FROM catalog.projects p JOIN catalog.folders f ON p.folder_id=f.$folderIdSql
ORDER BY f.name,p.name;
"@
        }
        catch {
            throw "SSIS project discovery failed: $($_.Exception.Message)"
        }

        $projectRows = if ($null -eq $projectTable) { @() } else { @($projectTable.Rows) }
        Write-CollectorMessage "SSIS projects: $($projectRows.Count) visible project(s)"
        foreach ($row in $projectRows) {
            $folderName = [string]$row['folder_name']
            $projectName = [string]$row['project_name']
            Write-CollectorMessage "SSIS project export: $folderName/$projectName"
            try {
                $folderDir = Get-SafePathSegment -Value $folderName
                $projectDir = Get-SafePathSegment -Value $projectName
                $targetProject = Join-Path (Join-Path (Join-Path $TargetDirectory 'projects') $folderDir) $projectDir
                [System.IO.Directory]::CreateDirectory($targetProject) | Out-Null
                $folderLit = Get-SqlLiteral $folderName
                $projectLit = Get-SqlLiteral $projectName
                $streamTable = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query "EXEC catalog.get_project @folder_name=$folderLit, @project_name=$projectLit;"
                if ($null -eq $streamTable -or $streamTable.Rows.Count -eq 0) { throw 'catalog.get_project returned no project stream' }
                $projectBytes = $streamTable.Rows[0][0]
                if ($null -eq $projectBytes) { throw 'catalog.get_project returned a null project stream' }
                if ($projectBytes -isnot [byte[]]) { throw "catalog.get_project returned unexpected stream type '$($projectBytes.GetType().FullName)'" }
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
            catch {
                throw "SSIS project export failed for '$folderName/$projectName': $($_.Exception.Message)"
            }
        }
        Write-CollectorMessage 'SSISDB project-deployment collection completed'
    }

    if ($IncludeLegacy) {
        Write-CollectorMessage 'Collecting legacy MSDB SSIS package metadata and package data'
        $legacyDir = Join-Path $TargetDirectory 'legacy-msdb'
        [System.IO.Directory]::CreateDirectory($legacyDir) | Out-Null
        try {
            $legacy = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'msdb' -Query 'SELECT id,name,description,folderid,ownersid,packagedata,packageformat FROM dbo.sysssispackages ORDER BY name,id;'
        }
        catch {
            throw "Legacy MSDB SSIS package query failed: $($_.Exception.Message)"
        }
        $metadata = @()
        $legacyRows = if ($null -eq $legacy) { @() } else { @($legacy.Rows) }
        foreach ($row in $legacyRows) {
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
        Write-CollectorMessage "Legacy SSIS: $($legacyRows.Count) package(s) collected"
    }
}

# A section is eligible only after all its outputs have been produced and hashed.
$script:CollectionSections = @()
$script:HadrDetectionError = $null
$script:RequireServerVisibility = $false
$script:IsSqlSysadmin = $false
$script:ReadOnlyServerVisible = $false
function Save-CollectionManifest {
    param([bool]$Finalized = $false)
    $payload = [ordered]@{schema_version=1; collected_at=[DateTime]::UtcNow.ToString('o'); run_id=[string]$env:CONFIGBACKUP_RUN_ID; finalized=$Finalized; sections=@($script:CollectionSections)}
    $temp = Join-Path $OutputDirectory 'collection-manifest.json.tmp'
    Write-StableJson -Path $temp -Value $payload -Depth 20
    Move-Item -LiteralPath $temp -Destination (Join-Path $OutputDirectory 'collection-manifest.json') -Force
}
function Test-SectionEnabled {
    param([string]$Name)
    if (-not $env:CONFIGBACKUP_SECTIONS) { return $true }
    $settings = $env:CONFIGBACKUP_SECTIONS | ConvertFrom-Json
    foreach ($property in $settings.PSObject.Properties) {
        $value=$property.Value
        $active=if ($value -is [bool]) { $value } elseif ($null -ne $value.PSObject.Properties['enabled']) { $value.enabled } else { $true }
        if (-not $active -and ($Name -like $property.Name -or $Name -like ($property.Name.TrimEnd('/')+'/*'))) { return $false }
    }
    return $true
}
function Invoke-CollectionSection {
    param([string]$Path, [scriptblock]$Action)
    if (-not (Test-SectionEnabled $Path)) {
        $script:CollectionSections += [ordered]@{path=$Path;status='disabled';error='Disabled by configuration';files=@{}}
        Save-CollectionManifest; return
    }
    $status = 'complete'; $errorText = ''; $files = [ordered]@{}
    try {
        if ($script:RequireServerVisibility -and -not $script:IsSqlSysadmin -and $Path -match '^instance/(catalog|scripts|agent|ssis)(/|$)') {
            if (-not ($ReadOnlyAccess -and $script:ReadOnlyServerVisible -and $Path -match '^instance/(catalog|agent)/' -and $Path -notmatch '^instance/agent/jobs(/|$)')) {
                throw 'Complete visibility unavailable with this account; privileged scripts/services preserved. ReadOnlyAccess supports checked catalogs and explicit Agent catalog reads.'
            }
        }
        & $Action
        $target = Join-Path $OutputDirectory $Path
        if (Test-Path -LiteralPath $target) {
            $items = if (Test-Path -LiteralPath $target -PathType Leaf) { @(Get-Item -LiteralPath $target) } else { @(Get-ChildItem -LiteralPath $target -Recurse -File) }
            foreach ($file in $items | Sort-Object FullName) {
                if ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are not valid collector output' }
                $relative = $file.FullName.Substring($OutputDirectory.Length).TrimStart([char[]]'\/').Replace('\','/')
                $files[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    }
    catch {
        $status = 'failed'; $errorText = $_.Exception.Message; $files = [ordered]@{}
        Write-CollectorError "Section '$Path' failed; previous archive/Git snapshot will be preserved: $errorText"
    }
    $script:CollectionSections += [ordered]@{path=$Path;status=$status;error=$errorText;files=$files}
    Save-CollectionManifest
}

try {
    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        throw 'OutputDirectory was not specified and CONFIGBACKUP_OUTPUT is not set.'
    }

    $OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
    [System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
    if (@(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count -gt 0) { throw 'Output directory must be empty; use clean_output: true or a fresh directory' }
    Save-CollectionManifest

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
    $script:SqlPackageError = $null
    if (-not $SkipSchema -and (Test-SectionEnabled 'schema')) {
      try {
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
      } catch { $script:SqlPackageError = $_.Exception.Message; Write-CollectorError "Schema extraction unavailable: $script:SqlPackageError" }
    }

    Assert-SafeAppendConnectionString -Value $AppendConnectionString

    $connectArgs = @{
        SqlInstance    = $SqlInstance
        ClientName     = 'ConfigBackup.SqlCollector'
        ConnectTimeout = $ConnectTimeout
    }
    if (-not [string]::IsNullOrWhiteSpace($AppendConnectionString)) {
        $connectArgs.AppendConnectionString = $AppendConnectionString.Trim().Trim(';') + ';'
    }
    if ($TrustServerCertificate) {
        $connectArgs.TrustServerCertificate = $true
    }

    if ($null -ne $SqlCredential) { $connectArgs.SqlCredential = $SqlCredential }

    Write-CollectorMessage 'Connecting with dbatools'
    $server = Connect-DbaInstance @connectArgs
    $visibility = Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query "SELECT IS_SRVROLEMEMBER(N'sysadmin') AS allowed;"
    $script:IsSqlSysadmin = ($visibility.Rows.Count -eq 1 -and [int]$visibility.Rows[0]['allowed'] -eq 1)
    $script:RequireServerVisibility = $true
    if ($ReadOnlyAccess) {
        $readVisibility = Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query "SELECT CASE WHEN HAS_PERMS_BY_NAME(NULL,NULL,'VIEW ANY DEFINITION')=1 AND HAS_PERMS_BY_NAME(NULL,NULL,'VIEW ANY DATABASE')=1 AND HAS_PERMS_BY_NAME(NULL,NULL,'VIEW SERVER STATE')=1 THEN 1 ELSE 0 END AS allowed;"
        $script:ReadOnlyServerVisible = ($readVisibility.Rows.Count -eq 1 -and [int]$readVisibility.Rows[0]['allowed'] -eq 1)
        if ([int]$server.VersionMajor -ge 16) {
            $extra = Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query "SELECT CASE WHEN HAS_PERMS_BY_NAME(NULL,NULL,'VIEW ANY SECURITY DEFINITION')=1 AND HAS_PERMS_BY_NAME(NULL,NULL,'VIEW SERVER PERFORMANCE STATE')=1 THEN 1 ELSE 0 END AS allowed;"
            $script:ReadOnlyServerVisible = $script:ReadOnlyServerVisible -and [int]$extra.Rows[0]['allowed'] -eq 1
        }
    }

    $requestedDatabases = @(Expand-NameList $Database)
    $excludedDatabases = @(Expand-NameList $ExcludeDatabase)

    $databaseArgs = @{
        SqlInstance     = $server
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

    foreach ($requested in $requestedDatabases) {
        if (-not ($databases | Where-Object { $_.Name -eq $requested })) {
            $script:CollectionSections += [ordered]@{path=('databases/' + (Get-SafePathSegment $requested));status='failed';error='Requested database was not discovered';files=@{}}
        }
    }
    Write-CollectorMessage ("Selected {0} database(s)" -f $databases.Count)

    $hadrEnabled = Get-HadrEnabled -ServerObject $server
    Write-CollectorMessage ("HADR enabled: {0}" -f $hadrEnabled)

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
        IsHadrEnabled             = $hadrEnabled
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
        VerifySchemaExtraction = [bool]$VerifySchemaExtraction
        SortSchemaElementsByName = (-not [bool]$DisableSchemaElementSorting)
        InstanceExport        = (-not $SkipInstanceExport)
        InstanceExportTransitionRetries = $InstanceExportTransitionRetries
        InstanceExportTransitionDelaySeconds = $InstanceExportTransitionDelaySeconds
        Inventory             = (-not $SkipInventory)
        PasswordMaterial      = 'excluded'
        SqlAgent               = (-not $SkipAgent)
        Ssis                   = (-not $SkipSsis)
        IspacFiles             = (-not $SkipIspac)
        LegacySsis             = [bool]$IncludeLegacySsis
        SsisPartialMode         = [bool]$AllowPartialSsis
        HostConfiguration       = (-not $SkipHostConfiguration)
        ForceLocalHostConfig    = [bool]$CollectLocalHostConfiguration
    }
    Write-StableJson -Path (Join-Path $OutputDirectory 'collector.json') -Value $collectorInfo

    if (-not $SkipHostConfiguration) {
        Invoke-CollectionSection 'instance/host-linux' {
        $hostPlatform = [string](Get-ObjectPropertyValue $server 'HostPlatform')
        if ($hostPlatform -match '^(?i:Linux)$') {
            if (Test-IsLinuxRuntime) {
                $isLocalSqlHost = Test-IsLocalSqlHost -ServerObject $server -SqlInstanceInput $SqlInstance
                if ($isLocalSqlHost) {
                    Export-LinuxSqlHostConfiguration -TargetDirectory (Join-Path $instanceDirectory 'host-linux')
                }
                else {
                    Write-CollectorMessage 'SQL Server reports Linux, but it does not appear to be the local host; skipping host-level Linux files'
                }
            }
            else {
                Write-CollectorMessage 'SQL Server reports Linux, but the collector is not running on Linux; skipping host-level Linux files'
            }
        }
    }

    } # Host section
    if (-not (Test-Path -LiteralPath (Join-Path $instanceDirectory 'host-linux'))) {
        $script:CollectionSections = @($script:CollectionSections | Where-Object { $_.path -ne 'instance/host-linux' })
    }

    $databaseMap = @()
    $usedDatabaseDirectories = @{}
    $databaseInventory = @()
    $allFileGroupRows = @()
    $allSpaceRows = @()

    if (-not $SkipInventory -and (Test-SectionEnabled 'inventory')) {
        $instanceCatalogs = @{
            'configuration' = 'SELECT name,value,minimum,maximum,is_dynamic,is_advanced,description FROM sys.configurations ORDER BY name;'
            'features' = "SELECT CONVERT(nvarchar(128),SERVERPROPERTY('Edition')) AS edition, CONVERT(nvarchar(128),SERVERPROPERTY('ProductVersion')) AS product_version, CONVERT(int,SERVERPROPERTY('IsFullTextInstalled')) AS fulltext_installed, CONVERT(int,SERVERPROPERTY('IsIntegratedSecurityOnly')) AS integrated_security_only, CONVERT(int,SERVERPROPERTY('IsHadrEnabled')) AS hadr_enabled, CONVERT(nvarchar(128),SERVERPROPERTY('FilestreamConfiguredLevel')) AS filestream_configured_level;"
            'database-features' = 'SELECT name,compatibility_level,collation_name,recovery_model_desc,containment_desc,is_read_only,is_auto_close_on,is_auto_shrink_on,is_published,is_subscribed,is_merge_published,is_distributor,is_cdc_enabled,is_broker_enabled,is_trustworthy_on,is_db_chaining_on,snapshot_isolation_state_desc,is_read_committed_snapshot_on,delayed_durability_desc FROM sys.databases ORDER BY name;'
            'cluster-identity' = "SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ServerName')) AS server_name,CONVERT(nvarchar(128),SERVERPROPERTY('MachineName')) AS virtual_machine_name,CONVERT(nvarchar(128),SERVERPROPERTY('InstanceName')) AS instance_name,CONVERT(nvarchar(128),SERVERPROPERTY('ComputerNamePhysicalNetBIOS')) AS physical_node,CONVERT(int,SERVERPROPERTY('IsClustered')) AS is_clustered,CONVERT(int,SERVERPROPERTY('IsHadrEnabled')) AS hadr_enabled;"
            'availability-groups' = 'SELECT name,automated_backup_preference_desc,failure_condition_level,health_check_timeout,db_failover,is_distributed,cluster_type_desc,required_synchronized_secondaries_to_commit FROM sys.availability_groups ORDER BY name;'
            'availability-replicas' = 'SELECT g.name AS group_name,r.replica_server_name,r.endpoint_url,r.availability_mode_desc,r.failover_mode_desc,r.session_timeout,r.primary_role_allow_connections_desc,r.secondary_role_allow_connections_desc,r.backup_priority,r.read_only_routing_url FROM sys.availability_replicas r JOIN sys.availability_groups g ON r.group_id=g.group_id ORDER BY g.name,r.replica_server_name;'
            'availability-databases' = 'SELECT g.name AS group_name,d.database_name FROM sys.availability_databases_cluster d JOIN sys.availability_groups g ON d.group_id=g.group_id ORDER BY g.name,d.database_name;'
            'availability-listeners' = 'SELECT g.name AS group_name,l.dns_name,l.port,l.is_conformant FROM sys.availability_group_listeners l JOIN sys.availability_groups g ON l.group_id=g.group_id ORDER BY g.name,l.dns_name;'
            'availability-listener-addresses' = 'SELECT g.name AS group_name,l.dns_name,a.ip_address,a.ip_subnet_mask,a.is_dhcp,a.network_subnet_ip,a.network_subnet_ipv4_mask,a.network_subnet_prefix_length FROM sys.availability_group_listener_ip_addresses a JOIN sys.availability_group_listeners l ON a.listener_id=l.listener_id JOIN sys.availability_groups g ON l.group_id=g.group_id ORDER BY g.name,l.dns_name,a.ip_address;'
            'availability-routing' = 'SELECT g.name AS group_name,r.replica_server_name,l.routing_priority,t.replica_server_name AS target_replica FROM sys.availability_read_only_routing_lists l JOIN sys.availability_replicas r ON l.replica_id=r.replica_id JOIN sys.availability_replicas t ON l.read_only_replica_id=t.replica_id JOIN sys.availability_groups g ON r.group_id=g.group_id ORDER BY g.name,r.replica_server_name,l.routing_priority,t.replica_server_name;'
            'services' = 'SELECT servicename,startup_type_desc,service_account,filename,is_clustered,cluster_nodename FROM sys.dm_server_services ORDER BY servicename;'
            'endpoints' = 'SELECT name,principal_id,protocol_desc,type_desc,state_desc,is_admin_endpoint FROM sys.endpoints ORDER BY name;'
            'tcp-endpoints' = 'SELECT e.name,t.port,t.ip_address FROM sys.tcp_endpoints t JOIN sys.endpoints e ON t.endpoint_id=e.endpoint_id ORDER BY e.name;'
            'linked-servers' = 'SELECT name,product,provider,data_source,location,catalog,is_linked,is_remote_login_enabled,is_rpc_out_enabled,is_data_access_enabled,is_collation_compatible,uses_remote_collation,collation_name,connect_timeout,query_timeout FROM sys.servers ORDER BY name;'
            'credentials' = 'SELECT name,credential_identity FROM sys.credentials ORDER BY name;'
            'server-principals' = "SELECT name,type_desc,is_disabled,default_database_name,default_language_name,CONVERT(varchar(max),sid,1) AS sid FROM sys.server_principals WHERE type NOT IN ('C','K') ORDER BY name;"
            'server-role-members' = 'SELECT r.name AS role_name,m.name AS member_name FROM sys.server_role_members rm JOIN sys.server_principals r ON rm.role_principal_id=r.principal_id JOIN sys.server_principals m ON rm.member_principal_id=m.principal_id ORDER BY r.name,m.name;'
            'server-permissions' = 'SELECT grantee.name AS grantee,grantor.name AS grantor,p.class_desc,p.major_id,p.permission_name,p.state_desc FROM sys.server_permissions p JOIN sys.server_principals grantee ON p.grantee_principal_id=grantee.principal_id JOIN sys.server_principals grantor ON p.grantor_principal_id=grantor.principal_id ORDER BY grantee.name,p.class_desc,p.major_id,p.permission_name;'
            'fulltext-languages' = 'SELECT lcid,name FROM sys.fulltext_languages ORDER BY lcid;'
            'fulltext-document-types' = 'SELECT document_type,class_id,path,version,manufacturer FROM sys.fulltext_document_types ORDER BY document_type;'
        }
        foreach ($catalogName in $instanceCatalogs.Keys | Sort-Object) {
            $catalogPath = "instance/catalog/$catalogName.csv"
            Invoke-CollectionSection $catalogPath {
                $rows = Convert-DataTableRows (Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query $instanceCatalogs[$catalogName])
                Write-StableCsv -Path (Join-Path $OutputDirectory $catalogPath) -Rows $rows
            }
        }
    }

    if (-not $SkipInstanceExport) {
        # One manifest boundary per component; an unsupported service cannot freeze
        # unrelated instance settings. Legacy flat script paths remain protected.
        $components = @('SpConfigure','CustomErrors','ServerRoles','Credentials','Logins','DatabaseMail','CentralManagementServer','BackupDevices','LinkedServers','SystemTriggers','Audits','ServerAuditSpecifications','Endpoints','PolicyManagement','ResourceGovernor','ExtendedEvents','ReplicationSettings','SysDbUserObjects','AvailabilityGroups','OleDbProvider')
        $allExportComponents = $components + @('Databases','AgentServer','DbCertificates')
        $requestedExcludes = @(Expand-NameList $InstanceExclude)
        foreach ($component in $components) {
            if ($requestedExcludes -contains $component) { continue }
            $componentPath = 'instance/scripts/' + $component
            Invoke-CollectionSection $componentPath {
                $target = Join-Path $OutputDirectory $componentPath
                if ($component -eq 'AvailabilityGroups' -and $script:HadrDetectionError) { throw $script:HadrDetectionError }
                if ($component -eq 'ReplicationSettings') {
                    # Export-DbaInstance swallows replication exceptions internally.
                    [System.IO.Directory]::CreateDirectory($target) | Out-Null
                    Export-DbaReplServerSetting -SqlInstance $server -Path $target -FilePath (Join-Path $target 'replication.sql') -EnableException | Out-Null
                } elseif ($component -eq 'PolicyManagement' -and ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT)) {
                    throw 'Policy Management scripting requires Windows; prior files preserved'
                } else {
                    $excludeOthers = @($allExportComponents | Where-Object { $_ -ne $component })
                    Export-InstanceConfiguration -ServerObject $server -TargetDirectory $target -AdditionalExcludes $excludeOthers -HadrEnabled:$hadrEnabled
                }
            }
        }
    }

    if (-not $SkipAgent) {
      if ($ReadOnlyAccess -and -not $script:IsSqlSysadmin) {
        # Direct base-table reads avoid SQLAgentReaderRole, which can create jobs.
        # File-level boundaries preserve existing privileged SMO job scripts.
        $readAgentQueries = [ordered]@{
            'jobs.csv' = "SELECT j.name AS Name,j.enabled AS Enabled,SUSER_SNAME(j.owner_sid) AS Owner,c.name AS Category,j.description AS Description,j.start_step_id AS StartStepID,(SELECT COUNT(*) FROM msdb.dbo.sysjobsteps x WHERE x.job_id=j.job_id) AS StepCount,(SELECT COUNT(*) FROM msdb.dbo.sysjobschedules x WHERE x.job_id=j.job_id) AS ScheduleCount FROM msdb.dbo.sysjobs j LEFT JOIN msdb.dbo.syscategories c ON j.category_id=c.category_id ORDER BY j.name;"
            'job-steps.csv' = 'SELECT j.name AS job_name,s.step_id,s.step_name,s.subsystem,s.command,s.database_name,s.on_success_action,s.on_success_step_id,s.on_fail_action,s.on_fail_step_id,s.retry_attempts,s.retry_interval,s.output_file_name,s.proxy_id FROM msdb.dbo.sysjobsteps s JOIN msdb.dbo.sysjobs j ON j.job_id=s.job_id ORDER BY j.name,s.step_id;'
            'schedules.csv' = 'SELECT schedule_id,schedule_uid,name,enabled,freq_type,freq_interval,freq_subday_type,freq_subday_interval,freq_relative_interval,freq_recurrence_factor,active_start_date,active_end_date,active_start_time,active_end_time FROM msdb.dbo.sysschedules ORDER BY name,schedule_id;'
            'schedule-jobs.csv' = 'SELECT s.name AS schedule_name,j.name AS job_name FROM msdb.dbo.sysjobschedules js JOIN msdb.dbo.sysschedules s ON js.schedule_id=s.schedule_id JOIN msdb.dbo.sysjobs j ON js.job_id=j.job_id ORDER BY s.name,j.name;'
            'operators.csv' = 'SELECT name,enabled,email_address,pager_address FROM msdb.dbo.sysoperators ORDER BY name;'
            'alerts.csv' = 'SELECT name,message_id,severity,enabled,delay_between_responses,include_event_description,database_name,notification_message FROM msdb.dbo.sysalerts ORDER BY name;'
            'categories.csv' = 'SELECT category_id,category_class,category_type,name FROM msdb.dbo.syscategories ORDER BY name,category_id;'
        }
        foreach ($filename in $readAgentQueries.Keys) {
            Invoke-CollectionSection ('instance/agent/'+$filename) {
                $table=Invoke-QueryTable -ServerObject $server -DatabaseName 'msdb' -Query $readAgentQueries[$filename]
                Write-StableCsv -Path (Join-Path $instanceDirectory ('agent/'+$filename)) -Rows (Convert-DataTableRows $table)
            }
        }
        $script:CollectionSections += [ordered]@{path='instance/agent/jobs';status='failed';error='Native Agent script export requires full service visibility; direct catalogs collected instead';files=@{}}
        $script:CollectionSections += [ordered]@{path='instance/agent/agent-settings.json';status='failed';error='Agent service settings are not certified by read-only catalog access';files=@{}}
      } else {
        Invoke-CollectionSection 'instance/agent' {
        Export-SqlAgentConfiguration -ServerObject $server -TargetDirectory (Join-Path $instanceDirectory 'agent')
        } # Agent config section
      }
        if ($IncludeAgentHistory -and (Test-SectionEnabled 'telemetry/history')) {
          try {
            # step_id=0 identifies the entire job execution (not each step).
            # Run date/time and duration are kept as raw SQL Agent integers;
            # schedule analyzer decodes HHMMSS even when hours exceed 23.
            $historyQuery = @"
SELECT TOP (20000)
    j.name AS job_name, h.job_id, h.step_id, h.step_name,
    h.run_date, h.run_time, h.run_duration, h.run_status
FROM msdb.dbo.sysjobhistory AS h
JOIN msdb.dbo.sysjobs AS j ON h.job_id=j.job_id
WHERE h.run_date >= CONVERT(int, CONVERT(varchar(8), DATEADD(day, -$AgentHistoryDays, GETDATE()), 112))
ORDER BY h.run_date DESC, h.run_time DESC, h.instance_id DESC;
"@
            Write-StableCsv -Path (Join-Path $OutputDirectory 'telemetry/agent/job-runs.csv') -Rows (Convert-DataTableRows (Invoke-QueryTable -ServerObject $server -DatabaseName 'msdb' -Query $historyQuery))
            $runningQuery = "SELECT j.name AS job_name,CONVERT(varchar(33),a.start_execution_date,126) AS start_time FROM msdb.dbo.sysjobactivity a JOIN msdb.dbo.sysjobs j ON a.job_id=j.job_id WHERE a.session_id=(SELECT MAX(session_id) FROM msdb.dbo.syssessions) AND a.start_execution_date IS NOT NULL AND a.stop_execution_date IS NULL ORDER BY j.name;"
            Write-StableCsv -Path (Join-Path $OutputDirectory 'telemetry/agent/running-jobs.csv') -Rows (Convert-DataTableRows (Invoke-QueryTable -ServerObject $server -DatabaseName 'msdb' -Query $runningQuery))
          }
          catch { Write-CollectorError "Agent history unavailable: $($_.Exception.Message)" }
        }
    }

    if (-not $SkipSsis) {
      try {
      $ssisVisibility = Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query "SELECT DB_ID(N'SSISDB') AS dbid, IS_SRVROLEMEMBER(N'sysadmin') AS is_sysadmin;"
      if (-not $IncludeLegacySsis -and $ssisVisibility.Rows.Count -eq 1 -and $ssisVisibility.Rows[0]['dbid'] -is [DBNull] -and [int]$ssisVisibility.Rows[0]['is_sysadmin'] -eq 1) {
        $script:CollectionSections += [ordered]@{path='instance/ssis';status='not_applicable';error='SSISDB is not installed';files=@{}}
      } else {
        Invoke-CollectionSection 'instance/ssis' {
        if ($AllowPartialSsis) { throw 'AllowPartialSsis cannot certify a complete archive section; collect with full visibility' }
        Export-SsisConfiguration -ServerObject $server -TargetDirectory (Join-Path $instanceDirectory 'ssis') -SkipIspacFiles:$SkipIspac -IncludeLegacy:$IncludeLegacySsis -AllowPartial:$AllowPartialSsis
    }

      }
      } catch {
        $script:CollectionSections += [ordered]@{path='instance/ssis';status='failed';error=$_.Exception.Message;files=@{}}
        Write-CollectorError ('SSIS discovery failed: ' + $_.Exception.Message)
      }
    } # SSIS section

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

        Invoke-CollectionSection "databases/$safeName" {
        $stateTable = Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query ("SELECT state_desc FROM sys.databases WHERE name=" + (Get-SqlLiteral $db.Name))
        if ($stateTable.Rows.Count -ne 1 -or [string]$stateTable.Rows[0]['state_desc'] -ne 'ONLINE') { throw 'Database is restoring, offline, transitional, or no longer visible' }
        $visibility = Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query "SELECT CASE WHEN USER_NAME()=N'dbo' OR IS_SRVROLEMEMBER(N'sysadmin')=1 THEN 1 ELSE 0 END AS can_view;"
        if ($ReadOnlyAccess) {
            $visibility=Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query "SELECT CASE WHEN HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DEFINITION')=1 AND HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','VIEW DATABASE STATE')=1 AND NOT EXISTS (SELECT 1 FROM sys.database_permissions p JOIN sys.user_token t ON p.grantee_principal_id=t.principal_id WHERE p.state='D' AND p.permission_name IN ('VIEW DEFINITION','SELECT','CONTROL')) THEN 1 ELSE 0 END AS can_view;"
            $writeAccess=Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query "SELECT 'table_write' AS kind,SCHEMA_NAME(schema_id)+'.'+name AS name FROM sys.tables WHERE is_ms_shipped=0 AND (HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(schema_id))+'.'+QUOTENAME(name),'OBJECT','INSERT')=1 OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(schema_id))+'.'+QUOTENAME(name),'OBJECT','UPDATE')=1 OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(schema_id))+'.'+QUOTENAME(name),'OBJECT','DELETE')=1 OR HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(schema_id))+'.'+QUOTENAME(name),'OBJECT','ALTER')=1) UNION ALL SELECT 'procedure_execute',SCHEMA_NAME(schema_id)+'.'+name FROM sys.procedures WHERE is_ms_shipped=0 AND HAS_PERMS_BY_NAME(QUOTENAME(SCHEMA_NAME(schema_id))+'.'+QUOTENAME(name),'OBJECT','EXECUTE')=1 UNION ALL SELECT 'database_write',DB_NAME() WHERE HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','CREATE TABLE')=1 OR HAS_PERMS_BY_NAME(DB_NAME(),'DATABASE','ALTER ANY SCHEMA')=1;"
            if ($writeAccess.Rows.Count -gt 0) { throw 'Read-only permission audit found write or application-procedure execution rights; review inherited permissions before using this profile' }
        }
        if ($visibility.Rows.Count -ne 1 -or [int]$visibility.Rows[0]['can_view'] -ne 1) { throw 'Run as dbo or sysadmin to certify complete metadata; lesser permissions can silently hide objects' }
        if (-not $SkipSchema -and (Test-SectionEnabled 'schema')) {
            if ($script:SqlPackageError) { throw $script:SqlPackageError }
            # DacFx writes object names to filesystem paths. Do not allow a
            # case-sensitive SQL schema to lose objects on Windows/default macOS.
            $objectNames = Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query "SELECT SCHEMA_NAME(schema_id) AS schema_name,name,type FROM sys.objects WHERE is_ms_shipped=0 UNION ALL SELECT N'',name,N'SCHEMA' FROM sys.schemas;"
            $portableNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
            foreach ($objectName in $objectNames.Rows) {
                $identity = [string]$objectName.schema_name + '/' + [string]$objectName.type + '/' + [string]$objectName.name
                if (-not $portableNames.Add($identity)) { throw 'Case-colliding SQL object names cannot be safely extracted to portable files' }
            }
            $unreadable = Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query 'SELECT COUNT(*) AS unreadable FROM sys.sql_modules m JOIN sys.objects o ON m.object_id=o.object_id WHERE m.definition IS NULL AND o.is_ms_shipped=0;'
            if ([int]$unreadable.Rows[0]['unreadable'] -gt 0) { throw 'Encrypted or unreadable SQL modules prevent a complete schema export' }
        }
        $dbDirectory = Join-Path $databaseRoot $safeName
        [System.IO.Directory]::CreateDirectory($dbDirectory) | Out-Null

        if (-not $SkipInventory -and (Test-SectionEnabled 'inventory')) {
            $metadata = Get-DatabaseMetadata -DatabaseObject $db
            Write-StableJson -Path (Join-Path $dbDirectory 'database.json') -Value $metadata

            $spaceObjects = @($db | Get-DbaDbSpace -EnableException)
            $dbSpaceRows = @(Convert-SpaceRows -SpaceObjects $spaceObjects)
            $dbFileGroupRows = @(Get-FileGroupRows -DatabaseObject $db)
            if ($IncludeCapacityMetrics) {
                $capacity = foreach ($space in $spaceObjects) {
                    [pscustomobject][ordered]@{
                        FileName = Convert-ToStableString (Get-ObjectPropertyValue $space 'FileName')
                        FileSizeMB = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'FileSize')
                        UsedSpaceMB = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'UsedSpace')
                        FreeSpaceMB = Get-SizeMegabytes (Get-ObjectPropertyValue $space 'FreeSpace')
                    }
                }
                Write-StableCsv -Path (Join-Path $OutputDirectory "telemetry/databases/$safeName/capacity.csv") -Rows @($capacity)
            }
            Write-StableCsv -Path (Join-Path $dbDirectory 'files.csv') -Rows $dbSpaceRows
            Write-StableCsv -Path (Join-Path $dbDirectory 'filegroups.csv') -Rows $dbFileGroupRows
            $databaseCatalogs = @{
                'replication-publications' = "IF OBJECT_ID(N'dbo.syspublications') IS NOT NULL SELECT name,description,status,sync_method,repl_freq,immediate_sync,enabled_for_internet,allow_push,allow_pull,allow_anonymous,independent_agent,retention,allow_sync_tran,autogen_sync_procs,allow_queued_tran,allow_dts,allow_subscription_copy FROM dbo.syspublications ORDER BY name;"
                'replication-articles' = "IF OBJECT_ID(N'dbo.sysarticles') IS NOT NULL SELECT p.name AS publication,a.name,a.dest_owner,a.dest_table,a.type,a.status,a.schema_option,a.ins_cmd,a.upd_cmd,a.del_cmd,OBJECT_SCHEMA_NAME(a.objid) AS source_schema,OBJECT_NAME(a.objid) AS source_object FROM dbo.sysarticles a JOIN dbo.syspublications p ON a.pubid=p.pubid ORDER BY p.name,a.name;"
                'replication-subscriptions' = "IF OBJECT_ID(N'dbo.syssubscriptions') IS NOT NULL SELECT a.name AS article,s.srvname AS subscriber,s.dest_db,s.status,s.sync_type,s.subscription_type,s.update_mode FROM dbo.syssubscriptions s JOIN dbo.sysarticles a ON s.artid=a.artid ORDER BY a.name,s.srvname,s.dest_db;"
                'merge-publications' = "IF OBJECT_ID(N'dbo.sysmergepublications') IS NOT NULL SELECT name,description,status,retention,sync_mode,allow_push,allow_pull,allow_anonymous,centralized_conflicts,dynamic_filters,snapshot_in_defaultfolder,compress_snapshot FROM dbo.sysmergepublications ORDER BY name;"
                'merge-articles' = "IF OBJECT_ID(N'dbo.sysmergearticles') IS NOT NULL SELECT p.name AS publication,a.name,a.type,a.status,a.destination_owner,a.destination_object,a.subset_filterclause,a.schema_option FROM dbo.sysmergearticles a JOIN dbo.sysmergepublications p ON a.pubid=p.pubid ORDER BY p.name,a.name;"
                'computed-columns' = 'SELECT OBJECT_SCHEMA_NAME(c.object_id) AS schema_name,OBJECT_NAME(c.object_id) AS object_name,c.name,c.definition,c.is_persisted FROM sys.computed_columns c JOIN sys.tables t ON c.object_id=t.object_id ORDER BY schema_name,object_name,c.name;'
                'identity-columns' = 'SELECT OBJECT_SCHEMA_NAME(c.object_id) AS schema_name,OBJECT_NAME(c.object_id) AS object_name,c.name,CONVERT(nvarchar(128),c.seed_value) AS seed_value,CONVERT(nvarchar(128),c.increment_value) AS increment_value,c.is_not_for_replication FROM sys.identity_columns c JOIN sys.tables t ON c.object_id=t.object_id ORDER BY schema_name,object_name,c.name;'
                'check-constraints' = 'SELECT OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,OBJECT_NAME(parent_object_id) AS table_name,name,definition,is_disabled,is_not_trusted,is_not_for_replication FROM sys.check_constraints ORDER BY schema_name,table_name,name;'
                'foreign-keys' = 'SELECT OBJECT_SCHEMA_NAME(parent_object_id) AS schema_name,OBJECT_NAME(parent_object_id) AS table_name,name,OBJECT_SCHEMA_NAME(referenced_object_id) AS referenced_schema,OBJECT_NAME(referenced_object_id) AS referenced_table,delete_referential_action_desc,update_referential_action_desc,is_disabled,is_not_trusted,is_not_for_replication FROM sys.foreign_keys ORDER BY schema_name,table_name,name;'
                'native-modules' = 'SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name,OBJECT_NAME(object_id) AS object_name,uses_native_compilation,is_schema_bound,execute_as_principal_id FROM sys.sql_modules WHERE uses_native_compilation=1 ORDER BY schema_name,object_name;'
                'tables' = 'SELECT SCHEMA_NAME(schema_id) AS schema_name,name,is_memory_optimized,durability_desc,temporal_type_desc,OBJECT_SCHEMA_NAME(history_table_id) AS history_schema,OBJECT_NAME(history_table_id) AS history_table,is_filetable,lock_escalation_desc,is_replicated,is_merge_published,is_tracked_by_cdc FROM sys.tables ORDER BY schema_name,name;'
                'columns' = 'SELECT OBJECT_SCHEMA_NAME(c.object_id) AS schema_name,OBJECT_NAME(c.object_id) AS object_name,c.name,c.column_id,TYPE_NAME(c.user_type_id) AS type_name,c.max_length,c.precision,c.scale,c.collation_name,c.is_nullable,c.is_identity,c.is_computed,c.is_sparse,c.is_column_set,c.generated_always_type_desc,c.encryption_type_desc,c.encryption_algorithm_name,k.name AS column_encryption_key FROM sys.columns c JOIN sys.tables t ON c.object_id=t.object_id LEFT JOIN sys.column_encryption_keys k ON c.column_encryption_key_id=k.column_encryption_key_id ORDER BY schema_name,object_name,c.column_id;'
                'security-policies' = 'SELECT SCHEMA_NAME(schema_id) AS schema_name,name,is_enabled,is_schema_bound FROM sys.security_policies ORDER BY schema_name,name;'
                'security-predicates' = 'SELECT SCHEMA_NAME(p.schema_id) AS policy_schema,p.name AS policy_name,OBJECT_SCHEMA_NAME(s.target_object_id) AS table_schema,OBJECT_NAME(s.target_object_id) AS table_name,s.predicate_definition,s.predicate_type_desc,s.operation_desc FROM sys.security_predicates s JOIN sys.security_policies p ON s.object_id=p.object_id ORDER BY policy_schema,policy_name,table_schema,table_name,s.predicate_type_desc,s.operation_desc;'
                'masked-columns' = 'SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name,OBJECT_NAME(object_id) AS object_name,name,is_masked,masking_function FROM sys.masked_columns WHERE is_masked=1 ORDER BY schema_name,object_name,name;'
                'column-master-keys' = 'SELECT name,key_store_provider_name,key_path FROM sys.column_master_keys ORDER BY name;'
                'column-encryption-keys' = 'SELECT name FROM sys.column_encryption_keys ORDER BY name;'
                'column-key-mappings' = 'SELECT c.name AS column_encryption_key,m.name AS column_master_key,v.encryption_algorithm_name FROM sys.column_encryption_key_values v JOIN sys.column_encryption_keys c ON v.column_encryption_key_id=c.column_encryption_key_id JOIN sys.column_master_keys m ON v.column_master_key_id=m.column_master_key_id ORDER BY c.name,m.name;'
                'indexes' = 'SELECT OBJECT_SCHEMA_NAME(i.object_id) AS schema_name,OBJECT_NAME(i.object_id) AS object_name,i.name,i.type_desc,i.is_unique,i.is_primary_key,i.is_unique_constraint,i.fill_factor,i.is_padded,i.is_disabled,i.ignore_dup_key,i.allow_row_locks,i.allow_page_locks,i.has_filter,i.filter_definition,d.name AS data_space FROM sys.indexes i JOIN sys.tables t ON i.object_id=t.object_id LEFT JOIN sys.data_spaces d ON i.data_space_id=d.data_space_id ORDER BY schema_name,object_name,i.name;'
                'index-columns' = 'SELECT OBJECT_SCHEMA_NAME(i.object_id) AS schema_name,OBJECT_NAME(i.object_id) AS object_name,i.name AS index_name,c.name AS column_name,ic.key_ordinal,ic.partition_ordinal,ic.is_descending_key,ic.is_included_column FROM sys.index_columns ic JOIN sys.indexes i ON ic.object_id=i.object_id AND ic.index_id=i.index_id JOIN sys.columns c ON ic.object_id=c.object_id AND ic.column_id=c.column_id ORDER BY schema_name,object_name,index_name,ic.index_column_id;'
                'hash-indexes' = 'SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name,OBJECT_NAME(object_id) AS object_name,name,bucket_count FROM sys.hash_indexes ORDER BY schema_name,object_name,name;'
                'partition-functions' = 'SELECT name,type_desc,fanout,boundary_value_on_right FROM sys.partition_functions ORDER BY name;'
                'partition-boundaries' = 'SELECT f.name,v.boundary_id,CONVERT(nvarchar(4000),v.value) AS boundary_value,CONVERT(nvarchar(128),SQL_VARIANT_PROPERTY(v.value,''BaseType'')) AS value_type FROM sys.partition_range_values v JOIN sys.partition_functions f ON v.function_id=f.function_id ORDER BY f.name,v.boundary_id;'
                'partition-schemes' = 'SELECT s.name,f.name AS partition_function,d.destination_id,g.name AS filegroup_name FROM sys.partition_schemes s JOIN sys.partition_functions f ON s.function_id=f.function_id JOIN sys.destination_data_spaces d ON s.data_space_id=d.partition_scheme_id JOIN sys.filegroups g ON d.data_space_id=g.data_space_id ORDER BY s.name,d.destination_id;'
                'compression' = 'SELECT OBJECT_SCHEMA_NAME(p.object_id) AS schema_name,OBJECT_NAME(p.object_id) AS object_name,i.name AS index_name,p.partition_number,p.data_compression_desc FROM sys.partitions p JOIN sys.indexes i ON p.object_id=i.object_id AND p.index_id=i.index_id JOIN sys.tables t ON p.object_id=t.object_id ORDER BY schema_name,object_name,index_name,p.partition_number;'
                'scoped-configuration' = 'SELECT name,value,value_for_secondary FROM sys.database_scoped_configurations ORDER BY name;'
                'principals' = "SELECT name,type_desc,default_schema_name,authentication_type_desc,CONVERT(varchar(max),sid,1) AS sid FROM sys.database_principals ORDER BY name;"
                'role-members' = 'SELECT r.name AS role_name,m.name AS member_name FROM sys.database_role_members rm JOIN sys.database_principals r ON rm.role_principal_id=r.principal_id JOIN sys.database_principals m ON rm.member_principal_id=m.principal_id ORDER BY r.name,m.name;'
                'permissions' = 'SELECT grantee.name AS grantee,grantor.name AS grantor,p.class_desc,p.major_id,p.minor_id,OBJECT_SCHEMA_NAME(p.major_id) AS object_schema,OBJECT_NAME(p.major_id) AS object_name,p.permission_name,p.state_desc FROM sys.database_permissions p JOIN sys.database_principals grantee ON p.grantee_principal_id=grantee.principal_id JOIN sys.database_principals grantor ON p.grantor_principal_id=grantor.principal_id ORDER BY grantee.name,p.class_desc,p.major_id,p.minor_id,p.permission_name;'
                'file-configuration' = 'SELECT name,type_desc,physical_name,max_size,growth,is_percent_growth FROM sys.database_files ORDER BY name;'
                'fulltext-catalogs' = 'SELECT name,path,is_default,is_accent_sensitivity_on FROM sys.fulltext_catalogs ORDER BY name;'
                'fulltext-indexes' = 'SELECT OBJECT_SCHEMA_NAME(f.object_id) AS schema_name,OBJECT_NAME(f.object_id) AS object_name,i.name AS unique_index,c.name AS catalog_name,f.is_enabled,f.change_tracking_state_desc,s.name AS stoplist FROM sys.fulltext_indexes f JOIN sys.indexes i ON f.object_id=i.object_id AND f.unique_index_id=i.index_id LEFT JOIN sys.fulltext_catalogs c ON f.fulltext_catalog_id=c.fulltext_catalog_id LEFT JOIN sys.fulltext_stoplists s ON f.stoplist_id=s.stoplist_id ORDER BY schema_name,object_name;'
                'fulltext-columns' = 'SELECT OBJECT_SCHEMA_NAME(f.object_id) AS schema_name,OBJECT_NAME(f.object_id) AS object_name,c.name AS column_name,f.language_id,tc.name AS type_column,f.statistical_semantics FROM sys.fulltext_index_columns f JOIN sys.columns c ON f.object_id=c.object_id AND f.column_id=c.column_id LEFT JOIN sys.columns tc ON f.object_id=tc.object_id AND f.type_column_id=tc.column_id ORDER BY schema_name,object_name,column_name;'
                'fulltext-stopwords' = 'SELECT s.name AS stoplist,w.stopword,w.language_id FROM sys.fulltext_stopwords w JOIN sys.fulltext_stoplists s ON w.stoplist_id=s.stoplist_id ORDER BY s.name,w.language_id,w.stopword;'
                'search-properties' = 'SELECT l.name AS property_list,p.property_name,p.property_set_guid,p.property_int_id,p.property_description FROM sys.registered_search_properties p JOIN sys.registered_search_property_lists l ON p.property_list_id=l.property_list_id ORDER BY l.name,p.property_name;'
            }
            foreach ($catalogName in $databaseCatalogs.Keys | Sort-Object) {
                $rows = Convert-DataTableRows (Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query $databaseCatalogs[$catalogName])
                Write-StableCsv -Path (Join-Path $dbDirectory ("catalog/" + $catalogName + '.csv')) -Rows $rows
            }
        }

        if (-not $SkipSchema -and (Test-SectionEnabled 'schema')) {
            $extractArgs = @{
                Executable      = $resolvedSqlPackage
                ServerName      = $SqlInstance
                DatabaseName    = $db.Name
                TargetDirectory = (Join-Path $dbDirectory 'schema')
                TimeoutSeconds  = $ConnectTimeout
                TrustCertificate = [bool]$TrustServerCertificate
                VerifyExtraction = [bool]$VerifySchemaExtraction
                SortElementsByName = (-not [bool]$DisableSchemaElementSorting)
                AdditionalConnectionOptions = $AppendConnectionString
            }
            Invoke-SqlPackageExtract @extractArgs
        }
    }

    } # Database section

    Write-StableCsv -Path (Join-Path $OutputDirectory 'database-map.csv') -Rows $databaseMap

    if ($IncludePerformanceMetrics -and (Test-SectionEnabled 'telemetry/performance')) {
        $metricQueries = @{
            'waits' = 'SELECT wait_type,waiting_tasks_count,wait_time_ms,signal_wait_time_ms FROM sys.dm_os_wait_stats;'
            'io' = 'SELECT database_id,file_id,num_of_reads,num_of_bytes_read,io_stall_read_ms,num_of_writes,num_of_bytes_written,io_stall_write_ms FROM sys.dm_io_virtual_file_stats(NULL,NULL);'
            'counters' = 'SELECT object_name,counter_name,instance_name,cntr_value,cntr_type FROM sys.dm_os_performance_counters;'
            'ssis-executions' = 'SELECT TOP (20000) execution_id,folder_name,project_name,package_name,status,start_time,end_time FROM SSISDB.catalog.executions ORDER BY execution_id DESC;'
        }
        $metricFailures = @()
        foreach ($metric in $metricQueries.Keys | Sort-Object) {
            try {
                $rows = Convert-DataTableRows (Invoke-QueryTable -ServerObject $server -DatabaseName 'master' -Query $metricQueries[$metric])
                Write-StableCsv -Path (Join-Path $OutputDirectory ("telemetry/" + $metric + '.csv')) -Rows $rows
            } catch { $metricFailures += [ordered]@{section=$metric;error=$_.Exception.Message}; Write-CollectorError "Telemetry $metric failed: $($_.Exception.Message)" }
        }
        foreach ($db in $databases) {
            try {
                $query = 'SELECT q.query_id,p.plan_id,rs.runtime_stats_interval_id,rs.count_executions,rs.avg_duration,rs.avg_cpu_time,rs.avg_logical_io_reads,rs.avg_physical_io_reads,rs.avg_query_max_used_memory FROM sys.query_store_runtime_stats rs JOIN sys.query_store_plan p ON rs.plan_id=p.plan_id JOIN sys.query_store_query q ON p.query_id=q.query_id;'
                $rows = Convert-DataTableRows (Invoke-QueryTable -ServerObject $server -DatabaseName $db.Name -Query $query)
                Write-StableCsv -Path (Join-Path $OutputDirectory ("telemetry/databases/" + (Get-SafePathSegment $db.Name) + '/query-store.csv')) -Rows $rows
            } catch { $metricFailures += [ordered]@{section=$db.Name;error=$_.Exception.Message} }
        }
        Write-StableJson -Path (Join-Path $OutputDirectory 'telemetry/collection-status.json') -Value @{failures=@($metricFailures)}
    }

    if (($IncludeHealthMetrics -or $IncludeIndexHealth) -and (Test-SectionEnabled 'telemetry/health')) {
        try {
            . (Join-Path $PSScriptRoot 'SqlHealth.ps1')
            Export-SqlHealth -ServerObject $server -Databases @($databases | ForEach-Object { $_.Name }) -Target (Join-Path $OutputDirectory 'telemetry/health.json') -QueryTimeout $HealthQueryTimeout -IndexHealth:$IncludeIndexHealth -IndexLimit $HealthIndexLimit
        } catch {
            Write-StableJson -Path (Join-Path $OutputDirectory 'telemetry/health-error.json') -Value @{observed_at=[DateTime]::UtcNow.ToString('o');failures=@(@{section='health';error=$_.Exception.Message})}
        }
    }
    $expandedSections = @()
    foreach ($entry in $script:CollectionSections) {
        if ($entry.status -eq 'complete' -and $entry.path.StartsWith('databases/')) {
            $paths = @()
            if (-not $SkipSchema -and (Test-SectionEnabled 'schema')) { $paths += ($entry.path + '/schema') }
            if (-not $SkipInventory -and (Test-SectionEnabled 'inventory')) { foreach ($leaf in @('database.json','files.csv','filegroups.csv','catalog')) { $paths += ($entry.path + '/' + $leaf) } }
            if ($SkipSchema -or -not (Test-SectionEnabled 'schema')) {
                $expandedSections += [ordered]@{path=($entry.path+'/schema');status='disabled';error='Schema collection disabled';files=@{}}
            }
            if ($SkipInventory -or -not (Test-SectionEnabled 'inventory')) {
                foreach ($leaf in @('database.json','files.csv','filegroups.csv','catalog')) {
                    $expandedSections += [ordered]@{path=($entry.path+'/'+$leaf);status='disabled';error='Inventory collection disabled';files=@{}}
                }
            }
            foreach ($scope in $paths) {
                $subset = [ordered]@{}
                foreach ($key in $entry.files.Keys) { if ($key -eq $scope -or $key.StartsWith($scope + '/')) { $subset[$key] = $entry.files[$key] } }
                $expandedSections += [ordered]@{path=$scope;status='complete';error='';files=$subset}
            }
        } else { $expandedSections += $entry }
    }
    $script:CollectionSections = $expandedSections
    Invoke-CollectionSection 'instance/server.json' { }
    Invoke-CollectionSection 'collector.json' { }
    Invoke-CollectionSection 'database-map.csv' { }
    Save-CollectionManifest -Finalized $true
    if (@($script:CollectionSections | Where-Object { $_.status -in @('failed','skipped') }).Count -gt 0) {
        Write-CollectorError 'SQL Server collection partially failed (exit 6)'
        exit 6
    }
    Write-CollectorMessage 'SQL Server collection completed successfully'
    exit 0
}
catch {
    Write-CollectorError $_.Exception.Message
    Write-CollectorError $_.ScriptStackTrace
    exit 1
}
