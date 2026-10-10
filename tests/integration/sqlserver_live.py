#!/usr/bin/env python3
"""SQL Server disposable-database integration. Credentials come only from an environment variable.
Creates/removes uniquely named cbtest_* databases and one native backup in the specified server directory.
"""
import argparse,contextlib,io,json,os,subprocess,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import configbackup as cb
from completeness import Coverage
from collectors.common.compare_snapshots import audit

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('pwsh','module-path','sqlpackage','output','backup-directory'):p.add_argument('--'+name,required=True)
    p.add_argument('--server',default='127.0.0.1,51439');p.add_argument('--user',default='sa')
    p.add_argument('--password-env',default='CONFIGBACKUP_TEST_SQL_PASSWORD')
    a=p.parse_args();out=Path(a.output).resolve();out.mkdir(parents=True,exist_ok=False)
    assert os.environ.get(a.password_env),'Set the named password environment variable'
    prefix='cbtest_'+uuid.uuid4().hex[:8];names=[prefix+'_a',prefix+'_b',prefix+'_c']
    env={**os.environ,'PSModulePath':a.module_path+os.pathsep+os.environ.get('PSModulePath',''),
         'CB_TEST_USER':a.user,'CB_TEST_PASSWORD':os.environ[a.password_env]}
    wrapper=out/'invoke.ps1'
    wrapper.write_text('''param([string]$Mode,[string]$Server,[string]$InputFile,[string]$OutputDirectory,[string]$Names,[string]$Collector,[string]$SqlPackage)
$ErrorActionPreference='Stop'
Import-Module dbatools -ErrorAction Stop
$credential=[pscredential]::new($env:CB_TEST_USER,(ConvertTo-SecureString $env:CB_TEST_PASSWORD -AsPlainText -Force))
if($Mode -eq 'query') {
    $connection=Connect-DbaInstance -SqlInstance $Server -SqlCredential $credential -TrustServerCertificate
    Invoke-DbaQuery -SqlInstance $connection -Database master -Query (Get-Content -LiteralPath $InputFile -Raw) -EnableException
} else {
    & $Collector -SqlInstance $Server -SqlCredential $credential -Database $Names -OutputDirectory $OutputDirectory -SqlPackagePath $SqlPackage -TrustServerCertificate -SkipInstanceExport -SkipAgent -SkipSsis -SkipHostConfiguration
    exit $LASTEXITCODE
}
''')
    def invoke(arguments):return subprocess.run([a.pwsh,'-NoLogo','-NoProfile','-File',str(wrapper),'-Server',a.server,*arguments],env=env,text=True,capture_output=True)
    def query(text):
        file=out/'query.sql';file.write_text(text)
        cp=invoke(['-Mode','query','-InputFile',str(file)])
        if cp.returncode:raise RuntimeError(cp.stderr)
    def collect(label,expected):
        target=out/label
        cp=invoke(['-Mode','collect','-Names',','.join(names),'-OutputDirectory',str(target),'-Collector',str(ROOT/'collectors/sqlserver/Collect-SqlServerConfiguration.ps1'),'-SqlPackage',a.sqlpackage])
        (out/(label+'.log')).write_text(cp.stdout+cp.stderr)
        assert cp.returncode==expected,(label,cp.returncode,cp.stderr[-2000:])
        Coverage(target);return target
    config={'backup':{'root':str(out/'archive')},'git':{'repository':str(out/'git')},'options':{'log_level':'CRITICAL'},'deletion':{'missing_runs':1},
            'tasks':[{'name':'sql','type':'directory','source':'','destination':'sql','storage':'both','collection_manifest':True,'git_canonicalize':True}]}
    def archive(source):
        config['tasks'][0]['source']=str(source)
        engine=cb.BackupEngine(cb.ConfigLoader(Path('unused'))._resolve(config))
        with contextlib.redirect_stdout(io.StringIO()):code=engine.run()
        return engine,code
    created=[]
    try:
        for db in names:
            query(f'CREATE DATABASE [{db}];');created.append(db)
            query(f"USE [{db}]; CREATE TABLE dbo.config_test(id int PRIMARY KEY, value nvarchar(80)); EXEC sys.sp_addextendedproperty @name=N'Description', @value=N'Stable test property';")
        first=collect('first',0);second=collect('second',0)
        difference=audit(first,second)
        assert not [x for x in difference['changed'] if x!='collection-manifest.json'],difference
        engine,code=archive(first);assert code==0
        frozen={k:json.dumps(v,sort_keys=True) for k,v in engine.state.task('sql')['files'].items() if '/'+names[1]+'/' in k}
        assert frozen
        query(f'USE [{names[0]}]; ALTER TABLE dbo.config_test ADD changed_setting int; USE [{names[2]}]; ALTER TABLE dbo.config_test ADD after_failure int;')
        query(f'ALTER DATABASE [{names[1]}] SET OFFLINE WITH ROLLBACK IMMEDIATE;')
        offline=collect('offline',6)
        engine,code=archive(offline);assert code==4
        assert all(json.dumps(engine.state.task('sql')['files'][k],sort_keys=True)==v for k,v in frozen.items())
        assert 'changed_setting' in (out/'git/sql/databases'/names[0]/'schema/dbo/Tables/config_test.sql').read_text()
        assert 'after_failure' in (out/'git/sql/databases'/names[2]/'schema/dbo/Tables/config_test.sql').read_text()
        query(f'ALTER DATABASE [{names[1]}] SET ONLINE;')
        backup=(a.backup_directory.rstrip('/\\')+'/'+prefix+'.bak').replace("'","''")
        query(f"BACKUP DATABASE [{names[1]}] TO DISK=N'{backup}' WITH INIT; RESTORE DATABASE [{names[1]}] FROM DISK=N'{backup}' WITH REPLACE,NORECOVERY;")
        restoring=collect('restoring',6);engine,code=archive(restoring);assert code==4
        assert all(json.dumps(engine.state.task('sql')['files'][k],sort_keys=True)==v for k,v in frozen.items())
        query(f'RESTORE DATABASE [{names[1]}] WITH RECOVERY;')
        recovered=collect('recovered',0);assert archive(recovered)[1]==0
        result={'status':'passed','databases':names,'native_backup':backup,'determinism':difference['counts'],
                'checks':['real SqlPackage extraction','two-run comparison','OFFLINE fault isolation','RESTORING fault isolation','healthy before/after updates committed','failed archive and Git unchanged','recovery next run']}
        (out/'results.json').write_text(json.dumps(result,indent=2)+'\n');print('PASS: live SQL Server OFFLINE/RESTORING isolation and recovery')
    finally:
        for db in created:
            try:query(f'IF EXISTS(SELECT 1 FROM sys.databases WHERE name=N\'{db}\') DROP DATABASE [{db}];')
            except Exception as exc:print('Cleanup warning for '+db+': '+str(exc),file=sys.stderr)
if __name__=='__main__':main()
