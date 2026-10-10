#!/usr/bin/env python3
"""Disposable PostgreSQL integration test. Creates unique cbtest_* databases; removes only those it created."""
import argparse, contextlib, importlib.util, io, json, os, subprocess, sys, tempfile, time, uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import configbackup as cb
from completeness import Coverage
from collectors.common.compare_snapshots import audit

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir',required=True);parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',default='55439');parser.add_argument('--user',default='postgres')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    prefix='cbtest_'+uuid.uuid4().hex[:8];names=[prefix+'_a',prefix+'_b',prefix+'_c']
    env={**os.environ,'PGHOST':args.host,'PGPORT':args.port,'PGUSER':args.user}
    psql=str(Path(args.bin_dir)/'psql')
    def sql(db,query):
        cp=subprocess.run([psql,'-X','-w','-v','ON_ERROR_STOP=1','-d',db,'-c',query],env=env,text=True,capture_output=True)
        if cp.returncode:raise RuntimeError(cp.stderr)
        return cp.stdout
    created=[];lock=None
    try:
        for name in names:
            sql('postgres',f'CREATE DATABASE "{name}"');created.append(name)
            sql(name,"CREATE TABLE config_table(id integer PRIMARY KEY, value text DEFAULT 'stable'); CREATE VIEW config_view AS SELECT value FROM config_table;")
        def collect(label,expect=0):
            target=args.output/label
            cmd=[sys.executable,str(ROOT/'collectors/postgresql/collect_postgresql.py'),'--output',str(target),'--bin-dir',args.bin_dir,
                 '--host',args.host,'--port',args.port,'--user',args.user,'--database',prefix+'_*','--lock-wait-timeout','1s']
            cp=subprocess.run(cmd,text=True,capture_output=True)
            (args.output/(label+'.log')).write_text(cp.stdout+cp.stderr)
            assert cp.returncode==expect,(cp.returncode,cp.stderr)
            return target
        first=collect('first');second=collect('second')
        diff=audit(first,second)
        significant=[x for x in diff['changed'] if x!='collection-manifest.json']
        assert not significant,significant
        config={'backup':{'root':str(args.output/'archive')},'git':{'repository':str(args.output/'git')},'options':{'log_level':'CRITICAL'},
                'deletion':{'missing_runs':1},'tasks':[{'name':'pg','type':'directory','source':str(first),'destination':'pg','storage':'both','collection_manifest':True,'git_canonicalize':True}]}
        def archive(source):
            config['tasks'][0]['source']=str(source)
            engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(config))
            with contextlib.redirect_stdout(io.StringIO()):result=engine.run()
            return engine,result
        engine,code=archive(first);assert code==0
        frozen=json.dumps(engine.state.task('pg')['files']['pg/databases/'+names[1]+'/schema.sql'],sort_keys=True)
        sql(names[0],'ALTER TABLE config_table ADD COLUMN new_setting boolean DEFAULT false')
        sql(names[2],'ALTER TABLE config_table ADD COLUMN after_failure integer')
        lock=subprocess.Popen([psql,'-X','-w','-d',names[1],'-v','ON_ERROR_STOP=1'],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        lock.stdin.write('BEGIN; LOCK TABLE config_table IN ACCESS EXCLUSIVE MODE;\n\\echo LOCK_READY\nSELECT pg_sleep(120);\n');lock.stdin.flush()
        while True:
            line=lock.stdout.readline()
            if 'LOCK_READY' in line:break
            if not line:raise RuntimeError('Could not acquire test lock')
        partial=collect('partial',6);coverage=Coverage(partial)
        assert any(x['path']=='databases/'+names[1] and x['status']=='failed' for x in coverage.sections)
        engine,code=archive(partial);assert code==4
        assert json.dumps(engine.state.task('pg')['files']['pg/databases/'+names[1]+'/schema.sql'],sort_keys=True)==frozen
        assert 'new_setting' in (args.output/'git/pg/databases'/names[0]/'schema.sql').read_text()
        assert 'after_failure' in (args.output/'git/pg/databases'/names[2]/'schema.sql').read_text()
        sql('postgres',f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{names[1]}' AND pid<>pg_backend_pid()")
        lock.communicate(timeout=10);lock=None
        recovered=collect('recovered');assert archive(recovered)[1]==0
        (args.output/'results.json').write_text(json.dumps({'status':'passed','databases':names,'determinism':diff['counts'],
            'checks':['live native dumps','two-run comparison','exclusive-lock schema failure','continue following database','archive and Git preservation','recovery next run']},indent=2)+'\n')
        print('PASS: live PostgreSQL determinism, partial failure, archive/Git isolation, and recovery')
    finally:
        if lock:
            sql('postgres',f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{names[1]}'")
            lock.communicate(timeout=10)
        for name in created:sql('postgres',f'DROP DATABASE "{name}" WITH (FORCE)')
if __name__=='__main__':main()
