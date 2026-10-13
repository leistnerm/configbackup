"""Opt-in native recording controls. Owns only a dedicated ConfigBackup recorder."""
from __future__ import annotations
from configbackup_lib import APPLICATION_ROOT
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import time
from configbackup_lib.recovery.transaction_journal import atomic

DEFAULT_COUNTERS=[r'\Processor(_Total)\% Processor Time',r'\System\Processor Queue Length',r'\Memory\Available MBytes',r'\Memory\% Committed Bytes In Use',r'\Memory\Pages/sec',r'\PhysicalDisk(*)\Avg. Disk sec/Read',r'\PhysicalDisk(*)\Avg. Disk sec/Write',r'\PhysicalDisk(*)\Avg. Disk Queue Length',r'\PhysicalDisk(*)\Disk Read Bytes/sec',r'\PhysicalDisk(*)\Disk Write Bytes/sec']


def state_path(config_path):return Path(str(Path(config_path).resolve())+'.performance.json')


def settings(config,config_path,system=None):
    from configbackup_lib.engine.configbackup import expand_value
    system=system or platform.system();value=dict(config.get('performance',{}).get('recording',{}))
    value=expand_value(value,config.get('variables',{}),strict=True)
    value.setdefault('backend',{'Windows':'perfmon','Linux':'sysstat','Darwin':'psutil'}.get(system,'unsupported'))
    value.setdefault('directory',str(Path(config_path).resolve().parent/'performance-history'))
    directory=Path(value['directory']).expanduser().absolute()
    if directory==Path(directory.anchor) or any(c in str(directory) for c in '\n\r\x00'):raise ValueError('Choose a dedicated private performance directory')
    value['directory']=str(directory)
    for key,default,low,high in [('interval_seconds',15,5,3600),('keep_days',7,1,365),('max_mb',256,1,4096),('chunk_samples',60,2,1000)]:
        value.setdefault(key,default)
        if not isinstance(value[key],int) or isinstance(value[key],bool) or not low<=value[key]<=high:raise ValueError(key+' outside supported range')
    if value['backend']!= {'Windows':'perfmon','Linux':'sysstat','Darwin':'psutil'}.get(system):raise ValueError('Backend is unavailable on this platform')
    counters=value.get('counters',DEFAULT_COUNTERS)
    if not isinstance(counters,list) or not counters or any(not isinstance(c,str) or not c.startswith('\\') or any(x in c for x in '\n\r\x00') for c in counters):raise ValueError('Invalid PerfMon counter paths')
    value['counters']=counters
    return value


def prerequisites(value,which=shutil.which):
    names={'perfmon':['logman','relog','schtasks','powershell.exe'],'sysstat':['sar','sadf','systemctl'],'psutil':['launchctl']}[value['backend']]
    missing=[name for name in names if not which(name)]
    if value['backend']=='psutil':
        try:__import__('psutil')
        except ImportError:missing.append('Python psutil (requirements-performance.txt)')
    return {'status':'ready' if not missing else 'missing','missing':missing,
        'guidance':'PerfMon uses a dedicated startup task under SYSTEM; run enable/disable elevated. Linux uses a user systemd service; unattended boot needs a separately reviewed login/linger policy. macOS uses a user LaunchAgent and optional psutil fallback. No global sysstat or existing PerfMon recorder is changed.'}


def plan(config,config_path,action,system=None,home=None):
    if action not in ('enable','disable'):raise ValueError('Unknown recorder action')
    system=system or platform.system();config_path=Path(config_path).resolve();anchor=state_path(config_path)
    if anchor.is_symlink():raise ValueError('Recorder ownership symlink refused')
    saved=json.loads(anchor.read_text()) if anchor.exists() else None
    if saved and (saved.get('config')!=str(config_path) or saved.get('system')!=system):raise ValueError('Recorder ownership differs from this configuration/platform')
    value=saved['settings'] if action=='disable' and saved else settings(config,config_path,system)
    name='ConfigBackup-Performance-'+hashlib.sha256(str(config_path).encode()).hexdigest()[:12]
    if action=='disable' and (not saved or not saved.get('owned')):raise ValueError('No ConfigBackup-owned recorder is registered')
    directory=Path(value['directory']);home=Path(home or Path.home());worker=str((APPLICATION_ROOT / 'native_recording.py'));snapshot=directory/'recorder.json';files={};commands=[]
    if system=='Windows':
        parts=set((saved or {}).get('components',[]))
        stop=['powershell.exe','-NoProfile','-NonInteractive','-Command',"$ErrorActionPreference='Stop'; $s=New-Object -ComObject Pla.DataCollectorSet; $s.Query('"+name+"',$null); if ($s.Status -eq 1) {$s.Stop($true)} elseif ($s.Status -ne 0) {throw 'Recorder is transitional'}"]
        if action=='enable':
            files[str(directory/'counters.txt')]='\n'.join(value['counters'])+'\n'
            if 'counter' in parts:commands.append(stop)
            commands.append(['logman','update' if 'counter' in parts else 'create','counter',name,'-cf',str(directory/'counters.txt'),'-si',str(value['interval_seconds']),'-f','bincirc','-v-','-a','-max',str(value['max_mb']),'-o',str(directory/'current.blg')])
            task=['schtasks','/Create','/TN',name,'/SC','ONSTART','/RU','SYSTEM','/RL','HIGHEST','/TR',subprocess.list2cmdline([shutil.which('logman') or 'logman.exe','start',name])]
            if 'startup' in parts:task.append('/F')
            commands.extend([task,['logman','start',name]])
        else:
            if 'startup' in parts:commands.append(['schtasks','/Change','/TN',name,'/Disable'])
            if 'counter' in parts:commands.append(stop)
    elif system=='Linux':
        unit=home/'.config/systemd/user'/(name+'.service')
        if action=='enable':
            quote=lambda s:'"'+str(s).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
            files[str(unit)]='[Unit]\nDescription=ConfigBackup native sysstat recording\n[Service]\nType=simple\nExecStart='+' '.join(quote(s) for s in [sys.executable,worker,'record','--settings',str(snapshot)])+'\nRestart=on-failure\nRestartSec=30\nUMask=0077\n[Install]\nWantedBy=default.target\n'
            commands=[['systemctl','--user','daemon-reload'],['systemctl','--user','enable',unit.name],['systemctl','--user','restart',unit.name],['systemctl','--user','is-active',unit.name]]
        else:commands=[['systemctl','--user','disable','--now',unit.name]]
    elif system=='Darwin':
        unit=home/'Library/LaunchAgents'/(name+'.plist');domain='gui/'+str(getattr(os,'getuid',lambda:0)());service=domain+'/'+name
        data={'Label':name,'ProgramArguments':[sys.executable,worker,'record','--settings',str(snapshot)],'RunAtLoad':action=='enable','Disabled':action=='disable','KeepAlive':{'SuccessfulExit':False} if action=='enable' else False,'ThrottleInterval':30}
        files[str(unit)]=plistlib.dumps(data).decode()
        if action=='enable':
            if saved and saved.get('owned'):commands.append(['launchctl','bootout',service])
            commands.extend([['launchctl','enable',service],['launchctl','bootstrap',domain,str(unit)],['launchctl','print',service]])
        else:commands=[['launchctl','disable',service],['launchctl','bootout',service]]
    else:raise ValueError('Unsupported recorder platform')
    if action=='enable':files[str(snapshot)]=json.dumps(value,indent=2,sort_keys=True)+'\n'
    payload={'config':str(config_path),'system':system,'action':action,'name':name,'settings':value,'files':files,'commands':commands,'prior_owned':bool(saved and saved.get('owned')),'prior_state':hashlib.sha256(anchor.read_bytes()).hexdigest() if saved else None,
        'limits':'Native Windows/Linux startup controls require platform acceptance. Disabling stops only this dedicated recorder and retains history. PerfMon BLG is a size-bounded circular file; keep_days restricts exports, not the age of records remaining inside BLG. Existing global sysstat/PerfMon services remain untouched.'}
    payload['plan_id']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest();return payload


def apply(config,config_path,action,plan_id,*,execute=subprocess.run,system=None,home=None):
    from configbackup_lib.engine.configbackup import single_instance_lock
    anchor=state_path(config_path)
    with single_instance_lock(anchor.with_suffix(anchor.suffix+'.lock')):
        value=plan(config,config_path,action,system,home)
        if value['plan_id']!=plan_id:raise ValueError('Recorder plan changed; inspect again')
        saved=json.loads(anchor.read_text()) if anchor.exists() else {};owned_files=dict(saved.get('files',{}))
        ready=prerequisites(value['settings'])
        if action=='enable' and ready['missing']:raise ValueError('Missing prerequisites: '+', '.join(ready['missing']))
        for name in value['files']:
            path=Path(name)
            if any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Recorder file symlink refused')
            if path.exists() and (not path.is_file() or owned_files.get(name)!=hashlib.sha256(path.read_bytes()).hexdigest()):raise ValueError('Refusing an unmanaged or modified recorder file: '+name)
        directory=Path(value['settings']['directory'])
        if any(p.is_symlink() for p in [directory,*directory.parents]):raise ValueError('Recorder directory symlink refused')
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        ownership={'schema':1,'config':value['config'],'system':value['system'],'name':value['name'],'settings':value['settings'],'owned':value['prior_owned'],'status':'configuring','files':owned_files,'components':list(saved.get('components',[])),'results':[]}
        def save():atomic(anchor,(json.dumps(ownership,sort_keys=True,indent=2)+'\n').encode())
        try:
            for name,content in value['files'].items():
                atomic(Path(name),content.encode());ownership['files'][name]=hashlib.sha256(content.encode()).hexdigest();save()
            if value['system']!='Windows':ownership['owned']=True;save()
            for command in value['commands']:
                if command[:2]==['launchctl','bootout']:
                    probe=execute(['launchctl','print',command[2]],capture_output=True,text=True,timeout=60)
                    if probe.returncode:
                        ownership['results'].append({'command':command,'status':'not_loaded','probe_return_code':probe.returncode});continue
                completed=execute(command,capture_output=True,text=True,timeout=60)
                ownership['results'].append({'command':command,'return_code':completed.returncode})
                if completed.returncode:raise RuntimeError('Recorder command failed: '+command[0])
                if command[:3] in (['logman','create','counter'],['logman','update','counter']):
                    ownership['owned']=True
                    if 'counter' not in ownership['components']:ownership['components'].append('counter')
                if command[:2]==['schtasks','/Create'] and 'startup' not in ownership['components']:ownership['components'].append('startup')
                save()
            ownership['status']='enabled' if action=='enable' else 'disabled'
        except Exception:
            ownership['status']='failed';raise
        finally:save()
        from configbackup_lib.recovery.recovery_assets import register_launcher
        register_launcher(config_path,[anchor,*map(Path,ownership['files'])],{'mode':'performance recorder','platform':value['system'],'recorder':value['name']})
        return ownership


def history_records(root):
    root=Path(root);index=root/'history-index.json'
    if index.is_symlink() or index.exists() and index.stat().st_size>4*1024*1024:raise ValueError('Unsafe history index')
    records=json.loads(index.read_text()) if index.exists() else []
    if not isinstance(records,list) or len(records)>10000:raise ValueError('Invalid/oversized history index')
    seen=set();prior=0
    for row in records:
        if not isinstance(row,dict) or not isinstance(row.get('created'),(int,float)) or not prior<=row['created']<=time.time()+86400:raise ValueError('Invalid history order/time')
        prior=row['created']
        if not isinstance(row.get('files'),list) or not 1<=len(row['files'])<=2:raise ValueError('Invalid history files')
        for name in row['files']:
            if not isinstance(name,str) or Path(name).name!=name or not name.startswith('sample-') or Path(name).suffix not in ('.sa','.json') or name in seen or (root/name).is_symlink():raise ValueError('Unsafe history index entry')
            seen.add(name)
    return records


def retain_history(root,value):
    """Prune only indexed chunks, after verifying a readable newest survivor."""
    root=Path(root);index=root/'history-index.json';records=history_records(root)
    if not records:return
    newest=records[-1]
    from configbackup_lib.reporting.performance_report import load_source
    for name in newest['files']:
        if not (root/name).is_file():raise ValueError('Latest history chunk is incomplete; retention refused')
        if name.endswith('.json'):load_source({'type':'sysstat_json' if value['backend']=='sysstat' else 'normalized_json','path':str(root/name)})
    cutoff=time.time()-value['keep_days']*86400;remaining=[]
    sizes={r['created']:sum((root/f).stat().st_size for f in r['files'] if (root/f).is_file()) for r in records};total=sum(sizes.values())
    for row in records[:-1]:
        if row['created']<cutoff or total>value['max_mb']*1024*1024:
            for name in row['files']:(root/name).unlink(missing_ok=True)
            total-=sizes[row['created']]
        else:remaining.append(row)
    remaining+=records[-1:];atomic(index,(json.dumps(remaining,sort_keys=True,indent=2)+'\n').encode())


def reconcile_capture(root):
    """Reclaim only files named by our durable in-progress capture record."""
    import re
    root=Path(root);pending=root/'capture-pending.json'
    if not pending.exists():return {'status':'clean','removed':[]}
    if pending.is_symlink() or pending.stat().st_size>16384:raise ValueError('Unsafe capture journal')
    value=json.loads(pending.read_text());names=value.get('files')
    if value.get('schema')!=1 or not isinstance(names,list) or not 1<=len(names)<=3 or len(set(names))!=len(names):raise ValueError('Invalid capture journal')
    for name in names:
        if not isinstance(name,str) or not re.fullmatch(r'sample-\d{8}T\d{12}Z\.(?:sa|json|partial\.json)',name) or (root/name).is_symlink():raise ValueError('Unsafe capture journal path')
    indexed={f for row in history_records(root) for f in row['files']};removed=[]
    for name in names:
        if name not in indexed:
            (root/name).unlink(missing_ok=True);removed.append(name)
    pending.unlink()
    from configbackup_lib.recovery.transaction_journal import fsync_directory
    fsync_directory(root)
    return {'status':'reconciled','removed':removed}


def record(value,once=False):
    from configbackup_lib.engine.configbackup import single_instance_lock
    # Revalidate snapshots; a malformed/edited backend must not silently fall back.
    value=settings({'performance':{'recording':value}},Path(value.get('directory','.'))/'recorder-config.yaml')
    if value['backend']=='perfmon':raise ValueError('PerfMon is controlled by logman, not the worker')
    root=Path(value['directory'])
    if any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('Recorder directory symlink refused')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with single_instance_lock(root/'.recorder.lock'):
        reconcile_capture(root)
        while True:
            history=history_records(root);stamp='sample-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');created=time.time();files=[]
            # Two native outputs share at most half the configured history budget.
            # OS file-size limits constrain each child even before rotation runs.
            limit=min(25*1024*1024,value['max_mb']*1024*1024//4)
            if shutil.disk_usage(root).free<2*limit:raise ValueError('Insufficient free space for next bounded history chunk')
            temporary=root/(stamp+'.partial.json');binary=root/(stamp+'.sa');target=root/(stamp+'.json')
            atomic(root/'capture-pending.json',(json.dumps({'schema':1,'files':[temporary.name,binary.name,target.name]},sort_keys=True)+'\n').encode())
            try:
                if value['backend']=='sysstat':
                    import resource
                    def bounded_file():resource.setrlimit(resource.RLIMIT_FSIZE,(limit,limit))
                    result=subprocess.run(['sar','-A','-o',str(binary),str(value['interval_seconds']),str(value['chunk_samples'])],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=value['interval_seconds']*(value['chunk_samples']+3),preexec_fn=bounded_file)
                    if result.returncode:raise RuntimeError('Native sar recording failed or exceeded chunk size budget')
                    binary.chmod(0o600)
                    with temporary.open('xb') as output:
                        result=subprocess.run(['sadf','-j',str(binary),'--','-A'],stdout=output,stderr=subprocess.DEVNULL,timeout=120,preexec_fn=bounded_file)
                    if result.returncode:raise RuntimeError('Native sadf export failed or exceeded chunk size budget')
                    temporary.chmod(0o600)
                    from configbackup_lib.reporting.performance_report import load_source
                    load_source({'path':str(temporary),'type':'sysstat_json'},limit)
                    os.replace(temporary,target);files=[binary.name,target.name]
                else:
                    import psutil
                    from configbackup_lib.reporting.performance_report import sample
                    rows=[];psutil.cpu_percent();previous=dt.datetime.now(dt.timezone.utc)
                    for _ in range(value['chunk_samples']):
                        cpu=psutil.cpu_percent(interval=value['interval_seconds']);now=dt.datetime.now(dt.timezone.utc);memory=psutil.virtual_memory();host=platform.node()
                        rows.extend([sample(host,'host','cpu.busy_percent','percent',previous,now,cpu),sample(host,'host','memory.used_percent','percent',previous,now,memory.percent),sample(host,'host','memory.available_mb','MiB',previous,now,memory.available/1024**2)]);previous=now
                    data=(json.dumps({'samples':rows,'limits':'macOS fallback records CPU and memory only; disk latency, queue and paging unavailable.'},sort_keys=True)+'\n').encode()
                    if len(data)>limit:raise ValueError('Fallback chunk exceeds size budget')
                    atomic(target,data);files=[target.name]
                history.append({'created':created,'files':files});atomic(root/'history-index.json',(json.dumps(history,indent=2)+'\n').encode())
            except Exception:
                # Only this uncommitted, uniquely named attempt is cleaned up.
                if not any(target.name in r['files'] for r in history_records(root)):
                    target.unlink(missing_ok=True);binary.unlink(missing_ok=True)
                reconcile_capture(root)
                raise
            finally:temporary.unlink(missing_ok=True)
            reconcile_capture(root)
            retain_history(root,value)
            if once:return


def export_perfmon(value):
    root=Path(value['directory']);target=root/'perfmon-export.csv';temporary=root/'perfmon-export.partial.csv'
    begin=(dt.datetime.now()-dt.timedelta(days=value['keep_days'])).strftime('%m/%d/%Y %H:%M:%S')
    try:
        result=subprocess.run(['relog',str(root/'current.blg'),'-f','csv','-o',str(temporary),'-b',begin,'-y'],capture_output=True,timeout=120)
        if result.returncode or not temporary.is_file():raise ValueError('PerfMon export failed; prior CSV preserved')
        from configbackup_lib.reporting.performance_report import load_source
        load_source({'type':'perfmon_csv','host':'local','timezone':value.get('timezone'),'path':str(temporary)})
        os.replace(temporary,target)
    finally:temporary.unlink(missing_ok=True)
    return target


def wizard(config,config_path,input_fn=input):
    from configbackup_lib.ui.configure import choice,ask,boolean
    while True:
        action=choice('Performance recording',['Configure recording','Show prerequisites / status','Enable recorder','Disable recorder','Configure reports','Generate performance report','Back'],input_fn)
        if action in (None,'Back'):return
        perf=config.setdefault('performance',{})
        if action=='Configure recording':
            value=settings(config,config_path);print(prerequisites(value)['guidance'])
            value['directory']=ask('Private runtime history directory',value['directory'],input_fn)
            for key,label in [('interval_seconds','Sample interval seconds (5..3600)'),('keep_days','History/export days (1..365)'),('max_mb','History size budget MiB (1..4096)')]:value[key]=int(ask(label,value[key],input_fn))
            if value['backend']=='perfmon':value['timezone']=ask('PerfMon timestamp IANA time zone',value.get('timezone','UTC'),input_fn)
            candidate={**config,'performance':{**perf,'recording':value}};settings(candidate,config_path);perf['recording']=value;print('Settings are unsaved. Enabling applies the reviewed settings immediately and records ownership.');continue
        if action=='Show prerequisites / status':
            from configbackup_lib.engine.recorder_health import inspect
            print(json.dumps(inspect(config,config_path),indent=2))
        elif action in ('Enable recorder','Disable recorder'):
            verb='enable' if action=='Enable recorder' else 'disable';value=plan(config,config_path,verb);print(json.dumps(value,indent=2))
            if boolean('Apply these native recorder changes now',False,input_fn):print(json.dumps(apply(config,config_path,verb,value['plan_id']),indent=2))
        elif action=='Configure reports':
            perf['enabled']=boolean('Generate reports during ConfigBackup runs',perf.get('enabled',False),input_fn)
            perf['output']=ask('Private performance report directory',perf.get('output',str(Path(config_path).resolve().parent/'performance-report')),input_fn)
            value=settings(config,config_path);source={'id':'local-performance','type':'perfmon_csv' if value['backend']=='perfmon' else 'native_history', 'path':str(Path(value['directory'])/('perfmon-export.csv' if value['backend']=='perfmon' else 'history-index.json')),'enabled':True,'host':ask('Host ID matching observed job history',platform.node(),input_fn)}
            if value['backend']=='perfmon':
                source['timezone']=ask('PerfMon timestamp IANA time zone',value.get('timezone','UTC'),input_fn)
                perf['export_native']=boolean('Export this dedicated PerfMon recorder before reports',False,input_fn)
            else:source['format']='sysstat_json' if value['backend']=='sysstat' else 'normalized_json'
            perf['sources']=[s for s in perf.get('sources',[]) if s.get('id')!=source['id']]+[source]
            if boolean('Include this performance report in configured monitoring email digests',False,input_fn):
                monitor=config.setdefault('monitoring',{});report={'path':str(Path(perf['output'])/'performance.json'),'enabled':True,'max_age_hours':24}
                monitor['performance_reports']=[p for p in monitor.get('performance_reports',[]) if p.get('path')!=report['path']]+[report]
                print('Report linked. Enable monitoring and configure an SMTP digest channel separately.')
            path=ask('Observed-runs CSV path (blank for usage report only)',input_fn=input_fn)
            if path:perf['job_history']=[path]
        else:
            from configbackup_lib.reporting.performance_report import run
            if perf.get('export_native',False):export_owned(config,config_path)
            value=run(perf,perf.get('output',str(Path(config_path).resolve().parent/'performance-report')));print(value['status'])


def export_owned(config,config_path):
    anchor=state_path(config_path)
    saved=json.loads(anchor.read_text())
    if not saved.get('owned') or saved.get('system')!='Windows' or saved.get('config')!=str(Path(config_path).resolve()):raise ValueError('No owned PerfMon recorder')
    return export_perfmon(saved['settings'])


def main(argv=None):
    import yaml
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['enable','disable','status','export','record']);p.add_argument('--config');p.add_argument('--settings');p.add_argument('--apply-plan');p.add_argument('--once',action='store_true');a=p.parse_args(argv)
    if a.action=='record':record(json.loads(Path(a.settings).read_text()),a.once);return 0
    config=yaml.safe_load(Path(a.config).read_text())
    if a.action=='status':
        from configbackup_lib.engine.recorder_health import inspect
        result=inspect(config,a.config)
    elif a.action=='export':result={'export':str(export_owned(config,a.config))}
    elif a.apply_plan:result=apply(config,a.config,a.action,a.apply_plan)
    else:result=plan(config,a.config,a.action)
    print(json.dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
