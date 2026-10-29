"""Fake ingest sockets and a local fake Unix WhoIs server; no equipment."""
from http.server import BaseHTTPRequestHandler
import io
import logging
import socket
import threading

import pytest

from makeros_hub import device_evidence, ingest
from test_device_evidence import whois


class FakeSocket:
    def __init__(self):
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.served = False

    def settimeout(self, timeout):
        pass

    def makefile(self, *args):
        return io.BytesIO()

    def sendall(self, data):
        pass

    def shutdown(self, how):
        assert how in (socket.SHUT_RDWR, socket.SHUT_WR)
        self.closed.set()

    def close(self):
        self.closed.set()


@pytest.fixture
def listener(tmp_path, monkeypatch, whois):
    handler = ingest.make_handler(lambda **kw: {}, tmp_path, 1024)
    handler.members_tailscale_socket = whois["path"]
    server = ingest._BoundedServer(("127.0.0.1", 0), handler, False)
    sockets = []
    accept_thread = threading.get_ident()
    lookup = device_evidence.lookup

    def lookup_in_worker(*args):
        assert threading.get_ident() != accept_thread
        return lookup(*args)

    def serve(handler):
        handler.request.served = True
        handler.request.ready.set()
        assert handler.request.closed.wait(10)

    worker = server.process_request_thread

    def run(request, peer):
        try:
            worker(request, peer)
        finally:
            request.ready.set()
            request.finished.set()

    monkeypatch.setattr(device_evidence, "lookup", lookup_in_worker)
    monkeypatch.setattr(BaseHTTPRequestHandler, "handle", serve)
    monkeypatch.setattr(server, "process_request_thread", run)

    def connect(ip="127.0.0.1"):
        request = FakeSocket()
        sockets.append(request)
        server.process_request(request, (ip, 40000 + len(sockets)))
        assert request.ready.wait(3)
        return request

    yield server, connect
    for request in sockets:
        request.close()
    for request in sockets:
        assert request.finished.wait(3)
    # Every handler, including an evicted one, returns exactly one global slot.
    assert all(server.connections.acquire(False) for _ in range(32))
    assert not server.connections.acquire(False)
    server.server_close()


@pytest.mark.parametrize("protect_first", [False, True])
def test_ten_connections_keep_six_and_leave_other_device_room(listener, whois, caplog, protect_first):
    server, connect = listener
    caplog.set_level(logging.INFO, logger="makeros-hub.ingest")
    whois["body"]["Node"]["StableID"] = "n-other"
    other = connect()  # The globally oldest connection belongs to a different device.
    whois["body"]["Node"]["StableID"] = "n-test"
    own = [connect()]
    if protect_first:
        assert server.set_uploading(own[0], True)
    own += [connect() for _ in range(9)]
    dropped = own[1:5] if protect_first else own[:4]
    assert [request for request in own if request.closed.is_set()] == dropped
    assert all(request.served for request in own)
    for request in dropped:
        assert request.finished.wait(3)
        assert not server.set_uploading(request, True)  # Eviction wins the upload-start race.
    assert not other.closed.is_set()
    whois["body"]["Node"]["StableID"] = "n-other"
    other = connect()
    assert other.served and not other.closed.is_set()
    assert len(whois["requests"]) == 12
    lines = [r.message for r in caplog.records if "oldest idle connection" in r.message]
    assert lines == ["ingest closed oldest idle connection nodeId=n-test"] * 4
    assert "Test laptop" not in caplog.text and "127.0.0.1" not in caplog.text


def test_lan_ips_have_separate_buckets_without_whois(listener, whois):
    _, connect = listener
    own = [connect("192.168.1.10") for _ in range(10)]
    other = connect("192.168.1.11")
    assert [s.closed.is_set() for s in own] == [True] * 4 + [False] * 6
    assert other.served and not other.closed.is_set()
    assert whois["requests"] == []


def test_unattributable_peers_are_not_capped_together(listener, whois):
    # A whois outage puts every tailnet client in "unknown"; a shared cap of six would refuse members wholesale.
    _, connect = listener
    whois["status"] = 404
    own = [connect("127.0.0.1" if i % 2 else "::1") for i in range(10)]
    assert not any(s.closed.is_set() for s in own)
    whois["status"] = 200
    other = connect()
    assert other.served and not other.closed.is_set()


def test_no_idle_connection_means_new_one_closes(listener):
    server, connect = listener
    own = [connect() for _ in range(6)]
    for request in own:
        assert server.set_uploading(request, True)
    refused = connect()
    assert refused.closed.is_set() and not refused.served
    assert not any(s.closed.is_set() for s in own)
    assert server.set_uploading(own[0], False)
    assert connect().served
    assert own[0].closed.is_set()
