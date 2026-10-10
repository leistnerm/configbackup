# ConfigBackup 1.6.0: collection safety and validation

This release changes the contract between collectors and the backup engine. Deploy the complete package together; do not copy only the new collector into an old engine.

## Partial collection and migration

Every database collector writes `collection-manifest.json` with a schema version, run ID, finalization flag, nonoverlapping relative scopes, section status, and SHA-256 hashes of eligible files. A failed or skipped scope is never copied into the archive or Git, even when partial files exist in staging. Missing-file counters advance only inside completed scopes. Unreported paths are protected. A database absent from discovery is **not** automatically treated as dropped; remove its archived representation only through a separately reviewed process.

SQL Server discovers databases without `OnlyAccessible`, checks current state inside each database section, and catches inventory/schema errors independently. Inventory queries now run per database. Each instance script category, Agent, SSIS, and host collection have their own boundaries. Optional Agent metadata/script failures invalidate that Agent section. Failed databases remain eligible for retry on the next run. Intentionally skipped schema/inventory sections cannot delete prior output.

The former aggregate `instance/databases.csv`, `database-files.csv`, and `filegroups.csv` are replaced by per-database inventories. Existing aggregate history is preserved but no longer updated. File size belongs in optional `telemetry/` output. Full SSIS visibility is required to certify its section; `AllowPartialSsis` no longer certifies a partial export for archival. Native `.ispac` archives remain available; exclude them from Git if you use expanded contents there.

PostgreSQL isolates databases too. An optional query failure conservatively protects the entire affected database; cluster optional failures protect cluster/config output. This favors retaining previous complete data over archiving a misleading partial database. System collector manifests certify individual produced files and never infer deletion of a missing system output file. System inventories within a file still represent the returned inventory; command/platform permission coverage must be reviewed.

Collectors require an empty staging output (`clean_output: true`). They publish an unfinished manifest before work starts and finalize it last. Exit **6** means completed with failed sections; exit **1** means fatal failure. The engine accepts exit 6 only with a valid current-run manifest. Directory tasks must declare `collection_manifest: true` to consume a partial dependency. Engine exit **4** signals a required partial/failed run even when healthy scopes were committed. Retention is skipped on required partial runs. Failed/skipped collectors do not block independent healthy tasks from committing. A file-storage task that encounters an exception rolls back its own archive/Git writes and state using a temporary file journal. The journal handles caught exceptions; it is not a crash-consistent multi-file filesystem transaction. Git secret-scan/commit errors fail Git finalization.

```yaml
tasks:
  - name: collect-sql
    type: execute
    executable: pwsh
    arguments: [-NoProfile, -File, /opt/configbackup/collectors/sqlserver/Collect-SqlServerConfiguration.ps1, -SqlInstance, SQL01]
    output_directory: /srv/configbackup-staging/sql
    clean_output: true
  - name: archive-sql
    type: directory
    source: /srv/configbackup-staging/sql
    destination: sql/SQL01
    depends_on: [collect-sql]
    collection_manifest: true
    git_canonicalize: true
    storage: both
```

Keep diagnostic/report tasks optional if their failure should not affect the run exit status. Never bypass manifests by adding `run_on_failure` to an unrestricted directory backup. Copying a manifest-bearing tree automatically invokes coverage protection even without the explicit flag; the flag additionally requires a manifest and allows partial dependencies. Freshness is enforced against the engine run ID for dependent directory tasks. Standalone snapshots without dependencies are hash-checked but cannot establish collection time freshness.

## Determinism and restoration

Native PostgreSQL dumps retain native random psql safety keys and SQL ordering. `git_canonicalize: true` creates a **comparison representation in Git** while filesystem archives retain native bytes. Use `storage: both` when enabling it. Comparison SQL containing `<COMPARISON-ONLY>` is not a restore script. JSON object keys are sorted; arrays are preserved. XML C14N is opt-in via this flag and applies only to comparison copies, preserving element order/text. Signed XML should be restored from native archives.

The SQL normalizer sorts only entire files consisting exclusively of literal, named-parameter `sp_addextendedproperty` batches with unique identities. Mixed DDL, expressions, variables, duplicate targets, and procedure bodies remain untouched. This conservative implementation does not promise stable ordering for arbitrary DacFx/dbatools output.

`collectors/common/compare_snapshots.py` separates native byte changes, CSV row-order differences, and comparison-only changes. `snapshot_manifest.py ROOT OUTPUT.json` emits native/comparison hashes outside the source tree. These are representation hashes, not a general proof of SQL semantic equivalence. There is no full semantic SQL dependency/diff engine.

```sh
python configbackup.py --config configbackup.yaml --verify
python configbackup.py --config configbackup.yaml --restore /new/isolated/directory
python configbackup.py --config configbackup.yaml --restore /new/historical/directory --as-of 2026-10-09T12:00:00+00:00
```

Verification checks every retained filesystem version, including deletion generations. Restore checks all hashes before copying, requires a new destination outside archive/live sources, stages the result and renames it into place. It restores configuration files, not database data or a running server. Historical dates use recorded version/deletion observation times; pruned versions are unavailable. Git-only histories require Git checkout/restore and are explicitly reported as unsupported by the filesystem recovery command. Protect state/manifest files: hash verification is not authentication against someone who can rewrite both content and hashes.

## Scheduling and telemetry

`examples/multi-host-schedules.yaml` configures named sources with explicit IANA time zones. Timeline `start`/`end_estimated` are UTC, with host-local start and zone columns. Legacy single-source flags default to UTC; migrate local-time inventories to named sources. Ambiguous/nonexistent DST wall times are omitted with explicit warnings. Offset-bearing observed times retain their absolute instant. Catch-up, jitter, resource contention, and event triggers are not exact predictions.

Windows time-trigger repetition expands beyond the original day, finite repetitions spill across midnight, end boundaries are respected, and weekly intervals use Sunday calendar-week anchors. pgAgent supports calendar bitmaps including last day of month and date/time exceptions. Invalid bitmaps produce warnings. Duplicate starts from multiple triggers are merged by job identity and UTC instant. Cron supports standard five-field expressions/macros and user crontabs. Anacron/reboot and unsupported systemd calendar expressions are reported as unpredictable. `CRON_TZ`/`TZ` files require separate sources; the analyzer stops interpreting remaining entries rather than silently using the wrong zone. User timer definitions are collected but full per-user systemd execution/session discovery remains incomplete.

New reports: `executions.csv`, `concurrency.csv` (median/P95 interval sweeps), `observed-concurrency.csv`, `overlaps-p95.csv`, `slack.csv`, and `coverage.csv`. Unknown duration is never zero. Watchdogs are omitted from load/overlaps but remain in inventory/timeline and slack analysis. Report-specific exclusions affect the named report, not source collection. Min/mean/median/P90/P95/max and sample count are reported. P95 is nearest-rank across the available duration sample; no statistical confidence or calibrated overlap probability is claimed. Mixed trigger cadence is labeled `mixed` when merged starts have different cadence.

`--include-performance` (PostgreSQL/system) and `-IncludePerformanceMetrics` (SQL Server) add optional telemetry outside config manifests. SQL captures waits, file I/O, counters, Query Store aggregates and SSIS execution summaries where available. PostgreSQL captures database statistics, version-gated `pg_stat_io` and installed `pg_stat_statements` counters. Windows/Linux host metrics are optional. Query/view permissions and installed versions can limit these outputs; failures go to telemetry status files. Telemetry counters are snapshots, often cumulative, not automatically attributable to individual jobs.

Execution resource attribution is available through optional `telemetry/executions.csv` with `job_id,start,end,status,cpu_seconds,logical_reads,physical_reads,peak_memory_mb,scheduled_start`. Actual CPU seconds are converted to mean cores during an execution for observed concurrency; peak-memory sums are conservative sums, not measured simultaneous peaks. No guessed resource weights are assigned to uninstrumented jobs. Automatic process-to-job attribution, weighted predicted CPU/I/O demand, full SSIS task statistics, automatic scheduled-versus-actual delay matching and detailed resource dashboards remain outside this implementation.

Archive telemetry with a separate filesystem-only directory task and a short retention policy. The collector config manifest deliberately does not certify `telemetry/` as configuration. Never put telemetry into the configuration Git task.

## Git secret gate

Commits are scanned before commit/push. PR/push modes cannot disable the gate. It examines tracked file content and ZIP/ISPAC entries, with bounds that fail closed for oversized/unreadable archives. Rules cover private keys, selected token patterns, credential assignments and credential-bearing URIs. Findings name files/rules, never matched values. `git.secret_scan.allow_sha256` is a list of explicitly reviewed whole-file hashes. A changed file no longer matches its allowlist entry. Local-only runs may set `enabled: false`.

This is a configurable heuristic gate, not a guarantee that all credentials are detected. Protect native archives too. In-memory `-SqlCredential` supports SQL authentication; do not put passwords into YAML. SqlPackage necessarily receives its authentication connection string as a child-process argument; use integrated authentication when that process-visibility exposure is unacceptable.

## Validation boundaries

See [VALIDATION.md](../VALIDATION.md) for exact executed tests and versions. No Windows guest was available. SQL Server tests on this Mac use an x86-64 Linux container under Rosetta, which Microsoft does not support as a production SQL Server platform. Production rollout still needs validation on your versions, permissions, collations, time zones, SSIS catalog and actual Task Scheduler events.

References: [Task repetition](https://learn.microsoft.com/en-us/windows/win32/taskschd/repetitionpattern-duration), [pgAgent schema and scheduler](https://github.com/pgadmin-org/pgagent/blob/master/sql/pgagent.sql), [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html), [SQL Server container platform support](https://learn.microsoft.com/sql/linux/sql-server-linux-docker-container-deployment).

## Platform and expanded service coverage

See [SQL platform coverage](sql-services.md) for named instances, advanced SQL features, SSRS/SSAS/cluster adapters, and tested versus unverified boundaries. PostgreSQL full-configuration certification requires superuser visibility to avoid silent object filtering. macOS host collection includes OS/network/package inventories and launchd definitions, with environment values redacted. Calendar launchd triggers are expanded; startup/interval/KeepAlive timing and sleep coalescing remain warnings. Windows/macOS host permissions and Linux container restrictions can produce partial collections. macOS host inventory was exercised inside the app sandbox; restricted sysctl, account and crontab calls were reported rather than treated as empty successes.

Case-colliding manifest/archive paths are rejected even on case-sensitive hosts. PostgreSQL output writes and SQL schema preflight detect case-only object collisions instead of silently overwriting portable files. This protection does not establish support for every filesystem Unicode-normalization rule.
