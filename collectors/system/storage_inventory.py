"""Read-only storage topology probes, independently certified by technology.

No assembly, repair, import, scrub, key export, header backup or mount commands.
Missing tools do not certify an empty inventory. Runtime data stays in telemetry.
"""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import subprocess
import sys
from completeness import section
from sections import enabled
from telemetry import envelope, dataset, write_envelope


class Unavailable(RuntimeError):
    pass


def command(argv, timeout=30):
    executable = shutil.which(argv[0])
    if not executable:
        raise Unavailable(argv[0] + ' unavailable; this inventory could not be determined')
    result = subprocess.run([executable, *argv[1:]], capture_output=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL':'C', 'LANG':'C'})
    if result.returncode:
        raise RuntimeError(argv[0] + ': ' + result.stderr.decode('utf-8', 'replace').strip()[:1000])
    return result.stdout


def text(argv):
    return command(argv).decode('utf-8')


def ordered_rows(rows):
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def block_tree(value):
    # These arrays are sets of devices/mounts. Never apply to RAID member positions.
    if isinstance(value, dict):
        return {k: block_tree(v) for k,v in value.items()}
    if isinstance(value, list):
        return sorted((block_tree(x) for x in value), key=lambda x: json.dumps(x,sort_keys=True))
    return value


def property_rows(raw):
    rows=[]
    for line in raw.splitlines():
        fields=line.split('\t')
        if len(fields)!=4: raise ValueError('Invalid property output')
        rows.append(dict(zip(('name','property','value','source'),fields)))
    return ordered_rows(rows)


def zfs_topology(value):
    """Narrow schema whitelist; runtime state/errors/txg never enter config."""
    scalar={'name','guid','path','vdev_type','type','id','is_log','alloc_bias','whole_disk','ashift'}
    branches={'pools','vdevs','children','spares','logs','cache','l2cache','special','dedup'}
    def node(obj):
        if not isinstance(obj,dict): raise ValueError('Unrecognized ZFS topology node')
        result={k:v for k,v in obj.items() if k in scalar and not isinstance(v,(dict,list))}
        for k,v in obj.items():
            if k not in branches: continue
            if isinstance(v,dict): result[k]={name:node(child) for name,child in sorted(v.items())}
            elif isinstance(v,list): result[k]=[node(child) for child in v]  # member order matters
            else: raise ValueError('Unrecognized ZFS topology children')
        return result
    if not isinstance(value.get('pools'),dict): raise ValueError('ZFS JSON pool topology unavailable')
    result=node(value)
    for name,pool in result['pools'].items():
        if not pool.get('vdevs'): raise ValueError('ZFS vdev topology unavailable for '+name)
    return result


MAC_KEYS={
    'AllDisks','AllDisksAndPartitions','WholeDisks','DeviceIdentifier','DeviceNode','Content',
    'Size','DiskUUID','PartitionMapPartitionOffset','Partitions','VolumeName','VolumeUUID',
    'MountPoint','FilesystemType','FilesystemName','APFSContainerReference','APFSContainerUUID',
    'Containers','ContainerReference','APFSPhysicalStores','PhysicalStores','Volumes','APFSVolumeUUID',
    'APFSVolumeGroupUUID','Roles','CapacityCeiling','CapacityQuota','CapacityReserve','Fusion',
    'DesignatedPhysicalStore','Encryption','Encrypted','FileVault','CaseSensitive',
    'Internal','VirtualOrPhysical','BusProtocol','MediaName','SolidState','DeviceBlockSize',
    'TotalSize','DiskSize','RAIDSets','RAIDSetUUID','RAIDType','RAIDName','Members','MemberUUID',
    'MemberIndex','ChunkSize','CoreStorageLogicalVolumeGroups','CoreStorageLogicalVolumeGroupUUID',
    'CoreStorageLogicalVolumeGroupName','CoreStoragePhysicalVolumes','CoreStoragePhysicalVolumeUUID',
    'CoreStorageLogicalVolumeFamilies','CoreStorageLogicalVolumeFamilyUUID',
    'CoreStorageLogicalVolumes','CoreStorageLogicalVolumeUUID','CoreStorageLogicalVolumeName',
    'AppleRAIDSets','AppleRAIDSetUUID','AppleRAIDMemberUUID','BSD Name','ChunkCount','Level','Name','Rebuild','Spares',
    'CoreStorageLogicalVolumeSize','CoreStorageLVGUUID','CoreStorageLVFUUID','CoreStoragePVUUID',
}


def mac_config(value, parent=''):
    if isinstance(value,dict):
        return {k:mac_config(v,k) for k,v in value.items() if k in MAC_KEYS}
    if isinstance(value,list):
        values=[mac_config(x,parent) for x in value]
        # UUID/device identity sorts inventory sets; RAID member arrays retain slots.
        return ordered_rows(values) if parent in {'AllDisks','WholeDisks','AllDisksAndPartitions','Containers','Volumes','PhysicalStores','APFSPhysicalStores','Roles','AppleRAIDSets'} else values
    return value


def apple_raid_health(raw):
    rows=[]
    for array in raw.get('AppleRAIDSets',[]):
        state=str(array.get('Status','')).lower()
        missing=sum(str(m.get('MemberStatus','')).lower() in ('missing','offline','failed')
                    or (bool(m.get('AppleRAIDMemberUUID')) and not m.get('BSD Name') and not m.get('MemberStatus'))
                    for m in array.get('Members',[]))
        rows.append({'set_uuid':array['AppleRAIDSetUUID'],'name':array.get('Name',''),'state':array.get('Status'),
                     'degraded_count':1 if 'degraded' in state else 0 if state=='online' else None,
                     'offline_count':1 if state in ('offline','failed') else 0 if state in ('online','degraded') else None,
                     'missing_members_count':missing,'members':array.get('Members',[])})
    return rows


def raid_export(raw):
    fields={}
    for line in raw.splitlines():
        key,sep,value=line.partition('=')
        if not sep: continue
        if key in {'MD_LEVEL','MD_DEVICES','MD_METADATA','MD_UUID','MD_NAME','MD_CHUNK_SIZE','MD_LAYOUT'} or re.fullmatch(r'MD_DEVICE_.+_(DEV|ROLE)',key):
            fields[key]=value
    if 'MD_UUID' not in fields: raise ValueError('RAID identity was not returned')
    return fields


def ext_geometry(raw):
    allowed={'Filesystem UUID','Filesystem volume name','Filesystem features','Filesystem flags',
      'Filesystem OS type','Inode count','Block count','Reserved block count','First block','Block size',
      'Fragment size','Group descriptor size','Reserved GDT blocks','Blocks per group','Fragments per group',
      'Inodes per group','Inode blocks per group','Flex block group size','Inode size','Required extra isize',
      'Desired extra isize','Default mount options','Journal inode','Journal device','Journal backup','Checksum type'}
    result={}
    for line in raw.splitlines():
        key,sep,value=line.partition(':')
        if sep and key in allowed: result[key]=value.strip()
    if 'Filesystem UUID' not in result: raise ValueError('Filesystem geometry unavailable')
    return result


def collect(root, platform_name=None, include_health=False):
    root=Path(root);platform_name=platform_name or sys.platform
    scopes=[];capabilities=[];failures=[];native_mac={}
    health=envelope('system',socket.gethostname())
    def save(path,value):
        path=root/path;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(value,indent=2,sort_keys=True,ensure_ascii=False)+'\n')
    def probe(name,fn):
        scope='storage/'+name
        if not enabled('configuration') or not enabled(scope):
            status,error='disabled','Disabled by configuration'
        else:
            try:
                value=fn();save(scope+'/configuration.json',value);status,error='complete',''
            except Unavailable as exc: status,error='not_applicable',str(exc)
            except Exception as exc:
                status,error='failed',str(exc);failures.append({'section':scope,'error':error,'required':'false'})
        scopes.append(section(root,scope,status,error));capabilities.append({'section':scope,'status':status,'detail':error})
    def mounts():
        return json.loads(text(['findmnt','--json','--list','--output','SOURCE,TARGET,FSTYPE,OPTIONS']))['filesystems']
    def lvm():
        result={}
        columns={'pvs':'pv_name,pv_uuid,vg_name,vg_uuid,pv_size,pe_start',
                 'vgs':'vg_name,vg_uuid,vg_size,vg_extent_size,pv_count',
                 'lvs':'vg_name,lv_name,lv_uuid,lv_size,lv_layout,lv_role,segtype,seg_start,seg_size,devices'}
        for cmd,fields in columns.items():
            args=[cmd,'--reportformat','json','--units','b','--nosuffix','-a','-o',fields]
            if cmd=='lvs': args.append('--segments')
            data=json.loads(text(args));rows=[]
            for report in data['report']:
                for entries in report.values(): rows.extend(entries)
            result[cmd]=ordered_rows(rows)
        return result
    def raid():
        scan=text(['mdadm','--detail','--scan'])
        arrays=[]
        for line in scan.splitlines():
            fields=line.split()
            if len(fields)>1 and fields[0]=='ARRAY':
                arrays.append({'device':fields[1],**raid_export(text(['mdadm','--detail','--export',fields[1]]))})
        return ordered_rows(arrays)
    def filesystems(kind):
        discovered=[]
        def discover():
            discovered.extend(m for m in mounts() if m.get('fstype') in (('xfs',) if kind=='xfs' else ('ext2','ext3','ext4')))
            return ordered_rows(discovered)
        probe(kind+'/mounts',discover)
        for mount in discovered:
            # Each mount is independent: inaccessible container/VM bind devices
            # must not prevent geometry capture for other mounted filesystems.
            identity=hashlib.sha256(mount['target'].encode('utf-8')).hexdigest()
            def geometry(mount=mount):
                if kind=='ext':
                    device=re.sub(r'\[/.*\]$','',mount['source'])
                    return {'source':mount['source'],'mount':mount['target'],'geometry':ext_geometry(text(['tune2fs','-l',device]))}
                geometry=text(['xfs_info',mount['target']]).strip()
                if 'meta-data=' not in geometry or 'bsize=' not in geometry: raise ValueError('Unrecognized XFS geometry')
                return {'source':mount['source'],'mount':mount['target'],'geometry':geometry}
            probe(kind+'/filesystems/'+identity,geometry)
    if platform_name.startswith('linux'):
        probe('block-devices',lambda:block_tree(json.loads(text(['lsblk','--json','--bytes','--output',
              'NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,PARTUUID,PARTLABEL,PTTYPE,MODEL,SERIAL,WWN,TRAN,ROTA,RO,RM,MOUNTPOINTS']))))
        probe('mounts',lambda:block_tree(mounts()))
        probe('lvm',lvm);probe('mdraid',raid)
        filesystems('xfs');filesystems('ext')
        # Explicit custom format omits live path state and failover counters.
        probe('multipath',lambda:sorted(text(['multipathd','show','maps','raw','format','%n %w %d']).splitlines()))
    if platform_name=='darwin':
        for name,args in {'disks':['list','-plist'],'apfs':['apfs','list','-plist'],
                          'corestorage':['coreStorage','list','-plist'],'apple-raid':['appleRAID','list','-plist']}.items():
            def read_mac(args=args):
                raw=plistlib.loads(command(['diskutil',*args]));native_mac[args[0]]=raw;normalized=mac_config(raw)
                if raw and not normalized:raise ValueError('Unrecognized diskutil inventory schema')
                return normalized
            probe(name,read_mac)
    if platform_name.startswith('linux') or platform_name=='darwin':
        probe('zpool-properties',lambda:property_rows(text(['zpool','get','-Hp','-o','name,property,value,source',
          'ashift,autoexpand,autoreplace,autotrim,bootfs,cachefile,comment,compatibility,delegation,failmode,listsnapshots,multihost,readonly,version'])))
        probe('zfs-properties',lambda:property_rows(text(['zfs','get','-Hp','-o','name,property,value,source','-t','filesystem,volume',
          'type,mountpoint,canmount,compression,recordsize,volblocksize,volsize,quota,refquota,reservation,refreservation,checksum,copies,dedup,sync,atime,relatime,xattr,acltype,aclmode,aclinherit,casesensitivity,normalization,utf8only,encryption,keyformat,encryptionroot'])))
        probe('zfs-topology',lambda:zfs_topology(json.loads(text(['zpool','status','-j','--json-int','-P']))))
    if include_health and enabled('telemetry/storage'):
        # Volatile health is independent of successful configuration certification.
        if platform_name=='darwin':
            if 'appleRAID' in native_mac:
                dataset(health,'apple_raid',apple_raid_health(native_mac['appleRAID']),['set_uuid','name'],
                        {'degraded_count':'count','offline_count':'count','missing_members_count':'count'})
            else:health['failures'].append({'section':'apple_raid','error':'RAID inventory unavailable'})
        for name,args in ({'zpool-status':['zpool','status','-P']}.items()):
            if shutil.which(args[0]):
                try: save('telemetry/storage/'+name+'.json',{'observed_at':health['observed_at'],'output':text(args)})
                except Exception as exc:health['failures'].append({'section':name,'error':str(exc)})
        if platform_name.startswith('linux'):
            try:
                arrays=[]
                for md in sorted(Path('/sys/block').glob('md*/md')):
                    row={'array':md.parent.name}
                    for source,key in [('degraded','degraded_count'),('sync_action','sync_action'),('array_state','array_state')]:
                        row[key]=(md/source).read_text().strip()
                    arrays.append(row)
                dataset(health,'mdraid',arrays,['array'],{'degraded_count':'count'})
            except Exception as exc:health['failures'].append({'section':'mdraid','error':str(exc)})
        write_envelope(root/'telemetry/storage/health.json',health)
    save('storage/capabilities.json',capabilities)
    scopes.append(section(root,'storage/capabilities.json'))
    return scopes,failures
