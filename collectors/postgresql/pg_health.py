"""Optional PostgreSQL health probes. Each query has its own failure boundary."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from telemetry import envelope, dataset, write_envelope


def collect_health(tools, database, output, cluster_only=False):
    payload = envelope('postgresql', tools.args.host or 'libpq-service', str(tools.args.port or 5432), database)
    old_timeout = tools.args.command_timeout
    old_options = tools.env.get('PGOPTIONS')
    timeout = max(1, min(int(tools.args.health_timeout), 300))
    tools.args.command_timeout = timeout + 5
    tools.env['PGOPTIONS'] = (old_options or '') + f' -c statement_timeout={timeout * 1000} -c lock_timeout=2000'

    def query(name, sql, keys, units, counters=(), reset=None):
        try:
            rows = tools.psql_rows(database, sql)
            dataset(payload, name, rows, keys, units, counters, reset)
            return rows
        except Exception as exc:
            payload['failures'].append({'section': name, 'error': str(exc)})
            return []
    try:
        if not cluster_only:
            query('database', """SELECT datname AS database, pg_database_size(oid) AS size_bytes,
              age(datfrozenxid)::bigint AS xid_age, mxid_age(datminmxid)::bigint AS multixact_age,
              100.0*age(datfrozenxid)/2147483647 AS xid_age_percent
              FROM pg_database WHERE datname=current_database()""", ['database'],
              {'size_bytes': 'bytes', 'xid_age': 'count', 'multixact_age': 'count', 'xid_age_percent': 'percent'})
            query('activity', """SELECT COUNT(*) AS connections_count,
              COUNT(*) FILTER (WHERE state='idle in transaction') AS idle_transactions_count,
              COUNT(*) FILTER (WHERE cardinality(pg_blocking_pids(pid))>0) AS blocked_count,
              COALESCE(MAX(EXTRACT(epoch FROM now()-xact_start)),0) AS oldest_transaction_seconds
              FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()""", [],
              {'connections_count': 'count', 'idle_transactions_count': 'count', 'blocked_count': 'count', 'oldest_transaction_seconds': 'seconds'})
            query('statistics', """SELECT deadlocks AS deadlocks_count,temp_bytes, xact_commit AS commits_count,
              xact_rollback AS rollbacks_count,stats_reset FROM pg_stat_database WHERE datname=current_database()""", [],
              {'deadlocks_count': 'count', 'temp_bytes': 'bytes', 'commits_count': 'count', 'rollbacks_count': 'count'},
              ['deadlocks_count','temp_bytes','commits_count','rollbacks_count'], 'stats_reset')
            query('table', """SELECT s.schemaname AS schema,s.relname AS table,
              pg_total_relation_size(s.relid) AS total_bytes, pg_relation_size(s.relid) AS table_bytes,
              pg_indexes_size(s.relid) AS index_bytes,
              CASE WHEN c.reltoastrelid<>0 THEN pg_total_relation_size(c.reltoastrelid) ELSE 0 END AS toast_bytes,
              s.n_live_tup AS live_rows_count,s.n_dead_tup AS dead_rows_count,
              100.0*s.n_dead_tup/NULLIF(s.n_live_tup+s.n_dead_tup,0) AS dead_rows_percent,
              s.n_mod_since_analyze AS modified_since_analyze_count,
              EXTRACT(epoch FROM now()-GREATEST(s.last_vacuum,s.last_autovacuum))/3600 AS vacuum_age_hours,
              EXTRACT(epoch FROM now()-GREATEST(s.last_analyze,s.last_autoanalyze))/3600 AS analyze_age_hours,
              age(c.relfrozenxid)::bigint AS xid_age
              FROM pg_stat_user_tables s JOIN pg_class c ON c.oid=s.relid ORDER BY s.schemaname,s.relname""",
              ['schema','table'], {'total_bytes':'bytes','table_bytes':'bytes','index_bytes':'bytes','toast_bytes':'bytes',
              'live_rows_count':'count','dead_rows_count':'count','dead_rows_percent':'percent',
              'modified_since_analyze_count':'count','vacuum_age_hours':'hours','analyze_age_hours':'hours','xid_age':'count'})
            query('index', """SELECT schemaname AS schema, relname AS table, indexrelname AS index,
              pg_relation_size(indexrelid) AS size_bytes,idx_scan AS scans_count
              FROM pg_stat_user_indexes ORDER BY schemaname,relname,indexrelname""", ['schema','table','index'],
              {'size_bytes':'bytes','scans_count':'count'})
            query('partition', """SELECT pn.nspname AS parent_schema,p.relname AS parent,
              cn.nspname AS schema,c.relname AS table,pg_total_relation_size(c.oid) AS size_bytes
              FROM pg_inherits i JOIN pg_class p ON p.oid=i.inhparent JOIN pg_class c ON c.oid=i.inhrelid
              JOIN pg_namespace pn ON pn.oid=p.relnamespace JOIN pg_namespace cn ON cn.oid=c.relnamespace
              WHERE c.relispartition ORDER BY 1,2,3,4""", ['parent_schema','parent','schema','table'], {'size_bytes':'bytes'})
        # Cluster facts are emitted once, from the configured maintenance database.
        if cluster_only:
            query('slot', """SELECT slot_name AS slot,slot_type,
              CASE WHEN active THEN 0 ELSE 1 END AS inactive,
              pg_wal_lsn_diff(CASE WHEN pg_is_in_recovery() THEN pg_last_wal_receive_lsn() ELSE pg_current_wal_lsn() END,restart_lsn) AS retained_bytes
              FROM pg_replication_slots ORDER BY slot_name""", ['slot','slot_type'], {'inactive':'boolean','retained_bytes':'bytes'})
            query('replication', """SELECT application_name,client_addr::text AS client,
              EXTRACT(epoch FROM write_lag) AS write_lag_seconds,
              EXTRACT(epoch FROM flush_lag) AS flush_lag_seconds,
              EXTRACT(epoch FROM replay_lag) AS replay_lag_seconds
              FROM pg_stat_replication ORDER BY application_name,client_addr""", ['application_name','client'],
              {'write_lag_seconds':'seconds','flush_lag_seconds':'seconds','replay_lag_seconds':'seconds'})
            query('archive', """SELECT archived_count,failed_count,stats_reset,
              CASE WHEN last_failed_time>COALESCE(last_archived_time,'-infinity'::timestamptz) THEN 1 ELSE 0 END AS failing,
              EXTRACT(epoch FROM now()-last_archived_time)/3600 AS last_archive_age_hours FROM pg_stat_archiver""", [],
              {'archived_count':'count','failed_count':'count','failing':'boolean','last_archive_age_hours':'hours'},
              ['archived_count','failed_count'], 'stats_reset')
            query('wal', 'SELECT COALESCE(SUM(size),0) AS size_bytes FROM pg_ls_waldir()', [], {'size_bytes':'bytes'})
            query('tablespace', "SELECT spcname AS tablespace,pg_tablespace_size(oid) AS size_bytes FROM pg_tablespace ORDER BY spcname",
                  ['tablespace'], {'size_bytes':'bytes'})
        for table in ([] if cluster_only else tools.args.bloat_table):
            literal = "'" + table.replace("'", "''") + "'"
            ext = tools.psql_rows(database, "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pgstattuple'")
            if not ext:
                payload['failures'].append({'section':'bloat:'+table,'error':'pgstattuple extension is not installed; nothing installed automatically'})
                continue
            schema = '"' + ext[0]['nspname'].replace('"','""') + '"'
            query('bloat:'+table, f"SELECT {literal} AS table,table_len AS size_bytes,scanned_percent,dead_tuple_percent,approx_free_percent FROM {schema}.pgstattuple_approx({literal}::regclass)",
                  ['table'], {'size_bytes':'bytes','scanned_percent':'percent','dead_tuple_percent':'percent','approx_free_percent':'percent'})
    except Exception as exc:
        payload['failures'].append({'section':'health','error':str(exc)})
    finally:
        tools.args.command_timeout = old_timeout
        if old_options is None: tools.env.pop('PGOPTIONS', None)
        else: tools.env['PGOPTIONS'] = old_options
        write_envelope(output, payload)
    return payload
