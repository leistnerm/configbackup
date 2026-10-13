"""Explicit ConfigBackup setup assets, independent history and portable export."""
from __future__ import annotations
from configbackup_lib import APPLICATION_ROOT
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

REGISTRY_SUFFIX='.assets.json'
KNOWN_INPUTS={'analyze_schedules.py':{'--config'},'collect_system.py':{'--registry-config','--smart-config'},
              'Invoke-ProfileCollection.ps1':{'-profilefile'},'Test-CollectionAccess.ps1':{'-profilefile'}}
MAX_ASSET_BYTES=50*1024*1024
RECOVER_TEXT='ConfigBackup setup recovery export\n\n1. Keep this folder private; configuration and scripts may contain inline secrets.\n2. Install an approved Python 3.10+ and run: python -m venv NEW_VENV\n3. Use that environment to install application/requirements.txt. The export contains no installers or wheels.\n4. Copy the original configuration and related assets from _configuration; the inventory maps original paths to stored paths and hashes.\n5. Edit all machine-specific paths/endpoints in the saved configuration. Recreate required environment/vault credentials and account permissions.\n6. Run application/configure.py --config NEW_CONFIG --check, then --autodetect. Use the menu to generate new launchers and run connection/setup tests under the actual scheduled account.\n7. Start with a fresh archive, or restore an existing archive AND its matching internal state together while stopped. This folder is not an archive/state or database-data backup.\n8. Validate, dry-run, inspect recovery coverage, then configure the scheduler deliberately.\n\nOriginal generated launchers are preserved for reference; their absolute paths usually need regeneration. The copied asset registry also contains original paths; review it or regenerate it on the new host.\n'


def registry_path(config):return Path(str(Path(config).resolve())+REGISTRY_SUFFIX)


def registry(config):
    path=registry_path(config)
    if not path.exists():return {'schema':1,'files':[],'launchers':[]}
    if path.stat().st_size>1024*1024:raise ValueError('Asset registry exceeds 1 MiB')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('files'),list) or not isinstance(data.get('launchers'),list):raise ValueError('Invalid asset registry')
    if len(data['files'])>1000 or any(not isinstance(p,str) or not Path(p).is_absolute() for p in data['files']):raise ValueError('Asset registry requires at most 1000 absolute file paths')
    return data


def register_launcher(config,files,metadata):
    """Register references, never secret values; lock prevents lost concurrent adds."""
    target=registry_path(config);target.parent.mkdir(parents=True,exist_ok=True)
    lock=target.with_name(target.name+'.lock')
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
    temporary=None
    try:
        data=registry(config)
        data['files']=sorted(set(data['files'])|{str(Path(p).resolve()) for p in files})
        if len(data['files'])>1000:raise ValueError('Asset registry supports at most 1000 files')
        if metadata not in data['launchers']:data['launchers'].append(metadata)
        fd,name=tempfile.mkstemp(prefix='.'+target.name+'.',dir=target.parent);temporary=Path(name)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(data,stream,indent=2,sort_keys=True);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,target)
    finally:
        if temporary:temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def register_existing(config,directory):
    directory=Path(directory).resolve()
    files=[directory/name for name in ('run-configbackup.sh','run-configbackup.bat','run-configbackup.ps1','AUTH-SETUP.txt') if (directory/name).is_file()]
    if not any(p.suffix in ('.sh','.bat','.ps1') for p in files):raise ValueError('No standard generated launcher found; add custom scripts with configuration_files instead')
    register_launcher(config,files,{'directory':str(directory),'mode':'existing launcher registration'})
    return files


def resolve_file(value,base,variables):
    from configbackup_lib.engine.configbackup import expand_string,ConfigError
    if not isinstance(value,str) or not value.strip():raise ValueError('Configuration asset must be a nonempty file path')
    try:value=expand_string(value,variables,strict=True)
    except ConfigError as exc:raise ValueError(str(exc)) from exc
    path=Path(value).expanduser()
    return (path if path.is_absolute() else base/path).resolve()


def assets(config,path):
    """Only known input flags, registered launchers and explicit additional files."""
    if not isinstance(config,dict):raise ValueError('Configuration must be a mapping')
    path=Path(path).resolve();backup=config.get('backup') or {};variables=config.get('variables') or {}
    if not isinstance(backup,dict) or not isinstance(variables,dict):raise ValueError('backup and variables must be mappings')
    for name in ('include_configuration','include_related_configuration'):
        if name in backup and not isinstance(backup[name],bool):raise ValueError(name+' must be a boolean')
    extra=backup.get('configuration_files',[])
    if not isinstance(extra,list) or len(extra)>1000:raise ValueError('configuration_files supports at most 1000 entries')
    for item in extra:
        if isinstance(item,dict):
            if not isinstance(item.get('path'),str) or not item['path'].strip() or not isinstance(item.get('enabled',True),bool):raise ValueError('File entries need path and boolean enabled')
        elif not isinstance(item,str) or not item.strip():raise ValueError('Configuration file must be a path or mapping')
    rows={str(path):{'source':str(path),'logical':'_configuration/'+path.name,'kind':'main configuration'}};errors=[]
    def add(value,kind,base=None):
        try:
            p=resolve_file(value,base or path.parent,variables);key=str(p)
            if key not in rows:
                ident=hashlib.sha256(os.path.normcase(key).encode()).hexdigest()[:20]
                rows[key]={'source':key,'logical':'_configuration/related/'+ident+'/'+p.name,'kind':kind}
        except (OSError,ValueError) as exc:errors.append({'kind':kind,'error':str(exc)})
    if backup.get('include_related_configuration',True):
        for task in config.get('tasks') or []:
            if not isinstance(task,dict):raise ValueError('Tasks must contain mappings')
            arguments=task.get('arguments',[])
            if not isinstance(arguments,list):continue
            # Recognize script identity, never execute it or parse arbitrary shells.
            scripts={Path(str(arg).replace('\\','/')).name for arg in arguments}
            flags=set().union(*({key.casefold():value for key,value in KNOWN_INPUTS.items()}.get(script.casefold(),set()) for script in scripts))
            base=Path.cwd()
            if task.get('working_directory'):
                try:base=resolve_file(task['working_directory'],Path.cwd(),variables)
                except (OSError,ValueError):errors.append({'kind':'task '+str(task.get('name')),'error':'Unresolved working_directory'});continue
            for n,arg in enumerate(arguments):
                if not isinstance(arg,str):continue
                key,sep,value=arg.partition('=')
                if key.lower() in flags:
                    value=value if sep else arguments[n+1] if n+1<len(arguments) else ''
                    add(value,'referenced configuration',base)
        reg=registry_path(path)
        if reg.exists():
            add(str(reg),'asset registry')
            try:
                for value in registry(path)['files']:add(value,'generated launcher/support file')
            except (OSError,ValueError) as exc:errors.append({'kind':'asset registry','error':str(exc)})
    for value in extra:
        if isinstance(value,dict):
            if not value.get('enabled',True):continue
            value=value['path']
        add(value,'explicit additional file')
    return sorted(rows.values(),key=lambda row:row['logical']),errors


def automatic_tasks(config,path):
    rows,errors=assets(config,path)
    common={'storage':'filesystem','enabled':True,'required':False,'phase':'backup','depends_on':[],
            'include':[],'exclude':[],'sections':{},'collection_manifest':False,'run_on_failure':True,
            'retention':{'active':{'mode':'simple','min_versions':3,'max_versions':30}},'deletion':{'enabled':False}}
    tasks=[]
    for row in rows:
        name='configbackup-settings' if row['kind']=='main configuration' else 'configbackup-asset-'+row['logical'].split('/')[2]
        tasks.append({**common,'name':name,'type':'file','source':row['source'],'destination':row['logical']})
    # Inventory generation is its own failure boundary; missing assets do not veto others.
    tasks.append({**common,'name':'configbackup-recovery-inventory','type':'command','executable':sys.executable,
                  'arguments':[str((APPLICATION_ROOT / 'recovery_assets.py')),'--config',str(Path(path).resolve())],
                  'output':'_configuration/recovery-inventory.json','timeout':60})
    return tasks


def inventory(config,path,include_application=False):
    from configbackup_lib.engine.configbackup import APP_VERSION,expand_string,ConfigError
    rows,errors=assets(config,path)
    for row in rows:
        try:
            source=Path(row['source'])
            if not source.is_file():raise ValueError('Missing or not a regular file')
            if source.stat().st_size>MAX_ASSET_BYTES:raise ValueError('Asset exceeds 50 MiB recovery export limit')
            data=source.read_bytes();row.update(status='available',bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
        except (OSError,ValueError) as exc:row.update(status='unavailable',error=str(exc))
    requirements={}
    for name in ('PyYAML','ruamel.yaml','tzdata','cryptography'):
        try:requirements[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:requirements[name]='not installed'
    env=set()
    def refs(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if str(key).endswith('_env') and isinstance(item,str) and item:env.add(item)
                refs(item)
        elif isinstance(value,list):
            for item in value:refs(item)
    refs(config)
    locations={}
    for key,value in [('archive',(config.get('backup') or {}).get('root')),('git',(config.get('git') or {}).get('repository'))]:
        if value:
            try:locations[key]=expand_string(str(value),config.get('variables') or {},strict=True)
            except ConfigError:locations[key]='unresolved; inspect saved configuration'
    result={'schema':1,'application_version':APP_VERSION,'python_version':'.'.join(map(str,sys.version_info[:3])),
            'python_packages':requirements,'assets':rows,'discovery_errors':errors,'environment_references':sorted(env),
            'existing_storage_locations':locations,'status':'partial' if errors or any(r['status']!='available' for r in rows) else 'complete',
            'coverage_note':'Complete means all known selected files are available; unregistered custom launchers and unrecognized command inputs are not discovered.',
            'separate_recovery_requirements':['Credential values/vaults, certificates with private keys and authentication accounts are not exported automatically.',
            'Existing archive and matching internal state must be copied together while ConfigBackup is stopped; this export does not contain their history.',
            'Restore filesystem/NTFS permissions, scheduler identity/triggers, database grants and external client tools separately.',
            'Review absolute paths and regenerate launchers for the new installation before running a backup.']}

    if include_application:
        result['application_files']=[{'path':'application/'+relative.as_posix(),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for relative,p in distribution_files()]
        result['application_source']=str(APPLICATION_ROOT)
        result['support_files']=[{'path':'RECOVER.txt','sha256':hashlib.sha256(RECOVER_TEXT.encode('utf-8')).hexdigest()}]
    return result


def distribution_files():
    source_root=APPLICATION_ROOT
    names=['requirements.txt','requirements-encryption.txt','requirements-performance.txt',*[p.name for p in source_root.glob('*.py')],'requirements-console.txt','configbackup_lib','collectors','docs','examples']
    for name in sorted(set(names)):
        item=source_root/name
        for p in sorted(item.rglob('*') if item.is_dir() else [item]):
            if not p.is_file() or p.is_symlink() or '__pycache__' in p.parts or p.suffix not in ('.py','.ps1','.md','.yaml','.json','.sql','.txt','.html','.sh','.bat','.cmd','.psm1'):continue
            yield p.relative_to(source_root),p


def export_recovery(config,path,destination):
    """New private directory, publish only after every selected asset verifies."""
    source_root=APPLICATION_ROOT;destination=Path(destination).absolute()
    if destination.exists():raise ValueError('Recovery export destination must be new')
    rows,errors=assets(config,path)
    if destination.resolve().is_relative_to(source_root) or any(destination.resolve()==Path(r['source']).resolve() for r in rows):raise ValueError('Recovery export must be outside the application and source files')
    result=inventory(config,path)
    if result['status']!='complete':raise ValueError('Recovery inventory is partial; fix unavailable assets before export')
    destination.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.configbackup-recovery-',dir=destination.parent))
    try:
        for row in result['assets']:
            target=staging/row['logical'];target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            data=Path(row['source']).read_bytes()
            if hashlib.sha256(data).hexdigest()!=row['sha256']:raise ValueError('Asset changed during export')
            target.write_bytes(data);target.chmod(0o600)
        # Fixed distribution allowlist avoids copying adjacent user configs or credentials.
        code=staging/'application';code.mkdir(mode=0o700)
        code_files=[]
        for relative,p in distribution_files():
            target=code/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            data=p.read_bytes();target.write_bytes(data);target.chmod(0o600)
            code_files.append({'path':'application/'+relative.as_posix(),'sha256':hashlib.sha256(data).hexdigest()})
        result['application_files']=code_files
        result['application_source']=str(source_root)
        (staging/'recovery-inventory.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        (staging/'RECOVER.txt').write_text(RECOVER_TEXT,encoding='utf-8')
        result['support_files']=[{'path':'RECOVER.txt','sha256':hashlib.sha256((staging/'RECOVER.txt').read_bytes()).hexdigest()}]
        (staging/'recovery-inventory.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        for p in staging.iterdir():
            if p.is_file():p.chmod(0o600)
        staging.rename(destination)
    finally:
        if staging.exists():shutil.rmtree(staging)
    return result


def wizard(config,path,input_fn=input):
    from configbackup_lib.ui.configure import choice,ask,boolean
    while True:
        action=choice('ConfigBackup recovery files',['Show recovery inventory','Add configuration/script file','Register existing launcher folder','Remove additional file','Enable/disable additional file','Include related files','Export recovery directory','Back'],input_fn)
        if action in (None,'Back'):return
        backup=config.setdefault('backup',{})
        if action=='Show recovery inventory':print(json.dumps(inventory(config,path),indent=2))
        elif action=='Add configuration/script file':
            value=ask('Explicit file path (relative to configuration directory)',input_fn=input_fn)
            if value and value not in backup.setdefault('configuration_files',[]):backup['configuration_files'].append(value)
        elif action=='Register existing launcher folder':
            files=register_existing(path,ask('Existing generated-launcher directory',input_fn=input_fn));print('Registered for backup: '+', '.join(str(p) for p in files))
        elif action=='Remove additional file':
            items=backup.setdefault('configuration_files',[])
            if items:
                value=choice('File',items,input_fn)
                if value:items.remove(value)
        elif action=='Enable/disable additional file':
            items=backup.setdefault('configuration_files',[])
            if items:
                labels=[item if isinstance(item,str) else item['path'] for item in items]
                selected=choice('File to toggle',labels,input_fn)
                if selected:
                    index=labels.index(selected);item=items[index]
                    items[index]={'path':item,'enabled':False} if isinstance(item,str) else {**item,'enabled':not item.get('enabled',True)}
        elif action=='Include related files':backup['include_related_configuration']=boolean('Back up known referenced configurations and registered launchers',backup.get('include_related_configuration',True),input_fn)
        else:
            print('Export uses saved configuration bytes; save edits before exporting. It contains private configuration and program files, not archive/state history or vault credentials.')
            import yaml
            saved=yaml.safe_load(Path(path).read_text(encoding='utf-8')) or {}
            export_recovery(saved,path,ask('New private recovery directory',input_fn=input_fn));print('Recovery export created.')


def main():
    import yaml
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=yaml.safe_load(Path(args.config).read_text(encoding='utf-8')) or {}
    value=inventory(config,args.config);print(json.dumps(value,indent=2,sort_keys=True))
    # Preserve the last complete inventory when current inventory is incomplete.
    return 0 if value['status']=='complete' else 6

if __name__=='__main__':raise SystemExit(main())
