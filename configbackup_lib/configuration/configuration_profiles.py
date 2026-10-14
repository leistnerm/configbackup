"""Reusable managed collector settings; task fields explicitly override profiles."""
import copy
from configbackup_lib.configuration.config_schema import field_type

FIELDS={'collection','sections','required_sections','collection_interval_hours','max_scope_bytes'}
HELP={
    'enabled':'Retains this item in YAML while stopping its collection. Dependent archives preserve prior history.',
    'required':'A failed required item makes the overall run fail; healthy independent items still proceed.',
    'state_id':'Stable archive identity across display-name changes. Use the lifecycle tool to migrate existing history.',
    'collection_interval_hours':'Minimum collection cadence. Outstanding archive/Git work can force an earlier retry.',
    'max_scope_bytes':'Rejects an oversized certified scope before archival; previous scope bytes stay unchanged.',
    'sections':'Named scope patterns with enabled switches. Disabled scopes preserve prior archive/Git history.',
    'required_sections':'Patterns that must be available in the current collector evidence.',
    'profile':'Named collection profile. Task-specific fields override it; Effective preview shows the origin.',
    'source':'Live input path. Variables resolve at runtime; this is not the archive destination.',
    'destination':'Relative logical archive path. Changing an existing destination requires a reviewed state migration.',
    'storage':'filesystem keeps retained versions; git stores current tracked snapshots; both uses each backend.',
    'collection':'Collector-specific options. Schema/history/health costs and permissions differ by engine.',
    'password_env':'Environment variable NAME, never the credential value. Test under the scheduled account.',
    'host':'Database endpoint or telemetry host identity. Correlation requires consistent host names.',
    'port':'Database TCP port. Named-instance discovery and an explicit fixed port are distinct connection choices.'}


def help_text(field):return HELP.get(field,'Value type: '+field_type(field).__name__+'. Review the selected collector documentation for scope and permissions.')


def validate(config):
    profiles=config.get('collection_profiles',{})
    if not isinstance(profiles,dict):raise ValueError('collection_profiles must be a mapping')
    for name,profile in profiles.items():
        if not isinstance(name,str) or not name.strip() or not isinstance(profile,dict) or set(profile)-FIELDS-{'engine'}:raise ValueError('Invalid collection profile')
        if profile.get('engine') not in ('sqlserver','postgresql'):raise ValueError('Collection profile requires a supported engine')
        if 'collection' in profile and not isinstance(profile['collection'],dict):raise ValueError('Profile collection must be a mapping')
        if 'required_sections' in profile and (not isinstance(profile['required_sections'],list) or any(not isinstance(v,str) for v in profile['required_sections'])):raise ValueError('Profile required_sections must contain strings')
        for key in ('collection_interval_hours','max_scope_bytes'):
            if key in profile:
                import math
                value=profile[key]
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or key=='max_scope_bytes' and not isinstance(value,int):raise ValueError('Invalid profile '+key)
    return config


def resolve(task,config):
    name=task.get('profile')
    if not name:return copy.deepcopy(task)
    profiles=config.get('collection_profiles',{})
    if not isinstance(profiles,dict) or name not in profiles:raise ValueError('Unknown collection profile: '+str(name))
    profile=profiles[name]
    if not isinstance(profile,dict) or set(profile)-FIELDS-{'engine'}:raise ValueError('Invalid collection profile fields')
    connection=config.get('connections',{}).get(task.get('connection'),{})
    if profile.get('engine') not in ('sqlserver','postgresql') or profile['engine']!=connection.get('engine'):raise ValueError('Collection profile engine does not match task connection')
    from configbackup_lib.engine.configbackup import deep_merge
    return deep_merge({k:v for k,v in profile.items() if k!='engine'},task)


def origins(task,runtime,config):
    profile=config.get('collection_profiles',{}).get(task.get('profile'),{})
    def walk(value,prefix=''):
        for key,item in value.items():
            path=prefix+'.'+str(key) if prefix else str(key)
            if isinstance(item,dict):yield from walk(item,path)
            else:yield path
    def exists(value,path):
        for part in path.split('.'):
            if not isinstance(value,dict) or part not in value:return False
            value=value[part]
        return True
    return {key:'task' if exists(task,key) else 'profile:'+task['profile'] if exists(profile,key) else 'defaults' if exists(config.get('defaults',{}),key) else 'runtime/default' for key in walk(runtime) if not key.startswith('_')}


def wizard(config,input_fn=input):
    from configbackup_lib.ui.configure import choice,ask
    while True:
        action=choice('Saved collection profiles',['Save from a managed task','Assign profile','Remove profile assignment','Edit profile','Clone profile','Delete profile','Preview affected tasks','Clear task overrides','Back'],input_fn)
        if action in (None,'Back'):return
        if action in ('Edit profile','Clone profile','Delete profile','Preview affected tasks'):
            manage(config,action,input_fn);continue
        tasks=[t for t in config.get('tasks',[]) if t.get('connection')]
        if not tasks:print('Add a managed database task first.');continue
        name=choice('Task',[t['name'] for t in tasks],input_fn)
        if name is None:continue
        task=next(t for t in tasks if t['name']==name)
        if action=='Clear task overrides':
            from configbackup_lib.ui.configure import boolean
            fields=[key for key in sorted(FIELDS) if key in task]
            print('Task overrides: '+', '.join(fields))
            key=choice('Override to clear',fields,input_fn)
            if key and boolean('Use the profile/default value instead',False,input_fn):task.pop(key)
            continue
        if action=='Remove profile assignment':task.pop('profile',None);continue
        profiles=config.setdefault('collection_profiles',{})
        if action=='Save from a managed task':
            name=ask('New profile name',input_fn=input_fn)
            if not name or name in profiles:raise ValueError('Choose an unused nonempty profile name')
            profiles[name]={'engine':config['connections'][task['connection']]['engine'],**{k:copy.deepcopy(v) for k,v in task.items() if k in FIELDS}}
        else:
            name=choice('Profile',list(profiles),input_fn)
            if name:
                resolve({**task,'profile':name},config);task['profile']=name
                print('Assigned. Existing task fields override the profile; inspect Effective preview before saving.')


def affected(config,name):
    assigned={t['name'] for t in config.get('tasks',[]) if t.get('profile')==name}
    result=[]
    for task in config.get('tasks',[]):
        deps=task.get('depends_on',[]);deps=[deps] if isinstance(deps,str) else deps
        if task['name'] in assigned or assigned.intersection(deps):
            result.append({'task':task['name'],'role':'profile consumer' if task['name'] in assigned else 'dependent archive',
                           'overrides':sorted(FIELDS.intersection(task))})
    return result


def manage(config,action,input_fn=input):
    import json
    import yaml
    from configbackup_lib.ui.configure import choice,ask,boolean
    profiles=config.setdefault('collection_profiles',{})
    name=choice('Profile',sorted(profiles),input_fn)
    if not name:return
    impact=affected(config,name);print(json.dumps(impact,indent=2))
    if action=='Preview affected tasks':return
    candidate=copy.deepcopy(config)
    if action=='Delete profile':
        if impact:print('Remove assignments before deleting this profile.');return
        if boolean('Delete unused profile '+name,False,input_fn):del profiles[name]
        return
    if action=='Clone profile':
        new=ask('New profile name',input_fn=input_fn)
        if not new or new in profiles:raise ValueError('Choose an unused nonempty profile name')
        profiles[new]=copy.deepcopy(profiles[name]);return
    key=choice('Profile field',sorted(FIELDS),input_fn)
    if not key:return
    print(help_text(key));print('Current: '+json.dumps(profiles[name].get(key)))
    from configbackup_lib.ui.editor_forms import validated
    def parse_value(text):
        try:return yaml.safe_load(text)
        except yaml.YAMLError:raise ValueError() from None
    parsed=validated('Replacement YAML value (null removes this field)','',input_fn,parse_value,error='Invalid YAML; correct this field or cancel.')
    if parsed is None:candidate['collection_profiles'][name].pop(key,None)
    else:candidate['collection_profiles'][name][key]=parsed
    validate(candidate)
    if boolean('Apply this profile change to the listed tasks',False,input_fn):profiles[name]=candidate['collection_profiles'][name]
