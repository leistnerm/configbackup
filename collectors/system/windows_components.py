"""Optional Windows role configuration, certified independently per query/object.

Unreported/disappeared objects are intentionally never certified as deletions.
Only top-level inventory rows are sorted. Nested ordered configuration is kept.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from completeness import publish, section
from sections import enabled

from collectors.system.optional_catalog import WINDOWS_COMPONENTS as COMPONENTS, WINDOWS_DETAILS
PREFIX = "windows-components/"


def canonical(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def write_records(root, component, records):
    root = Path(root)
    if not isinstance(records, list) or not records:
        raise ValueError("Reader returned no scope records")
    scopes, failures, names = [], [], set()
    for record in records:
        scope = record.get("scope", "")
        if (not re.fullmatch(r"windows-components/" + component + r"/[a-z0-9_/-]+", scope)
                or any(p in ("", ".", "..") for p in scope.split("/"))):
            raise ValueError("Invalid component scope")
        if scope in names or any(scope.startswith(n + "/") or n.startswith(scope + "/") for n in names):
            raise ValueError("Duplicate/overlapping component scopes")
        names.add(scope)
        if record.get("status") not in ("complete", "failed", "disabled"):
            raise ValueError("Invalid component status")
        if record["status"] == "complete" and "data" not in record:
            raise ValueError("Successful query omitted its payload")
    # Validate structure before writing. Per-scope encoding or I/O failures must
    # not stop other independent records or certify a partially written file.
    for record in records:
        scope, status = record["scope"], record["status"]
        if not enabled("configuration") or not enabled(scope):
            scopes.append(section(root, scope, "disabled", "Disabled by configuration"))
            continue
        if status == "complete":
            try:
                data = record["data"]
                if record.get("unordered_rows"):
                    if not isinstance(data, list):
                        raise ValueError("An unordered inventory must be an array")
                    # Only an explicitly identified set of inventory rows is
                    # sorted; DNS option lists, IIS rule ordering etc stay intact.
                    data = sorted(data, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False))
                path = root / scope / "configuration.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(canonical(data), encoding="utf-8")
                temporary.replace(path)
                scopes.append(section(root, scope))
                continue
            except Exception as exc:
                status = "failed"
                error = str(exc)
        else:
            error = record.get("error", "Disabled" if status == "disabled" else "Query failed")
        scopes.append(section(root, scope, status, error))
        if status == "failed":
            failures.append({"section": scope, "error": error})
    return scopes, failures


def collect(root, components=(), *, details=(), powershell=None, windows=None, execute=subprocess.run):
    root = Path(root)
    scopes, failures = [], []
    windows = os.name == "nt" if windows is None else windows
    if any(name not in WINDOWS_DETAILS for name in details):raise ValueError('Unknown optional component detail')
    for component in dict.fromkeys([*components,*(WINDOWS_DETAILS[name] for name in details)]):
        if component not in COMPONENTS:
            raise ValueError("Unsupported Windows component: " + component)
        scope = PREFIX + component
        if not enabled("configuration") or not enabled(scope):
            scopes.append(section(root, scope, "disabled", "Disabled by configuration"))
            continue
        try:
            if not windows:
                raise RuntimeError("This optional component requires Windows")
            # The inbox role modules are supported by Windows PowerShell. Prefer
            # it over pwsh rather than silently invoking a compatibility session.
            ps = powershell or shutil.which("powershell.exe") or shutil.which("pwsh")
            if not ps:
                raise RuntimeError("Windows PowerShell was not found")
            cp = execute([ps, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
                          str(Path(__file__).with_name("Read-WindowsComponents.ps1")),
                          *(['-DetailsJson',json.dumps(list(details))] if details else []), "-Component", component],
                         capture_output=True, timeout=300, shell=False)
            if cp.returncode:
                # Do not persist arbitrary stderr/serialized objects containing
                # unexpected secrets. Scope diagnostics are deliberately concise.
                raise RuntimeError("Component reader failed (exit %s); check module, rights and local role availability" % cp.returncode)
            payload = json.loads(cp.stdout.decode("utf-8-sig") if isinstance(cp.stdout, bytes) else cp.stdout)
            if payload.get("schema_version") != 1 or payload.get("component") != component:
                raise ValueError("Unexpected component reader envelope")
            new_scopes, new_failures = write_records(root, component, payload["records"])
            scopes.extend(new_scopes)
            failures.extend(new_failures)
        except Exception as exc:
            scopes.append(section(root, scope, "failed", str(exc)))
            failures.append({"section": scope, "error": str(exc)})
    if 'certificates' in components and enabled('telemetry/certificates'):
        from telemetry import envelope,dataset,write_envelope
        rows=[];now=dt.datetime.now(dt.timezone.utc)
        try:
            for entry in scopes:
                if entry['status']!='complete' or not entry['path'].startswith('windows-components/certificates/stores/'):continue
                for cert in json.loads((root/entry['path']/'configuration.json').read_text(encoding='utf-8')):
                    before=dt.datetime.fromisoformat(cert['not_before'].replace('Z','+00:00'));after=dt.datetime.fromisoformat(cert['not_after'].replace('Z','+00:00'))
                    rows.append({'source':cert['store'],'sha256_or_thumbprint':cert['thumbprint'],'days_remaining':(after-now).total_seconds()/86400,'not_yet_valid_count':int(before>now)})
            payload=envelope('certificates');dataset(payload,'certificate',rows,['source','sha256_or_thumbprint'],{'days_remaining':'days','not_yet_valid_count':'count'})
            payload['failures']=[x for x in failures if x['section'].startswith('windows-components/certificates/')]
            write_envelope(root/'telemetry/windows-certificate-health.json',payload)
        except Exception as exc:
            failures.append({'section':'telemetry/certificates','error':type(exc).__name__})
            scopes.append(section(root,'windows-components/certificates/telemetry-failure','failed','Certificate telemetry could not be written'))
    return scopes, failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.environ.get("CONFIGBACKUP_OUTPUT"))
    parser.add_argument("--component", action="append", choices=COMPONENTS, required=True)
    parser.add_argument('--detail',action='append',choices=WINDOWS_DETAILS,default=[])
    args = parser.parse_args(argv)
    if not args.output:
        parser.error("--output is required unless CONFIGBACKUP_OUTPUT is set")
    root = Path(args.output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        parser.error("Output must be empty")
    publish(root, [], finalized=False)
    scopes, failures = collect(root, args.component,details=args.detail)
    (root / "collection-errors.json").write_text(canonical(failures), encoding="utf-8")
    publish(root, scopes)
    print("%d scopes, %d failures. Review collection-manifest.json." % (len(scopes), len(failures)))
    return 6 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
