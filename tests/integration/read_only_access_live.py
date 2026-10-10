#!/usr/bin/env python3
"""Exercise generated grants on a disposable database server.

Creates a unique database and login, grants read access, rejects ordinary writes,
collects the database, and removes those test objects. Run only on an explicitly
disposable server. Credentials are read from the environment, never arguments.
Existing databases and identities are not modified.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import access_scripts
from completeness import Coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['postgresql', 'sqlserver'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--confirm-disposable-server', action='store_true')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default='5432')
    parser.add_argument('--user', default='postgres')
    parser.add_argument('--bin-dir', default='')
    parser.add_argument('--server', default='127.0.0.1,1433')
    parser.add_argument('--sql-user', default='sa')
    parser.add_argument('--password-env', default='CONFIGBACKUP_TEST_SQL_PASSWORD')
    parser.add_argument('--pwsh', default='pwsh')
    parser.add_argument('--module-path')
    parser.add_argument('--sqlpackage')
    parser.add_argument('--diagnostics', action='store_true', help='Also test the configuration CLI diagnostics before/after grants and with a nonexistent login')
    parser.add_argument('--shared',action='store_true',help='Exercise shared managed collection, readiness launcher, capability history and repeat determinism')
    args = parser.parse_args()
    if not args.confirm_disposable_server:
        parser.error('--confirm-disposable-server is required')
    if args.engine == 'sqlserver' and (not args.sqlpackage or not os.environ.get(args.password_env)):
        parser.error('SQL Server requires --sqlpackage and the password environment variable')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    name = 'cbtest_access_' + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(32) + 'Aa1!'
    access_scripts.generate(args.engine, name, [name], out / 'grants')
    result = {'status': 'failed', 'database': name, 'engine': args.engine, 'writes': {}}
    env = dict(os.environ)
    created_role = False
    created_database = False

    if args.engine == 'postgresql':
        env.update(PGHOST=args.host, PGPORT=args.port, PGUSER=args.user)
        psql = str(Path(args.bin_dir) / 'psql') if args.bin_dir else 'psql'

        def query(sql, reader=False, database='postgres', check=True):
            child = {**env, 'PGPASSWORD': password} if reader else env
            cp = subprocess.run([psql, '-X', '-w', '-v', 'ON_ERROR_STOP=1', '-d', database,
                                 '-U', name if reader else args.user], input=sql, env=child,
                                capture_output=True, text=True)
            if check and cp.returncode:
                raise RuntimeError('psql failed: ' + cp.stderr.replace(password, '[redacted]'))
            return cp

        def collect():
            command = [sys.executable, str(ROOT / 'collectors/postgresql/collect_postgresql.py'),
                       '--output', str(out / 'snapshot'), '--host', args.host, '--port', args.port,
                       '--user', name, '--database', name, '--read-only-access', '--include-health']
            if args.bin_dir:
                command += ['--bin-dir', args.bin_dir]
            return subprocess.run(command, env={**env, 'PGPASSWORD': password}, capture_output=True, text=True)
    else:
        env.update(CB_TEST_ADMIN_USER=args.sql_user, CB_TEST_ADMIN_PASSWORD=os.environ[args.password_env],
                   CB_TEST_READER=name, CB_TEST_READER_PASSWORD=password)
        if args.module_path:
            env['PSModulePath'] = args.module_path + os.pathsep + env.get('PSModulePath', '')
        wrapper = out / 'invoke.ps1'
        wrapper.write_text('''param([string]$Mode,[string]$Server,[string]$Database,[string]$InputFile,[switch]$Reader,[string]$Collector,[string]$OutputDirectory,[string]$SqlPackage)
$ErrorActionPreference='Stop'
Import-Module dbatools -ErrorAction Stop
$username=$env:CB_TEST_ADMIN_USER;$password=$env:CB_TEST_ADMIN_PASSWORD
if($Reader){$username=$env:CB_TEST_READER;$password=$env:CB_TEST_READER_PASSWORD}
$credential=[pscredential]::new($username,(ConvertTo-SecureString $password -AsPlainText -Force))
if($Mode -eq 'collect') {
 & $Collector -SqlInstance $Server -SqlCredential $credential -Database $Database -OutputDirectory $OutputDirectory -SqlPackagePath $SqlPackage -TrustServerCertificate -ReadOnlyAccess -IncludeAgentHistory -IncludeHealthMetrics -SkipInstanceExport
 exit $LASTEXITCODE
}
$connection=Connect-DbaInstance -SqlInstance $Server -SqlCredential $credential -TrustServerCertificate
if($Mode -eq 'create-reader') {
 $p=$env:CB_TEST_READER_PASSWORD.Replace("'","''");$n=$env:CB_TEST_READER
 Invoke-DbaQuery -SqlInstance $connection -Database master -Query "CREATE LOGIN [$n] WITH PASSWORD=N'$p',CHECK_POLICY=OFF;" -EnableException | Out-Null
} else {
 Invoke-DbaQuery -SqlInstance $connection -Database $Database -Query (Get-Content -LiteralPath $InputFile -Raw) -EnableException | Out-Null
}
''')

        def invoke(arguments):
            return subprocess.run([args.pwsh, '-NoProfile', '-File', str(wrapper), '-Server', args.server,
                                   *arguments], env=env, capture_output=True, text=True)

        def query(sql, reader=False, database='master', check=True):
            path = out / 'query.sql'
            path.write_text(sql)
            cp = invoke(['-Mode', 'query', '-Database', database, '-InputFile', str(path)] + (['-Reader'] if reader else []))
            if check and cp.returncode:
                raise RuntimeError('SQL query failed: ' + cp.stderr.replace(password, '[redacted]').replace(env['CB_TEST_ADMIN_PASSWORD'], '[redacted]'))
            return cp

        def collect():
            return invoke(['-Mode', 'collect', '-Reader', '-Database', name, '-Collector',
                           str(ROOT / 'collectors/sqlserver/Collect-SqlServerConfiguration.ps1'),
                           '-OutputDirectory', str(out / 'snapshot'), '-SqlPackage', args.sqlpackage])

    def shared_config(user=None,output=None):
        profile={'engine':args.engine,'user':user or name,'password_env':'CB_TEST_DIAGNOSTIC_PASSWORD',
                 'databases':[name],'access_profile':'read-only','timeout':600}
        if args.engine=='postgresql':profile.update(host=args.host,port=args.port,bin_dir=args.bin_dir)
        else:profile.update(server=args.server,pwsh=args.pwsh,sqlpackage=args.sqlpackage,trust_server_certificate=True)
        return {'backup':{'root':str(out/'archive')},'connections':{'fixture':profile},
                'tasks':[{'name':'fixture','type':'execute','connection':'fixture',
                          'output_directory':str(output or out/'snapshot'),
                          'required_sections':['databases/'+name+'/schema*', *(['instance/catalog/configuration.csv'] if args.engine=='sqlserver' else [])]}]}

    def managed_collect(output):
        path=out/'managed-config.json';path.write_text(json.dumps(shared_config(output=output)))
        cp=subprocess.run([sys.executable,str(ROOT/'configbackup.py'),'--config',str(path)],
                          env={**env,'CB_TEST_DIAGNOSTIC_PASSWORD':password},capture_output=True,text=True,timeout=660)
        result['managed_backup_exit_code']=cp.returncode
        if cp.returncode==4:cp.returncode=6
        return cp

    def diagnostic(label, user=None):
        config=shared_config(user)
        if not args.shared:
            config={'database_diagnostics':[dict(config['connections']['fixture'],name='fixture')]}
        path=out/(label+'-config.json');path.write_text(json.dumps(config))
        report=out/(label+'-report.json')
        with (out/(label+'.log')).open('w') as log:
            cp=subprocess.run([sys.executable,str(ROOT/'configure.py'),'--config',str(path),('--diagnose-task' if args.shared else '--diagnose-database'),'fixture','--diagnostic-report',str(report),*(['--capability-history',str(out/'capabilities')] if args.shared else [])],
                              env={**env,'CB_TEST_DIAGNOSTIC_PASSWORD':password},stdout=log,stderr=subprocess.STDOUT,timeout=660)
        data=json.loads(report.read_text())
        assert cp.returncode in (0,1,6),label
        return data

    try:
        if args.engine == 'postgresql':
            query('CREATE ROLE ' + name + " LOGIN PASSWORD '" + password + "';")
        else:
            cp = invoke(['-Mode', 'create-reader'])
            if cp.returncode:
                raise RuntimeError('Creating test login failed; inspect server permissions')
        created_role = True
        query('CREATE DATABASE ' + name + ';')
        created_database = True
        query('CREATE TABLE proof(id int PRIMARY KEY); INSERT INTO proof VALUES(1);', database=name)
        if args.diagnostics:
            before=diagnostic('before-grants')
            assert before['connection']['status']=='connected',before['connection']
            assert not any(r['status']=='available' and r['section'].startswith('databases/'+name) for r in before['sections'])
        if args.engine == 'postgresql':
            for script in ('grant-cluster-read-access.sql', 'grant-file-settings-read.sql'):
                query((out / 'grants' / script).read_text())
        else:
            query((out / 'grants/grant-read-access.sql').read_text())
        query('SELECT * FROM proof;' if args.engine == 'postgresql' else "SELECT * FROM sys.columns WHERE object_id=OBJECT_ID(N'dbo.proof');", reader=True, database=name)
        attempts = {'insert': 'INSERT INTO proof VALUES(2);', 'create_table': 'CREATE TABLE denied(id int);',
                    'alter_table': 'ALTER TABLE proof ADD denied int;'}
        if args.engine == 'postgresql':
            attempts['create_role'] = 'CREATE ROLE ' + name + '_denied;'
        for key, sql in attempts.items():
            # A read-only transaction default is not the access-control boundary.
            if args.engine == 'postgresql':
                sql = 'SET default_transaction_read_only=off; ' + sql
            rejected = query(sql, reader=True, database=name, check=False).returncode != 0
            result['writes'][key] = {'rejected': rejected}
            if not rejected:
                raise AssertionError('Write unexpectedly permitted: ' + key)
        if args.engine == 'sqlserver':
            rejected = query("EXEC dbo.sp_add_job @job_name=N'" + name + "';", reader=True, database='msdb', check=False).returncode != 0
            result['writes']['create_agent_job'] = {'rejected': rejected}
            if not rejected:
                raise AssertionError('SQL Agent job creation unexpectedly permitted')
        cp = managed_collect(out/'snapshot') if args.shared else collect()
        log = cp.stdout + cp.stderr
        for value in (password, env.get('CB_TEST_ADMIN_PASSWORD', '')):
            if value:
                log = log.replace(value, '[redacted]')
        (out / 'collector.log').write_text(log)
        result['collector_exit_code'] = cp.returncode
        if cp.returncode not in (0, 6):
            raise AssertionError('Unexpected collector exit code')
        coverage = Coverage(out / 'snapshot')
        paths = [path for path in coverage.files if path.startswith('databases/' + name + '/')]
        if not paths or not any('/schema' in path for path in paths):
            raise AssertionError('Test database schema was not certified')
        result.update(status='passed', certified_database_files=len(paths),
                      unavailable_scopes=[s['path'] for s in coverage.sections if s['status'] == 'failed'])
        if args.diagnostics:
            after=diagnostic('after-grants')
            assert after['connection']['status']=='connected',after['connection']
            assert any(r['status']=='available' and r['section'].startswith('databases/'+name) for r in after['sections'])
            failed=diagnostic('nonexistent-login',name+'_missing')
            assert failed['connection']['status']=='failed',failed['connection']
            assert not any(r['status']=='available' for r in failed['sections'])
            result['diagnostics']={'before_grants':before['status'],'after_grants':after['status'],
                                   'nonexistent_login':failed['connection']['status'],'checks':'CLI JSON/console, real permission changes, temporary real collection, missing login'}
            if args.shared:
                assert after['readiness'] in ('ready','ready_with_warnings'),after['requirements']
                # Same profile and account; revocation must preserve the prior baseline.
                if args.engine=='postgresql':query('REVOKE pg_monitor FROM '+name+';')
                else:query('REVOKE VIEW ANY DEFINITION FROM ['+name+'];')
                revoked=diagnostic('revoked-access')
                assert revoked['readiness']=='not_ready'
                assert revoked['capabilities']['lost'] or revoked['capabilities']['unverified']
                if args.engine=='postgresql':query('GRANT pg_monitor TO '+name+';')
                else:query('GRANT VIEW ANY DEFINITION TO ['+name+'];')
                recovered=diagnostic('recovered-access')
                assert recovered['readiness'] in ('ready','ready_with_warnings')
                assert not recovered['capabilities']['lost'] and not recovered['capabilities']['unverified']
                cp=managed_collect(out/'repeat');assert cp.returncode in (0,6)
                repeat=Coverage(out/'repeat');assert set(coverage.files)==set(repeat.files)
                from collectors.common.canonicalize import convert
                differences=[];compared=0
                for file in paths:
                    left=out/'snapshot'/file;right=out/'repeat'/file
                    if left.suffix.lower() in ('.sql','.xml','.json','.dtsx'):
                        equal=convert(left)==convert(right)
                    else:equal=left.read_bytes()==right.read_bytes()
                    compared+=1
                    if not equal:differences.append(file)
                result['shared']={'required_sections':after['readiness'],'revoked_access':revoked['readiness'],
                    'recovered_access':recovered['readiness'],'database_files_compared':compared,'comparison_differences':differences}
                result['shared']['all_database_comparisons_equal']=not bool(differences)
                if differences:raise AssertionError('Database comparison output changed between identical runs')
                if args.engine=='sqlserver':
                    scripts=[out/'snapshot'/file for file in paths if '/schema/Security/' in file and '<CONFIGBACKUP_PASSWORD_REMOVED>' in (out/'snapshot'/file).read_text()]
                    assert scripts,'Expected a sanitized generated login script'
                    for script in scripts:
                        assert query('SET PARSEONLY ON;\n'+script.read_text(),check=False).returncode!=0,'Removed password marker must not be executable SQL'
                    from secret_scan import scan_bytes
                    assert all(not scan_bytes(script.read_bytes(),script.name) for script in scripts)
                    result['shared']['sanitized_login_scripts']=len(scripts)
                    result['shared']['sanitized_scripts_require_new_password']=True

                import startup_launcher
                config_path=out/'launcher-config.json';config_path.write_text(json.dumps(shared_config()))
                startup_launcher.generate(config_path,out/'launcher','windows' if os.name=='nt' else 'macos' if sys.platform=='darwin' else 'linux',sys.executable,
                    mode='diagnostic',task='fixture',report_directory=str(out/'launcher-reports'))
                launcher_command=[args.pwsh,'-NoProfile','-NonInteractive','-File',str(out/'launcher/run-configbackup.ps1')] if os.name=='nt' else ['/bin/sh',str(out/'launcher/run-configbackup.sh')]
                cp=subprocess.run(launcher_command,env={**env,'CB_TEST_DIAGNOSTIC_PASSWORD':password},capture_output=True,text=True,timeout=660)
                assert cp.returncode in (0,6)
                report=json.loads(next((out/'launcher-reports').glob('*.json')).read_text())
                assert report['connection']['identity']==name and report['execution']['launcher_mode']=='diagnostic'
                result['shared']['launcher_identity_verified']=True

    except Exception as exc:
        result.update(status='failed',error=type(exc).__name__+': '+str(exc))
    finally:
        try:
            if created_database:
                query('DROP DATABASE ' + name + (' WITH (FORCE);' if args.engine == 'postgresql' else ';'))
            if created_role:
                if args.engine == 'postgresql':
                    query('DROP OWNED BY ' + name + '; DROP ROLE ' + name + '; DROP ROLE IF EXISTS ' + name + '_denied;')
                else:
                    query("IF EXISTS(SELECT 1 FROM msdb.dbo.sysjobs WHERE name=N'" + name + "') EXEC msdb.dbo.sp_delete_job @job_name=N'" + name + "'; USE msdb; IF USER_ID(N'" + name + "') IS NOT NULL DROP USER [" + name + "]; USE master; DROP LOGIN [" + name + "];")
            result['cleanup'] = 'passed'
        except Exception as exc:
            result.update(status='failed', cleanup='failed', cleanup_error=str(exc))
        (out / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
