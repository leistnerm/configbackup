-- PostgreSQL 14+. Run as superuser for an EXISTING dedicated login.
-- pg_read_all_data and BYPASSRLS grant broad CLUSTER-WIDE data read access.
-- They grant no DML, DDL, job-control, replication or administrator rights.
DO $cb_check$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='configbackup_reader') THEN RAISE EXCEPTION 'Create the dedicated login securely first'; END IF; IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='configbackup_reader' AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication)) THEN RAISE EXCEPTION 'Use a new unprivileged dedicated role'; END IF; END $cb_check$;
ALTER ROLE "configbackup_reader" INHERIT BYPASSRLS;
GRANT pg_monitor, pg_read_all_data TO "configbackup_reader";
ALTER ROLE "configbackup_reader" SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE "ApplicationDB" TO "configbackup_reader";
-- Read-only transaction mode is a guardrail, not a permission boundary: the role can change its own default.
SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname='configbackup_reader';
SELECT parent.rolname AS inherited_role FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid WHERE m.member=(SELECT oid FROM pg_roles WHERE rolname='configbackup_reader');
