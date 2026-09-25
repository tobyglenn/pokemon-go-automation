from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest import mock

from PIL import Image, ImageDraw

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


# --- The friends list, in the colours the SE draws it in ---
# Measured on a 750x1334 screenshot of the SE's own friends list: the rule
# between rows, the halo just outside a highlighted avatar, the orange a pinned
# remote trade's countdown carries, and the grey every other row dates itself in.
ROW_RULE_GREY = (222, 222, 218)
AVATAR_HALO = (213, 250, 252)
TRADE_ORANGE = (245, 140, 50)
DATE_GREY = (150, 150, 150)
AVATAR_SKIN = (240, 220, 200)
# The SE's own geometry: the header ends at 413 and rows are 239 tall, which is
# why its config carries NEXT_FRIEND_BTN [187, 270] in logical points.
LIST_TOP = 413
ROW_PITCH = 239
SE_SCALE = 2.0


def friends_list_image(rows, width=750, height=1334):
    """A friends list screenshot. Each row is (halo, waiting_on_a_trade)."""
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for index, (halo, trade) in enumerate(rows):
        top = LIST_TOP + index * ROW_PITCH
        for rule in (top, top + ROW_PITCH):
            draw.line(
                [(int(width * 0.06), rule), (int(width * 0.72), rule)],
                fill=ROW_RULE_GREY,
                width=2,
            )
        centre = (int(width * 0.15), top + ROW_PITCH // 2)
        radius = int(width * 0.073)
        if halo:
            glow = int(radius * 1.35)
            draw.ellipse(
                [centre[0] - glow, centre[1] - glow, centre[0] + glow, centre[1] + glow],
                fill=AVATAR_HALO,
            )
        draw.ellipse(
            [centre[0] - radius, centre[1] - radius, centre[0] + radius, centre[1] + radius],
            fill=AVATAR_SKIN,
        )
        # The countdown, or the date every other row shows in its place.
        draw.rectangle(
            [
                int(width * 0.78),
                top + int(ROW_PITCH * 0.10),
                int(width * 0.90),
                top + int(ROW_PITCH * 0.20),
            ],
            fill=TRADE_ORANGE if trade else DATE_GREY,
        )
    return image


class FriendRowTests(unittest.TestCase):
    """The same two rules as the Android worker, read off an iOS screenshot."""

    def test_rows_are_found_between_the_rules(self) -> None:
        image = friends_list_image([(False, False)] * 3)
        rows = gift_ios.friend_rows(image)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], (LIST_TOP, LIST_TOP + ROW_PITCH))

    def test_a_haloed_row_reads_as_highlighted(self) -> None:
        image = friends_list_image([(True, False)])
        row = gift_ios.friend_rows(image)[0]
        self.assertTrue(gift_ios.row_is_highlighted(image, row))

    def test_a_plain_row_does_not(self) -> None:
        image = friends_list_image([(False, False)])
        row = gift_ios.friend_rows(image)[0]
        self.assertFalse(gift_ios.row_is_highlighted(image, row))

    def test_an_orange_countdown_reads_as_a_pending_trade(self) -> None:
        image = friends_list_image([(False, True)])
        row = gift_ios.friend_rows(image)[0]
        self.assertTrue(gift_ios.row_waits_on_a_trade(image, row))

    def test_a_dated_row_does_not(self) -> None:
        image = friends_list_image([(False, False)])
        row = gift_ios.friend_rows(image)[0]
        self.assertFalse(gift_ios.row_waits_on_a_trade(image, row))

    def test_the_pinned_trade_row_is_stepped_over(self) -> None:
        image = friends_list_image([(False, True), (False, False)])
        choice = gift_ios.choose_friend_row(image, [187, 270], SE_SCALE)
        second = gift_ios.friend_rows(image)[1]
        self.assertEqual(choice.point[1], int((second[0] + second[1]) / 2 / SE_SCALE))
        self.assertTrue(choice.open_allowed)

    def test_a_highlighted_row_is_opened_but_not_for_its_gift(self) -> None:
        image = friends_list_image([(True, False)])
        choice = gift_ios.choose_friend_row(image, [187, 270], SE_SCALE)
        self.assertFalse(choice.open_allowed)
        self.assertTrue(any("highlighted" in note for note in choice.notes))

    def test_points_are_returned_in_logical_points(self) -> None:
        """The rows are pixels and the taps are points; the SE is 2x."""
        image = friends_list_image([(False, False)])
        choice = gift_ios.choose_friend_row(image, [187, 270], SE_SCALE)
        self.assertEqual(choice.point, [187, int((LIST_TOP + ROW_PITCH / 2) / SE_SCALE)])

    def test_an_unreadable_screen_falls_back_to_the_configured_point(self) -> None:
        image = Image.new("RGB", (750, 1334), (90, 160, 230))
        choice = gift_ios.choose_friend_row(image, [187, 270], SE_SCALE)
        self.assertEqual(choice.point, [187, 270])
        self.assertTrue(choice.open_allowed)

    def test_a_scrolled_list_falls_back_rather_than_tapping_a_misread_row(self) -> None:
        image = friends_list_image([(True, False)] * 3)
        # NEXT_FRIEND_BTN nowhere near the first row the rules found.
        choice = gift_ios.choose_friend_row(image, [187, 600], SE_SCALE)
        self.assertEqual(choice.point, [187, 600])
        self.assertTrue(choice.open_allowed)


class SelectiveOpeningTests(unittest.TestCase):
    """Send to everyone who can receive; open only from the rows without a halo."""

    @staticmethod
    def _points() -> dict[str, list[int]]:
        names = [step.name for step in gift_ios.GIFT_STEPS] + ["GIFT_SORT_BTN"]
        return {name: [index, index] for index, name in enumerate(names)}

    def _run(self, open_allowed: bool) -> list[str]:
        points = self._points()
        names = {tuple(point): name for name, point in points.items()}
        screen = {"name": "card"}
        sent = {"done": False}
        taps: list[str] = []

        def available(driver, point):
            if point == points["OPEN_BTN"]:
                return screen["name"] == "card"
            if point == points["SEND_GIFT_BTN"]:
                return not sent["done"]
            return True

        def saturation(driver, point, logical_radius=8):
            if point == points["OPEN_BTN"]:
                return 60.0 if screen["name"] == "card" else 0.0
            if point == points["SEND_BTN"]:
                return 80.0 if screen["name"] == "picker" else 0.0
            return 0.0

        def fake_tap(driver, point):
            taps.append(names[tuple(point)])
            if point == points["OPEN_BTN"]:
                screen["name"] = "profile"
            elif point == points["SEND_GIFT_BTN"]:
                screen["name"] = "picker"
            elif point == points["SEND_BTN"]:
                screen["name"] = "profile"
                sent["done"] = True
            elif point == points["CLOSE_BTN"]:
                screen["name"] = "profile" if screen["name"] in {"card", "picker"} else "list"
            elif point == points["NEXT_FRIEND_BTN"]:
                # Tapping the row opens the friend's unopened gift, not their
                # profile -- the whole reason the skip has to close something.
                screen["name"] = "card"
                sent["done"] = False

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
            mock.patch.object(
                gift_ios,
                "choose_friend_row",
                return_value=gift_ios.FriendRowChoice(
                    points["NEXT_FRIEND_BTN"], open_allowed
                ),
            ),
            mock.patch.object(
                gift_ios,
                "screenshot_image",
                return_value=friends_list_image([(False, False)]),
            ),
            mock.patch.object(gift_ios, "tap", side_effect=fake_tap),
            # The screen guard has its own tests; this one is about the row.
            mock.patch.object(gift_ios, "ensure_screen"),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            driver = FakeDriver(rect={"width": 375, "height": 667})
            gift_ios.run_gifts_v3(driver, {}, 2, guard=True, dry_run=False, all_mode=True)
        return taps

    def test_a_plain_friend_has_their_gift_opened(self) -> None:
        taps = self._run(open_allowed=True)
        second = taps[taps.index("NEXT_FRIEND_BTN") + 1:]
        self.assertEqual(second[0], "OPEN_BTN")
        self.assertIn("SEND_BTN", second)

    def test_a_highlighted_friend_is_sent_to_but_not_opened(self) -> None:
        taps = self._run(open_allowed=False)
        second = taps[taps.index("NEXT_FRIEND_BTN") + 1:]
        self.assertNotIn("OPEN_BTN", second)
        self.assertIn("SEND_BTN", second)

    def test_the_unopened_gift_is_closed_off_the_profile(self) -> None:
        # The gift covers SEND GIFT, so skipping the open has to close it
        # before the send half of the cycle reaches the profile behind it.
        taps = self._run(open_allowed=False)
        second = taps[taps.index("NEXT_FRIEND_BTN") + 1:]
        self.assertEqual(second[0], "CLOSE_BTN")
        self.assertLess(second.index("CLOSE_BTN"), second.index("SEND_GIFT_BTN"))


class CycleFailureRecoveryTests(unittest.TestCase):
    """One friend the cycle cannot finish must not end the run.

    Measured on the SE: friend 1 of 399 stranded every run on "OPEN did not
    reach the friend profile", so the phone sent nothing at all and the 398
    friends behind it were never tried.
    """

    @staticmethod
    def _points() -> dict[str, list[int]]:
        names = [step.name for step in gift_ios.GIFT_STEPS] + ["GIFT_SORT_BTN"]
        return {name: [index, index] for index, name in enumerate(names)}

    def _run(self, count: int, stuck: set[int]) -> int:
        """Send to `count` friends, where those in `stuck` never leave the card."""
        points = self._points()
        names = {tuple(point): name for name, point in points.items()}
        state = {"screen": "card", "friend": 1}
        sent = {"done": False}
        self.taps: list[str] = []

        def stuck_now() -> bool:
            return state["friend"] in stuck

        def available(driver, point):
            if point == points["OPEN_BTN"]:
                return state["screen"] == "card"
            if point == points["SEND_GIFT_BTN"]:
                return not sent["done"]
            return True

        def saturation(driver, point, logical_radius=8):
            if point == points["OPEN_BTN"]:
                return 60.0 if state["screen"] == "card" else 0.0
            if point == points["SEND_BTN"]:
                return 80.0 if state["screen"] == "picker" else 0.0
            return 0.0

        def fake_tap(driver, point):
            self.taps.append(names[tuple(point)])
            if point == points["OPEN_BTN"]:
                if not stuck_now():
                    state["screen"] = "profile"
            elif point == points["SEND_GIFT_BTN"]:
                state["screen"] = "picker"
            elif point == points["SEND_BTN"]:
                state["screen"] = "profile"
                sent["done"] = True
            elif point == points["CLOSE_BTN"]:
                # A stuck friend's card does not give way to anything: this is
                # the screen the run used to die on.
                if state["screen"] == "card" and stuck_now():
                    pass
                elif state["screen"] in {"card", "picker"}:
                    state["screen"] = "profile"
                else:
                    state["screen"] = "list"
            elif point == points["NEXT_FRIEND_BTN"]:
                state["friend"] += 1
                state["screen"] = "card"
                sent["done"] = False

        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(
                gift_ios, "colored_action_available", side_effect=available
            ),
            mock.patch.object(gift_ios, "point_saturation", side_effect=saturation),
            mock.patch.object(
                gift_ios,
                "is_friends_list",
                side_effect=lambda driver: state["screen"] == "list",
            ),
            mock.patch.object(
                gift_ios,
                "choose_friend_row",
                return_value=gift_ios.FriendRowChoice(
                    points["NEXT_FRIEND_BTN"], True
                ),
            ),
            mock.patch.object(
                gift_ios,
                "screenshot_image",
                return_value=friends_list_image([(False, False)]),
            ),
            mock.patch.object(gift_ios, "tap", side_effect=fake_tap),
            # The screen guard has its own tests; the recovery it performs for
            # the handler is one of them, so a no-op stands in for it here.
            mock.patch.object(gift_ios, "ensure_screen"),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            driver = FakeDriver(rect={"width": 375, "height": 667})
            return gift_ios.run_gifts_v3(
                driver, {}, count, guard=True, dry_run=False, all_mode=True
            )

    def test_a_friend_who_cannot_be_opened_is_skipped_not_fatal(self) -> None:
        completed = self._run(3, stuck={1})

        # The two friends behind the stuck one still got their gift.
        self.assertEqual(completed, 2)
        after_skip = self.taps[self.taps.index("NEXT_FRIEND_BTN") + 1:]
        self.assertIn("SEND_BTN", after_skip)

    def test_the_run_still_stops_once_nothing_is_getting_through(self) -> None:
        with self.assertRaisesRegex(
            gift_ios.GifterError, "OPEN did not reach the friend profile"
        ):
            self._run(50, stuck=set(range(1, 51)))

        # It gave up on the cap rather than grinding through all 50.
        self.assertEqual(
            self.taps.count("OPEN_BTN"), gift_ios.GIFT_CYCLE_RETRIES
        )


if __name__ == "__main__":
    unittest.main()
