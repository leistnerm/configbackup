"""Comment-aware configuration persistence, safe group duplication and previews."""
from __future__ import annotations
import copy
import difflib
import hashlib
import io
import json
from pathlib import Path
import yaml


def plain(value):
    if isinstance(value,dict):return {k:plain(v) for k,v in value.items()}
    if isinstance(value,list):return [plain(v) for v in value]
    return value


def render(config, original=''):
    from ruamel.yaml import YAML
    parser=YAML(typ='rt');parser.preserve_quotes=True;parser.width=120
    document=parser.load(original) if original.strip() else {}
    def merge(old,new):
        if isinstance(old,dict) and isinstance(new,dict):
            for key in list(old):
                if key not in new:del old[key]
            for key,value in new.items():old[key]=merge(old.get(key),value)
            return old
        if isinstance(old,list) and isinstance(new,list):
            # Match named records to keep their attached field comments on reorder.
            def identity(item):
                if not isinstance(item,dict):return None
                for key in ('name','id','host','path'):
                    if key in item:return (key,str(item[key]))
                return None
            indexed={identity(item):item for item in old if identity(item) is not None}
            values=[merge(indexed.get(identity(item),old[n] if n<len(old) and identity(item) is None else None),item) for n,item in enumerate(new)]
            old[:]=values;return old
        if type(old)==type(new) and old==new:return old
        return copy.deepcopy(new)
    stream=io.StringIO();parser.dump(merge(document,config),stream)
    text=stream.getvalue()
    # The runtime uses YAML 1.1 (PyYAML). Reject any round-trip semantic divergence.
    if yaml.safe_load(text)!=plain(config):raise ValueError('YAML editor could not preserve configuration semantics')
    return text


def semantic_diff(before,after):
    from configbackup import redact_for_display
    def dump(value):return yaml.safe_dump(redact_for_display(plain(value)),sort_keys=True).splitlines(True)
    return ''.join(difflib.unified_diff(dump(before),dump(after),fromfile='saved',tofile='edited')) or 'No semantic changes.\n'


def fingerprint(path):
    path=Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def duplicate_group(items,index,name):
    """Clone a collector and its direct archive consumers with independent paths."""
    if not name or any(c in name for c in '/\\:') or name in ('.','..'):
        raise ValueError('New name must be nonempty and contain no path separators')
    original=items[index];old=original.get('name')
    if not old:raise ValueError('Task group duplication requires a named task')
    existing={item.get('name') for item in items}
    if name in existing:raise ValueError('Task name already exists: '+name)
    clone=copy.deepcopy(original);clone['name']=name
    if original.get('type')!='execute':
        if original.get('type')=='command':clone['output']=name+Path(str(original.get('output',''))).suffix
        else:clone['destination']=name
        return [clone]
    old_output=original.get('output_directory','${CONFIGBACKUP_STAGING}/'+old)
    new_output='${CONFIGBACKUP_STAGING}/'+name
    def rewrite(value):
        if isinstance(value,str):return new_output+value[len(old_output):] if value==old_output or value.startswith(old_output.rstrip('/')+'/') else value
        if isinstance(value,list):return [rewrite(v) for v in value]
        if isinstance(value,dict):return {k:rewrite(v) for k,v in value.items()}
        return value
    clone=rewrite(clone)
    clone['output_directory']=new_output
    consumers=[item for item in items if old in ([item.get('depends_on')] if isinstance(item.get('depends_on'),str) else item.get('depends_on',[])) and item.get('type')=='directory' and item.get('source')==old_output]
    group=[clone]
    for n,item in enumerate(consumers):
        child=rewrite(copy.deepcopy(item));child['name']=name+'-archive'+(str(n+1) if n else '')
        child['depends_on']=[name if d==old else d for d in ([item['depends_on']] if isinstance(item['depends_on'],str) else item['depends_on'])]
        child['destination']=name if len(consumers)==1 else name+'/'+str(n+1)
        if child['name'] in existing:raise ValueError('Task name already exists: '+child['name'])
        group.append(child)
    return group


def effective_preview(config,path):
    from configbackup import ConfigLoader,BackupEngine,redact_for_display
    resolved=ConfigLoader(Path(path))._resolve(plain(config));engine=BackupEngine(resolved,dry_run=True)
    try:
        rows=[]
        for task in resolved['tasks']:
            runtime=engine.resolve_task_runtime(task)
            rows.append({'name':runtime['name'],'enabled':runtime.get('enabled',True),'type':runtime['type'],
                         'depends_on':runtime.get('depends_on',[]),'connection':runtime.get('connection'),
                         'source':runtime.get('source'),'output_directory':engine.variables_for_task(task)['CONFIGBACKUP_OUTPUT'] if task['type']=='execute' else None,
                         'destination':runtime.get('destination',runtime.get('output')),'storage':runtime.get('storage'),
                         'sections':runtime.get('sections',{}),'collection':runtime.get('_database_profile',runtime.get('collection',{}))})
            from configuration_profiles import origins
            declared=next((t for t in config.get('tasks',[]) if t['name']==runtime['name']),{})
            rows[-1]['field_origins']=origins(declared,runtime,config)
            rows[-1]['resolved_settings']=runtime
        return redact_for_display({'archive':str(engine.archive_root),'staging':str(engine.staging_root),'tasks':rows,
                                   'monitoring':resolved.get('monitoring',{}),'reporting':resolved.get('reporting',{})})
    finally:engine._close_logging()
