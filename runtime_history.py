"""Opt-in, filesystem-only runtime snapshots with independent source retention."""
from __future__ import annotations
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid
from completeness import digest


def validate(specs):
    seen=set()
    for spec in specs:
        key=spec.get('id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key) or key in seen:raise ValueError('Runtime snapshot IDs must be unique safe names')
        seen.add(key)
        if not spec.get('source'):raise ValueError('Runtime snapshot source required')
        if not isinstance(spec.get('enabled',True),bool):raise ValueError('Runtime snapshot enabled must be boolean')
        for name,default in [('keep_days',14),('interval_minutes',60),('max_file_bytes',50*1024**2),('max_snapshot_bytes',200*1024**2)]:
            value=spec.get(name,default)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0<value<float('inf'):raise ValueError(name+' must be positive and finite')
        for name in ('include','exclude'):
            if not isinstance(spec.get(name,[]),list) or any(not isinstance(p,str) for p in spec.get(name,[])):raise ValueError(name+' must be a list of patterns')


def capture(specs,directory,now=None):
    validate(specs);now=time.time() if now is None else float(now);base=Path(directory);results=[]
    for spec in specs:
        if spec.get('enabled',True) is False:
            results.append({'id':spec['id'],'status':'disabled'});continue
        root=base/spec['id'];lock=root/'.lock';stage=None;locked=False
        try:
            if base.is_symlink() or root.is_symlink():raise ValueError('Runtime history source directory must not be a symlink')
            root.mkdir(parents=True,exist_ok=True)
            fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd);locked=True
            index=root/'index.json'
            if index.is_symlink() or index.with_suffix('.tmp').is_symlink():raise ValueError('Runtime history index must not be a symlink')
            records=json.loads(index.read_text()) if index.exists() else []
            for record in records:
                if not re.fullmatch(r'\d+-[0-9a-f]{32}',record['directory']):raise ValueError('Unsafe runtime snapshot index')
            if records and now-max(r['captured_at'] for r in records)<spec.get('interval_minutes',60)*60:
                results.append({'id':spec['id'],'status':'interval_not_due'});continue
            source=Path(spec['source'])
            if source.is_symlink() or not source.is_dir():raise ValueError('Runtime source must be an existing real directory')
            if root.resolve().is_relative_to(source.resolve()):raise ValueError('Runtime history destination cannot be inside its source')
            files={};sizes={}
            for path in sorted(source.rglob('*')):
                name=path.relative_to(source).as_posix()
                if not any(fnmatch.fnmatchcase(name,p) for p in spec.get('include',['*'])) or any(fnmatch.fnmatchcase(name,p) for p in spec.get('exclude',[])):continue
                if path.is_symlink():raise ValueError('Runtime snapshot symlinks are not supported')
                if not path.is_file():continue
                if name=='snapshot-manifest.json':raise ValueError('Reserved runtime snapshot filename')
                size=path.stat().st_size
                if size>spec.get('max_file_bytes',50*1024**2):raise ValueError('Runtime file size limit exceeded')
                sizes[name]=size;files[name]=digest(path)
            if not files:raise ValueError('No selected runtime files; previous history preserved')
            if sum(sizes.values())>spec.get('max_snapshot_bytes',200*1024**2):raise ValueError('Runtime snapshot size limit exceeded')
            fingerprint=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
            name=str(int(now))+'-'+uuid.uuid4().hex;stage=root/('.'+name);stage.mkdir(mode=0o700)
            for relative,expected in files.items():
                src=source/relative;target=stage/relative
                if src.is_symlink() or not src.resolve().is_relative_to(source.resolve()):raise ValueError('Runtime source changed during capture')
                target.parent.mkdir(parents=True,exist_ok=True)
                copied=0
                with src.open('rb') as inp,target.open('xb') as stream:
                    while True:
                        block=inp.read(1024*1024)
                        if not block:break
                        copied+=len(block)
                        if copied>spec.get('max_file_bytes',50*1024**2):raise ValueError('Runtime file grew beyond its size limit')
                        stream.write(block)
                target.chmod(0o600)
                if copied!=sizes[relative] or digest(target)!=expected:raise ValueError('Runtime source changed during capture')
            record={'directory':name,'captured_at':now,'sha256':fingerprint,'files':files,'bytes':sum(sizes.values())}
            (stage/'snapshot-manifest.json').write_text(json.dumps(record,sort_keys=True,indent=2)+'\n')
            stage.rename(root/name);stage=None;records.append(record)
            # Publish the new snapshot before retention; prune only directories
            # named in this source's managed index, never caller-supplied paths.
            temporary=index.with_suffix('.tmp');temporary.write_text(json.dumps(records,indent=2)+'\n');temporary.replace(index)
            keep=[]
            for old in records:
                target=root/old['directory']
                if old is not record and old['captured_at']<now-spec.get('keep_days',14)*86400:
                    if target.is_symlink():raise ValueError('Refusing to prune a linked snapshot directory')
                    if target.exists():shutil.rmtree(target)
                else:keep.append(old)
            temporary.write_text(json.dumps(keep,indent=2)+'\n');temporary.replace(index)
            results.append({'id':spec['id'],'status':'captured','directory':str(root/name),'files':len(files),'retained':len(keep)})
        except Exception as exc:results.append({'id':spec['id'],'status':'failed','error':str(exc)})
        finally:
            if stage is not None:shutil.rmtree(stage,ignore_errors=True)
            if locked:lock.unlink(missing_ok=True)
    return results
