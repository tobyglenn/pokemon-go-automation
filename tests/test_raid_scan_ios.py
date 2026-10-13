"""Offline tests for the iOS storage scan.

No Appium, no device: every screen here is a list of `vision_ocr.TextBox`es
laid out on an iPhone SE (3rd gen) frame (750x1334 pixels over 375x667 points),
and the Appium calls are made against a fake driver that records them.  That is
enough to prove the two things this worker has to get right before it is ever
pointed at a real handset: that a screenshot is measured the same way the
Android worker measures one, and that the phone is only ever acted on through
something found on the screen.

The layouts are the Android handsets' measurements scaled to the SE's frame.
That is a transcription, not an SE measurement -- the geometry is unproven
until this runs against the real phone.
"""

from __future__ import annotations

from tests import support as _test_support  # noqa: F401

from pathlib import Path
import unittest

from PIL import Image

from sources import raid_scan_android, raid_scan_ios, vision_ocr

SE_WIDTH, SE_HEIGHT = 750, 1334
SE_POINTS = (375, 667)


def box(text: str, x: float, y: float, w: float = 100.0, h: float = 24.0):
    return vision_ocr.TextBox(text=text, x=x, y=y, w=w, h=h)


def se_frame() -> Image.Image:
    return Image.new("RGB", (SE_WIDTH, SE_HEIGHT))


def scaled(text, x, y, w, h):
    """An Android 720x1600 measurement, on the SE's 750x1334 frame."""
    return box(
        text,
        round(x * SE_WIDTH / 720),
        round(y * SE_HEIGHT / 1600),
        round(w * SE_WIDTH / 720),
        round(h * SE_HEIGHT / 1600),
    )


class FakeElement:
    """Stand-in for an XCUIElement."""

    def __init__(self, name: str = "control", displayed: bool = True) -> None:
        self.name = name
        self.displayed = displayed
        self.calls: list = []

    def is_displayed(self) -> bool:
        return self.displayed

    def click(self) -> None:
        self.calls.append("click")

    def clear(self) -> None:
        self.calls.append("clear")

    def send_keys(self, text: str) -> None:
        self.calls.append(("send_keys", text))


class FakeDriver:
    """Records what a session was asked to do, so the tests can assert on it."""

    def __init__(self, state: int = 4, elements: dict | None = None) -> None:
        self.state = state
        self.elements = elements or {}
        self.calls: list[tuple[str, dict]] = []
        self.viewport = {"width": SE_POINTS[0], "height": SE_POINTS[1], "x": 0, "y": 0}

    def get_window_rect(self) -> dict:
        return self.viewport

    def execute_script(self, script: str, args: dict | None = None) -> object:
        self.calls.append((script, args or {}))
        if script == "mobile: queryAppState":
            return self.state
        return None

    def find_elements(self, by: str, locator: str) -> list:
        for label, element in self.elements.items():
            if label in locator:
                return [element]
        return []

    def scripts(self) -> list[str]:
        return [script for script, _ in self.calls]


class ParserReuseTests(unittest.TestCase):
    """The two halves must not be able to drift apart."""

    SHARED = (
        "normalise",
        "classify",
        "parse_grid_entries",
        "parse_result_count",
        "search_bar_fraction",
        "looks_like_loading",
        "recovery_tap",
        "authorised_dismissal",
        "blocking_screen",
    )

    def test_every_parser_is_the_android_object_not_a_copy(self) -> None:
        for name in self.SHARED:
            self.assertIs(
                getattr(raid_scan_ios, name),
                getattr(raid_scan_android, name),
                f"{name} was forked instead of imported",
            )

    def test_the_authorised_dialog_list_is_shared(self) -> None:
        # Identity, not equality: a second copy here is how one half would come
        # to clear a dialog the owner never authorised.
        self.assertIs(raid_scan_ios.AUTHORISED_DISMISSALS,
                      raid_scan_android.AUTHORISED_DISMISSALS)


class AuthorisedDialogTests(unittest.TestCase):
    """The two dialogs cleared on standing permission, and only those two.

    The Android measurements for these dialogs are placed on the SE's frame
    here: what is under test is which button gets chosen, not the geometry.
    """

    def setUp(self) -> None:
        self.image = se_frame()

    def test_adventure_sync_card_resolves_to_claim_reward(self) -> None:
        boxes = [box("You walked 37.9", 220, 175, 310, 42),
                 box("CLAIM REWARD!", 227, 1144, 295, 28)]
        allowed = raid_scan_ios.authorised_dialog_tap(boxes, self.image)
        self.assertIsNotNone(allowed)
        target, why = allowed
        self.assertAlmostEqual(target[1] * SE_HEIGHT, 1158, delta=15)
        self.assertIn("authorised", why)
        self.assertIn("adventure sync", why)
        # Cleared, so no longer something the operator has to handle.
        self.assertIsNone(raid_scan_ios.blocking_screen(boxes))

    def test_party_play_rejoins_and_never_leaves(self) -> None:
        boxes = [box("Continue Party Play?", 200, 700, 400, 50),
                 box("REJOIN PARTY", 230, 990, 290, 44),
                 box("LEAVE PARTY", 235, 1130, 280, 46)]
        allowed = raid_scan_ios.authorised_dialog_tap(boxes, self.image)
        self.assertIsNotNone(allowed)
        target, _why = allowed
        self.assertAlmostEqual(target[1] * SE_HEIGHT, 1012, delta=10)
        # LEAVE PARTY sits below and ends the session the account was in.
        self.assertLess(target[1] * SE_HEIGHT, 1100)

    def test_leave_party_on_its_own_is_never_a_dismissal(self) -> None:
        boxes = [box("Continue Party Play?", 200, 700, 400, 50),
                 box("LEAVE PARTY", 235, 1130, 280, 46)]
        self.assertIsNone(raid_scan_ios.authorised_dialog_tap(boxes, self.image))

    def test_the_exit_prompt_resolves_to_cancel_and_never_to_ok(self) -> None:
        boxes = [box("Do you want to exit Pokémon GO?", 120, 660, 480, 40),
                 box("OK", 320, 790, 60, 30),
                 box("CANCEL", 300, 915, 120, 30)]
        # It is not on the standing-authority list: this workflow has not been
        # told it may press buttons on this dialog. But CANCEL is the only side
        # of the prompt that is safe -- OK closes the game -- and answering it
        # leaves the handset exactly where it already was, so the navigation
        # rule cancels it and it is not a reason to stop the run.
        self.assertIsNone(raid_scan_ios.authorised_dialog_tap(boxes, self.image))
        target = raid_scan_ios.navigation_tap(boxes, self.image)
        self.assertIsNotNone(target)
        cancel = next(b for b in boxes if b.text == "CANCEL")
        ok = next(b for b in boxes if b.text == "OK")
        self.assertGreater(cancel.cy, ok.cy, "OK closes the game and must not be tapped")
        self.assertAlmostEqual(target[1] * SE_HEIGHT, cancel.cy, delta=2)
        self.assertIsNone(raid_scan_ios.blocking_screen(boxes))


class UnknownScreenTests(unittest.TestCase):
    """A screen nothing recognises gets no tap at all."""

    def setUp(self) -> None:
        self.image = se_frame()
        self.boxes = [box("TrainerOne", 20, 1150, 200, 34),
                      box("51:32", 560, 240, 90, 28)]

    def test_nothing_on_it_is_tapped(self) -> None:
        self.assertEqual(raid_scan_ios.classify(self.boxes), "unknown")
        self.assertFalse(raid_scan_ios.storage_grid_visible(self.boxes, self.image))
        self.assertIsNone(raid_scan_ios.navigation_tap(self.boxes, self.image))
        self.assertIsNone(raid_scan_ios.authorised_dialog_tap(self.boxes, self.image))
        self.assertIsNone(raid_scan_ios.detail_close_point(self.boxes, self.image))
        self.assertIsNone(raid_scan_ios.search_field_point(self.boxes, self.image))
        self.assertIsNone(raid_scan_ios.blocking_screen(self.boxes))

    def test_it_is_not_mistaken_for_the_loading_screen(self) -> None:
        self.assertFalse(raid_scan_ios.looks_like_loading(self.boxes))


class CoordinateTests(unittest.TestCase):
    """Pixels off a screenshot become points WebDriverAgent will accept."""

    def test_scales_a_pixel_into_a_logical_point(self) -> None:
        image = se_frame()
        self.assertEqual(
            raid_scan_ios.logical_point((200, 700), image, SE_POINTS), (100, 350)
        )
        # A pixel handed over untapped lands at half the height it was found
        # at on a 2x screen, which is the whole reason this exists.
        self.assertNotEqual(
            raid_scan_ios.logical_point((187, 666), image, SE_POINTS), (187, 666)
        )

    def test_a_screen_fraction_taps_the_point_that_was_measured(self) -> None:
        phone = raid_scan_ios.IOSPhone(FakeDriver(), label="SE")
        x, y = phone.point((0.5, 0.5), se_frame())
        self.assertAlmostEqual(x, 187.5, delta=1)
        self.assertAlmostEqual(y, 333.5, delta=1)


class SessionSafetyTests(unittest.TestCase):
    """Rules about what may be asked of the phone, checked without one."""

    def test_a_stopped_game_is_never_launched(self) -> None:
        driver = FakeDriver(state=1)
        phone = raid_scan_ios.IOSPhone(driver, label="SE")
        with self.assertRaises(raid_scan_ios.RaidScanIOSError) as caught:
            phone.ensure_foreground()
        self.assertIn("not running", str(caught.exception))
        # Launching an app iOS has stopped is what a cold start costs: the
        # game's splash, and whatever Party Play session was in progress.
        self.assertNotIn("mobile: activateApp", driver.scripts())
        self.assertNotIn("mobile: launchApp", driver.scripts())

    def test_a_backgrounded_game_is_resumed_rather_than_restarted(self) -> None:
        driver = FakeDriver(state=3)
        phone = raid_scan_ios.IOSPhone(driver, label="SE")
        phone.ensure_foreground(settle=0.0)
        self.assertIn("mobile: activateApp", driver.scripts())
        self.assertEqual(
            driver.calls[-1][1], {"bundleId": "com.nianticlabs.pokemongo"}
        )

    def test_a_frontmost_game_is_left_alone(self) -> None:
        driver = FakeDriver(state=4)
        raid_scan_ios.IOSPhone(driver, label="SE").ensure_foreground()
        # Asked what is in front, and nothing else.
        self.assertEqual(driver.scripts(), ["mobile: queryAppState"])

    def test_the_worker_never_terminates_the_game(self) -> None:
        source = Path(raid_scan_ios.__file__).read_text(encoding="utf-8")
        # The Android worker tore down a live Party Play session this way and
        # it had to be repaired by hand.
        for banned in ("terminate_app(", '"mobile: terminateApp"', "mobile: launchApp"):
            self.assertNotIn(banned, source)
        # The capability that keeps Appium from closing the game at either end
        # of a session is the opposite instruction, and is set.
        self.assertIn('"appium:shouldTerminateApp": False', source)

    def test_capabilities_keep_the_game_and_its_data_alone(self) -> None:
        caps = raid_scan_ios.build_capabilities(
            {"udid": "00008030-001TEST", "team_id": "ABCD1234",
             "wda_bundle_id": "com.example.WebDriverAgentRunner"}
        )
        self.assertEqual(caps["platformName"], "iOS")
        self.assertEqual(caps["appium:automationName"], "XCUITest")
        self.assertTrue(caps["appium:noReset"])
        self.assertIs(caps["appium:shouldTerminateApp"], False)
        # Every screen here is a screenshot plus an OCR pass; the stock 60s
        # command timeout would cut a session off mid-species.
        self.assertEqual(caps["appium:newCommandTimeout"], 3600)
        self.assertNotIn("appium:wdaLocalPort", caps)

    def test_optional_device_keys_are_passed_through_when_configured(self) -> None:
        caps = raid_scan_ios.build_capabilities(
            {
                "udid": "00008030-001TEST",
                "team_id": "ABCD1234",
                "wda_bundle_id": "com.example.WebDriverAgentRunner",
                "wda_local_port": 8101,
                "mjpeg_server_port": 9101,
                "use_preinstalled_wda": True,
                "bundle_id": "com.nianticlabs.pokemongo",
            }
        )
        self.assertEqual(caps["appium:wdaLocalPort"], 8101)
        self.assertEqual(caps["appium:mjpegServerPort"], 9101)
        self.assertTrue(caps["appium:usePreinstalledWDA"])
        self.assertEqual(caps["appium:bundleId"], "com.nianticlabs.pokemongo")


class ControlLookupTests(unittest.TestCase):
    """Controls are found in the accessibility tree, never at a stored point."""

    def test_a_displayed_control_is_found_by_its_label(self) -> None:
        field = FakeElement("search")
        phone = raid_scan_ios.IOSPhone(FakeDriver(elements={"search": field}), label="SE")
        self.assertIs(phone.find_control(raid_scan_ios.SEARCH_FIELD_LABELS), field)

    def test_a_control_the_screen_does_not_show_is_not_invented(self) -> None:
        phone = raid_scan_ios.IOSPhone(FakeDriver(), label="SE")
        self.assertIsNone(phone.find_control(raid_scan_ios.MENU_CONTROL_LABELS))

    def test_a_control_that_is_not_displayed_is_not_used(self) -> None:
        hidden = FakeElement("close", displayed=False)
        phone = raid_scan_ios.IOSPhone(FakeDriver(elements={"close": hidden}), label="SE")
        self.assertIsNone(phone.find_control(raid_scan_ios.CLOSE_CONTROL_LABELS))

    def test_the_most_trustworthy_locator_is_tried_first(self) -> None:
        asked: list[tuple[str, str]] = []

        class Recorder(FakeDriver):
            def find_elements(self, by, locator):
                asked.append((by, locator))
                return []

        phone = raid_scan_ios.IOSPhone(Recorder(), label="SE")
        self.assertIsNone(phone.find_control(("close",)))
        self.assertEqual(asked[0][0], "accessibility id")
        self.assertEqual(asked[0][1], "close")
        # The game publishes these inconsistently, so every strategy is tried.
        self.assertEqual({by for by, _ in asked},
                         {"accessibility id", "-ios predicate string", "xpath"})


class TextEntryTests(unittest.TestCase):
    """Typing and clearing go through the field when there is one."""

    def test_a_real_field_is_cleared_and_typed_through(self) -> None:
        driver = FakeDriver()
        field = FakeElement("search")
        phone = raid_scan_ios.IOSPhone(driver, label="SE")
        phone.clear_field(field, settle=0.0)
        phone.type_text("groudon", field, settle=0.0)
        self.assertEqual(field.calls, ["clear", ("send_keys", "groudon")])
        self.assertEqual(driver.calls, [], "the key barrage is for a fieldless screen")

    def test_the_native_text_box_is_cleared_with_its_own_button(self) -> None:
        # iOS 26: the game edits its field through a native text box over the
        # keyboard, which ignores the delete-key barrage -- every search was
        # appended to the last until its "Clear text" button was used.
        driver = FakeDriver(elements={"Clear text": FakeElement("Clear text")})
        phone = raid_scan_ios.IOSPhone(driver, label="SE")
        phone.clear_field(settle=0.0)
        self.assertEqual(driver.elements["Clear text"].calls, ["click"])
        self.assertNotIn("mobile: keys", [script for script, _ in driver.calls])

    def test_a_custom_drawn_field_is_cleared_with_delete_keys(self) -> None:
        # The game draws the storage search field itself and often publishes
        # nothing for it, so the delete key goes at whatever has focus.
        driver = FakeDriver()
        phone = raid_scan_ios.IOSPhone(driver, label="SE")
        phone.clear_field(settle=0.0)
        script, args = driver.calls[0]
        self.assertEqual(script, "mobile: keys")
        self.assertEqual(args["keys"], [raid_scan_ios.DELETE_KEY] * raid_scan_ios.CLEAR_KEYS)
        self.assertEqual(raid_scan_ios.DELETE_KEY, chr(0xE003))

    def test_a_fieldless_screen_is_typed_through_at_the_focused_field(self) -> None:
        driver = FakeDriver()
        phone = raid_scan_ios.IOSPhone(driver, label="SE")
        phone.type_text("groudon", settle=0.0)
        self.assertEqual(driver.calls, [("mobile: keys", {"keys": list("groudon")})])


class ScreenIdentifiedOnlyByOcrTests(unittest.TestCase):
    """`looks_like_loading` is the Android shape test, on the SE's frame."""

    def test_a_cold_start_screen_is_recognised_by_its_shape(self) -> None:
        boxes = [box("Walk around your neighbourhood and discover new Pokémon to catch.",
                     60, 700, 630, 40)]
        self.assertTrue(raid_scan_ios.looks_like_loading(boxes))

    def test_a_coach_tip_mentioning_candy_is_not_the_loading_screen(self) -> None:
        # The shared test deliberately excludes these words: a tip offering
        # Candy is a dialog the operator may be reading, and the worker waits
        # on a loading screen rather than tapping through it.
        boxes = [box("Turn on Adventure Sync to earn bonus Candy.", 60, 1100, 630, 36)]
        self.assertFalse(raid_scan_ios.looks_like_loading(boxes))


class FakePhone:
    """A phone that replays a list of screens and records what it was told."""

    platform = "ios"

    def __init__(self, screens: list, label: str = "SE") -> None:
        self.screens = list(screens)
        self.label = label
        self.viewport = SE_POINTS
        self.taps: list = []
        self.typed: list[str] = []

    def read(self):
        if not self.screens:
            raise AssertionError("the scan asked for more screens than it was given")
        return self.screens.pop(0)

    def find_control(self, labels):
        return None  # this game build publishes no controls at all

    def tap(self, fraction, image, settle: float = 0.0) -> None:
        self.taps.append(("point", round(fraction[0], 3), round(fraction[1], 3)))

    def tap_control(self, control, settle: float = 0.0) -> None:
        self.taps.append(("control",))

    def clear_field(self, field=None, settle: float = 0.0) -> None:
        pass

    def type_text(self, text: str, field=None, settle: float = 0.0) -> None:
        self.typed.append(text)


GRID_BOXES = [
    box("POKÉMON", 180, 90, 140, 30),
    box("Q (17)", 300, 214, 62, 26),
    box("CP4526", 40, 430, 150, 34),
    box("CP4410", 300, 430, 150, 34),
    box("CP4301", 560, 430, 150, 34),
]
EMPTY_BOXES = [
    box("POKÉMON", 180, 90, 140, 30),
    box("Q (0)", 300, 214, 62, 26),
    box("No Pokémon found", 150, 500, 450, 40),
]
DETAIL_BOXES = [
    scaled("CP'", 242, 93, 74, 42),
    scaled("Zacian", 286, 644, 144, 40),
    scaled("173 / 173 HP", 295, 728, 125, 21),
    scaled("203", 348, 1177, 63, 28),
    scaled("72", 578, 1177, 42, 28),
    scaled("ZACIAN CANDY", 297, 1221, 160, 21),
    scaled("ZACIAN CANDY XL", 504, 1221, 188, 21),
]


class CloseDiscTests(unittest.TestCase):
    """The detail page's unlabelled close button, found by its colour."""

    def test_finds_the_disc_at_the_bottom_centre(self) -> None:
        frame = Image.new("RGB", (SE_WIDTH, SE_HEIGHT), (240, 248, 240))
        for y in range(1200, 1272):
            for x in range(339, 411):
                if (x - 375) ** 2 + (y - 1236) ** 2 <= 34 ** 2:
                    frame.putpixel((x, y), raid_scan_ios.CLOSE_DISC_RGB)
        # The menu disc in the corner shares the colour and must be ignored.
        for y in range(1200, 1272):
            for x in range(620, 700):
                frame.putpixel((x, y), raid_scan_ios.CLOSE_DISC_RGB)
        point = raid_scan_ios.close_button_point(frame)
        self.assertAlmostEqual(point[0], 0.5, places=2)
        self.assertAlmostEqual(point[1], 1236 / SE_HEIGHT, places=2)

    def test_no_disc_is_no_point(self) -> None:
        self.assertIsNone(raid_scan_ios.close_button_point(se_frame()))


class SearchConfirmationTests(unittest.TestCase):
    """A search counts only when the field shows the term."""

    def scanner(self, field_text: str):
        screen = [
            box("POKÉMON", 180, 90, 140, 30),
            box("Q (5)", 300, 214, 62, 26),
            box(field_text, 180, 226, 300, 40),
            box("SHOW EVOLUTIONARY LINE", 220, 328, 380, 30),
            box("CP1624", 40, 430, 150, 34),
        ]
        phone = FakePhone([(se_frame(), screen)] * 12)
        return phone, raid_scan_ios.IOSStorageScanner(phone, verbose=False)

    def test_a_confirmed_search_returns_the_grid(self) -> None:
        phone, scanner = self.scanner("Q 094")
        count, entries, _, _ = scanner.search("094")
        self.assertEqual((count, [e.cp for e in entries]), (5, [1624]))
        self.assertEqual(phone.typed, ["094"])

    def test_stale_results_are_refused(self) -> None:
        # The previous species' filter, counter and all, still on screen.
        phone, scanner = self.scanner("Q 094094006")
        count, _, _, _ = scanner.search("006")
        self.assertIsNone(count)
        self.assertEqual(phone.typed, ["006"] * 3)


if __name__ == "__main__":
    unittest.main()