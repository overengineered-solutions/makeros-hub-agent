import struct
import unittest

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


if __name__ == "__main__":
    unittest.main()
