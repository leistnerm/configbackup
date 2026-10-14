# ConfigBackup 1.6.0

This release improves configuration editing, recovery safety and release packaging.

- Keep task fields editable when malformed inheritance settings prevent the context preview from resolving.

- Preserve pre-existing seal destinations on copy failure and release launcher locks when backup creation fails.
- Omit invalid observed durations with warnings and reject non-finite or negative schedule overrides.

- Item-first task and connection menus, F1 help and F8 command/setting search.
- Configured selector defaults, consistent global shortcuts, reliable undo and
  cancellation, inline numeric/JSON/timezone errors and direct validation links.
- Effective task values and their default/profile/connection origins before reset;
  refreshed dependency status and explicit current-edit versus Save-and-test checks.
- Independent state identities for duplicated tasks. Empty-section navigation and
  cancelled no-match selector saves do not write configuration changes.
- Typed repair of malformed mapping/list settings, including validation navigation.
- Configuration restore uses validated bytes and stops if the candidate or target
  changes during confirmation. Non-mapping YAML is rejected even when empty/false.
- Git credential scanning detects quoted JSON/YAML keys, including UTF-16 and ZIP
  contents. Per-section collection and separate sensitive Git opt-ins remain required.
- Release builds derive the application version from source and include only
  tracked paths, excluding untracked local configuration and scratch files.

## Upgrade

Extract the complete application ZIP into a fresh directory and update launchers
as needed. Preserve configuration, archives/state, vaults and recovery evidence.
The `configbackup_lib` package must accompany the root commands. Existing YAML
formats remain supported; no state migration is required.

The stronger credential scanner may block previously missed quoted credentials.
Review findings and use the documented explicit sensitive-section policy where
appropriate; do not disable the gate to bypass unexplained findings.

See [configuration controls](console-configurator.md), [sensitive collection](sensitive-collection.md),
[release building](building-releases.md) and [validation](../VALIDATION.md).
Native Windows terminal/CMD, Windows PowerShell 5.1 and live Windows roles remain
unverified. Portable fixtures do not establish native platform compatibility.
