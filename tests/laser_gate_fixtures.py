"""Test-only helpers for the laser gate: a 20 mm square encoder built from the candidate ALLOWED table, a fake Ruida
controller on 127.0.0.1, and a gate HTTP server on an ephemeral loopback port. Nothing here touches a real network.

    python3 tests/laser_gate_fixtures.py OUT.rd     writes the square as a saved (swizzled) .rd file
"""
import http.client
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laser_gate.checker import swizzle, u14, unswizzle  # noqa: E402
from laser_gate.gate import ACK, Config, Gate, Server, guard  # noqa: E402

TOKEN = "t" * 40
FILE_COUNT = 0x0105  # any address: the real one comes from T0


def e14(v):
    v &= 0x3FFF
    return bytes([(v >> 7) & 0x7F, v & 0x7F])


def e35(v):
    v &= 0xFFFFFFFF  # negative absolute coordinates: two's complement in 32 bits, as the checker reads them
    return bytes([(v >> 28) & 0x7F, (v >> 21) & 0x7F, (v >> 14) & 0x7F, (v >> 7) & 0x7F, v & 0x7F])


def um(mm):
    return round(mm * 1000)


def xy(x_mm, y_mm):
    return e35(um(x_mm)) + e35(um(y_mm))


def pw(pct):
    return e14(round(pct * 16383 / 100))


def sp(mm_s):
    return e35(round(mm_s * 1000))


def square_plain(size=20, at=(10, 10), layers=((30, 20),), min_gap=10, extra_cuts=0):
    """Unswizzled .rd job: one square per layer. layers = ((max power %, speed mm/s), ...); min power = max - min_gap.
    extra_cuts adds 1 mm back-and-forth relative cuts (AA, 3 bytes each) inside the square, to make long files."""
    x0, y0 = at
    x1, y1 = x0 + size, y0 + size
    h = b"\xD8\x10" + b"\xE6\x01" + b"\xF0" + b"\xF1\x02\x00" + b"\xD8\x00"
    h += b"\xE7\x06" + e35(0) + e35(0)
    h += b"\xE7\x03" + xy(x0, y0) + b"\xE7\x07" + xy(x1, y1) + b"\xE7\x50" + xy(x0, y0) + b"\xE7\x51" + xy(x1, y1)
    h += b"\xE7\x04" + e14(1) * 2 + e14(0) * 5 + b"\xE7\x05\x00"
    for i, (power, speed) in enumerate(layers):
        part = bytes([i])
        h += b"\xC9\x04" + part + sp(speed) + b"\xC6\x31" + part + pw(power - min_gap) + b"\xC6\x32" + part + pw(power)
        h += b"\xCA\x06" + part + e35(0xFF)
        for op in (b"\xE7\x52", b"\xE7\x61"):
            h += op + part + xy(x0, y0)
        for op in (b"\xE7\x53", b"\xE7\x62"):
            h += op + part + xy(x1, y1)
    h += b"\xCA\x22" + bytes([len(layers) - 1]) + b"\xF1\x03" + xy(0, 0) + b"\xEA\x00"
    body = b""
    for i, (power, speed) in enumerate(layers):
        body += b"\xCA\x02" + bytes([i]) + b"\xCA\x01\x13" + b"\xC9\x02" + sp(speed)
        body += b"\xC6\x01" + pw(power - min_gap) + b"\xC6\x02" + pw(power)
        body += b"\x88" + xy(x0, y0)
        for px, py in ((x1, y0), (x1, y1), (x0, y1), (x0, y0)):
            body += b"\xA8" + xy(px, py)
        for k in range(extra_cuts):
            body += b"\xAA" + e14(um(1) if k % 2 == 0 else -um(1))
    return h + body + b"\xE7\x00" + b"\xEB" + b"\xD7"


def square(**kw):
    return swizzle(square_plain(**kw))


def insert_before(plain, op, cmd):
    """Insert cmd before the first command byte `op` (an opcode >= 0x80 is unique to command starts)."""
    i = plain.index(bytes([op]))
    return plain[:i] + cmd + plain[i:], i


def header_end(plain):
    return plain.index(b"\xD8\x00") + 2


# --- fake controller -------------------------------------------------------------------------------------------------

class FakeController:
    """UDP on 127.0.0.1: ACKs every datagram, answers DA 00 reads from `mem`, and counts a stored file when a job
    datagram ends with D7. Knobs: drop (job packet numbers whose ACK is withheld), nak (answered with NAK),
    spoof_from (send the withheld ACK from this other IP instead), ack_delay (seconds before each job ACK),
    step (how far the file count moves per stored file)."""

    def __init__(self, status=0x08000000, count=5, step=1, ip="127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((ip, 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.mem = {0x0200: status, FILE_COUNT: count}
        self.names = [f"OLD{i:05d}" for i in range(count)]
        self.storing = None
        self.step, self.drop, self.nak, self.spoof_from, self.ack_delay = step, set(), set(), None, 0.0
        self.got, self.log, self.job_packets, self.bad_frames = [], [], 0, 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def reset(self):
        self.drop, self.nak, self.job_packets = set(), set(), 0

    def jobs(self):
        return [p for p in self.got if not p.startswith((b"\xDA\x00", b"\xE8\x01"))]

    def _send(self, raw, addr, sock=None):
        (sock or self.sock).sendto(raw, addr)
        self.log.append(("sent", time.monotonic(), raw))

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                continue
            if (sum(data[2:]) & 0xFFFF).to_bytes(2, "big") != data[:2]:
                self.bad_frames += 1
                continue
            payload = unswizzle(data[2:])
            self.got.append(payload)
            self.log.append(("got", time.monotonic(), payload))
            if payload.startswith(b"\xDA\x00"):
                self._send(ACK, addr)
                self._send(swizzle(b"\xDA\x01" + payload[2:4] + e35(self.mem.get(u14(payload[2:4]), 0))), addr)
                continue
            if payload.startswith(b"\xe8\x01"):
                index = u14(payload[2:4]) - 1
                self._send(ACK, addr)
                if 0 <= index < len(self.names):
                    self._send(swizzle(payload + self.names[index].encode() + b"\x00"), addr)
                continue
            if payload.startswith(b"\xe8\x02\xe7\x01"):
                self.storing = payload[4:10].decode()
            self.job_packets += 1
            k = self.job_packets
            if k in self.drop:
                if self.spoof_from:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                        s.bind((self.spoof_from, 0))
                        self._send(ACK, addr, s)
                continue
            if self.ack_delay:
                time.sleep(self.ack_delay)
            self._send(swizzle(b"\xCF") if k in self.nak else ACK, addr)
            if k not in self.nak and payload.endswith(b"\xD7"):
                self.mem[FILE_COUNT] += self.step
                self.names.append(self.storing)

    def close(self):
        self._stop.set()
        self._t.join(2)
        self.sock.close()

    def assert_clean(self, reads):
        """G11: every datagram the gate sent decodes to ALLOWED commands, the envelope, or an allowlisted DA 00."""
        assert self.bad_frames == 0, "a datagram had a wrong checksum"
        for p in self.got:
            guard(p, reads)


def gate_for(fake, **over):
    cfg = dict(controller_ip="127.0.0.1", bind_ip="127.0.0.1", ack_timeout_s=0.2, store_enabled=True,
               file_count_addr=f"{FILE_COUNT:04X}", min_power_pct=10.0, names_verified=True, max_stored_files=128,
               idle_status_mask=(1 << 27) | (1 << 16) | (1 << 15), idle_status_value=1 << 27)
    cfg.update(over)
    return Gate(Config(**cfg), TOKEN, send_port=fake.port if fake else _closed_udp_port(), recv_port=0)


def _closed_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- gate HTTP server on loopback ------------------------------------------------------------------------------------

class Running:
    def __init__(self, gate):
        self.srv = Server(gate, ("127.0.0.1", 0))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, token=TOKEN, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null")
        c.close()
        return out

    def raw(self, request, wait=3.0):
        """Send raw bytes, return whatever the server answers within `wait` seconds (b"" if nothing)."""
        out = b""
        with socket.create_connection(("127.0.0.1", self.port), timeout=wait) as s:
            s.sendall(request)
            try:
                while chunk := s.recv(4096):  # the gate closes after every answer
                    out += chunk
            except socket.timeout:
                pass
        return out

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


if __name__ == "__main__":
    Path(sys.argv[1]).write_bytes(square())
    print(f"wrote {sys.argv[1]}")
