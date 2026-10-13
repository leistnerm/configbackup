"""Inspect interrupted runs and apply a hash-bound, unchanged-HEAD undo plan."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from transaction_journal import Journal, head, sha


def inspect(journal):
    if not journal.manifest.exists():
        orphans=sorted(p.name for p in journal.directory.glob('*') if p.is_file())
        return {'status':'orphaned' if orphans else 'clean','can_reconcile':False,'orphan_files':orphans,
                'guidance':'Preserve orphan files for manual review; automatic deletion is disabled.' if orphans else 'No pending transaction.'}
    try:
        data=journal.read()
        if data.get('schema') not in (1,2):raise ValueError('Unknown journal schema')
        if data.get('status')=='pending':journal.validate_pending(data)
        elif data.get('status')!='committed' or data.get('repo')!=journal.repo:raise ValueError('Invalid transaction')
        current_head=head(journal.repo)
        rows=[]
        for row in data['entries']:
            path=journal.allowed(row['path'])
            rows.append({'path':str(path),'action':'restore before image' if row['backup'] else 'remove newly created file',
                         'before_sha256':row.get('sha256'),'current_sha256':sha(path) if path.is_file() else None})
        allowed=data['status']=='committed' or current_head==data.get('head')
        value={'status':data['status'],'can_reconcile':allowed,'recorded_head':data.get('head'),'current_head':current_head,'files':rows,
               'guidance':'Reconcile retries committed cleanup or restores all before images; no published commit is undone.' if allowed else
               'Git HEAD changed. Preserve archive, state, Git checkout and journal together. Compare the recorded/current commits and state before choosing a manual repair. This tool will not discard the journal or rewrite Git history.'}
        # Bind approval to exact metadata and current target bytes, not merely a timestamp.
        value['plan_id']=hashlib.sha256(json.dumps({'plan':value,'journal':data},sort_keys=True).encode()).hexdigest()
        return value
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {'status':'invalid','can_reconcile':False,'reason':str(exc),'guidance':'Preserve all files. Repair missing/corrupt evidence before retrying inspection.'}


def reconcile(journal,plan_id):
    current=inspect(journal)
    if not current.get('can_reconcile') or current.get('plan_id')!=plan_id:
        raise ValueError('Plan changed or requires manual reconciliation; inspect again')
    return journal.recover()


def main(argv=None):
    from configbackup import BackupEngine,ConfigLoader,single_instance_lock
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--output',required=True);p.add_argument('--apply-plan',help='Exact plan_id from an inspected report; otherwise read-only')
    a=p.parse_args(argv);engine=BackupEngine(ConfigLoader(Path(a.config)).load(),dry_run=True)
    try:
        with single_instance_lock(engine.internal_root/'configbackup.lock'):
            journal=Journal(engine.internal_root/'transaction',[engine.root,engine.git_repo],engine.git_repo if engine._uses_git else None)
            result=reconcile(journal,a.apply_plan) if a.apply_plan else inspect(journal)
        from discovery_review import write_report
        write_report(result,a.output,'Interrupted run recovery');print(json.dumps(result,indent=2))
        return 6 if result.get('status') in ('invalid','orphaned') or result.get('can_reconcile') is False and result.get('status')!='clean' else 0
    finally:engine._close_logging()

if __name__=='__main__':raise SystemExit(main())
