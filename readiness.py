"""Evidence-based readiness, suggested remedies and persistent capability baselines."""
from __future__ import annotations
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import getpass
import socket


def account_context():
    result = {'host':socket.gethostname(),'platform':platform.system(),'python':sys.executable,'working_directory':str(Path.cwd()),
              'launcher_mode':os.environ.get('CONFIGBACKUP_LAUNCH_MODE','direct'),
              'scheduler_proven':False}
    if hasattr(os,'geteuid'):
        import pwd
        result.update(uid=os.getuid(),effective_uid=os.geteuid(),account=pwd.getpwuid(os.geteuid()).pw_name)
    else:
        try:
            row = subprocess.run(['whoami','/user','/fo','csv','/nh'],capture_output=True,text=True,timeout=10,check=True)
            import csv
            fields = next(csv.reader(row.stdout.splitlines()))
            result.update(account=fields[0],sid=fields[1])
        except (OSError,subprocess.SubprocessError,IndexError,StopIteration):
            result.update(account=getpass.getuser(),identity_unverified=True)
    return result


def requirements(result, patterns):
    rows = result.get('sections',[])
    evidence = {row['section']:row['status'] for row in rows}
    for row in rows:
        for path in row.get('verified_paths',[]): evidence[path] = 'available'
    checks=[]
    for pattern in patterns:
        matches={name:status for name,status in evidence.items() if fnmatch.fnmatchcase(name,pattern)}
        failed={name:status for name,status in matches.items() if status != 'available'}
        # An unavailable parent must defeat a wildcard matching surviving siblings.
        for row in rows:
            prefix=row['section'].rstrip('/')+'/'
            if row['status']!='available' and (pattern.startswith(prefix) or any(fnmatch.fnmatchcase(row['section'], '/'.join(pattern.split('/')[:n])) for n in range(1,len(pattern.split('/'))))):
                failed[row['section']]=row['status']
        checks.append({'pattern':pattern,'status':'passed' if matches and not failed else 'failed',
                       'matched':len(matches),'unavailable':failed,
                       'reason':'' if matches else 'No verified scope/file matched; absent, disabled and untested are not success.'})
    result['requirements']=checks
    result['readiness']='not_ready' if result['connection']['status']!='connected' or result.get('status') in ('disabled','incomplete') or any(x['status']=='failed' for x in checks) else 'ready_with_warnings' if result['status']=='partial' else 'ready'
    return result


def guided_fixes(result,profile):
    fixes=[]
    def add(code,action):fixes.append({'id':code,'action':action,'applied':False})
    if result['connection']['status']!='connected':
        add('connection','Verify the endpoint/port, database login, maintenance database, DNS and firewall from the scheduled host. Confirm the referenced password variable is present under that account. Use the server certificate trust chain and hostname; do not disable TLS validation as a permission fix.')
    message=str(result.get('error','')).lower()
    if 'no such file' in message or 'not found' in message:
        add('client-path','Install the missing database client and set bin_dir (PostgreSQL), pwsh and sqlpackage (SQL Server) to absolute executable paths. Install dbatools for the scheduled PowerShell account; retest through the launcher.')
    permissions=result['connection'].get('permissions',{})
    if profile['engine']=='postgresql' and not permissions.get('superuser'):
        missing=[key for key in ('pg_monitor','pg_read_all_data','bypass_rls') if permissions.get(key) is False]
        if missing:add('postgres-read-access','Missing observed capabilities: '+', '.join(missing)+'. Generate the PostgreSQL read-access script for administrator review. pg_read_all_data and BYPASSRLS grant broad cluster-wide read visibility, not just the selected databases.')
    if profile['engine']=='sqlserver' and not permissions.get('sysadmin'):
        missing=[key for key in ('view_any_database','view_any_definition','view_server_state') if permissions.get(key) is False]
        if missing:add('sql-read-access','Missing observed permissions: '+', '.join(missing)+'. Generate the SQL Server read-access script for administrator review, including selected database users and explicit msdb catalog SELECT grants.')
        if any(row['status']=='unavailable' and ('scripts/' in row['section'] or 'ssis' in row['section']) for row in result['sections']):
            add('protected-exports','Protected native instance/SSIS exports can remain unavailable under read-only access. Keep prior snapshots; use a separately reviewed privileged service collector only if needed. Do not grant sysadmin merely to make this check green.')
    for row in result['sections']:
        reason=row.get('reason','').lower()
        if row['status']=='unavailable' and any(term in reason for term in ('offline','restor','transition')):
            add('database-state','Wait until '+row['section']+' is online and stable, then retest that database. Its failed snapshot remains protected.');break
    if any(row['status']=='unavailable' for row in result['sections']):
        add('section-failure','Read the unavailable section reason below. Check that feature installation, database visibility and native extraction are supported by this server/client combination. Retest the affected database; metadata-only success does not prove native schema extraction.')
    if result.get('readiness')=='not_ready':add('required-sections','Resolve failed required-section checks, or deliberately revise required_sections after reviewing what is needed. Disabled/not-applicable sections do not satisfy a requirement.')
    result['guided_fixes']=fixes
    return result


def record_capabilities(result,profile,directory):
    """Keep successful evidence across outages; changed settings get a new baseline."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    identity={k:result['execution'].get(k) for k in ('host','platform','effective_uid','sid','account')}
    key=hashlib.sha256(json.dumps({'profile':profile,'execution':identity},sort_keys=True).encode()).hexdigest()
    path=directory/'capabilities.sqlite3'
    # Create privately before SQLite opens it. Use SQLite locking for parallel runs.
    fd=os.open(path,os.O_CREAT|os.O_WRONLY,0o600);os.close(fd)
    db=sqlite3.connect(path,timeout=30)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS baselines(id TEXT PRIMARY KEY,data TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        value=db.execute('SELECT data FROM baselines WHERE id=?',(key,)).fetchone()
        previous=json.loads(value[0]) if value else {'expected':[],'available':[],'identity':None}
        available={row['section'] for row in result['sections'] if row['status']=='available'}
        expected=set(previous['expected'])
        failed=[row['section'] for row in result['sections'] if row['status']=='unavailable']
        missing=expected-available
        lost={name for name in missing if any(name==parent or name.startswith(parent.rstrip('/')+'/') for parent in failed)}
        unknown=missing-lost
        changed_identity=bool(previous['identity'] and result['connection'].get('identity') and previous['identity']!=result['connection']['identity'])
        if changed_identity:result['readiness']='not_ready'
        result['capabilities']={'baseline':key,'initialized':not bool(value),'lost':sorted(lost),
            'unverified':sorted(unknown),'gained':sorted(available-expected) if value else [],
            'recovered':sorted(available-set(previous['available']) & expected),'database_identity_changed':changed_identity}
        state={'expected':sorted(expected|available),'available':sorted(available),
               'identity':previous['identity'] or result['connection'].get('identity'),'observed_at':result['observed_at']}
        db.execute('INSERT OR REPLACE INTO baselines VALUES(?,?)',(key,json.dumps(state)))
        # Publish aggregate evidence only; no schema/configuration payload or secrets.
        from telemetry import envelope,dataset
        payload=envelope('diagnostics',instance=result['name']);payload['observed_at']=result['observed_at']
        dataset(payload,'capability',[{'baseline':key,'lost_count':len(lost),'unverified_count':len(unknown),
            'identity_changed_count':int(changed_identity),'not_ready_count':int(result['readiness']=='not_ready')}],['baseline'],
            {name:'count' for name in ('lost_count','unverified_count','identity_changed_count','not_ready_count')})
        destination=directory/(key+'.json')
        fd,temporary=tempfile.mkstemp(prefix='.capability-',dir=directory)
        try:
            with os.fdopen(fd,'w') as stream:json.dump(payload,stream,indent=2)
            os.replace(temporary,destination)
        finally:Path(temporary).unlink(missing_ok=True)
        db.commit()
        return destination
    finally:db.close()


def run_task(config,path,name,databases=None,skip_sections=(),metadata_only=False,history=None,progress=None):
    from database_connections import task_context,working_directory
    from database_diagnostics import diagnose
    task,env=task_context(config,path,name)
    profile=task['_database_profile']
    profile['enabled']=task.get('enabled',True)
    if databases:profile['databases']=databases
    if metadata_only:profile['schema']=False
    for section in skip_sections:profile.setdefault('sections',{})[section]=False
    with working_directory(task.get('working_directory')):
        execution=account_context()
        result=diagnose(profile,progress=progress,environment=env)
    result['execution']=execution
    result['scope_note']='Uses the managed backup task connection, selection, environment and working directory. Exports are disposable; archive, Git and permission changes are not executed. Running this launcher manually does not prove scheduler execution.'
    requirements(result,task.get('required_sections',[]));guided_fixes(result,profile)
    if history:
        try:
            record_capabilities(result,profile,history)
            if result['capabilities']['database_identity_changed']:
                result['guided_fixes'].append({'id':'identity-changed','applied':False,'action':'The observed database login differs from the established baseline. Check integrated/vault/libpq identity and endpoint settings; deliberately use a new history directory only after reviewing an intended identity change.'})
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error):
            result['capability_history']={'status':'unavailable','reason':'Capability state could not be read or written. Check the private runtime directory and SQLite state; database section results remain available in this report.'}
            result['readiness']='not_ready'
    return result


def display(result):
    from database_diagnostics import display as show
    show(result)
    print('Readiness: '+result['readiness']+' | OS account: '+result['execution'].get('account','unknown'))
    for row in result['requirements']:print(row['status'].upper()+': required '+row['pattern']+' ('+str(row['matched'])+' matches)')
    for fix in result['guided_fixes']:print('Suggested fix ['+fix['id']+']: '+fix['action'])
    if 'capabilities' in result:print('Capability changes: '+json.dumps(result['capabilities'],sort_keys=True))
