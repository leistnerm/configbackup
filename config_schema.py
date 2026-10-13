"""Shared, pre-coercion validation and field types for the configuration editor."""
BOOL_FIELDS = frozenset('enabled required run_on_failure clean_output allow_clean_outside_staging collection_manifest recursive follow_symlinks include_hostname include_configuration include_related_configuration stop_on_error push auto_init draft include_health include_history schema trust_server_certificate encrypt default_rules html immediate digest include_defaults forever include_operational_inventory include_values export_native'.split())
BOOL_FIELDS = BOOL_FIELDS | frozenset('include_system_databases include_tempdb verify_schema_extraction include_capacity include_performance include_index_health skip_inventory skip_agent skip_ssis skip_ispac include_legacy_ssis collect_local_host_configuration include_template_databases include_sizes split_table_ddl skip_object_files skip_schedulers'.split())
MAP_FIELDS = frozenset('backup internal options logging deletion variables retention_policies defaults git monitoring reporting reports analysis environment collection sections retention notifications integrity performance recording collection_profiles'.split())
LIST_FIELDS = frozenset('tasks arguments depends_on include exclude required_sections databases sources channels rules paths schedule_paths snapshots extension_adapters public_certificates web_configs container_engines windows_components windows_details'.split())
LIST_FIELDS = LIST_FIELDS | {'exclude_databases','instance_exclude','bloat_tables','runtime_paths','configuration_files'}
INT_FIELDS = frozenset('port missing_runs max_items_per_run min_items_for_percent max_bytes backup_count max_versions min_versions grace_days max_age_days purge_after_days max_events max_rows history_days connect_timeout'.split())
INT_FIELDS = INT_FIELDS | {'health_query_timeout','health_index_limit','agent_history_days','health_timeout','command_timeout','dump_timeout','timeout','max_scope_bytes','max_files','max_preview_bytes','max_output_bytes','max_file_bytes','max_report_bytes','history_points'}
NUMBER_FIELDS = frozenset('collection_interval_hours run_budget_seconds timeout warning critical consecutive digest_hours max_percent_per_run stale_hours max_source_age_hours'.split())


def field_type(name, current=None):
    if name in BOOL_FIELDS:return bool
    if name in MAP_FIELDS:return dict
    if name in LIST_FIELDS:return list
    if name in INT_FIELDS:return int
    if name in NUMBER_FIELDS:return float
    return type(current) if current is not None else str


def validate_types(config):
    """Reject quoted booleans before truthiness/coercion can enable a collector.

    Environment/variable maps and arbitrary command arguments are data, not fields.
    Legacy string forms for include/exclude/depends_on remain supported by the engine.
    """
    if not isinstance(config,dict):raise ValueError('Configuration must be a mapping')
    def visit(value, path=''):
        if not isinstance(value,dict):return
        for key,item in value.items():
            label=(path+'.' if path else '')+str(key)
            if label=='git.sensitive_data':continue  # Validated separately below.
            if key=='sections' and 'channels[' in path and path.endswith(']') and isinstance(item,list):
                if any(not isinstance(v,str) for v in item):raise ValueError(label+' must contain email section names')
                continue
            if key in BOOL_FIELDS and not isinstance(item,bool):
                raise ValueError(label+' must be true or false (unquoted)')
            if key in MAP_FIELDS and item is not None and not isinstance(item,dict):
                raise ValueError(label+' must be a mapping')
            if key in ('variables','environment'):continue
            if key=='sections':
                if item is None:continue
                for pattern,setting in item.items():
                    if isinstance(setting,dict):
                        if set(setting)-{'enabled'}:raise ValueError(label+'.'+pattern+' supports only enabled')
                        if not isinstance(setting.get('enabled',True),bool):raise ValueError(label+'.'+pattern+'.enabled must be boolean')
                    elif not isinstance(setting,bool):raise ValueError(label+'.'+pattern+' must be a boolean or enabled mapping')
                continue
            if key in ('connections','retention_policies','collection_profiles'):
                if not isinstance(item,dict):raise ValueError(label+' must be a mapping')
                for name,profile in item.items():visit(profile,label+'.'+str(name))
            elif isinstance(item,dict):visit(item,label)
            elif isinstance(item,list):
                for n,child in enumerate(item):visit(child,label+'['+str(n)+']')
    visit(config)
    from sensitive_data import validate_git, validate_sections
    validate_git(config.get('git') or {})
    if 'sensitive_sections' in (config.get('defaults') or {}):raise ValueError('Sensitive collection cannot be enabled through task defaults')
    for task in config.get('tasks', []):
        if 'sensitive_sections' in task:
            if task.get('type') != 'execute':raise ValueError('sensitive_sections applies only to collector execute tasks')
            validate_sections(task['sensitive_sections'])
