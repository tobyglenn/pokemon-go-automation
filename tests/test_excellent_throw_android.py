from __future__ import annotations

from tests import support as _test_support

import asyncio
import struct
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from sources import excellent_throw_android as throw
from sources import excellent_throw_ios


def raw_frame(width: int, height: int, *, colorspace: bool) -> bytes:
    """What `adb exec-out screencap` writes, in both header shapes."""
    header = (
        struct.pack("<IIII", width, height, 1, 0)
        if colorspace
        else struct.pack("<III", width, height, 1)
    )
    return header + bytes([9, 8, 7, 255]) * (width * height)


class RawFramebufferTests(unittest.TestCase):
    """Both header shapes are on the fleet, so both have to decode."""

    def test_the_older_twelve_byte_header_decodes(self) -> None:
        image = throw.decode_raw_framebuffer(raw_frame(3, 2, colorspace=False))
        self.assertEqual((image.width, image.height), (3, 2))
        self.assertEqual(image.getpixel((0, 0)), (9, 8, 7))

    def test_the_colorspace_header_decodes(self) -> None:
        image = throw.decode_raw_framebuffer(raw_frame(3, 2, colorspace=True))
        self.assertEqual((image.width, image.height), (3, 2))
        self.assertEqual(image.getpixel((2, 1)), (9, 8, 7))

    def test_a_truncated_buffer_is_not_a_frame(self) -> None:
        self.assertIsNone(throw.decode_raw_framebuffer(b"nope"))
        self.assertIsNone(throw.decode_raw_framebuffer(struct.pack("<III", 9, 9, 1)))


class HoldLengthTests(unittest.TestCase):
    """The hold has to outlast the capture, or the ring is never on the frame.

    Measured on 24 Aug: one raw screencap costs 0.52-0.60s on the moto and
    1.09-1.21s on the android-one, against a configured 0.35s hold.  Every capture
    therefore finished after the release, `measure_ring` saw no ring on any
    Android phone, and every throw was a guessed fallback target.
    """

    def test_a_slow_phone_is_held_long_enough_to_be_photographed(self) -> None:
        for cost in (0.55, 1.21):
            with self.subTest(cost=cost):
                held = throw.hold_for_capture(cost, 0.35)
                self.assertGreaterEqual(held, cost * 2)

    def test_the_hold_leaves_room_for_the_wanted_samples(self) -> None:
        self.assertAlmostEqual(
            throw.hold_for_capture(0.5, 0.35),
            0.5 * throw.RING_SAMPLES + throw.RING_HOLD_MARGIN,
        )

    def test_a_configured_longer_hold_still_wins(self) -> None:
        self.assertEqual(throw.hold_for_capture(0.05, 2.5), 2.5)

    def test_a_phone_gone_slow_does_not_hold_the_run_forever(self) -> None:
        self.assertEqual(throw.hold_for_capture(30.0, 0.35), throw.RING_HOLD_CEILING)


@dataclass
class FakeConfig:
    calibration_hold_seconds: float = 0.02


class FakeDevice:
    """A phone whose screencap costs a known amount of wall-clock time."""

    def __init__(self, capture_seconds: float) -> None:
        self.capture_seconds = capture_seconds
        self.label = "fake-g"
        self.viewport = (720, 1600)
        self.config = FakeConfig()
        self.hold_ended: float | None = None
        self.held_seconds: float | None = None

    async def screenshot_raw(self) -> Image.Image:
        await asyncio.sleep(self.capture_seconds)
        return Image.new("RGB", (72, 160), (60, 90, 50))

    async def stationary_hold(self, _point, seconds: float):
        self.held_seconds = seconds
        await asyncio.sleep(seconds)
        self.hold_ended = time.monotonic()
        return 0.0, 0.0


class CalibrationSamplingTests(unittest.IsolatedAsyncioTestCase):
    """Frames only count while the ball is still down."""

    async def calibrate(self, device: FakeDevice):
        ball = excellent_throw_ios.BallDetection(1.0, 360, 1400, 60)
        seen: list = []

        def fake_measure(frames, _prior, _viewport):
            seen.extend(frames)
            return None

        with TemporaryDirectory() as tmp, patch.object(
            excellent_throw_ios, "measure_ring", fake_measure
        ):
            result = await throw.calibrate_target(device, ball, Path(tmp))
            saved = sorted(Path(tmp).glob("timed-hold-*.jpg"))
        return result, seen, saved

    async def test_frames_are_taken_while_the_ball_is_held(self) -> None:
        device = FakeDevice(capture_seconds=0.05)
        _result, seen, saved = await self.calibrate(device)
        self.assertTrue(seen, "no frame reached measure_ring")
        self.assertEqual(len(saved), len(seen))
        # The regression: every frame the old loop produced was taken after the
        # release, where the ring is no longer drawn.
        for frame in seen:
            self.assertLess(frame.timestamp, device.hold_ended)

    async def test_the_hold_is_stretched_to_fit_the_capture(self) -> None:
        device = FakeDevice(capture_seconds=0.05)
        await self.calibrate(device)
        # measure_capture_cost times the real capture, so the hold is derived
        # from what this phone just did rather than from a stored number.
        self.assertAlmostEqual(
            device.held_seconds,
            throw.hold_for_capture(device.capture_seconds, 0.02),
            delta=0.05,
        )
        self.assertGreater(device.held_seconds, device.capture_seconds * 2)

    async def test_a_hold_that_photographs_nothing_falls_back(self) -> None:
        """A phone slower than its own ceiling still answers, it does not hang."""
        device = FakeDevice(capture_seconds=0.05)
        device.config.calibration_hold_seconds = 0.001
        with patch.object(throw, "hold_for_capture", lambda *_a: 0.001):
            result, seen, _saved = await self.calibrate(device)
        self.assertIsNone(result)
        self.assertFalse(seen)


def held_disc(colour: tuple[int, int, int]) -> Image.Image:
    """A screen with a ball-sized disc of one colour at the throw spot."""
    image = Image.new("RGB", (720, 1600), (70, 110, 60))
    center_x, center_y, radius = 360, 1330, 110
    for y in range(center_y - radius, center_y + radius):
        for x in range(center_x - radius, center_x + radius):
            if (x - center_x) ** 2 + (y - center_y) ** 2 <= radius * radius:
                image.putpixel((x, y), colour)
    return image


def held_ball() -> Image.Image:
    """A held Great Ball: coloured on top, white below, as the game draws it.

    Two halves rather than one flat disc, because that is the whole of what
    tells a ball from a berry -- the pale lower hemisphere.  A flat disc stood
    in for a ball here for months and let a berry pass as one.
    """
    image = held_disc((40, 90, 210))
    center_x, center_y, radius = 360, 1330, 110
    for y in range(center_y, center_y + radius):
        for x in range(center_x - radius, center_x + radius):
            if (x - center_x) ** 2 + (y - center_y) ** 2 <= radius * radius:
                image.putpixel((x, y), (245, 245, 245))
    return image


def held_item(pink: bool) -> Image.Image:
    """A screen with a ball-sized disc at the throw spot, Nanab or ball."""
    return held_disc((236, 96, 158)) if pink else held_ball()


class BerryInHandTests(unittest.TestCase):
    """Telling a held Nanab from a held ball is what says the feed worked.

    Thresholded against the android-one's own artifacts: the pink fraction inside the
    held disc is 0.36 with a Nanab in hand and 0.000 with a Great Ball.
    """

    def test_a_held_nanab_is_seen(self) -> None:
        image = held_item(pink=True)
        self.assertTrue(throw.berry_in_hand(image, image.size))

    def test_a_held_ball_is_not_a_berry(self) -> None:
        image = held_item(pink=False)
        self.assertFalse(throw.berry_in_hand(image, image.size))

    def test_no_disc_at_all_is_not_a_berry(self) -> None:
        image = Image.new("RGB", (720, 1600), (70, 110, 60))
        self.assertFalse(throw.berry_in_hand(image, image.size))


class BallInHandTests(unittest.TestCase):
    """Naming the ball, not the Nanab, is what keeps a berry out of the throw.

    `berry_in_hand` knows one berry.  The game selects whatever berry is left
    once the Nanabs run out, and on 6 Sep 2026 that was a Golden Razz on all
    four phones: it is not pink, so it read as a ball, and every reward throw
    fed the Pokemon instead of catching it.  Thresholded off the fleet's own
    captures -- pale pixels are 0.36-0.39 of a held ball and under 0.10 of any
    held berry, and only the ball carries its white below the middle.
    """

    def test_a_held_ball_is_a_ball(self) -> None:
        image = held_ball()
        self.assertTrue(throw.ball_in_hand(image, image.size))

    def test_a_held_nanab_is_not_a_ball(self) -> None:
        image = held_disc((236, 96, 158))
        self.assertFalse(throw.ball_in_hand(image, image.size))

    def test_a_held_golden_razz_is_not_a_ball(self) -> None:
        image = held_disc((238, 145, 40))
        self.assertFalse(throw.ball_in_hand(image, image.size))

    def test_a_held_silver_pinap_is_not_a_ball(self) -> None:
        # The pale one, and the one closest to a ball: it is silver all over,
        # where a ball is white only underneath.
        image = held_disc((198, 202, 206))
        self.assertFalse(throw.ball_in_hand(image, image.size))

    def test_no_disc_at_all_is_not_a_ball(self) -> None:
        image = Image.new("RGB", (720, 1600), (70, 110, 60))
        self.assertFalse(throw.ball_in_hand(image, image.size))


class TallPhoneBallTests(unittest.TestCase):
    """A held ball on the tall_device is 0.225 of the analysis width, not 0.166.

    The radius cap is width-relative but the radius can come from the vertical
    extent, so the tall_device's ball measured 94 against a cap of 84 and vanished
    from the detector the moment it stopped being clipped by the bottom edge.
    The run read that as the encounter having ended and moved on, leaving a
    Magikarp on screen with 116 balls still in the bag.
    """

    @staticmethod
    def tall_device_frame() -> Image.Image:
        image = Image.new("RGB", (1224, 2992), (86, 122, 66))
        centre_x, centre_y, radius = 612, 2790, 250
        painter = ImageDraw.Draw(image)
        painter.ellipse(
            (centre_x - radius, centre_y - radius, centre_x + radius, centre_y + radius),
            fill=(46, 78, 190),
        )
        painter.rectangle(
            (centre_x - radius, centre_y, centre_x + radius, centre_y + radius),
            fill=(240, 240, 240),
        )
        return image

    def test_the_tall_devices_held_ball_is_found(self) -> None:
        image = self.tall_device_frame()
        view = throw.analysis_view(image.size)
        self.assertIsNotNone(
            excellent_throw_ios.locate_throw_ball(image, view.viewport),
            "a ball this size is still a ball on a tall phone",
        )

    def test_an_empty_screen_is_still_no_ball(self) -> None:
        image = Image.new("RGB", (1224, 2992), (86, 122, 66))
        view = throw.analysis_view(image.size)
        self.assertIsNone(
            excellent_throw_ios.locate_throw_ball(image, view.viewport)
        )


class EncounterButtonTests(unittest.TestCase):
    """The corner buttons are found, not assumed.

    Their x is the same everywhere -- 0.123 and 0.872 of the width -- but their
    y is not: 0.836 on the android-one, 0.853 on the moto, 0.914 on the tall_device.  The
    fixed 0.855 the berry picker used to tap landed on bare grass on the tall_device,
    which is why it could never open its picker and never fed a Nanab.
    """

    @staticmethod
    def encounter(size: tuple[int, int], button_y: float) -> Image.Image:
        width, height = size
        image = Image.new("RGB", size, (86, 122, 66))
        painter = ImageDraw.Draw(image)
        radius = round(width * 0.055)
        for fraction in (0.123, 0.872):
            centre_x, centre_y = round(width * fraction), round(height * button_y)
            painter.ellipse(
                (
                    centre_x - radius,
                    centre_y - radius,
                    centre_x + radius,
                    centre_y + radius,
                ),
                fill=(198, 198, 196),
            )
        return image

    def test_the_tall_devices_buttons_are_lower_than_the_compacts(self) -> None:
        tall_device = excellent_throw_ios.locate_encounter_buttons(
            self.encounter((1224, 2992), 0.914)
        )
        compact = excellent_throw_ios.locate_encounter_buttons(
            self.encounter((1316, 2560), 0.836)
        )
        self.assertIsNotNone(tall_device)
        self.assertIsNotNone(compact)
        berry, switch = tall_device
        self.assertAlmostEqual(berry[1] / 2992, 0.914, delta=0.01)
        self.assertAlmostEqual(switch[0] / 1224, 0.872, delta=0.01)
        self.assertGreater(
            berry[1] / 2992, compact[0][1] / 2560, "a fixed fraction cannot serve both"
        )

    def test_a_screen_with_no_buttons_is_not_an_encounter(self) -> None:
        self.assertIsNone(
            excellent_throw_ios.locate_encounter_buttons(
                Image.new("RGB", (1224, 2992), (86, 122, 66))
            )
        )


class BallChoiceTests(unittest.TestCase):
    """The Great Ball is placed against the chooser sheet, not the screen.

    The sheet is anchored to the bottom edge, so the same ball is at 0.747 of
    the android-one's height and 0.843 of the tall_device's -- but exactly 0.167 of the width
    below the sheet's top edge on both.
    """

    def test_the_tap_follows_the_sheet(self) -> None:
        config = throw.load_config(None, throw.parse_args([]))
        for size, sheet_top in (((1224, 2992), 2319), ((1316, 2560), 1692)):
            width, height = size
            image = Image.new("RGB", size, (40, 70, 40))
            ImageDraw.Draw(image).rectangle(
                (0, sheet_top, width, height), fill=(232, 232, 228)
            )
            device = throw.AndroidDevice(None, "fake", "fake", size, config, None)
            point = throw.ball_choice_point(image, device)
            self.assertAlmostEqual(point[0] / width, 0.498, delta=0.01)
            self.assertAlmostEqual(
                (point[1] - sheet_top) / width, 0.167, delta=0.02, msg=str(size)
            )


class GBLRewardThrowTests(unittest.TestCase):
    """The GBL reward catch aims at the ball it can see, not at a fraction.

    `gbl_android.throw_ball` used to swipe 0.81h -> 0.35h down the middle,
    which are the android-one's proportions.  On the tall_device the held ball sits at 0.913
    of the height, so that swipe began 310px below the ball -- outside its
    245px radius, on bare grass -- and no reward was ever caught on that phone.
    """

    @staticmethod
    def encounter(size: tuple[int, int], ball_y: float) -> Image.Image:
        width, height = size
        image = Image.new("RGB", size, (86, 122, 66))
        painter = ImageDraw.Draw(image)
        radius = round(width * 0.19)
        centre_x, centre_y = width // 2, round(height * ball_y)
        painter.ellipse(
            (
                centre_x - radius,
                centre_y - radius,
                centre_x + radius,
                centre_y + radius,
            ),
            fill=(46, 78, 190),
        )
        painter.rectangle(
            (centre_x - radius, centre_y, centre_x + radius, centre_y + radius),
            fill=(240, 240, 240),
        )
        return image

    def test_the_throw_starts_on_the_tall_devices_ball(self) -> None:
        size = (1224, 2992)
        image = self.encounter(size, 0.913)
        config = throw.load_config(None, throw.parse_args([]))
        device = throw.AndroidDevice(None, "tall_device", "tall_device", size, config, None)
        view = throw.analysis_view(size)
        found = excellent_throw_ios.locate_throw_ball(image, view.viewport)
        self.assertIsNotNone(found, "the tall_device's ball must be findable at all")
        ball = view.grow_ball(found)

        # 0.81h is the moto's ball, which the GBL path used to throw from on
        # every phone.
        blind_y = round(size[1] * 0.81)
        self.assertGreater(
            abs(blind_y - ball.center_y),
            ball.radius,
            "the old fixed fraction is meant to miss this ball; the test is stale",
        )

        target = throw.fallback_target(image, ball, device)
        start, _end = excellent_throw_ios.straight_throw_points(
            ball, target, size, throw.runtime_config(config)
        )
        self.assertLessEqual(
            abs(start[1] - ball.center_y),
            ball.radius,
            "the aimed throw must begin on the ball",
        )


class GBLRewardRetryTests(unittest.IsolatedAsyncioTestCase):
    """The reward catch retries the berry, not the wait."""

    async def attempts(self, error: Exception) -> tuple[bool, list[bool]]:
        from sources import gbl_android

        tried: list[bool] = []

        async def run_once(_thrower, **kwargs):
            tried.append(kwargs["use_nanab"])
            raise error

        device = SimpleNamespace(
            serial="android-one", label="android-one", config={}, display_id=None
        )
        with (
            patch.object(gbl_android, "frame_image", lambda _frame: None),
            patch.object(gbl_android, "excellent_thrower", lambda *_a: None),
            patch.object(throw, "run_once", run_once),
        ):
            return await gbl_android.throw_ball(device, object()), tried

    async def test_an_empty_pocket_still_gets_a_throw_without_the_berry(self) -> None:
        thrown, tried = await self.attempts(
            throw.AndroidExcellentThrowError("Nanab picker did not open")
        )
        self.assertFalse(thrown)
        self.assertEqual(tried, [True, False])

    async def test_nothing_to_throw_at_is_not_waited_for_twice(self) -> None:
        # 20 seconds of wait, then 20 more for a berry that was never the
        # problem, against a 30-second target for the whole catch.
        thrown, tried = await self.attempts(
            throw.NoEncounterError("No ready encounter appeared within 20 seconds")
        )
        self.assertFalse(thrown)
        self.assertEqual(tried, [True])


class FeedingDevice(FakeDevice):
    """Records the gestures a berry feed sends."""

    def __init__(self, frames: list[Image.Image]) -> None:
        super().__init__(capture_seconds=0.0)
        self.frames = frames
        self.throws: list[tuple[list[int], list[int], int]] = []
        self.taps: list[list[int]] = []
        self.curves: list[tuple[list[int], list]] = []
        self.shells: list[str] = []
        self.device = self

    async def screenshot_raw(self) -> Image.Image:
        return self.frames[min(len(self.taps + self.throws), len(self.frames) - 1)]

    async def straight_throw(self, start, end, duration_ms) -> None:
        self.throws.append((list(start), list(end), duration_ms))

    async def curve_throw(self, start, path, initial_pause=None) -> None:
        self.curves.append((list(start), list(path)))

    async def tap(self, point) -> None:
        self.taps.append(list(point))

    async def shell(self, command: str) -> str:
        self.shells.append(command)
        return ""


class NanabFeedTests(unittest.IsolatedAsyncioTestCase):
    """A berry is thrown at the Pokemon, never tapped onto it.

    Selecting a Nanab only puts it in the hand.  The shipped code tapped the
    Pokemon and moved on, so the berry stayed in the hand and the throw that
    followed threw the berry -- confirmed on the android-one, whose held frame shows
    the Nanab, not a ball.
    """

    def setUp(self) -> None:
        self.ball = excellent_throw_ios.BallDetection(1.0, 360, 1330, 110)
        self.encounter = held_item(pink=False)

    async def feed(self, frames: list[Image.Image]) -> FeedingDevice:
        device = FeedingDevice(frames)
        with TemporaryDirectory() as tmp, patch.object(
            throw, "capture_frame", self.capture(device)
        ), patch.object(
            throw, "fallback_target", lambda *_a: excellent_throw_ios.RingLock(360, 700, 90, 90, 1.0)
        ), patch("sources.berry_android.picker_read", lambda _f: [("nanab", [100, 1400])]),                 patch("asyncio.sleep", self.nosleep):
            await throw.use_nanab_berry(device, self.ball, self.encounter, Path(tmp))
        return device

    @staticmethod
    def capture(device):
        async def _capture(_device):
            return await device.screenshot_raw(), 0.0
        return _capture

    @staticmethod
    async def nosleep(_seconds) -> None:
        return None

    async def test_the_berry_is_flicked_at_the_pokemon(self) -> None:
        device = await self.feed([held_item(pink=True), held_item(pink=False)])
        self.assertEqual(len(device.throws), 1, "the berry was not thrown")
        start, end, _ms = device.throws[0]
        self.assertEqual(start, [self.ball.center_x, self.ball.center_y])
        self.assertLess(end[1], start[1], "the flick did not go up at the Pokemon")
        self.assertEqual(
            device.taps[0], [100, 1400], "the picker was not the first tap"
        )

    async def test_the_berry_is_only_flicked_once(self) -> None:
        """A fed berry is replaced in the hand, so a second flick feeds a second.

        The count went 39 -> 37 across two flicks the run reported as failures.
        """
        device = await self.feed([held_item(pink=True)])
        self.assertEqual(len(device.throws), 1, "a second berry was fed")

    async def test_the_ball_is_taken_back_after_the_feed(self) -> None:
        """Otherwise the throw throws the berry, which never catches anything."""
        device = await self.feed([held_item(pink=True), held_item(pink=False)])
        width, height = device.viewport
        self.assertIn(
            [
                round(width * throw.BALL_SWITCH_POINT[0]),
                round(height * throw.BALL_SWITCH_POINT[1]),
            ],
            device.taps,
            "the held-item chooser was never opened",
        )
        self.assertIn(
            [
                round(width * throw.BALL_CHOICE_POINT[0]),
                round(height * throw.BALL_CHOICE_POINT[1]),
            ],
            device.taps,
            "no ball was chosen",
        )

    async def test_a_berry_that_will_not_feed_is_never_backed_out_of(self) -> None:
        """BACK does not put the berry away, it runs from the encounter.

        The moto took that exit on 24 Aug and finished the attempt on the map
        screen, with the abandoned Pokemon counted as caught.
        """
        await self.feed([held_item(pink=True)])
        device = await self.feed([held_item(pink=True)])
        self.assertFalse(
            [command for command in device.shells if "keyevent" in command],
            f"an encounter key was pressed: {device.shells}",
        )


class EncounterLockTests(unittest.IsolatedAsyncioTestCase):
    """The lock wants two reads that agree, not two reads in a row."""

    def device(self) -> FakeDevice:
        return FakeDevice(0.0)

    async def lock(self, detections):
        """Runs the wait over a scripted sequence of per-frame detections."""
        frames = iter(detections)

        async def capture(_device):
            return Image.new("RGB", (72, 160), (60, 90, 50)), time.monotonic()

        with (
            patch.object(throw, "capture_frame", capture),
            patch.object(
                excellent_throw_ios,
                "locate_throw_ball",
                lambda _image, _viewport: next(frames, None),
            ),
            patch("asyncio.sleep", NanabFeedTests.nosleep),
            TemporaryDirectory() as artifacts,
        ):
            return await throw.detect_encounter_ball(
                self.device(), 5.0, Path(artifacts)
            )

    def ball(self, x: int, y: int):
        return excellent_throw_ios.BallDetection(1.0, x, y, 40)

    async def test_a_disagreeing_frame_does_not_undo_the_read_before_it(self) -> None:
        # The android-one's reward catch on 2026-09-09: a flat 1.00 on the ball, and a
        # refusal, because the detector kept flipping to the Pokemon's head and
        # every flip threw the good read away.
        head = self.ball(120, 300)
        ready = await self.lock([self.ball(180, 1000), head, self.ball(182, 1004)])
        self.assertAlmostEqual(ready.score, 1.0)

    async def test_a_blank_frame_does_not_undo_it_either(self) -> None:
        ready = await self.lock([self.ball(180, 1000), None, self.ball(182, 1004)])
        self.assertAlmostEqual(ready.score, 1.0)

    async def test_two_different_balls_are_still_not_an_encounter(self) -> None:
        # Agreement is what is wanted, not simply two detections: a run of
        # unrelated blobs must still time out rather than throw at one.
        with self.assertRaises(throw.NoEncounterError):
            await self.lock([self.ball(100 + 90 * n, 200 + 300 * n) for n in range(6)])


class ThrowGestureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from sources import gbl_vision
        patcher = patch.object(gbl_vision, "recognize", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    """The ball only leaves the hand if the gesture is one fast swipe.

    The curve path sends a dozen separate `input` invocations, and the android-one
    charges about 0.7s for each, so the game receives a seven-second drag and
    sets the ball down instead of throwing it: 683 Great Balls before three
    "throws" and 683 after.  One `input swipe` of the same length threw first
    time.
    """

    async def run_once(self, frame: Image.Image, *, use_nanab: bool):
        device = FeedingDevice([frame, held_item(pink=False)])
        device.config = throw.load_config(None, throw.parse_args([]))
        ball = excellent_throw_ios.BallDetection(1.0, 360, 1330, 110)
        lock = excellent_throw_ios.RingLock(360, 700, 90, 90, 1.0)
        with TemporaryDirectory() as tmp, patch.object(
            throw, "capture_frame", NanabFeedTests.capture(device)
        ), patch.object(
            throw, "detect_encounter_ball", AsyncMock(return_value=ball)
        ), patch.object(
            throw, "wait_for_throw_result", AsyncMock(return_value=True)
        ), patch.object(
            throw, "fallback_target", lambda *_a: lock
        ), patch("sources.berry_android.picker_read", lambda _f: [("nanab", [100, 1400])]), \
                patch("asyncio.sleep", NanabFeedTests.nosleep):
            await throw.run_once(
                device,
                wait_seconds=1.0,
                artifact_dir=Path(tmp),
                dry_run=False,
                use_nanab=use_nanab,
            )
        return device

    async def test_the_throw_is_one_swipe_and_never_a_curve(self) -> None:
        device = await self.run_once(held_item(pink=False), use_nanab=False)
        self.assertEqual(len(device.throws), 1, "expected exactly one thrown ball")
        self.assertFalse(device.curves, "the throw went out as a multi-part drag")
        _start, _end, duration = device.throws[0]
        self.assertLessEqual(duration, 300, "a slow drag is not a throw")

    async def test_the_ring_hold_is_off_unless_it_is_asked_for(self) -> None:
        """It costs 16s of a 30s catch and cannot buy an Excellent from a swipe."""
        calibrate = AsyncMock(return_value=None)
        with patch.object(throw, "calibrate_target", calibrate):
            await self.run_once(held_item(pink=False), use_nanab=False)
        calibrate.assert_not_awaited()

    async def test_a_leftover_berry_is_fed_before_the_ball_is_thrown(self) -> None:
        """Otherwise the throw throws the berry, which is what the android-one did."""
        device = await self.run_once(held_item(pink=True), use_nanab=False)
        self.assertEqual(len(device.throws), 2, "the berry was not flicked first")
        self.assertTrue(device.taps, "the ball was never taken back")
        self.assertNotIn(
            [100, 1400], device.taps, "the picker is not needed for a held berry"
        )


if __name__ == "__main__":
    unittest.main()


class CatchScreenTests(unittest.IsolatedAsyncioTestCase):
    """A catch leaves two screens up, and the next encounter is behind them.

    The tall_device proved it the hard way: a second run against a phone left on the
    XP card spent all 25 of its attempts reporting "No ready encounter
    appeared" until the card and the caught Pokemon's page were dismissed by
    hand.
    """

    @staticmethod
    def device():
        config = throw.load_config(None, throw.parse_args([]))
        device = throw.AndroidDevice(None, "tall_device", "tall_device", (1224, 2992), config, None)
        device.tap = AsyncMock()
        device.screenshot_raw = AsyncMock(
            return_value=Image.new("RGB", (1224, 2992), (20, 20, 20))
        )
        return device

    @staticmethod
    def boxes(*texts: str):
        from sources import gbl_vision

        return [gbl_vision.OCRBox(text, 1.0, 569, 1796, 91, 48) for text in texts]

    async def test_the_xp_card_and_the_pokemon_page_are_both_dismissed(self) -> None:
        device = self.device()
        screens = [
            self.boxes("POKEMON CAUGHT", "OK"),
            self.boxes("Paras", "POWER UP"),
            self.boxes("MewTwoFTWAlIDay"),
        ]
        with (
            patch.object(throw, "MENU_SETTLE", 0.0),
            patch.object(
                throw, "handset_close_button", new=AsyncMock(return_value=[612, 2830])
            ),
            patch("sources.gbl_vision.recognize", side_effect=screens),
        ):
            with TemporaryDirectory() as tmp:
                await throw.clear_catch_screens(device, Path(tmp))
                saved = Path(tmp) / "catch-summary.jpg"
                self.assertTrue(saved.exists())
        taps = [call.args[0] for call in device.tap.await_args_list]
        self.assertEqual(len(taps), 2)
        # The X is the phone's own, not a fraction: 2830 of 2992 here against
        # the moto's 1421 of 1600.
        self.assertEqual(taps[1], [612, 2830])

    async def test_a_phone_already_on_the_map_is_left_alone(self) -> None:
        device = self.device()
        with (
            patch.object(throw, "MENU_SETTLE", 0.0),
            patch(
                "sources.gbl_vision.recognize",
                return_value=self.boxes("MewTwoFTWAlIDay"),
            ),
        ):
            with TemporaryDirectory() as tmp:
                await throw.clear_catch_screens(device, Path(tmp))
        device.tap.assert_not_awaited()
        # One read, not four: a phone on the map is the ordinary case.
        self.assertEqual(device.screenshot_raw.await_count, 1)


class ThrowGeometryTests(unittest.TestCase):
    """How hard the ball is thrown, and what a reading has to look like.

    Reported live on 2026-09-02: the tall_device was missing and wasting balls and the
    android-one was falling short of a Regirock, and the operator caught both by hand.
    """

    def ball(self, center_x, center_y, radius=180):
        return excellent_throw_ios.BallDetection(0.9, center_x, center_y, radius)

    def ring(self, center_x, center_y):
        return excellent_throw_ios.RingLock(center_x, center_y, 400, 200, 0.9)

    def test_the_blind_throw_is_the_one_that_caught_by_hand(self) -> None:
        """moto at 720x1600: 360,1300 -> 360,560, sent by hand, caught."""
        start, end = throw.proven_throw_points(
            self.ball(360, 1300), (720, 1600)
        )
        self.assertEqual(start, [360, 1300])
        self.assertEqual(end, [360, 564])

    def test_the_blind_throw_keeps_its_length_on_a_taller_phone(self) -> None:
        """A shared endpoint is a different throw on every handset: the tall_device's
        ball rests at 0.913h against the moto's 0.8125h, so ending both at
        0.35h threw the tall_device 22% harder than the swipe that worked."""
        _start, moto = throw.proven_throw_points(self.ball(360, 1300), (720, 1600))
        _start, tall_device = throw.proven_throw_points(self.ball(612, 2732), (1224, 2992))
        self.assertAlmostEqual(
            (1300 - moto[1]) / 1600, (2732 - tall_device[1]) / 2992, places=2
        )
        self.assertGreater(
            tall_device[1], round(2992 * 0.35),
            "the tall_device must stop throwing the moto's endpoint",
        )

    def test_a_circle_up_in_the_sky_is_not_a_reading(self) -> None:
        """The tall_device's first hold came back with (688,283) at 1224x2992."""
        self.assertFalse(throw.ring_is_readable(self.ring(688, 283), (1224, 2992)))
        self.assertFalse(throw.ring_is_readable(self.ring(60, 1497), (1224, 2992)))
        self.assertTrue(throw.ring_is_readable(self.ring(688, 1497), (1224, 2992)))

    def test_a_read_ring_is_aimed_at_rather_than_the_blind_length(self) -> None:
        """A Pokemon standing further back draws its circle higher, and the
        throw has to reach it -- that is the Regirock case."""
        source = Path(throw.__file__).read_text()
        run_once = source[source.index("async def run_once("):]
        self.assertIn("if target is None:\n        start, end = proven_throw_points(", run_once)
        self.assertIn("excellent_throw_ios.straight_throw_points(", run_once)

    def test_the_shadow_is_not_aimed_at_when_the_ring_is_unread(self) -> None:
        """`fallback_target` degrades to the most ring-shaped blob around, and
        live that was the Pokemon's shadow, which pulls the flick short."""
        source = Path(throw.__file__).read_text()
        run_once = source[source.index("async def run_once("):]
        throw_block = run_once[: run_once.index("straight_throw(")]
        self.assertNotIn("fallback_target(image, ball, device)", throw_block)
