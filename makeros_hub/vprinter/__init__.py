"""Virtual Bambu printer support for native-print capture.

Runtime notes:
  - Default off: nothing binds unless config-down includes
    virtual_printer.enabled=true.
  - Optional dependency: certificate generation needs cryptography, imported
    lazily only when the VP starts.
  - Privileged ports: production binds implicit FTPS on 990, so systemd grants
    CAP_NET_BIND_SERVICE to the venv Python.
"""

from __future__ import annotations

from ..config import VirtualPrinterConfig, VirtualPrinterMember
from .capture import CapturedJob

__all__ = ["CapturedJob", "VirtualPrinterConfig", "VirtualPrinterMember"]
