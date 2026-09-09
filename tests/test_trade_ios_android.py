from __future__ import annotations

from tests import support as _test_support

import asyncio
import contextlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import yaml
from PIL import Image, ImageDraw

from sources import gbl_vision
from sources import trade_ios_android as cross_trade


class FakeAndroid:
    serial = "android-test"
    config = {
        "TRADE_BTN": [1, 1],
        "FIRST_PKMN_BTN": [2, 2],
        "NEXT_BTN": [3, 3],
        "CONFIRM_BTN": [4, 4],
        "X_BTN": [5, 5],
    }


class FakeIOS:
    def __init__(self) -> None:
        self.coordinates = {
            "TRADE_BTN": [10, 10],
            "FIRST_PKMN_BTN": [20, 20],
            "NEXT_BTN": [30, 30],
            "CONFIRM_BTN": [40, 40],
            "X_BTN": [50, 50],
        }
        self.tapped: list[str] = []
        self.points: list[tuple[int, int]] = []
        self.cleaned = 0
        self.overlay_clears = 0
        self.overlay_present = False

    async def tap_step(self, name: str) -> bool:
        self.tapped.append(name)
        return True

    async def tap_point(self, point: tuple[int, int]) -> None:
        self.points.append(point)

    def image_point(self, image: object, name: str) -> tuple[int, int] | None:
        point = self.coordinates.get(name)
        return (point[0], point[1]) if point else None

    async def post_trade_cleanup(self) -> None:
        self.cleaned += 1

    async def clear_system_overlay(self) -> bool:
        if not self.overlay_present:
            return False
        self.overlay_present = False
        self.overlay_clears += 1
        return True


class FakeDriver:
    """Enough of a WDA session to answer the foreground questions."""
    def __init__(self, front: str, state: int = 4) -> None:
        self.front = front
        self.state = state
        self.scripts: list[str] = []

    def get_window_rect(self) -> dict[str, int]:
        return {"width": 375, "height": 667}

    def execute_script(self, script: str, *args: object) -> object:
        self.scripts.append(script)
        if script == "mobile: activeAppInfo":
            return {"bundleId": self.front}
        if script == "mobile: queryAppState":
            return self.state
        return None


class SystemOverlayTests(unittest.TestCase):
    BUNDLE = "com.nianticlabs.pokemongo"

    def controller(self, driver: FakeDriver) -> object:
        return cross_trade.IOSController(driver, {"X_BTN": [50, 50]}, self.BUNDLE)

    def test_springboard_card_is_dismissed_by_reactivating_the_app(self) -> None:
        driver = FakeDriver("com.apple.springboard", state=4)
        self.assertTrue(self.controller(driver)._clear_system_overlay())
        self.assertIn("mobile: activateApp", driver.scripts)

    def test_app_already_in_front_is_left_alone(self) -> None:
        driver = FakeDriver(self.BUNDLE)
        self.assertFalse(self.controller(driver)._clear_system_overlay())
        self.assertNotIn("mobile: activateApp", driver.scripts)

    def test_a_dead_app_is_never_cold_started(self) -> None:
        """Activating a stopped app launches it, and a cold start loses the
        picker's hand-typed search filter."""
        driver = FakeDriver("com.apple.springboard", state=1)
        self.assertFalse(self.controller(driver)._clear_system_overlay())
        self.assertNotIn("mobile: activateApp", driver.scripts)

    def test_a_broken_session_does_not_take_the_run_down(self) -> None:
        driver = FakeDriver("com.apple.springboard")
        driver.execute_script = Mock(side_effect=RuntimeError("session gone"))
        self.assertFalse(self.controller(driver)._clear_system_overlay())


class OverlayGuardRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.android = FakeAndroid()
        self.ios = FakeIOS()
        self.ios.screenshot_image = AsyncMock(side_effect=lambda: object())

    def guard_patches(self, matches):
        return (
            patch.object(
                cross_trade, "android_screenshot_image", new=AsyncMock(side_effect=lambda _: object())
            ),
            patch.object(
                cross_trade,
                "screen_metrics",
                side_effect=[{"who": who} for who in ("android", "ios") * 20],
            ),
            patch.object(cross_trade, "state_matches", side_effect=matches),
            patch.object(cross_trade, "STATE_GUARD_DELAY", 0),
            patch.object(cross_trade, "IOS_OVERLAY_SETTLE_DELAY", 0),
            patch.object(cross_trade, "save_mismatch_diagnostics", return_value=Path("/tmp")),
        )

    async def test_overlaid_ios_recovers_and_the_guard_passes(self) -> None:
        """The AirPods-card failure: Android is a good friend screen, iOS reads
        as nothing at all, and the run used to die there."""
        self.ios.overlay_present = True

        def matches(expected: str, metrics: dict) -> bool:
            if metrics["who"] == "android":
                return True
            return not self.ios.overlay_present

        with contextlib.ExitStack() as stack:
            for patcher in self.guard_patches(matches):
                stack.enter_context(patcher)
            await cross_trade.guard_screen_state(
                self.android, self.ios, "friend", "TRADE_COMPLETE"
            )
        self.assertEqual(self.ios.overlay_clears, 1)
        self.assertEqual(self.ios.tapped, [])

    async def test_a_wrong_android_screen_still_fails_closed(self) -> None:
        """iOS is fine, so the overlay path must not fire and paper over it."""
        self.ios.overlay_present = True

        def matches(expected: str, metrics: dict) -> bool:
            return metrics["who"] == "ios"

        with contextlib.ExitStack() as stack:
            for patcher in self.guard_patches(matches):
                stack.enter_context(patcher)
            with self.assertRaisesRegex(
                cross_trade.CrossPlatformTradeError, "Screen mismatch before TRADE_COMPLETE"
            ):
                await cross_trade.guard_screen_state(
                    self.android, self.ios, "friend", "TRADE_COMPLETE"
                )
        self.assertEqual(self.ios.overlay_clears, 0)

    async def test_recovery_is_bounded_when_the_overlay_keeps_coming_back(self) -> None:
        stubborn = FakeIOS()
        stubborn.screenshot_image = AsyncMock(side_effect=lambda: object())

        async def always_clears() -> bool:
            stubborn.overlay_clears += 1
            return True

        stubborn.clear_system_overlay = always_clears

        with contextlib.ExitStack() as stack:
            for patcher in self.guard_patches(lambda expected, metrics: False):
                stack.enter_context(patcher)
            with self.assertRaises(cross_trade.CrossPlatformTradeError):
                await cross_trade.guard_screen_state(
                    self.android, stubborn, "friend", "TRADE_COMPLETE"
                )
        self.assertEqual(stubborn.overlay_clears, cross_trade.IOS_OVERLAY_RECOVERY_ATTEMPTS)


class PostTradeResyncTests(unittest.IsolatedAsyncioTestCase):
    FRIEND = {
        "action_green": 0.0699,
        "search_pale": 0.0685,
        "white_panel": 0.7011,
        "card_band": 0.4040,
        "card_edges": 0.9580,
    }
    POST_TRADE = {
        "action_green": 0.744,
        "search_pale": 0.0035,
        "white_panel": 0.9085,
        "card_band": 0.9210,
        "card_edges": 0.8010,
    }

    def setUp(self) -> None:
        self.android = FakeAndroid()
        self.ios = FakeIOS()

    async def test_only_the_straggler_is_tapped(self) -> None:
        """The live split: Android back on friend, iOS still on the card."""
        with patch.object(cross_trade.android_trade, "tap", new=AsyncMock()) as android_tap:
            acted = await cross_trade.close_stale_post_trade_card(
                self.android, self.ios, self.FRIEND, self.POST_TRADE
            )
        self.assertTrue(acted)
        self.assertEqual(self.ios.tapped, ["X_BTN"])
        android_tap.assert_not_awaited()

    async def test_both_cards_are_closed_together(self) -> None:
        with patch.object(cross_trade.android_trade, "tap", new=AsyncMock()) as android_tap:
            acted = await cross_trade.close_stale_post_trade_card(
                self.android, self.ios, self.POST_TRADE, self.POST_TRADE
            )
        self.assertTrue(acted)
        self.assertEqual(self.ios.tapped, ["X_BTN"])
        android_tap.assert_awaited_once()

    async def test_two_friend_screens_are_never_tapped(self) -> None:
        """X_BTN on a friend screen opens their Pokémon list — a real misfire."""
        with patch.object(cross_trade.android_trade, "tap", new=AsyncMock()) as android_tap:
            acted = await cross_trade.close_stale_post_trade_card(
                self.android, self.ios, self.FRIEND, self.FRIEND
            )
        self.assertFalse(acted)
        self.assertEqual(self.ios.tapped, [])
        android_tap.assert_not_awaited()

    async def test_the_guard_recovers_the_split_and_passes(self) -> None:
        self.ios.screenshot_image = AsyncMock(side_effect=lambda: object())
        closed: list[bool] = []

        async def close(*args: object) -> bool:
            closed.append(True)
            return True

        def matches(expected: str, metrics: dict) -> bool:
            if metrics["who"] == "android":
                return expected == "friend"
            # iOS reads as a post-trade card until the X_BTN tap lands.
            return expected == ("friend" if closed else "post_trade")

        with (
            patch.object(
                cross_trade, "android_screenshot_image", new=AsyncMock(side_effect=lambda _: object())
            ),
            patch.object(
                cross_trade,
                "screen_metrics",
                side_effect=[{"who": who} for who in ("android", "ios") * 20],
            ),
            patch.object(cross_trade, "state_matches", side_effect=matches),
            patch.object(cross_trade, "close_stale_post_trade_card", new=close),
            patch.object(cross_trade, "STATE_GUARD_DELAY", 0),
            patch.object(cross_trade, "POST_TRADE_RESYNC_DELAY", 0),
        ):
            await cross_trade.guard_screen_state(
                self.android, self.ios, "friend", "TRADE_BTN"
            )
        self.assertEqual(len(closed), 1)


class ConfigTests(unittest.TestCase):
    def test_placeholder_is_rejected(self) -> None:
        points = {
            name: [1, 1] for name in cross_trade.REQUIRED_IOS_COORDINATES
        }
        points["TRADE_BTN"] = [0, 0]
        with self.assertRaisesRegex(
            cross_trade.CrossPlatformTradeError, "calibration placeholder"
        ):
            cross_trade.validate_ios_coordinates(points)

    def test_placeholder_is_allowed_for_calibration_capture(self) -> None:
        points = {
            name: [0, 0] for name in cross_trade.REQUIRED_IOS_COORDINATES
        }
        self.assertEqual(
            cross_trade.validate_ios_coordinates(points, allow_placeholders=True),
            points,
        )

    def test_runtime_config_reuses_appium_device_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            appium = root / "ios-gifter.yaml"
            trader = root / "ios-android-trader.yaml"
            appium.write_text(
                yaml.safe_dump(
                    {
                        "server_url": "http://127.0.0.1:4723",
                        "device": {
                            "udid": "test-udid",
                            "team_id": "test-team",
                            "wda_bundle_id": "test.wda",
                        },
                    }
                )
            )
            trader.write_text(
                yaml.safe_dump(
                    {
                        "ios": {
                            "appium_config": appium.name,
                            "coordinates": {
                                name: [index + 1, index + 2]
                                for index, name in enumerate(
                                    sorted(cross_trade.REQUIRED_IOS_COORDINATES)
                                )
                            },
                        },
                        "android": {"serial": "android-test"},
                    }
                )
            )
            config = cross_trade.load_runtime_config(trader)
            self.assertEqual(config.android_serial, "android-test")
            self.assertEqual(config.ios_device["udid"], "test-udid")
            self.assertEqual(len(config.steps), 6)
            # A serial out of the config is a preference, not an instruction.
            self.assertFalse(config.android_serial_required)
            override = cross_trade.load_runtime_config(
                trader, android_serial_override="asked-for"
            )
            self.assertEqual(override.android_serial, "asked-for")
            self.assertTrue(override.android_serial_required)

    def test_appium_config_falls_back_to_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            (home / "private-config").mkdir(parents=True)
            shared = home / "private-config" / "only-in-home.yaml"
            shared.write_text("device: {}\n")
            with patch.dict("os.environ", {"POGO_CONFIG_DIR": str(home / "private-config")}):
                resolved = cross_trade.resolve_config_path("only-in-home.yaml", root)
            self.assertEqual(resolved, shared.resolve())

    def test_missing_appium_config_lists_searched_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(cross_trade.Path, "home", return_value=root / "home"):
                with self.assertRaisesRegex(
                    cross_trade.CrossPlatformTradeError, "not found:"
                ):
                    cross_trade.resolve_config_path("nowhere.yaml", root)

    def test_active_gift_runner_detection_parses_only_pids(self) -> None:
        completed = type(
            "Completed", (), {"returncode": 0, "stdout": "123\n456\n"}
        )()
        with patch.object(cross_trade.subprocess, "run", return_value=completed):
            self.assertEqual(cross_trade.active_gift_runner_pids(), [123, 456])

    def test_screen_states_fail_closed(self) -> None:
        self.assertTrue(
            cross_trade.state_matches(
                "friend",
                {"action_green": 0.20, "search_pale": 0.50, "white_panel": 0.55, "card_band": 0.38, "card_edges": 0.86},
            )
        )
        self.assertFalse(
            cross_trade.state_matches(
                "friend",
                {"action_green": 0.10, "search_pale": 0.05, "white_panel": 0.01, "card_band": 0.38, "card_edges": 0.86},
            )
        )
        self.assertTrue(
            cross_trade.state_matches(
                "post_trade",
                {"action_green": 0.10, "search_pale": 0.05, "white_panel": 0.90, "card_band": 0.92, "card_edges": 0.80},
            )
        )
        self.assertFalse(
            cross_trade.state_matches(
                "post_trade",
                {"action_green": 0.10, "search_pale": 0.93, "white_panel": 0.90, "card_band": 0.92, "card_edges": 0.80},
            )
        )

    def test_count_defaults_to_a_full_session(self) -> None:
        with patch.object(cross_trade.sys, "argv", ["trade_ios_android.py"]):
            self.assertEqual(cross_trade.parse_args().count, 100)

    def test_friend_screen_of_the_sparsest_phone_is_accepted(self) -> None:
        """Measured off the android-one, which a 0.07 green floor turned away."""
        self.assertTrue(
            cross_trade.state_matches(
                "friend",
                {"action_green": 0.0699, "search_pale": 0.0616, "white_panel": 0.6892, "card_band": 0.38, "card_edges": 0.86},
            )
        )
        # Still fails closed where there is no action button under the point.
        self.assertFalse(
            cross_trade.state_matches(
                "friend",
                {"action_green": 0.01, "search_pale": 0.0616, "white_panel": 0.6892, "card_band": 0.38, "card_edges": 0.86},
            )
        )

    def test_tall_handset_post_trade_card_is_accepted(self) -> None:
        """Measured on the tall_device, whose card fills less of the white band."""
        self.assertTrue(
            cross_trade.state_matches(
                "post_trade",
                {
                    "action_green": 0.2597,
                    "search_pale": 0.0020,
                    "white_panel": 0.8138,
                    "card_band": 0.9070,
                    "card_edges": 0.8300,
                },
            )
        )
        # Its own friend screen reads only 0.04 lower on white_panel, which is
        # why the card head has to be the thing that tells them apart.
        self.assertFalse(
            cross_trade.state_matches(
                "post_trade",
                {
                    "action_green": 0.2457,
                    "search_pale": 0.6224,
                    "white_panel": 0.7712,
                    "card_band": 0.3860,
                    "card_edges": 0.8580,
                },
            )
        )
        # "Are you sure you want to trade this Pokémon?" on an iPhone stopped
        # at CONFIRM_BTN: a white head, but a floating one.
        self.assertFalse(
            cross_trade.state_matches(
                "post_trade",
                {
                    "action_green": 0.10,
                    "search_pale": 0.0000,
                    "white_panel": 0.6919,
                    "card_band": 0.8180,
                    "card_edges": 0.3320,
                },
            )
        )

    def test_a_phone_waiting_on_the_other_trainer_is_still_in_the_lobby(self) -> None:
        """Measured on the android-two after it confirmed: the green CONFIRM pill
        under the coordinate has become an amber CANCEL while it waits. Reading
        only the green left it on a screen nothing recognised, so every
        recovery round walked past it until the trade expired."""
        self.assertTrue(
            cross_trade.state_matches(
                "lobby",
                {
                    "action_green": 0.0000,
                    "action_orange": 0.9040,
                    "search_pale": 0.0331,
                    "white_panel": 0.4870,
                    "card_band": 0.3860,
                    "card_edges": 0.0000,
                },
            )
        )
        # Before CONFIRM, on the green pill, the same screen still reads lobby.
        self.assertTrue(
            cross_trade.state_matches(
                "lobby",
                {
                    "action_green": 0.7440,
                    "action_orange": 0.0000,
                    "search_pale": 0.0020,
                    "white_panel": 0.4870,
                    "card_band": 0.3860,
                    "card_edges": 0.0000,
                },
            )
        )
        # And a screen with neither pill under the point is not a lobby.
        self.assertFalse(
            cross_trade.state_matches(
                "lobby",
                {
                    "action_green": 0.1598,
                    "action_orange": 0.0000,
                    "search_pale": 0.0331,
                    "white_panel": 0.8196,
                    "card_band": 0.9070,
                    "card_edges": 0.8300,
                },
            )
        )

    def test_the_action_patch_is_read_for_amber_as_well_as_green(self) -> None:
        waiting = Image.new("RGB", (720, 1600), (101, 178, 244))
        ImageDraw.Draw(waiting).rectangle((140, 1150, 580, 1260), fill=(249, 177, 54))
        metrics = cross_trade.screen_metrics(waiting, (360, 1205))
        self.assertGreater(metrics["action_orange"], 0.90)
        self.assertEqual(metrics["action_green"], 0.0)

        confirming = Image.new("RGB", (720, 1600), (101, 178, 244))
        ImageDraw.Draw(confirming).rectangle((140, 1150, 580, 1260), fill=(70, 205, 150))
        metrics = cross_trade.screen_metrics(confirming, (360, 1205))
        self.assertGreater(metrics["action_green"], 0.90)
        self.assertEqual(metrics["action_orange"], 0.0)

    def test_known_trade_entry_recovery_screens_are_specific(self) -> None:
        ios_modal = Image.new("RGB", (750, 1334), (30, 120, 130))
        ios_draw = ImageDraw.Draw(ios_modal)
        ios_draw.rectangle((0, 0, 750, 160), fill=(225, 75, 91))
        ios_draw.rectangle((40, 466, 710, 867), fill=(250, 252, 246))
        ios_draw.rectangle((164, 692, 586, 794), fill=(70, 205, 150))
        self.assertTrue(cross_trade.is_ios_trading_unavailable_dialog(ios_modal))

        android_waiting = Image.new("RGB", (720, 1600), (101, 178, 244))
        self.assertTrue(cross_trade.is_android_waiting_lobby(android_waiting))

        android_detail = android_waiting.copy()
        detail_draw = ImageDraw.Draw(android_detail)
        detail_draw.rectangle((36, 288, 684, 672), fill=(99, 158, 235))
        self.assertFalse(cross_trade.is_android_waiting_lobby(android_detail))


class IOSRecordCardTests(unittest.TestCase):
    """The iOS half of the same warning the Android side carries: this tap on a
    screen that needed no tap desyncs the loop, and the run then dies a step or
    two later somewhere that says nothing about why."""

    BUNDLE = "com.nianticlabs.pokemongo"

    def card(self) -> Image.Image:
        image = Image.new("RGB", (750, 1334), (30, 32, 36))
        ImageDraw.Draw(image).rectangle((0, 700, 750, 1250), fill=(230, 40, 60))
        return image

    def controller(self) -> object:
        return cross_trade.IOSController(
            FakeDriver(self.BUNDLE), {"X_BTN": [50, 50]}, self.BUNDLE
        )

    def test_the_frame_a_dismissal_fired_on_is_kept(self) -> None:
        controller = self.controller()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(cross_trade, "RECORD_DIAGNOSTICS_DIR", Path(tmp)),
                patch.object(cross_trade, "RECORD_DISMISS_DELAY", 0),
                patch.object(controller, "_screenshot_image", side_effect=[
                    self.card(), Image.new("RGB", (750, 1334), (30, 32, 36))
                ]),
            ):
                controller._post_trade_cleanup()

            saved = list(Path(tmp).iterdir())
            self.assertEqual(len(saved), 1)
            self.assertIn("ios", saved[0].name)
        self.assertIn("mobile: tap", controller.driver.scripts)

    def test_nothing_is_written_when_no_card_is_on_screen(self) -> None:
        controller = self.controller()
        plain = Image.new("RGB", (750, 1334), (30, 32, 36))
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(cross_trade, "RECORD_DIAGNOSTICS_DIR", Path(tmp)),
                patch.object(cross_trade, "RECORD_DISMISS_DELAY", 0),
                patch.object(controller, "_screenshot_image", return_value=plain),
            ):
                controller._post_trade_cleanup()

            self.assertEqual(list(Path(tmp).iterdir()), [])
        self.assertNotIn("mobile: tap", controller.driver.scripts)


class DialogButtonTests(unittest.TestCase):
    """Finding a dialog's own button in the picture. A run meets several
    dialogs and their buttons sit at different heights, so one mapped
    coordinate per handset does not cover them."""

    def dialog(self, pills: tuple[tuple[int, int], ...]) -> Image.Image:
        """A dimmed screen with a white card on it, holding pills at the given
        (top, bottom) rows — the shape of every dialog the trade sequence meets."""
        image = Image.new("RGB", (720, 1600), (48, 62, 74))
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 500, 660, 1180), fill=(250, 252, 246))
        for top, bottom in pills:
            draw.rectangle((140, top, 580, bottom), fill=(70, 205, 150))
        return image

    def test_the_pill_in_a_one_button_dialog_is_found(self) -> None:
        """"Trade expired." — one OK, and pressing it is the only way on."""
        point = cross_trade.find_dialog_button(self.dialog(((850, 960),)))

        self.assertIsNotNone(point)
        assert point is not None
        self.assertAlmostEqual(point[0], 360, delta=12)
        self.assertAlmostEqual(point[1], 905, delta=12)

    def test_the_top_pill_is_taken_when_a_dialog_has_two(self) -> None:
        """YES sits above NO, and YES is the answer that moves the phone on."""
        point = cross_trade.find_dialog_button(
            self.dialog(((760, 870), (960, 1070)))
        )

        assert point is not None
        self.assertAlmostEqual(point[1], 815, delta=12)

    def test_a_dialog_with_no_pill_is_not_guessed_at(self) -> None:
        """None is a real answer: the caller leaves the screen alone rather
        than pressing where a button usually is."""
        self.assertIsNone(cross_trade.find_dialog_button(self.dialog(())))

    def test_a_green_bar_that_is_not_inside_a_card_is_not_a_pill(self) -> None:
        """A band running edge to edge is a banner or a progress track. No
        white either side rules it out of the card pills, and its width rules
        it out of the overlay notices, whose buttons have screen either side."""
        screen = Image.new("RGB", (720, 1600), (101, 178, 244))
        ImageDraw.Draw(screen).rectangle((0, 1150, 720, 1260), fill=(70, 205, 150))

        self.assertIsNone(cross_trade.find_dialog_button(screen))


class OverlayNoticeButtonTests(unittest.TestCase):
    """The notices that have no card at all.

    "New Mega Level available!" dims the Pokémon's detail screen and writes
    onto it, so its OK has the dimmed screen either side rather than white and
    the flanked reading finds nothing. Measured off the android-one frames that
    stopped two runs (1316x2560): background (45, 104, 124), the OK pill
    y 0.802-0.873 and 0.53 of the width, and the detail screen's own HP bar —
    the other wide green run on the picture — thin at y 0.504.
    """

    def notice(self, pill: bool = True, hp_bar: bool = True) -> Image.Image:
        image = Image.new("RGB", (1316, 2560), (45, 104, 124))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 930, 1316, 1186), fill=(255, 255, 255))
        if hp_bar:
            draw.rectangle((330, 1280, 990, 1302), fill=(79, 177, 152))
        if pill:
            draw.rectangle((310, 2054, 1006, 2236), fill=(115, 214, 155))
        return image

    def test_the_pill_is_found_without_a_card_around_it(self) -> None:
        point = cross_trade.find_dialog_button(self.notice())

        self.assertIsNotNone(point)
        assert point is not None
        self.assertAlmostEqual(point[0], 658, delta=20)
        self.assertAlmostEqual(point[1], 2145, delta=20)

    def test_the_hp_bar_above_it_is_not_taken_for_the_button(self) -> None:
        """It is higher up the picture and the topmost pill is what this
        takes, so thickness is the only thing keeping them apart."""
        point = cross_trade.find_dialog_button(self.notice())

        assert point is not None
        self.assertGreater(point[1], 1400)

    def test_a_notice_with_no_pill_is_still_not_guessed_at(self) -> None:
        self.assertIsNone(cross_trade.find_dialog_button(self.notice(pill=False)))

    def test_a_card_pill_is_still_read_before_the_loose_one(self) -> None:
        """The flanked reading runs first, so nothing about the dialogs that
        wear a card changes."""
        image = Image.new("RGB", (720, 1600), (48, 62, 74))
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 500, 660, 1180), fill=(250, 252, 246))
        draw.rectangle((0, 200, 720, 400), fill=(70, 205, 150))
        draw.rectangle((140, 850, 580, 960), fill=(70, 205, 150))

        point = cross_trade.find_dialog_button(image)

        assert point is not None
        self.assertAlmostEqual(point[1], 905, delta=12)


class IncidentalNoticeTests(unittest.TestCase):
    """Notices the game raises by itself, read by their words.

    Shape says nothing here — every question the game asks during a trade
    wears the same green pill, and answering one of those puts a trade through.
    """

    def notice(self) -> Image.Image:
        """The mega notice's geometry: a dimmed screen with a white band deep
        enough to fill the head band, and nothing reaching the outer columns."""
        image = Image.new("RGB", (1316, 2560), (45, 104, 124))
        ImageDraw.Draw(image).rectangle((0, 930, 1316, 1186), fill=(255, 255, 255))
        return image

    def read(self, *lines: str) -> str | None:
        boxes = [
            gbl_vision.OCRBox(text=line, confidence=0.9, x=0, y=index * 50, width=200, height=40)
            for index, line in enumerate(lines)
        ]
        with patch.object(gbl_vision, "recognize", return_value=boxes):
            return cross_trade.incidental_notice(self.notice())

    def test_the_mega_notice_is_named(self) -> None:
        self.assertEqual(
            self.read(
                "New Mega Level available!",
                "This Pokémon can now reach Super Max Level and earn even",
                "stronger bonuses. Learn more on the Mega Level page.",
                "OK",
            ),
            "New Mega Level available!",
        )

    def test_anything_about_a_trade_is_not_one(self) -> None:
        """The disqualifier rather than one more phrase to match: a notice
        this presses is one the run has no stake in."""
        self.assertIsNone(self.read("Do you want to cancel the trade?"))
        self.assertIsNone(self.read("Trade expired."))
        self.assertIsNone(
            self.read("Daily trading limit reached. Come back tomorrow to trade more.")
        )

    def test_a_dialog_it_has_no_words_for_is_not_one(self) -> None:
        self.assertIsNone(self.read("Are you sure you want to power up this Pokémon?"))

    def test_a_screen_that_is_not_a_dialog_is_not_read(self) -> None:
        with patch.object(gbl_vision, "recognize") as recognize:
            plain = Image.new("RGB", (720, 1600), (101, 178, 244))
            self.assertIsNone(cross_trade.incidental_notice(plain))
        recognize.assert_not_called()


class MapScreenTests(unittest.TestCase):
    """The map is not a trade screen, and the colour tests cannot see that.

    A android-two that had fallen out to the map was read as a Pokémon's detail
    screen — the grass under NEXT_BTN's coordinate is as green as the button
    that belongs there — and recovery pressed BACK on it, which is the game's
    "Do you want to exit Pokémon GO?" prompt.
    """

    def map_screen(self, ball: bool = True) -> Image.Image:
        """Grass with the map's pokéball at the bottom: red over white, wide
        enough not to be a map pin."""
        image = Image.new("RGB", (720, 1600), (108, 200, 120))
        if ball:
            draw = ImageDraw.Draw(image)
            draw.rectangle((320, 1360, 400, 1410), fill=(235, 70, 60))
            draw.rectangle((320, 1412, 400, 1460), fill=(250, 250, 248))
        return image

    def points(self, name: str) -> tuple[int, int] | None:
        # The android-two's own trade coordinates, off the phone's config.
        return {
            "TRADE_BTN": (359, 1275),
            "NEXT_BTN": (359, 1272),
            "CONFIRM_BTN": (104, 824),
            "X_BTN": (367, 1421),
        }.get(name)

    def test_the_map_is_named(self) -> None:
        self.assertTrue(cross_trade.is_map_screen(self.map_screen()))
        state, _ = cross_trade.describe_state(self.map_screen(), self.points)
        self.assertEqual(state, "map")

    def test_grass_without_the_ball_is_not_the_map(self) -> None:
        """A guess here would be worse than "unknown": it stops the run."""
        self.assertFalse(cross_trade.is_map_screen(self.map_screen(ball=False)))

    def test_a_trade_screen_is_not_the_map(self) -> None:
        plain = Image.new("RGB", (720, 1600), (101, 178, 244))

        self.assertFalse(cross_trade.is_map_screen(plain))


class DailyTradingLimitDialogTests(unittest.TestCase):
    """The day's cap, read by its words: it wears the same card and the same
    single green pill as the notices that mean something retryable."""

    def dialog(self) -> Image.Image:
        image = Image.new("RGB", (720, 1600), (56, 140, 118))
        draw = ImageDraw.Draw(image)
        draw.rectangle((36, 600, 684, 990), fill=(250, 252, 248))
        draw.rectangle((158, 822, 562, 916), fill=(114, 214, 155))
        return image

    def read(self, *lines: str) -> bool:
        boxes = [
            gbl_vision.OCRBox(text=line, confidence=0.9, x=0, y=index * 50, width=200, height=40)
            for index, line in enumerate(lines)
        ]
        with patch.object(gbl_vision, "recognize", return_value=boxes):
            return cross_trade.is_trade_limit_dialog(self.dialog())

    def test_the_cap_is_read(self) -> None:
        self.assertTrue(
            self.read("Daily trading limit reached. Come", "back tomorrow to trade more.")
        )

    def test_the_expired_notice_is_not_the_cap(self) -> None:
        self.assertFalse(self.read("Trade expired."))

    def test_a_screen_that_is_not_a_dialog_is_not_read(self) -> None:
        with patch.object(gbl_vision, "recognize") as recognize:
            plain = Image.new("RGB", (720, 1600), (101, 178, 244))
            self.assertFalse(cross_trade.is_trade_limit_dialog(plain))
        recognize.assert_not_called()


class ExitGameDialogTests(unittest.TestCase):
    """The one dialog whose pill must never be pressed: OK closes the game.

    The layout below is measured off the android-two frame that reached it — card
    y 0.363-0.632, OK pill y 0.470-0.533, CANCEL at y 0.575 as a scattering of
    (153, 222, 196) ink on the card rather than a solid run.
    """

    def dialog(self, cancel: bool = True) -> Image.Image:
        image = Image.new("RGB", (720, 1600), (56, 140, 118))
        draw = ImageDraw.Draw(image)
        draw.rectangle((36, 580, 684, 1011), fill=(250, 252, 248))
        draw.rectangle((158, 752, 562, 853), fill=(114, 214, 155))
        if cancel:
            for x in range(296, 424, 6):
                draw.rectangle((x, 912, x + 2, 930), fill=(153, 222, 196))
        return image

    def read(self, image: Image.Image, *lines: str) -> bool:
        boxes = [
            gbl_vision.OCRBox(text=line, confidence=0.9, x=0, y=index * 50, width=200, height=40)
            for index, line in enumerate(lines)
        ]
        with patch.object(gbl_vision, "recognize", return_value=boxes):
            return cross_trade.is_exit_game_dialog(image)

    def test_the_prompt_is_read(self) -> None:
        self.assertTrue(self.read(self.dialog(), "Do you want to exit Pokémon GO?"))

    def test_quit_is_read_too(self) -> None:
        self.assertTrue(self.read(self.dialog(), "Quit Pokémon GO?"))

    def test_another_dialog_is_not_the_prompt(self) -> None:
        """The trade questions wear the same card and the same pill."""
        self.assertFalse(self.read(self.dialog(), "Do you want to cancel the trade?"))

    def test_a_screen_that_is_not_a_dialog_is_not_read(self) -> None:
        """No OCR on an ordinary trade screen: the geometry gate comes first."""
        with patch.object(gbl_vision, "recognize") as recognize:
            plain = Image.new("RGB", (720, 1600), (101, 178, 244))
            self.assertFalse(cross_trade.is_exit_game_dialog(plain))
        recognize.assert_not_called()

    def test_cancel_is_found_below_the_pill(self) -> None:
        point = cross_trade.find_dialog_label_button(self.dialog())

        self.assertIsNotNone(point)
        assert point is not None
        self.assertAlmostEqual(point[0], 360, delta=15)
        self.assertAlmostEqual(point[1], 920, delta=15)

    def test_the_pill_is_not_mistaken_for_the_label(self) -> None:
        """A solid run of green is a button; a label is a scattering."""
        point = cross_trade.find_dialog_label_button(self.dialog())

        assert point is not None
        self.assertGreater(point[1], 853)

    def test_a_prompt_with_no_label_gives_nothing_back(self) -> None:
        self.assertIsNone(cross_trade.find_dialog_label_button(self.dialog(cancel=False)))


class TradeExpiredDialogTests(unittest.TestCase):
    """The one screen in the trade path read by its words. Every dialog wears
    the same white card and the same green pill, so shape cannot separate the
    notice that a trade is dead from the question whose answer completes one."""

    def dialog(self) -> Image.Image:
        image = Image.new("RGB", (720, 1600), (48, 62, 74))
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 500, 660, 1180), fill=(250, 252, 246))
        draw.rectangle((140, 850, 580, 960), fill=(70, 205, 150))
        return image

    def boxes(self, *lines: str) -> list[gbl_vision.OCRBox]:
        return [
            gbl_vision.OCRBox(text=line, confidence=0.9, x=0, y=index * 50, width=200, height=40)
            for index, line in enumerate(lines)
        ]

    def read(self, image: Image.Image, *lines: str) -> bool:
        with patch.object(gbl_vision, "recognize", return_value=self.boxes(*lines)):
            return cross_trade.is_trade_expired_dialog(image)

    def test_the_notice_is_read(self) -> None:
        self.assertTrue(
            self.read(self.dialog(), "Trade expired.", "This trade was canceled.", "OK")
        )

    def test_the_other_wording_is_read_too(self) -> None:
        """The tall_device's version names the trainer instead of a second line."""
        self.assertTrue(
            self.read(self.dialog(), "This trade with BadMannas has expired", "OK")
        )

    def test_a_question_is_not_the_notice(self) -> None:
        """Answering this one cancels a trade that is still alive."""
        self.assertFalse(
            self.read(self.dialog(), "Do you want to cancel the trade?", "YES", "NO")
        )

    def test_a_confirmation_is_not_the_notice(self) -> None:
        """And answering this one puts a trade through."""
        self.assertFalse(
            self.read(
                self.dialog(), "Are you sure you want to trade this Pokémon?", "YES", "NO"
            )
        )

    def test_a_screen_that_is_not_a_dialog_is_never_read(self) -> None:
        """What keeps the OCR off the trade path: the cheap geometry answers
        first, and only a dialog costs a recognition."""
        screen = Image.new("RGB", (720, 1600), (101, 178, 244))

        with patch.object(gbl_vision, "recognize") as recognize:
            self.assertFalse(cross_trade.is_trade_expired_dialog(screen))

        recognize.assert_not_called()

    def test_an_ocr_failure_is_not_a_run_ending_error(self) -> None:
        """The helper has to compile itself the first time. A trade run is not
        worth ending over that; the screen just stays unread."""
        with patch.object(
            gbl_vision, "recognize", side_effect=gbl_vision.VisionOCRError("no helper")
        ):
            self.assertFalse(cross_trade.is_trade_expired_dialog(self.dialog()))


class EmptyHandedTradeScreenTests(unittest.TestCase):
    """Telling the trade a phone has not offered into apart from the one it
    has. Both are the same flat blue; only one of them has a pill on it."""

    TRADE_BLUE = (110, 190, 245)

    def trade_screen(self, pill: bool) -> Image.Image:
        image = Image.new("RGB", (750, 1334), self.TRADE_BLUE)
        draw = ImageDraw.Draw(image)
        # The other trainer's card, which both screens carry at the top.
        draw.rectangle((0, 236, 410, 490), fill=(90, 150, 220))
        if pill:
            draw.rectangle((160, 1030, 560, 1120), fill=(120, 210, 150))
        return image

    def test_a_trade_with_nothing_offered_is_named(self) -> None:
        self.assertTrue(
            cross_trade.is_empty_handed_trade_screen(self.trade_screen(pill=False))
        )

    def test_the_detail_screen_is_not_it(self) -> None:
        """Its NEXT pill is the difference, and pressing the corner door on it
        would throw away a Pokémon this phone had already chosen."""
        self.assertFalse(
            cross_trade.is_empty_handed_trade_screen(self.trade_screen(pill=True))
        )

    def test_a_screen_that_is_not_blue_is_not_it(self) -> None:
        self.assertFalse(
            cross_trade.is_empty_handed_trade_screen(
                Image.new("RGB", (750, 1334), (247, 250, 240))
            )
        )


class IOSPointConversionTests(unittest.TestCase):
    def test_a_pixel_found_by_looking_comes_back_as_a_tappable_point(self) -> None:
        """A screenshot is in device pixels and `mobile: tap` wants logical
        points: on this 2x phone a pixel tapped as a point lands at half the
        height it was found at."""
        controller = cross_trade.IOSController(
            FakeDriver("com.nianticlabs.pokemongo"), {}, "com.nianticlabs.pokemongo"
        )
        image = Image.new("RGB", (750, 1334))

        self.assertEqual(controller.point_from_image(image, (360, 905)), (180, 452))
        # And it undoes image_point, which sends a mapped point the other way.
        controller.coordinates = {"X_BTN": [180, 452]}
        pixel = controller.image_point(image, "X_BTN")
        assert pixel is not None
        self.assertEqual(controller.point_from_image(image, pixel), (180, 452))


class SequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sequence_taps_both_platforms_and_skips_optional_ios(self) -> None:
        android = FakeAndroid()
        ios = FakeIOS()

        async def no_sleep(_: float) -> None:
            await asyncio.sleep(0)

        with (
            patch.object(cross_trade.android_trade, "tap", new=AsyncMock()) as android_tap,
            patch.object(cross_trade, "guard_screen_state", new=AsyncMock()) as guard,
            patch.object(
                cross_trade,
                "android_post_trade_cleanup",
                new=AsyncMock(),
            ) as android_cleanup,
        ):
            await cross_trade.execute_trade_sequence(
                android,
                ios,
                cross_trade.DEFAULT_STEPS,
                delay_modifier=0,
                sleep=no_sleep,
            )

        self.assertEqual(android_tap.await_count, len(cross_trade.DEFAULT_STEPS))
        self.assertNotIn("MAX_LEVEL_RESET_BTN", ios.tapped)
        self.assertEqual(len(ios.tapped), len(cross_trade.DEFAULT_STEPS) - 1)
        android_cleanup.assert_awaited_once_with(android)
        self.assertEqual(ios.cleaned, 1)
        self.assertEqual(guard.await_count, 6)

    async def test_repeated_ios_service_error_recovers_without_android_taps(self) -> None:
        android = FakeAndroid()
        ios = FakeIOS()
        android_waiting = object()
        ios_modal = object()
        ios_friend = object()
        android_selection = object()
        ios_selection = object()

        async def no_sleep(_: float) -> None:
            await asyncio.sleep(0)

        def metrics(image: object, _: object) -> dict[str, object]:
            return {"image": image}

        def matches(expected: str, values: dict[str, object]) -> bool:
            image = values["image"]
            if expected == "selection":
                return image in {android_selection, ios_selection}
            if expected == "friend":
                return image is ios_friend
            return False

        ios.screenshot_image = AsyncMock(
            side_effect=[ios_modal, ios_friend, ios_selection]
        )
        with (
            patch.object(
                cross_trade,
                "android_screenshot_image",
                new=AsyncMock(
                    side_effect=[
                        android_waiting,
                        android_waiting,
                        android_selection,
                    ]
                ),
            ),
            patch.object(
                cross_trade,
                "is_android_waiting_lobby",
                side_effect=lambda image: image is android_waiting,
            ),
            patch.object(
                cross_trade,
                "is_ios_trading_unavailable_dialog",
                side_effect=lambda image: image is ios_modal,
            ),
            patch.object(cross_trade, "screen_metrics", side_effect=metrics),
            patch.object(cross_trade, "state_matches", side_effect=matches),
            patch.object(cross_trade.android_trade, "tap", new=AsyncMock()) as tap,
        ):
            await cross_trade.recover_ios_trade_entry(
                android,
                ios,
                android_waiting,
                ios_modal,
                sleep=no_sleep,
            )

        self.assertEqual(
            ios.points,
            [
                cross_trade.IOS_TRADING_UNAVAILABLE_OK_POINT,
                cross_trade.IOS_TRADING_UNAVAILABLE_OK_POINT,
            ],
        )
        self.assertEqual(ios.tapped, ["TRADE_BTN"])
        tap.assert_not_awaited()

    async def test_recovery_stops_after_unclassified_transition(self) -> None:
        android = FakeAndroid()
        ios = FakeIOS()
        android_waiting = object()
        ios_modal = object()
        android_unknown = object()
        ios_unknown = object()

        async def no_sleep(_: float) -> None:
            await asyncio.sleep(0)

        ios.screenshot_image = AsyncMock(side_effect=[ios_unknown, ios_unknown])
        with (
            patch.object(
                cross_trade,
                "android_screenshot_image",
                new=AsyncMock(side_effect=[android_unknown, android_unknown]),
            ),
            patch.object(
                cross_trade,
                "is_android_waiting_lobby",
                side_effect=lambda image: image is android_waiting,
            ),
            patch.object(
                cross_trade,
                "is_ios_trading_unavailable_dialog",
                side_effect=lambda image: image is ios_modal,
            ),
            patch.object(cross_trade, "screen_metrics", return_value={}),
            patch.object(cross_trade, "state_matches", return_value=False),
            patch.object(cross_trade, "RECOVERY_UNKNOWN_ATTEMPTS", 2),
            patch.object(
                cross_trade,
                "save_mismatch_diagnostics",
                return_value=Path("diagnostics"),
            ),
            patch.object(cross_trade.android_trade, "tap", new=AsyncMock()) as tap,
        ):
            with self.assertRaisesRegex(
                cross_trade.CrossPlatformTradeError,
                "unclassified screen state",
            ):
                await cross_trade.recover_ios_trade_entry(
                    android,
                    ios,
                    android_waiting,
                    ios_modal,
                    sleep=no_sleep,
                )

        self.assertEqual(ios.points, [cross_trade.IOS_TRADING_UNAVAILABLE_OK_POINT])
        self.assertEqual(ios.tapped, [])
        tap.assert_not_awaited()


class SessionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def runtime_config(self) -> cross_trade.RuntimeConfig:
        return cross_trade.RuntimeConfig(
            ios_coordinates={
                name: [1, 1] for name in cross_trade.REQUIRED_IOS_COORDINATES
            },
            ios_device={
                "udid": "test-udid",
                "team_id": "test-team",
                "wda_bundle_id": "test.wda",
            },
            appium_server_url="http://127.0.0.1:4723",
            android_serial=None,
            delay_modifier=0,
            steps=cross_trade.DEFAULT_STEPS,
        )

    async def test_capture_never_connects_while_gift_runner_is_active(self) -> None:
        args = SimpleNamespace(
            count=1,
            config=Path("unused.yaml"),
            ios_config=None,
            android_serial=None,
            delay_modifier=None,
            capture_ios=Path("capture.png"),
            check=False,
            dry_run=False,
        )
        connect = Mock()
        with (
            patch.object(
                cross_trade,
                "load_runtime_config",
                return_value=self.runtime_config(),
            ),
            patch.object(cross_trade, "check_appium_status"),
            patch.object(cross_trade, "active_gift_runner_pids", return_value=[123]),
            patch.object(cross_trade.IOSController, "connect", new=connect),
        ):
            with self.assertRaisesRegex(
                cross_trade.CrossPlatformTradeError, "gift runner is active"
            ):
                await cross_trade.async_main(args)
        connect.assert_not_called()


class DeviceSelectionTests(unittest.IsolatedAsyncioTestCase):
    """Any of the phones can be the Android side; see select_android_device."""

    async def select(self, serial, required, connected):
        devices = [SimpleNamespace(serial=name) for name in connected]
        adb = AsyncMock(
            return_value=SimpleNamespace(
                communicate=AsyncMock(return_value=(b"", b"")), returncode=0
            )
        )
        client = Mock(return_value=SimpleNamespace(devices=AsyncMock(return_value=devices)))
        with (
            patch.object(cross_trade.asyncio, "create_subprocess_exec", new=adb),
            patch.object(cross_trade.android_trade, "ClientAsync", new=client),
            patch.object(cross_trade.android_trade, "find_display_id", new=AsyncMock()),
            patch.object(cross_trade, "load_android_config", new=AsyncMock()),
        ):
            return await cross_trade.select_android_device(serial, required)

    async def test_falls_back_to_the_phone_that_is_plugged_in(self) -> None:
        """The config names the tall_device, but the android-one is the one on this Mac."""
        device = await self.select("test_device_9", False, ["test_device_4"])
        self.assertEqual(device.serial, "test_device_4")

    async def test_prefers_the_configured_phone_when_it_is_there(self) -> None:
        device = await self.select("test_device_8", False, ["test_device_4", "test_device_8"])
        self.assertEqual(device.serial, "test_device_8")

    async def test_will_not_guess_between_two_unconfigured_phones(self) -> None:
        with self.assertRaisesRegex(
            cross_trade.CrossPlatformTradeError, "--android-serial"
        ):
            await self.select("test_device_9", False, ["test_device_4", "test_device_8"])

    async def test_a_serial_asked_for_on_the_command_line_is_not_second_guessed(self) -> None:
        with self.assertRaisesRegex(cross_trade.CrossPlatformTradeError, "was not found"):
            await self.select("test_device_9", True, ["test_device_4"])

    async def test_no_serial_and_one_phone(self) -> None:
        device = await self.select(None, False, ["test_device_4"])
        self.assertEqual(device.serial, "test_device_4")

    async def test_no_phone_at_all(self) -> None:
        with self.assertRaisesRegex(cross_trade.CrossPlatformTradeError, "No authorized"):
            await self.select("test_device_9", False, [])


if __name__ == "__main__":
    unittest.main()
