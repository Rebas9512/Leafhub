#!/usr/bin/env python3
"""Quick diagnostic — run with the project venv's Python."""
import sys, subprocess
from pathlib import Path

ROOT     = Path(__file__).parent.parent.resolve()
VENV_PY  = ROOT / ".venv" / "bin" / "python"

def run(py, *args):
    r = subprocess.run([str(py), *args], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()

print(f"=== System Python: {sys.executable}")
print(f"    version      : {sys.version.split()[0]}")

import importlib.util
st = importlib.util.find_spec("setuptools")
if st:
    import setuptools
    print(f"    setuptools   : {setuptools.__version__}")
    has_legacy = importlib.util.find_spec("setuptools.backends.legacy")
    print(f"    backends.legacy importable: {has_legacy is not None}")
else:
    print("    setuptools   : NOT FOUND")

print()
if VENV_PY.exists():
    print(f"=== Venv Python: {VENV_PY}")
    print(f"    version      : {run(VENV_PY, '--version')}")
    print(f"    setuptools   : {run(VENV_PY, '-c', 'import setuptools; print(setuptools.__version__)')}")
    print(f"    pip          : {run(VENV_PY, '-m', 'pip', '--version')}")
    has = run(VENV_PY, '-c',
              'import importlib.util; '
              'print(importlib.util.find_spec(\"setuptools.backends.legacy\") is not None)')
    print(f"    backends.legacy importable: {has}")
else:
    print("=== Venv not found — run setup.py first to create it")
