param([Parameter(Mandatory=$true)][string]$SelectionsPath)
$ErrorActionPreference='Stop'
$selections = @(Get-Content -LiteralPath $SelectionsPath -Raw | ConvertFrom-Json)
$results = @()
foreach ($selection in $selections) {
    $base=$null
    $records=[System.Collections.Generic.List[object]]::new()
    try {
        $parts=([string]$selection.path).Replace(':','').Split('\',2)
        $hive=switch($parts[0]) {'HKLM' {[Microsoft.Win32.RegistryHive]::LocalMachine} 'HKCU' {[Microsoft.Win32.RegistryHive]::CurrentUser} 'HKU' {[Microsoft.Win32.RegistryHive]::Users} default {throw 'Unsupported hive'}}
        $view=if($selection.view -eq '32'){[Microsoft.Win32.RegistryView]::Registry32}else{[Microsoft.Win32.RegistryView]::Registry64}
        $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
        $key=$base.OpenSubKey($parts[1],$false)
        function Read-SelectedKey([Microsoft.Win32.RegistryKey]$Key,[string]$Relative,[object]$Selection) {
            $values=@()
            foreach($name in @($Key.GetValueNames() | Sort-Object)) {
                if($null -ne $Selection.values -and @($Selection.values) -notcontains $name){continue}
                $kind=$Key.GetValueKind($name).ToString()
                $value=$Key.GetValue($name,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                $redacted=$name -match '(?i)password|passwd|secret|token|credential|private.?key'
                if($redacted){$value='<REDACTED>'}
                elseif($kind -eq 'Binary'){$value=[Convert]::ToBase64String([byte[]]$value)}
                # MultiString order is retained; it can encode dependency or execution order.
                $values+= [ordered]@{name=$name;kind=$kind;data=$value;redacted=$redacted}
            }
            $records.Add([ordered]@{key=$Relative;values=@($values)})
            if($Selection.recursive -eq $true) {
                foreach($child in @($Key.GetSubKeyNames() | Sort-Object)) {
                    $opened=$Key.OpenSubKey($child,$false)
                    if($null -eq $opened){throw "Subkey disappeared: $Relative\$child"}
                    try {Read-SelectedKey $opened "$Relative\$child" $Selection} finally {$opened.Dispose()}
                }
            }
        }
        if($null -eq $key) {
            # Successful read of a missing selected key is evidence of absence.
            $results += [ordered]@{id=$selection.id;status='complete';exists=$false;records=@();error=''}
        } else {
            try {Read-SelectedKey $key $selection.path $selection} finally {$key.Dispose()}
            $results += [ordered]@{id=$selection.id;status='complete';exists=$true;records=@($records.ToArray());error=''}
        }
    } catch {
        $results += [ordered]@{id=$selection.id;status='failed';error=$_.Exception.GetType().Name;records=@()}
    } finally {if($null -ne $base){$base.Dispose()}}
}
ConvertTo-Json -InputObject @($results) -Depth 30 -Compress
