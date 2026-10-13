"""Independent collection, archive, Git and verification evidence per scope."""
import argparse
import datetime as dt
import json
from pathlib import Path


def record_publication(engine):
    from configbackup import iso_now
    publication=engine.state.data.get('publication',{});commit=publication.get('local_commit')
    if not commit:return
    for task in engine.cfg['tasks']:
        if task.get('storage') not in ('both','git') or engine.results.get(task['name']) is None:continue
        if engine.results[task['name']].status not in ('success','partial'):continue
        state=engine.state.task(task['name']);scopes=engine._retention_scopes.get(task['name'])
        for logical,record in state.get('files',{}).items():
            if scopes is not None and not any(logical==s or logical.startswith(s+'/') for s in scopes):continue
            if record.get('git_hash'):
                previous=record.get('publication',{})
                same=previous.get('hash')==record['git_hash']
                record['publication']={'hash':record['git_hash'],'commit':commit,
                    'committed_at':previous.get('committed_at') if same and previous.get('committed_at') else iso_now(),
                    'published_at':(previous.get('published_at') if same and previous.get('published_at') else iso_now()) if publication.get('pushed_commit')==commit else (previous.get('published_at') if same else None),
                    'last_checked_at':iso_now()}


def rows(state,evidence=None):
    evidence=evidence or {};result=[]
    for task,item in sorted(state.get('tasks',{}).items()):
        scopes=item.get('scopes') or {'task':{'logical_scope':'','status':item.get('last_status','unknown'),'last_success':item.get('last_success'),'source_collected_at':item.get('last_collection')}}
        for scope,details in sorted(scopes.items()):
            prefix=details.get('logical_scope','')
            files=[r for k,r in sorted(item.get('files',{}).items()) if (not prefix or k==prefix or k.startswith(prefix+'/')) and r.get('active',True)]
            latest=[max(enumerate(r['versions']),key=lambda pair:(dt.datetime.fromisoformat(pair[1].get('created','0001-01-01T00:00:00+00:00').replace('Z','+00:00')),pair[0]))[1] for r in files if r.get('versions')]
            checks=[evidence.get('files',{}).get(v['path'],{}) for v in latest]
            valid=[c for c,v in zip(checks,latest) if c.get('expected_hash')==v['hash'] and c.get('status')=='verified']
            git=[r for r in files if r.get('git_hash')];published=[r.get('publication',{}) for r in git]
            all_commit=bool(git) and all(p.get('hash')==r['git_hash'] and p.get('committed_at') for p,r in zip(published,git))
            all_push=all_commit and all(p.get('published_at') for p in published)
            restores=state.get('recovery',{}).get('restored_versions',{})
            restored=[restores.get(v['path'],{}) for v in latest]
            restore_ok=bool(latest) and all(r.get('hash')==v['hash'] for r,v in zip(restored,latest))
            result.append({'task':task,'scope':scope,'collection_status':details.get('status','unknown'),
                'collected_at':details.get('source_collected_at'),'archived_at':details.get('last_success'),
                'active_files':len(files),'filesystem_versions':len(latest),
                'git_status':'pending' if state.get('publication',{}).get('pending') and git else 'published' if all_push else 'committed_locally' if all_commit else 'unknown' if git else 'not_recorded',
                'git_committed_at':min(p['committed_at'] for p in published) if all_commit else None,
                'git_published_at':min(p['published_at'] for p in published) if all_push else None,
                'integrity_status':'verified' if latest and len(valid)==len(latest) else 'unknown_or_incomplete',
                'oldest_verification':min(c['checked_at'] for c in valid) if latest and len(valid)==len(latest) else None,
                'file_restore_status':'verified' if restore_ok else 'unknown',
                'oldest_file_restore':min(r['at'] for r in restored) if restore_ok else None,
                'live_database_service_restore':'unknown; requires a separate live acceptance test'})
    return result


def main(argv=None):
    from configbackup import ConfigLoader,BackupEngine,StateStore,single_instance_lock
    from archive_integrity import load_evidence
    from discovery_review import write_report
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--output',required=True);a=p.parse_args(argv)
    e=BackupEngine(ConfigLoader(Path(a.config)).load(),dry_run=True)
    try:
        with single_instance_lock(e.internal_root/'configbackup.lock'):
            e.state=StateStore(e.state.path,dry_run=True)
            value={'schema':1,'scopes':rows(e.state.data,load_evidence(e)),'limits':'Saved evidence only; dates are not new integrity or live restore checks. Verification becomes stale as time passes.'}
        write_report(value,a.output,'Recovery status by scope');print(json.dumps(value,indent=2));return 0
    finally:e._close_logging()

if __name__=='__main__':raise SystemExit(main())
