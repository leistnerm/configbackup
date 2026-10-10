#!/usr/bin/env python3
"""Write diff representations; never modify native/restorable SQL or XML."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
import xml.etree.ElementTree as ET


def canonical_pg(text):
    lines = text.replace('\r\n', '\n').splitlines()
    # Read SQL quote/comment state so a function body containing marker-like text
    # is never rewritten. Only top-level psql safety commands are candidates.
    single=False; double=False; dollar=None; block=0; candidates=[]
    for number,line in enumerate(lines):
        if not (single or double or dollar or block):
            marker=re.fullmatch(r"\\(restrict|unrestrict) ([A-Za-z0-9]+)",line)
            if marker:
                candidates.append((number,marker[1],marker[2]));continue
            if line.startswith('\\'):continue
        i=0
        while i<len(line):
            if dollar:
                if line.startswith(dollar,i):i+=len(dollar);dollar=None
                else:i+=1
            elif block:
                if line.startswith('/*',i):block+=1;i+=2
                elif line.startswith('*/',i):block-=1;i+=2
                else:i+=1
            elif single:
                if line.startswith("''",i):i+=2
                elif line[i]=="'":single=False;i+=1
                elif line[i]=='\\':i+=2
                else:i+=1
            elif double:
                if line.startswith('""',i):i+=2
                elif line[i]=='"':double=False;i+=1
                else:i+=1
            elif line.startswith('--',i):break
            elif line.startswith('/*',i):block=1;i+=2
            elif line[i]=="'":single=True;i+=1
            elif line[i]=='"':double=True;i+=1
            elif line[i]=='$':
                match=re.match(r'\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$',line[i:])
                if match:dollar=match[0];i+=len(dollar)
                else:i+=1
            else:i+=1
    if not candidates or len(candidates)%2:return text
    if any(kind!=('restrict' if i%2==0 else 'unrestrict') for i,(_,kind,_) in enumerate(candidates)):return text
    if any(candidates[i][2]!=candidates[i+1][2] for i in range(0,len(candidates),2)):return text
    for number,kind,key in candidates:lines[number]='\\'+kind+' <COMPARISON-ONLY>'
    return '\n'.join(lines).rstrip()+'\n'


# Recognize only complete standalone extended-property EXEC batches with literal
# named parameters. Reject comments, variables, expressions, duplicate targets,
# SQLCMD substitutions and multiline literals. Any uncertainty preserves order.
VALUE = r"(?:N?'(?:[^'\r\n]|'')*'|NULL|-?\d+(?:\.\d+)?)"
PARAM = re.compile(r'\s*@(name|value|level[012](?:type|name))\s*=\s*(' + VALUE + r')\s*(?:,|$)', re.I)


def property_key(batch):
    match = re.fullmatch(r'\s*EXEC(?:UTE)?\s+(?:\[?sys\]?\.)?\[?sp_addextendedproperty\]?\s+([^;]+);?\s*', batch, re.I)
    if not match or '$(' in batch:
        return None
    tail = match[1].strip()
    pairs = {}; offset = 0
    while offset < len(tail):
        part = PARAM.match(tail, offset)
        if not part or part[1].lower() in pairs:
            return None
        pairs[part[1].lower()] = part[2]
        offset = part.end()
    if 'name' not in pairs or 'value' not in pairs:
        return None
    return tuple(pairs.get(k, '') for k in ('level0type','level0name','level1type','level1name','level2type','level2name','name'))


def canonical_sql(text):
    # Deliberately conservative: only entire files composed of these batches.
    # Mixed DDL and procedure bodies are never rearranged.
    pieces = re.split(r'(?im)^GO\s*$', text)
    batches = [p.strip() for p in pieces if p.strip()]
    keys = [property_key(b) for b in batches]
    # SQL name collation is not known; reject case-insensitive duplicate identities.
    folded = [tuple(x.casefold() for x in key) for key in keys if key is not None]
    if not batches or None in keys or len(set(folded)) != len(keys):
        return text
    return '\nGO\n'.join(b for _, b in sorted(zip(keys, batches))) + '\nGO\n'


def canonical_json(value):
    # Preserve arrays: ordering can be meaningful (job steps, constraints, XML).
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + '\n'


def convert(path):
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix == '.json':
        return canonical_json(json.loads(text))
    if path.suffix in ('.xml', '.dtsx'):
        # XML C14N preserves element order and text. This is diff-only, unsigned.
        return ET.canonicalize(text, strip_text=False, with_comments=True)
    return canonical_pg(canonical_sql(text))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    if args.destination.exists() or args.destination.resolve() == args.source.resolve():
        parser.error('Destination must be a new file')
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(convert(args.source), encoding='utf-8')

if __name__ == '__main__':
    main()
