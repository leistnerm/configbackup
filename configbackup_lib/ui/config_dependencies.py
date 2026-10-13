"""Dependency diagnostics and explicitly approved, bounded package updates."""
from __future__ import annotations
import importlib.metadata
import importlib.util
import subprocess
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PACKAGES={
    'PyYAML':('yaml','PyYAML>=6.0,<7',(6,0),(7,0)),
    'ruamel.yaml':('ruamel','ruamel.yaml>=0.18.6,<0.19',(0,18,6),(0,19)),
    'tzdata':('tzdata','tzdata>=2024.1',(2024,1),None),
    'prompt_toolkit':('prompt_toolkit','prompt_toolkit>=3.0.52,<4',(3,0,52),(4,0)),
}


def inspect():
    rows=[]
    for name,(module,spec,minimum,maximum) in PACKAGES.items():
        try:version=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:version=None
        installed=version is not None and importlib.util.find_spec(module) is not None
        try:
            from pip._vendor.packaging.version import Version
            supported=installed and Version(version)>=Version('.'.join(map(str,minimum))) and (maximum is None or Version(version)<Version('.'.join(map(str,maximum))))
        except ImportError:supported=installed
        status='ready' if supported else 'missing' if not installed else 'unsupported version'
        detail=version or 'not installed'
        if name=='tzdata':
            try:ZoneInfo('UTC');ZoneInfo('America/New_York');available=True
            except ZoneInfoNotFoundError:available=False
            if available:status='ready';detail=version or 'system IANA timezone database available'
            else:status='timezone data unavailable';detail='Even UTC or America/New_York cannot be loaded; install tzdata in this Python environment.'
        rows.append({'name':name,'status':status,'detail':detail,'requirement':spec})
    return rows


def command(names,update=False):
    if not names or any(name not in PACKAGES for name in names):raise ValueError('Choose listed dependency names')
    return [sys.executable,'-m','pip','install',*(['--upgrade'] if update else []),*[PACKAGES[name][1] for name in names]]


def install(names,update=False,input_fn=input,execute=None):
    args=command(names,update)
    print('Python environment: '+sys.executable)
    print('Command: '+subprocess.list2cmdline(args))
    if input_fn('Type INSTALL to run this command (anything else cancels): ').strip()!='INSTALL':return False
    run=execute or subprocess.run
    action=lambda:run(args,check=False)
    result=input_fn.run_external(action) if getattr(input_fn,'console_ui',False) else action()
    if result.returncode:print('Installation failed. Review the installer output; use the same Python environment.');return False
    print('Packages installed. Restart the configurator before editing or saving to load the updated versions.')
    return True


def wizard(input_fn=input):
    from configbackup_lib.ui.configure import choice
    from configbackup_lib.ui.configure_features import select_many
    while True:
        rows=inspect()
        print('Dependency checks for '+sys.executable)
        for row in rows:print(f"  {row['name']}: {row['status']} — {row['detail']}")
        action=choice('Python dependencies',['Install missing/unsupported dependencies','Update selected dependencies','Recheck','Back'],input_fn)
        if action in (None,'Back'):return
        if action=='Recheck':continue
        missing=[r['name'] for r in rows if r['status']!='ready']
        names=select_many('Packages to '+('update' if action.startswith('Update') else 'install'),list(PACKAGES),[] if action.startswith('Update') else missing,input_fn)
        if names:install(names,update=True,input_fn=input_fn)
