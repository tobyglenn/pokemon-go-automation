#!/usr/bin/env python3
"""Unit tests for raid preparation workers (iOS & Android) and task runner."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from scripts import raid_prep
from sources import raid_prep_android, raid_prep_common, raid_prep_ios, vision_ocr


class TaskLoadingTests(unittest.TestCase):
    def test_load_tasks_empty_when_path_none(self) -> None:
        tasks = raid_prep.load_tasks(None)
        self.assertEqual(tasks, [])

    def test_adhoc_prep_task_creation(self) -> None:
        task = raid_prep.PrepTask(
            name="Tyranitar",
            search_term="98Plus&Tyranitar",
            expected_cp=2718,
            target_cp=3830,
            tag_name="mega",
        )
        self.assertEqual(task.name, "Tyranitar")
        self.assertEqual(task.expected_cp, 2718)
        self.assertEqual(task.target_cp, 3830)
        self.assertEqual(task.tag_name, "mega")


class AndroidRaidPrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.phone = MagicMock()
        self.phone.viewport = (720, 1600)
        self.worker = raid_prep_android.AndroidRaidPrepWorker(self.phone)

    def test_read_current_cp(self) -> None:
        boxes = [
            vision_ocr.TextBox(text="CP 2718", x=200, y=100, w=150, h=40),
            vision_ocr.TextBox(text="Tyranitar", x=220, y=150, w=200, h=40),
            vision_ocr.TextBox(text="182 / 182 HP", x=250, y=200, w=180, h=30),
        ]
        cp = self.worker.read_current_cp(boxes)
        self.assertEqual(cp, 2718)

    def test_tag_pokemon_skips_if_already_tagged(self) -> None:
        # Tag pill 'mega' already in upper area of detail page
        boxes = [
            vision_ocr.TextBox(text="CP 3830", x=200, y=100, w=150, h=40),
            vision_ocr.TextBox(text="Tyranitar", x=220, y=150, w=200, h=40),
            vision_ocr.TextBox(text="mega", x=220, y=250, w=100, h=30),
        ]
        self.phone.read.return_value = (Image.new("RGB", (720, 1600)), boxes)

        tagged = self.worker.tag_pokemon("mega")
        self.assertTrue(tagged)
        # Should not tap hamburger menu if already tagged
        self.phone.tap.assert_not_called()

    def test_power_up_result_dataclass(self) -> None:
        res = raid_prep_android.PowerUpResult(
            success=True,
            initial_cp=2718,
            final_cp=3830,
            stardust_cost=90000,
            candy_cost=70,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.final_cp, 3830)
        self.assertEqual(res.stardust_cost, 90000)


class IOSRaidPrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.phone = MagicMock()
        self.worker = raid_prep_ios.IOSRaidPrepWorker(self.phone)

    def test_read_current_cp(self) -> None:
        boxes = [
            vision_ocr.TextBox(text="CP 2049", x=100, y=80, w=120, h=30),
            vision_ocr.TextBox(text="Venusaur", x=120, y=120, w=150, h=30),
        ]
        cp = self.worker.read_current_cp(boxes)
        self.assertEqual(cp, 2049)

    def test_tag_pokemon_skips_if_already_tagged(self) -> None:
        img = Image.new("RGB", (750, 1334))
        boxes = [
            vision_ocr.TextBox(text="CP 2741", x=100, y=80, w=120, h=30),
            vision_ocr.TextBox(text="Mega", x=120, y=200, w=80, h=25),
        ]
        self.phone.read.return_value = (img, boxes)

        tagged = self.worker.tag_pokemon("Mega")
        self.assertTrue(tagged)
        self.phone.tap.assert_not_called()

    def test_power_up_result_dataclass(self) -> None:
        res = raid_prep_ios.PowerUpResult(
            success=True,
            initial_cp=2049,
            final_cp=2741,
            stardust_cost=75000,
            candy_cost=64,
        )
        self.assertTrue(res.success)
        self.assertEqual(res.final_cp, 2741)


def _box(text: str, y: int, x: int = 300) -> vision_ocr.TextBox:
    return vision_ocr.TextBox(text=text, x=x, y=y, w=80, h=30)


class SharedRaidPrepRuleTests(unittest.TestCase):
    def test_power_up_needs_a_higher_cp_on_the_page(self) -> None:
        self.assertFalse(raid_prep_common.powered_up(2000, None))
        self.assertFalse(raid_prep_common.powered_up(2000, 2000))
        self.assertTrue(raid_prep_common.powered_up(2000, 2100))
        self.assertTrue(raid_prep_common.powered_up(None, 2100))

    def test_overshoot_is_xl_or_past_the_target(self) -> None:
        band = (800, 1050)
        self.assertTrue(raid_prep_common.overshot([_box("Candy XL", 900)], 100, None, band))
        self.assertFalse(raid_prep_common.overshot([_box("Candy XL", 200)], 100, None, band))
        self.assertTrue(raid_prep_common.overshot([], 3900, 3830, band))
        self.assertFalse(raid_prep_common.overshot([], 3830, 3830, band))

    def test_preview_cp_reads_the_arrow(self) -> None:
        self.assertEqual(raid_prep_common.preview_cp([_box("CP 547 → 610", 500)], None), 610)
        self.assertEqual(raid_prep_common.preview_cp([], 42), 42)


class AndroidFailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.phone = MagicMock()
        self.phone.viewport = (720, 1600)
        self.worker = raid_prep_android.AndroidRaidPrepWorker(self.phone)

    def test_search_refuses_a_cp_that_is_not_on_the_grid(self) -> None:
        self.worker.scanner = MagicMock()
        self.worker.scanner.search.return_value = (2, [], Image.new("RGB", (720, 1600)), [])
        entry = MagicMock(cp=2700, x=100, y=400)
        with patch.object(raid_prep_android.raid_scan_android, "parse_grid_entries", return_value=[entry]):
            self.assertFalse(self.worker.search_and_open("tyranitar", expected_cp=2718))
        self.phone.tap.assert_not_called()

    def test_power_up_without_dialog_taps_nothing_more(self) -> None:
        image = Image.new("RGB", (720, 1600))
        detail = [_box("CP 2718", 100), _box("POWER UP", 1100)]
        self.phone.read.side_effect = [(image, detail), (image, detail)]
        result = self.worker.power_up_to_level_40(target_cp=3830)
        self.assertFalse(result.success)
        self.assertEqual(result.note, "Power up dialog did not open")
        self.assertEqual(self.phone.tap.call_count, 1)  # only the POWER UP button

    def test_evolve_without_yes_does_not_tap_blind(self) -> None:
        image = Image.new("RGB", (720, 1600))
        detail = [_box("CP 900", 100), _box("EVOLVE", 1200)]
        popup = [_box("NO", 1000)]
        self.phone.read.side_effect = [(image, detail), (image, popup)]
        result = self.worker.evolve()
        self.assertFalse(result.success)
        tapped = [call.args[0] for call in self.phone.tap.call_args_list]
        self.assertNotIn((0.50, 0.60), tapped)

    def test_power_up_steps_back_from_an_overshoot(self) -> None:
        image = Image.new("RGB", (720, 1600))
        h = 1600
        detail = [_box("CP 1000", 100), _box("POWER UP", int(h * 0.6))]
        dialog = [_box("POWER UP: 2", int(h * 0.3)), _box("+", int(h * 0.75), x=530), _box("CP 1000 → 1000", 500)]
        over = dialog[:2] + [_box("CP 1000 → 1300", 500)]
        back = dialog[:2] + [_box("CP 1000 → 1200", 500), _box("POWER UP", int(h * 0.8))]
        final = [_box("CP 1200", 100)]
        popup: list = []
        self.phone.read.side_effect = [
            (image, detail), (image, dialog), (image, over), (image, back), (image, popup), (image, final)
        ]
        with patch.object(raid_prep_android.time, "sleep"):
            result = self.worker.power_up_to_level_40(target_cp=1250)
        self.assertTrue(result.success)
        self.assertEqual(result.final_cp, 1200)


if __name__ == "__main__":
    unittest.main()
