#Requires -Version 7.2
<#
Optional service adapter. SSRS uses ReportService2010 SOAP, SSAS uses read-only
XMLA Discover through the SqlServer module, and WSFC uses FailoverClusters.
See docs/sql-services.md for coverage/permission limits. No server settings change.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory=$env:CONFIGBACKUP_OUTPUT,
    [uri]$ReportServerUri,
    [pscredential]$ReportCredential,
    [string]$AnalysisServer,
    [string]$ClusterName,
    [string[]]$ConfigurationFile=@(),
    [switch]$IncludeLocalServiceInventory
)
$ErrorActionPreference='Stop'
$script:Sections=@()
$script:Utf8=[Text.UTF8Encoding]::new($false)
function Write-ServiceText([string]$Path,[string]$Text) {
    $target=Join-Path $OutputDirectory $Path
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))|Out-Null
    [IO.File]::WriteAllText($target,$Text,$script:Utf8)
}
function Write-ServiceJson([string]$Path,$Data) {
    Write-ServiceText $Path (($Data|ConvertTo-Json -Depth 60)+"`n")
}
function Get-ServiceKey([string]$Value) {
    # Entire digest avoids case/path collisions on Windows and macOS.
    [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Value))).ToLowerInvariant()
}
function Save-ServiceManifest([bool]$Finalized=$false) {
    Write-ServiceJson 'collection-manifest.json.tmp' ([ordered]@{schema_version=1;run_id=[string]$env:CONFIGBACKUP_RUN_ID;finalized=$Finalized;sections=@($script:Sections)})
    Move-Item -LiteralPath (Join-Path $OutputDirectory 'collection-manifest.json.tmp') -Destination (Join-Path $OutputDirectory 'collection-manifest.json') -Force
}
function Invoke-ServiceSection([string]$SectionPath,[scriptblock]$Action) {
    $status='complete';$message='';$files=[ordered]@{}
    try {
        & $Action | Out-Null
        $target=Join-Path $OutputDirectory $SectionPath
        if(Test-Path -LiteralPath $target) {
            $items=if(Test-Path -LiteralPath $target -PathType Leaf){@(Get-Item -LiteralPath $target)}else{@(Get-ChildItem -LiteralPath $target -File -Recurse)}
            foreach($file in $items|Sort-Object FullName) {
                if($file.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Symlink output rejected'}
                $relative=[IO.Path]::GetRelativePath($OutputDirectory,$file.FullName).Replace('\','/')
                $files[$relative]=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
    } catch {
        $status='failed';$message=$_.Exception.Message;$files=[ordered]@{}
        Write-Warning "$SectionPath failed; prior snapshot preserved: $message"
    }
    $script:Sections += [ordered]@{path=$SectionPath;status=$status;error=$message;files=$files}
    Save-ServiceManifest
}
function Invoke-ReportRead([string]$Method,[string]$Parameters='') {
    if($Method -notin @('ListChildren','GetItemDefinition','GetItemLink','GetPolicies','GetSystemProperties','GetSystemPolicies','ListSchedules','ListSubscriptions','GetSubscriptionProperties','GetDataDrivenSubscriptionProperties')){throw 'Only approved read methods are allowed'}
    $namespace='http://schemas.microsoft.com/sqlserver/reporting/2010/03/01/ReportServer'
    $body='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><s:Body><'+$Method+' xmlns="'+$namespace+'">'+$Parameters+'</'+$Method+'></s:Body></s:Envelope>'
    $request=@{Uri=$ReportServerUri;Method='Post';ContentType='text/xml; charset=utf-8';Headers=@{SOAPAction=('"'+$namespace+'/'+$Method+'"')};Body=[Text.Encoding]::UTF8.GetBytes($body);TimeoutSec=120;MaximumRedirection=0}
    if($ReportServerUri.Scheme -eq 'http') {
        if($ReportCredential){throw 'Credentials require HTTPS'}
        # Loopback HTTP is only for unauthenticated protocol fixtures.
    } elseif($ReportCredential){$request.Credential=$ReportCredential}else{$request.UseDefaultCredentials=$true}
    $response=Invoke-WebRequest @request
    $settings=[Xml.XmlReaderSettings]::new();$settings.DtdProcessing=[Xml.DtdProcessing]::Prohibit;$settings.XmlResolver=$null
    $reader=[Xml.XmlReader]::Create([IO.StringReader]::new([string]$response.Content),$settings)
    try {$xml=[xml]::new();$xml.Load($reader)}finally{$reader.Dispose()}
    if($xml.SelectSingleNode('//*[local-name()="Fault"]')){throw "SSRS $Method returned a SOAP fault"}
    $result=$xml.SelectSingleNode('//*[local-name()="'+$Method+'Response"]')
    if(-not $result){throw "SSRS $Method response missing"}
    return ,$result
}
function Get-XmlText($Node,[string]$Name) {
    $value=$Node.SelectSingleNode('./*[local-name()="'+$Name+'"]')
    if($value){return $value.InnerText};return ''
}
function Convert-ReportParameters([string]$Name,[string]$Value) {
    '<'+$Name+'>'+[Security.SecurityElement]::Escape($Value)+'</'+$Name+'>'
}
function Remove-ReportRuntime($Node) {
    # Only named response fields; never touch report definitions or embedded SQL.
    $copy=$Node.CloneNode($true)
    foreach($item in @($copy.SelectNodes('./*[local-name()="LastRunTime" or local-name()="NextRunTime" or local-name()="LastExecuted" or local-name()="Status" or local-name()="State"]'))) {
        $item.ParentNode.RemoveChild($item)|Out-Null
    }
    return ,$copy
}
function Invoke-AnalysisDiscover([string]$Rowset,[string]$Restrictions='') {
    $query='<Discover xmlns="urn:schemas-microsoft-com:xml-analysis"><RequestType>'+$Rowset+'</RequestType><Restrictions><RestrictionList>'+$Restrictions+'</RestrictionList></Restrictions><Properties><PropertyList><Content>Data</Content></PropertyList></Properties></Discover>'
    [xml]$xml=Invoke-ASCmd -Server $AnalysisServer -Query $query -ErrorAction Stop
    if($xml.SelectSingleNode('//*[local-name()="Error" or local-name()="Fault"]')){throw "SSAS $Rowset returned an error"}
    return ,$xml
}
try {
    if(-not $OutputDirectory){throw 'OutputDirectory is required'}
    $OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
    [IO.Directory]::CreateDirectory($OutputDirectory)|Out-Null
    if(@(Get-ChildItem -LiteralPath $OutputDirectory -Force).Count){throw 'Output directory must be empty'}
    Save-ServiceManifest
    foreach($file in $ConfigurationFile) {
        $key=Get-ServiceKey ([IO.Path]::GetFullPath($file));$scope='files/'+$key
        Invoke-ServiceSection $scope {
            if(-not (Test-Path -LiteralPath $file -PathType Leaf)){throw "Configuration file unavailable: $file"}
            Write-ServiceJson ($scope+'/source.json') ([ordered]@{path=[IO.Path]::GetFullPath($file)})
            Copy-Item -LiteralPath $file -Destination (Join-Path $OutputDirectory ($scope+'/content.config'))
        }
    }
    if($IncludeLocalServiceInventory) {
        Invoke-ServiceSection 'host/services.json' {
            if(-not $IsWindows){throw 'Windows SQL service inventory requires running this section on Windows; use the system collector for Linux/macOS'}
            $rows=@(Get-CimInstance Win32_Service -ErrorAction Stop | Where-Object {$_.Name -match 'SQL|ReportServer|MSOLAP'} | Sort-Object Name | Select-Object Name,DisplayName,StartMode,StartName,PathName,ServiceType)
            Write-ServiceJson 'host/services.json' $rows
        }
    }
    if($ClusterName) {
        Invoke-ServiceSection 'cluster' {
            Import-Module FailoverClusters -ErrorAction Stop
            Write-ServiceJson 'cluster/identity.json' (Get-Cluster -Name $ClusterName|Select-Object Name,QuorumArbitrationTimeMax,QuorumArbitrationTimeMin,SameSubnetDelay,SameSubnetThreshold,CrossSubnetDelay,CrossSubnetThreshold)
            Write-ServiceJson 'cluster/nodes.json' @(Get-ClusterNode -Cluster $ClusterName|Sort-Object Name|Select-Object Name,NodeWeight,Id)
            Write-ServiceJson 'cluster/quorum.json' (Get-ClusterQuorum -Cluster $ClusterName|Select-Object QuorumType,@{Name='QuorumResource';Expression={[string]$_.QuorumResource}})
            Write-ServiceJson 'cluster/networks.json' @(Get-ClusterNetwork -Cluster $ClusterName|Sort-Object Name|Select-Object Name,Address,AddressMask,Role,Metric,AutoMetric)
            $resources=foreach($resource in Get-ClusterResource -Cluster $ClusterName|Sort-Object Name) {
                [ordered]@{name=$resource.Name;type=[string]$resource.ResourceType;group=[string]$resource.OwnerGroup;parameters=@($resource|Get-ClusterParameter|Sort-Object Name|Select-Object Name,Value,Type);owners=@(($resource|Get-ClusterOwnerNode).OwnerNodes|ForEach-Object Name|Sort-Object)}
            }
            Write-ServiceJson 'cluster/resources.json' @($resources)
        }
    }
    if($ReportServerUri) {
        if($ReportServerUri.Scheme -ne 'https' -and -not $ReportServerUri.IsLoopback){throw 'SSRS requires HTTPS except loopback test endpoints'}
        Invoke-ServiceSection 'ssrs/system' {
            Write-ServiceText 'ssrs/system/properties.xml' (Invoke-ReportRead 'GetSystemProperties' '<Properties xsi:nil="true"/>').OuterXml
            Write-ServiceText 'ssrs/system/policies.xml' (Invoke-ReportRead 'GetSystemPolicies').OuterXml
        }
        Invoke-ServiceSection 'ssrs/schedules' {
            $response=Invoke-ReportRead 'ListSchedules' '<SiteUrl/>'
            foreach($schedule in $response.SelectNodes('.//*[local-name()="Schedule"]')) {
                $id=Get-XmlText $schedule 'ScheduleID'
                if(-not $id){throw 'ScheduleID missing'}
                Write-ServiceText ('ssrs/schedules/'+(Get-ServiceKey $id)+'.xml') (Remove-ReportRuntime $schedule).OuterXml
            }
        }
        $script:ReportItems=@()
        Invoke-ServiceSection 'ssrs/discovery' {
            $listing=Invoke-ReportRead 'ListChildren' '<ItemPath>/</ItemPath><Recursive>true</Recursive>'
            $script:ReportItems=@($listing.SelectNodes('.//*[local-name()="CatalogItem"]')|Sort-Object {Get-XmlText $_ 'Path'})
            $index=foreach($item in $script:ReportItems){[ordered]@{path=Get-XmlText $item 'Path';type=Get-XmlText $item 'TypeName';id=Get-XmlText $item 'ID'}}
            Write-ServiceJson 'ssrs/discovery/items.json' @($index)
        }
        foreach($item in $script:ReportItems) {
            $path=Get-XmlText $item 'Path';$type=Get-XmlText $item 'TypeName';$key=Get-ServiceKey $path;$scope='ssrs/items/'+$key
            Invoke-ServiceSection $scope {
                $parameter=Convert-ReportParameters 'ItemPath' $path
                Write-ServiceJson ($scope+'/identity.json') ([ordered]@{path=$path;type=$type})
                Write-ServiceText ($scope+'/policies.xml') (Invoke-ReportRead 'GetPolicies' $parameter).OuterXml
                if($type -eq 'LinkedReport'){Write-ServiceText ($scope+'/link.xml') (Invoke-ReportRead 'GetItemLink' $parameter).OuterXml}
                elseif($type -ne 'Folder') {
                    $definition=Invoke-ReportRead 'GetItemDefinition' $parameter
                    $encoded=Get-XmlText $definition 'Definition'
                    if(-not $encoded){throw "Empty definition for $path"}
                    [IO.File]::WriteAllBytes((Join-Path $OutputDirectory ($scope+$(if($type -eq 'Resource'){'/definition.bin'}else{'/definition.xml'}))),[Convert]::FromBase64String($encoded))
                }
            }
            if($type -in @('Report','LinkedReport')) {
                Invoke-ServiceSection ('ssrs/subscriptions/'+$key) {
                    $subscriptions=Invoke-ReportRead 'ListSubscriptions' (Convert-ReportParameters 'ItemPathOrSiteURL' $path)
                    foreach($subscription in $subscriptions.SelectNodes('.//*[local-name()="Subscription"]')) {
                        $id=Get-XmlText $subscription 'SubscriptionID'
                        if(-not $id){throw 'SubscriptionID missing'}
                        $method=if((Get-XmlText $subscription 'IsDataDriven') -eq 'true'){'GetDataDrivenSubscriptionProperties'}else{'GetSubscriptionProperties'}
                        $response=Invoke-ReportRead $method (Convert-ReportParameters 'SubscriptionID' $id)
                        Write-ServiceText ('ssrs/subscriptions/'+$key+'/'+(Get-ServiceKey $id)+'.xml') (Remove-ReportRuntime $response).OuterXml
                    }
                }
            }
        }
    }
    if($AnalysisServer) {
        $script:Catalogs=@()
        Invoke-ServiceSection 'ssas/discovery' {
            Import-Module SqlServer -ErrorAction Stop
            $catalogs=Invoke-AnalysisDiscover 'DBSCHEMA_CATALOGS'
            $script:Catalogs=@($catalogs.SelectNodes('//*[local-name()="row"]')|ForEach-Object {Get-XmlText $_ 'CATALOG_NAME'}|Sort-Object -Unique)
            Write-ServiceJson 'ssas/discovery/catalogs.json' $script:Catalogs
        }
        foreach($catalog in $script:Catalogs) {
            $scope='ssas/databases/'+(Get-ServiceKey $catalog)
            Invoke-ServiceSection $scope {
                $restriction=(Convert-ReportParameters 'DatabaseID' $catalog)+'<ObjectExpansion>ExpandFull</ObjectExpansion>'
                $metadata=Invoke-AnalysisDiscover 'DISCOVER_XML_METADATA' $restriction
                $rows=@($metadata.SelectNodes('//*[local-name()="METADATA"]'))
                if(-not $rows.Count){throw "No metadata returned for $catalog"}
                Write-ServiceJson ($scope+'/identity.json') ([ordered]@{name=$catalog})
                Write-ServiceText ($scope+'/metadata.xml') $metadata.OuterXml
            }
        }
    }
    Save-ServiceManifest $true
    if(@($script:Sections|Where-Object status -eq 'failed').Count){exit 6}
    exit 0
} catch { Write-Error $_;exit 1 }
