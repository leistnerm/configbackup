# Console configurator

Install the optional interface using the same Python executable that runs ConfigBackup:

```shell
python -m pip install -r requirements.txt -r requirements-console.txt
python configure.py --config configbackup.yaml
```

On an interactive terminal, the configurator automatically opens a full-screen interface when prompt_toolkit is installed. It refreshes the current screen instead of appending menus. The existing configuration workflows, validation, comment preservation, atomic saving and timestamped backups remain in use. Nothing is saved merely by navigating or closing the interface.

## Keyboard controls

| Key | Action |
|---|---|
| Up / Down | Select a menu item |
| Home / End / Page Up / Page Down | Navigate long menus |
| Type | Filter the current menu; Backspace removes filter characters |
| Enter | Activate selection, accept a field, or finish a checklist |
| Space | Toggle a checklist item |
| Esc | Clear the menu filter, then go back; cancel checklist edits or decline a confirmation |
| Tab | Switch between the current form/menu and the scrollable results panel |
| Arrows / Page Up / Page Down in results | Scroll output, previews and validation messages |
| F2 / F3 / F4 at the main menu | Save / Validate / Preview changes |
| F10 at the main menu | Exit; asks before discarding changed configuration |
| Ctrl+C twice | Cancel the editor and discard unsaved configuration edits |

Text fields support normal cursor editing, Home/End and paste. Escape does not cancel a partially completed text form; use the menu's Back option or cancel the editor. Explicit side effects already performed, such as generating a launcher or approving an installation, are not undone by cancelling the configuration editor. While an operation is running, the interface waits for it to finish instead of abandoning a background write.

The details panel keeps the most recent 50,000 characters of output in memory; it does not create a new diagnostic log file. Existing secret redaction and sensitive-collection confirmations still apply. Native installers and deployment operations temporarily return to the normal terminal, then restore the interface.

## Terminal compatibility

Use a normal interactive terminal, preferably at least 80 columns by 24 rows. The layout resizes with the terminal. Windows Terminal, macOS Terminal and Linux terminals are intended targets. Native Windows console behavior remains unverified; automated key-dispatch tests run on macOS and Linux.

Force the interface with `--console` (missing dependencies or a noninteractive terminal produce an actionable error). Use `--plain` for numbered prompts, accessibility preferences, basic terminals or troubleshooting. Redirected input/output and terminals advertising `TERM=dumb` use plain prompts. Commands such as `--check` and `--preview` retain their noninteractive behavior.

For offline installation, include `--feature console` when building a deployment pack on the target platform with the console dependencies already installed.

## Existing configurations

The file selected by `--config` is loaded before the interface opens. The initial results panel lists its sections and item counts. **Current configuration** browses the actual nested mappings and lists, including existing tasks and connection profiles, and edits existing scalar settings without rebuilding the file. Dedicated Tasks, Shared connections and other menus add, duplicate or remove items. Unknown/custom fields are retained. Secret values and secret-bearing arguments remain hidden; environment-variable references can be edited. Changes remain in memory until Save validates and writes the file.

## Dependencies and timezone repair

**Python dependencies** checks installed core editor packages, the optional console package and actual IANA timezone availability. It uses the exact Python executable running the configurator. Select missing/unsupported packages or choose packages to update within supported version constraints, review the command, then type `INSTALL` to run it. Nothing installs merely by checking. Restart the configurator after installation to load updated packages.

A missing timezone database now produces an explicit `tzdata` installation message, rather than suggesting the default UTC timezone is invalid. Linux/macOS system timezone databases are accepted without requiring an unnecessary tzdata package. Missing console dependencies offer installation before falling back to numbered prompts.
