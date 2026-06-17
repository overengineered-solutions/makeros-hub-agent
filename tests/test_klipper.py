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

            def do_POST(self):
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


class StubMoonrakerPathCapture:
    """Like StubMoonraker but records which paths got POSTed to so we can
    assert command-name → endpoint translation (e.g. 'stop' → /cancel)."""

    def __init__(self):
        self.captured_paths: list[str] = []
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    def start(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                outer.captured_paths.append(self.path)
                body = b'{"result":"ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args, **_kw):
                return

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

    def test_pending_jobs_empty_initially(self):
        # No HTTP needed — a fresh adapter has no observed jobs.
        adapter = KlipperAdapter("printer-1", "http://127.0.0.1:1")
        self.assertEqual(adapter.pending_jobs(), [])

    def test_unsupported_command_returns_directional_reason(self):
        # Bambu-only commands (ams_dry, skip_objects) round-trip a clear
        # unsupported_command result so the cloud admin UI can render a
        # directional message.
        adapter = KlipperAdapter("printer-1", "http://127.0.0.1:1")
        out = adapter.send_command("ams_dry", {})
        self.assertEqual(out["ok"], False)
        self.assertEqual(out["reason"], "unsupported_command")
        self.assertEqual(out["command"], "ams_dry")

    def test_send_command_pause_succeeds_against_stub_moonraker(self):
        # The stub doesn't differentiate paths — it returns 200 + {"result":
        # "ok"} for any POST. We verify the adapter POSTs to the right path
        # and surfaces the success.
        stub = StubMoonraker(response_body={"result": "ok"})
        stub.start()
        try:
            adapter = KlipperAdapter("printer-1", stub.url)
            out = adapter.send_command("pause")
            self.assertEqual(out["ok"], True)
            self.assertEqual(out["command"], "pause")
            self.assertEqual(out["result"], "ok")
            # One POST landed; stub counts every request.
            self.assertEqual(stub.request_count, 1)
        finally:
            stub.stop()

    def test_send_command_stop_translates_to_cancel(self):
        # The cloud command is 'stop' (Bambu-aligned vocabulary). The
        # adapter MUST translate to Moonraker's /printer/print/cancel.
        # Verify with a stub that records the request path.
        stub = StubMoonrakerPathCapture()
        stub.start()
        try:
            adapter = KlipperAdapter("printer-1", stub.url)
            out = adapter.send_command("stop")
            self.assertEqual(out["ok"], True)
            self.assertIn("/printer/print/cancel", stub.captured_paths)
        finally:
            stub.stop()


class StubMoonrakerErrorPath(http.server.BaseHTTPRequestHandler):
    """Returns 400 + Moonraker-shaped error body for any POST. Used to
    verify the adapter's HTTP-error → directional-reason classifier."""

    def do_POST(self):
        body = b'{"error": {"message": "Print is not currently paused"}}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args, **_kw):
        return


def _start_stub_error_server() -> tuple[http.server.HTTPServer, str]:
    server = http.server.HTTPServer(("127.0.0.1", 0), StubMoonrakerErrorPath)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


class TestKlipperControlErrors(unittest.TestCase):
    def test_400_resume_when_not_paused_returns_not_paused(self):
        server, url = _start_stub_error_server()
        try:
            adapter = KlipperAdapter("printer-1", url)
            out = adapter.send_command("resume")
            self.assertEqual(out["ok"], False)
            self.assertEqual(out["reason"], "not_paused")
            self.assertEqual(out["httpStatus"], 400)
        finally:
            server.shutdown()
            server.server_close()

    def test_unreachable_url_returns_unreachable_reason(self):
        # Port 1 is closed — TCP connect refused.
        adapter = KlipperAdapter("printer-1", "http://127.0.0.1:1")
        out = adapter.send_command("pause")
        self.assertEqual(out["ok"], False)
        self.assertIn(out["reason"], ("unreachable", "timeout"))


# ---------------------------------------------------------------------------
# KlipperJobTracker — terminal-job emission from observed state transitions
# ---------------------------------------------------------------------------


class TestKlipperJobTracker(unittest.TestCase):
    def _import(self):
        from makeros_hub.printers.klipper import KlipperJobTracker

        return KlipperJobTracker

    def test_no_jobs_in_pre_print_states(self):
        Track = self._import()
        t = Track("p1")
        # Brand-new printer reporting standby (or no state) emits nothing.
        t.observe({"print_stats": {"state": "standby", "filename": ""}}, 0.0)
        t.observe({"print_stats": {"state": "standby", "filename": ""}}, 1.0)
        self.assertEqual(t.pending(), [])

    def test_open_on_transition_to_printing(self):
        Track = self._import()
        t = Track("p1")
        t.observe({"print_stats": {"state": "standby", "filename": ""}}, 0.0)
        t.observe(
            {"print_stats": {"state": "printing", "filename": "rocket.gcode"}}, 5.0
        )
        # No terminal yet, so no pending — but the active job is open.
        self.assertEqual(t.pending(), [])

    def test_close_on_complete_emits_done_job(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "rocket.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "complete", "filename": "rocket.gcode"}}, 600.0
        )
        pending = t.pending()
        self.assertEqual(len(pending), 1)
        job = pending[0]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["filename"], "rocket.gcode")
        self.assertEqual(job["printerId"], "p1")
        self.assertEqual(job["printTimeSeconds"], 600)
        self.assertTrue(job["jobKey"].startswith("fp_"))
        # ISO Z timestamp shape
        self.assertRegex(job["startedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(job["endedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_close_on_cancelled_emits_failed_job(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "x.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "cancelled", "filename": "x.gcode"}}, 100.0
        )
        pending = t.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "failed")

    def test_close_on_error_emits_failed_job(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "x.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "error", "filename": "x.gcode"}}, 50.0
        )
        self.assertEqual(t.pending()[0]["status"], "failed")

    def test_pause_does_not_open_or_close(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "x.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "paused", "filename": "x.gcode"}}, 30.0
        )
        # Active job is still open; no terminal job emitted yet.
        self.assertEqual(t.pending(), [])
        t.observe(
            {"print_stats": {"state": "printing", "filename": "x.gcode"}}, 60.0
        )
        t.observe(
            {"print_stats": {"state": "complete", "filename": "x.gcode"}}, 120.0
        )
        # ONE done job — pause didn't artificially close it.
        pending = t.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "done")
        # printTime accumulates from the open at t=0, not from the pause.
        self.assertEqual(pending[0]["printTimeSeconds"], 120)

    def test_filename_change_mid_print_closes_old_and_opens_new(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "a.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "printing", "filename": "b.gcode"}}, 60.0
        )
        # Old one closes as cancelled (we missed the gap)
        pending_after_swap = t.pending()
        self.assertEqual(len(pending_after_swap), 1)
        self.assertEqual(pending_after_swap[0]["status"], "cancelled")
        self.assertEqual(pending_after_swap[0]["filename"], "a.gcode")
        # New one is open
        t.observe(
            {"print_stats": {"state": "complete", "filename": "b.gcode"}}, 200.0
        )
        pending_final = t.pending()
        self.assertEqual(len(pending_final), 2)
        self.assertEqual(pending_final[1]["status"], "done")
        self.assertEqual(pending_final[1]["filename"], "b.gcode")

    def test_standby_after_printing_closes_as_cancelled(self):
        # Klipper sometimes drops back to standby without emitting an
        # explicit cancelled state (e.g. the operator manually reset).
        # The tracker treats this as missed-end → cancelled.
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "x.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "standby", "filename": "x.gcode"}}, 100.0
        )
        pending = t.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "cancelled")

    def test_recovered_terminal_with_filename_emits_done(self):
        # Agent restarted onto an already-complete printer that still
        # reports its prior filename. Emit ONE recovered job.
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "complete", "filename": "boot.gcode"}}, 0.0
        )
        pending = t.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "done")
        self.assertEqual(pending[0]["filename"], "boot.gcode")
        # Subsequent same-terminal observations must NOT re-emit (we set
        # _last_state, so the gate fires once only).
        t.observe(
            {"print_stats": {"state": "complete", "filename": "boot.gcode"}}, 5.0
        )
        self.assertEqual(len(t.pending()), 1)

    def test_recovered_terminal_with_no_filename_skips(self):
        Track = self._import()
        t = Track("p1")
        t.observe({"print_stats": {"state": "complete"}}, 0.0)
        self.assertEqual(t.pending(), [])

    def test_ack_clears_only_listed_keys(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "a.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "complete", "filename": "a.gcode"}}, 60.0
        )
        t.observe(
            {"print_stats": {"state": "printing", "filename": "b.gcode"}}, 100.0
        )
        t.observe(
            {"print_stats": {"state": "complete", "filename": "b.gcode"}}, 200.0
        )
        pending = t.pending()
        self.assertEqual(len(pending), 2)
        t.ack([pending[0]["jobKey"]])
        remaining = t.pending()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["jobKey"], pending[1]["jobKey"])

    def test_ack_unknown_key_is_noop(self):
        Track = self._import()
        t = Track("p1")
        t.observe(
            {"print_stats": {"state": "printing", "filename": "a.gcode"}}, 0.0
        )
        t.observe(
            {"print_stats": {"state": "complete", "filename": "a.gcode"}}, 60.0
        )
        t.ack(["fp_does_not_exist"])
        self.assertEqual(len(t.pending()), 1)

    def test_long_filename_truncated_to_300_chars(self):
        Track = self._import()
        t = Track("p1")
        big = "x" * 500
        t.observe({"print_stats": {"state": "printing", "filename": big}}, 0.0)
        t.observe({"print_stats": {"state": "complete", "filename": big}}, 1.0)
        pending = t.pending()
        self.assertEqual(len(pending[0]["filename"]), 300)


class TestKlipperAdapterJobIngestEndToEnd(unittest.TestCase):
    """Verify the adapter feeds the job tracker on each successful poll.

    The stub Moonraker walks through a small state sequence + we poll a few
    times to confirm the terminal job lands in pending_jobs().
    """

    def test_full_print_cycle_emits_done_via_poll(self):
        seq = [
            {
                "result": {
                    "status": {
                        "print_stats": {
                            "state": "printing",
                            "filename": "a.gcode",
                            "print_duration": 10,
                        },
                        "virtual_sdcard": {"progress": 0.1},
                    }
                }
            },
            {
                "result": {
                    "status": {
                        "print_stats": {
                            "state": "complete",
                            "filename": "a.gcode",
                            "print_duration": 600,
                        },
                        "virtual_sdcard": {"progress": 1.0},
                    }
                }
            },
        ]
        # Spin up a stub that cycles through the sequence on each GET.
        cursor = {"i": 0}

        class CyclingHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                i = cursor["i"]
                cursor["i"] = min(i + 1, len(seq) - 1)
                body = json.dumps(seq[i]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args, **_kw):
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), CyclingHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            adapter = KlipperAdapter("p1", f"http://127.0.0.1:{port}")
            adapter.start()
            try:
                # Wait for both polls to land + the terminal job to surface.
                ok = _wait_until(lambda: len(adapter.pending_jobs()) >= 1, timeout=20.0)
                self.assertTrue(ok, "expected a terminal job after the printing→complete cycle")
                jobs = adapter.pending_jobs()
                self.assertEqual(jobs[0]["status"], "done")
                self.assertEqual(jobs[0]["filename"], "a.gcode")
            finally:
                adapter.stop()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
