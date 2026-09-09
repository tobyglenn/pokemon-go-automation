from __future__ import annotations

from tests import support as _test_support

import subprocess
import unittest
from unittest import mock

from sources import ios_wda_cleanup


# Real `devicectl device info displays` output from the SE, face up on a desk
# with its screen off -- the state that made a GBL run fail with
# "xcodebuild failed with code 65" every time.
ASLEEP = """07:27:35  Acquired tunnel connection to device.
07:27:35  Acquired usage assertion.
Current Displays:
- 1: LCD (primary):
    - bounds: (0.0, 0.0, 750.0, 1334.0)
    - displayId: 1
    - nativeSize: (750.0, 1334.0)
Main display backlight state: backlight is off
Main display orientation: faceUp, non-flat orientation: portrait
"""

AWAKE = ASLEEP.replace("backlight is off", "backlight is on")


class BacklightTests(unittest.TestCase):
    def test_a_dark_screen_is_reported(self) -> None:
        with mock.patch.object(ios_wda_cleanup, "run_output", return_value=ASLEEP):
            self.assertIs(ios_wda_cleanup.display_backlight("udid"), False)

    def test_a_lit_screen_is_reported(self) -> None:
        with mock.patch.object(ios_wda_cleanup, "run_output", return_value=AWAKE):
            self.assertIs(ios_wda_cleanup.display_backlight("udid"), True)

    def test_no_verdict_when_devicectl_says_nothing_about_the_backlight(self) -> None:
        """Better no answer than a guess: an unread phone is not a dark one."""
        with mock.patch.object(
            ios_wda_cleanup, "run_output", return_value="Current Displays:\n"
        ):
            self.assertIsNone(ios_wda_cleanup.display_backlight("udid"))

    def test_no_verdict_when_devicectl_cannot_be_run(self) -> None:
        for failure in (
            FileNotFoundError("xcrun"),
            subprocess.TimeoutExpired("devicectl", 60),
        ):
            with mock.patch.object(ios_wda_cleanup, "run_output", side_effect=failure):
                self.assertIsNone(ios_wda_cleanup.display_backlight("udid"))


class AsleepMessageTests(unittest.TestCase):
    def test_a_sleeping_phone_is_named_along_with_the_fix(self) -> None:
        with mock.patch.object(
            ios_wda_cleanup, "display_backlight", return_value=False
        ):
            message = ios_wda_cleanup.asleep_message("udid", "Second iPhone (SE)")
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("Second iPhone (SE)", message)
        self.assertIn("side button", message)
        self.assertIn("Auto-Lock", message)

    def test_an_awake_phone_produces_no_complaint(self) -> None:
        with mock.patch.object(
            ios_wda_cleanup, "display_backlight", return_value=True
        ):
            self.assertIsNone(ios_wda_cleanup.asleep_message("udid"))

    def test_an_unreadable_phone_is_left_to_try_its_luck(self) -> None:
        """Only a definite 'off' stops a run; devicectl going quiet must not."""
        with mock.patch.object(
            ios_wda_cleanup, "display_backlight", return_value=None
        ):
            self.assertIsNone(ios_wda_cleanup.asleep_message("udid"))


if __name__ == "__main__":
    unittest.main()
