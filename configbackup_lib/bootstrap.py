"""CLI bootstrap with an explicit repair path for missing core dependencies."""
import importlib
import os
import subprocess
import sys
from configbackup_lib import APPLICATION_ROOT


def run(module):
    try:
        implementation=importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name not in ('yaml','ruamel','ruamel.yaml'):
            raise
        command=[sys.executable,'-m','pip','install','-r',str(APPLICATION_ROOT/'requirements.txt')]
        print('Missing core Python dependency. Use this Python environment:')
        print(subprocess.list2cmdline(command))
        if sys.stdin.isatty() and input('Type INSTALL to install core dependencies and restart (anything else cancels): ').strip()=='INSTALL':
            if subprocess.run(command,check=False).returncode==0:os.execv(sys.executable,[sys.executable,*sys.argv])
        return 2
    return implementation.main()
