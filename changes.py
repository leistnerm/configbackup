"""Explain changed file categories without exposing configuration values."""
from __future__ import annotations
import csv
import json
from pathlib import Path


def explain(changes):
    rows=[]
    for item in sorted(changes,key=lambda x:(x['task'],x['path'],x['change'])):
        name=item['path'].casefold()
        category,attention,reason='configuration','normal','Stored file content changed; inspect its native or comparison diff.'
        for words,label,why in [
            (('security','permission','principal','role','grant','policy','rls','mask','credential'),'security','Access or security-related file changed; review intended permissions.'),
            (('schedule','agent','cron','launchd','timer'),'scheduling','Scheduling-related file changed; regenerate expected execution and conflict reports.'),
            (('partition','filegroup','database-files','files.csv'),'storage','Storage-related definition changed; review placement, boundaries and capacity.'),
            (('replication','availability','cluster'),'availability','Availability-related definition changed; review topology and recovery assumptions.'),
            (('schema','table','view','procedure','function'),'schema','Schema/object representation changed; assess dependent applications before deployment.')]:
            if any(word in name for word in words): category,attention,reason=label,'review',why;break
        if item['change']=='deleted': attention,reason='review','Deletion observed inside a successfully certified scope; retained history may still be available.'
        rows.append({**item,'category':category,'attention':attention,'reason':reason})
    return rows


def write_report(directory, changes):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    rows=explain(changes)
    (directory/'changes.json').write_text(json.dumps(rows,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    with (directory/'changes.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['task','path','change','category','attention','reason']);w.writeheader();w.writerows(rows)
    return rows
