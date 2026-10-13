# Tests

Run from the project root:

```bash
python3 -m unittest discover -s tests -v
```

## Coverage

Run from the project root so local modules resolve correctly. `test_collection_monitoring_and_configuration.py` and `test_host_runtime_and_access.py` cover section rollback/sealing races, disabled dependency chains, actual staged-blob scanning, retrying Git publication, alert state/rates/staleness, local multipart SMTP delivery, scheduling observations, storage/firewall normalization, drive-health failures, independent runtime retention, and launcher/grant generation. See [platform validation](../VALIDATION.md) for platform limitations.

## Scheduling and determinism checks

`test_schedule_analyzer.py` covers SQL Agent daily/weekly and subday schedules, SQL Agent durations over 24 hours, Windows five-minute trigger repetition, cron and systemd recurrence, report-only exclusions that leave unrelated reports intact, deterministic report reruns, PostgreSQL CSV ordering, and offline snapshot-comparison classification. the Windows Task Scheduler Operational query and dbatools collector must be integration-tested on the intended Windows/SQL Server hosts.
