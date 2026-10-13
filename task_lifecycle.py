#!/usr/bin/env python3
"""Compatibility command for task_lifecycle."""
import importlib
import sys

if __name__ == "__main__":
    from configbackup_lib.bootstrap import run
    raise SystemExit(run("configbackup_lib.engine.task_lifecycle"))
else:
    sys.modules[__name__] = importlib.import_module("configbackup_lib.engine.task_lifecycle")
