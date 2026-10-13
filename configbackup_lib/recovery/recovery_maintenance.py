"""Refresh immutable recovery exports, check freshness and verify secondary copies."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import yaml
from configbackup_lib.recovery.recovery_assets import export_recovery, inventory
from configbackup_lib.recovery.setup_recovery import verify, digest
from configbackup_lib.recovery.transaction_journal import atomic


def signature(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()



def freshness(config,config_path,bundle):
    current=inventory(config,config_path,include_application=True)
    try:
        check,old=verify(bundle)
        match=signature(current)==signature(old)
        return {'status':'current' if check['status']=='passed' and match and current['status']=='complete' else 'stale_or_invalid','bundle_verified':check['status']=='passed','selected_inputs_match':match,'current_inputs_complete':current['status']=='complete'}
    except (ValueError,OSError,KeyError,TypeError):
        return {'status':'unavailable','bundle_verified':False,'selected_inputs_match':False,'current_inputs_complete':current['status']=='complete'}


def refresh(config,config_path,directory,secondary=None,*,password=None):
    from configbackup_lib.engine.configbackup import single_instance_lock, LockError
    root=Path(directory).resolve();config_path=Path(config_path).resolve()
    selected=inventory(config,config_path)
    if selected['status']!='complete':raise ValueError('Incomplete selected inputs; prior export retained')
    if any(root==Path(r['source']) or Path(r['source']).is_relative_to(root) for r in selected['assets']):raise ValueError('Recovery output cannot contain selected inputs')
    secondary=Path(secondary).resolve() if secondary else None
    if secondary and (secondary.is_relative_to(root) or root.is_relative_to(secondary)):raise ValueError('Secondary copy must be separate')
    if secondary and any(Path(r['source']).is_relative_to(secondary) for r in selected['assets']):raise ValueError('Secondary output cannot contain selected inputs')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    with single_instance_lock(root/'.refresh.lock'):
        # Export into new private scratch so code changes and byte races are included.
        with tempfile.TemporaryDirectory(prefix='.refresh-',dir=root) as td:
            stage=Path(td)/'bundle';value=export_recovery(config,config_path,stage)
            ident=signature(value)
            check_copy(stage,expected=ident)
            target=root/ident
            if target.exists():
                check_copy(target,expected=ident)
                status='unchanged'
            else:stage.rename(target);status='created'
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        result={'schema':1,'status':status,'bundle':str(target),'fingerprint':ident,'checked_at':now,'secondary':{'status':'not_configured'}}
        if secondary:
            try:
                secondary.mkdir(parents=True,exist_ok=True,mode=0o700)
                with single_instance_lock(secondary/'.refresh.lock'):
                    copy=secondary/(ident+'.cbrec' if password is not None else ident)
                    if not copy.exists():
                        with tempfile.TemporaryDirectory(prefix='.copy-',dir=secondary) as td:
                            candidate=Path(td)/('bundle.cbrec' if password is not None else 'bundle')
                            if password is None:shutil.copytree(target,candidate)
                            else:
                                from configbackup_lib.recovery.recovery_crypto import encrypt
                                encrypt(target,candidate,password)
                            check_copy(candidate,password,ident)
                            candidate.rename(copy)
                    check_copy(copy,password,ident)
                result['secondary']={'status':'verified','path':str(copy),'encrypted':password is not None,'verified_at':now,'note':'Operator-selected destination; this does not prove it is a different physical host.'}
            except (OSError,ValueError,ImportError,LockError) as exc:result['secondary']={'status':'failed','error_type':type(exc).__name__,'retryable':True}
        atomic(root/'latest.json',(json.dumps(result,indent=2,sort_keys=True)+'\n').encode())
        return result


def status(directory,password=None):
    data=json.loads((Path(directory)/'latest.json').read_text());now=dt.datetime.now(dt.timezone.utc)
    result={'bundle':data['bundle'],'local':{},'secondary':dict(data['secondary'])}
    try:result['local']=verify(data['bundle'])[0]
    except (OSError,ValueError):result['local']={'status':'failed'}
    copy=result['secondary']
    if copy.get('verified_at'):copy['verification_age_days']=max(0,(now-dt.datetime.fromisoformat(copy['verified_at'])).total_seconds()/86400)
    if copy.get('path'):
        try:
            if copy.get('encrypted') and password is None:copy['current_verification']='not_checked_passphrase_required'
            else:check_copy(Path(copy['path']),password,data['fingerprint']);copy['current_verification']='passed'
        except (OSError,ValueError,ImportError):copy['current_verification']='failed'
    return result


def check_copy(path,password=None,expected=None):
    path=Path(path)
    if path.is_symlink():raise ValueError('Symlink recovery copy refused')
    if path.suffix=='.cbrec':
        if password is None:raise ValueError('Passphrase required to verify encrypted copy')
        from configbackup_lib.recovery.recovery_crypto import decrypt
        with tempfile.TemporaryDirectory(prefix='configbackup-copy-verify-') as td:
            target=Path(td)/'bundle';decrypt(path,target,password);check,value=verify(target)
    else:check,value=verify(path)
    if check['status']!='passed' or expected and signature(value)!=expected:raise ValueError('Secondary copy verification failed')
    return value


def retain(directory,keep=3,*,password=None,apply=False):
    """Prune only recognized verified bundles; always protect newest good recovery."""
    import re
    from configbackup_lib.engine.configbackup import single_instance_lock
    if not isinstance(keep,int) or isinstance(keep,bool) or keep<1:raise ValueError('Keep at least one verified recovery copy')
    root=Path(directory).resolve();rows=[]
    with single_instance_lock(root/'.refresh.lock'):
        latest=None
        if (root/'latest.json').exists():latest=json.loads((root/'latest.json').read_text()).get('fingerprint')
        verified=[]
        for path in sorted(root.iterdir(),key=lambda p:(-p.lstat().st_mtime_ns,p.name)):
            ident=path.name.removesuffix('.cbrec')
            if not re.fullmatch('[0-9a-f]{64}',ident):continue
            row={'path':str(path),'fingerprint':ident,'action':'protected','reason':'unverified'};rows.append(row)
            try:
                check_copy(path,password,ident);verified.append((path,row))
                row['reason']='verified recovery'
            except (OSError,ValueError,KeyError,TypeError,ImportError):continue
        for n,(path,row) in enumerate(verified):
            if n<keep or row['fingerprint']==latest:row['reason']='retained verified recovery or latest pointer';continue
            row.update(action='delete',reason='beyond verified-copy retention')
            if apply:
                # Verify a protected survivor again immediately before each deletion.
                survivor=verified[0][0];check_copy(survivor,password,verified[0][1]['fingerprint'])
                try:
                    if path.is_dir():shutil.rmtree(path)
                    else:path.unlink()
                    row['action']='deleted'
                except OSError:row.update(action='failed',reason='deletion failed; protected recovery remains')
    return {'status':'no_verified_recovery' if not verified else 'failed' if any(r['action']=='failed' for r in rows) else 'applied' if apply else 'preview','copies':rows,'verified_copies':len(verified),
            'limits':'Unrecognized, corrupt, symlink and unverifiable encrypted copies are protected for manual review. Retention is opt-in. Passphrase rotation is not automatic. A retained copy must still be tested on the recovery machine.'}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['refresh','status','freshness','retention']);p.add_argument('--config');p.add_argument('--directory');p.add_argument('--bundle');p.add_argument('--secondary');p.add_argument('--output');p.add_argument('--encrypt-secondary',action='store_true');p.add_argument('--password-env',help='Passphrase environment variable NAME');p.add_argument('--keep',type=int,default=3);p.add_argument('--apply',action='store_true')
    a=p.parse_args(argv)
    import os,getpass
    password=os.environ.get(a.password_env,'') if a.password_env else getpass.getpass('Recovery passphrase: ') if a.encrypt_secondary else None
    if a.encrypt_secondary and not a.secondary:p.error('--encrypt-secondary requires --secondary')
    if a.action=='status':value=status(a.directory,password)
    elif a.action=='retention':value=retain(a.directory,a.keep,password=password,apply=a.apply)
    else:
        if not a.config:p.error('--config is required')
        config=yaml.safe_load(Path(a.config).read_text())
        value=freshness(config,a.config,a.bundle) if a.action=='freshness' else refresh(config,a.config,a.directory,a.secondary,password=password)
    if a.output:
        from configbackup_lib.engine.discovery_review import write_report
        write_report(value,a.output,'Recovery package freshness')
    print(json.dumps(value,indent=2,sort_keys=True))
    return 6 if value.get('status') in ('failed','no_verified_recovery','stale_or_invalid','unavailable') or value.get('local',{}).get('status')=='failed' or value.get('secondary',{}).get('status')=='failed' or value.get('secondary',{}).get('current_verification') in ('failed','not_checked_passphrase_required') else 0

if __name__=='__main__':raise SystemExit(main())
