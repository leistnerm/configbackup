"""Verify retained filesystem versions and restore into a new, isolated directory."""
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile


def recover(engine, destination=None, as_of=None):
    from configbackup import hash_file, parse_iso
    cutoff = parse_iso(as_of) if as_of else None
    records = []
    failures = []
    for task, state in engine.state.data.get('tasks', {}).items():
        for logical, item in state.get('files', {}).items():
            versions = list(item.get('versions', []))
            for gen in item.get('deleted_generations', []):
                versions.extend(gen.get('versions', []))
            if not versions and item.get('git_hash'):
                failures.append(logical + ': Git-only history requires git restore/checkout')
            for version in versions:
                path = engine._archive_state_path(version['path'])
                if not path.resolve().is_relative_to(engine.root.resolve()):
                    raise ValueError('Archive symlink escapes root')
                if not path.is_file() or hash_file(path, engine.hash_algorithm) != version['hash']:
                    failures.append(version['path'] + ': missing or corrupt')
            candidates = list(item.get('versions', [])) if item.get('active', True) else []
            if cutoff:
                candidates = [v for v in candidates if parse_iso(v['created']) <= cutoff]
                for generation in item.get('deleted_generations', []):
                    if cutoff < parse_iso(generation['deleted_at']):
                        candidates.extend(v for v in generation.get('versions', []) if parse_iso(v['created']) <= cutoff)
            if candidates:
                records.append((logical, max(candidates, key=lambda v: parse_iso(v['created']))))
    if failures:
        engine.state.data.setdefault('recovery',{}).update(last_attempt=dt.datetime.now(dt.timezone.utc).isoformat(),last_result='failed',failure_count=len(failures))
        engine.state.save()
        print(json.dumps({'verified': False, 'failures': failures}, indent=2))
        return 4
    if destination:
        dest = Path(destination).expanduser().absolute()
        if dest.exists() or dest.resolve().is_relative_to(engine.root.resolve()):
            raise ValueError('Restore destination must be new and outside the archive')
        for task in engine.cfg['tasks']:
            for source in engine._as_list(task.get('source')):
                if dest.resolve().is_relative_to(Path(source).resolve()):
                    raise ValueError('Restore destination must be outside live sources')
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix='.restore-', dir=dest.parent))
        try:
            seen = set()
            for logical, version in records:
                rel = Path(logical)
                if rel.is_absolute() or '..' in rel.parts or logical in seen:
                    raise ValueError('Unsafe or conflicting restore path')
                seen.add(logical)
                target = staging / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(engine._archive_state_path(version['path']), target)
            staging.rename(dest)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    evidence=engine.state.data.setdefault('recovery',{})
    evidence.update(last_verify=dt.datetime.now(dt.timezone.utc).isoformat(),last_result='success',verified_files=sum(len(v.get('versions',[]))+sum(len(g.get('versions',[])) for g in v.get('deleted_generations',[])) for t in engine.state.data.get('tasks',{}).values() for v in t.get('files',{}).values()))
    if destination: evidence.update(last_restore=dt.datetime.now(dt.timezone.utc).isoformat(),restored_files=len(records),restore_kind='configuration-files')
    engine.state.save()
    print(json.dumps({'verified': True, 'restored_files': len(records) if destination else 0}))
    return 0
