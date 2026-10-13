# Offline review portal

`reporting.py` turns local collector manifests, monitoring history, schedule reports and archive state into `dashboard.html` and `report.json`. It never connects to a database, uploads data, sends a message, edits a schedule or restores anything. Enable it independently from monitoring and collection, or use the [standalone example](../examples/review-portal.yaml):

```sh
python reporting.py --config examples/review-portal.yaml --output /srv/configbackup-review
```

Set real paths and `enabled: true` first. Open `dashboard.html` locally. The report contains all its JavaScript/data and uses no external assets. Search, sortable tables, date filters, display timezone, schedule-view/metric-series selectors and visible-row JSON export work offline. Table rendering shows at most 500 matches and explicitly says how many are retained; exports contain all retained filtered rows, not omitted input rows.

## Available reports

| View | Evidence and behavior |
|---|---|
| Coverage and freshness | Certified collector scopes and optional archive task/scope state; last source observation, success and age; unavailable, disabled and preserved sections remain distinguishable. Source status is always visible above the views. |
| Before/after changes | Compare each snapshot to its previous successful report capture, or an explicitly configured certified baseline (including another host's comparable capture). JSON properties use escaped pointer paths. CSV rows stay ordered unless explicit unique `csv_keys` are supplied. Ordered JSON arrays are never sorted. |
| Configuration explorer | Search certified paths, sizes, hashes, scopes and structured properties. This includes collected IIS/DNS/DHCP/Hyper-V or other files without assuming every platform feature is implemented. |
| Schedule workbench | Predicted median/P95 interval timeline with matched actual-start ticks (up to 200 filtered rows), projected starts by local day/hour heatmap (up to 62 days), and selectable detail tables for observed executions, runtime trends, concurrency, P95 overlap, resources, dependencies, deadlines, watchdogs, scenarios, coverage and warnings. Missing-duration jobs are points, not invented runtimes. |
| Metric history and capacity | Read-only SQLite observations and line chart per metric/entity series, plus latest capacity facts. No interpolation/downsampling is claimed; configured point caps select newest samples, sorted chronologically for display. Limits disclose omissions. |
| Notification delivery | Pending/sent/superseded queue metadata, attempts, retry time, age timestamps, channel ID and sanitized failure type. Message bodies, endpoint URLs and credentials are not imported from the outbox. No browser control sends or retries anything. |
| Recovery readiness | Retained file/version counts and archive-level verification/restore evidence, with a pure restore-plan preview. No archive content is read, verified or copied by preview. Live database/service restore readiness is not inferred from configuration-file history. |
| Missing/preserved objects | Failed, disabled and undiscovered captured files preserved in the report cache; inactive/missing-count records in archive state. This is a review list, not permission to retire or delete files. |
| Endpoint exposure evidence | Joins known IIS/web/container binding metadata with same-source TCP/UDP listener evidence, Windows process/service executable matches and exact certificate fingerprint expiry metadata. Windows structured runtime, Linux ss and macOS lsof field outputs are supported. The report explicitly leaves firewall/routing/external reachability unknown. |
| Collection quality | File count/bytes, certified/unavailable scope counts, preview mode and baseline hash-comparison evidence. Collector duration is explicitly unavailable where not supplied; the report does not fabricate feature completeness or statistical confidence. |

The configuration explorer is generic; it does not yet render dedicated network/topology graphs or rich service-specific IIS/DNS/DHCP/Hyper-V relationships. Schedule what-if comparisons are existing analytical scenarios displayed as tables; P95 is also overlaid on the timeline. The portal does not edit scenarios interactively or change real schedules. Row count is not an overlap probability or measured resource demand.

## Baselines, determinism and partial failures

Each snapshot requires a finalized, hash-valid `collection-manifest.json`. The portal seals certified bytes before reading them. Failed sections preserve their prior cached report records; undiscovered prior scopes are retained with `scope_not_discovered`. A missing/malformed source or baseline keeps that source's last report dataset and marks the new source attempt failed. Other sources continue. No report cache is used to authorize archive/Git deletions.

The `.report-cache` directory is local report state. Keep it with the portal if previous-run comparisons are desired. A fixed `as_of`, fixed baseline and identical inputs produce repeatable output bytes. Without a fixed baseline, the comparison intentionally advances to the last successful report capture. Without `as_of`, the report timestamp/ages intentionally change. A source failure can leave an older successful dataset displayed; always read the source-status/preserved indicators.

Monitoring and schedule inputs are independently cached. If their latest generation fails, the previous portal data is retained with a failed-source banner. Missing schedule output does not mean zero scheduled work. Standalone schedule generation stages its aggregate output: a parse/analysis failure preserves the prior aggregate files and writes a separate `generation-status.json`. It does not claim independently refreshed data inside that preserved aggregate.

The portal uses source-level cache replacement and atomic replacement of individual output files, not a crash-atomic transaction across every cache and report file. It refuses symlink outputs and unsafe manifest paths. Set reasonable file/row/report-byte limits; an oversized final report raises instead of replacing the previous portal with a truncated-looking success.

## Values and privacy

`include_values: false` is the default. Ordinary string property values appear as short hash references; numbers/booleans remain reviewable. Known password/token/credential/private-key/connection-string properties are masked, and secret-scanner findings suppress text previews. Enable values only in a protected local report directory when the extra detail is needed; JSON/CSV values and text diffs may then include sensitive configuration that no heuristic can reliably identify.

Raw collector error strings and outbox message bodies are not shown. Names, paths, identifiers, numeric values and hashes can still be sensitive. Reports/caches are created with restrictive POSIX file modes where supported; Windows deployment must use an appropriate directory ACL. Do not email or publish a portal merely because obvious credentials are masked. HTML uses escaped JSON and text nodes; input strings cannot become script or markup.

## Configuration and limits

`snapshots` entries need a stable `id`, source `path`, optional `baseline`, `enabled`, optional per-source `include_values`, and optional `csv_keys`. The top-level values include `timezone`, `as_of`, `state`, `monitoring_path`, `schedule_paths`, `runtime_paths`, `max_source_age_hours`, `restore_as_of`, `max_file_bytes`, `max_files`, `max_rows`, `max_report_bytes`, `history_days`, `history_points` and `sections`. Section names are listed in the example and can be disabled independently. A disabled view is omitted and its cached source data is retained.

Baselines and caches contain metadata from supplied evidence; there is no automatic “approved configuration” policy engine. The report does not discover remote machines, recover deleted collector outputs, infer unsupported OS components, establish a complete execution-history interval or infer a successful restore from a checksum alone. Real Windows role behavior and provider delivery still require their own integration tests.


## Endpoint correlation limits

`runtime_paths` entries are optional `{id, path, enabled}` mappings to collected `telemetry/network/inventory.json`. Use the matching snapshot id only when it is the same physical host/source. A configuration binding can be observed or not observed; absence from a privilege-limited snapshot is not proof that a service is stopped. A same-port match is a candidate relationship, not authenticated process ownership. UDP observations are endpoints, not TCP-style listeners.

Certificate expiry joins currently require an exact supplied fingerprint (for example IIS binding to Windows certificate inventory). They do not perform a TLS handshake, establish the actually served certificate, validate trust/hostname/revocation, or infer a certificate solely from a port. Web-file inventories do not automatically resolve includes or expand effective configuration. Container published ports are configuration intent; runtime observation must be collected separately.

Source freshness includes configurable `max_source_age_hours` (24 by default), future-timestamp and unknown indicators. Report-generation status describes whether inputs were read successfully, not whether every server is healthy. Source/observation age remains visible with cached data.


## Validation performed

The automated suite covers semantic comparison, redaction/escaping, declared CSV identity ordering, source/baseline/runtime/state failures, manifest integrity, failed/undiscovered scope preservation, source/report switches, output caps, fixed-input byte determinism, recovery preview without copying, notification limits/coalescing/routing/acknowledgement, schedule matching and generation preservation. Exposure fixtures cover Windows process/service/certificate correlation, Linux ss and macOS lsof parsing, and unobserved web/container bindings.

The [synthetic demo](../examples/review-portal-demo.html) exercises every populated view without live service data or notification delivery. Its JavaScript passed syntax checking and a 71-check fake-DOM harness covering view selection, search/date/timezone controls, chart generation and export. This is static interaction testing, not a real browser rendering test. Live browser visual QA was unavailable in this environment; inspect the demo locally in a current browser. Optional Node harness:

```sh
node tests/integration/test-report-portal-dom.cjs examples/review-portal-demo.html
```

No real Windows listener/service ownership, live TLS identity or external notification delivery is certified by these report fixtures. Those remain separate platform/provider integration checks.
