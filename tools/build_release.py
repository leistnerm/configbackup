#!/usr/bin/env python3
"""Build repeatable application, GPO harness and workstation kit archives."""
from __future__ import annotations
import argparse
import ast
import subprocess
import hashlib
import json
from pathlib import Path
import re
import stat
import zipfile
GPO_VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parents[1]

def application_version(root):
    tree=ast.parse((root/'configbackup_lib/engine/configbackup.py').read_text(encoding='utf-8'))
    values=[node.value.value for node in tree.body if isinstance(node,ast.Assign)
            and any(isinstance(target,ast.Name) and target.id=='APP_VERSION' for target in node.targets)
            and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str)]
    if len(values)!=1 or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',values[0]):
        raise ValueError('Expected one semantic APP_VERSION in the engine')
    return values[0]

VERSION=application_version(ROOT)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def files(root, excluded=()):
    result = {}
    # The index is the release source manifest. Never include untracked local data.
    result_run=subprocess.run(['git','-C',str(root),'ls-files','--cached','-z','--','.'],check=True,capture_output=True)
    for name in sorted(set(result_run.stdout.decode('utf-8').split('\0'))-{''}):
        rel=Path(name);path=root/rel
        if rel.is_absolute() or '..' in rel.parts:raise ValueError('Unsafe tracked release path')
        if any(part in {'.git', '__pycache__', '.venv', 'venv', 'dist', 'private'} for part in rel.parts) or rel.parts[0] in excluded:
            continue
        if path.is_symlink():
            raise ValueError(f'Release source contains a symbolic link: {rel}')
        if not path.is_file():raise ValueError(f'Tracked release file is missing or not regular: {rel}')
        if path.name != '.DS_Store' and path.suffix not in {'.pyc', '.pyo'}:
            result[rel.as_posix()] = path.read_bytes()
    return result

def checksums(data):
    return ''.join(f'{sha(value)}  {name}\n' for name, value in sorted(data.items()) if name != 'CHECKSUMS.sha256').encode()

def archive(output, name, data):
    def write(target):
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for member, value in sorted(data.items()):
                info = zipfile.ZipInfo(f'{name}/{member}', date_time=(2026, 10, 12 if name == f'configbackup-gpo-test-{GPO_VERSION}' else 13, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if member.endswith('.sh') else 0o644)) << 16
                z.writestr(info, value, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    target = output / (name + '.zip')
    repeat = output / (name + '.repeat.zip')
    write(target)
    write(repeat)
    if sha(target.read_bytes()) != sha(repeat.read_bytes()):
        raise RuntimeError('Repeated archive build differed')
    repeat.unlink()
    with zipfile.ZipFile(target) as z:
        if z.testzip() is not None:
            raise RuntimeError('Archive CRC validation failed')
    target.with_suffix('.zip.sha256').write_text(f'{sha(target.read_bytes())}  {target.name}\n', encoding='utf-8')
    print(f'{target.name}: {len(data)} files; sha256 {sha(target.read_bytes())}')
    return target

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    args = parser.parse_args()
    output = args.output.resolve()
    if output != ROOT/'dist' and (output == ROOT or ROOT in output.parents):
        parser.error('Use repository dist/ or an output directory outside the repository')
    output.mkdir(parents=True, exist_ok=True)
    app = archive(output, f'configbackup-{VERSION}', files(ROOT, {'windows-test-kit', 'tools', '.gitignore'}))
    gpo_files = files(ROOT/'tools/gpo-test')
    gpo_files['CHECKSUMS.sha256'] = checksums(gpo_files)
    gpo = archive(output, f'configbackup-gpo-test-{GPO_VERSION}', gpo_files)
    kit = files(ROOT/'windows-test-kit')
    for name in list(kit):
        if name.endswith(('.zip', '.zip.sha256')):
            del kit[name]
    releases = {p.name: sha(p.read_bytes()) for p in [app, gpo]}
    for p in [app, gpo]:
        kit[p.name] = p.read_bytes()
        kit[p.name+'.sha256'] = p.with_suffix('.zip.sha256').read_bytes()
    source = kit['Run-WorkstationTests.py'].decode('utf-8')
    source, count = re.subn(r'RELEASES = \{.*?\n\}', 'RELEASES = '+json.dumps(releases, indent=4), source, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError('Missing launcher release hash pins')
    kit['Run-WorkstationTests.py'] = source.encode('utf-8')
    kit['CHECKSUMS.sha256'] = checksums(kit)
    archive(output, f'configbackup-workstation-kit-{VERSION}', kit)

if __name__ == '__main__':
    main()
