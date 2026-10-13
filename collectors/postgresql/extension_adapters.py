"""Optional fixed, read-only metadata queries for explicitly supported extensions.

Never read foreign table contents, invoke maintenance, install an extension, or
accept SQL from configuration. Absence preserves previous adapter snapshots.
"""
from configbackup_lib.engine.completeness import section
from configbackup_lib.engine.sections import enabled

PROVIDERS = ('postgres_fdw', 'postgis', 'pg_partman')


def queries(provider, schema):
    if provider not in PROVIDERS: raise ValueError('Unsupported extension adapter')
    ident = '"' + schema.replace('"', '""') + '"'
    if provider == 'postgres_fdw':
        return {
            'servers': "SELECT s.srvname AS server,pg_get_userbyid(s.srvowner) AS owner,o.option_name,o.option_value FROM pg_foreign_server s JOIN pg_foreign_data_wrapper w ON s.srvfdw=w.oid LEFT JOIN LATERAL pg_options_to_table(s.srvoptions) o ON true WHERE w.fdwname='postgres_fdw' ORDER BY s.srvname,o.option_name",
            'user-mappings': "SELECT s.srvname AS server,CASE WHEN m.umuser=0 THEN 'PUBLIC' ELSE pg_get_userbyid(m.umuser) END AS local_role,o.option_name,o.option_value FROM pg_user_mapping m JOIN pg_foreign_server s ON m.umserver=s.oid JOIN pg_foreign_data_wrapper w ON s.srvfdw=w.oid LEFT JOIN LATERAL pg_options_to_table(m.umoptions) o ON true WHERE w.fdwname='postgres_fdw' ORDER BY s.srvname,local_role,o.option_name",
            'foreign-tables': "SELECT n.nspname AS schema_name,c.relname AS table_name,s.srvname AS server,o.option_name,o.option_value FROM pg_foreign_table f JOIN pg_class c ON f.ftrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid JOIN pg_foreign_server s ON f.ftserver=s.oid JOIN pg_foreign_data_wrapper w ON s.srvfdw=w.oid LEFT JOIN LATERAL pg_options_to_table(f.ftoptions) o ON true WHERE w.fdwname='postgres_fdw' ORDER BY n.nspname,c.relname,o.option_name",
            'column-options': "SELECT n.nspname AS schema_name,c.relname AS table_name,a.attname AS column_name,o.option_name,o.option_value FROM pg_foreign_table f JOIN pg_class c ON f.ftrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid JOIN pg_foreign_server s ON f.ftserver=s.oid JOIN pg_foreign_data_wrapper w ON s.srvfdw=w.oid JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped CROSS JOIN LATERAL pg_options_to_table(a.attfdwoptions) o WHERE w.fdwname='postgres_fdw' ORDER BY n.nspname,c.relname,a.attnum,o.option_name",
        }
    if provider == 'postgis':
        return {
            'geometry-columns': f'SELECT f_table_catalog,f_table_schema,f_table_name,f_geometry_column,coord_dimension,srid,type FROM {ident}.geometry_columns ORDER BY f_table_schema,f_table_name,f_geometry_column',
            'geography-columns': f'SELECT f_table_catalog,f_table_schema,f_table_name,f_geography_column,coord_dimension,srid,type FROM {ident}.geography_columns ORDER BY f_table_schema,f_table_name,f_geography_column',
            'referenced-spatial-systems': f'SELECT srid,auth_name,auth_srid,srtext,proj4text FROM {ident}.spatial_ref_sys WHERE srid IN (SELECT srid FROM {ident}.geometry_columns UNION SELECT srid FROM {ident}.geography_columns) ORDER BY srid',
        }
    return {
        'partition-sets': f'SELECT parent_table,control,partition_interval,premake,retention,retention_keep_table,retention_keep_index,infinite_time_partitions,automatic_maintenance FROM {ident}.part_config ORDER BY parent_table'
    }


def collect(tools, database, root, scope, providers, failures, capture_file, redact_text):
    for provider in dict.fromkeys(providers):
        if provider not in PROVIDERS: raise ValueError('Unsupported extension adapter: ' + provider)
        prefix = scope + '/extension-adapters/' + provider
        if not enabled('extension-adapters') or not enabled(prefix):
            tools.collection_scopes.append(section(root, prefix, 'disabled', 'Disabled by configuration')); continue
        try:
            # provider is an exact allowlisted literal, schema is quoted as an identifier.
            rows = tools.psql_rows(database, "SELECT e.extname AS name,e.extversion AS version,n.nspname AS schema_name FROM pg_extension e JOIN pg_namespace n ON e.extnamespace=n.oid WHERE e.extname='" + provider + "' ORDER BY e.extname")
            if not rows:
                tools.collection_scopes.append(section(root, prefix, 'not_applicable', 'Extension is not installed; previous snapshot preserved')); continue
            if len(rows)!=1 or not isinstance(rows[0].get('schema_name'),str): raise ValueError('Invalid extension identity')
        except Exception:
            message = 'Extension discovery failed; check metadata permissions and server compatibility'
            failures.append({'section':prefix,'error':message,'required':'false'})
            tools.collection_scopes.append(section(root,prefix,'failed',message)); continue
        identity = rows[0]
        capture_file(tools,root,prefix+'/identity.csv',failures,prefix,lambda:[identity],group='extension-adapters')
        for name, query in sorted(queries(provider, identity['schema_name']).items()):
            def read(query=query):
                values = tools.psql_rows(database,query)
                return [{**row, 'option_value':redact_text(row.get('option_value'),name=str(row.get('option_name') or ''))}
                        if 'option_value' in row else row for row in values]
            capture_file(tools,root,prefix+'/'+name+'.csv',failures,prefix+'/'+name,read,group='extension-adapters')
