# Changelog

## 1.6.0

- Keep task fields editable when malformed inheritance settings prevent the context preview from resolving.

- Preserve pre-existing seal destinations on copy failure and release launcher locks when backup creation fails.
- Omit invalid observed durations with warnings and reject non-finite or negative schedule overrides.

- Cancel saves from empty selector searches and repair malformed mapping/list values with typed input.
- Bind configuration restore to validated bytes and reject files changed during confirmation.
- Detect quoted JSON/YAML credentials in the Git secret-scan gate, including UTF-16 and archives.
- Package tracked source files only and derive release versions from the application.

- Give duplicated tasks independent state identities and separate connection names from menu commands.
- Protect unfinished selector input on exit/search/undo and resume fields when a command confirmation is cancelled.
- Unify guided field adapters, validate report/PerfMon timezones inline, and avoid inserting a report section on cancellation.
- Link validation failures to settings, show inherited task/profile/connection values before reset, and refresh dependency summaries after repair.

- Preserve configured selector values, unify console undo, cancel abandoned deferred saves and offer every engine task phase.
- Keep read-only settings navigation free of empty-section changes.
- Add context help, cross-menu command/setting search, inline field errors, inheritance/reset context and session overview status.
- Use item-first task/connection menus and explicit current-edit versus Save-and-test checks.
- Stage recorder forms and share validated field handling with older guided inputs.

## 1.5.0

- Group configuration workflows by purpose, with fixed session commands and clearer profile labels.
- Save, validate and review changes from nested menus and fields; retain navigation, input and cursor.
- Keep completed edits on cancellation; protect drafts, failed writes and external file changes.
- Add keyboard regression checks to the Windows kit, including explicit console dependency setup.

## 1.4.0

- Fix secret prompt exposure, cancelled edits, symlink/junction journal handling, prune-only recovery and launcher encoding/cleanup.
- Add undo/redo, typed settings, task/connection reference updates, launcher management, Windows readiness and explicit recovery/repair menus.
- Move implementation to `configbackup_lib` while preserving runnable root commands and complete recovery/deployment exports.
- Add native Windows launcher fixtures and expanded portable safety checks.

## 1.3.1

- Explicit process-scoped PowerShell execution policy in the configurator, collector environments, diagnostics and generated Windows launchers.
- Setup walkthrough with SSIS, sensitive collection and execution-policy guidance.

- Preserve PR worktrees until journal finalization; recover missing checkouts when the local PR branch is unchanged.
- Explicit journal reset preserves recovery evidence and current state, with an incomplete-run acknowledgement.

## 1.3.0

- Browse and edit existing nested configuration, with section/item inventory and preserved custom fields.
- Explicit dependency checks/install/update options, including actionable timezone-data diagnostics.
- Optional full-screen console configurator with arrow-key menus, search, checkbox selection, editable fields, colored header/shortcut bar and scrollable results.
- Save, validation and change-preview shortcuts; explicit discard confirmation and numbered-menu fallback.
- Console dependencies can be included in offline deployment packs. External installers temporarily suspend the screen.

## 1.2.0

- Independently approved sensitive SSIS project and package parameter exports.
- Current-deployment server literal recovery, explicit environment references and unrecovered design-default status.
- Per-section failure preservation with synthetic encryption, corruption, permission and repeat-determinism checks; live Windows SSISDB remains unverified.

## 1.1.0

- Explicit per-section sensitive exports with separate private Git publication approval.
- Isolated opt-in SSIS environment-value recovery; normal SSIS capture continues after recovery failure.
- Sensitive manifest classification, hidden HTML value previews and password-hash detection.
- Guided configuration and synthetic/disposable-database validation scripts.

## 1.0.0 — Initial release

- Configuration archives and Git snapshots with independent database/section failure preservation, completeness certification and scoped deletion detection.
- SQL Server, PostgreSQL and host collectors, deterministic comparison, and explicit unsupported/unavailable section status.
- Guided configuration menus, shared connections, collection profiles, discovery, read-only access scripts and startup launchers.
- Schedule analysis, observed runtimes, optional native performance recording, overlap reports and notification summaries.
- Integrity checks, reviewed repair, recovery exports, setup backups and platform-specific offline deployment packs.
- Windows local acceptance kit, offline fixtures, documentation and examples.

See [validation](VALIDATION.md) for platform coverage and limitations.
