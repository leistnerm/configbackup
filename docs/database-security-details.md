# Database security details

Deploy the engine and collectors from the same distribution. Install requirements in the interpreter used by the scheduled account. Failed or disabled collection scopes never authorize deletion of their previous snapshots.

The active configuration file receives its own filesystem task by default: `configbackup-settings`, destination `_configuration/<filename>`, up to 30 versions. It is excluded from Git regardless of default storage. Set `backup.include_configuration: false` if you already manage this file or need custom retention. Place the live configuration outside the archive. Recognized schedule/registry inputs and registered startup launchers are also included by default; see [setup recovery](recovery-setup.md) for discovery rules and exclusions. Credential stores are not automatically copied. Protect configuration archive copies like the original file.

String switches such as `enabled: "false"` fail validation. Use YAML booleans. SMTP email section selections support both existing list and enabled-mapping forms. Conflicting collector output paths are rejected before cleaning. Update duplicated configurations that reused staging paths; the editor duplicates related archive tasks as a group.

Optional inventories and the review portal are off unless configured. Existing base Windows role selections do not silently enable deeper detail bundles. Profiles are menu presets applied to managed database tasks; they do not install a scheduler. Runtime history, report caches and notification queues remain outside Git by default.

Read the dedicated [editor guide](configuration-editor.md), [database guide](database-collectors.md), [platform coverage](optional-platform-inventory.md) and [report guide](review-portal.md). The Windows test kit checks local fixtures, repeated read-only GPO capture and explicitly selected components. It does not change policies or provision roles. Optional prerequisite installation is a separate local choice, with fresh checks afterward.

Manifest fixtures cover disabled schema/inventory scopes and transitions from parent scopes. Targeted regressions cover secret handling, failure isolation, restore protections, configuration editing, queue recovery, report integrity and remote probes.

Configuration exports and restore previews are not database data backups or full bare-metal/service recovery plans. Availability Groups, clustering, replication, SSIS/SSAS/SSRS and Windows provider combinations require their own live labs; optional metadata does not establish full enterprise-service coverage. Use [VALIDATION.md](../VALIDATION.md) to distinguish supported coverage and platform limits.
