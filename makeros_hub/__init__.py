"""makeros-hub-agent — the on-LAN bridge between a makerspace's printers and the
makeros cloud control plane (the SimplyPrint replacement).

Enrollment + heartbeat: standard-library only. The Bambu LAN printer adapter
adds paho-mqtt — the agent pulls its printer list (incl. access codes) down from
the cloud (config-down), connects over MQTT, and reports normalized per-printer
status in the heartbeat. The report-parsing layer (`printers/bambu_parse`) stays
pure/stdlib so it is unit-testable without paho.

Over-the-air self-update (`update.py`): the heartbeat response names the release
this hub should run; if it's a newer release tag, the agent updates itself via a
narrow root script and restarts — no SSH. `bootstrap.sh` collapses first-time
setup to a single pasted command.
"""

__version__ = "0.4.0"
