#!/usr/bin/env python3
"""Disposable PostgreSQL role-hash export checks; use only a test database server."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from collectors.postgresql.collect_postgresql import PgTools,parse_args,CollectorError,capture_file,stable_text
from sensitive_data import ENVIRONMENT

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--bin-dir',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--confirm-disposable',action='store_true',required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    args=parse_args(['--bin-dir',a.bin_dir,'--host',os.environ.get('PGHOST','127.0.0.1'),'--port',os.environ.get('PGPORT','5432'),'--user',os.environ.get('PGUSER','postgres')]);tools=PgTools(args)
    role='cbtest_sensitive_'+uuid.uuid4().hex[:12];password='SYNTHETIC-ROLE-PASSWORD-Only42!'
    def sql(query):
        cp=tools.run('psql',['-X','-w','-d',args.maintenance_db,'-v','ON_ERROR_STOP=1','-c',query]);return cp.stdout
    created=False
    try:
        sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'");created=True
        with patch.dict(os.environ,{ENVIRONMENT:'[]'}):normal=tools.globals_dump()
        with patch.dict(os.environ,{ENVIRONMENT:'["cluster/globals.sql"]'}):
            first=tools.globals_dump();second=tools.globals_dump()
        rows=lambda value:[line for line in value.splitlines() if role in line]
        assert not any('PASSWORD' in line for line in rows(normal))
        assert any('SCRAM-SHA-256$' in line or 'md5' in line for line in rows(first))
        assert rows(first)==rows(second),'Role export changed on repeat'
        assert password not in first
        low=PgTools(args);low.env.update(PGUSER=role,PGPASSWORD=password)
        with patch.dict(os.environ,{ENVIRONMENT:'[]'}):low.globals_dump()
        failures=[]
        with patch.dict(os.environ,{ENVIRONMENT:'["cluster/globals.sql"]'}):capture_file(low,a.output,'cluster/globals.sql',failures,'globals',low.globals_dump,stable_text)
        assert failures and low.collection_scopes[0]['status']=='failed'
        assert password not in json.dumps(failures)
        result={'status':'PASS','checks':['default excludes password hashes','explicit opt-in exports hashes','stable repeated role export','no plaintext role password','limited account normal export works','limited account sensitive export fails without leaked errors']}
        (a.output/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    finally:
        if created:sql(f'DROP ROLE {role}')
if __name__=='__main__':main()
