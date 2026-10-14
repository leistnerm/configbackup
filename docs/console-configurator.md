# Console configurator

Install the optional interface using the same Python executable that runs ConfigBackup:

```shell
python -m pip install -r requirements.txt -r requirements-console.txt
python configure.py --config configbackup.yaml
```

On an interactive terminal, the configurator automatically opens a full-screen interface when prompt_toolkit is installed. It refreshes the current screen instead of appending menus. The existing configuration workflows, validation, comment preservation, atomic saving and timestamped backups remain in use. Nothing is saved merely by navigating or closing the interface.

## Menu layout

The backup configurator has seven areas, plus a Commands menu:

| Area | Contents |
|---|---|
| Overview | Loaded configuration summary, effective settings and setup checks |
| Backup & collection | Tasks, shared connections, optional collectors, section presets, reusable profiles and discovery |
| Storage & security | Archive/setup-backup settings, task defaults, named retention policies, Git, sensitive collection and deletion |
| Monitoring & reports | Sources, alerts, notification channels, monitoring settings, HTML and performance reports |
| Setup & launchers | Guided setup, dependencies, launcher generation/management, PowerShell policy, Windows readiness, diagnostic profiles and permission scripts |
| Recovery & maintenance | Journals, recovery exports, verification/rehearsal, readiness targets and maintenance workflows |
| Advanced configuration | Nested configuration browser and redacted configuration display |

Monitor, schedule and registry editors show only the relevant areas. Submenus keep
your selected area open after an action. The numbered fallback uses the same groups;
its Commands menu is reached through the main menu.

## Keyboard controls

| Key | Action |
|---|---|
| Ctrl+S / F2 | Save from any idle configuration menu or field; stay in the editor |
| F3 | Validate accepted configuration without altering unfinished input |
| F4 | Show changes since the last successful save |
| F5 / F6 | Undo / redo accepted configuration edits; return to the main menu |
| F10 | Exit, confirming before discarding unsaved edits or unfinished input |
| Up / Down | Select a menu item |
| Home / End / Page Up / Page Down | Navigate long menus |
| Type | Filter a menu; Backspace removes filter characters |
| Enter | Activate selection, accept a field, or finish a checklist |
| Escape | Clear a menu filter, go back, or cancel unfinished input |
| Tab | Switch between navigation/input and scrollable results |
| Ctrl+C twice | Abandon unsaved editing and exit |

The command bar stays visible in submenus. Commands are disabled while an operation
or another command is running. On macOS, function keys may require Fn; Ctrl+S is an
alternative for saving. Commands are not clickable buttons.

Saving an active configuration value accepts and parses it first. Invalid input is
not written. Save at an operational prompt preserves the unfinished prompt; it does
not approve the operation. An unfinished new item offers **Finish creating this
item** (save after completion) or **Save completed changes only** (keep editing the
draft). Cancelling the item cancels its deferred save.

Save validates, preserves a timestamped backup and writes atomically. It refuses to
overwrite a file changed on disk since loading or the last save. Failed validation
or writes retain accepted in-memory edits. Success updates the saved baseline and
clears the unsaved indicator. Repeated saves work without reopening the editor.

Validate and Changes preserve typed text, cursor position, checklist selections,
menu filtering and results-panel focus. They inspect accepted configuration, not an
unfinished draft. Undo/redo asks before discarding unfinished input and returns to
the main menu so old submenu references cannot edit stale objects. Escape cancels
unfinished input without discarding earlier accepted edits. Saved files are not
reverted by Undo until another Save.

Completed external actions, such as generating a launcher or approving an
installation, are not reversed by undo or cancellation. During such operations,
wait for completion; keyboard commands do not interrupt filesystem changes.

The details panel keeps the most recent 50,000 characters of output in memory;
it does not create a diagnostic log file. Secret redaction and sensitive-collection
confirmations still apply. External installers temporarily use the normal terminal.

## Terminal compatibility

Use a normal interactive terminal, preferably at least 80 columns by 24 rows. The layout resizes with the terminal. Windows Terminal, macOS Terminal and Linux terminals are intended targets. Native Windows console behavior remains unverified; automated key-dispatch tests run on macOS and Linux.

Force the interface with `--console` (missing dependencies or a noninteractive terminal produce an actionable error). Use `--plain` for numbered prompts, accessibility preferences, basic terminals or troubleshooting. Redirected input/output and terminals advertising `TERM=dumb` use plain prompts. Commands such as `--check` and `--preview` retain their noninteractive behavior.

For offline installation, include `--feature console` when building a deployment pack on the target platform with the console dependencies already installed.

## Existing configurations

The file selected by `--config` is loaded before the interface opens. The initial results panel lists its sections and item counts. **Current configuration** browses the actual nested mappings and lists, including existing tasks and connection profiles, and edits existing scalar settings without rebuilding the file. Dedicated Tasks, Shared connections and other menus add, duplicate or remove items. Unknown/custom fields are retained. Secret values and secret-bearing arguments remain hidden; environment-variable references can be edited. Changes remain in memory until Save validates and writes the file.

## Dependencies and timezone repair

**Python dependencies** checks installed core editor packages, the optional console package and actual IANA timezone availability. It uses the exact Python executable running the configurator. Select missing/unsupported packages or choose packages to update within supported version constraints, review the command, then type `INSTALL` to run it. Nothing installs merely by checking. Restart the configurator after installation to load updated packages.

A missing timezone database now produces an explicit `tzdata` installation message, rather than suggesting the default UTC timezone is invalid. Linux/macOS system timezone databases are accepted without requiring an unnecessary tzdata package. Missing console dependencies offer installation before falling back to numbered prompts.

The **PowerShell execution policy** menu controls Windows child-process policy and newly generated launchers. See [setup](setup-walkthrough.md#powershell-execution-policy-on-windows). Save changes and regenerate existing launchers.

See [configuration workflows](configuration-workflows.md) for launcher management, recovery, references and repair.
