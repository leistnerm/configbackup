# Application package layout

Root Python scripts are compatibility commands. Their implementation is grouped under:

- `configbackup_lib/engine`: execution, collection helpers, launchers and readiness.
- `configbackup_lib/configuration`: schema, persistence, connections, references and repair.
- `configbackup_lib/ui`: console adapter, forms, menus and dependency workflow.
- `configbackup_lib/git`: secret gates, sensitive approvals and change review policy.
- `configbackup_lib/recovery`: journaling, retention, recovery exports and deployment packs.
- `configbackup_lib/reporting`: reports, notifications, monitoring and runtime analysis.

`collectors/`, `examples/`, `docs/`, `tests/` and release `tools/` remain separate.
`APPLICATION_ROOT` locates bundled resources independently of the current directory.
Recovery exports and deployment packs include the entire implementation package.

Existing commands such as `python configbackup.py`, `python configure.py`,
`python journal_recovery.py` and `python deployment_pack.py` retain their names.
Root command imports alias their implementation for compatibility. Internal modules
are not a stable public API: custom Python integrations that imported helpers such
as `completeness` must use their new qualified imports, for example
`from configbackup_lib.engine.completeness import Coverage`. Collector manifests and
YAML settings retain their public formats.

Install the distribution as a complete tree. This release preserves ZIP deployment;
it does not require a wheel installation or new console-script command names.
