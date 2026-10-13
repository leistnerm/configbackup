# Recovering the ConfigBackup setup

Default filesystem configuration history includes related files and generated launchers. Each file is an independent task: a missing file keeps its last version, records a failed status and does not stop healthy files. A separate inventory records file hashes, package versions and credential environment-variable **names**. An incomplete inventory does not replace the previous complete archived inventory; inspect current run failures as well as archive history.

```yaml
backup:
  root: /srv/configbackup-archive
  include_configuration: true
  include_related_configuration: true
  configuration_files:
    - schedule-report.yaml
    - registry-selections.yaml
    - /opt/jobs/custom-startup.sh
```

The main YAML stays at `_configuration/<name>`; other files use `_configuration/related/<source-path-hash>/<name>`. Hashing the source path avoids collisions between same-named files. Automatic tasks use filesystem storage, deletion disabled, and simple retention of up to 30 versions with a minimum of three. Unchanged bytes deduplicate. Original contents are preserved, including any existing inline secrets: protect these archives and exports accordingly. This feature does not put configuration into Git.

Related-file discovery recognizes only known input arguments: schedule analyzer `--config`, system collector `--registry-config`/`--smart-config`, and SQL profile helpers `-ProfileFile`. Relative task inputs use the task working directory or process working directory, matching execution. Explicit `configuration_files` are relative to the main YAML. Dynamic custom wrappers and arbitrary directories are not crawled. Generated startup launchers and AUTH-SETUP.txt are registered in `<config>.assets.json`; the registry contains paths and credential references, not credential values. The menu can register an older generated-launcher folder. Add custom scripts explicitly.

`include_related_configuration: false` disables recognized-input/launcher discovery while preserving explicit additional files. `include_configuration: false` disables the whole automatic setup-backup group. Ordinary manually configured tasks remain independent. Custom retention/location needs an ordinary file task. Keep selected files outside archive/Git roots to avoid recursion.

## Export and verify

```sh
python configure.py --config configbackup.yaml --recovery-inventory
python configure.py --config configbackup.yaml --export-recovery /private/recovery/new-bundle
python setup_recovery.py verify --bundle /private/recovery/new-bundle
python setup_recovery.py checklist --bundle /private/recovery/new-bundle
```

An export requires all selected assets, copies application/collector code and documentation, and records hashes. It publishes a new directory only after every selected file verifies. Verification rejects missing/changed/unlisted files, duplicate paths, traversal and symlinks. Hashes detect accidental changes; the manifest is not a digital signature. Preserve a trusted versioned release and protect the inventory itself. POSIX exports have a private root; on Windows use an appropriately restricted NTFS destination.

This is a **setup export**, not the existing archive and internal-state history, database data, vault contents, filesystem ACLs or complete service reconstruction. Copy archive and matching internal state together while stopped if recovering history. Keep a protected off-host copy of the setup export using your existing storage/encryption process; automatic encrypted remote storage is not provided.

## Guided relocation

Use **Verify and rehearse setup recovery** in the configuration menu, or:

```sh
python setup_recovery.py relocate --bundle /private/recovery/new-bundle --destination /new-host/configbackup
# Optional reviewed JSON object mapping old directory prefixes to new prefixes:
python setup_recovery.py relocate --bundle /private/recovery/new-bundle --destination /new-host/other-copy --mappings paths.json
```

The destination must be new, outside the bundle and original configuration/asset directories. The tool copies and rehashes assets, maps exact paths and directory prefixes in the main YAML, creates a fresh archive/staging layout, disables every original task and disables Git publication, monitoring and reporting. It preserves untouched originals under `_configuration`, writes `RELOCATION.json` and `REBUILD-CHECKLIST.json`, and regenerates supported launchers for the current platform using stored secret references. Cross-platform/older launchers need manual regeneration. It never executes copied code, collectors or launchers during relocation.

The result is a **candidate requiring review**. If configuration validation or launcher generation fails, the command returns exit 6 and retains the disabled candidate and failure report for inspection. Nested configuration contents, custom shell strings and application-specific paths are not rewritten. Password/token fields are not rewritten. Validate the candidate, review mapped paths, reenable tasks individually, restore credentials/grants/tools, and run shared-connection/setup diagnostics through the actual scheduled account before scheduling production collection. A checklist reminds you to record trigger/time zone/account/working-directory settings; scheduler registration is not recreated automatically.

## Offline dependencies

```sh
# Explicit download for the current interpreter/OS/architecture; no installation:
python setup_recovery.py dependencies --download --destination /private/recovery/python-wheels
# Or retain existing approved wheels:
python setup_recovery.py dependencies --wheelhouse /approved/wheels --destination /private/recovery/python-wheels
python setup_recovery.py check-dependencies --bundle /private/recovery/python-wheels
```

The separate bundle records wheels, requirements and hashes. Its compatibility check runs pip's offline binary-wheel resolver for the current platform without installing or executing wheel code. A bundle copied from another host is not assumed compatible. Download on each target OS/Python/architecture; use approved mirrors as needed. External client installers, SQL Server/PostgreSQL servers and PowerShell modules are not bundled. Retain approved installers separately. New-environment installation is available via `autodetect.py --install python-packages --venv NEW --wheelhouse /private/recovery/python-wheels/wheels`, with an explicit INSTALL prompt and a fresh import/time-zone check.

## Periodic recovery drill

```sh
python setup_recovery.py drill --bundle /private/recovery/new-bundle --history /private/runtime/setup-drill --interval-days 7 --keep 12
```

This can be an explicit command in an existing scheduled ConfigBackup run; see `examples/setup-recovery-drill.yaml`. It does not install a scheduler. Successful runs within the requested interval return `not_due`; failures retry on the next invocation. `--force` runs immediately. A lock prevents overlapping drills. Each invocation refreshes runtime telemetry even when no new drill is due; the report retains the last actual drill time. The drill verifies the export, restores into disposable scratch, validates the disabled candidate with the trusted running code and regenerates launchers without executing them. Scratch is removed. Runtime history retains the last selected number of drill JSON reports, one HTML report, last-result.json and telemetry.json outside configuration/Git.

Add the telemetry source to monitoring to use the default `setup-recovery-failed` rule and ordinary source-staleness rules with your configured email/webhook channels. No notification is sent merely by enabling a drill. The HTML report describes exact bounds: passing a file rehearsal does not prove credentials, external tools, scheduler identity, database data recovery or service reconstruction. Perform live access checks and occasional human-reviewed recovery on a disposable host too.
