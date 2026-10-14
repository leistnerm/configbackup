"""Stable configuration areas and session-wide commands."""
from contextlib import contextmanager

GROUPS = {
    'Overview': ['Configuration summary', 'Effective preview', 'Check setup'],
    'Backup & collection': ['Tasks', 'Shared connections', 'Optional collectors and discovery', 'Collection profiles', 'Saved collection profiles', 'Discover files from scheduled jobs', 'Registry selections', 'Default registry selections'],
    'Storage & security': ['Backup settings', 'Task defaults', 'Retention settings', 'Git settings', 'Sensitive collection', 'Deletion settings'],
    'Monitoring & reports': ['Monitoring sources', 'Alert rules', 'Notification channels', 'Monitoring settings', 'HTML review reports', 'Performance recording and reports', 'Sources', 'Report/analysis settings'],
    'Setup & launchers': ['Autodetect and guided setup', 'Python dependencies', 'Generate startup launcher', 'Launcher manager', 'PowerShell execution policy', 'Windows readiness', 'Database connection tests', 'Generate database access scripts'],
    'Recovery & maintenance': ['Recovery status and reset', 'Recovery files and export', 'Verify and rehearse setup recovery', 'Recovery readiness targets', 'Recovery and review workflows'],
    'Advanced configuration': ['Current configuration', 'Show configuration'],
}
LABELS = {'Quit without saving':'Exit','Backup settings':'Archive location & setup backup', 'Retention settings':'Named retention policies','Collection profiles':'Collector section presets', 'Saved collection profiles':'Named reusable profiles', 'Database connection tests':'Standalone diagnostic profiles'}
COMMANDS = ['Save', 'Validate', 'Preview changes', 'Undo last action', 'Redo last action', 'Help', 'Command palette', 'Quit without saving']

class Navigate(BaseException):
    def __init__(self, destination, payload=None):self.destination=destination;self.payload=payload

@contextmanager
def draft(input_fn):
    """An object being constructed is not yet part of the saved configuration."""
    previous=getattr(input_fn,'draft_active',False)
    if getattr(input_fn,'console_ui',False):input_fn.draft_active=True
    try:yield
    finally:
        if getattr(input_fn,'console_ui',False):input_fn.draft_active=previous


def grouped_choice(available, state, input_fn):
    from configbackup_lib.ui.configure import choice
    groups={name:[item for item in items if item in available] for name,items in GROUPS.items()}
    groups={name:items for name,items in groups.items() if items}
    while True:
        if not state[0]:
            selected=choice('ConfigBackup configuration editor',list(groups)+['Commands'],input_fn)
            if selected is None:return 'Quit without saving'
            state[0]=selected
        options=COMMANDS if state[0]=='Commands' else groups[state[0]]
        labels=[LABELS.get(item,item) for item in options]
        selected=choice(state[0],labels+['Back'],input_fn)
        if selected in (None,'Back'):state[0]=None;continue
        return options[labels.index(selected)]


def field_editor(function):
    """Mark value selection/input, excluding operational confirmations."""
    import functools, inspect
    signature=inspect.signature(function)
    @functools.wraps(function)
    def wrapped(*args,**kwargs):
        arguments=signature.bind(*args,**kwargs);arguments.apply_defaults()
        ui=arguments.arguments.get('input_fn')
        previous=getattr(ui,'field_active',False)
        if getattr(ui,'console_ui',False):ui.field_active=True
        try:return function(*args,**kwargs)
        finally:
            if getattr(ui,'console_ui',False):ui.field_active=previous
    return wrapped


ALIASES={'HTML review reports':'timezone html review', 'Sensitive collection':'ssis passwords secrets', 'Recovery status and reset':'journal reset interrupted', 'Shared connections':'sql postgres database authentication', 'Performance recording and reports':'perfmon sysstat telemetry', 'Python dependencies':'install packages tzdata', 'PowerShell execution policy':'scripts blocked powershell'}

def palette_entries(config,available):
    from configbackup_lib.engine.configbackup import redact_for_display
    entries={}
    for group,actions in GROUPS.items():
        for action in actions:
            if action in available:entries[group+' / '+LABELS.get(action,action)+' '+ALIASES.get(action,'')]=('action',action)
    for action in COMMANDS:
        if action not in ('Command palette','Help'):entries['Command / '+LABELS.get(action,action)]=('action',action)
    def visit(value,trail=()):
        if len(trail)>12 or len(entries)>1500:return
        if isinstance(value,dict):
            for key,child in value.items():
                path=trail+(key,)
                entries['Setting / '+' / '.join(map(str,path))]=('setting',path)
                visit(child,path)
        elif isinstance(value,list):
            for index,child in enumerate(value):visit(child,trail+(index,))
    visit(redact_for_display(config))
    return entries


def changed_count(before,after):
    if before==after:return 0
    if isinstance(before,dict) and isinstance(after,dict):
        return sum(changed_count(before[k],after[k]) if k in before and k in after else 1 for k in before.keys()|after.keys())
    if isinstance(before,list) and isinstance(after,list):
        return sum(changed_count(a,b) for a,b in zip(before,after))+abs(len(before)-len(after))
    return 1


def field_context(config,trail,name,current):
    """An unresolved preview must not prevent repairing a configuration field."""
    try:return _field_context(config,trail,name,current)
    except (ValueError,KeyError,TypeError,AttributeError,IndexError):
        from configbackup_lib.engine.configbackup import redact_for_display
        import json
        value=json.dumps(redact_for_display({name:current})[name],ensure_ascii=True)
        return ('Path: '+'.'.join(map(str,(*trail,name)))+'\nCurrent: '+value+
                '\nOrigin: Unresolved: fix the task/profile/connection configuration first.\n'
                'Inheritance could not be resolved; this field can still be edited.')


def _field_context(config,trail,name,current):
    from configbackup_lib.engine.configbackup import redact_for_display, deep_merge
    from configbackup_lib.configuration.configuration_profiles import help_text, resolve
    import copy, json
    missing=object()
    def get(value,path):
        for part in path:
            if isinstance(value,list):
                try:value=value[int(part)]
                except (ValueError,TypeError,IndexError):return missing
            elif isinstance(value,dict) and part in value:value=value[part]
            else:return missing
        return value
    def display(value):
        return 'Application default or required setting (not resolved)' if value is missing else json.dumps(redact_for_display({name:value})[name],ensure_ascii=True)
    configured=current
    inherited=missing; origin='Explicit setting'; next_origin='Application default'
    if len(trail)>=2 and trail[0]=='tasks':
        task=config['tasks'][trail[1]];relative=(*trail[2:],name)
        profile=config.get('collection_profiles',{}).get(task.get('profile'),{})
        connection=config.get('connections',{}).get(task.get('connection'),{})
        def effective(candidate):
            merged=deep_merge(config.get('defaults') or {},resolve(candidate,config))
            if relative[0]=='collection':
                settings={**connection,**merged.get('collection',{})}
                settings['sections']={**connection.get('sections',{}),**merged.get('collection',{}).get('sections',{}),**merged.get('sections',{})}
                return get(settings,relative[1:])
            if relative[0]=='sections' and connection:
                sections={**connection.get('sections',{}),**merged.get('collection',{}).get('sections',{}),**merged.get('sections',{})}
                return get(sections,relative[1:])
            if relative==('enabled',) and connection:
                return bool(merged.get('enabled',True) and connection.get('enabled',True))
            return get(merged,relative)
        def source(candidate):
            if relative==('enabled',) and connection.get('enabled') is False:return 'Shared connection: '+str(task.get('connection'))+' (disabled)'
            layers=[('Task override',candidate),('Profile: '+str(task.get('profile')),profile),('Task defaults',config.get('defaults',{}))]
            if relative[0]=='sections' or relative[:2]==('collection','sections'):
                suffix=relative[1:] if relative[0]=='sections' else relative[2:]
                for prefix in (('sections',),('collection','sections')):
                    for label,layer in layers:
                        if get(layer,(*prefix,*suffix)) is not missing:return label
            else:
                for label,layer in layers:
                    if get(layer,relative) is not missing:return label
            if relative[0]=='collection' and get(connection,relative[1:]) is not missing:return 'Shared connection: '+str(task.get('connection'))
            if relative[0]=='sections' and get(connection,relative) is not missing:return 'Shared connection: '+str(task.get('connection'))
            return 'Application default'
        candidate=copy.deepcopy(task)
        parent=candidate
        for part in relative[:-1]:parent=parent[part]
        if not isinstance(parent,dict):
            return ('Path: '+'.'.join(map(str,(*trail,name)))+'\nCurrent: '+display(current)+
                    '\nOrigin: Explicit list item; no independent inherited override.\n'+help_text(str(name)))
        parent.pop(name,None)
        try:
            current=effective(task);inherited=effective(candidate);origin=source(task);next_origin=source(candidate)
        except (ValueError,KeyError,TypeError):
            origin='Unresolved: fix the task/profile configuration first'
    return ('Path: '+'.'.join(map(str,(*trail,name)))+'\nCurrent: '+display(configured)+'\nEffective: '+display(current)+
            '\nOrigin: '+origin+'\nInherited/default: '+display(inherited)+'\nAfter reset origin: '+next_origin+'\n'+help_text(str(name)))


def validation_targets(config,error):
    """Map a validation message to existing paths; never expose configuration values."""
    from zoneinfo import ZoneInfo
    entries={};message=str(error)
    def visit(value,trail=(),label=''):
        if isinstance(value,dict):
            for key,child in value.items():
                path=(*trail,key);qualified=(label+'.' if label else '')+str(key)
                match=message.startswith(qualified+' ') or message.startswith(qualified+':')
                if key=='timezone' and ('timezone' in message.lower() or 'time zone' in message.lower()):
                    try:ZoneInfo(child)
                    except (ValueError,TypeError,KeyError):match=True
                if match:entries['Open '+qualified]=('validation',path)
                if key not in ('environment','variables','arguments'):visit(child,path,qualified)
        elif isinstance(value,list):
            for i,child in enumerate(value):visit(child,(*trail,i),label+'['+str(i)+']')
    visit(config)
    if not entries:entries['Open configuration to correct this error']=('action','Current configuration')
    return entries


def operation_boundary(action):
    if action in ('Recovery status and reset','Recovery and review workflows','Verify and rehearse setup recovery'):
        return 'Uses saved configuration or the selected saved recovery bundle. Save edits first. Filesystem actions have their own confirmations and cannot be undone by configuration undo.'
    if action in ('Generate startup launcher','Launcher manager','Recovery files and export'):
        return 'Reads configuration/assets from disk; generation policy may use current edits. Save before generating or regenerating. Generated files are external actions, not configuration undo history.'
    if action in ('Check setup','Database connection tests','Shared connections','Tasks'):
        return 'Tests use current accepted edits. Choose Save and test to validate/write first; a failed save blocks the test.'
    if action=='Performance recording and reports':
        return 'Uses current accepted settings. Enabling/disabling a recorder requires a separate reviewed-plan confirmation; configuration Save alone does not activate it.'
    return ''
