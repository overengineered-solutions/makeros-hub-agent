"""Host routing on a local listener with fake site answers; no DNS or equipment."""
import http.client
import json
import logging

import pytest

from makeros_hub import config, ingest, moonraker
from makeros_hub.printers.ingest_facts import POOLS
from test_ingest import build_multipart
from test_moonraker import door, gcode, threemf


HOSTS = {
    "p2s-printers.oes-mesh.internal": "p2s",
    "x1c-printers.oes-mesh.internal": "x1c",
    "a1mini-printers.oes-mesh.internal": "a1-mini",
    "sv08-printers.oes-mesh.internal": "sv08-max",
    "giga-printers.oes-mesh.internal": "giga",
    "h2d-printers.oes-mesh.internal": "h2d",
}
PROFILES = {"p2s": "PS P2S", "x1c": "PS X1 Carbon", "a1-mini": "PS A1 mini",
            "sv08-max": "PS SV08 MAX", "giga": "PS Giga", "h2d": "PS H2D"}


def test_config_defaults_and_normalized_replacement_table(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://example.invalid"\n')
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    assert config.load_config().moonraker_hosts == HOSTS
    path.write_text(path.read_text() + '[moonraker_hosts]\n"CUSTOM.EXAMPLE." = "x1c"\n')
    assert config.load_config().moonraker_hosts == {"custom.example": "x1c"}


def test_configured_table_reaches_listener_and_empty_table_uses_fallback(door):
    cfg = config.Config(cloud_url="https://example.invalid", moonraker_hosts={"CUSTOM.EXAMPLE.": "x1c"})
    door.restart(pool=cfg.moonraker_pool, hosts=cfg.moonraker_hosts)
    assert door.request("/server/info", host="custom.example:8787")[1]["result"]["model_pool"] == "x1c"
    assert door.request("/server/info", host="giga-printers.oes-mesh.internal")[1]["result"]["model_pool"] == "p2s"
    door.restart(hosts={})
    assert door.request("/server/info", host="x1c-printers.oes-mesh.internal")[1]["result"]["model_pool"] == "p2s"


@pytest.mark.parametrize("hosts", [[], 42, {"100.64.0.1": "x1c"}, {"bad/name": "x1c"}, {"x" * 254: "x1c"}])
def test_invalid_host_config_keeps_fallback_and_logs_once(caplog, hosts):
    cfg = config.Config(cloud_url="https://example.invalid", moonraker_hosts=hosts)
    assert cfg.moonraker_hosts == {} and cfg.moonraker_pool == "p2s"
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


@pytest.mark.parametrize("raw", ['"unknown"', '"x1e"', '42', 'true', '[]', '{}'])
def test_invalid_host_pool_logs_once_and_ignores_only_that_entry(tmp_path, monkeypatch, caplog, raw):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://example.invalid"\n[moonraker_hosts]\n'
                    '"good.example" = "giga"\n"bad.example" = ' + raw)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.load_config()
    assert cfg.moonraker_pool == "p2s"
    assert cfg.moonraker_hosts == {"good.example": "giga"}
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "moonraker_hosts" in errors[0].message


@pytest.mark.parametrize("host,pool", HOSTS.items())
def test_each_host_scopes_authorization_lanes_upload_and_start(door, monkeypatch, host, pool):
    def authorize(token, requested):
        access = door.authorize(token, requested)
        access["printerIds"] = [requested]
        access["loadedSpools"] = {requested: {"material": "PLA", "colorHex": "#123456"}}
        return access

    monkeypatch.setattr(door.server._server.RequestHandlerClass.moonraker, "authorize", authorize)
    door.statuses = [{"printerId": p, "model": POOLS[p][0], "connectionState": "connected",
                      "ams": [{"trays": [{"slot": 0, "material": "PLA", "colorHex": "ABCDEF"}]}],
                      "vtTray": {"material": "PETG", "colorHex": "000000"} if POOLS[p][2] is None else None}
                     for p in HOSTS.values()]
    for status in door.statuses:  # an H2D reports two extruders; its AMS units name the extruder they feed
        if status["model"] == "H2D":
            status["extruderCount"] = 2
            status["nozzles"] = [{"id": 0, "extruderId": 0, "diameterMm": 0.4}, {"id": 1, "extruderId": 1, "diameterMm": 0.4}]
            status["ams"][0]["extruderId"] = 0
    door.refresh()
    for name in (host, host.upper() + ".:8787"):
        status, body = door.request("/server/info", host=name)
        assert status == 200 and body["result"]["model_pool"] == pool
        assert door.auth_calls[-1] == ("alice", pool)
    status, body = door.request("/server/database/item?namespace=lane_data", host=host)
    assert status == 200
    lanes = body["result"]["value"]
    assert len(lanes) == 1 and lanes["lane1"]["material"] == "PLA"
    model_id = POOLS[pool][2]
    assert lanes["lane1"]["color"] == ("#ABCDEFFF" if model_id else "#123456FF")
    data = threemf(model=PROFILES[pool], model_id=model_id) if model_id else gcode(PROFILES[pool])
    path = door.stage(host=host, name="part.3mf" if model_id else "part.gcode", data=data)
    assert (door.path / path).read_bytes() == data
    other = next(h for h, p in HOSTS.items() if p != pool)
    assert door.start(path, host=other)[0] == 404
    assert door.calls == []
    assert door.start(path, host=host)[0] == 200
    assert door.auth_calls[-1] == ("alice", pool)
    assert door.calls[0]["file_facts"]["modelPool"] == pool
    assert door.calls[0]["printer_model"] == POOLS[pool][0]


def test_x1c_slice_via_p2s_host_keeps_existing_refusal(door):
    # X1C fallback makes the Host essential to this rejection.
    door.restart(pool="x1c")
    status, body = door.upload(host="p2s-printers.oes-mesh.internal", name="part.3mf",
                               data=threemf(model="PS X1 Carbon", model_id="BL-P001"))
    assert status == 400
    assert body["error"]["message"] == (
        "Invalid request or sliced-file facts. Check the model, selected plate, filament and nozzle settings.")
    assert door.calls == [] and not list(door.path.glob("*/*.3mf"))


@pytest.mark.parametrize("pool", ["sv08-max", "giga"])
def test_named_klipper_pool_still_requires_one_filament_and_site_spool(door, monkeypatch, pool):
    host = next(h for h, p in HOSTS.items() if p == pool)
    assert door.upload(host=host, data=gcode(PROFILES[pool], mm="1, 1", g="1, 1",
                                           types="PLA;PETG", colours="#FFFFFF;#000000"))[0] == 400
    access = door.authorize("alice", pool)
    access["loadedSpools"] = {}
    monkeypatch.setattr(door.server._server.RequestHandlerClass.moonraker, "authorize", lambda *_: access)
    assert door.request("/server/database/item?namespace=lane_data", host=host)[0] == 503
    assert door.calls == []


@pytest.mark.parametrize("host", [None, "100.64.0.1:8787", "[::1]:8787", "unknown.example:8787",
    "x1e-printers.oes-mesh.internal", "x1c-printers.oes-mesh.internal.evil", "x" * 10000,
    "x1c-printers.oes-mesh.internal@evil", "evil@x1c-printers.oes-mesh.internal",
    "x1c-printers.oes-mesh.internal/path", "x1c-printers.oes-mesh.internal?x", "x1c-printers.oes-mesh.internal#x",
    "x1c-printers.oes-mesh.internal..", "x1c-printers.oes-mesh.internal:bad",
    "x1c-printers.oes-mesh.internal:65536", "x1c-printers.oes-mesh.internal:0",
    "x1c-printers.oes-mesh.internal\t.evil", "x1c-printers.oes-mesh.internal\x01",
    "x1c-printers.oes-mesh.internal:8787:42", "x1c-printers.oes-mesh.internal\\evil",
    "x1c-printers.oes-mesh.internal, p2s-printers.oes-mesh.internal"])
@pytest.mark.parametrize("fallback", ["p2s", "giga"])
def test_ip_unknown_and_malformed_hosts_use_configured_fallback(door, host, fallback):
    door.restart(pool=fallback)
    status, body = door.request("/server/info", host=host)
    assert status == 200 and body["result"]["model_pool"] == fallback
    assert door.auth_calls == [("alice", fallback)]


@pytest.mark.parametrize("target,hosts,expected", [
    ("http://h2d-printers.oes-mesh.internal/server/info", ["x1c-printers.oes-mesh.internal"], "x1c"),
    ("http://x1c-printers.oes-mesh.internal/server/info", ["unknown.example"], "sv08-max"),
    ("/server/info", [], "sv08-max"),
    ("/server/info", ["x1c-printers.oes-mesh.internal", "giga-printers.oes-mesh.internal"], "sv08-max"),
    ("http://[x1c-printers.oes-mesh.internal/server/info", ["x1c-printers.oes-mesh.internal"], None),
])
def test_only_one_host_header_selects_pool_not_absolute_request_target(door, target, hosts, expected):
    connection = http.client.HTTPConnection("127.0.0.1", door.server._server.server_address[1], timeout=3)
    try:
        connection.putrequest("GET", target, skip_host=True)
        for host in hosts:
            connection.putheader("Host", host)
        connection.putheader("X-Forwarded-Host", "h2d-printers.oes-mesh.internal")
        connection.putheader("X-Api-Key", "alice")
        connection.endheaders()
        response = connection.getresponse()
        if expected is None:
            assert response.status == 400
            assert json.load(response)["error"]["message"] == "Invalid request URL. Use the PS preset's hub address."
            assert door.auth_calls == []
            return
        assert response.status == 200
        assert json.load(response)["result"]["model_pool"] == expected
        assert door.auth_calls == [("alice", expected)]
    finally:
        connection.close()


@pytest.mark.parametrize("host", HOSTS)
def test_octoprint_ignores_model_host_and_moonraker_authorization(door, host):
    door.available = False
    assert door.request("/api/version", host=host)[1] == ingest.VERSION_BODY
    status, _ = door.request("/api/files/local", host=host, method="POST",
        data=build_multipart("TEST", "part.gcode", gcode("PS Giga"), {"print": "true"}),
        content_type="multipart/form-data; boundary=TEST")
    assert status == 201 and door.auth_calls == []
    assert door.calls[0]["printer_model"] == "PS Giga"
    assert "file_facts" not in door.calls[0]


def test_named_host_still_obeys_site_staff_gate(door, monkeypatch):
    words = "This Orca connection is being tested by staff. Use your existing PS connection."

    def deny(token, pool):
        assert pool == "x1c"
        raise moonraker.DoorError(403, words)

    monkeypatch.setattr(door.server._server.RequestHandlerClass.moonraker, "authorize", deny)
    status, body = door.request("/server/info", host="x1c-printers.oes-mesh.internal")
    assert status == 403 and body["error"]["message"] == words
    assert door.calls == []
