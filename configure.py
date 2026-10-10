#!/usr/bin/env python3
"""Guided, cross-platform ConfigBackup YAML editor. Does not run collectors or send mail."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import yaml
from configbackup import ConfigLoader, ConfigError, redact_for_display


def ask(prompt, default=None, input_fn=input):
    value=input_fn(prompt+(f' [{default}]' if default is not None else '')+': ').strip()
    return str(default) if not value and default is not None else value


def boolean(prompt, default=True, input_fn=input):
    while True:
        value=ask(prompt+' (yes/no)','yes' if default else 'no',input_fn).lower()
        if value in ('yes','y','true','1'): return True
        if value in ('no','n','false','0'): return False
        print('Choose yes or no.')


def choice(prompt, values, input_fn=input):
    print(prompt)
    for n,value in enumerate(values,1):print(f'  {n}. {value}')
    while True:
        value=ask('Number (q cancels)',input_fn=input_fn)
        if value.lower()=='q':return None
        if value.isdigit() and 1<=int(value)<=len(values):return values[int(value)-1]
        print('Choose a listed number.')


def json_value(prompt, value, input_fn=input):
    while True:
        raw=ask(prompt+' (JSON)',json.dumps(value),input_fn)
        try:return json.loads(raw)
        except ValueError as exc:print('Invalid JSON:',exc)


def validate(config, kind='backup'):
    if kind=='registry':
        from collectors.system.windows_settings import selections
        selections(config);return
    if kind=='backup':
        ConfigLoader(Path('configuration.yaml'))._resolve(config)
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
    from monitoring import validate_config
    validate_config(monitor)


def save(config, path, kind='backup'):
    """Validate first; preserve a byte-for-byte backup, then atomically replace."""
    validate(config,kind)
    path=Path(path).expanduser().absolute();path.parent.mkdir(parents=True,exist_ok=True)
    backup=None
    if path.exists():
        suffix=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup=path.with_name(path.name+'.'+suffix+'.bak');shutil.copy2(path,backup)
    fd,temporary=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            yaml.safe_dump(config,stream,sort_keys=False,allow_unicode=True)
            stream.flush();os.fsync(stream.fileno())
        if path.exists():os.chmod(temporary,path.stat().st_mode & 0o777)
        os.replace(temporary,path)
    finally:Path(temporary).unlink(missing_ok=True)
    return backup


def template_tasks(kind, input_fn=input):
    name=ask('Unique name',input_fn=input_fn)
    if not name:raise ValueError('Name is required')
    if kind=='Directory':
        return [{'name':name,'enabled':True,'type':'directory','source':ask('Source directory',input_fn=input_fn),
                 'destination':ask('Archive destination',name,input_fn),'storage':choice('Storage',['filesystem','both','git'],input_fn) or 'filesystem'}]
    root=Path(__file__).resolve().parent
    output='${CONFIGBACKUP_STAGING}/'+name
    common={'name':name,'enabled':True,'type':'execute','output_directory':output,'clean_output':True,'sections':{}}
    if kind=='SQL Server':
        endpoint=ask('SQL instance or host,port',input_fn=input_fn)
        common.update(executable=ask('PowerShell executable','pwsh',input_fn),arguments=['-NoProfile','-File',str(root/'collectors/sqlserver/Collect-SqlServerConfiguration.ps1'),'-SqlInstance',endpoint,'-IncludeHealthMetrics','-IncludeAgentHistory'])
        print('Uses the process authentication context. SQL credentials require your protected credential wrapper; do not put passwords in arguments.')
    elif kind=='PostgreSQL':
        host=ask('Host','127.0.0.1',input_fn);port=ask('Port','5432',input_fn)
        common.update(executable=ask('Python executable','python3',input_fn),arguments=[str(root/'collectors/postgresql/collect_postgresql.py'),'--host',host,'--port',port,'--include-health','--include-scheduler-history'])
        print('Use libpq service/.pgpass or protected environment authentication.')
    elif kind=='System':
        common.update(executable=ask('Python executable','python3',input_fn),arguments=[str(root/'collectors/system/collect_system.py'),'--include-performance'])
    else:raise ValueError('Unknown template')
    archive={'name':name+'-archive','enabled':True,'type':'directory','source':output,'destination':name,
             'depends_on':[name],'collection_manifest':True,'storage':'filesystem'}
    return [common,archive]


def edit_mapping(item,input_fn=input):
    while True:
        print(yaml.safe_dump(redact_for_display(item),sort_keys=False).strip())
        operation=choice('Edit item',['Change a field','Toggle enabled','Toggle a section','Done'],input_fn)
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
            field=ask('Field name',input_fn=input_fn)
            if not field:continue
            if any(word in field.lower() for word in ('password','token','secret')) and not field.endswith('_env'):
                print('Use an environment reference ending in _env instead.');continue
            current=item.get(field,'')
            if isinstance(current,bool):item[field]=boolean(field,current,input_fn)
            elif isinstance(current,(dict,list,int,float)) or current is None:item[field]=json_value(field,current,input_fn)
            else:item[field]=ask(field,current,input_fn)


def manage(items, label, maker, input_fn=input):
    while True:
        action=choice(label,['Add','Edit','Duplicate','Enable/disable','Remove','Back'],input_fn)
        if action in (None,'Back'):return
        if action=='Add':items.extend(maker(input_fn));continue
        if not items:print('No items yet.');continue
        labels=[f'{i+1}: {x.get("name",x.get("id",x.get("host",x.get("path","item"))))} ({"enabled" if x.get("enabled",True) else "disabled"})' for i,x in enumerate(items)]
        selected=choice('Choose item',labels,input_fn)
        if selected is None:continue
        index=labels.index(selected)
        if action=='Edit':edit_mapping(items[index],input_fn)
        elif action=='Enable/disable':items[index]['enabled']=not items[index].get('enabled',True)
        elif action=='Duplicate':
            value=copy.deepcopy(items[index]);key='name' if 'name' in value else 'id' if 'id' in value else 'host'
            value[key]=ask('New '+key,str(value.get(key,'item'))+'-copy',input_fn);items.append(value)
        elif boolean('Remove this item? Disabling keeps its settings',False,input_fn):items.pop(index)


def new_channel(input_fn=input):
    kind=choice('Notification method',['smtp','webhook','ntfy','heartbeat'],input_fn)
    if kind is None:return []
    value={'id':ask('Unique channel name',kind,input_fn),'type':kind,'enabled':True}
    if kind=='smtp':
        value.update(host=ask('SMTP host',input_fn=input_fn),port=int(ask('Port','587',input_fn)),tls=choice('Encryption',['starttls','ssl'],input_fn) or 'starttls',
                     **{'from':ask('From address',input_fn=input_fn),'to':[x.strip() for x in ask('Recipient addresses (comma separated)',input_fn=input_fn).split(',') if x.strip()]})
        value['username_env']=ask('SMTP username environment variable','CONFIGBACKUP_SMTP_USER',input_fn)
        value['password_env']=ask('SMTP password environment variable','CONFIGBACKUP_SMTP_PASSWORD',input_fn)
        value['html']=boolean('Send HTML with a plain-text alternative',True,input_fn)
        value['sections']=['alerts','capacity','freshness','schedules','recovery','changes']
    else:
        value['url_env']=ask('Endpoint URL environment variable','CONFIGBACKUP_NOTIFY_URL',input_fn)
        value['token_env']=ask('Bearer-token environment variable (blank if unused)',input_fn=input_fn)
    value['immediate']=boolean('Send immediate alert transitions',True,input_fn)
    value['digest']=boolean('Send periodic summaries',kind=='smtp',input_fn)
    if value['digest']:value['digest_hours']=float(ask('Digest interval in hours','24',input_fn))
    return [value]


def new_rule(input_fn=input):
    return [{'id':ask('Unique rule ID',input_fn=input_fn),'enabled':True,'metric':ask('Metric pattern (see dashboard)',input_fn=input_fn),
             'op':choice('Trigger when value is',['gt','lt'],input_fn) or 'gt','warning':float(ask('Warning threshold',input_fn=input_fn)),
             'critical':float(ask('Critical threshold',input_fn=input_fn)),'consecutive':int(ask('Consecutive observations','2',input_fn))}]


def new_source(input_fn=input,schedule=False):
    source={'enabled':True,'path':ask('Snapshot directory' if schedule else 'Telemetry health.json path/glob',input_fn=input_fn),
            'host':ask('Host label',input_fn=input_fn)}
    if schedule:source.update(type=choice('Source type',['sql','postgresql','system'],input_fn),timezone=ask('IANA timezone','UTC',input_fn))
    return [source]


def new_registry(input_fn=input):
    return [{'id':ask('Unique selection ID',input_fn=input_fn),'enabled':True,
       'path':ask(r'Exact key (e.g. HKLM\SOFTWARE\Vendor\Application)',input_fn=input_fn),
       'view':choice('Registry view',['64','32'],input_fn) or '64',
       'recursive':boolean('Include subkeys',False,input_fn),
       **({'values':json_value('Exact value names (empty string selects default value)',[],input_fn)}
          if boolean('Restrict to selected value names',True,input_fn) else {})}]


def launcher_wizard(path,input_fn=input):
    from startup_launcher import generate
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
    files=generate(path,ask('Fresh launcher output directory',input_fn=input_fn),target,
                   ask('Python executable path/name','python' if target=='windows' else 'python3',input_fn),provider,secrets,vault,account)
    print('Generated: '+', '.join(str(p) for p in files)+'. Read AUTH-SETUP.txt. Configuration edits must be saved separately.')


def access_wizard(input_fn=input):
    from access_scripts import generate
    engine=choice('Database permission script',['sqlserver','postgresql'],input_fn)
    if not engine:return
    principal=ask('Existing dedicated collector login/role',input_fn=input_fn)
    databases=json_value('Database names',[],input_fn)
    if not isinstance(databases,list) or any(not isinstance(n,str) for n in databases):raise ValueError('Database names must be a JSON list of strings')
    if engine=='postgresql':print('This PostgreSQL profile uses cluster-wide pg_read_all_data and BYPASSRLS. It is broader than the selected database list. Review before applying.')
    files=generate(engine,principal,databases,ask('Fresh script output directory',input_fn=input_fn))
    print('Generated for administrator review; nothing was applied: '+', '.join(str(p) for p in files))


def wizard(config,path,kind,input_fn=input):
    original=copy.deepcopy(config)
    while True:
        choices=['Tasks','Monitoring sources','Alert rules','Notification channels','Global switches','Show configuration','Validate','Save','Quit without saving'] if kind=='backup' else ['Sources','Report/analysis settings','Show configuration','Validate','Save','Quit without saving'] if kind=='schedule' else ['Monitoring sources','Alert rules','Notification channels','Global switches','Show configuration','Validate','Save','Quit without saving']
        if kind=='registry':choices=['Registry selections','Default registry selections','Show configuration','Validate','Save','Quit without saving']
        if kind=='backup':choices.insert(5,'Generate startup launcher');choices.insert(6,'Generate database access scripts')
        action=choice('ConfigBackup configuration editor',choices,input_fn)
        if action in (None,'Quit without saving'):return False
        try:
            monitor=config.setdefault('monitoring',{}) if kind=='backup' else config.get('monitoring',config)
            if action=='Registry selections':manage(config.setdefault('registry',[]),'Registry selections',new_registry,input_fn)
            elif action=='Generate startup launcher':launcher_wizard(path,input_fn)
            elif action=='Generate database access scripts':access_wizard(input_fn)
            elif action=='Default registry selections':config['include_defaults']=boolean('Include the built-in selected settings',config.get('include_defaults',True),input_fn)
            elif action=='Tasks':
                manage(config.setdefault('tasks',[]),'Backup tasks',lambda inp:template_tasks(choice('Task template',['Directory','SQL Server','PostgreSQL','System'],inp),inp),input_fn)
            elif action=='Monitoring sources':manage(monitor.setdefault('sources',[]),'Telemetry sources',new_source,input_fn)
            elif action=='Sources':manage(config.setdefault('sources',[]),'Schedule sources',lambda inp:new_source(inp,True),input_fn)
            elif action=='Alert rules':manage(monitor.setdefault('rules',[]),'Alert rules',new_rule,input_fn)
            elif action=='Notification channels':manage(monitor.setdefault('notifications',{}).setdefault('channels',[]),'Notification channels',new_channel,input_fn)
            elif action=='Global switches':
                monitor['enabled']=boolean('Enable monitoring',monitor.get('enabled',False),input_fn)
                monitor['default_rules']=boolean('Enable default alert rules',monitor.get('default_rules',True),input_fn)
                if kind=='backup':config.setdefault('deletion',{})['enabled']=boolean('Enable deletion detection',config.get('deletion',{}).get('enabled',True),input_fn)
                if kind=='backup':
                    git=config.setdefault('git',{})
                    git['enabled']=boolean('Enable Git storage',git.get('enabled',True),input_fn)
                    git['repository']=ask('Local Git repository path (blank if unused)',git.get('repository',''),input_fn)
                monitor['paths']=json_value('Local capacity paths',monitor.get('paths',[]),input_fn)
            elif action=='Report/analysis settings':
                group=choice('Settings',['reports','analysis'],input_fn)
                if group:config[group]=json_value(group,config.get(group,{}),input_fn)
            elif action=='Show configuration':print(yaml.safe_dump(redact_for_display(config),sort_keys=False))
            elif action=='Validate':validate(config,kind);print('Configuration is valid. No collectors or notifications were run.')
            elif action=='Save':
                validate(config,kind)
                keys=sorted(k for k in set(original)|set(config) if original.get(k)!=config.get(k))
                print('Changed top-level sections: '+(', '.join(keys) or 'none'))
                print('Saving reformats YAML. A byte-for-byte backup preserves the previous file and its comments.')
                if boolean('Save configuration',True,input_fn):
                    backup=save(config,path,kind);print('Saved '+str(path)+('; backup: '+str(backup) if backup else ''));return True
        except (ValueError,TypeError,KeyError,ConfigError) as exc:print('Please correct:',exc)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='configbackup.yaml')
    parser.add_argument('--kind',choices=['backup','monitor','schedule','registry'],default='backup')
    parser.add_argument('--check',action='store_true',help='Validate without opening the wizard')
    args=parser.parse_args(argv);path=Path(args.config)
    config=yaml.safe_load(path.read_text()) if path.exists() else {}
    config=config or {}
    if args.check:validate(config,args.kind);print('Configuration valid');return 0
    if args.kind=='backup' and not config:config={'backup':{'root':ask('Backup archive root')},'tasks':[]}
    try:wizard(config,path,args.kind)
    except (EOFError,KeyboardInterrupt):print('\nCancelled; configuration file unchanged.')
    return 0

if __name__=='__main__':raise SystemExit(main())
