"""Bounded aligned incident charts and descriptive observed-duration comparisons."""
import hashlib
import html
import math
import random
import statistics


def duration_comparisons(jobs):
    from configbackup_lib.reporting.performance_report import timestamp
    groups={};runs=sorted(jobs,key=lambda r:(r['host'],r['start'],r['end'],r['job_id']))
    intervals=[(timestamp(r['start']).timestamp(),timestamp(r['end']).timestamp()) for r in runs]
    # Sweep identifies runs that overlap another observed run, including same-job concurrency.
    overlap=set();active={}
    for i,(row,(start,end)) in enumerate(zip(runs,intervals)):
        active={j:e for j,e in active.items() if runs[j]['host']==row['host'] and e>start}
        if active:overlap.add(i);overlap.update(active)
        active[i]=end
    for i,(row,(start,end)) in enumerate(zip(runs,intervals)):
        group=groups.setdefault((row['host'],row['job_id']),{'solo':[],'overlap':[],'excluded':0})
        if str(row.get('status','unknown')).lower() not in ('success','succeeded','completed'):
            group['excluded']+=1;continue
        group['overlap' if i in overlap else 'solo'].append(end-start)
    output=[]
    for (host,job),group in sorted(groups.items()):
        a,b=group['solo'],group['overlap'];row={'host':host,'job_id':job,'solo_runs':len(a),'overlap_runs':len(b),'excluded_unknown_or_failed':group['excluded'],
            'solo_median_seconds':statistics.median(a) if a else None,'overlap_median_seconds':statistics.median(b) if b else None,
            'overlap_p95_seconds':sorted(b)[math.ceil(.95*len(b))-1] if b else None,'median_ratio_95pct_bootstrap':None,
            'confidence':'insufficient_runs' if min(len(a),len(b))<5 else 'descriptive_only'}
        if min(len(a),len(b))>=5:
            # Bounded recent subset and fixed seed: repeatable, not a causal confidence claim.
            a=a[-200:];b=b[-200:];rng=random.Random(int(hashlib.sha256((host+'\0'+job).encode()).hexdigest(),16))
            ratios=sorted(statistics.median(rng.choices(b,k=len(b)))/statistics.median(rng.choices(a,k=len(a))) for _ in range(400))
            row['median_ratio_95pct_bootstrap']=[ratios[9],ratios[389]]
            row['bootstrap_runs_used']={'solo':len(a),'overlap':len(b)}
        output.append(row)
    return output


def render(value):
    from configbackup_lib.reporting.performance_report import timestamp
    escape=lambda v:html.escape(str(v),quote=True)
    incidents={}
    for row in value.get('overlap_comparisons',[]):
        key=(row['host'],row['start'],row['end'])
        incidents.setdefault(key,row)
    choices=[];panels=[]
    for index,(key,incident) in enumerate(sorted(incidents.items())[:30]):
        host,start,end=key;low=timestamp(start).timestamp();high=timestamp(end).timestamp();span=max(1,high-low)
        x=lambda stamp:800*(timestamp(stamp).timestamp()-low)/span
        identity='incident-'+str(index);title=host+' · '+start+' — '+end
        choices.append('<option value="'+identity+'">'+escape(title)+'</option>')
        charts=[];series={}
        for r in value.get('samples',[]):
            if r['host']==host and timestamp(r['end']).timestamp()>low and timestamp(r['start']).timestamp()<high:
                series.setdefault((r['metric'],r['entity'],r['unit']),[]).append(r)
        for metric,rows in sorted(series.items())[:12]:
            bars=[];ceiling=max(1,max(r['value'] for r in rows));previous=low
            for r in rows[:300]:
                a=max(low,timestamp(r['start']).timestamp());b=min(high,timestamp(r['end']).timestamp())
                if a>previous:bars.append('<rect x="%.2f" y="0" width="%.2f" height="90" fill="#f9dbb3"/>'%(800*(previous-low)/span,800*(a-previous)/span))
                if any(g['host']==host and g['metric']==r['metric'] and g['entity']==r['entity'] and g['start']==r['start'] and g['end']==r['end'] for g in value.get('gaps',[]) if g['reason']=='sample interval too coarse'):continue
                height=max(0,70*r['value']/ceiling)
                bars.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#246a94"><title>%s</title></rect>'%(800*(a-low)/span,80-height,max(.5,800*(b-a)/span),height,escape(r['start']+': '+str(r['value']))));previous=max(previous,b)
            if previous<high:bars.append('<rect x="%.2f" y="0" width="%.2f" height="90" fill="#f9dbb3"/>'%(800*(previous-low)/span,800*(high-previous)/span))
            charts.append('<h3>'+escape(' / '.join(metric))+'</h3><svg role="img" aria-label="'+escape(' / '.join(metric))+'" viewBox="0 0 800 90" style="width:100%">'+''.join(bars)+'</svg>')
        jobs=[]
        for r in value.get('observed_runs',[]):
            if r['host']==host and timestamp(r['end']).timestamp()>low and timestamp(r['start']).timestamp()<high:
                left=max(0,x(r['start']));right=min(800,x(r['end']))
                jobs.append('<p>'+escape(r['job_id']+' · '+r['start']+' — '+r['end'])+'</p><svg role="img" aria-label="Observed job interval" viewBox="0 0 800 15" style="width:100%%"><rect x="%.2f" width="%.2f" height="12" fill="#517b36"/></svg>'%(left,max(.5,right-left)))
                if len(jobs)>=40:break
        panels.append('<div id="'+identity+'" class="incident-panel"'+(' hidden' if index else '')+'><h2>'+escape(title)+'</h2>'+''.join(jobs)+''.join(charts)+'</div>')
    if not choices:return '<div class="incident-view"><h2>Incident view</h2><p>No observed overlap windows are available.</p></div>'
    return '<div class="incident-view"><h2>Observed overlap incidents</h2><p>All plots share the selected time window. Orange marks missing or undisplayed samples. Up to 30 windows, 12 series, 300 samples and 40 runs per view; JSON retains accepted data. Duration confidence bounds are descriptive bootstrap estimates, not proof of slowdown caused by overlap.</p><label for="incident-select">Overlap window</label> <select id="incident-select" style="max-width:100%">'+''.join(choices)+'</select>'+''.join(panels)+'''<script>document.getElementById('incident-select').addEventListener('change',function(){document.querySelectorAll('.incident-panel').forEach(p=>p.hidden=p.id!==this.value);});</script></div>'''
