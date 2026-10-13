"""Selected typed registry values and effective computer/user Group Policy results."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import yaml
from completeness import section
from sections import enabled


def defaults():
    result=[]
    def add(identity,path,values=None,recursive=False,views=('64',)):
        for view in views:
            item={'id':identity+'-'+view,'path':path,'view':view,'recursive':recursive,'enabled':True}
            if values is not None:item['values']=values
            result.append(item)
    for hive,label in [('HKLM','machine'),('HKCU','current-user')]:
        for name in ('Run','RunOnce'):
            add(label+'-'+name.lower(),hive+'\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\'+name,views=('64','32'))
        add(label+'-explorer-policy',hive+'\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\Explorer',recursive=True)
        add(label+'-system-policy',hive+'\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System',recursive=True)
    add('memory',r'HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management',
        ['PagingFiles','ClearPageFileAtShutdown','DisablePagingExecutive','LargeSystemCache','SystemPages','SecondLevelDataCache'])
    add('crash-dump',r'HKLM\SYSTEM\CurrentControlSet\Control\CrashControl',
        ['CrashDumpEnabled','DumpFile','MinidumpDir','LogEvent','AutoReboot','Overwrite','AlwaysKeepMemoryDump','DedicatedDumpFile'])
    add('rdp',r'HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server',['fDenyTSConnections','fSingleSessionPerUser'])
    add('rdp-tcp',r'HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp',
        ['PortNumber','SecurityLayer','UserAuthentication','MinEncryptionLevel','SSLCertificateSHA1Hash'])
    add('windows-update',r'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate',recursive=True)
    add('rdp-policy',r'HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services',recursive=True)
    for group in ('Protocols','Ciphers','Hashes','KeyExchangeAlgorithms'):
        add('tls-'+group.lower(),r'HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\SCHANNEL'+'\\'+group,recursive=True)
    add('services',r'HKLM\SYSTEM\CurrentControlSet\Services',
        ['Start','Type','ErrorControl','Group','DependOnService','DependOnGroup','DelayedAutoStart','FailureActions','FailureActionsOnNonCrashFailures'],True)
    return result


def selections(path=None):
    config=path if isinstance(path,dict) else yaml.safe_load(Path(path).read_text()) if path else {}
    if config is None:config={}
    result=defaults() if config.get('include_defaults',True) else []
    result+=config.get('registry',[])
    found=set()
    for item in result:
        if not isinstance(item,dict) or not re.fullmatch(r'[A-Za-z0-9_-]+',str(item.get('id',''))):raise ValueError('Registry selection requires a safe unique id')
        if item['id'] in found:raise ValueError('Duplicate registry selection id')
        found.add(item['id'])
        key=str(item.get('path','')).replace(':','').rstrip('\\');item['path']=key
        if not re.fullmatch(r'HK(?:LM|CU|U)\\[^*?\x00]+',key) or len(key.split('\\'))<3:raise ValueError('Select a specific registry key, not a hive')
        if any(part.casefold() in ('sam','security','secrets','credentials','protect','vault') for part in key.split('\\')[1:]):raise ValueError('Credential-bearing registry stores are excluded')
        if key.casefold().startswith('hklm\\system') and '\\lsa' in key.casefold():raise ValueError('LSA stores are excluded; use effective policy reports')
        if str(item.get('view','64')) not in ('32','64'):raise ValueError('Registry view must be 32 or 64')
        item['view']=str(item.get('view','64'))
        for flag in ('enabled','recursive'):
            if not isinstance(item.get(flag,flag=='enabled'),bool):raise ValueError(flag+' must be boolean')
        if 'values' in item and (not isinstance(item['values'],list) or any(not isinstance(x,str) for x in item['values'])):raise ValueError('values must be a list of exact value names')
    return result


def normalize_rsop(raw):
    # Preserve extension data and policy/list order. Only the report-generation
    # timestamp at the document root is removed; policy modification dates remain.
    root=ET.fromstring(raw)
    if root.tag.rsplit('}',1)[-1].lower()!='rsop':raise ValueError('Expected an RSoP XML document')
    for child in list(root):
        if child.tag==root.tag.removesuffix('Rsop')+'CreationTime' or child.tag=='CreationTime':root.remove(child)
    return ET.canonicalize(ET.tostring(root,encoding='unicode'),strip_text=False)+'\n'


def collect_registry(root, powershell, config_path=None):
    root=Path(root);scopes=[];failures=[];wanted=[]
    for spec in selections(config_path):
        scope='registry/'+spec['id']
        if not spec.get('enabled',True) or not enabled('configuration') or not enabled(scope):scopes.append(section(root,scope,'disabled','Disabled by configuration'))
        else:wanted.append(spec)
    if not wanted:return scopes,failures
    try:
        with tempfile.TemporaryDirectory(prefix='cb-registry-') as td:
            path=Path(td)/'selections.json';path.write_text(json.dumps(wanted))
            cp=subprocess.run([powershell,'-NoLogo','-NoProfile','-NonInteractive','-File',str(Path(__file__).with_name('Read-RegistrySelections.ps1')),'-SelectionsPath',str(path)],capture_output=True,text=True,timeout=180)
        if cp.returncode:raise RuntimeError('Registry reader failed (exit '+str(cp.returncode)+')')
        payload=json.loads(cp.stdout);results={r['id']:r for r in payload}
    except Exception as exc:results={spec['id']:{'status':'failed','error':str(exc)} for spec in wanted}
    for spec in wanted:
        scope='registry/'+spec['id'];record=results.get(spec['id'],{'status':'failed','error':'Reader omitted selection'})
        if record.get('status')=='complete':
            path=root/scope/'values.json';path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'selection':spec,'exists':record['exists'],'records':record['records']},sort_keys=True,indent=2)+'\n')
            scopes.append(section(root,scope))
        else:
            error=record.get('error','Registry read failed');scopes.append(section(root,scope,'failed',error));failures.append({'section':scope,'error':error})
    return scopes,failures


def collect_rsop(root, users=()):
    root=Path(root);scopes=[];failures=[]
    targets=[('computer',None),('user',None)]+[('user',user) for user in users]
    for kind,user in targets:
        identity=kind+('-'+hashlib.sha256(user.encode()).hexdigest()[:16] if user else '')
        scope='policy/rsop/'+identity
        if not enabled('configuration') or not enabled(scope):scopes.append(section(root,scope,'disabled','Disabled by configuration'));continue
        try:
            with tempfile.TemporaryDirectory(prefix='cb-rsop-') as td:
                path=Path(td)/'report.xml';args=['gpresult','/scope',kind,'/x',str(path),'/f']
                if user:args+=['/user',user]
                cp=subprocess.run(args,capture_output=True,timeout=120)
                if cp.returncode:raise RuntimeError('gpresult exit '+str(cp.returncode)+'; elevation or RSoP data may be unavailable')
                raw=path.read_bytes();normalized=normalize_rsop(raw)
            target=root/scope;target.mkdir(parents=True,exist_ok=True)
            (target/'report.xml').write_text(normalized)
            (target/'context.json').write_text(json.dumps({'scope':kind,'requested_user':user or 'current process account'},sort_keys=True)+'\n')
            original=root/'telemetry/rsop'/identity/'report.xml';original.parent.mkdir(parents=True,exist_ok=True);original.write_bytes(raw)
            scopes.append(section(root,scope))
        except Exception as exc:
            scopes.append(section(root,scope,'failed',str(exc)));failures.append({'section':scope,'error':str(exc)})
    return scopes,failures
