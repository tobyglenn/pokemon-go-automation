from __future__ import annotations

from tests import support as _test_support

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from sources import gift_ios


# These tests script SEND GIFT as colour at its configured point. The runner
# now finds the lit icon on the screenshot instead (gift_profile); here that
# search answers from the same scripted colour, at the configured point.
_config: dict = {}
_friend_close_scroll = gift_ios.friend_close_scroll


def _remember_config(config):
    _config["config"] = config
    return _friend_close_scroll(config)


def _scripted_send_gift(driver):
    # Whatever coordinates() the test has patched in, at the moment of asking.
    point = gift_ios.coordinates(_config.get("config", {})).get("SEND_GIFT_BTN")
    if point is None or not gift_ios.colored_action_available(driver, point):
        return None
    return list(point)


_patches = [
    mock.patch.object(gift_ios, "friend_close_scroll", side_effect=_remember_config),
    mock.patch.object(gift_ios, "send_gift_point", side_effect=_scripted_send_gift),
    # Screens here are scripted by colour; the FRIENDSHIP LEVEL read behind
    # the colour test has nothing to read.
    mock.patch.object(gift_ios, "friendship_heading_visible", return_value=False),
    # Likewise the OPEN label read: a coloured gift card here is a gift card.
    mock.patch.object(gift_ios, "open_button_visible", return_value=True),
    mock.patch.object(gift_ios, "bag_full_visible", return_value=False),
]


def setUpModule():
    for patcher in _patches:
        patcher.start()


def tearDownModule():
    for patcher in reversed(_patches):
        patcher.stop()


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
    """Wait for a gift card to finish painting before sampling OPEN."""

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


class NothingLeftTests(unittest.TestCase):
    """End the run when there are no outgoing or incoming gifts."""

    def test_idle_friends_end_the_run(self) -> None:
        points = LatePaintingGiftTests._points()
        screen = {"name": "profile"}
        taps: list[list[int]] = []

        def available(driver, point):
            # No incoming gift, SEND GIFT greyed out; the list's sort works.
            return point == points["SORT_BTN"]

        def tap(driver, point):
            taps.append(point)
            if point == points["CLOSE_BTN"]:
                screen["name"] = "list"
            elif point == points["NEXT_FRIEND_BTN"]:
                screen["name"] = "profile"

        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(gift_ios, "colored_action_available", side_effect=available),
            mock.patch.object(gift_ios, "point_saturation", return_value=0.0),
            mock.patch.object(
                gift_ios, "is_friends_list", side_effect=lambda driver: screen["name"] == "list"
            ),
            mock.patch.object(gift_ios, "wait_for_screen_state", return_value=True),
            mock.patch.object(gift_ios, "tap", side_effect=tap),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            completed = gift_ios.run_gifts_v3(
                object(), {}, 100, guard=False, dry_run=False, all_mode=True
            )

        self.assertEqual(completed, gift_ios.IDLE_CYCLE_LIMIT)
        self.assertNotIn(points["SEND_GIFT_BTN"], taps)


class RefusedOpenLoopTests(unittest.TestCase):
    """Refused opens stop the loop if the same friend cannot receive a gift."""

    def test_refused_opens_end_the_run(self) -> None:
        points = LatePaintingGiftTests._points()
        screen = {"name": "profile"}

        def available(driver, point):
            # A gift card's OPEN is always lit; SEND GIFT never is.
            return point in (points["OPEN_BTN"], points["SORT_BTN"])

        def tap(driver, point):
            if point == points["CLOSE_BTN"]:
                screen["name"] = "list"
            elif point == points["NEXT_FRIEND_BTN"]:
                screen["name"] = "profile"

        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(gift_ios, "colored_action_available", side_effect=available),
            mock.patch.object(gift_ios, "point_saturation", return_value=0.0),
            mock.patch.object(
                gift_ios, "is_friends_list", side_effect=lambda driver: screen["name"] == "list"
            ),
            mock.patch.object(gift_ios, "wait_for_screen_state", return_value=True),
            mock.patch.object(gift_ios, "ensure_screen"),
            mock.patch.object(gift_ios, "screenshot_image"),
            mock.patch.object(gift_ios, "choose_friend_row",
                              return_value=gift_ios.FriendRowChoice(points["NEXT_FRIEND_BTN"], True)),
            mock.patch.object(gift_ios, "friend_name", return_value="SameTrainer"),
            mock.patch.object(gift_ios, "tap", side_effect=tap),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            driver = mock.Mock()
            driver.get_window_rect.return_value = {"width": 375, "height": 667}
            completed = gift_ios.run_gifts_v3(
                driver, {}, 100, guard=True, dry_run=False, all_mode=True
            )

        # The first cycle's open is believed; the repeats are not.
        self.assertLessEqual(completed, 1 + gift_ios.IDLE_CYCLE_LIMIT)


class TransientScreenTests(unittest.TestCase):
    """Wait for transient overlays and the incoming and outgoing gift controls."""

    _points = staticmethod(LatePaintingGiftTests._points)

    def _run(self, saturation_at, open_waiting=True):
        points = self._points()
        screen = {"name": "card" if open_waiting else "profile", "since": 0}
        taps: list[list[int]] = []

        def available(driver, point):
            if point == points["OPEN_BTN"]:
                return screen["name"] == "card"
            if point == points["SEND_GIFT_BTN"]:
                return screen["name"] == "profile" and not screen.get("sent")
            return True

        def saturation(driver, point, logical_radius=8):
            screen["since"] += 1
            return saturation_at(screen, points, point)

        def tap(driver, point):
            taps.append(point)
            screen["since"] = 0
            if point == points["OPEN_BTN"]:
                screen["name"] = "banner"
            elif point == points["SEND_GIFT_BTN"]:
                screen["name"] = "picker"
            elif point == points["FIRST_GIFT_BTN"]:
                screen["name"] = "selected"
            elif point == points["SEND_BTN"]:
                screen["name"], screen["sent"] = "profile", True
            elif point == points["CLOSE_BTN"]:
                screen["name"] = "profile" if screen["name"] == "picker" else "list"

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
        return completed, taps, points

    def test_the_daily_limit_banner_is_waited_out_not_closed(self) -> None:
        def saturation_at(screen, points, point):
            if screen["name"] == "banner":
                # The banner lies on OPEN_BTN for the first two looks, then fades.
                if screen["since"] > 2:
                    screen["name"] = "profile"
                    return 0.0
                return 60.0
            if point == points["SEND_BTN"]:
                return 80.0 if screen["name"] == "selected" else 0.0
            return 60.0 if screen["name"] == "card" else 0.0

        completed, taps, points = self._run(saturation_at)

        self.assertEqual(completed, 1)
        # The only CLOSE is the one that leaves the profile after the send.
        self.assertEqual(taps.count(points["CLOSE_BTN"]), 1)
        self.assertIn(points["SEND_BTN"], taps)

    def test_a_picker_still_sliding_in_is_not_taken_for_an_empty_bag(self) -> None:
        def saturation_at(screen, points, point):
            if point == points["SEND_BTN"]:
                return 80.0 if screen["name"] == "selected" and screen["since"] > 2 else 0.0
            return 0.0

        completed, taps, points = self._run(saturation_at, open_waiting=False)

        self.assertEqual(completed, 1)
        self.assertIn(points["SEND_BTN"], taps)
        self.assertEqual(taps.count(points["FIRST_GIFT_BTN"]), 1)

    def test_a_first_gift_tap_lost_to_the_picker_is_retried_once(self) -> None:
        lost = {"done": False}

        def saturation_at(screen, points, point):
            if point == points["SEND_BTN"]:
                if screen["name"] == "selected" and not lost["done"]:
                    # The first tap landed before the picker could take it.
                    screen["name"] = "picker"
                    lost["done"] = True
                return 80.0 if screen["name"] == "selected" else 0.0
            return 0.0

        completed, taps, points = self._run(saturation_at, open_waiting=False)

        self.assertEqual(completed, 1)
        self.assertEqual(taps.count(points["FIRST_GIFT_BTN"]), 2)
        self.assertIn(points["SEND_BTN"], taps)


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
    """Skip an isolated failed cycle and continue to the next friend."""

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


class RefusedOpenTests(unittest.TestCase):
    """Continue sending after a refused open unless the Item Bag is confirmed full."""

    _points = staticmethod(CycleFailureRecoveryTests._points)

    def _run(self, count: int, opens_before_refusal: int, *, full_bag=False, config=None):
        points = self._points()
        names = {tuple(point): name for name, point in points.items()}
        state = {"screen": "card", "opens": 0}
        sent = {"done": False}
        taps: list[str] = []

        def available(driver, point):
            if point == points["OPEN_BTN"]:
                return state["screen"] in {"card", "refused"}
            if point == points["SEND_GIFT_BTN"]:
                return not sent["done"]
            return True

        def saturation(driver, point, logical_radius=8):
            if point == points["OPEN_BTN"]:
                return 60.0 if state["screen"] in {"card", "refused"} else 0.0
            if point == points["SEND_BTN"]:
                return 80.0 if state["screen"] == "picker" else 0.0
            return 0.0

        def fake_tap(driver, point):
            taps.append(names[tuple(point)])
            if point == points["OPEN_BTN"]:
                if state["opens"] < opens_before_refusal:
                    state["opens"] += 1
                    state["screen"] = "profile"
                else:
                    state["screen"] = "refused"
            elif point == points["SEND_GIFT_BTN"]:
                state["screen"] = "picker"
            elif point == points["SEND_BTN"]:
                state["screen"] = "profile"
                sent["done"] = True
            elif point == points["CLOSE_BTN"]:
                state["screen"] = {
                    "refused": "card",
                    "card": "profile",
                    "picker": "profile",
                }.get(state["screen"], "list")
            elif point == points["NEXT_FRIEND_BTN"]:
                state["screen"] = "card"
                sent["done"] = False

        image = friends_list_image([(False, False)])
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
            mock.patch.object(gift_ios, "screenshot_image", return_value=image),
            mock.patch.object(gift_ios, "friend_name", return_value=None),
            mock.patch.object(Image.Image, "save") as save,
            mock.patch.object(gift_ios, "tap", side_effect=fake_tap),
            mock.patch.object(gift_ios, "ensure_screen"),
            mock.patch.object(gift_ios.time, "sleep"),
            mock.patch.object(
                gift_ios, "bag_full_visible",
                side_effect=lambda _driver: full_bag and state["screen"] == "refused",
            ),
        ):
            driver = FakeDriver(rect={"width": 375, "height": 667})
            completed = gift_ios.run_gifts_v3(
                driver, config or {}, count, guard=True, dry_run=False, all_mode=True
            )
        return completed, taps, save

    def test_every_friend_still_gets_a_gift_after_a_refused_open(self) -> None:
        completed, taps, _ = self._run(5, opens_before_refusal=2)

        self.assertEqual(completed, 5)
        self.assertEqual(taps.count("SEND_BTN"), 5)

    def test_opening_stops_after_the_first_refusal(self) -> None:
        _, taps, _ = self._run(5, opens_before_refusal=2)

        # Two opens that worked and the one that was refused; none after.
        self.assertEqual(taps.count("OPEN_BTN"), 3)

    def test_the_refused_gift_is_closed_twice_before_send_gift(self) -> None:
        _, taps, _ = self._run(1, opens_before_refusal=0)

        self.assertEqual(
            taps[: taps.index("SEND_GIFT_BTN")],
            ["OPEN_BTN", "CLOSE_BTN", "CLOSE_BTN"],
        )

    def test_the_refused_screen_is_kept_as_evidence(self) -> None:
        _, _, save = self._run(1, opens_before_refusal=0)

        save.assert_called_once_with(gift_ios.OPEN_REFUSED_SCREENSHOT)

    def test_confirmed_full_bag_hands_off_to_starter_before_any_further_tap(self) -> None:
        android = gift_ios.gift_android
        config = {"device": {"udid": "EXAMPLE-UDID", "team_id": "EXAMPLE", "wda_bundle_id": "org.example.wda"}}
        with (
            tempfile.TemporaryDirectory() as temp,
            mock.patch.object(android, "BAG_FULL_MARKERS", new=Path(temp)),
            mock.patch.dict(gift_ios.os.environ, {android.CLEAR_BAG_ENV: "1"}),
        ):
            completed, taps, _ = self._run(3, opens_before_refusal=0, full_bag=True, config=config)
            self.assertTrue(android.bag_full_marker("EXAMPLE-UDID").is_file())
        self.assertEqual(completed, 0)
        self.assertEqual(taps, ["OPEN_BTN"])

    def test_standalone_full_bag_also_avoids_the_upgrade_dialog(self) -> None:
        with mock.patch.dict(gift_ios.os.environ, {gift_ios.gift_android.CLEAR_BAG_ENV: ""}):
            completed, taps, _ = self._run(3, opens_before_refusal=0, full_bag=True)
        self.assertEqual(completed, 0)
        self.assertEqual(taps, ["OPEN_BTN"])


if __name__ == "__main__":
    unittest.main()


class FriendshipHeadingTests(unittest.TestCase):
    """The profile check behind the colour test: TRADE can sit under OPEN_BTN."""

    def read(self, texts):
        from sources import gbl_vision

        boxes = [gbl_vision.OCRBox(text, 0.9, 0, 0, 10, 10) for text in texts]
        with (
            mock.patch.object(gift_ios, "screenshot_image", return_value=Image.new("RGB", (750, 1334))),
            mock.patch.object(gbl_vision, "recognize", return_value=boxes),
        ):
            # The module-level patch stubs it for the colour-scripted tests.
            return _real_heading(object())

    def test_profile_heading_is_the_profile(self) -> None:
        self.assertTrue(self.read(["Level 39", "FRIENDSHIP LEVEL", "SEND GIFT"]))

    def test_gift_card_is_not_the_profile(self) -> None:
        self.assertFalse(self.read(["OPEN", "ExampleTrainer sent you a Gift!"]))


_real_heading = gift_ios.friendship_heading_visible
_real_open_label = gift_ios.open_button_visible
_real_bag_full = gift_ios.bag_full_visible


class BagFullNoticeTests(unittest.TestCase):
    def test_only_a_positive_full_bag_notice_is_recognized(self) -> None:
        image = Image.new("RGB", (750, 1334))
        for wording, expected in (
            ("Your Item Bag is full!", True),
            ("Daily Gift limit reached", False),
            ("Upgrade Item Bag", False),
        ):
            with (
                self.subTest(wording=wording),
                mock.patch.object(gift_ios, "screenshot_image", return_value=image),
                mock.patch.object(gift_ios.gbl_vision, "recognize", return_value=[
                    gift_ios.gbl_vision.OCRBox(wording, 0.95, 20, 20, 80, 20)
                ]),
            ):
                self.assertEqual(_real_bag_full(object()), expected)


class OpenLabelTests(unittest.TestCase):
    """OPEN is tapped only when its label is read on the button."""

    def read(self, boxes, rect_error=None):
        from sources import gbl_vision

        driver = mock.Mock()
        driver.get_window_rect.return_value = {"width": 375, "height": 667}
        if rect_error:
            driver.get_window_rect.side_effect = rect_error
        with (
            mock.patch.object(gift_ios, "screenshot_image", return_value=Image.new("RGB", (750, 1334))),
            mock.patch.object(gbl_vision, "recognize", return_value=boxes),
        ):
            return _real_open_label(driver, [188, 554])

    @staticmethod
    def box(text, x, y, confidence=1.0):
        from sources import gbl_vision

        return gbl_vision.OCRBox(text, confidence, x, y, 90, 26)

    def test_gift_card_open_label_is_read(self) -> None:
        # The sample gift card has OPEN at (330, 1094) pixels.
        self.assertTrue(self.read([self.box("OPEN", 330, 1094)]))

    def test_profile_has_no_open_label(self) -> None:
        self.assertFalse(
            self.read([self.box("SEND GIFT", 68, 1186), self.box("FRIENDSHIP LEVEL", 306, 846)])
        )

    def test_open_elsewhere_on_screen_does_not_count(self) -> None:
        self.assertFalse(self.read([self.box("OPEN", 330, 300)]))

    def test_a_failed_read_does_not_tap(self) -> None:
        self.assertFalse(self.read([self.box("OPEN", 330, 1094)], rect_error=RuntimeError("wda")))

    def test_trade_under_open_is_not_tapped_when_no_heading_is_read(self) -> None:
        # The profile heading was unreadable and the point was colored.
        points = SelectiveOpeningTests._points()
        taps: list[list[int]] = []
        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(gift_ios, "colored_action_available", return_value=True),
            mock.patch.object(gift_ios, "friendship_heading_visible", return_value=False),
            mock.patch.object(gift_ios, "open_button_visible", return_value=False),
            mock.patch.object(gift_ios, "point_saturation", return_value=60.0),
            mock.patch.object(gift_ios, "is_friends_list", return_value=False),
            mock.patch.object(gift_ios, "tap", side_effect=lambda d, p: taps.append(list(p))),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            try:
                gift_ios.run_gifts_v3(mock.Mock(), {}, 1, False, False, all_mode=False)
            except Exception:
                pass
        self.assertNotIn(points["OPEN_BTN"], taps)


class OpenOnProfileTests(unittest.TestCase):
    """A coloured OPEN_BTN point on the profile is TRADE, not a gift to open."""

    def test_trade_under_open_is_not_tapped(self) -> None:
        points = SelectiveOpeningTests._points()
        taps: list[list[int]] = []
        driver = mock.Mock()
        with (
            mock.patch.object(gift_ios, "coordinates", return_value=points),
            mock.patch.object(gift_ios, "colored_action_available", return_value=True),
            mock.patch.object(gift_ios, "friendship_heading_visible", return_value=True),
            mock.patch.object(gift_ios, "point_saturation", return_value=60.0),
            mock.patch.object(gift_ios, "is_friends_list", return_value=False),
            mock.patch.object(gift_ios, "tap", side_effect=lambda d, p: taps.append(list(p))),
            mock.patch.object(gift_ios.time, "sleep"),
        ):
            try:
                gift_ios.run_gifts_v3(driver, {}, 1, False, False, all_mode=False)
            except Exception:
                pass  # only whether OPEN was tapped matters here
        self.assertNotIn(points["OPEN_BTN"], taps)


import unittest as _ut


class CrashAndKeyboardTests(_ut.TestCase):
    def test_game_crashed_matches_wda_crash_message(self):
        from sources import gift_ios
        assert gift_ios.game_crashed(Exception(
            "Message: The application under test with bundle id 'com.nianticlabs.pokemongo' is not running, possibly crashed"))
        assert not gift_ios.game_crashed(Exception("socket hang up"))
