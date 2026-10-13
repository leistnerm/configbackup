# Optional Windows component configuration

ConfigBackup includes local, read-only IIS, DNS Server, DHCP Server and Hyper-V adapters. They are **off by default**. The base coverage below remains the default when selected. Optional coverage includes separately selected [deeper role queries, certificates, DFS/NPS/WEF/WSUS/ADCS and cross-platform adapters](optional-platform-inventory.md). Existing installed roles/features and Windows service inventory remain in the system collector. These adapters document selected configuration; they are not complete role backups or recovery tools.

Select roles in the configuration editor's **System** template, or add repeated flags to the system collector:

```powershell
python collectors/system/collect_system.py --output C:\Fresh\Host `
  --windows-component iis --windows-component dns
```

For component collection without the broader host inventory:

```powershell
python collectors/system/windows_components.py --output C:\Fresh\Roles `
  --component iis --component dns --component dhcp --component hyperv
```

Use an empty output directory each time. [The task example](../examples/windows-components.yaml) includes all four flags but initially enables only IIS; toggle `sections.windows-components/<role>.enabled` without removing the configuration. The standalone collector also accepts the engine's `CONFIGBACKUP_OUTPUT`. Use `collection_manifest: true` on its archive task. Normal exits are 0 for completed requested queries and 6 for partial collection. Existing system `--strict` behavior remains available.

## Base coverage (without extra detail flags)

| Component | Configuration documented | Explicit limits |
|---|---|---|
| IIS | Local site/application/virtual-directory definitions, application pool definitions and defaults, global modules, HTTP/HTTPS bindings and certificate references; selected effective server and application-root authentication, authorization, TLS access, request filtering, handlers, modules, default documents, directory browsing, errors/logging, headers, redirects, static content and compression | Uses Microsoft.Web.Administration. Does not copy website content, arbitrary raw web.config files, nested-directory overrides, URL Rewrite/ARR/custom extension sections, FTP configuration, shared-configuration encryption keys, certificate private keys or IIS backups. Effective values are not a deployable restoration script. |
| DNS Server | Server settings, configured forwarders/order, recursion, scavenging settings, root hints, zone discovery/settings/aging and resource records including TTL and type-specific data | Local DnsServer module. No DNSSEC private keys, cache/runtime counters, explicit DNS policy/zone-scope/virtualization-instance inventory or AD replication/ACL export. Dynamic DNS record changes and SOA serials remain meaningful differences. |
| DHCP Server | Server v4/v6 settings, options and option definitions; v4 DNS settings, classes, filters, failover configuration and policies; per-v4/v6 scope settings, options, reservations and exclusions; v4 scope DNS, policy options and address ranges | Local DhcpServer module. No leases/statistics, server credentials or live failover state. Reservation-specific option overrides, server-policy option/range details, v6 classes, DHCP database/recovery backup and every extension are not included. |
| Hyper-V | Host paths/migration settings and switches; per-VM identity, startup/shutdown/checkpoint settings, memory/CPU, attached disks/DVD paths and limits, NIC settings/VLAN, firmware/BIOS boot order and selected security flags | Local Hyper-V module. No VM/disk/checkpoint data, guest contents, key protectors, replication configuration, virtual SAN or switch-extension/team/ACL inventory. VM runtime state/usage/IP discovery is not included. Property support varies by Windows version; missing required properties fail their query. |

Read operations are local. The collector never enables roles or installs RSAT/modules, refreshes policy, starts/stops services or VMs, writes IIS configuration, or changes DHCP/DNS data. Windows PowerShell is preferred for inbox module compatibility. Use approved module installations and an account that can read the intended configuration; a role/module/permission error is reported, not silently treated as absence. No automatic privilege grants are applied.

## Failure and deletion rules

Each query or object has a separate manifest scope under `windows-components/<role>/...`. A failed scope cannot update or delete its prior archived/Git files. Other scopes continue. A whole-reader failure is isolated from other components. Discovery is recorded separately: an object missing from discovery is **not** treated as deleted, because visibility and permissions can change. This intentionally retains old absent-object snapshots until manually reviewed. A successfully read query can replace its own prior JSON, including an explicitly empty collection.

Object paths use a stable hash of the case-insensitive name, or VM ID. Discovery records map each key back to the visible name. This avoids unsafe file names and case collisions. Disabled scopes are reported and preserved. A parent switch disables children. Requested roles on macOS/Linux fail with a Windows-required status; ordinary host collection on those platforms remains available.

## Determinism and sensitive fields

JSON property names are sorted. Only explicitly identified sets of top-level inventory rows are sorted; nested arrays retain order, including DNS/DHCP address priorities, IIS handler/module order and VM boot order. No broad timestamp stripping is applied. DNS `LastScavengeTime`, `AvailForScavengeTime`, forwarder `ReorderedIPAddress`, record refresh timestamps and a dynamically assigned VM NIC MAC are excluded from configuration output; configured static MACs, TTLs and record data remain. Existing host telemetry collectors retain their separate behavior. This adapter does not add component runtime telemetry.

The IIS serializer substitutes `[SECRET NOT COLLECTED]` for known password/key/token attributes without reading their values, and for environment-variable values. Other adapters exclude credential queries and mask known password/secret/private-key properties. This is not a universal secret detector: configuration strings, DNS TXT records, custom headers and annotations can still contain sensitive data. Keep the existing Git secret gate and review policy. Certificates are references only. No raw role export is retained before sanitization. Concise failure records omit arbitrary exception messages.

## Local acceptance tests

The workstation launcher offers a comma-separated component choice (Enter selects none). Selected roles are captured twice into fresh folders, and their certified file hashes and differences appear in the single HTML report. Review the discovery records and several known objects even when all comparisons pass. Missing/failed/disabled scopes, or an object visible in only one capture, make the comparison INCOMPLETE. Config differences produce REVIEW and a full local diff. A permissions failure should preserve old archives; do not revoke permissions, stop services or remove real objects solely to force a test on a computer.

Record these checks locally:

1. IIS: expected sites/pools/apps/bindings, certificate references, identity type, effective auth settings, handler order and secret markers. Inspect subdirectory/custom-extension gaps separately.
2. DNS: expected zones, known A/AAAA/CNAME/MX/TXT/SRV/SOA records, TTLs, forwarder priority and aging. Account for legitimate dynamic DNS changes.
3. DHCP: v4/v6 scopes, options in their configured order, reservations/exclusions and v4 policies. Compare against the console; record the explicitly omitted overrides above.
4. Hyper-V: expected VM IDs, paths, memory/CPU, disk attachments, NIC/VLAN, boot order and security flags. Confirm omitted replication/checkpoint content is covered elsewhere.
5. For each role, compare both captures and record unavailable cmdlets/properties. Never infer “no configuration” from access denied or a missing module.

## Validation and remaining work

Python fixtures cover deterministic row ordering, preservation of nested order, per-query/per-role failures, disabled sections, invalid manifests, serialization failure, and actual filesystem/Git preservation of failed or disappeared objects. The configuration editor selection is tested. A PowerShell 7 fixture executes the actual reader functions with fake IIS elements and mocked DNS/DHCP/Hyper-V commands, including password getters that must never run and continued queries after failures.

**No live Windows role was available for this release's validation.** Windows PowerShell 5.1, real IIS/MWA schema traversal, Windows module/property variants, permissions, DHCP policies, Hyper-V devices and real installed-role repeatability require the local tests above. A passing mock does not establish those capabilities. The original SQL/PostgreSQL collectors are unchanged; their previous evidence remains separate in [VALIDATION.md](../VALIDATION.md).

Primary references: [IIS configuration elements](https://learn.microsoft.com/en-us/dotnet/api/microsoft.web.administration.configurationelement?view=iis-dotnet), [DNS Server cmdlets](https://learn.microsoft.com/en-us/powershell/module/dnsserver/), [DHCP Server cmdlets](https://learn.microsoft.com/en-us/powershell/module/dhcpserver/), [Hyper-V cmdlets](https://learn.microsoft.com/en-us/powershell/module/hyper-v/).
