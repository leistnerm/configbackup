"""Browse the loaded configuration and edit existing nested values safely."""
import re
import copy
from configbackup_lib.engine.configbackup import redact_for_display


def summary(config):
    lines=['Loaded configuration']
    for key,value in config.items():
        if isinstance(value,(dict,list)):detail=f'{len(value)} '+('settings' if isinstance(value,dict) else 'items')
        else:detail='configured'
        lines.append(f'  {key}: {detail}')
    if not config:lines.append('  No sections yet')
    return '\n'.join(lines)


def browse(config,input_fn=input,section=None,start=None,edit_start=False):
    from configbackup_lib.ui.configure import choice,ask,boolean,json_value
    from configbackup_lib.configuration.config_schema import field_type
    from configbackup_lib.ui.editor_forms import scalar, EditHistory
    history=EditHistory()
    auto_path=tuple(start) if start and edit_start else None
    class RestartBrowser(Exception):pass
    staged=config.get(section,{}) if section is not None else config
    def attach():
        if section is not None and section not in config and staged:config[section]=staged
    def visit(container,path,trail=()):
        nonlocal auto_path
        from configbackup_lib.ui.configure import boolean
        while True:
            keys=list(container) if isinstance(container,dict) else list(range(len(container)))
            safe=redact_for_display(config)
            for part in trail:
                if not isinstance(safe,(dict,list)):break
                safe=safe.get(part,{}) if isinstance(safe,dict) else safe[part]
            if not isinstance(safe,(dict,list)):
                print('Sensitive section hidden; edit securely outside the browser.');return
            labels=[]
            for key in keys:
                value=container[key];preview=safe[key]
                if isinstance(preview,(dict,list)):
                    identity=preview.get('name',preview.get('id',preview.get('host',''))) if isinstance(preview,dict) else ''
                    preview=f'{identity} ({len(value)} '+('settings)' if isinstance(value,dict) else 'items)')
                else:preview=str(preview)
                labels.append(f'{key}: {preview.strip()[:100]}')
            if auto_path and trail==auto_path[:-1] and auto_path[-1] in keys:
                selected=labels[keys.index(auto_path[-1])];auto_path=None
            else:selected=choice(path,labels+['Undo last edit','Add setting/item','Remove setting/item','Reset to inherited/default','Back'],input_fn)
            if selected in (None,'Back'):return
            if selected=='Undo last edit':
                if getattr(input_fn,'command_handler',None):input_fn.execute_command('Undo last action','menu');return
                if history.undo(config):raise RestartBrowser()
                print('Nothing to undo.');continue
            if selected=='Reset to inherited/default':
                selected_key=choice('Override to reset',labels,input_fn)
                if selected_key is None:continue
                key=keys[labels.index(selected_key)]
                if not isinstance(container,dict) or not trail or key in ('name','type','connection','depends_on','state_id'):
                    print('This structural setting cannot be reset here.');continue
                from configbackup_lib.ui.navigation import field_context
                print(field_context(config,trail,key,container[key]))
                if boolean('Remove this override and use inherited/default behavior?',False,input_fn):
                    before=copy.deepcopy(config);del container[key];history.record(before,config)
                continue
            if selected in ('Add setting/item','Remove setting/item'):
                from configbackup_lib.ui.configure import boolean
                if trail in (('tasks',),('connections',)):
                    print('Use Tasks or Shared connections to change named records safely.');continue
                before=copy.deepcopy(config)
                if selected=='Add setting/item':
                    new_key=ask('New setting name',input_fn=input_fn) if isinstance(container,dict) else len(container)
                    if isinstance(container,dict) and (not new_key or new_key in container):
                        print('Choose a new nonempty key.');continue
                    kind=choice('Value type',['Text','Boolean','Integer','Number','Mapping','List'],input_fn)
                    if kind is None:continue
                    initial={'Text':'','Boolean':False,'Integer':0,'Number':0.0,'Mapping':{},'List':[]}[kind]
                    value=initial if isinstance(initial,(dict,list)) else scalar(str(new_key),initial,input_fn,literal=True)
                    reference=str(new_key).endswith('_env') and isinstance(value,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',value)
                    if redact_for_display({new_key:value}).get(new_key)!=value and not reference:
                        print('Use an environment/vault reference; inline sensitive values are not accepted.');continue
                    if isinstance(container,dict):container[new_key]=value
                    else:container.append(value)
                else:
                    selected_key=choice('Setting/item to remove',labels,input_fn)
                    if selected_key is None:continue
                    removing=keys[labels.index(selected_key)]
                    if not trail or removing in ('name','connection','depends_on'):
                        print('Use the dedicated menu for structural settings.');continue
                    if boolean('Remove this setting? Its inherited/default behavior will apply',False,input_fn):
                        if isinstance(container,dict):del container[removing]
                        else:container.pop(removing)
                attach();history.record(before,config);continue
            key=keys[labels.index(selected)];value=container[key];name=str(key)
            literal=any(p in ('variables','environment','arguments') for p in trail)
            expected=type(value) if literal else field_type(name,value)
            if isinstance(value,(dict,list)) and isinstance(value,expected):visit(value,path+'/'+name,trail+(key,));continue
            reference=name.endswith('_env') and isinstance(value,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',value)
            if safe[key]!=value and not reference:
                print('Sensitive value hidden. Use an environment/vault reference or edit this value securely outside the browser.');continue
            from configbackup_lib.ui.navigation import field_context
            context=field_context(config,trail,name,value);print(context)
            if getattr(input_fn,'console_ui',False):input_fn.field_context=context
            before=copy.deepcopy(config)
            container[key]=scalar(name,value,input_fn,literal=any(p in ('variables','environment','arguments') for p in trail))
            if name=='name' and len(trail)==2 and trail[0]=='tasks' and container[key]!=value:
                from configbackup_lib.configuration.configuration_links import rename,references
                from configbackup_lib.ui.configure import boolean
                new=container[key];container[key]=value
                print('Dependent tasks: '+', '.join(references(config,'task',value)))
                if boolean('Update references and preserve task state identity?',False,input_fn):
                    rename(config,'task',value,new);history.record(before,config)
                raise RestartBrowser()
            attach();history.record(before,config)
            print('Updated '+path+'/'+name+' in memory. Use Save to write changes.')
    print(summary(config))
    while True:
        try:
            if start:
                trail=tuple(start);container=config
                for part in trail[:-1]:container=container[part]
                target=container[trail[-1]]
                expected=field_type(str(trail[-1]),target)
                if isinstance(target,(dict,list)) and (not edit_start or isinstance(target,expected)):
                    container=container[trail[-1]]
                else:trail=trail[:-1]
                visit(container,'Current configuration/'+ '/'.join(map(str,trail)),trail)
            elif section is None:visit(config,'Current configuration')
            else:
                staged=config.get(section,{})
                if not isinstance(staged,(dict,list)):raise ValueError(section+' must be a mapping or list')
                visit(staged,section,(section,))
            return
        except RestartBrowser:start=None;continue
