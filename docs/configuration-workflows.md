# Configuration and operational workflows

## Editing

Open the existing file with `python configure.py --config configbackup.yaml`.
**Current configuration** navigates nested fields with a path in the prompt.
Typed enum and IANA-timezone choices cover common fields; maps/lists can be
navigated and have settings/items added or removed. Advanced JSON remains available
for non-sensitive complex fields. Environment/variable entries retain literal types.
Hidden secret containers are not copied into prompts. Use environment/vault
references; existing inline secrets remain preserved without being displayed.

The menus are grouped by purpose; see the [console menu and shortcut guide](console-configurator.md).
**Ctrl+S/F2 Save**, **F3 Validate** and **F4 Changes** work in submenus and fields.
Save retains your location. **F5/F6 Undo/Redo** return to the main menu and ask
before discarding unfinished input. Earlier accepted edits survive Escape.
History is bounded to 100 accepted-change snapshots and stays in memory; it does
not reverse installations or filesystem operations. The screen marks unsaved changes.
Save validates, detects external edits and keeps a backup. Deferred saves for a
new item happen only after completion, or explicitly save completed changes alone.

**Tasks → Rename with references** retains the prior state identity and updates
dependencies and exact task-scoped sensitive Git approvals after confirmation.
**Shared connections → Rename connection** updates task references. Removal of a
referenced task/connection is blocked with the dependent names. Custom scripts,
paths and external scheduler arguments are not rewritten automatically.

## Launchers and Windows checks

**Launcher manager** lists registered directories, hashes and stale reasons.
Save configuration first. Type `REGENERATE` only after reviewing the selected
launcher. Previous files are retained under `backup-*` in that launcher directory;
a failed update restores installed files from those copies. Legacy registrations
without generation metadata need a newly generated launcher. Review and remove
old backups manually when no longer needed; they are not pruned automatically.

The generator defaults to the current Python interpreter. Windows PS1 output uses
UTF-8 with BOM for Windows PowerShell compatibility. Execution policy is recorded
with the launcher and passed only to the child process. Regeneration/installation
never changes user/machine execution policy. **Windows readiness** queries shell
versions, account, modules and effective policy scopes; run the corresponding
setup/diagnostic launcher under the real scheduler account as well. A successful
interactive check does not prove unattended vault access.

## Recovery and configuration repair

**Recovery status and reset** shows the same assessment used by startup plus
preserved reset-evidence size and age. `RESET` explicitly abandons undo while keeping
the journal and current state together. It does not repair an incomplete snapshot.
Rerun collection and review failures/publication before removing evidence.
`PURGE` permanently removes only the selected reviewed reset-evidence directory,
after rechecking it under the run lock, and leaves a local cleanup audit record.
Neither undo nor redo reverses these filesystem actions.

For malformed YAML:

```sh
python configure.py --config configbackup.yaml --repair-config
```

Select an existing `.bak`; it must parse and validate. Type `RESTORE` to preserve
the current file as an `.invalid.bak` and atomically restore the selected bytes.
Without a valid backup, edit the indicated line/column manually. The tool never
silently starts over with an empty configuration.

Missing core dependencies produce the exact Python installation command; an
interactive CLI offers an explicit `INSTALL` confirmation. **Python dependencies**
checks supported packages and timezone data. Restart after dependency updates.
