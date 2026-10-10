# ConfigBackup 2.0.1 diagnostics validation — 2026-10-09

The configuration CLI now tests connections and actual collection access using separate named profiles and private temporary exports. See [usage and scope](docs/database-diagnostics.md). It does not apply grants or run backup/Git tasks.

| Check | Result |
|---|---|
| Python suite | **143 tests passed on macOS Python 3.14.8 and Linux Python 3.10**, including 13 new diagnostic tests. These cover independent scopes, incomplete/corrupt manifests, missing secrets, secret redaction, temporary cleanup, actual process timeout, skipped native extraction, runtime failures, report overwrite refusal, CLI exit codes and permission-probe failure distinct from connection failure. |
| PostgreSQL live | Actual CLI against PostgreSQL 17.11: temporary role connected before grants but could not certify the database; generated grants enabled schema/catalog collection; nonexistent role failed connection. Ordinary writes remained rejected. Temporary role/database cleanup passed. Privileged rich fixture also collected configuration and optional runtime data. |
| SQL Server live | Actual CLI against SQL Server 2022 CU27: temporary login connected before grants but could not certify the database; generated grants enabled database schema/catalog collection while protected native exports remained unavailable; nonexistent login failed. Data/DDL/Agent-job writes rejected; cleanup passed. An `sa` rich-fixture diagnostic identified unsupported Mac/dbatools exports rather than claiming universal administrator coverage. |
| Other live diagnostic cases | Missing PostgreSQL client, closed local port and disabled database/native-schema extraction behaved as reported. No failed connection/client case reported any available configuration section. |
| Static checks | Python source parsed, all 22 YAML examples parsed, diagnostic examples validated, six PowerShell scripts parsed including the new driver. |
| Safety | Test exports used fresh private temporary directories; reports contained statuses/reasons and no exported definitions. Password references used existing environment/libpq mechanisms. Known test credentials were checked for absence from packaged files. No real backup configuration, archive, Git repository or service permissions were changed by diagnostics. The disposable integration harness intentionally applied grants to its own temporary identities. |

Current evidence is in `validation/2.0.1/`. The existing read-access integration harness accepts `--diagnostics` to reproduce before/after-grant CLI tests. Profiles must match the intended collector's identity/settings; custom task wrappers are not inferred or executed. Actual exports may take time and acquire schema locks. Unavailable means this test failed, not necessarily that a permission grant will fix it.

Windows integrated authentication/process-tree termination, enterprise service adapters and other collector options outside these profiles remain unverified. Diagnostic results are point-in-time observations, not a guarantee of future access or a formal proof of arbitrary role safety. The earlier engine, storage, alerting and advanced-feature evidence below is retained as **2.0.0 baseline evidence**, not a claim that every old live scenario was repeated for 2.0.1. Core collection logic is unchanged apart from release-version identifiers.

---

# ConfigBackup 2.0.0 validation — 2026-10-09

The release implements the features below with explicit limits. It is not a complete backup of every installed service or a replacement for native database-data and machine backups. Deploy the engine, shared modules and collectors together; read [upgrade notes](docs/release-2.0.0.md).

## Executed checks

| Area | Result and evidence |
|---|---|
| Python suite — macOS ARM | **130 passed**, Python 3.14.8. Actual local Git operations and SMTP multipart wire delivery are included alongside synthetic/fault tests. |
| Python suite — Linux ARM | **130 passed**, Python 3.10 in an isolated `python:3.10-slim` container. Tests cover the documented minimum Python version. |
| Static checks | All 21 YAML examples parsed; monitoring configuration validated. Six PowerShell scripts parsed, including a generated Windows launcher. Python integration scripts compiled. Syntax checks are not Windows runtime validation. |
| SQL Server regression | SQL Server 2022 CU27 Developer Linux x64 under Rosetta, dbatools 2.9 / library 2026.9.14 and SqlPackage 170.4.83.3. Three actual database extractions, middle database OFFLINE then RESTORING, healthy changes committed, failed archive/Git/state unchanged, next-run recovery passed. **149 files byte-identical** across unchanged collections; only the collection manifest changed. |
| PostgreSQL regression | PostgreSQL 17.11 Linux ARM and Homebrew client. Real exclusive lock blocked the middle schema dump; following database collected, prior failed snapshot/state preserved, recovery passed. **94 files byte-identical**, four comparison-only native safety-key differences, three manifest/telemetry changes; no configuration changes or added/removed files. |
| Generated read-access scripts | Packaged `read_only_access_live.py` passed on both servers with fresh temporary identities/databases, cleanup verified. INSERT/CREATE TABLE/ALTER rejected on both; PG CREATE ROLE and SQL Agent job creation rejected. PG certified 27 database files with exit 0. SQL certified 47 database files with expected partial exit 6: native Agent jobs/settings and SSIS exports stayed unavailable. |
| Inherited-permission audit | Richer existing test fixtures exposed PUBLIC DELETE on PG cron history and SQL application execution/write permissions. Those database scopes failed while clean databases collected. This is evidence of tested checks, not proof against every possible write path. |
| Populated database health | SQL 12,000-row fixture, >128-page physical index inspection with density/fragmentation, populated columnstore rowgroups, files/log/partition/backup health. PG 12,000 rows with half deleted, real pgstattuple approximation and table/index/TOAST sizes. No probe failures in these fixtures. Thresholds remain operator choices; no maintenance performed. |
| macOS AppleRAID fault | Two NEW image files formed a mirror. Repeated configuration was stable. One image was detached/deleted; actual state changed Offline → Degraded, both member UUIDs remained, surviving file was readable and a local critical alert opened. Devices cleaned up. No physical disk was faulted and no external notification sent. |
| Linux storage | Four NEW image files/verified loops: two-PV LVM with mounted XFS, two-member mdraid with mounted ext4. All four target scopes complete and byte-identical on repeat. Unmount/VG/array/loop cleanup passed. |
| Firewall | Real isolated Linux nft rule and loopback packets: raw counters changed while certified policy stayed identical; table cleanup passed. Mac application-firewall settings/apps, PF definitions and SMART identities: eight certified files identical on repeat. Active Mac PF/NAT queries lacked permissions and remained failed. |
| SMART and host runtime | Actual Mac NVMe health read; one optional error-log query unsupported and reported partial without dropping available readings. Mac hardware/kernel output compared stable on repeat. Mac share/listener/process probes and Linux socket/process/executable-path datasets executed. No physical failing-drive or live Windows reliability test. |
| History, alerts and scheduling | Selective raw retention, limits/symlinks, per-source failures, stale/unknown alert preservation, rate reset handling, trend minimums, debounce/cooldown, disabled controls, local SMTP HTML/text, expected/actual matching, P95, shared resources, dependencies/deadlines, watchdog useful-work facts and report preservation covered by tests. CPU/I/O attribution is not inferred from host counters. |
| Recovery/Git safety | Sealed certified-byte race tests, scoped rollback/deletion/retention/freshness, staged-blob secret gate including removed working files, failed-push retry on unchanged snapshots, archive verification and isolated file recovery. No remote PR or external notification was created. |
| HTML and launchers | HTML escaping/size bounds, email multipart contents, shell quoting/missing-secret failure/exit status and generated PowerShell syntax tested. Demonstration dashboard/email files included. **Visual browser inspection was blocked by local-file URL policy**; no claim of browser or email-client rendering validation. Actual OS vault integrations remain unverified. |

Evidence is in [validation/2.0.0](validation/2.0.0/). [Historical 1.6 validation](docs/validation-1.6.0.md) and `validation/1.6.0/` preserve earlier executed tests: rich SQL partitioning, compression/columnstore, In-Memory OLTP, native routines, RLS, masking, temporal/full-text objects and transactional replication catalogs; SQL/PG scheduler histories; two TCP-addressed SQL instances with identical object names and first-endpoint failure; native rich PostgreSQL schema restore into another cluster; SSRS SOAP protocol fixture. Those multi-instance, restore and enterprise-feature combinations were not all rerun after the 2.0 changes. The new regular regressions used the current engine/collectors.

## Known limits

- Live Windows Task Scheduler/GPO/registry/firewall/reliability/integrated-auth tests remain outstanding. Windows named-instance discovery was not tested; explicit distinct endpoints and snapshot destinations are supported.
- Live SSIS, SSRS/Power BI Report Server, SSAS tabular/multidimensional, WSFC/AG failover and merge replication are unverified. SSRS evidence is a local protocol fixture. SSAS output is metadata, not a verified deployable model backup. Some native dbatools exports are unsupported on the tested Mac and stay failed/preserved.
- Nonempty ZFS/multipath/CoreStorage and remote SMB/NFS authorization round trips were not tested. Current macOS here can list but cannot create CoreStorage groups. Image labs do not simulate physical SMART wear, controller faults or firmware behavior.
- The base host collector still has some coarse failure boundaries before enrichment. New probes are independently scoped; an unfinished base manifest prevents all archive updates. Some legacy disabled groups still perform read-only discovery before their output is marked disabled.
- Read-access grants are not a universal no-write guarantee for arbitrary existing roles, PUBLIC grants, extensions or privileged routines. PG's pg_read_all_data + BYPASSRLS intentionally grants **broad cluster-wide reads**, not just selected databases. Generated grants never apply themselves. See [access boundaries](docs/read-only-access.md).
- SQL-authenticated SqlPackage extraction still passes a connection string to a child process; privileged process inspection can expose it despite log redaction. Vault launchers do not remove this downstream limitation. Prefer integrated authentication where required.
- No automatic instance discovery, complete private-key/credential/service installation recovery, TDE/Always Encrypted round trip, FILESTREAM/FileTable validation, whole-machine or application-data restore is claimed. Verification hashes are integrity evidence, not authentication against someone who can rewrite content and state together.
- Schedule reports require source time zones and useful history. A missing execution becomes “missed” only with explicitly complete history coverage. Event triggers, jitter/catch-up, unsupported calendar forms, exact DST fold handling and every user timer session are not fully predicted. Resource forecasts depend on declared assumptions; no continuous per-job CPU/I/O profiler is implemented.
- Monitoring is polling-based. Trend estimates need history; a stopped agent needs an external dead-man monitor. Real SMTP TLS/authentication, external webhooks/ntfy and secret-vault unattended access need provider-specific tests. Delivery is at least once. Use one monitor per output directory.
- Rollback handles caught exceptions; this is not a crash-atomic transaction across filesystem, Git and state. Missing discovery objects remain protected until explicitly retired. Secret scanning is heuristic; inspect sensitive exports before sharing.

## Reproduction

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python configure.py --config configbackup.example.yaml --check
```

See [integration commands and fixtures](tests/integration/README.md), [image-only storage/firewall labs](docs/host-coverage.md), [operations](docs/operations.md), and [generated access examples](examples/access-scripts/). All database mutations in release testing targeted disposable local servers/test objects.
