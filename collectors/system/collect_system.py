#!/usr/bin/env python3
"""ConfigBackup guest-system inventory collector.

Creates a deterministic, current-state inventory tree for Windows, Linux or macOS.
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
import plistlib
import hashlib
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from completeness import publish, section
from sections import enabled as section_enabled

COLLECTOR_VERSION = "2.0.0"


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
    rows.sort(key=lambda row: tuple(str(normalize_scalar(row.get(k))) for k in fieldnames))
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


def collect_macos(root: Path, args: argparse.Namespace, failures: list[dict[str, str]]) -> None:
    """Read stable host settings and launchd definitions; never query browser/keychain data."""
    stable_json(root/'os/os.json', {'hostname':socket.gethostname(), 'architecture':platform.machine(),
                                  'kernel':platform.release(), 'macos':platform.mac_ver()[0]})
    commands = {
        'hardware/settings.txt':['/usr/sbin/sysctl','hw.model','hw.ncpu','hw.memsize','hw.physicalcpu','hw.logicalcpu'],
        'os/version.txt':['/usr/bin/sw_vers'],
        'network/hardware-ports.txt':['/usr/sbin/networksetup','-listallhardwareports'],
        'network/service-order.txt':['/usr/sbin/networksetup','-listnetworkserviceorder'],
        'software/packages.txt':['/usr/sbin/pkgutil','--pkgs'],
    }
    if not args.skip_accounts:
        commands.update({'security/users.txt':['/usr/bin/dscl','.','-list','/Users','UniqueID'],
                         'security/groups.txt':['/usr/bin/dscl','.','-list','/Groups','PrimaryGroupID']})
    for name, command in commands.items():
        try:
            result=run(command, check=True)
            text=result.stdout
            if name in ('software/packages.txt','security/users.txt','security/groups.txt'):
                text='\n'.join(sorted(text.splitlines()))+'\n'
            stable_text(root/name,text)
        except Exception as exc:failures.append({'section':name,'error':str(exc),'required':'false'})
    directories=[Path('/System/Library/LaunchDaemons'),Path('/System/Library/LaunchAgents'),
                 Path('/Library/LaunchDaemons'),Path('/Library/LaunchAgents'),Path.home()/'Library/LaunchAgents']
    for directory in directories:
        if not directory.exists():continue
        try:files=sorted(directory.glob('*.plist'))
        except OSError as exc:
            failures.append({'section':str(directory),'error':str(exc),'required':'false'});continue
        for source in files:
            try:
                try:
                    with source.open('rb') as stream:definition=plistlib.load(stream)
                except Exception:
                    # Apple's parser accepts some shipped plists with malformed XML headers.
                    # Convert a read-only stream; never rewrite the source file.
                    converted=run(['/usr/bin/plutil','-convert','xml1','-o','-',str(source)],check=True)
                    definition=plistlib.loads(converted.stdout.encode('utf-8'))
                if not isinstance(definition,dict):raise ValueError('Expected plist dictionary')
                if 'EnvironmentVariables' in definition:
                    definition['EnvironmentVariables']={k:'<REDACTED>' for k in definition['EnvironmentVariables']}
                key=hashlib.sha256(str(source).encode()).hexdigest()
                stable_json(root/'scheduling/launchd'/(key+'.json'),{'path':str(source),'definition':definition})
            except Exception as exc:failures.append({'section':str(source),'error':str(exc),'required':'false'})
    # A user crontab is separate from launchd. Exit 1 with 'no crontab' means empty.
    try:
        result=run(['/usr/bin/crontab','-l'])
        if result.returncode==0:stable_text(root/'scheduling/crontabs/current-user.txt',result.stdout)
        elif 'no crontab' not in result.stderr.lower():raise RuntimeError(result.stderr)
    except Exception as exc:failures.append({'section':'crontab','error':str(exc),'required':'false'})


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

    for name in ('fstab','crypttab'):
        source=Path('/etc')/name
        if source.exists() and section_enabled('storage/etc'):
            try: stable_text(root/'storage/etc'/(name+'.txt'),source.read_text())
            except OSError as exc: failures.append({'section':'storage/etc/'+name,'error':str(exc)})

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

    # Capture systemd timer definitions, excluding volatile next-elapse timestamps.
    if command_exists("systemctl"):
        cp = run(["systemctl", "list-unit-files", "--type=timer", "--no-pager", "--no-legend"], timeout=60)
        if cp.returncode == 0:
            timers = []
            for line in cp.stdout.splitlines():
                parts = line.split()
                if len(parts) < 2 or not parts[0].endswith(".timer"):
                    continue
                name, state = parts[:2]
                content = run(["systemctl", "cat", name, "--no-pager"], timeout=15)
                if content.returncode != 0:
                    continue
                calendar_specs = []
                monotonic_specs = []
                random_delay = ""
                for text in content.stdout.splitlines():
                    text = text.strip()
                    if text.startswith("OnCalendar="):
                        calendar_specs.append(text.partition("=")[2])
                    if text.startswith(("OnBootSec=", "OnUnitActiveSec=", "OnUnitInactiveSec=", "OnStartupSec=")):
                        monotonic_specs.append(text)
                    if text.startswith("RandomizedDelaySec="):
                        random_delay = text.partition("=")[2]
                timers.append({"unit": name, "unit_state": state,
                               "on_calendar": ";".join(sorted(calendar_specs)),
                               "monotonic": ";".join(sorted(monotonic_specs)),
                               "randomized_delay": random_delay, "service": name.removesuffix(".timer")+".service", "definition": content.stdout})
            stable_csv(root / "scheduling" / "systemd-timers.csv", timers)

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

    if command_exists('crontab'):
        import pwd
        accounts = pwd.getpwall() if os.geteuid() == 0 else [pwd.getpwuid(os.geteuid())]
        for account in accounts:
            cmd=['crontab','-u',account.pw_name,'-l'] if os.geteuid()==0 else ['crontab','-l']
            cp=run(cmd,timeout=30)
            if cp.returncode==0:
                stable_text(cron_out/'users'/(account.pw_name+'.cron'),cp.stdout)
            elif 'no crontab' not in cp.stderr.lower():
                failures.append({'section':'scheduling.cron','error':'Cannot read crontab for '+account.pw_name})
        if os.geteuid()!=0:
            failures.append({'section':'scheduling.cron','error':'Other users crontabs not accessible without root'})
    if command_exists('systemctl'):
        cp=run(['systemctl','--user','list-timers','--all','--no-pager'],timeout=30)
        if cp.returncode==0:
            stable_text(root/'telemetry'/'user-timer-status.txt',cp.stdout)
        for base in [Path('/etc/systemd/user'),Path.home()/'.config/systemd/user']:
            if base.is_dir():
                for timer in sorted(base.glob('*.timer')):
                    stable_text(root/'scheduling'/'user-timers'/timer.name,timer.read_text())

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
        "disks.json": "Get-Disk | Sort-Object Number | Select-Object Number,FriendlyName,SerialNumber,UniqueId,Path,Location,BusType,PartitionStyle,IsBoot,IsSystem,IsOffline,IsReadOnly,Size,LogicalSectorSize,PhysicalSectorSize",
        "partitions.json": "Get-Partition | Sort-Object DiskNumber,PartitionNumber | Select-Object DiskNumber,PartitionNumber,DriveLetter,AccessPaths,Type,GptType,MbrType,IsActive,IsBoot,IsSystem,Size,Offset",
        "volumes.json": "Get-Volume | Sort-Object DriveLetter,Path | Select-Object DriveLetter,Path,FileSystemLabel,FileSystem,DriveType,Size,UniqueId,AllocationUnitSize",
        "storage-pools.json": "Get-StoragePool | Sort-Object FriendlyName | Select-Object FriendlyName,UniqueId,IsPrimordial,IsReadOnly,Size,ResiliencySettingNameDefault,ProvisioningTypeDefault",
        "virtual-disks.json": "Get-VirtualDisk | Sort-Object FriendlyName | Select-Object FriendlyName,UniqueId,ResiliencySettingName,ProvisioningType,NumberOfDataCopies,PhysicalDiskRedundancy,Size,Interleave,NumberOfColumns,WriteCacheSize",
        "physical-disks.json": "Get-PhysicalDisk | Sort-Object FriendlyName,SerialNumber | Select-Object FriendlyName,SerialNumber,UniqueId,MediaType,BusType,CanPool,CannotPoolReason,Usage,Size,SpindleSpeed",
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


    # Services / scheduled tasks.
    services = best_effort("services", lambda: run_ps_json(
        "Get-CimInstance Win32_Service | Sort-Object Name | Select-Object Name,DisplayName,StartMode,StartName,PathName,ServiceType,DelayedAutoStart"
    ), failures)
    if services is not None:
        stable_json(root / "services" / "services.json", services)

    tasks = best_effort("scheduled-tasks", lambda: run_ps_json(
        "Get-ScheduledTask | Sort-Object TaskPath,TaskName | ForEach-Object { $t=$_; [pscustomobject]@{TaskPath=$t.TaskPath;TaskName=$t.TaskName;Author=$t.Author;Description=$t.Description;URI=$t.URI;Actions=@($t.Actions|ForEach-Object{[pscustomobject]@{Execute=$_.Execute;Arguments=$_.Arguments;WorkingDirectory=$_.WorkingDirectory;ClassId=$_.ClassId}});Triggers=@($t.Triggers|ForEach-Object{[pscustomobject]@{Enabled=$_.Enabled;StartBoundary=$_.StartBoundary;EndBoundary=$_.EndBoundary;ExecutionTimeLimit=$_.ExecutionTimeLimit;RandomDelay=$_.RandomDelay;Repetition=[pscustomobject]@{Interval=[string]$_.Repetition.Interval;Duration=[string]$_.Repetition.Duration;StopAtDurationEnd=$_.Repetition.StopAtDurationEnd};DaysInterval=$_.DaysInterval;WeeksInterval=$_.WeeksInterval;DaysOfWeek=$_.DaysOfWeek;DaysOfMonth=$_.DaysOfMonth;WeeksOfMonth=$_.WeeksOfMonth;MonthsOfYear=$_.MonthsOfYear;RunOnLastDayOfMonth=$_.RunOnLastDayOfMonth;RunOnLastWeekOfMonth=$_.RunOnLastWeekOfMonth;CimClass=$_.CimClass.CimClassName}});Principal=[pscustomobject]@{UserId=$t.Principal.UserId;GroupId=$t.Principal.GroupId;LogonType=[string]$t.Principal.LogonType;RunLevel=[string]$t.Principal.RunLevel};Settings=[pscustomobject]@{Enabled=$t.Settings.Enabled;Hidden=$t.Settings.Hidden;AllowDemandStart=$t.Settings.AllowDemandStart;StartWhenAvailable=$t.Settings.StartWhenAvailable;RunOnlyIfNetworkAvailable=$t.Settings.RunOnlyIfNetworkAvailable;WakeToRun=$t.Settings.WakeToRun;ExecutionTimeLimit=$t.Settings.ExecutionTimeLimit}} }"
    ), failures, required=False)
    if tasks is not None:
        stable_json(root / "scheduling" / "scheduled-tasks.json", tasks)

    task_xml = best_effort("scheduled-task-xml", lambda: run_ps_json(
        "Get-ScheduledTask | Sort-Object TaskPath,TaskName | ForEach-Object { [pscustomobject]@{TaskPath=$_.TaskPath;TaskName=$_.TaskName;Xml=(Export-ScheduledTask -TaskName $_.TaskName -TaskPath $_.TaskPath -ErrorAction Stop)} }"
    ), failures)
    if task_xml is not None:
        stable_json(root / 'scheduling' / 'task-xml.json', task_xml)

    # Optional historical runtimes from Task Scheduler Operational log events
    # 100 (started) and 102 (completed). Only completed matched instances count.
    # Store separately from stable config inventory: this rolls each day.
    if args.include_task_history:
        event_status = best_effort('scheduled-task-status-events', lambda: run_ps_json(
            "Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational';Id=101,103,107,111,118,119,140,141,142,203;StartTime=(Get-Date).AddDays(-" + str(args.task_history_days) + ")} -MaxEvents 50000 -ErrorAction Stop | ForEach-Object { [pscustomobject]@{Id=$_.Id;TimeCreated=$_.TimeCreated.ToString('o');Xml=$_.ToXml()} }"
        ), failures)
        if event_status is not None:
            stable_json(root / 'telemetry' / 'task-status-events.json', event_status)
        event_query = r"""
$log='Microsoft-Windows-TaskScheduler/Operational'
$cutoff=(Get-Date).AddDays(-{days})
$events=@(Get-WinEvent -FilterHashtable @{{LogName=$log;Id=100,102;StartTime=$cutoff}} -MaxEvents 50000 -ErrorAction Stop | Sort-Object TimeCreated)
$started=@{{}}
foreach($event in $events){{
  [xml]$xml=$event.ToXml()
  $data=@{{}}
  foreach($field in @($xml.Event.EventData.Data)){{
    if($field.Name){{ $data[[string]$field.Name]=[string]$field.'#text' }}
  }}
  $instance=$data['InstanceId'];$name=$data['TaskName']
  if(-not $instance -or -not $name){{continue}}
  $key="$name|$instance"
  if($event.Id -eq 100){{ $started[$key]=$event.TimeCreated;continue }}
  if($event.Id -eq 102 -and $started.ContainsKey($key)){{
    $begin=$started[$key];$started.Remove($key)
    $minutes=($event.TimeCreated-$begin).TotalMinutes
    if($minutes -ge 0){{
      [pscustomobject]@{{TaskName=$name;StartTime=$begin.ToString('o');EndTime=$event.TimeCreated.ToString('o');DurationMinutes=[math]::Round($minutes,4)}}
    }}
  }}
}}
""".format(days=args.task_history_days)
        runtime_rows = best_effort("scheduled-task-history", lambda: run_ps_json(event_query, timeout=180), failures)
        if runtime_rows is not None:
            if isinstance(runtime_rows, dict):
                runtime_rows = [runtime_rows]
            stable_csv(root / "telemetry" / "scheduled-task-runs.csv", runtime_rows)

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
            "Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | Sort-Object Name | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction,AllowInboundRules,AllowLocalFirewallRules,NotifyOnListen,LogFileName,LogMaxSizeKilobytes,LogAllowed,LogBlocked"
        ), failures)
        firewall_script = r'''$rules = @(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop | Sort-Object DisplayName,Name)
foreach ($r in $rules) {
    $address = $r | Get-NetFirewallAddressFilter -ErrorAction Stop
    $port = $r | Get-NetFirewallPortFilter -ErrorAction Stop
    $application = $r | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $service = $r | Get-NetFirewallServiceFilter -ErrorAction Stop
    $interface = $r | Get-NetFirewallInterfaceFilter -ErrorAction Stop
    $interfaceType = $r | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop
    $security = $r | Get-NetFirewallSecurityFilter -ErrorAction Stop

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
    parser = argparse.ArgumentParser(description="Collect Windows/Linux/macOS guest configuration and inventory")
    parser.add_argument("--output", default=os.environ.get("CONFIGBACKUP_OUTPUT"), help="Output directory (defaults to CONFIGBACKUP_OUTPUT)")
    parser.add_argument("--skip-firewall", action="store_true", help="Skip firewall rule/profile collection")
    parser.add_argument("--skip-accounts", action="store_true", help="Skip local users/groups inventory")
    parser.add_argument("--include-task-history", action="store_true", help="Capture Windows Task Scheduler completed-run durations from Operational event log (volatile output)")
    parser.add_argument("--task-history-days", type=int, default=60, help="Task Scheduler history lookback (default: 60 days)")
    parser.add_argument("--include-performance", action="store_true", help="Collect volatile host statistics outside configuration manifests")
    parser.add_argument("--include-network-runtime",action="store_true",help="Optional listening ports, processes and client share observations outside config/Git")
    parser.add_argument('--include-drive-health',action='store_true',help='Read SMART/NVMe and Windows reliability data; no tests or setting changes')
    parser.add_argument('--smart-config',help='Optional YAML selecting SMART devices and controller types')
    parser.add_argument('--smart-device',action='append',default=[],help='Explicit SMART device path; repeat for several drives')
    parser.add_argument("--capacity-path", action="append", default=[], help="Local path whose volume capacity should be measured")
    parser.add_argument('--sysctl-key',action='append',default=[],help='Additional exact Linux effective sysctl key')
    parser.add_argument('--registry-config',help='YAML registry allowlist additions/overrides (Windows)')
    parser.add_argument('--skip-registry',action='store_true',help='Disable selected Windows registry inventory')
    parser.add_argument('--include-rsop',action='store_true',help='Collect effective computer/current-user Group Policy XML (Windows)')
    parser.add_argument('--rsop-user',action='append',default=[],help='Additional DOMAIN\\user for RSoP, never a password')
    parser.add_argument("--strict", action="store_true", help="Fail if any best-effort section cannot be collected")
    args = parser.parse_args()
    if not args.output:
        parser.error("--output is required unless CONFIGBACKUP_OUTPUT is set")
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        parser.error("Output directory must be empty; use clean_output: true")
    publish(root, [], finalized=False)
    failures: list[dict[str, str]] = []

    stable_json(root / "collector.json", {
        "collector": "collect_system.py",
        "collector_version": COLLECTOR_VERSION,
        "python": platform.python_version(),
        "platform": sys.platform,
        "secrets_policy": "environment values/password material are not intentionally collected",
    })

    try:
        if not section_enabled("configuration"):
            pass
        elif os.name == "nt":
            info("Collecting Windows guest configuration")
            collect_windows(root, args, failures)
        elif sys.platform == "darwin":
            info("Collecting macOS guest configuration")
            collect_macos(root, args, failures)
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

    if args.include_performance and section_enabled("telemetry/performance"):
        telemetry_failures=[]
        if os.name=='nt':
            counters=best_effort('host-performance',lambda:run_ps_json(
                "Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor | Select-Object Name,PercentProcessorTime; Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory | Select-Object AvailableMBytes,PagesPersec; Get-CimInstance Win32_PerfFormattedData_PerfDisk_LogicalDisk | Select-Object Name,DiskBytesPersec,AvgDisksecPerRead,AvgDisksecPerWrite; Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface | Select-Object Name,BytesTotalPersec"
            ),telemetry_failures)
            if counters is not None:stable_json(root/'telemetry'/'host-counters.json',counters)
        elif sys.platform == 'darwin':
            for name, command in {'vm-stat':['/usr/bin/vm_stat'], 'load':['/usr/sbin/sysctl','vm.loadavg']}.items():
                try:stable_text(root/'telemetry'/(name+'.txt'),run(command,check=True).stdout)
                except Exception as exc:telemetry_failures.append({'section':name,'error':str(exc)})
        else:
            for name in ('stat','meminfo','diskstats','net/dev'):
                source=Path('/proc')/name
                try:stable_text(root/'telemetry'/('proc-'+name.replace('/','-')+'.txt'),source.read_text())
                except OSError as exc:telemetry_failures.append({'section':name,'error':str(exc)})
        stable_json(root/'telemetry'/'collection-status.json',{'failures':telemetry_failures})
    if args.capacity_path and section_enabled("telemetry/capacity"):
        from telemetry import collect_disks, write_envelope
        write_envelope(root/'telemetry'/'health.json', collect_disks(args.capacity_path))
    storage_scopes=[]
    def enrich(scope, action):
        try:
            selected_scopes,selected_failures=action()
            storage_scopes.extend(selected_scopes);failures.extend(selected_failures)
        except Exception as exc:
            storage_scopes.append(section(root,scope,'failed',str(exc)))
            failures.append({'section':scope,'error':str(exc)})
    if os.name!='nt':
        from storage_inventory import collect as collect_storage
        enrich('storage',lambda:collect_storage(root,include_health=args.include_performance))
        from host_details import collect as collect_details
        enrich('kernel',lambda:collect_details(root,extra_sysctls=args.sysctl_key))
    else:
        from windows_settings import collect_registry,collect_rsop
        if not args.skip_registry:
            enrich('registry',lambda:collect_registry(root,powershell_executable(),args.registry_config))
        if args.include_rsop:
            enrich('policy/rsop',lambda:collect_rsop(root,args.rsop_user))
    if not args.skip_firewall and os.name!='nt':
        from firewall_inventory import collect as collect_firewall
        enrich('firewall',lambda:collect_firewall(root))
    from network_inventory import collect as collect_network
    enrich('shares',lambda:collect_network(root,run_ps_json,runtime=args.include_network_runtime))
    if args.include_drive_health:
        from drive_health import collect as collect_drives
        enrich('storage/drive-identities',lambda:collect_drives(root,args.smart_config,args.smart_device,run_ps_json))
    stable_json(root / "collection-errors.json", failures)
    # Individual output files are the certified scopes. Missing files are never
    # evidence of removal when command discovery/access may vary across runs.
    scopes = [section(root, p.relative_to(root).as_posix(), 'complete' if section_enabled(p.relative_to(root).parts[0]) else 'disabled') for p in sorted(root.rglob('*'))
              if p.is_file() and p.name != 'collection-manifest.json' and 'telemetry' not in p.relative_to(root).parts
              and not any(p.relative_to(root).as_posix()==s['path'] or p.relative_to(root).as_posix().startswith(s['path']+'/') for s in storage_scopes)]
    scopes.extend(storage_scopes)
    if failures:
        scopes.append(section(root, '_incomplete-discovery', 'failed', 'See collection-errors.json'))
    publish(root, scopes)
    if failures:
        info(f"Completed with {len(failures)} best-effort collection warning(s)")
        if args.strict:
            return 2
        return 6
    else:
        info("Collection completed successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
