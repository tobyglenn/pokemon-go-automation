from __future__ import annotations

from tests import support as _test_support

import asyncio
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import trade_android as trade


def framebuffer(width: int = 4, height: int = 2) -> bytes:
    """A screencap's stdout: a 16 byte RGBA_8888 header, then the pixels."""
    return struct.pack('<IIII', width, height, 1, 0) + bytes(width * height * 4)


class WedgedProc:
    """A screencap child that never returns, the way a stuck transport leaves it."""
    def __init__(self) -> None:
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(3600)

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return -9


class DoneProc:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def communicate(self):
        return self.data, b''


class ScreencapTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.device = SimpleNamespace(serial='test-serial', display_id=None)
        for name, value in (('SCREENCAP_TIMEOUT', 0.01), ('SCREENCAP_RETRY_DELAY', 0)):
            patcher = patch.object(trade, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_wedged_screencap_is_killed_rather_than_waited_on(self) -> None:
        procs = []

        async def spawn(*args, **kwargs):
            procs.append(WedgedProc())
            return procs[-1]

        with patch('asyncio.create_subprocess_exec', spawn):
            self.assertIsNone(await trade.screencap_raw(self.device))
        self.assertEqual(len(procs), trade.SCREENCAP_ATTEMPTS)
        self.assertTrue(all(proc.killed for proc in procs))

    async def test_retry_after_a_wedge_returns_the_frame(self) -> None:
        procs = [WedgedProc(), DoneProc(framebuffer())]

        async def spawn(*args, **kwargs):
            return procs.pop(0)

        with patch('asyncio.create_subprocess_exec', spawn):
            frame = await trade.screencap_raw(self.device)
        self.assertIsNotNone(frame)
        self.assertEqual(frame[:3], (4, 2, 16))
        self.assertEqual(procs, [])

    async def test_truncated_read_is_retried(self) -> None:
        procs = [DoneProc(framebuffer()[:20]), DoneProc(framebuffer())]

        async def spawn(*args, **kwargs):
            return procs.pop(0)

        with patch('asyncio.create_subprocess_exec', spawn):
            frame = await trade.screencap_raw(self.device)
        self.assertIsNotNone(frame)
        self.assertEqual(procs, [])


if __name__ == '__main__':
    unittest.main()


def card_frame(width: int = 8, height: int = 8) -> tuple[int, int, int, bytes]:
    """A framebuffer whose lower half is one strong colour: a size record card."""
    top = bytes([20, 20, 20, 255]) * (width * height // 2)
    card = bytes([230, 40, 60, 255]) * (width * height // 2)
    return width, height, 0, top + card


class RecordCardFrameTests(unittest.IsolatedAsyncioTestCase):
    """A false positive here taps X on a screen that needed no tap and desyncs
    the loop, and the stop that causes lands somewhere else entirely — so every
    dismissal has to leave the frame it fired on behind."""

    async def test_the_frame_a_dismissal_fired_on_is_kept(self) -> None:
        frame = card_frame()
        device = SimpleNamespace(serial='android-two', config={'X_BTN': [1, 2]})

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(trade, 'RECORD_FRAMES_DIR', Path(tmp)),
                patch.object(trade, 'screencap_raw', AsyncMock(side_effect=[frame, None])),
                patch.object(trade, 'tap', AsyncMock()) as tapped,
                patch.object(trade, 'RECORD_DISMISS_DELAY', 0),
            ):
                await trade.dismiss_record_screen([device])

            saved = list(Path(tmp).iterdir())
            self.assertEqual(len(saved), 1)
            self.assertIn('android-two', saved[0].name)
            with Image.open(saved[0]) as image:
                self.assertEqual(image.size, (8, 8))
                # The card half of the frame, so the picture can be checked
                # against what the detector claimed to see.
                self.assertEqual(image.convert('RGB').getpixel((4, 6)), (230, 40, 60))
        tapped.assert_awaited_once()

    async def test_nothing_is_written_when_no_card_is_on_screen(self) -> None:
        plain = (8, 8, 0, bytes([20, 20, 20, 255]) * 64)
        device = SimpleNamespace(serial='android-two', config={'X_BTN': [1, 2]})

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(trade, 'RECORD_FRAMES_DIR', Path(tmp)),
                patch.object(trade, 'screencap_raw', AsyncMock(return_value=plain)),
                patch.object(trade, 'tap', AsyncMock()) as tapped,
                patch.object(trade, 'RECORD_DISMISS_DELAY', 0),
            ):
                await trade.dismiss_record_screen([device])

            self.assertEqual(list(Path(tmp).iterdir()), [])
        tapped.assert_not_awaited()
