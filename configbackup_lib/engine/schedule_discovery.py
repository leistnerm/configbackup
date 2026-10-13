"""Read scheduled-job definitions and suggest explicit script/configuration files."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

CONFIG_FLAGS={'--config','--config-file','--configfile','--configuration','--settings','-config','-configurationfile','--defaults-file'}
SCRIPT_SUFFIXES={'.py','.ps1','.sh','.bash','.bat','.cmd','.pl','.rb'}
MAX_BYTES=2*1024*1024
MAX_JOBS=500
CONFIG_SUFFIXES={'.ini','.config','.json','.yaml','.yml','.toml','.conf','.properties','.xml'}
MAX_DIRECTORIES=50
MAX_DIRECTORY_ENTRIES=200


def split_command(command,windows=False):
    if windows:
        # Windows argv quoting: backslashes are special only immediately before quotes.
        parts=[];current=[];quoted=False;active=False;i=0
        while i<len(command):
            c=command[i]
            if c in ' \t' and not quoted:
                if active:parts.append(''.join(current));current=[];active=False
                i+=1;continue
            active=True
            if c=='\\':
                start=i
                while i<len(command) and command[i]=='\\':i+=1
                count=i-start
                if i<len(command) and command[i]=='"':
                    current.extend('\\'*(count//2))
                    if count%2:current.append('"')
                    else:quoted=not quoted
                    i+=1
                else:current.extend('\\'*count)
                continue
            if c=='"':quoted=not quoted
            else:current.append(c)
            i+=1
        if quoted:raise ValueError('Unbalanced quotes')
        if active:parts.append(''.join(current))
        return parts
    return shlex.split(command,posix=True)


def candidates(jobs,exists=None,scan_directories=True):
    exists=exists or (lambda p:Path(p).is_file());found={};unresolved=[];directories={};scans=[]
    for job in jobs:
        if job.get('parse_error'):unresolved.append({'job':job['name'],'reason':job['parse_error']});continue
        windows=job.get('platform')=='windows';arguments=job.get('arguments',[])
        if not isinstance(arguments,list) or not all(isinstance(v,str) for v in arguments):unresolved.append({'job':job['name'],'reason':'Unsupported argument format'});continue
        inline=any(v.casefold() in ('-command','-encodedcommand','-enc','-c','/c','-lc') for v in arguments)
        if inline:unresolved.append({'job':job['name'],'reason':'Inline or shell command requires manual review; not executed'})
        selected=[]
        if scan_directories:
            hints=[job.get('working_directory','')]
            for arg in arguments[:1] if inline else arguments:
                if arg==arguments[0] or Path(arg).suffix.lower() in SCRIPT_SUFFIXES:
                    p=PureWindowsPath(arg) if windows else Path(arg)
                    if not p.is_absolute() and job.get('working_directory'):p=(PureWindowsPath(job['working_directory']) if windows else Path(job['working_directory']))/p
                    if p.is_absolute():hints.append(str(p.parent))
            for hint in hints:
                if hint and not any(t in hint for t in ('$','%','*','?','`','\n','\r','://')):
                    directories.setdefault((windows,hint),set()).add(job['name'])
        if inline:continue
        for index,arg in enumerate(arguments):
            flag,sep,value=arg.partition('=')
            if flag.casefold() in CONFIG_FLAGS:
                if not sep:value=arguments[index+1] if index+1<len(arguments) else ''
                selected.append((value,'explicit configuration argument'))
            elif flag.casefold()=='-file':selected.append((arguments[index+1] if index+1<len(arguments) else '', 'PowerShell script'))
            elif Path(arg).suffix.lower() in SCRIPT_SUFFIXES:selected.append((arg,'script argument'))
        for value,reason in selected:
            if not value or any(token in value for token in ('$','%','*','?','`','\n','\r','://')):
                unresolved.append({'job':job['name'],'reason':'Dynamic, remote or wildcard path requires manual review'});continue
            path=PureWindowsPath(value) if windows else Path(value)
            if not path.is_absolute():
                base=job.get('working_directory','')
                if not base or any(t in base for t in ('$','%','*','?')):
                    unresolved.append({'job':job['name'],'reason':'Relative file has no explicit working directory'});continue
                path=(PureWindowsPath(base) if windows else Path(base))/path
            if '..' in path.parts:
                unresolved.append({'job':job['name'],'reason':'Parent traversal requires manual review'});continue
            value=str(path);key=value.casefold() if windows else value
            status='unverified foreign platform' if windows!=(os.name=='nt') else 'available' if exists(value) else 'missing or not a regular file'
            row=found.setdefault(key,{'path':value,'status':status,'jobs':[],'reasons':[],
                    'storage':'filesystem only; file may contain credentials','selected':False})
            if job['name'] not in row['jobs']:row['jobs'].append(job['name'])
            if reason not in row['reasons']:row['reasons'].append(reason)
    shared={str(Path(sys.executable).absolute().parent),str(Path(sys.executable).resolve().parent),'/bin','/sbin','/usr/bin','/usr/sbin','/usr/local/bin','/opt/homebrew/bin','/opt/homebrew/sbin'}
    windows_root=os.environ.get('SystemRoot',r'C:\Windows')
    shared.update(str(PureWindowsPath(windows_root)/p) for p in ('System32','SysWOW64'))
    for number,((windows,directory),names) in enumerate(sorted(directories.items(),key=lambda item:item[0][1])):
        entry={'directory':directory,'jobs':sorted(names),'status':'unavailable','suggestions':0};scans.append(entry)
        if number>=MAX_DIRECTORIES:entry['status']='directory limit reached';continue
        if windows!=(os.name=='nt'):entry['status']='foreign platform';continue
        if any(directory.rstrip('/\\').casefold()==p.rstrip('/\\').casefold() for p in shared):entry['status']='shared executable directory skipped';continue
        path=Path(directory)
        if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in [path,*path.parents]):entry['status']='relative, parent-traversing or symlink directory skipped';continue
        try:
            entries=[]
            with os.scandir(path) as stream:
                for item in stream:
                    entries.append(item)
                    if len(entries)>MAX_DIRECTORY_ENTRIES:break
            if len(entries)>MAX_DIRECTORY_ENTRIES:entry['status']='entry limit reached; no directory suggestions';continue
            for item in sorted(entries,key=lambda e:e.name):
                if Path(item.name).suffix.lower() not in CONFIG_SUFFIXES or item.is_symlink() or not item.is_file(follow_symlinks=False):continue
                value=str(path/item.name);key=value.casefold() if windows else value
                row=found.setdefault(key,{'path':value,'status':'available','jobs':[],'reasons':[],'storage':'filesystem only; file may contain credentials','selected':False})
                for name in sorted(names):
                    if name not in row['jobs']:row['jobs'].append(name)
                if 'possible configuration in job directory' not in row['reasons']:row['reasons'].append('possible configuration in job directory')
                entry['suggestions']+=1
            entry['status']='scanned top level only'
        except OSError:entry['status']='unavailable'
    for row in found.values():row['jobs'].sort();row['reasons'].sort()
    return {'candidates':sorted(found.values(),key=lambda r:r['path']),'unresolved':unresolved,'directory_scans':scans,
      'limits':'Suggestions only. No jobs or scripts execute; contents and credential values are not inspected. Includes, generated paths, environment expansion, indirect dependencies and protected accounts may be missed.'}


def windows_xml(text,name):
    root=ET.fromstring(text);ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'};jobs=[]
    for index,action in enumerate(root.findall('.//t:Actions/t:Exec',ns)):
        command=action.findtext('t:Command','',ns);args=action.findtext('t:Arguments','',ns)
        try:arguments=[command,*split_command(args,True)]
        except ValueError:
            jobs.append({'name':name+' action '+str(index+1),'parse_error':'Unsupported quoted arguments'});continue
        jobs.append({'name':name+' action '+str(index+1),'platform':'windows','working_directory':action.findtext('t:WorkingDirectory','',ns),'arguments':arguments})
    return jobs


def cron(text,name,system=False):
    jobs=[]
    for number,line in enumerate(text.splitlines(),1):
        line=line.strip()
        if not line or line.startswith('#') or re.match(r'[A-Za-z_][A-Za-z0-9_]*\s*=',line):continue
        fields=line.split(None,(2 if system else 1) if line.startswith('@') else (6 if system else 5))
        expected=(3 if system else 2) if line.startswith('@') else (7 if system else 6)
        if len(fields)!=expected:continue
        try:arguments=split_command(fields[-1])
        except ValueError:
            jobs.append({'name':name+':'+str(number),'parse_error':'Unsupported quoted command'});continue
        jobs.append({'name':name+':'+str(number),'platform':'unix','arguments':arguments,'working_directory':''})
    return jobs


def launchd(data,name):
    value=plistlib.loads(data)
    if not isinstance(value,dict):raise ValueError('launchd definition must be a mapping')
    args=value.get('ProgramArguments') or ([value['Program']] if value.get('Program') else [])
    if not isinstance(args,list) or any(not isinstance(v,str) for v in args) or not isinstance(value.get('WorkingDirectory',''),str):raise ValueError('Unsupported launchd arguments')
    return [{'name':name,'platform':'unix','arguments':args,'working_directory':value.get('WorkingDirectory','')}]


def systemd(text,name):
    working='';commands=[]
    for line in text.splitlines():
        line=line.strip()
        if line.startswith('WorkingDirectory='):working=line.split('=',1)[1].strip()
        elif line.startswith('ExecStart='):
            command=line.split('=',1)[1]
            if command:commands.append(split_command(command))
            else:commands=[]
    return [{'name':name+' action '+str(i+1),'platform':'unix','arguments':args,'working_directory':working} for i,args in enumerate(commands)]


def discover(*,definitions=None,system=None,execute=None,which=None,roots=None,include_services=False,user_timers=False):
    shell=None;system=system or platform.system().lower();execute=execute or subprocess.run;which=which or shutil.which;jobs=[];errors=[]
    def parse(path,kind=None):
        try:
            path=Path(path)
            if path.is_symlink() or path.stat().st_size>MAX_BYTES:raise ValueError('Unsupported definition')
            data=path.read_bytes()
            if path.suffix.lower()=='.xml':jobs.extend(windows_xml(data,str(path)))
            elif path.suffix.lower()=='.plist':jobs.extend(launchd(data,str(path)))
            elif path.suffix.lower()=='.service':jobs.extend(systemd(data.decode('utf-8'),str(path)))
            else:jobs.extend(cron(data.decode('utf-8'),str(path),system=kind=='system-cron'))
        except (OSError,ValueError,ET.ParseError,plistlib.InvalidFileException):errors.append({'source':str(path),'status':'unavailable or unsupported'})
    def run(args):
        try:
            result=execute(args,check=False,capture_output=True,text=True,timeout=30)
            if result.returncode or len(result.stdout.encode('utf-8'))>MAX_BYTES:raise ValueError('Probe failed')
            return result.stdout
        except (OSError,subprocess.TimeoutExpired,ValueError):errors.append({'source':Path(args[0]).name,'status':'unavailable'});return None
    if definitions is not None:
        for path in list(definitions)[:MAX_JOBS]:parse(path)
        if len(definitions)>MAX_JOBS:errors.append({'source':'definitions','status':'limit reached'})
    elif system=='windows':
        shell=which('powershell.exe') or which('pwsh')
        if shell:
            script="$ErrorActionPreference='Stop'; $items=@(Get-ScheduledTask -ErrorAction Stop | Sort-Object TaskPath,TaskName | Select-Object -First 501); @($items | ForEach-Object { try { [pscustomobject]@{name=($_.TaskPath+$_.TaskName);xml=(Export-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -ErrorAction Stop)} } catch { [pscustomobject]@{name=($_.TaskPath+$_.TaskName);unavailable=$true} } }) | ConvertTo-Json -Depth 5 -Compress"
            text=run([shell,'-NoProfile','-NonInteractive','-Command',script])
            if text:
                try:
                    rows=json.loads(text);rows=[rows] if isinstance(rows,dict) else rows
                    if not isinstance(rows,list):raise ValueError('Invalid response')
                    for row in rows[:MAX_JOBS]:
                        if not isinstance(row,dict):errors.append({'source':'scheduled task','status':'unsupported record'});continue
                        try:
                            if not isinstance(row.get('name'),str):raise ValueError('Missing task name')
                            if row.get('unavailable'):errors.append({'source':row['name'],'status':'unavailable'});continue
                            if not isinstance(row.get('xml'),str):raise ValueError('Missing task XML')
                            jobs.extend(windows_xml(row['xml'],row['name']))
                        except (ValueError,KeyError,TypeError,ET.ParseError):errors.append({'source':str(row.get('name','task')),'status':'unsupported'})
                    if len(rows)>MAX_JOBS:errors.append({'source':'scheduled tasks','status':'limit reached'})
                except (ValueError,KeyError,TypeError):errors.append({'source':'scheduled tasks','status':'invalid response'})
        else:errors.append({'source':'scheduled tasks','status':'PowerShell unavailable'})
    else:
        crontab=which('crontab')
        if crontab:
            text=run([crontab,'-l'])
            if text is not None:
                try:jobs.extend(cron(text,'current user crontab'))
                except ValueError:errors.append({'source':'crontab','status':'unsupported quoting'})
        if roots is None:
            roots=[Path('/etc/crontab'),Path('/etc/cron.d')]
            if system=='darwin':roots += [Path.home()/'Library/LaunchAgents',Path('/Library/LaunchAgents'),Path('/Library/LaunchDaemons')]
        count=0
        for root in roots:
            root=Path(root)
            if not root.exists():continue
            try:paths=sorted(root.iterdir()) if root.is_dir() else [root]
            except OSError:errors.append({'source':str(root),'status':'unavailable'});continue
            for path in paths:
                if count>=MAX_JOBS:errors.append({'source':str(root),'status':'limit reached'});break
                if path.is_file() and (path.suffix=='.plist' or root.name=='cron.d' or path.name=='crontab'):
                    parse(path,'system-cron' if path.suffix!='.plist' else None);count+=1
        manager=which('systemctl') if system=='linux' else None
        if manager:
            text=run([manager,'list-unit-files','--type=timer','--no-pager','--no-legend'])
            for line in (text or '').splitlines()[:MAX_JOBS]:
                unit=line.split()[0] if line.split() else ''
                if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.timer',unit):continue
                # Read the timer's actual Unit= mapping; default is same-name .service.
                timer=run([manager,'cat',unit,'--no-pager'])
                service=unit[:-6]+'.service'
                for entry in (timer or '').splitlines():
                    if entry.strip().startswith('Unit='):service=entry.strip().split('=',1)[1]
                if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',service):errors.append({'source':unit,'status':'dynamic service mapping unsupported'});continue
                text=run([manager,'cat',service,'--no-pager'])
                if text:
                    try:jobs.extend(systemd(text,service))
                    except ValueError:errors.append({'source':service,'status':'unsupported quoting'})
    if definitions is None and include_services and system=='windows' and shell:
        service_text=run([shell,'-NoProfile','-NonInteractive','-Command',"Get-CimInstance Win32_Service -ErrorAction Stop | Sort-Object Name | Select-Object -First 501 Name,PathName | ConvertTo-Json -Compress"])
        if service_text:
            try:
                services=json.loads(service_text);services=[services] if isinstance(services,dict) else services
                if not isinstance(services,list):raise ValueError('Invalid services')
                for row in services[:MAX_JOBS]:
                    try:
                        if not isinstance(row,dict) or not isinstance(row.get('PathName'),str):raise ValueError('Unsupported service')
                        jobs.append({'name':'service '+row['Name'],'platform':'windows','arguments':split_command(row['PathName'],True),'working_directory':''})
                    except (ValueError,KeyError,TypeError):errors.append({'source':'service','status':'unsupported'})
                if len(services)>MAX_JOBS:errors.append({'source':'services','status':'limit reached'})
            except (ValueError,TypeError):errors.append({'source':'services','status':'invalid response'})
    if system=='linux' and definitions is None and (include_services or user_timers):
        manager=which('systemctl')
        if manager:
            for user in ([False,True] if user_timers else [False]):
                prefix=[manager]+(['--user'] if user else [])
                for kind in (['service','timer'] if user and include_services else ['timer'] if user else ['service'] if include_services else []):
                    listing=run([*prefix,'list-unit-files','--type='+kind,'--no-pager','--no-legend'])
                    lines=(listing or '').splitlines()
                    if len(lines)>MAX_JOBS:errors.append({'source':'systemd '+kind,'status':'limit reached'})
                    for line in lines[:MAX_JOBS]:
                        unit=line.split()[0] if line.split() else ''
                        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.(service|timer)',unit):continue
                        if unit.endswith('.timer'):
                            timer=run([*prefix,'cat',unit,'--no-pager']);service=unit[:-6]+'.service'
                            for entry in (timer or '').splitlines():
                                if entry.strip().startswith('Unit='):service=entry.strip().split('=',1)[1]
                            unit=service
                        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',unit):continue
                        text=run([*prefix,'cat',unit,'--no-pager'])
                        try:
                            if text:jobs.extend(systemd(text,('user ' if user else 'system ')+unit))
                        except ValueError:errors.append({'source':unit,'status':'unsupported'})
    report=candidates(jobs[:MAX_JOBS]);report.update(schema=1,jobs_inspected=min(len(jobs),MAX_JOBS),probe_errors=errors)
    if len(jobs)>MAX_JOBS:report['probe_errors'].append({'source':'jobs','status':'limit reached'})
    return report


def select(config,report,paths):
    eligible={r['path']:r for r in report['candidates'] if r['status']=='available'}
    if any(p not in eligible for p in paths):raise ValueError('Only available local candidate files can be selected')
    backup=config.setdefault('backup',{});files=backup.setdefault('configuration_files',[])
    for path in paths:
        if path not in files:files.append(path)
    return len(paths)


def wizard(config,input_fn=input):
    from configbackup_lib.ui.configure_features import select_many
    print('Reads local scheduled definitions only; no jobs execute. Candidates may contain secrets and use filesystem-only backup.')
    report=discover();print(json.dumps(report,indent=2));available=[r['path'] for r in report['candidates'] if r['status']=='available']
    if available:
        chosen=select_many('Select files to add to private backup',available,[],input_fn);select(config,report,chosen)
        print('Added selections to unsaved configuration. Review and Save; no backup was run.')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--definition',action='append',help='Read saved task XML, launchd plist, service unit or user crontab; repeatable')
    parser.add_argument('--services',action='store_true');parser.add_argument('--user-timers',action='store_true')
    args=parser.parse_args(argv);print(json.dumps(discover(definitions=args.definition,include_services=args.services,user_timers=args.user_timers),indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
