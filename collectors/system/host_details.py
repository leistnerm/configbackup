"""Read-only firmware, driver and kernel configuration inventories."""
from __future__ import annotations
import json
import platform
from pathlib import Path
import re
import shlex
import sys
from completeness import section
from sections import enabled
from storage_inventory import command, text, Unavailable, ordered_rows

LINUX_SYSCTLS = (
 'kernel.panic','kernel.panic_on_oops','kernel.nmi_watchdog','kernel.kptr_restrict','kernel.dmesg_restrict',
 'kernel.yama.ptrace_scope','kernel.randomize_va_space','kernel.sysrq','kernel.core_pattern',
 'kernel.core_uses_pid','kernel.shmmax','kernel.shmall','kernel.shmmni','kernel.sem','kernel.pid_max',
 'kernel.threads-max','fs.file-max','fs.aio-max-nr','fs.inotify.max_user_watches','fs.inotify.max_user_instances',
 'vm.swappiness','vm.overcommit_memory','vm.overcommit_ratio','vm.dirty_ratio','vm.dirty_background_ratio',
 'vm.dirty_bytes','vm.dirty_background_bytes','vm.max_map_count','vm.nr_hugepages','vm.min_free_kbytes',
 'vm.zone_reclaim_mode','net.ipv4.ip_forward','net.ipv4.conf.all.rp_filter',
 'net.ipv4.conf.default.rp_filter','net.ipv4.tcp_syncookies','net.ipv4.tcp_fin_timeout',
 'net.ipv4.tcp_keepalive_time','net.ipv4.ip_local_port_range','net.core.somaxconn',
 'net.core.rmem_max','net.core.wmem_max','net.ipv6.conf.all.forwarding')
SECRET = re.compile(r'password|passwd|secret|token|credential|(?:^|[._-])key(?:$|[._-])',re.I)


def boot_arguments(raw):
    # Preserve order; repeated command-line switches can be significant.
    return [argument.partition('=')[0]+'=<REDACTED>' if '=' in argument and SECRET.search(argument.partition('=')[0]) else argument for argument in shlex.split(raw)]


def linux_firmware(base=Path('/sys/class/dmi/id')):
    if not base.exists(): raise Unavailable('DMI is not exposed on this platform or guest')
    result={}
    for name in ('bios_vendor','bios_version','bios_date','bios_release','sys_vendor','product_name','product_version','board_vendor','board_name','board_version','chassis_vendor','chassis_type','chassis_version'):
        path=base/name
        if path.exists():result[name]=path.read_text().strip()
    if not result:raise Unavailable('DMI firmware identity unavailable')
    return result


def linux_modules(base=Path('/sys/module')):
    if not base.is_dir():raise Unavailable('Kernel module sysfs unavailable')
    result=[]
    for module in sorted(base.iterdir()):
        row={'name':module.name}
        for key in ('version','srcversion','taint'):
            if (module/key).is_file():row[key]=(module/key).read_text().strip()
        # sysfs includes built-in modules. Do not infer loadability from membership.
        result.append(row)
    return result


def module_parameters(base=Path('/sys/module')):
    result=[]
    for directory in sorted(base.glob('*/parameters')):
        for file in sorted(directory.iterdir()):
            if not file.is_file():continue
            result.append({'module':directory.parent.name,'parameter':file.name,
                'value':'<REDACTED>' if SECRET.search(file.name) else file.read_text().strip()})
    return result


def linux_drivers(base=Path('/sys/bus')):
    result=[]
    for bus in ('pci','usb','platform','virtio'):
        for device in sorted((base/bus/'devices').glob('*')):
            driver=device/'driver'
            if not driver.is_symlink():continue
            row={'bus':bus,'device':device.name,'driver':driver.resolve().name}
            module=driver/'module'
            if module.is_symlink():row['module']=module.resolve().name
            for key in ('vendor','device','subsystem_vendor','subsystem_device','class','modalias'):
                if (device/key).is_file():row[key]=(device/key).read_text().strip()
            result.append(row)
    return result


def sysctls(extra=(), base=Path('/proc/sys')):
    result={}
    for key in sorted(set(LINUX_SYSCTLS)|set(extra)):
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_-]+)+',key):raise ValueError('Invalid sysctl name')
        file=base.joinpath(*key.split('.'))
        if not file.exists():continue  # Kernel-specific keys are absent, not fabricated.
        result[key]='<REDACTED>' if SECRET.search(key) else file.read_text().strip()
    return result


def kernel_files():
    paths=[]
    for directory in ('/etc/sysctl.d','/usr/lib/sysctl.d','/run/sysctl.d','/etc/modprobe.d','/usr/lib/modprobe.d','/etc/modules-load.d','/usr/lib/modules-load.d','/etc/default/grub.d'):
        base=Path(directory)
        if base.is_dir():paths.extend(p for p in base.iterdir() if p.is_file() and p.suffix in ('.conf','.cfg'))
    for name in ('/etc/sysctl.conf','/etc/modules','/etc/default/grub','/etc/kernel/cmdline','/boot/config-'+platform.release(),'/lib/modules/'+platform.release()+'/modules.builtin','/lib/modules/'+platform.release()+'/modules.dep'):
        if Path(name).is_file():paths.append(Path(name))
    # Text is preserved verbatim; secrets in administrator configuration are gated at Git publication.
    return [{'path':str(p),'content':p.read_text()} for p in sorted(set(paths))]


def mac_extensions(raw):
    # kmutil address, size, refs and load tags are runtime values; retain exact bundle identity/version/UUID.
    rows=[]
    for line in raw.splitlines():
        if not line.strip():continue
        found=re.search(r'\b([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_.-]+)+)\s+\(([^)]+)\)\s+([A-Fa-f0-9-]{36})\b',line)
        if not found:raise ValueError('Unrecognized kmutil row; preserving previous extension inventory')
        rows.append(dict(zip(('bundle','version','uuid'),found.groups())))
    return ordered_rows(rows)


def collect(root, platform_name=None, extra_sysctls=()):
    root=Path(root);platform_name=platform_name or sys.platform;scopes=[];failures=[]
    def probe(name,fn):
        status,error='complete',''
        if not enabled('configuration') or not enabled(name):status,error='disabled','Disabled by configuration'
        else:
            try:
                value=fn();path=root/name/'configuration.json';path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
            except Unavailable as exc:status,error='not_applicable',str(exc)
            except Exception as exc:status,error='failed',str(exc);failures.append({'section':name,'error':error})
        scopes.append(section(root,name,status,error))
    if platform_name.startswith('linux'):
        probe('hardware/firmware',linux_firmware)
        probe('hardware/drivers',linux_drivers)
        probe('kernel/modules',linux_modules)
        probe('kernel/module-parameters',module_parameters)
        probe('kernel/effective-sysctl',lambda:sysctls(extra_sysctls))
        probe('kernel/boot-arguments',lambda:boot_arguments(Path('/proc/cmdline').read_text()))
        probe('kernel/configuration-files',kernel_files)
        probe('kernel/huge-pages',lambda:{p.name:p.read_text().strip() for p in sorted(Path('/sys/kernel/mm/transparent_hugepage').glob('*')) if p.is_file()})
    elif platform_name=='darwin':
        probe('hardware/firmware',lambda:json.loads(text(['system_profiler','SPHardwareDataType','-json'])))
        probe('kernel/extensions',lambda:mac_extensions(text(['kmutil','showloaded','--list-only'])))
        probe('kernel/system-extensions',lambda:sorted(text(['systemextensionsctl','list']).splitlines()))
        probe('kernel/settings',lambda:text(['sysctl','kern.osrelease','kern.osversion','kern.version','kern.bootargs','kern.maxfiles','kern.maxfilesperproc','kern.maxproc','kern.maxprocperuid']))
    return scopes,failures
