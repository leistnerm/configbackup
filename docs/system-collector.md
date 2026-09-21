# Windows/Linux guest-system collector

`collectors/system/collect_system.py` creates a current-state inventory of a Windows or Linux guest. It is designed for VMs and physical systems where you want to preserve enough machine configuration to understand or rebuild the guest later.

The collector uses only Python's standard library plus native operating-system tools. ConfigBackup itself remains the history engine.

## What it collects

### Both platforms

- Host/OS identity and architecture.
- CPU topology/count information.
- Memory allocation/total memory information.
- Disk, partition, volume, filesystem, and mount layout, including drive-letter/access-path relationships.
- Network interfaces, IP configuration, DHCP/static state, routes, DNS configuration, and interface metrics.
- Service configuration/startup state.
- Scheduled-task/timer/cron configuration.
- Installed software/packages and versions.
- Patch/update inventory where the OS exposes it.
- Local users/groups without password hashes.
- Firewall configuration where available and permitted.
- SMB/NFS share definitions where the relevant subsystem is available.
- Time-zone/locale or time-service configuration where available.

The collector intentionally does **not** dump process environment values, credential stores, password databases, SSH private keys, browser data, or other obvious secret stores. However, ordinary configuration can itself contain secrets (for example a scheduled-task argument, service command line, repository URL, or Samba configuration). Treat the resulting inventory as sensitive and review it before publishing it to any Git remote.

## Windows-specific inventory

Windows collection includes best-effort snapshots of:

- `Win32_ComputerSystem`, CPU, BIOS, OS/build information.
- `Get-Disk`, `Get-Partition`, `Get-Volume`.
- Storage Spaces: `Get-StoragePool`, `Get-VirtualDisk`, `Get-PhysicalDisk`, plus pool→physical-disk, pool→virtual-disk, and virtual-disk→guest-disk relationship maps.
- Drive-letter/provider mapping.
- NICs, IP configuration, DHCP/static interface state, routes, DNS servers and client suffix/registration settings.
- Windows services and service accounts/start modes.
- Task Scheduler definitions including actions, arguments, triggers, principals, and relevant settings.
- Installed software from uninstall registry keys (it intentionally avoids `Win32_Product`).
- `Get-HotFix` inventory.
- Installed Windows packages where `Get-WindowsPackage` is available/authorized.
- Installed Windows Server roles or optional features.
- PowerShell modules.
- Signed Plug-and-Play driver inventory.
- Installed .NET runtimes/SDKs when `dotnet` is available.
- Local users/groups and memberships.
- Windows Firewall profiles and rules.
- SMB shares and share ACLs; NFS shares when the NFS cmdlets are installed.
- Time zone/culture and active power scheme.
- Pagefile and current boot configuration.

Many Windows storage/firewall/package commands expose more data when run elevated. Missing optional data is recorded in `collection-errors.json` rather than making the normal collector fail.

## Linux-specific inventory

Linux collection includes best-effort snapshots of:

- `/etc/os-release`, kernel, CPU, and `/proc/meminfo` data.
- `lsblk` JSON inventory using a stable selected field set (rather than volatile/all fields).
- `/etc/fstab` and `/etc/crypttab` when present.
- `findmnt` mount topology.
- LVM (`pvs`, `vgs`, `lvs`) when installed.
- mdraid (`mdadm --detail --scan`) when installed.
- multipath configuration/status when the command is available.
- `ip` interface/route/rule state.
- `/etc/resolv.conf`.
- Stable `timedatectl` properties such as time zone, local-RTC, and NTP capability when available.
- `/etc/exports`/`exportfs -v` and Samba `smb.conf` when present.
- systemd service/timer unit-file enablement.
- `/etc/crontab`, `/etc/anacrontab`, and `/etc/cron.d` definitions.
- dpkg/DEB or RPM package inventory.
- Snap/Flatpak inventory when present.
- APT/YUM repository definition files.
- Loaded kernel modules.
- Local users/groups without `/etc/shadow` data.
- nftables or iptables rules when accessible.

## Output layout

Typical output:

```text
system/
├── collector.json
├── collection-errors.json
├── hardware/
│   ├── cpu.json
│   ├── memory.json
│   └── ...
├── os/
├── storage/
│   ├── disks.json                 # Windows
│   ├── partitions.json            # Windows
│   ├── volumes.json               # Windows
│   ├── storage-pools.json         # Windows Storage Spaces
│   ├── virtual-disks.json         # Windows Storage Spaces
│   ├── physical-disks.json        # Windows Storage Spaces
│   ├── lsblk.json                 # Linux
│   ├── mounts.json                # Linux
│   └── fstab.txt                  # Linux
├── network/
├── shares/
├── services/
├── scheduling/
├── software/
├── patches/
└── accounts/
```

Files are deliberately split by concern. A software update should change the package/patch artifact without forcing unrelated disk-layout or network artifacts to change.

## Usage

Direct test:

```bash
python3 collectors/system/collect_system.py --output /tmp/system-snapshot
```

Windows:

```powershell
py -3 .\collectors\system\collect_system.py --output C:\Temp\system-snapshot
```

Optional switches:

```text
--skip-firewall
--skip-accounts
--strict
```

Normal mode treats optional subsystem failures as warnings and records them in `collection-errors.json`. `--strict` returns a nonzero exit code if any best-effort section fails.

## ConfigBackup integration

```yaml
- name: collect-system
  type: execute
  phase: pre_backup
  executable: python3
  arguments:
    - /opt/configbackup/collectors/system/collect_system.py
  output_directory: ${CONFIGBACKUP_STAGING}/system
  clean_output: true
  required: true

- name: archive-system
  type: directory
  source: ${CONFIGBACKUP_STAGING}/system
  destination: system
  depends_on: [collect-system]
  retention_policy: system-inventory
```

For Git history instead of dated files:

```yaml
- name: archive-system
  type: directory
  source: ${CONFIGBACKUP_STAGING}/system
  destination: system
  depends_on: [collect-system]
  storage: git
```

## Security considerations

System inventories can still be sensitive even without passwords. They can reveal machine names, network topology, service-account names, scheduled commands, installed software, firewall rules, and storage paths. Protect filesystem archives and Git repositories accordingly; use a private remote when pushing to a hosted Git provider.
