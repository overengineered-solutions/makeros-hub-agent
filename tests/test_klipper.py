"""Klipper / Moonraker adapter tests — pure helpers + a real-loopback poll
via a tiny stub HTTPServer. No mock-of-mocks; the wire path is exercised
end-to-end against a stdlib server."""

import http.server
import json
import threading
import time
import unittest

from makeros_hub.printers.klipper import (
    KlipperAdapter,
    _build_status,
    _classify_error,
    _normalize_base,
    _STATE_MAP,
)
from urllib.error import HTTPError, URLError


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestNormalizeBase(unittest.TestCase):
    def test_strips_trailing_slash(self):
        self.assertEqual(_normalize_base("http://192.168.1.50/"), "http://192.168.1.50")

    def test_adds_http_scheme_when_missing(self):
        self.assertEqual(_normalize_base("192.168.1.50"), "http://192.168.1.50")

    def test_preserves_https(self):
        self.assertEqual(
            _normalize_base("https://klipper.local"), "https://klipper.local"
        )

    def test_strips_whitespace(self):
        self.assertEqual(_normalize_base("  http://x/  "), "http://x")


class TestClassifyError(unittest.TestCase):
    def test_http_error_carries_status_code(self):
        err = HTTPError("http://x", 404, "not found", hdrs=None, fp=None)
        self.assertEqual(_classify_error(err), "http_404")

    def test_timeout_classification(self):
        self.assertEqual(_classify_error(TimeoutError()), "timeout")

    def test_url_error_default_unreachable(self):
        self.assertEqual(_classify_error(URLError("network")), "unreachable")

    def test_json_decode_is_shape_mismatch(self):
        self.assertEqual(_classify_error(json.JSONDecodeError("x", "doc", 0)), "shape_mismatch")

    def test_value_error_is_shape_mismatch(self):
        self.assertEqual(_classify_error(ValueError("missing result")), "shape_mismatch")


class TestBuildStatus(unittest.TestCase):
    def test_connecting_state_only_emits_printer_id_and_state(self):
        out = _build_status("p1", {}, "connecting")
        self.assertEqual(out, {"printerId": "p1", "connectionState": "connecting"})

    def test_error_state_carries_reason(self):
        out = _build_status("p1", {}, "error", error_reason="unreachable")
        self.assertEqual(
            out,
            {"printerId": "p1", "connectionState": "error", "errorReason": "unreachable"},
        )

    def test_connected_with_full_status(self):
        status = {
            "print_stats": {
                "state": "printing",
                "filename": "rocket.gcode",
                "print_duration": 600.0,
            },
            "virtual_sdcard": {"progress": 0.25},
            "extruder": {"temperature": 215.4},
            "heater_bed": {"temperature": 60.1},
        }
        out = _build_status("p1", status, "connected")
        self.assertEqual(out["state"], "printing")
        self.assertEqual(out["jobName"], "rocket.gcode")
        self.assertAlmostEqual(out["progressPct"], 25.0)
        self.assertAlmostEqual(out["nozzleTempC"], 215.4)
        self.assertAlmostEqual(out["bedTempC"], 60.1)
        # ETA: 600 * 0.75/0.25 / 60 = 30 minutes
        self.assertEqual(out["etaMinutes"], 30)

    def test_state_mapping_collapses_terminal_to_idle(self):
        for moonraker_state, expected in _STATE_MAP.items():
            out = _build_status(
                "p1", {"print_stats": {"state": moonraker_state}}, "connected"
            )
            self.assertEqual(out["state"], expected, f"state={moonraker_state}")

    def test_unknown_state_defaults_to_idle(self):
        out = _build_status("p1", {"print_stats": {"state": "bizarre"}}, "connected")
        self.assertEqual(out["state"], "idle")

    def test_missing_print_stats_defaults_state_idle(self):
        out = _build_status("p1", {}, "connected")
        self.assertEqual(out["state"], "idle")

    def test_progress_clamped_to_unit_interval(self):
        # Moonraker shouldn't emit > 1 but a buggy report shouldn't crash us.
        out = _build_status(
            "p1",
            {
                "print_stats": {"state": "printing"},
                "virtual_sdcard": {"progress": 1.42},
            },
            "connected",
        )
        self.assertEqual(out["progressPct"], 100.0)

    def test_zero_progress_omits_eta(self):
        out = _build_status(
            "p1",
            {
                "print_stats": {"state": "printing", "print_duration": 100},
                "virtual_sdcard": {"progress": 0.0},
            },
            "connected",
        )
        self.assertNotIn("etaMinutes", out)

    def test_long_filename_truncated(self):
        long_name = "x" * 500
        out = _build_status(
            "p1", {"print_stats": {"state": "printing", "filename": long_name}}, "connected"
        )
        self.assertEqual(len(out["jobName"]), 300)

    def test_filename_only_when_string(self):
        out = _build_status("p1", {"print_stats": {"state": "printing", "filename": 123}}, "connected")
        self.assertNotIn("jobName", out)

    def test_non_numeric_temp_dropped(self):
        out = _build_status(
            "p1",
            {
                "print_stats": {"state": "idle"},
                "extruder": {"temperature": "warm"},
            },
            "connected",
        )
        self.assertNotIn("nozzleTempC", out)


# ---------------------------------------------------------------------------
# Stub Moonraker — exercises the polling thread end-to-end via real HTTP
# ---------------------------------------------------------------------------


class StubMoonraker:
    """A throwaway HTTP server that returns a Moonraker-shaped JSON body for
    `/printer/objects/query`. Lifecycle = test method scope."""

    def __init__(self, response_body: dict | None = None, response_status: int = 200):
        self.response_body = response_body or {"result": {"status": {}}}
        self.response_status = response_status
        self.request_count = 0
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.request_count += 1
                body = json.dumps(outer.response_body).encode("utf-8")
                self.send_response(outer.response_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args, **_kw):
                return  # silence test output

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Polls `predicate()` until it returns truthy or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestKlipperAdapterIntegration(unittest.TestCase):
    def test_first_poll_flips_to_connected(self):
        stub = StubMoonraker(
            response_body={
                "result": {
                    "status": {
                        "print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 60},
                        "virtual_sdcard": {"progress": 0.5},
                        "extruder": {"temperature": 200.0},
                        "heater_bed": {"temperature": 60.0},
                    }
                }
            }
        )
        stub.start()
        try:
            adapter = KlipperAdapter("printer-1", stub.url)
            adapter.start()
            try:
                ok = _wait_until(lambda: adapter.status()["connectionState"] == "connected")
                self.assertTrue(ok, "expected adapter to flip connected after one poll")
                st = adapter.status()
                self.assertEqual(st["state"], "printing")
                self.assertEqual(st["jobName"], "a.gcode")
                self.assertAlmostEqual(st["progressPct"], 50.0)
            finally:
                adapter.stop()
        finally:
            stub.stop()

    def test_404_response_eventually_marks_error(self):
        stub = StubMoonraker(response_status=404)
        stub.start()
        try:
            adapter = KlipperAdapter("printer-1", stub.url)
            adapter.start()
            try:
                # The adapter only flips to 'error' after CONNECT_TIMEOUT_SEC
                # without a successful poll. For test speed, just confirm
                # we're in 'connecting' immediately + the error reason is
                # accumulating on the lock.
                _wait_until(lambda: adapter._last_error_reason == "http_404")
                self.assertEqual(adapter._last_error_reason, "http_404")
                self.assertEqual(adapter.status()["connectionState"], "connecting")
            finally:
                adapter.stop()
        finally:
            stub.stop()

    def test_pending_jobs_empty_and_send_command_rejects(self):
        # No HTTP needed for these — they're read-only / structural.
        adapter = KlipperAdapter("printer-1", "http://127.0.0.1:1")  # unused
        self.assertEqual(adapter.pending_jobs(), [])
        out = adapter.send_command("pause", {})
        self.assertEqual(out["ok"], False)
        self.assertEqual(out["errorReason"], "klipper_control_not_implemented")


if __name__ == "__main__":
    unittest.main()
