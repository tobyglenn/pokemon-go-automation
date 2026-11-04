#!/usr/bin/env python3
"""Unit tests for raid preparation workers (iOS & Android) and task runner."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from scripts import raid_prep
from sources import raid_prep_android, raid_prep_ios, vision_ocr


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


if __name__ == "__main__":
    unittest.main()
