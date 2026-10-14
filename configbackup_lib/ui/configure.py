#!/usr/bin/env python3
"""Guided YAML editor with explicitly selected, temporary database access tests."""
from __future__ import annotations
from configbackup_lib import APPLICATION_ROOT
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import yaml
from configbackup_lib.engine.configbackup import ConfigLoader, ConfigError, redact_for_display
from configbackup_lib.ui.editor_forms import edit_boolean,integer,validated
from configbackup_lib.ui.navigation import field_editor


def ask(prompt, default=None, input_fn=input):
    if getattr(input_fn,'console_ui',False):
        value=input_fn.ask(prompt,default)
        if value is None:
            from configbackup_lib.ui.editor_forms import EditCancelled
            raise EditCancelled()
        return value
    value=input_fn(prompt+(f' [{default}]' if default is not None else '')+': ').strip()
    return str(default) if not value and default is not None else value


def boolean(prompt, default=True, input_fn=input):
    if getattr(input_fn,'console_ui',False):
        values=['Yes','No'] if default else ['No','Yes']
        answer=input_fn.choose_value(prompt,values,'Yes' if default else 'No') if hasattr(input_fn,'choose_value') else input_fn.choose(prompt,values)
        if answer is None and getattr(input_fn,'field_active',False) and not getattr(input_fn,'command_active',False):
            from configbackup_lib.ui.editor_forms import EditCancelled
            raise EditCancelled()
        return answer=='Yes'
    while True:
        value=ask(prompt+' (yes/no)','yes' if default else 'no',input_fn).lower()
        if value in ('yes','y','true','1'): return True
        if value in ('no','n','false','0'): return False
        print('Choose yes or no.')


def choice(prompt, values, input_fn=input, *, current=None):
    if getattr(input_fn,'console_ui',False):
        if current is not None and hasattr(input_fn,'choose_value'):return input_fn.choose_value(prompt,values,current)
        return input_fn.choose(prompt,values)
    print(prompt)
    for n,value in enumerate(values,1):print(f'  {n}. {value}')
    while True:
        value=ask('Number (q cancels)',input_fn=input_fn)
        if value.lower()=='q':return None
        if value.isdigit() and 1<=int(value)<=len(values):return values[int(value)-1]
        print('Choose a listed number.')


@field_editor
def json_value(prompt, value, input_fn=input):
    raw=json.dumps(value)
    while True:
        raw=ask(prompt+' (JSON)',raw,input_fn)
        try:
            value=json.loads(raw)
            if getattr(input_fn,'console_ui',False):input_fn.field_error=''
            return value
        except ValueError:
            if getattr(input_fn,'console_ui',False):input_fn.field_error='Invalid JSON; correct the field or press Esc.'
            print('Invalid JSON; correct the field or press Esc.')


def validate(config, kind='backup',path=None):
    from configbackup_lib.configuration.config_schema import validate_types
    validate_types(config)
    if kind=='registry':
        from collectors.system.windows_settings import selections
        selections(config);return
    if kind=='backup':
        resolved=ConfigLoader(Path(path or 'configuration.yaml'))._resolve(config)
        from configbackup_lib.engine.configbackup import BackupEngine
        engine=BackupEngine(resolved,dry_run=True);engine._close_logging()
        from configbackup_lib.engine.database_diagnostics import validate as validate_diagnostic
        profiles=config.get('database_diagnostics',[])
        if not isinstance(profiles,list):raise ValueError('database_diagnostics must be a list')
        names=set()
        for profile in profiles:
            validate_diagnostic(profile)
            if not profile.get('name') or profile['name'] in names:raise ValueError('Diagnostic profile names must be nonempty and unique')
            names.add(profile['name'])
        monitor=config.get('monitoring') or {}
    elif kind=='monitor':monitor=config.get('monitoring',config)
    else:
        if not isinstance(config.get('sources',[]),list):raise ValueError('sources must be a list')
        from zoneinfo import ZoneInfo
        for source in config.get('sources',[]):
            if source.get('type') not in ('sql','postgresql','system'):raise ValueError('Unknown schedule source type')
            for field in ('host','path','timezone'):
                if not source.get(field):raise ValueError('Schedule source requires '+field)
            ZoneInfo(source['timezone'])
            if not isinstance(source.get('enabled',True),bool):raise ValueError('enabled must be boolean')
        return
    from configbackup_lib.reporting.monitoring import validate_config
    validate_config(monitor)


_NO_EXPECTATION=object()


def save(config, path, kind='backup', expected_hash=_NO_EXPECTATION):
    """Validate first; preserve a byte-for-byte backup, then atomically replace."""
    validate(config,kind,path)
    path=Path(path).expanduser().absolute();path.parent.mkdir(parents=True,exist_ok=True)
    from configbackup_lib.configuration.config_editor import render, fingerprint
    if expected_hash is not _NO_EXPECTATION and fingerprint(path)!=expected_hash:raise ValueError('Configuration changed on disk; reload before saving')
    rendered=render(config,path.read_text(encoding='utf-8') if path.exists() else '')
    backup=None
    if path.exists():
        suffix=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup=path.with_name(path.name+'.'+suffix+'.bak');shutil.copy2(path,backup)
    fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(rendered)
            stream.flush();os.fsync(stream.fileno())
        if path.exists():os.chmod(temporary,path.stat().st_mode & 0o777)
        os.replace(temporary,path)
    finally:Path(temporary).unlink(missing_ok=True)
    return backup


def template_tasks(kind, input_fn=input):
    from configbackup_lib.ui.editor_forms import text as ask, flag as boolean, selection as choice
    name=ask('Unique name',input_fn=input_fn)
    if not name:raise ValueError('Name is required')
    if kind=='Directory':
        return [{'name':name,'enabled':True,'type':'directory','source':ask('Source directory',input_fn=input_fn),
                 'destination':ask('Archive destination',name,input_fn),'storage':choice('Storage',['filesystem','both','git'],input_fn) or 'filesystem'}]
    root=APPLICATION_ROOT
    output='${CONFIGBACKUP_STAGING}/'+name
    common={'name':name,'enabled':True,'type':'execute','output_directory':output,'clean_output':True,'sections':{}}
    if kind in ('SQL Server','PostgreSQL'):
        common['connection']=ask('Existing shared connection name (add it in Shared connections first)',input_fn=input_fn)
        common['collection']={}
        common['required_sections']=json_value('Required manifest scope/file patterns',[],input_fn)
    elif kind=='System':
        common.update(executable=ask('Python executable','python3',input_fn),arguments=[str(root/'collectors/system/collect_system.py'),'--include-performance'])
        for component, label in [('iis','IIS'),('dns','DNS Server'),('dhcp','DHCP Server'),('hyperv','Hyper-V')]:
            if boolean('Include optional local Windows '+label+' configuration',False,input_fn):
                common['arguments'] += ['--windows-component',component]
                common['sections']['windows-components/'+component]={'enabled':True}
    else:raise ValueError('Unknown template')
    archive={'name':name+'-archive','enabled':True,'type':'directory','source':output,'destination':name,
             'depends_on':[name],'collection_manifest':True,'storage':'filesystem'}
    return [common,archive]


def edit_mapping(item,input_fn=input,blocked_fields=()):
    while True:
        print(yaml.safe_dump(redact_for_display(item),sort_keys=False).strip())
        operation=choice('Edit item',['Change a field','Toggle enabled','Toggle a section','Done','Choose a field'],input_fn)
        if operation in (None,'Done'):return
        if operation=='Toggle enabled':item['enabled']=not item.get('enabled',True)
        elif operation=='Toggle a section':
            sections=item.setdefault('sections',{})
            if isinstance(sections,list):
                sections={name:True for name in sections};item['sections']=sections
            name=ask('Section name/pattern (e.g. schema, telemetry/health, instance/agent)',input_fn=input_fn)
            if name:
                previous=sections.get(name,True)
                previous=previous.get('enabled',True) if isinstance(previous,dict) else previous
                sections[name]={'enabled':not previous}
        else:
            if operation=='Choose a field':
                from configbackup_lib.configuration.config_schema import BOOL_FIELDS,MAP_FIELDS,LIST_FIELDS
                from configbackup_lib.engine.database_diagnostics import COMMON,ENGINE_FIELDS
                fields=sorted((COMMON|ENGINE_FIELDS[item['engine']]) if item.get('engine') in ENGINE_FIELDS else set(item)|{'enabled','required','sections','state_id','collection_interval_hours','max_scope_bytes'})
                field=choice('Setting',fields,input_fn)
            else:field=ask('Advanced field name',input_fn=input_fn)
            if not field:continue
            if field in blocked_fields:print('Use Rename with references for this field.');continue
            from configbackup_lib.configuration.configuration_profiles import help_text
            print(help_text(field))
            if any(word in field.lower() for word in ('password','token','secret')) and not field.endswith('_env'):
                print('Use an environment reference ending in _env instead.');continue
            from configbackup_lib.configuration.config_schema import field_type
            current=item.get(field);kind=field_type(field,current)
            if redact_for_display({field:current}).get(field) != current and not field.endswith('_env'):
                print('This field contains hidden sensitive values. Use Current configuration to edit non-sensitive children; existing secrets are preserved.');continue
            if kind is bool:
                from configbackup_lib.ui.editor_forms import edit_boolean
                item[field]=edit_boolean(field,current if isinstance(current,bool) else False,input_fn)
            elif kind in (dict,list):
                value=json_value(field,current if current is not None else kind(),input_fn)
                if not isinstance(value,kind):raise ValueError(field+' requires '+kind.__name__)
                if redact_for_display({field:value}).get(field) != value:raise ValueError('Use environment/vault references instead of inline secrets')
                item[field]=value
            elif kind in (int,float):
                from configbackup_lib.ui.editor_forms import scalar
                item[field]=scalar(field,current,input_fn)
            else:
                from configbackup_lib.ui.editor_forms import scalar
                item[field]=scalar(field,current if current is not None else '',input_fn)


def test_mode(input_fn):
    print('This check uses the current accepted configuration edits. Unfinished drafts are excluded.')
    options=['Test current edits']
    if getattr(input_fn,'save_configuration',None):options.append('Save and test')
    selected=choice('Configuration for this check',options+['Back'],input_fn)
    if selected in (None,'Back'):return False
    if selected=='Save and test':
        if not input_fn.save_configuration():print('Check not run: configuration was not saved.');return False
        print('Testing the just-saved configuration.')
    return True


def manage(items, label, maker, input_fn=input,config=None,attach=None,path=None):
    while True:
        if attach and items:attach(items)
        labels=[f'{i+1}: {x.get("name",x.get("id",x.get("host",x.get("path","item"))))} ({"enabled" if x.get("enabled",True) else "disabled"})' for i,x in enumerate(items)]
        selected=choice(label,labels+['Add','Back'],input_fn)
        if selected in (None,'Back'):return
        if selected=='Add':
            from configbackup_lib.ui.navigation import draft
            with draft(input_fn):created=maker(input_fn)
            items.extend(created);continue
        index=labels.index(selected)
        actions=['Edit','Enable/disable','Duplicate']
        if config is not None:actions+=['Rename with references']
        if config is not None and path is not None and items[index].get('connection'):actions+=['Test']
        action=choice(label+' / '+labels[index],actions+['Remove','Back'],input_fn)
        if action in (None,'Back'):continue
        if action=='Test':
            if test_mode(input_fn):
                from configbackup_lib.engine.readiness import run_task,display
                display(run_task(config,path,items[index]['name'],progress=print))
            continue
        if action=='Rename with references':
            from configbackup_lib.configuration.configuration_links import rename,references
            old=items[index]['name'];print('Dependent tasks: '+', '.join(references(config,'task',old)))
            new=ask('New task name',old,input_fn)
            if new!=old and boolean('Update declared references and retain state identity?',False,input_fn):rename(config,'task',old,new);return
        elif action=='Edit':
            original_name=items[index].get('name');edit_mapping(items[index],input_fn,blocked_fields=('name',) if config is not None else ())
            if config is not None and items[index].get('name')!=original_name:
                items[index]['name']=original_name;print('Use Rename with references to change task names safely.')
        elif action=='Enable/disable':items[index]['enabled']=not items[index].get('enabled',True)
        elif action=='Duplicate':
            value=copy.deepcopy(items[index]);key='name' if 'name' in value else 'id' if 'id' in value else 'host'
            new_name=ask('New '+key,str(value.get(key,'item'))+'-copy',input_fn)
            if 'type' in value and value['type'] in ('execute','directory','file','glob','command'):
                from configbackup_lib.configuration.config_editor import duplicate_group
                items.extend(duplicate_group(items,index,new_name))
            else:value[key]=new_name;items.append(value)
        elif action=='Remove':
            from configbackup_lib.configuration.configuration_links import references
            dependents=references(config,'task',items[index].get('name')) if config is not None else []
            if dependents:print('Referenced by: '+', '.join(dependents)+'. Disable this task or update dependencies before removing it.');continue
            if boolean('Remove this item? Disabling keeps its settings',False,input_fn):items.pop(index)


def new_channel(input_fn=input):
    from configbackup_lib.ui.editor_forms import text as ask, flag as boolean, selection as choice
    kind=choice('Notification method',['smtp','webhook','ntfy','heartbeat'],input_fn)
    if kind is None:return []
    value={'id':ask('Unique channel name',kind,input_fn),'type':kind,'enabled':True}
    if kind=='smtp':
        value.update(host=ask('SMTP host',input_fn=input_fn),port=integer('Port',587,input_fn,1,65535),tls=choice('Encryption',['starttls','ssl'],input_fn) or 'starttls',
                     **{'from':ask('From address',input_fn=input_fn),'to':[x.strip() for x in ask('Recipient addresses (comma separated)',input_fn=input_fn).split(',') if x.strip()]})
        value['username_env']=ask('SMTP username environment variable','CONFIGBACKUP_SMTP_USER',input_fn)
        value['password_env']=ask('SMTP password environment variable','CONFIGBACKUP_SMTP_PASSWORD',input_fn)
        value['html']=boolean('Send HTML with a plain-text alternative',True,input_fn)
        from configbackup_lib.ui.configure_features import select_many
        value['sections']=select_many('Choose email summary sections',['alerts','capacity','freshness','schedules','recovery','changes'],['alerts','capacity','freshness','schedules','recovery','changes'],input_fn)
    else:
        value['url_env']=ask('Endpoint URL environment variable','CONFIGBACKUP_NOTIFY_URL',input_fn)
        value['token_env']=ask('Bearer-token environment variable (blank if unused)',input_fn=input_fn)
    value['immediate']=boolean('Send immediate alert transitions',True,input_fn)
    value['digest']=boolean('Send periodic summaries',kind=='smtp',input_fn)
    if value['digest']:value['digest_hours']=float(ask('Digest interval in hours','24',input_fn))
    return [value]


def new_rule(input_fn=input):
    from configbackup_lib.ui.editor_forms import text as ask, flag as boolean, selection as choice
    return [{'id':ask('Unique rule ID',input_fn=input_fn),'enabled':True,'metric':ask('Metric pattern (see dashboard)',input_fn=input_fn),
             'op':choice('Trigger when value is',['gt','lt'],input_fn) or 'gt','warning':validated('Warning threshold','',input_fn,float,error='Enter a numeric warning threshold.'),
             'critical':validated('Critical threshold','',input_fn,float,error='Enter a numeric critical threshold.'),'consecutive':integer('Consecutive observations',2,input_fn,1)}]


def new_source(input_fn=input,schedule=False):
    from configbackup_lib.ui.editor_forms import text as ask, flag as boolean, selection as choice
    source={'enabled':True,'path':ask('Snapshot directory' if schedule else 'Telemetry health.json path/glob',input_fn=input_fn),
            'host':ask('Host label',input_fn=input_fn)}
    if schedule:
        from zoneinfo import ZoneInfo
        source.update(type=choice('Source type',['sql','postgresql','system'],input_fn),timezone=validated('IANA timezone','UTC',input_fn,lambda value:(ZoneInfo(value),value)[1],error='Enter an installed IANA timezone name.'))
    return [source]


def new_registry(input_fn=input):
    from configbackup_lib.ui.editor_forms import text as ask, flag as boolean, selection as choice
    return [{'id':ask('Unique selection ID',input_fn=input_fn),'enabled':True,
       'path':ask(r'Exact key (e.g. HKLM\SOFTWARE\Vendor\Application)',input_fn=input_fn),
       'view':choice('Registry view',['64','32'],input_fn) or '64',
       'recursive':boolean('Include subkeys',False,input_fn),
       **({'values':json_value('Exact value names (empty string selects default value)',[],input_fn)}
          if boolean('Restrict to selected value names',True,input_fn) else {})}]


def launcher_wizard(path,input_fn=input,execution_policy='inherit'):
    from configbackup_lib.engine.startup_launcher import generate
    target=choice('Launcher platform',['windows','macos','linux'],input_fn)
    if not target:return
    providers=['existing',{'windows':'powershell-vault','macos':'keychain','linux':'secret-service'}[target]]
    provider=choice('Authentication: existing Git credentials/environment, or named vault secrets',providers,input_fn)
    if not provider:return
    secrets=[];vault='';account=''
    if provider=='powershell-vault':vault=ask('Already registered vault name',input_fn=input_fn)
    if provider=='keychain':account=ask('Keychain account name',input_fn=input_fn)
    if provider!='existing':
        while boolean('Add a secret reference (never enter its value here)',not secrets,input_fn):
            secrets.append({'env':ask('Environment variable (e.g. GH_TOKEN)',input_fn=input_fn),'name':ask('Secret name/service in the vault',input_fn=input_fn)})
    mode=choice('Launcher action',['backup','diagnostic','setup'],input_fn) or 'backup'
    task=ask('Managed database task name',input_fn=input_fn) if mode=='diagnostic' else ''
    reports=ask('Private report directory',input_fn=input_fn) if mode!='backup' else ''
    history=ask('Optional runtime capability state directory (blank to disable)',input_fn=input_fn) if mode=='diagnostic' else ''
    shell=ask('PowerShell executable (Windows PowerShell 5.1 or PowerShell 7)','powershell.exe',input_fn) if target=='windows' else 'powershell.exe'
    files=generate(path,ask('Fresh launcher output directory',input_fn=input_fn),target,
                   ask('Python executable path/name',sys.executable,input_fn),provider,secrets,vault,account,mode,task,reports,history,shell=shell,execution_policy=execution_policy)
    print('Generated: '+', '.join(str(p) for p in files)+'. Read AUTH-SETUP.txt. Configuration edits must be saved separately.')


def access_wizard(input_fn=input):
    from configbackup_lib.engine.access_scripts import generate
    engine=choice('Database permission script',['sqlserver','postgresql'],input_fn)
    if not engine:return
    principal=ask('Existing dedicated collector login/role',input_fn=input_fn)
    databases=json_value('Database names',[],input_fn)
    if not isinstance(databases,list) or any(not isinstance(n,str) for n in databases):raise ValueError('Database names must be a JSON list of strings')
    if engine=='postgresql':print('This PostgreSQL profile uses cluster-wide pg_read_all_data and BYPASSRLS. It is broader than the selected database list. Review before applying.')
    files=generate(engine,principal,databases,ask('Fresh script output directory',input_fn=input_fn))
    print('Generated for administrator review; nothing was applied: '+', '.join(str(p) for p in files))


def diagnostic_profile(input_fn=input):
    from configbackup_lib.ui.navigation import draft
    with draft(input_fn):return _diagnostic_profile(input_fn)


def _diagnostic_profile(input_fn=input):
    from configbackup_lib.ui.editor_forms import text as ask, flag as boolean, selection as choice
    engine=choice('Database engine',['sqlserver','postgresql'],input_fn)
    if not engine:return None
    profile={'name':ask('Unique connection/profile name',input_fn=input_fn),'engine':engine,
             'access_profile':choice('Use the same access profile as your collector',['read-only','full'],input_fn) or 'read-only'}
    if engine=='sqlserver':
        profile['server']=ask('SQL instance or host,port',input_fn=input_fn)
        profile['pwsh']=ask('PowerShell executable','pwsh',input_fn)
        profile['sqlpackage']=ask('SqlPackage executable','sqlpackage',input_fn)
        profile['trust_server_certificate']=boolean('Trust server certificate without validation (test servers only)',False,input_fn)
        if boolean('Use SQL authentication instead of the process integrated identity',False,input_fn):
            profile['user']=ask('SQL login',input_fn=input_fn)
            profile['password_env']=ask('Existing password environment variable name (not its value)','CONFIGBACKUP_SQL_PASSWORD',input_fn)
    else:
        profile['host']=ask('Host (blank to use libpq defaults)',input_fn=input_fn)
        profile['port']=integer('Port',5432,input_fn,1,65535)
        profile['user']=ask('User (blank to use libpq defaults)',input_fn=input_fn)
        profile['service']=ask('libpq service name (blank if unused)',input_fn=input_fn)
        profile['bin_dir']=ask('PostgreSQL client bin directory (blank for PATH)',input_fn=input_fn)
        profile['maintenance_db']=ask('Maintenance database','postgres',input_fn)
        print('Authentication uses libpq service/pgpass/peer/Kerberos or an existing environment secret.')
        profile['password_env']=ask('Password environment variable name (blank for libpq authentication)',input_fn=input_fn)
    profile['databases']=json_value('Database names/patterns; empty list discovers all eligible databases',[],input_fn)
    profile['schema']=boolean('Test real schema extraction (may take several minutes)',True,input_fn)
    profile['include_health']=boolean('Test optional health queries',False,input_fn)
    profile['include_history']=boolean('Test scheduler history reads',False,input_fn)
    profile['timeout']=integer('Overall collector timeout in seconds',600,input_fn,1)
    from configbackup_lib.engine.database_diagnostics import validate as validate_diagnostic
    validate_diagnostic(profile)
    return profile


def diagnostics_wizard(config,input_fn=input):
    from configbackup_lib.configuration.powershell_policy import environment as policy_environment
    from configbackup_lib.engine.database_diagnostics import diagnose,display,write_report
    profiles=config.get('database_diagnostics',[])
    print('Tests use a separate diagnostic profile: match its identity, database filters and section switches to your collector. Custom task wrappers are not run.')
    while True:
        if profiles:config['database_diagnostics']=profiles
        action=choice('Database connection tests',['Add profile','Edit profile','Test profile','Remove profile','Back'],input_fn)
        if action in (None,'Back'):return
        if action=='Add profile':
            profile=diagnostic_profile(input_fn)
            if profile:profiles.append(profile)
            continue
        if not profiles:print('Add a diagnostic profile first.');continue
        labels=[str(p.get('name','unnamed'))+' ('+str(p.get('engine','unknown'))+')' for p in profiles]
        selected=choice('Choose diagnostic profile',labels,input_fn)
        if selected is None:continue
        index=labels.index(selected)
        if action=='Edit profile':edit_mapping(profiles[index],input_fn)
        elif action=='Remove profile':profiles.pop(index)
        else:
            if not test_mode(input_fn):continue
            result=diagnose(profiles[index],progress=print,environment=policy_environment(config));display(result)
            path=ask('Optional new JSON report path (blank to keep only on screen)',input_fn=input_fn)
            if path:write_report(result,path);print('Saved diagnostic report: '+path)


def shared_connections_wizard(config,path,input_fn=input):
    from configbackup_lib.engine.readiness import run_task,display
    connections=config.get('connections',{})
    while True:
        if connections:config['connections']=connections
        labels={f'{i+1}: {name}':name for i,name in enumerate(connections)}
        selected=choice('Shared connections',list(labels)+['Add connection','Back'],input_fn)
        name=labels.get(selected,selected)
        if selected in (None,'Back'):return
        if selected=='Add connection':
            profile=diagnostic_profile(input_fn)
            if profile:
                new_name=profile.pop('name')
                if not new_name or new_name in connections:raise ValueError('Choose a new nonempty connection name')
                connections[new_name]=profile
            continue
        action=choice('Connection / '+name,['Edit connection','Rename connection','Test managed task','Duplicate connection','Remove connection','Back'],input_fn)
        if action in (None,'Back'):continue
        if action=='Duplicate connection':
            new_name=ask('New connection name',name+'-copy',input_fn)
            if not new_name or new_name in connections:raise ValueError('Choose a new nonempty name')
            connections[new_name]=copy.deepcopy(connections[name]);continue
        if action=='Test managed task':
            names=[t['name'] for t in config.get('tasks',[]) if t.get('connection')==name]
            if not names:print('No managed tasks use this connection yet. Add a task to test its exact collection settings.');continue
            task_name=choice('Managed task',names,input_fn)
            if task_name and test_mode(input_fn):display(run_task(config,path,task_name,progress=print))
        else:
            if action=='Edit connection':edit_mapping(connections[name],input_fn)
            elif action=='Rename connection':
                from configbackup_lib.configuration.configuration_links import rename,references
                print('Referenced by: '+', '.join(references(config,'connection',name)))
                new=ask('New connection name',name,input_fn)
                if new!=name and boolean('Update task connection references?',False,input_fn):rename(config,'connection',name,new);connections=config['connections']
            else:
                from configbackup_lib.configuration.configuration_links import references
                used=references(config,'connection',name)
                if used:print('Referenced by: '+', '.join(used)+'. Update those tasks before removal.')
                elif boolean('Remove unused connection?',False,input_fn):del connections[name]


def wizard(config,path,kind,input_fn=input):
    try:return _wizard(config,path,kind,input_fn)
    finally:
        if getattr(input_fn,'console_ui',False):
            input_fn.command_handler=None;input_fn.checkpoint=None;input_fn.queued_command=None;input_fn.save_after_draft=False;input_fn.save_configuration=None


def _wizard(config,path,kind,input_fn=input):
    original=copy.deepcopy(config)
    from configbackup_lib.ui.editor_forms import EditHistory, EditCancelled
    history=EditHistory()
    from configbackup_lib.configuration.config_editor import fingerprint
    original_hash=fingerprint(path)
    pending_action=None;last_saved='No save this session';validation_status='Not checked this session';validated_snapshot=None;dependency_rows=[]
    from configbackup_lib.ui.navigation import grouped_choice, Navigate
    menu_area=[None]
    before_action=copy.deepcopy(config)
    checkpoint_value=copy.deepcopy(config)
    def checkpoint():
        nonlocal checkpoint_value,validation_status
        history.record(checkpoint_value,config);checkpoint_value=copy.deepcopy(config)
        if validated_snapshot is not None and config!=validated_snapshot:validation_status='Changed since last validation'
        if getattr(input_fn,'console_ui',False):
            from configbackup_lib.ui.navigation import changed_count
            input_fn.dirty=config!=original
            input_fn.overview_status=f'{changed_count(original,config)} unsaved changes; saved: {last_saved}; validation: {validation_status}'
    def command(action,mode='menu',draft=False):
        nonlocal original_hash,before_action,checkpoint_value,last_saved,validation_status,validated_snapshot
        checkpoint()
        if action=='Save':
            if draft:
                selection=choice('An item is still being created',['Finish creating this item','Save completed changes only'],input_fn)
                if selection!='Save completed changes only':
                    if selection=='Finish creating this item' and getattr(input_fn,'console_ui',False):
                        input_fn.save_after_draft=True;print('Save will run after the item is complete. Cancelling the item cancels that save.')
                    return
            try:
                backup=save(config,path,kind,expected_hash=original_hash)
            except (ValueError,TypeError,KeyError,OSError,ConfigError) as exc:
                validation_status='Save validation/write failed';validated_snapshot=copy.deepcopy(config);print('Save failed; edits retained:',exc);return False
            original.clear();original.update(copy.deepcopy(config));original_hash=fingerprint(path)
            before_action=copy.deepcopy(config)
            if getattr(input_fn,'console_ui',False):input_fn.dirty=False
            last_saved=dt.datetime.now().astimezone().isoformat(timespec='seconds');validation_status='Valid';validated_snapshot=copy.deepcopy(config)
            print('Saved '+str(path)+('; backup: '+str(backup) if backup else '')+'. Continue editing.');return True
        elif action=='Validate':
            try:
                validate(config,kind,path);validation_status='Valid';validated_snapshot=copy.deepcopy(config);print('Configuration is valid. Unfinished items are not included.')
            except (ValueError,TypeError,KeyError,OSError,ConfigError) as exc:
                validation_status='Invalid';validated_snapshot=copy.deepcopy(config);print('Validation failed; edits retained:',exc)
                from configbackup_lib.ui.navigation import validation_targets
                targets=validation_targets(config,exc)
                selected=choice('Validation issues',list(targets)+['Back'],input_fn)
                if selected in targets:
                    if (mode in ('text','multi') or draft or getattr(input_fn,'field_active',False)) and not boolean('Leave unfinished input to open the selected location?',False,input_fn):return
                    if getattr(input_fn,'console_ui',False):input_fn.save_after_draft=False;input_fn.queued_command=None
                    raise Navigate('open',targets[selected])
        elif action=='Help':
            print('Context: '+getattr(input_fn,'title','Configuration'))
            print(getattr(input_fn,'field_context',''))
            print(getattr(input_fn,'operation_context',''))
            print('Ctrl+S/F2 saves accepted fields. F3 validates; F4 reviews changes. F5/F6 undo/redo. F8 searches all areas and configured setting paths. Esc cancels input. F10 exits with unsaved-input protection.')
            print('Tests explicitly identify current edits versus saved configuration. External operations are not undone by configuration undo.')
        elif action=='Command palette':
            from configbackup_lib.ui.navigation import palette_entries
            entries=palette_entries(config,choices)
            selected=choice('Find command or setting',list(entries)+['Back'],input_fn)
            if selected in (None,'Back'):return
            if (mode in ('text','multi') or draft or getattr(input_fn,'field_active',False)) and not boolean('Leave unfinished input to open the selected location?',False,input_fn):return
            if getattr(input_fn,'console_ui',False):input_fn.save_after_draft=False;input_fn.queued_command=None
            raise Navigate('open',entries[selected])
        elif action=='Preview changes':
            from configbackup_lib.configuration.config_editor import semantic_diff
            print(semantic_diff(original,config))
        elif action in ('Undo last action','Redo last action'):
            if (mode in ('text','multi') or draft or getattr(input_fn,'field_active',False)) and not boolean('Discard the current unfinished field/item and return to the main menu?',False,input_fn):return
            if getattr(input_fn,'console_ui',False):input_fn.save_after_draft=False;input_fn.queued_command=None
            done=history.undo(config) if action=='Undo last action' else history.redo(config)
            checkpoint_value=copy.deepcopy(config)
            print(('Undone.' if action=='Undo last action' else 'Redone.') if done else 'No history available.')
            raise Navigate('home')
        elif action=='Quit without saving':
            if (config!=original or mode in ('text','multi') or draft or getattr(input_fn,'field_active',False)) and not boolean('Discard unsaved configuration changes and unfinished input?',False,input_fn):return
            raise Navigate('exit')
    if getattr(input_fn,'console_ui',False):input_fn.command_handler=command;input_fn.checkpoint=checkpoint;input_fn.save_configuration=lambda:command('Save')
    if getattr(input_fn,'console_ui',False):
        from configbackup_lib.ui.config_browser import summary
        print(summary(config))
        from configbackup_lib.ui.config_dependencies import inspect as inspect_dependencies
        dependency_rows=inspect_dependencies()
        for row in dependency_rows:
            if row['status']!='ready':print('Dependency attention: '+row['name']+' — '+row['detail']+'. Open Python dependencies to repair.')
    while True:
        choices=['Tasks','Monitoring sources','Alert rules','Notification channels','Show configuration'] if kind=='backup' else ['Sources','Report/analysis settings','Show configuration'] if kind=='schedule' else ['Monitoring sources','Alert rules','Notification channels','Monitoring settings','Show configuration']
        if kind=='registry':choices=['Registry selections','Default registry selections','Show configuration']
        if kind=='backup':
            choices += ['Generate startup launcher','Generate database access scripts','Database connection tests','Shared connections','Check setup','Recovery files and export','Autodetect and guided setup','Verify and rehearse setup recovery','Discover files from scheduled jobs','Recovery and review workflows','HTML review reports','Optional collectors and discovery','Collection profiles','Effective preview','Saved collection profiles','Performance recording and reports','Recovery readiness targets','Sensitive collection','PowerShell execution policy','Launcher manager','Recovery status and reset','Windows readiness','Task defaults','Retention settings','Git settings','Backup settings','Deletion settings','Monitoring settings']
        choices += ['Current configuration','Python dependencies','Configuration summary']
        checkpoint()
        try:
            if pending_action is not None:action=pending_action;pending_action=None
            else:action=grouped_choice(choices,menu_area,input_fn)
        except Navigate as navigation:
            menu_area[0]=None
            if navigation.destination=='open':pending_action=navigation.payload
            if navigation.destination=='exit':return False
            continue
        before_action=copy.deepcopy(config)
        if getattr(input_fn,'console_ui',False):input_fn.field_context='';input_fn.field_error=''
        target_path=None;edit_target=False
        if isinstance(action,tuple):
            if action[0] in ('setting','validation'):target_path=action[1];edit_target=action[0]=='validation';action='Current configuration'
            else:action=action[1]
        try:
            from configbackup_lib.ui.navigation import operation_boundary
            boundary=operation_boundary(action)
            if boundary:print(boundary)
            if getattr(input_fn,'console_ui',False):input_fn.operation_context=boundary
            monitor=(config.get('monitoring') or {}) if kind=='backup' else config.get('monitoring',config)
            def attach_monitor(key,value):
                monitor[key]=value
                if kind=='backup':config['monitoring']=monitor
            if action in ('Save','Validate','Preview changes','Undo last action','Redo last action','Help','Command palette','Quit without saving'):
                if getattr(input_fn,'console_ui',False):input_fn.execute_command(action,'menu')
                else:command(action)
            elif action=='Configuration summary':
                from configbackup_lib.ui.config_browser import summary
                from configbackup_lib.ui.navigation import changed_count
                tasks=config.get('tasks',[])
                print(summary(config))
                print(f'Tasks: {len(tasks)}; enabled: {sum(t.get("enabled",True) is True for t in tasks)}; disabled: {sum(t.get("enabled",True) is False for t in tasks)}')
                print(f'Unsaved changes: {changed_count(original,config)}; last saved: {last_saved}; validation: {validation_status}')
                print('Dependency attention: '+(', '.join(r['name'] for r in dependency_rows if r['status']!='ready') or 'none detected'))
            elif action in ('Task defaults','Retention settings','Git settings','Backup settings','Deletion settings','Monitoring settings'):
                key={'Task defaults':'defaults','Retention settings':'retention_policies','Git settings':'git','Backup settings':'backup','Deletion settings':'deletion','Monitoring settings':'monitoring'}[action]
                from configbackup_lib.ui.config_browser import browse
                browse(config,input_fn,section=None if kind=='monitor' and key=='monitoring' and key not in config else key)
            elif action=='Launcher manager':
                from configbackup_lib.engine.launcher_manager import wizard as launcher_manager
                launcher_manager(config,path,input_fn)
            elif action=='Recovery status and reset':
                from configbackup_lib.recovery.recovery_console import wizard as recovery_console
                recovery_console(config,path,input_fn)
            elif action=='Windows readiness':
                from configbackup_lib.engine.windows_readiness import inspect as windows_readiness
                print(json.dumps(windows_readiness(config),indent=2))
            elif action=='PowerShell execution policy':
                from configbackup_lib.configuration.powershell_policy import wizard as policy_wizard
                policy_wizard(config,input_fn)
            elif action=='Current configuration':
                from configbackup_lib.ui.config_browser import browse
                browse(config,input_fn,start=target_path,edit_start=edit_target)
            elif action=='Python dependencies':
                from configbackup_lib.ui.config_dependencies import wizard as dependency_wizard
                try:dependency_wizard(input_fn)
                finally:
                    from configbackup_lib.ui.config_dependencies import inspect as inspect_dependencies
                    dependency_rows=inspect_dependencies()
            elif action=='Sensitive collection':
                from configbackup_lib.git.sensitive_data import wizard as sensitive_wizard
                sensitive_wizard(config,input_fn)
            elif action=='Recovery files and export':
                from configbackup_lib.recovery.recovery_assets import wizard as recovery_wizard
                recovery_wizard(config,path,input_fn)
            elif action=='Recovery and review workflows':
                from configbackup_lib.ui.workflow_menu import wizard as workflow_wizard
                workflow_wizard(config,path,input_fn)
            elif action=='Discover files from scheduled jobs':
                from configbackup_lib.engine.discovery_review import wizard as schedule_discovery_wizard
                schedule_discovery_wizard(config,input_fn)
            elif action=='Verify and rehearse setup recovery':
                from configbackup_lib.recovery.setup_recovery import wizard as setup_recovery_wizard
                setup_recovery_wizard(input_fn)
            elif action=='Autodetect and guided setup':
                from configbackup_lib.engine.autodetect import wizard as autodetect_wizard
                autodetect_wizard(config,input_fn)
            elif action=='HTML review reports':
                from configbackup_lib.ui.configure_features import reporting_wizard
                reporting_wizard(config,input_fn)
            elif action=='Optional collectors and discovery':
                from configbackup_lib.ui.configure_features import optional_wizard
                optional_wizard(config,input_fn)
            elif action=='Collection profiles':
                from configbackup_lib.ui.configure_features import profiles_wizard
                profiles_wizard(config,input_fn)
            elif action=='Saved collection profiles':
                from configbackup_lib.configuration.configuration_profiles import wizard as profile_wizard
                profile_wizard(config,input_fn)
            elif action=='Recovery readiness targets':
                from configbackup_lib.recovery.recovery_readiness import wizard as targets_wizard
                targets_wizard(config,input_fn)
            elif action=='Performance recording and reports':
                from configbackup_lib.engine.native_recording import wizard as performance_wizard
                performance_wizard(config,path,input_fn)
            elif action=='Effective preview':
                from configbackup_lib.configuration.config_editor import effective_preview
                print(json.dumps(effective_preview(config,path),indent=2))
            elif action=='Preview changes':
                from configbackup_lib.configuration.config_editor import semantic_diff
                print(semantic_diff(original,config))
            elif action=='Registry selections':manage(config.get('registry',[]),'Registry selections',new_registry,input_fn,attach=lambda value:config.__setitem__('registry',value))
            elif action=='Generate startup launcher':launcher_wizard(path,input_fn,(config.get('options') or {}).get('powershell_execution_policy','inherit'))
            elif action=='Generate database access scripts':access_wizard(input_fn)
            elif action=='Database connection tests':diagnostics_wizard(config,input_fn)
            elif action=='Shared connections':shared_connections_wizard(config,path,input_fn)
            elif action=='Check setup':
                from configbackup_lib.engine.setup_checks import run
                if test_mode(input_fn):print(json.dumps(run(config,path),indent=2))
            elif action=='Default registry selections':config['include_defaults']=edit_boolean('Include the built-in selected settings',config.get('include_defaults',True),input_fn)
            elif action=='Tasks':
                manage(config.get('tasks',[]),'Backup tasks',lambda inp:template_tasks(choice('Task template',['Directory','SQL Server','PostgreSQL','System'],inp),inp),input_fn,config=config,attach=lambda value:config.__setitem__('tasks',value),path=path)
            elif action=='Monitoring sources':manage(monitor.get('sources',[]),'Telemetry sources',new_source,input_fn,attach=lambda value:attach_monitor('sources',value))
            elif action=='Sources':manage(config.get('sources',[]),'Schedule sources',lambda inp:new_source(inp,True),input_fn,attach=lambda value:config.__setitem__('sources',value))
            elif action=='Alert rules':manage(monitor.get('rules',[]),'Alert rules',new_rule,input_fn,attach=lambda value:attach_monitor('rules',value))
            elif action=='Notification channels':manage(monitor.get('notifications',{}).get('channels',[]),'Notification channels',new_channel,input_fn,attach=lambda value:attach_monitor('notifications',{**monitor.get('notifications',{}),'channels':value}))
            elif action=='Report/analysis settings':
                group=choice('Settings',['reports','analysis'],input_fn)
                if group:config[group]=json_value(group,config.get(group,{}),input_fn)
            elif action=='Show configuration':print(yaml.safe_dump(redact_for_display(config),sort_keys=False))
        except Navigate as navigation:
            menu_area[0]=None
            if navigation.destination=='open':pending_action=navigation.payload
            if navigation.destination=='exit':return False
        except EditCancelled:
            if getattr(input_fn,'console_ui',False):input_fn.queued_command=None;input_fn.save_after_draft=False
            print('Cancelled unfinished input; previously accepted edits are retained.')
        except (ValueError,TypeError,KeyError,OSError,ConfigError) as exc:
            if getattr(input_fn,'console_ui',False):input_fn.queued_command=None;input_fn.save_after_draft=False
            print('Please correct:',exc)
        checkpoint()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    ui_mode=parser.add_mutually_exclusive_group()
    ui_mode.add_argument('--plain',action='store_true',help='Use numbered text prompts instead of the full-screen interface')
    ui_mode.add_argument('--console',action='store_true',help='Require the full-screen keyboard interface')
    parser.add_argument('--config',default='configbackup.yaml')
    parser.add_argument('--repair-config',action='store_true',help='Restore a reviewed valid backup while preserving the current file')
    parser.add_argument('--kind',choices=['backup','monitor','schedule','registry'],default='backup')
    parser.add_argument('--preview',action='store_true',help='Show resolved paths, switches, connection and report settings; no collection')
    parser.add_argument('--undo',metavar='BACKUP',help='Restore an editor .bak after validating it, preserving a backup of the current file')
    parser.add_argument('--autodetect',action='store_true',help='Read local client/service presence and suggest setup; no installations or configuration changes')
    parser.add_argument('--recovery-inventory',action='store_true',help='Show related setup assets, hashes and missing recovery requirements')
    parser.add_argument('--export-recovery',metavar='DIRECTORY',help='Write program/configuration/launcher recovery files into a new private directory')
    parser.add_argument('--catalog',action='store_true',help='List optional collectors and requirements')
    parser.add_argument('--check',action='store_true',help='Validate without opening the wizard')
    parser.add_argument('--diagnose-database',metavar='PROFILE',help='Run a named database_diagnostics profile in disposable scratch space')
    parser.add_argument('--diagnostic-report',help='Optional new JSON output file; never overwritten')
    parser.add_argument('--diagnose-task',metavar='TASK',help='Test the exact managed backup task connection and environment')
    parser.add_argument('--database',action='append',help='Limit diagnostics to a database name/pattern; repeatable')
    parser.add_argument('--skip-section',action='append',default=[],help='Disable a section name/pattern for this test only; repeatable')
    parser.add_argument('--metadata-only',action='store_true',help='Skip native schema extraction, with explicit not-tested status')
    parser.add_argument('--capability-history',help='Private runtime directory for capability baselines and monitoring telemetry')
    parser.add_argument('--report-directory',help='Write a uniquely named private JSON report each run (scheduler launchers)')
    parser.add_argument('--setup-check',action='store_true',help='Check local paths/tools, credential references, Git remote read and notification configuration')
    parser.add_argument('--probe-notifications',action='store_true',help='With --setup-check, test SMTP TLS/login without sending mail')
    parser.add_argument('--test-notification',metavar='CHANNEL',help='Explicitly SEND one harmless test to an enabled channel')
    parser.add_argument('--generate-access-fix',metavar='DIRECTORY',help='With --diagnose-task, generate read-access SQL for review; never apply it')
    parser.add_argument('--principal',help='Existing database principal for generated permission script; defaults to connection user')
    args=parser.parse_args(argv);path=Path(args.config)
    from configbackup_lib.configuration.configuration_repair import load as load_configuration, repair
    if args.repair_config:
        try:return 0 if repair(path,args.kind) else 2
        except (ValueError,OSError,ConfigError) as exc:print('Repair failed:',exc);return 2
    try:config=load_configuration(path)
    except (ValueError,OSError,UnicodeError) as exc:print('Configuration error:',exc);return 2
    if not isinstance(config,dict):parser.error('Configuration must be a mapping')
    modes=[args.autodetect,args.recovery_inventory,args.export_recovery,args.preview,args.undo,args.catalog,args.check,args.diagnose_database,args.diagnose_task,args.setup_check,args.test_notification]
    if sum(bool(x) for x in modes)>1:parser.error('Choose one check, diagnostic, setup or notification action')
    if (args.diagnostic_report or args.report_directory) and not (args.diagnose_database or args.diagnose_task or args.setup_check):parser.error('Report output requires a diagnostic or setup action')
    if args.diagnostic_report and args.report_directory:parser.error('Choose a report file or a report directory')
    if (args.database or args.skip_section or args.metadata_only or args.capability_history or args.generate_access_fix or args.principal) and not args.diagnose_task:parser.error('Task selection/history/fix options require --diagnose-task')
    if args.probe_notifications and not args.setup_check:parser.error('--probe-notifications requires --setup-check')
    def report(result):
        from configbackup_lib.engine.database_diagnostics import write_report
        target=args.diagnostic_report
        if args.report_directory:
            import uuid
            directory=Path(args.report_directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            target=directory/('readiness-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex+'.json')
        if target:write_report(result,target);print('Saved report: '+str(target))
    if args.autodetect:
        from configbackup_lib.engine.autodetect import discover
        print(json.dumps(discover(),indent=2));return 0
    if args.recovery_inventory or args.export_recovery:
        from configbackup_lib.recovery.recovery_assets import inventory,export_recovery
        try:result=export_recovery(config,path,args.export_recovery) if args.export_recovery else inventory(config,path)
        except (ValueError,OSError,ConfigError) as exc:print('Recovery setup error:',exc);return 2
        print(json.dumps(result,indent=2));return 0 if result['status']=='complete' else 6
    if args.catalog:
        from configbackup_lib.engine.collector_catalog import catalog
        print(json.dumps(catalog(),indent=2));return 0
    if args.preview:
        from configbackup_lib.configuration.config_editor import effective_preview
        print(json.dumps(effective_preview(config,path),indent=2));return 0
    if args.undo:
        previous=Path(args.undo).resolve()
        if previous.parent!=path.resolve().parent or not previous.name.startswith(path.name+'.') or not previous.name.endswith('.bak'):
            parser.error('Choose a timestamped editor backup next to this configuration')
        restored=yaml.safe_load(previous.read_text(encoding='utf-8'));validate(restored,args.kind)
        backup=save(restored,path,args.kind);print('Restored configuration; previous settings saved to '+str(backup));return 0
    if args.test_notification:
        from configbackup_lib.engine.setup_checks import test_notification
        try:test_notification(config,args.test_notification)
        except Exception:print('Notification test failed; inspect channel settings and credentials. Raw provider response omitted.');return 1
        print('Test notification submitted. Verify receipt at the destination.');return 0
    if args.setup_check:
        from configbackup_lib.engine.setup_checks import run
        result=run(config,path,args.probe_notifications);print(json.dumps(result,indent=2));report(result)
        return 0 if result['status']=='complete' else 6
    if args.diagnose_task:
        from configbackup_lib.engine.readiness import run_task,display
        try:
            result=run_task(config,path,args.diagnose_task,args.database,args.skip_section,args.metadata_only,args.capability_history,print)
            display(result);report(result)
            if args.generate_access_fix:
                from configbackup_lib.configuration.database_connections import task_context
                from configbackup_lib.engine.access_scripts import generate
                task,_=task_context(config,path,args.diagnose_task);profile=task['_database_profile']
                principal=args.principal or profile.get('user');databases=args.database or profile.get('databases',[])
                if not principal or not databases or any(any(c in name for c in '*?[') for name in databases):
                    raise ValueError('Fix scripts need an explicit principal and literal database names; supply --principal and --database')
                paths=generate(profile['engine'],principal,databases,args.generate_access_fix)
                print('Generated for administrator review only: '+', '.join(map(str,paths)))
        except (ValueError,OSError,ConfigError) as exc:print('Readiness error:',exc);return 2
        return 1 if result['readiness']=='not_ready' else 6 if result['readiness']=='ready_with_warnings' else 0
    if args.diagnose_database:
        from configbackup_lib.configuration.powershell_policy import environment as policy_environment
        from configbackup_lib.engine.database_diagnostics import diagnose,display,write_report
        matches=[p for p in config.get('database_diagnostics',[]) if p.get('name')==args.diagnose_database]
        if len(matches)!=1:parser.error('Choose a unique database_diagnostics profile name')
        try:
            result=diagnose(matches[0],progress=print,environment=policy_environment(config));display(result)
            report(result)
        except (ValueError,OSError) as exc:print('Diagnostic error:',exc);return 2
        return 0 if result['status'] in ('complete','disabled') else 6 if result['status']=='partial' else 1
    if args.check:validate(config,args.kind);print('Configuration valid');return 0
    def interact(input_fn=input):
        if args.kind=='backup' and not config:config.update({'backup':{'root':ask('Backup archive root',input_fn=input_fn)},'tasks':[]})
        return wizard(config,path,args.kind,input_fn)
    interactive=sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get('TERM')!='dumb'
    if args.console and not interactive:parser.error('--console requires an interactive terminal')
    if not args.plain and interactive:
        try:from configbackup_lib.ui.console_ui import ConsoleUI
        except ImportError:
            if args.console:parser.error('Console dependencies missing. Run: python -m pip install -r requirements-console.txt')
            print('Console dependencies are missing. The numbered editor is still available.')
            if boolean('Install console dependencies now',False):
                from configbackup_lib.ui.config_dependencies import install
                if install(['prompt_toolkit'],update=True):
                    print('Restart this command to use the console interface.');return 0
            print('Using plain menus. You can also use Python dependencies from the main menu.')
        else:return ConsoleUI(path).run(interact)
    try:interact()
    except (EOFError,KeyboardInterrupt):print('\nCancelled; configuration file unchanged.')
    return 0

if __name__=='__main__':raise SystemExit(main())
