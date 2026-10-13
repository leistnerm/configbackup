from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import collector_adapter as adapter
import test_gpo_collection as harness

FIXTURES = Path(__file__).with_name("fixtures")


def fixture(kind, number=1):
    return (FIXTURES / (kind + ".xml")).read_bytes().replace(b"10:00:01", ("10:00:%02d" % number).encode())


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"CONFIGBACKUP_SECTIONS": "{}"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_case(self, transform=None, users=(), runs=2):
        output = self.root / ("case-%d" % len(list(self.root.iterdir())))
        output.mkdir()

        def factory(root, number):
            def execute(args, **kwargs):
                self.assertEqual(kwargs, {"capture_output": True, "timeout": 120, "shell": False})
                self.assertEqual(args[0], "fixed-gpresult.exe")
                raw = fixture(args[2], number)
                if transform:
                    value = transform(raw, args, number)
                    if isinstance(value, subprocess.CompletedProcess):
                        return value
                    raw = value
                if raw is not None:
                    Path(args[4]).write_bytes(raw)
                return subprocess.CompletedProcess(args, 0, b"exported", b"")
            return harness.LocalGpresult("fixed-gpresult.exe", root, execute=execute)

        with contextlib.redirect_stdout(io.StringIO()):
            result = harness.run_test(output, runs=runs, interval=0, users=users,
                                      machine={"computer": "SYNTHETIC FIXTURE", "elevated": True},
                                      runner_factory=factory)
        self.assertEqual(json.loads((output / "report.json").read_text())["status"], result["status"])
        self.assertTrue((output / "report.html").exists())
        self.assertTrue((output / "REVIEW-GUIDE.txt").exists())
        self.assertFalse(any((output / "run-01" / "scratch").iterdir()))
        return result, output

    def test_source_pins_and_exact_function_bodies(self):
        functions = adapter.load_collector(None)
        self.assertTrue(callable(functions["collect_rsop"]))
        self.assertNotIn("collect_registry", functions)
        for name, expected in adapter.SOURCE_HASHES.items():
            self.assertEqual(hashlib.sha256((Path(adapter.__file__).parent / "collector-source" / name).read_bytes()).hexdigest(), expected)

    def test_changed_source_is_rejected(self):
        source = self.root / "source"
        shutil.copytree(Path(adapter.__file__).parent / "collector-source", source)
        with (source / "windows_settings.py").open("a") as stream:
            stream.write("\n# changed\n")
        with self.assertRaisesRegex(ValueError, "source has changed"):
            adapter.load_collector(None, source_dir=source)

    def test_timestamp_only_changes_pass_with_different_raw_hashes(self):
        result, _ = self.run_case()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["comparisons"]), 2)
        for scope in result["comparisons"]:
            self.assertTrue(scope["pairs"][0]["config_identical"])
            self.assertFalse(scope["pairs"][0]["raw_identical"])

    def test_identical_raw_also_passes(self):
        result, _ = self.run_case(lambda raw, args, number: fixture(args[2], 1))
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(all(row["pairs"][0]["raw_identical"] for row in result["comparisons"]))

    def test_attribute_order_is_canonicalized(self):
        result, _ = self.run_case(lambda raw, args, number: raw.replace(b'name="Sample setting" state="Enabled"', b'state="Enabled" name="Sample setting"') if number == 2 else raw)
        self.assertEqual(result["status"], "PASS")

    def test_real_policy_value_change_is_review(self):
        result, output = self.run_case(lambda raw, args, number: raw.replace(b"<p:Value>1</p:Value>", b"<p:Value>2</p:Value>") if number == 2 else raw)
        self.assertEqual(result["status"], "REVIEW")
        diff = next((output / "diffs").glob("*.diff")).read_text(encoding="utf-8")
        self.assertIn(' = "1"', diff)
        self.assertIn(' = "2"', diff)

    def test_policy_modification_date_is_not_removed(self):
        result, _ = self.run_case(lambda raw, args, number: raw.replace(b"2026-09-01", b"2026-09-02") if number == 2 else raw)
        self.assertEqual(result["status"], "REVIEW")

    def test_nested_creation_time_is_not_removed(self):
        result, _ = self.run_case(lambda raw, args, number: raw.replace(b"2026-08-01", b"2026-08-02") if number == 2 else raw)
        self.assertEqual(result["status"], "REVIEW")

    def test_policy_list_order_is_preserved(self):
        result, _ = self.run_case(lambda raw, args, number: raw.replace(b"<Value>first</Value><Value>second</Value>", b"<Value>second</Value><Value>first</Value>") if number == 2 else raw)
        self.assertEqual(result["status"], "REVIEW")

    def test_indentation_changes_are_visible(self):
        result, _ = self.run_case(lambda raw, args, number: raw.replace(b"\n  ", b"\n    ") if number == 2 else raw)
        self.assertEqual(result["status"], "REVIEW")

    def test_utf16_xml_input(self):
        result, _ = self.run_case(lambda raw, args, number: raw.decode("utf-8").replace('encoding="utf-8"', 'encoding="utf-16"').encode("utf-16"))
        self.assertEqual(result["status"], "PASS")

    def test_unicode_normalizer_and_report(self):
        normalizer = adapter.load_collector(None)["normalize_rsop"]
        raw = fixture("user").replace(b"Example user baseline", "München 東京".encode("utf-8"))
        self.assertIn("München 東京", normalizer(raw))
        result, output = self.run_case()
        result["machine"]["user"] = "München 東京"
        harness.render_report(result, output)
        self.assertIn("user", (output / "report.html").read_text(encoding="utf-8"))

    def test_computer_denied_does_not_stop_user_or_later_runs(self):
        def transform(raw, args, number):
            return subprocess.CompletedProcess(args, 5, b"", b"Access denied") if args[2] == "computer" else raw
        result, _ = self.run_case(transform)
        self.assertEqual(result["status"], "INCOMPLETE")
        statuses = {row["scope"]: row["status"] for row in result["comparisons"]}
        self.assertEqual(statuses, {"policy/rsop/computer": "INCOMPLETE", "policy/rsop/user": "PASS"})
        self.assertEqual(len(result["runs"][1]["commands"]), 2)

    def test_recovery_after_failure_is_not_false_pass(self):
        result, _ = self.run_case(lambda raw, args, number: subprocess.CompletedProcess(args, 1, b"", b"No RSoP data") if number == 1 and args[2] == "user" else raw)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertTrue(result["runs"][1]["scopes"][1]["usable"])

    def test_additional_user_scopes_and_identity(self):
        users = [r"EXAMPLE\normaluser", r"EXAMPLE\anotheruser"]
        result, _ = self.run_case(users=users)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["comparisons"]), 4)
        self.assertEqual(result["runs"][0]["commands"][2]["requested_user"], users[0])
        self.assertEqual(result["runs"][0]["scopes"][2]["context"]["requested_user"], users[0])

    def test_timeout_is_per_scope(self):
        def transform(raw, args, number):
            if args[2] == "computer":
                raise subprocess.TimeoutExpired(args, 120, output=b"partial", stderr=b"still waiting")
            return raw
        result, _ = self.run_case(transform)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertTrue(result["runs"][0]["scopes"][1]["usable"])
        self.assertIn("timed out", result["runs"][0]["commands"][0]["error"])

    def test_missing_output_is_incomplete(self):
        result, _ = self.run_case(lambda raw, args, number: None if args[2] == "computer" else raw)
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_malformed_xml_is_retained_as_diagnostic(self):
        result, output = self.run_case(lambda raw, args, number: b"<Rsop><broken>" if args[2] == "computer" else raw)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual((output / "run-01/diagnostics/command-01/raw.xml").read_bytes(), b"<Rsop><broken>")

    def test_non_rsop_document_is_incomplete(self):
        result, _ = self.run_case(lambda raw, args, number: b"<Error>Unavailable</Error>")
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_wrong_scope_payload_is_incomplete(self):
        result, _ = self.run_case(lambda raw, args, number: fixture("user", number))
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("ComputerResults", result["runs"][0]["scopes"][0]["validation_error"])

    def test_empty_results_is_incomplete(self):
        result, _ = self.run_case(lambda raw, args, number: b"<Rsop><ComputerResults/></Rsop>")
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_duplicate_results_is_incomplete(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            harness.xml_details(b"<Rsop><ComputerResults><Name>a</Name></ComputerResults><ComputerResults><Name>b</Name></ComputerResults></Rsop>", "computer")

    def test_dtd_and_entity_are_rejected_in_utf8_and_utf16(self):
        xml = '<!DOCTYPE Rsop [<!ENTITY x "test">]><Rsop><ComputerResults><Name>&x;</Name></ComputerResults></Rsop>'
        for encoding in ("utf-8", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                with self.assertRaisesRegex(ValueError, "DTD/entity"):
                    harness.parse_xml(xml.encode(encoding))

    def test_large_xml_limit_is_incomplete(self):
        with mock.patch.object(harness, "MAX_XML_BYTES", 10):
            result, _ = self.run_case()
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_disabled_scope_is_not_a_pass(self):
        with mock.patch.dict(os.environ, {"CONFIGBACKUP_SECTIONS": '{"policy/rsop/computer": false}'}):
            result, _ = self.run_case()
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(len(result["runs"][0]["commands"]), 1)
        self.assertEqual(result["runs"][0]["scopes"][0]["status"], "disabled")

    def test_invalid_section_switches_are_reported(self):
        with mock.patch.dict(os.environ, {"CONFIGBACKUP_SECTIONS": 'not JSON'}):
            result, _ = self.run_case()
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("error", result["runs"][0])

    def test_third_run_is_checked_against_first(self):
        result, _ = self.run_case(lambda raw, args, number: raw.replace(b"2026-09-01", b"2026-09-03") if number == 3 else raw, runs=3)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(len(result["comparisons"][0]["pairs"]), 2)
        self.assertTrue(result["comparisons"][0]["pairs"][0]["config_identical"])
        self.assertFalse(result["comparisons"][0]["pairs"][1]["config_identical"])

    def test_tampered_certified_file_is_rejected(self):
        result, output = self.run_case()
        item = result["runs"][0]["scopes"][0]
        (output / "run-01" / item["path"] / "context.json").write_text("{}")
        verified = harness.verify_scope(output / "run-01", item, adapter.load_collector(None)["normalize_rsop"])
        self.assertFalse(verified["usable"])
        self.assertIn("hash mismatch", verified["validation_error"])

    def test_both_manifest_files_are_required(self):
        result, output = self.run_case()
        item = result["runs"][0]["scopes"][0]
        item["files"].pop(item["path"] + "/context.json")
        verified = harness.verify_scope(output / "run-01", item, adapter.load_collector(None)["normalize_rsop"])
        self.assertFalse(verified["usable"])

    def test_report_escapes_policy_html_and_command_diagnostics(self):
        raw_name = b"Example user baseline &amp; preferences"
        hostile = b"&lt;script&gt;alert('x')&lt;/script&gt;"
        result, output = self.run_case(lambda raw, args, number: raw.replace(raw_name, hostile))
        content = (output / "report.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)
        self.assertIn("default-src 'none'", content)
        self.assertNotIn("https://", content)
        self.assertEqual(result["status"], "PASS")

    def test_xml_path_diff_preserves_order_and_namespace(self):
        lines = "".join(harness.xml_lines(fixture("computer")))
        self.assertIn("{urn:synthetic-policy-extension}Value[1]", lines)
        self.assertLess(lines.index(' = "first"'), lines.index(' = "second"'))

    def test_existing_output_directory_is_refused(self):
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaises(FileExistsError):
            harness.create_output(existing)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_unc_output_is_refused(self):
        for path in (r"\\server\share\report", "//server/share/report"):
            with self.assertRaisesRegex(ValueError, "network"):
                harness.create_output(path)

    def test_allowlist_refuses_remote_and_wrong_executable(self):
        runner = harness.LocalGpresult("fixed-gpresult.exe", self.root, execute=mock.Mock())
        for args in (["gpupdate", "/force"], ["gpresult", "/s", "remote", "/x", "x.xml", "/f"],
                     ["gpresult", "/scope", "user", "/h", "x.html", "/f"],
                     ["gpresult", "/scope", "computer", "/x", "x.xml", "/f", "/user", "account"]):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ValueError, "allowlist"):
                    runner.run(args, capture_output=True, timeout=120)
        runner.execute.assert_not_called()

    def test_allowlist_refuses_output_outside_scratch(self):
        runner = harness.LocalGpresult("fixed-gpresult.exe", self.root, execute=mock.Mock())
        with self.assertRaisesRegex(ValueError, "scratch"):
            runner.run(["gpresult", "/scope", "user", "/x", str(self.root / "report.xml"), "/f"], capture_output=True, timeout=120)
        runner.execute.assert_not_called()

    def test_no_samples_is_incomplete(self):
        self.assertEqual(harness.overall_status([], []), "INCOMPLETE")

    def test_export_removed_before_comparison_is_incomplete(self):
        result, output = self.run_case()
        (output / "run-01/policy/rsop/computer/report.xml").unlink()
        rows = harness.compare_runs(output, result["runs"])
        self.assertEqual(rows[0]["status"], "INCOMPLETE")
        self.assertEqual(rows[1]["status"], "PASS")

    def test_raw_export_modified_before_comparison_is_incomplete(self):
        result, output = self.run_case()
        (output / "run-01/telemetry/rsop/computer/report.xml").write_text("changed")
        rows = harness.compare_runs(output, result["runs"])
        self.assertEqual(rows[0]["status"], "INCOMPLETE")

    def test_diff_error_does_not_prevent_report(self):
        with mock.patch.object(harness, "xml_lines", side_effect=OSError("Simulated diff failure")):
            result, output = self.run_case(lambda raw, args, number: raw.replace(b"2026-09-01", b"2026-09-02") if number == 2 else raw)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertIn("Simulated diff failure", (output / "report.html").read_text(encoding="utf-8"))

    def test_keyboard_interruption_writes_incomplete_report(self):
        def transform(raw, args, number):
            if number == 2:
                raise KeyboardInterrupt()
            return raw
        result, _ = self.run_case(transform)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["fatal_error"], "KeyboardInterrupt")

    def test_normalizer_is_idempotent_without_xml_namespace(self):
        normalizer = adapter.load_collector(None)["normalize_rsop"]
        raw = b"<Rsop><CreationTime>one</CreationTime><UserResults><Name>Example</Name></UserResults></Rsop>"
        normalized = normalizer(raw)
        self.assertNotIn("CreationTime", normalized)
        self.assertEqual(normalized, normalizer(normalized.encode("utf-8")))

    @unittest.skipIf(os.name == "nt", "Only checks the non-Windows guard")
    def test_live_cli_refuses_non_windows(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_info:
                harness.main([])
        self.assertEqual(exit_info.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
