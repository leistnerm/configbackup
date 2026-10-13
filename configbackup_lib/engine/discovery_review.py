"""Persistent discovery choices, bounded include discovery and coverage evidence."""
from __future__ import annotations
import argparse
import fnmatch
import html
import json
from pathlib import Path
import re
import shlex
import yaml
from configbackup_lib.reporting.reporting import atomic_json


def asset_path(item):
    return item if isinstance(item,str) else item['path']


def decisions(config):
    result=config.get('discovery',{}).get('decisions',{})
    if not isinstance(result,dict):raise ValueError('discovery.decisions must be a mapping')
    for path,value in result.items():
        if not isinstance(path,str) or value not in ('accepted','ignored','disabled'):
            raise ValueError('Discovery decisions require paths and accepted/ignored/disabled values')
    return result


def decide(config, report, path, decision):
    if decision not in ('accepted','ignored','disabled'):raise ValueError('Invalid decision')
    row=next((r for r in report['candidates'] if r['path']==path),None)
    if row is None or decision=='accepted' and row['status']!='available':raise ValueError('Choose an available local candidate to accept')
    files=config.setdefault('backup',{}).setdefault('configuration_files',[])
    index=next((i for i,item in enumerate(files) if asset_path(item)==path),None)
    if decision in ('accepted','disabled'):
        item={'path':path,'enabled':decision=='accepted','discovered_from':sorted(row.get('jobs',[]))}
        if index is None:files.append(item)
        else:files[index]=item
    elif index is not None:
        # Ignoring a suggestion must not silently stop an existing backup.
        raise ValueError('Existing selection must be disabled explicitly before ignoring; remove it separately')
    config.setdefault('discovery',{}).setdefault('decisions',{})[path]=decision


def review(config,report,new_only=False):
    saved=decisions(config);selected={asset_path(p): p for p in config.get('backup',{}).get('configuration_files',[])}
    result=[]
    for row in report['candidates']:
        item=selected.get(row['path']);decision=saved.get(row['path'],'new')
        if item is not None:decision='disabled' if isinstance(item,dict) and not item.get('enabled',True) else 'accepted'
        if new_only and decision!='new':continue
        reasons=row.get('reasons',[])
        result.append({**row,'decision':decision,'evidence':'direct reference' if any('argument' in x or 'script' in x for x in reasons) else 'nearby file; usage unproven','application':str(Path(row['path']).parent)})
    return sorted(result,key=lambda r:(r['application'],r['path']))


def includes(paths,allowed_roots,max_depth=3,max_files=200):
    """Follow explicit include/include_file/!include directives only, never commands."""
    if not 0<=max_depth<=10 or not 1<=max_files<=1000:raise ValueError('Invalid include limits')
    roots=[Path(p).resolve() for p in allowed_roots];queue=[(Path(p).absolute(),0) for p in sorted(paths)];seen=set();rows=[];errors=[]
    while queue:
        path,depth=queue.pop(0)
        if str(path) in seen:continue
        seen.add(str(path))
        if len(seen)>max_files:errors.append({'path':str(path),'reason':'file limit reached'});break
        if any(p.is_symlink() for p in [path,*path.parents]) or not any(path.resolve().is_relative_to(root) for root in roots):
            errors.append({'path':str(path),'reason':'outside allowed roots or symlink'});continue
        try:
            if path.stat().st_size>1024*1024:raise ValueError('include source too large')
            text=path.read_text(encoding='utf-8-sig')
            for line in text.splitlines():
                match=re.match(r'^\s*(?:!include|include|include_file)\s*(?:=\s*|\s+)(.+?)\s*;?\s*$',line,re.I)
                if not match:continue
                values=shlex.split(match[1].rstrip(';'),comments=True)
                if len(values)!=1 or any(c in values[0] for c in '$%*?`\x00'):
                    errors.append({'path':str(path),'reason':'dynamic or wildcard include requires review'});continue
                child=Path(values[0]);child=child if child.is_absolute() else path.parent/child
                child=Path(__import__('os').path.abspath(child))
                if depth>=max_depth:errors.append({'path':str(child),'reason':'depth limit reached'});continue
                if any(p.is_symlink() for p in [child,*child.parents]) or not any(child.resolve().is_relative_to(root) for root in roots):
                    errors.append({'path':str(child),'reason':'outside allowed roots or symlink'});continue
                rows.append({'path':str(child),'status':'available' if child.is_file() else 'missing or not a regular file','jobs':[str(path)],'reasons':['recognized include argument'],'selected':False,'storage':'filesystem only'})
                if child.is_file():queue.append((child,depth+1))
        except (OSError,ValueError,UnicodeError) as exc:errors.append({'path':str(path),'reason':type(exc).__name__})
    dedup={r['path']:r for r in rows}
    return {'candidates':sorted(dedup.values(),key=lambda r:r['path']),'probe_errors':errors,'limits':'Only explicit recognized include directives. No glob expansion, arbitrary YAML tags, scripts or environment evaluation.'}


def coverage(config,config_path,discovered=None,state=None):
    from configbackup_lib.recovery.recovery_assets import inventory
    inv=inventory(config,config_path);rows=[];state=state or {};retained={}
    for task in state.get('tasks',{}).values():
        for entry in task.get('files',{}).values():
            source=(entry.get('versions') or [{}])[-1].get('source')
            if source and entry.get('active',True) and entry.get('versions'):retained[str(Path(source).resolve())]=entry
    for row in inv['assets']:
        if not config.get('backup',{}).get('include_configuration',True):
            rows.append({'kind':'file','item':row['source'],'status':'disabled','evidence':'automatic setup backups disabled'});continue
        entry=retained.get(row['source']);status='inaccessible' if row['status']!='available' else 'selected; no archive evidence'
        if entry:
            latest=(entry.get('versions') or [])[-1]
            status='backed up (recorded hash matches)' if latest.get('hash')==row.get('sha256') else 'retained; current bytes not verified in archive'
        rows.append({'kind':'file','item':row['source'],'status':status,'evidence':row['kind']})
    for item in config.get('backup',{}).get('configuration_files',[]):
        if isinstance(item,dict) and not item.get('enabled',True):rows.append({'kind':'file','item':item['path'],'status':'disabled','evidence':'explicit file switch'})
    known={r['item'] for r in rows}
    for row in review(config,discovered or {'candidates':[]}):
        if row['path'] not in known:rows.append({'kind':'file','item':row['path'],'status':'excluded' if row['decision']=='ignored' else 'discovered but unselected','evidence':row['evidence'],'jobs':row['jobs']})
    for task in config.get('tasks',[]):
        rows.append({'kind':'task','item':task['name'],'status':'configured' if task.get('enabled',True) else 'disabled','dependencies':task.get('depends_on',[]),'last_success':state.get('tasks',{}).get(task.get('state_id',task['name']),{}).get('last_success','unknown'),'last_status':state.get('tasks',{}).get(task.get('state_id',task['name']),{}).get('last_status','unknown'),'connection':task.get('connection',''),'evidence':'configuration only; live permissions and scheduler identity unknown'})
    for name in inv['environment_references']:rows.append({'kind':'credential reference','item':name,'status':'unknown','evidence':'value not read; scheduled-account access must be tested'})
    for name in ('scheduler registration and account','filesystem permissions and database grants','external client tools and service data'):
        rows.append({'kind':'recovery dependency','item':name,'status':'unknown','evidence':'setup export does not reconstruct this dependency'})
    return {'schema':1,'status':inv['status'],'items':sorted([{**r,'application':str(Path(r['item']).parent) if r['kind']=='file' else r.get('connection') or r['kind']} for r in rows],key=lambda r:(r['kind'],r['item'])),'discovery_errors':inv['discovery_errors']+(discovered or {}).get('probe_errors',[]),'limits':'Archive state is evidence, not a fresh integrity check. Unknown/unregistered dependencies may be missed; no credential values are read.'}


def write_report(value,directory,title='Recovery coverage'):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    atomic_json(directory/'report.json',value)
    from configbackup_lib.recovery.transaction_journal import atomic
    from configbackup_lib.reporting.review_html import render
    body=render(value,title)
    atomic(directory/'report.html',body.encode('utf-8'))
    return value


def wizard(config,input_fn=input):
    from configbackup_lib.ui.configure import choice
    from configbackup_lib.engine.schedule_discovery import discover
    report=discover(include_services=True,user_timers=True)
    while True:
        action=choice('Discovery review',['New suggestions','All suggestions','Back'],input_fn)
        if action in (None,'Back'):return
        rows=review(config,report,action=='New suggestions')
        if not rows:print('No matching suggestions.');continue
        labels=[r['decision']+' | '+r['path']+' | '+r['evidence'] for r in rows]
        picked=choice('Select file',labels,input_fn)
        if picked is None:continue
        row=rows[labels.index(picked)]
        decision=choice('Decision',['accepted','disabled','ignored'],input_fn)
        if decision:
            try:decide(config,report,row['path'],decision)
            except ValueError as exc:print(exc)
        print('Changes are unsaved; use Save in the main menu.')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',required=True);parser.add_argument('--output',required=True);parser.add_argument('--discovery');parser.add_argument('--state');parser.add_argument('--managed');parser.add_argument('--drill-history')
    args=parser.parse_args(argv);config=yaml.safe_load(Path(args.config).read_text());load=lambda p:json.loads(Path(p).read_text()) if p else None
    value=coverage(config,args.config,load(args.discovery),load(args.state))
    if args.managed:
        from configbackup_lib.recovery.recovery_maintenance import status,freshness
        try:
            value['recovery']=status(args.managed)
            value['freshness']=freshness(config,args.config,value['recovery']['bundle'])
        except (OSError,ValueError,KeyError):value['recovery']={'status':'unavailable'}
    if args.drill_history:value['drill_history']=load(args.drill_history)
    write_report(value,args.output)
    return 0 if value['status']=='complete' else 6

if __name__=='__main__':raise SystemExit(main())
