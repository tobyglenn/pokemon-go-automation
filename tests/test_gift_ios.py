from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest import mock

from sources import gift_ios


class FakeDriver:
    def __init__(
        self,
        rect=None,
        errors: list[Exception | None] | None = None,
        activation_error: Exception | None = None,
    ) -> None:
        self.rect = rect
        self.errors = list(errors or [])
        self.activation_error = activation_error
        self.activation_calls = 0
        self.quit_called = False

    def get_window_rect(self):
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        return self.rect

    def execute_script(self, name, arguments) -> None:
        self.activation_calls += 1
        if self.activation_error is not None:
            raise self.activation_error

    def quit(self) -> None:
        self.quit_called = True


class ConnectionRecoveryTests(unittest.TestCase):
    @staticmethod
    def stale_error() -> RuntimeError:
        return RuntimeError(
            "previously found element not in current view anymore; "
            "Not authorized performing UI testing actions"
        )

    def test_stale_target_app_is_activated_before_rebuilding_wda(self) -> None:
        recovered = FakeDriver(
            rect={"width": 375, "height": 667},
            errors=[self.stale_error(), None],
        )

        with mock.patch.object(gift_ios, "connect", return_value=recovered) as connect:
            driver, rect = gift_ios.connect_ready(
                {"device": {"udid": "U", "team_id": "T", "wda_bundle_id": "W"}}
            )

        self.assertIs(driver, recovered)
        self.assertEqual(rect, {"width": 375, "height": 667})
        self.assertEqual(recovered.activation_calls, 1)
        self.assertFalse(recovered.quit_called)
        connect.assert_called_once()

    def test_stale_wda_stops_without_implicit_reinstall(self) -> None:
        config = {
            "device": {"udid": "U", "team_id": "T", "wda_bundle_id": "W"}
        }
        stale = FakeDriver(
            errors=[self.stale_error()],
            activation_error=self.stale_error(),
        )
        with mock.patch.object(gift_ios, "connect", return_value=stale) as connect:
            with self.assertRaisesRegex(
                gift_ios.GifterError, "without reinstalling WebDriverAgent"
            ):
                gift_ios.connect_ready(config)

        self.assertTrue(stale.quit_called)
        connect.assert_called_once_with(config)

    def test_unrelated_connection_error_is_not_retried(self) -> None:
        broken = FakeDriver(errors=[RuntimeError("unexpected failure")])

        with mock.patch.object(gift_ios, "connect", return_value=broken) as connect:
            with self.assertRaisesRegex(RuntimeError, "unexpected failure"):
                gift_ios.connect_ready({})

        self.assertTrue(broken.quit_called)
        connect.assert_called_once_with({})


class UntilIdleTests(unittest.TestCase):
    def test_stops_immediately_when_no_outgoing_gift_remains(self) -> None:
        points = {step.name: [10, 10] for step in gift_ios.GIFT_STEPS}
        points["GIFT_SORT_BTN"] = [10, 10]
        calls = 0

        def available_side_effect(driver, point):
            nonlocal calls
            calls += 1
            return calls == 2

        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(
                gift_ios,
                "colored_action_available",
                side_effect=available_side_effect,
            ) as available,
            mock.patch.object(gift_ios, "tap"),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            completed = gift_ios.run_gifts(
                object(), {}, 100, guard=False, dry_run=False, all_mode=True
            )

        self.assertEqual(completed, 1)

    def test_count_mode_requires_send_gift_to_become_disabled(self) -> None:
        points = {step.name: [10, 10] for step in gift_ios.GIFT_STEPS}
        points["GIFT_SORT_BTN"] = [10, 10]
        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(
                gift_ios, "colored_action_available", return_value=True
            ),
            mock.patch.object(gift_ios, "tap"),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                gift_ios.GifterError, "Gift send was not confirmed"
            ):
                gift_ios.run_gifts(
                    object(), {}, 1, guard=False, dry_run=False, all_mode=False
                )

    def test_count_mode_accepts_confirmed_send(self) -> None:
        points = {step.name: [10, 10] for step in gift_ios.GIFT_STEPS}
        points["GIFT_SORT_BTN"] = [10, 10]
        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(
                gift_ios, "colored_action_available", return_value=False
            ),
            mock.patch.object(gift_ios, "tap"),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            completed = gift_ios.run_gifts(
                object(), {}, 1, guard=False, dry_run=False, all_mode=False
            )

        self.assertEqual(completed, 1)


class LatePaintingGiftTests(unittest.TestCase):
    """A friend's gift card can still be painting when OPEN_BTN is sampled.

    Measured on the SE: the recovery tap returns while the friends list is only
    starting to fade, so the first look at OPEN_BTN reads a half-drawn button as
    grey. The settled look that follows sees the colored one, which used to read
    as "not on the friend profile" and stopped the run.
    """

    @staticmethod
    def _points() -> dict[str, list[int]]:
        names = [step.name for step in gift_ios.GIFT_STEPS] + ["GIFT_SORT_BTN"]
        return {name: [index, index] for index, name in enumerate(names)}

    def test_gift_card_that_paints_late_is_opened_rather_than_refused(self) -> None:
        points = self._points()
        screen = {"name": "card"}
        taps: list[list[int]] = []
        open_looks = 0

        def available(driver, point):
            nonlocal open_looks
            if point == points["OPEN_BTN"]:
                open_looks += 1
                # The first look lands on the half-drawn frame.
                return open_looks > 1 and screen["name"] == "card"
            if point == points["SEND_GIFT_BTN"]:
                return False
            return True

        def saturation(driver, point, logical_radius=8):
            return 60.0 if screen["name"] == "card" else 0.0

        def tap(driver, point):
            taps.append(point)
            if point == points["OPEN_BTN"]:
                screen["name"] = "profile"
            elif point == points["CLOSE_BTN"]:
                screen["name"] = "list"

        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(
                gift_ios, "colored_action_available", side_effect=available
            ),
            mock.patch.object(gift_ios, "point_saturation", side_effect=saturation),
            mock.patch.object(
                gift_ios,
                "is_friends_list",
                side_effect=lambda driver: screen["name"] == "list",
            ),
            mock.patch.object(gift_ios, "tap", side_effect=tap),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            completed = gift_ios.run_gifts_v3(
                object(), {}, 1, guard=False, dry_run=False, all_mode=True
            )

        self.assertEqual(completed, 1)
        self.assertIn(points["OPEN_BTN"], taps)

    def test_stranded_screen_still_stops_without_tapping_send_gift(self) -> None:
        points = self._points()
        taps: list[list[int]] = []

        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(
                gift_ios, "colored_action_available", return_value=False
            ),
            # Colored at OPEN_BTN's point but never a card the open tap can
            # reach: this is the stranded screen the guard exists for.
            mock.patch.object(gift_ios, "point_saturation", return_value=60.0),
            mock.patch.object(gift_ios, "is_friends_list", return_value=False),
            mock.patch.object(
                gift_ios, "tap", side_effect=lambda d, p: taps.append(p)
            ),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                gift_ios.GifterError, "Not on the friend profile before Send Gift"
            ):
                gift_ios.run_gifts_v3(
                    object(), {}, 1, guard=False, dry_run=False, all_mode=True
                )

        self.assertEqual(taps, [])


if __name__ == "__main__":
    unittest.main()
