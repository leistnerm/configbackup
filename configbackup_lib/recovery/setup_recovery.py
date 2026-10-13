"""Verify setup exports, relocate disabled candidates, and rehearse recovery offline."""
from __future__ import annotations
from configbackup_lib import APPLICATION_ROOT
import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile

MAX_MANIFEST=8*1024*1024


def digest(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def safe_file(root,name):
    if not isinstance(name,str) or '\\' in name or ':' in name:raise ValueError('Unsafe bundle path')
    parts=PurePosixPath(name)
    if parts.is_absolute() or not parts.parts or any(p in ('.','..') for p in name.split('/')):raise ValueError('Unsafe bundle path')
    path=root.joinpath(*parts.parts)
    if any(p.is_symlink() for p in [path,*path.parents] if p!=root.parent):raise ValueError('Symlinks are not allowed in recovery bundles')
    if not path.resolve().is_relative_to(root):raise ValueError('Bundle path escapes its root')
    return path


def read_json(path):
    if path.stat().st_size>MAX_MANIFEST:raise ValueError('Recovery manifest is too large')
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise ValueError('Manifest must be a mapping')
    return value


def verify(bundle):
    root=Path(bundle).resolve();failures=[];checked=0
    manifest=safe_file(root,'recovery-inventory.json')
    value=read_json(manifest)
    if value.get('schema')!=1 or value.get('status')!='complete':raise ValueError('Bundle requires a complete schema 1 inventory')
    assets=value.get('assets');code=value.get('application_files')
    if not isinstance(assets,list) or not isinstance(code,list) or not assets or not code or len(assets)+len(code)>10000:raise ValueError('Invalid asset/application inventory')
    expected={};main=[]
    for row in assets:
        if not isinstance(row,dict) or not str(row.get('logical','')).startswith('_configuration/'):raise ValueError('Invalid configuration inventory entry')
        if row.get('kind')=='main configuration':main.append(row)
    if len(main)!=1:raise ValueError('Exactly one main configuration is required')
    for row in code:
        if not isinstance(row,dict) or not str(row.get('path','')).startswith('application/'):raise ValueError('Invalid application inventory entry')
    extra=value.get('support_files',[])
    if not isinstance(extra,list) or any(not isinstance(r,dict) or r.get('path') not in ('RECOVER.txt',) for r in extra):raise ValueError('Invalid support inventory')
    for row in [*assets,*code,*extra]:
        name=row.get('logical',row.get('path'));path=safe_file(root,name);key=os.path.normcase(name).casefold()
        if key in expected or not re.fullmatch('[0-9a-f]{64}',str(row.get('sha256',''))):raise ValueError('Duplicate path or invalid digest')
        expected[key]=name
        try:
            if not path.is_file() or digest(path)!=row['sha256']:failures.append({'file':name,'problem':'missing or hash mismatch'})
            else:checked+=1
        except OSError:failures.append({'file':name,'problem':'unreadable'})
    if 'application/configbackup.py' not in expected or 'application/requirements.txt' not in expected:raise ValueError('Required application files are missing')
    for path in root.rglob('*'):
        if path.is_symlink():failures.append({'file':path.relative_to(root).as_posix(),'problem':'symlink'})
        elif path.is_file():
            name=path.relative_to(root).as_posix()
            if name.casefold() not in expected and name not in ('recovery-inventory.json','RECOVER.txt'):failures.append({'file':name,'problem':'unlisted file'})
    return {'status':'passed' if not failures else 'failed','files_verified':checked,'failures':failures,
            'trust_note':'Hashes detect missing/changed files. They are not a publisher signature; protect the inventory and obtain the release from a trusted copy.'},value


def require_verified(bundle):
    result,inventory=verify(bundle)
    if result['status']!='passed':raise ValueError('Bundle verification failed; no files restored')
    return Path(bundle).resolve(),inventory


def load_config(root,inventory):
    import yaml
    row=next(r for r in inventory['assets'] if r['kind']=='main configuration')
    value=yaml.safe_load(safe_file(root,row['logical']).read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict):raise ValueError('Main configuration must be a mapping')
    return row,value


def checklist(bundle):
    root,inventory=require_verified(bundle);_,config=load_config(root,inventory)
    launchers=[]
    for row in inventory['assets']:
        if row['kind']=='asset registry':launchers.extend(read_json(safe_file(root,row['logical'])).get('launchers',[]))
    references=[]
    for launcher in launchers:
        references.append({key:launcher.get(key,'') for key in ('platform','provider','vault','account','mode','task','secret_references')})
    connections=[]
    for name,profile in sorted((config.get('connections') or {}).items()):
        connections.append({'name':name,'engine':profile.get('engine'),'access_profile':profile.get('access_profile','unspecified'),
          'action':'Regenerate/review the read-access script, then test this connection and selected sections under the scheduled account.'})
    return {'environment_variable_names':inventory.get('environment_references',[]),'launcher_secret_references':references,
      'connections':connections,'python_packages':inventory.get('python_packages',{}),
      'required_review':[
       'Recreate credential values in the selected vault/environment; no credential values appear in this checklist.',
       'Record and recreate scheduler trigger, time zone, working directory, account and unattended logon requirements.',
       'Reapply filesystem/NTFS permissions, database grants and any required certificate trust.',
       'Install compatible Git, PostgreSQL clients, PowerShell/dbatools/SqlPackage and other selected collector tools.',
       'Regenerate launchers, enable reviewed tasks individually, validate and test access under the actual scheduled account.',
       'Use a fresh archive or restore the previous archive and matching internal state together while stopped.'],
      'limits':'Scheduler triggers, vault contents, grants, external tools and service data are not reconstructed by a setup export.'}


def replace_paths(value,mappings,changes,path='$'):
    if isinstance(value,dict):
        private={'password','token','secret','api_key','apikey','connection_string','private_key'}
        return {k:v if str(k).casefold() in private or str(k).casefold().endswith(('_password','_token','_secret','_key')) else replace_paths(v,mappings,changes,path+'.'+str(k)) for k,v in value.items()}
    if isinstance(value,list):return [replace_paths(v,mappings,changes,path+'['+str(i)+']') for i,v in enumerate(value)]
    if isinstance(value,str):
        for old,new in sorted(mappings.items(),key=lambda p:len(p[0]),reverse=True):
            # Never substitute inside SQL, shell command strings, URLs or secrets.
            if value==old or value.startswith(old.rstrip('/\\')+'/') or value.startswith(old.rstrip('/\\')+'\\'):
                tail=value[len(old):].lstrip('/\\').replace('\\','/')
                result=str(Path(new)/tail) if tail else str(new)
                changes.append({'field':path,'old_path':old,'new_path':str(new)})
                return result
    return value


def relocate(bundle,destination,mappings=None,python=None):
    import yaml
    from configbackup_lib.engine.configbackup import ConfigLoader
    from configbackup_lib.engine.startup_launcher import generate
    root,inventory=require_verified(bundle);main,config=load_config(root,inventory)
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():raise ValueError('Relocation destination must be new')
    if destination.resolve().is_relative_to(root):raise ValueError('Relocation must be outside the bundle')
    for row in inventory['assets']:
        source=Path(row['source'])
        if source.is_absolute() and (destination.resolve()==source.resolve() or destination.resolve().is_relative_to(source.resolve().parent)):
            raise ValueError('Relocation must be outside original configuration/asset directories')
    mappings=dict(mappings or {})
    if any(not isinstance(k,str) or not k or not isinstance(v,str) or not v for k,v in mappings.items()):raise ValueError('Path mappings require nonempty strings')
    destination.parent.mkdir(parents=True,exist_ok=True);stage=Path(tempfile.mkdtemp(prefix='.configbackup-relocate-',dir=destination.parent));changes=[]
    try:
        # Copy only verified entries and verify each copied hash again.
        for row in [*inventory['assets'],*inventory['application_files']]:
            name=row.get('logical',row.get('path'));target=stage/name;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            shutil.copyfile(safe_file(root,name),target);target.chmod(0o600)
            if digest(target)!=row['sha256']:raise ValueError('Bundle changed during relocation')
        source_app=inventory.get('application_source')
        if source_app:mappings[source_app]=str(destination/'application')
        for row in inventory['assets']:mappings[row['source']]=str(destination/row['logical'])
        config=replace_paths(config,mappings,changes)
        config.setdefault('backup',{})['root']=str(destination/'archive')
        config['internal']={'directory':'_configbackup','staging_directory':'staging','staging_root':str(destination/'staging')}
        config['git']={};config['monitoring']={'enabled':False};config['reporting']={'enabled':False}
        original_enabled=[]
        for task in config.get('tasks') or []:
            if task.get('enabled',True):original_enabled.append(task.get('name','unnamed'))
            task['enabled']=False;task['storage']='filesystem'
        config.setdefault('defaults',{})['storage']='filesystem'
        # All known assets are selected explicitly; old registry references are only preserved as evidence.
        config['backup']['include_related_configuration']=False
        config['backup']['configuration_files']=[str(destination/r['logical']) for r in inventory['assets'] if r['kind'] not in ('main configuration','asset registry','generated launcher/support file')]
        candidate=stage/'configbackup.yaml';candidate.write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8');candidate.chmod(0o600)
        notes=checklist(bundle);report={'status':'review_required','disabled_tasks':original_enabled,'path_changes':changes,
          'checklist':notes,'manual_review':['Review arbitrary command arguments, custom scripts, nested configuration contents and external absolute paths. Only complete path values/prefixes in the main YAML were mapped.','Reenable related configuration after registering/regenerating launchers.','Run managed diagnostics through the actual scheduled account; offline validation does not prove readiness.']}
        # Validate with the trusted running program, not code imported from the bundle.
        try:ConfigLoader(candidate).load();report['configuration_validation']='passed'
        except Exception as exc:report['configuration_validation']='failed';report['validation_error_type']=type(exc).__name__
        (stage/'REBUILD-CHECKLIST.json').write_text(json.dumps(notes,indent=2,sort_keys=True)+'\n')
        (stage/'RELOCATION.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
        # Publish a disabled candidate before generating scripts, so registered paths stay correct.
        stage.rename(destination)
        target_platform={'Darwin':'macos','Windows':'windows'}.get(platform.system(),'linux')
        launchers=[]
        for row in inventory['assets']:
            if row['kind']=='asset registry':launchers.extend(read_json(safe_file(root,row['logical'])).get('launchers',[]))
        report['launchers']=[]
        for number,metadata in enumerate(launchers):
            if metadata.get('platform')!=target_platform or 'provider' not in metadata:
                report['launchers'].append({'index':number,'status':'manual','reason':'Original platform/provider metadata unavailable or different'});continue
            try:
                files=generate(destination/'configbackup.yaml',destination/'launchers'/str(number),target_platform,python or sys.executable,
                    provider=metadata['provider'],secrets=metadata.get('secret_references',[]),vault=metadata.get('vault',''),account=metadata.get('account',''),
                    mode=metadata.get('mode','backup'),task=metadata.get('task',''),report_directory=str(destination/'runtime/diagnostics'),
                    shell=metadata.get('shell','powershell.exe'),execution_policy=metadata.get('execution_policy','inherit'),engine_path=destination/'application/configbackup.py')
                report['launchers'].append({'index':number,'status':'generated','files':[str(p.relative_to(destination)) for p in files]})
            except (ValueError,OSError) as exc:report['launchers'].append({'index':number,'status':'failed','error_type':type(exc).__name__})
        if report['configuration_validation']!='passed' or any(row['status']=='failed' for row in report['launchers']):report['status']='failed'
        config['backup']['configuration_files'] += [str(destination/name) for row in report['launchers'] if row['status']=='generated' for name in row['files']]
        if report['launchers'] and (destination/'configbackup.yaml.assets.json').is_file():config['backup']['configuration_files'].append(str(destination/'configbackup.yaml.assets.json'))
        (destination/'configbackup.yaml').write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
        (destination/'RELOCATION.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
        for p in destination.iterdir():
            if p.is_file():p.chmod(0o600)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)


def dependencies(destination,wheelhouse=None,download=False,execute=None):
    """Build a separate wheel bundle for this interpreter; never installs packages."""
    target=Path(destination).absolute()
    if target.exists():raise ValueError('Dependency bundle directory must be new')
    if bool(wheelhouse)==bool(download):raise ValueError('Choose one of an existing wheelhouse or download')
    target.parent.mkdir(parents=True,exist_ok=True);stage=Path(tempfile.mkdtemp(prefix='.configbackup-wheels-',dir=target.parent))
    try:
        wheels=stage/'wheels';wheels.mkdir(mode=0o700)
        if download:
            cmd=[sys.executable,'-m','pip','download','--only-binary=:all:','--dest',str(wheels),'-r',str(APPLICATION_ROOT.joinpath('requirements.txt')),'tzdata']
            if (execute or subprocess.run)(cmd,check=False,shell=False).returncode:raise ValueError('Wheel download failed; no bundle published')
        else:
            source=Path(wheelhouse).resolve()
            for p in sorted(source.glob('*.whl')):
                if p.is_symlink() or not p.is_file():raise ValueError('Wheel files must be regular files')
                shutil.copyfile(p,wheels/p.name)
        files=[{'path':'wheels/'+p.name,'sha256':digest(p)} for p in sorted(wheels.iterdir())]
        if not files:raise ValueError('No wheels found')
        result={'schema':1,'kind':'python-wheel-bundle','platform':platform.platform(),'system':platform.system(),'machine':platform.machine(),
                'python':platform.python_version(),'files':files,'compatibility':'Recorded host is the download/copy host, not proof that copied wheels match it. Use check-dependencies for an actual offline resolver check. External client installers are not included.'}
        shutil.copyfile(APPLICATION_ROOT.joinpath('requirements.txt'),stage/'requirements.txt');result['files'].append({'path':'requirements.txt','sha256':digest(stage/'requirements.txt')})
        (stage/'dependencies.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n');stage.rename(target);return result
    finally:
        if stage.exists():shutil.rmtree(stage)


def check_dependencies(bundle,execute=None):
    root=Path(bundle).resolve();data=read_json(safe_file(root,'dependencies.json'))
    if data.get('schema')!=1 or data.get('kind')!='python-wheel-bundle' or not isinstance(data.get('files'),list):raise ValueError('Invalid dependency bundle')
    seen=set()
    for row in data['files']:
        name=row['path'];p=safe_file(root,name)
        if name in seen or not (name=='requirements.txt' or name.startswith('wheels/') and name.endswith('.whl')):raise ValueError('Invalid dependency path')
        seen.add(name)
        if not p.is_file() or digest(p)!=row['sha256']:raise ValueError('Dependency hash mismatch')
    if 'requirements.txt' not in seen:raise ValueError('Missing pinned requirement inventory')
    trusted_requirements=APPLICATION_ROOT.joinpath('requirements.txt')
    if (root/'requirements.txt').read_bytes()!=trusted_requirements.read_bytes():raise ValueError('Requirements differ from the trusted running release; review separately')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual!=seen|{'dependencies.json'}:raise ValueError('Unlisted dependency files')
    # pip resolves current platform tags without installing or executing wheel code.
    with tempfile.TemporaryDirectory(prefix='configbackup-wheel-check-') as td:
        command=[sys.executable,'-m','pip','download','--no-index','--only-binary=:all:','--find-links',str(root/'wheels'),'-r',str(trusted_requirements),'tzdata','--dest',td]
        result=(execute or subprocess.run)(command,shell=False,check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return {'status':'passed' if result.returncode==0 else 'failed','hashes_verified':len(seen),'compatibility':'Offline resolver for the current Python/OS/architecture; external tools and runtime imports are separate checks.'}


def write_drill_telemetry(history,result):
    from configbackup_lib.reporting.telemetry import envelope,dataset
    payload=envelope('configbackup')
    age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(result['checked_at'])).total_seconds()/86400
    dataset(payload,'setup_recovery',[{'failed_count':int(result['status']!='passed'),'manual_launcher_count':result.get('manual_launcher_count',0),'checked_age_days':max(0,age)}],[],{'failed_count':'count','manual_launcher_count':'count','checked_age_days':'days'})
    metrics=history/'.telemetry.tmp';metrics.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n');metrics.chmod(0o600);os.replace(metrics,history/'telemetry.json')


def drill(bundle,history,interval_days=7,keep=12,force=False):
    from configbackup_lib.reporting.telemetry import envelope,dataset
    if not math.isfinite(interval_days) or interval_days<0 or keep<1 or keep>1000:raise ValueError('Invalid drill interval or retention')
    history=Path(history).resolve();root=Path(bundle).resolve()
    if history.is_relative_to(root) or root.is_relative_to(history):raise ValueError('Drill history and bundle must be separate directories')
    history.mkdir(parents=True,exist_ok=True,mode=0o700);lock=history/'.drill.lock'
    from configbackup_lib.engine.configbackup import single_instance_lock
    lock_context=single_instance_lock(lock);lock_context.__enter__()
    try:
        marker=history/'last-result.json';last=read_json(marker) if marker.exists() else {};now=dt.datetime.now(dt.timezone.utc)
        identity=str(root)
        # Verify every invocation: the expensive relocation rehearsal remains interval-based.
        try:
            current,inventory=verify(root)
            fingerprint=digest(root/'recovery-inventory.json') if current['status']=='passed' else None
        except (ValueError,OSError,KeyError,TypeError):
            current={'status':'failed'};fingerprint=None
        if not force and last.get('bundle')==identity and last.get('status')=='passed' and fingerprint and last.get('bundle_fingerprint')==fingerprint and 0<=(now-dt.datetime.fromisoformat(last['checked_at'])).total_seconds()<interval_days*86400:
            write_drill_telemetry(history,last)
            return {'status':'not_due','last_result':last}
        result={'schema':1,'kind':'configbackup-setup-drill','bundle':identity,'checked_at':now.isoformat(),'status':'failed','bundle_fingerprint':fingerprint}
        try:
            checked,inventory=verify(root);result['verification']=checked
            if checked['status']!='passed':raise ValueError('Verification failed')
            # Scratch is removed; no saved launcher or collector is ever executed.
            with tempfile.TemporaryDirectory(prefix='configbackup-setup-drill-') as td:
                dest=Path(td)/'candidate';relocation=relocate(root,dest)
                result['configuration_validation']=relocation['configuration_validation']
                if relocation['configuration_validation']!='passed':raise ValueError('Candidate configuration invalid')
                if any(r['status']=='failed' for r in relocation['launchers']):raise ValueError('Launcher regeneration failed')
                result['manual_launcher_count']=sum(r['status']=='manual' for r in relocation['launchers'])
            result['status']='passed'
        except (ValueError,OSError,KeyError,TypeError) as exc:result['error_type']=type(exc).__name__
        result['limits']='Offline file/hash/configuration and launcher-generation rehearsal only; no scheduler, credential, database access, external-tool execution or data/service recovery test.'
        identifier=now.strftime('%Y%m%dT%H%M%S%fZ');target=history/('drill-'+identifier+'.json')
        target.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n');target.chmod(0o600)
        temporary=history/'.last-result.tmp';temporary.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n');temporary.chmod(0o600);os.replace(temporary,marker)
        write_drill_telemetry(history,result)
        from configbackup_lib.engine.discovery_review import write_report
        write_report(result,history,'Setup recovery drill')
        files=sorted(p for p in history.glob('drill-*.json') if re.fullmatch(r'drill-\d{8}T\d{12}Z\.json',p.name) and not p.is_symlink())
        for p in files[:-keep]:
            old=read_json(p)
            if old.get('kind')=='configbackup-setup-drill':p.unlink()
        return result
    finally:lock_context.__exit__(None,None,None)


def wizard(input_fn=input):
    from configbackup_lib.ui.configure import choice,ask
    while True:
        action=choice('Setup recovery tools',['Verify bundle','Rebuild checklist','Relocate disabled candidate','Run recovery drill','Build offline wheel bundle','Check offline wheel bundle','Back'],input_fn)
        if action in (None,'Back'):return
        if action=='Build offline wheel bundle':
            source=ask('Existing wheel folder (blank downloads for this Python)',input_fn=input_fn)
            if not source and ask('Type DOWNLOAD to retrieve Python wheels',input_fn=input_fn)!='DOWNLOAD':continue
            result=dependencies(ask('New dependency bundle directory',input_fn=input_fn),source or None,not source)
        elif action=='Check offline wheel bundle':result=check_dependencies(ask('Dependency bundle directory',input_fn=input_fn))
        else:
            bundle=ask('Setup recovery bundle directory',input_fn=input_fn)
            if action=='Verify bundle':result=verify(bundle)[0]
            elif action=='Rebuild checklist':result=checklist(bundle)
            elif action=='Run recovery drill':result=drill(bundle,ask('Private drill history directory',input_fn=input_fn),force=True)
            else:
                mappings={}
                print('Map directory prefixes in the main YAML. Custom scripts and nested files need manual review. All tasks will be disabled.')
                while True:
                    old=ask('Old directory prefix (blank when done)',input_fn=input_fn)
                    if not old:break
                    mappings[old]=ask('New directory prefix',input_fn=input_fn)
                result=relocate(bundle,ask('New candidate directory outside original configuration folders',input_fn=input_fn),mappings)
        print(json.dumps(result,indent=2))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    for name in ('verify','checklist','relocate','drill'):
        p=sub.add_parser(name);p.add_argument('--bundle',required=True)
        if name=='relocate':p.add_argument('--destination',required=True);p.add_argument('--mappings',help='JSON mapping of old directory prefixes to new prefixes')
        if name=='drill':p.add_argument('--history',required=True);p.add_argument('--interval-days',type=float,default=7);p.add_argument('--keep',type=int,default=12);p.add_argument('--force',action='store_true')
    p=sub.add_parser('dependencies');p.add_argument('--destination',required=True);group=p.add_mutually_exclusive_group(required=True);group.add_argument('--wheelhouse');group.add_argument('--download',action='store_true')
    p=sub.add_parser('check-dependencies');p.add_argument('--bundle',required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='verify':result=verify(args.bundle)[0]
        elif args.action=='checklist':result=checklist(args.bundle)
        elif args.action=='relocate':result=relocate(args.bundle,args.destination,read_json(Path(args.mappings)) if args.mappings else None)
        elif args.action=='drill':result=drill(args.bundle,args.history,args.interval_days,args.keep,args.force)
        elif args.action=='dependencies':result=dependencies(args.destination,args.wheelhouse,args.download)
        else:result=check_dependencies(args.bundle)
        print(json.dumps(result,indent=2));return 6 if result.get('status')=='failed' else 0
    except (ValueError,OSError,KeyError,TypeError) as exc:print('Recovery setup failed: '+type(exc).__name__,file=sys.stderr);return 6

if __name__=='__main__':raise SystemExit(main())
