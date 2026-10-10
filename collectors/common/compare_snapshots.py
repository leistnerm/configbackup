#!/usr/bin/env python3
"""Audit two collected snapshots for non-semantic output churn.

Does not edit either input tree. CSV row-order-only changes are detected exactly.
Text SQL reordering is reported as a suspected cause, never auto-rewritten.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parent))
from canonicalize import convert


def csv_canonical(path: Path):
    try:
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader=csv.DictReader(stream)
            if not reader.fieldnames:return None
            header=tuple(reader.fieldnames)
            records=[tuple(row.get(key,'') for key in header) for row in reader]
            return header, sorted(records)
    except (OSError,UnicodeError,csv.Error):return None


def collect(root:Path):
    if not root.is_dir():raise ValueError(f'Not a directory: {root}')
    return {file.relative_to(root).as_posix():file for file in root.rglob('*') if file.is_file()}


def audit(left:Path,right:Path)->dict:
    a=collect(left);b=collect(right)
    changed=[];added=[];removed=[];same=[];order_only=[];representation_only=[]
    for key in sorted(set(a)|set(b)):
        if key not in a:added.append(key);continue
        if key not in b:removed.append(key);continue
        if a[key].read_bytes()==b[key].read_bytes():same.append(key);continue
        if key.lower().endswith('.csv') and csv_canonical(a[key])==csv_canonical(b[key]):
            order_only.append(key)
        else:
            try:
                equivalent=a[key].suffix in {'.sql','.xml','.dtsx','.json'} and convert(a[key])==convert(b[key])
            except (ValueError,UnicodeError, OSError): equivalent=False
            if equivalent:representation_only.append(key)
            else:changed.append(key)
    return {'comparison_representation_only':representation_only,'identical':same,'added':added,'removed':removed,'changed':changed,'csv_row_order_only':order_only,
            'counts':{'comparison_representation_only':len(representation_only),'identical':len(same),'added':len(added),'removed':len(removed),'changed':len(changed),'csv_row_order_only':len(order_only)}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True)
    p.add_argument('--after',type=Path,required=True)
    p.add_argument('--output',type=Path,help='Optional JSON audit output path')
    args=p.parse_args()
    data=audit(args.before,args.after)
    formatted=json.dumps(data,indent=2,ensure_ascii=False,sort_keys=True)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(formatted,encoding='utf-8')
    else:print(formatted)
    return 0

if __name__=='__main__':raise SystemExit(main())
