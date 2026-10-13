"""Opt-in public certificates, bounded web config, and container metadata.

No config execution, implicit include traversal, private keys, container mutation
or collection of environment/command/label/driver-option values is performed.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from configbackup_lib.engine.completeness import section
from configbackup_lib.engine.sections import enabled
from configbackup_lib.reporting.telemetry import envelope, dataset, write_envelope

MASK='[VALUE NOT COLLECTED]'
MAX_FILE=16*1024*1024

def key(value):return hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:24]

def command(args, *, data=None, execute=subprocess.run):
    result=execute(args,input=data,capture_output=True,timeout=90,shell=False)
    if result.returncode:raise RuntimeError('Read command failed (exit %s); stderr omitted' % result.returncode)
    return result.stdout.decode('utf-8-sig') if isinstance(result.stdout,bytes) else result.stdout

def public_certificates(path, *, execute=subprocess.run, openssl=None):
    """Read public X.509 metadata via OpenSSL; reject bundles containing keys."""
    path=Path(path)
    if path.stat().st_size>MAX_FILE:raise ValueError('Certificate input exceeds 16 MiB')
    raw=path.read_bytes()
    if re.search(br'-----BEGIN [^-]*PRIVATE KEY-----',raw):raise ValueError('Private-key material is not accepted')
    pem=re.findall(br'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----',raw)
    if b'-----BEGIN' in raw and not pem:raise ValueError('Expected public X.509 PEM or DER certificate')
    executable=openssl or shutil.which('openssl')
    if not executable:raise RuntimeError('OpenSSL is unavailable')
    rows=[]
    for cert in pem or [raw]:
        args=[executable,'x509','-inform','PEM' if pem else 'DER','-noout','-subject','-issuer','-startdate','-enddate','-serial','-fingerprint','-sha256','-nameopt','RFC2253']
        text=command(args,data=cert,execute=execute)
        parsed={line.split('=',1)[0].strip().lower():line.split('=',1)[1].strip() for line in text.splitlines() if '=' in line}
        fingerprint=next((v for k,v in parsed.items() if k.replace(' ','') in ('sha256fingerprint','sha256fingerprint')),None)
        if not fingerprint or any(name not in parsed for name in ('subject','issuer','notbefore','notafter','serial')):raise ValueError('Unrecognized OpenSSL certificate metadata')
        def date(name):return dt.datetime.strptime(parsed[name],'%b %d %H:%M:%S %Y %Z').replace(tzinfo=dt.timezone.utc).isoformat()
        # -text only decodes a PUBLIC certificate already isolated above. Persist
        # only SAN entries and signature algorithm, never arbitrary decoded text.
        decoded=command([executable,'x509','-inform','PEM' if pem else 'DER','-noout','-text'],data=cert,execute=execute)
        san=[];capture=False
        for line in decoded.splitlines():
            if 'X509v3 Subject Alternative Name:' in line:capture=True;continue
            if capture:
                stripped=line.strip()
                if not re.match(r'(DNS:|IP Address:|email:|URI:)',stripped):break
                san.extend(x.strip() for x in stripped.split(','));capture=False
        signature=re.search(r'^\s*Signature Algorithm:\s*(\S+)',decoded,re.M)
        rows.append({'sha256':fingerprint.replace(':','').lower(),'subject':parsed['subject'],'issuer':parsed['issuer'],
                     'serial':parsed['serial'],'not_before':date('notbefore'),'not_after':date('notafter'),
                     'subject_alternative_names':sorted(san),'signature_algorithm':signature.group(1) if signature else None})
    return sorted(rows,key=lambda row:row['sha256'])

NGINX_SAFE={'listen','server_name','root','index','ssl_certificate','ssl_certificate_key','ssl_protocols','ssl_ciphers',
 'ssl_prefer_server_ciphers','ssl_session_timeout','ssl_session_cache','ssl_stapling','ssl_stapling_verify','ssl_trusted_certificate',
 'include','client_max_body_size','keepalive_timeout','worker_processes','worker_connections','error_log','access_log','gzip','gzip_types',
 'resolver','allow','deny','satisfy','try_files','autoindex','sendfile','tcp_nopush','tcp_nodelay','proxy_read_timeout','proxy_connect_timeout'}
APACHE_SAFE={'listen','servername','serveralias','documentroot','directoryindex','sslcertificatefile','sslcertificatekeyfile',
 'sslcertificatechainfile','sslprotocol','sslciphersuite','sslhonorcipherorder','sslsessioncachetimeout','include','includeoptional',
 'loadmodule','options','allowoverride','require','order','allow','deny','timeout','keepalive','maxkeepaliverequests','keepalivetimeout',
 'errorlog','customlog','loglevel','serversignature','servertokens','limitrequestbody','proxypreservehost','proxytimeout'}
SAFE_BLOCKS={'http','events','server','location','upstream','limit_except','virtualhost','directory','directorymatch','locationmatch','files','filesmatch','ifmodule','ifdefine','limit','limitexcept'}

def directive(name,args,kind,block=False):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*',name):raise ValueError('Unsupported web directive syntax')
    low=name.lower();allowed=(name in NGINX_SAFE if kind=='nginx' else low in APACHE_SAFE)
    if block:allowed=low in SAFE_BLOCKS
    # Even generally safe directives can be configured with inline credentials.
    suspicious=any(re.search(r'(?i)(?:password|token|secret|authorization|api[_-]?key)\s*[=:]|://[^/\s]+@|[?&][^\s=]+=',x) for x in args)
    return {'directive':name,'arguments':list(args) if allowed and not suspicious else [MASK] if args else [],
            'values_collected':bool(allowed and not suspicious)}

def web_configuration(kind,text):
    if kind not in ('nginx','apache'):raise ValueError('Supported web config kinds are nginx and apache')
    result=[];stack=[result]
    if kind=='nginx':
        # Expansion braces and embedded language blocks are intentionally refused
        # rather than pretending this bounded parser is nginx's configuration engine.
        if '${' in text:raise ValueError('Braced nginx variables need a reviewed parser extension')
        lexer=shlex.shlex(text,posix=True,punctuation_chars='{};');lexer.whitespace_split=True;lexer.commenters='#'
        words=[];tokens=[]
        for token in lexer:
            tokens.extend(list(token) if re.fullmatch(r'[{};]+',token) else [token])
        for token in tokens:
            if token in (';','{'):
                if not words:raise ValueError('Empty nginx directive')
                row=directive(words[0],words[1:],kind,token=='{');stack[-1].append(row);words=[]
                if token=='{':row['children']=[];stack.append(row['children'])
            elif token=='}':
                if words or len(stack)==1:raise ValueError('Unbalanced nginx configuration')
                stack.pop()
            else:words.append(token)
        if words or len(stack)!=1:raise ValueError('Unterminated nginx configuration')
    else:
        text=re.sub(r'\\\r?\n',' ',text)
        block_names=[]
        for line in text.splitlines():
            line=line.strip()
            if not line or line.startswith('#'):continue
            if line.startswith('</') and line.endswith('>'):
                name=line[2:-1].strip().lower()
                if not block_names or name!=block_names.pop():raise ValueError('Unbalanced Apache section')
                stack.pop();continue
            block=line.startswith('<') and line.endswith('>')
            words=shlex.split(line[1:-1] if block else line,comments=True,posix=True)
            if not words:continue
            row=directive(words[0],words[1:],kind,block);stack[-1].append(row)
            if block:row['children']=[];stack.append(row['children']);block_names.append(words[0].lower())
        if len(stack)!=1:raise ValueError('Unterminated Apache section')
    return {'kind':kind,'directives':result,'includes_followed':False,'bounded_inventory':True}

def container_configuration(value, kind='container'):
    """Allowlist projection; unknown additions from newer engines cannot leak."""
    if not isinstance(value,dict):raise ValueError('Container inspect record must be an object')
    required=('Name','Driver') if kind=='volume' else ('Id','Name','Driver') if kind=='network' else ('Id','Name')
    if any(not isinstance(value.get(k),str) or not value[k] for k in required):raise ValueError('Inspect response omitted required identity fields')
    if kind=='network':
        return {k:value.get(k) for k in ('Name','Id','Driver','Scope','Internal','Attachable','Ingress','EnableIPv6')} | {
          'IPAM':{'Driver':(value.get('IPAM') or {}).get('Driver'),'Config':[{k:r.get(k) for k in ('Subnet','IPRange','Gateway')} for r in (value.get('IPAM') or {}).get('Config') or []]},
          'option_names':sorted((value.get('Options') or {}).keys()),'label_names':sorted((value.get('Labels') or {}).keys())}
    if kind=='volume':return {k:value.get(k) for k in ('Name','Driver','Mountpoint','Scope')} | {'option_names':sorted((value.get('Options') or {}).keys()),'label_names':sorted((value.get('Labels') or {}).keys())}
    if not isinstance(value.get('Config'),dict) or not isinstance(value.get('HostConfig'),dict):raise ValueError('Inspect response omitted configuration objects')
    config=value.get('Config') or {};host=value.get('HostConfig') or {};network=value.get('NetworkSettings') or {}
    if not isinstance(config,dict) or not isinstance(host,dict):raise ValueError('Invalid container configuration')
    row={k:value.get(k) for k in ('Id','Name','Image','Platform')}
    row['configuration']={k:config.get(k) for k in ('Image','Hostname','Domainname','User','WorkingDir','StopSignal','ExposedPorts','Volumes')}
    row['configuration']['environment_names']=sorted(set(x.split('=',1)[0] for x in config.get('Env') or []))
    row['configuration']['label_names']=sorted((config.get('Labels') or {}).keys())
    row['host']={k:host.get(k) for k in ('NetworkMode','IpcMode','PidMode','UTSMode','UsernsMode','Privileged','ReadonlyRootfs','PublishAllPorts','PortBindings','RestartPolicy','AutoRemove','CapAdd','CapDrop','Memory','MemorySwap','NanoCpus','CpuShares','CpusetCpus','PidsLimit','ShmSize','Ulimits')}
    row['mounts']=[{k:m.get(k) for k in ('Type','Source','Destination','Driver','Mode','RW','Propagation','Name')} for m in value.get('Mounts') or []]
    row['network_names']=sorted((network.get('Networks') or {}).keys())
    # IP/MAC leases, state, created/started times, counters and commands are runtime
    # or secret-bearing. They are deliberately absent from this configuration.
    row['mounts'].sort(key=lambda x:json.dumps(x,sort_keys=True))
    return row

def collect(root, *, certificates=(),web_configs=(),engines=(),execute=subprocess.run,which=shutil.which):
    root=Path(root);scopes=[];failures=[];cert_rows=[]
    def probe(scope,query):
        if not enabled('configuration') or not enabled(scope):scopes.append(section(root,scope,'disabled','Disabled by configuration'));return None
        try:
            data=query();path=root/scope/'configuration.json';path.parent.mkdir(parents=True,exist_ok=True)
            temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(data,sort_keys=True,indent=2,ensure_ascii=False,allow_nan=False)+'\n',encoding='utf-8');temporary.replace(path)
            scopes.append(section(root,scope));return data
        except Exception as exc:
            reason=type(exc).__name__+': read/parse failed; check selected source, tool and access'
            scopes.append(section(root,scope,'failed',reason));failures.append({'section':scope,'error':reason});return None
    for name in dict.fromkeys(str(Path(p).expanduser().absolute()) for p in certificates):
        scope='certificates/files/'+key(name)
        result=probe(scope,lambda name=name:{'source':name,'certificates':public_certificates(name,execute=execute,openssl=which('openssl'))})
        if result:
            now=dt.datetime.now(dt.timezone.utc)
            cert_rows.extend({'source':name,'sha256':c['sha256'],'days_remaining':(dt.datetime.fromisoformat(c['not_after'])-now).total_seconds()/86400,
                              'not_yet_valid_count':int(dt.datetime.fromisoformat(c['not_before'])>now)} for c in result['certificates'])
    if certificates and enabled('telemetry/certificates'):
        payload=envelope('certificates');dataset(payload,'certificate',cert_rows,['source','sha256'],{'days_remaining':'days','not_yet_valid_count':'count'});payload['failures']=[x for x in failures if x['section'].startswith('certificates/')]
        try:write_envelope(root/'telemetry'/'certificate-health.json',payload)
        except OSError as exc:failures.append({'section':'telemetry/certificates','error':type(exc).__name__})
    for spec in dict.fromkeys(web_configs):
        kind,separator,path=spec.partition('=')
        scope='web-servers/'+(kind if kind in ('nginx','apache') else 'invalid')+'/'+key(path or spec)
        def read_web():
            if not separator or kind not in ('nginx','apache'):raise ValueError('Expected nginx=PATH or apache=PATH')
            source=Path(path).expanduser().absolute()
            if source.stat().st_size>MAX_FILE:raise ValueError('Web config input exceeds 16 MiB')
            return {'source':str(source),**web_configuration(kind,source.read_text(encoding='utf-8-sig'))}
        probe(scope,read_web)
    for engine in dict.fromkeys(engines):
        if engine not in ('docker','podman'):raise ValueError('Unknown container engine')
        def invoke(args):
            executable=which(engine)
            if not executable:raise RuntimeError('Container client is unavailable')
            return command([executable,*args],execute=execute)
        for kind,list_args,inspect_args in [('container',['container','ls','--all','--quiet','--no-trunc'],['container','inspect']),
                                          ('network',['network','ls','--quiet','--no-trunc'],['network','inspect']),
                                          ('volume',['volume','ls','--quiet'],['volume','inspect'])]:
            base='containers/'+engine+'/'+kind
            def discover():
                ids=sorted(set(x.strip() for x in invoke(list_args).splitlines() if x.strip()))
                if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*',x) for x in ids):raise ValueError('Unexpected engine object identifier')
                return ids
            ids=probe(base+'/discovery',discover)
            for identity in ids or []:
                def inspect(identity=identity):
                    result=json.loads(invoke([*inspect_args,identity]))
                    if not isinstance(result,list) or len(result)!=1:raise ValueError('Expected one engine inspect object')
                    return container_configuration(result[0],kind)
                probe(base+'/objects/'+key(identity),inspect)
    return scopes,failures
