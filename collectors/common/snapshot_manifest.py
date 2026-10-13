#!/usr/bin/env python3
"""Create native and comparison hashes. These are representation hashes, not a SQL semantic proof."""
import argparse, hashlib, json
from pathlib import Path
from canonicalize import convert
from compare_snapshots import csv_canonical

def manifest(root):
    result={}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name=='collection-manifest.json' or 'telemetry' in path.relative_to(root).parts:continue
        data=path.read_bytes();normalized=data;kind='bytes'
        try:
            if path.suffix=='.csv':
                normalized=json.dumps(csv_canonical(path),sort_keys=True).encode();kind='csv-row-set'
            elif path.suffix in ('.json','.sql','.xml','.dtsx'):
                normalized=convert(path).encode();kind='narrow-canonical'
        except (ValueError,UnicodeError):pass
        result[path.relative_to(root).as_posix()]={'sha256':hashlib.sha256(data).hexdigest(),'comparison_sha256':hashlib.sha256(normalized).hexdigest(),'comparison_kind':kind}
    return {'schema_version':1,'files':result,'warning':'Comparison hashes preserve ordered arrays and arbitrary SQL; they do not prove full semantic equivalence.'}
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('root',type=Path);p.add_argument('output',type=Path);a=p.parse_args()
    if a.output.resolve().is_relative_to(a.root.resolve()):p.error('Write the manifest outside its input tree')
    a.output.write_text(json.dumps(manifest(a.root),indent=2,sort_keys=True)+'\n')
if __name__=='__main__':main()
