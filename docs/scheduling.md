# Scheduling ConfigBackup

ConfigBackup runs once and exits. Use the operating system's scheduler.

## Windows Task Scheduler

Example action:

```text
Program/script:
C:\Python311\python.exe

Arguments:
C:\Tools\ConfigBackup\configbackup.py --config C:\Tools\ConfigBackup\configbackup.yaml

Start in:
C:\Tools\ConfigBackup
```

Recommended task settings:

- Run whether the user is logged on or not.
- Use an account that has read access to sources, execute rights for collectors, and write access to the backup root.
- If backing up protected system configuration, run with the required elevated rights.
- Configure "If the task is already running" to **Do not start a new instance**. ConfigBackup also has its own lock as a second line of defense.
- Capture the process exit code in Task Scheduler history/monitoring.

Example PowerShell command for a manual run:

```powershell
& 'C:\Python311\python.exe' `
  'C:\Tools\ConfigBackup\configbackup.py' `
  --config 'C:\Tools\ConfigBackup\configbackup.yaml'
exit $LASTEXITCODE
```

## cron

Daily at 2:15 AM:

```cron
15 2 * * * /usr/bin/python3 /opt/configbackup/configbackup.py --config /etc/configbackup/configbackup.yaml >> /var/log/configbackup-cron.log 2>&1
```

The application also keeps its own rotating log beneath `_configbackup` in the backup root.

## systemd timer

Example service `/etc/systemd/system/configbackup.service`:

```ini
[Unit]
Description=ConfigBackup configuration archive

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/configbackup/configbackup.py --config /etc/configbackup/configbackup.yaml
User=root
Group=root
```

Example timer `/etc/systemd/system/configbackup.timer`:

```ini
[Unit]
Description=Run ConfigBackup daily

[Timer]
OnCalendar=*-*-* 02:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now configbackup.timer
```

Inspect runs:

```bash
systemctl status configbackup.service
journalctl -u configbackup.service
```

## Environment/secrets

For scheduled collector scripts, inject sensitive values through the scheduler/service environment rather than placing them in the YAML where practical.

For systemd, consider an `EnvironmentFile=` readable only by the service account. For Task Scheduler, prefer integrated authentication or a credential facility appropriate to the collector.
