"""V4 Slice 2 — managed CA delivery tests.

The agent's heartbeat now carries the per-VP CA cert PEM + fingerprint so
the cloud can bundle it into the member's `.orca_printer` + installer
instead of asking the operator to paste it into OrcaSlicer's printer.cer.

Coverage:
- read_vp_ca returns the PEM + sha256 fingerprint when the file exists
- read_vp_ca returns None when missing, unreadable, or malformed
- heartbeat_payload emits `virtualPrinterCa` only when both halves are
  present + within the 8KB size cap
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from makeros_hub.agent import heartbeat_payload
from makeros_hub.vprinter.cert import ensure_certificates, read_vp_ca


class TestReadVpCa(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_pem_and_fingerprint_after_ensure(self):
        # Bootstrapping the certs by calling ensure_certificates seeds the
        # exact on-disk layout the helper reads back.
        bundle = ensure_certificates(self.base_dir / "00M09VP263307879", "00M09VP263307879", "100.65.225.25")
        out = read_vp_ca(self.base_dir, "00M09VP263307879")
        self.assertIsNotNone(out)
        if out is None:
            return  # narrow for mypy/pyright
        pem, fp = out
        self.assertIn("-----BEGIN CERTIFICATE-----", pem)
        self.assertIn("-----END CERTIFICATE-----", pem)
        self.assertEqual(fp, bundle.ca_fingerprint_sha256)

    def test_returns_none_when_serial_dir_missing(self):
        self.assertIsNone(read_vp_ca(self.base_dir, "nonexistent-serial"))

    def test_returns_none_when_file_is_malformed(self):
        ca_path = self.base_dir / "fakeserial" / "certs" / "ca.crt"
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_bytes(b"this is not a real PEM certificate")
        self.assertIsNone(read_vp_ca(self.base_dir, "fakeserial"))

    def test_serial_with_unsafe_chars_normalized_to_underscore(self):
        # The production VirtualPrinterManager normalizes the serial into the
        # base_dir BEFORE calling ensure_certificates; the helper does the
        # same normalization on lookup. So a serial like "weird/serial!"
        # becomes "weird_serial_" in both the write path and the read path
        # — meeting in the middle.
        raw_serial = "weird/serial!"
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw_serial)
        ensure_certificates(self.base_dir / safe, raw_serial, "100.64.0.1")
        out = read_vp_ca(self.base_dir, raw_serial)
        self.assertIsNotNone(out)


class TestHeartbeatPayloadVpCa(unittest.TestCase):
    def test_omits_field_when_vp_ca_is_none(self):
        payload = heartbeat_payload()
        self.assertNotIn("virtualPrinterCa", payload)

    def test_includes_field_when_vp_ca_present(self):
        pem = "-----BEGIN CERTIFICATE-----\nfakebytes\n-----END CERTIFICATE-----\n"
        fp = "01:02:03"
        payload = heartbeat_payload(vp_ca=(pem, fp))
        self.assertEqual(
            payload["virtualPrinterCa"],
            {"caCertPem": pem, "caFingerprintSha256": fp},
        )

    def test_drops_when_pem_exceeds_size_cap(self):
        # 10 KB pem (> 8 KB cap) → field is omitted.
        pem = "x" * (10 * 1024)
        payload = heartbeat_payload(vp_ca=(pem, "fp"))
        self.assertNotIn("virtualPrinterCa", payload)

    def test_drops_when_either_half_empty(self):
        for pair in [("", "fp"), ("pem", "")]:
            with self.subTest(pair=pair):
                payload = heartbeat_payload(vp_ca=pair)
                self.assertNotIn("virtualPrinterCa", payload)


if __name__ == "__main__":
    unittest.main()
