"""Inspect and regenerate registered launchers with retained rollback copies."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from configbackup_lib.recovery.recovery_assets import registry, register_launcher
from configbackup_lib.recovery.transaction_journal import unsafe_link


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inventory(config_path, config):
    latest={}
    for row in registry(config_path)['launchers']:
        if isinstance(row,dict) and row.get('directory'):latest[row['directory']]=row
    result=[]
    for directory,row in latest.items():
        reasons=[];hashes=row.get('file_hashes') or {}
        if not hashes:reasons.append('No recorded file hashes; regeneration requires review')
        for name,expected in hashes.items():
            path=Path(directory)/name
            if Path(name).name!=name or unsafe_link(path) or not path.is_file():reasons.append('Missing/unsafe file: '+name)
            elif digest(path)!=expected:reasons.append('Modified file: '+name)
        if row.get('config_sha256')!= (digest(config_path) if Path(config_path).is_file() else None):reasons.append('Saved configuration changed')
        if row.get('execution_policy','inherit')!=(config.get('options') or {}).get('powershell_execution_policy','inherit'):reasons.append('Execution policy changed')
        result.append({'directory':directory,'status':'review' if reasons else 'current','reasons':reasons,'metadata':row})
    return result


def regenerate(config_path, config, metadata):
    from configbackup_lib.engine.startup_launcher import generate
    from configbackup_lib.configuration.configuration_repair import load
    if load(config_path)!=config:raise ValueError('Save configuration before regenerating a launcher')
    target=Path(metadata['directory']).absolute()
    if not target.is_dir() or unsafe_link(target) or unsafe_link(target.parent):raise ValueError('Launcher directory must be a regular directory')
    for key in ('platform','python','provider'):
        if key not in metadata:raise ValueError('Legacy launcher lacks regeneration metadata; generate a new launcher')
    lock=target/'.launcher-update.lock'
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    backup=target/('backup-'+uuid.uuid4().hex);backup.mkdir(mode=0o700)
    installed=[];old={}
    try:
        with tempfile.TemporaryDirectory(prefix='.launcher-stage-',dir=target.parent) as temp:
            args={k:metadata[k] for k in ('vault','account','mode','task','report_directory','history','shell') if k in metadata}
            policy=(config.get('options') or {}).get('powershell_execution_policy','inherit')
            files=generate(config_path,temp,metadata['platform'],metadata['python'],metadata['provider'],metadata.get('secret_references',[]),execution_policy=policy,register=False,**args)
            for source in files:
                dest=target/source.name
                if unsafe_link(dest) or (dest.exists() and not dest.is_file()):raise ValueError('Unsafe launcher target')
                old[dest]=dest.exists()
                if dest.exists():shutil.copy2(dest,backup/dest.name)
            for source in files:
                dest=target/source.name;os.replace(source,dest);installed.append(dest)
            updated=dict(metadata,execution_policy=policy,config_sha256=digest(config_path),file_hashes={p.name:digest(p) for p in installed})
            register_launcher(config_path,installed,updated)
        return str(backup)
    except BaseException:
        for dest in reversed(installed):
            if old[dest]:shutil.copy2(backup/dest.name,dest)
            else:dest.unlink(missing_ok=True)
        raise
    finally:lock.unlink(missing_ok=True)


def wizard(config,path,input_fn=input):
    from configbackup_lib.ui.configure import choice,ask
    rows=inventory(path,config)
    if not rows:print('No registered launchers. Use Generate startup launcher.');return
    labels=[r['directory']+' — '+r['status'] for r in rows]
    selected=choice('Registered launchers',labels+['Back'],input_fn)
    if selected in (None,'Back'):return
    row=rows[labels.index(selected)]
    print(json.dumps({k:v for k,v in row.items() if k!='metadata'},indent=2))
    print('Regeneration replaces generated files, retains a backup, and uses the selected policy. Save configuration first.')
    if ask('Type REGENERATE to replace this launcher (anything else cancels)',input_fn=input_fn)=='REGENERATE':
        print('Prior launcher saved in '+regenerate(path,config,row['metadata']))
