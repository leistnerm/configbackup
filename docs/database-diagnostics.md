# Database connection and collection-access diagnostics

In `python configure.py --config configbackup.yaml`, select **Database connection tests**, add a profile, and choose **Test profile**. Save the configuration separately to retain that profile. The wizard asks for endpoints, identity or password-environment references, the collector's full/read-only profile, database filters, schema extraction and optional health/history probes. It never asks for a password value.

Profiles live in `database_diagnostics`. They are separate from backup tasks: match the identity, database filters, access profile and section switches to the collector you intend to run. Custom executable/credential wrappers and their side effects are not run or inferred. This permits testing an account before configuring its backup tasks, without risking archive or Git changes.

A saved profile can also run noninteractively:

```sh
python configure.py --config configbackup.yaml --diagnose-database sql-reader
python configure.py --config examples/database-diagnostics.yaml \
  --diagnose-database pg-reader --diagnostic-report /new/path/pg-access.json
```

The optional report path must be new, and its parent directory must already exist. The JSON report contains section outcomes, reasons, observed identity/permissions and timestamp, not exported configuration or credential values. Protect identity/host/database names as operational information. Exit codes: `0` completed or deliberately disabled, `6` partial availability, `1` incomplete/connection failure, `2` invalid invocation/profile/report destination.

## What is tested

The test first connects and reads the identity and selected permission indicators. It then invokes the **same built-in collector** against a private temporary directory, including native schema extraction by default. Only a finalized, hash-verified collector manifest can mark a configuration section available. Database audits and protected-service checks are therefore the collector's actual checks, not a second independent permission approximation.

| Status | Meaning |
|---|---|
| Available | The collector completed and certified this section's files, or the named optional runtime query completed. An empty result remains empty, not proof that objects exist. |
| Unavailable | That section failed in this test. Reasons may include permissions, inherited write rights rejected by the read-only profile, restoring/offline databases, schema locks, missing tools, unsupported APIs or query errors. |
| Disabled | The profile/section switch disabled the test; no capability conclusion is made. |
| Not applicable | The collector reported absence/non-applicability. This is different from a successful service export. |
| Not tested | Skipped, unfinished or outside this diagnostic's scope. Turning schema extraction off never verifies a native dump. |

Temporary configuration exports are deleted after the result is summarized. The backup engine, archive, Git, notification channels and permission-grant scripts are not invoked. Database queries and schema extraction remain read-only, but can consume resources and acquire schema locks; select a few databases first on a large instance. `timeout` bounds the collector process tree (default 600 seconds); PostgreSQL's preliminary identity and permission checks each add up to `connect_timeout + 5` seconds. A timeout/cancellation terminates the process tree before cleanup. Windows process-tree cleanup uses `taskkill` and has not been live-tested here.

SQL authentication uses an existing password environment variable. Integrated authentication uses the process identity. PostgreSQL uses libpq authentication or an explicitly named password variable. Missing referenced secrets stop the test. Do not weaken TLS validation to fix a production certificate problem. The existing SqlPackage SQL-authentication limitation still applies: a connection string can appear in its child-process arguments despite log redaction. Prefer integrated authentication where required.

The diagnostic does not change profiles automatically, apply grants, elevate privileges or enable unavailable sections. Fix permissions through the generated [access scripts and audit process](read-only-access.md), then repeat the test. `enabled: false` preserves the profile and makes no connection. `sections` uses the collector's existing exact/glob disable rules.

## Limits

This proves an outcome at the recorded time for that profile and account, not future access or a universal proof against every inherited write path. Standalone SSRS/SSAS/WSFC adapters, remote host/service permissions, Windows integrated authentication and every optional collector flag need separate testing. This first diagnostic profile supports native schema, ordinary configuration/scheduler collection and optional health/history; it does not exercise opt-in physical-index scans, explicit pgstattuple targets, raw config-file reads, legacy SSIS, or custom connection-string additions. Those features remain not verified by this test.

The console always identifies external service adapters/remote host configuration as not tested. Existing tasks can have additional options; do not treat this profile as a complete audit of arbitrary task arguments. Unsupported fields are rejected instead of silently ignored.

Use [the example profiles](../examples/database-diagnostics.yaml) and [validation results](../VALIDATION.md). The portable read-access integration test has a `--diagnostics` option that checks the actual CLI before grants, after grants, and with a nonexistent login on disposable servers.

## Shared connections and readiness (2.1.0)

See [shared connections](shared-connections.md) and the [Windows lab plan](windows-integration-lab.md).
