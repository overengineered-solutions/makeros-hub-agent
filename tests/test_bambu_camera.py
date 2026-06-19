import socket
import ssl
import struct
import unittest
from unittest import mock

from makeros_hub.printers import bambu_camera
from makeros_hub.printers.bambu_camera import _auth_packet, _read_one_jpeg, capture_frame

JPEG = b"\xff\xd8\xff\xe0" + b"frame-payload-bytes" + b"\xff\xd9"


class FakeSock:
    """Minimal recv() stand-in: pops queued chunks, then '' (peer closed)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""


class TestAuthPacket(unittest.TestCase):
    def test_shape_and_fields(self):
        p = _auth_packet("AbCd1234")
        self.assertEqual(len(p), 80)
        self.assertEqual(struct.unpack("<IIII", p[:16]), (0x40, 0x3000, 0, 0))
        self.assertEqual(p[16:20], b"bblp")
        self.assertEqual(p[20:48], b"\x00" * 28)  # username null-padded to 32
        self.assertEqual(p[48:56], b"AbCd1234")
        self.assertEqual(p[56:80], b"\x00" * 24)  # access code null-padded to 32

    def test_overlong_code_truncated_to_32(self):
        p = _auth_packet("X" * 50)
        self.assertEqual(len(p), 80)
        self.assertEqual(p[48:80], b"X" * 32)


class TestReadOneJpeg(unittest.TestCase):
    def test_single_chunk(self):
        self.assertEqual(_read_one_jpeg(FakeSock([JPEG]), 1 << 20), JPEG)

    def test_split_across_chunks(self):
        mid = len(JPEG) // 2
        self.assertEqual(_read_one_jpeg(FakeSock([JPEG[:mid], JPEG[mid:]]), 1 << 20), JPEG)

    def test_skips_leading_garbage_before_soi(self):
        self.assertEqual(_read_one_jpeg(FakeSock([b"\x00\x11garbage", JPEG]), 1 << 20), JPEG)

    def test_soi_marker_straddling_two_chunks(self):
        # SOI split so 3 bytes land in chunk 1, the rest + frame in chunk 2.
        self.assertEqual(_read_one_jpeg(FakeSock([b"\xff\xd8\xff", b"\xe0body\xff\xd9"]), 1 << 20),
                         b"\xff\xd8\xff\xe0body\xff\xd9")

    def test_closed_before_eoi_returns_none(self):
        self.assertIsNone(_read_one_jpeg(FakeSock([b"\xff\xd8\xff\xe0partial"]), 1 << 20))

    def test_capped_when_soi_seen_but_eoi_never_arrives(self):
        chunks = [b"\xff\xd8\xff\xe0"] + [b"\x41" * 4096] * 50
        self.assertIsNone(_read_one_jpeg(FakeSock(chunks), 5000))


class TestCaptureFrameGuards(unittest.TestCase):
    def test_returns_none_on_empty_host_or_code(self):
        self.assertIsNone(capture_frame("", "code"))
        self.assertIsNone(capture_frame("1.2.3.4", ""))


class _FakeRaw:
    """Context-manager stand-in for the raw TCP socket."""

    def settimeout(self, _t):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeTLS:
    """Context-manager stand-in for the wrapped TLS socket; recv() pops chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def sendall(self, _b):
        pass

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeCtx:
    """Stand-in for ssl.SSLContext: wrap_socket returns a TLS sock or raises."""

    check_hostname = True
    verify_mode = None

    def __init__(self, tls_or_exc):
        self._t = tls_or_exc

    def wrap_socket(self, _raw, server_hostname=None):
        if isinstance(self._t, BaseException):
            raise self._t
        return self._t


class TestCaptureFrameWithReason(unittest.TestCase):
    """v0.42.0: the :6000 path categorizes failures (parity with the RTSP path)
    so an A1/P1 failure is diagnosable instead of a silent 'unknown'."""

    def _patch(self, *, connect=None, ctx=None):
        # connect: an exception to raise from create_connection, or None to
        # return a _FakeRaw. ctx: the _FakeCtx to use for wrap_socket.
        patches = []
        if connect is not None:
            patches.append(
                mock.patch.object(bambu_camera.socket, "create_connection", side_effect=connect)
            )
        else:
            patches.append(
                mock.patch.object(
                    bambu_camera.socket, "create_connection", return_value=_FakeRaw()
                )
            )
        if ctx is not None:
            patches.append(mock.patch.object(bambu_camera.ssl, "SSLContext", return_value=ctx))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_empty_host_is_unreachable(self):
        r = bambu_camera.capture_frame_with_reason("", "code")
        self.assertEqual(r.reason, "unreachable")

    def test_success(self):
        self._patch(ctx=_FakeCtx(_FakeTLS([JPEG])))
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertEqual(r.jpeg, JPEG)
        self.assertIsNone(r.reason)

    def test_clean_close_no_frame_is_liveview_off(self):
        self._patch(ctx=_FakeCtx(_FakeTLS([])))  # peer closes, no JPEG
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertIsNone(r.jpeg)
        self.assertEqual(r.reason, "liveview-off")

    def test_tls_error(self):
        self._patch(ctx=_FakeCtx(ssl.SSLError("handshake failure")))
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertEqual(r.reason, "tls-error")

    def test_connection_refused_is_unreachable(self):
        self._patch(connect=ConnectionRefusedError())
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertEqual(r.reason, "unreachable")

    def test_timeout(self):
        self._patch(connect=socket.timeout())
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertEqual(r.reason, "timeout")

    def test_recv_timeout_is_timeout_not_liveview_off(self):
        # A stalled :6000 stream (recv timeout mid-frame) must surface as
        # 'timeout', not collapse into the generic 'liveview-off' (Codex MEDIUM).
        class _TLSRecvTimeout(_FakeTLS):
            def recv(self, _n):
                raise socket.timeout()

        self._patch(ctx=_FakeCtx(_TLSRecvTimeout([])))
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertEqual(r.reason, "timeout")

    def test_host_unreachable_oserror(self):
        self._patch(connect=OSError("No route to host"))
        r = bambu_camera.capture_frame_with_reason("1.2.3.4", "code")
        self.assertEqual(r.reason, "unreachable")

    def test_capture_frame_shim_returns_bytes(self):
        self._patch(ctx=_FakeCtx(_FakeTLS([JPEG])))
        self.assertEqual(capture_frame("1.2.3.4", "code"), JPEG)


if __name__ == "__main__":
    unittest.main()
