from __future__ import annotations

from tests import support as _test_support

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import catch_ios, catch_rewards, excellent_throw_android


def frame() -> Image.Image:
    return Image.new("RGB", (750, 1334), (70, 110, 60))


class FakeSession:
    """The slice of gbl_ios.IOSGBLDevice an IOSCatchPhone touches."""

    def __init__(self) -> None:
        self.viewport = {"width": 375, "height": 667}
        self.label = "Second iPhone (SE)"
        self.taps: list[list[int]] = []
        self.scrolls: list[tuple[list[int], list[int], float]] = []
        self.screenshot = AsyncMock(return_value=frame())

    async def tap(self, point) -> None:
        self.taps.append(list(point))

    async def scroll(self, start, end, duration) -> None:
        self.scrolls.append((start, end, duration))

    def _screenshot(self) -> Image.Image:
        return frame()


def iphone() -> catch_ios.IOSCatchPhone:
    return catch_ios.IOSCatchPhone(FakeSession(), "iphone-second", (750, 1334))


class IPhoneCoordinateTests(unittest.IsolatedAsyncioTestCase):
    """The loops speak screenshot pixels; WebDriverAgent taps in points."""

    async def test_a_tap_is_scaled_to_points(self) -> None:
        phone = iphone()
        await phone.tap([400, 1000])
        self.assertEqual(phone.session.taps, [[200, 500]])

    async def test_a_swipe_is_scaled_to_points(self) -> None:
        phone = iphone()
        await phone.input_swipe(375, 960, 375, 506, 320)
        self.assertEqual(phone.session.scrolls, [([188, 480], [188, 253], 0.32)])

    async def test_the_name_is_the_fleet_name(self) -> None:
        phone = iphone()
        self.assertEqual(phone.label, "iphone-second")
        self.assertEqual(phone.session.label, "iphone-second")

    async def test_back_presses_nothing(self) -> None:
        """No BACK key exists; guessing one taps the GO Snapshot shutter."""
        phone = iphone()
        await phone.press_back()
        self.assertEqual(phone.session.taps, [])
        self.assertEqual(phone.session.scrolls, [])


class IPhoneThrowTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_ball_is_no_encounter(self) -> None:
        phone = iphone()
        with patch.object(catch_ios.excellent_throw_ios, "locate_throw_ball", lambda *_a: None), \
                patch("asyncio.sleep", AsyncMock()):
            with self.assertRaises(excellent_throw_android.NoEncounterError):
                await phone.run_once(
                    wait_seconds=0, artifact_dir=Path(tempfile.mkdtemp()), dry_run=False
                )

    async def test_a_ball_that_stays_gone_ends_the_encounter(self) -> None:
        phone = iphone()
        cleared = AsyncMock()
        with patch.object(catch_ios.excellent_throw_ios, "locate_throw_ball", lambda *_a: None), \
                patch.object(phone, "clear_catch_screens", cleared), \
                patch("asyncio.sleep", AsyncMock()), \
                patch.object(catch_ios, "RESULT_WINDOW", 0.05):
            self.assertTrue(await phone.throw_result(Path(tempfile.mkdtemp())))
        cleared.assert_awaited_once()

    async def test_a_ball_that_comes_back_is_a_break_out(self) -> None:
        phone = iphone()
        with patch.object(catch_ios.excellent_throw_ios, "locate_throw_ball", lambda *_a: object()), \
                patch("asyncio.sleep", AsyncMock()):
            self.assertFalse(await phone.throw_result(Path(tempfile.mkdtemp())))

    async def test_a_refused_lock_flicks_for_the_rest_of_the_run(self) -> None:
        phone = iphone()
        thrower = type("Thrower", (), {"thrower": None, "stream": None, "berry": "nanab", "close": lambda self: None})()
        flick = AsyncMock(return_value=True)
        with patch.object(catch_ios.gbl_ios, "open_excellent_thrower", lambda _s: thrower), \
                patch.object(
                    catch_ios.excellent_throw_ios, "run_once",
                    AsyncMock(side_effect=catch_ios.excellent_throw_ios.ExcellentThrowError("no readable circle")),
                ), \
                patch.object(catch_ios.gbl_ios, "throw_ball", flick):
            self.assertTrue(await phone.throw(frame(), Path(tempfile.mkdtemp()), use_nanab=False))
            self.assertTrue(phone.flicking)
            self.assertTrue(await phone.throw(frame(), Path(tempfile.mkdtemp()), use_nanab=False))
        self.assertEqual(flick.await_count, 2)

    async def test_a_flick_on_a_fresh_encounter_feeds_a_nanab_first(self) -> None:
        phone = iphone()
        phone.flicking = True
        phone.thrower = type(
            "Thrower", (), {"thrower": type("D", (), {"viewport": (375, 667)})(), "berry": "nanab"}
        )()
        fed = AsyncMock()
        flick = AsyncMock(return_value=True)
        with patch.object(catch_ios.excellent_throw_ios, "locate_throw_ball", lambda *_a: object()), \
                patch.object(catch_ios.excellent_throw_ios, "use_nanab_berry", fed), \
                patch.object(phone, "wait_for_encounter", AsyncMock(return_value=frame())), \
                patch.object(catch_ios.gbl_ios, "throw_ball", flick):
            self.assertTrue(await phone.throw(frame(), Path(tempfile.mkdtemp()), use_nanab=True))
            fed.assert_awaited_once()
            self.assertTrue(await phone.throw(frame(), Path(tempfile.mkdtemp()), use_nanab=False))
        fed.assert_awaited_once()
        self.assertEqual(flick.await_args.kwargs, {"rise": catch_ios.FLICK_RISE, "step_ms": catch_ios.FLICK_STEP_MS})


class SelectionTests(unittest.TestCase):
    def test_nothing_named_means_every_phone(self) -> None:
        self.assertEqual(catch_rewards.split_selection(None, {"iphone-second"}), (None, None, True))
        self.assertEqual(catch_rewards.split_selection(["all"], {"iphone-second"}), (None, None, True))

    def test_an_iphone_alone_asks_for_no_android(self) -> None:
        self.assertEqual(
            catch_rewards.split_selection(["iphone-second"], {"iphone-second"}),
            (["iphone-second"], None, False),
        )

    def test_mixed_names_are_split_by_platform(self) -> None:
        self.assertEqual(
            catch_rewards.split_selection(["iphone-second", "SERIAL1"], {"iphone-second"}),
            (["iphone-second"], ["SERIAL1"], True),
        )


class ModeTests(unittest.IsolatedAsyncioTestCase):
    """The screen picks the job; a screen that names none gets no touch."""

    async def run_phone(self, modes: list[str | None], extra: list[str] | None = None):
        device = type("Phone", (), {"label": "fake-g", "viewport": (720, 1600)})()
        args = catch_rewards.parse_args(["--artifacts", tempfile.mkdtemp(), *(extra or [])])
        award = AsyncMock(return_value=0)
        research = AsyncMock(return_value=0)
        catch_one = AsyncMock(return_value=True)
        with patch.object(catch_rewards, "detect_mode", AsyncMock(side_effect=modes)), \
                patch.object(catch_rewards.catch_awarded_android, "run_device", award), \
                patch.object(catch_rewards.claim_research_android, "run_device", research), \
                patch.object(catch_rewards.catch_awarded_android, "catch_one", catch_one), \
                patch.object(catch_rewards.catch_awarded_android, "clear_catch_screens", AsyncMock()):
            status = await catch_rewards.run_phone(device, args)
        return status, award, research, catch_one

    async def test_an_award_card_works_the_queue(self) -> None:
        _status, award, research, _catch = await self.run_phone(["award"])
        award.assert_awaited_once()
        research.assert_not_awaited()

    async def test_a_claim_button_works_the_research(self) -> None:
        _status, award, research, _catch = await self.run_phone(["research"])
        research.assert_awaited_once()
        award.assert_not_awaited()

    async def test_an_unknown_screen_is_left_alone(self) -> None:
        status, award, research, catch_one = await self.run_phone([None])
        self.assertEqual(status, 0)
        award.assert_not_awaited()
        research.assert_not_awaited()
        catch_one.assert_not_awaited()

    async def test_an_open_encounter_is_caught_before_the_queue_behind_it(self) -> None:
        _status, award, _research, catch_one = await self.run_phone(["encounter", "award"])
        catch_one.assert_awaited_once()
        award.assert_awaited_once()

    async def test_a_forced_mode_skips_the_read(self) -> None:
        _status, _award, research, _catch = await self.run_phone([], extra=["--mode", "research"])
        research.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
