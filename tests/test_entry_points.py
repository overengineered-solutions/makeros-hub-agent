"""The service imports the package the way systemd does: a fresh interpreter, config first.

A module-level import cycle (config -> printers -> vprinter -> config) passed the whole suite in 0.89.0 because tests
import agent or printers first; the real `makeros-hub run` died at start. Each case runs in its own interpreter.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(ROOT), "MAKEROS_HUB_CONFIG": str(ROOT / "nonexistent-config.toml")}


@pytest.mark.parametrize("code", ["import makeros_hub.config", "import makeros_hub.__main__", "import makeros_hub.agent",
                                  "import makeros_hub.ingest", "import makeros_hub.vprinter"])
def test_each_module_imports_first_in_a_fresh_interpreter(code):
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]


def test_the_cli_entry_starts():
    run = subprocess.run([sys.executable, "-m", "makeros_hub", "--help"], cwd=ROOT, env=ENV, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
