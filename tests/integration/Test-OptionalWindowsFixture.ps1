[CmdletBinding()]
param([string]$ReaderPath=(Join-Path $PSScriptRoot '../../collectors/system/Read-WindowsComponents.ps1'))
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path $ReaderPath),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors.Message -join '; ') }
foreach ($node in $ast.EndBlock.Statements) {
    if ($node -is [Management.Automation.Language.FunctionDefinitionAst]) { . ([scriptblock]::Create($node.Extent.Text)) }
}
$script:Records=New-Object 'System.Collections.Generic.List[object]'
$script:Selection=[pscustomobject]@{};$script:Details=@()
function Assert($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Import-Module { param($Name,$ErrorAction) }

# XML ancestor filtering must never reconstruct discarded credential children.
[xml]$xml='<Subscription xmlns="http://schemas.microsoft.com/2006/03/windows/events/subscription"><SubscriptionId>example</SubscriptionId><EventSources><EventSource Enabled="true"><Address>host.example</Address><UserName>operator</UserName><Password>DO-NOT-SAVE</Password></EventSource><EventSource><Password>ONLY-SECRET-CHILD</Password></EventSource></EventSources><Credentials><Password>OTHER-SECRET</Password></Credentials></Subscription>'
$safe=Public-Subscription $xml.DocumentElement | ConvertTo-Json -Depth 80 -Compress
Assert ($safe -notmatch 'DO-NOT-SAVE|ONLY-SECRET-CHILD|OTHER-SECRET|operator') 'Subscription filtering leaked credentials'
Assert ($safe -match 'host.example') 'Public subscription address was lost'
$secrets=Plain @{password='DO-NOT-SAVE';nested=@{SharedSecret='DO-NOT-SAVE';valid='kept'}} | ConvertTo-Json -Depth 10
Assert ($secrets -notmatch 'DO-NOT-SAVE') 'Dictionary redaction leaked secrets'

$policies=@(Parse-NpsPolicySummary "Network policy configuration:`nName = Example`nState = Enabled`nProcessing order = 3`nSecret blob = DO-NOT-SAVE`nName: Second`nProcessing order: 9")
Assert ($policies.Count -eq 2 -and $policies[0].processing_order -eq '3') 'NPS public summary metadata was not parsed'
Assert (($policies|ConvertTo-Json) -notmatch 'DO-NOT-SAVE') 'NPS persisted an unrecognized field'
$rejected=$false;try { Parse-NpsPolicySummary 'Unrecognized localized format' } catch { $rejected=$true }
Assert $rejected 'Unknown NPS output became a false empty success'

# Real NPS reader orchestration with a getter that must never be accessed.
function Get-NpsRadiusClient {
    param($ErrorAction)
    $client=[pscustomobject]@{Name='client';Address='192.0.2.1';Enabled=$true;VendorName='RADIUS Standard'}
    $client|Add-Member ScriptProperty SharedSecret { throw 'SECRET GETTER MUST NOT RUN' }
    $client
}
function netsh.exe { $global:LASTEXITCODE=0; "Name: fixture`nState: Enabled`nProcessing order: 1" }
$Component='nps';Read-Nps
Assert (@($script:Records|Where-Object status -ne 'complete').Count -eq 0) 'NPS safe projections failed'
Assert (($script:Records|ConvertTo-Json -Depth 80) -notmatch 'SharedSecret') 'NPS exported the secret field'

# Real WEF reader: first subscription denied, second succeeds; no service start.
function wecutil.exe {
    param($Verb,$Identity,$Format)
    if ($Verb -eq 'es') { $global:LASTEXITCODE=0;return @('denied','good') }
    if ($Identity -eq 'denied') { $global:LASTEXITCODE=5;return 'SECRET-ERROR-MUST-NOT-SAVE' }
    $global:LASTEXITCODE=0
    '<Subscription><SubscriptionId>good</SubscriptionId><Enabled>true</Enabled><EventSources><EventSource><Address>source.example</Address><Password>DO-NOT-SAVE</Password></EventSource></EventSources></Subscription>'
}
$Component='wef';$script:Records.Clear();Read-Wef
Assert (@($script:Records|Where-Object status -eq 'failed').Count -eq 1) 'WEF denied query did not fail independently'
Assert (@($script:Records|Where-Object { $_.scope -like '*/settings' -and $_.status -eq 'complete' }).Count -eq 1) 'WEF healthy subscription disappeared'
Assert (($script:Records|ConvertTo-Json -Depth 80) -notmatch 'DO-NOT-SAVE|SECRET-ERROR') 'WEF persisted sensitive raw output'

# Optional DNS queries use real reader control flow, preserving scoped priority.
function Get-DnsServerSetting { [pscustomobject]@{RoundRobin=$true} }
function Get-DnsServerForwarder { [pscustomobject]@{IPAddress=@('192.0.2.2','192.0.2.1')} }
function Get-DnsServerRecursion { [pscustomobject]@{Enable=$true} }
function Get-DnsServerScavenging { [pscustomobject]@{ScavengingState=$true} }
function Get-DnsServerRootHint { @() }
function Get-DnsServerZone { [pscustomobject]@{ZoneName='example.test';ZoneType='Primary'} }
function Get-DnsServerZoneAging { [pscustomobject]@{AgingEnabled=$true} }
function Get-DnsServerQueryResolutionPolicy { param($ZoneName);[pscustomobject]@{Name='policy';ProcessingOrder=2;ZoneName=$ZoneName;Criteria=@('first','second')} }
function Get-DnsServerClientSubnet { [pscustomobject]@{Name='office';IPv4Subnet=@('192.0.2.0/24')} }
function Get-DnsServerRecursionScope { [pscustomobject]@{Name='public';EnableRecursion=$true} }
function Get-DnsServerZoneScope { @([pscustomobject]@{ZoneScope='good'},[pscustomobject]@{ZoneScope='denied'}) }
function Get-DnsServerResourceRecord {
    param($ZoneName,$ZoneScope,$ErrorAction)
    if ($ZoneScope -eq 'denied') { throw [UnauthorizedAccessException]::new('secret error') }
    [pscustomobject]@{HostName='www';RecordType='A';RecordClass='IN';TimeToLive=[TimeSpan]::FromMinutes(5);RecordData=[pscustomobject]@{IPv4Address='192.0.2.7'}}
}
$Component='dns';$script:Details=@('dns-policies');$script:Records.Clear();Read-Dns
Assert (@($script:Records|Where-Object status -eq 'failed').Count -eq 1) 'DNS scope failure was not isolated'
Assert (@($script:Records|Where-Object { $_.scope -like '*/scope-records/*' -and $_.status -eq 'complete' }).Count -eq 1) 'DNS healthy zone scope was lost'
$script:Details=@();$script:Records.Clear();Read-Dns
Assert (@($script:Records|Where-Object scope -like '*/extended/*').Count -eq 0) 'DNS optional details ran by default'

# DHCP reservation option calls must not also supply ScopeId (mutually exclusive).
function Get-DhcpServerv4Setting { [pscustomobject]@{ConflictDetectionAttempts=1} }
function Get-DhcpServerv6Setting { [pscustomobject]@{UnicastFlag=$false} }
function Get-DhcpServerv4DnsSetting { [pscustomobject]@{DynamicUpdates='Always'} }
function Get-DhcpServerv4FilterList { [pscustomobject]@{Allow=$false;Deny=$true} }
foreach ($cmd in @('Get-DhcpServerv4OptionDefinition','Get-DhcpServerv6OptionDefinition','Get-DhcpServerv4Class','Get-DhcpServerv4Filter','Get-DhcpServerv4Failover','Get-DhcpServerv4Policy','Get-DhcpServerv4ExclusionRange','Get-DhcpServerv6ExclusionRange')) {
    Set-Item ('Function:\'+$cmd) { @() }
}
function Get-DhcpServerv4Scope { [pscustomobject]@{ScopeId='192.0.2.0';Name='office'} }
function Get-DhcpServerv6Scope { @() }
function Get-DhcpServerv4Reservation { @([pscustomobject]@{IPAddress='192.0.2.10'},[pscustomobject]@{IPAddress='192.0.2.11'}) }
function Get-DhcpServerv6Reservation { @() }
$script:ReservationArguments=New-Object 'System.Collections.Generic.List[object]'
function Get-DhcpServerv4OptionValue {
    param($ScopeId,$ReservedIP,[switch]$All,$ErrorAction)
    if ($ReservedIP) {
        $script:ReservationArguments.Add(@{scope=$ScopeId;address=[string]$ReservedIP})
        if ($ReservedIP -eq '192.0.2.10') { throw 'denied reservation' }
    }
    [pscustomobject]@{OptionId=6;Value=@('192.0.2.2','192.0.2.1')}
}
function Get-DhcpServerv6OptionValue { @() }
$Component='dhcp';$script:Details=@('dhcp-reservation-options');$script:Records.Clear();Read-Dhcp
Assert ($script:ReservationArguments.Count -eq 2) 'Reservation override queries missing'
Assert (@($script:ReservationArguments|Where-Object scope).Count -eq 0) 'Reservation query included invalid ScopeId parameter'
Assert (@($script:Records|Where-Object { $_.scope -like '*/reservation-options/*' -and $_.status -eq 'complete' }).Count -eq 1) 'Healthy DHCP reservation override lost'

# DFS replication configuration remains independent of denied namespace discovery.
function Get-DfsnRoot { throw [UnauthorizedAccessException]::new('denied') }
function Get-CimInstance { param($Namespace,$ClassName,$ErrorAction);[pscustomobject]@{Name='replication';Class=$ClassName;Enabled=$true} }
$Component='dfs';$script:Details=@();$script:Records.Clear();Read-Dfs
Assert (@($script:Records|Where-Object status -eq 'complete').Count -eq 4) 'DFS replication reads stopped after namespace failure'

# ADCS exact registry allowlist never reads a private key or an unknown value.
$script:RegistryReads=New-Object 'System.Collections.Generic.List[string]'
function Get-ChildItem { param($LiteralPath,$ErrorAction);[pscustomobject]@{PSChildName='FixtureCA'} }
function Get-Item {
    param($LiteralPath,$ErrorAction)
    $fake=[pscustomobject]@{}
    $fake|Add-Member ScriptMethod GetValueNames { @('CAType','CACertHash','PrivateKey','Password') }
    $fake|Add-Member ScriptMethod GetValue { param($Name,$Default,$Options);$script:RegistryReads.Add($Name);if ($Name -in @('PrivateKey','Password')) { throw 'SECRET GETTER' };if ($Name -eq 'CAType') { return 0 };return 'PUBLIC-CERT-HASH' }
    $fake|Add-Member ScriptMethod Close { }
    $fake
}
$Component='adcs';$script:Records.Clear();Read-Adcs
Assert (@($script:Records|Where-Object status -ne 'complete').Count -eq 0) 'ADCS public configuration query failed'
Assert (@($script:RegistryReads|Where-Object { $_ -in @('Password','PrivateKey') }).Count -eq 0) 'ADCS read a prohibited value'

# Public certificate projection must not touch a private-key accessor.
function Add-Type { throw 'Unavailable MWA fixture; never loads a real role assembly' }
function Get-ChildItem {
    param($LiteralPath,$ErrorAction)
    $cert=[pscustomobject]@{Thumbprint='ABCDEF';Subject='CN=fixture';Issuer='CN=fixture';SerialNumber='01';NotBefore=[DateTime]'2025-01-01Z';NotAfter=[DateTime]'2030-01-01Z';HasPrivateKey=$true;SignatureAlgorithm=[pscustomobject]@{Value='1.2.840.113549.1.1.11'};PublicKey=[pscustomobject]@{Oid=[pscustomobject]@{Value='1.2.840.113549.1.1.1'}};Extensions=@()}
    $cert|Add-Member ScriptProperty PrivateKey { throw 'PRIVATE KEY GETTER MUST NOT RUN' }
    $cert
}
$Component='certificates';$script:Records.Clear();Read-Certificates
Assert (@($script:Records|Where-Object { $_.scope -like '*/stores/*' -and $_.status -eq 'complete' }).Count -eq 5) 'Public certificate stores did not continue independently'
Assert (@($script:Records|Where-Object { $_.scope -like '*/bindings/*' -and $_.status -eq 'failed' }).Count -eq 1) 'Expected unavailable real MWA binding query in portable fixture'

# WSUS methods are reads only. Failed categories cannot erase server settings.
function Get-WsusServer {
    $server=[pscustomobject]@{}
    $server|Add-Member ScriptMethod GetConfiguration {
        $config=[pscustomobject]@{SyncFromMicrosoftUpdate=$true;UpstreamWsusServerName='';UpstreamWsusServerPortNumber=8530;UpstreamWsusServerUseSsl=$false;IsReplicaServer=$false;UseProxy=$true;ProxyName='proxy.example';ProxyServerPort=8080;AnonymousProxyAccess=$false;AllUpdateLanguagesEnabled=$false;DownloadUpdateBinariesAsNeeded=$true;DownloadExpressPackages=$false;HostBinariesOnMicrosoftUpdate=$false;LocalContentCachePath='C:\WSUS'}
        $config|Add-Member ScriptProperty ProxyPassword { throw 'PASSWORD GETTER MUST NOT RUN' }
        $config|Add-Member ScriptMethod GetEnabledUpdateLanguages { @('en','fr') };$config
    }
    $server|Add-Member ScriptMethod GetSubscription {
        $subscription=[pscustomobject]@{SynchronizeAutomatically=$true;SynchronizeAutomaticallyTimeOfDay=[TimeSpan]::FromHours(3);NumberOfSynchronizationsPerDay=1}
        $subscription|Add-Member ScriptMethod GetUpdateClassifications { [pscustomobject]@{Id='id';Title='Security Updates'} }
        $subscription|Add-Member ScriptMethod GetUpdateCategories { throw 'permission denied' };$subscription
    }
    $server|Add-Member ScriptMethod GetComputerTargetGroups { [pscustomobject]@{Id='group-id';Name='Servers'} };$server
}
$Component='wsus';$script:Records.Clear();Read-Wsus
Assert (@($script:Records|Where-Object status -eq 'failed').Count -eq 1) 'WSUS category failure was not isolated'
Assert (@($script:Records|Where-Object status -eq 'complete').Count -eq 5) 'WSUS healthy settings were lost'
Assert (($script:Records|ConvertTo-Json -Depth 80) -notmatch 'ProxyPassword') 'WSUS secret property was projected'

@{status='PASS';live_windows=$false;fixture='Optional WEF/NPS/DNS/DHCP/DFS/ADCS/certificate/WSUS reader control flow, secrets and query isolation. IIS/Hyper-V base fixtures remain separate.'}|ConvertTo-Json -Compress
