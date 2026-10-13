"""Read-only SMART/NVMe and Windows reliability observations, isolated per drive.

Never starts self-tests, enables SMART, changes drive settings or wakes ATA disks
in standby intentionally. Device/USB/controller support and privileges vary.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import yaml
from completeness import section
from sections import enabled
from telemetry import envelope, dataset, write_envelope

UNITS={'failed_count':'count','temperature_celsius':'celsius','wear_used_percent':'percent',
       'available_spare_percent':'percent','spare_below_threshold_count':'count','critical_warning_count':'count',
       'media_errors_count':'count','error_log_entries_count':'count','unsafe_shutdowns_count':'count',
       'power_on_hours':'hours','power_cycles_count':'count','reallocated_sectors_count':'count',
       'pending_sectors_count':'count','offline_uncorrectable_count':'count','interface_crc_errors_count':'count'}


def invoke(arguments):
    executable=shutil.which('smartctl')
    if not executable:raise RuntimeError('smartctl unavailable; SMART coverage unknown')
    result=subprocess.run([executable,'-j',*arguments],capture_output=True,timeout=30,
                          env={**os.environ,'LC_ALL':'C'})
    payload=json.loads(result.stdout.decode('utf-8'))
    code=int(payload.get('smartctl',{}).get('exit_status',result.returncode))
    # Bits 3..7 report health/history problems: nonzero does NOT mean no data.
    if code & 3:raise RuntimeError('SMART device unavailable, sleeping or invalid request (exit '+str(code)+')')
    return payload,code


def identity(raw,device):
    result={'device':device}
    for key in ('model_name','model_family','serial_number','firmware_version','user_capacity',
                'logical_block_size','physical_block_size','rotation_rate','form_factor','trim',
                'ata_version','sata_version','nvme_version','nvme_number_of_namespaces','smart_support','device'):
        if key in raw:result[key]=raw[key]
    if not any(key in raw for key in ('model_name','serial_number','user_capacity')):
        raise ValueError('SMART device identity unavailable')
    return result


def metrics(raw,device):
    row={'device':device,'serial':str(raw.get('serial_number','')),
         **{key:None for key in UNITS}}
    passed=raw.get('smart_status',{}).get('passed')
    if isinstance(passed,bool):row['failed_count']=int(not passed)
    row['temperature_celsius']=raw.get('temperature',{}).get('current')
    row['power_on_hours']=raw.get('power_on_time',{}).get('hours')
    row['power_cycles_count']=raw.get('power_cycle_count')
    nvme=raw.get('nvme_smart_health_information_log',{})
    for source,target in {'temperature':'temperature_celsius','percentage_used':'wear_used_percent',
            'available_spare':'available_spare_percent','media_errors':'media_errors_count',
            'num_err_log_entries':'error_log_entries_count','unsafe_shutdowns':'unsafe_shutdowns_count',
            'power_on_hours':'power_on_hours','power_cycles':'power_cycles_count'}.items():
        if source in nvme:row[target]=nvme[source]
    if 'critical_warning' in nvme:row['critical_warning_count']=int(nvme['critical_warning']!=0)
    if 'available_spare' in nvme and 'available_spare_threshold' in nvme:
        row['spare_below_threshold_count']=int(nvme['available_spare']<nvme['available_spare_threshold'])
    # Vendor attribute raw values are not universally interchangeable. Only use
    # these established ID+name pairs; preserve all others in raw telemetry.
    known={5:('Reallocated_Sector_Ct','reallocated_sectors_count'),
           197:('Current_Pending_Sector','pending_sectors_count'),
           198:('Offline_Uncorrectable','offline_uncorrectable_count'),
           199:('UDMA_CRC_Error_Count','interface_crc_errors_count')}
    for attribute in raw.get('ata_smart_attributes',{}).get('table',[]):
        candidate=known.get(attribute.get('id'))
        if candidate and attribute.get('name')==candidate[0]:row[candidate[1]]=attribute.get('raw',{}).get('value')
    return row


def specifications(config=None,devices=()):
    cfg=yaml.safe_load(Path(config).read_text()) if config else {}
    cfg=cfg or {}
    specs=cfg.get('devices',[])+[{'path':path} for path in devices]
    if not isinstance(specs,list):raise ValueError('SMART devices must be a list')
    seen=set()
    for spec in specs:
        if not isinstance(spec,dict) or not isinstance(spec.get('path'),str) or not spec['path'] or spec['path'].startswith('-'):
            raise ValueError('SMART device needs a path, not command options')
        if not isinstance(spec.get('enabled',True),bool):raise ValueError('SMART device enabled must be boolean')
        if spec['path'] in seen:raise ValueError('Duplicate SMART device path')
        seen.add(spec['path'])
        if 'type' in spec and (not isinstance(spec['type'],str) or not spec['type'] or spec['type'].startswith('-')):
            raise ValueError('Invalid SMART device type')
    return specs


def collect(root,config=None,devices=(),run_ps=None,platform_name=None):
    root=Path(root);scopes=[];failures=[];rows=[];health=envelope('system',socket.gethostname())
    if not enabled('telemetry/drives'):
        return [section(root,'storage/drive-identities','disabled','Drive collection disabled')],[]
    def save(path,data):
        target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(data,sort_keys=True,indent=2)+'\n')
    def failed(scope,exc):
        scopes.append(section(root,scope,'failed',str(exc)))
        item={'section':scope,'error':str(exc)};failures.append(item);health['failures'].append(item)
    def certify(scope,data):
        if enabled('configuration') and enabled(scope):
            save(scope+'/configuration.json',data);scopes.append(section(root,scope))
        else:scopes.append(section(root,scope,'disabled','Configuration inventory disabled; runtime readings remain separate'))
    specs=specifications(config,devices)
    if not specs:
        try:
            scanned,code=invoke(['--scan'])
            if code:raise RuntimeError('SMART discovery incomplete (exit '+str(code)+')')
            specs=[{'path':d['name'],'type':d.get('type')} for d in scanned.get('devices',[])]
            certify('storage/drive-identities/discovery',sorted(specs,key=lambda d:d['path']))
        except Exception as exc:failed('storage/drive-identities/discovery',exc)
    for spec in specs:
        name=spec['path'];key=hashlib.sha256(name.encode()).hexdigest();scope='storage/drive-identities/'+key
        if spec.get('enabled',True) is False:
            scopes.append(section(root,scope,'disabled','Disabled by configuration'));continue
        try:
            args=['-a','-n','standby,2']
            if spec.get('type'):args+=['-d',spec['type']]
            raw,code=invoke([*args,name])
            save('telemetry/drives/raw/'+key+'.json',{'observed_at':health['observed_at'],'device':name,'data':raw})
            rows.append(metrics(raw,name))
            certify(scope,identity(raw,name))
            if code & 4:health['failures'].append({'section':scope,'error':'Some SMART commands unavailable; returned readings retained (exit '+str(code)+')'})
        except Exception as exc:failed(scope,exc)
    dataset(health,'smart',rows,['device','serial'],UNITS)
    if (platform_name or sys.platform)=='win32' and run_ps:
        # Native reliability works without smartmontools on supported controllers.
        try:
            values=run_ps("Get-PhysicalDisk -ErrorAction Stop | Sort-Object DeviceId | ForEach-Object { $d=$_; $r=$null; $errorText=$null; try { $r=$d | Get-StorageReliabilityCounter -ErrorAction Stop } catch { $errorText='Reliability counters unavailable' }; [pscustomobject]@{device=[string]$d.DeviceId;serial=[string]$d.SerialNumber;health=[string]$d.HealthStatus;operational=[string]$d.OperationalStatus;temperature_celsius=$r.Temperature;wear_used_percent=$r.Wear;power_on_hours=$r.PowerOnHours;read_errors_count=$r.ReadErrorsTotal;write_errors_count=$r.WriteErrorsTotal;read_uncorrected_count=$r.ReadErrorsUncorrected;write_uncorrected_count=$r.WriteErrorsUncorrected;error=$errorText} }")
            values=[values] if isinstance(values,dict) else values or []
            for row in values:
                row['failed_count']=0 if row['health']=='Healthy' else 1 if row['health'] in ('Warning','Unhealthy') else None
                if row.get('error'):health['failures'].append({'section':'windows-drive:'+row['device'],'error':row['error']})
            dataset(health,'windows_drive',values,['device','serial'],{'failed_count':'count','temperature_celsius':'celsius','wear_used_percent':'percent','power_on_hours':'hours','read_errors_count':'count','write_errors_count':'count','read_uncorrected_count':'count','write_uncorrected_count':'count'})
        except Exception as exc:health['failures'].append({'section':'windows-reliability','error':str(exc)})
    write_envelope(root/'telemetry/drives/health.json',health)
    return scopes,failures
