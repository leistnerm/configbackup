# Explicit section approvals. No implicit parent or wildcard inheritance.
function Test-SensitiveSection {
    param([string]$Path)
    if (-not $env:CONFIGBACKUP_SENSITIVE_SECTIONS) { return $false }
    $raw = $env:CONFIGBACKUP_SENSITIVE_SECTIONS.Trim()
    if (-not $raw.StartsWith('[') -or -not $raw.EndsWith(']')) { throw 'Sensitive sections must be a JSON array' }
    $selected = @($raw | ConvertFrom-Json)
    $allowed = @('instance/scripts/Logins','instance/scripts/Credentials','instance/scripts/LinkedServers','instance/sensitive-ssis/environment-variables.csv')
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
