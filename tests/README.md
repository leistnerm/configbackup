# Tests

Run from the project root:

```bash
python3 -m unittest discover -s tests -v
```

## Coverage

The 2.0.1 release suite contains 143 tests, including 13 diagnostics tests. Run from the project root so local modules resolve correctly. `test_v2.py` and `test_v2_extended.py` cover section rollback/sealing races, disabled dependency chains, actual staged-blob scanning, retrying Git publication, alert state/rates/staleness, local multipart SMTP delivery, scheduling observations, storage/firewall normalization, drive-health failures, independent runtime retention, and launcher/grant generation. See [release validation](../VALIDATION.md) for live tests and limits.

## Inherited scheduling and determinism checks

`test_schedule_analyzer.py` covers SQL Agent daily/weekly and subday schedules, SQL Agent durations over 24 hours, Windows five-minute trigger repetition, cron and systemd recurrence, report-only exclusions that leave unrelated reports intact, deterministic report reruns, PostgreSQL CSV ordering, and offline snapshot-comparison classification. Linux synthetic/report tests execute here; the Windows Task Scheduler Operational query and dbatools collector must be integration-tested on the intended Windows/SQL Server hosts.
