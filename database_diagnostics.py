"""Read-only connection checks and temporary, real collector access tests."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

from completeness import Coverage
from sections import enabled

ROOT = Path(__file__).resolve().parent
COMMON = {'name', 'engine', 'enabled', 'access_profile', 'databases', 'sections', 'schema',
          'include_health', 'include_history', 'timeout', 'connect_timeout', 'password_env', 'user'}
ENGINE_FIELDS = {
    'sqlserver': {'server', 'pwsh', 'sqlpackage', 'trust_server_certificate'},
    'postgresql': {'host', 'port', 'service', 'sslmode', 'maintenance_db', 'bin_dir'},
}


def validate(profile):
    if not isinstance(profile, dict) or profile.get('engine') not in ENGINE_FIELDS:
        raise ValueError('Diagnostic engine must be sqlserver or postgresql')
    unknown = set(profile) - COMMON - ENGINE_FIELDS[profile['engine']]
    if unknown:
        raise ValueError('Unsupported diagnostic fields: ' + ', '.join(sorted(unknown)) + '. Use password_env, never a password value.')
    if profile.get('access_profile', 'read-only') not in ('read-only', 'full'):
        raise ValueError('access_profile must be read-only or full')
    for key in ('name','password_env','user','server','pwsh','sqlpackage','host','service','sslmode','maintenance_db','bin_dir'):
        if key in profile and not isinstance(profile[key],str):
            raise ValueError(key + ' must be a string')
    for key, value in profile.items():
        if isinstance(value, str) and any(ord(c) < 32 for c in value):
            raise ValueError('Diagnostic strings must not contain control characters')
    for key in ('enabled', 'schema', 'include_health', 'include_history', 'trust_server_certificate'):
        if key in profile and not isinstance(profile[key], bool):
            raise ValueError(key + ' must be boolean')
    for key, default, limit in (('timeout', 600, 14400), ('connect_timeout', 10, 60)):
        value = profile.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise ValueError(key + ' must be a positive integer no greater than ' + str(limit))
    databases = profile.get('databases', [])
    if not isinstance(databases, list) or any(not isinstance(n, str) or not n or any(ord(c) < 32 for c in n) for n in databases):
        raise ValueError('databases must be a list of names/patterns')
    switches = profile.get('sections', {})
    if not isinstance(switches, dict) or any(not isinstance(key,str) for key in switches):
        raise ValueError('sections must be a mapping')
    enabled('validation', switches)
    if profile.get('password_env') and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', profile['password_env']):
        raise ValueError('password_env must name an environment variable')
    if profile['engine'] == 'sqlserver':
        if not isinstance(profile.get('server'), str) or not profile['server']:
            raise ValueError('SQL Server endpoint required')
        if bool(profile.get('user')) != bool(profile.get('password_env')):
            raise ValueError('SQL authentication requires both user and password_env; omit both for integrated authentication')
    else:
        if profile.get('sslmode', 'prefer') not in ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'):
            raise ValueError('Unknown PostgreSQL sslmode')
        if 'port' in profile and (isinstance(profile['port'], bool) or not str(profile['port']).isdigit() or not 1 <= int(profile['port']) <= 65535):
            raise ValueError('Invalid PostgreSQL port')
        maintenance = profile.get('maintenance_db', 'postgres')
        if not isinstance(maintenance, str) or not maintenance or '=' in maintenance or '://' in maintenance:
            raise ValueError('maintenance_db must be a database name, not a connection string')


def sanitize(value, env):
    text = str(value)
    for name, secret in env.items():
        if secret and re.search(r'password|passwd|token|secret|api.?key', name, re.I):
            text = text.replace(secret, '[redacted]')
    from collectors.postgresql.collect_postgresql import redact_text
    text = str(redact_text(text))
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)[:1500]


def process(command, env, timeout, capture=False):
    """Kill the diagnostic process tree on timeout, before removing scratch data."""
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    with subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, text=True, **options) as child:
        try:
            stdout, _ = child.communicate(timeout=timeout)
            return child.returncode, stdout or ''
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                child.kill()
            else:
                try: os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
            child.wait()
            raise


def pg_arguments(profile):
    args = []
    for key in ('host', 'port', 'user', 'service', 'sslmode', 'maintenance_db', 'bin_dir'):
        if profile.get(key) is not None and profile[key] != '':
            args += ['--' + key.replace('_', '-'), str(profile[key])]
    for db in profile.get('databases', []):
        args += ['--database', db]
    args += ['--connect-timeout', str(profile.get('connect_timeout', 10)),
             '--command-timeout', str(min(120, profile.get('timeout', 600))),
             '--dump-timeout', str(profile.get('timeout', 600))]
    if profile.get('access_profile', 'read-only') == 'read-only': args += ['--read-only-access']
    if not profile.get('schema', True): args += ['--skip-schema-dump']
    if profile.get('include_health', False): args += ['--include-health']
    if profile.get('include_history', False): args += ['--include-scheduler-history']
    return args


def postgres(profile, scratch, env):
    from collectors.postgresql.collect_postgresql import PgTools, parse_args
    args = pg_arguments(profile)
    tools = PgTools(parse_args(args))
    tools.env.update(env)
    # Reapply explicit profile fields after inheriting the caller environment.
    for key, variable in [('host', 'PGHOST'), ('port', 'PGPORT'), ('user', 'PGUSER'), ('service', 'PGSERVICE'), ('sslmode', 'PGSSLMODE')]:
        if profile.get(key) is not None and profile[key] != '': tools.env[variable] = str(profile[key])
    tools.env['PGCONNECT_TIMEOUT'] = str(profile.get('connect_timeout', 10))
    identity = "SELECT json_build_object('status','connected','database',current_database(),'identity',current_user,'version',current_setting('server_version'))"
    permissions = """SELECT json_build_object('superuser',rolsuper,'bypass_rls',rolbypassrls,
          'pg_monitor',pg_has_role(current_user,'pg_monitor','USAGE'),
          'pg_read_all_data',CASE WHEN current_setting('server_version_num')::int>=140000
             THEN pg_has_role(current_user,'pg_read_all_data','USAGE') ELSE false END)
        FROM pg_roles WHERE rolname=current_user"""
    probe = [tools.exe('psql'), '-X', '-w', '-qAt', '-v', 'ON_ERROR_STOP=1', '-d', profile.get('maintenance_db', 'postgres')]
    code, output = process([*probe, '-c', identity], tools.env,
                           profile.get('connect_timeout', 10) + 5, capture=True)
    if code:
        connection = {'status': 'failed', 'reason': 'Connection/identity probe failed. Check endpoint, credentials, TLS, maintenance database and client compatibility.'}
    else:
        connection = json.loads(output)
        # A denied permission-catalog read is not a failed authentication.
        code, output = process([*probe, '-c', permissions], tools.env, profile.get('connect_timeout',10)+5,capture=True)
        if code: connection['permission_error'] = 'Permission indicators unavailable; actual collector checks will determine section access.'
        else: connection['permissions'] = json.loads(output)
    (scratch / 'connection.json').write_text(json.dumps(connection))
    if connection['status'] != 'connected': return code or 1
    return process([sys.executable, str(ROOT / 'collectors/postgresql/collect_postgresql.py'),
                    *args, '--output', str(scratch / 'snapshot')], tools.env, profile.get('timeout', 600))[0]


def summarize(scratch, result, env):
    connection = scratch / 'connection.json'
    if connection.exists():
        try: result['connection'] = json.loads(connection.read_text(encoding='utf-8-sig'))
        except (ValueError,OSError): result['connection'] = {'status': 'not_tested', 'reason': 'Connection probe did not finish publishing its result.'}
    root = scratch / 'snapshot'
    try:
        coverage = Coverage(root)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result['sections'].append({'section': 'configuration', 'status': 'not_tested', 'reason': 'No valid finalized collector manifest; no section can be certified.'})
        if (root / 'collector-error.json').exists():
            errors = json.loads((root / 'collector-error.json').read_text()).get('failures', [])
            for error in errors[:10]:
                result['sections'].append({'section': 'collector preflight', 'status': 'unavailable', 'reason': sanitize(error.get('error', ''), env)})
        result['status'] = 'incomplete'
        return
    mapping = {'complete': 'available', 'failed': 'unavailable', 'skipped': 'not_tested', 'disabled': 'disabled', 'not_applicable': 'not_applicable'}
    for scope in coverage.sections:
        result['sections'].append({'section': scope['path'], 'status': mapping[scope['status']],
                                   'files_verified': len(scope.get('files', {})) if scope['status'] == 'complete' else 0,
                                   'reason': sanitize(scope.get('error', ''), env)})
    # Runtime probes are outside the configuration manifest, and must not be
    # reported successful merely because the configuration collection succeeded.
    for path in sorted((root / 'telemetry').rglob('*.json')):
        try: data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError): continue
        if not isinstance(data, dict): continue
        name = path.relative_to(root).as_posix()
        for key in data.get('datasets', {}):
            result['sections'].append({'section': name + '#' + key, 'status': 'available', 'reason': 'Runtime query completed; values are not archived by this test.'})
        for failure in data.get('failures', []):
            result['sections'].append({'section': name, 'status': 'unavailable', 'reason': sanitize(json.dumps(failure), env)})
    result['status'] = 'partial' if result.get('collector_exit_code') or any(row['status'] == 'unavailable' for row in result['sections']) else 'complete'


def diagnose(profile, progress=None):
    validate(profile)
    env = os.environ.copy()
    env.pop('CONFIGBACKUP_OUTPUT', None)
    env['CONFIGBACKUP_SECTIONS'] = json.dumps(profile.get('sections', {}))
    env['PGCONNECT_TIMEOUT'] = str(profile.get('connect_timeout', 10))
    result = {'schema_version': 1, 'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'name': profile.get('name', profile['engine']), 'engine': profile['engine'],
              'access_profile': profile.get('access_profile', 'read-only'),
              'connection': {'status': 'not_tested'}, 'status': 'incomplete', 'sections': [],
              'scope_note': 'Actual collector test using this diagnostic profile and identity. Settings/permissions may change. Backup tasks and service adapters are not executed.'}
    if profile.get('enabled',True) is False:
        result.update(status='disabled',sections=[{'section':'diagnostic profile','status':'disabled','reason':'Disabled by configuration; no connection attempted.'}])
        return result
    password_name = profile.get('password_env')
    if password_name:
        if not env.get(password_name):
            result['connection'] = {'status': 'not_tested', 'reason': 'Required password environment variable is missing or empty: ' + password_name}
            return result
        env['PGPASSWORD' if profile['engine'] == 'postgresql' else 'CONFIGBACKUP_DIAGNOSTIC_PASSWORD'] = env[password_name]
    if progress: progress('Testing connection and running collectors into disposable scratch space. Schema extraction may take several minutes.')
    with tempfile.TemporaryDirectory(prefix='configbackup-access-') as folder:
        scratch = Path(folder)
        spec = scratch / 'profile.json'
        spec.write_text(json.dumps(profile));spec.chmod(0o600)
        try:
            if profile['engine'] == 'postgresql': code = postgres(profile, scratch, env)
            else:
                code = process([profile.get('pwsh', 'pwsh'), '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
                                str(ROOT / 'collectors/sqlserver/Test-CollectionAccess.ps1'),
                                '-ProfileFile', str(spec), '-ScratchDirectory', str(scratch)], env, profile.get('timeout', 600))[0]
            result['collector_exit_code'] = code
        except subprocess.TimeoutExpired:
            result['error'] = 'Diagnostic timed out; collector process tree stopped. Increase timeout or test fewer databases.'
        except (OSError, ValueError, RuntimeError) as exc:
            result['error'] = sanitize(str(exc), env)
        summarize(scratch, result, env)
        if result.get('error'): result['status'] = 'incomplete'
    if not profile.get('schema', True):
        result['sections'].append({'section': 'native schema extraction', 'status': 'not_tested', 'reason': 'Schema extraction disabled for this diagnostic profile; metadata success does not verify a native dump.'})
    if not profile.get('include_health', False):
        result['sections'].append({'section': 'optional health telemetry', 'status': 'not_tested', 'reason': 'Not requested by this diagnostic profile.'})
    result['sections'].append({'section': 'external service adapters and remote host configuration', 'status': 'not_tested', 'reason': 'SSRS/SSAS/WSFC adapters and remote operating-system privileges need separate tests.'})
    result['sections'].append({'section': 'additional opt-in collector features', 'status': 'not_tested', 'reason': 'Physical-index scans, selected pgstattuple targets, raw config files, legacy SSIS and custom connection options are outside this diagnostic profile.'})
    # Apply value redaction recursively without serializing secret values to disk.
    def clean(value):
        if isinstance(value, dict): return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list): return [clean(item) for item in value]
        return sanitize(value, env) if isinstance(value, str) else value
    return clean(result)


def display(result, print_fn=print):
    print_fn('Connection: ' + result['connection']['status'] + ' | Profile: ' + result['access_profile'] + ' | Result: ' + result['status'])
    if result['connection'].get('identity'): print_fn('Identity: ' + result['connection']['identity'])
    if result['connection'].get('permissions'): print_fn('Observed permissions: ' + json.dumps(result['connection']['permissions'], sort_keys=True))
    for message in (result.get('error'), result['connection'].get('reason'), result['connection'].get('permission_error')):
        if message: print_fn(message)
    for row in result['sections']:
        print_fn(f"{row['status'].upper():<15} {row['section']}" + (' — ' + row['reason'] if row.get('reason') else ''))
    print_fn(result['scope_note'])
    print_fn('Scratch exports removed. Configuration, archives, Git and permissions unchanged.')


def write_report(result, path):
    # Explicit destination only; never overwrite an existing report/config file.
    with Path(path).open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
