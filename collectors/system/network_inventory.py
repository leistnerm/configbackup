"""Share configuration and optional process/socket observations, collected locally."""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import sys
from completeness import section
from sections import enabled
from storage_inventory import text,Unavailable,ordered_rows
from telemetry import utcnow


def collect(root,run_ps=None,platform_name=None,runtime=False):
    root=Path(root);platform_name=platform_name or sys.platform;scopes=[];failures=[]
    def probe(scope,fn):
        if not enabled('configuration') or not enabled(scope):
            scopes.append(section(root,scope,'disabled','Disabled by configuration'));return
        try:
            data=fn();path=root/scope/'configuration.json';path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(data,sort_keys=True,indent=2)+'\n');scopes.append(section(root,scope))
        except Unavailable as exc:scopes.append(section(root,scope,'not_applicable',str(exc)))
        except Exception as exc:scopes.append(section(root,scope,'failed',str(exc)));failures.append({'section':scope,'error':str(exc)})
    def files(patterns):
        import glob
        result=[]
        for name in sorted({name for pattern in patterns for name in glob.glob(pattern)}):
            path=Path(name)
            if path.is_file():result.append({'path':name,'content':path.read_text()})
        return result
    if platform_name.startswith('linux'):
        probe('shares/nfs-definitions',lambda:files(['/etc/exports','/etc/exports.d/*.exports']))
        probe('shares/nfs-effective',lambda:text(['exportfs','-v']))
        probe('shares/samba-effective',lambda:text(['testparm','-s','--suppress-prompt']))
        probe('shares/client-mounts',lambda:ordered_rows([x for x in json.loads(text(['findmnt','--json','--list','--output','SOURCE,TARGET,FSTYPE,OPTIONS']))['filesystems'] if x.get('fstype') in ('cifs','smb3','nfs','nfs4')]))
        probe('shares/automount-definitions',lambda:files(['/etc/auto.master','/etc/auto.master.d/*','/etc/auto.*']))
    elif platform_name=='darwin':
        probe('shares/smb-exported',lambda:text(['sharing','-l']))
        probe('shares/nfs-definitions',lambda:files(['/etc/exports']))
        probe('shares/client-mounts',lambda:sorted(line for line in text(['mount']).splitlines() if '(smbfs,' in line or '(nfs,' in line))
        probe('shares/automount-definitions',lambda:files(['/etc/auto_master','/etc/auto_*']))
    elif platform_name=='win32' and run_ps:
        probe('shares/smb-definitions',lambda:run_ps("Get-Command Get-SmbShare -ErrorAction Stop | Out-Null; Get-SmbShare -ErrorAction Stop | Sort-Object ScopeName,Name | ForEach-Object { $s=$_; [pscustomobject]@{Name=$s.Name;Path=$s.Path;ScopeName=$s.ScopeName;Description=$s.Description;EncryptData=$s.EncryptData;ContinuouslyAvailable=$s.ContinuouslyAvailable;CachingMode=[string]$s.CachingMode;FolderEnumerationMode=[string]$s.FolderEnumerationMode;Access=@(Get-SmbShareAccess -Name $s.Name -ScopeName $s.ScopeName -ErrorAction Stop | Sort-Object AccountName,AccessControlType,AccessRight | Select-Object AccountName,AccessControlType,AccessRight)} }"))
        probe('shares/nfs-definitions',lambda:run_ps("Get-Command Get-NfsShare -ErrorAction Stop | Out-Null; Get-NfsShare -ErrorAction Stop | Sort-Object Name | ForEach-Object { $s=$_; [pscustomobject]@{Name=$s.Name;Path=$s.Path;Authentication=$s.Authentication;EnableAnonymousAccess=$s.EnableAnonymousAccess;AnonymousUid=$s.AnonymousUid;AnonymousGid=$s.AnonymousGid;Permissions=@(Get-NfsSharePermission -Name $s.Name -ErrorAction Stop | Sort-Object ClientName | Select-Object ClientName,ClientType,Permission,AllowRootAccess)} }"))
        probe('shares/smb-client-mappings',lambda:run_ps("Get-SmbMapping -ErrorAction Stop | Sort-Object LocalPath,RemotePath | Select-Object LocalPath,RemotePath,Persistent"))
        probe('shares/smb-client-settings',lambda:run_ps("Get-SmbClientConfiguration -ErrorAction Stop | Select-Object EnableSecuritySignature,RequireSecuritySignature,EnableInsecureGuestLogons,RequireEncryption,EnableMultiChannel,ConnectionCountPerRssNetworkInterface,DirectoryCacheLifetime,FileInfoCacheLifetime,FileNotFoundCacheLifetime"))
    if runtime and enabled('telemetry/network'):
        output={'observed_at':utcnow(),'platform':platform_name,'visibility':'current process privileges; process ownership can be incomplete','datasets':{},'failures':[]}
        def observe(name,fn):
            try:output['datasets'][name]=fn()
            except Exception as exc:output['failures'].append({'section':name,'error':str(exc)})
        if platform_name.startswith('linux'):
            observe('listeners',lambda:text(['ss','-H','-lntup']))
            observe('processes',lambda:text(['ps','-eo','pid,ppid,user,comm']))
            def executable_paths():
                rows=[]
                for proc in sorted(Path('/proc').iterdir()):
                    if not proc.name.isdigit():continue
                    try:rows.append({'pid':int(proc.name),'executable':os.readlink(proc/'exe')})
                    except FileNotFoundError:continue  # Kernel thread or process exited during snapshot.
                    except PermissionError:rows.append({'pid':int(proc.name),'executable':None,'visibility':'permission denied'})
                return rows
            observe('executable-paths',executable_paths)
        elif platform_name=='darwin':
            observe('tcp-listeners',lambda:text(['lsof','-nP','-iTCP','-sTCP:LISTEN','-FpcuLftn']))
            observe('udp-endpoints',lambda:text(['lsof','-nP','-iUDP','-FpcuLftn']))
            observe('processes',lambda:text(['ps','-axo','pid=,ppid=,user=,comm=']))
            observe('smb-negotiation',lambda:text(['smbutil','statshares','-a']))
            observe('nfs-client-mount-options',lambda:text(['nfsstat','-m']))
        elif platform_name=='win32' and run_ps:
            observe('tcp-listeners',lambda:run_ps("Get-NetTCPConnection -State Listen -ErrorAction Stop | Sort-Object LocalAddress,LocalPort,OwningProcess | Select-Object LocalAddress,LocalPort,OwningProcess"))
            observe('udp-endpoints',lambda:run_ps("Get-NetUDPEndpoint -ErrorAction Stop | Sort-Object LocalAddress,LocalPort,OwningProcess | Select-Object LocalAddress,LocalPort,OwningProcess"))
            observe('processes',lambda:run_ps("Get-CimInstance Win32_Process -ErrorAction Stop | Sort-Object ProcessId | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate"))
        path=root/'telemetry/network/inventory.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(output,sort_keys=True,indent=2)+'\n')
    return scopes,failures
