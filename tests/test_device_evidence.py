"""Device evidence from a fake Unix LocalAPI only; no real tailscaled or identity changes."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from makeros_hub import agent, config, device_evidence
from makeros_hub.http import Response
from test_ingest import build_multipart
from test_moonraker import door  # fixture also exercises the actual ingest listener


@pytest.fixture
def whois(tmp_path):
    path = str(tmp_path / 'members.sock')
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)
    listener.settimeout(0.1)
    stop = threading.Event()
    fixture = {'path': path, 'status': 200, 'body': {'Node': {'StableID': 'n-test', 'ID': 42,
               'ComputedName': 'Test laptop', 'Name': 'fallback.example'}}, 'requests': [], 'delay': 0}

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(2)
                request = b''
                while b'\r\n\r\n' not in request:
                    request += conn.recv(4096)
                fixture['requests'].append(request.decode())
                if stop.wait(fixture['delay']):
                    break
                body = fixture['body']
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    conn.sendall(f"HTTP/1.1 {fixture['status']} Result\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
                except BrokenPipeError:
                    pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield fixture
    stop.set()
    thread.join(3)
    listener.close()
    assert not thread.is_alive()


@pytest.mark.parametrize('peer', [('127.0.0.1', 43567), ('::1', 43567), ('::ffff:127.0.0.1', 43567)])
def test_loopback_uses_source_port_and_limits_name(whois, peer):
    whois['body']['Node']['ComputedName'] = 'x' * 100
    source, device = device_evidence.lookup(peer, whois['path'])
    assert source == 'tailnet'
    assert device == {'nodeId': 'n-test', 'name': 'x' * 64}
    request = whois['requests'][0].split()[1]
    query = parse_qs(urlsplit(request).query)
    assert query['addr'][0].endswith(':43567')
    assert urlsplit(request).path == '/localapi/v0/whois'


def test_fallback_id_and_name(whois):
    whois['body'] = {'Node': {'ID': 42, 'Name': 'fallback'}}
    assert device_evidence.lookup(('127.0.0.1', 2), whois['path']) == ('tailnet', {'nodeId': '42', 'name': 'fallback'})


@pytest.mark.parametrize('body', [b'broken', {}, [], {'Node': None}, {'Node': {'StableID': 'id\nsecret', 'Name': 'x'}},
    {'Node': {'StableID': True, 'Name': 'x'}}, {'Node': {'StableID': 'x', 'Name': 'bad\nname'}}, b'x' * 65537])
def test_invalid_evidence_never_raises(whois, body):
    whois['body'] = body
    assert device_evidence.lookup(('127.0.0.1', 2), whois['path']) == ('unknown', None)


def test_failed_and_slow_lookup_are_optional(whois, tmp_path):
    whois['status'] = 404
    assert device_evidence.lookup(('127.0.0.1', 2), whois['path']) == ('unknown', None)
    whois['status'] = 200
    whois['delay'] = 1.5
    before = time.monotonic()
    assert device_evidence.lookup(('127.0.0.1', 2), whois['path']) == ('unknown', None)
    assert time.monotonic() - before < 1.4
    assert device_evidence.lookup(('127.0.0.1', 2), str(tmp_path / 'missing')) == ('unknown', None)


def test_lan_never_queries_tailscaled(whois):
    assert device_evidence.lookup(('192.168.1.10', 43567), whois['path']) == ('lan', None)
    assert whois['requests'] == []


def test_both_submit_paths_send_device_and_only_log_node_id(door, whois, caplog):
    handler = door.server._server.RequestHandlerClass
    handler.members_tailscale_socket = whois['path']
    caplog.set_level(logging.INFO, logger='makeros-hub.ingest')
    path = door.stage()
    assert door.start(path)[0] == 200
    data = build_multipart('DEVICE', 'part.gcode', b'G1 X1', {'print': 'true'})
    assert door.request('/api/files/local', method='POST', data=data,
                        content_type='multipart/form-data; boundary=DEVICE')[0] == 201
    assert len(whois['requests']) == 3  # One lookup per connection, reused by submit; staging is attributed too.
    assert len(door.calls) == 2
    assert all(c['device'] == {'nodeId': 'n-test', 'name': 'Test laptop'} for c in door.calls)
    lines = [r.message for r in caplog.records if r.message.startswith('ingest submit source=')]
    assert lines == ['ingest submit source=tailnet nodeId=n-test'] * 2
    assert 'Test laptop' not in caplog.text and 'alice' not in '\n'.join(lines)


def test_lookup_failure_does_not_refuse_submit_or_add_device(door, caplog):
    caplog.set_level(logging.INFO, logger='makeros-hub.ingest')
    assert door.start(door.stage())[0] == 200
    assert 'device' not in door.calls[0]
    assert 'ingest submit source=unknown nodeId=-' in caplog.text


@pytest.mark.parametrize('route', ['moonraker', 'octoprint'])
def test_upload_survives_more_connections_from_its_device(door, whois, monkeypatch, route):
    handler = door.server._server.RequestHandlerClass
    handler.members_tailscale_socket = whois['path']
    reading, resume = threading.Event(), threading.Event()
    read_chunks = handler._read_chunks

    def slow_read(self, length):
        reading.set()
        assert resume.wait(5)
        yield from read_chunks(self, length)

    monkeypatch.setattr(handler, '_read_chunks', slow_read)
    sockets = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        if route == 'moonraker':
            upload = pool.submit(door.upload)
        else:
            data = build_multipart('BUSY', 'part.gcode', b'G1 X1', {'print': 'true'})
            upload = pool.submit(door.request, '/api/files/local', method='POST', data=data,
                                 content_type='multipart/form-data; boundary=BUSY')
        try:
            assert reading.wait(3)
            for _ in range(10):
                sockets.append(socket.create_connection(('127.0.0.1', door.server._server.server_address[1]), timeout=3))
            deadline = time.monotonic() + 3
            while len(whois['requests']) < 11 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(whois['requests']) == 11
            # A completed request proves the replacement handlers are also being served.
            assert door.request('/api/version')[0] == 200
        finally:
            resume.set()
            for connection in sockets:
                connection.close()
        assert upload.result(timeout=3)[0] == 201
    if route == 'moonraker':
        stored_path = upload.result()[1]['result']['item']['path']
        assert door.start(stored_path)[0] == 200
    assert len(door.calls) == 1


def test_submit_contract_and_socket_config(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    path.write_text('cloud_url="https://example.invalid"\nmembers_tailscale_socket="/custom/members.sock"\n')
    monkeypatch.setattr(config, 'CONFIG_PATH', path)
    cfg = config.load_config()
    assert cfg.members_tailscale_socket == '/custom/members.sock'
    assert config.Config(cloud_url='https://example.invalid').members_tailscale_socket == '/run/tailscale-members.sock'
    sent = []
    monkeypatch.setattr(agent, 'post_json', lambda url, body, **kw: sent.append(body) or Response(200, {'ok': True, 'jobId': 'job'}))
    submit = agent.make_cloud_submit(cfg, 'synthetic-hub')
    kwargs = dict(member_token='synthetic-member', submission_uid='a' * 32, file_name='part.gcode',
                  file_sha256='b' * 64, file_size=10, print_now=True)
    submit(**kwargs, device={'nodeId': 'n-test', 'name': 'Test laptop'})
    assert sent[-1]['device'] == {'nodeId': 'n-test', 'name': 'Test laptop'}
    submit(**kwargs)
    assert 'device' not in sent[-1]
