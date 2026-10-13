"""One retention decision path for previews and independently reversible scopes."""
from __future__ import annotations
import copy
from pathlib import Path
import tempfile
import shutil


def enabled(task,tasks):
    by_name={t['name']:t for t in tasks}
    return task.get('enabled',True) and all(enabled(by_name[n],tasks) for n in task.get('depends_on',[]))


def plan(engine, task, state, scopes=None):
    projected=copy.deepcopy(state);actions=[]
    engine._retention_plan_actions=actions
    try:engine._apply_task_retention(task,projected,scopes)
    finally:engine._retention_plan_actions=None
    owners={}
    for key,record in state.get('files',{}).items():
        for version in record.get('versions',[]):owners[version['path']]=key
        for generation in record.get('deleted_generations',[]):
            for version in generation.get('versions',[]):owners[version['path']]=key
            if generation.get('metadata_path'):owners[generation['metadata_path']]=key
    for action in actions:
        action['file']=owners[action['path']]
        action['scope']=next((s for s in (scopes or []) if action['file']==s or action['file'].startswith(s+'/')),action['file'])
    return projected,actions


def apply(engine,task,state,scopes=None):
    from configbackup_lib.engine.configbackup import atomic_copy
    projected,actions=plan(engine,task,state,scopes)
    if engine.dry_run:
        for action in actions:engine.logger.info('DRY-RUN: retention would remove %s',action['path'])
        return []
    errors=[]
    for scope in sorted({a['scope'] for a in actions}):
        group=[a for a in actions if a['scope']==scope];keys={a['file'] for a in group}
        previous_journal=getattr(engine,'_file_journal',{});previous_dir=getattr(engine,'_journal_directory',None)
        before_stats=copy.deepcopy(engine.stats)
        with tempfile.TemporaryDirectory(prefix='configbackup-retention-') as folder:
            engine._file_journal={};engine._journal_directory=Path(folder)
            try:
                validate_survivors(engine, state, projected, keys)
                for action in group:
                    if action['kind']=='version':engine._delete_version_file(action['version'])
                    else:
                        path=engine._archive_state_path(action['path']);engine._remember_file(path);path.unlink(missing_ok=True)
                for key in keys:state['files'][key]=projected['files'][key]
            except Exception as exc:
                # Failure to undo is fatal: keep the durable run journal for startup.
                try:
                    for path,saved in reversed(list(engine._file_journal.items())):
                        if saved is None:path.unlink(missing_ok=True)
                        else:atomic_copy(saved,path)
                except BaseException:
                    if getattr(engine,'_durable_journal',None):engine._durable_journal.poisoned=True
                    raise
                engine.stats=before_stats
                errors.append({'task':task['name'],'scope':scope,'error':str(exc)})
                engine.logger.error('Retention scope %s rolled back: %s',scope,exc)
            finally:engine._file_journal=previous_journal;engine._journal_directory=previous_dir
    return errors


def validate_survivors(engine,state,projected,keys):
    from configbackup_lib.recovery.archive_integrity import check_version
    for key in keys:
        record=projected['files'][key]
        versions=list(record.get('versions',[]))
        for generation in record.get('deleted_generations',[]):versions+=generation.get('versions',[])
        for version in versions:
            if check_version(engine,version)['status']!='verified':
                raise ValueError('Unverified retention survivor: '+version['path']+'; whole scope preserved')


def report(engine,incoming_bytes=0,recovery_bytes=0):
    """Read-only state preview; sizes are explicit estimates, not capacity guarantees."""
    if incoming_bytes<0 or recovery_bytes<0:raise ValueError('Size estimates must be nonnegative')
    pending=(engine.internal_root/'transaction/pending.json').exists()
    rows=[];delete_bytes=0
    tasks={t.get('state_id',t['name']):t for t in engine.cfg['tasks']}
    for name,state in sorted(engine.state.data.get('tasks',{}).items()):
        task=tasks.get(name);reason=None;scopes=None
        if pending:reason='pending transaction'
        elif not task:reason='task no longer configured'
        elif state.get('retired'):reason='retired task'
        elif not enabled(task,engine.cfg['tasks']):reason='disabled task or dependency'
        if state.get('scopes'):scopes=[s['logical_scope'] for s in state['scopes'].values() if s.get('status')=='complete']
        projected,actions=(state,[]) if reason else plan(engine,engine.resolve_task_runtime(task),state,scopes)
        unsafe={}
        for scope in sorted({a['scope'] for a in actions}):
            keys={a['file'] for a in actions if a['scope']==scope}
            try:validate_survivors(engine,state,projected,keys)
            except (OSError,ValueError) as exc:
                for key in keys:unsafe[key]=str(exc)
        deletions={a['path'] for a in actions}
        for key,record in sorted(state.get('files',{}).items()):
            protected=reason or unsafe.get(key) or ('scope not complete' if scopes is not None and not any(key==s or key.startswith(s+'/') for s in scopes) else '')
            versions=list(record.get('versions',[]))
            for generation in record.get('deleted_generations',[]):
                versions+=generation.get('versions',[])
                if generation.get('metadata_path'):versions.append({'path':generation['metadata_path'],'size':0})
            for version in versions:
                path=engine._archive_state_path(version['path']);size=path.stat().st_size if path.is_file() else 0
                action='protected' if protected else 'delete' if version['path'] in deletions else 'keep'
                rows.append({'task':name,'file':key,'path':version['path'],'action':action,'bytes':size,'present':path.is_file(),'reason':protected})
                if action=='delete':delete_bytes+=size
    staging=sum(p.stat().st_size for p in engine.staging_root.rglob('*') if p.is_file() and not p.is_symlink()) if engine.staging_root.exists() else 0
    volumes=[]
    for purpose,path in [('archive / undo',engine.root),('collector staging',engine.staging_root)]:
        probe=path
        while not probe.exists():probe=probe.parent
        volumes.append({'purpose':purpose,'path':str(path),'available_bytes':shutil.disk_usage(probe).free})
    return {'schema':1,'status':'blocked' if pending else 'preview','items':rows,'space':{
        'retention_reclaim_bytes':delete_bytes,'retention_undo_bytes':delete_bytes,
        'current_staging_bytes':staging,'incoming_estimate_bytes':incoming_bytes,'recovery_export_estimate_bytes':recovery_bytes,
        'additional_peak_estimate_bytes':delete_bytes+3*incoming_bytes+2*recovery_bytes},'volumes':volumes,
        'limits':'No files are deleted. Scope eligibility uses the most recent saved collection evidence; a future run can change the plan. Peak estimate budgets incoming staging, archive and undo, plus recovery staging/copy. Git growth, compression, concurrent writers, filesystem overhead and unknown collector sizes are excluded. Supply realistic estimates; zero means unspecified. Volume free space is observed separately and must not be summed.'}


def main(argv=None):
    import argparse,json
    from configbackup_lib.engine.configbackup import BackupEngine,ConfigLoader,single_instance_lock
    from configbackup_lib.engine.discovery_review import write_report
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--output',required=True);p.add_argument('--incoming-bytes',type=int,default=0);p.add_argument('--recovery-bytes',type=int,default=0)
    a=p.parse_args(argv);engine=BackupEngine(ConfigLoader(Path(a.config)).load(),dry_run=True)
    try:
        with single_instance_lock(engine.internal_root/'configbackup.lock'):value=report(engine,a.incoming_bytes,a.recovery_bytes)
        write_report(value,a.output,'Retention and space planning');print(json.dumps(value,indent=2));return 6 if value['status']=='blocked' else 0
    finally:engine._close_logging()

if __name__=='__main__':raise SystemExit(main())
