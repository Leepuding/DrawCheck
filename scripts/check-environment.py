#!/usr/bin/env python3
"""Read-only local dependency discovery; shares packaging's version probes."""
from pathlib import Path
import runpy
import sys

script = Path(__file__).resolve().parent.parent / "packaging/preflight.py"
sys.argv = [str(script), "local", *sys.argv[1:]]
runpy.run_path(str(script), run_name="__main__")
