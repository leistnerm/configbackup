"""Bounded, offline before/after scheduling scenarios; never alters schedules."""
import datetime as dt
import html
import json
from schedule_insights import instant,prediction,match,capacity_intervals


def write_scenarios(output,timeline,jobs,conflicts,dependencies,deadlines,scenarios,settings,truncated):
    maximum=int(settings.get('max_visual_occurrences',1000))
    if not 1<=maximum<=10000:raise ValueError('max_visual_occurrences must be 1..10000')
    all_cases=[{'name':'Baseline','timeline':timeline,'conflicts':conflicts,'dependencies':dependencies,'deadlines':deadlines,'truncated':truncated},*scenarios]
    for case in all_cases:
        case['capacity']=capacity_intervals(case['timeline'],jobs,settings)
        case['duration_evidence']=[{'job_id':key,'samples':len(jobs[key].durations),'basis':'override' if jobs[key].override_duration is not None else 'observed P95' if jobs[key].durations else 'unknown'} for key in sorted({r['job_id'] for r in case['timeline']})]
        case['occurrence_count']=len(case['timeline']);case['visual_truncated']=len(case['timeline'])>maximum
        case['timeline']=[{**row,'duration_minutes':prediction(jobs[row['job_id']]),'timezone':jobs[row['job_id']].timezone,
                          'excluded_from_load':match(row['job_id'],settings.get('watchdogs',[])+settings.get('exclude_from_load',[]))} for row in case['timeline'][:maximum]]
    value={'schema':1,'cases':all_cases,'limits':'P95/override scenarios are estimates, not probabilities. Unknown duration has no bar. Times are UTC; source zones are listed. Load exclusions remain visible. Shifts can cross the original horizon; prerequisite history outside it is unknown. Visual occurrence limits do not change CSV analysis.'}
    (output/'what-if.json').write_text(json.dumps(value,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    esc=lambda v:html.escape(str(v))
    starts=[instant(r['start']) for case in all_cases for r in case['timeline']]
    ends=[instant(r['start'])+dt.timedelta(minutes=r['duration_minutes'] or 0) for case in all_cases for r in case['timeline']]
    starts += [instant(r['start']) for case in all_cases for r in case['capacity']['intervals']]
    ends += [instant(r['end']) for case in all_cases for r in case['capacity']['intervals']]
    begin=min(starts) if starts else dt.datetime(2000,1,1);end=max(ends) if ends else begin
    span=max((end-begin).total_seconds(),1)
    parts=[]
    for n,case in enumerate(all_cases):
        rows=[]
        for row in case['timeline']:
            left=100*(instant(row['start'])-begin).total_seconds()/span
            width=100*(row['duration_minutes'] or 0)*60/span
            bar='<span class="bar" style="left:%.5f%%;width:%.5f%%"></span>'%(left,width) if row['duration_minutes'] else ''
            rows.append('<tr><th scope="row">'+esc(row['job_id'])+'</th><td>'+esc(row['start'])+'</td><td>'+esc(row['timezone'])+'</td><td>'+esc(row['duration_minutes'] if row['duration_minutes'] is not None else 'unknown')+'</td><td>'+('excluded from load' if row['excluded_from_load'] else 'included')+'</td><td class="track">'+bar+'</td></tr>')
        dep=sum(r['status']=='predicted_conflict' for r in case['dependencies']);late=sum(r['status']=='predicted_late' for r in case['deadlines'])
        unknown=sum(r['status'].startswith('unknown') for r in case['dependencies']+case['deadlines'])
        capacity_rows=''.join('<tr><th>'+esc(r['resource'])+'</th><td>'+esc(r['peak_weight'])+'</td><td>'+esc(r['capacity'] if r['capacity'] is not None else 'unknown')+'</td><td>'+esc(r['overload_minutes'])+'</td></tr>' for r in case['capacity']['summary'])
        heat=[]
        for resource in sorted({r['resource'] for r in case['capacity']['intervals']}):
            bars=[]
            for r in case['capacity']['intervals']:
                if r['resource']!=resource:continue
                left=100*(instant(r['start'])-begin).total_seconds()/span;width=100*(instant(r['end'])-instant(r['start'])).total_seconds()/span
                color='#b33b2e' if r['status']=='capacity_exceeded' else '#246a94' if r['capacity'] is not None else '#68737b'
                bars.append('<span class="bar" title="'+esc(r['start']+' to '+r['end']+'; weight '+str(r['weight'])+'; '+r['status'])+'" style="left:%.5f%%;width:%.5f%%;background:%s"></span>'%(left,width,color))
            heat.append('<tr><th>'+esc(resource)+'</th><td class="track">'+''.join(bars)+'</td></tr>')
        capacity_html='<h3>Resource capacity estimates</h3><p>Red: over configured capacity. Blue: within capacity. Gray: no capacity specified. These are scheduling estimates.</p><div class="scroll"><table><thead><tr><th>Resource</th><th>Peak weight</th><th>Capacity</th><th>Overload minutes</th></tr></thead><tbody>'+capacity_rows+'</tbody></table></div><div class="scroll"><table>'+''.join(heat)+'</table></div><details><summary>Duration evidence</summary><pre>'+esc(json.dumps(case['duration_evidence'],indent=2))+'</pre></details>'
        parts.append('<section data-case="'+str(n)+'"'+(' hidden' if n>1 else '')+'><h2>'+esc(case['name'])+'</h2><p>'+str(len(case['conflicts']))+' conflict rows; '+str(dep)+' dependency conflicts; '+str(late)+' predicted deadline misses; '+str(unknown)+' unknown checks.</p><p>'+('TRUNCATED: inspect CSV and JSON evidence.' if case['truncated'] or case['visual_truncated'] or case['capacity']['truncated'] else 'All analyzed rows shown.')+'</p><div class="scroll"><table><thead><tr><th>Job</th><th>UTC start</th><th>Source zone</th><th>Minutes</th><th>Load</th><th>Shared timeline</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'+capacity_html+'</section>')
    options=''.join('<option value="'+str(n)+'">'+esc(case['name'])+'</option>' for n,case in enumerate(all_cases) if n)
    doc='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Schedule scenarios</title><style>body{font:16px system-ui;margin:2rem;color:#182633;background:#f4f6f8}section{background:white;padding:1rem;margin:1rem 0;border:1px solid #c7d3dc}.scroll{overflow:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.5rem;border-bottom:1px solid #ddd}.track{position:relative;min-width:350px}.bar{position:absolute;height:12px;top:40%;background:#246a94;min-width:2px}select{font:inherit;padding:.5rem;max-width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}section{min-width:0}p{overflow-wrap:anywhere}[hidden]{display:none!important}</style><h1>Schedule scenarios</h1><p>'''+esc(value['limits'])+'</p><p>Shared UTC scale: '+esc(begin.isoformat())+' to '+esc(end.isoformat())+'</p><p><a href="what-if.json">Detailed JSON</a> · <a href="what-if.csv">Summary CSV</a> · <a href="dashboard.html">Dashboard</a></p><label for="scenario">Compare with baseline</label> <select id="scenario">'+options+'</select>'+''.join(parts)+'''<script defer>const choice=document.getElementById('scenario');function show(){document.querySelectorAll('[data-case]').forEach(s=>{s.hidden=s.dataset.case!=='0'&&s.dataset.case!==choice.value;});}choice.addEventListener('change',show);show();</script></html>'''
    (output/'what-if.html').write_text(doc,encoding='utf-8')
