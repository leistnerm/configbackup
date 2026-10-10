# Guided configuration and startup tools

```sh
python configure.py --config /path/to/configbackup.yaml
python configure.py --kind monitor --config /path/to/monitoring.yaml
python configure.py --kind schedule --config /path/to/schedules.yaml
python configure.py --kind registry --config /path/to/windows-registry.yaml
python configure.py --config /path/to/configbackup.yaml --check
```

The editor adds, edits, duplicates, removes and enables/disables items. It asks for common values and accepts JSON for advanced nested settings. Save validates first, creates a byte-for-byte dated backup, then atomically replaces the YAML. Comments/formatting are not preserved in the new YAML; they remain in the backup. Ordinary editing does not execute collectors, change database permissions, register scheduled tasks or send messages. The explicitly selected **Database connection tests** menu runs collectors in disposable scratch space; see [connection/access diagnostics](database-diagnostics.md).

Tasks use `enabled: false`. `sections` maps exact names or glob patterns to booleans or `{enabled: false}`; a disabled parent keeps all children disabled. Collectors and manifest consumers preserve previous output for disabled scopes. New enrichment probes skip their disabled reads. Some legacy host collectors still perform read-only discovery before their output is marked disabled. Turning Git off converts `storage: both` to filesystem; Git-only tasks become disabled. Sources, reports, analysis, monitor rules and channels also have switches.

## Startup launcher

Choose **Generate startup launcher** in the backup editor. Select Windows, macOS or Linux, a Python executable, and a fresh output directory. The generator writes `.bat` plus `.ps1` on Windows or `.sh` on Linux/macOS, and `AUTH-SETUP.txt`. Existing files are never overwritten. It uses absolute engine/configuration paths, starts in the configuration directory and returns Python's exit status.

The default uses existing Git authentication and the process environment. For GitHub, configure Git Credential Manager or `gh auth login`, `gh auth setup-git`, then `gh auth status` under the scheduled task's account. GitHub CLI can fall back to a plaintext token file when its secure store is unavailable; inspect the reported storage location. Never use `gh auth status --show-token` in job logs.

Optional named-secret providers are macOS Keychain, Linux Secret Service (`secret-tool`) and an already registered PowerShell SecretManagement vault. The wizard accepts a secret name and destination environment variable, never its value. Generated setup instructions use an interactive secret prompt or Keychain Access. Missing/empty secret retrieval stops startup. Windows uses `Get-Secret -AsPlainText` in memory, restores prior environment values in `finally`, and does not bypass PowerShell execution policy.

A vault must already support unattended access for the scheduled identity. This generator does not unlock a vault, weaken its authentication or save an unlock password. Runtime environment values can be inspected by sufficiently privileged processes; child collectors inherit them. Disable transcript/debug logging around secret handling. `.bat` uses `@echo off` and contains no token: replace a token-echoing batch file after testing the generated launcher.

SQL integrated authentication and PostgreSQL peer/Kerberos avoid password injection where available. PostgreSQL also supports protected pgpass/service files. SQL's current SqlPackage CLI path supplies SQL-authentication connection strings to the child process, so that password may be visible to privileged process inspection despite redacted application logs. The launcher does not eliminate that downstream tool limitation; prefer integrated authentication when it matters. Actual vault integration and unattended Windows scheduling remain unverified in this release; quoting, missing-secret failure, exit-code behavior and PowerShell syntax are tested.

## Permission scripts

Choose **Generate database access scripts**. Supply an existing dedicated login/role and database names as a JSON list. Output contains no credentials. Review the generated SQL and `READ-BEFORE-APPLY.txt`, then have an administrator apply it. See [read-only coverage and audits](read-only-access.md).

References: [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login), [Microsoft Get-Secret](https://learn.microsoft.com/powershell/module/microsoft.powershell.secretmanagement/get-secret?view=ps-modules), [SqlPackage authentication](https://learn.microsoft.com/sql/tools/sqlpackage/sqlpackage).
