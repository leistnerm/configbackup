"""Bounded performance facts for existing local dashboards and email digests."""
import json
import math
from pathlib import Path


def read(config,now):
    from performance_report import timestamp
    rows=[];metrics=[];failures=[]
    from monitoring import metric
    for spec in config.get('performance_reports',[]):
        if not spec.get('enabled',True):continue
        path=Path(spec['path']);label={'source':'performance:'+str(path)}
        try:
            if path.is_symlink() or path.stat().st_size>64*1024*1024:raise ValueError('Unsafe performance report')
            value=json.loads(path.read_text());end=value.get('data_window',{}).get('end')
            age=(now-timestamp(end).timestamp())/3600 if end else None
            maximum=float(spec.get('max_age_hours',24))
            if not math.isfinite(maximum) or maximum<=0:raise ValueError('Invalid maximum report age')
            status='unknown' if age is None else 'clock_skew' if age< -1/60 else 'stale' if age>maximum else value.get('status','unknown')
            rows.append({'source':str(path),'status':status,'sample_age_hours':round(age,3) if age is not None else None})
            metrics.append(metric('performance.source.unavailable',int(status!='complete'),label,now))
            if age is not None:metrics.append(metric('performance.sample_age_hours',max(0,age),label,now))
            if status not in ('complete','partial'):continue
            for row in value.get('summary',[])[:100]:
                rows.append({k:row.get(k) for k in ('host','metric','entity','samples','p95_sample_value','peak','minutes_at_or_above_warning')})
                labels={**label,**{k:str(row.get(k,'')) for k in ('host','metric','entity')}}
                pressure=row.get('minutes_at_or_above_warning')
                if isinstance(pressure,(int,float)) and math.isfinite(pressure):metrics.append(metric('performance.pressure_minutes',pressure,labels,now))
            count=sum(r.get('status')=='observed_overlap_with_pressure' for r in value.get('overlap_comparisons',[]))
            rows.append({'source':str(path),'overlap_metric_windows_with_pressure':count,'gaps':len(value.get('gaps',[])),'warning':'Correlation does not establish causation'})
            metrics.append(metric('performance.overlap_pressure_count',count,label,now))
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            rows.append({'source':str(path),'status':'unavailable'});failures.append({'source':str(path),'error':'Performance report unavailable'})
            metrics.append(metric('performance.source.unavailable',1,label,now))
    return rows,metrics,failures
