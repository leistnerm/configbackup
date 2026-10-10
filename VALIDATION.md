# ConfigBackup 1.6.0 validation — 2026-10-09

This is an implemented and tested update with explicit coverage limits, not a claim that every requested platform/service combination or earlier roadmap item is complete. Use the migration notes before replacing 1.5.0. Deploy the new collectors and engine together.

## Executed checks

| Area | Result and evidence |
|---|---|
| Python suite, macOS ARM | **72 passed**, Python 3.14.8. Includes inherited static checks plus functional archive/Git, retention, manifests, secret gate, recovery and schedule tests. |
| Python suite, Linux ARM | **72 passed**, Python 3.10 in `python:3.10-slim`, Git 2.47.3. Confirms the documented Python minimum. |
| Live SQL Server | SQL Server 2022 CU27, 16.0.4295.3, Developer Linux x64 under Rosetta; dbatools 2.9.0/library 2026.9.14, SqlPackage 170.4.83.3, PowerShell 7 preview. Real schema extracts across three uniquely named databases; middle database OFFLINE and then RESTORING after native backup/NORECOVERY restore; first/last changes committed; failed database archive/Git/state frozen; next-run recovery passed. |
| Live SQL determinism | Basic three-database final integration: **150 byte-identical files**, no changes. Rich fixture two-run comparison: **122 identical files**, five changed telemetry files; no configuration changes. The final rich extraction passed after the added visibility/case checks. |
| Advanced SQL features | Actual partition functions/schemes/tables, row/page and columnstore compression, clustered columnstore, memory-optimized/hash-index tables, native procedure, RLS filter/block predicates, masking, temporal/history tables, full-text catalog/index/stoplist, grants/denies, sequence/synonym, Agent schedules, and transactional replication publication/article inventories were collected. Deliberate partition-boundary and RLS enablement changes appeared in catalogs and native schema diffs; settings were restored afterwards. |
| Multiple SQL instances | Two live containers with distinct TCP endpoints and identical database/job names. Separate archive/Git destinations; offline database preserved; an unreachable first endpoint did not prevent the second instance's changed schema from committing. Test-created DBs/jobs cleaned up. Windows SQL Browser/name discovery was not exercised. |
| SQL failure checks | Real encrypted module rejected while other databases collected. Restricted login with SELECT/VIEW DEFINITION rejected for full certification. The login was removed after its connection closed. Missing SqlPackage failed database sections while instance catalogs completed. Actual PowerShell section wrapper tested healthy → restoring → partial extraction → healthy continuation and manifest eligibility. |
| Live PostgreSQL | PostgreSQL 17.11, Homebrew client and Linux ARM server. Three-database collection; real ACCESS EXCLUSIVE lock caused middle `pg_dump` failure; prior snapshot/state preserved; later database updated; release/retry succeeded. Restricted role rejected by full-visibility preflight. |
| PostgreSQL determinism | Final three-database integration: **94 identical**, four comparison-only native safety-key differences, one changed collection manifest. Rich pg_cron/pgAgent fixture: **131 identical**, 16 comparison-only dump differences; six manifest/telemetry files changed. No remaining configuration diff. |
| PostgreSQL features and restore | Quoted schema, enum/domain, identity, RLS, included/partial indexes, partitioned tables, overloaded functions, trigger, materialized view, grants/default privileges and FDW metadata. Native rich schema dump restored into a separate PostgreSQL container; RLS policy, partition and overloaded functions checked. This is schema/config restoration, not application data recovery. |
| Schedulers and reports | Real SQL Agent, pg_cron and pgAgent histories; fixed-date offline Windows repeat/weekly tests; pgAgent bitmap/exception tests; time zones/DST ambiguity; duplicate schedules; physical-host concurrency across named instances; report exclusions/watchdogs; half-open interval sweep. Real combined SQL/PG/macOS report produced 23 jobs and 7,209 projected starts over two days, explicitly reporting 13 unknown-duration macOS jobs and unsupported/unanchored trigger warnings. |
| Host collectors | Linux container collection completed with a finalized manifest; this container did not represent a full systemd host. macOS collection exercised hundreds of launchd definitions and OS/network/package inventories; sandbox denied hardware/account/crontab calls, correctly yielding partial status. One Apple plist required read-only `plutil` parsing fallback. |
| SSRS adapter | Actual PowerShell HTTP/SOAP requests against a **simulated local service**, stable repeated output, definition/policy retrieval and a failed item preserving archive/Git while another updated. Narrow runtime-field removal preserved nested settings. No live SSRS claim. |
| SSAS/WSFC adapter | PowerShell syntax checked. Missing SqlServer/FailoverClusters modules produced failed, finalized scopes. No live metadata or cluster validation. |
| Recovery and Git gate | Corrupt archive rejection; restore to new isolated directory; historical deletion/resurrection gap; secret-blocked commit and corrected next-run retry; nested ZIP/ISPAC scan, explicit hash allowlisting; task-local rollback after simulated disk-write failure; no remote PR was created/pushed. |

Small machine-readable integration results are in `validation/`. Reproduction scripts are under `tests/integration/`. Unit logs are also included. Test-only containers were used; no production instance was accessed.

## Important limits and outstanding work

- **Windows live validation is outstanding**: Task Scheduler event collection, integrated authentication, Windows named-instance discovery, Windows-only dbatools exports and Windows service/cluster collection. Windows needs the `tzdata` dependency from requirements for IANA time zones.
- **SSIS, live SSRS/Power BI Report Server, SSAS tabular/multidimensional and WSFC were unavailable.** The optional service adapter is experimental. SSAS XMLA metadata is not a verified deployable model backup. SSIS package restoration, delivery subscriptions, AG/cluster failover and merge replication were not tested.
- This Mac's dbatools cannot script Credentials, LinkedServers, Policy Management or replication via RMO. Those script scopes are failed/preserved; corresponding implemented catalog metadata continues. This is partial collection, not full native-script coverage. A known absent SSISDB is `not_applicable` and protects prior files.
- Older/newer SQL Server/PostgreSQL versions, managed cloud variants, case-sensitive SQL collations and unusual Unicode filesystem equivalences need further coverage. Case-colliding output/manifest/archive names are rejected; arbitrary cross-platform filename equivalence is not guaranteed.
- Full SQL instance certification requires sysadmin; database certification requires dbo/sysadmin. PostgreSQL requires superuser visibility. Limited privileges can silently hide objects, so they are not certified as full snapshots. These broad rights warrant a dedicated protected collection identity.
- No automatic instance discovery, comprehensive service installation backup, private-key/credential recovery, TDE/Always Encrypted client round-trip, FILESTREAM/FileTable validation, or whole-machine/database-data recovery is claimed.
- **Some report recommendations remain unimplemented**: automatic process/job CPU-I/O attribution, weighted predicted resource demand, automatic scheduled-versus-actual delay matching, complete SSIS task metrics, full per-user systemd runtime coverage, and exact event/jitter/catch-up/DST-fold prediction. Windows monthly-weekday triggers and unsupported calendar expressions produce warnings rather than invented schedules. `CRON_TZ` requires separate source handling.
- The secret scanner is heuristic. Native archives and exported service definitions can still contain secrets. It cannot certify arbitrary binaries or encrypted configuration as secret-free.
- Native SQL/PG files remain authoritative. Comparison-normalized Git SQL is not necessarily executable. DacFx may rename/reorder some output after real schema changes; no general semantic SQL-equivalence engine is implemented.
- The file journal rolls back caught task exceptions. A process/power loss or storage failure during rollback is not a crash-atomic multi-file transaction. Protect the archive/state with normal storage backups. Missing discovery objects remain preserved until deliberately retired; this favors safety over automatic removal.
- SQL Server under Rosetta is a disposable test setup, not a Microsoft-supported production deployment.

## Running checks

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests
pwsh -NoProfile -File tests/integration/Test-Sections.ps1 -OutputDirectory /fresh/test-output
python tests/integration/sql_services_fixture.py --pwsh /path/to/pwsh --output /fresh/soap-fixture
```

See [integration instructions](tests/integration/README.md), [migration and safety contract](docs/release-1.6.0.md), and [SQL service coverage](docs/sql-services.md).
