-- Optional: run in the MAINTENANCE database only. These built-in functions read parsed configuration; no pg_read_server_files role is granted.
GRANT EXECUTE ON FUNCTION pg_catalog.pg_show_all_file_settings() TO "configbackup_reader";
GRANT EXECUTE ON FUNCTION pg_catalog.pg_hba_file_rules() TO "configbackup_reader";
DO $cb_grant$ BEGIN IF to_regprocedure('pg_catalog.pg_ident_file_mappings()') IS NOT NULL THEN EXECUTE 'GRANT EXECUTE ON FUNCTION pg_catalog.pg_ident_file_mappings() TO "configbackup_reader";'; END IF; END $cb_grant$;
