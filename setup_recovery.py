#!/usr/bin/env python3
"""Compatibility command for setup_recovery."""
import importlib
import sys

if __name__ == "__main__":
    from configbackup_lib.bootstrap import run
    raise SystemExit(run("configbackup_lib.recovery.setup_recovery"))
else:
    sys.modules[__name__] = importlib.import_module("configbackup_lib.recovery.setup_recovery")
