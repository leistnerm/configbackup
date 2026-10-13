[CmdletBinding()]
param([string]$ReaderPath=(Join-Path $PSScriptRoot '../../collectors/system/Read-WindowsComponents.ps1'))
$ErrorActionPreference='Stop'
# Imports reader functions and known base VM mocks; no real Hyper-V cmdlets run.
$null=. (Join-Path $PSScriptRoot 'Test-WindowsComponentsFixture.ps1') -ReaderPath $ReaderPath
function Get-VMReplicationServer { [pscustomobject]@{ReplicationEnabled=$true;AllowedAuthenticationType='Certificate';KerberosAuthenticationPort=80;CertificateAuthenticationPort=443;CertificateThumbprint='PUBLIC';DefaultStorageLocation='D:\Replica';ReplicationAllowedFromAnyServer=$false} }
function Get-VMReplicationAuthorizationEntry { [pscustomobject]@{AllowedPrimaryServer='primary.example';ReplicaStorageLocation='D:\Replica';TrustGroup='fixture'} }
function Get-VMReplication {
    param($VM,$ErrorAction)
    [pscustomobject]@{VMName=$VM.Name;VMId=$VM.Id;PrimaryServer='primary.example';ReplicaServer='replica.example';ReplicaPort=443;AuthenticationType='Certificate';CertificateThumbprint='PUBLIC';CompressionEnabled=$true;FrequencySec=300;RecoveryHistory=4;ReplicationMode='Primary';ReplicationRelationshipType='Simple';LastReplicationTime=[DateTime]::Now;Health='Normal'}
}
function Get-VMSwitchExtension { [pscustomobject]@{Id='extension';Name='capture';ExtensionType='Capture';Enabled=$false;SwitchId='switch';SwitchName='external'} }
function Get-VMSwitchTeam { [pscustomobject]@{Name='external';Id='switch';NetAdapterInterfaceDescription=@('adapter2','adapter1');TeamingMode='SwitchIndependent';LoadBalancingAlgorithm='Dynamic'} }
function Get-VMSan { [pscustomobject]@{Name='san';WorldWidePortNames=@('abc','def')} }
function Get-VMNetworkAdapterAcl { [pscustomobject]@{LocalAddress='192.0.2.0/24';RemoteAddress='*';Direction='Both';Action='Deny';VMName='fixture';VMNetworkAdapterName='Network Adapter'} }
function Get-VMNetworkAdapterExtendedAcl { throw [UnauthorizedAccessException]::new('fixture ACL access denial') }
function Get-VMNetworkAdapterIsolation { [pscustomobject]@{IsolationMode='Vlan';AllowUntaggedTraffic=$false;DefaultIsolationID=10;MultiTenantStack='Off'} }
$Component='hyperv';$script:Details=@('hyperv-network','hyperv-replication');$script:Records.Clear();Read-Hyperv
if (-not @($script:Records|Where-Object { $_.scope -like '*/replication' -and $_.status -eq 'complete' }).Count) { throw 'VM replication settings were not read' }
if (-not @($script:Records|Where-Object { $_.scope -like '*/network-acls' -and $_.status -eq 'complete' }).Count) { throw 'Healthy ACL scope was lost' }
if (-not @($script:Records|Where-Object { $_.scope -like '*/network-extended-acls' -and $_.status -eq 'failed' }).Count) { throw 'Denied extended ACL scope did not fail' }
if (($script:Records|ConvertTo-Json -Depth 80) -match 'LastReplicationTime') { throw 'Replication runtime timestamp leaked into configuration' }
$script:Details=@();$script:Records.Clear();Read-Hyperv
if (@($script:Records|Where-Object scope -like '*/extended/*').Count) { throw 'Optional Hyper-V details ran by default' }
@{status='PASS';live_hyperv=$false;fixture='Actual extended Hyper-V reader with mock configuration, runtime omission and independent ACL failure; default off'}|ConvertTo-Json -Compress
