# ConfigBackup 1.2.0

Sensitive SSIS project and package parameters now have separate opt-in sections and separate Git approvals. Each recovers server-configured literals from the current deployed version, records environment references without resolving them, and labels design-only defaults as unrecovered.

Failed decryption, denied permissions, incompatible catalogs and incomplete inventories preserve the affected section's previous archive/Git bytes. Normal SSIS, environment recovery and the other parameter section continue.

The adapters depend on internal SSISDB structures. Synthetic SQL encryption and failure-isolation checks passed; real Windows SSISDB compatibility remains unverified. Protected package contents and execution-time values are outside scope. See [sensitive collection](sensitive-collection.md).

The Windows kit includes 278 application fixtures and the unchanged GPO harness 1.0.0.
