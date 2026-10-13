#!/usr/bin/env python3
"""Local presence discovery and opt-in dependency setup. Standard library only."""
from __future__ import annotations
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

DEPENDENCIES=('python-packages','git','postgresql-client','powershell','dotnet-sdk','sqlpackage','dbatools')
SOURCES={
 'postgresql-client':'https://www.postgresql.org/download/',
 'powershell':'https://learn.microsoft.com/powershell/scripting/install/install-powershell',
 'dotnet-sdk':'https://learn.microsoft.com/dotnet/core/install/',
 'sqlpackage':'https://learn.microsoft.com/sql/tools/sqlpackage/sqlpackage-download',
 'dbatools':'https://dbatools.io/install/',
 'git':'https://git-scm.com/downloads',
 'python-packages':'requirements.txt in this release',
}


def platform_name():return 'windows' if os.name=='nt' else 'macos' if sys.platform=='darwin' else 'linux'


def search_paths(system):
    paths=[Path.home()/'.dotnet/tools']
    if system=='macos':
        for prefix in ('/opt/homebrew','/usr/local'):
            paths += [Path(prefix)/'bin',Path(prefix)/'opt/libpq/bin']
            paths += sorted((Path(prefix)/'opt').glob('postgresql@*/bin'),reverse=True)
        paths += [Path('/usr/local/microsoft/powershell/7'),Path('/usr/local/microsoft/powershell/7-preview')]
    elif system=='windows':
        base=Path(os.environ.get('ProgramFiles','C:/Program Files'))
        paths += [base/'PowerShell/7',base/'Git/cmd',base/'dotnet']
        paths += sorted((base/'PostgreSQL').glob('*/bin'),reverse=True)
    else:paths += sorted(Path('/usr/lib/postgresql').glob('*/bin'),reverse=True)
    return paths


def find_tool(name,system,which,roots):
    found=which(name)
    if found:return str(Path(found).absolute())
    for base in roots:
        for suffix in ('.exe','') if system=='windows' else ('',):
            p=base/(name+suffix)
            if p.is_file() and (system=='windows' or os.access(p,os.X_OK)):return str(p.absolute())
    return None


def probe(arguments,execute,timeout=15):
    try:
        result=execute(arguments,capture_output=True,text=True,timeout=timeout,shell=False)
        if result.returncode:return None,'command failed (exit '+str(result.returncode)+')'
        text=result.stdout.decode('utf-8-sig',errors='replace') if isinstance(result.stdout,bytes) else result.stdout
        if len(text)>1024*1024:return None,'discovery output exceeded 1 MiB'
        return text,''
    except (OSError,subprocess.SubprocessError) as exc:return None,type(exc).__name__


def discover(*,system=None,which=None,execute=None,roots=None):
    system=system or platform_name();which=which or shutil.which;execute=execute or subprocess.run
    roots=search_paths(system) if roots is None else [Path(p) for p in roots]
    names=('git','psql','pg_dump','pg_dumpall','pwsh','powershell.exe','sqlpackage','dotnet','docker','podman','openssl','brew','winget','apt-get','sudo')
    paths={name:find_tool(name,system,which,roots) for name in names}
    tools={name:{'path':path,'status':'present' if path else 'missing'} for name,path in paths.items()}
    for name in ('git','psql','pg_dump','pg_dumpall','pwsh','sqlpackage','dotnet'):
        if not paths[name]:continue
        value,error=probe([paths[name],'/Version' if name=='sqlpackage' else '--list-sdks' if name=='dotnet' else '--version'],execute)
        if error:tools[name].update(status='unverified',error=error)
        else:tools[name]['version']=value.strip()[:500]
    packages={}
    for name in ('PyYAML','ruamel.yaml','tzdata'):
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    modules=[];services=[];errors=[]
    ps=paths['pwsh'] or paths['powershell.exe']
    if ps:
        command="@(Get-Module -ListAvailable -Name dbatools,SqlServer | Select-Object Name,Version -Unique | ForEach-Object { @{name=$_.Name;version=$_.Version.ToString()} }) | ConvertTo-Json -Compress"
        value,error=probe([ps,'-NoLogo','-NoProfile','-NonInteractive','-Command',command],execute,30)
        try:
            if error:raise ValueError(error)
            result=json.loads(value or '[]');result=[result] if isinstance(result,dict) else result
            if not isinstance(result,list) or any(not isinstance(m,dict) or m.get('name') not in ('dbatools','SqlServer') for m in result):raise ValueError('Unexpected module response')
            modules=[{'name':m['name'],'version':str(m.get('version',''))} for m in result]
        except (ValueError,TypeError):errors.append({'probe':'PowerShell modules','status':'unavailable'})
    if system=='windows' and ps:
        command="@(Get-Service -ErrorAction Stop | Where-Object { $_.Name -match '^(MSSQLSERVER|MSSQL\\$|SQLSERVERAGENT|SQLAgent\\$|postgresql)' } | ForEach-Object { @{name=$_.Name;state=$_.Status.ToString()} }) | ConvertTo-Json -Compress"
        value,error=probe([ps,'-NoLogo','-NoProfile','-NonInteractive','-Command',command],execute)
        try:
            if error:raise ValueError(error)
            result=json.loads(value or '[]');result=[result] if isinstance(result,dict) else result
            if not isinstance(result,list):raise ValueError('Invalid service response')
            for row in result:
                if not isinstance(row,dict) or not re.match(r'^(MSSQLSERVER|MSSQL\$|SQLSERVERAGENT|SQLAgent\$|postgresql)',str(row.get('name','')),re.I):raise ValueError('Unexpected service')
                services.append({'kind':'postgresql' if row['name'].lower().startswith('postgresql') else 'sqlserver','evidence':'Windows service','name':row['name'],'state':str(row.get('state','unknown'))})
        except (ValueError,TypeError):errors.append({'probe':'Windows services','status':'unavailable'})
    elif system!='windows':
        ps_tool=which('ps')
        if ps_tool:
            value,error=probe([ps_tool,'-A','-o','comm='],execute)
            if error:errors.append({'probe':'process names','status':'unavailable'})
            else:
                for kind,basename in [('postgresql','postgres'),('sqlserver','sqlservr')]:
                    if any(Path(line.strip()).name==basename for line in value.splitlines()):services.append({'kind':kind,'evidence':'local process name','name':basename,'state':'process observed'})
    for name in ('docker',):
        if paths[name]:
            # Inspect context metadata before contacting an engine. Remote contexts
            # are intentionally excluded from local autodetection.
            host=os.environ.get('DOCKER_HOST','') if not os.environ.get('DOCKER_CONTEXT') else ''
            if not host:
                value,error=probe([paths[name],'context','inspect','--format','{{json .Endpoints.docker.Host}}'],execute)
                try:host=json.loads(value) if not error else ''
                except (ValueError,TypeError):host=''
            if not isinstance(host,str) or not host.startswith(('unix://','npipe://')):
                errors.append({'probe':'docker running images','status':'not checked: local socket context not established'});continue
            value,error=probe([paths[name],'ps','--format','{{.Image}}'],execute)
            if error:errors.append({'probe':name+' running images','status':'unavailable'})
            else:
                for kind,pattern in [('sqlserver',r'(^|/)mssql/server(:|@|$)'),('postgresql',r'(^|/)postgres(:|@|$)')]:
                    if any(re.search(pattern,line.strip()) for line in value.splitlines()):services.append({'kind':kind,'evidence':name+' image name','name':kind,'state':'running container observed; endpoint unknown'})
    suggestions=[{'id':'system','reason':'Local configuration inventory is available on this platform.','next':'Choose specific sections and verify permissions.'}]
    for engine,names_ in [('postgresql',('psql','pg_dump')),('sqlserver',('sqlpackage',))]:
        hints=[s for s in services if s['kind']==engine];present=any(paths[n] for n in names_) or (engine=='sqlserver' and any(m['name']=='dbatools' for m in modules))
        if hints or present:suggestions.append({'id':engine,'reason':'Local server evidence found.' if hints else 'Client tool presence only; no server established.',
                                               'next':'Choose an endpoint, client version and read-only credentials; run managed connection diagnostics.'})
    if paths['git']:suggestions.append({'id':'git','reason':'Git executable found.','next':'Configure a repository and credential provider only if you want Git history.'})
    return {'schema':1,'platform':system,'python':sys.version.split()[0],'python_executable':sys.executable,'python_packages':packages,
            'tools':tools,'powershell_modules':modules,'services':sorted(services,key=lambda x:(x['kind'],x['evidence'],x['name'])),
            'suggestions':suggestions,'probe_errors':errors,
            'limits':'Local presence hints only: no network scan, database connection, credential lookup, configuration changes or installations. Client/server version compatibility and collection permissions require separate diagnostics.'}


def installation_plan(dependency,report,*,venv=None,wheelhouse=None):
    if dependency not in DEPENDENCIES:raise ValueError('Unknown dependency')
    system=report['platform'];tools=report['tools'];path=lambda name:tools.get(name,{}).get('path')
    commands=[];notes=[];check=None
    if dependency=='python-packages':
        if not venv:raise ValueError('Choose a new virtual-environment directory')
        target=Path(venv).absolute()
        if target.exists():raise ValueError('Virtual-environment directory must be new; existing interpreters are not modified')
        python=str(target/('Scripts/python.exe' if system=='windows' else 'bin/python'))
        commands=[[sys.executable,'-m','venv',str(target)],[python,'-m','pip','--disable-pip-version-check','install']]
        if wheelhouse:
            wheelhouse=Path(wheelhouse).resolve()
            if not wheelhouse.is_dir():raise ValueError('Offline wheel folder is unavailable')
            commands[-1]+= ['--no-index','--find-links',str(wheelhouse)]
        commands[-1]+=['-r',str(Path(__file__).resolve().with_name('requirements.txt'))]
        check=[python,'-c',"import yaml,ruamel.yaml; from zoneinfo import ZoneInfo; ZoneInfo('America/New_York'); print('Dependency imports and timezone passed')"]
        notes=['Creates a new environment; rerun configure.py using its Python. Existing Python environments remain unchanged.',
               'Uses configured pip repositories/proxies unless an offline wheel folder is selected.']
    elif dependency in ('sqlpackage','powershell') and (dependency=='sqlpackage' or system!='windows'):
        if path('dotnet') and tools['dotnet'].get('version','').strip():commands=[[path('dotnet'),'tool','install','--global','Microsoft.SqlPackage' if dependency=='sqlpackage' else 'PowerShell']]
        else:notes=['Install the .NET SDK first, or use the official signed installer. A runtime alone is insufficient.']
    elif dependency=='dbatools':
        if path('pwsh'):commands=[[path('pwsh'),'-NoLogo','-NoProfile','-Command','Install-Module -Name dbatools -Scope CurrentUser -Repository PSGallery -ErrorAction Stop']]
        else:notes=['Install PowerShell 7 first.']
    elif system=='macos' and path('brew'):
        packages={'git':['git'],'postgresql-client':['libpq'],'dotnet-sdk':['--cask','dotnet-sdk']}
        if dependency in packages:commands=[[path('brew'),'install',*packages[dependency]]]
    elif system=='windows' and path('winget'):
        packages={'git':'Git.Git','powershell':'Microsoft.PowerShell','dotnet-sdk':'Microsoft.DotNet.SDK.10'}
        if dependency in packages:commands=[[path('winget'),'install','--id',packages[dependency],'--exact','--source','winget']]
    elif system=='linux' and path('apt-get') and dependency in ('git','postgresql-client'):
        prefix=[]
        if hasattr(os,'geteuid') and os.geteuid()!=0:
            if not path('sudo'):raise ValueError('An administrator must install this package; sudo unavailable')
            prefix=[path('sudo')]
        commands=[[*prefix,path('apt-get'),'install','git' if dependency=='git' else 'postgresql-client']]
    if not commands:notes.append('No automatic installer for this setup. Use the official instructions; PostgreSQL Windows server installers are not run as client dependencies.')
    return {'dependency':dependency,'commands':commands,'postcheck':check,'notes':notes,'source':SOURCES[dependency],
            'scope':'Clients and tools only. No database server, service role, repository setup, firewall or grant changes are automated.'}


def ready(dependency,report):
    tools=report['tools']
    if dependency=='dbatools':return any(m['name']=='dbatools' for m in report['powershell_modules'])
    names={'git':['git'],'postgresql-client':['psql','pg_dump','pg_dumpall'],'powershell':['pwsh'],'dotnet-sdk':['dotnet'],'sqlpackage':['sqlpackage']}.get(dependency,[])
    return bool(names) and all(tools[n]['status']=='present' and tools[n].get('version','').strip() for n in names)


def install(dependency,*,venv=None,wheelhouse=None,input_fn=input,execute=None,discover_fn=None):
    execute=execute or subprocess.run;discover_fn=discover_fn or discover
    before=discover_fn()
    if dependency!='python-packages' and ready(dependency,before):return {'status':'verified','already_present':True,'discovery':before}
    plan=installation_plan(dependency,before,venv=venv,wheelhouse=wheelhouse)
    print(json.dumps(plan,indent=2))
    if not plan['commands']:return {'status':'manual','plan':plan}
    for command in plan['commands']:print(subprocess.list2cmdline(command) if before['platform']=='windows' else shlex.join(command))
    if input_fn('Type INSTALL to run these commands (anything else cancels): ').strip()!='INSTALL':return {'status':'declined','plan':plan}
    try:
        for command in plan['commands']:
            result=execute(command,shell=False,check=False)
            if result.returncode:return {'status':'failed','exit_code':result.returncode,'plan':plan,'note':'No automatic rollback; review installer output and any newly created environment.'}
        if plan['postcheck']:
            result=execute(plan['postcheck'],shell=False,check=False)
            return {'status':'verified' if result.returncode==0 else 'unverified','plan':plan,'python':plan['commands'][1][0]}
        after=discover_fn()
        return {'status':'verified' if ready(dependency,after) else 'unverified','plan':plan,'discovery':after,
                'note':'Installation success does not prove database connectivity. Restart the terminal or set explicit client paths if tools remain undiscovered.'}
    except (OSError,KeyboardInterrupt) as exc:return {'status':'failed','error':type(exc).__name__,'plan':plan}


def add_suggestion(config,engine,report,input_fn=input):
    from configure import ask,boolean
    name=ask('Unique collector name','collect-'+engine,input_fn)
    if not re.fullmatch(r'[A-Za-z0-9_-]+',name):raise ValueError('Use letters, digits, underscore and hyphen for task names')
    names={t.get('name') for t in config.get('tasks',[])}
    if name in names or name+'-archive' in names:raise ValueError('Task name already exists')
    output='${CONFIGBACKUP_STAGING}/'+name
    task={'name':name,'type':'execute','enabled':True,'output_directory':output,'clean_output':True}
    if engine=='system':task.update(executable=sys.executable,arguments=[str(Path(__file__).resolve().parent/'collectors/system/collect_system.py')])
    elif engine in ('postgresql','sqlserver'):
        connection=ask('Unique shared connection name',engine,input_fn)
        if not connection or connection in (config.get('connections') or {}):raise ValueError('Choose a new connection name')
        profile={'engine':engine,'access_profile':'read-only','schema':True,'include_health':False,'include_history':False}
        tool=lambda n:report['tools'][n].get('path')
        if engine=='postgresql':
            profile.update(host=ask('PostgreSQL host (blank for local libpq defaults)','',input_fn),port=int(ask('Port','5432',input_fn)),
                           user=ask('Database user (blank for libpq defaults)','',input_fn),bin_dir=ask('Client bin directory; choose version compatible with the server',str(Path(tool('pg_dump')).parent) if tool('pg_dump') else '',input_fn))
            profile['password_env']=ask('Existing password environment variable name (blank for pgpass/peer/Kerberos)','',input_fn)
        else:
            profile.update(server=ask('SQL Server endpoint (host\\instance or host,port)',input_fn=input_fn),pwsh=tool('pwsh') or 'pwsh',sqlpackage=tool('sqlpackage') or 'sqlpackage',trust_server_certificate=False)
            if boolean('Use SQL authentication',False,input_fn):
                profile['user']=ask('SQL login',input_fn=input_fn);profile['password_env']=ask('Existing password environment variable name','CONFIGBACKUP_SQL_PASSWORD',input_fn)
        from database_diagnostics import validate
        validate(profile)
        config.setdefault('connections',{})[connection]=profile;task.update(connection=connection,collection={})
    else:raise ValueError('Unknown suggestion')
    config.setdefault('tasks',[]).extend([task,{'name':name+'-archive','enabled':True,'type':'directory','source':output,'destination':name,'storage':'filesystem','depends_on':[name],'collection_manifest':True}])
    print('Added unsaved tasks. Review scope, save, and run Shared connections -> Test managed task before scheduling.')


def wizard(config,input_fn=input):
    from configure import choice,ask
    report=discover()
    while True:
        print(json.dumps(report,indent=2))
        action=choice('Autodetect and guided setup',['Refresh detection','Configure suggested collector','Install a dependency','Back'],input_fn)
        if action in (None,'Back'):return
        if action=='Refresh detection':report=discover()
        elif action=='Configure suggested collector':
            options=[r['id'] for r in report['suggestions'] if r['id'] in ('system','sqlserver','postgresql')]
            # Remote collectors remain available even if local clients are absent.
            options+= [name for name in ('sqlserver','postgresql') if name not in options]
            engine=choice('Collector (remote endpoints are also supported)',options,input_fn)
            if engine:add_suggestion(config,engine,report,input_fn)
        else:
            dependency=choice('Dependency',list(DEPENDENCIES),input_fn)
            if not dependency:continue
            venv=ask('New virtual-environment directory',input_fn=input_fn) if dependency=='python-packages' else None
            wheelhouse=ask('Optional approved offline wheel folder (blank uses configured index)',input_fn=input_fn) if venv else None
            print(json.dumps(install(dependency,venv=venv,wheelhouse=wheelhouse,input_fn=input_fn),indent=2));report=discover()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install',choices=DEPENDENCIES);parser.add_argument('--venv');parser.add_argument('--wheelhouse')
    args=parser.parse_args(argv)
    if (args.venv or args.wheelhouse) and args.install!='python-packages':parser.error('--venv/--wheelhouse require --install python-packages')
    try:
        result=install(args.install,venv=args.venv,wheelhouse=args.wheelhouse) if args.install else discover()
        print(json.dumps(result,indent=2));return 0 if result.get('status','complete') in ('complete','verified','declined') else 6
    except (ValueError,OSError) as exc:print('Setup error: '+str(exc),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
