"""The local runner keeps pytest's isolation, working directory and exit status."""
import os
import subprocess
from pathlib import Path


def test_runner_sets_the_package_path_and_returns_pytests_status(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    python = tmp_path / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$PYTHONPATH" "$@"\nexit 7\n')
    python.chmod(0o755)
    result = subprocess.run([str(repo / "scripts/test.sh"), "-k", "version_sync"], cwd=tmp_path,
                            env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PYTHONPATH": "/wrong"},
                            capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout.splitlines() == [str(repo), ".", "-m", "pytest", "-q", "tests", "-k", "version_sync"]
