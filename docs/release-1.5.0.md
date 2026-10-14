# ConfigBackup 1.5.0

The configuration editor now groups workflows by purpose and provides global
commands from submenus and fields.

- Seven areas replace the long flat menu. Commands remain separate; profile labels
  distinguish collector section presets from named reusable profiles.
- Ctrl+S/F2 saves without leaving the current location. F3 validates, F4 shows
  changes, F5/F6 undo/redo and F10 exits with unsaved-input protection.
- Save accepts active configuration fields after parsing. It never treats a Save
  key as approval for an operational confirmation.
- Unfinished items offer completion followed by save, or saving completed changes
  only. Cancelling a draft cancels its deferred save.
- Failed validation, failed writes and external file changes retain in-memory edits.
  Cancelling a field preserves earlier accepted changes.
- Validate and Changes retain typed text, cursor, menu selection/filter and focus.
  Undo/redo returns to the main menu to avoid stale submenu references.
- Commands are unavailable during running operations. The Windows test kit adds
  keyboard regression fixtures and an explicit prompt_toolkit prerequisite.

See [menu layout and shortcuts](console-configurator.md),
[configuration workflows](configuration-workflows.md) and
[validation limits](../VALIDATION.md).

## Upgrade

Extract the complete release into a fresh application directory and point launchers
at it. Preserve configuration, archives/state, vaults and recovery evidence.
The `configbackup_lib` package is required alongside root command wrappers.
YAML configuration formats remain unchanged. Save stays in the editor; use F10
or Commands → Exit to close it after saving.

Native Windows console, CMD, PowerShell 5.1 and Group Policy behavior still require
validation on Windows. Portable keyboard fixtures do not establish those results.
