"""Recovery assessment and explicit evidence management under the run lock."""
import json
import time
import shutil
from pathlib import Path
from configbackup_lib.recovery.transaction_journal import for_engine, unsafe_link, atomic
from configbackup_lib.recovery.journal_recovery import inspect, reset


def evidence(directory):
    rows=[]
    for path in sorted(Path(directory).glob('transaction-abandoned-*')):
        if unsafe_link(path) or not path.is_dir():continue
        files=list(path.rglob('*'))
        rows.append({'path':str(path),'bytes':sum(p.stat().st_size for p in files if p.is_file() and not unsafe_link(p)),
                     'age_days':round((time.time()-path.stat().st_mtime)/86400,1),
                     'safe':not any(unsafe_link(p) for p in files) and (path/'reset.json').is_file()})
    return rows


def purge(directory, selected):
    rows={r['path']:r for r in evidence(directory)}
    if selected not in rows or not rows[selected]['safe']:raise ValueError('Evidence changed or is unsafe; inspect again')
    path=Path(selected)
    record=json.loads((path/'reset.json').read_text())
    if record.get('status')!='abandoned':raise ValueError('Unknown evidence directory')
    atomic(Path(directory)/('purged-'+path.name+'.json'),json.dumps({'evidence':selected,'bytes':rows[selected]['bytes'],'purged_at':time.time()}).encode())
    shutil.rmtree(path)


def wizard(config,path,input_fn=input):
    from configbackup_lib.ui.configure import choice,ask
    from configbackup_lib.engine.configbackup import ConfigLoader,BackupEngine,single_instance_lock
    engine=BackupEngine(ConfigLoader(Path(path))._resolve(config),dry_run=True)
    try:
        with single_instance_lock(engine.internal_root/'configbackup.lock'):
            journal=for_engine(engine)
            print(json.dumps(inspect(journal),indent=2))
            rows=evidence(engine.internal_root);print(json.dumps(rows,indent=2))
            action=choice('Recovery actions',['Back','Preserve evidence and reset','Remove reviewed reset evidence'],input_fn)
            if action=='Preserve evidence and reset':
                print('Reset does not repair incomplete archive/state changes. Rerun collection and review its result.')
                if ask('Type RESET to acknowledge an incomplete run',input_fn=input_fn)=='RESET':
                    print(json.dumps(reset(journal,[engine.state.path,engine.state.backup_path]),indent=2))
            elif action=='Remove reviewed reset evidence' and rows:
                selected=choice('Evidence to remove',[r['path'] for r in rows if r['safe']]+['Back'],input_fn)
                if selected not in (None,'Back') and ask('Type PURGE to permanently remove only this evidence',input_fn=input_fn)=='PURGE':purge(engine.internal_root,selected)
    finally:engine._close_logging()
