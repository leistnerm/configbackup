# Explicit sensitive collection

ConfigBackup can retain sensitive values exposed by selected collectors. This is disabled by default and enabled only for exact section paths on an individual execute task. This option does not decrypt every stored credential. Ordinary file backups still copy their source bytes; this is not a universal redaction service.

Collection and Git publication are separate decisions. A private Git repository restricts access but does not encrypt Git history. Anyone with repository read access can retrieve committed secrets, including older versions. Disabling collection does not erase existing archives, working copies, recovery bundles or Git history. Use the source system's credential rotation procedure when necessary.

## Collector approvals

Add `sensitive_sections` to a collector **task**, not a connection profile, saved collection profile or task defaults. Exact spelling is required; parent scopes and wildcards are rejected. Existing `sections` enable/disable controls still apply.

```yaml
tasks:
  - name: sql-collect
    type: execute
    connection: sql-primary
    output_directory: '${CONFIGBACKUP_STAGING}/sql'
    clean_output: true
    sensitive_sections:
      - instance/scripts/Logins
      - instance/sensitive-ssis/environment-variables.csv
      - instance/sensitive-ssis/project-parameters.csv
      - instance/sensitive-ssis/package-parameters.csv

  - name: sql-archive
    type: directory
    source: '${CONFIGBACKUP_STAGING}/sql'
    destination: sql
    depends_on: [sql-collect]
    collection_manifest: true
    storage: filesystem
```

Set `access_profile: full` on the shared SQL connection. Read-only grants are never expanded automatically:

```yaml
connections:
  sql-primary:
    engine: sqlserver
    server: sql01.example.internal
    access_profile: full
    # Integrated authentication by default. SQL authentication may instead use
    # user plus password_env containing an environment-variable name.
```

The configurator's **Sensitive collection** menu lists exact supported sections and requires explicit confirmation. Its Git publication menu configures the independent publication approval. Saving configuration never grants database permissions or changes encryption keys.

Supported sections:

| Exact section | Optional content and limits |
|---|---|
| `instance/scripts/Logins` | SQL login password hashes through dbatools. Hashes are reusable authentication material, not original plaintext passwords. |
| `instance/scripts/Credentials` | Credential secrets only where dbatools and the account/platform can export them. Some exports require Windows and elevated privileges. |
| `instance/scripts/LinkedServers` | Linked-server credentials only where dbatools supports recovery. Permission/platform failure preserves that section. |
| `instance/sensitive-ssis/environment-variables.csv` | Opt-in recovery of sensitive SSISDB environment variables using the internal environment/certificate layout. See the compatibility limits below. |
| `instance/sensitive-ssis/project-parameters.csv` | Sensitive project parameter server literals for the current deployed version; references and unrecovered design defaults are labeled. |
| `instance/sensitive-ssis/package-parameters.csv` | Sensitive package parameter server literals for the current deployed version, including package identity. Independent approval and failure boundary. |
| `cluster/globals.sql` | PostgreSQL role password hashes: omits `pg_dumpall --no-role-passwords` only for this approval. Requires permission to read the role hashes. |
| `cluster/roles.csv` | Unredacted role configuration values; does not itself add role password hashes. |
| `cluster/database-role-settings.csv` | Unredacted database/role settings. |
| `config/pg-settings.csv` | Unredacted setting values visible to the collecting principal. |
| `config/pg-file-settings.csv` | Unredacted parsed setting values; parser error messages remain masked. |
| `config/pg-hba-file-rules.csv` | Parsed authentication options, including LDAP/RADIUS secrets visible to the principal. Parser error messages remain masked. |

The direct collector interface reads a JSON array in `CONFIGBACKUP_SENSITIVE_SECTIONS`. Managed tasks replace inherited values with the task's explicit selection; an ambient variable cannot silently enable sensitive exports. Connection diagnostics always suppress this setting and do not recover secrets. DacFX-generated random password placeholders are still removed: they are not recoverable original credentials.

## Independent Git approval

For the archive task above, change storage to `both` only after adding a separate Git approval:

```yaml
git:
  repository: /private/configbackup-snapshots
  push: true
  remote_url: git@github.com:YOUR-ORGANIZATION/private-configbackup.git
  sensitive_data:
    enabled: true
    private_repository: true
    allowed_remotes:
      - git@github.com:YOUR-ORGANIZATION/private-configbackup.git
    sections:
      - task: sql-archive
        section: instance/scripts/Logins
      - task: sql-archive
        section: instance/sensitive-ssis/environment-variables.csv
      - task: sql-archive
        section: instance/sensitive-ssis/project-parameters.csv
      - task: sql-archive
        section: instance/sensitive-ssis/package-parameters.csv
```

The approval names the **archive task** and exact **manifest section**, not a destination prefix. It applies only to files classified sensitive in that section. Unrelated staged files remain subject to normal secret scanning. Archive expansion limits and invalid/nonregular Git entries remain blocking even for approved sections.

All actual fetch and push URLs must match `allowed_remotes` exactly. GitHub.com URLs additionally require a successful authenticated API check confirming that the repository is private. A public repository or failed privacy check blocks publication. For other Git servers and local repositories, `private_repository: true` is the operator's explicit assertion; ConfigBackup cannot verify their server ACLs. A local repository without remotes can be approved for local commits. Restrict filesystem and remote access to the intended team.

A marked sensitive section without Git approval fails independently and retains its prior archive/Git bytes; healthy sections continue. Choose `filesystem` storage when sensitive local archival is desired without Git approval. Revoking approval also prevents a later commit/push from publishing retained sensitive files. Existing secrets in unrelated files do not receive a blanket exemption; the existing exact-content SHA-256 allowlist remains available for individually reviewed files.

## SSIS recovery is independent

Normal `instance/ssis` metadata and ISPAC export runs separately from `instance/sensitive-ssis/environment-variables.csv`. Failure to decrypt, unavailable keys, incompatible catalog layout or denied permissions fails only the recovery section. It does not invalidate normal SSIS metadata/projects or stop unrelated database collection. Old recovery bytes remain unchanged, and the manifest records failure rather than certifying NULL values as a successful recovery.

The adapter requires sysadmin, the expected `internal.environment_variables` columns, `internal.get_value_by_data_type`, and decryptable `MS_Cert_Env_<environment_id>` certificates. It issues SELECT queries and does not install procedures, alter permissions, change sensitivity flags, replace keys or run packages. These are internal SSISDB structures, not a supported stable Microsoft export API. An incompatible version fails closed. Full Windows SSIS compatibility remains unverified.

Environment variables and the two parameter sections have independent approvals and failure boundaries. Secrets embedded in DPAPI/password-protected DTSX/ISPAC files, external vault contents, SSIS runtime execution values and arbitrary third-party connection managers are not recovered by these adapters. Normal package exports retain their native protection behavior. Do not treat a successful environment export as proof that every package can be restored.

Microsoft documents that the ordinary [environment-variable view](https://learn.microsoft.com/en-us/sql/integration-services/system-views/catalog-environment-variables-ssisdb-database) returns NULL for sensitive values, and describes [package protection levels](https://learn.microsoft.com/en-us/sql/integration-services/security/access-control-for-sensitive-data-in-packages). The internal certificate-layout technique is also illustrated in the author's [SSIS environment utility source](https://github.com/PavelPawlowski/SQL-Scripts/blob/master/SSISDB/sp_SSISListEnvironment.sql); ConfigBackup does not install that utility.

## Project and package parameters (1.2)

The parameter adapters read sensitive parameters from `internal.object_parameters` for the current `internal.projects.object_version_lsn`. Project parameters (`object_type=20`) and package parameters (`object_type=30`) are separate sections. Recovery uses the project's `MS_Cert_Proj_<project_id>` certificate and existing keys; no catalog writes, package execution or permission changes are performed.

Each CSV identifies the folder, project, object/package, parameter name, data type and configuration state:

- `server_literal`: a server-configured sensitive literal was decrypted. Empty strings are valid; a NULL decryption fails the entire affected section.
- `environment_reference`: the variable name is recorded, but its value is not resolved. Enable environment recovery independently if needed; multiple environments can provide different values.
- `design_default_unrecovered`: no server override is set. A default inside the deployed package is not recovered by this path. This status does not mean the effective value is empty.

A complete manifest certifies this scoped export, not recovery of every effective runtime parameter. Historical deployment versions are excluded. Invalid references, unsupported states, incomplete joined inventories or unreadable literals fail the affected project/package section. Prior archive and Git bytes stay untouched; normal SSIS, environment recovery and the other parameter section continue. A failure in one project currently freezes the whole project-parameter section; isolation is per section, not per individual project.

Microsoft documents the [parameter types and literal/reference states](https://learn.microsoft.com/en-us/sql/integration-services/system-views/catalog-object-parameters-ssisdb-database). The author's [configuration utility](https://github.com/PavelPawlowski/SQL-Scripts/blob/master/SSISDB/sp_SSISCloneConfiguration.sql) illustrates the internal project-certificate layout. These structures are not a stable supported export API. The implementation has passed synthetic SQL encryption tests but remains unverified against real Windows SSISDB versions.

## Output handling and validation

Sensitive sections are explicitly marked in the collection manifest and archive state. HTML configuration previews show only file metadata/hashes for these sections, even with `include_values: true`. Sensitive query failures use generic diagnostics so recovered values are not inserted into exception reports. Normal authentication credentials remain environment/vault references. Managed sensitive collection uses a restrictive POSIX child-process umask; Windows output inherits the selected folder's ACLs. Configure restricted ACLs before collection. Direct PowerShell users must likewise secure output and temporary directories before running it.

Tests cover exact opt-in, default redaction, unrelated-secret blocking, destination privacy, revoked approval, hidden previews, repeated exports and failed-recovery preservation. The Windows kit runs synthetic fixtures only; it never enables sensitive collection on the local host.

Disposable integration scripts:

- `tests/integration/postgresql_sensitive.py`: temporary synthetic role; default hash exclusion, opt-in hash export, repeatability and denied-account behavior. Uses existing `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`; requires `--bin-dir`, a new `--output` directory and `--confirm-disposable`.
- `tests/integration/Test-SensitiveSqlFixture.ps1`: temporary synthetic database and AES key/certificate layout; environment/project/package decryption, current-version filtering, literal/reference/unset handling, repeatability, corrupt-value isolation and missing-key/incomplete-inventory preservation. Requires `-ConfirmDisposable` and a new `-OutputDirectory`. Credentials come from `CONFIGBACKUP_TEST_SQL_PASSWORD` or the named `-PasswordEnvironment`. This validates the SQL cryptographic path, not real SSIS installation compatibility.
- `tests/integration/Test-SensitiveCollection.ps1`: no server connection; tests the actual PowerShell functions against synthetic result tables, including normal-SSIS continuity after denied/NULL recovery.

Run disposable integration scripts only on isolated test instances. A real Windows SSIS lab must still compare recovered synthetic environment values with their known inputs, test unavailable keys/permissions, repeat captures and verify unchanged normal SSIS output. No Windows or production secrets need to be sent elsewhere for validation.

Sensitive collection manifests use schema version 2 and require ConfigBackup 1.1 or newer. Version 1.0 rejects these manifests instead of treating sensitive sections as ordinary configuration. Non-sensitive manifests retain schema version 1.
