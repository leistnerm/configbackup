"""Optional authenticated encryption of verified setup bundles; no secret in argv.

Format CBREC1: fixed scrypt parameters, 16-byte salt, 12-byte nonce, AES-256-GCM.
Maximum uncompressed bundle: 256 MiB. Encrypted bytes intentionally differ per run.
"""
from __future__ import annotations
import argparse
import getpass
import io
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile
from configbackup_lib.recovery.setup_recovery import verify,safe_file

MAGIC=b'CBREC1\0';MAX_BYTES=256*1024*1024


def key(password,salt):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    if len(password)<12:raise ValueError('Use a passphrase of at least 12 characters')
    return Scrypt(salt=salt,length=32,n=2**17,r=8,p=1).derive(password.encode('utf-8'))


def encrypt(bundle,destination,password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    root=Path(bundle).resolve();destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink() or destination.resolve().is_relative_to(root):raise ValueError('Choose a new encrypted file outside the bundle')
    if verify(root)[0]['status']!='passed':raise ValueError('Bundle verification failed')
    files=sorted(p for p in root.rglob('*') if p.is_file())
    if sum(p.stat().st_size for p in files)>MAX_BYTES:raise ValueError('Bundle exceeds encryption size limit')
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for path in files:
            if path.is_symlink():raise ValueError('Bundle changed while packaging')
            info=zipfile.ZipInfo(path.relative_to(root).as_posix(),date_time=(2000,1,1,0,0,0));info.external_attr=(stat.S_IFREG|0o600)<<16
            z.writestr(info,path.read_bytes(),compress_type=zipfile.ZIP_DEFLATED)
    # Validate the exact packaged bytes before encrypting, not just the source tree.
    with tempfile.TemporaryDirectory(prefix='configbackup-encrypt-') as td:
        _extract(data.getvalue(),Path(td))
        if verify(td)[0]['status']!='passed':raise ValueError('Bundle changed while packaging')
    salt=os.urandom(16);nonce=os.urandom(12);header=MAGIC+salt+nonce
    encrypted=AESGCM(key(password,salt)).encrypt(nonce,data.getvalue(),header)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'wb') as out:out.write(header+encrypted);out.flush();os.fsync(out.fileno())
    except BaseException:destination.unlink(missing_ok=True);raise
    return {'status':'encrypted','file':str(destination),'algorithm':'AES-256-GCM/scrypt','files':len(files)}


def _extract(data,root):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entries=z.infolist();seen=set()
        if len(entries)>10002 or sum(r.file_size for r in entries)>MAX_BYTES:raise ValueError('Encrypted bundle exceeds extraction limits')
        for row in entries:
            target=safe_file(root.resolve(),row.filename);name=row.filename.casefold()
            if row.is_dir() or name in seen or stat.S_ISLNK(row.external_attr>>16):raise ValueError('Invalid encrypted bundle member')
            seen.add(name);target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with target.open('xb') as out:os.chmod(target,0o600);out.write(z.read(row))


def decrypt(source,destination,password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    source=Path(source);target=Path(destination).absolute()
    if source.stat().st_size>MAX_BYTES+1024*1024:raise ValueError('Encrypted file exceeds limit')
    if target.exists() or target.is_symlink():raise ValueError('Decryption destination must be new')
    data=source.read_bytes();n=len(MAGIC)
    if not data.startswith(MAGIC) or len(data)<n+44:raise ValueError('Unsupported encrypted bundle')
    header=data[:n+28];salt=data[n:n+16];nonce=data[n+16:n+28]
    try:plain=AESGCM(key(password,salt)).decrypt(nonce,data[n+28:],header)
    except __import__('cryptography.exceptions',fromlist=['InvalidTag']).InvalidTag as exc:raise ValueError('Authentication failed; wrong passphrase or altered bundle') from exc
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.decrypt-',dir=target.parent) as td:
        stage=Path(td)/'bundle';stage.mkdir(mode=0o700);_extract(plain,stage)
        if verify(stage)[0]['status']!='passed':raise ValueError('Decrypted bundle failed verification')
        stage.rename(target)
    return {'status':'verified_and_decrypted','directory':str(target)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['encrypt','decrypt']);p.add_argument('--source',required=True);p.add_argument('--destination',required=True);p.add_argument('--password-env',help='Environment variable NAME, never the passphrase itself');a=p.parse_args(argv)
    password=os.environ.get(a.password_env,'') if a.password_env else getpass.getpass('Recovery passphrase: ')
    try:result=(encrypt if a.action=='encrypt' else decrypt)(a.source,a.destination,password)
    except (ValueError,OSError,ImportError) as exc:print('Recovery encryption failed: '+type(exc).__name__);return 6
    print(__import__('json').dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
