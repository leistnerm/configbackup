"""Invalid history values do not poison schedule estimates or prior reports."""
import csv
import datetime as dt
import json
from pathlib import Path
import tempfile
import types
import unittest
import yaml
from test_schedule_analyzer import schedule

class DurationValidationTests(unittest.TestCase):
    def test_nonfinite_history_has_no_estimate(self):
        self.assertEqual(schedule.median_p95([float('inf'),float('nan'),-1]),(None,None))
        self.assertEqual(schedule.median_p95([float('inf'),2,4]),(3,4))

    def test_invalid_observation_is_omitted_but_healthy_run_remains(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'telemetry').mkdir()
            job=schedule.Job('fixture','windows','fixture','daily','',lambda s,d:[])
            with (root/'telemetry/executions.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=['job_id','start','minutes','status']);writer.writeheader()
                for value in ('inf','nan','-1','bad','1e308','5'):writer.writerow({'job_id':job.key,'start':'2026-10-08T00:00:00Z','minutes':value,'status':'completed'})
            warnings=[];observed=schedule.load_observations(root,job,warnings)
            self.assertEqual(len(observed),1);self.assertEqual(observed[0]['duration_minutes'],5)
            self.assertEqual(len(warnings),5)
            self.assertEqual(observed,schedule.load_observations(root,job,[]))

    def test_invalid_override_preserves_previous_report(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);output=root/'reports';output.mkdir();prior=output/'jobs.csv';prior.write_bytes(b'previous report')
            config=root/'schedule.yaml'
            args=types.SimpleNamespace(output=str(output),config=str(config),start='2026-10-08',days=1,system=None,sql=None,postgresql=None)
            for value in (float('inf'),float('nan'),-1,True,10**400):
                with self.subTest(value=str(value)[:20]):
                    config.write_text(yaml.safe_dump({'analysis':{'duration_overrides_minutes':{'*':value}}}))
                    with self.assertRaisesRegex(ValueError,'finite'):schedule.report(args)
                    self.assertEqual(prior.read_bytes(),b'previous report')
                    self.assertEqual(json.loads((output/'generation-status.json').read_text())['status'],'failed')
