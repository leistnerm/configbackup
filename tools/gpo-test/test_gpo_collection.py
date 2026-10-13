#!/usr/bin/env python3
"""Local Windows GPO collection repeatability test; Python 3.10+, no pip packages."""
from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import datetime as dt
import difflib
import functools
import getpass
import hashlib
import html
import json
import locale
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import types
from urllib.parse import quote
import uuid
import xml.etree.ElementTree as ET

from collector_adapter import load_collector, SOURCE_HASHES

VERSION = "1.0.0"
MAX_XML_BYTES = 64 * 1024 * 1024


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def local_name(tag):
    return str(tag).rsplit("}", 1)[-1]


def parse_xml(raw):
    if len(raw) > MAX_XML_BYTES:
        raise ValueError("XML exceeds the test's 64 MiB limit")
    # gpresult does not need DTDs. Also catch UTF-16/32 representations.
    compact = raw.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in compact or b"<!ENTITY" in compact:
        raise ValueError("Unexpected XML DTD/entity declaration")
    return ET.fromstring(raw)


def decode_console(raw):
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode(locale.getpreferredencoding(False), errors="replace")


class LocalGpresult:
    """Allow only local XML report queries; retain diagnostics in this fresh run."""

    def __init__(self, executable, run_root, execute=subprocess.run):
        self.executable = str(executable)
        self.root = Path(run_root)
        self.execute = execute
        self.calls = []

    def run(self, args, *, capture_output, timeout):
        # Only six arguments, or eight with /user. No remote target, alternate
        # credentials, HTML command, or shell is permitted.
        valid = (isinstance(args, list) and len(args) in (6, 8)
                 and args[:2] == ["gpresult", "/scope"]
                 and args[2] in ("computer", "user") and args[3] == "/x"
                 and args[5] == "/f" and capture_output is True and timeout == 120)
        if isinstance(args, list) and len(args) == 8:
            valid = valid and args[2] == "user" and args[6] == "/user" and bool(args[7])
        if not valid:
            raise ValueError("Refused command outside the local gpresult XML allowlist")
        output = Path(args[4]).resolve()
        if not output.is_relative_to((self.root / "scratch").resolve()) or output.exists():
            raise ValueError("gpresult output must be a new file in this run's scratch folder")
        record = {"scope": args[2], "requested_user": args[7] if len(args) == 8 else None,
                  "command": [self.executable, *args[1:]], "started_at": now()}
        diag = self.root / "diagnostics" / ("command-%02d" % (len(self.calls) + 1))
        diag.mkdir(parents=True)
        started = time.monotonic()
        try:
            cp = self.execute(record["command"], capture_output=True, timeout=120, shell=False)
            record["returncode"] = cp.returncode
            for name, raw in (("stdout", cp.stdout), ("stderr", cp.stderr)):
                raw = raw or b""
                (diag / (name + ".bin")).write_bytes(raw)
                record[name] = decode_console(raw)[:4000]
            if output.is_file():
                if output.stat().st_size > MAX_XML_BYTES:
                    raise ValueError("XML exceeds the test's 64 MiB limit; export was not retained")
                raw = output.read_bytes()
                (diag / "raw.xml").write_bytes(raw)
                record["raw_xml"] = (diag / "raw.xml").relative_to(self.root).as_posix()
                if cp.returncode == 0:
                    parse_xml(raw)
            return cp
        except subprocess.TimeoutExpired as exc:
            record["error"] = "gpresult timed out after 120 seconds"
            for name, raw in (("stdout", exc.stdout), ("stderr", exc.stderr)):
                (diag / (name + ".bin")).write_bytes(raw or b"")
                record[name] = decode_console(raw or b"")[:4000]
            raise
        except Exception as exc:
            record["error"] = str(exc)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            record["diagnostics"] = diag.relative_to(self.root).as_posix()
            self.calls.append(record)
            write_json(diag / "command.json", record)


def windows_binary(name):
    # Get the OS directory from Windows, not PATH or a caller-controlled variable.
    buffer = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
        raise OSError("Cannot determine Windows directory")
    root = Path(buffer.value)
    for directory in ("Sysnative", "System32"):
        path = root / directory / name
        if path.is_file():
            return path
    raise FileNotFoundError(name + " was not found in the Windows system directory")


def machine_context():
    context = {"computer": platform.node(), "windows": platform.platform(),
               "architecture": platform.machine(), "python": sys.version,
               "python_executable": sys.executable, "python_utf8_mode": sys.flags.utf8_mode,
               "text_encoding": locale.getpreferredencoding(False),
               "fallback_user_name": getpass.getuser(),
               "elevated": bool(ctypes.windll.shell32.IsUserAnAdmin())}
    try:
        cp = subprocess.run([str(windows_binary("whoami.exe")), "/user", "/fo", "csv", "/nh"],
                            capture_output=True, timeout=15, shell=False)
        context["process_identity_csv"] = decode_console(cp.stdout).strip()
        context["identity_command_exit"] = cp.returncode
    except Exception as exc:
        context["identity_error"] = str(exc)
    return context


def create_output(value=None):
    if value is None:
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ConfigBackup" / "GpoTests"
        value = base / (dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    if str(value).startswith(("\\\\", "//")):
        raise ValueError("Choose a local output directory, not a UNC/network path")
    result = Path(value).expanduser().resolve()
    if str(result).startswith(("\\\\", "//")):
        raise ValueError("Output resolves to a network path")
    if os.name == "nt" and ctypes.windll.kernel32.GetDriveTypeW(str(result.anchor)) == 4:
        raise ValueError("Output is on a mapped network drive; choose a local directory")
    result.parent.mkdir(parents=True, exist_ok=True)
    result.mkdir(exist_ok=False)
    return result


def xml_details(raw, kind):
    root = parse_xml(raw)
    if local_name(root.tag).lower() != "rsop":
        raise ValueError("Expected an RSoP XML document")
    expected = kind.title() + "Results"
    result_nodes = [child for child in root if local_name(child.tag) == expected]
    if len(result_nodes) != 1 or not (len(result_nodes[0]) or (result_nodes[0].text or "").strip()):
        raise ValueError("Missing, empty, or duplicate " + expected + " element")
    names = []
    for node in result_nodes[0].iter():
        if local_name(node.tag).lower() == "gpo":
            names.append(next(((child.text or "").strip() for child in node
                               if local_name(child.tag).lower() == "name"), "(unnamed GPO node)"))
    return {"results_element": expected, "gpo_xml_node_count": len(names),
            "gpo_names_for_review": names,
            "root_creation_time_elements": sum(local_name(c.tag) == "CreationTime" for c in root)}


def verify_scope(run_root, item, normalize):
    result = dict(item)
    result["usable"] = False
    if item["status"] != "complete":
        return result
    try:
        base = Path(run_root)
        scope = item["path"]
        expected = {scope + "/report.xml", scope + "/context.json"}
        if set(item["files"]) != expected:
            raise ValueError("Collector manifest does not certify both expected files")
        for name, expected_hash in item["files"].items():
            if sha256(base / name) != expected_hash:
                raise ValueError("Collector manifest hash mismatch: " + name)
        identity = scope.rsplit("/", 1)[-1]
        raw_path = base / "telemetry" / "rsop" / identity / "report.xml"
        raw = raw_path.read_bytes()
        context = json.loads((base / scope / "context.json").read_text())
        normalized = (base / scope / "report.xml").read_text()
        result.update(xml_details(raw, context["scope"]))
        if normalized != normalize(raw):
            raise ValueError("Saved configuration XML differs from the collector normalizer result")
        if normalize(normalized.encode("utf-8")) != normalized:
            raise ValueError("Normalization is not idempotent on this XML")
        result.update(usable=True, context=context, raw_sha256=sha256(raw_path),
                      normalized_sha256=sha256(base / scope / "report.xml"),
                      normalization_idempotent=True)
    except Exception as exc:
        result["validation_error"] = str(exc)
    return result


def xml_lines(raw):
    """Order-sensitive XML path listing for readable diffs, not normalization."""
    lines = []

    def visit(node, path):
        lines.append(path + " element")
        for key, value in sorted(node.attrib.items()):
            lines.append(path + "/@" + key + " = " + json.dumps(value, ensure_ascii=False))
        if node.text is not None:
            lines.append(path + "/text() = " + json.dumps(node.text, ensure_ascii=False))
        counts = Counter()
        for child in node:
            counts[child.tag] += 1
            child_path = path + "/" + child.tag + "[" + str(counts[child.tag]) + "]"
            visit(child, child_path)
            if child.tail is not None:
                lines.append(child_path + "/tail() = " + json.dumps(child.tail, ensure_ascii=False))

    root = parse_xml(raw)
    visit(root, "/" + root.tag + "[1]")
    return [line + "\n" for line in lines]


def compare_runs(output, runs):
    comparisons = []
    # Recheck retained files before comparing cached hashes. A removed/modified
    # export must not become a false PASS or prevent the rest of the report.
    for run in runs:
        for item in run["scopes"]:
            if not item["usable"]:
                continue
            try:
                base = Path(output) / ("run-%02d" % run["number"])
                for name, expected_hash in item["files"].items():
                    if sha256(base / name) != expected_hash:
                        raise ValueError("Retained configuration file changed after collection: " + name)
                raw_path = base / "telemetry/rsop" / item["path"].rsplit("/", 1)[-1] / "report.xml"
                if sha256(raw_path) != item["raw_sha256"]:
                    raise ValueError("Retained raw XML changed after collection")
            except Exception as exc:
                item.update(usable=False, validation_error=str(exc))
    wanted = sorted({scope["path"] for run in runs for scope in run["scopes"]})
    for scope in wanted:
        entries = [next((item for item in run["scopes"] if item["path"] == scope), None) for run in runs]
        result = {"scope": scope, "status": "INCOMPLETE", "pairs": []}
        if len(runs) >= 2 and all(item and item["usable"] for item in entries):
            result["status"] = "PASS"
            first = entries[0]
            for index, entry in enumerate(entries[1:], 2):
                pair = {"baseline_run": 1, "compared_run": index,
                        "config_identical": entry["files"] == first["files"],
                        "raw_identical": entry["raw_sha256"] == first["raw_sha256"]}
                if not pair["config_identical"]:
                    if result["status"] != "INCOMPLETE":
                        result["status"] = "REVIEW"
                    left = Path(output) / "run-01" / scope / "report.xml"
                    right = Path(output) / ("run-%02d" % index) / scope / "report.xml"
                    # Read using the collector's same locale; it wrote text without
                    # an explicit encoding. Diff files themselves are always UTF-8.
                    try:
                        diff = "".join(difflib.unified_diff(xml_lines(left.read_text().encode("utf-8")),
                                                          xml_lines(right.read_text().encode("utf-8")),
                                                          fromfile="run-01/" + scope,
                                                          tofile="run-%02d/" % index + scope))
                        if not diff:
                            diff = "XML path values are identical; check byte encoding, namespace serialization, or context.json.\n"
                        name = "diffs/" + scope.rsplit("/", 1)[-1] + "-01-vs-%02d.diff" % index
                        (Path(output) / "diffs").mkdir(exist_ok=True)
                        (Path(output) / name).write_text(diff, encoding="utf-8")
                        pair.update(diff_file=name, diff_preview=diff[:12000], diff_characters=len(diff))
                    except Exception as exc:
                        result["status"] = "INCOMPLETE"
                        pair["diff_error"] = str(exc)
                result["pairs"].append(pair)
        comparisons.append(result)
    return comparisons


def overall_status(runs, comparisons, fatal_error=None):
    if (fatal_error or len(runs) < 2 or not comparisons or any(run.get("error") for run in runs)
            or any(row["status"] == "INCOMPLETE" for row in comparisons)):
        return "INCOMPLETE"
    return "REVIEW" if any(row["status"] == "REVIEW" for row in comparisons) else "PASS"


def link(path, label=None):
    return '<a href="' + html.escape(quote(path, safe="/"), quote=True) + '">' + html.escape(label or path) + "</a>"


def render_report(data, output):
    esc = html.escape
    status = data["status"]
    pieces = ["<!doctype html><html lang='en'><meta charset='utf-8'>",
              "<meta name='viewport' content='width=device-width, initial-scale=1'>",
              "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'\">",
              "<title>ConfigBackup GPO test</title><style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 24px;color:#182433;background:#f5f7fa}h1,h2{line-height:1.2}section{background:white;padding:20px;margin:20px 0;border:1px solid #d5dce5;border-radius:8px}table{border-collapse:collapse;width:100%}td,th{text-align:left;border-bottom:1px solid #d5dce5;padding:10px;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef1f5;padding:12px;font-size:13px}a{color:#1757a1}code{overflow-wrap:anywhere}.PASS{color:#166534}.REVIEW{color:#8a4b00}.INCOMPLETE{color:#b42318}</style>",
              "<body><h1>ConfigBackup GPO collection test</h1>",
              '<h2 class="' + status + '">' + status + " — repeatability check</h2>",
              "<p>Generated " + esc(data["finished_at"]) + ". Test bundle " + VERSION + "; collector 1.0.0.</p>",
              "<p>PASS means the captured configuration files matched across these successful runs. It does not establish complete GPO coverage or future stability. REVIEW means configuration output changed. INCOMPLETE means at least one required collection or validation failed or was disabled.</p>",
              "<p>This report is local, uses no external assets, and sends no data. All accounts, policy names, XML, command diagnostics, and differences stay in the output folder. Treat that folder as sensitive configuration data.</p>",
              "<p>" + link("report.json", "Machine-readable report") + " · " + link("REVIEW-GUIDE.txt", "Review checklist") + "</p>"]
    if data.get("fatal_error"):
        pieces.append("<section><h2>Stopped early</h2><pre>" + esc(data["fatal_error"]) + "</pre></section>")
    pieces.append("<section><h2>Execution identity and environment</h2><pre>" + esc(json.dumps(data["machine"], indent=2)) + "</pre></section>")
    pieces.append("<section><h2>Scope comparisons</h2><table><tr><th>Scope</th><th>Result</th><th>Comparison to run 1</th></tr>")
    for row in data["comparisons"]:
        detail = []
        for pair in row["pairs"]:
            text = "Run %d: config %s; raw XML %s" % (pair["compared_run"], "identical" if pair["config_identical"] else "CHANGED", "identical" if pair["raw_identical"] else "changed")
            if pair.get("diff_file"):
                text = esc(text) + " · " + link(pair["diff_file"], "full XML path diff")
            else:
                text = esc(text)
            if pair.get("diff_error"):
                text += " · Diff failed: " + esc(pair["diff_error"])
            detail.append(text)
        pieces.append("<tr><td>" + esc(row["scope"]) + '</td><td class="' + row["status"] + '">' + row["status"] + "</td><td>" + "<br>".join(detail or ["See the scope failures below."]) + "</td></tr>")
    pieces.append("</table><p>Raw changes with identical configuration are expected when the root CreationTime changes or XML canonicalization removes representational differences. Policy dates, extension data, list order, and whitespace are preserved by the existing collector. Additional volatile fields can therefore produce REVIEW.</p></section>")
    for run in data["runs"]:
        prefix = "run-%02d/" % run["number"]
        pieces.append("<section><h2>Run %d</h2><p>%s</p>" % (run["number"], esc(run["started_at"])))
        if run.get("error"):
            pieces.append("<pre>" + esc(run["error"]) + "</pre>")
        for item in run["scopes"]:
            scope = item["path"]
            pieces.append("<h3>" + esc(scope) + " — " + ("validated" if item["usable"] else "INCOMPLETE") + "</h3>")
            if item["usable"]:
                identity = scope.rsplit("/", 1)[-1]
                pieces.append("<p>" + link(prefix + scope + "/report.xml", "Normalized XML") + " · " + link(prefix + "telemetry/rsop/" + identity + "/report.xml", "Original gpresult XML") + " · " + link(prefix + scope + "/context.json", "Scope context") + "</p>")
                pieces.append("<p>GPO XML nodes: %d (not an applied-policy count). Review names, applied/denied status, filters, and actual settings in the XML.</p>" % item["gpo_xml_node_count"])
                pieces.append("<pre>" + esc("\n".join(item["gpo_names_for_review"]) or "No GPO nodes found: check whether this is expected.") + "</pre>")
                pieces.append("<details><summary>SHA-256 and normalization check</summary><pre>" + esc(json.dumps({key: item[key] for key in ("raw_sha256", "normalized_sha256", "normalization_idempotent")}, indent=2)) + "</pre></details>")
            else:
                pieces.append("<pre>" + esc(item.get("validation_error") or item.get("error") or item["status"]) + "</pre>")
        pieces.append("<details><summary>gpresult command diagnostics</summary>")
        for command in run["commands"]:
            pieces.append("<pre>" + esc(json.dumps(command, indent=2, ensure_ascii=False)) + "</pre>")
            pieces.append("<p>" + link(prefix + command["diagnostics"] + "/command.json", "Command record") + "</p>")
        pieces.append("</details></section>")
    for row in data["comparisons"]:
        for pair in row["pairs"]:
            if pair.get("diff_preview"):
                pieces.append("<section><h2>Difference: " + esc(row["scope"]) + " / run %d</h2><pre>" % pair["compared_run"] + esc(pair["diff_preview"]) + "</pre><p>Preview limited to 12,000 characters. " + link(pair["diff_file"], "Read the full diff") + "</p></section>")
    pieces.append("<section><h2>What this test covers</h2><p>Local effective computer/user RSoP exported by gpresult, ConfigBackup normalization, per-scope success/failure, manifest file hashes, and observed repeatability. It does not back up domain GPO definitions or SYSVOL, fully inventory Intune/MDM policy, test policy application, or run the archive/Git/deletion pipeline. No policy refresh or policy changes are requested.</p><h2>Review steps</h2><ol><li>Confirm the computer and process account/SID. If elevated as another account, include your normal account with --user.</li><li>Check every scope has usable exports on every run. A repeated failure is not a stable result.</li><li>Open XML and confirm known policy names, applied/denied status, filtering, and settings. An XML node count cannot establish completeness.</li><li>Review every changed normalized XML path. A background policy refresh may legitimately change results; no policy refresh is triggered here.</li><li>Store the report with appropriate access controls.</li></ol></section></body></html>")
    (Path(output) / "report.html").write_text("".join(pieces), encoding="utf-8")


def run_test(output, *, runs, interval, users, machine, runner_factory, sleeper=time.sleep):
    output = Path(output)
    data = {"test_version": VERSION, "collector_version": "1.0.0", "source_sha256": SOURCE_HASHES,
            "started_at": now(), "machine": machine, "requested_runs": runs,
            "interval_seconds": interval, "requested_users": list(users), "runs": [], "comparisons": []}
    expected = ["policy/rsop/computer", "policy/rsop/user"] + [
        "policy/rsop/user-" + hashlib.sha256(user.encode()).hexdigest()[:16] for user in users]
    try:
        for number in range(1, runs + 1):
            root = output / ("run-%02d" % number)
            root.mkdir()
            (root / "scratch").mkdir()
            runner = runner_factory(root, number)
            temporary = types.SimpleNamespace(TemporaryDirectory=functools.partial(tempfile.TemporaryDirectory, dir=root / "scratch"))
            collector = load_collector(runner, temporary)
            record = {"number": number, "started_at": now(), "scopes": [], "commands": runner.calls}
            data["runs"].append(record)
            print("Collecting run %d/%d..." % (number, runs), flush=True)
            try:
                scopes, failures = collector["collect_rsop"](root, users)
                record["collector_failures"] = failures
                for scope in expected:
                    matches = [item for item in scopes if item["path"] == scope]
                    item = matches[0] if len(matches) == 1 else {
                        "path": scope, "status": "failed", "error": "Missing or duplicate collector scope", "files": {}}
                    record["scopes"].append(verify_scope(root, item, collector["normalize_rsop"]))
            except Exception as exc:
                record["error"] = str(exc)
                record["scopes"] = [{"path": scope, "status": "failed", "usable": False,
                                     "files": {}, "error": "Run did not finish: " + str(exc)} for scope in expected]
            record["finished_at"] = now()
            write_json(root / "run.json", record)
            if number < runs and interval:
                sleeper(interval)
        data["comparisons"] = compare_runs(output, data["runs"])
    except (Exception, KeyboardInterrupt) as exc:
        data["fatal_error"] = str(exc) or type(exc).__name__
        try:
            data["comparisons"] = compare_runs(output, data["runs"])
        except Exception as compare_error:
            data["fatal_error"] += "; comparison failed: " + str(compare_error)
    data["finished_at"] = now()
    data["status"] = overall_status(data["runs"], data["comparisons"], data.get("fatal_error"))
    shutil.copyfile(Path(__file__).with_name("REVIEW-GUIDE.txt"), output / "REVIEW-GUIDE.txt")
    write_json(output / "report.json", data)
    render_report(data, output)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=2, help="2 to 5 exports per scope (default: 2)")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between full runs, 0 to 120 (default: 15)")
    parser.add_argument("--user", action="append", default=[], help=r"Additional local RSoP user, e.g. DOMAIN\yourname; repeatable")
    parser.add_argument("--output", help="NEW local directory; existing paths and network shares are refused")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 or newer is required")
    if os.name != "nt":
        parser.error("Live GPO collection requires Windows. See tests/ for offline fixture tests.")
    if not 2 <= args.runs <= 5 or not 0 <= args.interval <= 120:
        parser.error("Use 2..5 runs and an interval of 0..120 seconds")
    if any(not user.strip() or user.startswith(("/", "-")) or any(ord(c) < 32 for c in user) for user in args.user):
        parser.error("Each --user must be an account name, without control characters or leading / or -")
    if len(args.user) > 10 or len(set(user.casefold() for user in args.user)) != len(args.user):
        parser.error("Use at most 10 distinct additional users")
    try:
        load_collector(None)  # Verify the entire source bundle before any queries.
        executable = windows_binary("gpresult.exe")
        context = machine_context()
        output = create_output(args.output)
    except Exception as exc:
        parser.error(str(exc))
    print("Local report folder: " + str(output), flush=True)
    if not context["elevated"]:
        print("Not elevated: computer-scope collection may fail; the report will show that failure.", flush=True)
    data = run_test(output, runs=args.runs, interval=args.interval, users=args.user, machine=context,
                    runner_factory=lambda root, number: LocalGpresult(executable, root))
    print(data["status"] + ": " + str(output / "report.html"), flush=True)
    return {"PASS": 0, "REVIEW": 2, "INCOMPLETE": 3}[data["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
