"""Setup probes: owned temporary files and read-only remote checks. No implicit sends."""
from __future__ import annotations
import datetime as dt
import os
from pathlib import Path
import shutil
import smtplib
import ssl
import tempfile
import urllib.parse
from database_diagnostics import process
from readiness import account_context


def check_directory(path):
    path=Path(path).expanduser().absolute()
    if not path.is_dir():return {'status':'unavailable','reason':'Directory does not exist; create it with access for the scheduled account.'}
    with tempfile.TemporaryFile(dir=path) as stream:
        stream.write(b'ConfigBackup setup access test\n');stream.flush();os.fsync(stream.fileno());stream.seek(0)
        if not stream.read():raise OSError('Write/read verification failed')
    usage=shutil.disk_usage(path)
    return {'status':'available','free_bytes':usage.free,'free_percent':round(100*usage.free/usage.total,2)}


def smtp_check(channel,env):
    host=channel['host'];mode=channel.get('tls','starttls')
    if mode not in ('ssl','starttls','none'):raise ValueError('Unknown SMTP TLS mode')
    if mode=='none' and not (channel.get('allow_insecure_localhost') and host in ('127.0.0.1','localhost','::1')):
        raise ValueError('TLS required except explicitly configured localhost fixtures')
    client=smtplib.SMTP_SSL if mode=='ssl' else smtplib.SMTP
    options={'timeout':min(30,max(1,float(channel.get('timeout_seconds',15))))}
    if mode=='ssl':options['context']=ssl.create_default_context()
    with client(host,int(channel.get('port',465 if mode=='ssl' else 587)),**options) as smtp:
        code,_=smtp.ehlo()
        if code!=250:raise ValueError('SMTP EHLO rejected')
        if mode=='starttls':smtp.starttls(context=ssl.create_default_context());smtp.ehlo()
        username=env.get(channel.get('username_env',''))
        if username:
            password=env.get(channel.get('password_env',''))
            if not password:raise ValueError('SMTP password reference is unavailable')
            smtp.login(username,password)
        # Deliberately no MAIL, RCPT or DATA; recipient acceptance remains untested.


def run(config,path,probe_notifications=False):
    from configbackup import ConfigLoader,BackupEngine
    from database_connections import task_context,working_directory
    from monitoring import validate_config
    cfg=ConfigLoader(Path(path))._resolve(config);validate_config(cfg.get('monitoring',{}))
    rows=[]
    def probe(name,action):
        try:rows.append({'section':name,**(action() or {'status':'available'})})
        except Exception:
            # Third-party errors may contain credentials, URLs or protocol transcripts.
            rows.append({'section':name,'status':'unavailable','reason':'Probe failed. Check path/tool/connection access under this account; raw errors are intentionally omitted.'})
    engine=BackupEngine(cfg,dry_run=True)
    try:
        for label,directory in [('archive',engine.archive_root),('staging',engine.staging_root)]:
            probe('directory:'+label,lambda p=directory:check_directory(p))
    finally:
        for handler in list(engine.logger.handlers):handler.close();engine.logger.removeHandler(handler)
    for task in cfg['tasks']:
        if '_database_profile' not in task or not task.get('enabled',True):continue
        resolved,env=task_context(config,path,task['name']);profile=resolved['_database_profile']
        with working_directory(resolved.get('working_directory')):
            clients=[profile.get('pwsh','pwsh'),profile.get('sqlpackage','sqlpackage')] if profile['engine']=='sqlserver' else [str(Path(profile.get('bin_dir') or '')/exe) if profile.get('bin_dir') else exe for exe in ('psql','pg_dump','pg_dumpall')]
            for client in clients:
                rows.append({'section':task['name']+':client:'+Path(client).name,'status':'available' if shutil.which(client,path=env.get('PATH')) else 'unavailable','reason':'Executable lookup only; native versions/modules and TLS are tested by --diagnose-task.'})
            secret=profile.get('password_env')
            if secret:rows.append({'section':task['name']+':credential-reference','status':'available' if env.get(secret) else 'unavailable','reason':'Variable present/absent; value not recorded.'})
    git=cfg.get('git',{})
    if git.get('enabled',True) and git.get('repository'):
        repo=Path(git['repository']).expanduser().absolute()
        probe('directory:git',lambda:check_directory(repo))
        def git_probe():
            env=os.environ.copy();env.update(GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never',GIT_SSH_COMMAND='ssh -oBatchMode=yes')
            command=['git','-C',str(repo)]
            code,remote=process([*command,'remote','get-url',str(git.get('remote','origin'))],env,15,capture=True)
            if code:return {'status':'not_tested','reason':'No configured remote. Local write probe does not establish Git push or PR access.'}
            remote=remote.strip();parsed=urllib.parse.urlsplit(remote)
            if parsed.scheme in ('http','https') and (parsed.username or parsed.password):raise ValueError('Credentials in Git URL')
            code,_=process([*command,'ls-remote',str(git.get('remote','origin'))],env,30)
            return {'status':'available' if code==0 else 'unavailable','reason':'Read-only remote access; push, branch protection and PR creation remain untested.'}
        probe('git:remote-read',git_probe)
    for channel in cfg.get('monitoring',{}).get('notifications',{}).get('channels',[]):
        if not channel.get('enabled',True):continue
        name='notification:'+channel['id']
        missing=[key for key,value in channel.items() if key.endswith('_env') and value and not os.environ.get(value)]
        if missing:rows.append({'section':name,'status':'unavailable','reason':'Missing environment references: '+', '.join(missing)});continue
        if channel['type']=='smtp' and probe_notifications:probe(name,lambda c=channel:smtp_check(c,os.environ))
        else:rows.append({'section':name,'status':'not_tested','reason':'Configuration validated; delivery untested. SMTP handshake requires --probe-notifications; other channels require an explicit --test-notification send.'})
    return {'schema_version':1,'observed_at':dt.datetime.now(dt.timezone.utc).isoformat(),'execution':account_context(),'status':'partial' if any(r['status']=='unavailable' for r in rows) else 'complete','checks':rows,'scope_note':'Owned temporary write probes are removed. No database collection, Git push/PR or notification delivery performed. Free space is a point-in-time observation.'}


def test_notification(config,channel_id):
    from monitoring import validate_config
    from notifications import send
    monitor=config.get('monitoring',{});validate_config(monitor)
    selected=[c for c in monitor.get('notifications',{}).get('channels',[]) if c['id']==channel_id and c.get('enabled',True)]
    if len(selected)!=1:raise ValueError('Choose one enabled notification channel ID')
    send(selected[0],{'subject':'ConfigBackup explicit delivery test','text':'This test was explicitly requested from the ConfigBackup configuration CLI. No configuration or database contents are included.'})
