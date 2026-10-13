# Setup and feature configuration

## Install and open your existing configuration

Install Python 3.10+ and Git, extract the application ZIP, and use a dedicated
Python environment. Run these from the extracted directory (use `python3` on
systems where that is the Python command):

```sh
python -m pip install -r requirements.txt -r requirements-console.txt
python configure.py --config configbackup.yaml
```

The editor loads that file. **Current configuration** browses existing sections
and items; the dedicated menus add tasks, connections and optional features.
**Python dependencies** checks and offers approved installs/updates, including
`tzdata`. Select **Save** to validate and persist changes. See the
[console guide](console-configurator.md) and [configuration reference](configuration.md).
Use **Autodetect and guided setup** for native-client suggestions; Python packages
alone do not install PowerShell, SQL Server clients or PostgreSQL clients.

## PowerShell execution policy on Windows

Choose **PowerShell execution policy** in the main backup configuration menu.
The default is `inherit`, which preserves existing behavior. Available explicit
values are `RemoteSigned`, `Bypass`, `AllSigned`, `Restricted` and `Unrestricted`.
For trusted collector scripts where process-only Bypass is appropriate:

```yaml
options:
  powershell_execution_policy: Bypass
```

This supplies `PSExecutionPolicyPreference` to Windows task child environments,
including managed database collection, its diagnostic context, and PowerShell
launched inside Python collectors. It never sets user/machine registry policy.
Explicit `-ExecutionPolicy` arguments in custom commands override the environment;
edit/remove conflicting arguments yourself. On Linux/macOS the setting has no
runtime effect. Independent scripts launched outside ConfigBackup do not read it.

**Generate startup launcher** uses the selected policy for the Windows BAT-to-PS1
invocation. Regenerate existing launchers after changing it; an old launcher
cannot read YAML before PowerShell permits its script to start. Launch the BAT
file to use that option. No existing launchers are rewritten automatically.

`Bypass` disables execution-policy checks in these child processes. Group Policy
still takes precedence. If a script remains blocked, run `Get-ExecutionPolicy -List`
in the same PowerShell edition/account and inspect `MachinePolicy`/`UserPolicy`.
Downloaded scripts under `RemoteSigned` may need review and `Unblock-File`;
`AllSigned` requires suitable signatures. See [Microsoft's execution-policy documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_execution_policies).
Native Windows policy enforcement remains unverified in this release.

## SQL Server, Agent and SSIS

1. Install PowerShell, dbatools and SqlPackage as described in the
   [SQL Server collector guide](sql-server-collector.md). Confirm visibility under
   the actual scheduled account.
2. Use **Shared connections** to define each instance, client paths and identity.
   Use **Tasks** to create its collector and manifest-protected archive task.
   [The shared-connections example](../examples/shared-connections.yaml) shows
   both tasks. Give every instance a separate destination.
3. Choose `read-only` for the baseline metadata profile. Protected Agent/SSIS
   exports can be unavailable with that profile. For normal SSISDB exports use
   `access_profile: full` with a suitably authorized existing identity; this
   setting does not grant permissions. SSISDB must be online and accessible;
   full catalog visibility normally requires `sysadmin` or `ssis_admin`.
4. SSISDB capture includes project/package metadata, parameters, environments,
   references and project exports; normal output redacts sensitive values.
   Legacy SSIS is a separate optional standalone-collector setting. See the
   [SSIS sections](sql-server-collector.md#ssisdb--project-deployment-model) for
   parameters, limitations and permission requirements.
5. For sensitive recovery, use **Sensitive collection** and select exact sections.
   [Sensitive collection](sensitive-collection.md) documents separate environment,
   project-parameter and package-parameter approvals, plus a separate private-Git
   opt-in. Internal SSIS recovery remains experimental and Windows validation is
   outstanding. A recovery failure does not authorize deleting its prior snapshot.
6. Run **Database connection tests** and the managed-task diagnostic. Review
   unavailable sections before depending on the backup:

```sh
python configure.py --config configbackup.yaml --diagnose-task collect-sql --diagnostic-report sql-check.json
python configbackup.py --config configbackup.yaml --dry-run
python configbackup.py --config configbackup.yaml
```

See [read-only access](read-only-access.md) for generated permission scripts,
[SQL services](sql-services.md) for SSRS/SSAS and other adapters, and
[platform validation](../VALIDATION.md) for unverified Windows features.

## Other options and ongoing operation

- [PostgreSQL](postgresql-collector.md), [optional host inventory](optional-platform-inventory.md), [Windows components](windows-components.md).
- [Git and pull requests](git-storage.md), [retention](retention.md), [startup scheduling](scheduling.md).
- [HTML reviews](review-portal.md), [monitoring and notifications](operations.md), [performance reports](performance.md).
- [Configuration/startup recovery assets](recovery-setup.md), [journal recovery and explicit reset](recovery-and-scheduling-details.md#missing-pr-worktrees-and-explicit-journal-reset).

Start with a small collection, review its manifests and reports, then enable
additional sections. Keep runtime telemetry separate from configuration/Git.
