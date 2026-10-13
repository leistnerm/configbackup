"""Preview/apply explicit state identity migrations and retain retirement evidence."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import yaml


def plan(state,source,target=None,destination=None,retire=False,from_destination=None):
    tasks=state.get('tasks',{})
    if source not in tasks:raise ValueError('Source state identity is missing')
    if retire and (target or destination):raise ValueError('Retirement cannot be combined with migration')
    if not retire and (not target or target!=source and target in tasks):raise ValueError('Choose an unused target identity')
    result=copy.deepcopy(state);record=result['tasks'][source]
    changes=[]
    if destination is not None:
        root=Path(destination)
        if root.is_absolute() or '..' in root.parts or '\\' in destination or ':' in destination:raise ValueError('Destination must be a relative portable logical prefix')
        if any(v.get('git_path') for v in record.get('files',{}).values()):raise ValueError('Git destination moves need a separately reviewed Git migration; no state changed')
        old=from_destination or record.get('destination')
        if not isinstance(old,str) or not old or old=='.':raise ValueError('Supply --from-destination for legacy state; an old prefix is never inferred from filenames')
        if Path(old).is_absolute() or '..' in Path(old).parts or '\\' in old or ':' in old:raise ValueError('Invalid old destination prefix')
        moved={}
        for key,value in record['files'].items():
            new=(root/Path(key).relative_to(old)).as_posix()
            if any(new.casefold()==other.casefold() for task_id,task in tasks.items() if task_id!=source for other in task.get('files',{})):
                raise ValueError('Destination collides with another task')
            moved[new]=value;changes.append({'before':key,'after':new})
        record['files']=moved
        record['destination']=destination
        for scope in record.get('scopes',{}).values():
            path=scope.get('logical_scope','')
            if path==old or path.startswith(old+'/'):scope['logical_scope']=destination+path[len(old):]
    if retire:record['retired']=True
    else:
        result['tasks'][target]=result['tasks'].pop(source)
        result['tasks'][target]['retired']=False
    return {'schema':1,'action':'retire' if retire else 'migrate','source':source,'target':target,'destination':destination,'changes':changes,'retained_files':len(record.get('files',{}))},result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--source',required=True);p.add_argument('--target');p.add_argument('--destination');p.add_argument('--from-destination');p.add_argument('--retire',action='store_true');p.add_argument('--apply',action='store_true');a=p.parse_args(argv)
    from configbackup_lib.engine.configbackup import BackupEngine,ConfigLoader,single_instance_lock,StateStore,atomic_json_write
    engine=BackupEngine(ConfigLoader(Path(a.config)).load(),dry_run=not a.apply)
    try:
        def operation():
            state=StateStore(engine.state.path).data
            report,updated=plan(state,a.source,a.target,a.destination,a.retire,a.from_destination)
            matches=[t for t in engine.cfg['tasks'] if t.get('state_id',t['name'])==(a.source if a.retire else a.target)]
            if a.destination is not None:
                if len(matches)!=1 or matches[0].get('destination')!=a.destination:raise ValueError('Save the matching target identity and destination before previewing a move')
                task=engine.resolve_task_runtime(matches[0]);source_path=task.get('source')
                if task['type'] not in ('file','directory') or not isinstance(source_path,str):raise ValueError('Destination migration requires one file or directory source')
                for change in report['changes']:
                    record=state['tasks'][a.source]['files'][change['before']]
                    versions=record.get('versions',[])
                    if not versions:raise ValueError('Migration requires retained source identity evidence')
                    original=Path(versions[-1]['source'])
                    expected=engine._logical_for_single(original,Path(a.destination)) if task['type']=='file' else engine._logical_for_source(original,Path(source_path),Path(a.destination))
                    if expected.as_posix()!=change['after']:raise ValueError('Migration prefix does not match task logical paths; review --from-destination')
            if a.apply:
                from configbackup_lib.recovery.transaction_journal import Journal
                journal=Journal(engine.internal_root/'transaction',[engine.root,engine.git_repo],engine.git_repo if engine._uses_git else None)
                if journal.manifest.exists():raise ValueError('Reconcile interrupted backup before migrating state')
                # Configuration must already describe the reviewed identity/destination.
                if a.retire and any(t.get('enabled',True) for t in matches):raise ValueError('Disable the task before retiring it')
                if not a.retire and len(matches)!=1:raise ValueError('Save exactly one task with the target state_id before applying')
                if a.destination is not None and matches[0].get('destination')!=a.destination:raise ValueError('Save matching destination in configuration first')
                journal.begin();journal.remember(engine.state.path);journal.remember(engine.state.backup_path)
                store=StateStore(engine.state.path);store.data=updated;store.save();journal.commit()
                report['applied']=True
            print(json.dumps(report,indent=2,sort_keys=True));return 0
        if a.apply:
            with single_instance_lock(engine.internal_root/'configbackup.lock'):return operation()
        return operation()
    finally:engine._close_logging()

if __name__=='__main__':raise SystemExit(main())
