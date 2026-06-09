"""Printer adapters — the hub's LAN connection to physical printers.

`bambu_parse` is PURE (stdlib-only, no paho) so the report→status mapping is
unit-testable without a printer or the MQTT dependency. `bambu` is the thin paho
I/O wrapper around it. `manager` reconciles adapters from the cloud config-down
and gathers their normalized status for the heartbeat.
"""
