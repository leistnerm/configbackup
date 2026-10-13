"""Bounded archive scrubbing and explicit, hash-bound current-version repairs."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import time
from transaction_journal import atomic


def safe_path(engine, version):
    path=engine._archive_state_path(version['path'])
    if any(p.is_symlink() for p in [path,*path.parents] if p==engine.root or engine.root in p.parents):
        raise ValueError('Archive symlink refused')
    if not path.resolve().is_relative_to(engine.root.resolve()):raise ValueError('Archive escapes root')
    return path


def check_version(engine, version):
    from configbackup import hash_file
    try:
        path=safe_path(engine,version)
        if not path.exists():return {'status':'missing','bytes':0}
        if not path.is_file():return {'status':'unreadable','bytes':0}
        size=path.stat().st_size
        actual=hash_file(path,engine.hash_algorithm)
        return {'status':'verified' if actual==version['hash'] else 'corrupt','bytes':size,'actual_hash':actual}
    except (OSError,ValueError,KeyError) as exc:return {'status':'unreadable','error_type':type(exc).__name__,'bytes':0}


def records(engine):
    for task,state in sorted(engine.state.data.get('tasks',{}).items()):
        for logical,record in sorted(state.get('files',{}).items()):
            versions=list(record.get('versions',[]))
            for generation in record.get('deleted_generations',[]):versions+=generation.get('versions',[])
            for version in sorted(versions,key=lambda v:v['path']):yield task,logical,version


def validate(config):
    if not isinstance(config,dict):raise ValueError('integrity must be a mapping')
    for key in ('enabled','required'):
        if key in config and not isinstance(config[key],bool):raise ValueError('integrity.'+key+' must be boolean')
    for key in ('max_files','max_bytes'):
        if key in config and (not isinstance(config[key],int) or isinstance(config[key],bool) or config[key]<1):raise ValueError(key+' must be a positive integer')
    for key in ('interval_hours','max_seconds','max_verification_age_hours'):
        if key in config and (not isinstance(config[key],(int,float)) or isinstance(config[key],bool) or not math.isfinite(config[key]) or config[key]<=0):raise ValueError(key+' must be positive and finite')
    return config


def load_evidence(engine):
    path=engine.internal_root/'integrity.json'
    if not path.exists():return {'schema':1,'files':{}}
    if path.is_symlink() or path.stat().st_size>64*1024*1024:raise ValueError('Unsafe integrity evidence')
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema')!=1 or not isinstance(value.get('files'),dict):raise ValueError('Invalid integrity evidence')
    return value


def scrub(engine,config=None,*,force=False,now=None):
    config=validate(config or {});now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('Scrub time must include a time zone')
    evidence=load_evidence(engine);rows=[];used=0;started=time.monotonic()
    all_records=list(records(engine));by_path={v['path']:v for _,_,v in all_records}
    evidence['files']={k:v for k,v in evidence['files'].items() if k in by_path and v.get('expected_hash')==by_path[k]['hash']}
    interval=config.get('interval_hours',168)*3600;maximum=config.get('max_files',500);budget=config.get('max_bytes',256*1024*1024)
    deferred=[]
    for task,logical,version in sorted(all_records,key=lambda r:(evidence['files'].get(r[2]['path'],{}).get('checked_at',''),r[2]['path'])):
        prior=evidence['files'].get(version['path'],{})
        if not force and prior.get('checked_at'):
            age=(now-dt.datetime.fromisoformat(prior['checked_at'].replace('Z','+00:00'))).total_seconds()
            if 0<=age<interval:continue
        try:size=safe_path(engine,version).stat().st_size
        except (OSError,ValueError):size=0
        if len(rows)>=maximum or used+size>budget or time.monotonic()-started>=config.get('max_seconds',30):
            deferred.append(version['path']);continue
        result=check_version(engine,version);used+=result['bytes']
        row={'task':task,'logical':logical,'path':version['path'],'expected_hash':version['hash'],'checked_at':now.isoformat(),**result}
        rows.append(row);evidence['files'][version['path']]=row
    issues=[r for r in evidence['files'].values() if r['status']!='verified']
    overdue=[];age_limit=config.get('max_verification_age_hours',config.get('interval_hours',168))*3600
    for _,_,version in all_records:
        prior=evidence['files'].get(version['path'],{})
        stamp=prior.get('checked_at')
        if not stamp or not 0<=(now-dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds()<=age_limit:
            overdue.append(version['path'])
    verified=[v for v in evidence['files'].values() if v.get('status')=='verified']
    report={'overdue':sorted(set(overdue)),'oldest_verification':min((v['checked_at'] for v in verified),default=None),'schema':1,'status':'failed' if issues else 'partial' if deferred else 'passed','checked':rows,'deferred':deferred,'known_issues':issues,'bytes_read':used,
            'limits':'Incremental hash verification only; not a database/service restore. Oversized files remain deferred until the byte budget is increased. A time budget is checked between files.'}
    evidence.update(last_attempt=now.isoformat(),last_status=report['status'],checked_count=len(rows),deferred_count=len(deferred),overdue_count=len(report['overdue']))
    if all_records and not issues and not overdue:evidence['last_full_verification']=now.isoformat()
    report['last_full_verification']=evidence.get('last_full_verification')
    if not engine.dry_run:atomic(engine.internal_root/'integrity.json',(json.dumps(evidence,sort_keys=True,indent=2)+'\n').encode())
    return report


def repair_plan(engine):
    from configbackup import hash_file
    rows=[]
    for task,state in sorted(engine.state.data.get('tasks',{}).items()):
        for logical,record in sorted(state.get('files',{}).items()):
            version=engine._latest_version(record.get('versions',[]))
            if not record.get('active',True) or not version:continue
            check=check_version(engine,version)
            if check['status']=='verified':continue
            source=Path(version.get('source',''));repairable=False
            try:
                repairable=source.is_absolute() and source.is_file() and not source.is_symlink() and not source.resolve().is_relative_to(engine.root.resolve()) and hash_file(source,engine.hash_algorithm)==version['hash']
            except OSError:pass
            rows.append({'task':task,'logical':logical,'version':version,'current':check,'source':str(source),'repairable':repairable})
    payload={'schema':1,'root':str(engine.root.resolve()),'hash_algorithm':engine.hash_algorithm,'repairs':rows,
             'state_sha256':hashlib.sha256(engine.state.path.read_bytes()).hexdigest(),
             'limits':'Only current active versions with an exact matching source hash can be repaired. Corrupt existing bytes are preserved in private quarantine. Historical missing versions need another recovery copy.'}
    payload['plan_id']=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
    return payload


def repair(engine,plan_id):
    from configbackup import atomic_copy,hash_file
    from transaction_journal import Journal
    plan=repair_plan(engine)
    if plan['plan_id']!=plan_id:raise ValueError('Repair plan changed; inspect again')
    journal=Journal(engine.internal_root/'transaction',[engine.root,engine.git_repo],engine.git_repo if engine._uses_git else None)
    journal.begin();rows=[]
    # One isolated durable transaction per file; earlier successful repairs stay
    # committed if a later source becomes unreadable.
    journal.commit()
    for row in plan['repairs']:
        if not row['repairable']:rows.append({**row,'result':'unavailable'});continue
        journal.begin()
        try:
            target=safe_path(engine,row['version']);journal.remember(target)
            if target.exists():
                name=hash_file(target,'sha256')
                quarantine=engine.internal_root/'integrity-quarantine'/name
                if quarantine.is_symlink() or quarantine.exists() and (not quarantine.is_file() or hash_file(quarantine,'sha256')!=name):raise ValueError('Invalid quarantine copy; repair refused')
                if not quarantine.exists():journal.remember(quarantine);atomic_copy(target,quarantine);quarantine.chmod(0o600)
                if hash_file(quarantine,'sha256')!=name:raise ValueError('Quarantine verification failed')
            atomic_copy(Path(row['source']),target)
            if check_version(engine,row['version'])['status']!='verified':raise ValueError('Repair source changed; original restored')
            journal.commit();rows.append({**row,'result':'repaired'})
        except Exception as exc:
            if journal.poisoned:raise
            journal.recover();rows.append({**row,'result':'failed','error_type':type(exc).__name__})
    return {'status':'passed' if all(r['result']=='repaired' for r in rows) else 'partial','repairs':rows,'plan_id':plan_id}


def main(argv=None):
    from configbackup import BackupEngine,ConfigLoader,StateStore,single_instance_lock
    from discovery_review import write_report
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['scrub','plan-repair','repair']);p.add_argument('--config',required=True);p.add_argument('--output',required=True);p.add_argument('--force',action='store_true');p.add_argument('--plan-id');p.add_argument('--max-bytes',type=int);p.add_argument('--max-files',type=int)
    a=p.parse_args(argv);e=BackupEngine(ConfigLoader(Path(a.config)).load())
    try:
        with single_instance_lock(e.internal_root/'configbackup.lock'):
            e.state=StateStore(e.state.path)
            if (e.internal_root/'transaction/pending.json').exists():raise ValueError('Reconcile interrupted run first')
            settings=dict(e.cfg.get('integrity',{}))
            for k in ('max_bytes','max_files'):
                if getattr(a,k) is not None:settings[k]=getattr(a,k)
            value=scrub(e,settings,force=a.force) if a.action=='scrub' else repair_plan(e) if a.action=='plan-repair' else repair(e,a.plan_id)
        write_report(value,a.output,'Archive integrity');print(json.dumps(value,indent=2));return 6 if value.get('status') in ('partial','failed') else 0
    finally:e._close_logging()

if __name__=='__main__':raise SystemExit(main())
