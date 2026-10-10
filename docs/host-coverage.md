# Host, storage and network coverage in 2.0

Run `python collectors/system/collect_system.py --output /fresh/system-snapshot`. Collection reads the local host. Use one collector on each host; a remote SQL connection does not inventory that remote operating system. Output must be fresh. Configure `collection_manifest: true` on the archive task.

New enrichment probes have independent manifest scopes. Unavailable commands do not certify an empty inventory. Permission errors preserve earlier files. Disappeared discovery objects remain protected until explicitly retired. The older base collector still has a top-level failure boundary: an uncaught core discovery error can stop that invocation before enrichment; an unfinished manifest prevents archive updates.

| Area | Implemented collection | Limits |
|---|---|---|
| Linux storage | lsblk/findmnt, LVM PV/VG/LV/segments and geometry, mdraid membership/roles, per-mount XFS/ext geometry, fstab/crypttab, multipath maps, ZFS properties and JSON vdev topology | Requires each platform tool and permissions. ZFS topology requires compatible `zpool status -j`; older formats fail/preserve. Live nonempty ZFS/multipath not tested. |
| macOS storage | diskutil disk/partition, APFS containers/volumes, CoreStorage groups and AppleRAID sets/member UUIDs | Stable plist whitelist excludes capacity usage and runtime state. CoreStorage listing works here but current macOS cannot create a CoreStorage test set. |
| Storage health | AppleRAID degraded/offline/missing members; Linux mdraid degraded state; raw ZFS status | `--include-performance`. An offline AppleRAID mirror can transition to degraded after its configured timeout. |
| Drives | SMART/NVMe identity and firmware; pass/fail, temperature, wear/spare and supported errors; Windows physical-disk reliability | `--include-drive-health`; smartmontools optional, Windows native reliability where supported. USB bridges/RAID controllers/VMs may hide physical drives. No self-tests or setting changes are performed. |
| Linux firmware/drivers | DMI BIOS/board/chassis, sysfs driver bindings and module/version/parameter information | Not a complete vendor BIOS-setting export or signed driver backup. Some attributes require elevated read access. |
| Kernel settings | Linux boot arguments, selected effective sysctls, module parameters and config files; macOS hardware/firmware, extensions and selected sysctls | Effective Linux keys are an allowlist; add exact `--sysctl-key` values. Secret-like parameter values are redacted. No exhaustive arbitrary kernel-state dump. |
| Windows registry | Selected HKLM/HKCU Run/RunOnce in both registry views, policies, RDP, crash/memory, TLS and service startup settings | `--registry-config` for additional narrow selections; `--skip-registry` to disable. Typed values retained, secret-named values redacted. Credential hives/broad sensitive roots rejected. No live Windows verification. |
| RSoP/GPO | Effective computer/current-user and selected `--rsop-user` reports | `--include-rsop`; uses gpresult XML, narrowly removes report creation time for comparison. It is effective-policy evidence, not a domain GPO/SYSVOL backup. Privileges/logon history affect visibility. |
| SMB/CIFS/NFS | Linux definitions/effective Samba/NFS and mounted clients; Mac SMB exports/NFS definitions/current mounts; Windows shares, share ACLs and client settings | NTFS/filesystem ACL inheritance, remote server internals and every user session are not comprehensively captured. No live Windows or remote share authorization test. |
| Firewall | Windows ActiveStore profiles/rules and filters; Linux nftables/IPv4+IPv6 iptables, ufw/firewalld/config files; Mac application firewall settings/apps and PF config/recursive rules/NAT | Rule/anchor order retained. Packet/byte counters narrowly excluded. Windows filter read errors fail the output instead of silently dropping fields. Active PF requires root on this Mac and was permission-denied; definitions were read. |
| Ports/processes | Linux ss/process/executable paths; Mac TCP listeners/UDP/processes; Windows TCP/UDP and process identity; Mac SMB/NFS client observations | `--include-network-runtime`; telemetry only. Current privileges can hide ownership. Command-line arguments/environment are deliberately not collected by this runtime module. No continuous process-to-job attribution. |

Use [system section switches](../examples/system-sections.yaml), [registry selections](../examples/windows-registry.yaml), [drive selection](../examples/drive-health.yaml) and [monitoring/retention](../examples/monitoring.yaml).

## Image-backed testing

The integration scripts accept fresh output directories, never existing device paths. They validate that newly created image files still own their devices before destructive actions and cleanup.

```sh
# macOS: creates TWO NEW files, builds a mirror, deletes one when --degrade is set.
python tests/integration/macos_storage_lab.py --output /fresh/mac-lab \
  --confirm-image-only-lab --degrade

# Root in a DISPOSABLE Linux VM with loop/LVM/mdraid/filesystem tools:
python tests/integration/linux_storage_lab.py --output /fresh/linux-lab \
  --confirm-disposable-vm

# Only inside an ISOLATED disposable Linux network namespace/container:
python tests/integration/linux_firewall_lab.py --output /fresh/firewall-lab \
  --confirm-isolated-network-namespace
```

The actual Mac test deleted one new member image, observed Offline → Degraded, retained both member UUIDs, mounted the surviving volume, read a proof file and opened a local degraded alert. No external notification was sent. The Linux test exercised two-PV LVM/XFS and a two-member mdraid/ext4 mirror. Both compared repeated stable output and cleaned up their devices. These tests cannot reproduce physical media errors or SMART wear using ordinary disk images.
