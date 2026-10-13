"""Reviewed renames preserve state identity and update declared dependencies."""
import copy


def references(config, kind, name):
    if kind=='connection':return [t['name'] for t in config.get('tasks',[]) if t.get('connection')==name]
    result=[]
    for task in config.get('tasks',[]):
        deps=task.get('depends_on',[]);deps=[deps] if isinstance(deps,str) else deps
        if name in deps:result.append(task['name'])
    return result


def rename(config, kind, old, new):
    if not new or any(c in new for c in '/\\:') or new in ('.','..'):raise ValueError('Choose a nonempty name without path separators')
    draft=copy.deepcopy(config)
    if kind=='connection':
        values=draft.get('connections',{})
        if new in values or old not in values:raise ValueError('Missing old name or duplicate new name')
        values[new]=values.pop(old)
        for t in draft.get('tasks',[]):
            if t.get('connection')==old:t['connection']=new
    else:
        tasks=draft.get('tasks',[])
        if any(t['name']==new for t in tasks):raise ValueError('Duplicate task name')
        target=next((t for t in tasks if t['name']==old),None)
        if target is None:raise ValueError('Missing task')
        target.setdefault('state_id',old);target['name']=new
        for t in tasks:
            deps=t.get('depends_on',[])
            if isinstance(deps,str):
                if deps==old:t['depends_on']=new
            elif old in deps:t['depends_on']=[new if d==old else d for d in deps]
        for section in ((draft.get('git') or {}).get('sensitive_data') or {}).get('sections',[]):
            if section.get('task')==old:section['task']=new
    config.clear();config.update(draft)
