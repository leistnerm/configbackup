"""Guided menus for optional collection and local report configuration."""
import copy
import json


def select_many(prompt,values,selected=(),input_fn=input):
    if getattr(input_fn,'console_ui',False):return input_fn.select_many(prompt,values,selected)
    from configure import choice
    selected=set(selected)
    while True:
        labels=[('[x] ' if value in selected else '[ ] ')+value for value in values]
        action=choice(prompt,labels+['Done'],input_fn)
        if action in (None,'Done'):return [v for v in values if v in selected]
        value=values[labels.index(action)]
        if value in selected:selected.remove(value)
        else:selected.add(value)


def reporting_wizard(config,input_fn=input):
    from configure import ask,boolean,choice,manage
    from reporting import SECTIONS
    report=config.setdefault('reporting',{})
    report['enabled']=boolean('Generate the offline HTML review portal',report.get('enabled',False),input_fn)
    report['output']=ask('Report directory','${CONFIGBACKUP_ARCHIVE_ROOT}/_configbackup/review' if not report.get('output') else report['output'],input_fn)
    report['timezone']=ask('Display time zone (IANA)','UTC' if not report.get('timezone') else report['timezone'],input_fn)
    report['include_values']=boolean('Include sanitized configuration values in the local report',report.get('include_values',False),input_fn)
    from sections import enabled
    selected=select_many('Choose report pages',list(SECTIONS),[k for k in SECTIONS if enabled(k,report.get('sections',{}))],input_fn)
    report['sections']={name:{'enabled':name in selected} for name in SECTIONS}
    def source(inp):
        result={'id':ask('Unique source ID',input_fn=inp),'path':ask('Collector output directory containing collection-manifest.json',input_fn=inp),'enabled':True}
        baseline=ask('Optional fixed baseline capture directory (blank compares to previous report)',input_fn=inp)
        if baseline:result['baseline']=baseline
        return [result]
    manage(report.setdefault('snapshots',[]),'Report snapshot sources',source,input_fn)
    report['schedule_paths']=[{'path':p,'enabled':True} if isinstance(p,str) else p for p in report.get('schedule_paths',[])]
    manage(report['schedule_paths'],'Schedule report sources',lambda inp:[{'path':ask('Schedule report directory',input_fn=inp),'enabled':True}],input_fn)
    def runtime(inp):return [{'id':ask('Matching snapshot source ID',input_fn=inp),'path':ask('Network runtime inventory JSON path',input_fn=inp),'enabled':True}]
    manage(report.setdefault('runtime_paths',[]),'Optional listener evidence for exposure review',runtime,input_fn)
    path=ask('Optional monitoring output directory (blank uses engine default)',report.get('monitoring_path',''),input_fn)
    if path:report['monitoring_path']=path
    else:report.pop('monitoring_path',None)
    report['required']=boolean('Treat report-source failure as a required run failure',report.get('required',False),input_fn)


def optional_wizard(config,input_fn=input):
    from configure import ask,choice
    from collector_catalog import catalog,discover
    print(json.dumps(discover(),indent=2))
    tasks=[t for t in config.get('tasks',[]) if t.get('type')=='execute' and any(str(a).endswith('collect_system.py') for a in t.get('arguments',[]))]
    if not tasks:
        print('Add a System task first. Discovery checks presence; it never enables collection.');return
    selected=choice('System task',[t['name'] for t in tasks],input_fn)
    if selected is None:return
    task=next(t for t in tasks if t['name']==selected)
    rows=catalog()
    while True:
        labels=[r['id'] for r in rows]
        selected=choice('Add optional collector',labels+['Done'],input_fn)
        if selected in (None,'Done'):return
        row=rows[labels.index(selected)]
        print('Requirements: '+str(row['requirements'])+'\nLimits: '+row['limits'])
        value=row.get('value')
        if not value:
            value=choice('Option',row['values'],input_fn) if row.get('values') else ask('Explicit public certificate path' if selected=='public-certificate' else 'nginx=PATH or apache=PATH',input_fn=input_fn)
        if value:
            pairs=list(zip(task.get('arguments',[]),task.get('arguments',[])[1:]))
            if (row['flag'],value) not in pairs:task.setdefault('arguments',[]).extend([row['flag'],value])
            from sections import enabled
            if not enabled(row['scope'],task.get('sections',{})):print('This collector is selected but disabled by an existing section switch. Edit that switch to enable it.')


def apply_profile(task,engine,profile):
    if profile not in ('light','normal','deep'):raise ValueError('Unknown collection profile')
    result=copy.deepcopy(task)
    options=result.setdefault('collection',{})
    options.update(schema=profile!='light',include_health=profile=='deep',include_history=profile=='deep')
    if engine=='sqlserver':options['include_operational_inventory']=profile=='deep'
    return result


def profiles_wizard(config,input_fn=input):
    from configure import choice
    tasks=[t for t in config.get('tasks',[]) if t.get('connection')]
    if not tasks:print('Add a managed database task first.');return
    name=choice('Task',[t['name'] for t in tasks],input_fn)
    if name is None:return
    task=next(t for t in tasks if t['name']==name)
    print('Light: inventory only. Normal: inventory and schema. Deep: schema, health and history; SQL operational inventory. No scheduler or database settings are changed.')
    profile=choice('Collection profile',['light','normal','deep'],input_fn)
    if profile:
        engine=config['connections'][task['connection']]['engine'];updated=apply_profile(task,engine,profile)
        task.clear();task.update(updated)
        print('Profile applied. Existing section switches and required-section checks remain visible in Effective preview. Use separate configurations/launchers for different schedules.')
