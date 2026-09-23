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
    Collector version: 1.3.11
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

    # SqlPackage schema-model verification is intentionally opt-in. Extraction can
    # succeed for source-control/history purposes even when model verification finds
    # unresolved external references.
    [switch]$VerifySchemaExtraction,

    [switch]$SkipSchema,
    [switch]$SkipInstanceExport,
    [switch]$SkipInventory,
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

    [ValidateRange(1, 600)]
    [int]$ConnectTimeout = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$CollectorVersion = '1.3.11'
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
        [switch]$TrustCertificate,
        [switch]$VerifyExtraction
    )

    [System.IO.Directory]::CreateDirectory($TargetDirectory) | Out-Null

    $diagnosticsFile = Join-Path ([System.IO.Path]::GetTempPath()) ("configbackup-sqlpackage-" + [guid]::NewGuid().ToString('N') + '.log')
    $verifyValue = if ($VerifyExtraction) { 'True' } else { 'False' }
    $trustValue = if ($TrustCertificate) { 'True' } else { 'False' }

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
        "/p:VerifyExtraction=$verifyValue",
        "/DiagnosticsFile:$diagnosticsFile",
        '/DiagnosticsLevel:Error',
        '/Quiet:True'
    )

    Write-CollectorMessage "Extracting schema: $DatabaseName (verify=$verifyValue trust_server_certificate=$trustValue)"
    try {
        $consoleOutput = @(& $Executable @arguments 2>&1)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            Write-CollectorError "SqlPackage failed for database '$DatabaseName' with exit code $exitCode."
            foreach ($line in $consoleOutput) {
                if ($null -ne $line -and -not [string]::IsNullOrWhiteSpace([string]$line)) {
                    Write-CollectorError ("SqlPackage console: " + [string]$line)
                }
            }
            if (Test-Path -LiteralPath $diagnosticsFile) {
                $diagnosticLines = @(Get-Content -LiteralPath $diagnosticsFile -ErrorAction SilentlyContinue)
                if ($diagnosticLines.Count -gt 0) {
                    Write-CollectorError 'SqlPackage diagnostics:'
                    foreach ($line in @($diagnosticLines | Select-Object -Last 200)) {
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

    try {
        # Databases are handled by the inventory/SqlPackage path below. SQL Agent is
        # exported separately by Export-SqlAgentConfiguration. Availability Groups are
        # also excluded from the broad Export-DbaInstance pass because dbatools treats
        # "HADR not configured" as an exception when -EnableException is used. We
        # conditionally export AGs below only when SERVERPROPERTY('IsHadrEnabled') = 1.
        $requestedExcludes = @(Expand-NameList $AdditionalExcludes)
        $skipAvailabilityGroups = ($requestedExcludes -contains 'AvailabilityGroups')
        $excludes = @('Databases', 'AgentServer', 'AvailabilityGroups') + $requestedExcludes
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

        try {
            $files = @(Export-DbaInstance @exportArgs)
        }
        catch {
            Write-CollectorError ("Export-DbaInstance failed: {0}" -f $_.Exception.Message)
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

        if (-not $skipAvailabilityGroups) {
            Export-AvailabilityGroupConfiguration -ServerObject $ServerObject -TargetDirectory $TargetDirectory -HadrEnabled:$HadrEnabled
        }
        else {
            Write-CollectorMessage 'Skipping Availability Groups because InstanceExclude contains AvailabilityGroups'
        }
    }
    finally {
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
        [switch]$IncludeLegacy,
        [switch]$AllowPartial
    )

    if ($null -eq $ServerObject.Databases['SSISDB']) {
        Write-CollectorMessage 'SSISDB is not present; skipping project-deployment SSIS collection'
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
        InstanceExport        = (-not $SkipInstanceExport)
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
            HadrEnabled        = $hadrEnabled
        }
        Export-InstanceConfiguration @instanceExportArgs
    }

    if (-not $SkipAgent) {
        Export-SqlAgentConfiguration -ServerObject $server -TargetDirectory (Join-Path $instanceDirectory 'agent')
    }

    if (-not $SkipSsis) {
        Export-SsisConfiguration -ServerObject $server -TargetDirectory (Join-Path $instanceDirectory 'ssis') -SkipIspacFiles:$SkipIspac -IncludeLegacy:$IncludeLegacySsis -AllowPartial:$AllowPartialSsis
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
                VerifyExtraction = [bool]$VerifySchemaExtraction
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
