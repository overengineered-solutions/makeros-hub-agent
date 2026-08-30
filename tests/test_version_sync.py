"""The agent reports makeros_hub.__version__ to the cloud; the OTA compares it with the target tag. A release that
bumps pyproject but not __version__ reinstalls itself every cooldown forever (v0.51–v0.54 did). Keep them equal."""

import tomllib
from pathlib import Path

from makeros_hub import __version__


def test_pyproject_version_matches_dunder_version():
    meta = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert meta["project"]["version"] == __version__
