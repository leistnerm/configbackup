"""Read-only Windows PowerShell readiness; no policy changes or secret retrieval."""
import json
import os
import subprocess
import sys
from configbackup_lib.configuration.powershell_policy import environment


def inspect(config, *, platform=None, execute=None):
    result={'python':sys.executable,'configured_policy':(config.get('options') or {}).get('powershell_execution_policy','inherit'),
            'scope':'Current process/account only; run through the scheduler to verify that account.','shells':[]}
    if (os.name if platform is None else platform)!='nt':
        result['status']='not_applicable';return result
    shells=['powershell.exe','pwsh']
    shells+= [p.get('pwsh','pwsh') for p in (config.get('connections') or {}).values() if p.get('engine')=='sqlserver']
    script="""$ErrorActionPreference='Stop'; [ordered]@{version=$PSVersionTable.PSVersion.ToString();edition=$PSVersionTable.PSEdition;account=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name;effectivePolicy=[string](Get-ExecutionPolicy);policies=@(Get-ExecutionPolicy -List | ForEach-Object { @{scope=[string]$_.Scope;policy=[string]$_.ExecutionPolicy} });modules=@(Get-Module -ListAvailable -Name dbatools,SqlServer,Microsoft.PowerShell.SecretManagement | Select-Object Name,@{n='Version';e={$_.Version.ToString()}})} | ConvertTo-Json -Depth 5 -Compress"""
    for shell in dict.fromkeys(shells):
        try:
            cp=(execute or subprocess.run)([shell,'-NoLogo','-NoProfile','-NonInteractive','-Command',script],env=environment(config,platform='nt'),capture_output=True,text=True,timeout=30)
            if cp.returncode:raise ValueError('Probe failed')
            result['shells'].append({'executable':shell,'status':'available','details':json.loads(cp.stdout)})
        except (OSError,ValueError,subprocess.TimeoutExpired) as exc:
            result['shells'].append({'executable':shell,'status':'unavailable','error_type':type(exc).__name__})
    result['status']='complete' if all(r['status']=='available' for r in result['shells']) else 'partial'
    return result
