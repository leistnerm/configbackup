# Database collection

## Preservation and independent failures

A failed PostgreSQL globals export no longer prevents database collection. Cluster roles, memberships, settings, HBA/ident rules, tablespaces, globals and slot configuration have independent manifest file scopes. Database inventory queries also have individual file scopes. Failure preserves that file's previous filesystem/Git snapshot; successful siblings can update. A schema extraction or database visibility/preflight failure still preserves the **whole database**, then continues to the next database. Object-DDL and scheduler exports currently use group boundaries: one failed query preserves that group while ordinary inventory continues. Collector output directories must remain fresh and archive tasks must use `collection_manifest: true`.

SQL Server isolates optional database inventory/catalog queries and optional operational queries. A schema/preflight failure retains the whole previous database. Disabled schema/inventory remain protected; individual full manifest paths can also be disabled. Old archive state needs no file migration because logical file paths stay the same. Existing snapshots not covered by a new complete scope remain preserved.


## Authentication option protection

PostgreSQL HBA options are read as structured arrays. LDAP bind passwords, RADIUS secrets and other credential-named option values are removed before CSV serialization. The rule names, addresses and nonsecret options remain available. HBA parser error text can echo authentication lines, so it is replaced with a local-review marker. A malformed/unrecognized options representation fails only that HBA scope and preserves its prior snapshot.

The Git gate additionally detects `ldapbindpasswd`/`radiussecret(s)` assignments and PostgreSQL option literals, including pg_dump's quoted `"password" 'value'` spelling. Native PostgreSQL schema files remain authoritative and may contain stored FDW credentials or secrets in application SQL; they are not broadly rewritten. Protect filesystem archives and review rejected Git content. The gate is a conservative pattern check, not proof that arbitrary configuration contains no secrets.

## Optional SQL operational inventory

Enable `-IncludeOperationalInventory` in a direct PowerShell invocation, or use a managed connection/task setting:

```yaml
connections:
  sql:
    engine: sqlserver
    server: sql.example.invalid
    access_profile: read-only
    include_operational_inventory: true
```

Output lives under `databases/<database>/operational/`, one independent CSV per query:

- Service Broker queues/activation, services, message validation, contracts and message mappings, service-contract mappings, routes and remote-binding identities.
- CDC capture instances, captured columns and capture/cleanup job settings.
- Certificate public identity/subject/issuer/thumbprint/validity/private-key-protection type; asymmetric/symmetric key identities and protection metadata.
- Database-scoped credential identities, external data source/file-format configuration, CLR assembly identities and references.

This documents dependencies and selected operational configuration. It does not read queue messages, CDC captured rows, certificate private keys, symmetric keys, credential passwords, assembly binaries, external data or remote endpoints. It does not create, enable, start or repair these features. CDC catalog queries use explicit documented columns, which may vary across engine releases. Unsupported properties, denied access or unavailable catalogs fail their query rather than being silently certified as empty. It is not a TDE/key backup, Service Broker recovery tool or complete external-provider installation inventory.

Use `sections.operational.enabled: false` to disable the group, or a path such as `databases/Example/operational/certificates.csv` for a specific query. Path names follow the collector's database-map.csv; database names requiring portable escaping have encoded directory names.

## Optional PostgreSQL extension adapters

The existing extension inventory lists every installed extension. These additional adapters read selected provider configuration using fixed queries:

```sh
python collectors/postgresql/collect_postgresql.py --output /fresh/pg \
  --extension-adapter postgres_fdw --extension-adapter postgis \
  --extension-adapter pg_partman
```

Managed profile/task equivalent:

```yaml
connections:
  pg:
    engine: postgresql
    service: configbackup
    access_profile: read-only
    extension_adapters: [postgres_fdw, postgis, pg_partman]
```

| Adapter | Included metadata | Limits |
|---|---|---|
| `postgres_fdw` | Server options, local user mappings with credential-named options redacted, foreign table and column options | Reads local catalogs only. Never contacts foreign endpoints or reads foreign-table contents. Generic extension inventory/native schema remain separate. |
| `postgis` | Geometry/geography column metadata and spatial-reference definitions used by those columns | No geometry data, topology/raster coverage, extension binaries or transform-grid files. Requires the expected metadata views. |
| `pg_partman` | Parent/control/partition interval, premake, retention and automatic-maintenance settings in `part_config` | No partition maintenance calls, all-version schema promise, subpartition overrides or OS/background-worker deployment. Missing expected columns fail this query. |

Only these exact provider names are accepted. There is no configuration field for arbitrary SQL. Namespaces returned by extension discovery are quoted as SQL identifiers. Missing extensions are `not_applicable` and preserve previous adapter snapshots; missing permissions or incompatible catalogs are failures. Each successful query is certified independently. Disable all adapters with `sections.extension-adapters.enabled: false`, or use a complete path beneath `databases/<database>/extension-adapters/`.

## Validation and reproduction

`tests/test_database_isolation.py` covers HBA redaction/secret-gate variants, failure and disabled boundaries, repeat determinism, actual archive/Git preservation over repeated partial runs, provider allowlisting and the actual PowerShell scope finalizer. `tests/integration/Test-DatabaseScopes.ps1` also runs standalone without a SQL connection.

Native PostgreSQL safety-key differences are compared only through the comparison representation; native SQL is preserved. Collection and deterministic comparison do not prove successful restoration or operation of every database feature.

Nonempty external-provider, CLR, encryption variants, PostGIS and pg_partman configuration require validation with the intended server and extension versions. Windows integrated authentication and Windows services also require platform-specific validation. See [platform limitations](../VALIDATION.md).

Integration scripts accept fresh output directories and act only on uniquely named disposable databases:

```sh
python tests/integration/postgresql_extensions_live.py --bin-dir /path/to/postgresql/bin \
  --host /tmp --port 55439 --user lab_admin --output /fresh/extension-test \
  --confirm-disposable-server
```

```powershell
# Supply CONFIGBACKUP_TEST_SQL_PASSWORD securely in the process environment.
pwsh -NoProfile -File tests/integration/Test-SqlOperationalInventory.ps1 `
  -Server '127.0.0.1,51439' -ModulePath C:\Lab\Modules `
  -OutputDirectory C:\Fresh\SqlOperations -ConfirmDisposableServer
```

The SQL fixture creates a uniquely named database with Service Broker, CDC and a synthetic certificate, reads all operational queries twice and removes its fixture. It changes test-database configuration and must never be pointed at a production instance. Production collection itself remains read-only.

Primary references: [SQL Service Broker queues](https://learn.microsoft.com/en-us/sql/relational-databases/system-catalog-views/sys-service-queues-transact-sql), [CDC capture metadata](https://learn.microsoft.com/en-us/sql/relational-databases/system-tables/cdc-change-tables-transact-sql), [PostgreSQL HBA rule view](https://www.postgresql.org/docs/current/view-pg-hba-file-rules.html), [LDAP authentication options](https://www.postgresql.org/docs/current/auth-ldap.html), [postgres_fdw](https://www.postgresql.org/docs/current/postgres-fdw.html), [pg_partman documentation](https://github.com/pgpartman/pg_partman/blob/development/doc/pg_partman.md).
