# Tests

Run from the project root:

```bash
python3 -m unittest discover -s tests -v
```

## 1.5.0 scheduling and determinism checks

`test_schedule_analyzer.py` covers SQL Agent daily/weekly and subday schedules, SQL Agent durations over 24 hours, Windows five-minute trigger repetition, cron and systemd recurrence, report-only exclusions that leave unrelated reports intact, deterministic report reruns, PostgreSQL CSV ordering, and offline snapshot-comparison classification. Linux synthetic/report tests execute here; the Windows Task Scheduler Operational query and dbatools collector must be integration-tested on the intended Windows/SQL Server hosts.
