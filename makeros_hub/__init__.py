"""makeros-hub-agent — the on-LAN bridge between a makerspace's printers and the
makeros cloud control plane (the SimplyPrint replacement).

Enrollment slice (PR 2): standard-library only — no third-party deps, so it runs
on a fresh Raspberry Pi OS with zero `pip install`. The printer adapters (PR 5)
add httpx + the vendor libraries behind the abstracted transport in `http.py`.
"""

__version__ = "0.1.0"
