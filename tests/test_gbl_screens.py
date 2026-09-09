from __future__ import annotations

from tests import support as _test_support

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from sources import gbl_android as gbl
from sources import gbl_ios, gbl_strategy, gbl_vision


def frame(width: int = 720, height: int = 1600, background=(60, 90, 50)):
    """A blank frame in the raw layout `gbl` reads from `screencap`."""
    return [width, height, 0, bytearray(bytes(background) + b'\xff') * (width * height)]


def put(f, x0f: float, x1f: float, y0f: float, y1f: float, rgb):
    """Paint a rectangle, given as fractions of the screen."""
    width, height, _offset, data = f
    for y in range(int(height * y0f), int(height * y1f)):
        for x in range(int(width * x0f), int(width * x1f)):
            i = (y * width + x) * 4
            data[i:i + 3] = bytes(rgb)


def as_frame(f):
    return (f[0], f[1], f[2], bytes(f[3]))


def charged_disc(f, cx_fraction, cy_fraction=None, fill=(114, 168, 175),
                 rim=True, filled_from=-1.0):
    """Draw a charged-move button the way the game draws one.

    The disc is a circle of `gbl.CHARGED_DISC_RADIUS` screen widths with a white
    rim, and the rim is only painted when the move is ready.  `filled_from` is
    where the opaque energy fill starts, as a fraction of the radius measured
    from the centre, so -1.0 is a full button and +0.5 is a quarter-full one:
    above the line the battlefield shows through.
    """
    width, height, _offset, data = f
    if cy_fraction is None:
        cy_fraction = gbl.charged_disc_centre_y(as_frame(f))
    radius = width * gbl.CHARGED_DISC_RADIUS
    cx, cy = width * cx_fraction, height * cy_fraction
    rim_lo = min(gbl.CHARGED_RIM_FRACTIONS) - 0.03
    rim_hi = max(gbl.CHARGED_RIM_FRACTIONS) + 0.03
    for y in range(int(cy - radius) - 1, int(cy + radius) + 2):
        for x in range(int(cx - radius) - 1, int(cx + radius) + 2):
            if not (0 <= x < width and 0 <= y < height):
                continue
            away = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / radius
            if rim and rim_lo <= away <= rim_hi:
                rgb = (255, 255, 255)
            elif away < rim_lo and (y - cy) / radius >= filled_from:
                rgb = fill
            else:
                continue
            i = (y * width + x) * 4
            data[i:i + 3] = bytes(rgb)


# Sampled off real screens; see the constants in gbl.py.
MINT = (162, 218, 148)      # the light end of a pill
TEAL = (36, 203, 168)       # the dark end of the same pill
ORANGE = (254, 170, 74)     # CLAIM RANK REWARDS!
WHITE = (255, 255, 255)
PINK = (231, 128, 179)          # Basic/Premium reward-tier ribbon
GREY_PILL = (222, 246, 231)  # the pill once it is disabled


def pill(f, y0f=0.78, y1f=0.82, x0f=0.30, x1f=0.70, label=True):
    """A green pill, drawn as the two ends of its gradient with a white label."""
    put(f, x0f, (x0f + x1f) / 2, y0f, y1f, MINT)
    put(f, (x0f + x1f) / 2, x1f, y0f, y1f, TEAL)
    if label:
        put(f, 0.45, 0.55, y0f, y1f, WHITE)


class PillTests(unittest.TestCase):
    def test_finds_a_pill(self) -> None:
        f = frame()
        pill(f)
        self.assertIsNotNone(gbl.find_pill(as_frame(f)))

    def test_label_across_the_pill_does_not_hide_it(self) -> None:
        """The white label covers ~30% of the row it crosses."""
        f = frame()
        pill(f, x0f=0.30, x1f=0.70)
        put(f, 0.40, 0.60, 0.79, 0.81, WHITE)
        self.assertIsNotNone(gbl.find_pill(as_frame(f)))

    def test_ignores_grass(self) -> None:
        """The battlefield is green but not bright enough to be a button."""
        f = frame(background=(110, 180, 90))
        self.assertIsNone(gbl.find_pill(as_frame(f)))

    def test_ignores_flat_teal_map_band(self) -> None:
        """Map scenery can pass the colour/shape tests but has no pill gradient."""
        f = frame()
        put(f, 0.10, 0.90, 0.70, 0.75, (108, 201, 148))
        self.assertIsNone(gbl.find_pill(as_frame(f)))

    def test_ignores_a_narrow_bar(self) -> None:
        """An HP bar reaches 0.29 of the width; a pill reaches 0.40."""
        f = frame()
        put(f, 0.48, 0.76, 0.78, 0.80, TEAL)
        self.assertIsNone(gbl.find_pill(as_frame(f)))

    def test_ignores_a_disabled_pill(self) -> None:
        """Greyed out at the end of a set, it is there but does nothing."""
        f = frame()
        put(f, 0.30, 0.70, 0.78, 0.82, GREY_PILL)
        self.assertIsNone(gbl.find_pill(as_frame(f)))

    def test_reward_state_chooses_free_tier_when_premium_is_also_visible(self) -> None:
        """The upper reward-tier button is free; the lower one spends a pass."""
        f = frame()
        put(f, 0.06, 0.94, 0.28, 0.90, WHITE)
        pill(f, y0f=0.14, y1f=0.18)
        pill(f, y0f=0.62, y1f=0.66)
        point = gbl.read_screen_state(as_frame(f))[1]
        self.assertIsNotNone(point)
        self.assertLess(point[1], f[1] * 0.25)

    def test_compact_pink_banner_keeps_basic_scan_above_normal_band(self) -> None:
        """A clipped android-one panel must not make the lower Premium pill win."""
        f = frame()
        put(f, 0.28, 0.72, 0.30, 0.34, PINK)
        pill(f, y0f=0.48, y1f=0.52)  # Basic, above normal pill scan.
        pill(f, y0f=0.72, y1f=0.76)  # Premium, inside normal pill scan.
        state, point = gbl.read_screen_state(as_frame(f))
        self.assertEqual(state, 'pill')
        self.assertIsNotNone(point)
        self.assertLess(point[1], f[1] * 0.60)

    def test_compact_two_pills_choose_basic_when_ribbon_is_clipped(self) -> None:
        """Real rank-up layout clips the ribbon but shows both tier pills."""
        f = frame()
        pill(f, y0f=0.27, y1f=0.31)  # Basic, above normal pill scan.
        pill(f, y0f=0.62, y1f=0.66)  # Premium, inside normal pill scan.
        state, point = gbl.read_screen_state(as_frame(f))
        self.assertEqual(state, 'pill')
        self.assertIsNotNone(point)
        self.assertLess(point[1], f[1] * 0.40)


class RewardChooserVisionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def chooser_frame():
        f = frame()
        put(f, 0.28, 0.72, 0.27, 0.31, PINK)
        pill(f, y0f=0.48, y1f=0.52)
        pill(f, y0f=0.82, y1f=0.86)
        return f

    @staticmethod
    def chooser_boxes(height: int):
        return [
            gbl_vision.OCRBox("BATTLE", 1.0, 250, int(height * 0.49), 220, 40),
            gbl_vision.OCRBox("BATTLE", 1.0, 250, int(height * 0.83), 220, 40),
        ]

    async def test_android_ocr_cannot_replace_basic_with_premium(self) -> None:
        f = self.chooser_frame()
        with patch(
                "sources.gbl_android.gbl_vision.recognize",
                return_value=self.chooser_boxes(f[1])):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object())
        self.assertEqual(state, "pill")
        self.assertIsNotNone(point)
        self.assertLess(point[1], f[1] * 0.60)

    async def test_ios_ocr_cannot_replace_basic_with_premium(self) -> None:
        f = self.chooser_frame()
        image = gbl.frame_image(as_frame(f))
        with patch(
                "sources.gbl_ios.gbl_vision.recognize",
                return_value=self.chooser_boxes(f[1])):
            state, point, _label, _boxes = await gbl_ios.smart_screen_state(
                image, object())
        self.assertEqual(state, "pill")
        self.assertIsNotNone(point)
        self.assertLess(point[1], f[1] * 0.60)


class SpentSetTests(unittest.IsolatedAsyncioTestCase):
    """The card a finished set leaves behind: rewards owed, BATTLE greyed out.

    Neither colour test names this screen -- the reward tiles are too small for
    the orange scan to land in and the spent pill is grey -- while OCR still
    reads BATTLE off that dead pill. A live run tapped it until the stall guard
    stopped it, ending the day with the rewards unclaimed and the next set
    behind them.
    """

    @staticmethod
    def spent_frame():
        f = frame()
        put(f, 0.06, 0.94, 0.44, 0.94, WHITE)          # the GBL card
        put(f, 0.28, 0.72, 0.62, 0.66, PINK)           # Basic Rewards ribbon
        pill(f, y0f=0.81, y1f=0.85, label=False)
        put(f, 0.28, 0.72, 0.81, 0.85, GREY_PILL)      # BATTLE, spent
        return f

    @staticmethod
    def spent_boxes(height: int):
        return [
            gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, int(height * 0.51), 246, 21),
            gbl_vision.OCRBox("COLLECT", 1.0, 225, int(height * 0.773), 105, 21),
            gbl_vision.OCRBox("COLLECT", 1.0, 404, int(height * 0.773), 107, 23),
            gbl_vision.OCRBox("BATTLE", 1.0, 304, int(height * 0.822), 114, 26),
            gbl_vision.OCRBox("5/5 battles played", 1.0, 241, int(height * 0.872), 81, 24),
        ]

    async def test_the_owed_reward_outranks_the_spent_pill(self) -> None:
        f = self.spent_frame()
        boxes = self.spent_boxes(f[1])
        with patch("sources.gbl_android.gbl_vision.recognize", return_value=boxes):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "orange")
        # The left-hand tile, and the tile rather than the word under it.
        self.assertEqual(point[0], boxes[1].center_x)
        self.assertLess(point[1], boxes[1].center_y)

    async def test_the_claim_band_is_pressed_on_the_band(self) -> None:
        # CLAIM RANK REWARDS! is a band the width of the card with its label
        # written across it. Lifting the tap off the label, which is right for
        # the small tiles, lands on bare card -- a live run pressed nothing
        # eight times and stopped with the whole day still to play.
        f = frame()
        put(f, 0.06, 0.94, 0.44, 0.94, WHITE)
        put(f, 0.09, 0.91, 0.80, 0.85, ORANGE)
        boxes = [
            gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, int(f[1] * 0.51), 246, 21),
            gbl_vision.OCRBox("CLAIM RANK REWARDS!", 1.0, 130, int(f[1] * 0.816), 460, 26),
        ]
        with patch("sources.gbl_android.gbl_vision.recognize", return_value=boxes):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "orange")
        self.assertTrue(0.09 * f[0] <= point[0] <= 0.91 * f[0])
        self.assertTrue(0.80 * f[1] <= point[1] <= 0.85 * f[1])

    async def test_a_card_with_nothing_owed_still_offers_battle(self) -> None:
        f = self.spent_frame()
        boxes = [box for box in self.spent_boxes(f[1])
                 if gbl_vision.normalize(box.text) != "collect"]
        with patch("sources.gbl_android.gbl_vision.recognize", return_value=boxes):
            state, _point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "pill")


class RewardEncounterTests(unittest.IsolatedAsyncioTestCase):
    """The catch a finished set pays out, on the iPhone transport.

    The moto flicks from a fixed height because its ball is always drawn in the
    same place.  Here the ball is found on each frame: across four traced SE
    frames it sat between 0.79h and 0.92h, so the moto's 0.81h would have
    started the flick above the ball as often as on it.
    """

    @staticmethod
    def device():
        # A real device object with no driver behind it: the point conversions
        # are the thing under test in half of these, so they have to be the
        # module's own rather than a stand-in that agrees with the assertions.
        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "test-ios"
        device.viewport = {"width": 375, "height": 667}
        device.config = gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"name": "ios-two"},
            coordinates={"GBL_MOVE_BTN": [142, 391], "CLOSE_BTN": [187, 630]},
            delay_modifier=0,
            battles=5,
        )
        device.trace_path = AsyncMock()
        device.tap = AsyncMock()
        return device

    @staticmethod
    def no_berry():
        """These exercise the flick, and the held-item check needs a real frame."""
        return patch.object(
            gbl_ios.excellent_throw_ios, "ball_in_hand", return_value=True
        )

    @staticmethod
    def ball(center_y: int):
        return gbl_ios.excellent_throw_ios.BallDetection(1.0, 187, center_y, 58)

    async def test_the_flick_starts_on_the_ball_it_found(self) -> None:
        device = self.device()
        with (
            self.no_berry(),
            patch.object(
                gbl_ios.excellent_throw_ios, "locate_throw_ball",
                return_value=self.ball(606),
            ),
        ):
            thrown = await gbl_ios.throw_ball(device, object())
        self.assertTrue(thrown)
        path = device.trace_path.await_args.args[0]
        self.assertEqual(path[0], [187, 606])
        # The moto's flick length, kept: the throw's power is its speed.
        self.assertEqual(path[-1], [187, 606 - int(667 * gbl_ios.ENCOUNTER_THROW_RISE)])

    async def test_the_flick_is_stepped_not_one_drag(self) -> None:
        # A single dragFromToForDuration is read as a tap by Pokemon GO -- the
        # trade lobby gets opened that way -- so a throw has to be a path.
        device = self.device()
        with (
            self.no_berry(),
            patch.object(
                gbl_ios.excellent_throw_ios, "locate_throw_ball",
                return_value=self.ball(529),
            ),
        ):
            await gbl_ios.throw_ball(device, object())
        path = device.trace_path.await_args.args[0]
        self.assertEqual(len(path), gbl_ios.ENCOUNTER_THROW_STEPS + 1)
        rises = [after[1] - before[1] for before, after in zip(path, path[1:])]
        self.assertTrue(all(rise < 0 for rise in rises))
        self.assertGreaterEqual(path[-1][1], int(667 * gbl_ios.ENCOUNTER_THROW_CEILING))

    async def test_a_berry_in_hand_is_swapped_back_not_thrown(self) -> None:
        # `locate_throw_ball` scores a held berry at 0.99, so the flick would
        # throw it, and a thrown berry catches nothing.  Feeding does not give
        # the ball back on its own -- it has to be asked for.  Asked as "is a
        # ball in hand": the Nanab test alone was blind to the Golden Razz the
        # game selects once the Nanabs run out.
        device = self.device()
        with (
            patch.object(
                gbl_ios.excellent_throw_ios, "ball_in_hand", return_value=False
            ),
            patch.object(gbl_ios, "swap_to_ball", new=AsyncMock()) as swap,
            patch.object(
                gbl_ios.excellent_throw_ios, "locate_throw_ball",
                return_value=self.ball(606),
            ),
        ):
            thrown = await gbl_ios.throw_ball(device, object())
        self.assertFalse(thrown)
        device.trace_path.assert_not_awaited()
        swap.assert_awaited_once()

    async def test_no_ball_on_this_frame_throws_nothing(self) -> None:
        device = self.device()
        with patch.object(
            gbl_ios.excellent_throw_ios, "locate_throw_ball", return_value=None
        ):
            thrown = await gbl_ios.throw_ball(device, object())
        self.assertFalse(thrown)
        device.trace_path.assert_not_awaited()

    async def test_the_plate_is_thrown_at_and_the_card_ends_it(self) -> None:
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        device.screenshot = AsyncMock(return_value=image)
        catch = [gbl_vision.OCRBox("CP 1219", 1.0, 300, int(1334 * 0.30), 120, 30)]
        card = [gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, 680, 246, 21)]
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", side_effect=[catch, card]),
            patch.object(gbl_ios, "wait", new=AsyncMock()),
            patch.object(
                gbl_ios.excellent_throw_ios, "locate_throw_ball",
                return_value=self.ball(606),
            ),
            self.no_berry(),
        ):
            done = await gbl_ios.take_reward_encounter(device)
        self.assertTrue(done)
        device.trace_path.assert_awaited_once()

    async def test_a_mon_that_will_not_stay_caught_is_left(self) -> None:
        # Four Great Balls, not a morning: whatever is left over goes to the
        # ordinary recovery, which is where the run was going anyway.
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        device.screenshot = AsyncMock(return_value=image)
        catch = [gbl_vision.OCRBox("CP 1219", 1.0, 300, int(1334 * 0.30), 120, 30)]
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=catch),
            patch.object(gbl_ios, "wait", new=AsyncMock()),
            patch.object(
                gbl_ios.excellent_throw_ios, "locate_throw_ball",
                return_value=self.ball(606),
            ),
            self.no_berry(),
        ):
            done = await gbl_ios.take_reward_encounter(device)
        self.assertFalse(done)
        self.assertEqual(device.trace_path.await_count, gbl_ios.ENCOUNTER_THROWS)

    async def test_the_caught_page_is_closed_on_the_configured_x(self) -> None:
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        device.screenshot = AsyncMock(return_value=image)
        caught = [gbl_vision.OCRBox("POWER UP", 1.0, 300, 1100, 160, 30)]
        card = [gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, 680, 246, 21)]
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", side_effect=[caught, card]),
            patch.object(gbl_ios, "wait", new=AsyncMock()),
        ):
            done = await gbl_ios.take_reward_encounter(device)
        self.assertTrue(done)
        device.tap.assert_awaited_once_with([187, 630])

    async def test_a_phone_parked_on_a_catch_is_cleared_before_starting(self) -> None:
        # The startup check calls every catch screen 'battle' and refuses to
        # start on it, so a run that ended on one used to lock the phone out of
        # the rest of the day.  It is cleared first instead.
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        catch = [gbl_vision.OCRBox("CP 1219", 1.0, 300, int(1334 * 0.30), 120, 30)]
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=catch),
            patch.object(gbl_ios, "take_reward_encounter", new=AsyncMock()) as taken,
        ):
            cleared = await gbl_ios.start_on_encounter(device, image)
        self.assertTrue(cleared)
        taken.assert_awaited_once_with(device)

    async def test_an_ordinary_unknown_screen_is_still_refused(self) -> None:
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=[]),
            patch.object(gbl_ios, "take_reward_encounter", new=AsyncMock()) as taken,
        ):
            cleared = await gbl_ios.start_on_encounter(device, image)
        self.assertFalse(cleared)
        taken.assert_not_awaited()


class PreinstalledWDAFallbackTests(unittest.TestCase):
    """A phone asking for the preinstalled runner still connects where it cannot.

    The worker host must ask for it -- its rebuild cannot codesign over SSH -- and a Mac
    used directly cannot honour it, having no RemoteXPC to launch the installed
    runner with.  Both read these same config files, so the capability is asked
    for first and dropped on failure.
    """

    def setUp(self):
        # Session retries clean up the failed device runner; keep the synthetic
        # UDID at that boundary rather than invoking device process tools.
        cleanup = patch.object(gbl_ios.ios_wda_cleanup, "stop_wda_runner")
        self.addCleanup(cleanup.stop)
        self.stop_wda = cleanup.start()
        pause = patch.object(gbl_ios.time, "sleep")
        self.addCleanup(pause.stop)
        pause.start()

    def config(self):
        return gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"udid": "UDID-1", "name": "Pro Max"},
            coordinates={},
            delay_modifier=0,
            battles=5,
        )

    def webdriver(self, failures):
        """A stand-in whose Remote fails for the first ``failures`` calls."""
        calls = []

        def remote(command_executor, options):
            calls.append(dict(options.capabilities))
            if len(calls) <= failures:
                raise RuntimeError("Failed to start the preinstalled WebDriverAgent")
            return f"driver-{len(calls)}"

        return type("FakeWebdriver", (), {"Remote": staticmethod(remote)})(), calls

    def options(self):
        class FakeOptions:
            def __init__(self):
                self.capabilities = {}

            def load_capabilities(self, capabilities):
                self.capabilities = dict(capabilities)
                return self

        return FakeOptions

    def test_the_preinstalled_runner_is_asked_for_first(self):
        webdriver, calls = self.webdriver(failures=0)
        driver = gbl_ios.IOSGBLDevice._start_session(
            webdriver, self.options(), self.config(),
            {"appium:usePreinstalledWDA": True},
        )
        self.assertEqual(driver, "driver-1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["appium:usePreinstalledWDA"])
        self.stop_wda.assert_not_called()

    def test_a_refused_preinstalled_runner_is_retried_without_it(self):
        webdriver, calls = self.webdriver(failures=1)
        driver = gbl_ios.IOSGBLDevice._start_session(
            webdriver, self.options(), self.config(),
            {"appium:usePreinstalledWDA": True, "appium:udid": "UDID-1"},
        )
        self.assertEqual(driver, "driver-2")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("appium:usePreinstalledWDA", calls[1])
        self.assertEqual(calls[1]["appium:udid"], "UDID-1")
        self.stop_wda.assert_called_once_with("UDID-1")

    def test_a_phone_that_never_asked_for_it_is_not_retried(self):
        webdriver, calls = self.webdriver(failures=1)
        with self.assertRaises(RuntimeError):
            gbl_ios.IOSGBLDevice._start_session(
                webdriver, self.options(), self.config(), {"appium:udid": "UDID-1"},
            )
        self.assertEqual(len(calls), 1)
        self.stop_wda.assert_not_called()

    def test_a_phone_that_fails_twice_still_fails(self):
        webdriver, calls = self.webdriver(failures=2)
        with self.assertRaises(RuntimeError):
            gbl_ios.IOSGBLDevice._start_session(
                webdriver, self.options(), self.config(),
                {"appium:usePreinstalledWDA": True},
            )
        self.assertEqual(len(calls), 2)
        self.stop_wda.assert_called_once_with("UDID-1")


class ExcellentRewardThrowTests(unittest.IsolatedAsyncioTestCase):
    """The reward catch is thrown by excellent_throw.py's routine, not the flick.

    The flick ends an encounter but catches almost nothing, so a set that paid
    out in a Pokemon used to spend four Great Balls and usually lose it anyway.
    The excellent routine locks the shrinking circle before it releases, and it
    is reached here over the run's own WebDriverAgent session -- shelling out to
    the script would be refused, since this run already holds the phone's lock.
    """

    @staticmethod
    def device(udid: str | None = "test_device_2"):
        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "test-ios"
        device.viewport = {"width": 375, "height": 667}
        device.config = gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"name": "ios-one"} if udid is None else {"name": "ios-one", "udid": udid},
            coordinates={"GBL_MOVE_BTN": [142, 391], "CLOSE_BTN": [187, 630]},
            delay_modifier=0,
            battles=5,
        )
        device.trace_path = AsyncMock()
        device.tap = AsyncMock()
        return device

    @staticmethod
    def no_berry():
        """These exercise the flick, and the held-item check needs a real frame."""
        return patch.object(
            gbl_ios.excellent_throw_ios, "ball_in_hand", return_value=True
        )

    def test_the_profile_is_this_phones_not_whatever_is_plugged_in(self) -> None:
        # Both iPhones run at once, and each one's curve offsets are the other's
        # sign-flipped, so selecting by what is attached would throw the SE's
        # arc on the Pro Max.  The session's own UDID picks the profile.
        se = Path("excellent-throw-secondary-ios.yaml")
        pro_max = Path("excellent-throw-main-ios.yaml")
        udids = {
            se: "test_device_1",
            pro_max: "test_device_2",
        }
        with (
            patch.object(gbl_ios.excellent_throw_ios, "known_configs", return_value=[se, pro_max]),
            patch.object(gbl_ios.excellent_throw_ios, "config_udid", side_effect=udids.get),
        ):
            chosen = gbl_ios.throw_profile_path(self.device())
        self.assertEqual(chosen, pro_max)

    def test_the_named_profile_beats_the_generic_one(self) -> None:
        # The SE resolves from two files.  select_configs' own tie-break.
        generic = Path("excellent-throw-ios.yaml")
        named = Path("excellent-throw-secondary-ios.yaml")
        with (
            patch.object(
                gbl_ios.excellent_throw_ios, "known_configs", return_value=[generic, named]
            ),
            patch.object(
                gbl_ios.excellent_throw_ios,
                "config_udid",
                return_value="test_device_1",
            ),
        ):
            chosen = gbl_ios.throw_profile_path(self.device("test_device_1"))
        self.assertEqual(chosen, named)

    def test_a_phone_with_no_profile_is_not_a_failure(self) -> None:
        self.assertIsNone(gbl_ios.throw_profile_path(self.device(None)))

    async def test_the_reward_mon_is_thrown_at_by_the_excellent_routine(self) -> None:
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        device.screenshot = AsyncMock(return_value=image)
        catch = [gbl_vision.OCRBox("CP 1219", 1.0, 300, int(1334 * 0.30), 120, 30)]
        card = [gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, 680, 246, 21)]
        thrower = SimpleNamespace(throw=AsyncMock(return_value=True), close=MagicMock())
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", side_effect=[catch, card]),
            patch.object(gbl_ios, "wait", new=AsyncMock()),
            patch.object(gbl_ios, "open_excellent_thrower", return_value=thrower),
        ):
            done = await gbl_ios.take_reward_encounter(device)
        self.assertTrue(done)
        thrower.throw.assert_awaited_once()
        # The flick is what the excellent routine replaces, not something it
        # sends as well: two balls would be spent on one Pokemon.
        device.trace_path.assert_not_awaited()
        thrower.close.assert_called_once()

    async def test_a_refused_lock_still_ends_the_encounter(self) -> None:
        # An encounter that never ends strands every remaining set behind it, so
        # a circle that will not lock falls back to the flick rather than
        # leaving the phone standing on the catch screen.
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        device.screenshot = AsyncMock(return_value=image)
        catch = [gbl_vision.OCRBox("CP 1219", 1.0, 300, int(1334 * 0.30), 120, 30)]
        card = [gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, 680, 246, 21)]
        refused = gbl_ios.excellent_throw_ios.ExcellentThrowError(
            "The catch circle never entered the Excellent lock band; no throw was sent"
        )
        thrower = SimpleNamespace(throw=AsyncMock(side_effect=refused), close=MagicMock())
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", side_effect=[catch, card]),
            patch.object(gbl_ios, "wait", new=AsyncMock()),
            patch.object(gbl_ios, "open_excellent_thrower", return_value=thrower),
            patch.object(
                gbl_ios.excellent_throw_ios,
                "locate_throw_ball",
                return_value=gbl_ios.excellent_throw_ios.BallDetection(1.0, 187, 606, 58),
            ),
            self.no_berry(),
        ):
            done = await gbl_ios.take_reward_encounter(device)
        self.assertTrue(done)
        device.trace_path.assert_awaited_once()
        thrower.close.assert_called_once()

    async def test_a_set_paid_out_in_items_starts_no_stream(self) -> None:
        # Most sets pay out in dust and stardust.  Opening an MJPEG stream on
        # every one of those would cost a second of every set for nothing.
        device = self.device()
        image = type("FakeImage", (), {"width": 750, "height": 1334})()
        device.screenshot = AsyncMock(return_value=image)
        card = [gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, 680, 246, 21)]
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=card),
            patch.object(gbl_ios, "wait", new=AsyncMock()),
            patch.object(gbl_ios, "open_excellent_thrower") as opened,
        ):
            done = await gbl_ios.take_reward_encounter(device)
        self.assertTrue(done)
        opened.assert_not_called()

    def test_closing_leaves_the_shared_session_running(self) -> None:
        # The driver belongs to the GBL run.  Quitting it here -- which is what
        # the script's own teardown does -- would end the set mid-reward.
        thrower = gbl_ios.ExcellentThrower.__new__(gbl_ios.ExcellentThrower)
        driver = MagicMock()
        thrower.thrower = SimpleNamespace(driver=driver, quit=AsyncMock())
        thrower.stream = MagicMock()
        stream = thrower.stream
        thrower.close()
        stream.stop.assert_called_once()
        driver.quit.assert_not_called()
        self.assertIsNone(thrower.stream)


class OrangeTests(unittest.TestCase):
    def test_finds_a_wide_reward_button(self) -> None:
        f = frame()
        put(f, 0.10, 0.90, 0.82, 0.86, ORANGE)
        point = gbl.find_orange(as_frame(f))
        self.assertIsNotNone(point)
        self.assertLess(point[0], f[0] * 0.50)

    def test_finds_a_narrow_collect_tile(self) -> None:
        """One reward in a row of them, measured 0.138 of the width."""
        f = frame()
        put(f, 0.13, 0.27, 0.72, 0.78, ORANGE)
        point = gbl.find_orange(as_frame(f))
        self.assertIsNotNone(point)
        self.assertLess(point[0], f[0] * 0.35)

    def test_ignores_a_line_of_orange_text(self) -> None:
        """Letters reach right across the card with white between them."""
        f = frame(background=WHITE)
        for i in range(9):
            put(f, 0.15 + i * 0.08, 0.17 + i * 0.08, 0.70, 0.74, ORANGE)
        self.assertIsNone(gbl.find_orange(as_frame(f)))


class RewardTileTests(unittest.TestCase):
    """The highlighted tile a finished set leaves, whose middle is artwork.

    `find_orange` needs a run of rows that are orange right across the button.
    The SE's next unclaimed tile is an orange square holding a white "?" disc
    over green grass, so rows through its middle are only a third orange and
    every run breaks well short of PILL_MIN_HEIGHT. The scan returned nothing,
    OCR could not name the tile either -- the card's teal X covers all but 'CT'
    of SELECT -- and the spent BATTLE pill below won by default. The SE tapped
    that dead pill 8 times, three recoveries deep, and played 0 battles.
    """

    GRASS = (109, 188, 79)
    CLOSE_X = (28, 118, 137)

    def card(self, f):
        put(f, 0.04, 0.96, 0.50, 0.97, WHITE)
        put(f, 0.28, 0.72, 0.62, 0.66, PINK)      # BASIC REWARDS ribbon

    def tile(self, f, x0=0.44, x1=0.62):
        """An orange tile with its middle painted over, as the real one is."""
        put(f, x0, x1, 0.67, 0.77, ORANGE)
        put(f, x0 + 0.03, x1 - 0.03, 0.69, 0.75, WHITE)
        put(f, x0 + 0.04, x1 - 0.04, 0.735, 0.765, self.GRASS)

    def test_the_old_scan_cannot_see_this_tile(self) -> None:
        """Without this the rest of the class would pass on a tile-less fix."""
        f = frame()
        self.card(f)
        self.tile(f)
        self.assertIsNone(gbl.find_orange(as_frame(f)))

    def test_the_tile_is_found_by_column(self) -> None:
        f = frame()
        self.card(f)
        self.tile(f)
        point = gbl.find_reward_tile(as_frame(f))
        self.assertIsNotNone(point)
        self.assertTrue(0.44 * f[0] <= point[0] <= 0.62 * f[0])
        self.assertTrue(0.67 * f[1] <= point[1] <= 0.77 * f[1])

    def test_the_tap_stays_clear_of_the_close_x(self) -> None:
        """The X overlaps the tile's bottom-left; its centre is below the tile.

        Tapping the tile's own centre came within 33px of the X's edge. Closing
        the card instead of taking the reward puts the phone back on the map
        with the set still owed.
        """
        f = frame()
        self.card(f)
        self.tile(f)
        point = gbl.find_reward_tile(as_frame(f))
        # The X's centre, measured off the SE at 0.50 across the tile and just
        # below its bottom edge.
        x_centre = (0.50 * f[0], 0.782 * f[1])
        radius = 0.058 * f[0]
        distance = ((point[0] - x_centre[0]) ** 2
                    + (point[1] - x_centre[1]) ** 2) ** 0.5
        self.assertGreater(distance, radius * 1.5)

    def test_a_card_with_no_tile_offers_nothing(self) -> None:
        """The tall_device's card, where every reward is already taken."""
        f = frame()
        self.card(f)
        self.assertIsNone(gbl.find_reward_tile(as_frame(f)))

    def test_badges_beside_the_tile_are_too_narrow(self) -> None:
        """The "x3" count and the tail of SELECT reached only 0.07 of the width."""
        f = frame()
        self.card(f)
        put(f, 0.72, 0.79, 0.70, 0.72, ORANGE)
        self.assertIsNone(gbl.find_reward_tile(as_frame(f)))

    def test_no_banner_means_no_tile_row(self) -> None:
        """Orange elsewhere is not a reward tile without the ribbon above it."""
        f = frame()
        self.tile(f)
        self.assertIsNone(gbl.find_reward_tile(as_frame(f)))

    def test_the_leftmost_tile_is_the_one_owed(self) -> None:
        """The row fills left to right, so anything left of it is claimed."""
        f = frame()
        self.card(f)
        self.tile(f, x0=0.20, x1=0.38)
        self.tile(f, x0=0.44, x1=0.62)
        point = gbl.find_reward_tile(as_frame(f))
        self.assertLess(point[0], 0.44 * f[0])

    def test_the_tile_outranks_a_spent_pill(self) -> None:
        f = frame()
        self.card(f)
        self.tile(f)
        put(f, 0.28, 0.72, 0.86, 0.90, GREY_PILL)
        state, point = gbl.read_screen_state(as_frame(f))
        self.assertEqual(state, 'orange')
        self.assertTrue(0.67 * f[1] <= point[1] <= 0.77 * f[1])


class UnnamedRewardTileTests(unittest.IsolatedAsyncioTestCase):
    """The SE's screen end to end: a tile OCR cannot name, over a dead BATTLE."""

    @staticmethod
    def boxes(width: int, height: int):
        return [
            gbl_vision.OCRBox("GO BATTLE LEAGUE", 1.0, 235, int(height * 0.53), 246, 21),
            # All that survives of SELECT under the card's close X.
            gbl_vision.OCRBox("CT", 1.0, int(width * 0.58), int(height * 0.79), 30, 20),
            gbl_vision.OCRBox("BATTLE", 1.0, int(width * 0.38), int(height * 0.88), 114, 26),
        ]

    def screen(self):
        f = frame()
        helper = RewardTileTests()
        helper.card(f)
        helper.tile(f)
        put(f, 0.28, 0.72, 0.86, 0.90, GREY_PILL)
        return f

    async def test_ios_takes_the_tile_not_the_dead_pill(self) -> None:
        f = self.screen()
        image = gbl.frame_image(as_frame(f))
        with patch("sources.gbl_ios.gbl_vision.recognize",
                   return_value=self.boxes(f[0], f[1])):
            state, point, _label, _boxes = await gbl_ios.smart_screen_state(
                image, object(), probe_unknown=True)
        self.assertEqual(state, "orange")
        self.assertTrue(0.67 * f[1] <= point[1] <= 0.77 * f[1])

    async def test_android_takes_the_tile_not_the_dead_pill(self) -> None:
        f = self.screen()
        with patch("sources.gbl_android.gbl_vision.recognize",
                   return_value=self.boxes(f[0], f[1])):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "orange")
        self.assertTrue(0.67 * f[1] <= point[1] <= 0.77 * f[1])


class LockedRewardTileTests(unittest.IsolatedAsyncioTestCase):
    """The reward row scrolled past the tile the set earned.

    The SE finished a set with one win, so the stardust tile was the only one
    it could take -- and the row was parked with that tile half off the left
    edge, leaving two locked tiles ("3 wins", "4 wins") whole in the middle.
    The colour scan takes the left-most *whole* tile, which was a locked one;
    tapping it opens "Pokemon available at your rank", which the loop then
    closed and tapped again, every five seconds, for twenty minutes. Both
    phones had to be scrolled back by hand before they would take the reward.
    """

    # Read off the SE's own card at 750x1334, scaled to fractions here.
    CUT_COLLECT = (0.12, 0.751)      # 'LLECT', clipped by the screen edge
    LOCKED_CAPTIONS = ((0.55, 0.744), (0.77, 0.744))

    def screen(self):
        """The card with a sliver of the earned tile and two whole locked ones."""
        f = frame()
        helper = RewardTileTests()
        helper.card(f)
        helper.tile(f, x0=0.0, x1=0.07)   # too narrow for the tile scan to take
        helper.tile(f, x0=0.44, x1=0.62)
        helper.tile(f, x0=0.68, x1=0.86)
        put(f, 0.28, 0.72, 0.86, 0.90, GREY_PILL)
        return f

    def captions(self, width: int, height: int):
        return [
            gbl_vision.OCRBox(
                "GO BATTLE LEAGUE", 1.0, 235, int(height * 0.53), 246, 21),
            gbl_vision.OCRBox(
                "3 wins", 1.0, int(width * 0.49), int(height * 0.78), 80, 22),
            gbl_vision.OCRBox(
                "4 wins", 1.0, int(width * 0.73), int(height * 0.78), 80, 22),
            gbl_vision.OCRBox(
                "BATTLE", 1.0, int(width * 0.38), int(height * 0.88), 114, 26),
        ]

    def test_a_cut_off_collect_is_still_a_reward(self) -> None:
        """'LLECT' is COLLECT with the screen edge through it, not a new word."""
        width, height = 750, 1334
        boxes = [
            gbl_vision.OCRBox("LLECT", 1.0, 54, 990, 74, 24),
            gbl_vision.OCRBox("3 wins", 1.0, 371, 981, 80, 24),
        ]
        point = gbl_vision.reward_point(boxes, width, height)
        self.assertIsNotNone(point)
        # Over the clipped tile, and above its button as every caption is.
        self.assertLess(point[0], width * 0.20)
        self.assertLess(point[1], 990)

    def test_a_word_is_not_mistaken_for_a_cut_off_collect(self) -> None:
        for text in ("BATTLE", "3 wins", "x3480", "PREMIUM REWARDS", "CT"):
            with self.subTest(text=text):
                self.assertFalse(gbl_vision.reward_fragment(text))

    def test_a_caption_marks_its_tile_as_locked(self) -> None:
        boxes = self.captions(720, 1600)
        # The colour scan's answer: the left-most whole tile, which is locked.
        self.assertTrue(gbl_vision.locked_reward_tile(
            boxes, [int(720 * 0.53), int(1600 * 0.70)], 720, 1600))
        # An earned tile carries a button where a locked one carries a caption.
        self.assertFalse(gbl_vision.locked_reward_tile(
            boxes, [int(720 * 0.05), int(1600 * 0.70)], 720, 1600))

    def test_a_cut_off_tile_is_told_from_a_paid_out_row(self) -> None:
        """The row only gets dragged when something is hanging off the edge."""
        f = self.screen()
        self.assertTrue(gbl.clipped_reward_tile(as_frame(f)))
        whole = frame()
        helper = RewardTileTests()
        helper.card(whole)
        helper.tile(whole, x0=0.44, x1=0.62)
        helper.tile(whole, x0=0.68, x1=0.86)
        self.assertFalse(gbl.clipped_reward_tile(as_frame(whole)))

    async def test_a_paid_out_card_is_left_to_its_battle_pill(self) -> None:
        """Locked tiles with nothing cut off is a card whose rewards are taken.

        Scrolling that row is three wasted drags and then, before this, a leg
        that ended with 0 battles played on a card it could have battled from.
        """
        f = frame()
        helper = RewardTileTests()
        helper.card(f)
        helper.tile(f, x0=0.44, x1=0.62)
        helper.tile(f, x0=0.68, x1=0.86)
        pill(f, y0f=0.86, y1f=0.90)
        with patch("sources.gbl_android.gbl_vision.recognize",
                   return_value=self.captions(f[0], f[1])):
            state, _point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertNotEqual(state, "tiles")

    async def test_a_spent_scroll_budget_stops_asking(self) -> None:
        """The row would not move, so the card is taken as it is."""
        f = self.screen()
        with patch("sources.gbl_android.gbl_vision.recognize",
                   return_value=self.captions(f[0], f[1])):
            state, _point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True,
                allow_row_scroll=False)
        self.assertNotEqual(state, "tiles")

    def test_the_budget_leaves_the_leg_alive(self) -> None:
        """Running out of scrolls used to break the loop and end the day."""
        for source in (Path(gbl.__file__).read_text(),
                       Path(gbl_ios.__file__).read_text()):
            start = source.index("Reward row will not move")
            self.assertIn("row_scroll_spent = True", source[start - 400:start])

    async def test_android_scrolls_the_row_back_instead_of_pressing(self) -> None:
        f = self.screen()
        with patch("sources.gbl_android.gbl_vision.recognize",
                   return_value=self.captions(f[0], f[1])):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "tiles")
        # On the row, so the drag takes the tiles with it.
        self.assertTrue(0.60 * f[1] <= point[1] <= 0.80 * f[1])

    async def test_ios_scrolls_the_row_back_instead_of_pressing(self) -> None:
        f = self.screen()
        image = gbl.frame_image(as_frame(f))
        with patch("sources.gbl_ios.gbl_vision.recognize",
                   return_value=self.captions(f[0], f[1])):
            state, point, _label, _boxes = await gbl_ios.smart_screen_state(
                image, object(), probe_unknown=True)
        self.assertEqual(state, "tiles")
        self.assertTrue(0.60 * f[1] <= point[1] <= 0.80 * f[1])

    async def test_the_earned_tile_is_taken_once_the_row_shows_it(self) -> None:
        """After the scroll the clipped COLLECT is whole, and it wins."""
        f = self.screen()
        boxes = self.captions(f[0], f[1]) + [
            gbl_vision.OCRBox(
                "COLLECT", 1.0, int(f[0] * 0.02), int(f[1] * 0.78), 105, 21),
        ]
        with patch("sources.gbl_android.gbl_vision.recognize", return_value=boxes):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "orange")
        self.assertLess(point[0], 0.30 * f[0])

    async def test_an_unlocked_tile_is_still_pressed(self) -> None:
        """No captions on the row means nothing is locked; take the tile."""
        f = self.screen()
        boxes = [box for box in self.captions(f[0], f[1])
                 if "wins" not in box.text]
        with patch("sources.gbl_android.gbl_vision.recognize", return_value=boxes):
            state, point, _label, _boxes = await gbl.smart_screen_state(
                as_frame(f), object(), probe_unknown=True)
        self.assertEqual(state, "orange")
        self.assertTrue(0.67 * f[1] <= point[1] <= 0.77 * f[1])


class AbandonedRewardEncounterTests(unittest.IsolatedAsyncioTestCase):
    """Giving up on a reward catch has to actually leave the encounter.

    `take_reward_encounter` is bounded at four Great Balls, but it used to give
    up by returning, and the main loop calls straight back in the moment it sees
    the plate again -- with `throws` reset to zero. A live SE run spent 11 Great
    Balls re-offering itself the same CP 240 Marill and played 2 battles of 40.
    """

    @staticmethod
    def plate(text="CP 240"):
        return SimpleNamespace(text=text)

    def device(self):
        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "test-ios"
        device.viewport = {"width": 375, "height": 667}
        device.config = gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"name": "ios-two"},
            coordinates={"GBL_MOVE_BTN": [142, 391], "CLOSE_BTN": [187, 630]},
            delay_modifier=0,
            battles=5,
        )
        device.tap = AsyncMock()
        device.screenshot = AsyncMock(return_value=Image.new("RGB", (750, 1334)))
        return device

    def test_the_flee_control_is_not_the_close_button(self) -> None:
        # The bug this guards is tapping ENCOUNTER_CLOSE_* on an encounter: at
        # 0.944h that is the ball, so "leaving" would throw instead.
        device = self.device()
        self.assertNotEqual(
            gbl_ios.encounter_flee_point(device),
            gbl_ios.encounter_close_point(device),
        )
        self.assertLess(gbl_ios.ENCOUNTER_FLEE_Y, 0.2)

    def test_a_configured_flee_button_wins(self) -> None:
        device = self.device()
        device.config.coordinates["FLEE_BTN"] = [30, 40]
        self.assertEqual(
            gbl_ios.encounter_flee_point(device), device.scale_point([30, 40]))

    async def run_encounter(self, device, spend_balls=True, throws_ok=True):
        with (
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=[]),
            patch.object(gbl_ios.gbl_vision, "gbl_card_visible", return_value=False),
            patch.object(
                gbl_ios.gbl_vision, "encounter_plate", return_value=self.plate()),
            patch.object(gbl_ios, "wait", AsyncMock()),
            patch.object(gbl_ios, "open_excellent_thrower", MagicMock()),
            patch.object(
                gbl_ios, "send_throw", AsyncMock(return_value=throws_ok)) as throw,
        ):
            result = await gbl_ios.take_reward_encounter(
                device, spend_balls=spend_balls)
        return result, throw

    async def test_the_budget_ends_in_a_flee_not_a_return(self) -> None:
        device = self.device()
        _result, throw = await self.run_encounter(device)
        self.assertEqual(throw.await_count, gbl_ios.ENCOUNTER_THROWS)
        device.tap.assert_awaited_with(gbl_ios.encounter_flee_point(device))

    async def test_a_given_up_encounter_is_never_paid_for_again(self) -> None:
        device = self.device()
        _result, throw = await self.run_encounter(device, spend_balls=False)
        self.assertEqual(throw.await_count, 0)
        device.tap.assert_awaited_with(gbl_ios.encounter_flee_point(device))

    async def test_android_flees_with_back_rather_than_a_coordinate(self) -> None:
        device = SimpleNamespace(
            label="test-android", shell=AsyncMock(), config={})
        with (
            patch.object(gbl, "read_screen", AsyncMock(return_value=frame())),
            patch.object(gbl, "frame_image", MagicMock(
                return_value=Image.new("RGB", (720, 1600)))),
            patch.object(gbl.gbl_vision, "recognize", return_value=[]),
            patch.object(gbl.gbl_vision, "gbl_card_visible", return_value=False),
            patch.object(
                gbl.gbl_vision, "encounter_plate", return_value=self.plate()),
            patch.object(gbl, "wait", AsyncMock()),
            patch.object(gbl, "throw_ball", AsyncMock()) as throw,
        ):
            await gbl.take_reward_encounter(device, spend_balls=False)
        self.assertEqual(throw.await_count, 0)
        device.shell.assert_awaited_with("input keyevent KEYCODE_BACK")

    def test_the_limit_stops_paying_after_the_first_surrender(self) -> None:
        # The caller's arithmetic, stated plainly: one abandonment is enough.
        self.assertEqual(gbl_ios.ABANDONED_ENCOUNTER_LIMIT, 1)
        self.assertEqual(
            gbl.ABANDONED_ENCOUNTER_LIMIT, gbl_ios.ABANDONED_ENCOUNTER_LIMIT)
        abandoned = 0
        self.assertTrue(abandoned < gbl_ios.ABANDONED_ENCOUNTER_LIMIT)
        abandoned += 1
        self.assertFalse(abandoned < gbl_ios.ABANDONED_ENCOUNTER_LIMIT)


class FastPhoneTimingTests(unittest.IsolatedAsyncioTestCase):
    """Which phones earn the aggressive tap/sweep profile.

    The profile used to be chosen by `moto_layout`, an aspect test. The tall_device is
    the quickest handset in the fleet at 35ms per `input` but is 1224x2992,
    aspect 2.44, outside that band -- so it took the conservative defaults and
    built charge energy slower than the Moto G, which is nearly twice as
    expensive per `input`. Speed is now read from the measurement.
    """

    async def timing_for(self, cost: float, label: str = "test-android"):
        device = type("FakeDevice", (), {"label": label})()
        with patch.object(gbl, "measure_input_cost", AsyncMock(return_value=cost)):
            await gbl.prepare_device_timing(device)
        return device

    async def test_the_tall_device_measurement_earns_the_fast_profile(self) -> None:
        device = await self.timing_for(0.035, "tall_device")
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.FAST_MOVE_TAPS_PER_BATCH)
        self.assertEqual(
            device.fast_attack_batches_per_read, gbl.FAST_MOVE_BATCHES_PER_READ)
        self.assertEqual(device.minigame_swipes, gbl.FAST_MINIGAME_SWEEPS)
        self.assertEqual(device.minigame_sweep_ms, gbl.FAST_MINIGAME_SWIPE_MS)

    async def test_the_tall_device_aspect_still_fails_the_layout_test(self) -> None:
        # The bug in one assertion: the phone that needs the fast profile is
        # exactly the phone the old gate rejected. If `moto_layout` ever grew to
        # cover the tall_device this test would go quiet, so it is pinned here.
        self.assertFalse(gbl.moto_layout(as_frame(frame(1224, 2992))))
        device = await self.timing_for(0.035, "tall_device")
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.FAST_MOVE_TAPS_PER_BATCH)

    async def test_the_moto_keeps_the_profile_it_already_had(self) -> None:
        device = await self.timing_for(0.061, "android-two")
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.FAST_MOVE_TAPS_PER_BATCH)
        self.assertEqual(device.minigame_swipes, gbl.FAST_MINIGAME_SWEEPS)

    async def test_a_slow_phone_stays_conservative(self) -> None:
        device = await self.timing_for(0.713, "android-one")
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.MOVE_TAPS_PER_BATCH)
        self.assertEqual(
            device.fast_attack_batches_per_read, gbl.MOVE_BATCHES_PER_READ)
        self.assertEqual(device.minigame_swipes, gbl.MINIGAME_SWEEPS)

    async def test_the_boundary_reading_counts_as_fast(self) -> None:
        device = await self.timing_for(gbl.FAST_INPUT_MAX)
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.FAST_MOVE_TAPS_PER_BATCH)

    async def test_timing_no_longer_needs_a_screen_read(self) -> None:
        # The old gate had to look at the battlefield to measure its aspect,
        # which meant setup could be defeated by whatever was on screen.
        read = AsyncMock(return_value=None)
        device = type("FakeDevice", (), {"label": "tall_device"})()
        with (
            patch.object(gbl, "measure_input_cost", AsyncMock(return_value=0.035)),
            patch.object(gbl, "read_screen", read),
        ):
            await gbl.prepare_device_timing(device)
        read.assert_not_awaited()
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.FAST_MOVE_TAPS_PER_BATCH)

    async def test_the_fast_phone_actually_taps_more_per_batch(self) -> None:
        # Guards the arithmetic, not just the constants: at 35ms the tall_device's
        # budget has room for the full cap, so the promotion is real rather
        # than a bigger cap trimmed straight back down by `batch_size`.
        fast = gbl.batch_size(
            0.035 + gbl.MOVE_TAP_GAP,
            gbl.FAST_MOVE_BATCH_BUDGET,
            gbl.FAST_MOVE_TAPS_PER_BATCH,
            gbl.MOVE_TAPS_MINIMUM,
        )
        slow = gbl.batch_size(
            0.035 + gbl.MOVE_TAP_GAP,
            gbl.MOVE_BATCH_BUDGET,
            gbl.MOVE_TAPS_PER_BATCH,
            gbl.MOVE_TAPS_MINIMUM,
        )
        self.assertEqual(fast, gbl.FAST_MOVE_TAPS_PER_BATCH)
        self.assertGreater(fast, slow)


class CombatTacticsTests(unittest.TestCase):
    def button(self, rgb, width=720, height=1600):
        f = frame(width=width, height=height)
        point = gbl.charged_move_point(as_frame(f))
        x = point[0] / width
        y = point[1] / height
        radius = 0.07
        put(f, x - radius, x + radius, y - radius, y + radius, rgb)
        return as_frame(f)

    def test_charged_move_uses_moto_position(self) -> None:
        self.assertEqual(
            gbl.charged_move_point(self.button((0, 0, 0))),
            [360, 1368],
        )

    def test_moto_full_charged_disc_uses_live_geometry(self) -> None:
        f = frame(width=720, height=1600, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[0])
        self.assertTrue(gbl.charged_move_ready(as_frame(f)))

    def test_moto_partial_charged_disc_is_not_ready(self) -> None:
        # A half-charged button is drawn without a rim, so it is not a tap
        # target however much of its fill is showing.
        f = frame(width=720, height=1600, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[0], rim=False, filled_from=0.0)
        self.assertFalse(gbl.charged_move_ready(as_frame(f)))

    def test_a_charged_but_greyed_button_is_not_a_tap_target(self) -> None:
        """Full and dimmed is what the android-one shows all through an animation.

        The disc is opaque top to bottom there, so every fill test calls it
        ready, but the game will not take the tap until the rim comes back.
        """
        f = frame(width=1316, height=2560, background=(60, 96, 58))
        charged_disc(f, gbl.CHARGED_DISC_CX[1], fill=(81, 132, 118), rim=False)
        self.assertFalse(gbl.charged_move_ready(as_frame(f)))

    def test_a_ready_button_with_spare_energy_is_still_ready(self) -> None:
        """Surplus energy draws a second darker band up from the bottom.

        Measured on the android-one's Fusion Flare: 255,172,114 above 255,125,1.  A
        flat-colour test reads that as two-tone and skips a ready move.
        """
        f = frame(width=1316, height=2560, background=(60, 96, 58))
        charged_disc(f, gbl.CHARGED_DISC_CX[1], fill=(255, 172, 114))
        charged_disc(f, gbl.CHARGED_DISC_CX[1], fill=(255, 125, 1),
                     rim=False, filled_from=0.5)
        self.assertTrue(gbl.charged_disc_ready(
            as_frame(f), gbl.CHARGED_DISC_CX[1]))

    def test_second_charged_move_button_ready(self) -> None:
        f = frame(width=1000, height=2400, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[0])
        self.assertTrue(gbl.charged_move_ready(as_frame(f)))
        target = gbl.charged_move_target_point(as_frame(f))
        self.assertEqual(target[0], int(1000 * gbl.CHARGED_DISC_CX[0]))

    def test_right_charged_move_button_ready(self) -> None:
        f = frame(width=1000, height=2400, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[1])
        self.assertTrue(gbl.charged_move_ready(as_frame(f)))
        target = gbl.charged_move_target_point(as_frame(f))
        self.assertEqual(target[0], int(1000 * gbl.CHARGED_DISC_CX[1]))

    def test_the_lit_button_is_the_one_that_gets_tapped(self) -> None:
        """Move 1 half-charged, Move 2 lit: the tap must go to Move 2.

        The older test picked a column by contrast, which the brighter of two
        drawn discs won whether or not it was the ready one.
        """
        f = frame(width=720, height=1600, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[0], rim=False, filled_from=0.3)
        charged_disc(f, gbl.CHARGED_DISC_CX[1], fill=(214, 97, 132))
        self.assertEqual(
            gbl.charged_move_target_point(as_frame(f))[0],
            int(720 * gbl.CHARGED_DISC_CX[1]),
        )

    def test_a_white_screen_is_not_a_charged_move(self) -> None:
        # The ring test alone passes on anything white, so the ground just
        # outside the button has to still be battlefield.
        f = frame(width=720, height=1600, background=(255, 255, 255))
        self.assertFalse(gbl.charged_move_ready(as_frame(f)))

    def test_charged_move_uses_compact_position(self) -> None:
        f = frame(width=1316, height=2560)
        self.assertEqual(gbl.charged_move_point(as_frame(f)), [658, 2150])

    def test_saturated_charged_move_is_ready(self) -> None:
        f = frame(width=720, height=1600)
        charged_disc(f, gbl.CHARGED_DISC_CX[0], fill=(246, 212, 60))
        self.assertTrue(gbl.charged_move_ready(as_frame(f)))

    def test_saturated_compact_charged_move_is_ready(self) -> None:
        f = frame(width=1316, height=2560)
        charged_disc(f, gbl.CHARGED_DISC_CX[0], fill=(100, 205, 70))
        self.assertTrue(gbl.charged_move_ready(as_frame(f)))

    def test_compact_full_charged_disc_is_ready_at_its_real_height(self) -> None:
        f = frame(width=1316, height=2560, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[0], gbl.COMPACT_CHARGED_DISC_CY)
        self.assertTrue(gbl.charged_move_ready(as_frame(f)))

    def test_compact_partially_filled_disc_is_not_ready(self) -> None:
        f = frame(width=1316, height=2560, background=(35, 60, 40))
        charged_disc(f, gbl.CHARGED_DISC_CX[0], gbl.COMPACT_CHARGED_DISC_CY,
                     rim=False, filled_from=0.6)
        self.assertFalse(gbl.charged_move_ready(as_frame(f)))

    def test_each_layout_reads_its_own_measured_row(self) -> None:
        """Traced disc centres: android-one 0.8245, moto g 0.8431, tall_device 0.9052.

        These were read off battle frames on each handset, and a row that is
        wrong by more than the search band leaves that phone unable to see a
        charged move at all -- which is what the android-one did for twelve battles.
        """
        for width, height, cy in (
            (1316, 2560, gbl.COMPACT_CHARGED_DISC_CY),
            (720, 1600, gbl.MOTO_CHARGED_DISC_CY),
            (1224, 2992, gbl.CHARGED_DISC_CY),
        ):
            with self.subTest(width=width):
                f = frame(width=width, height=height, background=(35, 60, 40))
                self.assertAlmostEqual(
                    gbl.charged_disc_centre_y(as_frame(f)), cy)
                charged_disc(f, gbl.CHARGED_DISC_CX[1], cy)
                self.assertTrue(gbl.charged_move_ready(as_frame(f)))

    def test_grey_charged_move_is_not_ready(self) -> None:
        f = frame(width=720, height=1600)
        charged_disc(f, gbl.CHARGED_DISC_CX[0], fill=(133, 133, 128), rim=False)
        self.assertFalse(gbl.charged_move_ready(as_frame(f)))

    def test_teal_result_tick_is_not_a_charged_move(self) -> None:
        self.assertFalse(gbl.charged_move_ready(self.button((28, 133, 148))))

    def test_teal_result_tick_is_a_single_result_action(self) -> None:
        self.assertTrue(gbl.teal_result_ready(self.button((28, 133, 148))))

    def test_grey_charged_move_is_not_a_result_action(self) -> None:
        self.assertFalse(gbl.teal_result_ready(self.button((133, 133, 128))))

    def test_iphone_result_tick_uses_bottom_checkmark_geometry(self) -> None:
        f = frame(width=1320, height=2868, background=(20, 20, 20))
        put(f, 0.44, 0.56, 0.915, 0.960, (28, 133, 148))
        self.assertTrue(
            gbl.teal_result_ready(
                as_frame(f), gbl_ios.TEAL_SAMPLE_X, gbl_ios.TEAL_SAMPLE_Y
            )
        )
        self.assertGreater(int(f[1] * gbl_ios.RESULT_TAP_Y), int(f[1] * 0.915))

    def test_iphone_charged_disc_uses_pro_max_bottom_rows(self) -> None:
        # The disc extent here is measured off traced Pro Max battle frames,
        # not chosen to suit the rows: 0.855-0.9375h, centre 0.8962.
        f = frame(width=1320, height=2868, background=(35, 60, 40))
        put(f, 0.44, 0.56, 0.855, 0.9375, (220, 220, 220))
        self.assertTrue(
            gbl.charged_move_ready(as_frame(f), gbl_ios.CHARGED_DISC_ROWS)
        )

    def test_pro_max_charged_rows_sit_inside_the_measured_disc(self) -> None:
        """Every row must land on the button, or disc_present never fires.

        The shipped rows once ended at 0.950, below the disc's 0.9375 bottom
        edge, so all-true was unreachable and the phone played whole sets on
        fast attacks alone. Same defect the SE had, one row instead of two.
        """

        for row in gbl_ios.CHARGED_DISC_ROWS:
            self.assertGreater(row, 0.855)
            self.assertLess(row, 0.9375)
        self.assertAlmostEqual(gbl_ios.CHARGED_Y, 0.896, places=3)
        self.assertGreater(gbl_ios.CHARGED_Y, 0.855)
        self.assertLess(gbl_ios.CHARGED_Y, 0.9375)

    def test_pro_max_charged_rows_reject_the_shield_prompt_hexagon(self) -> None:
        f = frame(width=1320, height=2868, background=(60, 90, 50))
        put(f, 0.0, 1.0, 0.650, 0.980, (25, 25, 25))
        put(f, 0.44, 0.56, 0.790, 0.860, (220, 180, 235))
        self.assertFalse(
            gbl.charged_move_ready(as_frame(f), gbl_ios.CHARGED_DISC_ROWS)
        )

    def test_iphone_shield_prompt_uses_pro_max_hexagon_rows(self) -> None:
        f = frame(width=1320, height=2868, background=(60, 90, 50))
        put(f, 0.0, 1.0, 0.650, 0.980, (25, 25, 25))
        put(f, 0.44, 0.56, 0.790, 0.860, (220, 180, 235))
        self.assertTrue(
            gbl.shield_prompt(
                as_frame(f), gbl_ios.SHIELD_DISC_ROWS, panel_y=0.72
            )
        )

    def test_compact_shield_tap_tracks_the_visible_hexagon(self) -> None:
        f = frame(width=1316, height=2560, background=(60, 90, 50))
        put(f, 0.0, 1.0, 0.60, 0.98, (25, 25, 25))
        put(f, 0.42, 0.58, 0.717, 0.817, (220, 180, 235))
        point = gbl.shield_point(as_frame(f))
        self.assertIsNotNone(point)
        self.assertGreater(point[1], int(f[1] * 0.717))
        self.assertLess(point[1], int(f[1] * 0.817))

    def test_tall_device_shield_tap_tracks_its_lower_hexagon(self) -> None:
        f = frame(width=1224, height=2992, background=(60, 90, 50))
        put(f, 0.0, 1.0, 0.65, 0.98, (25, 25, 25))
        put(f, 0.42, 0.58, 0.817, 0.899, (220, 180, 235))
        point = gbl.shield_point(as_frame(f))
        self.assertIsNotNone(point)
        self.assertGreater(point[1], int(f[1] * 0.817))
        self.assertLess(point[1], int(f[1] * 0.899))

    def test_iphone_forced_switch_sheet_uses_lower_panel_rows(self) -> None:
        f = frame(width=1320, height=2868, background=(60, 90, 50))
        put(f, 0.0, 1.0, 0.760, 0.810, (40, 46, 59))
        self.assertTrue(
            gbl.switch_sheet(
                as_frame(f), gbl_ios.SHEET_ABOVE_Y, gbl_ios.SHEET_SCAN_Y
            )
        )

    def test_battle_move_text_rejects_orange_animation_false_positive(self) -> None:
        move = [gbl_vision.OCRBox("Dartrix used Seed Bomb!", 1.0, 0, 0, 100, 20)]
        reward = [gbl_vision.OCRBox("CLAIM RANK REWARDS", 1.0, 0, 0, 100, 20)]
        self.assertTrue(gbl.battle_animation(move))
        self.assertFalse(gbl.battle_animation(reward))

    def test_charged_minigame_rating_prefers_game_text(self) -> None:
        boxes = [gbl_vision.OCRBox("EXCELLENT!", 1.0, 0, 0, 100, 20)]
        self.assertEqual(gbl.charged_minigame_rating(boxes), "EXCELLENT")

    def test_not_very_effective_battle_text_requests_counter(self) -> None:
        boxes = [gbl_vision.OCRBox("NOT VERY EFFECTIVE...", 1.0, 0, 0, 160, 20)]
        not_effective, super_effective = gbl.battle_effectiveness(boxes)
        self.assertTrue(not_effective)
        self.assertFalse(super_effective)

    def test_minigame_raster_is_dense_and_covers_the_playfield(self) -> None:
        segments = gbl.minigame_segments(1000, 2000)
        # Keep the live Android command stream inside the minigame's time
        # window; three complete 11-row passes are denser than the old path
        # that was still executing after the rating screen disappeared.
        self.assertGreaterEqual(len(segments), 21)
        xs = [value for segment in segments for value in (segment[0], segment[2])]
        ys = [value for segment in segments for value in (segment[1], segment[3])]
        self.assertLessEqual(min(xs), 100)
        self.assertGreaterEqual(max(xs), 900)
        # Every gesture belongs inside the bubble field, which traced
        # charge-result frames put in the lower half of the screen. Sweeping
        # from 0.14h spent most of each wave in empty sky and put the
        # top-left corner beside the flee button; the floor keeps the raster
        # clear of the navigation bar.
        self.assertGreaterEqual(min(ys), 1000)
        self.assertGreaterEqual(max(ys), 1800)
        self.assertLessEqual(max(ys), 1860)


class ChargedLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_android_polls_the_prompt_before_tapping_again(self) -> None:
        """A prompt that has not rendered yet must not cost a second tap.

        GET READY is routinely still absent one CHARGED_LAUNCH_CONFIRM after
        the tap. Re-tapping there sends the second tap into the minigame
        playfield of a move that did launch, and reports the launch as missed.
        """
        ready = CombatTacticsTests().button((246, 212, 60))
        gone = CombatTacticsTests().button((133, 133, 128))
        tap_mock = AsyncMock()
        prompt_mock = AsyncMock(side_effect=[(None, ready), ("get ready", gone)])
        with (
            patch.object(gbl, "tap", tap_mock),
            patch.object(gbl, "wait", AsyncMock()),
            patch.object(gbl, "read_charged_prompt", prompt_mock),
        ):
            launched, launch_frame, attempts, label = await gbl.launch_charged_move(
                object(), ready
            )
        self.assertTrue(launched)
        self.assertEqual(label, "get ready")
        self.assertIs(launch_frame, gone)
        self.assertEqual(attempts, 1)
        self.assertEqual(tap_mock.await_count, 1)

    async def test_android_retries_until_launch_is_visible(self) -> None:
        """A genuinely dropped tap is still retried, once polling comes up empty."""
        ready = CombatTacticsTests().button((246, 212, 60))
        gone = CombatTacticsTests().button((133, 133, 128))
        tap_mock = AsyncMock()
        prompt_mock = AsyncMock(
            side_effect=[(None, ready)] * gbl.CHARGED_LAUNCH_POLLS
            + [("get ready", gone)]
        )
        with (
            patch.object(gbl, "tap", tap_mock),
            patch.object(gbl, "wait", AsyncMock()),
            patch.object(gbl, "read_charged_prompt", prompt_mock),
        ):
            launched, launch_frame, attempts, label = await gbl.launch_charged_move(
                object(), ready
            )
        self.assertTrue(launched)
        self.assertEqual(label, "get ready")
        self.assertIs(launch_frame, gone)
        self.assertEqual(attempts, 2)
        self.assertEqual(tap_mock.await_count, 2)

    async def test_android_failed_launch_is_bounded(self) -> None:
        ready = CombatTacticsTests().button((246, 212, 60))
        with (
            patch.object(gbl, "tap", new=AsyncMock()) as tap_mock,
            patch.object(gbl, "wait", new=AsyncMock()),
            patch.object(gbl, "read_screen", new=AsyncMock(return_value=ready)),
            patch.object(gbl_vision, "recognize", return_value=[]),
        ):
            launched, _frame, attempts, label = await gbl.launch_charged_move(
                object(), ready
            )
        self.assertFalse(launched)
        self.assertIsNone(label)
        self.assertEqual(attempts, gbl.CHARGED_LAUNCH_ATTEMPTS)
        self.assertEqual(tap_mock.await_count, gbl.CHARGED_LAUNCH_ATTEMPTS)

    async def test_android_attack_batch_probes_exact_compact_charge_centre(self) -> None:
        f = as_frame(frame(width=1316, height=2560))
        device = type("FakeDevice", (), {"label": "test-android"})()
        tap_mock = AsyncMock()
        minigame_mock = AsyncMock()
        with (
            patch.object(gbl, "tap", tap_mock),
            patch.object(gbl, "wait", AsyncMock()),
            patch.object(
                gbl, "read_charged_prompt",
                AsyncMock(return_value=("get ready", f))),
            patch.object(
                gbl, "wait_for_charged_swipe",
                AsyncMock(return_value=f)),
            patch.object(gbl, "charged_minigame", minigame_mock),
            patch.object(gbl, "capture_charged_rating", AsyncMock()),
        ):
            launched = await gbl.probe_charged_after_fast_attacks(device, f)
        self.assertTrue(launched)
        tap_mock.assert_awaited_once_with(device, [658, 2150])
        minigame_mock.assert_awaited_once_with(device, f)

    def test_android_charged_prompt_recognizes_both_phases(self) -> None:
        self.assertEqual(gbl.charged_prompt_label([
            gbl_vision.OCRBox("GET READY!", 1.0, 0, 0, 100, 20),
        ]), "get ready")
        self.assertEqual(gbl.charged_prompt_label([
            gbl_vision.OCRBox("SWIPE!", 1.0, 0, 0, 100, 20),
        ]), "swipe")

    async def test_ios_minigame_uses_dense_fresh_touch_gestures(self) -> None:
        device = type("FakeIOS", (), {})()
        device.viewport = {"width": 1000, "height": 2000}
        device.trace_path = AsyncMock()
        with patch.object(gbl_ios, "wait", new=AsyncMock()):
            await gbl_ios.charged_minigame(device, seconds=0)
        self.assertEqual(device.trace_path.await_count, 1)
        points = [
            point
            for call in device.trace_path.await_args_list
            for point in call.args[0]
        ]
        self.assertGreaterEqual(len(points), 7)

    async def test_ios_minigame_covers_the_field_for_the_whole_window(self) -> None:
        """A phone whose gestures are cheap keeps sweeping, it does not stop.

        The raster used to be one pass of six chunks whatever the handset, so
        the Pro Max finished while the field was still spawning bubbles.
        """
        device = type("FakeIOS", (), {})()
        device.viewport = {"width": 1000, "height": 2000}
        device.trace_path = AsyncMock()
        clock = iter([0.0] + [0.1 * step for step in range(1, 200)])
        with (
            patch.object(gbl_ios, "wait", new=AsyncMock()),
            patch.object(
                gbl_ios.asyncio, "get_event_loop",
                return_value=type("L", (), {"time": lambda self: next(clock)})(),
            ),
        ):
            await gbl_ios.charged_minigame(device, seconds=4.5)
        # 4.5s of a 0.1s-per-gesture phone is far more than the six chunks a
        # single pass would have sent.
        self.assertGreater(device.trace_path.await_count, 6)

    async def test_ios_minigame_does_not_wait_for_the_swipe_prompt(self) -> None:
        """Coverage starts on the tap: reading the prompt cost the field.

        Every launch on both phones logged 'prompt timed out' -- the 2.2s of
        screenshots and OCR ran out before the word was ever caught, so the
        first gesture landed after the early bubbles had gone.
        """
        self.assertFalse(hasattr(gbl_ios, "wait_for_minigame_start"))
        # Names the compiled body touches, so the docstring cannot satisfy it.
        called = gbl_ios.charged_minigame.__code__.co_names
        self.assertNotIn("screenshot", called)
        self.assertNotIn("recognize", called)


class ScreenStateTests(unittest.TestCase):
    def cards(self, f, count=3):
        for i in range(count):
            put(f, 0.06, 0.94, 0.30 + i * 0.20, 0.44 + i * 0.20, WHITE)

    def test_league_list_is_cards_without_a_pill(self) -> None:
        f = frame()
        self.cards(f)
        state, point = gbl.read_screen_state(as_frame(f))
        self.assertEqual(state, 'league')
        # The bottom card, which is the current cup.
        self.assertGreater(point[1], f[1] * 0.65)

    def test_pill_wins_over_cards(self) -> None:
        """The GO BATTLE LEAGUE screen shows both; only the pill is actionable."""
        f = frame()
        self.cards(f, count=2)
        pill(f)
        self.assertEqual(gbl.read_screen_state(as_frame(f))[0], 'pill')

    def test_orange_wins_over_a_disabled_pill(self) -> None:
        """At the end of a set both are on screen and the pill does nothing."""
        f = frame()
        self.cards(f, count=2)
        put(f, 0.30, 0.70, 0.78, 0.82, GREY_PILL)
        put(f, 0.10, 0.90, 0.84, 0.88, ORANGE)
        self.assertEqual(gbl.read_screen_state(as_frame(f))[0], 'orange')

    def test_bare_screen_is_a_battle(self) -> None:
        f = frame()
        state, point = gbl.read_screen_state(as_frame(f))
        self.assertEqual(state, 'battle')
        self.assertIsNone(point)

    def test_tall_single_reward_panel_requests_a_scroll(self) -> None:
        f = frame()
        put(f, 0.06, 0.94, 0.49, 0.68, WHITE)
        state, point = gbl.read_screen_state(as_frame(f))
        self.assertEqual(state, 'scroll')
        self.assertIsNone(point)

    def test_short_single_white_strip_is_not_a_reward_panel(self) -> None:
        f = frame()
        put(f, 0.06, 0.94, 0.49, 0.58, WHITE)
        self.assertEqual(gbl.read_screen_state(as_frame(f))[0], 'battle')

    def test_large_completed_set_panels_scroll_back_instead_of_tapping(self) -> None:
        """Season/Nearby panels are not league cards after the fifth battle."""
        f = frame()
        put(f, 0.06, 0.94, 0.30, 0.58, WHITE)
        put(f, 0.06, 0.94, 0.62, 0.93, WHITE)
        self.assertEqual(gbl.read_screen_state(as_frame(f))[0], 'scroll_back')

    def test_nearby_battle_pill_is_never_tapped(self) -> None:
        """SCAN A BATTLE CODE is green but belongs to huge non-GBL panels."""
        f = frame()
        put(f, 0.06, 0.94, 0.30, 0.60, WHITE)
        put(f, 0.06, 0.94, 0.64, 0.93, WHITE)
        pill(f, y0f=0.48, y1f=0.53, x0f=0.20, x1f=0.80)
        self.assertEqual(gbl.read_screen_state(as_frame(f))[0], 'scroll_back')


class ConfirmSwitchTests(unittest.IsolatedAsyncioTestCase):
    """The swap animation outlasts one poll, so verification has to wait."""

    async def test_a_swap_seen_on_a_later_read_counts_as_verified(self) -> None:
        # The header keeps showing the outgoing name for the first two reads.
        names = iter(['Skarmory', 'Skarmory', 'Swampert'])
        with patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl, 'read_screen', AsyncMock(return_value=as_frame(frame()))), \
                patch.object(gbl, 'recognize_player',
                             AsyncMock(side_effect=lambda *_: next(names))):
            seen = await gbl.confirm_switch(object(), ('Skarmory', 'Swampert'), 'Swampert')
        self.assertEqual(seen, 'Swampert')

    async def test_a_swap_that_never_lands_reports_what_it_saw(self) -> None:
        with patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl, 'read_screen', AsyncMock(return_value=as_frame(frame()))), \
                patch.object(gbl, 'recognize_player', AsyncMock(return_value='Skarmory')):
            seen = await gbl.confirm_switch(object(), ('Skarmory', 'Swampert'), 'Swampert')
        self.assertEqual(seen, 'Skarmory')

    async def test_unreadable_frames_do_not_crash_the_check(self) -> None:
        with patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl, 'read_screen', AsyncMock(return_value=None)):
            seen = await gbl.confirm_switch(object(), ('Skarmory',), 'Swampert')
        self.assertIsNone(seen)

    async def test_the_wait_for_a_swap_is_spent_attacking(self) -> None:
        """Given the move point, verification must not sleep through the gap.

        Four idle POLL_GAPs on every switch is what a watcher sees as the
        phone doing nothing after a swap, and it throws away the energy those
        seconds would have built.
        """
        wait_mock = AsyncMock()
        attack_mock = AsyncMock()
        with patch.object(gbl, 'wait', wait_mock), \
                patch.object(gbl, 'fast_attack', attack_mock), \
                patch.object(gbl, 'read_screen', AsyncMock(return_value=as_frame(frame()))), \
                patch.object(gbl, 'recognize_player', AsyncMock(return_value='Skarmory')):
            await gbl.confirm_switch(
                object(), ('Skarmory', 'Swampert'), 'Swampert', move_point=[10, 20])
        self.assertEqual(attack_mock.await_count, gbl.SWITCH_VERIFY_READS)
        self.assertEqual(wait_mock.await_count, 0)
        self.assertEqual(attack_mock.await_args.args[1], [10, 20])


class ShieldPromptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The battlefield screen that names itself, and the misread that needs it.

    A live run stopped after battle 7 with 18 battles unplayed: matchmaking
    read as an end-of-set page scrolled past its rewards, which disarmed the
    loop, and battle 8 was then a screen it would not tap.
    """

    def boxes(self, *lines):
        return [gbl_vision.OCRBox(line, 1.0, 0, i * 30, 300, 20)
                for i, line in enumerate(lines)]

    def test_the_shield_prompt_is_named_from_its_words(self) -> None:
        self.assertTrue(gbl.shield_prompt_text(
            self.boxes('Attack incoming! Use a Protect Shield?', 'NOT NOW')))

    def test_a_menu_is_not_a_shield_prompt(self) -> None:
        self.assertFalse(gbl.shield_prompt_text(
            self.boxes('GO BATTLE LEAGUE', 'CLAIM RANK REWARDS!', 'BATTLE')))

    def test_matchmaking_panels_still_read_as_a_scrolled_reward_page(self) -> None:
        """The misread itself: two tall panels and no pill."""
        f = frame()
        put(f, 0.06, 0.94, 0.10, 0.40, WHITE)
        put(f, 0.06, 0.94, 0.55, 0.90, WHITE)
        self.assertEqual(gbl.read_screen_state(as_frame(f))[0], 'scroll_back')

    async def test_a_shield_prompt_arms_a_loop_that_lost_its_catch(self) -> None:
        """Unarmed plus a shield prompt means attack, not wait it out."""
        taps: list[list[int]] = []
        stop = RuntimeError('stopped after the shield')

        async def fake_tap(_device, point):
            taps.append(point)
            raise stop

        device = SimpleNamespace(
            config={'GBL_MOVE_BTN': [360, 800]}, label=' moto ')
        boxes = self.boxes('Attack incoming! Use a Protect Shield?', 'NOT NOW')
        with patch.object(gbl, 'read_screen',
                          AsyncMock(return_value=as_frame(frame()))), \
                patch.object(gbl, 'smart_screen_state',
                             AsyncMock(return_value=('battle', None, None, boxes))), \
                patch.object(gbl, 'recognized_shield_point',
                             AsyncMock(return_value=[360, 1330])), \
                patch.object(gbl, 'tap', fake_tap), \
                patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl_strategy, 'load_strategy_profile',
                             lambda **kwargs: None):
            with self.assertRaises(RuntimeError):
                await gbl.play_device(device, 1)
        self.assertEqual(taps, [[360, 1330]])


def team_member(name: str, cp: int = 1495) -> gbl_strategy.TeamMember:
    meta = gbl_strategy.gbl_evaluator.lookup_meta_pokemon(name)
    return gbl_strategy.TeamMember(
        meta.name,
        cp,
        tuple(meta.types),
        meta.fast_move,
        tuple(meta.charged_moves),
        meta.bulk_rating,
        meta.auto_battle_rating,
    )


class IOSRosterPagingTests(unittest.IsolatedAsyncioTestCase):
    """Paging the party picker's roster on the iPhone.

    One "mobile: dragFromToForDuration" is read as a click by the game's UI, so
    a roster paged with it is page one read eight times -- and a click that
    lands on a tile picks that Pokemon and drops the picker.  The pages have to
    be turned with a held, stepped gesture, and a page that comes back
    unchanged has to end the scan rather than send seven more into the sheet.
    """

    @staticmethod
    def device():
        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "test-ios"
        device.viewport = {"width": 375, "height": 667}
        device.config = gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"name": "ios-two"},
            coordinates={"GBL_MOVE_BTN": [142, 391]},
            delay_modifier=0,
            battles=5,
        )
        device.tap = AsyncMock()
        device.drag = AsyncMock()
        device.scroll = AsyncMock()
        device.hide_keyboard = AsyncMock(return_value=True)
        return device

    async def scan(self, device, images):
        """Run the party preparation over the given picker frames."""
        from PIL import Image

        frames = [Image.new("RGB", (750, 1334), colour) for colour in images]
        device.screenshot = AsyncMock(return_value=frames[0])
        profile = gbl_strategy.StrategyProfile(
            gbl_strategy.BattleTeam(
                (team_member('Noctowl'), team_member('Lanturn'),
                 team_member('Whiscash'))
            )
        )
        logged: list[str] = []
        # The first snapshot is the one that proves the picker opened; the rest
        # are the pages of the scan.
        snapshots = [(frames[0], [])] + [(image, []) for image in frames]
        with patch.object(gbl_ios, "ios_picker_snapshot",
                          AsyncMock(side_effect=snapshots)), \
                patch.object(gbl_ios.gbl_vision, "recognize", lambda *a, **k: []), \
                patch.object(gbl_ios.android_gbl, "picker_body_screen",
                             lambda boxes: True), \
                patch.object(gbl_ios.gbl_meta, "load_meta_index", lambda cap: {}), \
                patch.object(gbl_ios.gbl_meta, "parse_roster_page",
                             lambda boxes, index: []), \
                patch.object(gbl_ios.gbl_strategy, "team_from_party_ocr",
                             lambda boxes, names: None), \
                patch.object(gbl_ios, "cancel_verified_picker", AsyncMock()), \
                patch.object(gbl_ios, "wait", AsyncMock()), \
                patch.object(gbl_ios, "log",
                             lambda _device, *parts: logged.append(" ".join(map(str, parts)))):
            prepared = await gbl_ios.prepare_best_party(device, object(), [], profile)
        self.assertIsNone(prepared)
        return logged

    async def test_the_roster_is_paged_with_a_stepped_scroll(self) -> None:
        device = self.device()
        # A distinct frame per configured page, so the scan pages all the way
        # through rather than stopping on an unchanged one.
        pages = gbl_strategy.StrategySettings().roster_scan_pages
        await self.scan(device, [(step * 20, 40, 50) for step in range(pages)])
        device.drag.assert_not_awaited()
        self.assertEqual(
            device.scroll.await_args.args[:2],
            (device.fraction_point(*gbl_ios.IOS_PICKER_SCROLL[0]),
             device.fraction_point(*gbl_ios.IOS_PICKER_SCROLL[1])),
        )

    async def test_an_unmoved_page_ends_the_scan(self) -> None:
        device = self.device()
        # The same frame twice is a scroll that did not scroll.  Eight pages are
        # configured; the scan has to stop at the second read.
        logged = await self.scan(device, [(30, 40, 50), (30, 40, 50)])
        self.assertEqual(device.scroll.await_count, 1)
        pages_read = [line for line in logged if line.startswith("  Roster page ")
                      and line.rstrip().endswith("no ranked names")]
        self.assertEqual(len(pages_read), 2)
        self.assertTrue(any("did not move" in line for line in logged))


class IOSSteppedScrollGestureTests(unittest.IsolatedAsyncioTestCase):
    """The scroll gesture itself, as WebDriverAgent receives it."""

    async def test_the_scroll_is_a_held_stepped_path(self) -> None:
        sent: list[dict] = []

        class FakeDriver:
            def execute(self, _command, payload=None):
                sent.append(payload)
                return {"value": None}

        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "test-ios"
        device.viewport = {"width": 375, "height": 667}
        device.driver = FakeDriver()
        await device.scroll([270, 546], [270, 166], 0.30)

        actions = sent[0]["actions"][0]["actions"]
        moves = [action for action in actions if action["type"] == "pointerMove"]
        pauses = [action for action in actions if action["type"] == "pause"]
        # One move onto the start point, then one per step.
        self.assertEqual(len(moves), gbl_ios.SCROLL_STEPS + 1)
        self.assertEqual(moves[0]["y"], 546)
        self.assertEqual(moves[-1]["y"], 166)
        # Held before the first step and after the last: without those the game
        # reads the whole gesture as a click.
        self.assertEqual(pauses[0]["duration"], gbl_ios.SCROLL_HOLD * 1000)
        self.assertEqual(pauses[-1]["duration"], gbl_ios.SCROLL_HOLD * 1000)
        self.assertEqual(
            [action["type"] for action in actions[-2:]],
            ["pause", "pointerUp"],
        )


class IOSForegroundRestoreTests(unittest.IsolatedAsyncioTestCase):
    """Getting the game back in front of a home screen before refusing to run.

    A leg that stops takes its WebDriverAgent runner down with it and leaves the
    phone on the home screen, where every later leg of the day reads five
    SpringBoard frames and refuses.  A dead game is still left for a person, and a game parked on the wrong
    Pokemon GO screen is not papered over: asking for an app that is
    already frontmost changes nothing, and the caller reads the screen
    again and still refuses.
    """

    @staticmethod
    def device(front: str, state: int = 4):
        class FakeDriver:
            def __init__(self) -> None:
                self.activated: list[str] = []

            def execute_script(self, script, args=None):
                if script == "mobile: activeAppInfo":
                    return {"bundleId": front}
                if script == "mobile: queryAppState":
                    return state
                if script == "mobile: activateApp":
                    self.activated.append(args["bundleId"])
                return None

        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "test-ios"
        device.viewport = {"width": 375, "height": 667}
        device.config = gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"name": "ios-two",
                        "bundle_id": "com.nianticlabs.pokemongo"},
            coordinates={"GBL_MOVE_BTN": [142, 391]},
            delay_modifier=0,
            battles=5,
        )
        device.driver = FakeDriver()
        return device

    async def test_a_home_screen_is_answered_with_an_activate(self) -> None:
        device = self.device("com.apple.springboard")
        self.assertTrue(await device.restore_foreground())
        self.assertEqual(device.driver.activated, ["com.nianticlabs.pokemongo"])

    async def test_the_game_reported_in_front_is_asked_for_again(self) -> None:
        # The app switcher is an overlay: the game stays the active app
        # underneath it, so being in front is no evidence of being visible.
        # Refusing here left the SE unable to start any leg of a day.
        device = self.device("com.nianticlabs.pokemongo")
        self.assertTrue(await device.restore_foreground())
        self.assertEqual(device.driver.activated, ["com.nianticlabs.pokemongo"])

    async def test_a_dead_game_is_not_cold_started(self) -> None:
        # Activating an app that is not running launches it, and a cold start
        # lands on the map behind a splash rather than on the league screen.
        device = self.device("com.apple.springboard",
                             state=gbl_ios.IOS_APP_STATE_NOT_RUNNING)
        self.assertFalse(await device.restore_foreground())
        self.assertEqual(device.driver.activated, [])


class WalkBackFromTheMapTests(unittest.IsolatedAsyncioTestCase):
    """Getting from the world map back to GBL, which is where a set restarts.

    The exit door works: it closes GBL and leaves the phone on the map. Nothing
    on the map is a pill or a league card, so the loop used to spend its
    remaining reads waiting for one -- a day stopped at 8 of 11 battles with the
    card still offering 2/5 played.
    """

    def boxes(self, *lines):
        return [gbl_vision.OCRBox(line, 1.0, 0, i * 30, 300, 20)
                for i, line in enumerate(lines)]

    async def test_the_map_is_left_through_the_menu_button(self) -> None:
        taps: list[list[int]] = []

        async def fake_tap(_device, point):
            taps.append(point)

        device = SimpleNamespace(config={}, label='android-two')
        with patch.object(gbl, 'tap', fake_tap), \
                patch.object(gbl, 'wait', AsyncMock()):
            # Nothing readable on screen, and well past the attempts that would
            # rather press a mapped close button.
            await gbl.recover_to_gbl(device, as_frame(frame()), [], 32)
        self.assertEqual(taps, [[360, 1412]])

    async def test_the_menu_it_opens_is_taken_to_battle(self) -> None:
        taps: list[list[int]] = []

        async def fake_tap(_device, point):
            taps.append(point)

        device = SimpleNamespace(config={}, label='android-two')
        boxes = self.boxes('POKEDEX', 'BATTLE', 'SHOP', 'POKEMON', 'ITEMS')
        with patch.object(gbl, 'tap', fake_tap), \
                patch.object(gbl, 'wait', AsyncMock()):
            await gbl.recover_to_gbl(device, as_frame(frame()), boxes, 33)
        self.assertEqual(len(taps), 1)
        # The label is what OCR finds; the tap belongs on the icon below it.
        self.assertGreater(taps[0][1], 30)

    async def test_an_unreadable_screen_ends_in_the_walk_back(self) -> None:
        """The ladder must reach the menu, not just scroll and quit."""
        taps: list[list[int]] = []
        stop = RuntimeError('walked back')

        async def fake_tap(_device, point):
            taps.append(point)
            if point == [360, 1412]:
                raise stop

        device = SimpleNamespace(config={'GBL_MOVE_BTN': [360, 800]}, label='android-two')
        profile = gbl_strategy.StrategyProfile(
            gbl_strategy.BattleTeam(
                (team_member('Noctowl'), team_member('Lanturn'),
                 team_member('Whiscash'))
            )
        )
        with patch.object(gbl, 'read_screen',
                          AsyncMock(return_value=as_frame(frame()))), \
                patch.object(gbl, 'smart_screen_state',
                             AsyncMock(return_value=('battle', None, None, []))), \
                patch.object(gbl, 'tap', fake_tap), \
                patch.object(gbl, 'scroll_reward_tiers', AsyncMock()), \
                patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl_strategy, 'load_strategy_profile',
                             lambda **kwargs: profile):
            with self.assertRaises(RuntimeError):
                await gbl.play_device(device, 1)
        self.assertIn([360, 1412], taps)


class OpenSetLeagueTests(unittest.IsolatedAsyncioTestCase):
    """A set that is already running is judged by the name on its party screen.

    A leg that starts while the phone is inside a league never meets the league
    list, so nothing chooses the configured one.  Live, that spent an afternoon
    on Competitors Cup while the config asked for Master League.
    """

    @staticmethod
    def box(text: str) -> gbl_vision.OCRBox:
        return gbl_vision.OCRBox(text, 1.0, 200, 440, 350, 60)

    def test_the_party_screen_names_its_league(self) -> None:
        boxes = [
            self.box("CHOOSE YOUR PARTY"),
            self.box("COMPETITORS CUP"),
            self.box("Max CP per Pokemon: 1,500"),
        ]
        self.assertEqual(gbl_ios._party_league_name(boxes), "competitors cup")

    def test_master_league_reads_back(self) -> None:
        boxes = [self.box("MASTER LEAGUE"), self.box("CHOOSE YOUR PARTY")]
        self.assertEqual(gbl_ios._party_league_name(boxes), "master league")

    def test_a_screen_naming_no_league_reads_as_nothing(self) -> None:
        boxes = [self.box("CHOOSE YOUR PARTY"), self.box("USE THIS PARTY")]
        self.assertIsNone(gbl_ios._party_league_name(boxes))

    def test_a_mega_edition_counts_as_the_wanted_league(self) -> None:
        # 2026-09-05: both Android legs chose the Mega card off the list and
        # then refused the set it opened, backing out until they stopped with
        # 0 battles played.
        boxes = [
            self.box("MASTER LEAGUE: MEGA EDITION"),
            self.box("CHOOSE YOUR PARTY"),
        ]
        for name in (
            gbl_ios._party_league_name(boxes),
            gbl.party_league_name(boxes),
        ):
            self.assertEqual(name, "master league mega edition")
            self.assertTrue(gbl_strategy.league_matches("Master League", name))

    def test_a_different_league_is_still_refused(self) -> None:
        boxes = [self.box("GREAT LEAGUE"), self.box("CHOOSE YOUR PARTY")]
        self.assertFalse(
            gbl_strategy.league_matches(
                "Master League", gbl.party_league_name(boxes)
            )
        )

    async def test_leaving_a_set_taps_the_exit_door(self) -> None:
        taps: list[list[int]] = []

        class FakeDevice:
            label = "test-ios"
            viewport = {"width": 375, "height": 667}

            def fraction_point(self, x: float, y: float) -> list[int]:
                return [int(375 * x), int(667 * y)]

            async def tap(self, point: list[int]) -> None:
                taps.append(point)

        await gbl_ios.leave_to_league_list(FakeDevice())
        self.assertEqual(taps, [[39, 62]])


class IOSStalledCardTests(unittest.IsolatedAsyncioTestCase):
    """The iPhone's own recovery, which had never been run before the SE hit it.

    A set that ends back on the GBL card gets one turn of being left alone for
    the battle loop, and after that the card is refreshed through its close
    button.  That second turn called `logical_point` without the image it takes,
    so on 2026-09-09 the SE died of a TypeError there twice and stopped playing
    with the day unfinished.
    """

    def device(self):
        taps: list[list[int]] = []

        class FakeDevice:
            label = "Second iPhone (SE)"
            viewport = {"width": 375, "height": 667}

            def fraction_point(self, x: float, y: float) -> list[int]:
                return [int(375 * x), int(667 * y)]

            async def tap(self, point: list[int]) -> None:
                taps.append(point)

        return FakeDevice(), taps

    async def recover(self, attempt: int):
        device, taps = self.device()
        image = gbl.frame_image(as_frame(frame()))
        with (
            patch.object(gbl_ios.gbl_vision, "gbl_card_visible", return_value=True),
            patch.object(gbl_ios.android_gbl, "picker_screen", return_value=False),
            patch.object(gbl_ios.android_gbl, "picker_body_screen", return_value=False),
        ):
            await gbl_ios.recover_to_gbl(device, image, as_frame(frame()), [], attempt)
        return taps

    async def test_the_first_turn_leaves_the_card_for_the_battle_loop(self) -> None:
        self.assertEqual(await self.recover(1), [])

    async def test_a_stalled_card_is_refreshed_through_its_close_button(self) -> None:
        self.assertEqual(await self.recover(2), [[187, 626]])


class NamedLeagueTests(unittest.IsolatedAsyncioTestCase):
    """A configured league is the only card the iPhone is allowed to tap.

    The SE fits two league cards on screen.  With the OCR choice bypassed the
    run took the shape detector's bottom card instead and played a Master
    League team through whatever league was scrolled into view.
    """

    def league_image(self):
        f = frame()
        for i in range(2):
            put(f, 0.06, 0.94, 0.30 + i * 0.20, 0.44 + i * 0.20, WHITE)
        return gbl.frame_image(as_frame(f))

    def profile(self, preferred: str):
        return SimpleNamespace(
            settings=SimpleNamespace(preferred_league=preferred), team=None,
        )

    def card(self, text: str, card: int, height: int = 1600):
        top = height * (0.30 + card * 0.20)
        return gbl_vision.OCRBox(text, 1.0, 200, int(top) + 40, 300, 40)

    async def read(self, boxes, preferred: str):
        image = self.league_image()
        with patch("sources.gbl_ios.gbl_vision.recognize", return_value=boxes):
            return await gbl_ios.smart_screen_state(image, self.profile(preferred))

    async def test_named_league_is_taken_wherever_it_sits(self) -> None:
        boxes = [self.card("GREAT LEAGUE", 0), self.card("MASTER LEAGUE", 1)]
        state, point, label, _boxes = await self.read(boxes, "master league")
        self.assertEqual(state, "league")
        self.assertEqual(label, "MASTER LEAGUE")
        self.assertEqual(point, [boxes[1].center_x, boxes[1].center_y])

    async def test_a_split_label_still_counts_as_the_league(self) -> None:
        """Vision returns "MASTER" and "LEAGUE" as two observations often."""
        boxes = [self.card("GREAT LEAGUE", 0), self.card("MASTER", 1)]
        state, point, _label, _boxes = await self.read(boxes, "master league")
        self.assertEqual(state, "league")
        self.assertEqual(point, [boxes[1].center_x, boxes[1].center_y])

    async def test_the_plain_card_wins_over_the_classic_one(self) -> None:
        boxes = [self.card("MASTER LEAGUE CLASSIC", 0), self.card("MASTER LEAGUE", 1)]
        state, point, _label, _boxes = await self.read(boxes, "master league")
        self.assertEqual(state, "league")
        self.assertEqual(point, [boxes[1].center_x, boxes[1].center_y])

    async def test_missing_league_scrolls_instead_of_settling(self) -> None:
        boxes = [self.card("GREAT LEAGUE", 0), self.card("ULTRA LEAGUE", 1)]
        state, point, _label, _boxes = await self.read(boxes, "master league")
        self.assertEqual(state, "league_scroll")
        self.assertIsNone(point)

    async def test_auto_still_takes_the_easiest_card(self) -> None:
        boxes = [self.card("GREAT LEAGUE", 0), self.card("MASTER LEAGUE", 1)]
        state, point, label, _boxes = await self.read(boxes, "auto")
        self.assertEqual(state, "league")
        self.assertEqual(label, "GREAT LEAGUE")
        self.assertEqual(point, [boxes[0].center_x, boxes[0].center_y])


class IOSBattleStartingTests(unittest.IsolatedAsyncioTestCase):
    """The screen after "battle starting" is the battlefield, not a result.

    Live on 24 Aug the SE read the opening frame of a battle as a bare pill.
    The pill branch recomputed `armed` against it -- no party screen, no league
    list -- so the catch dropped, the frame was tapped as NEXT BATTLE, and the
    loop spent the rest of the fight in the unrecognised-screen limbo: thirty
    reads of map recovery pressing a BATTLE label that was not there, then the
    exit door.  Both times the battle it was standing in was lost without a
    move played.
    """

    @staticmethod
    def device():
        device = gbl_ios.IOSGBLDevice.__new__(gbl_ios.IOSGBLDevice)
        device.label = "Second iPhone (SE)"
        device.viewport = {"width": 375, "height": 667}
        device.config = gbl_ios.RuntimeConfig(
            appium_server_url="http://127.0.0.1:4723",
            ios_device={"name": "ios-two"},
            coordinates={"GBL_MOVE_BTN": [142, 391]},
            delay_modifier=0,
            battles=5,
        )
        device.tap = AsyncMock()
        device.trace_path = AsyncMock()
        return device

    @staticmethod
    def profile():
        return gbl_strategy.StrategyProfile(
            gbl_strategy.BattleTeam(
                (team_member('Noctowl'), team_member('Lanturn'),
                 team_member('Whiscash'))
            )
        )

    @staticmethod
    def boxes(*lines):
        return [gbl_vision.OCRBox(line, 1.0, 300, 200 + i * 40, 200, 30)
                for i, line in enumerate(lines)]

    # The misread: a pill the detector puts mid-screen, nowhere near the
    # party screen's own pill height.
    PILL = [270, 572]

    async def run_reads(self, reads):
        """Play one battle against a scripted sequence of screen reads."""
        from PIL import Image

        device = self.device()
        image = Image.new("RGB", (750, 1334), (60, 90, 50))
        messages: list[str] = []
        with (
            patch.object(device, "screenshot",
                         AsyncMock(side_effect=list(reads) and [image] * len(reads) + [None])),
            patch.object(gbl_ios, "smart_screen_state",
                         AsyncMock(side_effect=list(reads))),
            patch.object(gbl_ios, "prepare_best_party", AsyncMock(return_value=None)),
            patch.object(gbl_ios, "fast_attack", AsyncMock()),
            patch.object(gbl_ios, "wait", AsyncMock()),
            patch.object(gbl_ios.gbl_vision, "recognize", return_value=[]),
            patch.object(gbl_ios.gbl_home_recovery, "on_map", return_value=False),
            patch.object(gbl_ios.gbl_home_recovery, "main_menu_open",
                         return_value=False),
            patch.object(gbl_ios, "log",
                         lambda _device, message: messages.append(message)),
        ):
            await gbl_ios.play_device(device, 1, self.profile())
        taps = [call.args[0] for call in device.tap.await_args_list]
        return messages, taps

    @property
    def party_reads(self):
        """Two party screens: the first prepares the team, the second arms."""
        party = self.boxes("CHOOSE YOUR PARTY", "USE THIS PARTY")
        return [
            ("pill", [284, 1192], None, party),
            ("pill", [284, 1192], None, party),
        ]

    async def test_the_frame_after_matchmaking_is_not_tapped_as_a_result(self):
        messages, taps = await self.run_reads(self.party_reads + [
            ("battle", None, None, self.boxes("Battle starting")),
            ("pill", self.PILL, None, []),
        ])
        self.assertIn("  Battle starting; that screen is the battlefield, not a result",
                      messages)
        self.assertTrue(any("started, attacking" in line for line in messages))
        self.assertFalse(any("Result screen" in line for line in messages))
        # The stray tap on the battlefield, in logical space.
        self.assertNotIn([135, 286], taps)
        self.assertNotIn(self.PILL, taps)

    async def test_a_pill_with_no_matchmaking_behind_it_is_still_a_result(self):
        """The guard is narrow: only combat that was promised is assumed."""
        messages, _taps = await self.run_reads(self.party_reads + [
            ("pill", self.PILL, None, []),
        ])
        self.assertTrue(any("Result screen" in line for line in messages))
        self.assertNotIn("  Battle starting; that screen is the battlefield, not a result",
                         messages)

    async def test_a_league_list_after_matchmaking_is_still_a_league_list(self):
        """Matchmaking that fell back to the cards must not read as a battle."""
        cards = self.boxes("GREAT LEAGUE", "ULTRA LEAGUE")
        messages, _taps = await self.run_reads(self.party_reads + [
            ("battle", None, None, self.boxes("Battle starting")),
            ("league", [284, 600], "GREAT LEAGUE", cards),
        ])
        self.assertTrue(any("League list" in line for line in messages))
        self.assertNotIn("  Battle starting; that screen is the battlefield, not a result",
                         messages)


class IOSShieldPromptRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The iPhone half of ShieldPromptRecoveryTests.

    Android arms on the shield prompt because it is drawn nowhere but on a
    battlefield.  Without the same test here, a catch dropped on the way in
    left the SE sitting out the battle it was standing in, counting
    unrecognised reads and taking the exit door while the timer ran down.
    """

    async def test_a_shield_prompt_arms_a_loop_that_lost_its_catch(self) -> None:
        from PIL import Image

        device = IOSBattleStartingTests.device()
        image = Image.new("RGB", (750, 1334), (60, 90, 50))
        boxes = [gbl_vision.OCRBox("Attack incoming! Use a Protect Shield?",
                                   1.0, 200, 900, 350, 30),
                 gbl_vision.OCRBox("NOT NOW", 1.0, 200, 1000, 120, 30)]
        messages: list[str] = []
        with (
            patch.object(device, "screenshot", AsyncMock(side_effect=[image, None])),
            patch.object(gbl_ios, "smart_screen_state",
                         AsyncMock(return_value=("battle", None, None, boxes))),
            patch.object(gbl_ios.android_gbl, "shield_point",
                         return_value=[200, 700]),
            patch.object(gbl_ios, "wait", AsyncMock()),
            patch.object(gbl_ios, "log",
                         lambda _device, message: messages.append(message)),
        ):
            await gbl_ios.play_device(device, 1,
                                      IOSBattleStartingTests.profile())
        self.assertIn("  Shield prompt: this is a battlefield, arming the loop",
                      messages)
        # shield_point measures the screenshot; the tap belongs in logical space.
        self.assertEqual([call.args[0] for call in device.tap.await_args_list],
                         [[100, 350]])
        self.assertFalse(any("Unrecognised screen" in line for line in messages))


if __name__ == '__main__':
    unittest.main()


class IOSCatchSummaryTests(unittest.IsolatedAsyncioTestCase):
    """The XP card is the only ground truth for what a throw actually scored.

    Ring ratios say what was aimed at; the card says whether the game agreed,
    because it itemises the bonuses.  A catch that earned none reads
    "TOTAL 100 XP" and nothing else.
    """

    @staticmethod
    def device():
        from sources import excellent_throw_ios

        device = excellent_throw_ios.IOSExcellentThrowDevice.__new__(
            excellent_throw_ios.IOSExcellentThrowDevice
        )
        device.label = "test-se"
        device.viewport = (375, 667)
        device.screenshot = lambda: Image.new("RGB", (750, 1334), (20, 20, 20))
        device.tap = AsyncMock()
        return device

    @staticmethod
    def boxes(*texts: str):
        return [gbl_vision.OCRBox(text, 1.0, 300, 900, 120, 40) for text in texts]

    async def test_the_card_is_kept_and_both_screens_are_dismissed(self) -> None:
        from sources import excellent_throw_ios

        device = self.device()
        screens = [
            self.boxes("POKEMON CAUGHT", "TOTAL 100 XP", "OK"),
            self.boxes("Wimpod", "POWER UP"),
            self.boxes("MewTwoFTWAlIDay"),
        ]
        with (
            patch.object(excellent_throw_ios, "CATCH_MENU_SETTLE", 0.0),
            patch("sources.gbl_vision.recognize", side_effect=screens),
        ):
            with TemporaryDirectory() as tmp:
                await excellent_throw_ios.clear_catch_screens(device, Path(tmp))
                self.assertTrue((Path(tmp) / "catch-summary.png").exists())
        taps = [call.args[0] for call in device.tap.await_args_list]
        self.assertEqual(len(taps), 2)
        # The frame is 750x1334 and the session drives 375x667 points, so the
        # OK the OCR found has to be halved before it is tapped.
        self.assertEqual(taps[0], [180, 460])
        self.assertEqual(taps[1], [187, 630])


class RingLockReachableTests(unittest.TestCase):
    """`lock_excellent_ring` has to be reachable from the command line.

    The run loop pinned `timed_mode=True`, so every iOS throw was the
    single-shot timing sweep -- pause 1.29s, then 1.47s, then 1.65s -- and the
    measured release into the Excellent band never ran at all.  Four tall_device catch
    cards and three SE throws later, that was the whole reason no Excellent had
    ever been scored.
    """

    def test_the_default_is_still_the_single_shot_sweep(self) -> None:
        from sources import excellent_throw_ios

        self.assertFalse(excellent_throw_ios.parse_args([]).ring_lock)

    def test_ring_lock_turns_the_timing_sweep_off(self) -> None:
        from sources import excellent_throw_ios

        self.assertTrue(excellent_throw_ios.parse_args(["--ring-lock"]).ring_lock)


class FastAttackShareTests(unittest.IsolatedAsyncioTestCase):
    """Most of a burst has to be fast moves.

    `fast_attack` round-robined the battlefield point with every charged disc,
    and `charged_move_points` returns three, so one tap in four was a fast move:
    the tall_device's 14-tap batch bought three quick attacks and eleven taps at
    buttons that are dead until the energy those quick attacks generate arrives.
    """

    async def sent(self, count: int, charged) -> list[str]:
        shell = AsyncMock()
        device = type("FakeDevice", (), {"label": "tall_device"})()
        device.shell = shell
        device.fast_attack_batch_budget = gbl.FAST_MOVE_BATCH_BUDGET
        device.fast_attack_taps_per_batch = count
        device.input_cost = 0.035
        await gbl.fast_attack(device, [100, 200], charged)
        return shell.await_args.args[0].split("; ")

    async def test_most_taps_are_the_fast_move(self) -> None:
        discs = [[10, 900], [20, 900], [30, 900]]
        taps = await self.sent(14, discs)
        fast = [tap for tap in taps if tap == "input tap 100 200"]
        self.assertEqual(len(taps), 14)
        # Three probes, one per disc; the rest build energy.
        self.assertEqual(len(fast), 11)

    async def test_every_charged_disc_is_still_probed(self) -> None:
        discs = [[10, 900], [20, 900], [30, 900]]
        taps = await self.sent(14, discs)
        for disc in discs:
            self.assertIn(f"input tap {disc[0]} {disc[1]}", taps)

    async def test_a_burst_with_no_charged_move_is_all_fast(self) -> None:
        taps = await self.sent(14, None)
        self.assertEqual(taps, ["input tap 100 200"] * 14)


class SlowPhoneBurstTests(unittest.IsolatedAsyncioTestCase):
    """A phone where the JVM start is the price taps its way around it.

    Every `input` on the android-one is a zygote fork against a phone with its swap
    entirely full, so the start is spent faulting pages off flash: 712ms each,
    which bought two taps per batch and left a visible stare between them.
    Waiting on flash overlaps, though -- eight chained `input` measured 5524ms
    and 1314ms backgrounded -- so the launches go out concurrently and the
    stagger, not the start, decides how far apart the events land.
    """

    def device(self, cost: float, label: str = 'android-one'):
        device = SimpleNamespace(label=label, input_cost=cost)
        device.shell = AsyncMock()
        return device

    async def sent(self, cost: float) -> str:
        device = self.device(cost)
        device.fast_attack_batch_budget = gbl.MOVE_BATCH_BUDGET
        device.fast_attack_taps_per_batch = gbl.MOVE_TAPS_PER_BATCH
        await gbl.fast_attack(device, [100, 200])
        return device.shell.await_args.args[0]

    async def test_a_slow_phone_launches_its_taps_concurrently(self) -> None:
        command = await self.sent(0.713)
        self.assertIn(f' & sleep {gbl.PARALLEL_TAP_STAGGER:g}; ', command)
        # The trailing wait is what keeps the burst a bounded window of
        # blindness: without it the call returns while taps are still pending.
        self.assertTrue(command.endswith(' & wait'), command)

    async def test_the_taps_still_go_out_in_order(self) -> None:
        command = await self.sent(0.713)
        self.assertEqual(
            [part for part in command.split() if part.isdigit()][::2],
            ['100'] * command.count('input tap'),
        )

    async def test_a_slow_phone_now_fits_more_taps_in_the_same_window(self) -> None:
        # The point of the change in one assertion. Same budget either way; it
        # is the interval the budget is divided by that moves.
        chained = gbl.batch_size(
            0.713, gbl.MOVE_BATCH_BUDGET, gbl.MOVE_TAPS_PER_BATCH,
            gbl.MOVE_TAPS_MINIMUM,
        )
        overlapped = gbl.batch_size(
            gbl.tap_interval(self.device(0.713)), gbl.MOVE_BATCH_BUDGET,
            gbl.MOVE_TAPS_PER_BATCH, gbl.MOVE_TAPS_MINIMUM,
        )
        self.assertEqual(chained, 2)
        self.assertGreater(overlapped, chained)
        command = await self.sent(0.713)
        self.assertEqual(command.count('input tap'), overlapped)

    async def test_a_fast_phone_still_chains_its_burst(self) -> None:
        # On the tall_device a start is cheaper than the stagger, so chaining already
        # spaces the taps better than backgrounding them would.
        device = self.device(0.035, 'tall_device')
        device.fast_attack_batch_budget = gbl.FAST_MOVE_BATCH_BUDGET
        device.fast_attack_taps_per_batch = gbl.FAST_MOVE_TAPS_PER_BATCH
        await gbl.fast_attack(device, [100, 200])
        command = device.shell.await_args.args[0]
        self.assertNotIn('&', command)
        self.assertEqual(
            command.count('input tap'), gbl.FAST_MOVE_TAPS_PER_BATCH)

    def test_the_boundary_measurement_is_backgrounded(self) -> None:
        self.assertTrue(gbl.parallel_taps(self.device(gbl.PARALLEL_INPUT_MIN)))
        self.assertFalse(
            gbl.parallel_taps(self.device(gbl.PARALLEL_INPUT_MIN - 0.001)))

    def test_an_unmeasured_phone_is_assumed_fast(self) -> None:
        # ppadb hands back a plain DeviceAsync, so a device that never met
        # `prepare_device_timing` has no cost at all; it must not be given a
        # profile that assumes one.
        self.assertFalse(gbl.parallel_taps(SimpleNamespace(label='new')))


class SlowPhoneMinigameTests(unittest.IsolatedAsyncioTestCase):
    """The bubble field on a phone where the gesture start is the price.

    The trimmer had cut the android-one to a single pass: 738ms a start plus a 50ms
    flick is 0.79s a gesture against a 0.75s per-sweep budget, so nine flicks
    were spread over 7.1s and the finger was on the glass for 0.45s of the
    window. The tall_device and the moto g get eight sweeps in the same window, which
    is exactly the difference a watcher sees between them.
    """

    def device(self, cost: float, label: str = 'android-one'):
        device = SimpleNamespace(label=label, input_cost=cost)
        device.shell = AsyncMock()
        return device

    async def swept(self, cost: float, label: str = 'android-one') -> str:
        device = self.device(cost, label)
        with patch.object(gbl, 'measure_input_cost', AsyncMock(return_value=cost)):
            await gbl.prepare_device_timing(device)
        device.shell.reset_mock()
        with patch.object(gbl, 'wait', AsyncMock()):
            await gbl.charged_minigame(device, as_frame(frame(1316, 2560)))
        return device.shell.await_args.args[0]

    async def test_a_slow_phone_drags_instead_of_flicking(self) -> None:
        # The start is paid either way, so the phone that pays most for one
        # should get the most playfield out of it.
        command = await self.swept(0.738)
        self.assertIn(f' {gbl.SLOW_MINIGAME_SWIPE_MS}', command)
        self.assertGreater(gbl.SLOW_MINIGAME_SWIPE_MS, gbl.MINIGAME_SWIPE_MS)

    async def test_a_slow_phone_backgrounds_its_gestures(self) -> None:
        command = await self.swept(0.738)
        self.assertIn(f' & sleep {gbl.PARALLEL_SWIPE_STAGGER:g}; ', command)
        self.assertTrue(command.endswith(' & wait'), command)

    async def test_a_slow_phone_now_gets_more_than_one_pass(self) -> None:
        # The point of the change in one assertion.
        command = await self.swept(0.738)
        gestures = command.count('input swipe')
        chained = gbl.batch_size(
            0.738 + gbl.MINIGAME_SWIPE_MS / 1000,
            gbl.MINIGAME_BUDGET / gbl.MINIGAME_GESTURES_PER_SWEEP,
            gbl.MINIGAME_SWEEPS,
        )
        self.assertEqual(chained, 1)
        self.assertGreater(gestures, gbl.MINIGAME_GESTURES_PER_SWEEP * chained)

    async def test_the_slow_sweep_still_fits_the_bubble_window(self) -> None:
        # The trimmer exists because a sweep that overruns leaves the loop
        # somewhere else entirely; backgrounding must not smuggle that back in.
        command = await self.swept(0.738)
        gestures = command.count('input swipe')
        interval = gbl.gesture_interval(
            self.device(0.738), gbl.SLOW_MINIGAME_SWIPE_MS)
        self.assertLessEqual(gestures * interval, gbl.MINIGAME_BUDGET + 1.0)

    async def test_the_drags_do_not_overlap_each_other(self) -> None:
        # Two drags injected at once are a pinch, not two drags. What spaces
        # them is the phone's sustained launch rate, not the shell's stagger.
        interval = gbl.gesture_interval(
            self.device(0.738), gbl.SLOW_MINIGAME_SWIPE_MS)
        self.assertGreaterEqual(interval, gbl.SLOW_MINIGAME_SWIPE_MS / 1000)

    async def test_a_fast_phone_still_chains_short_flicks(self) -> None:
        command = await self.swept(0.035, 'tall_device')
        self.assertNotIn('&', command)
        self.assertIn(f' {gbl.FAST_MINIGAME_SWIPE_MS}', command)
        self.assertEqual(
            command.count('input swipe'),
            gbl.FAST_MINIGAME_SWEEPS * gbl.FAST_MINIGAME_GESTURES_PER_SWEEP,
        )


class ChargedDiscBandTests(unittest.IsolatedAsyncioTestCase):
    """Reading the button row alone, which is what makes re-reading it cheap.

    The bill for a screencap is the USB transfer of a 13-15MB frame, so a
    question about one band is answered for a tenth of it: 2.3MB and ~0.6s on
    the android-one against 13.5MB and ~1.3s.
    """

    def lit(self, width=1316, height=2560, columns=(0,)):
        f = frame(width=width, height=height, background=(35, 60, 40))
        for column in columns:
            charged_disc(f, gbl.CHARGED_DISC_CX[column])
        return as_frame(f)

    def band_of(self, full):
        width, height, offset, data = full
        y0, y1 = gbl.charged_disc_band(full)
        return (width, height, offset - y0 * width * 4,
                data[offset + y0 * width * 4:offset + y1 * width * 4])

    def test_the_band_reads_the_same_buttons_as_the_whole_frame(self) -> None:
        for phone in ((1316, 2560), (720, 1600), (1224, 2992)):
            with self.subTest(phone=phone):
                full = self.lit(*phone, columns=(0, 1))
                self.assertEqual(
                    gbl.ready_charged_discs(self.band_of(full)),
                    gbl.ready_charged_discs(full),
                )

    def test_the_band_is_a_small_part_of_the_frame(self) -> None:
        full = self.lit()
        y0, y1 = gbl.charged_disc_band(full)
        self.assertLess((y1 - y0) / full[1], 0.2)

    def test_the_band_covers_everything_the_rim_test_samples(self) -> None:
        # A band a row short reads another row's bytes rather than failing, so
        # the reach is pinned rather than trusted.
        full = self.lit()
        y0, y1 = gbl.charged_disc_band(full)
        centre = full[1] * gbl.charged_disc_centre_y(full)
        reach = (full[0] * gbl.CHARGED_DISC_RADIUS * gbl.CHARGED_RIM_OUTSIDE
                 + full[1] * max(abs(s) for s in gbl.CHARGED_DISC_CY_SEARCH))
        self.assertLess(y0, centre - reach)
        self.assertGreater(y1, centre + reach)

    async def test_the_fresh_read_pulls_only_that_band(self) -> None:
        full = self.lit()
        y0, y1 = gbl.charged_disc_band(full)
        device = SimpleNamespace(label='android-one', serial='COMPACT', display_id=None)
        band = self.band_of(full)
        with patch.object(gbl, 'screencap_band',
                          AsyncMock(return_value=band)) as read:
            self.assertEqual(
                await gbl.fresh_charged_discs(device, full),
                [gbl.CHARGED_DISC_CX[0]],
            )
        read.assert_awaited_once_with(device, full, y0, y1)

    async def test_a_failed_band_read_is_not_a_verdict(self) -> None:
        # None means "could not tell", and the caller goes ahead on the stale
        # frame rather than skipping a charged move it can see.
        full = self.lit()
        with patch.object(gbl, 'screencap_band', AsyncMock(return_value=None)):
            self.assertIsNone(await gbl.fresh_charged_discs(object(), full))


class ChargedStaleFrameTests(unittest.IsolatedAsyncioTestCase):
    """The move the loop is about to launch may already have gone off.

    The frame the battle loop decides on is started alongside the tap burst and
    then has two or three OCR passes run over it, and the burst taps the disc
    centres itself. Across one traced morning that combination gave 22 lit
    buttons and one verified launch on the tall_device, and 22 and two on the android-one:
    each miss then spent its whole confirmation budget tapping a spent button,
    which is ten to fifteen seconds of a battle standing still.
    """

    async def run_loop(self, stale, fresh):
        reads = 0
        stop = RuntimeError('second burst')
        boxes = [gbl_vision.OCRBox('Attack incoming! Use a Protect Shield?',
                                   1.0, 0, 0, 300, 20)]
        f = as_frame(frame(1316, 2560))

        async def fake_read(_device):
            nonlocal reads
            reads += 1
            return f

        async def fake_attack(*_args, **_kwargs):
            raise stop

        launch = AsyncMock(side_effect=stop)
        device = SimpleNamespace(config={'GBL_MOVE_BTN': [658, 1200]},
                                 label='android-one')
        profile = gbl_strategy.StrategyProfile(
            gbl_strategy.BattleTeam(
                (team_member('Noctowl'), team_member('Lanturn'),
                 team_member('Whiscash'))
            )
        )
        with patch.object(gbl, 'read_screen', fake_read), \
                patch.object(gbl, 'smart_screen_state',
                             AsyncMock(return_value=('battle', None, None, boxes))), \
                patch.object(gbl, 'recognized_shield_point',
                             AsyncMock(return_value=None)), \
                patch.object(gbl, 'shield_prompt_text',
                             lambda _boxes: reads == 1), \
                patch.object(gbl, 'charged_prompt_from_frame',
                             AsyncMock(return_value=None)), \
                patch.object(gbl, 'ready_charged_discs', lambda _f: list(stale)), \
                patch.object(gbl, 'fresh_charged_discs',
                             AsyncMock(return_value=fresh)), \
                patch.object(gbl, 'launch_charged_move', launch), \
                patch.object(gbl, 'probe_charged_after_fast_attacks',
                             AsyncMock(return_value=False)), \
                patch.object(gbl, 'fast_attack', fake_attack), \
                patch.object(gbl, 'tap', AsyncMock()), \
                patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl, 'recognize_battle_context',
                             AsyncMock(return_value=(None, False))), \
                patch.object(gbl_strategy, 'load_strategy_profile',
                             lambda **kwargs: profile):
            with self.assertRaises(RuntimeError):
                await gbl.play_device(device, 1)
        return launch

    async def test_a_button_the_burst_already_spent_is_not_launched(self) -> None:
        launch = await self.run_loop([gbl.CHARGED_DISC_CX[1]], [])
        launch.assert_not_awaited()

    async def test_a_button_still_lit_is_launched_at_that_column(self) -> None:
        column = gbl.CHARGED_DISC_CX[1]
        # The fresh read decides the column too: the stale frame named another.
        launch = await self.run_loop([gbl.CHARGED_DISC_CX[0]], [column])
        self.assertEqual(launch.await_args.kwargs['column'], column)
        self.assertEqual(
            launch.await_args.kwargs['target_point'][0], int(1316 * column))

    async def test_an_unreadable_band_falls_back_to_the_stale_frame(self) -> None:
        # None means "could not tell", and a charged move the loop can see is
        # worth more than the cost of one wasted confirmation.
        column = gbl.CHARGED_DISC_CX[0]
        launch = await self.run_loop([column], None)
        self.assertEqual(launch.await_args.kwargs['column'], column)


class ChargedLaunchWitnessTests(unittest.IsolatedAsyncioTestCase):
    """The button going dark is the same fact as GET READY, read off the button.

    GET READY is up for about a second -- one frame in sixty at a read a second
    on the tall_device -- and a android-one screencap plus its OCR is longer than that, so a
    launch that lands is routinely reported as a miss and paid for again.
    """

    def frames(self, columns):
        f = frame(1316, 2560, background=(35, 60, 40))
        for column in columns:
            charged_disc(f, gbl.CHARGED_DISC_CX[column])
        return as_frame(f)

    async def launch(self, after, column=gbl.CHARGED_DISC_CX[1]):
        tap_mock = AsyncMock()
        with (
            patch.object(gbl, 'tap', tap_mock),
            patch.object(gbl, 'wait', AsyncMock()),
            patch.object(gbl, 'read_charged_prompt',
                         AsyncMock(side_effect=[(None, f) for f in after])),
        ):
            result = await gbl.launch_charged_move(
                object(), after[0], target_point=[908, 2110], column=column)
        return result, tap_mock

    async def test_the_tapped_button_going_dark_counts_as_a_launch(self) -> None:
        lit = self.frames((0, 1))
        spent = self.frames((0,))
        (launched, _f, attempts, label), tap_mock = await self.launch([lit, spent])
        self.assertTrue(launched)
        self.assertIsNone(label)
        self.assertEqual(attempts, 1)
        self.assertEqual(tap_mock.await_count, 1)

    async def test_the_other_button_going_dark_does_not(self) -> None:
        # Only the column that was tapped is evidence about the tap.
        lit = self.frames((0, 1))
        (launched, _f, _a, _l), _tap = await self.launch(
            [lit, self.frames((1,))] * gbl.CHARGED_LAUNCH_ATTEMPTS
            * gbl.CHARGED_LAUNCH_POLLS)
        self.assertFalse(launched)

    async def test_a_shield_prompt_hiding_the_buttons_is_not_a_launch(self) -> None:
        # A prompt covers the button row and swallows the tap meant for it, so
        # the dark button means the move did not go out rather than that it did.
        lit = self.frames((0, 1))
        prompt = frame(1316, 2560, background=(35, 60, 40))
        charged_disc(prompt, gbl.CHARGED_DISC_CX[0])
        put(prompt, 0.395, 0.612, 0.712, 0.826, WHITE)
        covered = as_frame(prompt)
        self.assertIsNotNone(gbl.shield_candidate_point(covered))
        (launched, _f, _a, _l), _tap = await self.launch(
            [lit, covered] * gbl.CHARGED_LAUNCH_ATTEMPTS * gbl.CHARGED_LAUNCH_POLLS)
        self.assertFalse(launched)

    def test_the_confirmation_budget_stays_short(self) -> None:
        # Each poll is a whole frame and an OCR pass, about 1.5s on the android-one.
        self.assertLessEqual(
            gbl.CHARGED_LAUNCH_ATTEMPTS * gbl.CHARGED_LAUNCH_POLLS, 4)


class PrefetchedReadTests(unittest.IsolatedAsyncioTestCase):
    """The read runs alongside the burst instead of after it.

    A burst and the screencap that followed it cost about the same -- 1.4s and
    0.95s on the android-one, 0.6s and 1.2s on the tall_device -- and the phone tapped
    nothing for the whole of the read. That stare is half of every cycle.
    """

    async def play(self, probe_launched: bool = False) -> list[str]:
        events: list[str] = []
        stop = RuntimeError('second burst')
        boxes = [gbl_vision.OCRBox('Attack incoming! Use a Protect Shield?',
                                   1.0, 0, 0, 300, 20)]

        async def fake_read(_device):
            events.append('read')
            await asyncio.sleep(0)
            return as_frame(frame())

        async def fake_attack(*_args, **_kwargs):
            events.append('burst')
            # The prefetch is a task, so it cannot start until the burst gives
            # the loop back; a burst is a shell round trip and always does.
            await asyncio.sleep(0)
            events.append('burst done')
            if events.count('burst') > 1:
                raise stop

        device = SimpleNamespace(config={'GBL_MOVE_BTN': [360, 800]},
                                 label='android-one')
        profile = gbl_strategy.StrategyProfile(
            gbl_strategy.BattleTeam(
                (team_member('Noctowl'), team_member('Lanturn'),
                 team_member('Whiscash'))
            )
        )
        with patch.object(gbl, 'read_screen', fake_read), \
                patch.object(gbl_vision, 'recognize', return_value=boxes), \
                patch.object(gbl, 'smart_screen_state',
                             AsyncMock(return_value=('battle', None, None, boxes))), \
                patch.object(gbl, 'recognized_shield_point',
                             AsyncMock(return_value=None)), \
                patch.object(gbl, 'shield_prompt_text',
                             lambda _boxes: events.count('read') == 1), \
                patch.object(gbl, 'probe_charged_after_fast_attacks',
                             AsyncMock(return_value=probe_launched)), \
                patch.object(gbl, 'fast_attack', fake_attack), \
                patch.object(gbl, 'tap', AsyncMock()), \
                patch.object(gbl, 'wait', AsyncMock()), \
                patch.object(gbl, 'recognize_battle_context',
                             AsyncMock(return_value=(None, False))), \
                patch.object(gbl_strategy, 'load_strategy_profile',
                             lambda **kwargs: profile):
            with self.assertRaises(RuntimeError):
                await gbl.play_device(device, 1)
        return events

    async def test_the_read_is_started_before_the_burst_finishes(self) -> None:
        events = await self.play()
        first = events.index('burst')
        during = events[first:events.index('burst done', first)]
        self.assertIn('read', during)

    async def test_the_frame_it_fetched_is_the_one_the_loop_uses(self) -> None:
        # One read per cycle still, not two: nothing is read between the end of
        # a burst and the start of the next, because the prefetched frame is
        # what the top of the loop takes.
        events = await self.play()
        self.assertEqual(self.between_cycles(events), [])

    async def test_a_minigame_throws_the_prefetched_frame_away(self) -> None:
        # A swiped minigame leaves nothing of the screen behind that read, so
        # the next cycle has to look again rather than act on it.
        events = await self.play(probe_launched=True)
        self.assertEqual(self.between_cycles(events), ['read'])

    @staticmethod
    def between_cycles(events: list[str]) -> list[str]:
        """What happens after a burst ends and before the next one starts."""
        done = events.index('burst done')
        return [event for event in events[done + 1:events.index('burst', done)]
                if event == 'read']


class StalledActionRecoveryTests(unittest.TestCase):
    """A stalled tap gets the battle screen reopened whatever named it.

    The stop used to sit after a `continue` inside the `state == 'league'`
    branch, so it could never run and neither could recovery for any other
    state: on 2026-08-31 the tall_device sat tapping an action button at [465, 2157]
    that did nothing, and no counter in the loop ever acted on it.
    """

    def test_the_recovery_budget_is_bounded(self) -> None:
        self.assertGreater(gbl.STALL_RECOVERY_LIMIT, 0)

    def test_no_statement_in_play_device_sits_after_a_jump(self) -> None:
        import ast

        tree = ast.parse(Path(gbl.__file__).read_text())
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "play_device"
        )
        dead: list[int] = []
        for node in ast.walk(function):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for index, statement in enumerate(block[:-1]):
                    if isinstance(statement, (ast.Continue, ast.Break, ast.Return)):
                        dead.append(block[index + 1].lineno)
        self.assertEqual(dead, [], f"unreachable statements at lines {dead}")


class CardBattlePillTests(unittest.TestCase):
    """The GBL card's own BATTLE pill has to be findable.

    With the reward ribbon, the season panel and the NEARBY BATTLE card drawn on
    it, the moto's pill measured 0.337 of screen height -- under the old 0.35
    cut -- so `action_point` returned None on a live button. The loop then met
    the NEARBY BATTLE panel's teal X, correctly refused it as the card's close
    button, and read the same screen 253 times.
    """

    def boxes(self, *labels):
        return [
            SimpleNamespace(text=text, center_x=x, center_y=y)
            for text, x, y in labels
        ]

    def test_the_cards_pill_is_found_at_its_measured_height(self) -> None:
        found = gbl_vision.action_point(
            self.boxes(("BATTLE", 361, 539)), 720, 1600)
        self.assertIsNotNone(found)
        self.assertEqual(found[1], "BATTLE")

    def test_the_top_tab_is_still_rejected(self) -> None:
        # Measured on the same frame: the tab is high *and* off-centre, so it
        # fails on either test alone.
        self.assertIsNone(
            gbl_vision.action_point(self.boxes(("BATTLE", 213, 163)), 720, 1600))

    def test_the_cut_still_sits_above_the_tab(self) -> None:
        self.assertLess(gbl_vision.ACTION_MIN_Y, 0.337)
        self.assertGreater(gbl_vision.ACTION_MIN_Y, 0.102)


class CardDiscEscapeTests(unittest.TestCase):
    """Refusing the card's close disc has to be able to escalate."""

    def test_the_refusal_is_bounded(self) -> None:
        self.assertGreater(gbl.CARD_DISC_LIMIT, 0)

    def test_the_branch_reaches_for_the_action_point(self) -> None:
        source = Path(gbl.__file__).read_text()
        start = source.index("if gbl_vision.gbl_card_visible(result_boxes):\n"
                             "                card_disc_reads")
        branch = source[start:start + 900]
        self.assertIn("gbl_vision.action_point", branch)
        self.assertIn("CARD_DISC_LIMIT", branch)


# Sampled off the SE's stuck frame on 2026-08-31.
SEA = (67, 132, 137)        # the map showing below a full-screen card
DISC = (28, 134, 148)       # the weekly report's close X
SYSTEM_BAR = (51, 51, 51)   # the Moto's navigation bar, sampled at 360,1512


def weekly_report_frame(width: int = 750, height: int = 1334):
    """The weekly report as the SE drew it: card, close disc, sea beneath."""
    f = frame(width, height, background=SEA)
    put(f, 0.05, 0.95, 0.07, 0.92, WHITE)
    put(f, 0.45, 0.55, 0.839, 0.894, DISC)
    return f


class WeeklyReportTests(unittest.TestCase):
    """The weekly adventure-sync report has to be named and closed.

    On 2026-08-31 the SE sat on it for 151 reads: no pill test answered, its
    white slab counted as a league card, and the sea left showing below it put
    two samples into the teal grid -- so the loop dismissed a post-battle
    checkmark that was not there, tapping 0.938h into the map, and played no
    battles all morning.
    """

    def boxes(self, *labels):
        return [gbl_vision.OCRBox(text, 1.0, 100, 100, 200, 30) for text in labels]

    def test_named_from_its_headline(self) -> None:
        self.assertTrue(gbl.weekly_report_visible(
            self.boxes("You walked 13.4", "km last week!", "REWARDS")))

    def test_named_from_its_stat_rows(self) -> None:
        """The headline carries a distance OCR splits unpredictably."""
        self.assertTrue(gbl.weekly_report_visible(
            self.boxes("5 Eggs Hatched", "18,067 Steps", "O Calories")))

    def test_the_gbl_card_is_not_the_report(self) -> None:
        self.assertFalse(gbl.weekly_report_visible(
            self.boxes("GO BATTLE LEAGUE", "BATTLE", "5/5 battles played")))

    def test_the_close_disc_is_found(self) -> None:
        found = gbl.weekly_report_close_point(as_frame(weekly_report_frame()))
        self.assertIsNotNone(found)
        self.assertEqual(found[0], 375)
        self.assertAlmostEqual(found[1] / 1334, 0.8666, places=2)

    def test_the_sea_below_the_card_is_not_the_disc(self) -> None:
        """Both are teal; only the sea reaches the bottom edge."""
        f = frame(750, 1334, background=SEA)
        put(f, 0.05, 0.95, 0.07, 0.92, WHITE)
        self.assertIsNone(gbl.weekly_report_close_point(as_frame(f)))

    def test_that_sea_is_what_reads_as_a_checkmark(self) -> None:
        """The false positive this branch exists to get in front of."""
        f = as_frame(weekly_report_frame())
        self.assertTrue(gbl.teal_result_ready(
            f, gbl_ios.TEAL_SAMPLE_X, gbl_ios.TEAL_SAMPLE_Y))

    def test_the_report_is_answered_before_the_checkmark(self) -> None:
        source = Path(gbl_ios.__file__).read_text()
        self.assertLess(
            source.index("weekly_report_visible"),
            source.index("teal_result_ready(frame, TEAL_SAMPLE_X"),
        )


class PokemonDetailPageTests(unittest.TestCase):
    """A caught Pokemon's own page has to be named and closed.

    On 2026-09-06 the SE's reward encounter left the game on the Stunfisk it
    had just caught. That page carries "CAUGHT IN THE GBL MASTER LEAGUE: MEGA
    EDITION", which contains both a league and a cup, so the league test
    claimed it: "League list, taking Master League" every five seconds, tapping
    a move list, until the page was closed by hand.
    """

    def boxes(self, *labels):
        return [gbl_vision.OCRBox(text, 1.0, 100, 100, 200, 30) for text in labels]

    def test_named_from_its_headings(self) -> None:
        self.assertTrue(gbl_vision.pokemon_detail_visible(self.boxes(
            "POWER UP", "STARDUST", "STUNFISK CANDY",
            "CAUGHT IN THE GBL MASTER LEAGUE:", "MEGA EDITION")))

    def test_named_from_its_footer(self) -> None:
        self.assertTrue(gbl_vision.pokemon_detail_visible(self.boxes(
            "POWER UP", "SWAP BUDDIES", "GYMS & RAIDS", "TRAINER BATTLES")))

    def test_a_bare_power_up_pill_is_not_the_page(self) -> None:
        """POWER UP is printed on other cards, and they are not this one."""
        self.assertFalse(gbl_vision.pokemon_detail_visible(
            self.boxes("POWER UP", "GO BATTLE LEAGUE", "BATTLE")))

    def test_the_league_list_is_not_the_page(self) -> None:
        self.assertFalse(gbl_vision.pokemon_detail_visible(
            self.boxes("CHOOSE YOUR LEAGUE", "Master League", "Great League")))

    def test_the_page_is_answered_before_the_league_test(self) -> None:
        source = Path(gbl_ios.__file__).read_text()
        self.assertLess(
            source.index("pokemon_detail_visible(menu_boxes)"),
            source.index("  League list, taking "),
        )


class TealDismissalTests(unittest.TestCase):
    """Dismissing a checkmark has to give up if the checkmark never clears."""

    def test_the_budget_is_bounded(self) -> None:
        self.assertGreater(gbl_ios.TEAL_DISMISS_LIMIT, 0)

    def test_the_branch_counts_and_resets(self) -> None:
        source = Path(gbl_ios.__file__).read_text()
        start = source.index(
            "if not android_gbl.teal_result_ready(frame, TEAL_SAMPLE_X")
        branch = source[start:start + 1600]
        self.assertIn("teal_reads = 0", branch)
        self.assertIn("teal_reads >= TEAL_DISMISS_LIMIT", branch)
        self.assertIn("teal_reads += 1", branch)


class RankModalTests(unittest.TestCase):
    """The rank roster sheet has to be named and shut.

    On 2026-08-31 the tall_device sat under it and read the card straight through it:
    BATTLE was still drawn, still green and still the right shape, so the pill
    test answered and the leg tapped a dimmed button 36 times, spent all three
    flow refreshes on a screen it had never actually left, and stopped with 32
    of the day's battles unplayed.
    """

    def boxes(self, *labels):
        return [gbl_vision.OCRBox(text, 1.0, 100, 100, 200, 30) for text in labels]

    def test_named_from_its_headline(self) -> None:
        self.assertTrue(gbl.rank_modal_visible(
            self.boxes("Pokemon available at your rank:", "Wins: 249")))

    def test_named_from_its_footer(self) -> None:
        """OCR splits the accented headline; the footer line does not move."""
        self.assertTrue(gbl.rank_modal_visible(
            self.boxes("Keep winning to unlock more", "Pokemon!")))

    def test_the_card_underneath_is_not_the_sheet(self) -> None:
        self.assertFalse(gbl.rank_modal_visible(
            self.boxes("GO BATTLE LEAGUE", "BATTLE", "Battle entry good for 5 rounds.")))

    def test_close_point_is_where_the_tap_cleared_it(self) -> None:
        """Measured on the tall_device: 612,2827 on a 1224x2992 frame."""
        found = gbl.rank_modal_close_point(as_frame(frame(1224, 2992)))
        self.assertEqual(found, [612, 2827])

    def test_the_measured_point_wins_where_it_is_on_the_disc(self) -> None:
        """The tall_device goes on tapping the point that has always worked there."""
        f = frame(1224, 2992)
        put(f, 0.45, 0.55, 0.930, 0.960, DISC)
        self.assertEqual(gbl.rank_modal_close_point(as_frame(f)), [612, 2827])

    def test_the_disc_is_found_where_the_fraction_is_off_it(self) -> None:
        """The tall_device's fraction lands on the Moto's navigation bar.

        On a 720x1600 screen 0.945 is 1512, which reads a flat (51, 51, 51)
        while the disc it is aiming for sits at 1426.  Live on 2026-09-01 the
        Moto sat on this sheet logging that it was closing it, and not one of
        those taps landed on the disc.
        """
        f = frame(720, 1600)
        put(f, 0.0, 1.0, 0.930, 1.0, SYSTEM_BAR)
        put(f, 0.45, 0.55, 0.869, 0.915, DISC)
        self.assertEqual(gbl.rank_modal_close_point(as_frame(f)), [360, 1426])

    def test_a_frame_with_no_disc_still_gets_the_measured_point(self) -> None:
        """No disc found is not a reason to return nothing to tap."""
        f = frame(720, 1600)
        put(f, 0.0, 1.0, 0.930, 1.0, SYSTEM_BAR)
        self.assertEqual(gbl.rank_modal_close_point(as_frame(f)), [360, 1512])

    def test_the_sheet_is_answered_before_the_pill(self) -> None:
        source = Path(gbl.__file__).read_text()
        self.assertLess(
            source.index("rank_modal_visible(menu_boxes)"),
            source.index("here = (state, point[0], point[1])"),
        )

    def test_an_armed_leg_waits_out_an_unreadable_frame(self) -> None:
        """The frames between "battle starting" and the first vouched-for
        battlefield match nothing, and recovering off one taps the map menu at
        a phone that is already fighting."""
        source = Path(gbl.__file__).read_text()
        self.assertIn("if (attacking or armed) and stray < STRAY_SETTLE:", source)


class RewardThrowTests(unittest.IsolatedAsyncioTestCase):
    """The reward catch is thrown by `excellent_throw.py`, berry and all.

    It used to be a second implementation of throwing that borrowed a few of
    that routine's helpers and reinvented the rest, and it inherited none of
    the tuning.  Two faults came of it and both were reported live: it never
    fed a Nanab on either platform, and on 2026-09-01/02 it threw a fixed
    length at every distance, so the tall_device's balls sailed over the Pokemon and
    the android-one's fell short of a Regirock while the operator caught both by hand.
    """

    def android(self):
        return SimpleNamespace(
            label="test-android",
            serial="TEST",
            shell=AsyncMock(),
            config={},
            display_id=None,
        )

    async def throw(self, results):
        device = self.android()
        run_once = AsyncMock(side_effect=results)
        with (
            patch.object(gbl, "frame_image", MagicMock(
                return_value=Image.new("RGB", (1224, 2992)))),
            patch.object(gbl.excellent_throw_android, "run_once", run_once),
        ):
            ended = await gbl.throw_ball(device, object())
        return ended, run_once

    async def test_the_throw_is_the_routine_that_is_tuned_for_catching(self) -> None:
        ended, run_once = await self.throw([True])
        self.assertTrue(ended)
        self.assertEqual(run_once.await_count, 1)
        self.assertIs(run_once.await_args.kwargs["ring_hold"], True)
        self.assertIs(run_once.await_args.kwargs["dry_run"], False)

    async def test_every_throw_is_fed_a_nanab(self) -> None:
        """The berry's calm lasts until the Pokemon breaks out of a ball, so
        the throw after a break-out needs its own."""
        _ended, run_once = await self.throw([True])
        self.assertIs(run_once.await_args.kwargs["use_nanab"], True)

    async def test_an_empty_berry_pocket_still_throws(self) -> None:
        refused = gbl.excellent_throw_android.AndroidExcellentThrowError(
            "No Nanab berry is available on tall_device (picker: razz)")
        ended, run_once = await self.throw([refused, True])
        self.assertTrue(ended)
        self.assertEqual(run_once.await_count, 2)
        self.assertIs(run_once.await_args.kwargs["use_nanab"], False)

    async def test_a_refused_throw_is_reported_not_retried_forever(self) -> None:
        refused = gbl.excellent_throw_android.AndroidExcellentThrowError("no ball")
        ended, run_once = await self.throw([refused, refused])
        self.assertFalse(ended)
        self.assertEqual(run_once.await_count, 2)

    async def test_the_ios_reward_catch_is_fed_too(self) -> None:
        thrower = gbl_ios.ExcellentThrower.__new__(gbl_ios.ExcellentThrower)
        thrower.thrower = object()
        thrower.stream = MagicMock()
        thrower.attempts = 0
        with TemporaryDirectory() as directory:
            thrower.artifacts = Path(directory)
            run_once = AsyncMock(return_value=True)
            with patch.object(gbl_ios.excellent_throw_ios, "run_once", run_once):
                await thrower.throw()
        self.assertIs(run_once.await_args.kwargs["use_nanab"], True)

    def test_there_is_only_one_throw_implementation_left(self) -> None:
        """The geometry lives in `excellent_throw_android`, tested there."""
        source = Path(gbl.__file__).read_text()
        self.assertNotIn("input swipe {start[0]}", source)
        self.assertIn("excellent_throw_android.run_once(", source)


class BlindRingTests(unittest.TestCase):
    """A phone that cannot see the catch circle should stop pressing it.

    On 2026-08-31 the SE spent all 30 holds on "no readable circle" before
    falling back to the flick it could have thrown first, while the user waited
    on it and caught the reward Pokemon by hand.
    """

    def test_a_blind_phone_is_written_off_early(self) -> None:
        source = Path(gbl_ios.excellent_throw_ios.__file__).read_text()
        self.assertIn(
            "if last_measured is None and attempt >= BLIND_RING_ATTEMPTS:", source
        )

    def test_a_phone_that_has_read_the_ring_keeps_its_budget(self) -> None:
        """The bail is gated on never having measured, not on this hold."""
        source = Path(gbl_ios.excellent_throw_ios.__file__).read_text()
        loop = source[source.index("for attempt in range(1, config.max_ring_attempts"):]
        bail = loop.index("BLIND_RING_ATTEMPTS")
        self.assertIn("last_measured is None", loop[bail - 60 : bail])

    def test_the_budget_is_smaller_than_a_catch(self) -> None:
        """Catches are meant to land inside 30s; 30 holds never could."""
        self.assertLessEqual(gbl_ios.excellent_throw_ios.BLIND_RING_ATTEMPTS, 5)


class DailyCapTests(unittest.TestCase):
    """The daily cap has to end the day, not burn the stall budget.

    On 2026-08-31 the SE reached it and kept tapping: the cap leaves BATTLE
    drawn in full green and the tier chooser up, so every shape test said the
    card was live while the taps were swallowed.
    """

    def boxes(self, *labels):
        return [gbl_vision.OCRBox(text, 1.0, 100, 100, 200, 30) for text in labels]

    def test_named_from_the_banner(self) -> None:
        self.assertTrue(gbl.daily_cap_reached(
            self.boxes("You've reached the maximum number of battles for today.")))

    def test_named_when_ocr_splits_the_banner(self) -> None:
        self.assertTrue(gbl.daily_cap_reached(
            self.boxes("You've reached the maximum", "number of battles for today")))

    def test_a_playable_card_is_not_capped(self) -> None:
        self.assertFalse(gbl.daily_cap_reached(
            self.boxes("GO BATTLE LEAGUE", "BATTLE", "Battle entry good for 5 rounds.")))

    def test_the_cap_is_answered_before_the_pill(self) -> None:
        source = Path(gbl.__file__).read_text()
        self.assertLess(
            source.index("daily_cap_reached(menu_boxes)"),
            source.index("here = (state, point[0], point[1])"),
        )


class MonkeyTransportTests(unittest.IsolatedAsyncioTestCase):
    """The socket a slow phone taps through instead of `input`.

    An `input` on the android-one is 650ms of JVM start, eighteen times the Moto G's,
    and the loop spends it on every fast attack: the leg lands three quick
    moves where the rest of the fleet lands fourteen, and loses matches it was
    winning. `monkey --port` pays the start once and then answers a line of
    text in 5ms, through the same InputManager path `input` uses -- so what is
    tested here is the translation into that dialect, and that a channel which
    stops answering costs one gesture rather than the run.
    """

    def channel(self):
        """A stand-in for the socket that records the lines it was given."""
        sent: list[str] = []

        class Fake:
            async def run(self, lines):
                sent.extend(lines)

            async def close(self):
                sent.append('closed')

        return Fake(), sent

    def device(self, channel=None):
        device = SimpleNamespace(label='android-one', input_cost=0.65)
        device.shell = AsyncMock()
        if channel is not None:
            device.monkey = channel
        return device

    def test_a_tap_is_one_line(self) -> None:
        self.assertEqual(gbl.monkey_commands('input tap 10 20'), ['tap 10 20'])

    def test_a_key_is_pressed_by_name(self) -> None:
        self.assertEqual(
            gbl.monkey_commands('input keyevent KEYCODE_BACK'),
            ['press KEYCODE_BACK'],
        )

    def test_a_swipe_becomes_a_walked_path(self) -> None:
        lines = gbl.monkey_commands('input swipe 100 200 100 800 500')
        self.assertEqual(lines[0], 'touch down 100 200')
        self.assertEqual(lines[-1], 'touch up 100 800')
        moves = [line for line in lines if line.startswith('touch move')]
        self.assertGreater(len(moves), 1)
        self.assertEqual(moves[-1], 'touch move 100 800')

    def test_a_swipe_still_takes_about_as_long_as_it_asked(self) -> None:
        # The drag is the gesture whose *duration* is the thing that works --
        # the minigame pops what the finger crosses -- so a path walked at wire
        # speed would be a flick with extra steps.
        lines = gbl.monkey_commands('input swipe 100 200 100 800 500')
        paused = sum(float(line.split()[1])
                     for line in lines if line.startswith('sleep '))
        self.assertGreater(paused, 0.3)
        self.assertLess(paused, 0.5)

    def test_typing_has_no_monkey_dialect(self) -> None:
        # The picker types search terms; monkey cannot, so the whole line goes
        # back to the shell rather than half of it going each way.
        self.assertIsNone(gbl.monkey_commands('input text Zacian'))

    def test_a_backgrounded_burst_keeps_only_its_staggers(self) -> None:
        # `&` exists to overlap JVM starts. Over a socket there are none, and
        # what the burst wanted all along was taps spaced out on the glass.
        lines = gbl.monkey_commands(
            'input tap 1 2 & sleep 0.25; input tap 3 4 & wait')
        self.assertEqual(lines, ['tap 1 2', 'sleep 0.250', 'tap 3 4'])

    async def test_a_phone_with_a_channel_never_touches_the_shell(self) -> None:
        channel, sent = self.channel()
        device = self.device(channel)
        await gbl.send_input(device, 'input tap 5 6')
        self.assertEqual(sent, ['tap 5 6'])
        device.shell.assert_not_awaited()

    async def test_a_phone_without_one_is_unchanged(self) -> None:
        device = self.device()
        await gbl.send_input(device, 'input tap 5 6')
        device.shell.assert_awaited_once_with('input tap 5 6')

    async def test_a_lost_channel_costs_one_gesture_not_the_run(self) -> None:
        class Dead:
            async def run(self, lines):
                raise gbl.MonkeyGone('closed')

            async def close(self):
                pass

        device = self.device(Dead())
        await gbl.send_input(device, 'input tap 5 6')
        device.shell.assert_awaited_once_with('input tap 5 6')
        self.assertIsNone(gbl.monkey_channel(device))

    async def test_a_refused_command_is_a_lost_channel(self) -> None:
        connection = SimpleNamespace(
            writer=SimpleNamespace(write=lambda data: None, drain=AsyncMock()),
            reader=SimpleNamespace(
                readline=AsyncMock(return_value=b'ERROR:Invalid Argument\n')),
        )
        channel = gbl.MonkeyChannel(SimpleNamespace(), connection)
        with self.assertRaises(gbl.MonkeyGone):
            await channel.run(['press KEYCODE_UNKNOWN'])

    async def test_a_batch_costs_one_round_trip(self) -> None:
        # The reason a burst is worth sending at all: written in one go, its
        # replies read afterwards, four taps cost one write rather than four.
        writes: list[bytes] = []
        connection = SimpleNamespace(
            writer=SimpleNamespace(write=writes.append, drain=AsyncMock()),
            reader=SimpleNamespace(readline=AsyncMock(return_value=b'OK\n')),
        )
        channel = gbl.MonkeyChannel(SimpleNamespace(), connection)
        await channel.run(['tap 1 2'] * 4)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].count(b'tap 1 2'), 4)

    async def test_a_sleep_is_honoured_here_not_sent_on(self) -> None:
        writes: list[bytes] = []
        connection = SimpleNamespace(
            writer=SimpleNamespace(write=writes.append, drain=AsyncMock()),
            reader=SimpleNamespace(readline=AsyncMock(return_value=b'OK\n')),
        )
        channel = gbl.MonkeyChannel(SimpleNamespace(), connection)
        with patch.object(gbl.asyncio, 'sleep', AsyncMock()) as slept:
            await channel.run(['tap 1 2', 'sleep 0.060', 'tap 3 4'])
        slept.assert_awaited_once_with(0.060)
        self.assertEqual(len(writes), 2)
        self.assertNotIn(b'sleep', b''.join(writes))

    def test_a_monkey_burst_asks_for_the_spacing_a_start_used_to_give(self) -> None:
        # Chained `input` on the Moto G spaces taps 61ms apart for free. Over a
        # socket they would land in the same instant and be read as one gesture.
        channel, _ = self.channel()
        device = self.device(channel)
        command = gbl.tap_burst(device, [[1, 2]] * 3)
        self.assertNotIn('&', command)
        self.assertEqual(command.count(f'sleep {gbl.MONKEY_TAP_GAP:g}'), 2)

    def test_the_interval_is_the_stagger_not_the_measurement(self) -> None:
        channel, _ = self.channel()
        self.assertEqual(gbl.tap_interval(self.device(channel)), gbl.MONKEY_TAP_GAP)

    async def test_a_channel_promotes_the_phone_to_the_fast_profile(self) -> None:
        # The whole point: the android-one is slow at starting JVMs, not at touching.
        # Nothing below the measurement is special-cased for it -- the batch
        # sizes follow the cost, and the cost is now the wire.
        channel, _ = self.channel()
        device = SimpleNamespace(label='android-one')
        costs = [0.65, 0.005]
        with (
            patch.object(gbl, 'open_monkey', AsyncMock(return_value=channel)),
            patch.object(gbl, 'measure_input_cost',
                         AsyncMock(side_effect=lambda _: costs.pop(0))),
        ):
            await gbl.prepare_device_timing(device)
        self.assertIs(device.monkey, channel)
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.FAST_MOVE_TAPS_PER_BATCH)
        self.assertEqual(device.minigame_swipes, gbl.FAST_MINIGAME_SWEEPS)

    async def test_a_phone_that_will_not_open_one_keeps_its_old_profile(self) -> None:
        device = SimpleNamespace(label='android-one')
        with (
            patch.object(gbl, 'open_monkey', AsyncMock(return_value=None)),
            patch.object(gbl, 'measure_input_cost', AsyncMock(return_value=0.65)),
        ):
            await gbl.prepare_device_timing(device)
        self.assertIsNone(gbl.monkey_channel(device))
        self.assertEqual(
            device.fast_attack_taps_per_batch, gbl.MOVE_TAPS_PER_BATCH)

    async def test_a_fast_phone_is_never_asked_for_a_channel(self) -> None:
        # The tall_device starts a JVM in 35ms; a socket would buy it nothing and cost
        # it a process sitting on the phone for the length of the run.
        opened = AsyncMock(return_value=None)
        device = SimpleNamespace(label='tall_device')
        with (
            patch.object(gbl, 'open_monkey', opened),
            patch.object(gbl, 'measure_input_cost', AsyncMock(return_value=0.035)),
        ):
            await gbl.prepare_device_timing(device)
        opened.assert_not_awaited()

    async def test_closing_leaves_nothing_to_tap_through(self) -> None:
        channel, sent = self.channel()
        device = self.device(channel)
        await gbl.close_monkey(device)
        self.assertIsNone(gbl.monkey_channel(device))
        self.assertEqual(sent, ['closed'])

    async def test_a_device_with_no_streams_is_slow_not_broken(self) -> None:
        # ppadb hands back plain devices and the tests hand back fakes; neither
        # should raise its way out of setup.
        self.assertIsNone(await gbl.open_monkey(SimpleNamespace(label='fake')))
