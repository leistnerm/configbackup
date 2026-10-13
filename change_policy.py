"""Explain observed changes with explicit review rules; never infer unseen values."""
from __future__ import annotations
import datetime as dt
import fnmatch
import hashlib
import json

DEFAULTS=[
 {'id':'security-change','path':'*security*','attention':'review'},
 {'id':'permission-change','path':'*permission*','attention':'review'},
 {'id':'firewall-change','path':'*firewall*','attention':'review'},
 {'id':'listener-change','path':'*listener*','attention':'review'},
 {'id':'database-option-change','path':'*database*','attention':'review'},
 {'id':'disabled-item','property':'*/enabled','before':True,'after':False,'attention':'high'},
]


def validate(policy):
    if not isinstance(policy,dict):raise ValueError('change_policy must be a mapping')
    rules=policy.get('rules',DEFAULTS);approvals=policy.get('approved_fingerprints',[]);windows=policy.get('maintenance_windows',[])
    if not isinstance(rules,list) or not isinstance(approvals,list) or not isinstance(windows,list):raise ValueError('Policy rules, approvals and windows must be lists')
    if any(not isinstance(x,str) or not __import__('re').fullmatch('[0-9a-f]{64}',x) for x in approvals):raise ValueError('Approvals must be exact evidence fingerprints')
    ids=set()
    for rule in rules:
        if not isinstance(rule,dict) or not isinstance(rule.get('id'),str) or not rule['id'] or rule['id'] in ids:raise ValueError('Rules require unique string ids')
        ids.add(rule['id'])
        if not isinstance(rule.get('enabled',True),bool):raise ValueError('Rule enabled must be boolean')
        if rule.get('attention','review') not in ('normal','review','high'):raise ValueError('Unknown attention level')
        if any(not isinstance(rule[key],str) for key in ('source','task','path','property','change','description') if key in rule):raise ValueError('Rule patterns must be strings')
    for window in windows:
        if not isinstance(window,dict):raise ValueError('Invalid maintenance window')
        start=dt.datetime.fromisoformat(window['start']);end=dt.datetime.fromisoformat(window['end'])
        if start.tzinfo is None or end.tzinfo is None or end<=start:raise ValueError('Maintenance windows need ordered offset-aware dates')
    return policy


def evaluate(changes,policy=None,as_of=None):
    policy=validate(policy or {});rules=policy.get('rules',DEFAULTS);approved=policy.get('approved_fingerprints',[])
    if not isinstance(rules,list) or not isinstance(approved,list):raise ValueError('Policy rules and approvals must be lists')
    now=dt.datetime.fromisoformat(as_of) if isinstance(as_of,str) else as_of or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('Policy time must have an explicit UTC offset')
    output=[]
    for row in changes:
        if row.get('change','').startswith('unverified'):continue
        evidence={k:row.get(k) for k in ('source','task','path','property','change','before_sha256','after_sha256','before','after')}
        fingerprint=hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()
        for rule in rules:
            if not isinstance(rule,dict) or not rule.get('id'):raise ValueError('Every policy rule needs an id')
            if rule.get('enabled',True) is False:continue
            if any(not fnmatch.fnmatchcase(str(row.get(field,'')).casefold(),str(rule[field]).casefold()) for field in ('source','task','path','property','change') if field in rule):continue
            if 'before' in rule and row.get('before')!=rule['before'] or 'after' in rule and row.get('after')!=rule['after']:continue
            approvable=bool(row.get('before_sha256') or row.get('after_sha256'))
            disposition='approved' if approvable and fingerprint in approved else 'review'
            for window in policy.get('maintenance_windows',[]):
                start=dt.datetime.fromisoformat(window['start']);end=dt.datetime.fromisoformat(window['end'])
                if start.tzinfo is None or end.tzinfo is None or end<=start:raise ValueError('Maintenance windows need ordered offset-aware dates')
                if start<=now<end and fnmatch.fnmatchcase(str(row.get('source',row.get('task',''))),window.get('source','*')):disposition='maintenance'
            output.append({'rule':rule['id'],'source':row.get('source',row.get('task','')),'path':row.get('path',''),'property':row.get('property',''),'change':row.get('change'),'attention':rule.get('attention','review'),'disposition':disposition,'fingerprint':fingerprint,'approvable':approvable,'explanation':rule.get('description','Observed matching change; review the native diff. No privilege expansion or reachability is inferred.')})
    return sorted(output,key=lambda r:(r['source'],r['path'],r['property'],r['rule']))
