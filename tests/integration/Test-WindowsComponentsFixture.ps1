[CmdletBinding()]
param([string]$ReaderPath=(Join-Path $PSScriptRoot '../../collectors/system/Read-WindowsComponents.ps1'))
$ErrorActionPreference='Stop'
# Import only function definitions from the shipped reader. No live role queries.
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $ReaderPath),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors.Message -join '; ') }
foreach ($node in $ast.EndBlock.Statements) {
    if ($node -is [Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($node.Extent.Text)) }
}
$script:Records=New-Object 'System.Collections.Generic.List[object]'
$script:Selection=[pscustomobject]@{}
$Component='dns'
Read-Scope 'test/empty' { @() } -Rows
if ($script:Records[0].data.Count -ne 0) { throw 'Empty query was not retained as an empty array' }
$scope=[pscustomobject]@{ScopeId='10.0.0.0';Name='test'}
Read-Scope 'test/dynamic-scope' { Plain $scope }
if ($script:Records[1].data.Name -ne 'test') { throw 'Helper local variable shadows query data' }
Read-Scope 'test/failure' { throw 'fixture exception containing DO-NOT-RECORD-ME' }
if ($script:Records[2].status -ne 'failed' -or $script:Records[2].error -like '*DO-NOT-RECORD-ME*') { throw 'Failure diagnostics leaked an arbitrary exception value' }
$script:Selection=[pscustomobject]@{'windows-components/dns/test/disabled'=$false}
Read-Scope 'test/disabled' { throw 'query must not run' }
if ($script:Records[3].status -ne 'disabled') { throw 'Section switch was ignored' }
$script:Selection=[pscustomobject]@{}
# Use a throwing getter: sanitization must not even retrieve password values.
$password=[pscustomobject]@{Name='password'}
$password | Add-Member ScriptProperty Value { throw 'PASSWORD GETTER MUST NOT RUN' }
$attr=[pscustomobject]@{Name='name';Value='pool'}
$element=[pscustomobject]@{ElementTagName='add';Attributes=@($attr,$password);ChildElements=@();Schema=[pscustomobject]@{CollectionSchema=$null}}
$serialized=Iis-Element $element
if ($serialized.attributes.password -ne '[SECRET NOT COLLECTED]') { throw 'Password did not become a marker' }
$envElement=[pscustomobject]@{ElementTagName='environmentVariables';Attributes=@();ChildElements=@($element);Schema=[pscustomobject]@{CollectionSchema=$null}}
$envResult=Iis-Element $envElement
if ($envResult.children.add.attributes.password -ne '[SECRET NOT COLLECTED]') { throw 'Nested marker missing' }
$item1=[pscustomobject]@{ElementTagName='add';Attributes=@([pscustomobject]@{Name='name';Value='z'});ChildElements=@();Schema=[pscustomobject]@{CollectionSchema=$null}}
$item2=[pscustomobject]@{ElementTagName='add';Attributes=@([pscustomobject]@{Name='name';Value='a'});ChildElements=@();Schema=[pscustomobject]@{CollectionSchema=$null}}
$collection=[pscustomobject]@{ElementTagName='handlers';Attributes=@();ChildElements=@();Schema=[pscustomobject]@{CollectionSchema=[pscustomobject]@{}};FixtureItems=@($item1,$item2)}
$collection | Add-Member ScriptMethod GetCollection { return $this.FixtureItems }
$serialized=Iis-Element $collection
if ($serialized.items[0].attributes.name -ne 'z' -or $serialized.items[1].attributes.name -ne 'a') { throw 'IIS handler order changed' }
if ((Scope-Key 'Example.COM') -ne (Scope-Key 'example.com')) { throw 'Stable case-insensitive identity mismatch' }
# Actual DNS reader control flow against mock cmdlets, including per-zone failure.
function Import-Module { param($Name,$ErrorAction) }
function Get-DnsServerSetting { param([switch]$All,$ErrorAction); [pscustomobject]@{RoundRobin=$true} }
function Get-DnsServerForwarder { param($ErrorAction); [pscustomobject]@{IPAddress=@('192.0.2.2','192.0.2.1');ReorderedIPAddress=@('192.0.2.1','192.0.2.2')} }
function Get-DnsServerRecursion { param($ErrorAction); [pscustomobject]@{Enable=$true} }
function Get-DnsServerScavenging { param($ErrorAction); [pscustomobject]@{ScavengingState=$true;LastScavengeTime=[DateTime]::Now;ScavengingInterval=[TimeSpan]::FromDays(7)} }
function Get-DnsServerRootHint { param($ErrorAction); @() }
function Get-DnsServerZone { param($ErrorAction); @([pscustomobject]@{ZoneName='good.example';ZoneType='Primary'},[pscustomobject]@{ZoneName='denied.example';ZoneType='Primary'}) }
function Get-DnsServerZoneAging { param($Name,$ErrorAction); [pscustomobject]@{AgingEnabled=$true;AvailForScavengeTime=[DateTime]::Now} }
function Get-DnsServerResourceRecord {
    param($ZoneName,$ErrorAction)
    if ($ZoneName -eq 'denied.example') { throw [UnauthorizedAccessException]::new('fixture denial') }
    [pscustomobject]@{HostName='www';RecordType='A';RecordClass='IN';TimeToLive=[TimeSpan]::FromMinutes(5);Timestamp=[DateTime]::Now;RecordData=[pscustomobject]@{IPv4Address=[Net.IPAddress]::Parse('192.0.2.1')}}
}
$script:Records.Clear();Read-Dns
if (@($script:Records|Where-Object status -eq 'failed').Count -ne 1) { throw 'Expected exactly one failed DNS scope' }
if (-not @($script:Records|Where-Object { $_.scope -like '*/records' -and $_.status -eq 'complete' }).Count) { throw 'Healthy DNS zone was lost' }
$json=@($script:Records.ToArray())|ConvertTo-Json -Depth 80 -Compress
if ($json -match 'LastScavengeTime|ReorderedIPAddress|AvailForScavengeTime|Timestamp') { throw 'Known runtime fields leaked into config' }
if ($json -notmatch '192.0.2.2') { throw 'Configured forwarder priority was lost' }
# Second actual DNS pass must produce exactly the same JSON despite timestamps.
$script:Records.Clear();Read-Dns
$second=@($script:Records.ToArray())|ConvertTo-Json -Depth 80 -Compress
# Hashtable property order is not contractual. Canonicalization is tested in Python.
$firstObject=$json|ConvertFrom-Json;$secondObject=$second|ConvertFrom-Json
if ($firstObject.Count -ne $secondObject.Count) { throw 'Repeat scope count changed' }
# DHCP control flow uses only local fake responses; no DHCP service is contacted.
$Component='dhcp';$script:Records.Clear()
foreach ($command in @('Get-DhcpServerv4Setting','Get-DhcpServerv6Setting','Get-DhcpServerv4DnsSetting','Get-DhcpServerv4FilterList')) {
    Set-Item ('Function:'+$command) { [pscustomobject]@{Enabled=$true} }
}
foreach ($command in @('Get-DhcpServerv4OptionDefinition','Get-DhcpServerv6OptionDefinition','Get-DhcpServerv4Class','Get-DhcpServerv4Filter','Get-DhcpServerv4Failover','Get-DhcpServerv4Policy','Get-DhcpServerv4Reservation','Get-DhcpServerv6Reservation','Get-DhcpServerv4ExclusionRange','Get-DhcpServerv6ExclusionRange')) {
    Set-Item ('Function:'+$command) { @() }
}
function Get-DhcpServerv4OptionValue { [pscustomobject]@{OptionId=6;Value=@('192.0.2.2','192.0.2.1')} }
function Get-DhcpServerv6OptionValue { [pscustomobject]@{OptionId=23;Value=@('2001:db8::2','2001:db8::1')} }
function Get-DhcpServerv4Scope { [pscustomobject]@{ScopeId=[Net.IPAddress]::Parse('192.0.2.0');Name='fixture v4';State='Active'} }
function Get-DhcpServerv6Scope { [pscustomobject]@{Prefix=[Net.IPAddress]::Parse('2001:db8::');Name='fixture v6';State='Active'} }
Read-Dhcp
if (@($script:Records|Where-Object status -eq 'failed').Count) { throw 'DHCP fixture query unexpectedly failed' }
$v4=$script:Records|Where-Object { $_.scope -like '*/v4/*/settings' }
$v6=$script:Records|Where-Object { $_.scope -like '*/v6/*/settings' }
if ($v4.data.Name -ne 'fixture v4' -or $v6.data.Name -ne 'fixture v6') { throw 'DHCP scope identity was lost' }
$options=$script:Records|Where-Object { $_.scope -eq 'windows-components/dhcp/server/v4-options' }
if ($options.data[0].Value[0] -ne '192.0.2.2') { throw 'Ordered DHCP DNS-server values changed' }
# Hyper-V control flow, including an unreadable VM memory scope.
$Component='hyperv';$script:Records.Clear()
function Get-VMHost { [pscustomobject]@{VirtualHardDiskPath='C:\VHD';VirtualMachinePath='C:\VM';EnableEnhancedSessionMode=$true;VirtualMachineMigrationEnabled=$false;VirtualMachineMigrationAuthenticationType='Kerberos';MaximumVirtualMachineMigrations=2;MaximumStorageMigrations=2;NumaSpanningEnabled=$true} }
function Get-VMSwitch { @() }
function Get-VM { [pscustomobject]@{Name='fixture-vm';Id=[Guid]'00000000-0000-0000-0000-000000000001';Generation=2;Version='12.0';Path='C:\VM';ConfigurationLocation='C:\VM';SnapshotFileLocation='C:\VM';SmartPagingFilePath='C:\VM';AutomaticStartAction='Nothing';AutomaticStartDelay=0;AutomaticStopAction='Save';CheckpointType='Production';Notes='fixture'} }
function Get-VMMemory { throw [UnauthorizedAccessException]::new('fixture denial') }
function Get-VMProcessor { [pscustomobject]@{Count=4;Reserve=0;Maximum=100;RelativeWeight=100;CompatibilityForMigrationEnabled=$false;CompatibilityForOlderOperatingSystemsEnabled=$false;ExposeVirtualizationExtensions=$true} }
function Get-VMHardDiskDrive { @() }
function Get-VMDvdDrive { @() }
function Get-VMNetworkAdapter { @() }
function Get-VMNetworkAdapterVlan { @() }
function Get-VMFirmware { [pscustomobject]@{EnableSecureBoot='On';SecureBootTemplate='MicrosoftWindows';BootOrder=@('first','second')} }
function Get-VMSecurity { [pscustomobject]@{TpmEnabled=$true;KsdEnabled=$false;Shielded=$false;EncryptStateAndVmMigrationTraffic=$false;VirtualizationBasedSecurityOptOut=$false} }
Read-Hyperv
if (@($script:Records|Where-Object status -eq 'failed').Count -ne 1) { throw 'Expected one isolated Hyper-V memory failure' }
$processor=$script:Records|Where-Object { $_.scope -like '*/processor' }
if ($processor.status -ne 'complete' -or $processor.data.Count -ne 4) { throw 'Other VM configuration did not continue' }
$boot=$script:Records|Where-Object { $_.scope -like '*/boot' }
if ($boot.data.BootOrder[0] -ne 'first') { throw 'Boot priority ordering changed' }

[pscustomobject]@{status='PASS';live_windows=$false;fixture='Reader helpers, IIS secret/order fixtures, mocked DNS/DHCP/Hyper-V control flow'} | ConvertTo-Json -Compress
