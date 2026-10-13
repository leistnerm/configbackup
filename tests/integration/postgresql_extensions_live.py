#!/usr/bin/env python3
"""Optional-adapter live test: unique disposable databases; never contact an FDW server."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'collectors/postgresql')]
from configbackup_lib.engine.completeness import Coverage
from collectors.common.canonicalize import convert
from collectors.postgresql import collect_postgresql as pg
from configbackup_lib.git.secret_scan import scan_bytes


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir',required=True); parser.add_argument('--host',default='/tmp')
    parser.add_argument('--port',default='55439'); parser.add_argument('--user',default='mark')
    parser.add_argument('--output',type=Path,required=True); parser.add_argument('--confirm-disposable-server',action='store_true',required=True)
    args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'PGHOST':args.host,'PGPORT':args.port,'PGUSER':args.user}
    prefix='cbtest_adapter_'+uuid.uuid4().hex[:10]; names=[prefix+'_a',prefix+'_b']; created=[]
    def sql(db,query):
        cp=subprocess.run([str(Path(args.bin_dir)/'psql'),'-X','-w','-v','ON_ERROR_STOP=1','-d',db,'-c',query],env=env,capture_output=True,text=True)
        if cp.returncode: raise RuntimeError('Disposable fixture SQL failed: '+cp.stderr)
        return cp.stdout
    try:
        for name in names:
            sql('postgres',f'CREATE DATABASE "{name}"'); created.append(name)
            sql(name,"CREATE TABLE normal_table(id integer PRIMARY KEY); CREATE EXTENSION postgres_fdw; CREATE SERVER fixture_remote FOREIGN DATA WRAPPER postgres_fdw OPTIONS(host '127.0.0.1',port '9',dbname 'not_contacted'); CREATE USER MAPPING FOR CURRENT_USER SERVER fixture_remote OPTIONS(user 'fixture',password 'SYNTHETIC_TEST_VALUE'); CREATE FOREIGN TABLE remote_table(id int) SERVER fixture_remote OPTIONS(schema_name 'public',table_name 'remote_name');")
        base=pg.PgTools
        def collect(label,fail_globals=False,fail_catalog=False):
            class Tools(base):
                def globals_dump(self):
                    if fail_globals: raise pg.CollectorError('Simulated globals-only export failure')
                    return super().globals_dump()
                def psql_rows(self,db,query):
                    if fail_catalog and db==names[0] and 'FROM pg_sequences' in query: raise pg.CollectorError('Simulated denied sequence catalog')
                    return super().psql_rows(db,query)
            target=args.output/label
            command=['--output',str(target),'--bin-dir',args.bin_dir,'--host',args.host,'--port',args.port,'--user',args.user,'--database',prefix+'_*','--extension-adapter','postgres_fdw','--extension-adapter','postgis','--extension-adapter','pg_partman']
            stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(pg,'PgTools',Tools),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr): code=pg.main(command)
            (args.output/(label+'.log')).write_text(stdout.getvalue()+stderr.getvalue())
            if code!=(6 if fail_globals or fail_catalog else 0):raise AssertionError('Unexpected collector status '+str(code))
            return target,Coverage(target)
        first,a=collect('first'); second,b=collect('second')
        assert a.files==b.files
        differences=[name for name in a.files if (first/name).read_bytes()!=(second/name).read_bytes() and convert(first/name)!=convert(second/name)]
        assert not differences,differences
        adapter='databases/'+names[0]+'/extension-adapters/postgres_fdw/'
        assert adapter+'foreign-tables.csv' in a.files
        data=(first/(adapter+'user-mappings.csv')).read_bytes()
        assert b'SYNTHETIC_TEST_VALUE' not in data and b'<REDACTED>' in data
        assert not scan_bytes(data,'adapter.csv')
        # Native schema intentionally remains authoritative and may contain stored
        # credentials. Verify that the Git gate detects that separate sensitive file.
        assert scan_bytes((first/('databases/'+names[0]+'/schema.sql')).read_bytes(),'native-schema.sql')
        partial,c=collect('partial',True,True)
        assert not c.permits_deletion('cluster/globals.sql')
        assert not c.permits_deletion('databases/'+names[0]+'/inventory/sequences.csv')
        assert 'databases/'+names[1]+'/schema.sql' in c.files
        report={'status':'PASS','postgres_fdw':'live: metadata/secret masking/repeat determinism; remote endpoint was not contacted',
                'postgis':'not installed; not_applicable/preserved','pg_partman':'not installed; not_applicable/preserved',
                'certified_files':len(a.files),'comparison_differences':differences,'globals_and_catalog_failure_isolation':'PASS',
                'native_schema_secret_gate':'PASS','cleanup':'pending'}
        for name in reversed(created):sql('postgres',f'DROP DATABASE "{name}"');created.remove(name)
        report['cleanup']='PASS';(args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report))
    finally:
        for name in reversed(created):sql('postgres',f'DROP DATABASE "{name}" WITH (FORCE)')

if __name__=='__main__':main()
