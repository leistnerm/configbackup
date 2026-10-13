"""Conservative local endpoint relationships, never a network reachability scan."""
from __future__ import annotations
import base64
import datetime as dt
import ipaddress
import re


def fingerprint(value):
    if isinstance(value,list):
        try:return bytes(value).hex()
        except (ValueError,TypeError):return None
    value=str(value or '')
    normalized=value.replace(':','').replace('-','').lower()
    if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',normalized):return normalized
    try:
        data=base64.b64decode(value,validate=True)
        return data.hex() if len(data) in (20,32) else None
    except ValueError:return None


def executable(value):
    value=str(value or '')
    match=re.match(r'^\s*"([^"]+)"',value) or re.match(r'^\s*(\S+?\.exe)(?:\s|$)',value,re.I)
    return match.group(1).replace('\\','/').casefold() if match else None


def evidence(name,value):
    """Only known collector shapes contribute relationships; no arbitrary values."""
    rows=[];values=value if isinstance(value,list) else [value]
    if name.startswith(('certificates/','windows-components/certificates/stores/')):
        for item in values:
            if not isinstance(item,dict):continue
            certificates=item.get('certificates',[item])
            for cert in certificates:
                if not isinstance(cert,dict):continue
                key=fingerprint(cert.get('thumbprint') or cert.get('sha256'))
                if key:rows.append({'kind':'certificate','fingerprint':key,'not_after':cert.get('not_after'),'certificate_path':item.get('source'),'evidence_path':name})
    if name.startswith('services/'):
        for item in values:
            if isinstance(item,dict) and item.get('Name'):rows.append({'kind':'service','entity':item['Name'],'executable':executable(item.get('PathName')),'evidence_path':name})
    if name.startswith('windows-components/') and ('tls-bindings/' in name or '/bindings/iis/' in name):
        for item in values:
            if not isinstance(item,dict):continue
            match=re.match(r'^(.*):(\d+):(.*)$',str(item.get('binding','')))
            if match:rows.append({'kind':'configured_endpoint','entity':item.get('site') or name,'address':match.group(1),'port':int(match.group(2)),'protocol':'tcp','application_protocol':item.get('protocol','https'),'fingerprint':fingerprint(item.get('certificate_hash')),'evidence_path':name})
    if name.startswith('containers/') and '/container/objects/' in name and isinstance(value,dict):
        for target,bindings in (value.get('host',{}).get('PortBindings') or {}).items():
            for binding in bindings or []:
                try:port=int(binding.get('HostPort',''))
                except ValueError:continue
                rows.append({'kind':'configured_endpoint','entity':value.get('Name') or name,'address':binding.get('HostIp') or '*','port':port,'protocol':target.rsplit('/',1)[-1] if '/' in target else 'tcp','container_port':target,'evidence_path':name})
    if name.startswith('web-servers/') and isinstance(value,dict):
        def walk(items):
            for item in items:
                args=item.get('arguments') or [];directive=str(item.get('directive','')).lower()
                if directive=='listen' and args and item.get('values_collected'):
                    match=re.match(r'^(?:(.*):)?(\d+)$',str(args[0]))
                    if match:rows.append({'kind':'configured_endpoint','entity':value.get('source') or name,'address':match.group(1) or '*','port':int(match.group(2)),'protocol':'tcp','evidence_path':name})
                walk(item.get('children',[]))
        walk(value.get('directives',[]))
    return rows


def runtime(payload):
    """Windows structured rows, Linux ss and macOS lsof field output."""
    result=[];datasets=payload.get('datasets',{});platform=payload.get('platform');processes={}
    for process in datasets.get('processes',[]) if isinstance(datasets.get('processes'),list) else []:
        if isinstance(process,dict):processes[str(process.get('ProcessId'))]={'process':process.get('Name'),'executable':str(process.get('ExecutablePath') or '').replace('\\','/').casefold() or None}
    if platform=='win32':
        for key,protocol in [('tcp-listeners','tcp'),('udp-endpoints','udp')]:
            values=datasets.get(key,[]);values=[values] if isinstance(values,dict) else values
            if not isinstance(values,list):continue
            for value in values:
                try:port=int(value['LocalPort'])
                except (ValueError,TypeError,KeyError):continue
                pid=str(value.get('OwningProcess',''))
                result.append({'kind':'observed_endpoint','address':value.get('LocalAddress'),'port':port,'protocol':protocol,'pid':pid,**processes.get(pid,{})})
    elif str(platform).startswith('linux'):
        for line in str(datasets.get('listeners','')).splitlines():
            parts=line.split()
            if len(parts)<5 or parts[0] not in ('tcp','udp'):continue
            match=re.match(r'^(.*):(\d+)$',parts[4]);owner=re.search(r'\("([^"]+)",pid=(\d+)',line)
            if match:result.append({'kind':'observed_endpoint','address':match.group(1),'port':int(match.group(2)),'protocol':parts[0],'pid':owner.group(2) if owner else None,'process':owner.group(1) if owner else None})
    elif platform=='darwin':
        for key,protocol in [('tcp-listeners','tcp'),('udp-endpoints','udp')]:
            process={}
            for line in str(datasets.get(key,'')).splitlines():
                if line.startswith('p'):process={'pid':line[1:]}
                elif line.startswith('c'):process['process']=line[1:]
                elif line.startswith('n'):
                    match=re.match(r'^(.*):(\d+)$',line[1:].split('->',1)[0])
                    if match:result.append({'kind':'observed_endpoint','address':match.group(1),'port':int(match.group(2)),'protocol':protocol,**process})
    for row in result:row['observed_at']=payload.get('observed_at')
    return result


def address_scope(value):
    value=str(value or '').strip('[]')
    if value in ('*','0.0.0.0','::'):return 'all interfaces'
    try:return 'loopback' if ipaddress.ip_address(value.split('%')[0]).is_loopback else 'specific interface'
    except ValueError:return 'address unknown'


def correlate(source,configured,observed,now):
    rows=[];certs={r['fingerprint']:r for r in configured if r['kind']=='certificate'}
    services=[r for r in configured if r['kind']=='service']
    endpoints=[r for r in configured if r['kind']=='configured_endpoint'];used=set()
    for item in endpoints:
        matches=[(i,r) for i,r in enumerate(observed) if r['port']==item['port'] and r['protocol']==item['protocol'] and (r.get('address')==item.get('address') or address_scope(r.get('address'))=='all interfaces' or address_scope(item.get('address'))=='all interfaces')]
        selected=matches or [(None,{})]
        for index,observation in selected:
            if index is not None:used.add(index)
            row={**item,**observation,'source':source,'binding_scope':address_scope(item.get('address')),'listener_evidence':'observed' if observation else 'not observed / runtime unavailable','reachability':'unknown: firewall, routing, proxy and external access not evaluated','correlation':'same host/source, transport, port and compatible address; not proof of ownership'}
            if observation.get('executable'):
                row['service_candidates']=[s['entity'] for s in services if s.get('executable')==observation['executable']]
            cert=certs.get(item.get('fingerprint'))
            if item.get('application_protocol')=='https' or item.get('fingerprint'):
                row['certificate_status']='metadata unavailable'
                if cert:
                    row['certificate_not_after']=cert.get('not_after')
                    try:
                        expiry=dt.datetime.fromisoformat(str(cert['not_after']).replace('Z','+00:00')).timestamp()
                        row['certificate_days_remaining']=round((expiry-now)/86400,2);row['certificate_status']='expired' if expiry<now else 'in date; trust/name/served certificate unverified'
                    except (ValueError,TypeError):row['certificate_status']='expiry unavailable'
            rows.append(row)
    for index,item in enumerate(observed):
        if index not in used:rows.append({**item,'source':source,'entity':'unmatched listener','binding_scope':address_scope(item.get('address')),'listener_evidence':'observed','reachability':'unknown: firewall/routing not evaluated','correlation':'no matching selected configuration binding'})
    if not rows:rows.append({'source':source,'status':'unavailable_or_no_endpoints','reason':'No supported selected binding/listener evidence; not proof of no exposure'})
    return rows
