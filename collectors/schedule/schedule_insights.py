"""Explain scheduling risk from explicit execution facts and declared resources."""
from __future__ import annotations
import datetime as dt
import fnmatch
import math
import json
import statistics
from zoneinfo import ZoneInfo


def match(value, patterns):
    return any(fnmatch.fnmatchcase(value.casefold(),str(p).casefold()) for p in patterns)


def instant(value):
    if not value: return None
    value=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def success(source, status):
    status=str(status or '').casefold()
    return status in ({'1','success','succeeded'} if source=='sql_agent' else {'s','success','succeeded','completed'})


def percentile(values, proportion=.95):
    return sorted(values)[max(0,math.ceil(len(values)*proportion)-1)] if values else None


def prediction(job, basis='p95'):
    if job.override_duration is not None: return job.override_duration
    return percentile(job.durations) if basis=='p95' else statistics.median(job.durations) if job.durations else None


def history_covers(job, when, settings):
    for pattern, coverage in settings.get('history_coverage',{}).items():
        if match(job.key,[pattern]) and coverage.get('complete') and instant(coverage['start'])<=when<instant(coverage['end']): return True
    return False


def executions(timeline, jobs, settings, now):
    rows=[];used=set();matches={}
    early=float(settings.get('early_tolerance_minutes',5));late=float(settings.get('match_window_minutes',60))
    # Assign globally before emitting rows: an early projected occurrence must
    # not steal an exact later match inside overlapping tolerance windows.
    pairs=[]
    for occurrence_index,occurrence in enumerate(timeline):
        job=jobs[occurrence['job_id']];planned=instant(occurrence['start'])
        for n,observed in enumerate(job.observed):
            start=instant(observed['start'])
            explicit=instant(observed.get('scheduled_start'))
            if explicit == planned or (explicit is None and planned-dt.timedelta(minutes=early)<=start<=planned+dt.timedelta(minutes=late)):
                if len(pairs)>=int(settings.get('max_match_pairs',500000)):raise ValueError('Execution matching limit exceeded; narrow horizon/history or tolerances')
                pairs.append((0 if explicit else 1,abs((start-planned).total_seconds()),occurrence['start'],job.key,json.dumps(observed,sort_keys=True),n,occurrence_index))
    for _,_,_,key,_,n,index in sorted(pairs):
        if index not in matches and (key,n) not in used:
            matches[index]=jobs[key].observed[n];used.add((key,n))
    for occurrence_index,occurrence in enumerate(timeline):
        job=jobs[occurrence['job_id']];planned=instant(occurrence['start'])
        row={'job_id':job.key,'scheduled_start':occurrence['start'],'actual_start':'','delay_minutes':'','status':'future' if planned>now else 'unobserved'}
        if occurrence_index in matches:
            observed=matches[occurrence_index]
            begin=instant(observed['start']);end=instant(observed.get('end'))
            delay=(begin-planned).total_seconds()/60
            row.update(actual_start=begin.isoformat(),delay_minutes=round(delay,3))
            if end is None:
                limit=prediction(job)
                row['status']='overrunning' if limit is not None and (now-begin).total_seconds()/60>limit else 'running'
            elif not success(job.source,observed.get('status')):
                row['status']='failed' if str(observed.get('status','')).casefold() in ('0','failed','f','cancelled','3','d') else 'outcome_unknown'
            else: row['status']='late' if delay>float(settings.get('late_after_minutes',5)) else 'matched'
        elif planned+dt.timedelta(minutes=late)<=now:
            row['status']='missed' if history_covers(job,planned,settings) else 'unobserved'
        rows.append(row)
    for job in jobs.values():
        for n,observed in enumerate(job.observed):
            if (job.key,n) in used: continue
            start=instant(observed['start'])
            if not timeline or not (instant(timeline[0]['start'])<=start<=now): continue
            rows.append({'job_id':job.key,'scheduled_start':'','actual_start':observed['start'],
                         'delay_minutes':'','status':'running' if not observed.get('end') else 'unscheduled_or_unmatched'})
    return rows


def trends(jobs, settings, now):
    rows=[]
    recent_days=float(settings.get('recent_days',7));baseline_days=float(settings.get('baseline_days',60))
    minimum=int(settings.get('minimum_success_samples',10))
    for job in jobs.values():
        recent=[];baseline=[];failed=0;latest=None
        for observed in job.observed:
            begin=instant(observed['start']);age=(now-begin).total_seconds()/86400
            if age<0 or age>baseline_days: continue
            latest=max(latest,begin) if latest else begin
            if not success(job.source,observed.get('status')): failed+=1;continue
            value=observed.get('duration_minutes')
            if value is None: continue
            (recent if age<=recent_days else baseline).append(float(value))
        recent_median=statistics.median(recent) if recent else None
        baseline_median=statistics.median(baseline) if baseline else None
        ratio=recent_median/baseline_median if recent_median is not None and baseline_median else None
        enough=len(recent)>=minimum and len(baseline)>=minimum
        rows.append({'job_id':job.key,'recent_samples':len(recent),'baseline_samples':len(baseline),'failed_or_unknown_executions':failed,
                     'recent_median_minutes':recent_median,'baseline_median_minutes':baseline_median,'recent_p95_minutes':percentile(recent),
                     'duration_ratio':ratio,'latest_observation':latest.isoformat() if latest else '',
                     'status':'insufficient_history' if not enough else 'regression' if ratio and ratio>float(settings.get('regression_ratio',1.5)) else 'ok'})
    return rows


def resource_events(timeline,jobs,settings):
    events=[]
    for row in timeline:
        key=row['job_id'];job=jobs[key]
        if match(key,settings.get('watchdogs',[])+settings.get('exclude_from_load',[])): continue
        duration=prediction(job)
        if duration is None or duration<=0: continue
        resources={'host:'+job.host}
        for pattern,items in settings.get('job_resources',{}).items():
            if match(key,[pattern]): resources.update(items)
        for resource in sorted(resources):
            weight=1.0
            for pattern,value in settings.get('resource_weights',{}).get(resource,{}).items():
                if match(key,[pattern]): weight=float(value)
            events.append({'job_id':key,'resource':resource,'start':instant(row['start']),
                           'end':instant(row['start'])+dt.timedelta(minutes=duration),'weight':weight})
    return events


def conflicts(timeline,jobs,settings):
    result=[];active={};limit=int(settings.get('max_conflict_rows',20000));truncated=False
    for row in sorted(resource_events(timeline,jobs,settings),key=lambda r:(r['start'],r['job_id'],r['resource'])):
        resource=row['resource'];others=[r for r in active.get(resource,[]) if r['end']>row['start']]
        capacity=settings.get('resource_capacity',{}).get(resource)
        if not others and capacity is not None and row['weight']>float(capacity):
            if len(result)>=limit:truncated=True
            else:result.append({'resource':resource,'first_job_id':row['job_id'],'second_job_id':'',
                'start':row['start'].isoformat(),'overlap_minutes':(row['end']-row['start']).total_seconds()/60,
                'concurrent_weight':row['weight'],'capacity':capacity,'status':'capacity_exceeded','basis':'single-job p95 capacity scenario'})
        for earlier in others:
            if len(result)>=limit: truncated=True;break
            result.append({'resource':resource,'first_job_id':earlier['job_id'],'second_job_id':row['job_id'],
                           'start':row['start'].isoformat(),'overlap_minutes':(min(earlier['end'],row['end'])-row['start']).total_seconds()/60,
                           'concurrent_weight':row['weight']+sum(r['weight'] for r in others),'capacity':capacity,
                           'status':'capacity_exceeded' if capacity is not None and row['weight']+sum(r['weight'] for r in others)>float(capacity) else 'possible_contention',
                           'basis':'p95 scenario; not overlap probability'})
        active[resource]=others+[row]
    return result,truncated


def capacity_intervals(timeline,jobs,settings):
    """Half-open resource intervals, independent of pairwise contention rows."""
    resources={};rows=[];truncated=False;limit=int(settings.get('max_conflict_rows',20000))
    for i,event in enumerate(resource_events(timeline,jobs,settings)):
        if not math.isfinite(event['weight']) or event['weight']<0:raise ValueError('Resource weights must be finite and nonnegative')
        resources.setdefault(event['resource'],[]).extend([(event['start'],1,i,event),(event['end'],-1,i,event)])
    summary=[]
    for resource,events in sorted(resources.items()):
        active={};previous=None;peak=0;overload=0;capacity=settings.get('resource_capacity',{}).get(resource)
        if capacity is not None and (not math.isfinite(float(capacity)) or float(capacity)<0):raise ValueError('Resource capacity must be finite and nonnegative')
        for when,kind,i,event in sorted(events,key=lambda r:(r[0],r[1],r[2])):
            if previous is not None and when>previous and active:
                load=sum(e['weight'] for e in active.values());peak=max(peak,load)
                exceeded=capacity is not None and load>float(capacity)
                if exceeded:overload+=(when-previous).total_seconds()/60
                row={'resource':resource,'start':previous.isoformat(),'end':when.isoformat(),'jobs':sorted(e['job_id'] for e in active.values()),'weight':load,'capacity':capacity,'status':'capacity_exceeded' if exceeded else 'within_capacity' if capacity is not None else 'capacity_unknown'}
                if len(rows)<limit:rows.append(row)
                else:truncated=True
            if kind==1:active[i]=event
            else:active.pop(i,None)
            previous=when
        summary.append({'resource':resource,'peak_weight':peak,'capacity':capacity,'overload_minutes':round(overload,6)})
    return {'intervals':rows,'summary':summary,'truncated':truncated,'basis':'P95/override estimates, not measured utilization or probability'}


def dependencies(timeline,jobs,settings):
    result=[]
    for row in timeline:
        for definition in settings.get('dependencies',[]):
            if not match(row['job_id'],[definition['job']]): continue
            when=instant(row['start']);lookback=dt.timedelta(hours=float(definition.get('max_age_hours',24)))
            for prerequisite in definition.get('requires',[]):
                options=[r for r in timeline if match(r['job_id'],[prerequisite]) and when-lookback<=instant(r['start'])<=when]
                if not options:
                    result.append({'job_id':row['job_id'],'start':row['start'],'requires':prerequisite,'prerequisite_end':'','status':'unknown_no_occurrence_in_horizon'});continue
                selected=max(options,key=lambda r:r['start']);duration=prediction(jobs[selected['job_id']])
                end=instant(selected['start'])+dt.timedelta(minutes=duration) if duration is not None else None
                result.append({'job_id':row['job_id'],'start':row['start'],'requires':prerequisite,'prerequisite_end':end.isoformat() if end else '',
                               'status':'unknown_duration' if end is None else 'predicted_conflict' if end>when else 'ok'})
    return result


def deadlines(timeline,jobs,settings):
    result=[]
    for row in timeline:
        job=jobs[row['job_id']]
        for pattern,definition in settings.get('deadlines',{}).items():
            if not match(job.key,[pattern]):continue
            definition={'time':definition} if isinstance(definition,str) else definition
            zone=ZoneInfo(job.timezone)
            start=instant(row['start']);day=start.replace(tzinfo=dt.timezone.utc).astimezone(zone).date()+dt.timedelta(days=int(definition.get('day_offset',0)))
            naive=dt.datetime.combine(day,dt.time.fromisoformat(definition['time']))
            first=naive.replace(tzinfo=zone,fold=0);second=naive.replace(tzinfo=zone,fold=1)
            ambiguous=first.utcoffset()!=second.utcoffset() or first.astimezone(dt.timezone.utc).astimezone(zone).replace(tzinfo=None)!=naive
            deadline=first.astimezone(dt.timezone.utc).replace(tzinfo=None)
            duration=prediction(job)
            end=start+dt.timedelta(minutes=duration) if duration is not None else None
            result.append({'job_id':job.key,'start':row['start'],'deadline':deadline.isoformat() if not ambiguous else '',
                           'estimated_end':end.isoformat() if end else '', 'status':'unknown_dst' if ambiguous else 'unknown_duration' if end is None else 'predicted_late' if end>deadline else 'ok'})
    return result


def watchdogs(jobs,settings,now):
    rows=[]
    for job in jobs.values():
        if not match(job.key,settings.get('watchdogs',[])):continue
        threshold=None
        for pattern,value in settings.get('watchdog_max_silence_minutes',{}).items():
            if match(job.key,[pattern]):threshold=float(value)
        valid=[instant(r['observed_at']) for r in getattr(job,'heartbeats',[]) if r.get('status','success') in ('success','healthy') and instant(r['observed_at'])<=now]
        latest=max(valid) if valid else None;age=(now-latest).total_seconds()/60 if latest else None
        rows.append({'job_id':job.key,'last_useful_work':latest.isoformat() if latest else '', 'silence_minutes':age,'threshold_minutes':threshold,
                     'status':'unknown' if latest is None or threshold is None else 'stale' if age>threshold else 'healthy'})
    return rows


def analyze(output,timeline,jobs,settings,write_csv,now):
    now=instant(now)
    execution=executions(timeline,jobs,settings,now)
    trend=trends(jobs,settings,now)
    conflict,truncated=conflicts(timeline,jobs,settings)
    dependent=dependencies(timeline,jobs,settings)
    deadline=deadlines(timeline,jobs,settings)
    watchdog=watchdogs(jobs,settings,now)
    outputs={'execution-analysis':(execution,['job_id','scheduled_start','actual_start','delay_minutes','status']),
             'runtime-trends':(trend,['job_id','recent_samples','baseline_samples','failed_or_unknown_executions','recent_median_minutes','baseline_median_minutes','recent_p95_minutes','duration_ratio','latest_observation','status']),
             'resource-conflicts':(conflict,['resource','first_job_id','second_job_id','start','overlap_minutes','concurrent_weight','capacity','status','basis']),
             'dependencies':(dependent,['job_id','start','requires','prerequisite_end','status']),
             'deadlines':(deadline,['job_id','start','deadline','estimated_end','status']),
             'watchdogs':(watchdog,['job_id','last_useful_work','silence_minutes','threshold_minutes','status'])}
    capacity=capacity_intervals(timeline,jobs,settings)
    outputs['resource-capacity']=(capacity['intervals'],['resource','start','end','jobs','weight','capacity','status'])
    scenario_timeline=[r for r in timeline if not match(r['job_id'],settings.get('_what_if_exclude',[]))]
    scenario_baseline,scenario_truncated=conflicts(scenario_timeline,jobs,settings)
    scenarios=[];details=[]
    for scenario in settings.get('what_if',[]):
        shifted=[]
        for row in scenario_timeline:
            minutes=sum(float(v) for pattern,v in scenario.get('shifts_minutes',{}).items() if match(row['job_id'],[pattern]))
            shifted.append({**row,'start':(instant(row['start'])+dt.timedelta(minutes=minutes)).isoformat()})
        shifted.sort(key=lambda r:(r['start'],r['job_id']))
        after,cut=conflicts(shifted,jobs,settings)
        deps=dependencies(shifted,jobs,settings);limits=deadlines(shifted,jobs,settings)
        details.append({'name':scenario['name'],'timeline':shifted,'conflicts':after,'dependencies':deps,'deadlines':limits,'truncated':cut})
        scenarios.append({'scenario':scenario['name'],'baseline_conflict_rows':len(scenario_baseline),'scenario_conflict_rows':len(after),
                          'conflict_delta':len(after)-len(scenario_baseline),'dependency_conflicts':sum(r['status']=='predicted_conflict' for r in deps),
                          'deadline_misses':sum(r['status']=='predicted_late' for r in limits),'truncated':cut or scenario_truncated})
    outputs['what-if']=(scenarios,['scenario','baseline_conflict_rows','scenario_conflict_rows','conflict_delta','dependency_conflicts','deadline_misses','truncated'])
    for name,(rows,columns) in outputs.items():write_csv(output/(name+'.csv'),rows,columns)
    from schedule_visual import write_scenarios
    write_scenarios(output,scenario_timeline,jobs,scenario_baseline,dependencies(scenario_timeline,jobs,settings),deadlines(scenario_timeline,jobs,settings),details,settings,scenario_truncated)
    return {'analysis_as_of':now.isoformat(),'resource_conflicts_truncated':truncated,'scenario_count':len(scenarios)}
