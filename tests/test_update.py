"""Stdlib-only tests for the OTA self-update decision logic. The version-compare
+ safety gates are pure; apply_update (subprocess) is integration-tested on the Pi.
Run: python3 -m unittest discover -s tests"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from makeros_hub import update


class TestParseVersion(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(update.parse_version("v0.3.0"), (0, 3, 0))
        self.assertEqual(update.parse_version("0.3.0"), (0, 3, 0))
        self.assertEqual(update.parse_version("v12.4.9"), (12, 4, 9))

    def test_invalid(self):
        for bad in ["main", "v0.3", "0.3", "", "v0.3.0-rc1", None, 3, "v0.3.0; rm -rf /"]:
            self.assertIsNone(update.parse_version(bad), bad)


class TestIsReleaseTag(unittest.TestCase):
    def test_only_strict_vX_Y_Z(self):
        self.assertTrue(update.is_release_tag("v0.3.0"))
        self.assertTrue(update.is_release_tag("v1.0.0"))
        for bad in ["0.3.0", "main", "v0.3", "HEAD", "v0.3.0 ", "v0.3.0;rm", "feat/x", None]:
            self.assertFalse(update.is_release_tag(bad), bad)


class TestShouldUpdate(unittest.TestCase):
    def test_updates_forward_only_to_release_tags(self):
        self.assertTrue(update.should_update("0.3.0", "v0.3.1"))
        self.assertTrue(update.should_update("0.3.0", "v1.0.0"))

    def test_refuses_equal_downgrade_and_nonrelease(self):
        self.assertFalse(update.should_update("0.3.0", "v0.3.0"))  # equal
        self.assertFalse(update.should_update("0.3.0", "v0.2.0"))  # downgrade
        self.assertFalse(update.should_update("0.3.0", "main"))  # not a release tag
        self.assertFalse(update.should_update("0.3.0", "v0.3.0; rm -rf /"))  # injection-shaped
        self.assertFalse(update.should_update("0.3.0", ""))


class TestCooldown(unittest.TestCase):
    def test_recently_attempted_window(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "last_update.json"
            with mock.patch.object(update, "STATE_PATH", p):
                update._write_state({"target": "v0.3.1", "at": 1000.0})
                # within the cooldown
                self.assertTrue(update.recently_attempted("v0.3.1", now=1000.0 + 10))
                # past the cooldown
                self.assertFalse(
                    update.recently_attempted("v0.3.1", now=1000.0 + update.ATTEMPT_COOLDOWN_SEC + 1)
                )
                # a different target is not cooled down
                self.assertFalse(update.recently_attempted("v0.3.2", now=1000.0 + 10))

    def test_attempt_cap_stops_a_non_converging_target(self):
        # v0.55: installed 3x and still running the old version ⇒ never launch again for that target.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "last_update.json"
            with mock.patch.object(update, "STATE_PATH", p), mock.patch.object(
                update, "apply_update", return_value=True
            ) as launch:
                update._write_state({"target": "v0.54.0", "at": 0.0, "attempts": update.MAX_ATTEMPTS_PER_TARGET})
                self.assertFalse(update.maybe_update("0.50.0", "v0.54.0", "a" * 40))
                launch.assert_not_called()
                # one attempt short of the cap (and past the cooldown) still launches
                update._write_state({"target": "v0.54.0", "at": 0.0, "attempts": update.MAX_ATTEMPTS_PER_TARGET - 1})
                self.assertTrue(update.maybe_update("0.50.0", "v0.54.0", "a" * 40))
                launch.assert_called_once()
                # a NEW target starts its own count
                update._write_state({"target": "v0.54.0", "at": 0.0, "attempts": 99})
                self.assertFalse(update.attempts_exhausted("v0.56.0"))

    def test_apply_update_counts_attempts_per_target(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "last_update.json"
            with mock.patch.object(update, "STATE_PATH", p), mock.patch.object(
                update.subprocess, "run", return_value=None
            ):
                for n in (1, 2):
                    self.assertTrue(update.apply_update("v0.54.0", "a" * 40))
                    self.assertEqual(update._read_state()["attempts"], n)
                self.assertTrue(update.apply_update("v0.55.0", "b" * 40))
                self.assertEqual(update._read_state()["attempts"], 1)

    def test_no_state_means_not_recent(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(update, "STATE_PATH", Path(d) / "missing.json"):
                self.assertFalse(update.recently_attempted("v0.3.1", now=1.0))


class TestMaybeUpdate(unittest.TestCase):
    def test_noop_when_not_newer(self):
        with mock.patch.object(update, "apply_update") as ap:
            self.assertFalse(update.maybe_update("0.3.0", "v0.3.0"))
            self.assertFalse(update.maybe_update("0.3.0", "main"))
            self.assertFalse(update.maybe_update("0.3.0", None))
            ap.assert_not_called()

    def test_applies_when_newer_and_not_cooled_down(self):
        with mock.patch.object(update, "recently_attempted", return_value=False), mock.patch.object(
            update, "apply_update", return_value=True
        ) as ap:
            self.assertTrue(update.maybe_update("0.3.0", "v0.4.0"))
            ap.assert_called_once_with("v0.4.0", None)

    def test_skips_when_cooled_down(self):
        with mock.patch.object(update, "recently_attempted", return_value=True), mock.patch.object(
            update, "apply_update"
        ) as ap:
            self.assertFalse(update.maybe_update("0.3.0", "v0.4.0"))
            ap.assert_not_called()


class TestIsCommitSha(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(update.is_commit_sha("a" * 40))
        self.assertTrue(update.is_commit_sha("0123456789abcdef0123456789abcdef01234567"))
        for bad in ["A" * 40, "a" * 39, "a" * 41, "main", "", None, 40, "a" * 40 + " "]:
            self.assertFalse(update.is_commit_sha(bad), bad)


class TestApplyUpdateContentTrust(unittest.TestCase):
    def test_systemd_passes_tag_and_sha_to_root_script(self):
        sha = "b" * 40
        with mock.patch.object(update, "UPDATE_MODE", "systemd"), mock.patch.object(
            update, "_write_state"
        ), mock.patch("makeros_hub.update.subprocess.run") as run:
            self.assertTrue(update.apply_update("v0.46.0", sha))
            self.assertEqual(run.call_args[0][0], ["sudo", update.UPDATE_SCRIPT, "v0.46.0", sha])

    def test_systemd_without_sha_installs_unverified(self):
        with mock.patch.object(update, "UPDATE_MODE", "systemd"), mock.patch.object(
            update, "_write_state"
        ), mock.patch("makeros_hub.update.subprocess.run") as run:
            self.assertTrue(update.apply_update("v0.46.0"))
            self.assertEqual(run.call_args[0][0], ["sudo", update.UPDATE_SCRIPT, "v0.46.0"])

    def test_refuses_malformed_sha_before_running_anything(self):
        with mock.patch.object(update, "UPDATE_MODE", "systemd"), mock.patch(
            "makeros_hub.update.subprocess.run"
        ) as run:
            self.assertFalse(update.apply_update("v0.46.0", "NOThex"))
            run.assert_not_called()

    def test_refuses_nonrelease_tag(self):
        with mock.patch("makeros_hub.update.subprocess.run") as run:
            self.assertFalse(update.apply_update("main"))
            run.assert_not_called()


class TestUpdateModes(unittest.TestCase):
    def test_disabled_ignores(self):
        with mock.patch.object(update, "UPDATE_MODE", "disabled"), mock.patch(
            "makeros_hub.update.subprocess.run"
        ) as run:
            self.assertFalse(update.apply_update("v0.46.0", "c" * 40))
            run.assert_not_called()

    def test_exit_mode_records_and_requests_repull(self):
        captured = {}
        with mock.patch.object(update, "UPDATE_MODE", "exit"), mock.patch.object(
            update, "_write_state"
        ) as ws, mock.patch.object(
            update, "_request_exit", side_effect=lambda code: captured.setdefault("code", code)
        ), mock.patch("makeros_hub.update.subprocess.run") as run:
            update.apply_update("v0.46.0", "d" * 40)
            self.assertEqual(captured["code"], update.EXIT_FOR_UPDATE_CODE)
            ws.assert_called_once()  # recorded the target for the orchestrator
            run.assert_not_called()  # no sudo/systemd in container mode

    def test_request_exit_raises_systemexit(self):
        with self.assertRaises(SystemExit) as cm:
            update._request_exit(update.EXIT_FOR_UPDATE_CODE)
        self.assertEqual(cm.exception.code, update.EXIT_FOR_UPDATE_CODE)


class TestMaybeUpdateThreadsSha(unittest.TestCase):
    def test_passes_valid_sha_through(self):
        with mock.patch.object(update, "recently_attempted", return_value=False), mock.patch.object(
            update, "apply_update", return_value=True
        ) as ap:
            self.assertTrue(update.maybe_update("0.45.0", "v0.46.0", "e" * 40))
            ap.assert_called_once_with("v0.46.0", "e" * 40)

    def test_drops_malformed_sha_to_none(self):
        with mock.patch.object(update, "recently_attempted", return_value=False), mock.patch.object(
            update, "apply_update", return_value=True
        ) as ap:
            update.maybe_update("0.45.0", "v0.46.0", "not-a-sha")
            ap.assert_called_once_with("v0.46.0", None)


if __name__ == "__main__":
    unittest.main()
