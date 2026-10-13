#!/usr/bin/env python3
"""Safe local workstation acceptance runner. Standard library bootstrap, Python 3.10+."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import traceback
import unittest
from urllib.parse import quote
import uuid
import zipfile
from zoneinfo import ZoneInfo

KIT_VERSION = "1.2.0"
RELEASES = {
    "configbackup-1.2.0.zip": "94be864ff5b49845800217204167c88fc585120043cf694a0f576eac8b9e3517",
    "configbackup-gpo-test-1.0.0.zip": "ccdcef46cf9803969059fc12d432a57bbaa56d5f247149a8f6ae860ade037a8a"
}
EXPECTED_CHECKS = 278
EXPECTED_GPO_FIXTURES = 42
WINDOWS_COMPONENTS = ('iis','dns','dhcp','hyperv','certificates','dfs','nps','wef','wsus','adcs')
WINDOWS_DETAILS = {'iis-extended':'iis','dns-policies':'dns','dhcp-reservation-options':'dhcp','hyperv-network':'hyperv','hyperv-replication':'hyperv'}
STATES = {"PASS", "FAIL", "REVIEW", "INCOMPLETE", "NOT RUN"}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def local_path(value):
    """Resolve junctions/symlinks first, then reject UNC/mapped output locations."""
    if str(value).startswith(("\\\\", "//")):
        raise ValueError("Use a local, nonsynced directory, not a network share")
    path = Path(value).expanduser().resolve()
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Use a local, nonsynced directory, not a network share")
    if os.name == "nt":
        import ctypes
        kind = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
        if kind not in (2, 3, 6):
            raise ValueError("Use a writable local drive (network/unknown drives are refused)")
    return path


def new_output(value=None):
    if value is None:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        value = base / "ConfigBackup" / "SafeTests" / (
            dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    result = local_path(value)
    result.mkdir(parents=True, exist_ok=False)
    return result


def verify_kit(kit):
    manifest = kit / "CHECKSUMS.sha256"
    records = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\:]+)", line)
        if not match or match[2] in (".", "..") or match[2] in records:
            raise ValueError("Invalid/duplicate kit checksum entry")
        records[match[2]] = match[1]
    required = {*RELEASES, "Run-WorkstationTests.py", "Run-SafeTests.cmd", "Run-OfflineChecks.py", "prerequisite_setup.py", "component_repeat.py"}
    if not required.issubset(records):
        raise ValueError("Required launcher/release files missing from checksum manifest")
    for name, expected in records.items():
        path = kit / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != kit.resolve():
            raise ValueError("Kit member is missing or is a link: " + name)
        if digest(path) != expected:
            raise ValueError("Kit checksum mismatch: " + name)
    for name, expected in RELEASES.items():
        if records[name] != expected:
            raise ValueError("Unexpected pinned release checksum: " + name)
    return len(records)


def extract_release(archive, parent):
    """Validate every ZIP member before extraction into an exclusive directory."""
    expected_root = archive.stem
    with zipfile.ZipFile(archive) as bundle:
        names = set()
        members = bundle.infolist()
        if len(members) > 10000 or sum(m.file_size for m in members) > 100 * 1024 * 1024:
            raise ValueError("Release exceeds extraction bounds")
        for member in members:
            path = PurePosixPath(member.filename)
            mode = (member.external_attr >> 16) & 0xFFFF
            if (path.is_absolute() or not path.parts or path.parts[0] != expected_root
                    or ".." in path.parts or "\\" in member.filename or ":" in member.filename
                    or any(part.endswith((".", " ")) for part in path.parts)
                    or stat.S_ISLNK(mode) or member.filename.casefold() in names):
                raise ValueError("Unsafe/duplicate release ZIP member: " + member.filename)
            names.add(member.filename.casefold())
        if bundle.testzip() is not None:
            raise ValueError("Release ZIP CRC check failed")
        target = parent / expected_root
        target.mkdir(exist_ok=False)
        bundle.extractall(parent)
    return target


def child_env(output):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("GIT_", "CONFIGBACKUP_", "PYTHON"))}
    scratch = output / "scratch"
    scratch.mkdir(exist_ok=True)
    empty = scratch / "empty-git-config"
    empty.write_text("", encoding="utf-8")
    templates = scratch / "empty-git-template"
    templates.mkdir(exist_ok=True)
    env.update(TMP=str(scratch), TEMP=str(scratch), TMPDIR=str(scratch),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(empty),
               GIT_TEMPLATE_DIR=str(templates), GIT_TERMINAL_PROMPT="0",
               PYTHONDONTWRITEBYTECODE="1")
    return env


def command(args, output, log_name, env, timeout=240):
    with (output / log_name).open("wb") as log:
        result = subprocess.run(args, cwd=output, env=env, stdout=log,
                                stderr=subprocess.STDOUT, shell=False, timeout=timeout)
    return result.returncode


def package_check():
    checks = []
    try:
        import yaml
        version = importlib.metadata.version("PyYAML")
        good = version.split(".")[0] == "6" and yaml.safe_load("ok: true") == {"ok": True}
        checks.append({"name": "PyYAML 6.x", "status": "PASS" if good else "INCOMPLETE",
                       "detail": "Installed: " + version})
    except Exception as exc:
        checks.append({"name": "PyYAML 6.x", "status": "INCOMPLETE", "detail": str(exc)})
    try:
        from ruamel.yaml import YAML
        version=importlib.metadata.version('ruamel.yaml')
        valid=version.startswith('0.18.') and YAML(typ='rt').load('ok: true')['ok'] is True
        checks.append({'name':'ruamel.yaml 0.18.x','status':'PASS' if valid else 'INCOMPLETE','detail':'Installed: '+version})
    except Exception as exc:
        checks.append({'name':'ruamel.yaml 0.18.x','status':'INCOMPLETE','detail':str(exc)})
    try:
        # The shipped fixtures exercise both zones. Windows normally needs tzdata.
        for zone in ("America/New_York", "UTC"):
            ZoneInfo(zone)
        checks.append({"name": "IANA time zone data", "status": "PASS", "detail": "New York and UTC loaded"})
    except Exception as exc:
        checks.append({"name": "IANA time zone data", "status": "INCOMPLETE", "detail": str(exc)})
    return checks


def probe_interpreter(python):
    result = subprocess.run([str(python), "-I", "-B", str(Path(__file__).resolve()), "--dependency-probe"],
                            capture_output=True, text=True, encoding="utf-8", timeout=45, shell=False)
    if result.returncode:
        raise RuntimeError("Dependency probe failed (exit %s)" % result.returncode)
    return json.loads(result.stdout)


def windows_details():
    if os.name != "nt":
        return {"windows": False, "gpresult": None}
    import ctypes
    buffer = ctypes.create_unicode_buffer(32788)
    gpresult = None
    if ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
        for directory in ("Sysnative", "System32"):
            path = Path(buffer.value) / directory / "gpresult.exe"
            if path.is_file():
                gpresult = str(path)
                break
    return {"windows": True, "elevated": bool(ctypes.windll.shell32.IsUserAnAdmin()),
            "gpresult": gpresult}


def fixture_summary(result, windows):
    unexpected_skips = [(test.id(), reason) for test, reason in result.skipped
                        if not (windows and test.id().endswith(".test_live_cli_refuses_non_windows"))]
    status = ("FAIL" if not result.wasSuccessful() else "INCOMPLETE" if
              result.testsRun != EXPECTED_GPO_FIXTURES or unexpected_skips else "PASS")
    return {"status": status, "tests_run": result.testsRun, "passed": result.passed,
            "skipped": [(test.id(), reason) for test, reason in result.skipped],
            "failures": [(test.id(), message) for test, message in result.failures],
            "errors": [(test.id(), message) for test, message in result.errors]}


def fixture_worker(root, result_path):
    sys.path.insert(0, str(root))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))

    class Result(unittest.TextTestResult):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.passed = []

        def addSuccess(self, test):
            super().addSuccess(test)
            self.passed.append(test.id())

    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=Result)
    result = runner.run(suite)
    payload = fixture_summary(result, os.name == "nt")
    write_json(result_path, payload)
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}[payload["status"]]


def checked_result(path, code, allowed):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") not in allowed or allowed[data["status"]] != code:
        raise ValueError("Result status disagrees with process exit code")
    return data


def overall(data):
    statuses = [row["status"] for row in data["prerequisites"] + data["stages"]]
    if "FAIL" in statuses:
        return "FAIL"
    if any(s in ("INCOMPLETE", "NOT RUN") for s in statuses) or len(data["stages"]) != 3 + bool(data.get("requested_components")):
        return "INCOMPLETE"
    return "REVIEW" if "REVIEW" in statuses else "PASS"


def safe_link(output, relative, label):
    path = (output / relative).resolve()
    if not path.is_relative_to(output.resolve()) or not path.is_file():
        return html.escape(label + " (file unavailable)")
    return '<a href="' + html.escape(quote(relative, safe="/"), quote=True) + '">' + html.escape(label) + '</a>'


def render(data, output):
    esc = lambda v: html.escape(str(v), quote=True)
    table = lambda rows: ("<table><thead><tr><th>Check</th><th>Result</th><th>Details</th></tr></thead><tbody>" +
                         "".join("<tr><td>" + esc(r["name"]) + "</td><td><b>" + esc(r["status"]) +
                                 "</b></td><td>" + esc(r.get("detail", "")) + "</td></tr>" for r in rows) + "</tbody></table>")
    pieces = ["<!doctype html><html lang='en'><head><meta charset='utf-8'>",
              "<meta name='viewport' content='width=device-width, initial-scale=1'>",
              "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">",
              "<title>ConfigBackup safe test report</title><style>body{font:16px system-ui,sans-serif;color:#182433;background:#f3f5f8;max-width:1100px;margin:36px auto;padding:0 22px}section{background:white;border:1px solid #d2d9e3;border-radius:8px;padding:20px;margin:20px 0}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #d2d9e3;padding:10px;vertical-align:top;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f6;padding:12px}a{color:#145b9f}h1,h2{line-height:1.25}li{margin:8px 0}.status{font-size:24px;font-weight:bold}</style></head><body>",
              "<h1>ConfigBackup safe workstation tests</h1><p class='status'>" + esc(data["status"]) + "</p>",
              "<p>Kit " + KIT_VERSION + " · Started " + esc(data["started_at"]) + " · Updated " + esc(data["updated_at"]) + "</p>",
              "<p>" + esc(data["progress"]) + "</p>",
              "<p>This is the single review report. Supporting logs, raw exports and full diffs are kept beside it. Nothing is uploaded or emailed. Keep this entire folder local under your data retention and access rules.</p>",
              "<section><h2>How to interpret this result</h2><p>PASS means the selected checks completed successfully. It does not certify complete policy coverage or approve a production cutover. REVIEW means captured configuration changed. INCOMPLETE means a prerequisite, scope or test was unavailable or not run. FAIL means a fixture, integrity check or runner failed. Always review every row; an incomplete stage can also contain changes.</p>",
              "<ol><li>Confirm the machine and account below, especially if you ran as a different administrator.</li><li>Check prerequisites, every test result, and both GPO scopes in all three captures.</li><li>Confirm a few expected GPOs and settings in the saved XML even when repeatability passes. GPO node counts are not counts of applied policies.</li><li>Review every normalized change. Raw timestamps can change while configuration stays identical. Background policy refresh can still happen.</li><li>See WINDOWS-TESTING.md for platform limits and optional acceptance checks. The launcher does not test or update your production installation.</li></ol></section>",
              "<section><h2>Environment</h2><pre>" + esc(json.dumps(data["machine"], indent=2)) + "</pre></section>",
              "<section><h2>Prerequisites</h2>" + table(data["prerequisites"]) + "</section>",
              "<section><h2>Test stages</h2>" + table(data["stages"]) + "</section>"]
    offline = data.get("offline", {})
    if offline:
        rows = [dict(name=r["check"], status=r["status"], detail=r.get("reason", "")) for r in offline.get("results", [])]
        pieces += ["<section><h2>Application safety and scheduling fixtures</h2>", table(rows),
                   "<p>" + safe_link(output, "offline-checks/test-log.txt", "Full fixture log") + "</p></section>"]
    fixtures = data.get("gpo_fixtures", {})
    if fixtures:
        pieces += ["<section><h2>GPO fixture suite</h2><p>Passed: " + str(len(fixtures.get("passed", []))) +
                   "; skipped: " + str(len(fixtures.get("skipped", []))) + "; attempted: " + str(fixtures.get("tests_run", 0)) + ".</p>"]
        for field in ("skipped", "failures", "errors"):
            if fixtures.get(field):
                pieces.append("<h3>" + esc(field.title()) + "</h3><pre>" + esc(json.dumps(fixtures[field], indent=2)) + "</pre>")
        pieces += ["<details><summary>Passed test names</summary><pre>" + esc("\n".join(fixtures.get("passed", []))) + "</pre></details></section>"]
    if data.get("setup"):
        pieces.append("<section><h2>Optional prerequisite setup</h2>" + table(data["setup"]) +
                      "<p>These are installation attempts and consent decisions. The prerequisite/test results above reflect fresh checks afterward. Installation alone is not a test pass.</p></section>")
    if data.get("components") or data.get("requested_components") or data.get("requested_details"):
        pieces.append("<section><h2>Optional Windows component comparison</h2><p>Selected live tests compare two fresh read-only captures. Check the stage status above for tests that could not run. Missing, failed or disabled scopes are INCOMPLETE. Review actual coverage; this is configuration documentation, not a service/data recovery backup.</p>")
        pieces.append("<p>Requested components: " + esc(", ".join(data.get("requested_components", []))) + ". Detail bundles: " + esc(", ".join(data.get("requested_details", [])) or "none") + ".</p>")
        for row in data.get("components", {}).get("scopes", []):
            pieces.append("<details><summary>" + esc(row["status"] + " — " + row["scope"]) + "</summary>")
            for number, capture in enumerate(row["captures"], 1):
                pieces.append("<p>Capture " + str(number) + ": " + esc(capture["status"]) + ". " + esc(capture.get("error", "")) + "</p>")
                for name in capture.get("files", {}):
                    pieces.append("<p>" + safe_link(output, "component-run-%d/" % number + name, "Configuration JSON") + "</p>")
            if row.get("diff_file"):
                pieces.append("<pre>" + esc(row["diff_preview"]) + "</pre><p>" + safe_link(output, row["diff_file"], "Full difference") + "</p>")
            pieces.append("</details>")
        pieces.append("</section>")
    gpo = data.get("gpo", {})
    if gpo:
        pieces += ["<section><h2>Live GPO repeatability</h2><pre>" + esc(json.dumps(gpo.get("machine", {}), indent=2)) + "</pre>"]
        if gpo.get("fatal_error"):
            pieces.append("<pre>" + esc(gpo["fatal_error"]) + "</pre>")
        for scope in gpo.get("comparisons", []):
            pieces.append("<h3>" + esc(scope["scope"]) + " — " + esc(scope["status"]) + "</h3>")
            for pair in scope.get("pairs", []):
                pieces.append("<p>Run " + str(pair["compared_run"]) + ": configuration " +
                              ("identical" if pair["config_identical"] else "CHANGED") + "; raw XML " +
                              ("identical" if pair["raw_identical"] else "changed") + ".</p>")
                if pair.get("diff_preview"):
                    pieces.append("<pre>" + esc(pair["diff_preview"]) + "</pre><p>" +
                                  safe_link(output, "gpo/" + pair["diff_file"], "Full XML path diff") + "</p>")
                if pair.get("diff_error"):
                    pieces.append("<pre>" + esc(pair["diff_error"]) + "</pre>")
        for run in gpo.get("runs", []):
            pieces.append("<h3>Capture " + str(run["number"]) + "</h3>")
            if run.get("error"):
                pieces.append("<pre>" + esc(run["error"]) + "</pre>")
            for item in run.get("scopes", []):
                pieces.append("<p><b>" + esc(item["path"]) + ": " + ("validated" if item["usable"] else "INCOMPLETE") + "</b></p>")
                if not item["usable"]:
                    pieces.append("<pre>" + esc(item.get("validation_error") or item.get("error") or item["status"]) + "</pre>")
                    continue
                prefix = "gpo/run-%02d/" % run["number"]
                pieces.append("<p>" + safe_link(output, prefix + item["path"] + "/report.xml", "Normalized XML") + " · " +
                              safe_link(output, prefix + "telemetry/rsop/" + item["path"].rsplit("/", 1)[-1] + "/report.xml", "Original XML") + "</p>")
                pieces.append("<details><summary>Policy names, context and hashes</summary><pre>" + esc(json.dumps({
                    key: item.get(key) for key in ("gpo_names_for_review", "gpo_xml_node_count", "context", "raw_sha256",
                                                 "normalized_sha256", "normalization_idempotent")}, indent=2)) + "</pre></details>")
            pieces.append("<details><summary>Command diagnostics</summary><pre>" + esc(json.dumps(run.get("commands", []), indent=2)) + "</pre></details>")
        pieces.append("</section>")
    pieces.append("<section><h2>Next steps and supporting files</h2><ul>")
    for note in data["guidance"]:
        pieces.append("<li>" + esc(note) + "</li>")
    pieces.append("</ul>")
    for relative in ("report.json", "offline-console.txt", "gpo-fixtures.log", "gpo-console.txt", "runner-error.txt", "setup-venv.log", "setup-pip.log", "setup-git-probe.log", "component-comparison.json", "component-run-1.log", "component-run-2.log"):
        if (output / relative).is_file():
            pieces.append("<p>" + safe_link(output, relative, relative) + "</p>")
    pieces.append("<p>Tests do not refresh policy, change services/tasks, connect to SQL, touch production archives/Git, or send notifications. Optional prerequisite installation requires consent and may download software; its actions are listed above. Synthetic tests create temporary archives and local Git commits only. This is local effective RSoP, not a domain GPO/SYSVOL or complete Intune/MDM backup.</p></section></body></html>")
    return "".join(pieces)


def save_report(data, output):
    data["updated_at"] = now()
    data["status"] = overall(data)
    write_json(output / "report.json", data)
    temporary = output / "report.html.tmp"
    temporary.write_text(render(data, output), encoding="utf-8")
    temporary.replace(output / "report.html")


def run_suite(kit, output, *, offline_only=False, users=(), execute=command, offer_install=False, wheelhouse=None, components=(), details=()):
    data = {"kit": KIT_VERSION, "started_at": now(), "progress": "Starting; unfinished stages are not a pass.",
            "machine": {"computer": platform.node(), "platform": platform.platform(),
                        "python": sys.version, "python_executable": sys.executable,
                        "isolated_python": bool(sys.flags.isolated), "output": str(output)},
            "prerequisites": [], "stages": [], "guidance": [], "setup": [], "requested_components": list(components), "requested_details": list(details)}
    save_report(data, output)
    try:
        count = verify_kit(kit)
        data["prerequisites"].append(dict(name="Kit integrity", status="PASS", detail=f"Verified {count} files and pinned release ZIP hashes; not a publisher signature."))
        test_python = sys.executable
        if os.environ.get("CONFIGBACKUP_TEST_PYTHON_INSTALL_EXIT"):
            data["setup"].append(dict(name="Python bootstrap", status="REVIEW", detail="WinGet Python installation was requested in the launcher; exit " + os.environ["CONFIGBACKUP_TEST_PYTHON_INSTALL_EXIT"][:24] + ". This controller is now running; review its recorded version/path."))
        if offer_install:
            spec = importlib.util.spec_from_file_location("cb_prerequisite_setup", kit / "prerequisite_setup.py")
            setup = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(setup)
            def record(event):
                data["setup"].append(event)
                data["progress"] = "Prerequisite setup: " + event["name"] + " / " + event["status"]
                save_report(data, output)
            test_python = setup.prepare(output, probe_interpreter, record, wheelhouse=wheelhouse)
        installed = output / "staged"
        installed.mkdir()
        roots = {name: extract_release(kit / name, installed) for name in RELEASES}
        release, gpo = roots.values()
        env = child_env(output)
        data["prerequisites"].append(dict(name="Python 3.10+", status="PASS", detail=sys.version.split()[0]))
        if test_python == sys.executable:
            packages = package_check()
        else:
            probe = probe_interpreter(test_python)
            packages = probe["checks"]
            data["machine"]["test_python_executable"] = probe["executable"]
            data["machine"]["test_python_version"] = probe["python"]
        data["prerequisites"].extend(packages)
        git = shutil.which("git")
        data["prerequisites"].append(dict(name="Git for 25 synthetic checks", status="PASS" if git else "INCOMPLETE", detail=git or "Not on PATH; 25 checks will be NOT RUN."))
        win = windows_details()
        windows = win["windows"]
        data["prerequisites"].append(dict(name="Windows for live GPO", status="PASS" if windows else "INCOMPLETE", detail=platform.system()))
        if windows:
            data["machine"].update(win)
            data["prerequisites"].append(dict(name="Windows gpresult", status="PASS" if win["gpresult"] else "INCOMPLETE",
                                               detail=win["gpresult"] or "gpresult.exe unavailable in Windows system directory."))
            if not win["elevated"]:
                data["guidance"].append("The process is not elevated. Computer GPO capture may be denied; user capture still runs. To include computer results, rerun the launcher using Run as administrator if permitted. No automatic elevation occurs.")
        if users:
            data["guidance"].append("Requested additional user: " + users[0] + ". Check that this is your intended normal account; no password is requested.")
        data["guidance"].append("User GPO is the account running this process. When using a different admin account, set CONFIGBACKUP_TEST_USER to your normal DOMAIN\\user and rerun; confirm the recorded identity.")
        if not all(row["status"] == "PASS" for row in packages):
            data["guidance"].append("Use an approved Python environment with PyYAML 6.x, ruamel.yaml 0.18.x and IANA tzdata, or rerun with --offer-install. See LAUNCHER-GUIDE.txt for isolated venv/offline-wheel options.")
        if not git:
            data["guidance"].append("Install/use your approved Git for Windows on PATH and rerun to include all 25 Git-dependent fixtures.")

        def stage(name, action):
            data["progress"] = "Running: " + name
            print(data["progress"], flush=True)
            save_report(data, output)
            try:
                row = action()
            except Exception as exc:
                row = dict(status="FAIL", detail=type(exc).__name__ + ": " + str(exc))
            row["name"] = name
            data["stages"].append(row)
            save_report(data, output)

        def offline():
            if not all(row["status"] == "PASS" for row in packages):
                return dict(status="NOT RUN", detail="Application dependencies unavailable. GPO stages continue independently.")
            code = execute([test_python, "-I", "-B", str(kit / "Run-OfflineChecks.py"),
                            "--release", str(release), "--output", str(output / "offline-checks")],
                           output, "offline-console.txt", env)
            result = checked_result(output / "offline-checks/results.json", code, {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3})
            data["offline"] = result
            if result["status"] == "PASS" and (len(result.get("results", [])) != EXPECTED_CHECKS or
                                               any(row["status"] != "PASS" for row in result["results"])):
                raise ValueError("Incomplete application fixture list reported PASS")
            return dict(status=result["status"], detail="Synthetic archives, recovery, local Git, secrets and scheduling. " + result.get("error", ""))

        def fixtures():
            code = execute([test_python, "-I", "-B", str(kit / "Run-WorkstationTests.py"),
                            "--fixture-worker", str(gpo), "--fixture-result", str(output / "gpo-fixtures.json")],
                           output, "gpo-fixtures.log", env)
            result = checked_result(output / "gpo-fixtures.json", code, {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3})
            data["gpo_fixtures"] = result
            return dict(status=result["status"], detail=f"{len(result['passed'])} passed; {len(result['skipped'])} skipped; {result['tests_run']} attempted. No live policy queries in this stage.")

        def live():
            if offline_only or not windows:
                return dict(status="NOT RUN", detail="Explicit offline-only run." if offline_only else "Windows gpresult is unavailable on this platform.")
            if not win["gpresult"]:
                return dict(status="NOT RUN", detail="gpresult.exe is unavailable. Fixture results are retained.")
            args = [test_python, "-I", "-B", str(gpo / "test_gpo_collection.py"),
                    "--runs", "3", "--interval", "30", "--output", str(output / "gpo")]
            # The original harness imports its sibling adapter; -I intentionally
            # removes that path. Use a fixed bootstrap restoring only this bundle.
            args = [test_python, "-I", "-B", "-c",
                    "import runpy,sys; from pathlib import Path; p=sys.argv.pop(1); sys.path.insert(0,str(Path(p).parent)); runpy.run_path(p,run_name='__main__')",
                    *args[3:]]
            for user in users:
                args += ["--user", user]
            # Each gpresult call has the harness's 120s timeout. Avoid killing
            # its parent and leaving a gpresult child behind with an outer timer.
            code = execute(args, output, "gpo-console.txt", env, timeout=None)
            result = checked_result(output / "gpo/report.json", code, {"PASS": 0, "REVIEW": 2, "INCOMPLETE": 3})
            data["gpo"] = result
            return dict(status=result["status"], detail="Three captures, 30 seconds apart; computer, process user and any requested additional user. Review actual policy coverage locally.")

        stage(f"Application fixtures ({EXPECTED_CHECKS})", offline)
        stage("GPO fixtures (42)", fixtures)
        stage("Read-only live GPO repeatability", live)
        if components:
            def component_captures():
                if offline_only or not windows:
                    return dict(status="NOT RUN", detail="Component captures require a live Windows run.")
                spec = importlib.util.spec_from_file_location("cb_component_repeat", kit / "component_repeat.py")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                data["components"] = module.capture(release, output, components, test_python, env, execute, details=details)
                return dict(status=data["components"]["status"], detail="Two captures: " + ", ".join(components) + ". Details: " + (", ".join(details) or "none") + ". Individual role/query failures preserve all other results; Windows roles are never installed automatically.")
            stage("Optional Windows component repeatability", component_captures)
        data["progress"] = "Finished. Review all statuses and scope details below."
    except (Exception, KeyboardInterrupt) as exc:
        (output / "runner-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        data["stages"].append(dict(name="Runner stopped", status="FAIL" if not isinstance(exc, KeyboardInterrupt) else "INCOMPLETE", detail=type(exc).__name__ + ": " + str(exc)))
        data["progress"] = "Stopped early. Previously saved results are retained."
    save_report(data, output)
    return data


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="New local, nonsynced results folder; never overwritten")
    parser.add_argument("--offline-only", action="store_true", help="Skip live GPO; overall report remains INCOMPLETE")
    parser.add_argument("--no-open", action="store_true", help="Do not open the HTML report automatically")
    parser.add_argument("--user", help="Additional normal Windows user; no password. Or CONFIGBACKUP_TEST_USER.")
    parser.add_argument("--offer-install", action="store_true", help="Offer optional prerequisite installation; never install without interactive consent")
    parser.add_argument("--wheelhouse", help="Approved local wheel folder offered for offline package setup")
    parser.add_argument("--windows-component", action="append", default=[], choices=WINDOWS_COMPONENTS, help="Optional two-capture role test; repeat to select several")
    parser.add_argument("--windows-detail", action="append", default=[], choices=tuple(WINDOWS_DETAILS), help="Optional deeper query bundle; implies its component")
    parser.add_argument("--offer-components", action="store_true", help="Ask which optional Windows components to test (default none)")
    parser.add_argument("--dependency-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error("Python 3.10+ is required")
    if args.dependency_probe:
        print(json.dumps({"checks": package_check(), "python": sys.version, "executable": sys.executable}, ensure_ascii=True))
        return 0
    if args.fixture_worker:
        if not args.fixture_result:
            parser.error("Missing fixture result path")
        return fixture_worker(args.fixture_worker, args.fixture_result)
    details=list(dict.fromkeys(args.windows_detail))
    components = list(dict.fromkeys([*args.windows_component,*(WINDOWS_DETAILS[d] for d in details)]))
    if args.offer_components and not components and os.name == "nt" and not args.offline_only:
        print("Optional role configuration test: iis, dns, dhcp, hyperv, certificates, dfs, nps, wef, wsus, adcs. Each selected role is captured twice locally.")
        print("No Windows role is installed/enabled. Missing modules or permissions are reported as incomplete.")
        try:
            selection = input("Components (comma-separated; Enter skips all): ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            selection = ""
        selected = [c.strip() for c in selection.split(",") if c.strip()]
        if all(c in WINDOWS_COMPONENTS for c in selected):
            components = list(dict.fromkeys(selected))
        else:
            print("Unknown selection; optional role tests skipped. Use explicit --windows-component flags to retry.")
    user = args.user or os.environ.get("CONFIGBACKUP_TEST_USER")
    if user and (not user.strip() or user.startswith(("/", "-")) or any(ord(c) < 32 for c in user)):
        parser.error("Additional user must be an account name, without control characters or leading / or -")
    try:
        output = new_output(args.output)
    except Exception as exc:
        print("Cannot create a new local results directory: " + str(exc), file=sys.stderr)
        print("No tests ran; no new report could be saved. Existing folders are never overwritten.", file=sys.stderr)
        return 3
    print("Local report: " + str(output / "report.html"), flush=True)
    data = run_suite(Path(__file__).resolve().parent, output, offline_only=args.offline_only, users=[user] if user else [], offer_install=args.offer_install, wheelhouse=args.wheelhouse, components=components, details=details)
    print(data["status"] + ": " + str(output / "report.html"), flush=True)
    if os.name == "nt" and not args.no_open:
        try:
            os.startfile(str(output / "report.html"))
        except OSError as exc:
            print("Open report.html manually; browser launch failed: " + str(exc))
    return {"PASS": 0, "FAIL": 1, "REVIEW": 2, "INCOMPLETE": 3}[data["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
