"""Test isolation for the agent's PERSISTED state.

WHY THIS EXISTS. Every persisted path in the agent is a module-level global resolved ONCE at import time
from an env var with a real system default:

    manager.DISPATCHED_STATE_PATH  -> /var/lib/makeros-hub/dispatched.json
    report_outbox.PATH             -> /var/lib/makeros-hub/queue-reports.json
    bambu.QUEUE_PROGRESS_DIR       -> /var/lib/makeros-hub/queue-progress
    bambu.TERMINAL_JOBS_DIR        -> /var/lib/makeros-hub/terminal-jobs
    klipper.QUEUE_PROGRESS_DIR     -> /var/lib/makeros-hub/queue-progress   (same dirs, one file per printer)
    klipper.TERMINAL_JOBS_DIR      -> /var/lib/makeros-hub/terminal-jobs
    update.STATE_PATH              -> /var/lib/makeros-hub/last_update.json
    laser.RUNS_STATE_PATH          -> /var/lib/makeros-hub/laser-runs.json
    manager.DISPATCHED_COMMANDS_PATH     -> /var/lib/makeros-hub/dispatched-commands.json   (v0.69)
    report_outbox.COMMAND_RESULTS_PATH   -> /var/lib/makeros-hub/command-results.json       (v0.69)

That is right for the agent and wrong for a test suite. A fresh PrinterManager() calls _load_dispatched(),
so state written by one test is loaded by the next: the dispatch guards then correctly refuse to re-start a
job "already dispatched" in an earlier test, and assertions about first-dispatch behaviour fail. _save_dispatched
even does mkdir(parents=True), so the suite CREATES /var/lib/makeros-hub on the host.

It only bites where the process can write /var/lib — as root, in a container, or in CI. Run as an ordinary
user the writes fail silently and everything passes, which is why this looked like flakiness that came and
went with the environment rather than a fixed leak. Observed 2026-09-07: 8 failures, then 16 on an identical
re-run in a root container, while the same commit was green on a developer machine.

Every path is redirected per test, so no test can inherit another's state and none can touch the real box.
Reads happen through the module global at call time, so rebinding the attribute is enough.
"""

import pytest

from makeros_hub import laser, report_outbox, update
from makeros_hub.printers import bambu, klipper, manager

# (module, attribute, is_dir) — a directory is created so code that lists it finds an empty dir, not a missing one.
_STATE_PATHS = [
    (manager, "DISPATCHED_STATE_PATH", False),
    (manager, "DISPATCHED_COMMANDS_PATH", False),
    (report_outbox, "PATH", False),
    (report_outbox, "COMMAND_RESULTS_PATH", False),
    (bambu, "QUEUE_PROGRESS_DIR", True),
    (bambu, "TERMINAL_JOBS_DIR", True),
    (klipper, "QUEUE_PROGRESS_DIR", True),
    (klipper, "TERMINAL_JOBS_DIR", True),
    (klipper, "STOP_POINTS_DIR", True),   # v0.86: the recorded stop point of a print stopped on a shutdown or error
    (update, "STATE_PATH", False),
    (laser, "RUNS_STATE_PATH", False),
    (laser, "SECRETS_DIR", True),   # v0.68: the operator's laser secret files; a test never reads the real ones
]


@pytest.fixture(autouse=True)
def isolate_agent_state(tmp_path, monkeypatch):
    """Point every persisted path at this test's own tmp_path. Autouse so unittest.TestCase tests get it too."""
    for module, attr, is_dir in _STATE_PATHS:
        target = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}-{attr.lower()}"
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, attr, target)
