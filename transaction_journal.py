"""Durable local undo journal. Call only while holding the engine run lock.

A changed Git HEAD requires operator review: published commits are never undone.
Filesystem durability depends on the local filesystem honoring fsync/rename.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def fsync_directory(path):
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix='.journal-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fsync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def head(repo):
    if not repo:
        return None
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Distinguish a valid unborn repository from an unavailable repository.
        check = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--git-dir'], capture_output=True, timeout=30)
        if check.returncode:
            raise ValueError('Journal Git repository is unavailable')
        return ''
    return result.stdout.strip()


class Journal:
    def __init__(self, directory, roots, repo=None):
        self.directory = Path(directory).resolve()
        self.manifest = self.directory / 'pending.json'
        self.roots = [Path(p).resolve() for p in roots if p]
        self.repo = str(Path(repo).resolve()) if repo else None
        self.data = None
        self.poisoned = False
        self._paths = set()
        self._persisted = 0

    def allowed(self, path):
        path = Path(path).absolute()
        if any(p.is_symlink() and any(p.resolve().is_relative_to(root) for root in self.roots) for p in [path, *path.parents]):
            raise ValueError('Journal refuses symlink paths')
        if not any(path.resolve().is_relative_to(root) and path.resolve()!=root for root in self.roots):
            raise ValueError('Journal target is outside configured storage')
        if path.resolve().is_relative_to(self.directory.resolve()) or '.git' in path.parts:
            raise ValueError('Journal target overlaps its metadata or Git internals')
        return path.resolve()

    def begin(self):
        if self.manifest.exists():
            raise ValueError('Pending transaction must be reconciled first')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Version 2 stores each immutable entry separately. The small header
        # anchors the durable entry count; old version-1 journals remain readable.
        if any(self.directory.glob('entry-*.json')) or any(self.directory.glob('before-*')):
            raise ValueError('Orphan journal files require inspection before a new transaction')
        self.data = {'schema':2, 'status':'pending', 'repo':self.repo, 'head':head(self.repo), 'entries':[]}
        self._paths=set();self._persisted=0
        self.save()

    def save(self):
        try:
            if self.data['schema']==2:
                while self._persisted<len(self.data['entries']):
                    row=self.data['entries'][self._persisted]
                    atomic(self.directory/f'entry-{self._persisted:08d}.json',(json.dumps(row,sort_keys=True)+'\n').encode())
                    self._persisted+=1
                payload={k:v for k,v in self.data.items() if k!='entries'}
                payload['entry_count']=self._persisted
            else:payload=self.data
            atomic(self.manifest, (json.dumps(payload, sort_keys=True, indent=2)+'\n').encode())
        except OSError:
            self.poisoned=True
            raise

    def remember(self, path):
        if self.poisoned:raise OSError('Journal persistence failed; stop storage mutations and reconcile on restart')
        if self.data is None:
            return
        path = self.allowed(path)
        if str(path) in self._paths:
            return
        row = {'path':str(path), 'backup':None}
        if path.exists():
            if not path.is_file():
                raise ValueError('Journal supports regular files only')
            name = f"before-{len(self.data['entries']):08d}"
            target = self.directory/name
            # Copy and sync before publishing undo metadata; mutation happens later.
            try:
                with path.open('rb') as source, target.open('xb') as out:
                    os.chmod(target,0o600)
                    shutil.copyfileobj(source,out)
                    out.flush();os.fsync(out.fileno())
                row.update(backup=name, sha256=sha(target), mode=path.stat().st_mode & 0o777)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        self.data['entries'].append(row)
        self.save()
        self._paths.add(str(path))

    def commit(self):
        if self.poisoned:raise OSError('Journal persistence failed; pending recovery retained')
        # Sync final bytes before the durable committed marker. Retain journal on error.
        for row in self.data['entries']:
            path=self.allowed(row['path'])
            if path.is_file():
                with path.open('rb') as stream:os.fsync(stream.fileno())
            if path.parent.exists():fsync_directory(path.parent)
        self.data['status']='committed';self.save()
        self.cleanup()

    def cleanup(self):
        # Remove manifest last: interrupted cleanup of committed journals is retryable.
        for path in [*self.directory.glob('before-*'),*self.directory.glob('entry-*.json')]:
            if path.is_symlink():raise ValueError('Unsafe journal backup')
            path.unlink()
        self.manifest.unlink(missing_ok=True)
        fsync_directory(self.directory)
        self.data=None

    def read(self):
        if self.manifest.is_symlink() or self.manifest.stat().st_size>64*1024*1024:
            raise ValueError('Unsafe transaction manifest')
        data=json.loads(self.manifest.read_text())
        if data.get('schema')==2:
            count=data.get('entry_count')
            if not isinstance(count,int) or isinstance(count,bool) or not 0<=count<=1_000_000:raise ValueError('Invalid journal entry count')
            data['entries']=[]
            # A crash may leave one durable record before publishing its count;
            # no corresponding file mutation was yet authorized. It is safe to undo.
            paths=sorted(self.directory.glob('entry-*.json'))
            if data.get('status')!='committed' and not count<=len(paths)<=count+1:raise ValueError('Missing or unexpected journal entries')
            if data.get('status')!='committed':
                for i,path in enumerate(paths):
                    if path.name!=f'entry-{i:08d}.json' or path.is_symlink() or path.stat().st_size>1024*1024:raise ValueError('Unsafe journal entry')
                    data['entries'].append(json.loads(path.read_text()))
        return data

    def validate_pending(self, data):
        if data.get('schema') not in (1,2) or data.get('repo') != self.repo or data.get('status') != 'pending' or not isinstance(data.get('entries'),list):
            raise ValueError('Invalid pending transaction identity')
        seen=set()
        backups=set()
        # Validate every target and saved hash before changing any file.
        for row in data['entries']:
            if not isinstance(row,dict) or not isinstance(row.get('path'),str) or not Path(row['path']).is_absolute() or 'backup' not in row:
                raise ValueError('Invalid journal entry fields')
            path=self.allowed(row['path'])
            if path.exists() and not path.is_file():raise ValueError('Journal target is not a regular file')
            if str(path) in seen:raise ValueError('Duplicate journal target')
            seen.add(str(path))
            if row['backup'] is not None:
                name=row['backup']
                if not isinstance(name,str) or not __import__('re').fullmatch(r'before-\d{8}',name):raise ValueError('Unsafe journal backup')
                if name in backups:raise ValueError('Duplicate journal backup')
                backups.add(name)
                if not isinstance(row.get('mode'),int) or isinstance(row['mode'],bool) or not 0<=row['mode']<=0o777:raise ValueError('Invalid journal restore mode')
                if not isinstance(row.get('sha256'),str) or not __import__('re').fullmatch('[0-9a-f]{64}',row['sha256']):raise ValueError('Invalid journal backup digest')
                saved=self.directory/name
                if saved.is_symlink() or not saved.is_file() or sha(saved)!=row['sha256']:raise ValueError('Journal backup is missing or corrupt; no recovery performed')
    def recover(self):
        if not self.manifest.exists():return {'status':'clean'}
        data=self.read()
        if data.get('schema') not in (1,2) or data.get('repo')!=self.repo or not isinstance(data.get('entries'),list):
            raise ValueError('Transaction storage identity changed; manual review required')
        if data.get('status')=='committed':
            self.data=data;self.cleanup();return {'status':'cleaned_committed'}
        if data.get('status')!='pending':raise ValueError('Unknown transaction state')
        if head(self.repo)!=data.get('head'):
            raise ValueError('Interrupted transaction has a different Git HEAD; preserve archive, state and journal for manual reconciliation. No commit or file was rolled back.')
        self.validate_pending(data)
        for row in reversed(data['entries']):
            path=self.allowed(row['path'])
            if row['backup'] is None:
                path.unlink(missing_ok=True)
            else:
                # Stream into a sibling, preserving metadata without loading large files.
                path.parent.mkdir(parents=True,exist_ok=True)
                fd,tmp=tempfile.mkstemp(prefix='.undo-',dir=path.parent)
                try:
                    with os.fdopen(fd,'wb') as out, (self.directory/row['backup']).open('rb') as src:
                        shutil.copyfileobj(src,out);out.flush();os.fsync(out.fileno())
                    os.chmod(tmp,row['mode']);os.replace(tmp,path)
                finally:Path(tmp).unlink(missing_ok=True)
            if path.parent.exists():fsync_directory(path.parent)
        if self.repo:
            args=['reset','--mixed',data['head']] if data['head'] else ['read-tree','--empty']
            result=subprocess.run(['git','-C',self.repo,*args],capture_output=True,timeout=30)
            if result.returncode:raise ValueError('Files recovered; Git index reconciliation requires review')
        self.data=data;self._persisted=len(data['entries']);self.data['status']='committed';self.save();self.cleanup()
        return {'status':'rolled_back','files':len(data['entries'])}
