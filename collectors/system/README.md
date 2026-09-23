# System collector

`collect_system.py` collects Windows/Linux guest hardware, CPU/memory, disk/volume/Storage Spaces or Linux storage layout, networking, shares, services, scheduling, software, patches, roles/features, driver, account, firewall, time, and related configuration into a deterministic current-state tree for ConfigBackup.

On Windows, firewall collection includes the effective rule conditions from `ActiveStore`—addresses, protocol/ports, application/service, interface constraints, security conditions, direction/action/profile, and policy source—and writes both `network/firewall-rules.json` and a flat `network/firewall-rules.csv` with a human-readable summary.

See `../../docs/system-collector.md` for the full inventory list, security notes, and examples.

Quick test:

```bash
python3 collect_system.py --output /tmp/configbackup-system-test
```
