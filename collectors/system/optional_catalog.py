"""Stdlib-only optional collector catalog; discovery never enables a feature."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

WINDOWS_COMPONENTS = ('iis','dns','dhcp','hyperv','certificates','dfs','nps','wef','wsus','adcs')
WINDOWS_DETAILS = {'iis-extended':'iis','dns-policies':'dns','dhcp-reservation-options':'dhcp',
                   'hyperv-network':'hyperv','hyperv-replication':'hyperv'}
_MODULES = {'dns':'DnsServer','dhcp':'DhcpServer','hyperv':'Hyper-V','dfs':'DFSN',
            'nps':'NPS','wsus':'UpdateServices'}
_LIMITS = {
 'iis':'Selected effective sections and configured locations; no content/private keys or arbitrary extension dump.',
 'dns':'Configuration and records, not cache, DNSSEC keys or AD ACLs.',
 'dhcp':'Configuration, not leases, credentials or recovery database.',
 'hyperv':'Configuration, not VM/disk contents, guest data or key protectors.',
 'certificates':'LocalMachine and CurrentUser public certificate metadata only; no private-key access or online validation.',
 'dfs':'Local namespace roots/folders/targets and local DFSR CIM configuration; no share contents or domain-wide discovery.',
 'nps':'RADIUS client metadata and recognized English netsh policy summary fields; no secrets or full policy reconstruction.',
 'wef':'Selected subscription XML configuration only; no events, passwords or service changes.',
 'wsus':'Selected server/proxy/synchronization settings and group names; no update content, credentials or approval export.',
 'adcs':'Exact local CA public registry settings and policy/extension paths; no CA database, issued certificates or signing keys.',
}

def catalog():
    """Each row describes one opt-in choice; consumers must not infer permission."""
    rows=[dict(id=name,flag='--windows-component',value=name,platforms=['windows'],
               scope='windows-components/'+name,default_enabled=False,
               requirements=_MODULES.get(name,{'iis':'Microsoft.Web.Administration','wef':'wecutil'}.get(name,'Windows provider')),
               limits=_LIMITS[name]) for name in WINDOWS_COMPONENTS]
    rows.extend(dict(id=name,flag='--windows-detail',value=name,platforms=['windows'],scope='windows-components/'+base,
                     implies=base,default_enabled=False,requirements=_MODULES.get(base,'Microsoft.Web.Administration'),
                     limits='Optional additional queries; unsupported cmdlets/properties fail their scope.') for name,base in WINDOWS_DETAILS.items())
    rows.extend([
      dict(id='public-certificate',flag='--public-certificate',platforms=['windows','linux','macos'],scope='certificates/files',default_enabled=False,requirements='OpenSSL and explicit public certificate file',limits='PEM/DER X.509; no PKCS#12, trust/revocation/network validation, private keys or TLS endpoint probing.'),
      dict(id='web-config',flag='--web-config',platforms=['windows','linux','macos'],scope='web-servers',default_enabled=False,requirements='Explicit nginx=PATH or apache=PATH',limits='Bounded allowlisted directive inventory; no config execution or implicit include traversal.'),
      dict(id='container-engine',flag='--container-engine',values=['docker','podman'],platforms=['windows','linux','macos'],scope='containers',default_enabled=False,requirements='Docker/Podman client and existing accessible engine',limits='Containers, network and volume metadata; no environment/command/label/option values, contents or engine mutation.')])
    return rows

def discover(*,windows=None,which=shutil.which,execute=subprocess.run):
    """Read executable/module availability, not collection access or role state."""
    windows=os.name=='nt' if windows is None else windows
    tools={name:which(name) for name in ('openssl','docker','podman','nginx','apachectl','httpd','powershell.exe','pwsh')}
    modules=[];error=''
    ps=tools['powershell.exe'] or tools['pwsh']
    if windows and ps:
        try:
            command="@('DnsServer','DhcpServer','Hyper-V','DFSN','NPS','UpdateServices') | ForEach-Object { if (Get-Module -ListAvailable -Name $_) { $_ } } | ConvertTo-Json -Compress"
            result=execute([ps,'-NoLogo','-NoProfile','-NonInteractive','-Command',command],capture_output=True,timeout=30,shell=False)
            if result.returncode:raise ValueError('Module discovery failed')
            raw=result.stdout.decode('utf-8-sig') if isinstance(result.stdout,bytes) else result.stdout
            value=json.loads(raw) if raw.strip() else []
            modules=[value] if isinstance(value,str) else value
            if not isinstance(modules,list) or any(x not in _MODULES.values() for x in modules):raise ValueError('Unexpected module discovery response')
        except Exception as exc:modules=[];error=type(exc).__name__
    return {'platform':'windows' if windows else 'macos' if sys.platform=='darwin' else 'linux',
            'tools':tools,'windows_modules':sorted(set(modules)), 'module_probe_error':error,
            'scope_note':'Presence only. Permissions, service availability, selected files and collection completeness remain untested.'}
