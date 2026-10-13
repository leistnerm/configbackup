# ConfigBackup 1.4.0

This release fixes journal recovery and configuration editing, expands launcher
management and readiness checks, and moves implementation modules into a package.

## Fixes

- Hidden inline credentials cannot become advanced field-prompt defaults.
- Cancelled boolean/text edits preserve existing settings; confirmations still default safely.
- Journal directories reject symlinks/junctions before resolution. Prune-only
  transactions no longer require a temporary Git checkout.
- Inspection and startup use the same recovery assessment, including missing PR
  worktrees and committed journals. Reset retains evidence instead of undoing files.
- Generated Windows PS1 files use UTF-8 with BOM; partial writes are cleaned up.

## Configuration workflows

- Undo/redo, an unsaved indicator, typed enum/timezone choices, nested field/item
  creation/removal and context-aware secret protection.
- Reviewed task/connection renames update declared references; task renames retain
  state identity. Referenced objects cannot be removed through dedicated menus.
- Launcher inventory shows missing/modified files and changed configuration/policy.
  Regeneration requires saved configuration and retains previous files in a backup.
- Windows readiness reports the current account, interpreter, PowerShell editions,
  effective policies and scopes, and installed relevant modules without changing policy.
- Recovery screen offers explicit reset/evidence cleanup under the run lock.
- `--repair-config` restores an explicitly selected validated backup while preserving
  the malformed original. YAML errors show location without printing source secrets.
- CLI bootstrap provides an explicit repair option for missing core dependencies.

## Upgrade and package layout

Extract into a **new application directory**. Keep your YAML, archive/state, vaults
and asset registry. Do not delete pending journals as part of an upgrade. Point
launchers at the new application, or regenerate them after saving configuration.
The existing runnable root commands remain available; internal imports now use
`configbackup_lib`. Do not copy only the root Python files: the package is required.
See [package layout](package-layout.md), [configuration workflows](configuration-workflows.md)
and [setup](setup-walkthrough.md).

The Windows kit adds a native CMD/PowerShell launcher fixture using temporary files
and a harmless Python child. It tests Unicode/spaces, process policy and exit codes;
it does not run a real backup or change policies. Group Policy can block the test.
Native Windows results must be reviewed on Windows; macOS fixtures do not certify them.
