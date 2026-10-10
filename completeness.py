"""Fail-closed collector scope contract shared by the engine and Python collectors."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import datetime as dt
from sections import enabled
from pathlib import Path, PurePosixPath

MANIFEST = 'collection-manifest.json'


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Invalid manifest path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('.', '..') for p in value.split('/')):
        raise ValueError('Unsafe manifest path')
    return path.as_posix()


def within(path, scope):
    return path == scope or path.startswith(scope + '/')


def digest(path):
    with Path(path).open('rb') as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
        return result.hexdigest()


def section(root, path, status='complete', error=''):
    path = relative(path)
    base = Path(root) / path
    files = {}
    if status == 'complete':
        candidates = [base] if base.is_file() else sorted(base.rglob('*')) if base.is_dir() else []
        for item in candidates:
            if item.is_symlink():
                raise ValueError('Collector output must not contain symlinks')
            if item.is_file():
                files[item.relative_to(root).as_posix()] = digest(item)
    return {'path': path, 'status': status, 'error': error, 'files': files}


def publish(root, sections, finalized=True):
    root = Path(root)
    payload = {'schema_version': 1, 'run_id': os.environ.get('CONFIGBACKUP_RUN_ID', ''),
               'finalized': finalized, 'sections': sections, 'collected_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}
    target = root / MANIFEST
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(target)


class Coverage:
    def __init__(self, root, expected_run=None, settings=None):
        self.root = Path(root).resolve()
        data = json.loads((self.root / MANIFEST).read_text(encoding='utf-8-sig'))
        if data.get('schema_version') != 1 or data.get('finalized') is not True:
            raise ValueError('Missing finalized collection manifest')
        if expected_run is not None and data.get('run_id') != expected_run:
            raise ValueError('Stale collection manifest: run ID does not match')
        self.collected_at = data.get('collected_at')
        self.sections = data.get('sections')
        if not isinstance(self.sections, list):
            raise ValueError('Invalid collection sections')
        scopes = []
        self.files = set()
        self.hashes = {}
        self.complete = []
        for item in self.sections:
            scope = relative(item['path'])
            if not enabled(scope,settings or {}): item.update(status='disabled',error='Disabled by configuration')
            if any(within(scope.casefold(), other.casefold()) or within(other.casefold(), scope.casefold()) for other in scopes):
                raise ValueError('Overlapping collection scopes')
            scopes.append(scope)
            if item['status'] not in ('complete', 'failed', 'skipped', 'not_applicable', 'disabled'):
                raise ValueError('Invalid collection status')
            if item['status'] != 'complete':
                continue
            self.complete.append(scope)
            for name, expected in item['files'].items():
                name = relative(name)
                if not within(name, scope):
                    raise ValueError('File outside declared section')
                file = self.root / name
                if any(part.is_symlink() for part in [file, *file.parents] if part != self.root and self.root in part.parents) or not file.resolve().is_relative_to(self.root) or not file.is_file() or digest(file) != expected:
                    raise ValueError('Collection integrity check failed: ' + name)
                self.files.add(name)
                self.hashes[name] = expected
        self.partial = any(s['status'] in ('failed','skipped') for s in self.sections)

    def permits_deletion(self, name):
        return any(within(name, scope) for scope in self.complete)

    def seal(self, destination):
        """Copy certified bytes to a private tree; changed/missing scopes fail closed.

        Use the manifest's complete file list, never a second directory discovery as
        evidence of deletion. Consumers must use only this tree after sealing.
        """
        destination = Path(destination)
        for item in self.sections:
            if item['status'] != 'complete':
                continue
            copied = []
            try:
                for name, expected in sorted(item['files'].items()):
                    source = self.root / name
                    if source.is_symlink() or not source.resolve().is_relative_to(self.root):
                        raise ValueError('Unsafe collection path: ' + name)
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copied.append(target)
                    # Copy once, then hash the private bytes actually used by consumers.
                    with source.open('rb') as inp, target.open('xb') as out:
                        shutil.copyfileobj(inp, out)
                    if digest(target) != expected:
                        raise ValueError('Collection changed while sealing: ' + name)
            except Exception as exc:
                for target in copied:
                    target.unlink(missing_ok=True)
                item['status'] = 'failed'
                item['error'] = str(exc)
        self.complete = [s['path'] for s in self.sections if s['status'] == 'complete']
        self.files = {name for s in self.sections if s['status'] == 'complete' for name in s['files']}
        self.partial = any(s['status'] in ('failed', 'skipped') for s in self.sections)
        return destination
