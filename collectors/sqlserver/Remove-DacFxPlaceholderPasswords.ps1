#Requires -Version 5.1
<# Called ONLY on the fresh SchemaObjectType directory created by our SqlPackage
extract. Never use on arbitrary SQL scripts or existing archive/Git snapshots. #>
function Remove-DacFxPlaceholderPasswords {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$ExtractDirectory)
    $security=Join-Path $ExtractDirectory 'Security'
    if(-not (Test-Path -LiteralPath $security -PathType Container)){return 0}
    $identifier='\[(?:[^\]]|\]\])+\]'
    $prefix='\s*CREATE\s+(?:LOGIN|USER)\s+'+$identifier+'\s+WITH\s+PASSWORD\s*=\s*'
    $options='(?:\s*,\s*(?:DEFAULT_SCHEMA|DEFAULT_DATABASE|DEFAULT_LANGUAGE)\s*=\s*'+$identifier+'|\s*,\s*(?:CHECK_POLICY|CHECK_EXPIRATION)\s*=\s*(?:ON|OFF))*'
    $suffix=$options+'\s*;\s*(?:GO\s*)?'
    # Match a COMPLETE standalone creation script, not text embedded in a routine,
    # comment, string or a multi-statement batch. Unknown creation syntax fails closed.
    $pattern='(?is)\A(?<prefix>'+$prefix+")N?'(?:[^']|'')*'(?<suffix>"+$suffix+')\z'
    $marker='<CONFIGBACKUP_PASSWORD_REMOVED>'
    $notice="-- ConfigBackup: PASSWORD REMOVED from a SqlPackage-generated placeholder.`n-- Not a saved source password. Supply a new secure password before executing this script.`n"
    $count=0
    foreach($file in @(Get-ChildItem -LiteralPath $security -File -Filter '*.sql' | Sort-Object Name)) {
        if($file.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'DacFx security output must not contain links'}
        $text=[IO.File]::ReadAllText($file.FullName)
        if($text.StartsWith($notice) -and $text.Substring($notice.Length) -match ('(?is)\A'+$prefix+[regex]::Escape($marker)+$suffix+'\z')){continue}
        $match=[regex]::Match($text,$pattern)
        if(-not $match.Success){
            if($text -match '(?is)\A\s*CREATE\s+(?:LOGIN|USER)\b.*\bPASSWORD\s*='){
                throw ('Unrecognized DacFx password-bearing security script; database snapshot must be preserved: '+$file.Name)
            }
            continue
        }
        $clean=$notice+$match.Groups['prefix'].Value+$marker+$match.Groups['suffix'].Value
        [IO.File]::WriteAllText($file.FullName,$clean,(New-Object Text.UTF8Encoding($false)))
        $count++
    }
    return $count
}
