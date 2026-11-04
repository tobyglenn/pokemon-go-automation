from __future__ import annotations

from tests import support as _test_support

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import catch_awarded_android as awarded


class FakePhone:
    """A phone that records taps and serves a fixed frame."""

    def __init__(self) -> None:
        self.label = "fake-g"
        self.viewport = (720, 1600)
        self.taps: list[list[int]] = []
        self.shells: list[str] = []

    async def tap(self, point) -> None:
        self.taps.append(list(point))

    async def shell(self, command: str) -> str:
        self.shells.append(command)
        return ""


def blank() -> Image.Image:
    return Image.new("RGB", (720, 1600), (70, 110, 60))


class ItemSheetRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """A sheet left standing over an encounter must not end the run.

    An open berry or ball chooser hides the ball, so the encounter recovery
    reads the phone as having nothing to throw at and reports the queue
    finished.  The moto did that on 10 Sep 2026 with nine awards still queued
    and a Swinub on screen behind the sheet.
    """

    async def close(self, sheet_top: int | None) -> FakePhone:
        device = FakePhone()
        with patch.object(
            awarded.excellent_throw_android,
            "capture_frame",
            AsyncMock(return_value=(blank(), 0.0)),
        ), patch(
            "sources.berry_android.picker_sheet_top", lambda *_a: sheet_top
        ), patch(
            "asyncio.sleep", AsyncMock()
        ):
            self.closed = await awarded.close_item_sheet(device)
        return device

    async def test_an_open_sheet_is_tapped_away(self) -> None:
        device = await self.close(900)
        self.assertTrue(self.closed)
        self.assertEqual(device.taps, [[360, 720]], "the sheet was not put away")

    async def test_a_screen_with_no_sheet_is_left_alone(self) -> None:
        device = await self.close(None)
        self.assertFalse(self.closed)
        self.assertEqual(device.taps, [], "an encounter was tapped for no reason")

    async def test_the_sheet_is_never_closed_with_back(self) -> None:
        """BACK leaves the encounter, not the sheet, and abandons the Pokemon."""
        device = await self.close(900)
        self.assertFalse(
            [command for command in device.shells if "keyevent" in command],
            f"an encounter key was pressed: {device.shells}",
        )


class LateAwardCardTests(unittest.IsolatedAsyncioTestCase):
    """A card that is slow to come back is not an empty queue.

    On 5 Oct 2026 android-three was given up on with four still queued, and the android-two
    before a set of sixteen came up, both after a 38-second wait.
    """

    async def wait(self, reads: list, patience: float) -> tuple:
        clock = [0.0]

        async def card_after(_device, timeout):
            clock[0] += timeout
            return reads.pop(0) if reads else None

        args = SimpleNamespace(card_wait=30.0)
        cleared = AsyncMock()
        with patch.object(awarded, "wait_for_award_card", card_after), patch.object(
            awarded, "clear_catch_screens", cleared
        ), patch.object(awarded, "close_item_sheet", AsyncMock()), patch.object(
            awarded, "encounter_open", AsyncMock(return_value=False)
        ), patch.object(
            awarded, "capture_frame", AsyncMock(return_value=(blank(), 0.0))
        ), patch.object(
            awarded.time, "monotonic", lambda: clock[0]
        ), tempfile.TemporaryDirectory() as directory:
            card = await awarded.next_award_card(
                FakePhone(), args, Path(directory), patience
            )
            saved = (Path(directory) / "no-card.jpg").exists()
        return card, clock[0], saved

    async def test_a_late_card_is_still_worked(self) -> None:
        late = awarded.AwardCard(remaining=4, start_point=[360, 974])
        card, waited, _saved = await self.wait([None, None, None, None, late], 300.0)
        self.assertIs(card, late)
        self.assertGreater(waited, 38.0)

    async def test_patience_runs_out(self) -> None:
        card, waited, saved = await self.wait([], 300.0)
        self.assertIsNone(card)
        self.assertEqual(waited, 300.0)
        self.assertTrue(saved, "the screen the run gave up on was not kept")

    async def test_the_first_card_keeps_the_short_wait(self) -> None:
        card, waited, _saved = await self.wait([], 0.0)
        self.assertIsNone(card)
        self.assertEqual(waited, 30.0)


class AwardCardTests(unittest.TestCase):
    """The card's own words are what say how much of the queue is left."""

    def test_a_frame_with_no_card_is_no_card(self) -> None:
        with patch.object(awarded.gbl_vision, "recognize", return_value=[]):
            self.assertIsNone(awarded.card_from_frame(blank()))


if __name__ == "__main__":
    unittest.main()


class PlatePointTests(unittest.TestCase):
    def test_the_plate_middle_spans_name_and_cp(self) -> None:
        from sources import gbl_vision

        boxes = [
            gbl_vision.OCRBox("Tympole", 1.0, 10, 150, 80, 20),
            gbl_vision.OCRBox("CP 343", 1.0, 110, 152, 60, 18),
            gbl_vision.OCRBox("3894", 1.0, 300, 800, 40, 20),
        ]
        self.assertEqual(awarded.plate_point(boxes, 2.0), (180, 322))

    def test_no_cp_means_no_plate(self) -> None:
        from sources import gbl_vision

        boxes = [gbl_vision.OCRBox("Tympole", 1.0, 10, 150, 80, 20)]
        self.assertIsNone(awarded.plate_point(boxes, 1.0))


class ThrowReachTests(unittest.TestCase):
    """Every ball fell short of a Metang on 3 Nov 2026; misses lengthen the flick."""

    def test_reach_grows_with_misses_then_starts_over(self) -> None:
        reaches = [awarded.throw_reach(miss) for miss in range(6)]
        self.assertEqual(reaches[0], 1.0)
        self.assertTrue(all(b > a for a, b in zip(reaches[:4], reaches[1:5])))
        self.assertEqual(reaches[5], 1.0)

    def test_a_longer_reach_ends_the_flick_higher(self) -> None:
        from sources import excellent_throw_android as throw, excellent_throw_ios

        ball = excellent_throw_ios.BallDetection(0.9, 540, 2200, 200)
        _start, proven = throw.proven_throw_points(ball, (1080, 2412))
        _start, longer = throw.proven_throw_points(ball, (1080, 2412), None, 1.48)
        self.assertLess(longer[1], proven[1])
        self.assertGreaterEqual(longer[1], round(2412 * throw.PROVEN_THROW_CEILING))
