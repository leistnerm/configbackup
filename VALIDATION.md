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

## 1.6.0 validation

- macOS: 601 application tests attempted; 600 passed, one native CMD test skipped.
- Linux/Python 3.10: 601 attempted; 597 passed, four platform-specific tests skipped.
- The keyboard matrix covers text, integers, floats, booleans, enums, timezones,
  JSON objects/lists and checklists: save, validation, help, changes, undo/redo,
  search, exit, cancellation and preservation of prior unsaved edits.
- Additional regressions cover malformed configuration repair and inheritance,
  configuration restore races, quoted credential scanning, snapshot sealing,
  launcher lock cleanup, invalid scheduling durations and tracked-file packaging.
- All 387 selected application checks and 42 GPO fixtures passed through the
  packaged Windows kit on macOS. All 60 kit launcher/prerequisite/component
  tests passed (33/15/12). Archive pins and integrity manifests were checked.
- Application, GPO and kit ZIPs rebuilt with identical bytes and passed ZIP CRC
  checks. Public Markdown local links were checked for missing targets.
- PowerShell 7 executed the generated PS1 fixture on macOS. Native Windows
  terminal behavior, CMD and Windows PowerShell 5.1 remain unverified. The kit's
  overall result is INCOMPLETE on macOS because live Windows stages are not run.
- Live database integration was not repeated for this release; the earlier live
  database results below belong to their stated versions.

## Sensitive exports (1.2)

SQL Server login hash exports and PostgreSQL role hash exports were checked against disposable databases for default exclusion, explicit opt-in and repeat determinism. A synthetic SQL certificate/key fixture verifies environment/project/package recovery, current-version filtering, repeat determinism, corrupt-value isolation and missing-key/incomplete-inventory preservation. Mocked PowerShell and archive/Git fixtures verify that failed recovery preserves prior bytes while normal SSIS sections continue. Real Windows SSISDB recovery, protected credential exports and linked-server password recovery remain unverified. See [sensitive collection](docs/sensitive-collection.md).

## 1.4.0 validation

- macOS: 533 application tests attempted; 532 passed, one native CMD test skipped.
  PowerShell 7 executed the generated PS1 fixture, including Unicode paths and exit codes.
- Linux/Python 3.10: 533 attempted; 529 passed, four PowerShell/Windows tests skipped.
- After the final dangling-junction detection adjustment, all 33 targeted journal
  tests passed. Native Windows junction behavior remains unverified.
- Windows kit: 313 selected application checks and 42 GPO fixtures passed locally.
  Live Windows stages correctly remain NOT RUN/INCOMPLETE on macOS.
- All 60 kit runner/prerequisite/component tests passed (33/15/12), covering
  continued execution after individual stage failure, integrity checks and
  simulated installation/interpreter changes.
- Live SQL Server: OFFLINE and RESTORING database isolation preserved failed
  snapshots while healthy databases continued; subsequent recovery passed.
- Live PostgreSQL 17: deterministic repeat output, locked-schema failure isolation,
  archive/Git preservation, next-run recovery and schema reconstruction passed.
- A standalone offline deployment pack rebuilt the reorganized application in a
  fresh macOS environment; the restored version and configurator commands ran.
- Release archives are built twice with matching bytes and verified ZIP CRCs.

Portable tests do not establish native CMD, Windows PowerShell 5.1, Group Policy
execution-policy precedence, scheduled-account or Windows reparse-point behavior.
Use the Windows kit on the intended host and review its individual stage results.

## 1.5.0 validation

- macOS/Python 3.14: 550 application tests attempted; 549 passed, native CMD skipped.
- Linux/Python 3.10: 550 attempted; 546 passed, four Windows/PowerShell tests skipped.
- The suite includes 26 console/navigation tests using actual key dispatch through
  synthetic terminal input. These cover nested saves, repeated saves, active text
  and boolean fields, cursor/filter/focus preservation, invalid input, draft
  completion/cancellation, external modification, write failure, exit cancellation,
  undo/redo and blocked commands during operations.
- Packaged kit: all 339 selected application checks and 42 GPO fixtures passed on
  macOS. All 60 kit runner/prerequisite/component unit tests passed (33/15/12).
- Existing snapshot failure-isolation and journal regression tests passed as part
  of the full suite. Live database integration was not repeated for this UI release;
  the live database results above belong to 1.4.0.
- The generated PS1 execution fixture ran on macOS with PowerShell 7. Native CMD,
  Windows PowerShell 5.1, real Windows terminal key handling, Group Policy and
  scheduled-account behavior remain unverified.

The kit requires prompt_toolkit for its console checks; optional prerequisite
installation includes it only after explicit approval. macOS offline kit results
remain INCOMPLETE overall because native Windows/GPO stages are not run.
