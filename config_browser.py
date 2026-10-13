"""Browse the loaded configuration and edit existing nested values safely."""
import re
from configbackup import redact_for_display


def summary(config):
    lines=['Loaded configuration']
    for key,value in config.items():
        if isinstance(value,(dict,list)):detail=f'{len(value)} '+('settings' if isinstance(value,dict) else 'items')
        else:detail='configured'
        lines.append(f'  {key}: {detail}')
    if not config:lines.append('  No sections yet')
    return '\n'.join(lines)


def browse(config,input_fn=input):
    from configure import choice,ask,boolean,json_value
    from config_schema import field_type
    def visit(container,path,trail=()):
        while True:
            keys=list(container) if isinstance(container,dict) else list(range(len(container)))
            safe=redact_for_display(config)
            for part in trail:
                if not isinstance(safe,(dict,list)):break
                safe=safe[part]
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
            selected=choice(path,labels+['Back'],input_fn)
            if selected in (None,'Back'):return
            key=keys[labels.index(selected)];value=container[key];name=str(key)
            if isinstance(value,(dict,list)):visit(value,path+'/'+name,trail+(key,));continue
            reference=name.endswith('_env') and isinstance(value,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',value)
            if safe[key]!=value and not reference:
                print('Sensitive value hidden. Use an environment/vault reference or edit this value securely outside the browser.');continue
            kind=field_type(name,value)
            if kind is bool:new=boolean(path+'/'+name,bool(value),input_fn)
            elif kind in (int,float):new=kind(ask(path+'/'+name,value,input_fn))
            elif value is None:new=json_value(path+'/'+name,None,input_fn)
            else:new=ask(path+'/'+name,value,input_fn)
            container[key]=new
            print('Updated '+path+'/'+name+' in memory. Use Save to write changes.')
    print(summary(config))
    visit(config,'Current configuration')
