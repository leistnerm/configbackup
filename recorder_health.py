"""Read-only recorder state and capture freshness; registration alone is not health."""
import datetime as dt
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time


def inspect(config,config_path,*,execute=subprocess.run,now=None,system=None):
    from native_recording import settings,state_path,history_records,prerequisites
    from performance_report import load_source,timestamp
    now=time.time() if now is None else now;system=system or platform.system()
    value=settings(config,config_path,system);anchor=state_path(config_path)
    result={'schema':1,'prerequisites':prerequisites(value),'registration':'absent','native_status':'unknown','capture_status':'unknown',
            'latest_sample':None,'sample_age_seconds':None,'coverage':[], 'warnings':[],
            'limits':'Read-only point-in-time check. Running native service is not proof of usable samples. Raw BLG modification time is not an exported sample timestamp.'}
    if not anchor.exists():result['status']='unregistered';return result
    if anchor.is_symlink() or anchor.stat().st_size>1024*1024:raise ValueError('Unsafe recorder registration')
    saved=json.loads(anchor.read_text());name=saved.get('name','')
    if saved.get('config')!=str(Path(config_path).resolve()) or saved.get('system')!=system or not re.fullmatch(r'ConfigBackup-Performance-[0-9a-f]{12}',name):raise ValueError('Recorder registration mismatch')
    result['registration']=saved.get('status','unknown');value=saved['settings'];root=Path(value['directory'])
    if any(p.is_symlink() for p in [root,*root.parents]):raise ValueError('Recorder directory symlink refused')
    if system=='Linux':command=['systemctl','--user','show',name+'.service','--property=ActiveState,SubState,MainPID,Result']
    elif system=='Darwin':command=['launchctl','print','gui/'+str(os.getuid())+'/'+name]
    else:command=['powershell.exe','-NoProfile','-NonInteractive','-Command',"$ErrorActionPreference='Stop'; $s=New-Object -ComObject Pla.DataCollectorSet; $s.Query('"+name+"',$null); Write-Output ([int]$s.Status)"]
    try:
        probe=execute(command,capture_output=True,text=True,timeout=15)
        if probe.returncode:result['warnings'].append('Native query failed; state is unknown')
        elif system=='Linux':
            fields=dict(line.split('=',1) for line in probe.stdout.splitlines() if '=' in line)
            result['native_status']='running' if fields.get('ActiveState')=='active' and fields.get('SubState')=='running' else 'stopped' if fields.get('ActiveState') in ('inactive','failed') else 'unknown'
        elif system=='Darwin':result['native_status']='running' if re.search(r'\bstate = running\b',probe.stdout) else 'stopped' if re.search(r'\bstate = (?:not running|waiting|exited)\b',probe.stdout) else 'unknown'
        else:result['native_status']={'1':'running','0':'stopped'}.get(probe.stdout.strip(),'unknown')
    except (OSError,subprocess.SubprocessError):result['warnings'].append('Native query unavailable')
    try:
        if value['backend']=='perfmon':
            source={'type':'perfmon_csv','path':str(root/'perfmon-export.csv'),'timezone':value.get('timezone','UTC')}
            raw=root/'current.blg'
            if raw.exists() and not raw.is_symlink():result['raw_capture_age_seconds']=round(now-raw.stat().st_mtime,3)
        else:
            records=history_records(root);indexed={n for r in records for n in r['files']}
            result['unindexed_capture_files']=sum(1 for p in root.glob('sample-*') if p.name not in indexed)
            if not records:raise ValueError('No completed chunks')
            source={'type':'sysstat_json' if value['backend']=='sysstat' else 'normalized_json','path':str(root/next(n for n in records[-1]['files'] if n.endswith('.json')))}
        diagnostics=[];rows=load_source(source,diagnostics=diagnostics)
        latest=max(timestamp(r['end']) for r in rows);age=now-latest.timestamp()
        result.update(latest_sample=latest.isoformat(),sample_age_seconds=round(age,3))
        result['coverage']=[{'host':host,'metrics':sorted({r['metric'] for r in rows if r['host']==host})} for host in sorted({r['host'] for r in rows})]
        threshold=value['interval_seconds']*value.get('chunk_samples',60)*2+60
        result['capture_status']='clock_skew' if age< -60 else 'stale' if age>threshold else 'recent'
        result['coverage_warnings']=diagnostics
        missing=set(value.get('expected_metrics',[]))-{r['metric'] for r in rows}
        if missing:result['warnings'].append('Missing expected metrics: '+', '.join(sorted(missing)))
        if diagnostics:result['warnings'].append('Some counters are unavailable')
    except (OSError,ValueError,KeyError,TypeError,StopIteration):result['warnings'].append('No readable completed sample evidence')
    result['status']='healthy' if result['native_status']=='running' and result['capture_status']=='recent' and not result['warnings'] else 'attention'
    if result['registration']=='disabled':result['status']='disabled' if result['native_status']=='stopped' else 'attention'
    return result
