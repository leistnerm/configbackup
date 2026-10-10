"""Versioned telemetry envelopes; volatile facts never belong in config manifests."""
from __future__ import annotations
import datetime as dt
import json
import os
import shutil
import socket
from pathlib import Path


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def envelope(engine, host=None, instance='', database=None):
    return {'schema_version': 1, 'engine': engine, 'host': host or socket.gethostname(),
            'instance': str(instance), 'database': database, 'observed_at': utcnow(),
            'datasets': {}, 'failures': []}


def dataset(payload, name, rows, keys, units, counters=(), reset_field=None):
    payload['datasets'][name] = {'rows': list(rows), 'keys': list(keys), 'units': units,
                                 'counters': list(counters), 'reset_field': reset_field}


def write_envelope(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, sort_keys=True, indent=2, default=str) + '\n', encoding='utf-8')
    temporary.replace(path)


def disk_rows(paths):
    """Only inspect explicitly local paths. Missing mounts are not replaced by parents."""
    rows, failures = [], []
    for spec in paths:
        spec = {'path': str(spec)} if isinstance(spec, (str, Path)) else spec
        path = Path(spec['path'])
        try:
            if not path.exists():
                raise ValueError('Configured capacity path is missing')
            if spec.get('require_mount') and not path.is_mount():
                raise ValueError('Expected mount is not mounted')
            usage = shutil.disk_usage(path)
            row = {'path': str(path), 'total_bytes': usage.total, 'free_bytes': usage.free,
                   'free_percent': 100 * usage.free / usage.total if usage.total else None}
            if hasattr(os, 'statvfs'):
                stat = os.statvfs(path)
                row['free_inodes'] = stat.f_favail if stat.f_files else None
                row['free_inodes_percent'] = 100 * stat.f_favail / stat.f_files if stat.f_files else None
            rows.append(row)
        except Exception as exc:
            failures.append({'section': 'disk:' + str(path), 'error': str(exc)})
    return rows, failures


def collect_disks(paths, host=None):
    payload = envelope('system', host)
    rows, payload['failures'] = disk_rows(paths)
    dataset(payload, 'disk', rows, ['path'], {'total_bytes': 'bytes', 'free_bytes': 'bytes',
            'free_percent': 'percent', 'free_inodes': 'count', 'free_inodes_percent': 'percent'})
    return payload
