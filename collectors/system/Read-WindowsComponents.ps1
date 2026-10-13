[CmdletBinding()]
param([Parameter(Mandatory=$true)][ValidateSet('iis','dns','dhcp','hyperv','certificates','dfs','nps','wef','wsus','adcs')][string]$Component,
      [string]$DetailsJson='[]')
# Read-only local queries. Never call CommitChanges, Set/Add/Remove/Export role cmdlets.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$script:Records = New-Object 'System.Collections.Generic.List[object]'
$script:Selection = if ($env:CONFIGBACKUP_SECTIONS) { $env:CONFIGBACKUP_SECTIONS | ConvertFrom-Json } else { [pscustomobject]@{} }
$script:Details = @($DetailsJson | ConvertFrom-Json)
if (@($script:Details | Where-Object { $_ -notin @('iis-extended','dns-policies','dhcp-reservation-options','hyperv-network','hyperv-replication') }).Count) { throw 'Unsupported component detail' }

function Has-Detail([string]$Name) { return $Name -in $script:Details }

function Test-Selected([string]$Scope) {
    foreach ($entry in $script:Selection.PSObject.Properties) {
        $value = $entry.Value
        if ($value -isnot [bool]) { $value = $value.enabled }
        if ($value -isnot [bool]) { throw 'Section enabled must be boolean' }
        if (-not $value -and ($entry.Name -eq 'configuration' -or $Scope -like $entry.Name -or $Scope -like ($entry.Name.TrimEnd('/') + '/*'))) { return $false }
    }
    return $true
}
function Scope-Key([string]$Value) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())))).Replace('-','').ToLowerInvariant().Substring(0,24) }
    finally { $sha.Dispose() }
}
function Plain($Value, [int]$Depth=0) {
    if ($Depth -gt 24) { throw 'Configuration nesting exceeds supported depth' }
    if ($null -eq $Value) { return $null }
    if ($Value -is [string] -or $Value -is [bool]) { return $Value }
    if ($Value -is [System.Net.IPAddress]) { return $Value.ToString() }
    if ($Value -is [TimeSpan]) { return $Value.ToString('c',[Globalization.CultureInfo]::InvariantCulture) }
    if ($Value -is [DateTime]) { return $Value.ToString('o',[Globalization.CultureInfo]::InvariantCulture) }
    if ($Value -is [Guid] -or $Value -is [Enum]) { return $Value.ToString() }
    if ($Value -is [byte[]]) { return [Convert]::ToBase64String($Value) }
    if ($Value.GetType().IsPrimitive -or $Value -is [decimal]) { return $Value }
    if ($Value -is [System.Collections.IDictionary]) {
        $map = [ordered]@{}
        foreach ($key in @($Value.Keys | Sort-Object)) {
            if ([string]$key -match '(?i)password|secret|privatekey|sharedsecret') { $map[[string]$key]='[SECRET NOT COLLECTED]' }
            else { $map[[string]$key] = Plain $Value[$key] ($Depth+1) }
        }
        return $map
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $rows = @($Value | ForEach-Object { Plain $_ ($Depth+1) })
        return ,$rows
    }
    $map = [ordered]@{}
    $props = if ($Value.PSObject.Properties['CimInstanceProperties']) { $Value.CimInstanceProperties } else { $Value.PSObject.Properties }
    foreach ($prop in @($props | Sort-Object Name)) {
        if ($prop.Name -in @('PSComputerName','RunspaceId','PSShowComputerName','CimClass','CimSystemProperties')) { continue }
        if ($prop.Name -match '(?i)password|secret|privatekey|sharedsecret') { $map[$prop.Name] = '[SECRET NOT COLLECTED]'; continue }
        $map[$prop.Name] = Plain $prop.Value ($Depth+1)
    }
    return $map
}
function Fields($Value, [string[]]$Names) {
    $data = [ordered]@{}
    foreach ($name in $Names) {
        $p = $Value.PSObject.Properties[$name]
        if ($null -eq $p) { throw ('Required property unavailable: ' + $name) }
        $data[$name] = Plain $p.Value
    }
    return $data
}
function Read-Scope([string]$RelativePath, [scriptblock]$ReaderQuery, [switch]$Rows) {
    $_cbScope = 'windows-components/' + $Component + '/' + $RelativePath
    if (-not (Test-Selected $_cbScope)) {
        $script:Records.Add(@{scope=$_cbScope;status='disabled';error='Disabled by configuration'})
        return
    }
    try {
        if ($Rows) { $_cbValue = @(& $ReaderQuery) } else { $_cbValue = & $ReaderQuery }
        $script:Records.Add(@{scope=$_cbScope;status='complete';data=$_cbValue;unordered_rows=[bool]$Rows})
    } catch {
        $_cbReason = $_.Exception.GetType().Name + ': ' + [string]$_.CategoryInfo.Category
        $script:Records.Add(@{scope=$_cbScope;status='failed';error=$_cbReason})
    }
}
function Iis-Element($Element, [int]$Depth=0, [bool]$HideValues=$false) {
    if ($Depth -gt 20) { throw 'IIS configuration nesting limit exceeded' }
    $hide = $HideValues -or $Element.ElementTagName -in @('environmentVariables','appSettings','connectionStrings')
    $named = @($Element.Attributes | Where-Object { $_.Name -in @('name','key') } | ForEach-Object { [string]$_.Value }) -join ' '
    $attrs = [ordered]@{}
    foreach ($a in @($Element.Attributes | Sort-Object Name)) {
        if ($a.Name -match '(?i)password|secret|token|privatekey|connectionstring|validationkey|decryptionkey' -or
            ($a.Name -eq 'value' -and ($hide -or $named -match '(?i)password|secret|token|authorization|api.?key'))) {
            $attrs[$a.Name] = '[SECRET NOT COLLECTED]'
        } else { $attrs[$a.Name] = Plain $a.Value }
    }
    $children = [ordered]@{}
    foreach ($child in $Element.ChildElements) { $children[$child.ElementTagName] = Iis-Element $child ($Depth+1) $hide }
    $items = @()
    if ($null -ne $Element.Schema.CollectionSchema) {
        # Collection order can determine IIS behavior (handlers/rules/modules).
        $items = @($Element.GetCollection() | ForEach-Object { Iis-Element $_ ($Depth+1) $hide })
    }
    return @{element=$Element.ElementTagName;attributes=$attrs;children=$children;items=$items}
}
function Read-Iis {
    $dll = Join-Path ([Environment]::GetFolderPath('Windows')) 'System32\inetsrv\Microsoft.Web.Administration.dll'
    Add-Type -Path $dll
    $manager = New-Object Microsoft.Web.Administration.ServerManager
    try {
        $config = $manager.GetApplicationHostConfiguration()
        foreach ($name in @('system.applicationHost/sites','system.applicationHost/applicationPools','system.webServer/globalModules')) {
            if ($name -eq 'system.webServer/globalModules') {
                Read-Scope 'server/global-modules' { Iis-Element ($config.GetSection($name)) }
            } else {
                $kind = if ($name -like '*/sites') { 'sites' } else { 'pools' }
                $objects = $null
                Read-Scope ($kind + '/discovery') {
                    $script:IisDiscovered = @($config.GetSection($name).GetCollection())
                    @($script:IisDiscovered | ForEach-Object { @{name=[string]$_.GetAttributeValue('name');key=(Scope-Key ([string]$_.GetAttributeValue('name')))} })
                } -Rows
                $objects = $script:IisDiscovered; $script:IisDiscovered = $null
                foreach ($object in $objects) {
                    $identity = [string]$object.GetAttributeValue('name'); $key = Scope-Key $identity
                    Read-Scope ($kind + '/' + $key + '/definition') { Iis-Element $object }
                }
            }
        }
        foreach ($name in @('siteDefaults','applicationDefaults','virtualDirectoryDefaults')) {
            Read-Scope ('server/' + $name.ToLowerInvariant()) { Iis-Element ($config.GetSection('system.applicationHost/sites').GetChildElement($name)) }
        }
        Read-Scope 'server/pool-defaults' { Iis-Element ($config.GetSection('system.applicationHost/applicationPools').GetChildElement('applicationPoolDefaults')) }
        $sections = @('system.webServer/security/authentication/anonymousAuthentication','system.webServer/security/authentication/windowsAuthentication',
            'system.webServer/security/authentication/basicAuthentication','system.webServer/security/authorization','system.webServer/security/access',
            'system.webServer/security/requestFiltering','system.webServer/handlers','system.webServer/modules','system.webServer/defaultDocument',
            'system.webServer/directoryBrowse','system.webServer/httpErrors','system.webServer/httpLogging','system.webServer/httpProtocol',
            'system.webServer/httpRedirect','system.webServer/staticContent','system.webServer/urlCompression')
        if (Has-Detail 'iis-extended') {
            $sections += @('system.webServer/rewrite/rules','system.webServer/rewrite/outboundRules','system.webServer/rewrite/allowedServerVariables',
                'system.webServer/proxy','system.ftpServer/security/authentication/anonymousAuthentication','system.ftpServer/security/authentication/basicAuthentication',
                'system.ftpServer/security/authorization','system.ftpServer/security/ssl','system.ftpServer/log','system.ftpServer/messages','system.ftpServer/firewallSupport')
            Read-Scope 'extended/web-farms' { Iis-Element ($config.GetSection('webFarms')) }
            $script:IisLocations=$null
            Read-Scope 'extended/locations-discovery' { $script:IisLocations=@($config.GetLocationPaths()); $script:IisLocations } -Rows
            foreach ($location in $script:IisLocations) {
                foreach ($name in $sections) {
                    Read-Scope ('extended/locations/' + (Scope-Key $location) + '/' + (Scope-Key $name)) {
                        @{location=$location;section=$name;configuration=(Iis-Element ($config.GetSection($name,$location)))}
                    }
                }
            }
        }
        foreach ($name in $sections) { Read-Scope ('server/effective-' + (Scope-Key $name)) { @{section=$name;configuration=(Iis-Element ($config.GetSection($name)))} } }
        $sites = $null
        Read-Scope 'applications/discovery' {
            $script:IisSites = @($manager.Sites)
            @($script:IisSites | ForEach-Object { $siteName=$_.Name; $_.Applications | ForEach-Object { @{site=$siteName;path=$_.Path;pool=$_.ApplicationPoolName} } })
        } -Rows
        $sites=$script:IisSites; $script:IisSites=$null
        foreach ($site in $sites) {
            $key=Scope-Key $site.Name
            Read-Scope ('sites/' + $key + '/tls-bindings') {
                @($site.Bindings | ForEach-Object {
                    $row=@{protocol=$_.Protocol;binding=$_.BindingInformation}
                    if ($_.Protocol -eq 'https') { $row['certificate_hash']=Plain $_.CertificateHash; $row['certificate_store']=$_.CertificateStoreName; $row['ssl_flags']=Plain $_.SslFlags }
                    $row
                })
            } -Rows
            $script:IisApplications=$null
            Read-Scope ('sites/' + $key + '/applications-discovery') {
                $script:IisApplications=@($site.Applications)
                $script:IisApplications | ForEach-Object { @{path=$_.Path;pool=$_.ApplicationPoolName} }
            } -Rows
            foreach ($app in $script:IisApplications) {
                $appKey = Scope-Key ($site.Name + '|' + $app.Path)
                foreach ($name in $sections) {
                    Read-Scope ('applications/' + $appKey + '/' + (Scope-Key $name)) {
                        @{site=$site.Name;application=$app.Path;section=$name;configuration=(Iis-Element ($manager.GetWebConfiguration($site.Name,$app.Path).GetSection($name)))}
                    }
                }
                if (Has-Detail 'iis-extended') {
                    $script:AppLocations=$null
                    Read-Scope ('extended/applications/' + $appKey + '/locations-discovery') {
                        $script:AppLocations=@($manager.GetWebConfiguration($site.Name,$app.Path).GetLocationPaths()); $script:AppLocations
                    } -Rows
                    foreach ($appLocation in $script:AppLocations) {
                        foreach ($name in $sections) {
                            Read-Scope ('extended/applications/' + $appKey + '/locations/' + (Scope-Key $appLocation) + '/' + (Scope-Key $name)) {
                                @{site=$site.Name;application=$app.Path;location=$appLocation;section=$name;configuration=(Iis-Element ($manager.GetWebConfiguration($site.Name,$app.Path).GetSection($name,$appLocation)))}
                            }
                        }
                    }
                }
            }
        }
    } finally { $manager.Dispose() }
}
function Read-Dns {
    Import-Module DnsServer -ErrorAction Stop
    Read-Scope 'server/settings' { Plain (Get-DnsServerSetting -All -ErrorAction Stop) }
    Read-Scope 'server/forwarders' { $data=Plain (Get-DnsServerForwarder -ErrorAction Stop); $data.Remove('ReorderedIPAddress'); $data }
    Read-Scope 'server/recursion' { Plain (Get-DnsServerRecursion -ErrorAction Stop) }
    Read-Scope 'server/aging' { $data=Plain (Get-DnsServerScavenging -ErrorAction Stop); $data.Remove('LastScavengeTime'); $data }
    Read-Scope 'server/root-hints' { Get-DnsServerRootHint -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    if (Has-Detail 'dns-policies') {
        Read-Scope 'extended/server-policies' { Get-DnsServerQueryResolutionPolicy -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
        Read-Scope 'extended/client-subnets' { Get-DnsServerClientSubnet -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
        Read-Scope 'extended/recursion-scopes' { Get-DnsServerRecursionScope -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    }
    $script:Zones = $null
    Read-Scope 'zones/discovery' {
        $script:Zones = @(Get-DnsServerZone -ErrorAction Stop)
        $script:Zones | ForEach-Object { @{name=$_.ZoneName;key=(Scope-Key $_.ZoneName)} }
    } -Rows
    foreach ($zone in $script:Zones) {
        $key=Scope-Key $zone.ZoneName
        Read-Scope ('zones/' + $key + '/settings') { Plain $zone }
        Read-Scope ('zones/' + $key + '/aging') { $data=Plain (Get-DnsServerZoneAging -Name $zone.ZoneName -ErrorAction Stop); $data.Remove('AvailForScavengeTime'); $data }
        Read-Scope ('zones/' + $key + '/records') {
            Get-DnsServerResourceRecord -ZoneName $zone.ZoneName -ErrorAction Stop | ForEach-Object {
                @{hostname=$_.HostName;type=$_.RecordType;record_class=$_.RecordClass;ttl=(Plain $_.TimeToLive);data=(Plain $_.RecordData)}
            }
        } -Rows
        if (Has-Detail 'dns-policies') {
            Read-Scope ('extended/zones/' + $key + '/policies') { Get-DnsServerQueryResolutionPolicy -ZoneName $zone.ZoneName -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
            $script:ZoneScopes=$null
            Read-Scope ('extended/zones/' + $key + '/scopes') { $script:ZoneScopes=@(Get-DnsServerZoneScope -ZoneName $zone.ZoneName -ErrorAction Stop); $script:ZoneScopes | ForEach-Object { Plain $_ } } -Rows
            foreach ($zoneScope in $script:ZoneScopes) {
                Read-Scope ('extended/zones/' + $key + '/scope-records/' + (Scope-Key $zoneScope.ZoneScope)) {
                    Get-DnsServerResourceRecord -ZoneName $zone.ZoneName -ZoneScope $zoneScope.ZoneScope -ErrorAction Stop | ForEach-Object {
                        @{hostname=$_.HostName;type=$_.RecordType;record_class=$_.RecordClass;ttl=(Plain $_.TimeToLive);data=(Plain $_.RecordData)}
                    }
                } -Rows
            }
        }
    }
}
function Read-Dhcp {
    Import-Module DhcpServer -ErrorAction Stop
    Read-Scope 'server/v4-settings' { Plain (Get-DhcpServerv4Setting -ErrorAction Stop) }
    Read-Scope 'server/v6-settings' { Plain (Get-DhcpServerv6Setting -ErrorAction Stop) }
    Read-Scope 'server/v4-dns' { Plain (Get-DhcpServerv4DnsSetting -ErrorAction Stop) }
    Read-Scope 'server/v4-options' { Get-DhcpServerv4OptionValue -All -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    Read-Scope 'server/v6-options' { Get-DhcpServerv6OptionValue -All -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    Read-Scope 'server/v4-option-definitions' { Get-DhcpServerv4OptionDefinition -All -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    Read-Scope 'server/v6-option-definitions' { Get-DhcpServerv6OptionDefinition -All -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    Read-Scope 'server/v4-classes' { Get-DhcpServerv4Class -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    Read-Scope 'server/v4-filters' { Get-DhcpServerv4Filter -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    Read-Scope 'server/v4-filter-list' { Plain (Get-DhcpServerv4FilterList -ErrorAction Stop) }
    Read-Scope 'server/v4-failover' {
        Get-DhcpServerv4Failover -ErrorAction Stop | ForEach-Object {
            Fields $_ @('Name','Mode','PartnerServer','ScopeId','MaxClientLeadTime','AutoStateTransition','StateSwitchInterval','LoadBalancePercent','ReservePercent','ServerRole')
        }
    } -Rows
    Read-Scope 'server/v4-policies' { Get-DhcpServerv4Policy -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    foreach ($version in @('v4','v6')) {
        $script:DhcpScopes=$null
        Read-Scope ($version + '/discovery') {
            $script:DhcpScopes = @(& ('Get-DhcpServer' + $version + 'Scope') -ErrorAction Stop)
            $script:DhcpScopes | ForEach-Object { $id=if ($version -eq 'v4') { [string]$_.ScopeId } else { [string]$_.Prefix }; @{id=$id;key=(Scope-Key $id)} }
        } -Rows
        foreach ($scope in $script:DhcpScopes) {
            $id=if ($version -eq 'v4') { [string]$scope.ScopeId } else { [string]$scope.Prefix }
            $key=Scope-Key $id; $path=$version + '/' + $key
            Read-Scope ($path + '/settings') { Plain $scope }
            $param=if ($version -eq 'v4') { @{ScopeId=$id} } else { @{Prefix=$id} }
            Read-Scope ($path + '/options') { & ('Get-DhcpServer' + $version + 'OptionValue') @param -All -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
            Read-Scope ($path + '/reservations') { & ('Get-DhcpServer' + $version + 'Reservation') @param -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
            if (Has-Detail 'dhcp-reservation-options') {
                $script:DetailedReservations=$null
                Read-Scope ('extended/' + $path + '/reservations-discovery') {
                    $script:DetailedReservations=@(& ('Get-DhcpServer' + $version + 'Reservation') @param -ErrorAction Stop)
                    $script:DetailedReservations | ForEach-Object { @{address=[string]$_.IPAddress;key=(Scope-Key ([string]$_.IPAddress))} }
                } -Rows
                foreach ($reservation in $script:DetailedReservations) {
                    Read-Scope ('extended/' + $path + '/reservation-options/' + (Scope-Key ([string]$reservation.IPAddress))) {
                        & ('Get-DhcpServer' + $version + 'OptionValue') -ReservedIP $reservation.IPAddress -All -ErrorAction Stop | ForEach-Object { Plain $_ }
                    } -Rows
                }
            }
            Read-Scope ($path + '/exclusions') { & ('Get-DhcpServer' + $version + 'ExclusionRange') @param -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
            if ($version -eq 'v4') {
                Read-Scope ($path + '/dns') { Plain (Get-DhcpServerv4DnsSetting -ScopeId $id -ErrorAction Stop) }
                $script:Policies=$null
                Read-Scope ($path + '/policies') { $script:Policies=@(Get-DhcpServerv4Policy -ScopeId $id -ErrorAction Stop); $script:Policies | ForEach-Object { Plain $_ } } -Rows
                foreach ($policy in $script:Policies) {
                    $policyKey=Scope-Key $policy.Name
                    Read-Scope ($path + '/policy-options/' + $policyKey) { Get-DhcpServerv4OptionValue -ScopeId $id -PolicyName $policy.Name -All -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
                    Read-Scope ($path + '/policy-ranges/' + $policyKey) { Get-DhcpServerv4PolicyIPRange -ScopeId $id -Name $policy.Name -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
                }
            }
        }
    }
}
function Read-Hyperv {
    Import-Module Hyper-V -ErrorAction Stop
    Read-Scope 'host/settings' { Fields (Get-VMHost -ErrorAction Stop) @('VirtualHardDiskPath','VirtualMachinePath','EnableEnhancedSessionMode','VirtualMachineMigrationEnabled','VirtualMachineMigrationAuthenticationType','MaximumVirtualMachineMigrations','MaximumStorageMigrations','NumaSpanningEnabled') }
    Read-Scope 'host/switches' {
        Get-VMSwitch -ErrorAction Stop | ForEach-Object { Fields $_ @('Name','Id','SwitchType','NetAdapterInterfaceDescriptions','AllowManagementOS','EmbeddedTeamingEnabled','IovEnabled','MinimumBandwidthMode') }
    } -Rows
    if (Has-Detail 'hyperv-replication') {
        Read-Scope 'extended/replication-server' { Fields (Get-VMReplicationServer -ErrorAction Stop) @('ReplicationEnabled','AllowedAuthenticationType','KerberosAuthenticationPort','CertificateAuthenticationPort','CertificateThumbprint','DefaultStorageLocation','ReplicationAllowedFromAnyServer') }
        Read-Scope 'extended/replication-authorization' { Get-VMReplicationAuthorizationEntry -ErrorAction Stop | ForEach-Object { Fields $_ @('AllowedPrimaryServer','ReplicaStorageLocation','TrustGroup') } } -Rows
    }
    if (Has-Detail 'hyperv-network') {
        Read-Scope 'extended/switch-extensions' { Get-VMSwitch -ErrorAction Stop | Get-VMSwitchExtension -ErrorAction Stop | ForEach-Object { Fields $_ @('Id','Name','ExtensionType','Enabled','SwitchId','SwitchName') } } -Rows
        Read-Scope 'extended/switch-teams' { Get-VMSwitchTeam -ErrorAction Stop | ForEach-Object { Fields $_ @('Name','Id','NetAdapterInterfaceDescription','TeamingMode','LoadBalancingAlgorithm') } } -Rows
        Read-Scope 'extended/san' { Get-VMSan -ErrorAction Stop | ForEach-Object { Fields $_ @('Name','WorldWidePortNames') } } -Rows
    }
    $script:Vms=$null
    Read-Scope 'vms/discovery' { $script:Vms=@(Get-VM -ErrorAction Stop); $script:Vms | ForEach-Object { @{name=$_.Name;id=[string]$_.Id;key=(Scope-Key ([string]$_.Id))} } } -Rows
    foreach ($vm in $script:Vms) {
        $path='vms/' + (Scope-Key ([string]$vm.Id))
        Read-Scope ($path + '/settings') { Fields $vm @('Name','Id','Generation','Version','Path','ConfigurationLocation','SnapshotFileLocation','SmartPagingFilePath','AutomaticStartAction','AutomaticStartDelay','AutomaticStopAction','CheckpointType','Notes') }
        Read-Scope ($path + '/memory') { Fields (Get-VMMemory -VM $vm -ErrorAction Stop) @('DynamicMemoryEnabled','Startup','Minimum','Maximum','Buffer','Priority') }
        Read-Scope ($path + '/processor') { Fields (Get-VMProcessor -VM $vm -ErrorAction Stop) @('Count','Reserve','Maximum','RelativeWeight','CompatibilityForMigrationEnabled','CompatibilityForOlderOperatingSystemsEnabled','ExposeVirtualizationExtensions') }
        Read-Scope ($path + '/disks') { Get-VMHardDiskDrive -VM $vm -ErrorAction Stop | ForEach-Object { Fields $_ @('ControllerType','ControllerNumber','ControllerLocation','Path','DiskNumber','SupportPersistentReservations','MinimumIOPS','MaximumIOPS') } } -Rows
        Read-Scope ($path + '/dvd') { Get-VMDvdDrive -VM $vm -ErrorAction Stop | ForEach-Object { Fields $_ @('ControllerNumber','ControllerLocation','Path') } } -Rows
        Read-Scope ($path + '/network') {
            Get-VMNetworkAdapter -VM $vm -ErrorAction Stop | ForEach-Object {
                $nic=Fields $_ @('Name','Id','SwitchName','SwitchId','StaticMacAddress','MacAddress','MacAddressSpoofing','DhcpGuard','RouterGuard','AllowTeaming','MinimumBandwidthAbsolute','MinimumBandwidthWeight','MaximumBandwidth','IovWeight')
                if (-not $_.StaticMacAddress) { $nic['MacAddress']='[DYNAMIC ADDRESS: RUNTIME]' }
                $nic
            }
        } -Rows
        Read-Scope ($path + '/vlan') { Get-VMNetworkAdapter -VM $vm -ErrorAction Stop | Get-VMNetworkAdapterVlan -ErrorAction Stop | ForEach-Object { $data=Fields $_ @('OperationMode','AccessVlanId','NativeVlanId','AllowedVlanIdList','PrimaryVlanId','SecondaryVlanId','SecondaryVlanIdList'); $data['adapter_name']=$_.ParentAdapter.Name; $data } } -Rows
        Read-Scope ($path + '/boot') {
            if ($vm.Generation -eq 2) { Fields (Get-VMFirmware -VM $vm -ErrorAction Stop) @('EnableSecureBoot','SecureBootTemplate','BootOrder') }
            else { Fields (Get-VMBios -VM $vm -ErrorAction Stop) @('StartupOrder','NumLockEnabled') }
        }
        Read-Scope ($path + '/security') { Fields (Get-VMSecurity -VM $vm -ErrorAction Stop) @('TpmEnabled','KsdEnabled','Shielded','EncryptStateAndVmMigrationTraffic','VirtualizationBasedSecurityOptOut') }
        if (Has-Detail 'hyperv-replication') {
            Read-Scope ('extended/' + $path + '/replication') { Get-VMReplication -VM $vm -ErrorAction Stop | ForEach-Object { Fields $_ @('VMName','VMId','PrimaryServer','ReplicaServer','ReplicaPort','AuthenticationType','CertificateThumbprint','CompressionEnabled','FrequencySec','RecoveryHistory','ReplicationMode','ReplicationRelationshipType') } } -Rows
        }
        if (Has-Detail 'hyperv-network') {
            Read-Scope ('extended/' + $path + '/network-acls') { Get-VMNetworkAdapter -VM $vm -ErrorAction Stop | Get-VMNetworkAdapterAcl -ErrorAction Stop | ForEach-Object { Fields $_ @('LocalAddress','RemoteAddress','Direction','Action','VMName','VMNetworkAdapterName') } } -Rows
            Read-Scope ('extended/' + $path + '/network-extended-acls') { Get-VMNetworkAdapter -VM $vm -ErrorAction Stop | Get-VMNetworkAdapterExtendedAcl -ErrorAction Stop | ForEach-Object { Fields $_ @('LocalIPAddress','RemoteIPAddress','LocalPort','RemotePort','Protocol','Direction','Action','Weight','Stateful','IdleSessionTimeout') } } -Rows
            Read-Scope ('extended/' + $path + '/network-isolation') { Get-VMNetworkAdapter -VM $vm -ErrorAction Stop | Get-VMNetworkAdapterIsolation -ErrorAction Stop | ForEach-Object { Fields $_ @('IsolationMode','AllowUntaggedTraffic','DefaultIsolationID','MultiTenantStack') } } -Rows
        }
    }
}
function Read-Certificates {
    foreach ($store in @('LocalMachine\My','LocalMachine\WebHosting','LocalMachine\Root','LocalMachine\CA','CurrentUser\My')) {
        Read-Scope ('stores/' + (Scope-Key $store)) {
            Get-ChildItem -LiteralPath ('Cert:\' + $store) -ErrorAction Stop | ForEach-Object {
                $cert=$_
                @{store=$store;thumbprint=$cert.Thumbprint;subject=$cert.Subject;issuer=$cert.Issuer;serial=$cert.SerialNumber;
                  not_before=$cert.NotBefore.ToUniversalTime().ToString('o');not_after=$cert.NotAfter.ToUniversalTime().ToString('o');
                  has_private_key=[bool]$cert.HasPrivateKey;signature_algorithm=$cert.SignatureAlgorithm.Value;
                  public_key_algorithm=$cert.PublicKey.Oid.Value;
                  extensions=@($cert.Extensions | Sort-Object { $_.Oid.Value } | ForEach-Object { @{oid=$_.Oid.Value;critical=$_.Critical;public_der=[Convert]::ToBase64String($_.RawData)} })}
            }
        } -Rows
    }
    Read-Scope 'bindings/iis' {
        $dll=Join-Path ([Environment]::GetFolderPath('Windows')) 'System32\inetsrv\Microsoft.Web.Administration.dll'
        Add-Type -Path $dll
        $certManager=New-Object Microsoft.Web.Administration.ServerManager
        try {
            foreach ($certSite in $certManager.Sites) {
                foreach ($binding in $certSite.Bindings) {
                    if ($binding.Protocol -eq 'https') { @{site=$certSite.Name;binding=$binding.BindingInformation;certificate_hash=([BitConverter]::ToString($binding.CertificateHash)).Replace('-','');certificate_store=$binding.CertificateStoreName;ssl_flags=[string]$binding.SslFlags} }
                }
            }
        } finally { $certManager.Dispose() }
    } -Rows
}

function Read-Dfs {
    # Namespace discovery is scoped to this machine, not a domain-wide inventory.
    $script:DfsRoots=$null
    Read-Scope 'namespaces/discovery' {
        Import-Module DFSN -ErrorAction Stop
        $script:DfsRoots=@(Get-DfsnRoot -ComputerName $env:COMPUTERNAME -ErrorAction Stop)
        $script:DfsRoots | ForEach-Object { Fields $_ @('Path','Type','State','Description','TimeToLiveSec','EnableAccessBasedEnumeration','EnableSiteCosting','EnableInsiteReferrals','EnableRootScalability') }
    } -Rows
    foreach ($dfsRoot in $script:DfsRoots) {
        $dfsKey=Scope-Key $dfsRoot.Path
        Read-Scope ('namespaces/' + $dfsKey + '/targets') { Get-DfsnRootTarget -Path $dfsRoot.Path -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
        $script:DfsFolders=$null
        Read-Scope ('namespaces/' + $dfsKey + '/folders') { $script:DfsFolders=@(Get-DfsnFolder -Path ($dfsRoot.Path.TrimEnd('\') + '\*') -ErrorAction Stop); $script:DfsFolders | ForEach-Object { Plain $_ } } -Rows
        foreach ($dfsFolder in $script:DfsFolders) {
            Read-Scope ('namespaces/' + $dfsKey + '/folder-targets/' + (Scope-Key $dfsFolder.Path)) { Get-DfsnFolderTarget -Path $dfsFolder.Path -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
        }
    }
    foreach ($class in @('DfsrMachineConfig','DfsrReplicationGroupConfig','DfsrReplicatedFolderConfig','DfsrConnectionConfig')) {
        Read-Scope ('replication/' + $class.ToLowerInvariant()) { Get-CimInstance -Namespace 'root\MicrosoftDFS' -ClassName $class -ErrorAction Stop | ForEach-Object { Plain $_ } } -Rows
    }
}

function Parse-NpsPolicySummary([string]$Text) {
    # netsh has no stable structured policy read API. Only recognized English
    # metadata labels are accepted; no raw output or condition/profile blobs.
    $rows=New-Object 'System.Collections.Generic.List[object]';$row=$null
    foreach ($line in ($Text -split '\r?\n')) {
        if ($line -match '^\s*(Name|Policy name)\s*[:=]\s*(.+?)\s*$') {
            if ($null -ne $row) { $rows.Add($row) }
            $row=[ordered]@{name=$matches[2]};continue
        }
        if ($null -ne $row -and $line -match '^\s*(State|Processing order|Policy source|Access permission)\s*[:=]\s*(.+?)\s*$') {
            $field=$matches[1].ToLowerInvariant().Replace(' ','_');$value=$matches[2]
            if ($field -eq 'processing_order' -and $value -notmatch '^\d+$') { throw 'Unrecognized policy order' }
            $row[$field]=$value
        }
    }
    if ($null -ne $row) { $rows.Add($row) }
    if ($rows.Count -eq 0 -and $Text -notmatch '(?i)there are no.*polic|no.*polic.*configured|no.*polic.*found') { throw 'Unsupported/localized NPS policy summary; raw output omitted' }
    return $rows.ToArray()
}
function Read-Nps {
    Read-Scope 'radius-clients' {
        Import-Module NPS -ErrorAction Stop
        Get-NpsRadiusClient -ErrorAction Stop | ForEach-Object { Fields $_ @('Name','Address','Enabled','VendorName') }
    } -Rows
    foreach ($policyType in @('np','crp')) {
        Read-Scope ('policies/' + $policyType) {
            $raw=& netsh.exe nps show $policyType 2>$null
            if ($LASTEXITCODE -ne 0) { throw 'NPS policy query failed' }
            Parse-NpsPolicySummary ($raw -join "`n")
        } -Rows
    }
}

function Public-Subscription($Element) {
    $allowed=@('Subscription','SubscriptionId','SubscriptionType','Description','Enabled','Uri','ConfigurationMode','Delivery','Batching','MaxItems','MaxLatencyTime','PushSettings','Heartbeat','Interval','PullSettings','ContentFormat','Locale','LogFile','PublisherName','CredentialsType','ReadExistingEvents','TransportName','TransportPort','Query','EventSources','EventSource','Address','AllowedSourceDomainComputers','AllowedSourceNonDomainComputers','AllowedIssuerCAList','IssuerCA','AllowedSubjectList','Subject')
    if ($Element.LocalName -notin $allowed) { return $null }
    $attributes=[ordered]@{}
    foreach ($a in $Element.Attributes) { if ($a.LocalName -in @('Enabled','Language','Mode')) { $attributes[$a.LocalName]=$a.Value } }
    $elementChildren=@($Element.ChildNodes | Where-Object { $_.NodeType -eq [Xml.XmlNodeType]::Element })
    $children=@($elementChildren | ForEach-Object { $p=Public-Subscription $_; if ($null -ne $p) { $p } })
    $value=if ($elementChildren.Count -eq 0) { $Element.InnerText } else { $null }
    if ($value -match '://[^/\s]+@|[?&](?:token|key|password|secret)=') { $value='[VALUE NOT COLLECTED]' }
    return @{element=$Element.LocalName;attributes=$attributes;value=$value;children=$children}
}
function Read-Wef {
    $script:Subscriptions=$null
    Read-Scope 'subscriptions/discovery' {
        $ids=& wecutil.exe es 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Subscription discovery failed' }
        $script:Subscriptions=@($ids | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        $script:Subscriptions | ForEach-Object { @{name=[string]$_;key=(Scope-Key ([string]$_))} }
    } -Rows
    foreach ($id in $script:Subscriptions) {
        Read-Scope ('subscriptions/' + (Scope-Key $id) + '/settings') {
            $raw=& wecutil.exe gs $id /f:xml 2>$null
            if ($LASTEXITCODE -ne 0) { throw 'Subscription read failed' }
            $settings=New-Object Xml.XmlReaderSettings;$settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit;$settings.XmlResolver=$null
            $reader=[Xml.XmlReader]::Create([IO.StringReader]::new(($raw -join "`n")),$settings)
            try { $document=New-Object Xml.XmlDocument;$document.XmlResolver=$null;$document.Load($reader);if ($document.DocumentElement.LocalName -ne 'Subscription') { throw 'Unexpected subscription XML' };Public-Subscription $document.DocumentElement }
            finally { $reader.Dispose() }
        }
    }
}

function Read-Wsus {
    Import-Module UpdateServices -ErrorAction Stop
    $wsus=Get-WsusServer -ErrorAction Stop
    Read-Scope 'server/settings' { Fields ($wsus.GetConfiguration()) @('SyncFromMicrosoftUpdate','UpstreamWsusServerName','UpstreamWsusServerPortNumber','UpstreamWsusServerUseSsl','IsReplicaServer','UseProxy','ProxyName','ProxyServerPort','AnonymousProxyAccess','AllUpdateLanguagesEnabled','DownloadUpdateBinariesAsNeeded','DownloadExpressPackages','HostBinariesOnMicrosoftUpdate','LocalContentCachePath') }
    Read-Scope 'server/languages' { @($wsus.GetConfiguration().GetEnabledUpdateLanguages()) } -Rows
    Read-Scope 'subscription/settings' { Fields ($wsus.GetSubscription()) @('SynchronizeAutomatically','SynchronizeAutomaticallyTimeOfDay','NumberOfSynchronizationsPerDay') }
    Read-Scope 'subscription/classifications' { $wsus.GetSubscription().GetUpdateClassifications() | ForEach-Object { Fields $_ @('Id','Title') } } -Rows
    Read-Scope 'subscription/categories' { $wsus.GetSubscription().GetUpdateCategories() | ForEach-Object { Fields $_ @('Id','Title','Type') } } -Rows
    Read-Scope 'computer-groups' { $wsus.GetComputerTargetGroups() | ForEach-Object { Fields $_ @('Id','Name') } } -Rows
}

function Registry-Fields([string]$Path,[string[]]$Names) {
    $key=Get-Item -LiteralPath $Path -ErrorAction Stop
    try {
        $available=@($key.GetValueNames());$values=[ordered]@{}
        foreach ($name in $Names) {
            if ($name -in $available) { $values[$name]=Plain ($key.GetValue($name,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)) }
        }
        return @{path=$Path;values=$values}
    } finally { $key.Close() }
}
function Read-Adcs {
    $base='HKLM:\SYSTEM\CurrentControlSet\Services\CertSvc\Configuration'
    $script:CaNames=$null
    Read-Scope 'authorities/discovery' {
        $script:CaNames=@(Get-ChildItem -LiteralPath $base -ErrorAction Stop | ForEach-Object { $_.PSChildName })
        $script:CaNames | ForEach-Object { @{name=$_;key=(Scope-Key $_)} }
    } -Rows
    foreach ($ca in $script:CaNames) {
        $path=$base+'\'+$ca;$caKey=Scope-Key $ca
        Read-Scope ('authorities/'+$caKey+'/settings') { Registry-Fields $path @('CAType','CAServerName','CommonName','DistinguishedName','CACertHash','ValidityPeriod','ValidityPeriodUnits','CRLPeriod','CRLPeriodUnits','CRLDeltaPeriod','CRLDeltaPeriodUnits','CRLOverlapPeriod','CRLOverlapUnits','CRLPublicationURLs','CACertPublicationURLs','AuditFilter','InterfaceFlags','EncryptionCsp','EncryptionAlgorithm','CNGPublicKeyAlgorithm','CNGHashAlgorithm') }
        Read-Scope ('authorities/'+$caKey+'/policy') { Registry-Fields ($path+'\PolicyModules\CertificateAuthority_MicrosoftDefault.Policy') @('EditFlags','RequestDisposition','EnableRequestExtensionList','DisableExtensionList','EnableEnrolleeRequestExtensionList') }
    }
}

try {
    switch ($Component) {
        'iis' { Read-Iis }
        'dns' { Read-Dns }
        'dhcp' { Read-Dhcp }
        'hyperv' { Read-Hyperv }
        'certificates' { Read-Certificates }
        'dfs' { Read-Dfs }
        'nps' { Read-Nps }
        'wef' { Read-Wef }
        'wsus' { Read-Wsus }
        'adcs' { Read-Adcs }
    }
} catch {
    # This sibling failure scope cannot overlap the successfully read subscopes.
    $script:Records.Add(@{scope=('windows-components/' + $Component + '/reader-failure');status='failed';error=($_.Exception.GetType().Name + ': ' + [string]$_.CategoryInfo.Category)})
}
@{schema_version=1;component=$Component;records=@($script:Records.ToArray())} | ConvertTo-Json -Depth 80 -Compress
