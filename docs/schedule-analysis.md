> **1.6.0:** [Migration, changed behavior, new options, and limitations](release-1.6.0.md) supersede older descriptions below, especially collector failure handling, telemetry paths, and time zones.

# Schedule inventory, timelines, and overlap analysis

This is a **separate, read-only analyzer** built on ConfigBackup collector snapshots. It does **not** connect to databases or change task definitions. Run the system/SQL/PostgreSQL collectors first, then call `collectors/schedule/analyze_schedules.py` on their generated directories.

The goal is to see *what is scheduled*, *when it might run*, *how long it typically runs*, and *which jobs might overlap*. Schedule predictions are not an audit of actual executions.

## Inputs and supported scheduling systems

| System | Source file | Start-time support | Duration history |
|---|---|---|---|
| Windows Task Scheduler | `system/scheduling/scheduled-tasks.json` | Daily, weekly, simple monthly, fixed time, ISO-8601 repetition | Optional TaskScheduler/Operational events 100+102 |
| SQL Server Agent | `sql/instance/agent/schedules.csv`, `schedule-jobs.csv`, `jobs.csv` | Once, every N days, weekly masks, monthly fixed and monthly relative, every N seconds/minutes/hours | Optional `msdb.dbo.sysjobhistory`, step_id=0 |
| PostgreSQL `pg_cron` | `postgresql/databases/*/schedulers/pg-cron-jobs.csv` | Standard five-field cron expressions | Optional `cron.job_run_details` |
| Linux system cron | `system/scheduling/cron/` | Five-field cron (system crontab and cron.d), common aliases | None from cron definitions |
| Linux systemd timers | `system/scheduling/systemd-timers.csv` | Simple `OnCalendar` forms | None; monotonic/event triggers are reported as unknown |
| PostgreSQL pgAgent | `postgresql/databases/*/schedulers/pgagent/*` | Included in collection, **bitmap recurrence not expanded yet** | Not available in this version |

Unrecognized schedules, missed-run/catch-up behavior, jitter, event-driven triggers, and scheduler timezone peculiarities are **reported, not guessed**. Systemd's randomized delay may mean an actual start is later than the projected clock start. Report calculation uses the local time zone of the machine running the analyzer: run it on the target host or explicitly account for host time-zone differences. DST transitions are not modeled to sub-hour precision.

### Collect runtime history (optional)

Actual durations require execution history, not merely recurrence definitions. New optional collector switches:

**Windows system collector**:

```powershell
py collectors/system/collect_system.py --output C:\Temp\sys-snapshot --include-task-history --task-history-days 60
```

The Windows **Task Scheduler Operational log must be enabled and retain suitable 100/102 start/completion events**. The collector matches a task name and execution instance ID; missing/disabled history yields unknown durations. These log events may not be available to unprivileged accounts.

**SQL Server collector**:

```powershell
pwsh -File collectors/sqlserver/Collect-SqlServerConfiguration.ps1 -SqlInstance SQL01 -IncludeAgentHistory -AgentHistoryDays 60
```

It exports up to 20,000 recent job-level history entries (`step_id=0`), including `run_duration`, without reading job-step output or credential material. SQL Agent `run_duration` can exceed 24 hours; the analyzer handles that encoding. Older SQL Agent history may have been purged, and only observed completions can inform runtime estimates.

**PostgreSQL collector**:

```bash
python3 collectors/postgresql/collect_postgresql.py --include-scheduler-history
```

It reads up to 20,000 recent `pg_cron` run timestamps (60-day lookback) if the extension and permissions allow. It does not export error output or SQL command text from `job_run_details`.

**History is deliberately optional and volatile**: if storing collector snapshots in Git, configure Git-only exclusions to prevent rolling run histories from polluting commits:

```yaml
git:
  ignore:
    - '**/*.ispac'
    - '**/job-runs.csv'
    - '**/scheduled-task-runs.csv'
    - '**/pg-cron-runs.csv'
```

Filesystem history can still preserve these files when using `storage: both`. The schedule report itself generally belongs in `storage: filesystem`, not Git, because the reporting horizon shifts daily.

## Run analyzer on existing snapshots

On Windows:

```powershell
py collectors\schedule\analyze_schedules.py `
  --system C:\Temp\system-snapshot `
  --sql C:\Temp\sql-snapshot `
  --sql-host SQL01 `
  --config examples\schedule-report.yaml `
  --output C:\Temp\schedule-report `
  --days 35
```

On Linux with PostgreSQL:

```bash
python3 collectors/schedule/analyze_schedules.py \
  --system /tmp/system-snapshot \
  --postgresql /tmp/postgresql-snapshot \
  --pg-host pg01 \
  --config examples/schedule-report.yaml \
  --output /tmp/schedule-report --days 35
```

To produce a repeatable report on fixed dates for debugging, provide `--start YYYY-MM-DD`. The default is the collector host's local calendar date.

### Integrate with ConfigBackup YAML

See `examples/full-stack-windows-schedule-analysis.yaml`. There are three dependency stages: collect definitions/history → run schedule analyzer → archive the reports. You can add the analyzer task to your existing YAML without changing the backup engine. Use `storage: filesystem` for the rolling reports. If a required collector fails, the dependent report task is skipped, rather than analyzing an incomplete snapshot.

## Output files

| File | Meaning |
|---|---|
| `dashboard.html` | Offline browseable summary of today's starts, overlaps, median/P95 durations, limitations |
| `jobs.csv` | Complete inventory, schedule, recurrence, history sample size, median/P95 duration, exclusions |
| `today.csv` | All known predicted starts of any cadence in first calendar day |
| `daily.csv` | Daily recurrence jobs for first day, including every X hours/minutes |
| `daily-recurring.csv` | Daily recurring jobs expanded over the complete configured horizon |
| `weekly.csv` | Weekly recurrence occurrences over configured horizon |
| `monthly.csv` | Monthly recurrence occurrences over configured horizon |
| `timeline.csv` | All predictable events over configured horizon, sorted chronologically |
| `starts-by-hour.csv` | Starts per hour, with counts split by known/unknown duration |
| `overlaps.csv` | Cross-job overlap of **estimated duration windows**, only where duration is known or overridden |
| `exclusions.csv` | What was excluded and from which report or load analysis |
| `warnings.csv` | Schedules that couldn't be predicted exactly or weren't supported |
| `summary.json` | Machine-readable report counts and horizon |

Start/end time columns in timeline are **predictions**. `end_estimated` is blank when historical durations are unavailable. Median runtimes are used for overlap computation. P95 is provided for reference when assessing worst-case contention. A watchdog that starts every five minutes but exits immediately upon detecting another instance is a good candidate for exclusion from **load/overlap analysis**, not necessarily from schedule inventory.

## Exclusions, per report only

Create `schedule-report.yaml` (a ready-made example is included):

```yaml
reports:
  daily:
    exclude:
      - 'windows:*\My Five Minute Restart Task'
  overlaps:
    exclude:
      - 'windows:*\My Five Minute Restart Task'
  weekly:
    exclude: []
  monthly:
    exclude: []
  timeline:
    exclude: []
```

This **does not remove the job** from `jobs.csv`, from its actual scheduler, or from any other report. Matching uses case-insensitive `*`/`?` patterns over `job_id`. Use `jobs.csv` to discover the exact generated IDs. Examples:

```text
windows:\Maintenance\Restart Watchdog
sql_agent:SQL01:Nightly ETL
pg_cron:PG01:appdb:cleanup
cron:cron.d/backup:3
systemd:logrotate.timer
```

For an execution that *doesn't represent real concurrent load*, such as a single-instance restart/watchdog timer, you can exclude it from load/overlap analysis while keeping its starts in the timeline:

```yaml
analysis:
  watchdogs:
    - 'windows:*\My Five Minute Restart Task'
```

To supply a well-founded duration estimate for jobs without available historical execution data:

```yaml
analysis:
  duration_overrides_minutes:
    'sql_agent:SQL01:Nightly ETL': 90
```

Unlike normal exclusions, `analysis.watchdogs` and `analysis.exclude_from_load` prevent a task from contributing to `overlaps.csv` and `starts-by-hour.csv`, but don't delete it from the inventory. All decisions appear in `exclusions.csv`.

## Limitations and next improvements

This first release covers core recurring schedules. It deliberately does not claim to predict calendar exceptions, SQL Agent idle/startup events, advanced Windows event triggers, systemd monotonic timers, pgAgent's bitmap calendar, arbitrary systemd calendar expressions, SQL Agent schedules with complex server-specific timezone effects, or runtime contention. Those items appear in `warnings.csv` where identified.

**Overlaps are potential scheduling conflicts, not proof of simultaneous CPU/disk load.** A database scheduler, OS task, or external orchestrator may skip duplicate launches, queue jobs, have singleton/mutex enforcement, use randomized delays, or run with widely varying data-dependent durations. The report provides median/P95, sample size and override provenance so you can prioritize investigating plausible hotspots instead of relying on a false certainty.
