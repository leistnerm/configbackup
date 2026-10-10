"""One validated connection profile for managed collection and access diagnostics."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import contextmanager
import database_diagnostics as diagnostics

ROOT = Path(__file__).resolve().parent
OVERRIDES = {'databases','sections','schema','include_health','include_history','timeout','connect_timeout'}


def validate_connections(config):
    values = config.get('connections', {})
    if not isinstance(values, dict): raise ValueError('connections must be a mapping of names to profiles')
    for name, profile in values.items():
        if not isinstance(name, str) or not name: raise ValueError('Connection names must be nonempty strings')
        diagnostics.validate(profile)


def prepare(task, config):
    """Resolve once, before runtime variable expansion. Legacy commands stay unchanged."""
    if 'connection' not in task:
        if 'collection' in task or 'required_sections' in task:
            raise ValueError('collection and required_sections require a managed connection task')
        return task
    if task.get('type') != 'execute': raise ValueError('connection requires an execute task')
    if task.get('executable') or task.get('arguments'):
        raise ValueError('Managed connection tasks cannot also specify executable/arguments')
    name = task['connection']
    if not isinstance(name,str) or not name:raise ValueError('connection must name one shared profile')
    if name not in config.get('connections', {}): raise ValueError('Unknown connection: ' + str(name))
    profile = copy.deepcopy(config['connections'][name])
    overrides = task.get('collection', {})
    if not isinstance(overrides, dict) or set(overrides) - OVERRIDES:
        raise ValueError('collection supports only: ' + ', '.join(sorted(OVERRIDES)))
    profile.update(overrides)
    profile['sections'] = {**config['connections'][name].get('sections', {}),
                           **overrides.get('sections', {}), **task.get('sections', {})}
    profile['name'] = task['name']
    diagnostics.validate(profile)
    requirements = task.get('required_sections', [])
    if not isinstance(requirements, list) or any(not isinstance(x, str) or not x or x.startswith('/') or '..' in x.split('/') for x in requirements):
        raise ValueError('required_sections must contain relative manifest scope/file patterns')
    task['_database_profile'] = profile
    task['sections'] = profile['sections']
    task['enabled'] = bool(task.get('enabled', True) and profile.get('enabled', True))
    task['executable'] = sys.executable
    task['arguments'] = [str(ROOT / 'database_connections.py')]
    # The shared timeout bounds the entire child process tree, not just its parent.
    if task.get('timeout') is not None:
        raise ValueError('Use collection.timeout for managed database tasks')
    return task


@contextmanager
def working_directory(path):
    before = Path.cwd()
    try:
        if path: os.chdir(path)
        yield
    finally: os.chdir(before)


def environment(profile, env):
    env = dict(env)
    env['CONFIGBACKUP_SECTIONS'] = json.dumps(profile.get('sections', {}))
    variable = profile.get('password_env')
    if variable:
        if not env.get(variable): raise ValueError('Missing or empty credential environment variable: ' + variable)
        env['PGPASSWORD' if profile['engine'] == 'postgresql' else 'CONFIGBACKUP_DIAGNOSTIC_PASSWORD'] = env[variable]
    return env


def collect(profile, output, env, cwd=None):
    diagnostics.validate(profile)
    env = environment(profile, env)
    with working_directory(cwd), tempfile.TemporaryDirectory(prefix='configbackup-profile-') as folder:
        if profile['engine'] == 'postgresql':
            command = [sys.executable, str(ROOT/'collectors/postgresql/collect_postgresql.py'),
                       *diagnostics.pg_arguments(profile), '--output', str(output)]
        else:
            spec = Path(folder)/'profile.json'
            spec.write_text(json.dumps(profile)); spec.chmod(0o600)
            command = [profile.get('pwsh','pwsh'), '-NoLogo','-NoProfile','-NonInteractive','-File',
                       str(ROOT/'collectors/sqlserver/Invoke-ProfileCollection.ps1'),
                       '-ProfileFile',str(spec),'-OutputDirectory',str(output)]
        code, _ = diagnostics.process(command, env, profile.get('timeout',600))
        return subprocess.CompletedProcess(command, code, b'', b'')


def task_context(config, path, name):
    from configbackup import ConfigLoader, BackupEngine
    resolved = ConfigLoader(Path(path))._resolve(config)
    matches = [t for t in resolved['tasks'] if t['name'] == name and '_database_profile' in t]
    if len(matches) != 1: raise ValueError('Choose a managed connection task name')
    engine = BackupEngine(resolved, dry_run=True)
    try:
        task = engine.resolve_task_runtime(matches[0])
        env = os.environ.copy()
        env.update({str(k):str(v) for k,v in task.get('environment',{}).items()})
        env.update(engine.variables_for_task(task))
        return task, env
    finally:
        for handler in list(engine.logger.handlers):
            handler.close();engine.logger.removeHandler(handler)


if __name__ == '__main__':
    raise SystemExit('Managed collections are launched through configbackup.py with a connection task.')
