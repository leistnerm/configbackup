# ConfigBackup 2.0.0

Deploy the engine, shared modules and collectors together. Python 3.10+ and PyYAML remain the core requirements. Back up existing configuration, state and archives before upgrading. The YAML editor preserves a dated copy before reformatting an existing file.

## Preservation contract

A collector publishes a finalized manifest with a run ID, section statuses, file names and SHA-256 hashes. The engine verifies and copies certified bytes into a private staging tree before using them. Files changing during that copy fail their section. Only successful sections may update snapshots or count missing files toward deletion. Failed, skipped, disabled and unsupported sections retain their previous archive, Git content and deletion counters. SQL databases remain independent: a restoring/offline database or failed schema extraction does not block later databases.

The manifest scopes also govern retention and freshness. A successful sibling is not evidence that a failed database is fresh. Missing discovery objects remain protected until explicitly retired. Some new storage/service reports use finer scopes; older aggregate output can remain preserved beside the new reports. Review obsolete paths before manually retiring them.

Caught write failures roll back a section's changes. This is not a crash-atomic transaction across multiple files, Git, state and storage. Keep normal backups of the archive and state. A process or disk failure during rollback still requires recovery review.

Git scans the actual staged blobs before commit, including files removed from the working directory after staging. Push/PR publication failures remain pending and are retried on later runs even if configuration is unchanged. Secret scanning is heuristic, not a guarantee that arbitrary binary/service exports contain no credentials. No live remote PR was created during release validation.

## Added in 2.0

- Optional SQL, PostgreSQL, local disk and storage health observations; SQLite numeric history, trend/rate calculations, configurable alerts, maintenance windows and persistent notification retries.
- Offline filterable HTML operations report and optional HTML/text SMTP summaries, HTTPS webhooks, ntfy and generic heartbeat POSTs. Each channel and summary section can be disabled independently.
- Observed scheduling analysis, success-only duration/P95 estimates, shared-resource capacity assumptions, dependencies, deadlines, hypothetical shifts and useful-work watchdog checks. These report findings; they do not alter schedules.
- Guided YAML editor for tasks, sources, rules, notification channels and Windows registry selections; token-free startup launcher and administrator-reviewed database read-access script generation.
- Task/section/source/report/rule/channel switches that preserve existing settings and prior snapshots.
- More host inventory: Linux LVM/mdraid/filesystems/ZFS, macOS APFS/CoreStorage/AppleRAID, firmware, kernel/driver settings, selected Windows registry/RSoP, shares and firewall policy.
- Optional SMART/NVMe/Windows reliability, socket/process and share-client runtime observations outside configuration manifests. Selective raw history can have its own retention.

See [operations and notifications](operations.md), [host collection](host-coverage.md), [configuration tools](configuration-tools.md), [read-only access](read-only-access.md) and [validation](../VALIDATION.md). The validation matrix distinguishes actual hardware/services, simulated failures and unverified platforms.

## Runtime separation

Collector `telemetry/` trees are excluded from certified configuration/Git snapshots. Numeric monitoring history defaults to 90 days. Optional `monitoring.runtime_snapshots` retains selected raw files under the monitor's filesystem directory with its own interval, size limits and age policy. Configure a dedicated filesystem directory outside repositories; no Git backend is used by this mechanism. Ordinary directory tasks can archive arbitrary paths, so explicitly including telemetry in a custom Git task still overrides this default separation.

Source capture times remain in runtime files. The raw archive's capture timestamp is not proof that a source observation is fresh. An unavailable or empty runtime source preserves its previous history and reports a failure. Runtime history never runs destructive database maintenance, disk repairs or SMART tests.

## Coverage limits

All service combinations cannot be tested on one Mac and disposable Linux database containers. Live Windows GPO/registry/Task Scheduler, SSIS, SSRS, SSAS, WSFC/AG failover, Windows named-instance discovery, multipath, nonempty ZFS and CoreStorage still need platform-specific validation. Modern macOS here supports CoreStorage listing but not creation. Hardware images test filesystem/RAID behavior; they do not simulate physical SMART faults, controller firmware or actual failing media.
