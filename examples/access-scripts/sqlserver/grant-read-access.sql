-- ConfigBackup read-only grants. Run as sysadmin, for an EXISTING dedicated login.
-- No SQLAgentReaderRole/UserRole, db_owner, db_datawriter, CONTROL or sysadmin membership is granted.
USE [master];
IF SUSER_ID(N'configbackup_reader') IS NULL THROW 51000, 'Create the dedicated login securely first', 1;
IF IS_SRVROLEMEMBER(N'sysadmin',N'configbackup_reader')=1 THROW 51000, 'Refusing an existing sysadmin account', 1;
GRANT CONNECT SQL TO [configbackup_reader];
GRANT VIEW ANY DATABASE TO [configbackup_reader];
GRANT VIEW ANY DEFINITION TO [configbackup_reader];
GRANT VIEW SERVER STATE TO [configbackup_reader];
IF CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) >= 16 BEGIN
  EXEC(N'GRANT VIEW SERVER PERFORMANCE STATE TO [configbackup_reader];');
  EXEC(N'GRANT VIEW ANY SECURITY DEFINITION TO [configbackup_reader];');
END;
USE [msdb];
IF USER_ID(N'configbackup_reader') IS NULL CREATE USER [configbackup_reader] FOR LOGIN [configbackup_reader];
GRANT CONNECT, VIEW DEFINITION, VIEW DATABASE STATE TO [configbackup_reader];
IF CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) >= 16
  EXEC(N'GRANT VIEW DATABASE PERFORMANCE STATE TO [configbackup_reader];');
GRANT SELECT ON OBJECT::dbo.[sysjobs] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysjobsteps] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysjobschedules] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysschedules] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysjobhistory] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysjobactivity] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[syssessions] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[syscategories] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysoperators] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysalerts] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysnotifications] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysproxies] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[sysproxysubsystem] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[syssubsystems] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[backupset] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[backupmediafamily] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[backupmediaset] TO [configbackup_reader];
GRANT SELECT ON OBJECT::dbo.[restorehistory] TO [configbackup_reader];
-- Audit effective permissions in msdb; investigate any unexpected ownership/write rights.
EXECUTE AS USER = N'configbackup_reader';
SELECT DB_NAME() AS database_name, * FROM sys.fn_my_permissions(NULL,'DATABASE');
REVERT;
USE [ApplicationDB];
IF USER_ID(N'configbackup_reader') IS NULL CREATE USER [configbackup_reader] FOR LOGIN [configbackup_reader];
GRANT CONNECT, VIEW DEFINITION, VIEW DATABASE STATE TO [configbackup_reader];
IF CONVERT(int, SERVERPROPERTY('ProductMajorVersion')) >= 16
  EXEC(N'GRANT VIEW DATABASE PERFORMANCE STATE TO [configbackup_reader];');
IF OBJECT_ID(N'dbo.syspublications') IS NOT NULL GRANT SELECT ON OBJECT::dbo.[syspublications] TO [configbackup_reader];
IF OBJECT_ID(N'dbo.sysarticles') IS NOT NULL GRANT SELECT ON OBJECT::dbo.[sysarticles] TO [configbackup_reader];
IF OBJECT_ID(N'dbo.syssubscriptions') IS NOT NULL GRANT SELECT ON OBJECT::dbo.[syssubscriptions] TO [configbackup_reader];
IF OBJECT_ID(N'dbo.sysmergepublications') IS NOT NULL GRANT SELECT ON OBJECT::dbo.[sysmergepublications] TO [configbackup_reader];
IF OBJECT_ID(N'dbo.sysmergearticles') IS NOT NULL GRANT SELECT ON OBJECT::dbo.[sysmergearticles] TO [configbackup_reader];
IF OBJECT_ID(N'dbo.sysmergesubscriptions') IS NOT NULL GRANT SELECT ON OBJECT::dbo.[sysmergesubscriptions] TO [configbackup_reader];
-- Audit effective permissions in ApplicationDB; investigate any unexpected ownership/write rights.
EXECUTE AS USER = N'configbackup_reader';
SELECT DB_NAME() AS database_name, * FROM sys.fn_my_permissions(NULL,'DATABASE');
REVERT;
