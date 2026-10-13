#!/usr/bin/env python3
"""Optional offline review portal over certified captures and operational evidence.

Reads local files only. It never restores, edits configuration, or sends messages.
Failed sources retain their last report data with explicit stale/preserved status.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import difflib
import fnmatch
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from completeness import Coverage, MANIFEST, within
from sections import enabled
from secret_scan import scan_bytes

SECTIONS=('coverage','changes','explorer','schedules','history','notifications','recovery','disappeared','quality','exposure','attention')
SENSITIVE=re.compile(r'password|passwd|secret|token|credential|private.?key|connection.?string',re.I)
UTC=dt.timezone.utc


def stamp(value):
    if not value:return None
    if isinstance(value,(int,float)):return float(value)
    parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def validate(config):
    if not isinstance(config,dict):raise ValueError('reporting must be a mapping')
    def path_value(value,label,optional=False):
        if optional and value is None:return
        if not isinstance(value,str) or not value.strip() or '\x00' in value:raise ValueError(label+' must be a nonempty path string')
    def csv_keys(value,label):
        if not isinstance(value,dict):raise ValueError(label+' must be a mapping of file paths to identity column lists')
        for name,columns in value.items():
            path_value(name,label+' file')
            if not isinstance(columns,list) or not columns or any(not isinstance(column,str) or not column.strip() or '\x00' in column for column in columns) or len(set(columns))!=len(columns):raise ValueError(label+' columns must be a nonempty list of unique column names')
    for key in ('enabled','include_values','required'):
        if key in config and not isinstance(config[key],bool):raise ValueError('reporting.'+key+' must be boolean')
    from change_policy import validate as validate_policy
    validate_policy(config.get('change_policy',{}))
    zone=config.get('timezone','UTC')
    if not isinstance(zone,str) or not zone.strip():raise ValueError('reporting.timezone must be an IANA time zone name')
    try:ZoneInfo(zone)
    except (ValueError,ZoneInfoNotFoundError) as exc:raise ValueError('reporting.timezone must be an available IANA time zone name') from exc
    for key in ('output','state','monitoring_path','integrity_path'):
        if key in config:path_value(config[key],'reporting.'+key,optional=key!='output')
    if 'csv_keys' in config:csv_keys(config['csv_keys'],'reporting.csv_keys')
    for key in ('max_file_bytes','max_files','max_rows','history_days','history_points','max_report_bytes'):
        if key in config and (not isinstance(config[key],int) or isinstance(config[key],bool) or config[key]<1):raise ValueError('reporting.'+key+' must be a positive integer')
    for key in ('snapshots','schedule_paths','runtime_paths'):
        if not isinstance(config.get(key,[]),list):raise ValueError('reporting.'+key+' must be a list')
    ids=set()
    for spec in config.get('snapshots',[]):
        if not isinstance(spec,dict) or not isinstance(spec.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',spec['id']):raise ValueError('Each report snapshot needs a safe string id')
        path_value(spec.get('path'),'Snapshot path')
        if 'baseline' in spec:path_value(spec['baseline'],'Snapshot baseline',optional=True)
        if 'csv_keys' in spec:csv_keys(spec['csv_keys'],'Snapshot csv_keys')
        if spec['id'] in ids:raise ValueError('Duplicate report snapshot id')
        for key in ('enabled','include_values'):
            if key in spec and not isinstance(spec[key],bool):raise ValueError('Snapshot '+key+' must be boolean')
        ids.add(spec['id'])
    ids=set()
    for spec in config.get('runtime_paths',[]):
        if not isinstance(spec,dict) or not isinstance(spec.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',spec['id']):raise ValueError('Each runtime report source needs a safe string id')
        path_value(spec.get('path'),'Runtime path')
        if spec['id'] in ids:raise ValueError('Duplicate runtime report source id')
        ids.add(spec['id'])
        if 'enabled' in spec and not isinstance(spec['enabled'],bool):raise ValueError('Runtime enabled must be boolean')
    for spec in config.get('schedule_paths',[]):
        if not isinstance(spec,(str,dict)) or (isinstance(spec,dict) and not isinstance(spec.get('enabled',True),bool)):raise ValueError('Schedule report source needs a path and boolean enabled')
        path_value(spec.get('path') if isinstance(spec,dict) else spec,'Schedule path')
    if 'max_source_age_hours' in config and (not isinstance(config['max_source_age_hours'],(int,float)) or isinstance(config['max_source_age_hours'],bool) or not math.isfinite(config['max_source_age_hours']) or config['max_source_age_hours']<=0):raise ValueError('max_source_age_hours must be positive')
    sections=config.get('sections',{})
    if not isinstance(sections,dict) or set(sections)-set(SECTIONS):raise ValueError('Unknown reporting section')
    for name in SECTIONS:enabled(name,sections)
    return config


def read_json(path,limit=20_000_000):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>limit:raise ValueError('Unsafe or oversized input')
    return json.loads(path.read_text(encoding='utf-8-sig'))


def atomic_json(path,value):
    path=Path(path)
    if path.is_symlink():raise ValueError('Refusing symlink output')
    path.parent.mkdir(parents=True,exist_ok=True)
    text=json.dumps(value,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+'\n'
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,prefix='.'+path.name,delete=False) as stream:
        temporary=Path(stream.name);stream.write(text)
    try:
        os.chmod(temporary,0o600);temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


def protected(value,key=''):
    if SENSITIVE.search(str(key)):return '[REDACTED]'
    if isinstance(value,dict):return {k:protected(v,k) for k,v in sorted(value.items())}
    if isinstance(value,list):return [protected(v,key) for v in value]
    if isinstance(value,str) and scan_bytes(value.encode('utf-8'),'value'):return '[REDACTED]'
    return value


def flatten(value,path='$'):
    if isinstance(value,dict):
        if not value:return {path:{}}
        result={}
        for key,item in sorted(value.items()):result.update(flatten(item,path+'/'+str(key).replace('~','~0').replace('/','~1')))
        return result
    if isinstance(value,list):
        if not value:return {path:[]}
        result={}
        for index,item in enumerate(value):result.update(flatten(item,path+'/'+str(index)))
        return result
    return {path:value}


def shown(value,include):
    if value=='[REDACTED]' or (isinstance(value,str) and value.startswith('[value hidden; sha256 ')):return value
    if include or isinstance(value,(int,float,bool)) or value is None:return value
    return '[value hidden; sha256 '+hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()[:12]+']'


def file_record(path,name,scope,observed,config):
    from completeness import digest
    size=path.stat().st_size
    record={'path':name,'scope':scope,'bytes':size,'sha256':digest(path),'observed_at':observed,'preserved':False}
    limit=config.get('max_file_bytes',1_000_000)
    if size>limit:record['preview_status']='size_limit';return record
    data=path.read_bytes()
    include=config.get('include_values',False)
    try:
        text=data.decode('utf-8-sig')
        if '\x00' in text:raise UnicodeError('binary')
        suffix=path.suffix.lower()
        if suffix=='.json':value=json.loads(text)
        elif suffix=='.csv':
            value=list(csv.DictReader(io.StringIO(text)))
            keys=config.get('csv_keys',{}).get(name)
            if keys:
                if not all(all(k in row for k in keys) for row in value):raise ValueError('CSV identity columns missing')
                identities=[tuple(row[k] for k in keys) for row in value]
                if len(set(identities))!=len(identities):raise ValueError('CSV identity columns are not unique')
                value={json.dumps([row[k] for k in keys],ensure_ascii=False):row for row in value}
        else:
            record['format']='text';record['lines']=len(text.splitlines())
            record['preview_status']='secret_detected' if scan_bytes(data,name) else 'values_hidden' if not include else 'available'
            if record['preview_status']=='available':record['text']=text
            return record
        from exposure_review import evidence
        record['exposure_evidence']=protected(evidence(name,value))
        clean=protected(value);record['format']='structured'
        properties=flatten(clean)
        record['property_count']=len(properties)
        record['properties']={k:shown(v,include) for k,v in list(properties.items())[:config.get('max_rows',5000)]}
        record['preview_status']='truncated' if len(record['properties'])<len(properties) else 'available'
        return record
    except (UnicodeError,ValueError,csv.Error):record['preview_status']='unavailable';return record


def capture(spec,config,previous=None):
    previous=previous or {};root=Path(spec['path'])
    coverage=Coverage(root)
    if len(coverage.files)>config.get('max_files',20000):raise ValueError('Snapshot file limit exceeded')
    result={'id':spec['id'],'path':str(root),'observed_at':coverage.collected_at,'files':{},'coverage':[],'status':'complete'}
    oldfiles=previous.get('files',{})
    with tempfile.TemporaryDirectory(prefix='configbackup-review-') as temporary:
        sealed=coverage.seal(temporary)
        for scope in coverage.sections:
            name=scope['path'];ok=scope['status']=='complete'
            row={'source':spec['id'],'scope':name,'status':scope['status'],'observed_at':coverage.collected_at if ok else None,'files':len(scope.get('files',{})) if ok else None,'previous_preserved':not ok and any(within(p,name) for p in oldfiles)}
            # Collector error strings may contain arbitrary protected data. The
            # precise local collector log remains the place to investigate them.
            if not ok:row['reason']='Collection unavailable; inspect local collector diagnostics';result['status']='partial' if scope['status'] in ('failed','skipped') else result['status']
            result['coverage'].append(row)
            if ok:
                for path in sorted(scope['files']):result['files'][path]=file_record(sealed/path,path,name,coverage.collected_at,{**config,**spec})
            else:
                for path,record in oldfiles.items():
                    if within(path,name):result['files'][path]={**record,'preserved':True,'preservation_reason':scope['status']}
    declared={s['path'] for s in coverage.sections}
    for path,record in oldfiles.items():
        if not any(within(path,s) for s in declared):
            result['files'][path]={**record,'preserved':True,'preservation_reason':'scope_not_discovered'}
    for old in previous.get('coverage',[]):
        if old['scope'] not in declared:result['coverage'].append({**old,'status':'not_discovered','previous_preserved':True})
    return result


def compare(current,baseline,config):
    rows=[];before=baseline.get('files',{});after=current.get('files',{})
    for name in sorted(set(before)|set(after)):
        a,b=before.get(name),after.get(name)
        if b and b.get('preserved'):
            if a:rows.append({'source':current['id'],'path':name,'change':'unverified_preserved','reason':b.get('preservation_reason')})
            continue
        if a and b and a['sha256']==b['sha256']:continue
        status='added' if a is None else 'removed' if b is None else 'modified'
        if b is None and not any(s['status']=='complete' and within(name,s['scope']) for s in current['coverage']):status='unverified_missing'
        base={'source':current['id'],'path':name,'change':status,'before_sha256':a.get('sha256') if a else None,'after_sha256':b.get('sha256') if b else None}
        if a and b and 'properties' in a and 'properties' in b:
            keys=sorted(set(a['properties'])|set(b['properties']))
            differences=[{**base,'property':key,'before':a['properties'].get(key,'[absent]'),'after':b['properties'].get(key,'[absent]')} for key in keys if a['properties'].get(key,'[absent]')!=b['properties'].get(key,'[absent]')]
            rows.extend(differences or [{**base,'reason':'Representation/order changed or preview is incomplete; inspect native diff'}])
        elif a and b and 'text' in a and 'text' in b:
            rows.append({**base,'diff':'\n'.join(difflib.unified_diff(a['text'].splitlines(),b['text'].splitlines(),fromfile='baseline',tofile='current',n=3))})
        else:rows.append(base)
    return rows


def history_rows(directory,config,now):
    path=Path(directory)/'history.sqlite3'
    if path.is_symlink():raise ValueError('Symlink history database')
    if not path.is_file():raise FileNotFoundError('Monitoring history unavailable')
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        since=now-config.get('history_days',30)*86400;limit=config.get('history_points',10000)
        rows=connection.execute('SELECT series,observed,value,metric,labels,unit FROM samples WHERE observed>=? ORDER BY observed DESC,series LIMIT ?',(since,limit+1)).fetchall()
        result=[{**dict(row),'labels':json.loads(row['labels'])} for row in rows[:limit]]
        result.sort(key=lambda r:(r['observed'],r['series']))
        queue=[dict(row) for row in connection.execute('SELECT id,channel,status,attempts,next_attempt,error,created FROM outbox ORDER BY id DESC LIMIT ?',(config.get('max_rows',5000)+1,))]
        total=connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
        return result,queue,{'history_truncated':len(rows)>limit,'history_since':dt.datetime.fromtimestamp(since,UTC).isoformat(),'notification_rows':total}
    finally:connection.close()


def schedule_rows(directory,config):
    base=Path(directory);summary=read_json(base/'summary.json')
    attempt=read_json(base/'generation-status.json') if (base/'generation-status.json').is_file() else {}
    if attempt.get('status')=='failed':raise ValueError('Schedule generation failed; prior report retained')
    status=read_json(base/'report-status.json') if (base/'report-status.json').is_file() else {}
    disabled=status.get('disabled',[]);result=[];counts={}
    for name in ('timeline','jobs','execution-analysis','runtime-trends','concurrency','observed-concurrency','observed-runs','overlaps-p95','resource-conflicts','resource-capacity','dependencies','deadlines','watchdogs','what-if','coverage','warnings'):
        if name in disabled:continue
        path=base/(name+'.csv')
        if not path.is_file():continue
        if path.is_symlink() or path.stat().st_size>config.get('max_report_bytes',50_000_000):raise ValueError('Unsafe or oversized schedule CSV')
        with path.open(encoding='utf-8-sig',newline='') as stream:
            count=0
            for row in csv.DictReader(stream):
                count+=1
                if count<=config.get('max_rows',5000):result.append({'report':name,'input_source':str(directory),**protected(row)})
            counts[name]={'total':count,'shown':min(count,config.get('max_rows',5000))}
    return result,{'summary':summary,'counts':counts,'disabled':disabled}


def recovery_rows(state,config):
    rows=[];preview=[];disappeared=[]
    evidence=state.get('recovery',{})
    for task,item in sorted(state.get('tasks',{}).items()):
        for logical,record in sorted(item.get('files',{}).items()):
            versions=record.get('versions',[])
            rows.append({'task':task,'path':logical,'active':record.get('active',True),'retained_versions':len(versions),'git_only':bool(record.get('git_hash') and not versions),'last_verify':evidence.get('last_verify'),'last_restore':evidence.get('last_restore'),'evidence_scope':'archive-level evidence; not proof of live database/service recovery'})
            if not record.get('active',True) or record.get('missing_count',0):disappeared.append({'task':task,'path':logical,'active':record.get('active',True),'missing_count':record.get('missing_count',0)})
    try:
        from recovery import plan_restore
        planned=plan_restore(state,config.get('restore_as_of'))
        if isinstance(planned,dict):preview=planned.get('records',planned.get('files',[]))+[{'status':'unavailable','error':e} for e in planned.get('errors',[])]
        elif isinstance(planned,list):preview=planned
    except ImportError:
        for task,item in sorted(state.get('tasks',{}).items()):
            for logical,record in sorted(item.get('files',{}).items()):
                if record.get('active',True) and record.get('versions'):
                    version=max(enumerate(record['versions']),key=lambda pair:(stamp(pair[1]['created']),pair[0]))[1]
                    preview.append({'task':task,'path':logical,'version':version,'status':'latest active version inventory; no restore validation'})
    return rows,preview,disappeared


def hide_cached_values(value,config):
    if config.get('include_values',False):return value
    for record in value.get('files',{}).values():
        record.pop('text',None)
        if 'properties' in record:record['properties']={k:shown(v,False) for k,v in record['properties'].items()}
    for change in value.get('changes',[]):
        change.pop('diff',None)
        for key in ('before','after'):
            if key in change:change[key]=shown(change[key],False)
    return value


def _read_cached(path):
    try:return read_json(path)
    except (OSError,ValueError):return {}


def run_reports(config,directory,state=None,changes=None):
    validate(config)
    if config.get('enabled',True) is False:return {'status':'disabled','section_status':{},'source_status':[]}
    output=Path(directory)
    if output.is_symlink():raise ValueError('Report output cannot be a symlink')
    output.mkdir(parents=True,exist_ok=True)
    cache=output/'.report-cache'
    if cache.is_symlink():raise ValueError('Report cache cannot be a symlink')
    cache.mkdir(exist_ok=True)
    now=stamp(config.get('as_of')) if config.get('as_of') else dt.datetime.now(UTC).timestamp()
    data={'schema_version':1,'version':'1.0.0','observed_at':dt.datetime.fromtimestamp(now,UTC).isoformat(),'timezone':config.get('timezone','UTC'),'source_status':[],'section_status':{},'limits':{},**{name:[] for name in SECTIONS}}
    exposure_sources={}
    for spec in config.get('snapshots',[]):
        identity=spec['id'];path=cache/('snapshot-'+identity+'.json');old=hide_cached_values(_read_cached(path),{**config,**spec})
        if spec.get('enabled',True) is False:
            data['source_status'].append({'source':identity,'status':'disabled','previous_preserved':bool(old)});continue
        baseline=old
        try:
            current=capture(spec,config,old)
            baseline_spec={**spec,'path':spec['baseline']} if spec.get('baseline') else None
            baseline=capture(baseline_spec,config) if baseline_spec else old
            comparison=compare(current,baseline,config) if baseline else []
            current['changes']=comparison;current['baseline']=spec.get('baseline','previous successful report capture')
            atomic_json(path,current)
        except Exception as exc:
            current=old
            data['source_status'].append({'source':identity,'status':'failed','error':type(exc).__name__,'previous_preserved':bool(old)})
        else:data['source_status'].append({'source':identity,'status':current['status'],'observed_at':current.get('observed_at'),'baseline':current['baseline']})
        if not current:continue
        data['coverage']+=current.get('coverage',[])
        data['changes']+=current.get('changes',[])
        exposure_sources[identity]={'configured':[entry for record in current.get('files',{}).values() for entry in record.get('exposure_evidence',[])],'observed':[]}
        for record in current.get('files',{}).values():
            record={k:v for k,v in record.items() if k!='exposure_evidence'}
            data['explorer'].append({'source':identity,**record})
            if record.get('preserved'):data['disappeared'].append({'source':identity,**{k:record[k] for k in ('path','scope','observed_at','preservation_reason') if k in record}})
        sizes=[r['bytes'] for r in current.get('files',{}).values()]
        data['quality'].append({'source':identity,'files':len(sizes),'bytes':sum(sizes),'certified_scopes':sum(s['status']=='complete' for s in current['coverage']),'unavailable_scopes':sum(s['status'] not in ('complete','not_applicable','disabled') for s in current['coverage']),'content_previews_enabled':config.get('include_values',False),'capture_duration':'unavailable unless collector records it','determinism':'unchanged hashes' if baseline and not current.get('changes') else 'changes observed; compare evidence' if baseline else 'no baseline'})
    if enabled('exposure',config.get('sections',{})):
        from exposure_review import runtime,correlate
        for spec in config.get('runtime_paths',[]):
            if spec.get('enabled',True) is False:continue
            identity=spec['id'];cached=cache/('runtime-'+hashlib.sha256(identity.encode()).hexdigest()[:16]+'.json')
            try:
                payload=read_json(spec['path'],config.get('max_report_bytes',50_000_000))
                rows=protected(runtime(payload));current={'rows':rows,'observed_at':payload.get('observed_at')};atomic_json(cached,current)
                data['source_status'].append({'source':'runtime:'+identity,'status':'partial' if payload.get('failures') else 'complete','observed_at':payload.get('observed_at'),'unavailable_datasets':len(payload.get('failures',[]))})
            except Exception as exc:
                current=_read_cached(cached);data['source_status'].append({'source':'runtime:'+identity,'status':'failed','error':type(exc).__name__,'previous_preserved':bool(current)})
            exposure_sources.setdefault(identity,{'configured':[],'observed':[]})['observed']+=current.get('rows',[])
        for identity,facts in sorted(exposure_sources.items()):data['exposure']+=correlate(identity,facts['configured'],facts['observed'],now)
    state_path=config.get('state');state_failed=False;state_cache=cache/'archive-state.json'
    if state is None and state_path:
        try:state=read_json(state_path)
        except Exception as exc:
            state_failed=True;data['source_status'].append({'source':'state','status':'failed','error':type(exc).__name__,'previous_preserved':state_cache.is_file()})
    state_evidence={}
    if state is not None:
        try:
            recovery,preview,retired=recovery_rows(state,config);coverage=[]
            for task,item in sorted(state.get('tasks',{}).items()):
                for name,scope in sorted(item.get('scopes',{}).items()):coverage.append({'source':'archive-state','task':task,'scope':name,'status':scope.get('status','unknown'),'last_success':scope.get('last_success'),'observed_at':scope.get('source_collected_at'),'failures':scope.get('consecutive_failures',0)})
            state_evidence={'recovery':recovery,'restore_preview':preview,'disappeared':retired,'coverage':coverage,'recovery_evidence':state.get('recovery',{})}
            from recovery_status import rows as scope_recovery
            evidence={}
            integrity_path=config.get('integrity_path')
            if integrity_path and Path(integrity_path).exists():evidence=read_json(integrity_path,64*1024*1024)
            state_evidence['recovery']=recovery+scope_recovery(state,evidence)
            atomic_json(state_cache,state_evidence)
            data['source_status'].append({'source':'state','status':'complete'})
        except Exception as exc:
            state_failed=True;data['source_status'].append({'source':'state','status':'failed','error':type(exc).__name__,'previous_preserved':state_cache.is_file()})
    if state_failed:state_evidence=_read_cached(state_cache)
    if state_evidence:
        data['recovery']=state_evidence.get('recovery',[]);data['restore_preview']=state_evidence.get('restore_preview',[])
        data['disappeared']+=state_evidence.get('disappeared',[]);data['coverage']+=state_evidence.get('coverage',[])
        data['recovery_evidence']=state_evidence.get('recovery_evidence',{})
    if changes:data['changes']+=protected(changes)
    for raw in config.get('schedule_paths',[]):
        spec={'path':raw} if isinstance(raw,str) else raw
        key='schedule-'+hashlib.sha256(str(spec['path']).encode()).hexdigest()[:16];cached=cache/(key+'.json')
        if spec.get('enabled',True) is False:data['source_status'].append({'source':key,'status':'disabled'});continue
        try:
            rows,metadata=schedule_rows(spec['path'],config);current={'rows':rows,'metadata':metadata};atomic_json(cached,current)
            data['source_status'].append({'source':key,'status':'complete','observed_at':metadata['summary'].get('analysis_as_of'),'path':spec['path']})
        except Exception as exc:
            current=_read_cached(cached);data['source_status'].append({'source':key,'status':'failed','error':type(exc).__name__,'previous_preserved':bool(current)})
        data['schedules']+=current.get('rows',[]);data['limits'][key]=current.get('metadata',{})
    if config.get('monitoring_path'):
        key='monitoring';cached=cache/'monitoring.json'
        try:
            history,queue,limits=history_rows(config['monitoring_path'],config,now)
            summary=read_json(Path(config['monitoring_path'])/'summary.json')
            current={'history':history,'notifications':queue,'limits':limits,'summary':summary};atomic_json(cached,current)
            data['source_status'].append({'source':key,'status':'complete','observed_at':summary.get('observed_at')})
        except Exception as exc:
            current=_read_cached(cached);data['source_status'].append({'source':key,'status':'failed','error':type(exc).__name__,'previous_preserved':bool(current)})
        data['history']=current.get('history',[]);data['notifications']=current.get('notifications',[]);data['limits'][key]=current.get('limits',{})
        summary=current.get('summary',{})
        data['capacity']=summary.get('capacity',[]);data['alerts']=summary.get('alerts',[]);data['notification_errors']=summary.get('delivery_errors',[])
        data['collection_errors']=summary.get('collection_errors',[])
    if enabled('attention',config.get('sections',{})):
        from change_policy import evaluate
        data['attention']=evaluate(data['changes'],config.get('change_policy'),dt.datetime.fromtimestamp(now,UTC))
    if state:
        for identity,record in sorted(state.get('tasks',{}).items()):
            for scope,item in sorted(record.get('scopes',{}).items()):
                if item.get('cost'):data['quality'].append({'task':record.get('display_name',identity),'scope':scope,**item['cost']})
            history=record.get('cost_history',[])
            if history:data['quality'].append({'task':record.get('display_name',identity),'observations':len(history),'failure_count':sum(r['status'] in ('failed','partial') for r in history),'latest_duration_seconds':history[-1]['duration_seconds']})
    for name in SECTIONS:
        if not enabled(name,config.get('sections',{})):
            data[name]=[];data['section_status'][name]='disabled'
        else:data['section_status'][name]='available' if data[name] else 'unavailable_or_empty'
    # Keep related auxiliary data behind the same section switch.
    if data['section_status']['recovery']=='disabled':data.pop('restore_preview',None);data.pop('recovery_evidence',None)
    if data['section_status']['history']=='disabled':data.pop('capacity',None)
    if data['section_status']['notifications']=='disabled':data.pop('notification_errors',None)
    for row in data['coverage']:
        source_time=row.get('observed_at') or row.get('last_success')
        try:row['age_hours']=round((now-stamp(source_time))/3600,3) if source_time else None
        except (ValueError,TypeError):row['age_hours']=None
    for source in data['source_status']:
        observed=source.get('observed_at')
        if observed and source['status']=='complete':
            try:
                source['age_hours']=round((now-stamp(observed))/3600,3)
                if source['age_hours']>config.get('max_source_age_hours',24):source['freshness']='stale'
                elif source['age_hours']<-.0834:source['freshness']='future_timestamp'
                else:source['freshness']='fresh'
            except (ValueError,TypeError):source['freshness']='unknown'
        elif source['status']=='complete':source['freshness']='unknown'
    data['status']='partial' if any(r['status'] in ('partial','failed') for r in data['source_status']) else 'complete' if data['source_status'] else 'unknown'
    data['limits']['section_rows']={}
    for name in SECTIONS:
        total=len(data[name]);data[name]=data[name][:config.get('max_rows',5000)]
        data['limits']['section_rows'][name]={'total':total,'shown':len(data[name])}
    data['limits']['auxiliary_rows']={}
    for name in ('restore_preview','capacity','notification_errors','collection_errors'):
        if name in data:
            total=len(data[name]);data[name]=data[name][:config.get('max_rows',5000)]
            data['limits']['auxiliary_rows'][name]={'total':total,'shown':len(data[name])}
    encoded=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
    if len(encoded)>config.get('max_report_bytes',50_000_000):raise ValueError('Report exceeds size limit; prior portal preserved; reduce rows or content previews')
    atomic_json(output/'report.json',data)
    render_portal(output/'dashboard.html',data)
    return {'status':data['status'],'section_status':data['section_status'],'source_status':data['source_status'],'path':str(output/'dashboard.html')}


def render_portal(path,data):
    payload=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('&','\\u0026')
    page=HTML.replace('PAYLOAD',payload)
    path=Path(path)
    if path.is_symlink():raise ValueError('Refusing symlink report')
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as stream:temporary=Path(stream.name);stream.write(page)
    try:os.chmod(temporary,0o600);temporary.replace(path)
    finally:temporary.unlink(missing_ok=True)


HTML=r'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ConfigBackup review portal</title>
<style>body{font:15px/1.5 system-ui;margin:0;color:#183046;background:#f2f5f8}main{max-width:1450px;margin:auto;padding:28px}h1{margin:0}h2{font-size:20px}.muted{color:#52697a}.bar,.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}.card{background:white;border:1px solid #cedae4;border-radius:8px;padding:14px;min-width:140px}.card strong{display:block;font-size:24px}input,select,button{font:inherit;padding:8px;max-width:100%}table{border-collapse:collapse;background:white;width:100%;font-size:13px}th,td{border:1px solid #dae2e9;padding:8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#e5eef5;cursor:pointer}section{overflow:auto;margin:20px 0}.notice{border-left:4px solid #a66405;padding:12px;background:#fff7e9}pre{white-space:pre-wrap;max-height:350px;overflow:auto}svg{background:white;max-width:100%;border:1px solid #dae2e9}.legend{font-size:13px}.timeline{overflow:auto;max-height:550px}button{cursor:pointer}summary{cursor:pointer}</style>
<main><p class="muted">CONFIGBACKUP 1.0.0 · LOCAL REVIEW</p><h1>Configuration and operations evidence</h1><p id="asof" class="muted"></p><div id="cards" class="cards"></div>
<p class="notice">Offline snapshot. Preserved data may be older than the current attempt. Empty, disabled and inaccessible results are different. Forecasts and P95 scenarios are estimates; this report performs no configuration changes, maintenance or restores. Content previews are hidden by default.</p>
<details><summary>Source status and report limits</summary><div id="sources"></div><pre id="limits"></pre></details>
<div class="bar"><label>View <select id="section"></select></label><label>Search <input id="search" placeholder="Host, database, component, path or rule"></label><label>From <input type="date" id="from"></label><label>To <input type="date" id="to"></label><label>Time zone <input id="zone" size="23"></label><button id="download">Export visible JSON</button></div>
<p id="explanation" class="muted"></p><div id="controls" class="bar"></div><section id="chart"></section><section id="table"></section><p id="count" class="muted"></p>
<details><summary>Recovery preview / capacity / delivery diagnostics</summary><pre id="aux"></pre></details></main>
<script id="data" type="application/json">PAYLOAD</script><script>
'use strict';const d=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id),str=v=>typeof v==='object'?JSON.stringify(v):String(v??'');let visible=[],sortKey='',sortAsc=true;
const titles={attention:'Changes needing attention',coverage:'Coverage & freshness',changes:'Before / after changes',explorer:'Configuration explorer',schedules:'Schedule workbench',history:'Metric history & capacity',notifications:'Notification delivery',recovery:'Recovery readiness',disappeared:'Missing / preserved objects',quality:'Collection quality',exposure:'Endpoint exposure evidence'};
const explanations={attention:'Explicit review rules over observed changes. Approved and maintenance changes remain visible; matching a filename does not prove a security regression.',coverage:'Certified collector scopes and archive freshness. A healthy sibling does not certify a failed scope.',changes:'Compare the selected baseline or previous successful report capture. Values are hidden unless explicitly enabled; redacted values cannot be compared meaningfully.',explorer:'Search certified files and structured properties. Ordered arrays stay ordered. Binary, oversized and secret-bearing text has no preview.',schedules:'Choose a CSV-derived view. Timeline and heatmap use predicted starts; execution-analysis shows actual matches. P95 and what-if describe scenarios, not probabilities.',history:'Stored observations, not interpolated measurements. Select a series to graph. Input limits and retained period appear above.',notifications:'Queue metadata only: message bodies, tokens and endpoint credentials are omitted. This page does not retry or send messages.',recovery:'Retained versions and archive-level verification evidence. This is not proof of successful live database or service recovery; preview never restores files.',disappeared:'Failed, disabled or undiscovered objects kept for review. Nothing is retired or deleted here.',quality:'File sizes, certified scope counts and comparison evidence. Identical hashes indicate unchanged captured bytes, not that unsupported settings were captured.',exposure:'Correlated local listener, process, service, container, web binding and certificate evidence. A matching port is not proof of service ownership or remote reachability. Firewall, routing, certificate trust/name and served TLS identity are not evaluated.'};
$('asof').textContent=(d.demo_notice?d.demo_notice+' · ':'')+'Report as of '+d.observed_at+' · '+d.timezone;$('zone').value=d.timezone;
for(const [title,value] of [['Report status',d.status],['Sources',d.source_status.length],['Files shown',(d.explorer||[]).length],['Changes shown',(d.changes||[]).length]]){const card=document.createElement('div');card.className='card';const strong=document.createElement('strong');strong.textContent=value;card.append(strong,document.createTextNode(title));$('cards').append(card)}
function table(rows,area){area.replaceChildren();if(!rows.length){area.textContent='No records in this view. Consult source coverage before interpreting an empty result.';return}const keys=[...new Set(rows.flatMap(Object.keys))],t=document.createElement('table'),head=t.createTHead().insertRow();for(const k of keys){const th=document.createElement('th');th.textContent=k;th.onclick=()=>{sortAsc=sortKey===k?!sortAsc:true;sortKey=k;draw(false)};head.append(th)}const body=t.createTBody();for(const row of rows.slice(0,500)){const tr=body.insertRow();for(const key of keys){const td=tr.insertCell(),value=row[key];if(typeof value==='object'||str(value).length>300){const detail=document.createElement('details'),label=document.createElement('summary'),pre=document.createElement('pre');label.textContent=typeof value==='object'?'View structured data':'View details';pre.textContent=typeof value==='object'?JSON.stringify(value,null,2):str(value);detail.append(label,pre);td.append(detail)}else td.textContent=str(value)}}area.append(t)}
table(d.source_status,$('sources'));$('limits').textContent=JSON.stringify(d.limits,null,2);$('aux').textContent=JSON.stringify({restore_preview:d.restore_preview,recovery_evidence:d.recovery_evidence,capacity:d.capacity,delivery_errors:d.notification_errors,collection_errors:d.collection_errors},null,2);
for(const [key,title] of Object.entries(titles)){if(d.section_status[key]==='disabled')continue;const o=document.createElement('option');o.value=key;o.textContent=title;$('section').append(o)}
function date(row){const value=row.observed??row.start??row.scheduled_start??row.actual_start??row.observed_at??row.created;return typeof value==='number'?new Date(value*1000):value?new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value)?value:value+'Z'):null}
function formatted(value){try{return new Intl.DateTimeFormat('en-CA',{timeZone:$('zone').value,dateStyle:'short',timeStyle:'short'}).format(value)}catch{return value.toISOString()}}
function select(id,values,label){const l=document.createElement('label');l.textContent=label+' ';const s=document.createElement('select');s.id=id;for(const value of values){const o=document.createElement('option');o.value=typeof value==='object'?value.value:value;o.textContent=typeof value==='object'?value.label:value;s.append(o)}l.append(s);s.onchange=()=>draw(false);$('controls').append(l)}
const ns='http://www.w3.org/2000/svg';function svg(width,height){const s=document.createElementNS(ns,'svg');s.setAttribute('viewBox',`0 0 ${width} ${height}`);s.setAttribute('width',width);s.setAttribute('height',height);s.setAttribute('role','img');return s}function element(s,kind,attrs,text){const e=document.createElementNS(ns,kind);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;s.append(e);return e}
function graph(rows){$('chart').replaceChildren();const name=$('section').value;if(name==='history'){const selected=$('series')?.value,points=rows.filter(r=>r.series===selected&&Number.isFinite(r.value)).slice(-10000);if(!points.length)return;const s=svg(1000,260),xs=points.map(r=>r.observed),ys=points.map(r=>r.value),lo=Math.min(...ys),hi=Math.max(...ys),a=Math.min(...xs),b=Math.max(...xs);element(s,'text',{x:15,y:22},points[0].metric+' ['+points[0].unit+'] · '+points.length+' plotted observations (maximum 10,000)');element(s,'polyline',{points:points.map(r=>`${60+900*(r.observed-a)/(b-a||1)},${220-165*(r.value-lo)/(hi-lo||1)}`).join(' '),fill:'none',stroke:'#145b8c','stroke-width':2});element(s,'text',{x:5,y:55},hi.toPrecision(5));element(s,'text',{x:5,y:223},lo.toPrecision(5));element(s,'text',{x:60,y:245},formatted(new Date(a*1000)));element(s,'text',{x:680,y:245},formatted(new Date(b*1000)));$('chart').append(s);return}
if(name!=='schedules')return;const chosen=$('scheduleKind')?.value;if(chosen==='timeline'){
const valid=rows.filter(r=>r.report==='timeline'&&date(r)&&!isNaN(date(r))).slice(0,200).map(r=>{const start=date(r).getTime(),job=(d.schedules||[]).find(j=>j.report==='jobs'&&j.job_id===r.job_id&&j.input_source===r.input_source),fact=(d.schedules||[]).find(j=>j.report==='execution-analysis'&&j.job_id===r.job_id&&j.scheduled_start===r.start&&j.input_source===r.input_source),actual=fact?.actual_start?date({actual_start:fact.actual_start}).getTime():null,median=r.duration_minutes===''?null:Number(r.duration_minutes),p95=r.duration_basis==='override'?median:job?.p95_minutes?Number(job.p95_minutes):null;return {...r,startMs:start,medianEnd:start+(median||0)*60000,p95End:p95===null?null:start+p95*60000,actualMs:actual,samples:job?.measured_runs??'unknown'}});
if(!valid.length)return;const min=Math.min(...valid.map(r=>Math.min(r.startMs,r.actualMs??r.startMs))),max=Math.max(...valid.map(r=>Math.max(r.medianEnd,r.p95End??r.startMs,r.actualMs??r.startMs)))+1,s=svg(1200,75+valid.length*22),x=t=>300+880*(t-min)/(max-min);
element(s,'text',{x:300,y:18},'Median blue · P95 scenario amber · matched actual start green · unknown duration point · at most 200 rows');element(s,'text',{x:300,y:38,'font-size':11},formatted(new Date(min)));element(s,'text',{x:960,y:38,'font-size':11},formatted(new Date(max)));
valid.forEach((r,i)=>{const y=55+i*22;element(s,'text',{x:5,y:y+12,'font-size':11},r.job_id.slice(0,43));if(r.p95End!==null)element(s,'rect',{x:x(r.startMs),y:y+2,width:Math.max(3,x(r.p95End)-x(r.startMs)),height:11,fill:'#edbd71'});const rect=element(s,'rect',{x:x(r.startMs),y,width:Math.max(3,x(r.medianEnd)-x(r.startMs)),height:15,fill:r.end_estimated?'#256d94':'#bd751b'});element(rect,'title',{},r.job_id+' '+formatted(new Date(r.startMs))+' duration '+(r.duration_minutes||'unknown')+' minutes; historical samples '+r.samples);if(r.actualMs!==null){const tick=element(s,'line',{x1:x(r.actualMs),x2:x(r.actualMs),y1:y-3,y2:y+18,stroke:'#11804c','stroke-width':3});element(tick,'title',{},'Matched actual start '+formatted(new Date(r.actualMs)))}});$('chart').className='timeline';$('chart').append(s)}

if(chosen==='heatmap'){const counts=new Map();for(const r of rows){const when=date(r);if(!when)continue;let parts;try{parts=new Intl.DateTimeFormat('en-CA',{timeZone:$('zone').value,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',hourCycle:'h23'}).formatToParts(when)}catch{continue}const p=Object.fromEntries(parts.map(x=>[x.type,x.value])),day=p.year+'-'+p.month+'-'+p.day,key=day+'|'+p.hour;counts.set(key,(counts.get(key)||0)+1)}const days=[...new Set([...counts.keys()].map(k=>k.split('|')[0]))].sort().slice(0,62),peak=Math.max(1,...counts.values()),s=svg(1100,45+days.length*23);element(s,'text',{x:5,y:15},'Projected starts by local day/hour · count only, not measured resource load');for(let h=0;h<24;h++)element(s,'text',{x:120+h*38,y:35,'font-size':11},h);days.forEach((day,i)=>{element(s,'text',{x:5,y:53+i*23,'font-size':11},day);for(let h=0;h<24;h++){const count=counts.get(day+'|'+String(h).padStart(2,'0'))||0,rect=element(s,'rect',{x:112+h*38,y:40+i*23,width:36,height:21,fill:`rgba(20,92,141,${.06+.94*count/peak})`});element(rect,'title',{},day+' '+h+':00 — '+count+' starts')}});$('chart').append(s)}}
function draw(reset=true){const name=$('section').value,all=d[name]||[];$('explanation').textContent=explanations[name];if(reset){$('controls').replaceChildren();sortKey='';if(name==='schedules')select('scheduleKind',['timeline','heatmap',...new Set(all.map(r=>r.report).filter(r=>r!=='timeline'))],'Schedule view');if(name==='history')select('series',[...new Map(all.map(r=>[r.series,{value:r.series,label:r.metric+' '+JSON.stringify(r.labels)}])).values()],'Series')}
const query=$('search').value.toLowerCase(),from=$('from').value,to=$('to').value;visible=all.filter(r=>{if(!JSON.stringify(r).toLowerCase().includes(query))return false;const instant=date(r);let day=null;if(instant&&!isNaN(instant)){try{const p=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:$('zone').value,year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(instant).map(x=>[x.type,x.value]));day=p.year+'-'+p.month+'-'+p.day}catch{day=instant.toISOString().slice(0,10)}}if(day&&((from&&day<from)||(to&&day>to)))return false;if(name==='schedules'){const kind=$('scheduleKind')?.value;return r.report===(kind==='heatmap'?'timeline':kind)}if(name==='history')return r.series===$('series')?.value;return true});if(sortKey)visible.sort((a,b)=>str(a[sortKey]).localeCompare(str(b[sortKey]),undefined,{numeric:true})*(sortAsc?1:-1));table(visible,$('table'));graph(visible);$('count').textContent=`Showing ${Math.min(500,visible.length)} of ${visible.length} filtered rows. Export contains all filtered rows retained in this report; input caps are listed above.`}
$('section').onchange=()=>draw();for(const id of ['search','from','to','zone'])$(id).oninput=()=>draw(false);$('download').onclick=()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(visible,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=u;a.download='configbackup-'+$('section').value+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)};draw();
</script></html>'''


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    import yaml
    config=yaml.safe_load(Path(args.config).read_text(encoding='utf-8-sig')) or {}
    result=run_reports(config.get('reporting',config),args.output)
    print(json.dumps(result,sort_keys=True));return 4 if result['status']=='partial' else 0

if __name__=='__main__':raise SystemExit(main())
