# Platform validation and limitations

ConfigBackup's automated application suite has passed on macOS and Linux. Portable fixtures cover failure isolation, deterministic comparison, scheduling, configuration and recovery behavior. Fixture results do not establish native Windows compatibility or complete collection coverage.

## Windows functionality not yet verified on a live Windows system

| Area | Unverified behavior |
|---|---|
| Group Policy | Live gpresult/RSoP capture, domain policy coverage, account elevation, encoding and repeated native exports. |
| Task Scheduler | Native task collection and event history, unattended account execution, startup and reboot behavior. |
| Performance recording | PerfMon/logman creation, capture, export, disable/re-enable, localized counters and startup persistence. |
| Credentials and permissions | Scheduled-account credential/vault access, logged-off execution and native permission boundaries. |
| Filesystems and shares | NTFS permissions, UNC paths, mapped drives, share access and Windows filesystem failure behavior. |
| Setup and launchers | CMD bootstrap, WinGet installation, Windows PowerShell 5.1 execution and architecture-specific executable selection. |
| Host configuration | Live registry, firewall, drivers, hardware, storage reliability and SMB/NFS inventory. |
| Windows roles | IIS, DNS, DHCP, Hyper-V, certificates, DFS, NPS, WEF, WSUS and AD CS queries across supported versions and permissions. |
| SQL Server on Windows | Named-instance browser discovery, protected Agent/SSIS exports, SSRS/SSAS, replication, Availability Groups and failover clustering. |

These features may be available in the code but require testing with the intended OS, installed roles, database versions and execution account. Missing permissions or unsupported queries must remain unavailable or incomplete, rather than being treated as empty successful collection.

## Other limits

- Linux/macOS service activation and reboot behavior for systemd/launchd are not live-verified.
- Offline deployment pack installation on Windows and Linux is not live-verified.
- Performance comparisons show correlation, not proof that overlapping jobs caused contention.
- Archive hashes do not prove independent physical storage or successful database/application recovery.
- Configuration and schema capture is not a full machine image, database data backup or complete service restoration.

See the individual collector guides for scope and prerequisites. Use a disposable environment for fault injection and schema reconstruction; do not run those tests against production systems. The Windows test kit provides optional local acceptance checks.

## Sensitive exports (1.2)

SQL Server login hash exports and PostgreSQL role hash exports were checked against disposable databases for default exclusion, explicit opt-in and repeat determinism. A synthetic SQL certificate/key fixture verifies environment/project/package recovery, current-version filtering, repeat determinism, corrupt-value isolation and missing-key/incomplete-inventory preservation. Mocked PowerShell and archive/Git fixtures verify that failed recovery preserves prior bytes while normal SSIS sections continue. Real Windows SSISDB recovery, protected credential exports and linked-server password recovery remain unverified. See [sensitive collection](docs/sensitive-collection.md).
