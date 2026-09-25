"""Slow screenshots and refused throws must not become fictitious ball use."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from sources import excellent_throw_android as throw
from sources import excellent_throw_ios, gbl_android as gbl


class SlowBallConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_slow_captures_can_confirm_a_ball(self):
        clock = [0.0]
        reads = []
        device = SimpleNamespace(viewport=(720, 1600), label="example-phone")
        image = Image.new("RGB", device.viewport)

        async def capture(_device):
            clock[0] += 3.2
            reads.append(clock[0])
            return image, clock[0]

        with (
            TemporaryDirectory() as tmp,
            patch.object(throw.time, "monotonic", lambda: clock[0]),
            patch.object(throw, "capture_frame", capture),
            patch.object(throw.asyncio, "sleep", AsyncMock()),
            patch.object(excellent_throw_ios, "locate_throw_ball", return_value=
                         excellent_throw_ios.BallDetection(1.0, 360, 1300, 90)),
        ):
            result = await throw.detect_encounter_ball(device, 20, Path(tmp))
        self.assertEqual(len(reads), 2)
        self.assertEqual(result.score, 1.0)


class RewardCountTests(unittest.IsolatedAsyncioTestCase):
    async def run_encounter(self, outcome):
        device = SimpleNamespace(label="example-phone", config={}, shell=AsyncMock())
        messages = []
        with (
            patch.object(gbl, "read_screen", AsyncMock(return_value=object())),
            patch.object(gbl, "frame_image", return_value=Image.new("RGB", (100, 200))),
            patch.object(gbl.gbl_vision, "recognize", return_value=[]),
            patch.object(gbl.gbl_vision, "gbl_card_visible", side_effect=[False, True]),
            patch.object(gbl.gbl_vision, "encounter_plate", return_value=SimpleNamespace(text="CP 100")),
            patch.object(gbl, "throw_ball", AsyncMock(return_value=outcome)) as thrown,
            patch.object(gbl, "log", lambda _device, message: messages.append(message)),
        ):
            if outcome is None:
                with self.assertRaisesRegex(gbl.AutoGBLError, "after 0 sent throw"):
                    await gbl.take_reward_encounter(device)
            else:
                self.assertTrue(await gbl.take_reward_encounter(device))
        thrown.assert_awaited_once()
        device.shell.assert_not_awaited()
        return messages

    async def test_refused_throw_is_not_counted_or_fled(self):
        messages = await self.run_encounter(None)
        self.assertFalse(any("will not stay" in message for message in messages))

    async def test_sent_but_uncaught_throw_is_counted(self):
        messages = await self.run_encounter(False)
        self.assertTrue(any("after 1 throw(s)" in message for message in messages))

    async def test_detector_timeout_reports_no_throw(self):
        with (
            TemporaryDirectory() as tmp,
            patch.object(gbl.config_paths, "state_dir", return_value=Path(tmp)),
            patch.object(gbl, "frame_image", return_value=object()),
            patch.object(gbl, "excellent_thrower", return_value=object()),
            patch.object(throw, "run_once", AsyncMock(side_effect=throw.NoEncounterError("no ball"))) as run,
        ):
            result = await gbl.throw_ball(SimpleNamespace(label="example-phone"), object())
        self.assertIsNone(result)
        run.assert_awaited_once()
