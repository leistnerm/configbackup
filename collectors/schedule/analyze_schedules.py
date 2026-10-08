#!/usr/bin/env python3
"""Combine saved Windows/SQL Agent/PostgreSQL schedule snapshots into a workload calendar.

This is an offline report generator: no database connections, no OS scheduler changes,
no credentials. Predicted overlaps use actual run duration history where supplied.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import fnmatch
import html
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

VERSION = '1.5.0'


def notice(message: str) -> None:
    print(f'[schedule-analyzer] {message}', file=sys.stderr)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def read_json(path: Path) -> Any:
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, '') for name in columns})


def match_id(value: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(value.casefold(), str(pattern).casefold()) for pattern in patterns)


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool:
    return str(value).lower() in ('1', 'true', 'yes')


def sql_clock(value: Any) -> dt.time:
    n = as_int(value)
    return dt.time((n // 10000) % 24, (n // 100) % 100, n % 100)


def sql_date(value: Any) -> dt.date | None:
    try:
        n = str(value).replace('-', '')[:8]
        return dt.datetime.strptime(n, '%Y%m%d').date()
    except ValueError:
        return None


def sql_duration_minutes(value: Any) -> float:
    # SQL Agent run_duration is HHMMSS, but HH may exceed 23.
    n = as_int(value)
    return round((n // 10000) * 60 + (n // 100 % 100) + (n % 100) / 60, 4)


def parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        # Analysis works in naive wall-clock time using configured system timezone.
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def median_p95(durations: list[float]) -> tuple[float | None, float | None]:
    items = sorted(x for x in durations if x >= 0)
    if not items:
        return None, None
    return round(statistics.median(items), 2), round(items[math.ceil(0.95 * len(items)) - 1], 2)


@dataclass
class Job:
    key: str
    source: str
    name: str
    cadence: str
    schedule: str
    expand: Callable[[dt.date, int], list[dt.datetime]]
    durations: list[float] = field(default_factory=list)
    uncertainty: str = ''
    override_duration: float | None = None

    def duration(self) -> float | None:
        if self.override_duration is not None:
            return self.override_duration
        return median_p95(self.durations)[0]

    def duration_source(self) -> str:
        if self.override_duration is not None:
            return 'override'
        return 'observed' if self.durations else 'unknown'


def date_range(start: dt.date, days: int):
    for index in range(days):
        yield start + dt.timedelta(days=index)


def at(day: dt.date, when: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, when)


def expand_fixed(begin: dt.datetime, start: dt.date, days: int, period_minutes: int | None = None,
                 until: dt.datetime | None = None) -> list[dt.datetime]:
    lower = dt.datetime.combine(start, dt.time.min)
    upper = lower + dt.timedelta(days=days)
    if period_minutes is None or period_minutes <= 0:
        return [begin] if lower <= begin < upper else []
    # fast-forward without traversing years of old repeats
    step = dt.timedelta(minutes=period_minutes)
    current = begin
    if current < lower:
        current += step * max(0, int((lower-current).total_seconds() // step.total_seconds()))
        while current < lower:
            current += step
    result = []
    while current < upper and (until is None or current <= until):
        result.append(current)
        current += step
    return result


def expand_sql_schedule(spec: dict[str, str], start: dt.date, days: int) -> tuple[list[dt.datetime], str, str]:
    freq_type = as_int(spec.get('freq_type'))
    recurrence = max(1, as_int(spec.get('freq_recurrence_factor'), 1))
    interval = as_int(spec.get('freq_interval'), 1)
    anchor = sql_date(spec.get('active_start_date')) or start
    end = sql_date(spec.get('active_end_date'))
    first_time, last_time = sql_clock(spec.get('active_start_time')), sql_clock(spec.get('active_end_time') or 235959)
    subday_type = as_int(spec.get('freq_subday_type'), 1)
    subday_interval = max(1, as_int(spec.get('freq_subday_interval'), 1))
    period_seconds = {2: subday_interval, 4: subday_interval * 60, 8: subday_interval * 3600}.get(subday_type, 0)
    cadence = {1:'once', 4:'daily', 8:'weekly', 16:'monthly', 32:'monthly', 64:'event', 128:'event'}.get(freq_type,'unknown')
    if freq_type in (64,128):
        return [], cadence, 'SQL Agent startup/idle event; no predictable clock start'

    def date_matches(day: dt.date) -> bool:
        if day < anchor or end is not None and day > end:
            return False
        if freq_type == 1:
            return day == anchor
        if freq_type == 4:
            return (day-anchor).days % max(1,interval) == 0
        if freq_type == 8:
            # SQL Agent: Sunday=1, Monday=2,... Saturday=64
            mask = 1 << ((day.weekday() + 1) % 7)
            week0 = anchor - dt.timedelta(days=(anchor.weekday() + 1) % 7)
            weekn = day - dt.timedelta(days=(day.weekday() + 1) % 7)
            return bool(mask & interval) and ((weekn-week0).days//7)%recurrence == 0
        if freq_type in (16, 32):
            months = (day.year-anchor.year)*12 + (day.month-anchor.month)
            if months < 0 or months%recurrence:
                return False
            if freq_type == 16:
                return day.day == interval
            # Relative ordinal: 1 first 2 second 4 third 8 fourth 16 last
            relative = as_int(spec.get('freq_relative_interval'), 1)
            candidates = [d for d in range(1,calendar.monthrange(day.year, day.month)[1]+1) if
                          interval == 8 or
                          (interval == 9 and dt.date(day.year,day.month,d).weekday() < 5) or
                          (interval == 10 and dt.date(day.year,day.month,d).weekday() >= 5) or
                          (interval in range(1,8) and ((dt.date(day.year,day.month,d).weekday()+1)%7)+1 == interval)]
            if not candidates:
                return False
            index = {1:0,2:1,4:2,8:3,16:-1}.get(relative,0)
            return day.day == candidates[min(index,len(candidates)-1)]
        return False

    if period_seconds and days * (86400 / period_seconds) > 300000:
        return [], cadence, 'Very frequent schedule (>300,000 possible runs); expansion intentionally limited'
    events = []
    for day in date_range(start, days):
        if not date_matches(day):
            continue
        earliest = at(day, first_time)
        latest = at(day, last_time)
        if period_seconds == 0:
            events.append(earliest)
        else:
            current = earliest
            while current <= latest:
                events.append(current)
                current += dt.timedelta(seconds=period_seconds)
    return events, cadence, '' if freq_type in (1,4,8,16,32) else 'Unsupported frequency'


def cron_field(expr: str, minval: int, maxval: int, names: dict[str,int] | None = None) -> set[int]:
    names = names or {}
    def num(text: str) -> int:
        text = text.lower()
        return names[text] if text in names else int(text)
    result: set[int] = set()
    for chunk in expr.lower().split(','):
        base, slash, step_text = chunk.partition('/')
        step = int(step_text) if slash else 1
        if step < 1:
            raise ValueError('bad step')
        if base == '*':
            lo,hi = minval,maxval
        elif '-' in base:
            lhs,rhs=base.split('-',1);lo,hi=num(lhs),num(rhs)
        else:
            lo=hi=num(base)
            if slash:
                hi=maxval
        if lo<minval or hi>maxval or lo>hi:
            raise ValueError('cron field outside range')
        result.update(range(lo,hi+1,step))
    return result


def expand_cron(expression: str, start: dt.date, days: int) -> tuple[list[dt.datetime],str,str]:
    aliases = {'@hourly':'0 * * * *','@daily':'0 0 * * *','@midnight':'0 0 * * *',
               '@weekly':'0 0 * * 0','@monthly':'0 0 1 * *','@yearly':'0 0 1 1 *','@annually':'0 0 1 1 *'}
    expression = aliases.get(expression.strip().lower(),expression)
    parts=expression.split()
    if len(parts)!=5:
        return [],'unknown',f'Unsupported cron expression {expression!r}'
    try:
        mi=cron_field(parts[0],0,59);hr=cron_field(parts[1],0,23)
        dom=cron_field(parts[2],1,31)
        mo=cron_field(parts[3],1,12,{x.lower():i for i,x in enumerate(calendar.month_abbr) if x})
        wd=cron_field(parts[4],0,7,{x.lower():i for i,x in enumerate(('sun','mon','tue','wed','thu','fri','sat'))})
        if 7 in wd: wd.add(0)
    except (ValueError,TypeError) as exc:
        return [],'unknown',f'Invalid cron expression: {exc}'
    cadence = 'monthly' if parts[2] != '*' or parts[3] != '*' else ('weekly' if parts[4] != '*' else 'daily')
    events=[]
    for day in date_range(start, days):
        if day.month not in mo: continue
        # cron day-of-month vs weekday uses OR if both fields restricted.
        dom_ok=day.day in dom
        dow_ok=(day.weekday()+1)%7 in wd
        matches= (dom_ok or dow_ok) if parts[2]!='*' and parts[4]!='*' else (dom_ok and dow_ok)
        if not matches: continue
        for h in sorted(hr):
            for minute in sorted(mi):
                events.append(at(day,dt.time(h,minute)))
    return events,cadence,''


def parse_duration_iso(value: str) -> int | None:
    match=re.fullmatch(r'P(?:([0-9]+)D)?(?:T(?:([0-9]+)H)?(?:([0-9]+)M)?(?:([0-9]+)S)?)?',str(value or ''),re.I)
    if not match or not any(match.groups()):return None
    d,h,m,s=(int(x or 0) for x in match.groups())
    return d*86400+h*3600+m*60+s


def expand_windows_trigger(trigger: dict[str, Any], start: dt.date, days: int) -> tuple[list[dt.datetime],str,str]:
    begin=parse_dt(trigger.get('StartBoundary'))
    if not begin:
        return [],'event','Windows startup/login/event trigger or absent StartBoundary'
    cls=str(trigger.get('CimClass') or '').lower()
    end=parse_dt(trigger.get('EndBoundary'))
    weeks=as_int(trigger.get('WeeksInterval'),1)
    day_int=as_int(trigger.get('DaysInterval'),1)
    dow=as_int(trigger.get('DaysOfWeek'))
    dom=as_int(trigger.get('DaysOfMonth'))
    month_mask=as_int(trigger.get('MonthsOfYear'))
    # Some Windows CIM monthly triggers are projected as their base class or
    # have incorrectly typed DaysOfMonth fields. Never silently fabricate days.
    if 'monthlydow' in cls:
        return [],'monthly','Monthly day-of-week CIM trigger requires XML-based calendar parsing; unsupported'
    if 'monthly' in cls and (not dom or not month_mask):
        return [],'monthly','Missing/unsupported monthly day or month mask in Windows CIM export'
    if 'weekly' in cls and not dow:
        return [],'weekly','Missing/unsupported DaysOfWeek mask in Windows CIM export'
    cadence='once'
    def matches(date: dt.date) -> bool:
        nonlocal cadence
        if date < begin.date() or end is not None and date > end.date(): return False
        if 'daily' in cls:
            cadence='daily';return (date-begin.date()).days%max(1,day_int)==0
        if 'weekly' in cls:
            cadence='weekly';mask=1 << ((date.weekday()+1)%7)
            return bool(mask&dow) and ((date-begin.date()).days//7)%max(1,weeks)==0
        if 'monthly' in cls:
            cadence='monthly';return bool((1 << (date.month-1))&month_mask) and bool((1 << (date.day-1))&dom)
        if 'once' in cls or 'time' in cls:
            cadence='once';return date==begin.date()
        return False
    events=[]
    for day in date_range(start,days):
        if not matches(day): continue
        base=at(day,begin.time())
        repetition=trigger.get('Repetition') or {}
        seconds=parse_duration_iso(repetition.get('Interval'))
        duration=parse_duration_iso(repetition.get('Duration'))
        if seconds:
            window=duration if duration is not None else 86400
            current=base
            while (current-base).total_seconds() < window and current.date()==day:
                events.append(current);current+=dt.timedelta(seconds=seconds)
        else: events.append(base)
    return events,cadence, '' if cadence!='once' or 'once' in cls or 'time' in cls else f'Unsupported Windows trigger {cls}'


def load_windows(path: Path, start: dt.date, days: int) -> tuple[list[Job],list[str]]:
    root=path / 'scheduling'
    raw=read_json(root/'scheduled-tasks.json')
    if isinstance(raw,dict):raw=[raw]
    jobs=[];warnings=[]
    observed: dict[str,list[float]]={}
    for item in read_csv(root/'scheduled-task-runs.csv'):
        duration=item.get('DurationMinutes')
        if duration is None:continue
        try:
            observed.setdefault(str(item.get('TaskName','')).casefold(),[]).append(float(duration))
        except ValueError:
            continue
    for task in raw:
        if not isinstance(task,dict):continue
        config=task.get('Settings') or {}
        if config.get('Enabled') is False:continue
        name=str(task.get('TaskPath') or '\\')+str(task.get('TaskName') or '')
        for n,trigger in enumerate(task.get('Triggers') or []):
            if trigger.get('Enabled') is False:continue
            events,cadence,warning=expand_windows_trigger(trigger,start,days)
            key=f'windows:{name}'
            jobs.append(Job(key,'windows',name,cadence,f'Trigger {n+1}: {trigger.get("CimClass", "")}',lambda s,d,e=events:e,durations=observed.get(name.casefold(),[]),uncertainty=warning))
            if warning:warnings.append(f'{key}: {warning}')
    return jobs,warnings


def load_sql(path: Path, start: dt.date, days: int, server: str) -> tuple[list[Job],list[str]]:
    root=path/'instance'/'agent'
    specs=read_csv(root/'schedules.csv')
    attachments=read_csv(root/'schedule-jobs.csv')
    job_defs={r['Name']:r for r in read_csv(root/'jobs.csv') if r.get('Name')}
    history:dict[str,list[float]]={}
    for row in read_csv(root/'job-runs.csv'):
        if row.get('run_status') not in ('1','0','2','3'):continue
        name=row.get('job_name','')
        history.setdefault(name,[]).append(sql_duration_minutes(row.get('run_duration')))
    lookup={row.get('name'):row for row in specs if row.get('name')}
    result=[];warnings=[]
    for link in attachments:
        name=link.get('job_name','')
        if not name or str(job_defs.get(name,{}).get('Enabled','true')).lower() in ('false','0'):continue
        schedule=lookup.get(link.get('schedule_name'))
        if not schedule or not as_bool(schedule.get('enabled')):continue
        events,cadence,warning=expand_sql_schedule(schedule,start,days)
        key=f'sql_agent:{server}:{name}'
        result.append(Job(key,'sql_agent',name,cadence,link.get('schedule_name',''),lambda s,d,e=events:e,history.get(name,[]),warning))
        if warning:warnings.append(f'{key}: {warning}')
    return result,warnings


def load_postgres(path: Path, start: dt.date, days: int, server: str) -> tuple[list[Job],list[str]]:
    jobs=[];warnings=[]
    for db_dir in sorted((path/'databases').glob('*')):
        if not db_dir.is_dir():continue
        db=db_dir.name
        sched=db_dir/'schedulers'
        runs:dict[str,list[float]]={}
        for row in read_csv(sched/'pg-cron-runs.csv'):
            started=parse_dt(row.get('start_time'));ended=parse_dt(row.get('end_time'))
            if started and ended and ended>=started:
                runs.setdefault(row.get('jobid',''),[]).append((ended-started).total_seconds()/60)
        for row in read_csv(sched/'pg-cron-jobs.csv'):
            if row.get('active','true').lower() in ('false','0'):continue
            name=row.get('jobname') or row.get('jobid') or 'unnamed'
            key=f'pg_cron:{server}:{db}:{name}'
            events,cadence,warning=expand_cron(row.get('schedule',''),start,days)
            jobs.append(Job(key,'pg_cron',str(name),cadence,row.get('schedule',''),lambda s,d,e=events:e,runs.get(row.get('jobid',''),[]),warning))
            if warning:warnings.append(f'{key}: {warning}')
        # pgAgent schedules are documented as collected, but pgAgent bitmap schedules
        # require a separate parser. Never invent occurrences or average durations.
        for row in read_csv(sched/'pgagent'/'jobs.csv'):
            warnings.append(f"pgAgent job {row.get('jobname', row.get('jobid', '?'))} in {db}: schedule bitmap expansion not supported yet")
    return jobs,warnings


def load_crontabs(system_root: Path, start:dt.date,days:int)->tuple[list[Job],list[str]]:
    root=system_root/'scheduling'/'cron'
    jobs=[];warnings=[]
    if not root.exists():return jobs,warnings
    for file in sorted(root.rglob('*')):
        if not file.is_file():continue
        for number,line in enumerate(file.read_text(encoding='utf-8',errors='replace').splitlines(),1):
            item=line.strip()
            if not item or item.startswith('#') or '=' in item.split()[0] or item.startswith('@reboot'):continue
            parts=item.split()
            if len(parts)<6:continue
            # /etc/crontab and cron.d include 'user' between 5 cron fields and command
            expr=' '.join(parts[:5]);command=' '.join(parts[6:] if file.name=='crontab' or file.parent.name=='cron.d' else parts[5:])
            key=f'cron:{file.relative_to(root).as_posix()}:{number}'
            events,cadence,warning=expand_cron(expr,start,days)
            jobs.append(Job(key,'cron',command[:100],cadence,expr,lambda s,d,e=events:e,uncertainty=warning))
            if warning:warnings.append(f'{key}: {warning}')
    return jobs,warnings


def expand_systemd_calendar(expression:str,start:dt.date,days:int)->tuple[list[dt.datetime],str,str]:
    value=expression.strip()
    aliases={'hourly':'*-*-* *:00:00','daily':'*-*-* 00:00:00','weekly':'Mon *-*-* 00:00:00',
             'monthly':'*-*-01 00:00:00','yearly':'*-01-01 00:00:00'}
    value=aliases.get(value.lower(),value)
    # Support simple fixed wall-clock times, weekdays, and day-of-month patterns.
    match=re.fullmatch(r'(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+)?\*-(\*|\d{2})-(\*|\d{2})\s+(\d{2}|\*):(\d{2}):\d{2}',value,re.I)
    if not match:return [],'unknown',f'Unsupported systemd OnCalendar expression {expression!r}'
    weekday,month,day,hour,minute=match.groups()
    weekday_num=['mon','tue','wed','thu','fri','sat','sun'].index(weekday.lower()) if weekday else None
    cadence='monthly' if month!='*' or day!='*' else 'weekly' if weekday else 'daily'
    events=[]
    for date in date_range(start,days):
        if month!='*' and date.month!=int(month):continue
        if day!='*' and date.day!=int(day):continue
        if weekday_num is not None and date.weekday()!=weekday_num:continue
        for h in range(24) if hour=='*' else [int(hour)]:
            events.append(at(date,dt.time(h,int(minute))))
    return events,cadence,''


def load_systemd(path:Path,start:dt.date,days:int)->tuple[list[Job],list[str]]:
    jobs=[];warnings=[]
    for row in read_csv(path/'scheduling'/'systemd-timers.csv'):
        if row.get('unit_state','').lower() in ('disabled','masked'):continue
        name=row.get('unit','')
        key='systemd:'+name
        calendars=[part for part in row.get('on_calendar','').split(';') if part]
        for expr in calendars:
            events,cadence,warning=expand_systemd_calendar(expr,start,days)
            jobs.append(Job(key,'systemd',name,cadence,expr,lambda s,d,e=events:e,uncertainty=warning))
            if warning:warnings.append(f'{key}: {warning}')
        if not calendars:
            warnings.append(f"{key}: monotonic/event timer {row.get('monotonic','')}, cannot predict clock times")
        if row.get('randomized_delay') not in ('','0','0s','0min'):
            warnings.append(f"{key}: RandomizedDelaySec={row['randomized_delay']}; exact start times may differ")
    return jobs,warnings


def options_from_yaml(path:Path | None)->dict[str,Any]:
    raw=yaml.safe_load(path.read_text(encoding='utf-8')) if path else {}
    if raw is None:raw={}
    if not isinstance(raw,dict):raise ValueError('Schedule report YAML must be a mapping')
    reports=raw.get('reports') or {}
    if not isinstance(reports,dict):raise ValueError('reports must be a mapping')
    for report,settings in reports.items():
        if not isinstance(settings,dict) or not isinstance(settings.get('exclude',[]),list):
            raise ValueError(f'reports.{report}.exclude must be a list')
    return raw


def render_dashboard(output: Path, start: dt.date, timeline: list[dict[str, Any]],
                     jobs: list[dict[str, Any]], overlaps: list[dict[str, Any]], warnings: list[str]) -> None:
    """Offline HTML report: no JavaScript or remote assets."""
    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ''), quote=True)
    def table(rows, columns, limit=200):
        heads = ''.join(f'<th>{esc(col)}</th>' for col in columns)
        body = ''.join('<tr>' + ''.join(f'<td>{esc(row.get(col, ""))}</td>' for col in columns) + '</tr>' for row in rows[:limit])
        return f'<table><thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table>'
    first_end = dt.datetime.combine(start, dt.time.min) + dt.timedelta(days=1)
    today = [row for row in timeline if parse_dt(row['start']) < first_end]
    files = ['jobs', 'timeline', 'today', 'daily', 'weekly', 'monthly', 'overlaps', 'exclusions', 'warnings', 'starts-by-hour']
    links = ' '.join(f'<a href="{name}.csv">{name}.csv</a>' for name in files)
    doc = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ConfigBackup schedules</title>
<style>body{font:14px/1.5 system-ui,sans-serif;max-width:1400px;margin:25px auto;padding:0 20px;color:#18263c}
section{margin:25px 0}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;border-bottom:1px solid #ddd;padding:7px;vertical-align:top}
th{background:#eef2f6;position:sticky;top:0}tr:nth-child(even){background:#f8fafc}a{margin-right:12px}
.caution{padding:12px;border-left:4px solid #af6909;background:#fff7eb}</style></head><body>"""
    doc += f'<h1>Schedule analysis</h1><p>Starting {start.isoformat()} | {len(jobs)} schedule definitions | {len(timeline)} projected occurrences | {len(overlaps)} estimated overlaps</p>'
    doc += '<p class="caution"><strong>Predictions, not observations.</strong> Durations use observed median runtimes or explicit overrides. Unknown durations are excluded from overlap detection. Times use the report host local timezone; DST, jitter, catch-up and contention may change actual starts.</p>'
    doc += '<p>' + links + '</p>'
    doc += '<section><h2>First day</h2>' + table(today,['start','job_id','duration_minutes','duration_basis']) + '</section>'
    doc += '<section><h2>Estimated overlaps</h2>' + table(overlaps,['first_start','first_job_id','second_job_id','overlap_minutes']) + '</section>'
    doc += '<section><h2>Jobs and durations</h2>' + table(jobs,['job_id','cadence','schedule','runs_in_horizon','measured_runs','median_minutes','p95_minutes','duration_source','excluded_from_load']) + '</section>'
    if warnings:
        doc += '<section><h2>Unresolved schedules</h2><ul>' + ''.join(f'<li>{esc(w)}</li>' for w in warnings[:100]) + '</ul></section>'
    doc += '<p>The downloadable CSVs contain all occurrences and exclusions. No schedulers have been changed.</p></body></html>\n'
    (output/'dashboard.html').write_text(doc, encoding='utf-8')


def report(args: argparse.Namespace) -> dict[str,int]:
    cfg=options_from_yaml(Path(args.config) if args.config else None)
    start=dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    days=int(args.days)
    if days<1 or days>366:raise ValueError('--days must be between 1 and 366')
    output=Path(args.output)
    all_jobs:list[Job]=[];warnings:list[str]=[]
    if args.system:
        path=Path(args.system)
        js,ws=load_windows(path,start,days);all_jobs+=js;warnings+=ws
        js,ws=load_crontabs(path,start,days);all_jobs+=js;warnings+=ws
        js,ws=load_systemd(path,start,days);all_jobs+=js;warnings+=ws
    if args.sql:
        js,ws=load_sql(Path(args.sql),start,days,args.sql_host);all_jobs+=js;warnings+=ws
    if args.postgresql:
        js,ws=load_postgres(Path(args.postgresql),start,days,args.pg_host);all_jobs+=js;warnings+=ws
    settings=cfg.get('analysis') or {}
    exclude_load=settings.get('exclude_from_load') or []
    watchdogs=settings.get('watchdogs') or []
    duration_overrides=settings.get('duration_overrides_minutes') or {}
    for job in all_jobs:
        for pattern,value in duration_overrides.items():
            if match_id(job.key,[pattern]):job.override_duration=float(value)
    columns=['start','end_estimated','duration_minutes','duration_basis','job_id','source','job','schedule','cadence','watchdog']
    timeline=[]
    distinct={}
    for job in all_jobs:
        distinct[job.key]=job
        duration=job.duration()
        for instant in job.expand(start,days):
            timeline.append({'start':instant.isoformat(timespec='seconds'),
                'end_estimated':(instant+dt.timedelta(minutes=duration)).isoformat(timespec='seconds') if duration is not None else '',
                'duration_minutes':duration if duration is not None else '', 'duration_basis':job.duration_source(),
                'job_id':job.key,'source':job.source,'job':job.name,'schedule':job.schedule,'cadence':job.cadence,'watchdog': 'true' if match_id(job.key,watchdogs) else 'false'})
    timeline.sort(key=lambda r:(r['start'],r['job_id']))
    report_config=cfg.get('reports') or {}
    def excludes(section:str)->list[str]:
        return (report_config.get(section) or {}).get('exclude') or []
    def filtered(section:str,cadences:tuple[str,...]|None=None):
        return [r for r in timeline if (cadences is None or r['cadence'] in cadences) and not match_id(r['job_id'],excludes(section))]
    write_csv(output/'timeline.csv',filtered('timeline'),columns)
    # Frequency-oriented files: daily, weekly and monthly jobs (not merely
    # chronological windows). A separate today.csv includes all job cadences.
    start_dt=dt.datetime.combine(start,dt.time.min)
    d1=start_dt+dt.timedelta(days=1)
    write_csv(output/'today.csv',[r for r in filtered('today') if parse_dt(r['start'])<d1],columns)
    write_csv(output/'daily.csv',[r for r in filtered('daily',('daily',)) if parse_dt(r['start'])<d1],columns)
    write_csv(output/'weekly.csv',filtered('weekly',('weekly',)),columns)
    write_csv(output/'monthly.csv',filtered('monthly',('monthly',)),columns)
    write_csv(output/'daily-recurring.csv',filtered('daily-recurring',('daily',)),columns)
    jobs_cols=['job_id','source','job','cadence','schedule','runs_in_horizon','measured_runs','median_minutes','p95_minutes','duration_source','watchdog','excluded_from_load','note']
    jobs_rows=[]
    for job in sorted(all_jobs,key=lambda j:j.key):
        median,p95=median_p95(job.durations)
        jobs_rows.append({'job_id':job.key,'source':job.source,'job':job.name,'cadence':job.cadence,'schedule':job.schedule,
            'runs_in_horizon':len(job.expand(start,days)), 'measured_runs':len(job.durations),
            'median_minutes':median if median is not None else '', 'p95_minutes':p95 if p95 is not None else '',
            'duration_source':job.duration_source(), 'watchdog': match_id(job.key,watchdogs),
            'excluded_from_load':match_id(job.key,exclude_load) or match_id(job.key,watchdogs),'note':job.uncertainty})
    write_csv(output/'jobs.csv',jobs_rows,jobs_cols)
    # Two-pointer overlap sweep. An unknown duration is never fabricated.
    overlap_cols=['first_start','first_end','first_job_id','second_start','second_end','second_job_id','overlap_minutes']
    overlaps=[];open_events=[]
    for row in filtered('overlaps'):
        if match_id(row['job_id'], exclude_load) or match_id(row['job_id'], watchdogs):continue
        if not row['end_estimated']:continue
        begins=parse_dt(row['start']);ends=parse_dt(row['end_estimated'])
        open_events=[x for x in open_events if x[1]>begins]
        for earlier,earlier_end in open_events:
            if earlier['job_id'] == row['job_id']:continue
            overlaps.append({'first_start':earlier['start'],'first_end':earlier['end_estimated'],'first_job_id':earlier['job_id'],
                'second_start':row['start'],'second_end':row['end_estimated'],'second_job_id':row['job_id'],
                'overlap_minutes':round((min(earlier_end,ends)-begins).total_seconds()/60,2)})
        open_events.append((row,ends))
    write_csv(output/'overlaps.csv',overlaps,overlap_cols)
    excluded=[]
    for section in ['daily','weekly','monthly','today','timeline','overlaps','daily-recurring']:
        for job in distinct.values():
            if match_id(job.key,excludes(section)):
                excluded.append({'report':section,'job_id':job.key,'reason':'report-specific exclusion'})
    for job in distinct.values():
        if match_id(job.key,exclude_load) or match_id(job.key,watchdogs):
            excluded.append({'report':'load-analysis','job_id':job.key,'reason':'watchdog/singleton' if match_id(job.key,watchdogs) else 'excluded from load'})
    write_csv(output/'exclusions.csv',sorted(excluded,key=lambda e:(e['report'],e['job_id'])),['report','job_id','reason'])
    write_csv(output/'warnings.csv',[{'message':w} for w in sorted(set(warnings))],['message'])
    # Hourly aggregate counts based on projected starts and median duration; unknown
    # duration remains unknown, not assigned a speculative fallback.
    buckets={}
    for row in filtered('timeline'):
        if match_id(row['job_id'], exclude_load) or match_id(row['job_id'], watchdogs):continue
        begin=parse_dt(row['start']);key=begin.strftime('%Y-%m-%d %H:00')
        buckets.setdefault(key,{'hour':key,'starts':0,'known_duration_starts':0,'unknown_duration_starts':0})
        buckets[key]['starts']+=1
        if row['end_estimated']:buckets[key]['known_duration_starts']+=1
        else:buckets[key]['unknown_duration_starts']+=1
    write_csv(output/'starts-by-hour.csv',list(dict(sorted(buckets.items())).values()),['hour','starts','known_duration_starts','unknown_duration_starts'])
    summary={'jobs':len(all_jobs),'predicted_occurrences':len(timeline),'overlaps':len(overlaps),'warnings':len(warnings),
             'known_duration_jobs':sum(1 for j in all_jobs if j.duration() is not None),'start':start.isoformat(),'days':days}
    (output/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    render_dashboard(output, start, filtered('timeline'), jobs_rows, overlaps, warnings)
    return summary


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,help='Output report directory')
    parser.add_argument('--system',help='Current system collector snapshot directory')
    parser.add_argument('--sql',help='Current SQL collector snapshot directory')
    parser.add_argument('--sql-host',default='sqlserver',help='SQL host ID for unique job identifiers')
    parser.add_argument('--postgresql',help='Current PostgreSQL collector snapshot directory')
    parser.add_argument('--pg-host',default='postgresql',help='PostgreSQL host ID')
    parser.add_argument('--config',help='Optional YAML report exclusions/duration overrides')
    parser.add_argument('--start',help='Calendar start date YYYY-MM-DD, default local today')
    parser.add_argument('--days',type=int,default=35,help='Analysis horizon in days, 1 to 366')
    parser.add_argument('--version',action='version',version=VERSION)
    args=parser.parse_args(argv)
    try:
        result=report(args)
        print('[schedule-analyzer] '+json.dumps(result,sort_keys=True))
        return 0
    except (ValueError,TypeError,KeyError,OSError,yaml.YAMLError) as exc:
        notice(f'ERROR: {exc}')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
