# Integrity profiles and deployment

Integrity checks, recovery evidence, reusable profiles and offline deployment are optional workflows in the initial release.

## Failure handling

- Unchanged live source bytes cannot conceal a missing/corrupt retained current copy. That scope fails and keeps older versions for review.
- Retention, including prune-only, verifies proposed survivors before deleting a scope's candidates. A bad survivor protects the whole scope; healthy scopes proceed.
- Structurally valid manifests isolate unreadable/hash-mismatched scopes. Unsafe manifest identity, path or overlap structure still blocks acceptance.
- Failed Git finalization forces collection retry despite a normal cadence delay. Publication remains pending until a successful retry.
- New recovery exports must pass verification before publication. Optional secondary lock failures leave a valid local export and a retryable secondary status.
- Journal inspection validates entry schemas, modes, backup identities and hashes before any rollback.
- Resource capacity uses a separate sweep of intervals, including a single job whose weight exceeds capacity. Pair conflicts and capacity exceedance are different measures.

## Integrity and recovery evidence

Enable `integrity.enabled: true` for post-run scrubbing; it defaults off. Bounds default to 500 files, 256 MiB and 30 seconds per run, with a 168-hour revisit interval. Time limits are checked between files. Oversized files remain deferred until the byte budget is increased. Evidence is operational state under the internal directory, not Git configuration.

```sh
python archive_integrity.py scrub --config candidate.yaml --output private/integrity
python archive_integrity.py plan-repair --config candidate.yaml --output private/repair-plan
python archive_integrity.py repair --config candidate.yaml --output private/repair-result --plan-id PLAN_ID
python recovery_status.py --config candidate.yaml --output private/recovery-status
```

Repair is deliberate: inspect the plan first. Only an exact expected-hash source can repair a current active version. Corrupt bytes are retained in a private hash-addressed quarantine. Each file has a durable undo transaction; later failures do not undo earlier successful repairs. Historical missing versions need another copy. Re-run scrub after repairs to refresh verification evidence. Do not delete a pending journal to bypass reconciliation.

Recovery status separates collected/archived timestamps, pending/local/pushed Git state, oldest current-version hash verification and file-restore evidence. Unknown/stale timestamps do not certify safety. Live database/service restore stays explicitly unknown unless separately accepted in your lab. Git evidence cannot reconstruct unavailable historical provenance.

## Configuration profiles

`collection_profiles` stores named SQL Server/PostgreSQL collection settings. A task selects `profile: NAME`. Connection engine must match; explicit task values override profile values, which override defaults. Profiles cover collection options, section switches, required sections, cadence and scope-size budgets. Connection authentication stays in `connections`. The saved-profile menu and Effective preview show inherited values, origins and resolved paths; sensitive display fields are redacted. Context help explains common fields. Creating a profile from a task leaves its explicit overrides in place; review those before expecting profile changes to affect that task.

## Capacity and observed performance

The schedule dashboard links resource-capacity.csv and observed-runs.csv. The scenario HTML shows weighted capacity heatmaps, peak load, overload minutes and duration sample evidence. Predictions and measured telemetry remain distinct. Unknown durations cannot prove spare capacity; heatmap limits are disclosed.

See [performance recording and reports](performance.md) for optional PerfMon/sysstat controls, macOS fallback, interval correlation, gaps and host/time-zone handling. Native startup acceptance is still required on each deployment platform.

## Offline deployment packs

```sh
python deployment_pack.py build --pack private/platform-pack --feature encryption
python deployment_pack.py verify --pack private/platform-pack
python private/platform-pack/install.py install --pack private/platform-pack --venv private/new-venv
```

Build downloads binary wheels for the exact installed, requirement-compatible dependency closure. Optional `--feature performance` adds the macOS fallback dependency. The bill of materials and hash-locked requirements cover every wheel and application file. Install verifies all files and the OS/CPU/Python-minor fingerprint, then installs into a **new** virtual environment with no index access and required hashes, runs pip check and compares installed versions. Existing environments are refused. A failed new environment is left for diagnosis; use another new path for retry.

Build on each target platform; a Mac pack is not a Windows pack. Native tools, OS installers, configuration, credentials and database data are excluded. Verify the trusted external ZIP digest before executing any installer: an internal unsigned manifest alone does not establish authenticity. The Windows kit's prerequisite workflow still covers approved native/client setup. Native Windows rebuild behavior is unverified.

## Limits

Signed deployment manifests, authenticated immutable remote recovery transport and causal workload attribution are not provided. Native Windows and enterprise service coverage require validation in the target environment.
