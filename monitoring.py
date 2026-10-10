"""Offline health analysis, persistent alert state and notification outbox.

Only explicit notification channels use the network. Collection, alert evaluation,
publication and delivery statuses remain independent.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import fnmatch
import glob
import hashlib
import html
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import time
from zoneinfo import ZoneInfo
import yaml
from telemetry import collect_disks, utcnow
from notifications import send

UTC = dt.timezone.utc
DEFAULT_RULES = [
    {'id':'disk-free-percent','metric':'*.volume.free_percent','op':'lt','warning':15,'critical':5,'clear':18},
    {'id':'local-disk-free-percent','metric':'system.disk.free_percent','op':'lt','warning':15,'critical':5,'clear':18},
    {'id':'local-disk-free-bytes','metric':'system.disk.free_bytes','op':'lt','warning':10*1024**3,'critical':2*1024**3,'clear':12*1024**3},
    {'id':'inodes','metric':'system.disk.free_inodes_percent','op':'lt','warning':10,'critical':3,'clear':12},
    {'id':'exhaustion','metric':'*.days_until_full','op':'lt','warning':14,'critical':3,'clear':18},
    {'id':'log-utilization','metric':'sqlserver.log.used_percent','op':'gt','warning':80,'critical':95,'clear':75},
    {'id':'sql-blocking','metric':'sqlserver.activity.blocking_seconds','op':'gt','warning':60,'critical':300,'clear':30},
    {'id':'pg-long-transaction','metric':'postgresql.activity.oldest_transaction_seconds','op':'gt','warning':1800,'critical':7200,'clear':900},
    {'id':'pg-wraparound','metric':'postgresql.database.xid_age_percent','op':'gt','warning':50,'critical':75,'clear':40},
    {'id':'pg-wal-retention','metric':'postgresql.slot.retained_bytes','op':'gt','warning':10*1024**3,'critical':50*1024**3,'clear':5*1024**3},
    {'id':'archive-failing','metric':'postgresql.archive.failing','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'ag-health','metric':'sqlserver.availability.unhealthy_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'scope-freshness','metric':'backup.scope.age_hours','op':'gt','warning':24,'critical':72,'clear':20},
    {'id':'failed-scope','metric':'backup.scope.failures_count','op':'gt','warning':0,'critical':3,'clear':0},
    {'id':'publication','metric':'backup.git.pending_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'source-unavailable','metric':'monitor.source.unavailable_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'source-stale','metric':'monitor.source.age_hours','op':'gt','warning':6,'critical':24,'clear':4},
    {'id':'collection-failures','metric':'monitor.source.failures_count','op':'gt','warning':0,'clear':0},
    {'id':'failed-task','metric':'backup.task.failed_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'apple-raid-degraded','metric':'system.apple_raid.degraded_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'apple-raid-offline','metric':'system.apple_raid.offline_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'raid-degraded','metric':'system.mdraid.degraded_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'smart-failing','metric':'system.smart.failed_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'nvme-critical','metric':'system.smart.critical_warning_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'nvme-spare','metric':'system.smart.spare_below_threshold_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'smart-pending','metric':'system.smart.pending_sectors_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'smart-uncorrectable','metric':'system.smart.offline_uncorrectable_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'windows-drive-health','metric':'system.windows_drive.failed_count','op':'gt','warning':0,'critical':0,'clear':0},
    {'id':'restore-age','metric':'backup.recovery.restore_age_days','op':'gt','warning':30,'critical':90,'clear':20},
    {'id':'verify-age','metric':'backup.recovery.verify_age_days','op':'gt','warning':7,'critical':30,'clear':5},
]


def validate_config(config):
    if not isinstance(config,dict): raise ValueError('monitoring must be a mapping')
    for field in ('enabled','default_rules','required'):
        if field in config and not isinstance(config[field],bool):raise ValueError(field+' must be true or false')
    for field in ('sources','rules','paths','maintenance','schedule_reports','runtime_snapshots'):
        if not isinstance(config.get(field,[]),list):raise ValueError('monitoring.'+field+' must be a list')
    seen=set()
    for rule in config.get('rules',[]):
        if not isinstance(rule,dict) or not rule.get('id') or not rule.get('metric'):raise ValueError('Each rule needs id and metric')
        if rule['id'] in seen:raise ValueError('Duplicate rule ID: '+rule['id'])
        seen.add(rule['id'])
        if rule.get('op','gt') not in ('gt','lt'):raise ValueError('Rule op must be gt or lt')
        if not isinstance(rule.get('enabled',True),bool):raise ValueError('Rule enabled must be true or false')
        for field in ('warning','critical','clear','consecutive','clear_consecutive','cooldown_minutes','stale_after_minutes'):
            if field in rule and (not isinstance(rule[field],(float,int)) or not math.isfinite(rule[field])):raise ValueError('Rule '+field+' must be finite numeric')
        for field in ('consecutive','clear_consecutive'):
            if field in rule and (rule[field]<1 or int(rule[field])!=rule[field]):raise ValueError(field+' must be a positive integer')
        if 'warning' in rule and 'critical' in rule and ((rule['critical']<rule['warning']) if rule.get('op','gt')=='gt' else (rule['critical']>rule['warning'])):raise ValueError('Critical threshold must be more severe than warning')
    seen=set()
    for channel in config.get('notifications',{}).get('channels',[]):
        if not channel.get('id') or channel['id'] in seen:raise ValueError('Notification channel IDs must be unique')
        seen.add(channel['id'])
        if channel.get('type') not in ('smtp','webhook','ntfy','heartbeat'):raise ValueError('Unknown notification channel type')
        if not isinstance(channel.get('enabled',True),bool):raise ValueError('Channel enabled must be true or false')
        if any(k in channel for k in ('password','token','secret')):raise ValueError('Notification secrets must use environment references')
        if channel['type']=='smtp':
            for key in ('host','from','to'):
                if not channel.get(key):raise ValueError('SMTP channel requires '+key)
            from notifications import SECTIONS
            if any(x not in SECTIONS for x in channel.get('sections',SECTIONS)):raise ValueError('Unknown email section')
            if int(channel.get('max_rows',30))<1 or int(channel.get('max_html_bytes',100000))<1024:raise ValueError('Email limits must allow at least one row and 1024 bytes')
        elif not (channel.get('url') or channel.get('url_env')):raise ValueError('Notification endpoint URL or url_env required')
    from runtime_history import validate as validate_runtime
    validate_runtime(config.get('runtime_snapshots',[]))
    return config


def timestamp(value):
    if isinstance(value, (int,float)): return float(value)
    parsed = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if parsed.tzinfo is None: raise ValueError('Observation timestamp must include an offset')
    return parsed.timestamp()


def key_for(metric, labels):
    return hashlib.sha256(json.dumps([metric,labels],sort_keys=True).encode()).hexdigest()


def metric(name, value, labels, observed, unit='count', kind='gauge', reset=None):
    if value is not None:
        value = float(value)
        if not math.isfinite(value): value = None
    return {'metric':name,'value':value,'labels':labels,'observed':timestamp(observed),
            'unit':unit,'kind':kind,'reset':str(reset or '')}


def flatten(payload, overrides=None):
    if payload.get('schema_version') != 1: raise ValueError('Unsupported telemetry schema')
    overrides = overrides or {}
    observed = payload['observed_at']
    labels = {k:str(overrides.get(k,payload.get(k)) or '') for k in ('host','instance')}
    if payload.get('database'): labels['database'] = str(payload['database'])
    if overrides.get('source'):labels['source']=overrides['source']
    result=[]
    for name, data in sorted(payload.get('datasets',{}).items()):
        category = name.split('/')[-1] if name.startswith('db/') else name.split(':',1)[0]
        for row in data['rows']:
            identity = {**labels, **{k:str(row.get(k) or '') for k in data.get('keys',[])}}
            for field, unit in data['units'].items():
                result.append(metric(payload['engine']+'.'+category+'.'+field,row.get(field),identity,observed,
                                     unit,'counter' if field in data.get('counters',[]) else 'gauge',row.get(data.get('reset_field'))))
    return result


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=30)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS samples(series TEXT,observed REAL,value REAL,metric TEXT,labels TEXT,unit TEXT,kind TEXT,reset TEXT,PRIMARY KEY(series,observed));
        CREATE INDEX IF NOT EXISTS sample_time ON samples(observed);
        CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,channel TEXT,message TEXT,attempts INTEGER DEFAULT 0,next_attempt REAL,status TEXT DEFAULT 'pending',error TEXT DEFAULT '',created REAL);
        ''')
        try: os.chmod(path,0o600)
        except OSError: pass
    def close(self): self.db.commit();self.db.close()
    def get(self, table, key, default=None):
        if table not in ('alerts','meta'): raise ValueError('Invalid state table')
        row=self.db.execute(f'SELECT data FROM {table} WHERE id=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def put(self, table, key, value):
        if table not in ('alerts','meta'): raise ValueError('Invalid state table')
        self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?)',(key,json.dumps(value,sort_keys=True)))
    def add(self, samples):
        for sample in samples:
            self.db.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?)',
                (key_for(sample['metric'],sample['labels']),sample['observed'],sample['value'],sample['metric'],
                 json.dumps(sample['labels'],sort_keys=True),sample['unit'],sample['kind'],sample['reset']))
    def latest(self):
        rows=self.db.execute('SELECT s.* FROM samples s JOIN (SELECT series,MAX(observed) t FROM samples GROUP BY series) x ON s.series=x.series AND s.observed=x.t ORDER BY s.series').fetchall()
        return [{**dict(r),'labels':json.loads(r['labels'])} for r in rows]
    def derived(self, samples, now, window_hours=168, minimum_hours=6, minimum_samples=3):
        result=[]
        for sample in samples:
            if sample['value'] is None: continue
            series=key_for(sample['metric'],sample['labels'])
            history=self.db.execute('SELECT observed,value,reset FROM samples WHERE series=? AND observed>=? AND observed<=? AND value IS NOT NULL ORDER BY observed',
                    (series,now-window_hours*3600,sample['observed'])).fetchall()
            if sample['kind']=='counter' and len(history)>=2:
                previous,current=history[-2:]
                elapsed=current['observed']-previous['observed']
                if elapsed>0 and previous['reset']==current['reset'] and current['value']>=previous['value']:
                    result.append(metric(sample['metric']+'.per_second',(current['value']-previous['value'])/elapsed,sample['labels'],sample['observed'],sample['unit']+'/second'))
            if sample['unit']=='bytes' and sample['kind']=='gauge' and len(history)>=minimum_samples:
                span=history[-1]['observed']-history[0]['observed']
                if span<minimum_hours*3600: continue
                # Median pairwise slope tolerates a single unusual sample better than endpoint subtraction.
                points=history[-100:]
                slopes=[(b['value']-a['value'])/(b['observed']-a['observed']) for i,a in enumerate(points) for b in points[i+1:] if b['observed']>a['observed']]
                if not slopes: continue
                rate=statistics.median(slopes)
                result.append(metric(sample['metric']+'.growth_bytes_per_day',rate*86400,sample['labels'],sample['observed'],'bytes/day'))
                if sample['metric'].endswith('free_bytes') and rate<0:
                    result.append(metric(sample['metric']+'.days_until_full',sample['value']/(-rate*86400),sample['labels'],sample['observed'],'days'))
        return result


def matches(sample, rule):
    return fnmatch.fnmatchcase(sample['metric'],rule['metric']) and all(
        fnmatch.fnmatchcase(str(sample['labels'].get(k,'')),str(pattern)) for k,pattern in rule.get('where',{}).items())


def maintenance(config, labels, now):
    for window in config.get('maintenance',[]):
        if any(not fnmatch.fnmatchcase(labels.get(k,''),str(v)) for k,v in window.get('where',{}).items()): continue
        if 'start' in window and 'end' in window:
            if timestamp(window['start'])<=now<timestamp(window['end']): return True
        elif 'start_time' in window and 'end_time' in window:
            local=dt.datetime.fromtimestamp(now,ZoneInfo(window.get('timezone','UTC')))
            if local.weekday() not in window.get('weekdays',list(range(7))): continue
            current=local.strftime('%H:%M');start,end=window['start_time'],window['end_time']
            if (start<=current<end) if start<=end else (current>=start or current<end): return True
    return False


def evaluate(store, config, samples, now):
    rules=(DEFAULT_RULES if config.get('default_rules',True) else [])+config.get('rules',[])
    # Explicit rules override the defaults with the same ID.
    rules=list({r['id']:r for r in rules}.values())
    events=[];seen=set();current=[]
    for rule in rules:
        if rule.get('enabled',True) is False: continue
        for sample in samples:
            if not matches(sample,rule): continue
            identity=rule['id']+':'+key_for(sample['metric'],sample['labels']);seen.add(identity)
            prior=store.get('alerts',identity,{})
            previous=prior.get('status','ok')
            value=sample['value'];age=now-sample['observed']
            stale=age>float(rule.get('stale_after_minutes',config.get('stale_after_minutes',120)))*60
            candidate='unknown' if value is None or stale else 'ok'
            op=rule.get('op','gt')
            if op not in ('gt','lt'): raise ValueError('Alert op must be gt or lt')
            compare=(lambda a,b:a>b) if op=='gt' else (lambda a,b:a<b)
            if candidate!='unknown':
                for level in ('warning','critical'):
                    if level in rule and compare(value,float(rule[level])): candidate=level
                if previous in ('warning','critical') and candidate=='ok' and 'clear' in rule and compare(value,float(rule['clear'])):
                    candidate=previous
                for suffix,minimum in rule.get('minimum',{}).items():
                    related=[x for x in samples if x['labels']==sample['labels'] and x['metric'].rsplit('.',1)[0]==sample['metric'].rsplit('.',1)[0] and x['metric'].endswith('.'+suffix)]
                    if not related or related[0]['value'] is None: candidate='unknown'
                    elif related[0]['value']<float(minimum): candidate='ok'
            fresh_observation=sample['observed']!=prior.get('last_observed')
            consecutive=prior.get('consecutive',0)
            if fresh_observation: consecutive=consecutive+1 if candidate==prior.get('candidate') else 1
            required=int(rule.get('clear_consecutive',1) if candidate=='ok' else rule.get('consecutive',config.get('consecutive',2)))
            status=candidate if candidate=='unknown' or consecutive>=required else previous
            # Unknown never closes an existing problem. Keep that condition visible until evidence recovers.
            if candidate=='unknown' and previous in ('warning','critical'): status=previous
            suppressed=maintenance(config,sample['labels'],now)
            data={**prior,'id':identity,'rule':rule['id'],'metric':sample['metric'],'labels':sample['labels'],
                  'value':value,'unit':sample['unit'],'status':status,'candidate':candidate,'consecutive':consecutive,
                  'last_observed':sample['observed'],'age_seconds':max(0,age),'unknown':candidate=='unknown',
                  'suppressed':suppressed,'threshold':rule.get(status),'updated':now,
                  'message':str(rule.get('message',''))}
            last_notified=prior.get('notified_status','ok')
            cooldown=float(rule.get('cooldown_minutes',config.get('cooldown_minutes',360)))*60
            transition=status!=last_notified and status!='unknown'
            repeat=status in ('warning','critical') and now-prior.get('last_notified',0)>=cooldown
            if not suppressed and not data['unknown'] and (transition or repeat):
                event={k:data[k] for k in ('id','rule','metric','labels','value','unit','status','threshold','message')}
                event['event']='recovered' if status=='ok' else 'opened' if last_notified in ('ok','unknown') else 'changed' if status!=last_notified else 'reminder'
                events.append(event);data.update(last_notified=now,notified_status=status)
            store.put('alerts',identity,data);current.append(data)
    for row in store.db.execute('SELECT id,data FROM alerts').fetchall():
        if row['id'] not in seen:
            data=json.loads(row['data'])
            active_rules={r['id'] for r in rules if r.get('enabled',True)}
            disabled=config.get('_disabled_labels',[])
            if data['rule'] not in active_rules or any(all(data.get('labels',{}).get(k)==v for k,v in labels.items()) for labels in disabled):
                data.update(status='disabled',unknown=False,candidate='disabled')
            else: data.update(unknown=True,candidate='unknown')
            store.put('alerts',row['id'],data);current.append(data)
    return current,events


def age_metric(name, value, labels, now, unit='hours'):
    divisor={'hours':3600,'days':86400}[unit]
    return metric(name,(now-timestamp(value))/divisor if value else None,labels,now,unit)


def backup_metrics(state, host, now, run=None):
    samples=[]
    for task, values in state.get('tasks',{}).items():
        scopes=values.get('scopes') or {'task':values}
        for scope, value in scopes.items():
            if value.get('status') in ('disabled','not_applicable','superseded'):continue
            labels={'host':host,'task':task,'scope':scope}
            samples.append(age_metric('backup.scope.age_hours',value.get('source_collected_at',value.get('last_success')),labels,now))
            samples.append(metric('backup.scope.failures_count',value.get('consecutive_failures',0),labels,now))
    pub=state.get('publication',{})
    if pub.get('local_commit'):
        samples.append(metric('backup.git.pending_count',int(pub.get('push_required',False) and (pub.get('local_commit')!=pub.get('pushed_commit') or (pub.get('pr_required') and not pub.get('pr_url')))),{'host':host},now))
    recovery=state.get('recovery',{})
    for kind in ('verify','restore'):
        samples.append(age_metric('backup.recovery.'+kind+'_age_days',recovery.get('last_'+kind),{'host':host},now,'days'))
    if run:
        for task, result in run.get('tasks',{}).items():
            samples.append(metric('backup.task.failed_count',int(result['status'] in ('failed','partial','skipped')),{'host':host,'task':task},now))
    return samples


def ingest(store, config, now):
    samples=[];failures=[]
    for spec in config.get('sources',[]):
        spec={'path':spec} if isinstance(spec,str) else spec
        if spec.get('enabled',True) is False:continue
        paths=sorted(glob.glob(spec['path'],recursive=True))
        label={'source':spec['path'],'host':spec.get('host','')}
        last=store.get('meta','source:'+spec['path'],{})
        observed=[];count=0
        for path in paths:
            try:
                payload=json.loads(Path(path).read_text(encoding='utf-8-sig'))
                instant=timestamp(payload['observed_at'])
                if instant>now+300: raise ValueError('Telemetry timestamp is in the future')
                values=flatten(payload,{**spec,'source':spec['path']})
                samples+=values;observed.append(instant);count+=len(payload.get('failures',[]))
            except Exception as exc:
                failures.append({'source':path,'error':type(exc).__name__});count+=1
        if observed:
            # Oldest file matters: a fresh healthy database cannot hide a stale sibling.
            last={'observed':min(observed)};store.put('meta','source:'+spec['path'],last)
        samples.append(metric('monitor.source.unavailable_count',int(not paths or not observed),label,now))
        samples.append(metric('monitor.source.failures_count',count,label,now))
        samples.append(metric('monitor.source.age_hours',(now-last['observed'])/3600 if last.get('observed') else None,label,now,'hours'))
    if config.get('paths'):
        payload=collect_disks([p for p in config['paths'] if not isinstance(p,dict) or p.get('enabled',True)],config.get('host'))
        samples+=flatten(payload)
        for failure in payload['failures']:
            samples.append(metric('monitor.source.unavailable_count',1,{'source':failure['section']},now));failures.append(failure)
    return samples,failures


def summary_rows(samples, prefix):
    return [{'metric':s['metric'],'entity':', '.join(k+'='+v for k,v in sorted(s['labels'].items())),
             'value':round(s['value'],3) if s['value'] is not None else 'unknown','unit':s['unit']} for s in samples if prefix(s['metric'])]


def build_summary(samples, alerts, now, state=None, schedules=None, changes=None, run=None):
    problems=[a for a in alerts if a['status'] in ('warning','critical')]
    status='critical' if any(a['status']=='critical' for a in problems) else 'warning' if problems else 'unknown' if any(a.get('unknown') for a in alerts) else 'healthy' if samples else 'unknown'
    if run and any(r.get('required') and r['status'] in ('failed','partial','skipped') for r in run.get('tasks',{}).values()): status='critical'
    return {'schema_version':1,'observed_at':dt.datetime.fromtimestamp(now,UTC).isoformat(),'status':status,
        'alerts':[{'severity':a['status'],'rule':a['rule'],'entity':json.dumps(a['labels'],sort_keys=True),
                   'value':a['value'],'unit':a['unit'],'threshold':a.get('threshold'),'stale':a.get('unknown',False),'muted':a.get('suppressed',False)} for a in problems],
        'capacity':summary_rows(samples,lambda n:any(word in n for word in ('free_bytes','free_percent','days_until_full','used_percent','growth_bytes_per_day'))),
        'freshness':summary_rows(samples,lambda n:n.startswith(('backup.scope','monitor.source'))),
        'recovery':summary_rows(samples,lambda n:n.startswith('backup.recovery') or n.startswith('sqlserver.backups')),
        'schedules':schedules or [],'changes':changes or [],'metrics':samples,
        'unknown_checks':[{'rule':a['rule'],'entity':a['labels']} for a in alerts if a.get('unknown')]}


def enqueue(store, channel, message, now):
    pending=store.db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
    if pending>=1000: raise RuntimeError('Notification outbox limit reached; pending messages preserved')
    store.db.execute('INSERT INTO outbox(channel,message,next_attempt,created) VALUES(?,?,?,?)',
                     (channel,json.dumps(message,sort_keys=True),now,now))


def notifications(store, config, events, summary, now, deliver=True):
    notify_config=config.get('notifications',{})
    channels={x['id']:x for x in notify_config.get('channels',[]) if x.get('enabled',True)} if notify_config.get('enabled',True) else {}
    for channel_id, channel in channels.items():
        interval=float(channel.get('digest_hours',24))*3600
        last=store.get('meta','digest:'+channel_id,{}).get('sent',0)
        if channel['type']=='heartbeat':
            enqueue(store,channel_id,{'subject':'ConfigBackup heartbeat','status':summary['status'],'observed_at':summary['observed_at']},now)
            continue
        if events and channel.get('immediate',True):
            facts='\n'.join(e['event']+' '+e['rule']+' '+json.dumps(e['labels'],sort_keys=True)+': '+str(e['value'])+' '+e['unit'] for e in events)
            enqueue(store,channel_id,{'subject':'ConfigBackup: '+str(len(events))+' health event(s)','text':facts,
                    'events':events,'summary':{k:v for k,v in summary.items() if k not in ('metrics',)}},now)
        elif channel.get('digest',False) and now-last>=interval:
            enqueue(store,channel_id,{'subject':'ConfigBackup '+summary['status']+' summary','text':'Operations summary: '+summary['status'],
                    'summary':{k:v for k,v in summary.items() if k!='metrics'}},now)
            store.put('meta','digest:'+channel_id,{'sent':now})
    store.db.commit()  # Persist before any network I/O; a crash can cause duplicate delivery, never silent loss.
    errors=[]
    if deliver:
        for row in store.db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 50",(now,)).fetchall():
            channel=channels.get(row['channel'])
            if not channel: continue
            try:
                send(channel,json.loads(row['message']))
                store.db.execute("UPDATE outbox SET status='sent',error='' WHERE id=?",(row['id'],))
            except Exception as exc:
                delay=min(86400,60*2**min(row['attempts'],10))
                # Do not persist SMTP tokens, endpoint responses or credential-bearing URLs from exception strings.
                error=type(exc).__name__
                store.db.execute('UPDATE outbox SET attempts=attempts+1,next_attempt=?,error=? WHERE id=?',(now+delay,error,row['id']))
                errors.append({'channel':row['channel'],'error':error})
            store.db.commit()
    return errors


def dashboard(path, summary):
    safe=json.dumps(summary,ensure_ascii=False).replace('<','\\u003c').replace('&','\\u0026')
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ConfigBackup operations</title>
<style>body{font:15px system-ui;background:#f3f6fa;color:#17283a;margin:0}main{max-width:1450px;margin:auto;padding:32px}h1{font-size:30px;margin-bottom:6px}.muted{color:#5d7082}.cards{display:flex;gap:16px;flex-wrap:wrap}.card{background:white;border:1px solid #d9e3ec;padding:20px;border-radius:10px;min-width:170px}.card strong{display:block;font-size:28px}input,select,button{font:inherit;padding:9px;border:1px solid #bbcbd9;border-radius:6px;background:white}table{border-collapse:collapse;width:100%;background:white;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid #dfe7ef;overflow-wrap:anywhere}th{background:#e7eef6;cursor:pointer}section{margin:28px 0;overflow:auto}.critical{color:#b32237}.warning{color:#8a5700}.healthy{color:#14714e}details{margin:16px 0}</style>
<main><p class="muted">CONFIGBACKUP 2.0 · OFFLINE OPERATIONS</p><h1>What needs attention</h1><p id="time" class="muted"></p><div id="cards" class="cards"></div>
<p><input id="search" placeholder="Filter host, database, rule or metric" size="45"> <select id="section"><option value="alerts">Active alerts</option><option value="capacity">Capacity</option><option value="freshness">Collection freshness</option><option value="schedules">Scheduling</option><option value="recovery">Recovery readiness</option><option value="changes">Configuration changes</option><option value="metrics">All measurements</option><option value="unknown_checks">Unknown checks</option></select> <button id="download">Download visible JSON</button></p><section id="table"></section><p class="muted">This report is a snapshot, not a live connection. Unknown and stale observations require investigation. Growth forecasts are estimates. No database maintenance or scheduler changes are performed.</p></main>
<script id="data" type="application/json">PAYLOAD</script><script>
const d=JSON.parse(document.getElementById('data').textContent);let visible=[];const str=v=>typeof v==='object'?JSON.stringify(v):String(v??'');
document.getElementById('time').textContent=d.observed_at;for(const [label,value] of [['Overall status',d.status],['Active alerts',d.alerts.length],['Unknown checks',d.unknown_checks.length],['Measurements',d.metrics.length]]){const c=document.createElement('div');c.className='card';const b=document.createElement('strong');b.textContent=value;b.className=d.status;c.append(b,document.createTextNode(label));document.getElementById('cards').append(c)}
function draw(){const name=document.getElementById('section').value,q=document.getElementById('search').value.toLowerCase();visible=(d[name]||[]).filter(r=>JSON.stringify(r).toLowerCase().includes(q));const area=document.getElementById('table');area.replaceChildren();if(!visible.length){area.textContent='No matching records. Check source coverage before interpreting an empty result.';return}const keys=[...new Set(visible.flatMap(Object.keys))],t=document.createElement('table'),head=t.createTHead().insertRow();for(const k of keys){const th=document.createElement('th');th.textContent=k;th.onclick=()=>{d[name].sort((a,b)=>str(a[k]).localeCompare(str(b[k]),undefined,{numeric:true}));draw()};head.append(th)}const body=t.createTBody();for(const r of visible.slice(0,2000)){const row=body.insertRow();for(const k of keys)row.insertCell().textContent=str(r[k])}area.append(t);if(visible.length>2000)area.append(document.createTextNode('Showing first 2,000 matches. Narrow the filter or download JSON.'))}
document.getElementById('search').oninput=draw;document.getElementById('section').onchange=draw;document.getElementById('download').onclick=()=>{const u=URL.createObjectURL(new Blob([JSON.stringify(visible,null,2)],{type:'application/json'})),a=document.createElement('a');a.href=u;a.download='configbackup-'+document.getElementById('section').value+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)};draw();</script></html>'''
    path.write_text(page.replace('PAYLOAD',safe),encoding='utf-8')


def run_monitor(config, directory, state=None, run=None, changes=None, now=None, deliver=True):
    validate_config(config)
    if config.get('enabled',True) is False:
        return {'status':'disabled','alerts':[],'delivery_errors':[],'pending_notifications':0}
    now=time.time() if now is None else timestamp(now)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    store=Store(directory/'history.sqlite3')
    try:
        samples,failures=ingest(store,config,now)
        from runtime_history import capture
        runtime_snapshots=capture(config.get('runtime_snapshots',[]),directory/'runtime-history',now)
        for result in runtime_snapshots:
            if result['status']!='disabled':
                samples.append(metric('monitor.source.failures_count',int(result['status']=='failed'),{'source':'runtime-history:'+result['id']},now))
            if result['status']=='failed':failures.append({'source':'runtime-history:'+result['id'],'error':result['error']})
        if state is not None: samples+=backup_metrics(state,config.get('host','local'),now,run)
        store.add(samples)
        derived=store.derived(samples,now,float(config.get('trend_hours',168)),float(config.get('trend_minimum_hours',6)),int(config.get('trend_minimum_samples',3)))
        store.add(derived)
        disabled_sources={s['path'] for s in config.get('sources',[]) if isinstance(s,dict) and s.get('enabled',True) is False}
        latest=[s for s in store.latest() if s['labels'].get('source') not in disabled_sources]
        disabled_labels=[{'source':path} for path in disabled_sources]
        disabled_labels.extend({'source':'runtime-history:'+spec['id']} for spec in config.get('runtime_snapshots',[]) if spec.get('enabled',True) is False)
        for spec in config.get('paths',[]):
            if isinstance(spec,dict) and spec.get('enabled',True) is False:
                disabled_labels.extend([{'path':spec['path']},{'source':'disk:'+spec['path']}])
        if run:
            disabled_labels.extend({'task':name} for name,result in run.get('tasks',{}).items() if result.get('status')=='disabled')
        if state:
            for task,record in state.get('tasks',{}).items():
                for scope,value in record.get('scopes',{}).items():
                    if value.get('status') in ('disabled','not_applicable','superseded'):
                        disabled_labels.append({'task':task,'scope':scope})
        latest=[s for s in latest if not any(all(s['labels'].get(k)==v for k,v in labels.items()) for labels in disabled_labels)]
        alerts,events=evaluate(store,{**config,'_disabled_labels':disabled_labels},latest,now)
        scheduling=[]
        for spec in config.get('schedule_reports',[]):
            spec={'path':spec} if isinstance(spec,str) else spec
            if spec.get('enabled',True) is False: continue
            base=Path(spec['path'])
            status=base/'report-status.json'
            disabled_reports=json.loads(status.read_text()).get('disabled',[]) if status.is_file() else []
            for filename in ('execution-analysis.csv','resource-conflicts.csv','dependencies.csv','deadlines.csv','watchdogs.csv'):
                if filename.removesuffix('.csv') in disabled_reports: continue
                report=base/filename
                if report.is_file():
                    with report.open(encoding='utf-8',newline='') as stream:
                        for row in csv.DictReader(stream):
                            if row.get('status') not in ('ok','matched','future','healthy'):
                                scheduling.append({'report':filename,**row})
        summary=build_summary(latest,alerts,now,state,scheduling,changes,run)
        summary['runtime_snapshots']=runtime_snapshots
        summary['disabled_sources']=sorted(disabled_sources)
        summary['collection_errors']=failures
        errors=notifications(store,config,events,summary,now,deliver)
        summary['delivery_errors']=errors
        summary['pending_notifications']=store.db.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0]
        summary['events']=events
        cutoff=now-float(config.get('history_days',90))*86400
        store.db.execute('DELETE FROM samples WHERE observed<?',(cutoff,))
        store.db.execute("DELETE FROM outbox WHERE status='sent' AND created<?",(cutoff,))
        store.db.commit()
        (directory/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        dashboard(directory/'dashboard.html',summary)
        from notifications import render_summary
        plain,rich=render_summary(summary)
        (directory/'email-preview.html').write_text(rich,encoding='utf-8')
        (directory/'email-preview.txt').write_text(plain,encoding='utf-8')
        return summary
    finally: store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--state',help='Optional ConfigBackup state.json for backup/recovery freshness')
    parser.add_argument('--no-send',action='store_true',help='Evaluate and queue messages but do not deliver')
    args=parser.parse_args(argv)
    cfg=yaml.safe_load(Path(args.config).read_text()) or {}
    state=json.loads(Path(args.state).read_text()) if args.state else None
    summary=run_monitor(cfg.get('monitoring',cfg),args.output,state,deliver=not args.no_send)
    print(json.dumps({'status':summary['status'],'alerts':len(summary['alerts']),'pending_notifications':summary['pending_notifications']}))
    return 4 if summary['delivery_errors'] else 0

if __name__=='__main__': raise SystemExit(main())
