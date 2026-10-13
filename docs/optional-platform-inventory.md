# Optional platform inventory 

These adapters are read-only and **off by default**. Use explicit selections on `collectors/system/collect_system.py`; a selected missing tool, denied query or unsupported format is a failed scope, not an empty successful backup. Every query/object has its own manifest scope. Failed, disabled and unreported objects preserve prior archive/Git files. Keep `collection_manifest: true` on the archive task. Disappeared discovered objects are retained for review; they are not automatically deleted.

The system collector continues independent adapters after a broad host-inventory exception. Files already written by that failed broad read are marked failed rather than certified. Independent adapters can still publish their successful scopes. A complete manifest is finalized and the normal partial exit code is 6 (strict mode remains nonzero).

## Selections

```powershell
python collectors/system/collect_system.py --output C:\Fresh\Host `
  --windows-component certificates --windows-component wef `
  --windows-component dfs --windows-component nps `
  --windows-component wsus --windows-component adcs `
  --windows-detail iis-extended --windows-detail dns-policies `
  --windows-detail dhcp-reservation-options `
  --windows-detail hyperv-network --windows-detail hyperv-replication
```

Choose only installed services that you intend to read. Every detail option implies its existing base component. The existing `iis`, `dns`, `dhcp` and `hyperv` selections retain their base coverage when no detail option is selected. For component-only collection use `windows_components.py --component NAME --detail DETAIL`; both flags repeat. Selection itself does not install roles, RSAT, modules or clients, grant permissions, refresh policies or start services.

`collector_catalog.catalog()` is a stdlib-only catalog with flags, default states, scopes, prerequisites, costs and limits. `collector_catalog.discover()` checks local executable paths and, on Windows, available module names without importing/enabling roles. Presence does not establish permissions, service availability or complete coverage.

## Windows additions

| Selection | Implemented reads | Limits to review |
|---|---|---|
| `certificates` | Public subject, issuer, serial, thumbprint, validity, signature/public-key algorithm, encoded public extensions and private-key-presence flag for LocalMachine My/WebHosting/Root/CA and CurrentUser My; IIS HTTPS certificate references | Never obtains/exports private keys. No HTTP.sys/RDP/SQL binding inventory, certificate enrollment/renewal, chain trust/revocation or remote TLS validation. A missing store or IIS reader fails only its query. Public extensions include SAN data as encoded DER; no promise of a complete friendly SAN decode on Windows. |
| `dfs` | Namespace roots hosted by this computer, folders and targets; local DFSR machine/group/folder/connection CIM configuration | Local scope, no domain-wide search, share contents, replication backlog/health or ACL export. DFS namespaces can depend on directory-service reads performed by the installed management module. |
| `nps` | RADIUS client name/address/enabled/vendor; network/connection-request policy name, state, processing order and source/access metadata when recognized in `netsh nps show np/crp` | English summary parser only. Localized/unrecognized output fails that query. No conditions/profile blobs, secrets, shared-secret templates or full deployable policy export. RADIUS secret properties are never accessed. |
| `wef` | Subscription discovery and allowlisted subscription XML: enabled state, query, delivery/batching/heartbeat, transport, source addresses/allowed sources, log and content settings | No events, runtime subscription status, credentials, service changes or `wecutil qc`. XML DTDs are prohibited. Credential nodes are omitted before JSON is written. |
| `wsus` | Selected upstream/proxy/download settings, language selections, synchronization schedule, classifications/categories and computer-group names | No passwords, update binaries, computer inventory, approvals, native database backup, synchronization or cleanup. Server property variations fail their individual query. |
| `adcs` | Exact public CA registry value allowlists: identity/type/certificate hashes, validity/CRL/AIA publication settings, selected algorithm names and default-policy flags | No signing keys, key containers, CA database, issued-certificate inventory, recovery backup, enrollment templates/AD objects or unrestricted registry export. Absent allowlisted values remain absent, reflecting OS/CA configuration variations. |
| `iis-extended` | Rewrite rules/outbound rules/allowed variables, ARR proxy and server farms, FTP auth/SSL/logging/message/firewall sections; configured location paths in applicationHost and application-root configuration | Preserves ordered collections. Only selected sections at explicitly configured locations; does not recursively crawl website content or every nested `web.config`. Missing extensions/sections fail independently. Known password/token/key/connection-string/environment values are masked, not a universal secret detector. |
| `dns-policies` | Server/zone query-resolution policies, client subnets, recursion scopes, zone-scope discovery and per-scope records | No DNSSEC private keys or every virtualization/policy feature. Processing order and record/TTL values remain configuration. |
| `dhcp-reservation-options` | Per-reservation IPv4/IPv6 option overrides, independently scoped by address | No leases, credentials or DHCP database; support for the installed server/module is required. Option value order is retained. |
| `hyperv-network` | Switch extensions, switch teams, SAN WWPN configuration; per-VM normal/extended NIC ACL and isolation settings | Selected properties only, no packet data, full fabric/SDN configuration or guest network inventory. |
| `hyperv-replication` | Replica server authentication/ports/certificate reference/storage authorization; per-VM primary/replica endpoints, authentication, frequency and recovery history | No keys, replication data, runtime health/last-replication time, failover or replication changes. |

Configuration fields are sorted; meaningful nested ordering is retained. Public keys/certificates and internal host names are still organizational information. Generic names, annotations, DNS records and IIS strings can contain sensitive content; retain the Git secret gate and local review.

## Public certificate files on Windows, Linux and macOS

```sh
python collectors/system/collect_system.py --output /srv/configbackup/staging/fresh \
  --public-certificate /etc/ssl/certs/site-public.pem
```

Requires OpenSSL on PATH. Supports explicit PEM certificate bundles and DER X.509 files, limited to 16 MiB per input. Inputs containing PEM private-key material are rejected before invoking OpenSSL. Only public certificate metadata is persisted, including SHA-256, subject/issuer, serial, validity, selected SAN entries and signature algorithm. PKCS#12/PFX, remote endpoint discovery and automatic renewal are not supported. An explicit input is never treated as permission to walk a directory or read a neighboring key.

Fixed dates stay in configuration. Time-dependent `days_remaining` and `not_yet_valid_count` are written separately to `telemetry/certificate-health.json`; Windows store observations use `telemetry/windows-certificate-health.json`. These files are outside configuration manifests. Add them as monitoring sources to alert on `certificates.certificate.days_remaining` (for example warning below 30 days, critical below 7) and `certificates.certificate.not_yet_valid_count`. Missing/failed collection is not evidence that all certificates are healthy.

## Bounded nginx/Apache configuration inventory

```sh
python collectors/system/collect_system.py --output /srv/configbackup/staging/fresh \
  --web-config nginx=/etc/nginx/nginx.conf \
  --web-config apache=/etc/apache2/sites-enabled/example.conf
```

Each explicitly supplied file has an independent scope. No web-server process is started, config test executed, include opened implicitly, or full config dump retained. The parser preserves block/directive order and collects an allowlist of configuration values such as listeners, server names, roots, TLS certificate/key **paths**, protocols, restrictions, limits and include references. Unknown directive arguments and recognized inline credentials are masked. Add included files explicitly if needed; the output states `includes_followed: false`.

This is a bounded inventory, not an authoritative parser or deployable configuration backup. Embedded languages, nginx braced variable syntax and unsupported/unbalanced formats fail rather than produce silently incomplete success. Comments are excluded. Complex module-specific directives require reviewed parser extensions; key files, `.htpasswd`, environment files and website contents are never followed by this adapter.

## Docker/Podman metadata

```sh
python collectors/system/collect_system.py --output /srv/configbackup/staging/fresh \
  --container-engine docker --container-engine podman
```

Uses the explicitly selected client's existing context/socket; it never creates/starts/stops containers, starts a VM/daemon, pulls images or changes engine state. Reads container, network and volume IDs, then inspects each independently. Configuration captures selected identity/image/user/workdir/ports/resource limits/restart policy/mounts and network/volume names/settings. Environment, command/entrypoint, label and driver-option **values** are omitted; environment/label/option names remain. Runtime state, timestamps, counters and assigned IP/MAC leases are omitted. No container filesystem/image contents, Compose secret files, Swarm/Kubernetes secrets or recovery images are collected. Podman output differences are checked against the same bounded projection; live Podman is unverified.

## Evidence and limits

The targeted Python tests exercise default-off catalogs, malformed discovery, real temporary OpenSSL public certificates and deterministic repeat reads, key-bundle rejection, web parsing/redaction/order, container allowlists, missing clients and failed objects, archive preservation, implied detail selection and core-failure continuation. Portable PowerShell fixtures execute actual reader functions against fake objects/cmdlets: base roles; deeper IIS/MWA traversal; extended Hyper-V; NPS/WEF/DNS/DHCP/DFS/ADCS/certificate/WSUS queries and failure/redaction boundaries.

Two read-only captures of the existing Docker lab produced 12 certified scopes each, no failures and identical certified hashes. Two actual sensitive environment values from those lab containers were checked in memory and absent from output; only the count/result was recorded. No container was changed. The Mac's existing Apache main configuration file was parsed twice with one certified scope per capture, no failures and identical hashes; includes were not followed and no server was executed. **No live Windows role or Windows PowerShell 5.1 was available; property/module/version behavior remains a local acceptance-test requirement.** Mocks do not certify real role coverage. No live Podman, nginx or Apache service was used; web tests parse files without executing a server.

Primary references: [NPS available cmdlets](https://learn.microsoft.com/en-us/powershell/module/nps/), [NPS administration](https://learn.microsoft.com/en-us/windows-server/networking/technologies/nps/nps-admintools), [WEF read commands](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/wecutil), [Hyper-V replication reads](https://learn.microsoft.com/en-us/powershell/module/hyper-v/get-vmreplication), [DNS Server module](https://learn.microsoft.com/en-us/powershell/module/dnsserver/), [DHCP Server module](https://learn.microsoft.com/en-us/powershell/module/dhcpserver/), [IIS configuration elements](https://learn.microsoft.com/en-us/dotnet/api/microsoft.web.administration.configurationelement).
