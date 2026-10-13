# ConfigBackup 1.0.0

Initial release with configuration archives, collectors, recovery workflows and optional operational review tools. Monitoring, recorder controls and recovery targets are opt-in. Failed collection remains scoped: no replacement or deletion for failed sections, while healthy independent sections proceed.

## Reliability guarantees

- Effective previews redact secret values carried through environment expansion and command arguments without altering collector inputs.
- A managed collector profile's `max_scope_bytes` reaches dependent directory archives; an explicit archive limit overrides it. Oversized certified scopes retain prior bytes and state.
- Native sysstat/psutil capture journals recover only their own interrupted, unindexed files. Committed chunks and unrelated files survive. Legacy orphan files are reported by recorder health, not automatically deleted.
- Malformed sysstat structures fail only that source. Prior failed-source cache bytes remain unchanged and are not used as current healthy evidence.
- Missing mapped PerfMon counters are explicit coverage warnings. `expected_metrics` and `expected_counters` can detect absent coverage. Partial coverage is not complete health.
- Offline deployment installs a verified application copy into the new virtual environment. Run that copy; the original pack remains immutable and re-verifiable after normal Python imports create bytecode.
- Unchanged Git content keeps its original commit/publication event dates; `last_checked_at` records a new check separately.
- Integrity work always has a visible task result with checked/deferred/overdue counts. Required overdue or failed verification affects the exit status; optional incremental work can remain partial without failing independent backups.

## Operational tools

- `native_recording.py status --config configbackup.yaml` queries native state as well as registration, newest completed sample age, host/metric coverage, and unindexed captures. Unknown query results never count as healthy. PerfMon raw BLG freshness is distinct from exported sample freshness.
- Performance HTML adds selectable observed overlap windows with aligned job boundaries and metric charts. Missing/coarse intervals remain gaps. Views are bounded to 30 windows, 12 series, 300 samples and 40 job runs; full accepted inputs are in JSON.
- Duration comparisons group successful runs by host/job and solo versus overlapping execution, with counts, medians, P95 and deterministic 95% bootstrap median-ratio intervals after five runs per group. Up to 200 recent runs/group and 400 resamples. Failed/unknown runs are excluded. This is descriptive correlation, not workload-controlled causal evidence.
- Monitoring can import `performance_reports` and include a static `performance` email section. The recorder report wizard can link the report; SMTP digest setup remains separate. Stale/unavailable reports do not provide current pressure facts. Existing notification queue/routing/TLS controls apply.
- Saved profiles support editing, cloning, deletion of unused profiles, affected-task previews and clearing task overrides. Connection credentials stay outside profiles. Review the Effective preview before saving.
- `recovery_readiness.py` evaluates optional per-task/scope backup age, integrity age and complete verified archive-copy targets. The primary archive counts as one; additional roots must mirror archive-relative files. Read budgets are shared. Missing/corrupt/deferred copies do not satisfy targets. Distinct roots do not prove independent hardware, and hashes do not establish live database recoverability.

## Configure and run

See [the example](../examples/recovery-and-performance.yaml). The menu exposes Recovery readiness targets, Saved collection profiles, and Performance recording and reports. Explicit target changes are reviewable before saving. Native recorder enable/disable requires the existing reviewed ownership plan; saving YAML does not activate services.

Use `python recovery_readiness.py --config configbackup.yaml --output readiness-report` for a read-only evaluation. Normal runs evaluate it after integrity when enabled. `required: true` makes unmet targets affect the run result without undoing successful backups.

For offline packs:

```text
python install.py verify --pack PACK
python install.py install --pack PACK --venv NEW_ENV
NEW_ENV/bin/python NEW_ENV/application/configure.py --config CONFIG
```

On Windows use `NEW_ENV\Scripts\python.exe`. Do not execute the immutable pack's application directory directly. Packs remain OS/architecture/Python-minor specific.

## Acceptance limits

See [validation](../VALIDATION.md) for platform limitations. Native Windows PerfMon/GPO/Task Scheduler, domain policies, enterprise services, NTFS/UNC and scheduled-account behavior require local Windows acceptance. macOS/Linux worker capture is tested separately from launchd/systemd service activation and reboot behavior. SQL Server Linux cannot validate Windows-only services.
