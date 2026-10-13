#!/usr/bin/env python3
"""Create an AppleRAID mirror from TWO NEW IMAGE FILES ONLY, collect twice, detach.

Requires --confirm-image-only-lab. Never accepts an existing device or image.
The fresh output directory retains images and evidence for review/reproduction.
CoreStorage creation is deliberately not attempted on current macOS.
"""
import argparse,json,plistlib,subprocess,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'collectors/system')]
from storage_inventory import collect
from completeness import publish,Coverage


def execute(argv):
    cp=subprocess.run(argv,capture_output=True,timeout=120)
    if cp.returncode:raise RuntimeError(argv[0]+': '+cp.stderr.decode(errors='replace'))
    return cp.stdout


def image_devices(path):
    payload=plistlib.loads(execute(['hdiutil','info','-plist']))
    matches=[i for i in payload.get('images',[]) if Path(i.get('image-path','')).resolve()==path.resolve()]
    if len(matches)!=1:raise RuntimeError('Image ownership could not be established: '+str(path))
    return [s['dev-entry'] for s in matches[0].get('system-entities',[]) if s.get('dev-entry')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--confirm-image-only-lab',action='store_true')
    parser.add_argument('--degrade',action='store_true',help='Detach and delete ONE newly created member image, then verify health/alert and surviving data')
    args=parser.parse_args()
    if sys.platform!='darwin' or not args.confirm_image_only_lab:parser.error('macOS and --confirm-image-only-lab are required')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    name='CBLab-'+uuid.uuid4().hex[:12];created=[];devices=[];result={};cleanup=[];faulted=set()
    try:
        for index in range(2):
            path=out/('member'+str(index)+'.img')
            execute(['diskutil','image','create','blank','--size','128M','--format','RAW','--fs','None',str(path)])
            created.append(path)
            attached=plistlib.loads(execute(['hdiutil','attach','-nomount','-plist',str(path)]))
            candidates=[s['dev-entry'] for s in attached['system-entities'] if s.get('dev-entry')]
            # Whole, newly attached image disks only. Never fall back to disk numbers.
            owned=image_devices(path)
            candidate=next((d for d in candidates if d in owned and d.removeprefix('/dev/disk').isdigit()),None)
            if candidate is None:raise RuntimeError('No verified whole image device')
            devices.append(candidate)
        assert len(set(devices))==2
        for path,device in zip(created,devices):
            if device not in image_devices(path):raise RuntimeError('Image-device binding changed')
        (out/'create.log').write_bytes(execute(['diskutil','appleRAID','create','mirror',name,'JHFS+',*devices]))
        # Keep native plist evidence so collectors can be checked against the actual schema.
        (out/'raid.plist').write_bytes(execute(['diskutil','appleRAID','list','-plist']))
        snapshots=[]
        for number in (1,2):
            target=out/str(number);target.mkdir();scopes,failures=collect(target,'darwin',include_health=True);publish(target,scopes)
            coverage=Coverage(target);snapshots.append((target,coverage))
        a,b=snapshots
        changed=[name for name in sorted(a[1].files & b[1].files) if (a[0]/name).read_bytes()!=(b[0]/name).read_bytes()]
        raid=next(s for s in a[1].sections if s['path']=='storage/apple-raid')
        if raid['status']=='complete':
            inventory=json.loads((a[0]/'storage/apple-raid/configuration.json').read_text())
            lab=next(item for item in inventory['AppleRAIDSets'] if item['Name']==name)
            assert lab['Level']=='Mirror' and len(lab['Members'])==2
            assert all('AppleRAIDMemberUUID' in member for member in lab['Members'])
        result={'status':'passed' if raid['status']=='complete' and not changed else 'needs_review',
                'raid_scope':raid['status'],'changed_certified_files':changed,'lab_name':name,'images':[p.name for p in created]}
        if args.degrade:
            native=plistlib.loads((out/'raid.plist').read_bytes())
            native_lab=next(item for item in native['AppleRAIDSets'] if item['Name']==name)
            identity=native_lab['AppleRAIDSetUUID']
            # Use a short, explicit timeout on THIS NEW LAB SET ONLY. The default
            # can leave a removed-member mirror Offline before it degrades.
            execute(['diskutil','appleRAID','update','SetTimeout','5',identity])
            info=plistlib.loads(execute(['diskutil','info','-plist',native_lab['BSD Name']]))
            mount=Path(info['MountPoint']);fixture=mount/'configbackup-lab-proof.txt'
            with fixture.open('xb') as stream:
                stream.write(b'ConfigBackup disposable mirror data survives a missing image.\n')
                stream.flush()
                import os
                os.fsync(stream.fileno())
            if devices[1] not in image_devices(created[1]):raise RuntimeError('Fault injection refused: image binding changed')
            execute(['hdiutil','detach','-force',devices[1]]);faulted.add(created[1]);created[1].unlink()
            import time
            from storage_inventory import apple_raid_health
            until=time.monotonic()+45;transitions=[]
            while True:
                native=plistlib.loads(execute(['diskutil','appleRAID','list','-plist']))
                (out/'degraded-raid.plist').write_bytes(plistlib.dumps(native))
                health=next((r for r in apple_raid_health(native) if r['set_uuid']==identity),None)
                if health and (not transitions or transitions[-1]['state']!=health['state']):
                    transitions.append(health)
                    (out/'state-transitions.json').write_text(json.dumps(transitions,indent=2)+'\n')
                if health and health['degraded_count']==1:break
                if time.monotonic()>=until:raise AssertionError('AppleRAID did not expose expected degraded state: '+repr(health))
                time.sleep(.5)
            (out/'degraded-raid.plist').write_bytes(plistlib.dumps(native))
            # Device/mount identifiers can change after the driver reassembles.
            native_lab=next(item for item in native['AppleRAIDSets'] if item['AppleRAIDSetUUID']==identity)
            info=plistlib.loads(execute(['diskutil','info','-plist',native_lab['BSD Name']]))
            if not info.get('MountPoint'):
                execute(['diskutil','mount',native_lab['BSD Name']])
                info=plistlib.loads(execute(['diskutil','info','-plist',native_lab['BSD Name']]))
            if not info.get('MountPoint'):raise AssertionError('Degraded lab volume could not be mounted')
            fixture=Path(info['MountPoint'])/'configbackup-lab-proof.txt'
            assert fixture.read_bytes()==b'ConfigBackup disposable mirror data survives a missing image.\n'
            target=out/'degraded';target.mkdir();scopes,failures=collect(target,'darwin',include_health=True);publish(target,scopes);Coverage(target)
            inventory=json.loads((target/'storage/apple-raid/configuration.json').read_text())
            surviving=next(item for item in inventory['AppleRAIDSets'] if item['AppleRAIDSetUUID']==identity)
            assert len(surviving['Members'])==2 and sum(bool(m.get('BSD Name')) for m in surviving['Members'])>=1
            from monitoring import run_monitor
            summary=run_monitor({'sources':[str(target/'telemetry/storage/health.json')],'consecutive':1},out/'health-report',deliver=False)
            assert any(a['rule']=='apple-raid-degraded' for a in summary['alerts'])
            results_checks=['member image detached and deleted','RAID UUID retained','both member UUIDs retained','surviving file readable','degraded health metric captured','degraded alert opened locally; no notification sent']
            result['degraded_test']={'status':'passed','checks':results_checks,'health':health}
            result['degraded_test']['transitions']=transitions

    except Exception as exc:
        result.update(status='failed',error=str(exc))
        raise
    finally:
        # Detach only devices STILL bound to the exact images created above.
        # Retain image contents; detaching does not erase real disks or other images.
        for path,device in reversed(list(zip(created,devices))):
            if path in faulted:
                cleanup.append({'image':path.name,'detached':True,'deleted_for_test':True});continue
            try:
                if device not in image_devices(path):raise RuntimeError('Refusing cleanup after image-device binding changed')
                execute(['hdiutil','detach','-force',device]);cleanup.append({'image':path.name,'detached':True})
            except Exception as exc:cleanup.append({'image':path.name,'detached':False,'error':str(exc)})
        result['cleanup']=cleanup
        (out/'results.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if result.get('status')=='passed' and all(r['detached'] for r in cleanup) else 1

if __name__=='__main__':raise SystemExit(main())
