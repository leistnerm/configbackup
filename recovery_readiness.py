"""Read-only freshness targets and verified archive copies, not live service restores."""
import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import math
from pathlib import Path
import time


def validate(config):
    if not isinstance(config,dict):raise ValueError('recovery_readiness must be a mapping')
    for key in ('enabled','required'):
        if key in config and not isinstance(config[key],bool):raise ValueError(key+' must be boolean')
    roots=config.get('copy_roots',[])
    if not isinstance(roots,list) or any(not isinstance(r,str) or not Path(r).is_absolute() for r in roots):raise ValueError('copy_roots must be absolute archive mirror roots')
    if len(roots)>10:raise ValueError('At most ten additional copies')
    targets=config.get('targets',[])
    if not isinstance(targets,list) or len(targets)>1000:raise ValueError('Invalid recovery targets')
    for target in targets:
        if not isinstance(target,dict) or any(not isinstance(target.get(k,'*'),str) for k in ('task','scope')):raise ValueError('Invalid recovery target selector')
        for key in ('max_backup_age_hours','max_verification_age_hours'):
            v=target.get(key,24)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:raise ValueError('Invalid '+key)
        count=target.get('min_verified_copies',1)
        if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=11:raise ValueError('min_verified_copies must be 1..11')
    for key,default in [('max_bytes',256*1024*1024),('max_seconds',30)]:
        v=config.get(key,default)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:raise ValueError('Invalid '+key)
    return config


def evaluate(engine,config,*,now=None):
    from recovery_status import rows
    from archive_integrity import load_evidence
    validate(config);now=now or dt.datetime.now(dt.timezone.utc)
    scopes=rows(engine.state.data,load_evidence(engine));started=time.monotonic();used=0;cache={}
    roots=list(dict.fromkeys([engine.root.resolve(),*(Path(r).resolve() for r in config.get('copy_roots',[]))]))
    # Reject symlink aliases before resolving: distinct path names are not distinct copies.
    unsafe={Path(r).resolve() for r in config.get('copy_roots',[]) if any(p.is_symlink() for p in [Path(r),*Path(r).parents])}
    def age(stamp):
        try:
            delta=(now-dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))).total_seconds()/3600
            return delta if delta>=0 else None
        except (ValueError,TypeError,AttributeError):return None
    def verify(root,version):
        nonlocal used
        key=(str(root),version['path'],version['hash'])
        if key in cache:return cache[key]
        status='unavailable'
        try:
            rel=Path(version['path']);path=root/rel
            if root in unsafe or rel.is_absolute() or '..' in rel.parts or any(p.is_symlink() for p in [path,*path.parents]):raise ValueError('Unsafe mirror path')
            if not path.is_file():raise ValueError('Missing archive copy')
            size=path.stat().st_size
            if used+size>config.get('max_bytes',256*1024*1024) or time.monotonic()-started>config.get('max_seconds',30):status='deferred'
            else:
                used+=size;digest=hashlib.new(engine.hash_algorithm)
                with path.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
                status='verified' if digest.hexdigest()==version['hash'] else 'corrupt'
        except (OSError,ValueError,KeyError):pass
        cache[key]=status;return status
    results=[]
    for target in config.get('targets',[]):
        matched=[r for r in scopes if fnmatch.fnmatchcase(r['task'],target.get('task','*')) and fnmatch.fnmatchcase(r['scope'],target.get('scope','*'))]
        if not matched:results.append({'task':target.get('task','*'),'scope':target.get('scope','*'),'status':'unmet','reasons':['No matching archived scope']});continue
        for row in matched:
            reasons=[];backup_age=age(row['archived_at']);verification_age=age(row['oldest_verification'])
            if backup_age is None or backup_age>target.get('max_backup_age_hours',24):reasons.append('Backup age is unknown, future-dated or exceeds target')
            if verification_age is None or verification_age>target.get('max_verification_age_hours',168):reasons.append('Integrity verification is unknown, future-dated or exceeds target')
            task=engine.state.data['tasks'][row['task']];prefix=task.get('scopes',{}).get(row['scope'],{}).get('logical_scope','')
            files=[r for k,r in task.get('files',{}).items() if r.get('active',True) and (not prefix or k==prefix or k.startswith(prefix+'/'))]
            versions=[engine._latest_version(r.get('versions',[])) for r in files]
            copies=[]
            for root in roots:
                checks=[verify(root,v) for v in versions if v]
                complete=bool(files) and len(checks)==len(files) and all(c=='verified' for c in checks)
                copies.append({'root':str(root),'status':'verified' if complete else 'incomplete','results':{s:checks.count(s) for s in sorted(set(checks))}})
            count=sum(c['status']=='verified' for c in copies)
            if count<target.get('min_verified_copies',1):reasons.append('Too few complete hash-verified archive copies')
            if row['collection_status'] not in ('success','complete','not_due'):reasons.append('Latest collection is not successful: '+row['collection_status'])
            results.append({'task':row['task'],'scope':row['scope'],'status':'unmet' if reasons else 'met','reasons':reasons,'backup_age_hours':backup_age,'verification_age_hours':verification_age,'verified_copies':count,'copies':copies})
    return {'schema':1,'status':'met' if results and all(r['status']=='met' for r in results) else 'unmet','targets':results,'bytes_read':used,
        'limits':'Configured distinct roots are not proof of independent hardware/offsite storage. Read-only hash verification does not test database/service restoration. The primary archive counts as one copy. Extra copies must mirror archive-relative paths. Budgets are shared; deferred checks cannot satisfy a target.'}


def wizard(config,input_fn=input):
    from configure import boolean,ask
    import copy
    value=copy.deepcopy(config.get('recovery_readiness',{}))
    value['enabled']=boolean('Evaluate recovery targets after backup',value.get('enabled',False),input_fn)
    value['required']=boolean('Fail overall run when recovery targets are unmet',value.get('required',False),input_fn)
    task=ask('Task pattern for a new target (blank keeps existing targets)',input_fn=input_fn)
    if task:
        target={'task':task,'scope':ask('Scope pattern','*',input_fn)}
        for key,default in [('max_backup_age_hours',24),('max_verification_age_hours',168),('min_verified_copies',1)]:target[key]=int(ask(key,default,input_fn))
        value.setdefault('targets',[]).append(target)
    root=ask('Additional archive mirror root (blank keeps existing roots)',input_fn=input_fn)
    if root:value.setdefault('copy_roots',[]).append(root)
    validate(value);config['recovery_readiness']=value


def main(argv=None):
    from configbackup import ConfigLoader,BackupEngine,StateStore,single_instance_lock
    from discovery_review import write_report
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--output',required=True);a=p.parse_args(argv)
    engine=BackupEngine(ConfigLoader(Path(a.config)).load(),dry_run=True)
    try:
        with single_instance_lock(engine.internal_root/'configbackup.lock'):
            engine.state=StateStore(engine.state.path,dry_run=True)
            value=evaluate(engine,engine.cfg.get('recovery_readiness',{}))
        write_report(value,a.output,'Recovery readiness targets');print(json.dumps(value,indent=2));return 0 if value['status']=='met' else 6
    finally:engine._close_logging()

if __name__=='__main__':raise SystemExit(main())
