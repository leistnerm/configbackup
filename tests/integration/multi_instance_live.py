#!/usr/bin/env python3
"""Two disposable SQL endpoints: same database/job names, partial and unreachable failure isolation.
Creates/removes only unique cbtest_* objects. Password comes from an environment variable.
"""
import argparse, contextlib, io, json, os, subprocess, sys, uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import configbackup as cb
from completeness import Coverage

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('pwsh','module-path','sqlpackage','server-one','server-two','output'):p.add_argument('--'+arg,required=True)
    p.add_argument('--password-env',default='CONFIGBACKUP_TEST_SQL_PASSWORD');p.add_argument('--user',default='sa');a=p.parse_args()
    out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=False);name='cbtest_'+uuid.uuid4().hex[:10]
    env={**os.environ,'PSModulePath':a.module_path+os.pathsep+os.environ.get('PSModulePath',''),'CB_USER':a.user,'CB_PASSWORD':os.environ[a.password_env]}
    wrapper=out/'invoke.ps1'
    wrapper.write_text('''param([string]$Server,[string]$QueryFile,[string]$Database='master',[string]$OutputDirectory,[string]$Collector,[string]$SqlPackage)
$ErrorActionPreference='Stop'
Import-Module dbatools
$credential=[pscredential]::new($env:CB_USER,(ConvertTo-SecureString $env:CB_PASSWORD -AsPlainText -Force))
if($QueryFile){$s=Connect-DbaInstance -SqlInstance $Server -SqlCredential $credential -TrustServerCertificate;Invoke-DbaQuery -SqlInstance $s -Database $Database -Query (Get-Content $QueryFile -Raw) -EnableException}
else{& $Collector -SqlInstance $Server -SqlCredential $credential -Database $Database -OutputDirectory $OutputDirectory -SqlPackagePath $SqlPackage -TrustServerCertificate -SkipInstanceExport -SkipSsis -SkipHostConfiguration -IncludeAgentHistory -ConnectTimeout 3;exit $LASTEXITCODE}
''')
    def call(endpoint,args):return subprocess.run([a.pwsh,'-NoProfile','-File',str(wrapper),'-Server',endpoint,*args],env=env,text=True,capture_output=True)
    def query(endpoint,sql,db='master'):
        path=out/'query.sql';path.write_text(sql);cp=call(endpoint,['-QueryFile',str(path),'-Database',db]);assert cp.returncode==0,cp.stderr
    def collect(endpoint,label,code=0):
        target=out/label;cp=call(endpoint,['-Database',name,'-OutputDirectory',str(target),'-Collector',str(ROOT/'collectors/sqlserver/Collect-SqlServerConfiguration.ps1'),'-SqlPackage',a.sqlpackage])
        (out/(label+'.log')).write_text(cp.stdout+cp.stderr);assert cp.returncode==code,(label,cp.returncode,cp.stderr[-500:]);return target
    cfg={'backup':{'root':str(out/'archive')},'git':{'repository':str(out/'git')},'options':{'log_level':'CRITICAL'},'deletion':{'missing_runs':1},'tasks':[]}
    def archive(one,two):
        cfg['tasks']=[{'name':i,'type':'directory','source':str(source),'destination':i,'storage':'both','collection_manifest':True} for i,source in [('one',one),('two',two)]]
        engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(cfg))
        with contextlib.redirect_stdout(io.StringIO()):code=engine.run()
        return engine,code
    created=[]
    try:
        for endpoint in (a.server_one,a.server_two):
            query(endpoint,f'CREATE DATABASE [{name}]');created.append(endpoint)
            query(endpoint,'CREATE TABLE dbo.settings(id int PRIMARY KEY);',name)
            query(endpoint,f"EXEC dbo.sp_add_job @job_name=N'{name}'; EXEC dbo.sp_add_jobstep @job_name=N'{name}',@step_name=N'Check',@subsystem=N'TSQL',@command=N'SELECT 1;'; EXEC dbo.sp_add_jobschedule @job_name=N'{name}',@name=N'Daily',@freq_type=4,@freq_interval=1,@active_start_date=20260101,@active_start_time=10000; EXEC dbo.sp_add_jobserver @job_name=N'{name}';",'msdb')
        one=collect(a.server_one,'one');two=collect(a.server_two,'two');engine,code=archive(one,two);assert code==0
        frozen=json.dumps(engine.state.task('one')['files'],sort_keys=True)
        query(a.server_two,'ALTER TABLE dbo.settings ADD second_only int;',name)
        query(a.server_one,f'ALTER DATABASE [{name}] SET OFFLINE WITH ROLLBACK IMMEDIATE;')
        partial=collect(a.server_one,'partial',6);two_new=collect(a.server_two,'two-new');engine,code=archive(partial,two_new);assert code==4
        # Instance-level metadata can update; only the failed database must freeze.
        frozen_db={k:v for k,v in json.loads(frozen).items() if '/databases/' in k}
        assert all(engine.state.task('one')['files'][k]==v for k,v in frozen_db.items())
        target=out/'git/two/databases'/name/'schema/dbo/Tables/settings.sql';assert 'second_only' in target.read_text()
        before_unreachable=json.dumps(engine.state.task('one')['files'],sort_keys=True)
        unreachable=collect('127.0.0.1,51999','unreachable',1)
        query(a.server_two,'ALTER TABLE dbo.settings ADD after_unreachable int;',name)
        two_last=collect(a.server_two,'two-last');engine,code=archive(unreachable,two_last);assert code==4
        assert json.dumps(engine.state.task('one')['files'],sort_keys=True)==before_unreachable
        assert 'after_unreachable' in target.read_text()
        result={'status':'passed','database':name,'checks':['same DB/job names on two live SQL instances','separate archive and Git paths','offline database unchanged','healthy second instance commits','unreachable first instance preserves all its state','second instance still commits after first cannot connect'], 'limitation':'Explicit TCP ports tested; Windows SQL Browser named-instance discovery not tested'}
        (out/'results.json').write_text(json.dumps(result,indent=2)+'\n');print('PASS: two live SQL instance isolation, including unreachable endpoint')
    finally:
        for endpoint in created:
            query(endpoint,f"IF EXISTS(SELECT 1 FROM msdb.dbo.sysjobs WHERE name=N'{name}') EXEC msdb.dbo.sp_delete_job @job_name=N'{name}';")
            query(endpoint,f'IF DB_ID(N\'{name}\') IS NOT NULL DROP DATABASE [{name}];')
if __name__=='__main__':main()
