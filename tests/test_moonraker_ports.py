"""Local sockets and a fake submitter only; no printer or site is contacted."""
import json
from pathlib import Path
import re
import socket
import urllib.request

import pytest

from makeros_hub import config, ingest
from makeros_hub.agent import heartbeat_payload
from test_ingest import build_multipart


@pytest.mark.parametrize("failed_pools", [("sv08-max",), ("giga",), ("p2s",), ("sv08-max", "giga", "p2s")])
def test_model_bind_collision_keeps_member_upload_and_other_models_serving(tmp_path, caplog, failed_pools):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        blocked = occupied.getsockname()[1]
        calls = []

        def submit(**kwargs):
            calls.append(kwargs)
            return {"status": "queued", "jobId": "local-test-job"}

        server = ingest.IngestServer(submit, port=8787, spool_dir=tmp_path, max_bytes=1024,
            moonraker_authorize=lambda *_: {"ok": False},
            moonraker_ports={pool: blocked if pool in failed_pools else 0 for pool in ("sv08-max", "giga", "p2s")})
        server.start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:8787/api/version", timeout=2) as response:
                assert response.status == 200
                assert json.load(response)["text"].startswith("OctoPrint")
            body = build_multipart("TEST", "part.gcode", b"G1 X1", {"print": "true"})
            request = urllib.request.Request("http://127.0.0.1:8787/api/files/local", data=body,
                headers={"X-Api-Key": "synthetic-token", "Content-Type": "multipart/form-data; boundary=TEST"})
            with urllib.request.urlopen(request, timeout=2) as response:
                assert response.status == 201
            assert len(calls) == 1
            assert set(server.model_servers) == {"sv08-max", "giga", "p2s"} - set(failed_pools)
            for model in server.model_servers.values():
                with pytest.raises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(f"http://127.0.0.1:{model.server_address[1]}/server/info", timeout=2)
                assert denied.value.code == 403  # accepting requests, with authorization intact
            for pool in failed_pools:
                assert {"modelPool": pool, "port": blocked, "state": "failed", "error": "EADDRINUSE"} in server.listener_status
                assert pool in caplog.text and str(blocked) in caplog.text and "EADDRINUSE" in caplog.text
            assert heartbeat_payload(moonraker_listeners=server.listener_status)["moonrakerListeners"] == server.listener_status
        finally:
            server.stop()


def test_default_model_ports_are_explicit_and_disjoint_from_documented_inventory(tmp_path, monkeypatch):
    defaults = config.DEFAULT_MOONRAKER_PORTS
    assert defaults == {"sv08-max": 8810, "giga": 8811, "p2s": 8812, "a1-mini": 8813, "x1c": 8814}
    doc = (Path(__file__).parents[1] / "docs/moonraker-door.md").read_text()
    table = doc.split("<!-- existing-port-inventory -->")[1].split("<!-- /existing-port-inventory -->")[0]
    occupied = set()
    for ports in re.findall(r"^\| `([0-9, -]+)` \|", table, re.M):
        for item in ports.replace(" ", "").split(","):
            ends = list(map(int, item.split("-")))
            occupied.update(range(ends[0], ends[-1] + 1))
    assert {8787, 8788, 8790, 8883, 50009} <= occupied
    assert set(defaults.values()).isdisjoint(occupied)
    bound = []
    real_server = ingest._BoundedServer

    def bind_ephemeral(address, handler):
        bound.append(address[1])
        return real_server(("127.0.0.1", 0), handler)

    monkeypatch.setattr(ingest, "_BoundedServer", bind_ephemeral)
    server = ingest.IngestServer(lambda **_: {}, port=65535, spool_dir=tmp_path, max_bytes=1024,
        moonraker_authorize=lambda *_: {})
    server.start()
    try:
        assert bound == [65535, *defaults.values()]
    finally:
        server.stop()


def test_config_overrides_only_named_model_ports(tmp_path, monkeypatch, caplog):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://example.invalid"\ningest_port = 12345\n[moonraker_ports]\np2s = 18812\nunknown-model = 18815\n')
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.delenv("MAKEROS_HUB_INGEST_PORT", raising=False)
    cfg = config.load_config()
    assert cfg.ingest_port == 12345
    assert "unknown model names" in caplog.text
    assert cfg.moonraker_ports == {**config.DEFAULT_MOONRAKER_PORTS, "p2s": 18812}
    cfg.moonraker_ports["giga"] = 18811
    assert config.Config(cloud_url="https://example.invalid").moonraker_ports["giga"] == 8811


@pytest.mark.parametrize("raw", ['moonraker_ports = "bad"', '[moonraker_ports]\np2s = "bad"',
                                  '[moonraker_ports]\np2s = 0', '[moonraker_ports]\np2s = 65536',
                                  '[moonraker_ports]\np2s = true'])
def test_bad_optional_port_config_cannot_take_down_ingest(tmp_path, monkeypatch, raw):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://example.invalid"\n' + raw)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.load_config()
    server = ingest.IngestServer(lambda **_: {}, port=0, spool_dir=tmp_path, max_bytes=1024,
        moonraker_authorize=lambda *_: {}, moonraker_ports={"p2s": cfg.moonraker_ports["p2s"]})
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server._server.server_address[1]}/api/version", timeout=2) as response:
            assert response.status == 200
        assert server.listener_status == [{"modelPool": "p2s", "port": None, "state": "failed", "error": "invalid_port"}]
    finally:
        server.stop()


def test_unreadable_door_store_keeps_member_ingest_serving(tmp_path, caplog):
    # A damaged staged-file database (disk trouble, a torn copy) must not stop the member door or the hub.
    (tmp_path / ".moonraker.sqlite3").write_bytes(b"not a database" * 512)
    server = ingest.IngestServer(lambda **_: {}, port=0, spool_dir=tmp_path, max_bytes=1024,
        moonraker_authorize=lambda *_: {}, moonraker_ports={"p2s": 0})
    server.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server._server.server_address[1]}/api/version", timeout=2) as response:
            assert response.status == 200
        assert server.model_servers == {}
        assert "staged-file store unavailable" in caplog.text
    finally:
        server.stop()


def test_failing_cleanup_retries_in_a_minute_not_every_server_tick(tmp_path, monkeypatch):
    # service_actions runs maintain() every 0.1 s on each of six listeners; a cleanup that keeps failing must not log
    # a traceback on every tick.
    from makeros_hub import moonraker
    store = moonraker.StagedUploads(tmp_path)
    calls = []

    def broken(db):
        calls.append(db)
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(store, "_clean", broken)
    store._last_clean = 0.0
    for _ in range(3):
        store.maintain()
    assert len(calls) == 1
