#!/usr/bin/env python3
"""Image-only LVM/mdraid/ext4/XFS lab for a DISPOSABLE Linux VM.

Requires root and --confirm-disposable-vm. Accepts no existing device paths.
Four NEW sparse files become loop devices. All destructive operations target
those verified loops, a uniquely named VG/LV, or the uniquely named test array.
Run with lvm2, mdadm, util-linux, e2fsprogs and xfsprogs installed.
"""
import argparse,json,os,re,shutil,stat,subprocess,sys,tempfile,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'collectors/system')]
from storage_inventory import collect
from completeness import publish,Coverage


def execute(args,check=True):
    result=subprocess.run(args,capture_output=True,text=True,timeout=90)
    if check and result.returncode:raise RuntimeError(args[0]+': '+result.stderr.strip())
    return result


def owns(loop,path):
    result=execute(['losetup','--json','--output','NAME,BACK-FILE',loop])
    records=json.loads(result.stdout)['loopdevices']
    return len(records)==1 and records[0]['name']==loop and Path(records[0]['back-file']).resolve()==path.resolve()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--confirm-disposable-vm',action='store_true');args=parser.parse_args()
    if not sys.platform.startswith('linux') or os.geteuid()!=0 or not args.confirm_disposable_vm:parser.error('Root in a disposable Linux VM and --confirm-disposable-vm are required')
    for command in ('losetup','pvcreate','vgcreate','lvcreate','lvremove','vgremove','pvremove','mdadm','mkfs.ext4','mkfs.xfs','mount','umount'):
        if not shutil.which(command):parser.error('Missing tool: '+command)
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    lab=out/'images';lab.mkdir();name='cblab_'+uuid.uuid4().hex[:10];pairs=[];vg=False;array=False;mounted=[];results={};cleanup=[]
    array_path='/dev/md/'+name;lv='/dev/'+name+'/data'
    def owned():
        if not all(owns(loop,path) for loop,path in pairs):raise RuntimeError('Image-device mapping changed; refusing disk operations')
    def only_lvm():return ['--devices',','.join(loop for loop,path in pairs[:2]),'--config','activation { udev_sync=0 udev_rules=0 }']
    try:
        for n in range(4):
            path=lab/('member'+str(n)+'.img')
            with path.open('xb') as stream:stream.truncate(512*1024**2)
            # Minimal containers may lack device nodes even with a loop-capable VM kernel.
            candidate=execute(['losetup','--find']).stdout.strip()
            if re.fullmatch(r'/dev/loop\d+',candidate) and not Path(candidate).exists():os.mknod(candidate,stat.S_IFBLK|0o600,os.makedev(7,int(candidate[9:])))
            loop=execute(['losetup','--find','--show',str(path)]).stdout.strip()
            if not re.fullmatch(r'/dev/loop\d+',loop) or not owns(loop,path):raise RuntimeError('Loop ownership verification failed')
            pairs.append((loop,path))
        owned();execute(['pvcreate',*only_lvm(),*[p[0] for p in pairs[:2]]])
        execute(['vgcreate',*only_lvm(),name,*[p[0] for p in pairs[:2]]]);vg=True
        execute(['lvcreate',*only_lvm(),'-L','320M','-n','data',name]);execute(['mkfs.xfs','-f',lv])
        owned();execute(['mdadm','--create',array_path,'--run','--level=1','--raid-devices=2','--metadata=1.2','--name='+name,*[p[0] for p in pairs[2:]]]);array=True
        execute(['mkfs.ext4','-F',array_path])
        for device,leaf in [(lv,'xfs'),(array_path,'ext4')]:
            target=out/leaf;target.mkdir();execute(['mount',device,str(target)]);mounted.append(target)
        snapshots=[]
        for n in (1,2):
            target=out/str(n);target.mkdir();scopes,failures=collect(target,'linux',include_health=True);publish(target,scopes);snapshots.append((target,Coverage(target)))
        import hashlib
        a,b=snapshots;targets=('storage/lvm','storage/mdraid',
            'storage/xfs/filesystems/'+hashlib.sha256(str(out/'xfs').encode()).hexdigest(),
            'storage/ext/filesystems/'+hashlib.sha256(str(out/'ext4').encode()).hexdigest())
        for target in targets:
            assert next(s for s in a[1].sections if s['path']==target)['status']=='complete',target
        changed=[f for f in sorted(a[1].files & b[1].files) if any(f.startswith(p+'/') for p in targets) and (a[0]/f).read_bytes()!=(b[0]/f).read_bytes()]
        assert not changed,changed
        results={'status':'passed','checks':['live LVM across two image-backed PVs','live mdraid mirror across two image-backed members','mounted XFS and ext4 geometry','two-run deterministic storage configuration'],'changed':changed,
                 'unrelated_failed_scopes':[s for s in a[1].sections if s['status']=='failed']}
    except Exception as exc:
        results.update(status='failed',error=str(exc))
        raise
    finally:
        for target in reversed(mounted):
            result=execute(['umount',str(target)],False);cleanup.append({'unmount':target.name,'success':result.returncode==0})
        # Keep images if cleanup fails. Never attempt cleanup after a mapping changed.
        if pairs:
            try:
                owned()
                if array:
                    result=execute(['mdadm','--stop',array_path],False);cleanup.append({'array_stop':result.returncode==0})
                if vg:
                    result=execute(['vgremove',*only_lvm(),'-f',name],False);cleanup.append({'vg_remove':result.returncode==0})
                for loop,path in reversed(pairs):
                    if not owns(loop,path):raise RuntimeError('Refusing detach: image binding changed')
                    result=execute(['losetup','--detach',loop],False);cleanup.append({'detach':path.name,'success':result.returncode==0})
            except Exception as exc:cleanup.append({'error':str(exc)})
        results['cleanup']=cleanup
        if any('error' in item or any(value is False for value in item.values()) for item in cleanup):
            results.update(status='failed',cleanup_status='failed')
        else:results['cleanup_status']='passed'
        (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2));return 0 if results.get('status')=='passed' else 1
if __name__=='__main__':raise SystemExit(main())
