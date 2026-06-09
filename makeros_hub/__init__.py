"""makeros-hub-agent — the on-LAN bridge between a makerspace's printers and the
makeros cloud control plane (the SimplyPrint replacement).

Enrollment + heartbeat (PR 2): standard-library only. The Bambu LAN printer
adapter (PR 5) adds paho-mqtt — the agent pulls its printer list (incl. access
codes) down from the cloud (config-down), connects over MQTT, and reports
normalized per-printer status in the heartbeat. The report-parsing layer
(`printers/bambu_parse`) stays pure/stdlib so it is unit-testable without paho.
"""

__version__ = "0.2.0"
