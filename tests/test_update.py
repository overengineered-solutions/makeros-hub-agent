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
            ap.assert_called_once_with("v0.4.0")

    def test_skips_when_cooled_down(self):
        with mock.patch.object(update, "recently_attempted", return_value=True), mock.patch.object(
            update, "apply_update"
        ) as ap:
            self.assertFalse(update.maybe_update("0.3.0", "v0.4.0"))
            ap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
