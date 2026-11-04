from __future__ import annotations

from tests import support as _test_support

import unittest

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


class MegaCardTests(unittest.TestCase):
    """Text of the YES/NO cards, as read on ph-1 and the oneplus on 3 Nov."""

    FREE = ("do you want to mega evolve heracross cp 1756 cp 2730 mega evolution lasts "
            "for 8 hours only one pokemon may be mega evolved at a time yes")
    REPLACE = ("do you really want to mega evolve name your mega heracross will no longer "
               "be mega evolved yes")

    def test_the_free_card_spends_nothing(self) -> None:
        self.assertFalse(starter.card_spends_energy(self.FREE))

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


class ArgumentTests(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(starter.parse_args([]).steps, list(starter.STEPS))
        self.assertEqual(starter.parse_args(["--skip", "gbl"]).steps, ["mega", "buddy", "bag", "gifts"])
        self.assertEqual(starter.parse_args(["--only", "buddy", "mega"]).steps, ["mega", "buddy"])
        with self.assertRaises(SystemExit):
            starter.parse_args(["--berries", "4"])

    def test_handoff_commands(self):
        self.assertEqual(starter.gifts_command("phone"),
                         ["gift.py", "--local", "--all", "--gifts-only", "--devices", "phone"])
        self.assertEqual(starter.gbl_command("phone"), ["gbl_day.py", "--local", "--devices", "phone"])


if __name__ == "__main__":
    unittest.main()
