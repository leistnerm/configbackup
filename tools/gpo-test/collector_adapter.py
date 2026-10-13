"""Load only the unchanged, hash-pinned GPO functions from ConfigBackup.

AST selection avoids executing registry code or importing PyYAML. There is no
substitute normalizer here: the selected function bodies are compiled as shipped.
The caller supplies the subprocess and temporary-directory adapters.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import xml.etree.ElementTree as ET

SOURCE_HASHES = {
    "windows_settings.py": "f2e1fd906c6c8e1118f8e321dfa2490cc93cb230c2c98a5cb4c4e5b24fd8059d",
    "completeness.py": "cc85848f23e1e35417db7a104be74a12a4053019bfca456304c389c10755a5ad",
    "sections.py": "c3fd64f82d8d5a34ad0053ecbdeca50ebb29d34819a31eef6f2e2935a207457d",
}
FUNCTIONS = {
    "sections.py": ("enabled",),
    "completeness.py": ("relative", "digest", "section"),
    "windows_settings.py": ("normalize_rsop", "collect_rsop"),
}


def load_collector(runner, temporary=tempfile, source_dir=None):
    source_dir = Path(source_dir or Path(__file__).parent / "collector-source")
    namespace = {"fnmatch": fnmatch, "hashlib": hashlib, "json": json,
                 "os": os, "Path": Path, "PurePosixPath": PurePosixPath,
                 "ET": ET, "subprocess": runner, "tempfile": temporary}
    for name, wanted in FUNCTIONS.items():
        raw = (source_dir / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != SOURCE_HASHES[name]:
            raise ValueError("Bundled collector source has changed: " + name)
        tree = ast.parse(raw, filename=str(source_dir / name))
        selected = [node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name in wanted]
        if sorted(node.name for node in selected) != sorted(wanted):
            raise ValueError("Missing or duplicate collector functions: " + name)
        module = ast.Module(body=selected, type_ignores=[])
        exec(compile(module, str(source_dir / name), "exec"), namespace)
    return namespace
