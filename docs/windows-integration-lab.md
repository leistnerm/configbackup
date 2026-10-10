# Windows integration lab for ConfigBackup 2.1.0

The Windows-specific behaviors below are **not live-certified by this release**. PowerShell syntax parsing and portable/local simulations are useful but do not substitute for a Windows host, actual service installations and the real scheduled account.

## Machines

Start with **one disposable x64 Windows Server 2022 VM**, SQL Server 2022 Developer and two named instances. A practical starting allocation is **4–8 vCPU, 16 GB RAM and 120–160 GB disk**; these are lab planning estimates, not vendor minimums. Increase memory/disk when adding SSAS/SSRS/SSIS or multiple instances. SQL Server 2022 requires an x64 processor and supports Windows Server 2022/2025; Windows ARM on the M3 Mac is not a supported substitute for this SQL-on-Windows matrix. Use an Intel/AMD Windows/Linux hypervisor host or an x64 cloud VM. [Microsoft requirements](https://learn.microsoft.com/en-us/sql/sql-server/install/hardware-and-software-requirements-for-installing-sql-server-2022?view=sql-server-ver16).

Developer edition is intended for development/test use and includes Enterprise features. Keep this an isolated lab with synthetic data and use appropriately licensed/evaluation Windows media. [Microsoft edition guidance](https://learn.microsoft.com/en-us/sql/sql-server/editions-and-components-of-sql-server-2022?view=sql-server-ver16).

Expand to three VMs for domain and high-availability testing:

| VM | Suggested allocation | Purpose |
|---|---|---|
| DC01 | 2 vCPU / 4 GB / 60 GB | Isolated AD DS/DNS; GPO and gMSA tests; optional lab witness share |
| SQL01 | 4–8 vCPU / 12–16 GB / 120–160 GB | First SQL/WSFC node, scheduled collector, service fixtures |
| SQL02 | 4–8 vCPU / 12–16 GB / 120–160 GB | Second SQL/WSFC node, AG secondary and failover target |

Use a private network, stable DNS/IPs and VM snapshots. Put the domain controller on its own VM. Real Windows AG failover needs replicas on separate WSFC nodes; two named instances on one standalone machine test instance isolation, not clustered failover. A failover cluster instance (FCI) additionally needs supported shared storage. [Microsoft AG prerequisites](https://learn.microsoft.com/en-us/sql/database-engine/availability-groups/windows/getting-started-with-always-on-availability-groups-sql-server?view=sql-server-ver17).

## Install and record

- Windows Server Desktop Experience is convenient for initial Task Scheduler/GPO/registry review. Record OS build, cumulative updates and architecture.
- SQL Server Developer: database engine, Agent, full-text and replication; two instances (for example `CBLAB1`, `CBLAB2`) on distinct static TCP ports. Record CU/build, collation, feature selection and service identities. Test `HOST\INSTANCE`/SQL Browser separately from `tcp:HOST,port`; do not make production discovery depend on UDP Browser availability.
- Install SSIS with SSISDB and one harmless deployed project; install SSRS separately and one report/shared data source; install an SSAS instance/model. These are separate tests and need service-specific permissions. No empty service inventory should be mistaken for a verified export.
- Python 3.10+ x64, PyYAML, PowerShell 7 x64, Git, dbatools and SqlPackage. Use exact executable paths in shared connections and launchers. Also test the system collector's Windows PowerShell path where applicable. PostgreSQL 17 plus native clients on Windows gives a separate platform matrix; pg_cron/pgAgent availability must be recorded rather than assumed to match Linux.
- A dedicated ordinary collector account with read grants; a separate administrator only for lab provisioning. Use a domain account/gMSA when testing Kerberos and remote services. Configure a trusted test certificate so TLS validation succeeds, then deliberately test wrong hostname/untrusted issuer.
- Git: local bare remote first, then a disposable remote repository/account if push/PR integration is desired. SMTP: local capture server first; use a dedicated test destination for real TLS/auth delivery.

After downloading/installing those platform components from their official installers, these local Python/PowerShell commands prepare the project:

```powershell
py -3 -m venv C:\ConfigBackupLab\venv
C:\ConfigBackupLab\venv\Scripts\python.exe -m pip install PyYAML
# Run under the account that will launch collection, or install centrally for that account.
Install-Module dbatools -Scope CurrentUser
Get-Module -ListAvailable dbatools | Select-Object Name, Version, Path
Get-Command pwsh, git, sqlpackage
Set-Location C:\ConfigBackupLab\configbackup-2.1.0
C:\ConfigBackupLab\venv\Scripts\python.exe -m unittest discover -s tests -v
```

Run the suite from the release directory (or change into it first) so local modules import correctly. Do not embed database/Git/API secrets in these commands. Generate reviewed grant scripts with the configuration wizard. Generated scripts do not create logins or passwords; provisioning the dedicated principal is an administrator step. The Linux/macOS tests used PostgreSQL 17.11, SQL Server 2022 CU27, dbatools 2.9-era modules and SqlPackage available in the validation environment; this is evidence, not a guarantee about all other versions.

## Scheduled-account acceptance test

1. Put the extracted release, configuration and runtime directory somewhere the scheduled account can access, such as `C:\ConfigBackupLab`. Grant that account read/execute on code/config and write only on its output/runtime directories. Protect reports/vault access with NTFS ACLs. Do not use mapped drive letters; test UNC paths explicitly.
2. Add a shared connection and managed task. Run `--setup-check`, then `--diagnose-task` interactively with that account. Save the report. Generate a **diagnostic** Windows launcher with the same credential references planned for backup.
3. In Task Scheduler create a dedicated, initially on-demand lab task. Select the intended account and **Run whether user is logged on or not**. Use the absolute `pwsh.exe` path with arguments `-NoProfile -NonInteractive -File "C:\...\run-configbackup.ps1"`. Set the working directory; allow only one run at a time; give it enough time for native extraction. Do not enable highest privileges merely to make a read test pass.
4. Choose a logon method with the access you intend to use. The S4U “do not store password” mode lacks network/encrypted-file access and is not an equivalent test of a normal network-capable service logon. [Microsoft Task Scheduler logon types](https://github.com/MicrosoftDocs/sdk-api/blob/docs/sdk-api-src/content/taskschd/ne-taskschd-task_logon_type.md).
5. Enable Task Scheduler Operational history and run the helper below. It starts only an already registered diagnostic task, waits for a fresh report and compares the report SID to the registered account. Use a private report directory dedicated to this one task. Retain the JSON, task XML, LastTaskResult and event history as evidence. A successful manual launcher run alone is insufficient.

```powershell
.\tests\integration\Test-ScheduledLauncher.ps1 `
  -TaskName ConfigBackup-Readiness-Lab `
  -LauncherPath C:\ConfigBackupLab\launcher\run-configbackup.ps1 `
  -ReportDirectory C:\ConfigBackupLab\reports
```

Check the helper's exit status: 0 ready, 6 ready with warnings, 1 not ready. The underlying report keeps `scheduler_proven: false`; the separate harness correlates scheduler provenance. The helper never registers a task, stores credentials, changes privilege or forcibly stops a timed-out task. It is syntax-checked but awaits a Windows live run.

Repeat with the interactive account logged off, a missing/locked vault, missing user-local modules/client PATH, denied report/output directory, missing `PGPASSFILE`, read-only share, invalid certificate and revoked database permission. Ensure failure is visible even when the launcher cannot create a report (scheduler history is then essential).

## Feature/failure matrix

| Area | Acceptance evidence |
|---|---|
| Two SQL instances | Same database/job names on both remain distinct; stopping one instance leaves the other's snapshot current and failed instance untouched |
| Partial databases | ONLINE/OFFLINE/RESTORING, schema extraction timeout, denied metadata and database drop; hash prior archive/Git files before/after; healthy siblings still update |
| Complex SQL schema | Partition functions/schemes, RLS predicates, memory-optimized tables, columnstore, full-text, computed/encrypted columns and permissions; repeat extraction, controlled DDL change, compare specific files |
| SQL services | Agent steps/schedules/proxies, SSIS package visibility, SSRS exports, SSAS model metadata; verify explicit failed/preserved scopes under the reader, privileged exports only in a separate reviewed test |
| Domain/cluster | GPO/RSoP under user/computer contexts; remote integrated authentication; WSFC/AG health and configuration before/after failover; record role/state telemetry separately |
| Windows host | Selected/custom registry keys, Run/RunOnce, driver/firmware, storage/shares, firewall, listeners/processes and SMART unavailable paths |
| Scheduler | Five-minute watchdog, indefinite repetitions, weekly interval anchors, DST gap/fold, execution history/duration permissions, task XML escaping and account logon contexts |
| Git/restore | Secret gate rejects staged secret fixture; failed database has no deletion; pending push retries; verify and restore archived config into a disposable destination |
| Notifications | SMTP STARTTLS/auth, recipient rejection, timeout/retry, HTML/text, recovery and capability-loss alert; no unrequested sends during setup checks |

Use [portable integration scripts](../tests/integration/README.md) and review the fixture paths before running. They create/drop uniquely named lab objects and some tests intentionally change state; never point them at production. The shared access test has `--confirm-disposable-server`. SQL backup-directory arguments must be paths writable by the SQL Server **service**, not just the interactive user. Existing fixture templates may need Windows file paths/service installation adaptations; until executed here they remain unverified.
