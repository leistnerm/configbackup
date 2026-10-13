# Schedule analyzer

Runs entirely offline against current collector outputs; never changes scheduled jobs. Requires PyYAML, supplied with ConfigBackup.

Use `python collectors/schedule/analyze_schedules.py --help` or see `docs/schedule-analysis.md`.

For a five-minute watchdog that only restarts a singleton if needed, add it to `reports.daily.exclude` and/or `reports.overlaps.exclude` in `examples/schedule-report.yaml`. The exclusion affects those report files only; the task remains collected.
