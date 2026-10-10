"""Generate administrator-reviewed read access grants. Never connects or applies them."""
from pathlib import Path
import os


def sql_id(name):return '['+name.replace(']',']]')+']'
def sql_value(name):return "N'"+name.replace("'","''")+"'"
def pg_id(name):return '"'+name.replace('"','""')+'"'
def pg_value(name):return "'"+name.replace("'","''")+"'"


def generate(engine,principal,databases,directory):
    if engine not in ('sqlserver','postgresql'):raise ValueError('Unknown database engine')
    if not principal or any(not n or '\x00' in n or '\n' in n or '\r' in n for n in [principal,*databases]):raise ValueError('Principal and database names must be nonempty single lines')
    if not databases or len(set(databases))!=len(databases):raise ValueError('Choose unique database names')
    out=Path(directory);out.mkdir(parents=True,exist_ok=True);files={}
    common=['Run as an administrator after reviewing the generated SQL. These scripts do not create logins or store passwords.',
      'Use a NEW dedicated account. Existing ownership, inherited roles, PUBLIC grants and executable privileged procedures/functions can add write powers.',
      'The grants add read/metadata access only. They cannot universally prove that an arbitrary existing account has no write path.',
      'Read access can expose confidential data, SQL text and job commands. Protect output and inspect the audit results.',
      'No database is modified by the generator. Applying the SQL changes permissions; no data or application objects are changed.',
      'Use the collector read-only access profile. Privileged instance script exports and protected services can remain failed/preserved. See docs/read-only-access.md.']
    if engine=='sqlserver':
        if any(len(n)>128 for n in [principal,*databases]):raise ValueError('SQL Server identifiers cannot exceed 128 characters')
        p=sql_id(principal);v=sql_value(principal)
        lines=['-- ConfigBackup read-only grants. Run as sysadmin, for an EXISTING dedicated login.',
          '-- No SQLAgentReaderRole/UserRole, db_owner, db_datawriter, CONTROL or sysadmin membership is granted.',
          'USE [master];',f"IF SUSER_ID({v}) IS NULL THROW 51000, 'Create the dedicated login securely first', 1;",
          f"IF IS_SRVROLEMEMBER(N'sysadmin',{v})=1 THROW 51000, 'Refusing an existing sysadmin account', 1;",
          *[f'GRANT {permission} TO {p};' for permission in ('CONNECT SQL','VIEW ANY DATABASE','VIEW ANY DEFINITION','VIEW SERVER STATE')],
          "IF CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) >= 16 BEGIN",
          f"  EXEC({sql_value('GRANT VIEW SERVER PERFORMANCE STATE TO '+p+';')});",
          f"  EXEC({sql_value('GRANT VIEW ANY SECURITY DEFINITION TO '+p+';')});",'END;']
        for db in list(dict.fromkeys(['msdb',*databases])):
            lines += [f'USE {sql_id(db)};',f'IF USER_ID({v}) IS NULL CREATE USER {p} FOR LOGIN {p};',
                      f'GRANT CONNECT, VIEW DEFINITION, VIEW DATABASE STATE TO {p};',
                      "IF CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) >= 16",
                      f'  EXEC({sql_value("GRANT VIEW DATABASE PERFORMANCE STATE TO "+p+";")});']
            if db=='msdb':
                for name in ('sysjobs','sysjobsteps','sysjobschedules','sysschedules','sysjobhistory','sysjobactivity','syssessions','syscategories','sysoperators','sysalerts','sysnotifications','sysproxies','sysproxysubsystem','syssubsystems','backupset','backupmediafamily','backupmediaset','restorehistory'):
                    lines += [f'GRANT SELECT ON OBJECT::dbo.{sql_id(name)} TO {p};']
            else:
                for name in ('syspublications','sysarticles','syssubscriptions','sysmergepublications','sysmergearticles','sysmergesubscriptions'):
                    lines += [f'IF OBJECT_ID(N\'dbo.{name}\') IS NOT NULL GRANT SELECT ON OBJECT::dbo.{sql_id(name)} TO {p};']
            lines += [f'-- Audit effective permissions in {db}; investigate any unexpected ownership/write rights.',
                      f'EXECUTE AS USER = {v};',"SELECT DB_NAME() AS database_name, * FROM sys.fn_my_permissions(NULL,'DATABASE');",'REVERT;']
        files['grant-read-access.sql']='\n'.join(lines)+'\n'
        common += ['SQL Server: authenticate the login with Windows/Kerberos where available, or provision its password outside this script. Run the collector with -ReadOnlyAccess.',
                   'Metadata access does not grant SELECT on application tables. Performance DMVs can expose workload text. Job catalog SELECT exposes commands, potentially including embedded credentials.',
                   'Rerun for newly selected databases. Offline/restoring databases cannot receive database grants until accessible. The script stops on SQL errors; rerunning existing grants is safe.']
    else:
        if len(principal.encode())>63 or any(len(n.encode())>63 for n in databases):raise ValueError('PostgreSQL identifiers exceed 63 UTF-8 bytes')
        p=pg_id(principal);v=pg_value(principal)
        files['grant-cluster-read-access.sql']='\n'.join([
          '-- PostgreSQL 14+. Run as superuser for an EXISTING dedicated login.',
          '-- pg_read_all_data and BYPASSRLS grant broad CLUSTER-WIDE data read access.',
          '-- They grant no DML, DDL, job-control, replication or administrator rights.',
          f"DO $cb_check$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={v}) THEN RAISE EXCEPTION 'Create the dedicated login securely first'; END IF; IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname={v} AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication)) THEN RAISE EXCEPTION 'Use a new unprivileged dedicated role'; END IF; END $cb_check$;",
          f'ALTER ROLE {p} INHERIT BYPASSRLS;',
          f'GRANT pg_monitor, pg_read_all_data TO {p};',
          f"ALTER ROLE {p} SET default_transaction_read_only = on;",
          *[f'GRANT CONNECT ON DATABASE {pg_id(db)} TO {p};' for db in databases],
          '-- Read-only transaction mode is a guardrail, not a permission boundary: the role can change its own default.',
          f'SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname={v};',
          f'SELECT parent.rolname AS inherited_role FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname={v});'])+'\n'
        # Run this separately in each target DB; no unescaped psql meta-commands.
        files['audit-database-access.sql']='\n'.join([
          '-- Run in EACH selected database as an administrator. Review every returned row.',
          f'SELECT nspname AS schema_with_create FROM pg_namespace WHERE has_schema_privilege({v},oid,\'CREATE\');',
          f"SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND (has_table_privilege({v},c.oid,'INSERT') OR has_table_privilege({v},c.oid,'UPDATE') OR has_table_privilege({v},c.oid,'DELETE') OR has_table_privilege({v},c.oid,'TRUNCATE'));",
          f"SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS arguments FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND p.prosecdef AND has_function_privilege({v},p.oid,'EXECUTE');",
          '-- PostgreSQL has no per-user DENY overriding PUBLIC. Do not revoke PUBLIC privileges blindly; review application/service requirements first.'])+'\n'
        files['grant-file-settings-read.sql']='\n'.join([
          '-- Optional: run in the MAINTENANCE database only. These built-in functions read parsed configuration; no pg_read_server_files role is granted.',
          f'GRANT EXECUTE ON FUNCTION pg_catalog.pg_show_all_file_settings() TO {p};',
          f'GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO {p};',
          f"DO $cb_grant$ BEGIN IF to_regprocedure('pg_catalog.pg_ident_file_mappings()') IS NOT NULL THEN EXECUTE {pg_value('GRANT EXECUTE ON FUNCTION pg_catalog.pg_ident_file_mappings() TO '+p+';')}; END IF; END $cb_grant$;"])+'\n'
        common += ['PostgreSQL: pg_read_all_data applies across the cluster, including future tables/schemas; this is broader than the databases selected for collection. BYPASSRLS prevents scheduler/table policies from silently hiding metadata.',
          'If cluster-wide data read or BYPASSRLS is unacceptable, do not apply this profile: retain the full-visibility collector requirement or design a separately audited narrower inventory.',
          'Run audit-database-access.sql in each database, and remove inappropriate existing permissions through your normal admin process. PUBLIC CREATE and SECURITY DEFINER execution can defeat a read-only identity.',
          'Use --read-only-access. Authentication remains pg_service.conf/pgpass, peer/Kerberos or environment secret injection; this generator never embeds a password.']
    files['READ-BEFORE-APPLY.txt']='\n\n'.join(common)+'\n'
    if any((out/name).exists() for name in files):raise ValueError('Access script files already exist; choose a fresh directory')
    created=[]
    try:
        for name,content in files.items():
            target=out/name;fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(content)
            created.append(target)
    except Exception:
        for target in created:target.unlink(missing_ok=True)
        raise
    return created
