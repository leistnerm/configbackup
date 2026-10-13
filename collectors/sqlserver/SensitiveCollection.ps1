# Explicit section approvals. No implicit parent or wildcard inheritance.
function Test-SensitiveSection {
    param([string]$Path)
    if (-not $env:CONFIGBACKUP_SENSITIVE_SECTIONS) { return $false }
    $raw = $env:CONFIGBACKUP_SENSITIVE_SECTIONS.Trim()
    if (-not $raw.StartsWith('[') -or -not $raw.EndsWith(']')) { throw 'Sensitive sections must be a JSON array' }
    $selected = @($raw | ConvertFrom-Json)
    $allowed = @('instance/scripts/Logins','instance/scripts/Credentials','instance/scripts/LinkedServers','instance/sensitive-ssis/environment-variables.csv','instance/sensitive-ssis/project-parameters.csv','instance/sensitive-ssis/package-parameters.csv')
    foreach ($name in $selected) {
        if ($name -isnot [string] -or $allowed -cnotcontains $name) { throw 'Unsupported sensitive SQL section; exact paths are required' }
    }
    return $selected -ccontains $Path
}

function Export-SsisSensitiveEnvironment {
    param($ServerObject, [string]$TargetPath)
    # This adapter depends on internal SSISDB structures. Refuse incompatible
    # catalogs and incomplete permissions without altering policy, roles or keys.
    $preflight = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query @'
SELECT CASE WHEN IS_SRVROLEMEMBER('sysadmin')=1
 AND OBJECT_ID(N'internal.environment_variables') IS NOT NULL
 AND COL_LENGTH(N'internal.environment_variables',N'sensitive_value') IS NOT NULL
 AND OBJECT_ID(N'internal.get_value_by_data_type') IS NOT NULL
 THEN 1 ELSE 0 END AS compatible;
'@
    if ($null -eq $preflight -or $preflight.Rows.Count -ne 1 -or [int]$preflight.Rows[0]['compatible'] -ne 1) {
        throw 'Sensitive SSIS recovery unavailable: requires sysadmin and a compatible SSISDB environment/key layout'
    }
    $countTable = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query 'SELECT COUNT_BIG(*) AS expected_count FROM internal.environment_variables WHERE sensitive=1;'
    if ($null -eq $countTable -or $countTable.Rows.Count -ne 1) { throw 'Sensitive SSIS inventory count unavailable' }
    $expectedCount = [long]$countTable.Rows[0]['expected_count']
    $folderColumn = Get-SsisFolderIdentifierColumn -ServerObject $ServerObject
    if ($folderColumn -notin @('folder_id','id')) { throw 'Unsupported SSIS folder identifier' }
    $query = @'
SELECT f.name AS folder_name,e.name AS environment_name,v.name AS variable_name,
       v.description,v.type,CAST(1 AS bit) AS sensitive,
       CONVERT(nvarchar(max),internal.get_value_by_data_type(
           DECRYPTBYKEYAUTOCERT(CERT_ID(N'MS_Cert_Env_' + CONVERT(nvarchar(20),v.environment_id)),NULL,v.sensitive_value),v.type)) AS value
FROM internal.environment_variables AS v
JOIN catalog.environments AS e ON e.environment_id=v.environment_id
JOIN catalog.folders AS f ON f.[FOLDER_COLUMN]=e.folder_id
WHERE v.sensitive=1
ORDER BY f.name,e.name,v.name;
'@
    $query = $query.Replace('[FOLDER_COLUMN]', '[' + $folderColumn + ']')
    $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query $query
    # NULL can indicate a missing certificate, inaccessible key or incompatible
    # encryption. Never replace a previous snapshot with an apparent empty value.
    if ($null -eq $table -or $table.Rows.Count -ne $expectedCount) { throw 'Sensitive SSIS recovery inventory incomplete; previous snapshot preserved' }
    foreach ($row in $table.Rows) {
        if ($row['value'] -is [System.DBNull] -or $null -eq $row['value']) {
            throw 'Sensitive SSIS recovery could not decrypt every selected value; previous snapshot preserved'
        }
    }
    Write-StableCsv -Path $TargetPath -Rows (Convert-DataTableRows $table)
}


function Export-SsisSensitiveParameters {
    param($ServerObject, [string]$TargetPath, [ValidateSet(20,30)][int]$ObjectType)
    # Only server-configured defaults for the current deployment are recovered.
    # References never resolve environment values across approval boundaries.
    $preflight = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query @'
SELECT CASE WHEN IS_SRVROLEMEMBER('sysadmin')=1
 AND OBJECT_ID(N'internal.object_parameters') IS NOT NULL
 AND COL_LENGTH(N'internal.object_parameters',N'sensitive_default_value') IS NOT NULL
 AND COL_LENGTH(N'internal.object_parameters',N'project_version_lsn') IS NOT NULL
 AND COL_LENGTH(N'internal.projects',N'object_version_lsn') IS NOT NULL
 AND OBJECT_ID(N'internal.get_value_by_data_type') IS NOT NULL
 THEN 1 ELSE 0 END AS compatible;
'@
    if ($null -eq $preflight -or $preflight.Rows.Count -ne 1 -or [int]$preflight.Rows[0]['compatible'] -ne 1) {
        throw 'Sensitive parameter recovery unavailable: requires sysadmin and a compatible SSISDB parameter/key layout'
    }
    $selection = @"
FROM internal.object_parameters AS a
JOIN internal.projects AS p ON p.project_id=a.project_id AND p.object_version_lsn=a.project_version_lsn
WHERE a.sensitive=1 AND a.object_type=$ObjectType
"@
    $inventory = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query ("SELECT COUNT_BIG(*) AS expected_count " + $selection)
    if ($null -eq $inventory -or $inventory.Rows.Count -ne 1) { throw 'Sensitive parameter inventory unavailable' }
    $query = @"
SELECT f.name AS folder_name,p.name AS project_name,a.object_type,a.object_name,
       a.parameter_name,a.parameter_data_type AS data_type,a.description,a.required,
       a.value_set,a.value_type,a.referenced_variable_name,
       CASE WHEN a.value_set=0 THEN N'design_default_unrecovered'
            WHEN a.value_type='R' THEN N'environment_reference'
            WHEN a.value_type='V' THEN N'server_literal' ELSE N'unsupported' END AS recovery_status,
       CASE WHEN a.value_set=1 AND a.value_type='V' THEN
         CONVERT(nvarchar(max),internal.get_value_by_data_type(
           DECRYPTBYKEYAUTOCERT(CERT_ID(N'MS_Cert_Proj_' + CONVERT(nvarchar(20),a.project_id)),NULL,a.sensitive_default_value),a.parameter_data_type),126)
       END AS value
FROM internal.object_parameters AS a
JOIN internal.projects AS p ON p.project_id=a.project_id AND p.object_version_lsn=a.project_version_lsn
JOIN internal.folders AS f ON f.folder_id=p.folder_id
WHERE a.sensitive=1 AND a.object_type=$ObjectType
ORDER BY f.name,p.name,a.object_name,a.parameter_name;
"@
    $table = Invoke-QueryTable -ServerObject $ServerObject -DatabaseName 'SSISDB' -Query $query
    if ($null -eq $table -or $table.Rows.Count -ne [long]$inventory.Rows[0]['expected_count']) {
        throw 'Sensitive parameter inventory incomplete; previous snapshot preserved'
    }
    foreach ($row in $table.Rows) {
        $status = [string]$row['recovery_status']
        if ($status -notin @('server_literal','environment_reference','design_default_unrecovered')) {
            throw 'Unsupported sensitive parameter configuration; previous snapshot preserved'
        }
        if ($status -eq 'server_literal' -and ($row['value'] -is [DBNull] -or $null -eq $row['value'])) {
            throw 'Sensitive parameter decryption failed; previous snapshot preserved'
        }
        if ($status -eq 'environment_reference' -and [string]::IsNullOrWhiteSpace([string]$row['referenced_variable_name'])) {
            throw 'Sensitive parameter reference incomplete; previous snapshot preserved'
        }
    }
    Write-StableCsv -Path $TargetPath -Rows (Convert-DataTableRows $table)
}

function Invoke-SsisSensitiveCollections {
    param($ServerObject, [string]$OutputDirectory, [bool]$SkipSsis = $false)
    foreach ($leaf in @('environment-variables','project-parameters','package-parameters')) {
        $scope = 'instance/sensitive-ssis/' + $leaf + '.csv'
        if (-not (Test-SensitiveSection $scope)) { continue }
        if ($SkipSsis -or -not (Test-SectionEnabled 'instance/ssis')) {
            $script:CollectionSections += [ordered]@{path=$scope;status='disabled';error='SSIS collection disabled';files=@{};sensitive=$true}
            continue
        }
        Invoke-CollectionSection $scope {
            $target = Join-Path $OutputDirectory $scope
            if ($leaf -eq 'environment-variables') {
                Export-SsisSensitiveEnvironment -ServerObject $ServerObject -TargetPath $target
            } else {
                $objectType = if ($leaf -eq 'project-parameters') { 20 } else { 30 }
                Export-SsisSensitiveParameters -ServerObject $ServerObject -TargetPath $target -ObjectType $objectType
            }
        }
    }
}
