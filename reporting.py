#!/usr/bin/env python3
"""Compatibility command for reporting."""
import importlib
import sys

if __name__ == "__main__":
    from configbackup_lib.bootstrap import run
    raise SystemExit(run("configbackup_lib.reporting.reporting"))
else:
    sys.modules[__name__] = importlib.import_module("configbackup_lib.reporting.reporting")
