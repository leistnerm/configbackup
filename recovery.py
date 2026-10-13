"""Verify retained filesystem versions and restore into a new, isolated directory."""
import datetime as dt
import json
from pathlib import Path
import shutil
import tempfile


def plan_restore(state, as_of=None, task_names=None):
    """Pure selection plan. Hash verification is a separate operation, never implied."""
    from configbackup import parse_iso
    state = state.data if hasattr(state, 'data') else state
    cutoff = parse_iso(as_of) if as_of else None
    records, errors, seen = [], [], set()
    for task, value in sorted(state.get('tasks', {}).items()):
        if task_names is not None and task not in task_names:continue
        for logical, item in sorted(value.get('files', {}).items()):
            versions = list(item.get('versions', []))
            if not versions and item.get('git_hash') and item.get('active',True):
                errors.append(task+': '+logical+': Git-only history requires git restore/checkout')
            candidates = versions if item.get('active',True) else []
            if cutoff:
                active_candidates = [v for v in candidates if parse_iso(v['created']) <= cutoff]
                candidates = []
                for generation in item.get('deleted_generations', []):
                    if cutoff < parse_iso(generation['deleted_at']):
                        candidates.extend(v for v in generation.get('versions', []) if parse_iso(v['created']) <= cutoff)
                candidates.extend(active_candidates)
            if candidates:
                rel=Path(logical)
                key=logical.replace('\\','/').casefold()
                if rel.is_absolute() or '\\' in logical or ':' in logical or '..' in rel.parts or key in seen:
                    errors.append(task+': unsafe or conflicting restore path: '+logical);continue
                seen.add(key)
                # Retained versions are appended in collection order. Legacy
                # second-resolution timestamps can tie, so prefer the later entry.
                selected=max(enumerate(candidates),key=lambda pair:(parse_iso(pair[1]['created']),pair[0]))[1]
                records.append({'task':task,'logical':logical,'version':selected})
    return {'records':records,'errors':errors,'verified':False}


def source_boundary(source):
    """The non-pattern root of a glob, resolved through existing symlinks."""
    path=Path(source).expanduser().absolute()
    prefix=Path(path.anchor)
    for part in path.parts[1:]:
        if any(c in part for c in '*?['):break
        prefix=prefix/part
    return prefix.resolve()


def recover(engine, destination=None, as_of=None):
    from configbackup import hash_file
    plan=plan_restore(engine.state.data,as_of)
    records, failures=plan['records'],list(plan['errors'])
    verified_count=0
    for task, state in engine.state.data.get('tasks', {}).items():
        for logical, item in state.get('files', {}).items():
            versions=list(item.get('versions', []))
            for generation in item.get('deleted_generations', []):versions.extend(generation.get('versions', []))
            for version in versions:
                path=engine._archive_state_path(version['path'])
                if not path.resolve().is_relative_to(engine.root.resolve()):
                    failures.append(version['path']+': archive symlink escapes root');continue
                if not path.is_file() or hash_file(path,engine.hash_algorithm)!=version['hash']:
                    failures.append(version['path']+': missing or corrupt')
                verified_count+=1
    dest=None
    if destination:
        dest=Path(destination).expanduser().absolute()
        if dest.exists() or dest.resolve().is_relative_to(engine.root.resolve()):
            raise ValueError('Restore destination must be new and outside the archive')
        for raw in engine.cfg['tasks']:
            task=engine.resolve_task_runtime(raw)
            for source in engine._as_list(task.get('source')):
                if dest.resolve().is_relative_to(source_boundary(source)):
                    raise ValueError('Restore destination must be outside live sources (including glob roots)')
    if failures:
        if not engine.dry_run:
            engine.state.data.setdefault('recovery',{}).update(last_attempt=dt.datetime.now(dt.timezone.utc).isoformat(),last_result='failed',failure_count=len(failures))
            engine.state.save()
        print(json.dumps({'verified':False,'dry_run':engine.dry_run,'failures':failures},indent=2));return 4
    if engine.dry_run:
        print(json.dumps({'verified':True,'dry_run':True,'restored_files':0,'would_restore':records if dest else []},indent=2));return 0
    if dest:
        dest.parent.mkdir(parents=True,exist_ok=True)
        staging=Path(tempfile.mkdtemp(prefix='.restore-',dir=dest.parent))
        try:
            for record in records:
                version=record['version'];target=staging/record['logical']
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(engine._archive_state_path(version['path']),target)
                if hash_file(target,engine.hash_algorithm)!=version['hash']:
                    raise ValueError('Archive changed during restore: '+record['logical'])
            staging.rename(dest)
        finally:
            if staging.exists():shutil.rmtree(staging)
    evidence=engine.state.data.setdefault('recovery',{})
    evidence.update(last_verify=dt.datetime.now(dt.timezone.utc).isoformat(),last_result='success',verified_files=verified_count)
    if dest:evidence.update(last_restore=dt.datetime.now(dt.timezone.utc).isoformat(),restored_files=len(records),restore_kind='configuration-files')
    if dest:
        for record in records:
            evidence.setdefault('restored_versions',{})[record['version']['path']]={'hash':record['version']['hash'],'at':evidence['last_restore']}
    engine.state.save()
    print(json.dumps({'verified':True,'restored_files':len(records) if dest else 0}));return 0
