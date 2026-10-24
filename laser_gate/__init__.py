"""Laser gate: the only network path to the OMTech's Ruida controller.

This service can check a job file, store it in the controller's memory under a short code, and read status. Start, stop,
pause, frame, jog, home, panel keys (the panel-key port), delete and settings writes were never written. Any new endpoint
or command needs a review. No OTA: a person installs every update.

(The operator's rule names the panel-key port by its number. A hard rule and a static test keep that number out of
this package, so it is written in words here.)

Stdlib only, Python >= 3.11. Design: pstation docs/laser-control-plan-2026-10-14.md; install: docs/laser-gate.md.
"""
__version__ = "0.2.0"
