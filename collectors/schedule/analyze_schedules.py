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
import contextlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

VERSION = '2.1.0'
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from completeness import Coverage, MANIFEST
from schedule_insights import analyze as analyze_insights, success as execution_success
REPORT_SETTINGS = {}

from zoneinfo import ZoneInfo
SOURCE_ZONE = ZoneInfo('UTC')


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
    if (REPORT_SETTINGS.get(path.stem) or {}).get('enabled', True) is False: return
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
        return parsed.astimezone(SOURCE_ZONE).replace(tzinfo=None) if parsed.tzinfo else parsed
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
    host: str = ''
    timezone: str = 'UTC'
    observed: list = field(default_factory=list)
    heartbeats: list = field(default_factory=list)

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
    begin = parse_dt(trigger.get('StartBoundary'))
    cls = str(trigger.get('CimClass') or '').lower()
    if not begin:
        return [], 'event', 'Event trigger or missing StartBoundary; no predictable clock start'
    end = parse_dt(trigger.get('EndBoundary'))
    lower = at(start, dt.time.min); upper = lower + dt.timedelta(days=days)
    repetition = trigger.get('Repetition') or {}
    seconds = parse_duration_iso(repetition.get('Interval'))
    duration = parse_duration_iso(repetition.get('Duration'))
    cadence = next((name for name in ('daily','weekly','monthly') if name in cls), 'once')
    if 'monthlydow' in cls:
        return [], 'monthly', 'Monthly weekday trigger requires explicit XML calendar conversion'
    if not any(name in cls for name in ('daily','weekly','monthly','once','time')):
        return [], 'event', 'Event trigger cannot be expanded from StartBoundary'
    def matches(day):
        if day < begin.date(): return False
        if cadence == 'daily':
            return (day-begin.date()).days % max(1,as_int(trigger.get('DaysInterval'),1)) == 0
        if cadence == 'weekly':
            anchor = begin.date()-dt.timedelta(days=(begin.weekday()+1)%7)
            week = day-dt.timedelta(days=(day.weekday()+1)%7)
            return bool((1 << ((day.weekday()+1)%7)) & as_int(trigger.get('DaysOfWeek'))) and ((week-anchor).days//7)%max(1,as_int(trigger.get('WeeksInterval'),1)) == 0
        if cadence == 'monthly':
            return bool((1 << (day.month-1)) & as_int(trigger.get('MonthsOfYear'))) and bool((1 << (day.day-1)) & as_int(trigger.get('DaysOfMonth')))
        return day == begin.date()
    if cadence == 'once':
        bases = [begin]
    else:
        # Include preceding triggers whose repetition can spill into the horizon.
        lookback = int(math.ceil((duration or 86400)/86400)) if seconds else 0
        origin = max(begin.date(), start-dt.timedelta(days=lookback))
        if seconds and duration is None:
            # Indefinite calendar repetition needs all earlier bases. Refuse huge
            # histories rather than silently truncating continuing repetitions.
            origin = begin.date()
        if (upper.date()-origin).days > 20000:
            return [], cadence, 'Repetition anchor range exceeds safety limit'
        bases = [at(day, begin.time()) for day in date_range(origin,(upper.date()-origin).days) if matches(day)]
    events = set()
    for base in bases:
        if not seconds:
            if lower <= base < upper and (end is None or base < end): events.add(base)
            continue
        stop = min(upper, base+dt.timedelta(seconds=duration)) if duration is not None else upper
        if end is not None: stop = min(stop,end)
        index = max(0, math.ceil((lower-base).total_seconds()/seconds))
        current = base+dt.timedelta(seconds=index*seconds)
        while current < stop:
            events.add(current)
            if len(events)>300000: return [], cadence, 'Expansion exceeds 300,000 occurrences'
            current += dt.timedelta(seconds=seconds)
    if cadence == 'once' and seconds and duration is None: cadence='daily'
    return sorted(events), cadence, ''


def pg_bitmap(value, count):
    if isinstance(value, str):
        value = value.strip()
        if value.startswith('['): value = json.loads(value.replace('True','true').replace('False','false'))
        else: value = value.strip('{}').split(',') if ',' in value else value.split(';')
    if not isinstance(value,list) or len(value)!=count:
        raise ValueError('Invalid pgAgent bitmap length')
    flags = [str(x).strip().lower() in ('t','true','1') for x in value]
    return flags if any(flags) else [True]*count


def expand_pgagent(spec, exceptions, start, days):
    if not as_bool(spec.get('jscenabled','true')): return [], 'daily', ''
    try:
        minute,hour,weekday,monthday,month = [pg_bitmap(spec.get(key,''),n) for key,n in
            [('jscminutes',60),('jschours',24),('jscweekdays',7),('jscmonthdays',32),('jscmonths',12)]]
    except (ValueError,TypeError) as exc: return [],'unknown',str(exc)
    begin=parse_dt(spec.get('jscstart'));end=parse_dt(spec.get('jscend'))
    if begin is None: return [],'unknown','Missing pgAgent start boundary'
    cadence='monthly' if not all(monthday) or not all(month) else 'weekly' if not all(weekday) else 'daily'
    events=[]
    for day in date_range(start,days):
        if not month[day.month-1] or not weekday[(day.weekday()+1)%7]:continue
        if not (monthday[day.day-1] or monthday[31] and day.day==calendar.monthrange(day.year,day.month)[1]):continue
        for h in range(24):
            if not hour[h]:continue
            for m in range(60):
                if not minute[m]:continue
                instant=at(day,dt.time(h,m))
                if instant<begin or end and instant>end:continue
                if any((not ex.get('jexdate') or ex['jexdate']==day.isoformat()) and
                       (not ex.get('jextime') or ex['jextime']==instant.time().isoformat()) for ex in exceptions):continue
                events.append(instant)
                if len(events)>300000:return [],cadence,'Expansion exceeds 300,000 occurrences'
    return events,cadence,''


def load_windows(path: Path, start: dt.date, days: int) -> tuple[list[Job],list[str]]:
    root=path / 'scheduling'
    raw=read_json(root/'scheduled-tasks.json')
    if isinstance(raw,dict):raw=[raw]
    jobs=[];warnings=[]
    observed: dict[str,list[float]]={}
    for item in (read_csv(path/'telemetry'/'scheduled-task-runs.csv') or read_csv(root/'scheduled-task-runs.csv')):
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
    for row in (read_csv(path/'telemetry'/'agent'/'job-runs.csv') or read_csv(root/'job-runs.csv')):
        if row.get('step_id','0') != '0':continue
        if row.get('run_status') != '1':continue
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
        for row in (read_csv(path/'telemetry'/db/'pg-cron-runs.csv') or read_csv(sched/'pg-cron-runs.csv')):
            started=parse_dt(row.get('start_time'));ended=parse_dt(row.get('end_time'))
            if started and ended and ended>=started and execution_success('pg_cron',row.get('status')):
                runs.setdefault(row.get('jobid',''),[]).append((ended-started).total_seconds()/60)
        for row in read_csv(sched/'pg-cron-jobs.csv'):
            if row.get('active','true').lower() in ('false','0'):continue
            name=row.get('jobname') or row.get('jobid') or 'unnamed'
            key=f'pg_cron:{server}:{db}:{name}'
            events,cadence,warning=expand_cron(row.get('schedule',''),start,days)
            jobs.append(Job(key,'pg_cron',str(name),cadence,row.get('schedule',''),lambda s,d,e=events:e,runs.get(row.get('jobid',''),[]),warning))
            if warning:warnings.append(f'{key}: {warning}')
        definitions={r['jobid']:r for r in read_csv(sched/'pgagent'/'jobs.csv')}
        exceptions=read_csv(sched/'pgagent'/'exceptions.csv')
        for spec in read_csv(sched/'pgagent'/'schedules.csv'):
            job=definitions.get(spec.get('jscjobid'),{})
            if not as_bool(job.get('jobenabled','true')):continue
            selected=[e for e in exceptions if e.get('jexscid')==spec.get('jscid')]
            events,cadence,warning=expand_pgagent(spec,selected,start,days)
            key=f"pgagent:{server}:{db}:{job.get('jobid',spec.get('jscjobid'))}"
            durations=[float(r['duration_seconds'])/60 for r in read_csv(path/'telemetry'/db/'pgagent-runs.csv')
                       if r.get('jlgjobid')==spec.get('jscjobid') and r.get('duration_seconds') and execution_success('pgagent',r.get('jlgstatus'))]
            jobs.append(Job(key,'pgagent',job.get('jobname',key),cadence,spec.get('jscname',''),lambda s,d,e=events:e,durations,warning))
            if warning:warnings.append(key+': '+warning)
    return jobs,warnings


def load_crontabs(system_root: Path, start:dt.date,days:int)->tuple[list[Job],list[str]]:
    root=system_root/'scheduling'/'cron'
    jobs=[];warnings=[]
    if not root.exists():return jobs,warnings
    for file in sorted(root.rglob('*')):
        if not file.is_file():continue
        if file.name=='anacrontab':
            warnings.append('anacron: boot/catch-up and delay dependent; no fixed predicted times')
            continue
        for number,line in enumerate(file.read_text(encoding='utf-8',errors='replace').splitlines(),1):
            item=line.strip()
            if not item or item.startswith('#') or ('=' in item.split()[0] and not item.startswith(('CRON_TZ=','TZ='))) :continue
            if item.startswith('@reboot'):
                warnings.append(f'{file}:{number}: @reboot depends on boot time');continue
            if item.startswith('CRON_TZ=') or item.startswith('TZ='):
                warnings.append(f'{file}: per-file timezone requires a separate named source; remaining entries skipped')
                break
            aliases={'@hourly':'0 * * * *','@daily':'0 0 * * *','@weekly':'0 0 * * 0','@monthly':'0 0 1 * *','@yearly':'0 0 1 1 *','@annually':'0 0 1 1 *','@midnight':'0 0 * * *'}
            for alias, expression in aliases.items():
                if item.startswith(alias+' '):item=expression+item[len(alias):];break
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


def load_launchd(path:Path,start:dt.date,days:int)->tuple[list[Job],list[str]]:
    jobs=[];warnings=[]
    for file in sorted((path/'scheduling/launchd').glob('*.json')):
        data=json.loads(file.read_text());definition=data['definition'];name=definition.get('Label',file.stem)
        if definition.get('Disabled') is True:continue
        schedules=definition.get('StartCalendarInterval',[])
        if isinstance(schedules,dict):schedules=[schedules]
        if definition.get('StartInterval') or definition.get('RunAtLoad') or definition.get('KeepAlive'):
            warnings.append(f'launchd:{name}: interval/startup/KeepAlive starts have no reliable wall-clock anchor')
        if not schedules:
            continue
        events=set()
        for rule in schedules:
            if not isinstance(rule,dict) or set(rule)-{'Minute','Hour','Day','Weekday','Month'} or ('Day' in rule and 'Weekday' in rule):
                warnings.append(f'launchd:{name}: unsupported calendar rule {rule}');continue
            bounds={'Minute':(0,59),'Hour':(0,23),'Day':(1,31),'Weekday':(0,7),'Month':(1,12)}
            if any(not isinstance(v,int) or not bounds[k][0]<=v<=bounds[k][1] for k,v in rule.items()):
                warnings.append(f'launchd:{name}: invalid calendar rule');continue
            for day in date_range(start,days):
                if 'Month' in rule and day.month!=rule['Month']:continue
                if 'Day' in rule and day.day!=rule['Day']:continue
                if 'Weekday' in rule and (day.weekday()+1)%7!=rule['Weekday']%7:continue
                for hour in ([rule['Hour']] if 'Hour' in rule else range(24)):
                    for minute in ([rule['Minute']] if 'Minute' in rule else range(60)):
                        events.add(dt.datetime.combine(day,dt.time(hour,minute)))
        key='launchd:'+str(data['path'])
        cadence='monthly' if any('Month' in r or 'Day' in r for r in schedules if isinstance(r,dict)) else 'weekly' if any('Weekday' in r for r in schedules if isinstance(r,dict)) else 'daily'
        jobs.append(Job(key,'launchd',name,cadence,json.dumps(schedules,sort_keys=True),lambda s,d,e=sorted(events):e,uncertainty='Sleep/wake coalescing and launchd overrides are not projected'))
    return jobs,warnings


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
    links = ' '.join(f'<a href="{name}.csv">{name}.csv</a>' for name in files if (REPORT_SETTINGS.get(name) or {}).get('enabled',True))
    doc = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>ConfigBackup schedules</title>
<style>body{font:14px/1.5 system-ui,sans-serif;max-width:1400px;margin:25px auto;padding:0 20px;color:#18263c}
section{margin:25px 0}table{border-collapse:collapse;width:100%;font-size:13px}td,th{text-align:left;border-bottom:1px solid #ddd;padding:7px;vertical-align:top}
th{background:#eef2f6;position:sticky;top:0}tr:nth-child(even){background:#f8fafc}a{margin-right:12px}
.caution{padding:12px;border-left:4px solid #af6909;background:#fff7eb}</style></head><body>"""
    doc += f'<h1>Schedule analysis</h1><p>Starting {start.isoformat()} | {len(jobs)} schedule definitions | {len(timeline)} projected occurrences | {len(overlaps)} estimated overlaps</p>'
    doc += '<p class="caution"><strong>Predictions, not observations.</strong> Durations use observed median runtimes or explicit overrides. Unknown durations are excluded from overlap detection. Timeline times are UTC with host-local columns. Ambiguous and nonexistent DST times are omitted with warnings; jitter, catch-up and contention can change starts.</p>'
    unknown=sum(1 for j in jobs if j.get('duration_source')=='unknown')
    doc += f'<p class="caution">Duration coverage: {len(jobs)-unknown}/{len(jobs)} jobs have estimates. {unknown} unknown-duration jobs are excluded from predicted load.</p>'
    links += ' ' + ' '.join(f'<a href="{name}.csv">{name}.csv</a>' for name in ['concurrency','observed-concurrency','overlaps-p95','executions','slack','coverage'])
    doc += '<p>' + links + '</p>'
    doc += '<section><h2>First day</h2>' + table(today,['start','job_id','duration_minutes','duration_basis']) + '</section>'
    doc += '<section><h2>Estimated overlaps</h2>' + table(overlaps,['first_start','first_job_id','second_job_id','overlap_minutes']) + '</section>'
    doc += '<section><h2>Jobs and durations</h2>' + table(jobs,['job_id','cadence','schedule','runs_in_horizon','measured_runs','median_minutes','p90_minutes','p95_minutes','min_minutes','max_minutes','average_minutes','duration_source','excluded_from_load']) + '</section>'
    if warnings:
        doc += '<section><h2>Unresolved schedules</h2><ul>' + ''.join(f'<li>{esc(w)}</li>' for w in warnings[:100]) + '</ul></section>'
    doc += '<p>The downloadable CSVs contain all occurrences and exclusions. No schedulers have been changed.</p></body></html>\n'
    (output/'dashboard.html').write_text(doc, encoding='utf-8')


def wall_to_utc(local, zone, warnings, key):
    if local.tzinfo:
        return local.astimezone(dt.timezone.utc).replace(tzinfo=None)
    tz=ZoneInfo(zone)
    first=local.replace(tzinfo=tz,fold=0);second=local.replace(tzinfo=tz,fold=1)
    if first.utcoffset()!=second.utcoffset() or first.astimezone(dt.timezone.utc).astimezone(tz).replace(tzinfo=None)!=local:
        warning=f'{key}: ambiguous/nonexistent local time {local.isoformat()} in {zone}; occurrence omitted'
        if warning not in warnings:warnings.append(warning)
        return None
    return first.astimezone(dt.timezone.utc).replace(tzinfo=None)


def observation_time(value, zone):
    if not value:return None
    try:
        parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return wall_to_utc(parsed,zone,[], 'observed')
    except ValueError:return None


def load_observations(path,job):
    rows=[]
    if job.source=='windows':
        for r in (read_csv(path/'telemetry'/'scheduled-task-runs.csv') or read_csv(path/'scheduling'/'scheduled-task-runs.csv')):
            if r.get('TaskName','').casefold()!=job.name.casefold():continue
            rows.append({'start':r.get('StartTime'),'end':r.get('EndTime'),'status':r.get('Status','completed')})
    elif job.source=='sql_agent':
        for r in (read_csv(path/'telemetry'/'agent'/'job-runs.csv') or read_csv(path/'instance'/'agent'/'job-runs.csv')):
            if r.get('job_name')!=job.name or r.get('step_id','0')!='0':continue
            date=sql_date(r.get('run_date'))
            if date:
                rows.append({'start':at(date,sql_clock(r.get('run_time'))).isoformat(),
                             'minutes':sql_duration_minutes(r.get('run_duration')), 'status':r.get('run_status','')})
        for r in read_csv(path/'telemetry'/'agent'/'running-jobs.csv'):
            if r.get('job_name')==job.name:rows.append({'start':r.get('start_time') or r.get('start'),'status':'running'})
    elif job.source in ('pg_cron','pgagent'):
        for db in (path/'databases').glob('*'):
            if f':{db.name}:' not in job.key:continue
            if job.source=='pg_cron':
                jobs=read_csv(db/'schedulers'/'pg-cron-jobs.csv')
                ids={r.get('jobid') for r in jobs if (r.get('jobname') or r.get('jobid'))==job.name}
                for r in (read_csv(path/'telemetry'/db.name/'pg-cron-runs.csv') or read_csv(db/'schedulers'/'pg-cron-runs.csv')):
                    if r.get('jobid') in ids:rows.append({'start':r.get('start_time'),'end':r.get('end_time'),'status':r.get('status','')})
            else:
                for r in read_csv(path/'telemetry'/db.name/'pgagent-runs.csv'):
                    if job.key.endswith(':'+r.get('jlgjobid','')) and r.get('duration_seconds'):
                        rows.append({'start':r.get('jlgstart'),'minutes':float(r['duration_seconds'])/60,'status':r.get('jlgstatus','')})
    # Optional instrumented execution facts. No attribution from host counters.
    for r in read_csv(path/'telemetry'/'executions.csv'):
        if r.get('job_id')==job.key:rows.append(r)
    result=[]
    for r in rows:
        begin=observation_time(r.get('start'),job.timezone)
        end=observation_time(r.get('end'),job.timezone)
        if begin and 'minutes' in r:end=begin+dt.timedelta(minutes=float(r['minutes']))
        if begin and not end and str(r.get('status','')).casefold() in ('running','r','4'):
            result.append({**r,'start':begin.isoformat(),'end':'','job_id':job.key,'host':job.host,'duration_minutes':None})
        if begin and end and end>=begin:
            result.append({**r,'start':begin.isoformat(),'end':end.isoformat(),'job_id':job.key,'host':job.host,
                           'duration_minutes':(end-begin).total_seconds()/60})
    return result


def concurrency(events, basis):
    boundaries={}
    for row in events:
        begin=parse_dt(row['start']);end=parse_dt(row['end'])
        if end<=begin:continue
        host=row['host']
        for group in (host,'ALL_HOSTS'):
            for when,delta in ((begin,1),(end,-1)):
                key=(group,when)
                value=boundaries.setdefault(key, [0,0.0,0.0,0])
                value[0]+=delta
                if row.get('cpu_cores') is not None:value[1]+=delta*float(row['cpu_cores']);value[3]+=delta
                if row.get('peak_memory_mb') is not None:value[2]+=delta*float(row['peak_memory_mb'])
    result=[];current={};previous={}
    for (host,instant),change in sorted(boundaries.items()):
        values=current.setdefault(host,[0,0.0,0.0,0])
        if host in previous and values[0]>0 and instant>previous[host]:
            result.append({'host':host,'basis':basis,'start':previous[host].isoformat(),'end':instant.isoformat(),
                           'concurrent_jobs':values[0], 'measured_cpu_jobs':values[3],
                           'cpu_cores':round(values[1],4) if values[3] else '',
                           'peak_memory_mb_sum':round(values[2],4) if values[2] else ''})
        for i in range(4):values[i]+=change[i]
        previous[host]=instant
    return result


def workload_reports(output,timeline,jobs,settings,reports,warnings):
    exclude=(settings.get('exclude_from_load') or [])+(settings.get('watchdogs') or [])
    def allowed(key,report):return not match_id(key,exclude+((reports.get(report) or {}).get('exclude') or []))
    observed={}
    for job in jobs.values():
        for row in job.observed:
            observed[(row['job_id'],row['start'],row['end'])]=row
    facts=sorted(observed.values(),key=lambda r:(r['start'],r['job_id']))
    write_csv(output/'executions.csv',[r for r in facts if not match_id(r['job_id'],((reports.get('executions') or {}).get('exclude') or []))],
              ['host','job_id','start','end','duration_minutes','status','scheduled_start','cpu_seconds','logical_reads','physical_reads','peak_memory_mb'])
    observed_load=[]
    for row in facts:
        if not row.get('end') or not allowed(row['job_id'],'observed-concurrency'):continue
        row=dict(row)
        duration=row['duration_minutes']*60
        row['cpu_cores']=float(row['cpu_seconds'])/duration if row.get('cpu_seconds') and duration else None
        row['peak_memory_mb']=float(row['peak_memory_mb']) if row.get('peak_memory_mb') else None
        observed_load.append(row)
    concurrency_columns=['host','basis','start','end','concurrent_jobs','measured_cpu_jobs','cpu_cores','peak_memory_mb_sum']
    write_csv(output/'observed-concurrency.csv',concurrency(observed_load,'observed'),concurrency_columns)
    predicted=[];overlaps=[];slack=[]
    for basis in ('median','p95'):
        events=[]
        for row in timeline:
            if not allowed(row['job_id'],'__load__'):continue
            job=jobs[row['job_id']]
            duration=job.duration() if basis=='median' else (job.override_duration if job.override_duration is not None else median_p95(job.durations)[1])
            if duration is None:continue
            end=parse_dt(row['start'])+dt.timedelta(minutes=duration)
            events.append({**row,'end':end.isoformat()})
        predicted+=concurrency([r for r in events if allowed(r['job_id'],'concurrency')],basis)
        active=[]
        for row in events:
            start=parse_dt(row['start']);end=parse_dt(row['end'])
            active=[r for r in active if parse_dt(r['end'])>start]
            if allowed(row['job_id'],'overlaps'):
                for earlier in active:
                    if not allowed(earlier['job_id'],'overlaps'):continue
                    overlaps.append({'basis':basis,'first_job_id':earlier['job_id'],'second_job_id':row['job_id'],
                                     'same_host':earlier['host']==row['host'],'start':row['start'],
                                     'overlap_minutes':(min(parse_dt(earlier['end']),end)-start).total_seconds()/60})
            active.append(row)
    for key,job in jobs.items():
        starts=sorted(parse_dt(r['start']) for r in timeline if r['job_id']==key)
        p95=median_p95(job.durations)[1]
        if p95 is None:continue
        # Watchdogs remain in slack: a single-instance guard does not guarantee timely completion.
        if match_id(key,((reports.get('slack') or {}).get('exclude') or [])):continue
        for first,second in zip(starts,starts[1:]):
            gap=(second-first).total_seconds()/60
            slack.append({'job_id':key,'start':first.isoformat(),'next_start':second.isoformat(),'p95_minutes':p95,'slack_minutes':gap-p95,'overrun':p95>gap})
    write_csv(output/'concurrency.csv',predicted,concurrency_columns)
    write_csv(output/'overlaps-p95.csv',overlaps,['basis','first_job_id','second_job_id','same_host','start','overlap_minutes'])
    write_csv(output/'slack.csv',slack,['job_id','start','next_start','p95_minutes','slack_minutes','overrun'])
    write_csv(output/'coverage.csv',[{'host':host,'jobs':sum(j.host==host for j in jobs.values()),
        'unknown_duration_jobs':sum(j.host==host and j.duration() is None for j in jobs.values()),
        'observed_executions':sum(r['host']==host for r in facts)} for host in sorted({j.host for j in jobs.values()})],
        ['host','jobs','unknown_duration_jobs','observed_executions'])
    write_csv(output/'warnings.csv',[{'message':w} for w in sorted(set(warnings))],['message'])


@contextlib.contextmanager
def certified_source(path, source, warnings):
    if not (path / MANIFEST).is_file():
        if source.get('require_manifest',False):
            raise ValueError('Source requires a collection manifest: ' + str(path))
        warnings.append(str(path) + ': legacy source has no completeness manifest; certification/freshness unknown')
        yield path
        return
    with tempfile.TemporaryDirectory(prefix='configbackup-report-') as folder:
        coverage=Coverage(path,settings=source.get("sections"))
        root=coverage.seal(folder)
        for entry in coverage.sections:
            if entry['status']!='complete':warnings.append(str(path)+': section '+entry['path']+' '+entry['status']+'; omitted from report')
        if coverage.collected_at:
            warnings.append(str(path)+': certified snapshot collected at '+coverage.collected_at)
        else: warnings.append(str(path)+': collection timestamp unavailable')
        if (path/'telemetry').is_dir():
            # Telemetry is deliberately independent of configuration certification.
            for item in sorted((path/'telemetry').rglob('*')):
                if item.is_symlink(): continue
                if item.is_file():
                    target=root/item.relative_to(path);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(item,target)
        yield root


def report(args: argparse.Namespace) -> dict[str,int]:
    cfg=options_from_yaml(Path(args.config) if args.config else None)
    global REPORT_SETTINGS
    REPORT_SETTINGS=cfg.get('reports') or {}
    start=dt.date.fromisoformat(args.start) if args.start else dt.date.today()
    days=int(args.days)
    if days<1 or days>366:raise ValueError('--days must be between 1 and 366')
    output=Path(args.output)
    all_jobs:list[Job]=[];warnings:list[str]=[]
    global SOURCE_ZONE
    sources=cfg.get('sources') or []
    if args.system:sources.append({'type':'system','path':args.system,'host':'system','timezone':'UTC'})
    if args.sql:sources.append({'type':'sql','path':args.sql,'host':args.sql_host,'timezone':'UTC'})
    if args.postgresql:sources.append({'type':'postgresql','path':args.postgresql,'host':args.pg_host,'timezone':'UTC'})
    identities=set()
    for source in sources:
        if source.get('enabled',True) is False:
            warnings.append('Disabled source: '+str(source.get('host',source.get('path'))));continue
        kind=source['type'];host=source['host'];zone=source['timezone'];path=Path(source['path'])
        source_id=host + ('\\' + str(source['instance']) if source.get('instance') else '')
        if (kind,source_id) in identities:raise ValueError('Duplicate source type/host/instance')
        identities.add((kind,source_id));SOURCE_ZONE=ZoneInfo(zone)
        loaders={'system':[load_windows,load_crontabs,load_systemd,load_launchd], 'sql':[load_sql], 'postgresql':[load_postgres]}
        if kind not in loaders:raise ValueError('Unknown source type '+kind)
        with certified_source(path,source,warnings) as certified:
            for loader in loaders[kind]:
                js,ws=loader(certified,start-dt.timedelta(days=1),days+2,*([source_id] if kind!='system' else []))
                warnings+=ws
                for job in js:
                    job.host=host;job.timezone=zone
                    if kind=='system':job.key=job.key.replace(':',':'+host+':',1)
                    # Legacy system IDs remain available when using the old CLI.
                    if kind=='system' and args.system and host=='system':job.key=job.key.replace(':system:',':',1)
                    job.observed=load_observations(certified,job)
                    job.heartbeats=[r for r in read_csv(certified/'telemetry'/'heartbeats.csv') if r.get('job_id')==job.key]
                    if job.observed:
                        job.durations=[float(r['duration_minutes']) for r in job.observed if r.get('duration_minutes') is not None and execution_success(job.source,r.get('status'))]
                all_jobs+=js
    SOURCE_ZONE=ZoneInfo('UTC')
    settings=cfg.get('analysis') or {}
    exclude_load=settings.get('exclude_from_load') or []
    watchdogs=settings.get('watchdogs') or []
    duration_overrides=settings.get('duration_overrides_minutes') or {}
    for job in all_jobs:
        for pattern,value in duration_overrides.items():
            if match_id(job.key,[pattern]):job.override_duration=float(value)
    columns=['host','timezone','local_start','start','end_estimated','duration_minutes','duration_basis','job_id','source','job','schedule','cadence','watchdog']
    timeline=[]
    distinct={}
    for job in all_jobs:
        if job.key not in distinct:
            distinct[job.key]=job
        else:
            aggregate=distinct[job.key]
            aggregate.schedule=' | '.join(sorted(set(aggregate.schedule.split(' | ')+[job.schedule])))
            if aggregate.cadence!=job.cadence:aggregate.cadence='mixed'
        duration=job.duration()
        for local in job.expand(start,days):
            instant = wall_to_utc(local, job.timezone, warnings, job.key)
            if instant is None or not at(start,dt.time.min) <= instant < at(start+dt.timedelta(days=days),dt.time.min):continue
            timeline.append({'host':job.host,'timezone':job.timezone,'local_start':local.isoformat(timespec='seconds'),'start':instant.isoformat(timespec='seconds'),
                'end_estimated':(instant+dt.timedelta(minutes=duration)).isoformat(timespec='seconds') if duration is not None else '',
                'duration_minutes':duration if duration is not None else '', 'duration_basis':job.duration_source(),
                'job_id':job.key,'source':job.source,'job':job.name,'schedule':job.schedule,'cadence':job.cadence,'watchdog': 'true' if match_id(job.key,watchdogs) else 'false'})
    deduplicated={}
    for row in timeline:
        key=(row['job_id'],row['start'])
        if key in deduplicated:
            previous=deduplicated[key]
            previous['schedule']=' | '.join(sorted(set(previous['schedule'].split(' | ')+[row['schedule']])))
            if previous['cadence']!=row['cadence']:previous['cadence']='mixed'
        else:deduplicated[key]=row
    timeline=list(deduplicated.values())
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
    jobs_cols=['job_id','source','job','cadence','schedule','runs_in_horizon','measured_runs','median_minutes','p90_minutes','p95_minutes','min_minutes','max_minutes','average_minutes','duration_source','watchdog','excluded_from_load','note']
    jobs_rows=[]
    for job in sorted(distinct.values(),key=lambda j:j.key):
        median,p95=median_p95(job.durations)
        jobs_rows.append({'job_id':job.key,'source':job.source,'job':job.name,'cadence':job.cadence,'schedule':job.schedule,
            'runs_in_horizon':sum(r['job_id']==job.key for r in timeline), 'measured_runs':len(job.durations),
            'median_minutes':median if median is not None else '', 'p95_minutes':p95 if p95 is not None else '',
            'p90_minutes':sorted(job.durations)[math.ceil(.9*len(job.durations))-1] if job.durations else '',
            'min_minutes':min(job.durations) if job.durations else '', 'max_minutes':max(job.durations) if job.durations else '',
            'average_minutes':round(statistics.mean(job.durations),4) if job.durations else '',
            'duration_source':job.duration_source(), 'watchdog': match_id(job.key,watchdogs),
            'excluded_from_load':match_id(job.key,exclude_load) or match_id(job.key,watchdogs),'note':job.uncertainty})
    write_csv(output/'jobs.csv',[r for r in jobs_rows if not match_id(r['job_id'],excludes('jobs'))],jobs_cols)
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
    for section in sorted(set(['daily','weekly','monthly','today','timeline','overlaps','daily-recurring']) | set(report_config)):
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
    for row in filtered('starts-by-hour'):
        if match_id(row['job_id'], exclude_load) or match_id(row['job_id'], watchdogs):continue
        begin=parse_dt(row['start']);key=begin.strftime('%Y-%m-%d %H:00')
        buckets.setdefault(key,{'hour':key,'starts':0,'known_duration_starts':0,'unknown_duration_starts':0})
        buckets[key]['starts']+=1
        if row['end_estimated']:buckets[key]['known_duration_starts']+=1
        else:buckets[key]['unknown_duration_starts']+=1
    write_csv(output/'starts-by-hour.csv',list(dict(sorted(buckets.items())).values()),['hour','starts','known_duration_starts','unknown_duration_starts'])
    workload_reports(output, timeline, distinct, settings, report_config, warnings)
    summary={'jobs':len(distinct),'predicted_occurrences':len(timeline),'overlaps':len(overlaps),'warnings':len(warnings),
             'known_duration_jobs':sum(1 for j in distinct.values() if j.duration() is not None),'start':start.isoformat(),'days':days}
    def write_insight(path, rows, columns):
        patterns=excludes(path.stem)
        rows=[r for r in rows if not any(match_id(str(r.get(k,'')),patterns) for k in ('job_id','first_job_id','second_job_id'))]
        write_csv(path,rows,columns)
    if settings.get('enabled',True) is False:
        for name in ('execution-analysis','runtime-trends','resource-conflicts','dependencies','deadlines','watchdogs','what-if'):
            REPORT_SETTINGS[name]={**REPORT_SETTINGS.get(name,{}),'enabled':False}
        insight={'analysis_status':'disabled'}
    else:
        insight=analyze_insights(output,timeline,distinct,settings,write_insight,settings.get('as_of') or dt.datetime.now(dt.timezone.utc).isoformat())
    summary.update(insight)
    (output/'report-status.json').write_text(json.dumps({'disabled':[name for name,rule in REPORT_SETTINGS.items() if rule.get('enabled',True) is False]},indent=2)+'\n')
    (output/'summary.json').write_text(json.dumps(summary,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    if (REPORT_SETTINGS.get('dashboard') or {}).get('enabled',True):render_dashboard(output, start, filtered('timeline'), jobs_rows, overlaps, warnings)
    REPORT_SETTINGS={}
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
