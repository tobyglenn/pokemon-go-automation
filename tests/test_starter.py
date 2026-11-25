from __future__ import annotations

from tests import support as _test_support

import asyncio
import io
from pathlib import Path
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from sources import gbl_vision, starter

W, H = 1080, 2412


def box(text: str, x: int, y: int, w: int = 120, h: int = 40) -> gbl_vision.OCRBox:
    return gbl_vision.OCRBox(text, 1.0, x, y, w, h)


def blank(colour=(240, 252, 240)) -> Image.Image:
    return Image.new("RGB", (W, H), colour)


class MegaButtonTests(unittest.TestCase):
    """The pill and its cost, measured on a Pokemon page."""

    def page(self, *extra):
        return [
            box("Mega", 450, 1180),                 # the tag chip under the name
            box("20,277,891", 160, 1520), box("STARDUST", 230, 1570),
            box("5,016", 700, 1520), box("BELDUM CANDY", 620, 1570),
            box("MEGA", 270, 1355, 140, 40), box("EVOLVE", 270, 1405, 180, 40),
            *extra,
        ]

    def test_icon_without_a_number_is_free(self):
        button = starter.mega_button(self.page(), W, H)
        self.assertIsNotNone(button)
        self.assertIsNone(button.cost)
        # The row is the pill's own, not halfway up to the tag chip.
        self.assertTrue(1355 < button.point[1] < 1445)

    def test_number_beside_the_pill_is_a_cost(self):
        button = starter.mega_button(self.page(box("40", 640, 1380, 60, 40)), W, H)
        self.assertEqual(button.cost, 40)

    def test_candy_count_rows_above_are_not_a_cost(self):
        self.assertIsNone(starter.mega_button(self.page(), W, H).cost)

    def test_primal_reversion_counts(self):
        boxes = [box("PRIMAL", 270, 1355), box("REVERSION", 270, 1405, 200)]
        self.assertIsNotNone(starter.mega_button(boxes, W, H))

    def test_no_pill(self):
        self.assertIsNone(starter.mega_button([box("POWER UP", 100, 1000)], W, H))

    def test_misread_digit_beside_the_pill_is_not_free(self):
        # A 6 read as "G" (sample-android-b Blaziken, 5 Nov) once went through as free.
        button = starter.mega_button(self.page(box("G", 640, 1380, 30, 40)), W, H)
        self.assertEqual(button.cost, starter.UNREAD_COST)

    def ink_page(self, digit: bool) -> Image.Image:
        image = blank((236, 234, 240))
        draw = ImageDraw.Draw(image)
        draw.ellipse((560, 1375, 600, 1420), fill=(150, 170, 230))   # the energy icon
        if digit:
            draw.rectangle((615, 1380, 640, 1420), fill=(61, 84, 84))
        draw.ellipse((470, 1360, 540, 1430), fill=(30, 125, 135))     # the teal close disc
        return image

    def test_dark_digits_beside_the_pill_are_a_cost(self):
        button = starter.mega_button(self.page(), W, H)
        self.assertTrue(starter.cost_printed(self.ink_page(True), button))

    def test_icon_alone_prints_no_cost(self):
        button = starter.mega_button(self.page(), W, H)
        self.assertFalse(starter.cost_printed(self.ink_page(False), button))


class MegaCardTests(unittest.TestCase):
    """Text of the YES/NO cards, as read on sample-receiver and the sample-android-b on 3 Nov."""

    FREE = ("do you want to mega evolve heracross cp 1756 cp 2730 mega evolution lasts "
            "for 8 hours only one pokemon may be mega evolved at a time yes")
    REPLACE = ("do you really want to mega evolve name your mega heracross will no longer "
               "be mega evolved yes")

    def test_the_free_card_spends_nothing(self) -> None:
        self.assertFalse(starter.card_spends_energy(self.FREE))

    def test_nickname_digits_before_the_cp_pair_are_not_a_cost(self) -> None:
        # sample-android-b 19 Nov: "Electrike100" read as "electrike 100" on the free card.
        text = ("do you want to mega evolve electrike 100 cp 2340 cp 3580 mega evolution lasts "
                "for 8 hours only one pokemon may be mega evolved at a time yes")
        self.assertFalse(starter.card_spends_energy(text))
        self.assertTrue(starter.card_spends_energy(text.replace("yes", "9 yes")))

    def test_nickname_digits_on_the_replacement_card(self) -> None:
        text = ("do you really want to mega evolve sableye your electrike 100 will no longer "
                "be mega evolved yes")
        self.assertTrue(starter.replacement_card(text))
        self.assertFalse(starter.replacement_card(text.replace("yes", "26 yes")))

    def test_the_free_card_is_recognised_for_a_second_yes(self) -> None:
        self.assertTrue(starter.free_card(self.FREE))
        self.assertFalse(starter.free_card(self.FREE.replace("yes", "26 mega energy yes")))
        self.assertFalse(starter.free_card(self.REPLACE))

    def test_a_card_naming_energy_or_an_amount_costs(self) -> None:
        self.assertTrue(starter.card_spends_energy("use 9 mega energy to mega evolve manectric yes"))
        self.assertTrue(starter.card_spends_energy("do you want to mega evolve manectric 9 yes"))

    def test_only_the_replacement_warning_gets_a_second_yes(self) -> None:
        self.assertTrue(starter.replacement_card(self.REPLACE))
        self.assertFalse(starter.replacement_card("are you sure you want to use 26 mega energy yes"))
        self.assertFalse(starter.replacement_card(self.REPLACE.replace("yes", "26 mega energy yes")))

    def test_nicknamed_percentages_are_not_amounts(self) -> None:
        boxes = [gbl_vision.OCRBox("Do you really want to Mega Evolve Beldum 100%?", 1.0, 0, 100, 500, 40),
                 gbl_vision.OCRBox("Your Mega Swablu100% will no longer be", 1.0, 0, 200, 500, 40),
                 gbl_vision.OCRBox("YES", 1.0, 0, 400, 80, 40)]
        text = starter.card_text(boxes, boxes[0], boxes[2])
        self.assertTrue(starter.replacement_card(text), text)


class PageTests(unittest.TestCase):
    def test_detail_page_and_timer(self):
        boxes = [box("STARDUST", 200, 1700), box("07:59 left", 540, 1570)]
        self.assertTrue(starter.detail_page(boxes))
        self.assertTrue(starter.mega_active(boxes))
        self.assertFalse(starter.mega_active([box("STARDUST", 200, 1700)]))

    def test_mega_level_page_is_not_a_pokemon_page(self):
        boxes = [box("Houndour100%'s Mega Level", 200, 550), box("To Reach Max Level:", 330, 1040),
                 box("LEVEL UP", 180, 1610), box("HOUNDOOM MEGA ENERGY", 180, 1750)]
        self.assertTrue(starter.mega_level_page(boxes))
        self.assertFalse(starter.detail_page(boxes))

    def test_species_from_energy_caption(self):
        self.assertEqual(starter.species([box("HERACROSS MEGA ENERGY", 560, 1940)]), "Heracross")
        self.assertEqual(starter.species([box("KYOGRE PRIMAL ENERGY", 560, 1940)]), "Kyogre")


class GridTests(unittest.TestCase):
    def grid(self):
        boxes = [box("Mega", 470, 115), box("19", 520, 175, 40, 30)]
        names = ["Metagross", "Beldum100%", "• Mega Heracross", "Diancie", "Beldum 100%", "Glalie"]
        for index, name in enumerate(names):
            x = [100, 440, 780][index % 3]
            y = 500 + 390 * (index // 3)
            boxes += [box(f"CP {1000 + index}", x, y), box(name, x, y + 245, 200)]
        return boxes

    def test_count_and_reading_order(self):
        boxes = self.grid()
        self.assertEqual(starter.tag_grid_count(boxes, "mega"), 19)
        names = [name for name, _ in starter.grid_tiles(boxes, W, H)]
        self.assertEqual(names, ["metagross", "beldum100", "heracross", "diancie", "beldum100", "glalie"])

    def test_same_names_are_taken_in_order(self):
        tiles = starter.grid_tiles(self.grid(), W, H)
        name, point = starter.next_tile(tiles, {"metagross": 1, "beldum100": 1, "heracross": 1, "diancie": 1})
        self.assertEqual(name, "beldum100")
        self.assertGreater(point[1], 800)   # the second Beldum, in the second row
        self.assertIsNone(starter.next_tile(tiles, {n: 2 for n, _ in tiles}))

    def test_tiles_under_the_close_button_are_left(self):
        boxes = [box("CP 100", 480, int(H * 0.86)), box("Low", 480, int(H * 0.86) + 245)]
        self.assertEqual(starter.grid_tiles(boxes, W, H), [])


class ColourTests(unittest.TestCase):
    def test_close_button(self):
        image = blank()
        ImageDraw.Draw(image).ellipse((480, 2210, 600, 2330), fill=starter.CLOSE_FILL)
        point = starter.close_button(image)
        self.assertTrue(abs(point[0] - 540) < 10 and abs(point[1] - 2270) < 10)
        self.assertIsNone(starter.close_button(blank()))

    def ring(self, pink_segments: int) -> Image.Image:
        image = blank((250, 254, 248))
        draw = ImageDraw.Draw(image)
        for segment in range(6):
            colour = starter.RING_PINK if segment < pink_segments else (120, 124, 118)
            draw.arc((460, 100, 620, 260), segment * 60 - 85, segment * 60 - 35, fill=colour, width=14)
        return image

    def test_mood_ring(self):
        self.assertIsNone(starter.mood_ring(blank((250, 254, 248))))
        self.assertFalse(starter.buddy_excited(self.ring(4)))
        self.assertTrue(starter.buddy_excited(self.ring(6)))

    def test_heart_rows(self):
        image = blank((255, 255, 255))
        draw = ImageDraw.Draw(image)
        boxes = [box("Give your buddy a treat", 200, 1715, 500), box("Play together", 200, 1820, 300),
                 box("Take a snapshot", 200, 2030, 300)]
        draw.ellipse((760, 1705, 820, 1765), fill=starter.HEART_PINK)       # treat: filled
        draw.ellipse((760, 1810, 820, 1870), fill=(194, 222, 220))          # play: empty
        draw.ellipse((760, 2020, 820, 2080), fill=starter.HEART_PINK)       # snapshot: filled
        self.assertEqual(starter.heart_rows(image, boxes), {"treat": True, "play": False, "snapshot": True})
        self.assertEqual(starter.heart_rows(image, boxes[:1])["play"], None)


class EffectCardTests(unittest.TestCase):
    def test_an_adventure_effect_card_is_cancelled_never_used(self):
        card = [box("Use Ice Burn", 280, 720, 190), box("Time 10:00", 300, 815, 160),
                box("USE", 340, 1127, 60), box("CANCEL", 310, 1230, 130)]
        self.assertEqual(starter.effect_card(card).text, "CANCEL")
        # The profile's own Adventure Effects row has a USE but no "Use ..." title.
        profile = [box("ADVENTURE EFFECTS", 200, 900, 300), box("Ice Burn", 120, 980, 120),
                   box("USE", 560, 980, 60), box("QUICK TREAT", 250, 760, 250)]
        self.assertIsNone(starter.effect_card(profile))


class ArgumentTests(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(starter.parse_args([]).steps,
                         ["buddy", "battle", "special", "mega", "bag", "gifts", "gbl", "buddy-again"])
        self.assertIn("trade", starter.parse_args(["--trade"]).steps)
        self.assertEqual(starter.parse_args(["--skip", "gbl", "special"]).steps,
                         ["buddy", "battle", "mega", "bag", "gifts", "buddy-again"])

    def test_phases_split_at_pair_steps(self):
        steps = starter.parse_args(["--trade"]).steps
        self.assertEqual(starter.phases(steps, ["special", "trade"]), [
            ({"buddy", "battle"}, "special"),
            ({"mega", "bag", "gifts"}, "trade"),
            ({"gbl", "buddy-again"}, None)])
        # No receiver here: the phones never wait, and the special step is dropped.
        self.assertEqual(starter.phases(steps[:3] + ["mega"], []),
                         [({"buddy", "battle", "mega"}, None)])
        self.assertEqual(starter.parse_args(["--only", "buddy", "mega"]).steps, ["buddy", "mega"])
        self.assertEqual(starter.parse_args([]).berries, starter.MAX_BERRIES)

    def test_buddy_entry_point(self):
        import buddy
        args = starter.parse_args(buddy.buddy_arguments(["--devices", "phone"]))
        self.assertEqual(args.steps, ["buddy", "battle"])
        self.assertEqual(args.devices, ["phone"])
        self.assertEqual(starter.parse_args(buddy.buddy_arguments(["--skip", "battle"])).steps, ["buddy"])
        self.assertEqual(buddy.buddy_arguments(["--only", "buddy"]), ["--only", "buddy"])

    def test_explicit_all_worker_plan_selects_attached_fleet_devices(self):
        import buddy
        import starter as starter_cli

        device = mock.Mock(name="configured_phone", platform="android", identifier="test-serial", enabled=True)
        device.name = "android-one"
        fleet = mock.Mock(devices={device.name: device})
        worker_args = ["--local", "--devices", "all", "--allow-empty", "--plan"]
        for entry, extra in ((starter_cli, ["--only", "buddy"]), (buddy, [])):
            with self.subTest(entry=entry.__name__), \
                    mock.patch.object(entry.fleet_entrypoint, "apply_local_environment"), \
                    mock.patch.object(starter.pokemon_fleet, "load_fleet", return_value=fleet), \
                    mock.patch.object(starter.pokemon_fleet, "ios_connected_udids", return_value=set()), \
                    mock.patch.object(starter.screen_router, "attached_androids", mock.AsyncMock(return_value={device.identifier})) as attached, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(entry.main(worker_args + extra), 0)
                attached.assert_awaited_once_with([device])
                self.assertIn("[android-one] would run: buddy", output.getvalue())

    def test_handoff_commands(self):
        self.assertEqual(starter.gifts_command("phone"),
                         ["gift.py", "--local", "--all", "--gifts-only", "--devices", "phone"])
        self.assertEqual(starter.gbl_command("phone"), ["gbl_day.py", "--local", "--devices", "phone"])


class QuickTreatFallbackTests(unittest.IsolatedAsyncioTestCase):
    """A QUICK TREAT that opens nothing hands feeding to the play screen (9 Nov)."""

    async def run_feed(self, *results):
        phone = mock.Mock()
        report = starter.Report("buddy", True, "")
        with mock.patch.object(starter, "feed", mock.AsyncMock(side_effect=results)):
            return await starter.feed_berries(phone, 3, report), report

    async def test_buddy_on_the_map_falls_back(self):
        (fed, fallback, golden_ok), _ = await self.run_feed(
            starter.Fed("QUICK TREAT is off while the buddy is on the map", unavailable=True))
        self.assertEqual((fed, fallback, golden_ok), (0, True, True))

    async def test_effect_card_falls_back(self):
        (fed, fallback, _), _ = await self.run_feed(
            starter.Fed("an Adventure Effect card opened", refused=True, effect=True))
        self.assertEqual((fed, fallback), (0, True))

    async def test_berry_guard_still_ends_feeding(self):
        (fed, fallback, _), _ = await self.run_feed(starter.Fed("Silver Pinap by the tap", refused=True))
        self.assertEqual((fed, fallback), (0, False))

    async def test_full_buddy_does_not_fall_back(self):
        (fed, fallback, golden_ok), report = await self.run_feed(
            starter.Fed(None, golden=True), starter.Fed(None, full=True))
        self.assertEqual((fed, fallback, golden_ok), (2, False, False))
        self.assertIn("full", report.lines[-1])

    async def test_play_screen_gets_the_remaining_berries(self):
        phone = mock.Mock()
        report = starter.Report("buddy", True, "")
        feed = mock.AsyncMock(return_value=starter.Fed(None))
        with mock.patch.object(starter, "feed", feed):
            fed, _, _ = await starter.feed_berries(phone, 3 - 1, report, golden_ok=False)
        self.assertEqual(fed, 2)
        feed.assert_awaited_with(phone, False)

    def test_on_map_toast_is_read(self):
        boxes = [box("You can only use Quick Treat when your buddy is not", 540, 1100, 900),
                 box("on the map.", 540, 1150)]
        self.assertIsNotNone(starter.find_box(boxes, "only use quick treat", "not on the map"))


class TreatHeartTests(unittest.TestCase):
    """The treat row's filled hearts are counted one by one (9 Nov)."""

    def row(self, filled):
        image = blank()
        draw = ImageDraw.Draw(image)
        y = 1300
        for i in range(3):
            x = int(W * 0.72) + i * int(W * 0.08)
            colour = starter.HEART_PINK if i < filled else (200, 225, 230)
            draw.rectangle([x, y - 25, x + int(W * 0.065), y + 25], fill=colour)
        return image, [box("Give your buddy a treat", 300, y, 400)]

    def test_counts(self):
        for filled in range(4):
            image, boxes = self.row(filled)
            self.assertEqual(starter.treat_hearts(image, boxes), filled)

    def test_row_not_seen(self):
        self.assertIsNone(starter.treat_hearts(blank(), []))


class TreatGapTests(unittest.TestCase):
    """A new treat heart needs 30 minutes after the last one (user, 9 Nov)."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(starter.config_paths, "state_dir", return_value=starter.Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_no_stamp_feeds_now(self):
        self.assertEqual(starter.treat_wait("sample-android-a"), 0)

    def test_waits_thirty_minutes(self):
        noon = starter.datetime(2026, 11, 9, 12, 0).timestamp()
        starter.mark_treat_heart("sample-android-a", noon)
        self.assertAlmostEqual(starter.treat_wait("sample-android-a", noon + 600), 1200)
        self.assertEqual(starter.treat_wait("sample-android-a", noon + 1800), 0)
        self.assertEqual(starter.treat_wait("sample-receiver", noon + 600), 0)

    def test_new_day_resets(self):
        late = starter.datetime(2026, 11, 9, 23, 55).timestamp()
        starter.mark_treat_heart("sample-android-a", late)
        self.assertEqual(starter.treat_wait("sample-android-a", late + 600), 0)


class BerryGuardTests(unittest.TestCase):
    """Silver Pinap, Poffin and a second Golden Razz must never reach the buddy."""

    def test_allowed_names(self):
        for name in ("razz berry", "nanab berry", "pinap berry"):
            self.assertTrue(starter.berry_allowed(name, golden_ok=False))
        self.assertTrue(starter.berry_allowed("golden razz", golden_ok=True))
        self.assertFalse(starter.berry_allowed("golden razz", golden_ok=False))
        for name in ("silver pinap", "silver pinap berry", "pinap berry silver", "poffin", "silver"):
            self.assertFalse(starter.berry_allowed(name, golden_ok=True))

    def test_feed_order_razz_nanab_pinap(self):
        def tile(text, x, y):
            return gbl_vision.OCRBox(text, 1.0, x, y, 160, 40)
        picker = [tile("PINAP BERRY", 800, 1500), tile("NANAB BERRY", 450, 1500),
                  tile("SILVER PINAP", 450, 1800), tile("RAZZ BERRY", 100, 1500)]
        self.assertEqual(starter.first_feed_berry(picker).text, "RAZZ BERRY")
        self.assertEqual(starter.first_feed_berry(picker[:3]).text, "NANAB BERRY")
        self.assertEqual(starter.first_feed_berry(picker[:1] + picker[2:3]).text, "PINAP BERRY")
        # Only a Silver Pinap (and Golden Razz) left: nothing from the list.
        self.assertIsNone(starter.first_feed_berry([picker[2], tile("GOLDEN RAZZ", 100, 1800)]))

    def test_three_berries_a_session(self):
        self.assertEqual(starter.MAX_BERRIES, 3)
        with self.assertRaises(SystemExit):
            starter.parse_args(["--berries", "4"])

    def test_forbidden_tile_near_tap(self):
        image = Image.new("RGB", (1080, 2400))
        silver = gbl_vision.OCRBox("SILVER PINAP", 1.0, 480, 2280, 160, 40)
        golden = gbl_vision.OCRBox("GOLDEN RAZZ", 1.0, 120, 2280, 160, 40)
        boxes = [silver, golden]
        # A tap on the middle tile of the second row is a Silver Pinap tap.
        self.assertIs(starter.forbidden_tile_near(boxes, [560, 2160], image, allow_golden=False), silver)
        # The left tile is the Golden Razz: refused unless a Golden Razz was chosen.
        self.assertIs(starter.forbidden_tile_near(boxes, [200, 2160], image, allow_golden=False), golden)
        self.assertIsNone(starter.forbidden_tile_near(boxes, [200, 2160], image, allow_golden=True))
        # A first-row tile is clear of both.
        self.assertIsNone(starter.forbidden_tile_near(boxes, [560, 1700], image, allow_golden=False))

    def test_held_berry_colour(self):
        def held(colour):
            image = Image.new("RGB", (720, 1600), (225, 245, 215))   # play-screen backdrop
            ImageDraw.Draw(image).ellipse((230, 780, 490, 1150), fill=colour)
            return image
        self.assertTrue(starter.held_berry_coloured(held((235, 60, 120))))    # Razz
        self.assertTrue(starter.held_berry_coloured(held((245, 150, 30))))    # Golden Razz
        self.assertTrue(starter.held_berry_coloured(held((240, 215, 40))))    # Pinap
        self.assertFalse(starter.held_berry_coloured(held((185, 190, 195))))  # Silver Pinap
        self.assertFalse(starter.held_berry_coloured(held((225, 245, 215))))  # nothing held


class BuddyNameTests(unittest.TestCase):
    def test_clean_buddy_name(self):
        self.assertEqual(starter.clean_buddy_name("Garchomp 100% g"), "Garchomp100%")
        self.assertEqual(starter.clean_buddy_name("Garchomp100% *"), "Garchomp100%")
        self.assertEqual(starter.clean_buddy_name("Garchomp100% \u2600"), "Garchomp100%")
        self.assertEqual(starter.clean_buddy_name("Beldum100%"), "Beldum100%")
        self.assertEqual(starter.clean_buddy_name("Raichu"), "Raichu")
        self.assertEqual(starter.clean_buddy_name("Mr. Mime"), "Mr. Mime")


class MenuCloseAndTagScrollTests(unittest.TestCase):
    def test_main_menu_close_button_finds_pale_teal_ring(self):
        image = Image.new("RGB", (750, 1334), (242, 253, 240))
        ImageDraw.Draw(image).ellipse((340, 1183, 408, 1249), outline=(48, 160, 152), width=4)
        self.assertIsNone(starter.close_button(image))
        point = starter.main_menu_close_button(image)
        self.assertIsNotNone(point)
        self.assertAlmostEqual(point[0], 374, delta=3)
        self.assertAlmostEqual(point[1], 1216, delta=3)

    @staticmethod
    def _modal_with_pale_close(w: int = 720, h: int = 1600, cy: int = 1504) -> Image.Image:
        image = Image.new("RGB", (w, h), (68, 128, 120))
        draw = ImageDraw.Draw(image)
        cx = w // 2
        r = int(w * 0.068)
        teal = (36, 138, 138)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(240, 248, 240), outline=teal, width=3)
        arm = int(w * 0.022)
        draw.line((cx - arm, cy - arm, cx + arm, cy + arm), fill=teal, width=3)
        draw.line((cx - arm, cy + arm, cx + arm, cy - arm), fill=teal, width=3)
        return image

    def test_pale_close_button_finds_teal_ringed_pale_disc(self):
        image = self._modal_with_pale_close()
        self.assertIsNone(starter.close_button(image))
        point = starter.pale_close_button(image)
        self.assertIsNotNone(point)
        assert point is not None
        self.assertEqual(point[0], 360)
        self.assertAlmostEqual(point[1], 1504, delta=4)
        self.assertIsNone(starter.pale_close_button(blank()))

    def test_go_home_closes_main_menu_when_close_button_is_none(self):
        class FakeTarget:
            front_package = None

            def __init__(self) -> None:
                self.taps: list[list[int]] = []

            async def screenshot(self):
                return self.frames[min(len(self.taps), len(self.frames) - 1)]

        class FakeIOSTarget(FakeTarget, starter.home.IOSTarget):
            def __init__(self) -> None:
                self.label = "sample-ios-secondary"
                self.device = object()
                self.taps = []
                menu = Image.new("RGB", (750, 1334), (242, 253, 240))
                ImageDraw.Draw(menu).ellipse((340, 1183, 408, 1249), outline=(48, 160, 152), width=4)
                map_img = Image.new("RGB", (750, 1334), (180, 220, 160))
                self.frames = [menu, map_img]

        target = FakeIOSTarget()
        phone = starter.Phone("sample-ios-secondary", target, Path("/tmp"))

        async def fake_tap(point, image):
            target.taps.append(list(point))

        with mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(starter.home, "main_menu_open", side_effect=[True, False]), \
             mock.patch.object(starter.home, "on_map", side_effect=[False, True, True]), \
             mock.patch.object(starter.gbl_vision, "recognize", return_value=[]):
            result = asyncio.run(phone.go_home())
        self.assertEqual(result.size, (750, 1334))
        self.assertEqual(len(target.taps), 1)
        self.assertAlmostEqual(target.taps[0][1], 1216, delta=3)

    def test_go_home_cancels_adventure_effect_card(self):
        card = Image.new("RGB", (750, 1334), (242, 253, 240))
        map_img = Image.new("RGB", (750, 1334), (180, 220, 160))

        class FakeTarget:
            front_package = None
            device = object()
            label = "sample-ios-secondary"

            def __init__(self) -> None:
                self.taps: list[list[int]] = []

            async def screenshot(self):
                return card if not self.taps else map_img

        target = FakeTarget()
        phone = starter.Phone("sample-ios-secondary", target, Path("/tmp"))
        card_boxes = [box("Use Ice Burn", 200, 400), box("USE", 300, 500), box("CANCEL", 320, 620)]

        async def fake_tap(point, image):
            target.taps.append(list(point))

        with mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(phone, "save", return_value=Path("/tmp/effect.png")), \
             mock.patch.object(starter.home, "on_map", side_effect=[False, True, True]), \
             mock.patch.object(starter.home, "main_menu_open", return_value=False), \
             mock.patch.object(starter.gbl_vision, "recognize", side_effect=[card_boxes, card_boxes, card_boxes, []]):
            result = asyncio.run(phone.go_home())
        self.assertEqual(result.size, (750, 1334))
        self.assertEqual(target.taps, [[380, 640]])

    def test_go_home_closes_item_bag_that_reads_as_map(self):
        # sample-receiver, 15 Nov: the bag's sticker passed the Poke Ball test, the bag was
        # taken for the map, and the gift step tapped the bag's Gift tile.
        bag = Image.new("RGB", (1316, 2560), (240, 250, 240))
        map_img = Image.new("RGB", (1316, 2560), (180, 220, 160))

        class FakeTarget:
            front_package = None
            device = object()
            label = "sample-receiver"

            def __init__(self) -> None:
                self.taps: list[list[int]] = []

            async def screenshot(self):
                return bag if not self.taps else map_img

        target = FakeTarget()
        phone = starter.Phone("sample-receiver", target, Path("/tmp"))
        bag_boxes = [box("ITEMS", 658, 160), box("9207/9300", 658, 235), box("BERRIES", 658, 1225)]

        async def fake_tap(point, image):
            target.taps.append(list(point))

        with mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(starter.home, "on_map", return_value=True), \
             mock.patch.object(starter, "close_button", return_value=[657, 2386]), \
             mock.patch.object(starter.gbl_vision, "recognize", side_effect=[bag_boxes, []]):
            result = asyncio.run(phone.go_home())
        self.assertIs(result, map_img)
        self.assertEqual(target.taps, [[657, 2386]])

    def test_open_tag_scrolls_past_six_screens(self):
        tags_page = [box("TAGS", 100, 80)]
        scrolled_pages = [[box(f"Tag{i}", 80, 300)] for i in range(7)] + [
            [box("Mega", 73, 294)],
            [box("Mega", 73, 294), box("15", 75, 340), box("CP 2100", 80, 480), box("Charizard", 80, 640)],
        ]
        phone = starter.Phone("sample-ios-secondary", object(), Path("/tmp"))
        phone.ios = False
        idx = {"n": 0}

        async def fake_open_storage(_p):
            return Image.new("RGB", (750, 1334)), tags_page

        async def fake_settle(pred, timeout=12.0):
            boxes = scrolled_pages[min(idx["n"], len(scrolled_pages) - 1)]
            idx["n"] += 1
            im = Image.new("RGB", (750, 1334))
            return pred(im, boxes), im, boxes

        async def noop(*_a, **_kw):
            return None

        with mock.patch.object(starter, "open_storage", side_effect=fake_open_storage), \
             mock.patch.object(phone, "tap_box", side_effect=noop), \
             mock.patch.object(phone, "swipe", side_effect=noop), \
             mock.patch.object(phone, "settle", side_effect=fake_settle), \
             mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            count = asyncio.run(starter.open_tag(phone, "mega"))
        self.assertEqual(count, 15)

    def test_vision_server_read_raises_on_error_payload(self):
        class FakeProc:
            def __init__(self) -> None:
                self.stdout = io.StringIO('{"error":"recognition failed"}\n<<<gbl-ocr-end>>>\n')

        with mock.patch.object(starter.gbl_vision.select, "select", return_value=([True], [], [])):
            with self.assertRaises(starter.gbl_vision.VisionOCRError):
                starter.gbl_vision._server_read(FakeProc(), 1.0)

    def test_buddy_profile_accepts_fast_ocr_fallback_labels(self):
        self.assertTrue(starter.buddy_profile(blank(), [box("TODAVS ACTIVITIES", 365, 1434)]))
        self.assertTrue(starter.buddy_profile(blank(), [box("PLAY!", 470, 1069), box("Walk together", 200, 1578)]))

    def test_go_home_taps_lets_go_on_gbl_welcome_card(self):
        tapped: list[tuple[int, int]] = []
        card = Image.new("RGB", (1080, 2412), (242, 253, 240))
        map_img = Image.new("RGB", (1080, 2412), (180, 220, 160))

        class FakeTarget:
            platform = "android"
            label = "sample-android-b"
            front_package = None
            device = None

            def _adb(self, *args):
                return ""

        phone = starter.Phone("sample-android-b", FakeTarget(), Path("/tmp"))
        screens = iter([card, map_img])

        async def fake_look():
            return next(screens)

        async def fake_tap(point, _image=None):
            tapped.append((point[0], point[1]))

        async def noop(_seconds):
            return None

        on_map_results = iter([False, True])
        ocr_results = iter([
            [box("Welcome to the GO", 300, 1400), box("Battle League!", 300, 1460), box("LET'S GO!", 450, 2100)],
            [],
        ])

        with mock.patch.object(phone, "look", side_effect=fake_look), \
             mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(starter.home, "on_map", side_effect=lambda _i: next(on_map_results)), \
             mock.patch.object(starter.gbl_vision, "recognize", side_effect=lambda _i: next(ocr_results)), \
             mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            asyncio.run(phone.go_home())

        self.assertEqual(len(tapped), 1)
        self.assertGreaterEqual(tapped[0][1], 2100)

    def test_go_home_dismisses_stamp_rally_modal_via_pale_close_button(self):
        tapped: list[tuple[int, int]] = []
        adb_calls: list[tuple[str, ...]] = []
        card = self._modal_with_pale_close()
        map_img = Image.new("RGB", (720, 1600), (180, 220, 160))

        class FakeTarget:
            platform = "android"
            label = "sample-android-a"
            front_package = None
            device = None

            def _adb(self, *args):
                adb_calls.append(args)
                return ""

        phone = starter.Phone("sample-android-a", FakeTarget(), Path("/tmp"))
        screens = iter([card, map_img])

        async def fake_look():
            return next(screens)

        async def fake_tap(point, _image=None):
            tapped.append((point[0], point[1]))

        async def noop(_seconds):
            return None

        on_map_results = iter([False, True])
        card_boxes = [box("What is a stamp rally?", 200, 220), box("During the Poke Lid Stamp Rally", 120, 800)]

        with mock.patch.object(phone, "look", side_effect=fake_look), \
             mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(starter.home, "on_map", side_effect=lambda _i: next(on_map_results)), \
             mock.patch.object(starter.home, "main_menu_open", return_value=False), \
             mock.patch.object(starter.gbl_vision, "recognize", side_effect=lambda img: [] if img is map_img else card_boxes), \
             mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            result = asyncio.run(phone.go_home())

        self.assertIs(result, map_img)
        self.assertEqual(adb_calls, [])
        self.assertEqual(len(tapped), 1)
        self.assertEqual(tapped[0][0], 360)
        self.assertAlmostEqual(tapped[0][1], 1504, delta=4)

    def test_go_home_android_falls_back_to_pale_close_when_back_leaves_screen_unchanged(self):
        tapped: list[tuple[int, int]] = []
        adb_calls: list[tuple[str, ...]] = []
        modal = self._modal_with_pale_close()
        map_img = Image.new("RGB", (720, 1600), (180, 220, 160))

        class FakeTarget:
            platform = "android"
            label = "sample-android-a"
            front_package = None
            device = None

            def _adb(self, *args):
                adb_calls.append(args)
                return ""

        phone = starter.Phone("sample-android-a", FakeTarget(), Path("/tmp"))
        screens = iter([modal, modal, map_img])

        async def fake_look():
            return next(screens)

        async def fake_tap(point, _image=None):
            tapped.append((point[0], point[1]))

        async def noop(_seconds):
            return None

        on_map_results = iter([False, False, True])
        unknown_boxes = [box("Special Event", 200, 220)]

        with mock.patch.object(phone, "look", side_effect=fake_look), \
             mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(starter.home, "on_map", side_effect=lambda _i: next(on_map_results)), \
             mock.patch.object(starter.home, "main_menu_open", return_value=False), \
             mock.patch.object(starter.gbl_vision, "recognize", side_effect=lambda img: [] if img is map_img else unknown_boxes), \
             mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            result = asyncio.run(phone.go_home())

        self.assertIs(result, map_img)
        self.assertEqual(adb_calls, [("shell", "input keyevent 4")])
        self.assertEqual(len(tapped), 1)
        self.assertEqual(tapped[0][0], 360)
        self.assertAlmostEqual(tapped[0][1], 1504, delta=4)

    def test_buddy_present_detects_green_bag_and_blue_flower_and_ignores_floor(self):
        img = Image.new("RGB", (750, 1334), (191, 246, 171))
        # Blue buddy on pale green floor must not match without leaf-green bag.
        for y in range(940, 960):
            for x in range(365, 385):
                img.putpixel((x, y), (80, 120, 220))
        self.assertIsNone(starter.buddy_present(img))
        # Adding the leaf-green present bag beneath the blue flower matches cleanly.
        for y in range(962, 982):
            for x in range(365, 385):
                img.putpixel((x, y), (105, 150, 35))
        point = starter.buddy_present(img)
        self.assertIsNotNone(point)
        assert point is not None
        self.assertAlmostEqual(point[0], 375, delta=4)
        self.assertAlmostEqual(point[1], 961, delta=12)

    def test_open_buddy_present_taps_bag_and_returns_play_screen(self):
        with_bag = Image.new("RGB", (750, 1334), (191, 246, 171))
        for y in range(945, 960):
            for x in range(368, 382):
                with_bag.putpixel((x, y), (80, 120, 220))
        for y in range(962, 980):
            for x in range(365, 385):
                with_bag.putpixel((x, y), (105, 150, 35))
        without_bag = Image.new("RGB", (750, 1334), (191, 246, 171))

        class FakeTarget:
            platform = "ios"
            label = "sample-ios-secondary"
            front_package = None
            device = object()

        phone = starter.Phone("sample-ios-secondary", FakeTarget(), Path("/tmp"))
        report = starter.Report("buddy", True, "")
        tapped: list[tuple[int, int]] = []

        async def fake_tap(point, _image=None):
            tapped.append((point[0], point[1]))

        async def fake_look():
            return without_bag

        async def noop(_seconds):
            return None

        with mock.patch.object(phone, "tap", side_effect=fake_tap), \
             mock.patch.object(phone, "look", side_effect=fake_look), \
             mock.patch.object(starter, "camera_button", return_value=[375, 1200]), \
             mock.patch.object(starter, "exit_door", return_value=[90, 120]), \
             mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            after = asyncio.run(starter.open_buddy_present(phone, with_bag, report))

        self.assertEqual(len(tapped), 1)
        self.assertEqual(after, without_bag)
        self.assertIn("present: opened", report.lines)

    def test_pet_strokes_stay_on_narrow_sprite_core(self):
        img = Image.new("RGB", (750, 1334), (191, 246, 171))
        for y in range(730, 950):
            for x in range(330, 420):
                img.putpixel((x, y), (90, 110, 140))

        class FakeTarget:
            platform = "ios"
            label = "sample-ios-secondary"
            front_package = None
            device = object()

        phone = starter.Phone("sample-ios-secondary", FakeTarget(), Path("/tmp"))
        strokes: list[tuple[list[int], list[int]]] = []

        async def fake_look():
            return img

        async def fake_swipe(a, b, _image, _seconds, stroke=False):
            strokes.append((list(a), list(b)))

        async def noop(_seconds):
            return None

        with mock.patch.object(phone, "look", side_effect=fake_look), \
             mock.patch.object(phone, "swipe", side_effect=fake_swipe), \
             mock.patch.object(starter, "buddy_excited", side_effect=[True]), \
             mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            msg = asyncio.run(starter.pet(phone))

        self.assertIn("excited", msg)
        self.assertGreater(len(strokes), 0)
        for a, b in strokes:
            self.assertGreaterEqual(min(a[0], b[0]), 330)
            self.assertLessEqual(max(a[0], b[0]), 420)

    def test_heart_rows_accepts_fast_ocr_treat_label_variants(self):
        img = Image.new("RGB", (1080, 2412), (255, 255, 255))
        for x in range(760, 825):
            for y in range(1705, 1745):
                img.putpixel((x, y), starter.HEART_PINK)
        boxes = [box("Give your buddv a treat", 180, 1700)]
        rows = starter.heart_rows(img, boxes)
        self.assertTrue(rows["treat"])
        self.assertEqual(starter.treat_hearts(img, boxes), 1)

    def test_heart_rows_infers_treat_row_when_fast_ocr_skips_it(self):
        img = Image.new("RGB", (1080, 2412), (255, 255, 255))
        for x in range(760, 825):
            for y in range(1715, 1755):
                img.putpixel((x, y), starter.HEART_PINK)
        for row_y in (1818, 2028):
            for x in range(760, 825):
                for y in range(row_y + 5, row_y + 45):
                    img.putpixel((x, y), starter.HEART_PINK)
        boxes = [
            box("Walk together", 180, 1580),
            box("Play together", 180, 1818),
            box("Battle together", 180, 1924),
            box("Take a snapshot", 180, 2028),
        ]
        rows = starter.heart_rows(img, boxes)
        self.assertEqual(rows, {"treat": True, "play": True, "snapshot": True})
        self.assertEqual(starter.treat_hearts(img, boxes), 1)


if __name__ == "__main__":
    unittest.main()


class BuddyNameTests(unittest.TestCase):
    def test_reads_the_name_under_the_status_bar(self):
        image = Image.new("RGB", (1316, 2560))
        boxes = [starter.OCRBox("1:18", 1.0, 30, 60, 60, 30),
                 starter.OCRBox("Raichu", 1.0, 60, 150, 190, 30),
                 starter.OCRBox("0000 \u203a", 1.0, 160, 230, 160, 30),
                 starter.OCRBox("PLAY!", 1.0, 600, 1320, 120, 30)]
        self.assertEqual(starter.buddy_name(image, boxes), "Raichu")

    def test_no_name(self):
        image = Image.new("RGB", (1316, 2560))
        self.assertIsNone(starter.buddy_name(image, [starter.OCRBox("PLAY!", 1.0, 600, 1320, 120, 30)]))


class NoticeBannerTests(unittest.TestCase):
    """A friend's gift notice slid over the buddy's name (sample-android-b, 12 Nov)."""

    def test_gift_notice_is_a_banner(self) -> None:
        boxes = [box("Amine21st received 5 items", 330, 140, 600),
                 box("from your Gift!", 330, 190, 300)]
        self.assertTrue(starter.notice_banner(blank(), boxes))

    def test_plain_profile_is_not(self) -> None:
        boxes = [box("Metagross", 60, 150, 300), box("PLAY!", 470, 1020)]
        self.assertFalse(starter.notice_banner(blank(), boxes))
        self.assertEqual(starter.buddy_name(blank(), boxes), "Metagross")


class BattleBuddyTests(unittest.TestCase):
    """14 Nov: a hungry buddy and a buddy behind the lead both earned no heart."""

    def test_berry_bubble_reads_hungry(self):
        image = blank()
        ImageDraw.Draw(image).ellipse((760, 230, 880, 350), fill=(235, 60, 110))
        self.assertTrue(starter.buddy_hungry(image))

    def test_face_bubble_is_not_hungry(self):
        image = blank()
        ImageDraw.Draw(image).ellipse((760, 230, 880, 350), outline=(0, 0, 0), width=8)
        self.assertFalse(starter.buddy_hungry(image))

    def test_party_index_by_name_under_slot(self):
        image = blank()
        boxes = [box("CP 5613", 140, 1600), box("CP 4955", 480, 1600), box("CP 5083", 820, 1600),
                 box("Zacian", 140, 1900), box("Zacian", 480, 1900), box("Kyurem100%", 820, 1900)]
        self.assertEqual(starter.party_index(image, boxes, gbl_vision.normalize("Kyurem100%")), 2)
        self.assertIsNone(starter.party_index(image, boxes, "raichu"))

    def test_bench_labels_top_first(self):
        image = blank()
        boxes = [box("CP 4286", 300, 160), box("CP 5566", 920, 1600), box("CP 4945", 920, 1390)]
        self.assertEqual([b.text for b in starter.bench_labels(image, boxes)], ["CP 4945", "CP 5566"])


class GblCapSkipTests(unittest.TestCase):
    """A phone that reached today's battle cap skips GBL only (user, 14 Nov)."""

    def test_marker_is_for_today_only(self):
        import tempfile
        from sources import gbl_day
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"POGO_STATE_DIR": tmp}):
            self.assertFalse(gbl_day.cap_reached_today("sample-android-a", "2026-11-14"))
            gbl_day.mark_cap_reached("sample-android-a", "2026-11-14")
            self.assertTrue(gbl_day.cap_reached_today("sample-android-a", "2026-11-14"))
            self.assertFalse(gbl_day.cap_reached_today("sample-android-a", "2026-11-15"))
            self.assertFalse(gbl_day.cap_reached_today("sample-receiver", "2026-11-14"))

    def test_capped_phone_skips_gbl_but_keeps_buddy_again(self):
        args = starter.parse_args(["--only", "gbl", "buddy-again"])
        calls = []

        async def fake_buddy(phone, berries):
            calls.append("buddy-again")
            return starter.Report("buddy", True, "fine")

        async def fake_with_phone(spec, a, work):
            return await work(mock.Mock())

        spec = mock.Mock()
        spec.name = "sample-android-a"
        with mock.patch("sources.gbl_day.cap_reached_today", return_value=True), \
                mock.patch.object(starter, "buddy_step", fake_buddy), \
                mock.patch.object(starter, "with_phone", fake_with_phone), \
                mock.patch.object(starter, "launch") as launch:
            reports = asyncio.run(starter.run_phone(spec, args))
        launch.assert_not_called()
        self.assertEqual(calls, ["buddy-again"])
        self.assertTrue(any(r.step == "gbl" and r.ok and "skipped" in r.detail for r in reports))

    def test_uncapped_phone_still_launches_gbl(self):
        args = starter.parse_args(["--only", "gbl"])

        async def fake_with_phone(spec, a, work):
            return await work(mock.AsyncMock())

        async def fake_launch(name, arguments):
            return 0

        spec = mock.Mock()
        spec.name = "sample-android-b"
        with mock.patch("sources.gbl_day.cap_reached_today", return_value=False), \
                mock.patch.object(starter, "with_phone", fake_with_phone), \
                mock.patch.object(starter, "launch", side_effect=fake_launch) as launch:
            asyncio.run(starter.run_phone(spec, args))
        launch.assert_called_once()


class MLMegaOrderTests(unittest.TestCase):
    def test_rank_reads_the_species_inside_longer_names(self) -> None:
        from sources import starter
        self.assertEqual(starter.ml_mega_rank("Kyogre"), 0)
        self.assertEqual(starter.ml_mega_rank("Charizard X"), starter.ML_MEGA_RANK.index("charizard"))
        self.assertIsNone(starter.ml_mega_rank("Beedrill"))

    def test_a_levelling_mega_beats_a_max_mega_and_rank_breaks_ties(self) -> None:
        from sources import starter
        P = starter.MLPick
        picks = [P("a", 1, "Mewtwo", 2, True), P("b", 1, "Scizor", 17, False),
                 P("c", 1, "Metagross", 8, False), P("d", 1, "Kyogre", 0, True)]
        order = [p.name for p in starter.ml_pick_order(picks)]
        self.assertEqual(order, ["Mewtwo", "Kyogre", "Scizor", "Metagross"])

    def test_max_mega_tag(self) -> None:
        from sources import starter
        from sources.gbl_vision import OCRBox
        box = lambda t: OCRBox(t, 0, 0, 10, 10, 1.0)
        self.assertTrue(starter.max_mega([box("MaxMega")]))
        self.assertFalse(starter.max_mega([box("Mega")]))


class PageMLRankTests(unittest.TestCase):
    def test_unreadable_name_falls_back_to_the_candy_caption(self) -> None:
        from sources import starter
        from sources.gbl_vision import OCRBox
        b = lambda t: OCRBox(t, 1.0, 0, 0, 10, 10)
        self.assertEqual(starter.page_ml_rank("K", [b("KYOGRE CANDY")]), 0)
        self.assertEqual(starter.page_ml_rank("On", [b("GIBLE CANDY")]), starter.ML_MEGA_RANK.index("garchomp"))
        self.assertIsNone(starter.page_ml_rank("Gardevoir", [b("RALTS CANDY")]))


class CrossMachineSpecialTradeTests(unittest.TestCase):
    """Multi-machine starter.py coordinates special_trade between pre- and post-special phases."""

    def test_phase_arguments_rewrites_step_selectors_and_preserves_other_flags(self) -> None:
        raw = ["--devices", "sample-receiver", "sample-android-a", "--skip", "gbl", "--berries", "2", "--tag", "Mega", "--no-sleep"]
        self.assertEqual(
            starter.phase_arguments(raw, ["buddy", "battle"], no_sleep=True),
            ["--devices", "sample-receiver", "sample-android-a", "--berries", "2", "--tag", "Mega", "--only", "buddy", "battle", "--no-sleep"],
        )
        self.assertEqual(
            starter.phase_arguments(["--only", "buddy", "special", "mega", "--trade"], ["mega"], no_sleep=False),
            ["--trade", "--only", "mega"],
        )

    def test_multi_machine_starter_splits_around_special_and_continues_after_pre_failure(self) -> None:
        import starter as starter_cli

        calls: list[tuple[str, object]] = []

        def fake_run_all(script, argv):
            calls.append(("fanout", list(argv)))
            if "--no-sleep" in argv:
                raise starter_cli.pokemon_fleet.FleetError("Host command failed: local=1")
            return 0

        def fake_special(args):
            calls.append(("special", list(args.steps)))
            return starter.Report("special-trade", True, "1 special trade(s) made")

        with mock.patch.object(starter_cli.fleet_entrypoint, "machine_configs", return_value={"local": {}, "remote": {}}), \
                mock.patch.object(starter_cli.fleet_entrypoint, "apply_local_environment"), \
                mock.patch.object(starter_cli.fleet_entrypoint, "run_all_machines", side_effect=fake_run_all), \
                mock.patch.object(starter, "special_selected", return_value=(True, "")), \
                mock.patch.object(starter, "run_special_coordinator", side_effect=fake_special):
            rc = starter_cli.main([])

        self.assertEqual(rc, 1)  # pre-special phase failure is still reflected in exit code
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ("fanout", ["--only", "buddy", "battle", "--no-sleep"]))
        self.assertEqual(calls[1][0], "special")
        self.assertEqual(calls[2], ("fanout", ["--only", "mega", "bag", "gifts", "gbl", "buddy-again"]))

    def test_multi_machine_starter_only_special_runs_coordinator_without_fanout(self) -> None:
        import starter as starter_cli

        with mock.patch.object(starter_cli.fleet_entrypoint, "machine_configs", return_value={"local": {}, "remote": {}}), \
                mock.patch.object(starter_cli.fleet_entrypoint, "apply_local_environment"), \
                mock.patch.object(starter_cli.fleet_entrypoint, "run_all_machines") as fanout, \
                mock.patch.object(starter, "special_selected", return_value=(True, "")), \
                mock.patch.object(starter, "run_special_coordinator",
                                  return_value=starter.Report("special-trade", True, "1 special trade(s) made")) as coord:
            rc = starter_cli.main(["--only", "special"])

        self.assertEqual(rc, 0)
        fanout.assert_not_called()
        coord.assert_called_once()

    def test_multi_machine_starter_skips_split_when_special_already_done_today(self) -> None:
        import starter as starter_cli

        with mock.patch.object(starter_cli.fleet_entrypoint, "machine_configs", return_value={"local": {}, "remote": {}}), \
                mock.patch.object(starter_cli.fleet_entrypoint, "apply_local_environment"), \
                mock.patch.object(starter_cli.fleet_entrypoint, "run_all_machines", return_value=0) as fanout, \
                mock.patch.object(starter, "special_selected", return_value=(False, "already made today: Bouffalant")), \
                mock.patch.object(starter, "run_special_coordinator") as coord:
            rc = starter_cli.main([])

        self.assertEqual(rc, 0)
        coord.assert_not_called()
        fanout.assert_called_once_with(
            "starter.py",
            ["--only", "buddy", "battle", "mega", "bag", "gifts", "gbl", "buddy-again"],
        )

    def test_special_selected_respects_device_filter(self) -> None:
        with mock.patch.object(starter, "special_receiver", return_value="sample-receiver"), \
                mock.patch("sources.trading_accounts.aliases", return_value={"sample-ios-primary": "sample-receiver"}), \
                mock.patch("sources.special_trade.done_today", return_value=None):
            self.assertEqual(starter.special_selected(starter.parse_args([]))[0], True)
            self.assertEqual(starter.special_selected(starter.parse_args(["--devices", "all"]))[0], True)
            self.assertEqual(starter.special_selected(starter.parse_args(["--devices", "sample-receiver", "sample-android-a"]))[0], True)
            self.assertEqual(starter.special_selected(starter.parse_args(["--devices", "sample-ios-primary", "sample-android-a"]))[0], True)
            self.assertEqual(starter.special_selected(starter.parse_args(["--devices", "sample-android-a"]))[0], False)
            self.assertEqual(starter.special_selected(starter.parse_args(["--devices", "sample-receiver"]))[0], False)

    def test_special_here_passes_local_only_flag_when_explicitly_local(self) -> None:
        args = starter.parse_args(["--devices", "sample-receiver", "sample-ios-secondary"])
        args.local_only = True
        seen: list[list[str]] = []

        def fake_parse(argv):
            seen.append(list(argv))
            return mock.Mock()

        with mock.patch("sources.special_trade.parse_args", side_effect=fake_parse), \
                mock.patch("sources.special_trade.special_step",
                           new_callable=mock.AsyncMock,
                           return_value=starter.Report("special-trade", True, "0 special trade(s) made")):
            asyncio.run(starter.special_here([], args))
            asyncio.run(starter.special_here([], args, local_only=False))

        self.assertIn("--local", seen[0])
        self.assertNotIn("--local", seen[1])


class BuddyIdentityTests(unittest.TestCase):
    """22-23 Nov: a regular Kingambit led Blanche's battle on sample-receiver, not the shiny buddy."""

    def profile(self, filled: int) -> tuple[Image.Image, list]:
        image = Image.new("RGB", (1316, 2560), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        name = starter.OCRBox("Kingambit", 1.0, 70, 142, 250, 40)
        y = name.center_y + int(2560 * starter.LEVEL_HEART_DROP)
        for i in range(4):
            colour = (255, 105, 65) if i < filled else (220, 220, 220)
            x = 76 + i * 83
            draw.rectangle([x, y - 25, x + 66, y + 25], fill=colour)
        return image, [name]

    def test_counts_filled_level_hearts(self) -> None:
        for filled in range(5):
            image, boxes = self.profile(filled)
            self.assertEqual(starter.buddy_level(image, boxes), filled)

    def test_no_name_no_level(self) -> None:
        self.assertIsNone(starter.buddy_level(blank(), []))

    def test_searches_narrow_to_the_buddy_first(self) -> None:
        self.assertEqual(starter.buddy_searches("Kingambit", 1),
                         ["Kingambit&buddy2", "Kingambit&buddy1-5", "Kingambit"])
        self.assertEqual(starter.buddy_searches("Kingambit", None),
                         ["Kingambit&buddy1-5", "Kingambit"])


class OneTileTagGridTests(unittest.TestCase):
    """24 Nov: 'MaxMega' holding one Charizard showed a lone "1" OCR never reads."""

    def test_header_title_and_a_tile_is_the_grid(self) -> None:
        image = Image.new("RGB", (750, 1334))
        boxes = [starter.OCRBox("MaxMega", 1.0, 330, 80, 116, 28),
                 starter.OCRBox("Q Search", 1.0, 320, 232, 114, 26),
                 starter.OCRBox("CP 2614", 1.0, 80, 350, 104, 26),
                 starter.OCRBox("• Charizard", 1.0, 60, 520, 136, 26)]
        self.assertTrue(starter.tag_grid(image, boxes, "MaxMega"))

    def test_title_low_on_the_tags_page_is_not(self) -> None:
        image = Image.new("RGB", (750, 1334))
        boxes = [starter.OCRBox("MaxMega", 1.0, 330, 600, 116, 28)]
        self.assertFalse(starter.tag_grid(image, boxes, "MaxMega"))


class FastMaxMegaTests(unittest.TestCase):
    """24 Nov: sample-receiver opened all 42 Max Megas before GBL."""

    def test_other_mega_species_are_not_opened(self) -> None:
        for tile in ("blaziken", "sceptile", "beedrill", "gardevoir100", "lopunny"):
            self.assertFalse(starter.max_tile_worth_opening(tile, None), tile)

    def test_nicknames_are_opened(self) -> None:
        for tile in ("k", "on", "maskhan", "bigfish"):
            self.assertTrue(starter.max_tile_worth_opening(tile, 3), tile)

    def test_ranked_species_only_when_better(self) -> None:
        self.assertTrue(starter.max_tile_worth_opening("kyogre", 3))       # #1 beats Garchomp #4
        self.assertFalse(starter.max_tile_worth_opening("tyranitar", 3))   # #8 does not
        self.assertTrue(starter.max_tile_worth_opening("tyranitar", None))
        self.assertEqual(starter.max_tile_rank("beldum100"), starter.ml_mega_rank("metagross"))

    def test_megapower_walks_before_maxmega(self) -> None:
        self.assertEqual(starter.EXTRA_MEGA_TAGS, ("MegaPower", "MaxMega"))


class TruncatedNicknameRankTests(unittest.TestCase):
    def test_cut_nickname_keeps_its_rank(self) -> None:
        self.assertEqual(starter.ml_mega_rank("Tyranit"), starter.ml_mega_rank("tyranitar"))
        self.assertIsNone(starter.ml_mega_rank("Gblmedi100"))
        self.assertIsNone(starter.ml_mega_rank("Ghost"))
