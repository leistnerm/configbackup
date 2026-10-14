"""Repair malformed configuration only from an explicitly selected validated backup."""
import datetime
from pathlib import Path
import os
import tempfile
import yaml


def parse(payload):
    try:
        value=yaml.safe_load(payload.decode('utf-8-sig'))
        if value is None:value={}
    except yaml.YAMLError as exc:
        mark=getattr(exc,'problem_mark',None)
        location=f' at line {mark.line+1}, column {mark.column+1}' if mark else ''
        raise ValueError('Invalid YAML'+location+'. Source text omitted to protect secrets. Use --repair-config or edit the indicated location.') from None
    if not isinstance(value,dict):raise ValueError('Configuration must be a mapping')
    return value


def load(path):
    path=Path(path)
    if not path.exists():return {}
    return parse(path.read_bytes())


def repair(path,kind='backup',input_fn=input):
    from configbackup_lib.ui.configure import choice,ask,validate
    path=Path(path);backups=sorted(path.parent.glob(path.name+'.*.bak'),reverse=True)
    if not backups:print('No backups found. Edit the YAML manually; original file has not been changed.');return False
    selected=choice('Backup to validate and restore',[str(p) for p in backups]+['Cancel'],input_fn)
    if selected in (None,'Cancel'):return False
    candidate=Path(selected)
    from configbackup_lib.recovery.transaction_journal import unsafe_link
    if unsafe_link(path) or unsafe_link(candidate):raise ValueError('Configuration repair refuses symlink/junction files')
    payload=candidate.read_bytes();before=path.read_bytes() if path.exists() else None
    value=parse(payload);validate(value,kind,path)
    def unchanged():
        if unsafe_link(path) or unsafe_link(candidate) or candidate.read_bytes()!=payload or (path.read_bytes() if path.exists() else None)!=before:
            raise ValueError('Configuration or selected backup changed; inspect again before restoring')
    if ask('Type RESTORE to preserve the current file and restore this backup',input_fn=input_fn)!='RESTORE':return False
    unchanged()
    # Preserve the exact reviewed bytes; never reread an unvalidated candidate.
    if before is not None:
        backup=path.with_name(path.name+'.'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.invalid.bak')
        with backup.open('xb') as stream:
            os.chmod(backup,0o600);stream.write(before);stream.flush();os.fsync(stream.fileno())
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.repair-')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        unchanged()
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)
    return True
