"""One local ingest socket, fake authorization and submission; no live services."""
import json
import logging
import urllib.error
import urllib.request

import pytest

from makeros_hub import config, ingest, moonraker
from makeros_hub.printers.ingest_facts import POOLS
from test_ingest import build_multipart


def assert_member_ingest(base, calls):
    with urllib.request.urlopen(base + "/api/version", timeout=2) as response:
        assert response.status == 200
        assert json.load(response)["text"].startswith("OctoPrint")
    request = urllib.request.Request(base + "/api/files/local",
        data=build_multipart("TEST", "part.gcode", b"G1 X1", {"print": "true"}),
        headers={"X-Api-Key": "synthetic-token", "Content-Type": "multipart/form-data; boundary=TEST"})
    with urllib.request.urlopen(request, timeout=2) as response:
        assert response.status == 201
    assert len(calls) == 1


def test_default_pool_and_member_ingest_share_the_only_listener(tmp_path, monkeypatch):
    bound, calls, authorized = [], [], []
    real_server = ingest._BoundedServer

    def bind_local(address, handler):
        bound.append(address)
        return real_server(("127.0.0.1", 0), handler)

    def authorize(token, pool):
        authorized.append((token, pool))
        return {"ok": True, "submissionProtocol": moonraker.PROTOCOL, "memberId": "test-member",
                "modelPool": pool, "printerModel": POOLS[pool][0], "printerIds": [], "loadedSpools": {}}

    monkeypatch.setattr(ingest, "_BoundedServer", bind_local)
    server = ingest.IngestServer(lambda **kw: calls.append(kw) or {"status": "queued", "jobId": "test-job"},
        port=12345, spool_dir=tmp_path, max_bytes=1024, moonraker_authorize=authorize)
    server.start()
    base = f"http://127.0.0.1:{server._server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/server/info", headers={"X-Api-Key": "synthetic-token"})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response)["result"]["model_pool"] == "p2s"
        assert authorized == [("synthetic-token", "p2s")]
        assert_member_ingest(base, calls)
        assert bound == [("0.0.0.0", 12345)]
    finally:
        server.stop()


@pytest.mark.parametrize("pool", [None, *POOLS])
def test_config_accepts_only_known_pool_names(tmp_path, monkeypatch, pool):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://example.invalid"\n' +
                    (f'moonraker_pool = "{pool}"\n' if pool is not None else ""))
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    assert config.load_config().moonraker_pool == (pool or "p2s")


@pytest.mark.parametrize("raw", ['"nope"', '""', '42', 'true', '[]', '{}'])
def test_disabled_or_invalid_pool_leaves_member_ingest_up(tmp_path, monkeypatch, caplog, raw):
    path = tmp_path / "config.toml"
    path.write_text('cloud_url = "https://example.invalid"\nmoonraker_pool = ' + raw)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    cfg = config.load_config()
    assert cfg.moonraker_pool == ""
    calls, authorized = [], []
    server = ingest.IngestServer(lambda **kw: calls.append(kw) or {"status": "queued", "jobId": "test-job"},
        port=0, spool_dir=tmp_path, max_bytes=1024, moonraker_pool=cfg.moonraker_pool,
        moonraker_authorize=lambda *args: authorized.append(args))
    server.start()
    base = f"http://127.0.0.1:{server._server.server_address[1]}"
    try:
        request = urllib.request.Request(base + "/server/info", headers={"X-Api-Key": "synthetic-token"})
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=2)
        assert refused.value.code == 404
        assert authorized == []
        assert server._server.RequestHandlerClass.model_pool is None
        assert_member_ingest(base, calls)
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == (0 if raw == '""' else 1)
        if errors:
            assert "moonraker_pool" in errors[0].message
    finally:
        server.stop()


def test_unreadable_door_store_keeps_member_ingest_serving(tmp_path, caplog):
    (tmp_path / ".moonraker.sqlite3").write_bytes(b"not a database" * 512)
    calls = []
    server = ingest.IngestServer(lambda **kw: calls.append(kw) or {"status": "queued", "jobId": "test-job"},
        port=0, spool_dir=tmp_path, max_bytes=1024, moonraker_authorize=lambda *_: {})
    server.start()
    try:
        assert_member_ingest(f"http://127.0.0.1:{server._server.server_address[1]}", calls)
        assert server._server.RequestHandlerClass.model_pool is None
        assert "staged-file store unavailable" in caplog.text
    finally:
        server.stop()


def test_failing_cleanup_retries_in_a_minute_not_every_server_tick(tmp_path, monkeypatch):
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
