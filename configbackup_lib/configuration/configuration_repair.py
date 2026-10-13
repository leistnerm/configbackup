"""Repair malformed configuration only from an explicitly selected validated backup."""
import datetime
from pathlib import Path
import os
import shutil
import tempfile
import yaml


def load(path):
    path=Path(path)
    if not path.exists():return {}
    try:
        value=yaml.safe_load(path.read_text(encoding='utf-8-sig')) or {}
    except yaml.YAMLError as exc:
        mark=getattr(exc,'problem_mark',None)
        location=f' at line {mark.line+1}, column {mark.column+1}' if mark else ''
        raise ValueError('Invalid YAML'+location+'. Source text omitted to protect secrets. Use --repair-config or edit the indicated location.') from None
    if not isinstance(value,dict):raise ValueError('Configuration must be a mapping')
    return value


def repair(path,kind='backup',input_fn=input):
    from configbackup_lib.ui.configure import choice,ask,validate
    path=Path(path);backups=sorted(path.parent.glob(path.name+'.*.bak'),reverse=True)
    if not backups:print('No backups found. Edit the YAML manually; original file has not been changed.');return False
    selected=choice('Backup to validate and restore',[str(p) for p in backups]+['Cancel'],input_fn)
    if selected in (None,'Cancel'):return False
    candidate=Path(selected);value=load(candidate);validate(value,kind,path)
    if ask('Type RESTORE to preserve the current file and restore this backup',input_fn=input_fn)!='RESTORE':return False
    # Read before copying: the selected backup is never overwritten.
    payload=candidate.read_bytes()
    if path.exists():shutil.copy2(path,path.with_name(path.name+'.'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.invalid.bak'))
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.repair-')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(payload);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)
    return True
