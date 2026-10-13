"""Offline native performance-log review and observed job overlap correlation."""
from __future__ import annotations
import argparse
import bisect
import csv
import datetime as dt
import fnmatch
import html
import io
import json
import math
from pathlib import Path
import re
import statistics
from zoneinfo import ZoneInfo
from reporting import atomic_json

UTC=dt.timezone.utc
# Suffix -> metric, unit, scale. Explicit mappings support localized counters.
COUNTERS={
    r'processor(_total)\% processor time':('cpu.busy_percent','percent',1),
    r'memory\% committed bytes in use':('memory.commit_percent','percent',1),
    r'memory\available mbytes':('memory.available_mb','MiB',1),
    r'memory\pages/sec':('memory.pages_per_sec','pages/s',1),
    r'system\processor queue length':('cpu.queue','tasks',1),
    'avg. disk sec/read':('disk.read_latency_ms','ms',1000),
    'avg. disk sec/write':('disk.write_latency_ms','ms',1000),
    'avg. disk queue length':('disk.queue','requests',1),
    '% disk time':('disk.busy_percent','percent',1),
    'disk read bytes/sec':('disk.read_bytes_per_sec','bytes/s',1),
    'disk write bytes/sec':('disk.write_bytes_per_sec','bytes/s',1)}
DEFAULT_THRESHOLDS={'cpu.busy_percent':90,'cpu.iowait_percent':20,'memory.used_percent':95,'memory.commit_percent':95,
                    'disk.latency_ms':20,'disk.read_latency_ms':20,'disk.write_latency_ms':20,'disk.queue':2}


def timestamp(value,zone=None):
    text=str(value).strip();parsed=None
    try:parsed=dt.datetime.fromisoformat(text.replace('Z','+00:00'))
    except ValueError:
        for fmt in ('%m/%d/%Y %H:%M:%S.%f','%m/%d/%Y %H:%M:%S'):
            try:parsed=dt.datetime.strptime(text,fmt);break
            except ValueError:pass
    if parsed is None:raise ValueError('Invalid performance timestamp')
    if parsed.tzinfo is None:
        if not zone:raise ValueError('Naive timestamps require an explicit IANA time zone')
        z=ZoneInfo(zone);first=parsed.replace(tzinfo=z,fold=0);second=parsed.replace(tzinfo=z,fold=1)
        if first.utcoffset()!=second.utcoffset() or first.astimezone(UTC).astimezone(z).replace(tzinfo=None)!=parsed:raise ValueError('Ambiguous/nonexistent local time; export UTC instead')
        parsed=first
    return parsed.astimezone(UTC)


def number(value):
    result=float(value)
    if not math.isfinite(result) or result<0:raise ValueError('Metrics must be finite and nonnegative')
    return result


def sample(host,entity,metric,unit,start,end,value):
    if any(not isinstance(v,str) or not v or len(v)>2048 for v in (host,metric,unit)) or end<=start:raise ValueError('Invalid host or sample interval')
    return {'host':host,'entity':str(entity),'metric':metric,'unit':unit,'start':start.isoformat(),'end':end.isoformat(),'value':number(value)}


def perfmon(text,spec,diagnostics=None):
    diagnostics=diagnostics if diagnostics is not None else []
    missing={}
    reader=csv.reader(io.StringIO(text));headers=next(reader,None);rows=[];previous=None;mappings={}
    if not headers:raise ValueError('Empty PerfMon input')
    for i,name in enumerate(headers[1:],1):
        custom=spec.get('counters',{}).get(name)
        if custom:mappings[i]=(custom['metric'],custom['unit'],float(custom.get('scale',1)),custom.get('entity','host'));continue
        lower=name.casefold();parts=lower.split('\\');suffix='\\'.join(parts[-2:])
        if suffix in COUNTERS:mappings[i]=(*COUNTERS[suffix],'host')
        elif len(parts)>=2 and parts[-2].startswith('physicaldisk(') and parts[-1] in COUNTERS:mappings[i]=(*COUNTERS[parts[-1]],name.split('\\')[-2])
    if not mappings:raise ValueError('No supported counters; supply explicit localized counter mappings')
    for values in reader:
        if not values:continue
        end=timestamp(values[0],spec.get('timezone'))
        if previous is not None:
            if end<=previous:raise ValueError('PerfMon timestamps must increase')
            for i,(metric,unit,scale,entity) in mappings.items():
                if i>=len(values):raise ValueError('Truncated PerfMon row')
                if values[i].strip() in ('','-1'):
                    missing.setdefault(i,[]).append((previous.isoformat(),end.isoformat()))
                    continue # Preserve an explicit unavailable-counter record below.
                rows.append(sample(spec['host'],entity,metric,unit,previous,end,number(values[i])*scale))
        previous=end
    for i,intervals in sorted(missing.items()):
        diagnostics.append({'kind':'unavailable_counter','counter':headers[i],'metric':mappings[i][0],
            'missing_intervals':len(intervals),'first_start':intervals[0][0],'last_end':intervals[-1][1]})
    for name in spec.get('expected_counters',[]):
        if name not in headers:diagnostics.append({'kind':'missing_counter','counter':name})
    return rows


def sysstat(value,spec):
    rows=[]
    if not isinstance(value,dict) or not isinstance(value.get('sysstat'),dict) or not isinstance(value['sysstat'].get('hosts'),list):raise ValueError('Invalid sysstat hosts')
    for host in value['sysstat']['hosts']:
        if not isinstance(host,dict) or not isinstance(host.get('statistics'),list):raise ValueError('Invalid sysstat host')
        identity=spec.get('host') or host['nodename'];previous=None
        for event in host['statistics']:
            if not isinstance(event,dict) or not isinstance(event.get('timestamp'),dict):raise ValueError('Invalid sysstat event')
            for name in ('cpu-load','disk'):
                if not isinstance(event.get(name,[]),list) or any(not isinstance(v,dict) for v in event.get(name,[])):raise ValueError('Invalid sysstat metric rows')
            for name in ('memory','queue','paging'):
                if not isinstance(event.get(name,{}),dict):raise ValueError('Invalid sysstat metric mapping')
            stamp=event['timestamp'];utc=stamp.get('utc') in (1,True,'1') or stamp.get('tz') in ('UTC','GMT','Z')
            end=timestamp(stamp['date']+'T'+stamp['time']+('Z' if utc else ''),spec.get('timezone') or host.get('timezone'))
            interval=float(stamp.get('interval',0))
            start=end-dt.timedelta(seconds=interval) if interval>0 else previous
            previous=end
            if start is None:continue
            def add(entity,metric,unit,value):rows.append(sample(identity,entity,metric,unit,start,end,value))
            for cpu in event.get('cpu-load',[]):
                if cpu.get('cpu')=='all':
                    add('host','cpu.busy_percent','percent',max(0,100-number(cpu['idle'])-number(cpu.get('iowait',0))))
                    if 'iowait' in cpu:add('host','cpu.iowait_percent','percent',cpu['iowait'])
            for field,metric,unit,scale in [('memused-percent','memory.used_percent','percent',1),('commit-percent','memory.commit_percent','percent',1),('avail','memory.available_mb','MiB',1/1024)]:
                if field in event.get('memory',{}):add('host',metric,unit,float(event['memory'][field])*scale)
            for disk in event.get('disk',[]):
                entity=disk.get('disk-device',disk.get('disk_device','unknown'))
                for field,metric,unit,scale in [('await','disk.latency_ms','ms',1),('aqu-sz','disk.queue','requests',1),('util-percent','disk.busy_percent','percent',1),('rkB','disk.read_bytes_per_sec','bytes/s',1024),('wkB','disk.write_bytes_per_sec','bytes/s',1024)]:
                    if field in disk:add(entity,metric,unit,float(disk[field])*scale)
            if 'runq-sz' in event.get('queue',{}):add('host','cpu.queue','tasks',event['queue']['runq-sz'])
            if 'majflt' in event.get('paging',{}):add('host','memory.major_faults_per_sec','faults/s',event['paging']['majflt'])
    return rows


def read_input(path,max_bytes):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>max_bytes:raise ValueError('Unsafe or oversized performance input')
    with path.open('rb') as stream:data=stream.read(max_bytes+1)
    if len(data)>max_bytes:raise ValueError('Performance input grew past limit')
    return data.decode('utf-16' if data.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig')


def load_source(spec,max_bytes=50*1024*1024,max_rows=200000,diagnostics=None):
    path=Path(spec['path']);kind=spec['type']
    if kind=='native_history':
        from native_recording import history_records
        records=history_records(path.parent)
        selected=records[-spec.get('max_chunks',96):];rows=[];used=0
        for record in selected:
            for name in record['files']:
                if not name.endswith('.json'):continue
                child=path.parent/name;used+=child.stat().st_size
                if used>max_bytes:raise ValueError('Native history exceeds byte limit; reduce max_chunks')
                child_spec={**spec,'path':str(child),'type':spec.get('format','sysstat_json')}
                # Apply offsets and aliases once, at the leaf.
                rows+=load_source(child_spec,max_bytes,max_rows-len(rows),diagnostics)
        if not rows:raise ValueError('No completed native history chunks')
        return rows
    text=read_input(path,max_bytes)
    rows=perfmon(text,spec,diagnostics) if kind=='perfmon_csv' else sysstat(json.loads(text),spec) if kind=='sysstat_json' else json.loads(text)['samples'] if kind=='normalized_json' else None
    if rows is None or not isinstance(rows,list) or not rows:raise ValueError('No performance samples in selected format')
    if len(rows)>max_rows:raise ValueError('Performance row limit exceeded; narrow export window')
    shift=dt.timedelta(seconds=float(spec.get('clock_offset_seconds',0)));result=[]
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Invalid performance sample')
        result.append(sample(spec.get('host') or row['host'],row.get('entity','host'),str(row['metric']),str(row['unit']),timestamp(row['start'])+shift,timestamp(row['end'])+shift,row['value']))
    return result


def load_jobs(path,max_bytes=20*1024*1024,max_rows=10000):
    path=Path(path)
    if path.is_symlink() or path.stat().st_size>max_bytes:raise ValueError('Unsafe/oversized job history')
    text=read_input(path,max_bytes);rows=json.loads(text) if path.suffix=='.json' else list(csv.DictReader(io.StringIO(text)))
    if not isinstance(rows,list) or len(rows)>max_rows:raise ValueError('Job history row limit exceeded')
    result=[];unknown=[]
    for row in rows:
        if not isinstance(row,dict):raise ValueError('Invalid observed job row')
        if not row.get('end'):unknown.append({'job_id':row.get('job_id'),'reason':'No observed end; excluded from overlap comparison'});continue
        start=timestamp(row['start']);end=timestamp(row['end'])
        if end<=start or not row.get('host') or not row.get('job_id'):raise ValueError('Invalid observed job interval/host')
        result.append({'job_id':str(row['job_id']),'host':str(row['host']),'start':start.isoformat(),'end':end.isoformat(),'status':str(row.get('status','unknown'))})
    return result,unknown


def job_windows(jobs):
    events={};windows=[]
    for i,row in enumerate(jobs):events.setdefault(row['host'],[]).extend([(timestamp(row['start']),1,i,row['job_id']),(timestamp(row['end']),-1,i,row['job_id'])])
    for host,items in sorted(events.items()):
        active={};previous=None
        for when,kind,index,job in sorted(items):
            if previous is not None and when>previous and active:windows.append({'host':host,'start':previous,'end':when,'jobs':sorted(active.values())[:256],'active_count':len(active)})
            if kind==1:active[index]=job
            else:active.pop(index,None)
            previous=when
    return windows


def validate(config):
    if not isinstance(config,dict):raise ValueError('performance must be a mapping')
    for key in ('enabled','required','export_native'):
        if key in config and not isinstance(config[key],bool):raise ValueError('performance.'+key+' must be boolean')
    for key,ceiling in [('max_input_bytes',512*1024*1024),('max_rows',1000000),('minimum_samples',10000),('max_comparisons',50000),('max_details',50000),('max_series',2000)]:
        if key in config and (not isinstance(config[key],int) or isinstance(config[key],bool) or not 1<=config[key]<=ceiling):raise ValueError('Invalid performance.'+key)
    for key in ('max_sample_seconds','minimum_coverage'):
        if key in config and (isinstance(config[key],bool) or not isinstance(config[key],(int,float)) or not math.isfinite(config[key]) or config[key]<=0):raise ValueError('Invalid performance.'+key)
    if config.get('minimum_coverage',.7)>1:raise ValueError('minimum_coverage must be <= 1')
    if not isinstance(config.get('thresholds',{}),dict):raise ValueError('thresholds must be a mapping')
    for key,value in config.get('thresholds',{}).items():
        if not isinstance(key,str) or isinstance(value,bool):raise ValueError('Invalid pressure threshold')
        number(value)
    for key in ('job_history','exclude_jobs'):
        if not isinstance(config.get(key,[]),list) or any(not isinstance(x,str) or not x for x in config.get(key,[])):raise ValueError(key+' must contain strings')
    if 'output' in config and (not isinstance(config['output'],str) or not config['output']):raise ValueError('performance.output must be a path')
    if not isinstance(config.get('sources',[]),list):raise ValueError('performance.sources must be a list')
    ids=set()
    for spec in config.get('sources',[]):
        if not isinstance(spec,dict):raise ValueError('Invalid performance source')
        identity=spec.get('id','')
        if not isinstance(identity,str) or not re.fullmatch('[A-Za-z0-9_-]{1,80}',identity) or identity in ids:raise ValueError('Performance source IDs must be safe and unique')
        ids.add(identity)
        if not isinstance(spec.get('enabled',True),bool):raise ValueError('source.enabled must be boolean')
        if spec.get('type') not in ('perfmon_csv','sysstat_json','normalized_json','native_history'):raise ValueError('Unknown performance source format')
        if not isinstance(spec.get('path'),str) or not spec['path']:raise ValueError('Source path is required')
        for field in ('expected_metrics','expected_counters'):
            if not isinstance(spec.get(field,[]),list) or any(not isinstance(v,str) or not v for v in spec.get(field,[])):raise ValueError(field+' must contain names')
        if spec['type']=='perfmon_csv' and not spec.get('host'):raise ValueError('PerfMon sources require a host ID')
        if 'timezone' in spec and '${' not in spec['timezone']:ZoneInfo(spec['timezone'])
        if not math.isfinite(float(spec.get('clock_offset_seconds',0))) or abs(float(spec.get('clock_offset_seconds',0)))>86400:raise ValueError('Clock offset must be finite')
        if not isinstance(spec.get('max_chunks',96),int) or isinstance(spec.get('max_chunks',96),bool) or not 1<=spec.get('max_chunks',96)<=1000:raise ValueError('max_chunks must be 1..1000')
        if spec.get('format','sysstat_json') not in ('sysstat_json','normalized_json'):raise ValueError('Invalid native history format')
    return config


def canonical_samples(samples):
    dedup={};units={};series={}
    for row in samples:
        row=sample(row['host'],row['entity'],row['metric'],row['unit'],timestamp(row['start']),timestamp(row['end']),row['value'])
        key=(row['host'],row['entity'],row['metric']);unit=units.setdefault(key,row['unit'])
        if unit!=row['unit']:raise ValueError('Conflicting units for one metric series')
        stamp=(*key,row['start'],row['end'])
        if stamp in dedup and dedup[stamp]!=row:raise ValueError('Conflicting duplicate performance samples')
        dedup[stamp]=row
    for row in dedup.values():series.setdefault((row['host'],row['entity'],row['metric'],row['unit']),[]).append(row)
    for values in series.values():
        values.sort(key=lambda r:(r['start'],r['end']));prior=None
        for row in values:
            if prior is not None and timestamp(row['start'])<prior:raise ValueError('Overlapping intervals in one metric series')
            prior=timestamp(row['end'])
    return series


def analyze(samples,jobs,config=None):
    config=validate(config or {});thresholds={**DEFAULT_THRESHOLDS,**config.get('thresholds',{})};series=canonical_samples(samples)
    if len(series)>config.get('max_series',500):raise ValueError('Metric series limit exceeded')
    summaries=[];findings=[];gaps=[];usable={};max_gap=float(config.get('max_sample_seconds',180));minimum=config.get('minimum_samples',3)
    windows=job_windows(jobs);by_host={};window_ends={};details=config.get('max_details',10000);truncated={'findings':0,'gaps':0,'comparisons':0}
    for w in windows:by_host.setdefault(w['host'],[]).append(w)
    for host,values in by_host.items():window_ends[host]=[w['end'].timestamp() for w in values]
    def nearby(host,start,end):
        values=by_host.get(host,[]);i=bisect.bisect_right(window_ends.get(host,[]),start)
        while i<len(values) and values[i]['start'].timestamp()<end:
            yield values[i];i+=1
    def detail(name,rows,row):
        if len(rows)<details:rows.append(row)
        else:truncated[name]+=1
    for key,values in sorted(series.items()):
        valid=[];prior=None
        for row in values:
            begin=timestamp(row['start']);end=timestamp(row['end'])
            if prior is not None and begin>prior:detail('gaps',gaps,{'host':key[0],'entity':key[1],'metric':key[2],'start':prior.isoformat(),'end':begin.isoformat(),'reason':'missing samples'})
            prior=end
            if (end-begin).total_seconds()>max_gap:detail('gaps',gaps,{**row,'reason':'sample interval too coarse'});continue
            valid.append(row)
        starts=[timestamp(r['start']).timestamp() for r in valid];ends=[timestamp(r['end']).timestamp() for r in valid];durations=[b-a for a,b in zip(starts,ends)]
        baseline={}
        for row,start,end in zip(valid,starts,ends):
            for w in nearby(key[0],start,end):
                if len(w['jobs'])==1 and w['start'].timestamp()<=start and w['end'].timestamp()>=end:baseline.setdefault(w['jobs'][0],[]).append(row['value'])
        usable[key]=(valid,starts,ends,baseline);threshold=thresholds.get(key[2]);seconds=sum(durations)
        over=[(r,secs) for r,secs in zip(valid,durations) if threshold is not None and r['value']>=number(threshold)]
        summaries.append({'host':key[0],'entity':key[1],'metric':key[2],'unit':key[3],'samples':len(valid),
            'mean':sum(r['value']*secs for r,secs in zip(valid,durations))/seconds if seconds else None,
            'p95_sample_value':sorted(r['value'] for r in valid)[math.ceil(len(valid)*.95)-1] if valid else None,
            'peak':max((r['value'] for r in valid),default=None),'warning_threshold':threshold,
            'minutes_at_or_above_warning':sum(secs/60 for _,secs in over)})
        for row,_ in over:
            active=sorted({j for w in nearby(key[0],timestamp(row['start']).timestamp(),timestamp(row['end']).timestamp()) for j in w['jobs']})
            detail('findings',findings,{**row,'status':'sampled_pressure','observed_jobs':active[:256],'jobs_truncated':max(0,len(active)-256)})
    comparisons=[];baseline_cache={};maximum=config.get('max_comparisons',5000)
    for window in windows:
        if len(window['jobs'])<2:continue
        for key,(values,starts,ends,baselines) in sorted(usable.items()):
            if key[0]!=window['host']:continue
            if len(comparisons)>=maximum:truncated['comparisons']+=1;continue
            start=window['start'].timestamp();end=window['end'].timestamp();first=bisect.bisect_right(ends,start);last=bisect.bisect_left(starts,end)
            selected=values[first:last];weights=[min(b,end)-max(a,start) for a,b in zip(starts[first:last],ends[first:last])]
            covered=sum(weights);coverage=covered/(end-start);enough=len(selected)>=minimum and coverage>=config.get('minimum_coverage',.7)
            job_ids=tuple(sorted(set(window['jobs'])));cache_key=(key,job_ids)
            if cache_key not in baseline_cache:
                baseline=[v for job in job_ids for v in baselines.get(job,[])];baseline_cache[cache_key]=(statistics.median(baseline) if len(baseline)>=minimum else None,len(baseline))
            median,count=baseline_cache[cache_key];threshold=thresholds.get(key[2]);pressure=threshold is not None and any(r['value']>=number(threshold) for r in selected)
            comparisons.append({'host':key[0],'jobs':list(window['jobs'][:256]),'jobs_truncated':max(0,window['active_count']-256),'start':window['start'].isoformat(),'end':window['end'].isoformat(),
                'metric':key[2],'entity':key[1],'unit':key[3],'samples':len(selected),'coverage_fraction':round(coverage,6),
                'overlap_mean':sum(r['value']*w for r,w in zip(selected,weights))/covered if covered else None,
                'solo_sample_median':median,'solo_samples':count,
                'status':'insufficient_evidence' if not enough or window['active_count']>256 else 'observed_overlap_with_pressure' if pressure else 'no_sampled_pressure' if threshold is not None else 'no_threshold_configured'})
    return {'schema':1,'data_window':{'start':min((r['start'] for values in series.values() for r in values),default=None),'end':max((r['end'] for values in series.values() for r in values),default=None)},'summary':summaries,'findings':findings,'overlap_comparisons':comparisons,'gaps':gaps,'truncated':truncated,
        'unmatched_job_hosts':sorted({j['host'] for j in jobs}-{key[0] for key in series}),
        'samples':sorted([r for values in series.values() for r in values],key=lambda r:(r['host'],r['metric'],r['entity'],r['start'])),
        'limits':'Observed interval correlation, not causation or proof of job ownership of resources. Pressure thresholds require workload review. Missing metrics, unmatched hosts, coarse samples and unknown job ends are not healthy evidence. Solo baselines use the same host and job IDs, but are not workload-controlled experiments. P95 describes sample values, not a time-weighted percentile. Unknown unlogged jobs and background load may contribute. Charts and detailed rows are bounded; summary totals include all accepted samples.'}


def render_charts(value,max_points=1500):
    from review_html import render
    detail={k:v for k,v in value.items() if k!='samples'};doc=render(detail,'Server performance and observed overlaps');charts=[];groups={}
    for row in value['samples']:groups.setdefault((row['host'],row['entity'],row['metric'],row['unit']),[]).append(row)
    for key,rows in sorted(groups.items())[:100]:
        selected=rows[-max_points:];low=min(timestamp(r['start']).timestamp() for r in selected);high=max(timestamp(r['end']).timestamp() for r in selected);maximum=max(1,max(r['value'] for r in selected));bars=[]
        for row in selected:
            x=800*(timestamp(row['start']).timestamp()-low)/max(1,high-low);width=max(.5,800*(timestamp(row['end'])-timestamp(row['start'])).total_seconds()/max(1,high-low));height=110*row['value']/maximum
            bars.append('<rect x="%.3f" y="%.3f" width="%.3f" height="%.3f" fill="#246a94"><title>%s</title></rect>'%(x,120-height,width,height,html.escape(row['start']+' — '+row['end']+': '+str(row['value']))))
        charts.append('<section class="chart"><h2>'+html.escape(' / '.join(key))+'</h2><p>'+html.escape(selected[0]['start']+' to '+selected[-1]['end'])+'; peak scale '+str(round(maximum,3))+('. Chart shows newest '+str(max_points)+' samples; JSON retains all.' if len(rows)>max_points else '')+'</p><svg role="img" aria-label="'+html.escape(' / '.join(key),quote=True)+'" viewBox="0 0 800 130" style="width:100%;max-height:220px">'+''.join(bars)+'</svg></section>')
    note='<p>Charts show up to 100 series and the newest '+str(max_points)+' samples per series. Full accepted data is in performance.json.</p>'
    doc=doc.replace("section.hidden=visible===0&&term!=='';","section.hidden=section.classList.contains('chart') ? !section.textContent.toLocaleLowerCase().includes(term) : visible===0&&term!=='';")
    from performance_incidents import render as incidents
    doc=doc.replace('<script defer>',incidents(value)+'<script defer>')
    return doc.replace('href="report.json"','href="performance.json"').replace('<script defer>',note+''.join(charts)+'<script defer>')


def run(config,output):
    from configbackup import single_instance_lock
    from transaction_journal import atomic
    validate(config)
    output=Path(output);output.mkdir(parents=True,exist_ok=True,mode=0o700);samples=[];sources=[];cache=output/'.source-cache';cache.mkdir(exist_ok=True,mode=0o700)
    with single_instance_lock(output/'.performance.lock'):
        ids=set()
        for spec in config.get('sources',[]):
            identity=spec['id']
            if not re.fullmatch('[A-Za-z0-9_-]{1,80}',identity) or identity in ids:raise ValueError('Performance source IDs must be safe and unique')
            ids.add(identity)
            if not spec.get('enabled',True):sources.append({'id':identity,'status':'disabled'});continue
            target=cache/(identity+'.json')
            try:
                diagnostics=[]
                rows=load_source(spec,config.get('max_input_bytes',50*1024*1024),config.get('max_rows',200000),diagnostics)
                present={r['metric'] for r in rows}
                diagnostics.extend({'kind':'missing_metric','metric':metric} for metric in spec.get('expected_metrics',[]) if metric not in present)
                if len(samples)+len(rows)>config.get('max_rows',200000):raise ValueError('Combined row limit exceeded')
                combined=canonical_samples(samples+rows)
                if len(combined)>config.get('max_series',500):raise ValueError('Combined metric series limit exceeded')
                atomic_json(target,{'samples':rows});sources.append({'id':identity,'status':'partial' if diagnostics else 'complete','samples':len(rows),'coverage_warnings':diagnostics,'metrics':sorted(present)})
                samples+=rows
            except (OSError,ValueError,KeyError,TypeError,AttributeError,UnicodeError,OverflowError) as exc:
                sources.append({'id':identity,'status':'failed','error_type':type(exc).__name__,'prior_cache_preserved':target.is_file()})
                # Preserve old bytes, but do not use failed input as healthy evidence.
        if len(samples)>config.get('max_rows',200000):raise ValueError('Combined performance rows exceed limit')
        jobs=[];unknown=[]
        for path in config.get('job_history',[]):
            try:
                rows,notes=load_jobs(path)
                if len(jobs)+len(rows)>10000:raise ValueError('Combined job limit exceeded')
                jobs+=rows;unknown+=notes
            except (OSError,ValueError,KeyError,TypeError):unknown.append({'source':str(path),'reason':'Job history unavailable'})
        unique={}
        for row in jobs:
            key=(row['host'],row['job_id'],row['start'],row['end'])
            if key in unique and unique[key].get('status')!=row.get('status'):unique[key]={**row,'status':'unknown'}
            else:unique[key]=row
        jobs=list(unique.values())
        jobs=[r for r in jobs if not any(fnmatch.fnmatchcase(r['job_id'],p) for p in config.get('exclude_jobs',[]))]
        value=analyze(samples,jobs,config);value.update(source_status=sources,job_history_warnings=unknown,
            status='partial' if unknown or any(r['status'] in ('failed','partial') for r in sources) or any(value['truncated'].values()) or value['unmatched_job_hosts'] or value['gaps'] else 'complete' if samples else 'no_evidence')
        from performance_incidents import duration_comparisons
        value['observed_runs']=sorted(jobs,key=lambda r:(r['host'],r['start'],r['job_id'],r['end']))
        value['duration_comparisons']=duration_comparisons(value['observed_runs'])
        atomic_json(output/'performance.json',value);atomic(output/'performance.html',render_charts(value).encode())
        return value


def main(argv=None):
    import yaml
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--output',required=True);a=p.parse_args(argv)
    value=yaml.safe_load(Path(a.config).read_text());result=run(value.get('performance',value),a.output)
    print(json.dumps({k:result[k] for k in ('status','source_status','job_history_warnings')},indent=2));return 0 if result['status']=='complete' else 6

if __name__=='__main__':raise SystemExit(main())
