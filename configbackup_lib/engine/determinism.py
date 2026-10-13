"""Compare certified captures, or repeat a managed database collector in scratch."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import yaml
from configbackup_lib.engine.completeness import Coverage
from configbackup_lib.engine.discovery_review import write_report


def capture(path):
    c=Coverage(Path(path));rows={}
    with tempfile.TemporaryDirectory(prefix='configbackup-determinism-') as scratch:
        sealed=c.seal(scratch)
        from collectors.common.canonicalize import canonical_pg
        for row in c.sections:
            files=dict(sorted(row.get('files',{}).items())) if row['status']=='complete' else {}
            comparison=dict(files)
            for name in files:
                if name.endswith('.sql'):
                    try:comparison[name]=hashlib.sha256(canonical_pg((Path(scratch)/name).read_bytes().decode('utf-8')).encode()).hexdigest()
                    except UnicodeError:pass
            rows[row['path']]={'status':row['status'],'files':files,'comparison':comparison}
    return rows


def compare(paths):
    captures=[];errors=[]
    for index,path in enumerate(paths):
        try:captures.append(capture(path))
        except (ValueError,OSError,RuntimeError,KeyError,TypeError) as exc:captures.append({});errors.append({'capture':index+1,'error_type':type(exc).__name__})
    if len(captures)<2:raise ValueError('At least two captures are required')
    results=[]
    for scope in sorted(set().union(*(set(c) for c in captures))):
        rows=[c.get(scope,{'status':'unreported','files':{}}) for c in captures]
        complete=all(r['status']=='complete' for r in rows)
        hashes=[r['files'] for r in rows]
        changed=sorted(k for k in set().union(*(set(h) for h in hashes)) if any(h.get(k)!=hashes[0].get(k) for h in hashes[1:]))
        comparison=[r.get('comparison',r['files']) for r in rows]
        comparison_equal=all(c==comparison[0] for c in comparison[1:])
        results.append({'scope':scope,'comparison_only_paths':changed if complete and comparison_equal else [],'status':'identical' if complete and not changed else 'representation_only' if complete and comparison_equal else 'changed' if complete else 'not_collected' if len(set(r['status'] for r in rows))==1 and rows[0]['status'] in ('disabled','not_applicable') else 'incomplete','observations':[r['status'] for r in rows],'changed_paths':changed,'files':len(hashes[0])})
    status='incomplete' if errors or any(r['status']=='incomplete' for r in results) or not results or not any(r['status'] in ('identical','representation_only','changed') for r in results) else 'changed' if any(r['status']=='changed' for r in results) else 'representation_only' if any(r['status']=='representation_only' for r in results) else 'identical'
    return {'schema':1,'status':status,'captures':len(captures),'scopes':results,'errors':errors,'limits':'Certified configuration only; telemetry excluded by manifests. representation_only means native SQL bytes changed but the existing SQL-aware comparison differs only in top-level paired PostgreSQL restriction keys. Native dumps are never rewritten; comparison copies are not restore scripts. Other changes may be real drift or churn; no SQL semantic equivalence is inferred.'}


def repeat(config,config_path,task_name,count=3):
    if not 2<=count<=10:raise ValueError('Repeat count must be 2..10')
    from configbackup_lib.configuration.database_connections import task_context,collect
    task,_=task_context(config,config_path,task_name)
    profile=task['_database_profile'];outcomes=[]
    with tempfile.TemporaryDirectory(prefix='configbackup-repeat-') as td:
        paths=[]
        for i in range(count):
            path=Path(td)/str(i);paths.append(path)
            env={**os.environ,**{str(k):str(v) for k,v in task.get('environment',{}).items()},'CONFIGBACKUP_RUN_ID':uuid.uuid4().hex,'CONFIGBACKUP_TASK_NAME':task_name,'CONFIGBACKUP_OUTPUT':str(path)}
            try:outcomes.append({'capture':i+1,'return_code':collect(profile,path,env,task.get('working_directory')).returncode})
            except (OSError,ValueError,RuntimeError,__import__('subprocess').TimeoutExpired) as exc:outcomes.append({'capture':i+1,'error_type':type(exc).__name__})
        report=compare(paths);report['collection_attempts']=outcomes
        if any(r.get('return_code')!=0 for r in outcomes):report['status']='incomplete'
        return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--snapshot',action='append');p.add_argument('--config');p.add_argument('--task');p.add_argument('--repeat',type=int,default=3);p.add_argument('--output',required=True);a=p.parse_args(argv)
    if a.snapshot:
        if a.task or a.config:p.error('Choose saved snapshots or managed database collection')
        report=compare(a.snapshot)
    else:
        if not a.config or not a.task:p.error('Supply --config and --task for explicit repeated collection')
        report=repeat(yaml.safe_load(Path(a.config).read_text()),Path(a.config),a.task,a.repeat)
    write_report(report,a.output,'Determinism evidence');print(json.dumps(report,indent=2));return 0 if report['status'] in ('identical','representation_only') else 6

if __name__=='__main__':raise SystemExit(main())
