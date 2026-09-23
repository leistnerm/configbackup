#!/usr/bin/env python3
"""ConfigBackup guest-system inventory collector.

Creates a deterministic, current-state inventory tree for Windows or Linux.
ConfigBackup is responsible for versioning/history (filesystem, Git, or both).

The collector intentionally avoids collecting secret-bearing environment values,
passwords, browser data, registry credential stores, SSH private keys, etc.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

COLLECTOR_VERSION = "1.3.14"


def eprint(msg: str) -> None:
    print(f"[system-collector] {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"[system-collector] {msg}")


def stable_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def stable_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text, encoding="utf-8")


def stable_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        keys: set[str] = set()
        for row in rows:
            keys.update(str(k) for k in row)
        fieldnames = sorted(keys)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        if fieldnames:
            writer.writeheader()
            for row in rows:
                writer.writerow({k: normalize_scalar(row.get(k)) for k in fieldnames})


def normalize_scalar(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set)):
        return ";".join(str(x) for x in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return value


def run(cmd: list[str], *, timeout: int = 60, check: bool = False) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    if check and cp.returncode != 0:
        raise RuntimeError(f"Command failed ({cp.returncode}): {' '.join(cmd)}\n{cp.stderr.strip()}")
    return cp


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def parse_json_output(text: str) -> Any:
    text = text.strip()
    if not text:
        return []
    return json.loads(text)


def strip_runtime_fields(value: Any, names: set[str]) -> Any:
    """Remove volatile runtime/counter fields from nested command JSON."""
    if isinstance(value, dict):
        return {k: strip_runtime_fields(v, names) for k, v in value.items() if str(k).lower() not in names}
    if isinstance(value, list):
        return [strip_runtime_fields(v, names) for v in value]
    return value


def powershell_executable() -> str:
    for name in ("pwsh", "powershell.exe", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("PowerShell was not found")


def run_ps_json(script: str, *, timeout: int = 120) -> Any:
    ps = powershell_executable()
    wrapped = (
        "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';& { "
        + script
        + " } | ConvertTo-Json -Depth 12 -Compress"
    )
    cp = run([ps, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", wrapped], timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or f"PowerShell failed with {cp.returncode}")
    return parse_json_output(cp.stdout)


def best_effort(label: str, fn, failures: list[dict[str, str]], *, required: bool = False) -> Any:
    try:
        return fn()
    except Exception as exc:
        failures.append({"section": label, "error": str(exc), "required": str(required).lower()})
        eprint(f"{label}: {exc}")
        if required:
            raise
        return None


def linux_os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    p = Path("/etc/os-release")
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            result[k] = v.strip().strip('"')
    return result


def linux_cpu_info() -> dict[str, Any]:
    info_map: dict[str, str] = {}
    p = Path("/proc/cpuinfo")
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info_map.setdefault(k.strip(), v.strip())
    physical_cores = None
    if command_exists("lscpu"):
        cp = run(["lscpu", "-J"])
        if cp.returncode == 0:
            try:
                entries = json.loads(cp.stdout).get("lscpu", [])
                lmap = {str(x.get("field", "")).rstrip(":"): x.get("data") for x in entries}
                sockets = int(lmap.get("Socket(s)", 0) or 0)
                cores_per = int(lmap.get("Core(s) per socket", 0) or 0)
                if sockets and cores_per:
                    physical_cores = sockets * cores_per
            except Exception:
                pass
    return {
        "architecture": platform.machine(),
        "model": info_map.get("model name") or info_map.get("Processor") or platform.processor(),
        "logical_processors": os.cpu_count(),
        "physical_cores": physical_cores,
    }


def linux_memory_info() -> dict[str, Any]:
    mem: dict[str, int] = {}
    p = Path("/proc/meminfo")
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"([^:]+):\s+(\d+)\s+kB", line)
            if m:
                mem[m.group(1)] = int(m.group(2)) * 1024
    return {
        "total_bytes": mem.get("MemTotal"),
        "swap_total_bytes": mem.get("SwapTotal"),
    }


def collect_linux(root: Path, args: argparse.Namespace, failures: list[dict[str, str]]) -> None:
    osrel = linux_os_release()
    stable_json(root / "hardware" / "cpu.json", linux_cpu_info())
    stable_json(root / "hardware" / "memory.json", linux_memory_info())
    stable_json(
        root / "os" / "os.json",
        {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "kernel": platform.release(),
            "kernel_version": platform.version(),
            "architecture": platform.machine(),
            "distribution": osrel,
        },
    )

    if command_exists("lsblk"):
        cp = run(["lsblk", "--json", "--bytes", "--output", "NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,FSVER,LABEL,UUID,PARTUUID,PARTLABEL,PTTYPE,MODEL,SERIAL,WWN,TRAN,ROTA,RO,RM,HOTPLUG,MOUNTPOINTS"], timeout=60)
        if cp.returncode == 0:
            try:
                data = json.loads(cp.stdout)
                stable_json(root / "storage" / "lsblk.json", data)
            except Exception as exc:
                failures.append({"section": "storage.lsblk", "error": str(exc), "required": "false"})
    for src, out in [
        ("/etc/fstab", root / "storage" / "fstab.txt"),
        ("/etc/crypttab", root / "storage" / "crypttab.txt"),
    ]:
        p = Path(src)
        if p.exists():
            stable_text(out, p.read_text(encoding="utf-8", errors="replace"))

    for command, outfile in [
        (["findmnt", "--json", "--bytes", "--all"], "mounts.json"),
        (["pvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-a"], "lvm-pvs.json"),
        (["vgs", "--reportformat", "json", "--units", "b", "--nosuffix", "-a"], "lvm-vgs.json"),
        (["lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-a", "-o", "+devices"], "lvm-lvs.json"),
        (["mdadm", "--detail", "--scan"], "mdraid.conf"),
        (["multipath", "-ll"], "multipath.txt"),
    ]:
        if command_exists(command[0]):
            cp = run(command, timeout=60)
            if cp.returncode == 0:
                if outfile.endswith(".json"):
                    try:
                        stable_json(root / "storage" / outfile, json.loads(cp.stdout))
                    except Exception:
                        stable_text(root / "storage" / outfile.replace(".json", ".txt"), cp.stdout)
                else:
                    stable_text(root / "storage" / outfile, cp.stdout)

    # Network current configuration.
    for command, outfile in [
        (["ip", "-details", "-json", "addr"], "interfaces.json"),
        (["ip", "-json", "route", "show", "table", "all"], "routes.json"),
        (["ip", "-json", "rule", "show"], "rules.json"),
    ]:
        if command_exists(command[0]):
            cp = run(command, timeout=30)
            if cp.returncode == 0:
                try:
                    network_data = json.loads(cp.stdout)
                    network_data = strip_runtime_fields(network_data, {
                        "valid_life_time", "preferred_life_time", "cacheinfo", "stats", "stats64", "expires"
                    })
                    stable_json(root / "network" / outfile, network_data)
                except Exception:
                    stable_text(root / "network" / outfile.replace(".json", ".txt"), cp.stdout)
    resolv = Path("/etc/resolv.conf")
    if resolv.exists():
        stable_text(root / "network" / "resolv.conf", resolv.read_text(encoding="utf-8", errors="replace"))

    if command_exists("timedatectl"):
        cp = run(["timedatectl", "show", "-p", "Timezone", "-p", "LocalRTC", "-p", "CanNTP", "-p", "NTP"], timeout=30)
        if cp.returncode == 0:
            lines = sorted(x for x in cp.stdout.splitlines() if x.strip())
            stable_text(root / "os" / "time-settings.txt", "\n".join(lines))

    exports = Path("/etc/exports")
    if exports.exists():
        stable_text(root / "shares" / "exports.txt", exports.read_text(encoding="utf-8", errors="replace"))
    if command_exists("exportfs"):
        cp = run(["exportfs", "-v"], timeout=30)
        if cp.returncode == 0:
            stable_text(root / "shares" / "nfs-active.txt", cp.stdout)
    smbconf = Path("/etc/samba/smb.conf")
    if smbconf.exists():
        stable_text(root / "shares" / "smb.conf", smbconf.read_text(encoding="utf-8", errors="replace"))

    # Services and timers. Avoid runtime status timestamps; capture definitions/enabled state.
    if command_exists("systemctl"):
        for cmd, out in [
            (["systemctl", "list-unit-files", "--type=service", "--no-pager", "--no-legend"], "services.txt"),
            (["systemctl", "list-unit-files", "--type=timer", "--no-pager", "--no-legend"], "timers.txt"),
        ]:
            cp = run(cmd, timeout=45)
            if cp.returncode == 0:
                lines = sorted(x.strip() for x in cp.stdout.splitlines() if x.strip())
                stable_text(root / "services" / out, "\n".join(lines))

    # Cron definitions are configuration; copy text but never spool/history.
    cron_out = root / "scheduling" / "cron"
    for path in [Path("/etc/crontab"), Path("/etc/anacrontab")]:
        if path.exists() and path.is_file():
            stable_text(cron_out / path.name, path.read_text(encoding="utf-8", errors="replace"))
    for d in [Path("/etc/cron.d")]:
        if d.exists():
            for p in sorted(d.iterdir(), key=lambda x: x.name):
                if p.is_file():
                    stable_text(cron_out / "cron.d" / p.name, p.read_text(encoding="utf-8", errors="replace"))

    # Installed packages and repositories.
    software = root / "software"
    if command_exists("dpkg-query"):
        cp = run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Architecture}\t${Status}\n"], timeout=120)
        if cp.returncode == 0:
            rows = []
            for line in cp.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 4 and "installed" in parts[3]:
                    rows.append({"name": parts[0], "version": parts[1], "architecture": parts[2], "status": parts[3]})
            stable_csv(software / "packages.csv", sorted(rows, key=lambda r: (r["name"], r["architecture"])))
    elif command_exists("rpm"):
        cp = run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n"], timeout=120)
        if cp.returncode == 0:
            rows = []
            for line in cp.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    rows.append({"name": parts[0], "version": parts[1], "architecture": parts[2]})
            stable_csv(software / "packages.csv", sorted(rows, key=lambda r: (r["name"], r["architecture"])))

    if command_exists("snap"):
        cp = run(["snap", "list"], timeout=60)
        if cp.returncode == 0:
            stable_text(software / "snap.txt", cp.stdout)
    if command_exists("flatpak"):
        cp = run(["flatpak", "list", "--columns=application,ref,version,branch,origin"], timeout=60)
        if cp.returncode == 0:
            stable_text(software / "flatpak.txt", cp.stdout)

    repo_files = []
    for pat in ["/etc/apt/sources.list", "/etc/apt/sources.list.d/*.list", "/etc/apt/sources.list.d/*.sources", "/etc/yum.repos.d/*.repo"]:
        import glob as _glob
        repo_files.extend(Path(x) for x in _glob.glob(pat))
    for p in sorted(set(repo_files), key=lambda x: str(x)):
        if p.is_file():
            safe = str(p).lstrip("/").replace("/", "__")
            stable_text(software / "repositories" / safe, p.read_text(encoding="utf-8", errors="replace"))

    if command_exists("lsmod"):
        cp = run(["lsmod"])
        if cp.returncode == 0:
            stable_text(root / "hardware" / "loaded-kernel-modules.txt", cp.stdout)

    # Users/groups without password hashes.
    if not args.skip_accounts:
        import pwd, grp
        users = [
            {"name": u.pw_name, "uid": u.pw_uid, "gid": u.pw_gid, "home": u.pw_dir, "shell": u.pw_shell}
            for u in pwd.getpwall()
        ]
        groups = [
            {"name": g.gr_name, "gid": g.gr_gid, "members": sorted(g.gr_mem)}
            for g in grp.getgrall()
        ]
        stable_csv(root / "accounts" / "users.csv", sorted(users, key=lambda r: (int(r["uid"]), r["name"])))
        stable_json(root / "accounts" / "groups.json", sorted(groups, key=lambda r: (int(r["gid"]), r["name"])))

    if not args.skip_firewall:
        if command_exists("nft"):
            cp = run(["nft", "-j", "list", "ruleset"], timeout=60)
            if cp.returncode == 0:
                try:
                    fw_data = strip_runtime_fields(json.loads(cp.stdout), {"packets", "bytes"})
                    stable_json(root / "network" / "firewall-nftables.json", fw_data)
                except Exception:
                    stable_text(root / "network" / "firewall-nftables.txt", cp.stdout)
        elif command_exists("iptables-save"):
            cp = run(["iptables-save"], timeout=60)
            if cp.returncode == 0:
                stable_text(root / "network" / "firewall-iptables.txt", cp.stdout)



WINDOWS_FIREWALL_CSV_FIELDS = [
    "Summary", "Name", "DisplayName", "Enabled", "Direction", "Action", "Profile",
    "Protocol", "LocalAddress", "LocalPort", "RemoteAddress", "RemotePort", "IcmpType",
    "Program", "Service", "Package", "InterfaceAlias", "InterfaceType",
    "EdgeTraversalPolicy", "Authentication", "Encryption", "OverrideBlockRules",
    "LocalUser", "RemoteUser", "RemoteMachine", "PolicyStoreSource",
    "PolicyStoreSourceType", "Group", "DisplayGroup", "Description", "PrimaryStatus",
]


def _fw_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value if x is not None and str(x) != ""]
    text = str(value)
    return [] if text == "" else [text]


def _fw_display(value: Any, default: str = "Any") -> str:
    values = _fw_values(value)
    return ";".join(values) if values else default


def windows_firewall_rule_summary(rule: dict[str, Any]) -> str:
    direction = _fw_display(rule.get("Direction"), "Any-direction")
    action = _fw_display(rule.get("Action"), "Unknown-action")
    protocol = _fw_display(rule.get("Protocol"), "Any")
    parts = [direction, action, protocol]

    for key, label in (
        ("LocalPort", "local-port"),
        ("RemotePort", "remote-port"),
        ("LocalAddress", "local-address"),
        ("RemoteAddress", "remote-address"),
    ):
        value = _fw_display(rule.get(key))
        if value.lower() != "any":
            parts.append(f"{label}={value}")

    for key, label in (
        ("Program", "program"),
        ("Service", "service"),
        ("InterfaceAlias", "interface"),
        ("InterfaceType", "interface-type"),
    ):
        value = _fw_display(rule.get(key))
        if value.lower() not in {"any", "notapplicable", "none"}:
            parts.append(f"{label}={value}")
    return " ".join(parts)


def flatten_windows_firewall_rule(rule: dict[str, Any]) -> dict[str, Any]:
    flat = {k: rule.get(k) for k in WINDOWS_FIREWALL_CSV_FIELDS if k != "Summary"}
    flat["Summary"] = windows_firewall_rule_summary(rule)
    return flat

def collect_windows(root: Path, args: argparse.Namespace, failures: list[dict[str, str]]) -> None:
    # Hardware / OS
    computer = best_effort("hardware.computer", lambda: run_ps_json(
        "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,Name,Domain,PartOfDomain,SystemType,TotalPhysicalMemory,NumberOfProcessors,NumberOfLogicalProcessors,HypervisorPresent"
    ), failures) or {}
    cpus = best_effort("hardware.cpu", lambda: run_ps_json(
        "Get-CimInstance Win32_Processor | Sort-Object DeviceID | Select-Object DeviceID,Name,Manufacturer,Description,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,SocketDesignation,ProcessorId"
    ), failures) or []
    bios = best_effort("hardware.bios", lambda: run_ps_json(
        "Get-CimInstance Win32_BIOS | Select-Object Manufacturer,Name,SMBIOSBIOSVersion,SerialNumber,ReleaseDate"
    ), failures) or {}
    osinfo = best_effort("os", lambda: run_ps_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,InstallDate,WindowsDirectory,SystemDrive,ProductType"
    ), failures) or {}
    stable_json(root / "hardware" / "computer.json", computer)
    stable_json(root / "hardware" / "cpu.json", cpus)
    stable_json(root / "hardware" / "bios.json", bios)
    stable_json(root / "os" / "os.json", osinfo)
    time_settings = best_effort("os.time", lambda: run_ps_json(
        "[pscustomobject]@{TimeZone=(Get-TimeZone | Select-Object Id,DisplayName,StandardName,DaylightName);Culture=(Get-Culture).Name;UICulture=(Get-UICulture).Name}"
    ), failures)
    if time_settings is not None:
        stable_json(root / "os" / "time-locale.json", time_settings)

    # Storage topology including Storage Spaces when present.
    storage_queries = {
        "disks.json": "Get-Disk | Sort-Object Number | Select-Object Number,FriendlyName,SerialNumber,UniqueId,Path,Location,BusType,PartitionStyle,OperationalStatus,HealthStatus,IsBoot,IsSystem,IsOffline,IsReadOnly,Size,LogicalSectorSize,PhysicalSectorSize",
        "partitions.json": "Get-Partition | Sort-Object DiskNumber,PartitionNumber | Select-Object DiskNumber,PartitionNumber,DriveLetter,AccessPaths,Type,GptType,MbrType,IsActive,IsBoot,IsSystem,Size,Offset",
        "volumes.json": "Get-Volume | Sort-Object DriveLetter,Path | Select-Object DriveLetter,Path,FileSystemLabel,FileSystem,DriveType,HealthStatus,OperationalStatus,Size,UniqueId,AllocationUnitSize",
        "storage-pools.json": "Get-StoragePool | Sort-Object FriendlyName | Select-Object FriendlyName,UniqueId,HealthStatus,OperationalStatus,IsPrimordial,IsReadOnly,Size,AllocatedSize,ResiliencySettingNameDefault,ProvisioningTypeDefault",
        "virtual-disks.json": "Get-VirtualDisk | Sort-Object FriendlyName | Select-Object FriendlyName,UniqueId,HealthStatus,OperationalStatus,ResiliencySettingName,ProvisioningType,NumberOfDataCopies,PhysicalDiskRedundancy,Size,FootprintOnPool,Interleave,NumberOfColumns,WriteCacheSize",
        "physical-disks.json": "Get-PhysicalDisk | Sort-Object FriendlyName,SerialNumber | Select-Object FriendlyName,SerialNumber,UniqueId,MediaType,BusType,CanPool,CannotPoolReason,HealthStatus,OperationalStatus,Usage,Size,AllocatedSize,SpindleSpeed",
    }
    for outfile, script in storage_queries.items():
        data = best_effort("storage." + outfile, lambda s=script: run_ps_json(s), failures)
        if data is not None:
            stable_json(root / "storage" / outfile, data)

    # Drive-letter/mount relationships are often the key recovery detail.
    mountmap = best_effort("storage.mount-map", lambda: run_ps_json(
        "Get-CimInstance Win32_LogicalDisk | Sort-Object DeviceID | Select-Object DeviceID,VolumeName,FileSystem,DriveType,ProviderName,Size,VolumeSerialNumber"
    ), failures)
    if mountmap is not None:
        stable_json(root / "storage" / "drive-map.json", mountmap)

    storage_spaces_map = best_effort("storage.storage-spaces-map", lambda: run_ps_json(
        "Get-StoragePool | Sort-Object FriendlyName | ForEach-Object { $p=$_; [pscustomobject]@{Pool=$p.FriendlyName;PoolUniqueId=$p.UniqueId;PhysicalDisks=@($p | Get-PhysicalDisk -ErrorAction SilentlyContinue | Sort-Object FriendlyName,SerialNumber | ForEach-Object {[pscustomobject]@{FriendlyName=$_.FriendlyName;SerialNumber=$_.SerialNumber;UniqueId=$_.UniqueId;Usage=[string]$_.Usage;Size=$_.Size}});VirtualDisks=@($p | Get-VirtualDisk -ErrorAction SilentlyContinue | Sort-Object FriendlyName | ForEach-Object {[pscustomobject]@{FriendlyName=$_.FriendlyName;UniqueId=$_.UniqueId;Resiliency=[string]$_.ResiliencySettingName;Provisioning=[string]$_.ProvisioningType;Size=$_.Size}})} }"
    ), failures)
    if storage_spaces_map is not None:
        stable_json(root / "storage" / "storage-spaces-map.json", storage_spaces_map)

    virtual_disk_map = best_effort("storage.virtual-disk-map", lambda: run_ps_json(
        "Get-VirtualDisk | Sort-Object FriendlyName | ForEach-Object { $v=$_; [pscustomobject]@{VirtualDisk=$v.FriendlyName;UniqueId=$v.UniqueId;PhysicalDisks=@($v | Get-PhysicalDisk -ErrorAction SilentlyContinue | Sort-Object FriendlyName,SerialNumber | ForEach-Object {[pscustomobject]@{FriendlyName=$_.FriendlyName;SerialNumber=$_.SerialNumber;UniqueId=$_.UniqueId}});Disks=@($v | Get-Disk -ErrorAction SilentlyContinue | Sort-Object Number | ForEach-Object {[pscustomobject]@{Number=$_.Number;FriendlyName=$_.FriendlyName;UniqueId=$_.UniqueId;Path=$_.Path;Location=$_.Location}})} }"
    ), failures)
    if virtual_disk_map is not None:
        stable_json(root / "storage" / "virtual-disk-map.json", virtual_disk_map)

    # Network.
    network_queries = {
        "adapters.json": "Get-NetAdapter | Sort-Object ifIndex | Select-Object ifIndex,Name,InterfaceDescription,MacAddress,Status,LinkSpeed,MediaType,PhysicalMediaType,Virtual",
        "ip-configuration.json": "Get-NetIPConfiguration -All | Sort-Object InterfaceIndex | Select-Object InterfaceIndex,InterfaceAlias,NetProfile,@{n='IPv4Address';e={@($_.IPv4Address.IPAddress)}},@{n='IPv6Address';e={@($_.IPv6Address.IPAddress)}},@{n='IPv4DefaultGateway';e={@($_.IPv4DefaultGateway.NextHop)}},@{n='IPv6DefaultGateway';e={@($_.IPv6DefaultGateway.NextHop)}},@{n='DNSServer';e={@($_.DNSServer.ServerAddresses)}}",
        "routes.json": "Get-NetRoute | Sort-Object AddressFamily,DestinationPrefix,RouteMetric,ifIndex | Select-Object AddressFamily,DestinationPrefix,NextHop,RouteMetric,ifIndex,InterfaceAlias,PolicyStore,Protocol",
        "dns-client.json": "Get-DnsClientServerAddress | Sort-Object InterfaceIndex,AddressFamily | Select-Object InterfaceAlias,InterfaceIndex,AddressFamily,ServerAddresses",
        "ip-interfaces.json": "Get-NetIPInterface | Sort-Object InterfaceIndex,AddressFamily | Select-Object InterfaceAlias,InterfaceIndex,AddressFamily,Dhcp,ConnectionState,NlMtu,InterfaceMetric,AutomaticMetric,Forwarding,WeakHostSend,WeakHostReceive",
        "dns-client-settings.json": "Get-DnsClient | Sort-Object InterfaceIndex | Select-Object InterfaceAlias,InterfaceIndex,ConnectionSpecificSuffix,RegisterThisConnectionsAddress,UseSuffixWhenRegistering",
    }
    for outfile, script in network_queries.items():
        data = best_effort("network." + outfile, lambda s=script: run_ps_json(s), failures)
        if data is not None:
            stable_json(root / "network" / outfile, data)

    smb_shares = best_effort("shares.smb", lambda: run_ps_json(
        "if (Get-Command Get-SmbShare -ErrorAction SilentlyContinue) { Get-SmbShare | Sort-Object Name | ForEach-Object { $s=$_; [pscustomobject]@{Name=$s.Name;Path=$s.Path;Description=$s.Description;ScopeName=$s.ScopeName;Special=$s.Special;Temporary=$s.Temporary;FolderEnumerationMode=[string]$s.FolderEnumerationMode;CachingMode=[string]$s.CachingMode;ContinuouslyAvailable=$s.ContinuouslyAvailable;Access=@(Get-SmbShareAccess -Name $s.Name -ErrorAction SilentlyContinue | Sort-Object AccountName,AccessRight | Select-Object AccountName,AccessControlType,AccessRight)} } }"
    ), failures)
    if smb_shares is not None:
        stable_json(root / "shares" / "smb-shares.json", smb_shares)

    nfs_shares = best_effort("shares.nfs", lambda: run_ps_json(
        "if (Get-Command Get-NfsShare -ErrorAction SilentlyContinue) { Get-NfsShare | Sort-Object Name | Select-Object Name,Path,NetworkName,Authentication,EnableAnonymousAccess,AnonymousUid,AnonymousGid,Permission,AllowRootAccess }"
    ), failures)
    if nfs_shares is not None:
        stable_json(root / "shares" / "nfs-shares.json", nfs_shares)

    # Services / scheduled tasks.
    services = best_effort("services", lambda: run_ps_json(
        "Get-CimInstance Win32_Service | Sort-Object Name | Select-Object Name,DisplayName,StartMode,StartName,PathName,ServiceType,DelayedAutoStart"
    ), failures)
    if services is not None:
        stable_json(root / "services" / "services.json", services)

    tasks = best_effort("scheduled-tasks", lambda: run_ps_json(
        "Get-ScheduledTask | Sort-Object TaskPath,TaskName | ForEach-Object { $t=$_; [pscustomobject]@{TaskPath=$t.TaskPath;TaskName=$t.TaskName;Author=$t.Author;Description=$t.Description;URI=$t.URI;Actions=@($t.Actions|ForEach-Object{[pscustomobject]@{Execute=$_.Execute;Arguments=$_.Arguments;WorkingDirectory=$_.WorkingDirectory;ClassId=$_.ClassId}});Triggers=@($t.Triggers|ForEach-Object{[pscustomobject]@{Enabled=$_.Enabled;StartBoundary=$_.StartBoundary;EndBoundary=$_.EndBoundary;ExecutionTimeLimit=$_.ExecutionTimeLimit;RandomDelay=$_.RandomDelay;Repetition=$_.Repetition;CimClass=$_.CimClass.CimClassName}});Principal=[pscustomobject]@{UserId=$t.Principal.UserId;GroupId=$t.Principal.GroupId;LogonType=[string]$t.Principal.LogonType;RunLevel=[string]$t.Principal.RunLevel};Settings=[pscustomobject]@{Enabled=$t.Settings.Enabled;Hidden=$t.Settings.Hidden;AllowDemandStart=$t.Settings.AllowDemandStart;StartWhenAvailable=$t.Settings.StartWhenAvailable;RunOnlyIfNetworkAvailable=$t.Settings.RunOnlyIfNetworkAvailable;WakeToRun=$t.Settings.WakeToRun;ExecutionTimeLimit=$t.Settings.ExecutionTimeLimit}} }"
    ), failures, required=False)
    if tasks is not None:
        stable_json(root / "scheduling" / "scheduled-tasks.json", tasks)

    # Installed software from registry (Win32_Product intentionally avoided).
    software = best_effort("software.installed", lambda: run_ps_json(
        "$paths=@('HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*','HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*');"
        "Get-ItemProperty $paths -ErrorAction SilentlyContinue | Where-Object {$_.DisplayName} | Select-Object DisplayName,DisplayVersion,Publisher,InstallDate,InstallLocation,WindowsInstaller,SystemComponent | Sort-Object DisplayName,DisplayVersion,Publisher"
    ), failures)
    if software is not None:
        stable_json(root / "software" / "installed-software.json", software)

    hotfixes = best_effort("patches.hotfixes", lambda: run_ps_json(
        "Get-HotFix | Sort-Object HotFixID | Select-Object HotFixID,Description,InstalledBy,InstalledOn"
    ), failures)
    if hotfixes is not None:
        stable_json(root / "patches" / "hotfixes.json", hotfixes)

    packages = best_effort("patches.windows-packages", lambda: run_ps_json(
        "Get-WindowsPackage -Online -ErrorAction Stop | Where-Object {$_.PackageState -eq 'Installed'} | Sort-Object PackageName | Select-Object PackageName,PackageState,ReleaseType,InstallTime"
    ), failures)
    if packages is not None:
        stable_json(root / "patches" / "windows-packages.json", packages)

    features = best_effort("software.windows-features", lambda: run_ps_json(
        "if (Get-Command Get-WindowsFeature -ErrorAction SilentlyContinue) { Get-WindowsFeature | Where-Object {$_.Installed} | Sort-Object Name | Select-Object Name,DisplayName,FeatureType,InstallState } else { Get-WindowsOptionalFeature -Online | Where-Object {$_.State -eq 'Enabled'} | Sort-Object FeatureName | Select-Object FeatureName,State }"
    ), failures)
    if features is not None:
        stable_json(root / "software" / "windows-features.json", features)

    modules = best_effort("software.powershell-modules", lambda: run_ps_json(
        "Get-Module -ListAvailable | Group-Object Name | ForEach-Object { $_.Group | Sort-Object Version -Descending | Select-Object -First 1 Name,Version,ModuleBase } | Sort-Object Name"
    ), failures)
    if modules is not None:
        stable_json(root / "software" / "powershell-modules.json", modules)

    drivers = best_effort("software.drivers", lambda: run_ps_json(
        "Get-CimInstance Win32_PnPSignedDriver | Where-Object {$_.DeviceName} | Sort-Object DeviceName,DriverVersion | Select-Object DeviceName,DeviceClass,Manufacturer,DriverProviderName,DriverVersion,DriverDate,InfName,IsSigned,Signer"
    , timeout=180), failures)
    if drivers is not None:
        stable_json(root / "software" / "drivers.json", drivers)

    # .NET inventory is lightweight and useful for rebuilds.
    dotnet = shutil.which("dotnet")
    if dotnet:
        for flag, name in [("--list-runtimes", "dotnet-runtimes.txt"), ("--list-sdks", "dotnet-sdks.txt")]:
            cp = run([dotnet, flag], timeout=60)
            if cp.returncode == 0:
                stable_text(root / "software" / name, "\n".join(sorted(x for x in cp.stdout.splitlines() if x.strip())))

    if not args.skip_accounts:
        users = best_effort("accounts.users", lambda: run_ps_json(
            "Get-LocalUser | Sort-Object Name | Select-Object Name,Enabled,Description,PasswordRequired,PasswordExpires,UserMayChangePassword,SID,PrincipalSource"
        ), failures)
        groups = best_effort("accounts.groups", lambda: run_ps_json(
            "Get-LocalGroup | Sort-Object Name | ForEach-Object { $g=$_; [pscustomobject]@{Name=$g.Name;Description=$g.Description;SID=[string]$g.SID;Members=@(Get-LocalGroupMember -Group $g.Name -ErrorAction SilentlyContinue | ForEach-Object {[pscustomobject]@{Name=$_.Name;ObjectClass=$_.ObjectClass;PrincipalSource=[string]$_.PrincipalSource;SID=[string]$_.SID}})} }"
        ), failures)
        if users is not None:
            stable_json(root / "accounts" / "local-users.json", users)
        if groups is not None:
            stable_json(root / "accounts" / "local-groups.json", groups)

    if not args.skip_firewall:
        fw_profiles = best_effort("network.firewall-profiles", lambda: run_ps_json(
            "Get-NetFirewallProfile -PolicyStore ActiveStore | Sort-Object Name | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction,AllowInboundRules,AllowLocalFirewallRules,NotifyOnListen,LogFileName,LogMaxSizeKilobytes,LogAllowed,LogBlocked"
        ), failures)
        firewall_script = r'''$rules = @(Get-NetFirewallRule -PolicyStore ActiveStore | Sort-Object DisplayName,Name)
foreach ($r in $rules) {
    $address = $r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
    $port = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
    $application = $r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
    $service = $r | Get-NetFirewallServiceFilter -ErrorAction SilentlyContinue
    $interface = $r | Get-NetFirewallInterfaceFilter -ErrorAction SilentlyContinue
    $interfaceType = $r | Get-NetFirewallInterfaceTypeFilter -ErrorAction SilentlyContinue
    $security = $r | Get-NetFirewallSecurityFilter -ErrorAction SilentlyContinue

    [pscustomobject]@{
        Name = $r.Name
        DisplayName = $r.DisplayName
        Description = $r.Description
        DisplayGroup = $r.DisplayGroup
        Group = $r.Group
        Enabled = [string]$r.Enabled
        Profile = [string]$r.Profile
        Direction = [string]$r.Direction
        Action = [string]$r.Action
        EdgeTraversalPolicy = [string]$r.EdgeTraversalPolicy
        LooseSourceMapping = $r.LooseSourceMapping
        LocalOnlyMapping = $r.LocalOnlyMapping
        Owner = $r.Owner
        PolicyStoreSource = $r.PolicyStoreSource
        PolicyStoreSourceType = [string]$r.PolicyStoreSourceType
        PrimaryStatus = [string]$r.PrimaryStatus
        Status = [string]$r.Status
        StatusCode = $r.StatusCode
        LocalAddress = @($address.LocalAddress)
        RemoteAddress = @($address.RemoteAddress)
        Protocol = [string]$port.Protocol
        LocalPort = @($port.LocalPort)
        RemotePort = @($port.RemotePort)
        IcmpType = @($port.IcmpType)
        DynamicTransport = [string]$port.DynamicTransport
        Program = [string]$application.Program
        Package = [string]$application.Package
        Service = [string]$service.Service
        InterfaceAlias = @($interface.InterfaceAlias)
        InterfaceType = @($interfaceType.InterfaceType)
        Authentication = [string]$security.Authentication
        Encryption = [string]$security.Encryption
        OverrideBlockRules = [string]$security.OverrideBlockRules
        LocalUser = [string]$security.LocalUser
        RemoteUser = [string]$security.RemoteUser
        RemoteMachine = [string]$security.RemoteMachine
    }
}
'''
        fw_rules = best_effort("network.firewall-rules", lambda: run_ps_json(firewall_script, timeout=300), failures)
        if fw_profiles is not None:
            stable_json(root / "network" / "firewall-profiles.json", fw_profiles)
        if fw_rules is not None:
            if isinstance(fw_rules, dict):
                fw_rules = [fw_rules]
            stable_json(root / "network" / "firewall-rules.json", fw_rules)
            flattened_rules = [flatten_windows_firewall_rule(x) for x in fw_rules]
            stable_csv(root / "network" / "firewall-rules.csv", flattened_rules, WINDOWS_FIREWALL_CSV_FIELDS)

    # Paging / boot configuration.
    paging = best_effort("os.pagefile", lambda: run_ps_json(
        "Get-CimInstance Win32_PageFileSetting | Sort-Object Name | Select-Object Name,InitialSize,MaximumSize"
    ), failures)
    if paging is not None:
        stable_json(root / "os" / "pagefile.json", paging)
    powercfg = shutil.which("powercfg.exe") or shutil.which("powercfg")
    if powercfg:
        cp = run([powercfg, "/getactivescheme"], timeout=30)
        if cp.returncode == 0:
            stable_text(root / "os" / "active-power-scheme.txt", cp.stdout)

    bcdedit = shutil.which("bcdedit.exe") or shutil.which("bcdedit")
    if bcdedit:
        cp = run([bcdedit, "/enum", "{current}"], timeout=30)
        if cp.returncode == 0:
            stable_text(root / "os" / "boot-current.txt", cp.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Windows/Linux guest configuration and inventory")
    parser.add_argument("--output", default=os.environ.get("CONFIGBACKUP_OUTPUT"), help="Output directory (defaults to CONFIGBACKUP_OUTPUT)")
    parser.add_argument("--skip-firewall", action="store_true", help="Skip firewall rule/profile collection")
    parser.add_argument("--skip-accounts", action="store_true", help="Skip local users/groups inventory")
    parser.add_argument("--strict", action="store_true", help="Fail if any best-effort section cannot be collected")
    args = parser.parse_args()
    if not args.output:
        parser.error("--output is required unless CONFIGBACKUP_OUTPUT is set")
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, str]] = []

    stable_json(root / "collector.json", {
        "collector": "collect_system.py",
        "collector_version": COLLECTOR_VERSION,
        "python": platform.python_version(),
        "platform": sys.platform,
        "secrets_policy": "environment values/password material are not intentionally collected",
    })

    try:
        if os.name == "nt":
            info("Collecting Windows guest configuration")
            collect_windows(root, args, failures)
        elif sys.platform.startswith("linux"):
            info("Collecting Linux guest configuration")
            collect_linux(root, args, failures)
        else:
            raise RuntimeError(f"Unsupported platform: {sys.platform}")
    except Exception as exc:
        failures.append({"section": "collector", "error": str(exc), "required": "true"})
        eprint(str(exc))
        stable_json(root / "collection-errors.json", failures)
        return 1

    stable_json(root / "collection-errors.json", failures)
    if failures:
        info(f"Completed with {len(failures)} best-effort collection warning(s)")
        if args.strict:
            return 2
    else:
        info("Collection completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
