"""Generate launchers containing secret REFERENCES only, never credential values."""
from pathlib import Path
import os
import re
import shlex


def ps(value):return "'"+str(value).replace("'","''")+"'"


def generate(config,directory,platform_name,python,provider='existing',secrets=(),vault='',account=''):
    if platform_name not in ('windows','macos','linux'):raise ValueError('Unknown launcher platform')
    if provider not in ('existing','keychain','secret-service','powershell-vault'):raise ValueError('Unknown secret provider')
    if provider=='powershell-vault' and platform_name!='windows':raise ValueError('PowerShell vault launcher requires Windows')
    if provider=='keychain' and platform_name!='macos':raise ValueError('Keychain requires macOS')
    if provider=='secret-service' and platform_name!='linux':raise ValueError('Secret Service requires Linux')
    seen=set()
    for item in secrets:
        name=item.get('env','')
        if not re.fullmatch('[A-Z_][A-Z0-9_]*',name) or name in seen:raise ValueError('Unique uppercase environment variable names required')
        # Do not allow credential fields to replace executable search/config paths.
        if name in ('PATH','HOME','SHELL','BASH_ENV','ENV','PYTHONPATH','PYTHONHOME','PSMODULEPATH','COMSPEC','LD_PRELOAD','DYLD_INSERT_LIBRARIES'):raise ValueError('Reserved environment variable')
        if not item.get('name') or any(k not in ('env','name') for k in item):raise ValueError('Only env and secret name references are accepted')
        seen.add(name)
    if provider=='existing' and secrets:raise ValueError('Existing authentication does not accept secret mappings')
    if provider=='powershell-vault' and not vault:raise ValueError('A registered PowerShell vault name is required')
    if provider=='keychain' and not account:raise ValueError('Keychain account name required')
    config=Path(config).absolute();directory=Path(directory).absolute();directory.mkdir(parents=True,exist_ok=True)
    engine=Path(__file__).resolve().with_name('configbackup.py')
    files={}
    guidance=['Launchers contain references only. No passwords or tokens were requested or saved.',
      'Use the same OS account for setup and scheduled execution; unlock/access to the selected vault must work unattended.',
      'Git HTTPS: configure Git Credential Manager for this account. GitHub PRs: run gh auth login, then gh auth setup-git and gh auth status.',
      'Check gh auth status for the credential storage location: gh may fall back to a plaintext file when its secure store is unavailable.',
      'Do not echo a token, put it in command arguments, commit it, or enable shell/PowerShell tracing or transcription for secret retrieval.',
      'Environment variables exist in process memory and can be read by sufficiently privileged processes. Child collectors inherit them.',
      'No scheduler registration, vault creation, credential storage or backup execution was performed by the generator.']
    if platform_name=='windows':
        lines=["$ErrorActionPreference = 'Stop'","Set-PSDebug -Off","$previous = @{}","$result = 1","try {"]
        for item in secrets:
            name=ps(item['env']);secret=ps(item['name'])
            lines.extend([f"    $previous[{name}] = [Environment]::GetEnvironmentVariable({name}, 'Process')",
              f"    $value = Get-Secret -Name {secret} -Vault {ps(vault)} -AsPlainText -ErrorAction Stop",
              "    if ($value -isnot [string] -or [string]::IsNullOrEmpty($value)) { throw 'Secret is missing or is not a string' }",
              f"    [Environment]::SetEnvironmentVariable({name}, $value, 'Process')","    $value = $null"])
            guidance.extend([f'For {item["env"]}, use an already registered non-interactive vault:',
                '$value = Read-Host '+ps('Secret for '+item['name'])+' -AsSecureString',
                f'Set-Secret -Name {secret} -Vault {ps(vault)} -Secret $value', '$value = $null'])
        lines += [f"    Set-Location -LiteralPath {ps(config.parent)}",f"    & {ps(python)} {ps(engine)} --config {ps(config)}", "    $result = $LASTEXITCODE",
                  "} catch {", "    [Console]::Error.WriteLine('ConfigBackup startup failed; check paths and vault access without printing credentials.')", "} finally {",
                  "    foreach ($name in $previous.Keys) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }", "}", "exit $result"]
        files['run-configbackup.ps1']='\n'.join(lines)+'\n'
        files['run-configbackup.bat']='@echo off\r\nsetlocal DisableDelayedExpansion\r\npwsh -NoLogo -NoProfile -NonInteractive -File "%~dp0run-configbackup.ps1"\r\nexit /b %errorlevel%\r\n'
    else:
        q=shlex.quote;lines=['#!/bin/sh','set +x','set -eu','umask 077']
        for item in secrets:
            name=item['env']
            cmd=(['/usr/bin/security','find-generic-password','-a',account,'-s',item['name'],'-w'] if provider=='keychain'
                 else ['secret-tool','lookup','application','ConfigBackup','name',item['name']])
            lines += [name+'=$('+shlex.join(cmd)+" 2>/dev/null) || { printf '%s\\n' 'Credential lookup failed' >&2; exit 1; }",
                      '[ -n "$'+name+'" ] || { printf \'%s\\n\' \'Credential is empty\' >&2; exit 1; }','export '+name]
            if provider=='keychain':
                guidance.append('Add a generic password in Keychain Access with account '+account+' and service '+item['name']+'. Enter its value in the password field; never supply it as a command-line argument.')
            else:guidance.append('Store interactively (enter the secret at the prompt): '+shlex.join(['secret-tool','store','--label=ConfigBackup '+item['name'],'application','ConfigBackup','name',item['name']]))
        lines+=['cd '+q(str(config.parent)),'exec '+shlex.join([python,str(engine),'--config',str(config)])]
        files['run-configbackup.sh']='\n'.join(lines)+'\n'
    files['AUTH-SETUP.txt']='\n\n'.join(guidance)+'\n'
    if any((directory/name).exists() for name in files):raise ValueError('Launcher files already exist; choose a fresh directory')
    created=[]
    try:
        for name,content in files.items():
            path=directory/name
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o700 if name.endswith('.sh') else 0o600)
            with os.fdopen(fd,'w',newline='') as stream:stream.write(content)
            created.append(path)
    except Exception:
        for path in created:path.unlink(missing_ok=True)
        raise
    return created
