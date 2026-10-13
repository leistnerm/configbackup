# Autodetection and guided setup

Run `python autodetect.py` even before PyYAML is installed, or choose **Autodetect and guided setup** from `python configure.py --config configbackup.yaml`.

Detection reads client versions, available PowerShell modules, local database process/service names, and running Docker image names only when a local socket context is established. Windows service names retain named-instance distinctions. Remote Docker SSH/TCP contexts are not contacted. Client presence is a hint, not proof of a database server, endpoint, permissions or compatible version. Probe failures remain visible.

The menu suggests system, PostgreSQL and SQL Server collection. Choosing one asks for a unique task/connection name, endpoint, client directory and authentication references; it adds a collector and independent filesystem archive task to the **unsaved** configuration. Remote servers can be selected even with no local detection. Review, save and use Shared connections → Test managed task to check real access and coverage. Detection never opens database connections or scans the network.

## Dependency installation

The installer presents its exact allowlisted commands and requires typing INSTALL before execution. Normal package-manager consent/trust/admin prompts remain in effect. It rechecks availability afterward; exit zero without a detectable tool is unverified. It does not install database servers, change roles/firewalls/grants or register schedules.

- Python requirements: new virtual environment only; optional approved offline wheels. A postcheck imports YAML libraries and IANA time zones.
- macOS: Homebrew Git/libpq/.NET SDK; PowerShell and SqlPackage via existing .NET SDK global tools.
- Windows: WinGet Git/PowerShell/.NET SDK. PostgreSQL clients require a separately approved client distribution.
- Linux: existing apt-get for Git/PostgreSQL clients; no repositories are added. Other distributions receive manual guidance.
- dbatools: PowerShell Gallery CurrentUser install, preserving publisher/repository prompts.

```sh
python autodetect.py --install python-packages --venv /new/venv
python autodetect.py --install postgresql-client
python autodetect.py --install powershell
python configure.py --config configbackup.yaml --autodetect
```

These are install capabilities, not evidence that every installer has run here. The validation record distinguishes the real offline Python installation from package-manager fixtures. Consult current official distribution instructions before deployment: [SqlPackage](https://learn.microsoft.com/en-us/sql/tools/sqlpackage/sqlpackage-download), [PowerShell](https://learn.microsoft.com/en-us/powershell/scripting/install/installing-powershell), [dbatools](https://dbatools.io/install/), [PostgreSQL](https://www.postgresql.org/download/).

## Discover files from scheduled jobs

The editor offers **Discover files from scheduled jobs**. It reads Windows task XML, current-user cron and selected system cron definitions, macOS launchd plists, or Linux systemd timer/service definitions. It proposes script paths and explicit configuration flags such as `--config`, `--settings`, `--defaults-file` and PowerShell `-File`. Paths are resolved only with an explicit working directory. You choose each available file; selections become unsaved filesystem-only `backup.configuration_files` entries.

```sh
python schedule_discovery.py
python schedule_discovery.py --definition saved-task.xml --definition saved-agent.plist
```

No scheduled job/script is executed. Raw command lines and file contents are not printed. Inline shell/PowerShell commands, environment expansion, wildcards, indirect wrappers, includes and missing working directories require manual review. Saved Windows definitions on another platform can be inspected, but foreign paths cannot be selected as available local files. Reads are bounded to 500 jobs/definitions and 2 MiB per definition/response. Unavailable protected sources and limits are reported. Linux systemd discovery currently reads system timers, not per-user timer managers; cron discovery is not an inventory of every user's private crontab. Selected files may contain credentials: review them and protect filesystem history.

The CLI also scans the top level of explicit working directories and executable/script directories for `.ini`, `.config`, `.json`, `.yaml`, `.yml`, `.toml`, `.conf`, `.properties` and `.xml` files. These are possible configurations, not proof of use by that job. It reads directory entries only, skips shared system/Python executable directories and directory symlinks, does not recurse, and scans at most 50 directories with at most 200 entries each. Oversized directories produce no directory-based suggestions, avoiding an arbitrary partial selection. Direct references from job arguments remain separately visible. Missing/inaccessible/foreign directories and limits appear in `directory_scans`.
