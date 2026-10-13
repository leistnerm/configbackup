#!/usr/bin/env python3
"""Compatibility command for retention_planning."""
import importlib
import sys

if __name__ == "__main__":
    from configbackup_lib.bootstrap import run
    raise SystemExit(run("configbackup_lib.recovery.retention_planning"))
else:
    sys.modules[__name__] = importlib.import_module("configbackup_lib.recovery.retention_planning")
