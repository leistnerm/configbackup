"""Shared section switches. A disabled parent disables all children."""
import fnmatch
import json
import os


def enabled(name, settings=None):
    settings = json.loads(os.environ.get('CONFIGBACKUP_SECTIONS','{}')) if settings is None else settings
    for pattern, value in settings.items():
        active = value.get('enabled',True) if isinstance(value,dict) else value
        if not isinstance(active,bool): raise ValueError('Section enabled must be true or false: '+pattern)
        if active is False and (fnmatch.fnmatchcase(name.casefold(),pattern.casefold()) or
                                fnmatch.fnmatchcase(name.casefold(),pattern.casefold().rstrip('/')+'/*')):
            return False
    return True
