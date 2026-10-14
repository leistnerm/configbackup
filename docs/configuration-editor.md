# Configuration menus and automatic configuration backup

Run `python configure.py --config configbackup.yaml`. Full-screen menus (with a numbered fallback) add, edit, duplicate, enable/disable and remove tasks, connections, sources, alert rules and notification channels. **Choose a field** lists supported shared-connection fields; common values use typed yes/no, numeric, list or mapping prompts. **Change a field** remains an advanced escape hatch. Invalid types are rejected before saving or collecting. JSON is still used for complex lists/maps; this is a terminal menu application, not a graphical editor.

**Effective preview** resolves paths, connections, overrides, dependencies and section switches without executing collectors. **Preview changes** shows a redacted semantic diff. Comments and quoting are retained with ruamel.yaml where possible; changed structures can still be reformatted. Saving validates first, writes atomically, preserves a timestamped `.bak`, and refuses to overwrite an externally changed file during an editing session. `--undo path/to/config.yaml.TIMESTAMP.bak` restores earlier settings after validation and keeps another backup. Treat configuration and backup files as private: existing inline secrets are copied, while environment references remain references.

Duplicating a collector also duplicates its direct directory archive consumers when their source matches its output. The copies receive new names, dependencies, staging paths and destinations. Arbitrary custom wrappers may contain hardcoded paths outside the configuration; review those manually. The engine rejects equal or nested collector output trees, including case variants and existing symlink aliases, before any cleaning. Built-in manifests identify the producing task; archives reject an explicit producer mismatch. Older third-party manifests without producer fields remain supported, so they rely on separate outputs, run IDs and content hashes.

## Back up this configuration by default

When loading a real configuration file, ConfigBackup adds the `configbackup-settings` task. It saves the exact file in filesystem storage under `_configuration/<filename>` with up to 30 versions (at least three retained), regardless of Git defaults. Unchanged bytes do not create a new version. The task participates in verification and restore selection. Its failure is reported without stopping other tasks. Keep the configuration outside the archive to satisfy normal recursion protection.

Turn it off through **Storage & security → Archive location & setup backup → include_configuration**, or set:

```yaml
backup:
  root: /srv/config-archive
  include_configuration: false
```

For a custom location/retention policy, disable the automatic task and add an ordinary filesystem `file` task pointing to the configuration. ConfigBackup also captures recognized referenced configuration files and registered launchers by default; add custom files through Recovery files and export. Secret stores are not automatically captured. See [setup recovery](recovery-setup.md) for exact rules. The wizard's `.bak` history and archive versions are separate.

## Optional collection and reports

**Optional collectors and discovery** lists local tools/modules and lets you add explicitly selected system adapters. Discovery does not change roles or enable collection. See [optional inventory](optional-platform-inventory.md) for platform and query limits. **Collection profiles** applies light (inventory only), normal (schema), or deep (health/history and SQL operational inventory) settings to managed database tasks. Existing section switches and required-section checks remain in effect. Use separate configurations/launchers to run profiles at different times; the editor does not install schedules.

**HTML review reports** selects each portal view, source, baseline, time zone and value-display option. SMTP setup independently selects email summary sections. These are separate controls: enabling a local portal does not send it to anyone. Read [review portal](review-portal.md) for supported data and source failure handling.

Useful noninteractive commands:

```sh
python configure.py --config configbackup.yaml --preview
python configure.py --config configbackup.yaml --catalog
python configure.py --config configbackup.yaml --check
python configbackup.py --config configbackup.yaml --dry-run --restore /new/restore-preview
```

The last command verifies hashes and prints selected restore files. It creates neither the destination nor state/lock files. A real restore requires a new directory outside the archive and all live source roots, including glob prefixes. Restored copies are rehashed before publication. This restores collected configuration files; it does not apply GPOs, rebuild databases, import private keys, or reconstruct service data.

## Advanced shared connections

Managed SQL settings expose database exclusions, system/tempdb inclusion, native schema verification, capacity/performance/index-health switches and limits, Agent history bounds, Agent/SSIS/inventory skips and explicit local host collection. `append_connection_string` accepts only selected non-secret routing/client properties; endpoint/authentication/TLS/timeout settings remain explicit. All settings feed the same adapter used by normal collection and access diagnostics.

PostgreSQL settings expose excluded/template databases, split table DDL, object/scheduler skips, explicit pgstattuple table selection, performance/size switches, and command/dump/health/lock timeouts. Extension adapters are explicit allowlisted selections. Deep scans remain opt-in and can consume resources; connection diagnostics exercise the selected queries using the same account and options.

Generated Windows launchers allow a Windows PowerShell 5.1 or PowerShell 7 executable path, defaulting to `powershell.exe`. Managed SQL collection still uses its independently configured `pwsh` executable and required modules. Run diagnostic launchers through the actual scheduled account to verify that account's credentials, paths and permissions.

## Setup and recovery menus

**Recovery files and export** selects additional files, registers older launchers and exports the saved setup. **Verify and rehearse setup recovery** verifies exports, prints a rebuild checklist, maps paths into a disabled candidate, manages offline wheel bundles and runs a disposable drill. **Autodetect and guided setup** suggests clients/collectors and offers explicit installation. **Discover files from scheduled jobs** proposes local script/configuration paths for your individual selection; it never executes jobs. Read [autodetection](autodetect.md) and [recovery](recovery-setup.md) for supported formats and limitations.

## Recovery and profile workflows

**Saved collection profiles** creates, assigns and removes named SQL/PostgreSQL settings. Explicit task overrides still win; Effective preview shows their origin. This is separate from the light/normal/deep preset menu above.

**Performance recording and reports** reviews native prerequisites and generated commands, enables or disables the dedicated recorder, configures source imports and generates the report. Recording defaults off. Disabling stops future owned recording and retains completed history; unrelated PerfMon/sysstat collection is left alone. See [performance setup](performance.md), including platform acceptance steps.

**Recovery and review workflows** includes **Archive integrity and repair**, **Recovery status by scope**, and **Offline deployment pack**. Repair requires a reviewed hash-bound plan; rebuild requires a new virtual environment matching the pack's platform. See [recovery and deployment workflows](integrity-profiles-and-deployment.md).
