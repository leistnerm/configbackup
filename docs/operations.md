# Operations monitoring, runtime history and notifications

Monitoring is optional. Copy the `monitoring:` mapping from [the example](../examples/monitoring.yaml) into the main backup configuration, or run it independently:

```sh
python monitoring.py --config examples/monitoring.yaml --output /srv/configbackup-health --no-send
```

`--no-send` evaluates and queues messages but does not deliver them. Use a separate preview output directory if you do not want preview messages delivered on a later enabled run. The main engine evaluates monitoring after Git publication and retention. Collection, configuration preservation, alerting and notification delivery have separate statuses.

## Inputs and measurements

Enable SQL `-IncludeHealthMetrics`, with `-IncludeIndexHealth` only when bounded physical-index inspection is acceptable. PostgreSQL uses `--include-health`; approximate `pgstattuple` checks require explicitly selected `--bloat-table` entries and the extension. System collection offers `--include-performance`, `--capacity-path`, `--include-drive-health` and `--include-network-runtime`.

Health envelopes have `schema_version: 1`, UTC `observed_at`, engine/host/instance/database identity, datasets with entity keys, units, values and optional cumulative-counter/reset descriptions. Raw runtime files and normalized numeric envelopes are separate. A successful empty dataset means no returned entities, not that every possible health test ran. Unsupported or unreadable queries are reported in `failures`; missing values remain unknown.

SQL observations include files/volumes, log use and reuse waits, blocking/transactions, tempdb, native backup history, available CHECKDB metadata, AG health, statistics modifications, table/index/partition sizes, columnstore, memory-optimized/hash and full-text information. Physical rowstore inspection is opt-in, SAMPLED, bounded by timeout/index count, skips small indexes and avoids readable AG secondaries. These are diagnostic readings, not automatic rebuild recommendations.

PostgreSQL observations include databases/tables/indexes/TOAST/partitions, live/dead tuple estimates, vacuum/analyze ages, XID age, long transactions, slots/WAL, replication, archiver and tablespaces. `pgstattuple_approx` is opt-in and can perform I/O. Free table space may be reusable; a high percentage alone does not justify a rewrite.

SMART/NVMe health includes supported pass/fail, temperature, wear, available spare, critical warnings, power history and selected error counts. ATA vendor attributes are interpreted only for recognized ID/name pairs; raw attributes are retained separately. A failed optional SMART command does not discard other readings. Historical errors and unsafe shutdowns are not proof of an active failure. Temperature/wear thresholds are workload and hardware dependent.

## Alert behavior

Defaults cover disk capacity/inodes, projected exhaustion, SQL log pressure/blocking, PostgreSQL transaction/XID/WAL/archive risks, AG and RAID health, SMART failure indicators, collection freshness/failures, pending Git publication and verification/restore evidence age. Missing recovery evidence is unknown, not successful.

Rules select metric patterns and optional entity labels. Configure comparison, warning/critical/clear thresholds, consecutive fresh observations, cooldown, minimum table/index size or usage and maintenance windows. Re-reading one old observation does not satisfy debounce. Unknown/stale data cannot resolve an existing active problem. Disabled sources/rules are marked disabled without sending a false recovery.

Derived rates require compatible cumulative counter observations without a detected reset/decrease. Growth uses median pairwise slopes over a configurable window, with at least three samples and six hours by default. Falling/no growth does not predict exhaustion. These estimates do not account for scheduled purges, future workload changes or quotas unless represented in the supplied measurements.

Default history retention is `history_days: 90`. SQLite stores samples, alert state and the notification outbox under the output directory. Protect that directory as operational data. Use one scheduled monitor per output directory; the engine has its own run lock, while independently concurrent monitor processes are not coordinated into one logical run.

## Raw history

`runtime_snapshots` entries select source directories, include/exclude glob patterns, interval, `keep_days` and per-file/per-snapshot byte limits. They are optional and disabled in the example. Successful captures copy and hash selected files before publishing a new indexed snapshot. Retention removes only older indexed directories for that source; the latest successful snapshot is retained. A failed/empty source does not prune its previous history. Disabled entries keep their history.

Snapshots live at `runtime-history/<id>/<timestamp>-<uuid>/`, with `snapshot-manifest.json` and source-relative paths. Compare matching files or use `diff -ru` on two snapshot directories. They contain no restoreable configuration manifest and never feed Git automatically. A stale lock after a killed process requires checking that no capture is running before removing that source's `.lock` file.

## Delivery and HTML

SMTP sends plain text plus optional escaped HTML. Use STARTTLS or implicit TLS and environment references for credentials. Summary sections include alerts, capacity, freshness, scheduling, recovery and configuration changes; per-section switches and row/byte limits bound email size. JavaScript, interactive tables and arbitrary external HTML are not embedded in mail.

HTTPS webhook/ntfy destinations and tokens can also use environment references. Redirects are not followed. Heartbeats are generic JSON POSTs with final status, not a provider-specific integration. Use an external dead-man monitor to detect the machine/job never running at all; a stopped ConfigBackup process cannot notify you itself.

The persistent outbox retries failed delivery with bounded exponential delay. Delivery is at least once: a crash after a remote receiver accepts a message can cause a duplicate. Disabled channels retain pending messages without sending. Notification secrets and server response bodies are not persisted in delivery errors. Local SMTP formatting was tested; real SMTP TLS/authentication and external webhooks require testing with your chosen provider.

## monitoring and delivery improvements

Schedule sources are evaluated independently. A missing directory, malformed metadata or failed `generation-status.json` creates source-health evidence without aborting other monitoring, delivery or dashboard work. Source timestamps come from the schedule summary; absent timestamps mean unknown freshness. `schedule_reports` entries accept `max_age_hours` (default 24), `alerts: true` and optional `alert_statuses`. Scheduling alerts are opt-in: by default, findings are visible but predictions do not generate incidents. Default alert statuses when opted in are missed, failed, overrunning, capacity_exceeded, predicted_conflict, predicted_late, stale and regression. Stale/failed source findings cannot falsely recover an existing incident.

The dashboard exposes collection errors, delivery errors, queue metadata, events, raw-snapshot results and schedule-source coverage. The [review portal](review-portal.md) adds retained metric charts, source/drift views and recovery preview. Monitoring's source paths still need to point to the actual telemetry locations; optional preview values belong only in a protected local report.

Outbox draining happens independently before and after new enqueues, so a full queue can recover. Disabled channels cannot starve enabled delivery. The safety cap is 1,000 pending messages per channel. Pending heartbeat/digest snapshots are coalesced to their latest state; individual alert transitions remain distinct. Queue failure is reported and does not stop dashboard generation; undelivered alert transitions remain eligible for retry. Delivery remains at least once, with possible duplicates after crashes or partial channel success.

SMTP bounds apply after event text is added and HTML escaping occurs; a shortened message says to inspect the local dashboard. Immediate-event channels can use `severities: [critical, warning]`, `rules: ['disk-*']` and `where: {host: 'db-*'}`. These filters route immediate event notifications; a separately enabled digest still contains its configured summary sections, so use distinct channels/summary section settings when a digest must have different disclosure rules.

Temporary acknowledgement is local YAML state:

```yaml
monitoring:
  acknowledgements:
    - rule: 'disk-*'
      where: {host: 'db-01'}
      until: '2026-10-12T18:00:00Z'
      reason: 'Capacity change being investigated'
```

Acknowledgement suppresses sending while leaving the problem visible, then expires. It does not mark an incident recovered, modify the source, or send a message to anyone. Use an offset-aware expiry. Monitor processes sharing one output directory still require external serialization.
