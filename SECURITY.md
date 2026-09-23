# Security considerations

ConfigBackup is intended to preserve operational configuration. That makes its output sensitive even when the tool does not intentionally collect passwords.

## Credentials

- Do not put database passwords, Git access tokens, or other secrets directly in YAML.
- Git authentication should use normal Git mechanisms such as SSH keys/agents, Git Credential Manager, approved service identities, or deploy keys.
- ConfigBackup rejects HTTP(S) Git remote URLs containing embedded user information/credentials.
- The SQL collector uses the current PowerShell/dbatools identity by default and excludes password material from `Export-DbaInstance`.
- Sensitive SSIS environment/parameter values are written as `<REDACTED>` where the SSISDB catalog marks them sensitive.
- The PostgreSQL collector uses `pg_dumpall --no-role-passwords`, excludes subscriptions from native schema dumps when supported, and writes a separate redacted subscription inventory.
- PostgreSQL settings and FDW/user-mapping inventory apply best-effort redaction to recognized credential-bearing fields. Raw PostgreSQL config-file copying is disabled unless explicitly requested.

## Collected configuration can still contain secrets

No generic collector can reliably determine whether arbitrary configuration text contains credentials. Examples include:

- SQL Agent job commands or PowerShell/CmdExec arguments,
- Windows service command lines and scheduled-task arguments,
- application `.ini`/`.yaml`/`.json` files,
- SSIS package/project content and connection metadata,
- PostgreSQL function bodies, pg_cron commands, FDW/schema SQL, or custom settings that embed credentials as arbitrary text,
- Linux repository/share configuration,
- firewall, network, share, account, and infrastructure names.

Treat filesystem archives and Git repositories as confidential operational data. Prefer private Git repositories, least-privilege access, encryption at rest, and protected backup locations.

## Destructive-operation safeguards

ConfigBackup includes several safeguards:

- automatic retention defaults to indefinite unless configured otherwise,
- `min_versions` is a retention floor,
- deletion requires consecutive successful missing scans by default,
- mass-deletion guards suspend deletion advancement on implausibly large changes,
- required collector failures prevent dependent deletion processing,
- collector cleanup is restricted to ConfigBackup staging unless explicitly overridden,
- filesystem and Git destinations are protected from recursive source traversal,
- Git repositories must be clean before a run,
- Git snapshot changes are rolled back if a required task fails before commit,
- `--dry-run` and `--prune --dry-run` are available for validation.

Run new configurations with `--validate` and `--dry-run` before scheduling them unattended.

## SQL Server on Linux host artifacts

When the SQL collector runs locally on a Linux SQL Server guest it can copy `/var/opt/mssql/mssql.conf` and systemd service configuration. These files normally contain configuration rather than database credentials, but they can reveal certificate/key paths, directory layout, service environment references, domain/account names, ports, and other operationally sensitive information. Treat them as confidential and review local customizations before pushing them to a remote Git repository. The parsed `mssql-settings.csv` redacts keys whose names look password/secret/token related, but the raw `mssql.conf` is preserved verbatim for recoverability.

## SQL Server credentials

Do not put SQL passwords directly in `configbackup.yaml` or collector arguments. The SQL Server collector supports credentials from environment variables or a credential file. On Windows, `Get-Credential | Export-Clixml` uses Windows DPAPI and binds the exported credential to the same Windows user and computer. On Linux/macOS, PowerShell CLIXML credential export is not encrypted; use environment injection or a tightly permissioned secret file/secret manager instead. JSON credential files are plaintext on every platform and must be protected accordingly.

SqlPackage requires SQL-auth credentials for its independent database connection. When SQL authentication is used, the password is passed to the SqlPackage child process as a source credential argument (or in a source connection string when advanced connection options are enabled); this can be visible to sufficiently privileged local process-inspection tools while SqlPackage is running. Prefer integrated authentication/service identities when available.
