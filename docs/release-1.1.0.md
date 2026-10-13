# ConfigBackup 1.1.0

Sensitive-data collection is disabled by default. Each approved collector section may retain exposed secrets or reusable password hashes; writing its output to Git requires an independent archive-task/section approval and a private destination.

See [sensitive collection](sensitive-collection.md) for supported sections, configuration, failure isolation, validation and platform limits. The optional SSIS environment recovery adapter depends on internal SSISDB structures and remains unverified against a real Windows SSISDB catalog. It does not recover every package/project secret.

The Windows kit includes 276 selected application fixtures and the unchanged GPO harness 1.0.0. Native Windows collection remains subject to the [platform limitations](../VALIDATION.md).
