"""Two local role captures and certified-byte comparisons, without archive updates."""
from __future__ import annotations
import difflib
import hashlib
import json
from pathlib import Path
import re


def verified_scopes(root, wanted):
    root = Path(root)
    manifest = json.loads((root / "collection-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("finalized") is not True:
        raise ValueError("Missing finalized component manifest")
    records = manifest.get("sections")
    if not isinstance(records, list):
        raise ValueError("Missing sections")
    rows = {}
    for item in records:
        name = item.get("path", "")
        if not re.fullmatch(r"windows-components/(?:iis|dns|dhcp|hyperv|certificates|dfs|nps|wef|wsus|adcs)/[a-z0-9_/-]+", name):
            # Component-level failed/disabled scope is valid too.
            if name not in {"windows-components/" + c for c in wanted} or item.get("status") == "complete":
                raise ValueError("Unexpected scope path")
        if name.split('/')[1] not in wanted or any(part in ('', '.', '..') for part in name.split('/')):
            raise ValueError("Unexpected scope path")
        if name in rows or any(name.startswith(p + "/") or p.startswith(name + "/") for p in rows):
            raise ValueError("Duplicate/overlapping component scope")
        if item.get("status") not in ("complete", "failed", "disabled", "not_applicable", "skipped"):
            raise ValueError("Unknown scope status")
        row = dict(item)
        if item["status"] == "complete":
            expected = name + "/configuration.json"
            if set(item.get("files", {})) != {expected}:
                raise ValueError("Successful scope does not certify its expected file")
            path = root / expected
            if not path.resolve().is_relative_to(root.resolve()) or any((root / Path(*Path(expected).parts[:i])).is_symlink() for i in range(1, len(Path(expected).parts) + 1)):
                raise ValueError("Unsafe component output")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["files"][expected]:
                raise ValueError("Component file hash mismatch")
        rows[name] = row
    for component in wanted:
        base = "windows-components/" + component
        if not any(name == base or name.startswith(base + "/") for name in rows):
            rows[base] = dict(path=base, status="failed", error="Requested component missing from manifest", files={})
    return rows


def compare(output, wanted):
    roots = [Path(output) / ("component-run-%d" % n) for n in (1, 2)]
    runs = [verified_scopes(root, wanted) for root in roots]
    rows = []
    for name in sorted(set(runs[0]) | set(runs[1])):
        pair = [run.get(name) for run in runs]
        row = dict(scope=name, status="INCOMPLETE", captures=[p or {"status": "missing"} for p in pair])
        if all(p and p["status"] == "complete" for p in pair):
            same = pair[0]["files"] == pair[1]["files"]
            row["status"] = "PASS" if same else "REVIEW"
            if not same:
                relative = name + "/configuration.json"
                difference = "".join(difflib.unified_diff(
                    (roots[0]/relative).read_text(encoding="utf-8").splitlines(True),
                    (roots[1]/relative).read_text(encoding="utf-8").splitlines(True),
                    fromfile="capture-1/"+relative, tofile="capture-2/"+relative))
                path = Path(output) / "component-diffs" / (hashlib.sha256(name.encode()).hexdigest()[:24] + ".diff")
                path.parent.mkdir(exist_ok=True)
                path.write_text(difference, encoding="utf-8")
                row.update(diff_file=path.relative_to(output).as_posix(), diff_preview=difference[:12000])
        rows.append(row)
    status = ("INCOMPLETE" if not rows or any(r["status"] == "INCOMPLETE" for r in rows)
              else "REVIEW" if any(r["status"] == "REVIEW" for r in rows) else "PASS")
    return {"status": status, "scopes": rows, "requested": list(wanted)}


def capture(release, output, wanted, python, env, execute, details=()):
    collector = Path(release) / "collectors/system/windows_components.py"
    for number in (1, 2):
        args = [python, "-I", "-B", str(collector), "--output", str(Path(output) / ("component-run-%d" % number))]
        for component in wanted:
            args += ["--component", component]
        for detail in details:args += ['--detail',detail]
        code = execute(args, output, "component-run-%d.log" % number, env, timeout=None)
        if code not in (0, 6):
            raise RuntimeError("Component capture %d failed with exit %d; see its log" % (number, code))
    data = compare(output, wanted)
    (Path(output) / "component-comparison.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
