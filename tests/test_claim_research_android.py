from __future__ import annotations

from tests import support as _test_support

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import claim_research_android as claim
from sources.gbl_vision import OCRBox


CLAIM_ORANGE = (255, 174, 76)
CARD_WHITE = (255, 255, 255)
TEAL = (125, 185, 193)


def research_screen(rows: int = 3, size: tuple[int, int] = (480, 1173)) -> Image.Image:
    """A research card with `rows` full-width orange pills down it.

    Laid out like the razr's GO Battle League Timed Research (1/6): pills a
    twentieth of the screen tall, spanning most of the card, with white gaps.
    """
    image = Image.new("RGB", size, CARD_WHITE)
    width, height = size
    pixels = image.load()
    for row in range(rows):
        top = round(height * (0.28 + row * 0.10))
        for y in range(top, top + round(height * 0.075)):
            for x in range(round(width * 0.06), round(width * 0.94)):
                pixels[x, y] = CLAIM_ORANGE
    return image


def labels(*words: str, size: tuple[int, int] = (480, 1173), rows: int = 3) -> list[OCRBox]:
    """One OCR box centred in each pill, in the order the pills are drawn."""
    width, height = size
    boxes = []
    for row, text in enumerate(words):
        top = round(height * (0.28 + row * 0.10))
        centre = top + round(height * 0.075) // 2
        boxes.append(
            OCRBox(
                text=text,
                confidence=1.0,
                x=width // 2 - 85,
                y=centre - 11,
                width=170,
                height=22,
            )
        )
    return boxes


class OrangeBandTests(unittest.TestCase):
    """A claim button is a wide orange band, and nothing else is."""

    def test_every_pill_is_found_in_order(self) -> None:
        bands = claim.orange_bands(research_screen(rows=3))
        self.assertEqual(len(bands), 3, f"pills read as {len(bands)} bands")
        tops = [band[0] for band in bands]
        self.assertEqual(tops, sorted(tops), "bands came back out of order")

    def test_a_narrow_orange_speck_is_not_a_button(self) -> None:
        """The GO PASS tab's orange dot and an item's artwork are both specks."""
        image = Image.new("RGB", (480, 1173), CARD_WHITE)
        pixels = image.load()
        for y in range(300, 340):
            for x in range(200, 230):
                pixels[x, y] = CLAIM_ORANGE
        self.assertEqual(claim.orange_bands(image), [])

    def test_a_thin_orange_line_is_not_a_button(self) -> None:
        image = Image.new("RGB", (480, 1173), CARD_WHITE)
        pixels = image.load()
        for y in range(300, 304):
            for x in range(30, 450):
                pixels[x, y] = CLAIM_ORANGE
        self.assertEqual(claim.orange_bands(image), [])

    def test_the_teal_furniture_is_not_orange(self) -> None:
        self.assertFalse(claim.is_orange(TEAL))
        self.assertFalse(claim.is_orange(CARD_WHITE))
        self.assertTrue(claim.is_orange(CLAIM_ORANGE))


class ClaimButtonTests(unittest.TestCase):
    """What the band says decides whether it is pressed."""

    def buttons(self, image: Image.Image, boxes: list[OCRBox]) -> list[claim.ClaimButton]:
        with patch.object(claim.gbl_vision, "recognize", return_value=boxes):
            return claim.claim_buttons(image, re.compile(claim.DEFAULT_LABEL, re.I))

    def test_claim_pills_become_buttons(self) -> None:
        found = self.buttons(
            research_screen(), labels("CLAIM REWARD", "CLAIM REWARD", "CLAIM REWARD")
        )
        self.assertEqual(len(found), 3)
        self.assertEqual([button.label for button in found], ["CLAIM REWARD"] * 3)

    def test_an_orange_tile_that_is_not_a_claim_is_left_alone(self) -> None:
        """The GO Pass reward tile is this same orange and must not be pressed.

        razr showed one on 10 Sep 2026: a MYSTERIOUS COMPONENT x2 tile, orange
        edge to edge, sitting on the rank-21 row of the September pass.
        """
        found = self.buttons(
            research_screen(rows=1), labels("MYSTERIOUS COMPONENT")
        )
        self.assertEqual(found, [], "an item tile was read as a button")

    def test_a_band_with_no_words_in_it_is_left_alone(self) -> None:
        self.assertEqual(self.buttons(research_screen(rows=1), []), [])

    def test_a_label_outside_the_band_does_not_arm_it(self) -> None:
        """"Time left to claim rewards:" is printed on the GO Pass page in black."""
        stray = [OCRBox(text="Time left to claim rewards:", confidence=1.0, x=40, y=60, width=300, height=20)]
        self.assertEqual(self.buttons(research_screen(rows=1), stray), [])

    def test_the_point_is_in_the_phones_own_pixels(self) -> None:
        """The read is downscaled; the tap has to go back up to native size."""
        native = research_screen(size=(960, 2346))
        boxes = labels("CLAIM REWARD", size=(480, 1173))
        found = self.buttons(native, boxes)
        self.assertEqual(len(found), 1)
        x, y = found[0].point
        self.assertAlmostEqual(x, 480, delta=8, msg="tap x did not scale back")
        self.assertAlmostEqual(y, 730, delta=30, msg="tap y did not scale back")
        self.assertLess(found[0].top, y)
        self.assertGreater(found[0].bottom, y)

    def test_a_custom_label_reaches_another_screen(self) -> None:
        found = self.buttons(research_screen(rows=1), labels("COLLECT"))
        self.assertEqual(found, [])
        with patch.object(claim.gbl_vision, "recognize", return_value=labels("COLLECT")):
            other = claim.claim_buttons(research_screen(rows=1), re.compile("collect", re.I))
        self.assertEqual(len(other), 1)


class EncounterGuardTests(unittest.TestCase):
    """What counts as "a Pokemon is standing in front of this phone"."""

    def frame_with(self, *texts: str) -> list[OCRBox]:
        return [
            OCRBox(text=text, confidence=1.0, x=100, y=60 + index * 40, width=120, height=24)
            for index, text in enumerate(texts)
        ]

    def test_a_cp_plate_anywhere_is_an_encounter(self) -> None:
        """The plate rides with the Pokemon, so its height proves nothing."""
        for text in ("CP 767", "CP1941", "cp 12"):
            with patch.object(claim.gbl_vision, "recognize", return_value=self.frame_with(text)):
                self.assertTrue(claim.plate_visible(research_screen(rows=1)), text)

    def test_a_research_screen_is_not_an_encounter(self) -> None:
        boxes = self.frame_with("CLAIM REWARD", "Ends in 82 days and 3 hours")
        with patch.object(claim.gbl_vision, "recognize", return_value=boxes):
            self.assertFalse(claim.plate_visible(research_screen()))

    def test_a_bare_number_is_not_a_plate(self) -> None:
        boxes = self.frame_with("x10,000", "RANK 200", "875")
        with patch.object(claim.gbl_vision, "recognize", return_value=boxes):
            self.assertFalse(claim.plate_visible(research_screen()))


class _FakeShell:
    """The ppadb handle underneath a phone, recording what was sent to it."""

    def __init__(self, sent: list[str]) -> None:
        self.sent = sent

    async def shell(self, command: str) -> str:
        self.sent.append(command)
        return ""


class FakePhone:
    """A phone that records taps and swipes and serves scripted frames."""

    def __init__(self, frames: list[Image.Image]) -> None:
        self.label = "fake-g"
        self.viewport = (480, 1173)
        self.frames = frames
        self.taps: list[list[int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.shells: list[str] = []
        self.device = _FakeShell(self.shells)

    async def tap(self, point) -> None:
        self.taps.append(list(point))

    async def input_swipe(self, x0, y0, x1, y1, duration_ms) -> None:
        self.swipes.append((x0, y0, x1, y1, duration_ms))


class EncounterOnScreenTests(unittest.IsolatedAsyncioTestCase):
    """Ball or plate: either one is enough to hold every gesture back."""

    async def check(self, *, ball, plate: bool) -> bool:
        device = FakePhone([])
        with patch.object(
            claim.excellent_throw_android, "capture_frame",
            AsyncMock(return_value=(research_screen(), 0.0)),
        ), patch.object(
            claim.excellent_throw_android, "analysis_view",
            lambda _viewport: type("View", (), {"viewport": (420, 1027)})(),
        ), patch.object(
            claim.excellent_throw_android.excellent_throw_ios, "locate_throw_ball",
            lambda *_a: ball,
        ), patch.object(claim, "plate_visible", lambda _image: plate):
            return await claim.encounter_on_screen(device)

    async def test_a_ball_in_hand_is_an_encounter(self) -> None:
        self.assertTrue(await self.check(ball=object(), plate=False))

    async def test_a_ball_in_flight_still_leaves_the_plate(self) -> None:
        """A swipe throws the ball; the next read must not call the screen empty."""
        self.assertTrue(await self.check(ball=None, plate=True))

    async def test_a_menu_is_not_an_encounter(self) -> None:
        self.assertFalse(await self.check(ball=None, plate=False))

    async def test_one_missed_plate_does_not_open_the_gate(self) -> None:
        """The read a scroll is decided on gets more than one chance.

        Neither test is certain on a single frame, and razr scrolled a Fearow's
        encounter screen on 10 Sep 2026 -- a thrown Great Ball -- off one read
        whose OCR missed the grey-on-grass plate.
        """
        device = FakePhone([])
        seen = [False, False, True]
        with patch.object(
            claim.excellent_throw_android, "capture_frame",
            AsyncMock(return_value=(research_screen(), 0.0)),
        ), patch.object(
            claim.excellent_throw_android, "analysis_view",
            lambda _viewport: type("View", (), {"viewport": (420, 1027)})(),
        ), patch.object(
            claim.excellent_throw_android.excellent_throw_ios, "locate_throw_ball",
            lambda *_a: None,
        ), patch.object(claim, "plate_visible", lambda _image: seen.pop(0)):
            self.assertTrue(await claim.encounter_on_screen(device, reads=3))
        self.assertEqual(seen, [], "gave up before it had looked three times")


class PlateOnScreenTests(unittest.IsolatedAsyncioTestCase):
    """A ball is not a Pokemon; only a name and a CP say something can be thrown at."""

    async def check(self, plates: list[bool]) -> bool:
        device = FakePhone([])
        seen = list(plates)
        with patch.object(
            claim.excellent_throw_android, "capture_frame",
            AsyncMock(return_value=(research_screen(), 0.0)),
        ), patch.object(claim, "plate_visible", lambda _image: seen.pop(0)):
            return await claim.plate_on_screen(device, reads=len(plates))

    async def test_a_plate_on_any_read_is_a_pokemon(self) -> None:
        self.assertTrue(await self.check([False, False, True]))

    async def test_no_plate_on_any_read_is_not(self) -> None:
        """The shutter razr threw at on 10 Sep 2026 had nothing named over it."""
        self.assertFalse(await self.check([False, False, False]))


class PressBackTests(unittest.IsolatedAsyncioTestCase):
    async def test_back_is_a_keyevent_not_a_swipe(self) -> None:
        """A swipe on a sub-screen of an encounter is a thrown ball."""
        device = FakePhone([])
        await claim.press_back(device)
        self.assertEqual(device.shells, ["input keyevent KEYCODE_BACK"])
        self.assertEqual(device.swipes, [])


class ClaimOneTests(unittest.IsolatedAsyncioTestCase):
    """One press, then whichever ending the reward turns out to have."""

    def setUp(self) -> None:
        self.args = claim.parse_args(["--claim-wait", "5"])
        self.button = claim.ClaimButton(
            label="CLAIM REWARD", point=[240, 386], top=339, bottom=428
        )

    async def claim_one(self, *, encounter: bool, caught: bool, after: list[claim.ClaimButton]):
        device = FakePhone([])
        with patch.object(
            claim, "encounter_on_screen", AsyncMock(return_value=encounter)
        ), patch.object(
            claim.catch_awarded_android, "catch_one", AsyncMock(return_value=caught)
        ) as catch_one, patch.object(
            claim.catch_awarded_android, "close_item_sheet", AsyncMock(return_value=False)
        ), patch.object(
            claim.excellent_throw_android, "clear_catch_screens", AsyncMock()
        ), patch.object(
            claim, "read_claim_buttons", AsyncMock(return_value=after)
        ), patch(
            "asyncio.sleep", AsyncMock()
        ):
            self.result = await claim.claim_one(
                device, self.button, self.args, Path(tempfile.mkdtemp()) / "claim"
            )
        self.catch_one = catch_one
        return device

    async def test_the_button_is_pressed_where_its_words_are(self) -> None:
        device = await self.claim_one(encounter=False, caught=False, after=[])
        self.assertEqual(device.taps[0], [240, 386])

    async def test_an_encounter_reward_is_thrown_at(self) -> None:
        await self.claim_one(encounter=True, caught=True, after=[])
        self.assertEqual(self.result, (True, True))
        self.assertEqual(self.catch_one.await_count, 1)

    async def test_an_item_reward_needs_no_throw(self) -> None:
        await self.claim_one(encounter=False, caught=False, after=[])
        self.assertEqual(self.result, (True, False))
        self.assertEqual(self.catch_one.await_count, 0, "threw a ball at an item")

    async def test_an_encounter_that_draws_late_is_still_caught(self) -> None:
        """The button goes the instant the tap lands; the Pokemon takes seconds.

        razr walked away from a Fearow on 10 Sep 2026 because the vanished
        button was read as the whole story.
        """
        device = FakePhone([])
        opens = [False, False, True]

        async def encounter_open(_device):
            return opens.pop(0) if opens else True

        with patch.object(
            claim, "encounter_on_screen", encounter_open
        ), patch.object(
            claim.catch_awarded_android, "catch_one", AsyncMock(return_value=True)
        ) as catch_one, patch.object(
            claim.catch_awarded_android, "close_item_sheet", AsyncMock(return_value=False)
        ), patch.object(
            claim.excellent_throw_android, "clear_catch_screens", AsyncMock()
        ), patch.object(
            claim, "read_claim_buttons", AsyncMock(return_value=[])
        ), patch(
            "asyncio.sleep", AsyncMock()
        ):
            result = await claim.claim_one(
                device, self.button, self.args, Path(tempfile.mkdtemp()) / "claim"
            )
        self.assertEqual(result, (True, True))
        self.assertEqual(catch_one.await_count, 1)

    async def test_a_button_still_standing_there_did_not_take(self) -> None:
        """The row is only claimed once it stops being an orange claim pill."""
        await self.claim_one(encounter=False, caught=False, after=[self.button])
        self.assertEqual(self.result, (False, False))

    async def test_the_rows_below_do_not_count_as_the_pressed_one(self) -> None:
        lower = claim.ClaimButton(
            label="CLAIM REWARD", point=[240, 501], top=454, bottom=542
        )
        await self.claim_one(encounter=False, caught=False, after=[lower])
        self.assertEqual(self.result, (True, False))


class RunDeviceTests(unittest.IsolatedAsyncioTestCase):
    """Working a list: press what is in view, look further down, then stop."""

    async def run_device(self, screens: list[list[claim.ClaimButton]], extra: list[str] | None = None):
        args = claim.parse_args(["--artifacts", tempfile.mkdtemp(), *(extra or [])])
        device = FakePhone([])
        reads = list(screens)
        # One log for both gestures, so a test can say which came first.
        self.events: list[str] = []

        async def read(_device, _label):
            return reads.pop(0) if reads else []

        async def press(_device, button, _args, _dir):
            self.events.append(f"claim {button.top}")
            return True, True

        async def scroll(_device):
            self.events.append("scroll")

        with patch.object(claim, "read_claim_buttons", read), patch.object(
            claim, "claim_one", press
        ), patch.object(claim, "scroll_list", scroll), patch.object(
            claim, "plate_on_screen", AsyncMock(return_value=False)
        ), patch.object(
            claim, "encounter_on_screen", AsyncMock(return_value=False)
        ):
            self.status = await claim.run_device(device, args)
        self.claims = [event for event in self.events if event.startswith("claim")]
        self.scrolls = [event for event in self.events if event == "scroll"]
        return device

    def button(self, top: int) -> claim.ClaimButton:
        return claim.ClaimButton(
            label="CLAIM REWARD", point=[240, top + 40], top=top, bottom=top + 89
        )

    async def test_each_screenful_is_worked_top_down(self) -> None:
        await self.run_device([[self.button(339), self.button(454)], [self.button(454)]])
        self.assertEqual(self.claims, ["claim 339", "claim 454"])
        self.assertEqual(self.status, 0)

    async def test_an_empty_screen_is_scrolled_before_it_is_believed(self) -> None:
        """A list is only finished once looking further down finds nothing."""
        await self.run_device([[], [self.button(339)]], extra=["--scrolls", "2"])
        self.assertEqual(self.events[:2], ["scroll", "claim 339"])

    async def test_a_screenful_wins_the_scroll_budget_back(self) -> None:
        """Otherwise a long list stops three screens in, however much is left."""
        screens = [[], [self.button(339)], [], [self.button(454)]]
        await self.run_device(screens, extra=["--scrolls", "1"])
        self.assertEqual(self.claims, ["claim 339", "claim 454"])

    async def test_an_open_encounter_is_thrown_at_and_never_swiped_over(self) -> None:
        """On an encounter screen a swipe is a thrown ball, not a scroll.

        razr threw three Great Balls sideways at a Fearow on 10 Sep 2026, when
        a claim's late encounter met a loop that scrolled to look for buttons.
        The real scroll runs here, so the gesture it makes is on the record.
        """
        device = FakePhone([])
        args = claim.parse_args(["--artifacts", tempfile.mkdtemp(), "--scrolls", "1"])
        events: list[str] = []
        opened = [True]

        async def swipe(*_args) -> None:
            events.append("swipe")

        device.input_swipe = swipe

        async def plate_open(_device, reads=1):
            return bool(opened and opened.pop())

        async def catch_one(_device, _args, _dir, lone_ball_reading=False):
            events.append("catch")
            return True

        with patch.object(claim, "read_claim_buttons", AsyncMock(return_value=[])), \
                patch.object(claim, "plate_on_screen", plate_open), \
                patch.object(claim, "encounter_on_screen", AsyncMock(return_value=False)), \
                patch.object(claim.catch_awarded_android, "catch_one", catch_one), \
                patch.object(claim.excellent_throw_android, "clear_catch_screens", AsyncMock()), \
                patch("asyncio.sleep", AsyncMock()):
            await claim.run_device(device, args)
        self.assertEqual(events, ["catch", "swipe"], f"gestures went {events}")

    async def test_an_encounter_that_takes_no_throw_is_given_up_on(self) -> None:
        """Otherwise one standing Pokemon eats the whole run.

        razr's Fearow was open, was seen, and could not be thrown at, so the
        loop threw at it again for every one of its 40 cycles and never got
        back to the five claims still on the card.
        """
        device = FakePhone([])
        args = claim.parse_args(
            ["--artifacts", tempfile.mkdtemp(), "--max-failures", "2"]
        )
        attempts: list[int] = []

        async def catch_one(_device, _args, _dir, lone_ball_reading=False):
            attempts.append(1)
            return False

        with patch.object(claim, "read_claim_buttons", AsyncMock(return_value=[])), \
                patch.object(claim, "plate_on_screen", AsyncMock(return_value=True)), \
                patch.object(claim.catch_awarded_android, "catch_one", catch_one), \
                patch.object(claim.excellent_throw_android, "clear_catch_screens", AsyncMock()), \
                patch("asyncio.sleep", AsyncMock()):
            status = await claim.run_device(device, args)
        self.assertEqual(len(attempts), 2, "kept throwing past --max-failures")
        self.assertEqual(status, 1)

    async def test_a_caught_stray_clears_the_failure_count(self) -> None:
        """A phone that is catching is working, however it got to the encounter."""
        device = FakePhone([])
        args = claim.parse_args(
            ["--artifacts", tempfile.mkdtemp(), "--max-failures", "2", "--scrolls", "0"]
        )
        caught = [False, True, False]
        attempts: list[bool] = []

        async def catch_one(_device, _args, _dir, lone_ball_reading=False):
            attempts.append(caught[len(attempts)])
            return attempts[-1]

        async def open_encounter(_device, reads=1):
            return len(attempts) < len(caught)

        with patch.object(claim, "read_claim_buttons", AsyncMock(return_value=[])), \
                patch.object(claim, "plate_on_screen", open_encounter), \
                patch.object(claim, "encounter_on_screen", AsyncMock(return_value=False)), \
                patch.object(claim.catch_awarded_android, "catch_one", catch_one), \
                patch.object(claim.excellent_throw_android, "clear_catch_screens", AsyncMock()), \
                patch("asyncio.sleep", AsyncMock()):
            status = await claim.run_device(device, args)
        self.assertEqual(attempts, caught, "the catch in the middle did not reset it")
        self.assertEqual(status, 0)

    async def test_a_ball_with_no_pokemon_over_it_is_backed_out_of(self) -> None:
        """The GO Snapshot screen draws its shutter exactly where the ball sits.

        razr wandered onto it after a catch on 10 Sep 2026 and the run threw at
        the camera button eleven times, because a round disc looked like an
        encounter.  Nothing is named over a shutter, so there is nothing to
        throw at and the way out is BACK.
        """
        device = FakePhone([])
        args = claim.parse_args(
            ["--artifacts", tempfile.mkdtemp(), "--max-failures", "3", "--scrolls", "0"]
        )
        attempts: list[int] = []
        plates = [False, True]

        async def catch_one(_device, _args, _dir, lone_ball_reading=False):
            attempts.append(1)
            return True

        async def plate_on_screen(_device, reads=1):
            return plates.pop(0) if plates else False

        with patch.object(claim, "read_claim_buttons", AsyncMock(return_value=[])), \
                patch.object(claim, "plate_on_screen", plate_on_screen), \
                patch.object(claim, "encounter_on_screen", AsyncMock(return_value=True)), \
                patch.object(claim.catch_awarded_android, "catch_one", catch_one), \
                patch.object(claim.excellent_throw_android, "clear_catch_screens", AsyncMock()), \
                patch("asyncio.sleep", AsyncMock()):
            await claim.run_device(device, args)
        self.assertEqual(
            device.shells[:1],
            ["input keyevent KEYCODE_BACK"],
            "the plateless screen was not backed out of",
        )
        self.assertEqual(len(attempts), 1, "threw at a screen with no Pokemon on it")

    async def test_a_screen_that_will_not_back_out_is_given_up_on(self) -> None:
        """BACK is not guaranteed to land, and a stuck phone must not loop."""
        device = FakePhone([])
        args = claim.parse_args(
            ["--artifacts", tempfile.mkdtemp(), "--max-failures", "2", "--scrolls", "0"]
        )
        with patch.object(claim, "read_claim_buttons", AsyncMock(return_value=[])), \
                patch.object(claim, "plate_on_screen", AsyncMock(return_value=False)), \
                patch.object(claim, "encounter_on_screen", AsyncMock(return_value=True)), \
                patch.object(claim.excellent_throw_android, "clear_catch_screens", AsyncMock()), \
                patch("asyncio.sleep", AsyncMock()):
            status = await claim.run_device(device, args)
        self.assertEqual(len(device.shells), 2, "kept pressing BACK past --max-failures")
        self.assertEqual(status, 1)

    async def test_a_plain_screen_is_scrolled_not_backed_out_of(self) -> None:
        """No ball and no plate is a list to look further down, not a sub-screen."""
        device = FakePhone([])
        args = claim.parse_args(
            ["--artifacts", tempfile.mkdtemp(), "--scrolls", "1"]
        )
        with patch.object(claim, "read_claim_buttons", AsyncMock(return_value=[])), \
                patch.object(claim, "plate_on_screen", AsyncMock(return_value=False)), \
                patch.object(claim, "encounter_on_screen", AsyncMock(return_value=False)), \
                patch.object(claim, "scroll_list", AsyncMock()) as scroll, \
                patch("asyncio.sleep", AsyncMock()):
            status = await claim.run_device(device, args)
        self.assertEqual(device.shells, [], "pressed BACK on an ordinary list")
        self.assertEqual(scroll.await_count, 1)
        self.assertEqual(status, 0)

    async def test_the_scroll_budget_runs_out(self) -> None:
        await self.run_device([], extra=["--scrolls", "2"])
        self.assertEqual(self.scrolls, ["scroll"] * 2, "scrolled a list with no end")
        self.assertEqual(self.claims, [])
        self.assertEqual(self.status, 0)

    async def test_a_dry_run_sends_no_touch(self) -> None:
        device = await self.run_device([[self.button(339)]], extra=["--dry-run"])
        self.assertEqual(device.taps, [])
        self.assertEqual(self.claims, [])


class ArgumentTests(unittest.TestCase):
    def test_the_label_becomes_a_pattern(self) -> None:
        args = claim.parse_args([])
        self.assertTrue(args.label.search("claim reward"))
        self.assertFalse(args.label.search("mysterious component"))

    def test_devices_take_one_flag_each(self) -> None:
        args = claim.parse_args(["--devices", "AAA", "--devices", "BBB"])
        self.assertEqual(args.devices, ["AAA", "BBB"])

    def test_nanab_is_on_unless_it_is_turned_off(self) -> None:
        self.assertTrue(claim.parse_args([]).nanab)
        self.assertFalse(claim.parse_args(["--no-nanab"]).nanab)


if __name__ == "__main__":
    unittest.main()
