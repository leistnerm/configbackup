# Optional native performance recording and review

Recording and reports default off. The configurator has **Performance recording and reports**: configure limits, inspect prerequisites/status, preview/apply Enable or Disable, configure reports and generate a report. Saving YAML alone never activates or stops an OS service. Enabling applies the displayed settings immediately and records them separately even if menu edits have not yet been saved. Disabling uses those recorded settings, preserves history and affects only the dedicated ConfigBackup recorder.

## Native sources and startup

| Platform | Recording | Startup/control and limits |
|---|---|---|
| Windows | PerfMon Data Collector Set, circular BLG; relog exports CSV | Elevated logman/PLA controls and a dedicated SYSTEM ONSTART task. Disable stops the owned set and disables that task. Native Windows execution remains unverified here. |
| Linux | Native sar binary chunks; sadf JSON | Separate user systemd service. Does not edit or stop a distribution's global sysstat timers/services. Unattended startup without login needs an administrator-reviewed linger/account policy; the CLI does not silently enable it. |
| macOS | Optional psutil CPU/memory sampling | User LaunchAgent; disabling persists for future logins. CPU/memory worker tested; launchd startup acceptance remains required. Disk latency/queues/paging are unavailable in this fallback. |

Existing PerfMon CSV or sadf JSON can be imported without enabling recording. Importing uses no database or remote connection. Use native capture on each host and make exported files available locally; remote log retrieval is not implemented.

Defaults: 15-second intervals, 7-day history budget, 256 MiB, 60 samples per Linux/macOS chunk. Allowed interval 5..3600 seconds; reports reject overly coarse intervals by default above 180 seconds. New Linux/macOS chunks are published only after successful capture/export/validation. Retention touches only indexed completed chunks and preserves a verified newest chunk. Child file-size limits cap native chunk files; an incomplete attempt is removed, leaving prior indexed history intact. Retention is enforced after completed chunks, so transient space can exceed the retained-history budget by one bounded chunk. Service/system logs and unrelated files are outside that budget.

PerfMon's BLG limit is a circular size limit, **not an age guarantee**. `keep_days` limits the CSV export window, not the age of bytes in BLG. CSV exports and reports can use additional space and are not included in the BLG size budget. Re-enabling/reconfiguring a recorder may reset its circular BLG; preserve an export separately if you need the old window. Disable itself keeps existing files.

Prerequisites are reported with guidance; enabling requires them to be installed. The existing autodetect/dependency setup remains separate. English default PerfMon paths may not exist on localized Windows; configure the native counter list and full-path report mappings. Neither missing counters nor missing permissions are treated as healthy metrics.

## Import and report configuration

See [example](../examples/performance.yaml). Configure `performance.enabled` to generate reports during ordinary backup runs; `required` controls whether report failure makes the overall run fail. Report failures never roll back successful configuration backups. Optional export of an owned PerfMon set is explicitly enabled with `export_native`; failed export preserves the previous CSV and stops that attempt from using it as fresh evidence.

`performance_report.py --config candidate.yaml --output private/performance` writes performance.html and performance.json. Sources have unique safe IDs, a type and a local path. Types are perfmon_csv, sysstat_json, normalized_json and native_history. Native history reads the newest 96 indexed completed chunks by default (24 hours with default settings), ignoring partial captures and recorder metadata. Adjust max_chunks to 1..1000. Oversized input fails visibly; it is not silently treated as a complete window. Keep report/history directories outside any configuration/Git source tree.

Each source's last accepted cache stays untouched if that source fails. Other valid sources can update. Failed cached data is not reused as current healthy evidence. Conflicting duplicates, mismatched units and overlapping intervals within one metric are rejected; exact duplicates are deduplicated. The first accepted source wins if a later source conflicts, so configure priority in source order. Source errors are visible in the HTML/JSON.

Bounds default to 50 MiB per source, 200,000 combined samples, 500 metric series, 5,000 overlap comparison rows and 10,000 detailed findings/gaps. Comparisons/details report truncation; summary totals use all accepted samples. Charts show up to 100 series and 1,500 newest points per series. Full accepted samples remain in JSON. Exceeding sample/series bounds rejects that source rather than silently downsampling it. Processing is indexed by metric and job intervals; many unique job combinations can still be expensive, so restrict history windows on busy hosts.

## Time and observed overlaps

Use the schedule analyzer's observed-runs.csv, which respects watchdog/load and per-report exclusions. executions.csv remains the broader execution inventory. You can also supply a CSV/JSON array containing host, job_id, start and end. Times must include offsets. Missing ends are reported and omitted; projected starts never enter measured comparisons.

Match each performance source's `host` to the corresponding schedule source ID, including distinct hosts/instances as appropriate. PerfMon naive timestamps require an IANA time zone such as America/New_York. sysstat UTC markers in older and newer JSON formats are supported. Ambiguous/nonexistent local DST times are rejected; export UTC or explicit offsets. `clock_offset_seconds` applies an explicit correction, bounded to ±24 hours; no automatic clock synchronization is claimed.

Metrics include CPU busy/iowait/queue, memory usage/commit/availability/paging where supplied, and per-disk latency, queue, utilization and throughput. Units are converted explicitly. Unsupported or unavailable metrics are omitted. Values are interval aggregates, so spikes between samples may be invisible.

The report shows weighted means, peak values, sample-value P95, warning minutes and usage charts. Default advisory thresholds include CPU busy 90%, memory usage/commit 95%, disk latency 20 ms and queue 2; tune these for hardware/workload. Paging and available-memory metrics have no automatic threshold by default. Queue depth alone is not a hardware-independent bottleneck test.

Overlap comparisons require at least three samples and 70% time coverage by default. They compare observed overlap intervals with a median of samples fully contained in periods when one of the same job IDs ran alone on that host. Gaps, coarse samples, unmatched hosts, unknown ends, too many active jobs and insufficient samples stay explicit. A threshold crossing identifies **observed overlap with pressure**, not causation or proof that rescheduling will help. Unknown jobs/background load may contribute; solo samples are not controlled experiments. Job-duration slowdown attribution and automatic schedule changes are not implemented.

## Local acceptance

Use the Windows kit's optional native recording acceptance steps on an authorized test machine. Its default safe launcher runs synthetic controls/import tests without enabling a recorder or changing policies. Verify enable → capture → report → disable → retained history → re-enable, inspect logman/task state and scheduled identity, and confirm unrelated recorders remain untouched. Test localized counters and permissions explicitly. On Linux verify the generated user unit under the intended account and its boot/login behavior; on macOS verify the LaunchAgent under the intended logged-in account.

Primary interface references: [logman counter](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/logman-create-counter), [relog](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/relog), [PLA status](https://learn.microsoft.com/en-us/windows/win32/api/pla/ne-pla-datacollectorsetstatus), [sysstat](https://sysstat.github.io/index.html), [systemd service escaping](https://github.com/systemd/systemd/blob/main/man/systemd.service.xml), [psutil](https://psutil.readthedocs.io/stable/index.html).

## Operational review

See [release notes](release-1.0.0.md) for native health, coverage warnings, incident charts, duration confidence bounds and optional performance email summaries. Source paths and host identifiers may be sensitive; keep reports local unless you deliberately configure a delivery channel.
