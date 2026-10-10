# Optional read-only health queries. Dot-sourced by the configuration collector.
function Export-SqlHealth {
    param($ServerObject, [string[]]$Databases, [string]$Target,
          [int]$QueryTimeout=15, [switch]$IndexHealth, [int]$IndexLimit=25)
    $payload = [ordered]@{schema_version=1;engine='sqlserver';host=[string]$ServerObject.NetName;
        instance=[string]$ServerObject.Name;observed_at=[DateTime]::UtcNow.ToString('o');datasets=@{};failures=@()}
    function Add-HealthQuery {
        param([string]$Name,[string]$Db,[string]$Query,[string[]]$Keys=@(),[string[]]$Counters=@(),[string]$ResetField='')
        try {
            $ds = Invoke-DbaQuery -SqlInstance $ServerObject -Database $Db -Query ("SET LOCK_TIMEOUT 2000; " + $Query) -QueryTimeout $QueryTimeout -As DataSet -EnableException
            $rows = @(); $units = @{}
            if ($null -ne $ds -and $ds.Tables.Count -gt 0) {
                foreach ($r in $ds.Tables[0].Rows) {
                    $value = [ordered]@{database=$Db}
                    foreach ($c in $ds.Tables[0].Columns) {
                        $v=$r[$c.ColumnName]; if ($v -is [DBNull]) { $v=$null }
                        $value[$c.ColumnName]=$v
                        if ($c.ColumnName -notin $Keys -and $c.ColumnName -match '_(bytes|percent|seconds|hours|count|days)$') { $units[$c.ColumnName]=$Matches[1] }
                    }
                    $rows += [pscustomobject]$value
                }
            }
            $payload.datasets[$Name]=@{rows=@($rows);keys=@('database')+@($Keys);units=$units;counters=@($Counters);reset_field=$ResetField}
        } catch { $payload.failures += @{section=$Name;database=$Db;error=$_.Exception.Message} }
    }
    Add-HealthQuery 'volume' 'master' @'
SELECT DISTINCT COALESCE(v.volume_mount_point,CONVERT(nvarchar(20),f.database_id)+':'+CONVERT(nvarchar(20),f.file_id)) AS volume,
 v.total_bytes,v.available_bytes AS free_bytes,100.0*v.available_bytes/NULLIF(v.total_bytes,0) AS free_percent
FROM sys.master_files f CROSS APPLY sys.dm_os_volume_stats(f.database_id,f.file_id) v;
'@ @('volume')
    Add-HealthQuery 'activity' 'master' @'
SELECT COUNT(*) AS requests_count,SUM(CASE WHEN blocking_session_id>0 THEN 1 ELSE 0 END) AS blocked_count,
 COALESCE(MAX(CASE WHEN blocking_session_id>0 THEN wait_time/1000.0 END),0) AS blocking_seconds
FROM sys.dm_exec_requests WHERE session_id<>@@SPID AND session_id>50;
'@
    Add-HealthQuery 'transaction' 'master' 'SELECT COALESCE(MAX(DATEDIFF_BIG(second,transaction_begin_time,GETDATE())),0) AS oldest_seconds FROM sys.dm_tran_active_transactions WHERE transaction_type=1;'
    Add-HealthQuery 'availability' 'master' @'
SELECT DB_NAME(database_id) AS db_name,CONVERT(nvarchar(36),replica_id) AS replica,
 log_send_queue_size*CONVERT(bigint,1024) AS send_queue_bytes,redo_queue_size*CONVERT(bigint,1024) AS redo_queue_bytes,
 CASE WHEN synchronization_health=2 THEN 0 ELSE 1 END AS unhealthy_count,is_suspended AS suspended_count
FROM sys.dm_hadr_database_replica_states;
'@ @('db_name','replica')
    Add-HealthQuery 'tempdb' 'tempdb' @'
SELECT SUM(unallocated_extent_page_count)*CONVERT(bigint,8192) AS free_bytes,
 SUM(version_store_reserved_page_count)*CONVERT(bigint,8192) AS version_store_bytes,
 SUM(user_object_reserved_page_count)*CONVERT(bigint,8192) AS user_objects_bytes,
 SUM(internal_object_reserved_page_count)*CONVERT(bigint,8192) AS internal_objects_bytes FROM sys.dm_db_file_space_usage;
'@
    Add-HealthQuery 'backups' 'msdb' @'
SELECT d.name AS db_name,d.recovery_model_desc AS recovery_model,
 DATEDIFF_BIG(second,MAX(CASE WHEN b.type='D' THEN b.backup_finish_date END),GETDATE())/3600.0 AS full_age_hours,
 DATEDIFF_BIG(second,MAX(CASE WHEN b.type='L' THEN b.backup_finish_date END),GETDATE())/3600.0 AS log_age_hours,
 CASE WHEN MAX(CASE WHEN b.type='D' THEN b.backup_finish_date END) IS NULL THEN 1 ELSE 0 END AS missing_full_count,
 CASE WHEN d.recovery_model_desc<>'SIMPLE' AND MAX(CASE WHEN b.type='L' THEN b.backup_finish_date END) IS NULL THEN 1 ELSE 0 END AS missing_log_count
FROM sys.databases d LEFT JOIN msdb.dbo.backupset b ON b.database_name=d.name
WHERE d.database_id<>2 GROUP BY d.name,d.recovery_model_desc;
'@ @('db_name','recovery_model')
    foreach ($db in $Databases) {
        $tag = 'db/' + $db + '/'
        Add-HealthQuery ($tag+'file') $db @'
SELECT name AS file_name,type_desc AS file_type,physical_name AS path,
 size*CONVERT(bigint,8192) AS size_bytes,
 CASE WHEN type=0 THEN FILEPROPERTY(name,'SpaceUsed')*CONVERT(bigint,8192) END AS used_bytes,
 CASE WHEN type=0 THEN (size-FILEPROPERTY(name,'SpaceUsed'))*CONVERT(bigint,8192) END AS free_bytes,
 CASE WHEN type=0 THEN 100.0*(size-FILEPROPERTY(name,'SpaceUsed'))/NULLIF(size,0) END AS free_percent,
 CASE WHEN max_size>0 THEN max_size*CONVERT(bigint,8192) END AS max_bytes,
 CASE WHEN max_size>0 THEN (max_size-size)*CONVERT(bigint,8192) END AS growth_headroom_bytes,
 CASE WHEN growth=0 THEN 1 ELSE 0 END AS autogrowth_disabled_count
FROM sys.database_files;
'@ @('file_name','file_type','path')
        Add-HealthQuery ($tag+'log') $db @'
SELECT total_log_size_in_bytes AS size_bytes,used_log_space_in_bytes AS used_bytes,
 used_log_space_in_percent AS used_percent,log_space_in_bytes_since_last_backup AS since_backup_bytes,
 (SELECT log_reuse_wait_desc FROM sys.databases WHERE database_id=DB_ID()) AS reuse_wait
FROM sys.dm_db_log_space_usage;
'@
        Add-HealthQuery ($tag+'partition') $db @'
SELECT s.name AS schema_name,t.name AS table_name,i.name AS index_name,p.partition_number AS partition,
 SUM(p.reserved_page_count)*CONVERT(bigint,8192) AS reserved_bytes,SUM(p.used_page_count)*CONVERT(bigint,8192) AS used_bytes,
 SUM(p.row_count) AS rows_count
FROM sys.dm_db_partition_stats p JOIN sys.tables t ON t.object_id=p.object_id
JOIN sys.schemas s ON s.schema_id=t.schema_id JOIN sys.indexes i ON i.object_id=p.object_id AND i.index_id=p.index_id
GROUP BY s.name,t.name,i.name,p.partition_number;
'@ @('schema_name','table_name','index_name','partition')
        Add-HealthQuery ($tag+'statistics') $db @'
SELECT OBJECT_SCHEMA_NAME(s.object_id) AS schema_name,OBJECT_NAME(s.object_id) AS table_name,s.name AS statistics_name,
 DATEDIFF_BIG(second,p.last_updated,GETDATE())/3600.0 AS age_hours,p.rows AS rows_count,
 p.modification_counter AS modifications_count,100.0*p.modification_counter/NULLIF(p.rows,0) AS modified_percent
FROM sys.stats s CROSS APPLY sys.dm_db_stats_properties(s.object_id,s.stats_id) p
JOIN sys.tables t ON t.object_id=s.object_id WHERE t.is_ms_shipped=0;
'@ @('schema_name','table_name','statistics_name')
        Add-HealthQuery ($tag+'columnstore') $db @'
SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name,OBJECT_NAME(object_id) AS table_name,
 index_id AS index_number,partition_number AS partition,state_desc AS rowgroup_state,
 SUM(total_rows) AS rows_count,SUM(deleted_rows) AS deleted_rows_count,
 100.0*SUM(deleted_rows)/NULLIF(SUM(total_rows),0) AS deleted_percent,COUNT(*) AS rowgroups_count
FROM sys.dm_db_column_store_row_group_physical_stats
GROUP BY object_id,index_id,partition_number,state_desc;
'@ @('schema_name','table_name','index_number','partition','rowgroup_state')
        Add-HealthQuery ($tag+'oltp') $db @'
SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name,OBJECT_NAME(object_id) AS table_name,
 memory_allocated_for_table_kb*CONVERT(bigint,1024) AS table_allocated_bytes,
 memory_used_by_table_kb*CONVERT(bigint,1024) AS table_used_bytes,
 memory_allocated_for_indexes_kb*CONVERT(bigint,1024) AS indexes_allocated_bytes,
 memory_used_by_indexes_kb*CONVERT(bigint,1024) AS indexes_used_bytes FROM sys.dm_db_xtp_table_memory_stats WHERE object_id>0;
'@ @('schema_name','table_name')
        Add-HealthQuery ($tag+'hash_index') $db @'
SELECT OBJECT_SCHEMA_NAME(object_id) AS schema_name,OBJECT_NAME(object_id) AS table_name,index_id AS index_number,
 total_bucket_count AS buckets_count,empty_bucket_count AS empty_buckets_count,avg_chain_length AS average_chain_count,
 max_chain_length AS maximum_chain_count FROM sys.dm_db_xtp_hash_index_stats;
'@ @('schema_name','table_name','index_number')
        Add-HealthQuery ($tag+'fulltext') $db @'
SELECT name AS catalog,FULLTEXTCATALOGPROPERTY(name,'PopulateStatus') AS population_status_count,
 FULLTEXTCATALOGPROPERTY(name,'ItemCount') AS items_count FROM sys.fulltext_catalogs;
'@ @('catalog')
        Add-HealthQuery ($tag+'integrity') $db "SELECT DATEDIFF_BIG(second,NULLIF(TRY_CONVERT(datetime,DATABASEPROPERTYEX(DB_NAME(),'LastGoodCheckDbTime')),CONVERT(datetime,'19000101',112)),GETDATE())/3600.0 AS check_age_hours;"
        if ($IndexHealth) {
            # Scope the work to bounded, sizeable rowstore candidates; do not scan AG secondaries.
            $indexQuery = @"
IF COALESCE(sys.fn_hadr_is_primary_replica(DB_NAME()),1)=0 THROW 50001,'Index inspection skipped on AG secondary',1;
SELECT TOP ($IndexLimit) p.object_id,p.index_id,p.partition_number INTO #cb_indexes
FROM sys.dm_db_partition_stats p JOIN sys.indexes i ON i.object_id=p.object_id AND i.index_id=p.index_id
WHERE i.type IN (1,2) AND p.used_page_count>=128 ORDER BY p.used_page_count DESC,p.object_id,p.index_id,p.partition_number;
SELECT OBJECT_SCHEMA_NAME(c.object_id) AS schema_name,OBJECT_NAME(c.object_id) AS table_name,i.name AS index_name,
 c.partition_number AS partition,i.fill_factor AS fill_factor_percent,s.page_count AS pages_count,
 s.avg_fragmentation_in_percent AS fragmentation_percent,s.avg_page_space_used_in_percent AS density_percent,
 COALESCE(u.user_seeks,0)+COALESCE(u.user_scans,0)+COALESCE(u.user_lookups,0) AS reads_count,
 CONVERT(varchar(33),(SELECT sqlserver_start_time FROM sys.dm_os_sys_info),126) AS reset_at
FROM #cb_indexes c CROSS APPLY sys.dm_db_index_physical_stats(DB_ID(),c.object_id,c.index_id,c.partition_number,'SAMPLED') s
JOIN sys.indexes i ON i.object_id=c.object_id AND i.index_id=c.index_id
LEFT JOIN sys.dm_db_index_usage_stats u ON u.database_id=DB_ID() AND u.object_id=c.object_id AND u.index_id=c.index_id
WHERE s.index_level=0 AND s.alloc_unit_type_desc='IN_ROW_DATA';
DROP TABLE #cb_indexes;
"@
            Add-HealthQuery ($tag+'index') $db $indexQuery @('schema_name','table_name','index_name','partition') @('reads_count') 'reset_at'
        }
    }
    Write-StableJson -Path $Target -Value $payload
}
