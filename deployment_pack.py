"""Build and verify a hash-pinned, current-platform Python/application rebuild pack.

Native tools (PowerShell, DacFX, database clients, Git, sysstat) are inventoried by
setup diagnostics separately and must be installed through approved OS packages.
"""
from __future__ import annotations
import argparse
import email
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile


def fingerprint():
    return {'system':platform.system(),'machine':platform.machine().lower(),'python':f'{sys.version_info.major}.{sys.version_info.minor}','implementation':platform.python_implementation()}


def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):result.update(chunk)
    return result.hexdigest()


def dependencies(features):
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name
    root=Path(__file__).resolve().parent
    requirements=['requirements.txt']+[f'requirements-{name}.txt' for name in sorted(features)]
    pending=[Requirement(line) for name in requirements for line in (root/name).read_text().splitlines() if line.strip() and not line.startswith('#')]
    result={}
    while pending:
        req=pending.pop()
        if req.marker and not req.marker.evaluate({'extra':''}):continue
        if req.url or req.extras:raise ValueError('URL and extra dependency requirements need manual packaging review')
        name=canonicalize_name(req.name);dist=importlib.metadata.distribution(name)
        if dist.version not in req.specifier:raise ValueError('Installed dependency does not satisfy application requirements: '+name)
        if name in result:continue
        result[name]=dist.version
        pending.extend(Requirement(value) for value in dist.requires or [])
    return dict(sorted(result.items()))


def build(output,features=(),*,execute=subprocess.run):
    if set(features)-{'encryption','performance','console'}:raise ValueError('Unknown optional deployment feature')
    output=Path(output).absolute()
    if output.exists() or output.is_symlink():raise ValueError('Deployment pack output must be new')
    pins=dependencies(features);output.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.deployment-',dir=output.parent))
    try:
        wheels=stage/'wheels';wheels.mkdir();args=[sys.executable,'-m','pip','download','--disable-pip-version-check','--only-binary=:all:','--no-deps','--dest',str(wheels),*[k+'=='+v for k,v in pins.items()]]
        execute(args,check=True)
        found={};lock=[]
        for path in sorted(wheels.iterdir()):
            if path.suffix!='.whl' or path.is_symlink():raise ValueError('Only binary wheels are accepted')
            with zipfile.ZipFile(path) as archive:
                names=[n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
                if len(names)!=1:raise ValueError('Invalid wheel metadata')
                metadata=email.message_from_bytes(archive.read(names[0]))
            from pip._vendor.packaging.utils import canonicalize_name,parse_wheel_filename
            from pip._vendor.packaging.tags import sys_tags
            name=canonicalize_name(metadata['Name']);version=metadata['Version'];_,_,_,tags=parse_wheel_filename(path.name)
            if not tags.intersection(set(sys_tags())) or pins.get(name)!=version or name in found:raise ValueError('Wrong, duplicate, or incompatible wheel')
            found[name]=version;lock.append(name+'=='+version+' --hash=sha256:'+digest(path))
        if found!=pins:raise ValueError('Downloaded wheel inventory does not match pinned dependencies')
        (stage/'requirements.lock').write_text('\n'.join(sorted(lock))+'\n',encoding='utf-8')
        from recovery_assets import distribution_files
        for relative,source in distribution_files():
            target=stage/'application'/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
        shutil.copy2(__file__,stage/'install.py')
        inventory={p.relative_to(stage).as_posix():digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        manifest={'schema':1,'platform':fingerprint(),'features':sorted(features),'dependencies':pins,'files':inventory,
                  'limits':'Current OS, architecture and Python minor version only. No native installers, credentials, configuration, archive state, or database data. Verify the separately delivered pack digest before running install.py. A successful Python rebuild does not verify native tools or live services.'}
        (stage/'deployment.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n',encoding='utf-8')
        verify(stage);os.replace(stage,output);return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)


def verify(root,require_platform=False):
    root=Path(root);manifest=root/'deployment.json'
    if manifest.is_symlink() or manifest.stat().st_size>16*1024*1024:raise ValueError('Unsafe deployment manifest')
    value=json.loads(manifest.read_text(encoding='utf-8'))
    if value.get('schema')!=1 or not isinstance(value.get('files'),dict) or not value['files']:raise ValueError('Invalid deployment manifest')
    if require_platform and value['platform']!=fingerprint():raise ValueError('Pack platform/Python differs; build on the target platform')
    if not {'install.py','requirements.lock','application/configbackup.py','application/requirements.txt'}.issubset(value['files']):raise ValueError('Required deployment files missing')
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=manifest}
    if actual!=set(value['files']):raise ValueError('Deployment inventory mismatch')
    for name,expected in value['files'].items():
        path=Path(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or any(p.is_symlink() for p in [root/path,*(root/path).parents] if p==root or root in p.parents):raise ValueError('Unsafe deployment path')
        if not isinstance(expected,str) or len(expected)!=64 or digest(root/path)!=expected:raise ValueError('Deployment hash mismatch: '+name)
    return value


def install(root,destination):
    root=Path(root).resolve();value=verify(root,True);destination=Path(destination).resolve()
    if destination.exists() or destination.is_symlink():raise ValueError('Rebuild requires a new virtual-environment directory')
    if destination==root or root in destination.parents:raise ValueError('Rebuild environment must be outside the immutable pack')
    venv.EnvBuilder(with_pip=True).create(destination)
    python=destination/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check','--no-index','--no-deps','--require-hashes','--find-links',str(root/'wheels'),'-r',str(root/'requirements.lock')],check=True)
    subprocess.run([str(python),'-m','pip','check'],check=True)
    probe=subprocess.run([str(python),'-c','import importlib.metadata,json,sys;print(json.dumps({n:importlib.metadata.version(n) for n in sys.argv[1:]}))',*value['dependencies']],capture_output=True,text=True,check=True)
    if json.loads(probe.stdout)!=value['dependencies']:raise ValueError('Rebuilt versions differ from bill of materials')
    # Runtime imports may create bytecode; keep it out of the immutable pack.
    application=destination/'application'
    shutil.copytree(root/'application',application)
    for source in sorted((root/'application').rglob('*')):
        if source.is_file() and digest(source)!=digest(application/source.relative_to(root/'application')):raise ValueError('Installed application copy differs')
    return {'application':str(application),'status':'passed','python':str(python),'platform':fingerprint(),'dependencies':value['dependencies'],'limits':value['limits']}


def wizard(input_fn=input):
    from configure import choice,ask,boolean
    action=choice('Offline deployment pack',['Build for this platform','Verify pack','Rebuild in new environment','Back'],input_fn)
    if action in (None,'Back'):return
    if action=='Build for this platform':
        features=[name for name in ('encryption','performance','console') if boolean('Include optional '+name+' dependencies',False,input_fn)]
        print('Downloads exact installed dependency versions as binary wheels. Does not install OS tools.')
        target=ask('New pack directory',input_fn=input_fn)
        result=input_fn.run_external(lambda:build(target,features)) if getattr(input_fn,'console_ui',False) else build(target,features)
    elif action=='Verify pack':result=verify(ask('Pack directory',input_fn=input_fn))
    else:
        source=ask('Pack directory',input_fn=input_fn);target=ask('New virtual-environment directory',input_fn=input_fn)
        result=input_fn.run_external(lambda:install(source,target)) if getattr(input_fn,'console_ui',False) else install(source,target)
    print(json.dumps(result,indent=2))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['build','verify','install']);parser.add_argument('--pack',required=True);parser.add_argument('--venv');parser.add_argument('--feature',action='append',default=[],choices=['encryption','performance','console']);args=parser.parse_args(argv)
    if args.action=='install' and not args.venv:parser.error('--venv is required for install')
    result=build(args.pack,args.feature) if args.action=='build' else verify(args.pack) if args.action=='verify' else install(args.pack,args.venv)
    print(json.dumps(result,sort_keys=True,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
