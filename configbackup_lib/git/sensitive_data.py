"""Explicit section approvals for sensitive collection and separate Git publication."""
from __future__ import annotations
import json
import os
import re
from pathlib import PurePosixPath

SQL_SECTIONS = frozenset({
    'instance/scripts/Logins', 'instance/scripts/Credentials', 'instance/scripts/LinkedServers',
    'instance/sensitive-ssis/environment-variables.csv',
    'instance/sensitive-ssis/project-parameters.csv', 'instance/sensitive-ssis/package-parameters.csv',
})
PG_SECTIONS = frozenset({
    'cluster/globals.sql', 'cluster/roles.csv', 'cluster/database-role-settings.csv',
    'config/pg-settings.csv', 'config/pg-file-settings.csv', 'config/pg-hba-file-rules.csv',
})
SUPPORTED = SQL_SECTIONS | PG_SECTIONS
ENVIRONMENT = 'CONFIGBACKUP_SENSITIVE_SECTIONS'


def validate_sections(value, supported=SUPPORTED):
    if not isinstance(value, list) or any(not isinstance(p, str) or p not in supported for p in value):
        raise ValueError('sensitive_sections must list exact supported section paths; wildcards and parent scopes are not permitted')
    if len(value) != len(set(value)):
        raise ValueError('Duplicate sensitive section approval')
    return value


def requested(path, settings=None):
    settings = json.loads(os.environ.get(ENVIRONMENT, '[]')) if settings is None else settings
    validate_sections(settings)
    return path in settings


def validate_git(config):
    value = config.get('sensitive_data', {})
    if not isinstance(value, dict) or set(value) - {'enabled', 'private_repository', 'allowed_remotes', 'sections'}:
        raise ValueError('git.sensitive_data supports enabled, private_repository, allowed_remotes and sections')
    for key in ('enabled', 'private_repository'):
        if key in value and type(value[key]) is not bool:
            raise ValueError('git.sensitive_data.' + key + ' must be boolean')
    remotes = value.get('allowed_remotes', [])
    if not isinstance(remotes, list) or any(not isinstance(x, str) or not x or any(c.isspace() for c in x) for x in remotes):
        raise ValueError('Sensitive Git allowed_remotes must be exact credential-free URLs or local paths without whitespace')
    for url in remotes:
        if re.match(r'https?://[^/]*@', url):
            raise ValueError('Sensitive Git remote URLs must not contain credentials')
    approvals = value.get('sections', [])
    if not isinstance(approvals, list):raise ValueError('Sensitive Git sections must be a list')
    seen=set()
    for entry in approvals:
        if not isinstance(entry, dict) or set(entry) != {'task', 'section'} or not isinstance(entry['task'], str) or not entry['task']:
            raise ValueError('Each sensitive Git section requires a task name and exact section path')
        path=entry['section']
        if not isinstance(path,str) or not path or '\\' in path or ':' in path or any(c in path for c in '*?[') or any(x in ('','.','..') for x in path.split('/')):
            raise ValueError('Sensitive Git section paths must be exact relative paths')
        key=(entry['task'],path)
        if key in seen:raise ValueError('Duplicate sensitive Git section')
        seen.add(key)
    if value.get('enabled') and (value.get('private_repository') is not True or not approvals):
        raise ValueError('Sensitive Git requires explicit private_repository: true and per-section approvals')
    return value


def git_approved(config, task, section):
    value=validate_git(config)
    return value.get('enabled') is True and {'task':task,'section':section} in value.get('sections',[])


def check_git_destination(engine):
    """Check actual fetch/push URLs each time; GitHub visibility must be private."""
    value=validate_git(engine.git_cfg)
    if value.get('enabled') is not True:raise RuntimeError('Sensitive Git publication is not enabled')
    names=engine._git_command(['remote']).stdout.splitlines()
    urls=set()
    for name in names:
        for args in (['remote','get-url','--all',name],['remote','get-url','--push','--all',name]):
            urls.update(engine._git_command(args).stdout.splitlines())
    if not urls and (engine.git_cfg.get('push') or engine.git_mode=='pull_request'):
        raise RuntimeError('Sensitive Git publication requires a configured approved remote')
    if not urls.issubset(set(value.get('allowed_remotes',[]))):
        raise RuntimeError('Sensitive Git destination has an unapproved fetch or push URL')
    for url in sorted(urls):
        match=re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?',url,re.IGNORECASE)
        from urllib.parse import urlsplit
        if urlsplit(url).hostname and urlsplit(url).hostname.lower()=='github.com' and not match:
            raise RuntimeError('Sensitive Git requires a canonical GitHub HTTPS or SSH URL')
        if match:
            try:
                response=engine._gh_command(['api','repos/'+match.group(1)])
                private=json.loads(response.stdout).get('private')
            except Exception:
                raise RuntimeError('Cannot verify sensitive Git destination privacy') from None
            if private is not True:raise RuntimeError('Sensitive Git publication requires a private GitHub repository')


def collection_wizard(config, input_fn=input):
    from configbackup_lib.ui.configure import choice
    from configbackup_lib.ui.configure_features import select_many
    tasks=[t for t in config.get('tasks',[]) if t.get('type')=='execute']
    name=choice('Collector task', [t['name'] for t in tasks], input_fn)
    if name is None:return
    task=next(t for t in tasks if t['name']==name)
    print('Selected sections may write plaintext passwords or reusable password hashes to local output and archives. Logs and report previews remain masked.')
    selected=select_many('Sensitive collection sections',sorted(SUPPORTED),task.get('sensitive_sections',[]),input_fn)
    if selected and input_fn('Type ENABLE SENSITIVE COLLECTION to approve: ').strip()!='ENABLE SENSITIVE COLLECTION':return
    task['sensitive_sections']=selected
    print('Collection permission does not allow Git publication. Configure git.sensitive_data separately for exact archive task/section pairs and approved private remotes.')


def git_wizard(config, input_fn=input):
    from configbackup_lib.ui.configure import choice, json_value
    from configbackup_lib.ui.configure_features import select_many
    previous=(config.get('git') or {}).get('sensitive_data',{})
    action=choice('Sensitive Git publication',['Configure explicit approvals','Disable sensitive publication','Cancel'],input_fn)
    if action in (None,'Cancel'):return
    if action=='Disable sensitive publication':
        config.setdefault('git',{}).setdefault('sensitive_data',{})['enabled']=False
        return
    tasks=[t for t in config.get('tasks',[]) if t.get('type')=='directory' and t.get('collection_manifest')]
    name=choice('Archive task receiving sensitive sections',[t['name'] for t in tasks],input_fn)
    if name is None:return
    selected=select_many('Sections approved for this archive task',sorted(SUPPORTED),[v['section'] for v in previous.get('sections',[]) if v.get('task')==name],input_fn)
    approvals=[v for v in previous.get('sections',[]) if v.get('task')!=name]+[{'task':name,'section':path} for path in selected]
    remotes=json_value('Exact approved private fetch/push URLs; empty for local-only repositories',previous.get('allowed_remotes',[]),input_fn)
    print('Git history will contain sensitive values. Restrict local and server access. GitHub.com privacy is verified; other servers require your own ACL verification.')
    if input_fn('Type ENABLE PRIVATE SENSITIVE GIT to confirm the destination is private: ').strip()!='ENABLE PRIVATE SENSITIVE GIT':return
    value={'enabled':True,'private_repository':True,'allowed_remotes':remotes,'sections':approvals}
    validate_git({'sensitive_data':value})
    config.setdefault('git',{})['sensitive_data']=value


def wizard(config, input_fn=input):
    from configbackup_lib.ui.configure import choice
    while True:
        action=choice('Sensitive data controls',['Collector sections','Git publication','Done'],input_fn)
        if action in (None,'Done'):return
        if action=='Collector sections':collection_wizard(config,input_fn)
        else:git_wizard(config,input_fn)
