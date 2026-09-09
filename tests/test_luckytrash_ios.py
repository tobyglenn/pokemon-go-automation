from __future__ import annotations

from tests import support as _test_support

import unittest
from unittest.mock import Mock

from PIL import Image, ImageDraw

from sources import luckytrash_ios as module


class FakeDevice:
    def __init__(self) -> None:
        self.config = module.RuntimeConfig(
            "http://127.0.0.1:4723",
            {},
            {
                "FIRST_TILE_BTN": [63, 247],
                "TILE_PITCH": [125, 135],
                "X_BTN": [187, 618],
                "TRASH_MENU_BTN": [323, 618],
                "TRANSFER_BTN": [250, 548],
                "TRANSFER_YES_BTN": [187, 374],
                "TRANSFER_NO_BTN": [187, 437],
                "FILTER_CLEAR_BTN": [344, 122],
            },
            0,
            {},
        )
        self.coordinates = self.config.coordinates
        self.viewport = {"width": 375, "height": 667}

    def image_point(self, point, image):
        return int(point[0] * image.width / 375), int(point[1] * image.height / 667)

    def logical_point(self, point, image):
        return [round(point[0] * 375 / image.width), round(point[1] * 667 / image.height)]


def screen(top: tuple[int, int, int], mid: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (750, 1334), (20, 20, 20))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 750, 400), fill=top)
    draw.rectangle((0, 520, 750, 760), fill=mid)
    return image


class ScreenClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = FakeDevice()

    def test_four_known_screen_classes(self) -> None:
        white = (245, 245, 245)
        dark = (90, 70, 50)
        green = (40, 150, 110)
        self.assertEqual(module.screen_of(self.device, screen(white, white)), "list")
        self.assertEqual(module.screen_of(self.device, screen(dark, white)), "detail")
        self.assertEqual(module.screen_of(self.device, screen(green, green)), "menu")
        self.assertEqual(module.screen_of(self.device, screen(green, white)), "confirm")

    def test_dialog_yes_is_detected_from_card_not_fallback(self) -> None:
        image = Image.new("RGB", (750, 1334), (40, 150, 110))
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 360, 710, 970), fill=(250, 250, 250))
        draw.rounded_rectangle((165, 700, 585, 800), radius=50, fill=(70, 210, 150))
        yes = module.dialog_yes_point(self.device, image)
        self.assertIsNotNone(yes)
        assert yes is not None
        self.assertAlmostEqual(yes[0], 188, delta=1)
        self.assertAlmostEqual(yes[1], 375, delta=4)

    def test_no_yes_when_card_has_no_solid_pill(self) -> None:
        image = Image.new("RGB", (750, 1334), (40, 150, 110))
        ImageDraw.Draw(image).rectangle((40, 360, 710, 970), fill=(250, 250, 250))
        self.assertIsNone(module.dialog_yes_point(self.device, image))


class SafetyHelperTests(unittest.TestCase):
    def test_filter_bit_match(self) -> None:
        self.assertEqual(module.bit_match([True, False], [True, False]), 1.0)
        self.assertEqual(module.bit_match([True, False], [False, False]), 0.5)
        self.assertEqual(module.bit_match([], []), 0.0)

    def test_coordinate_validation_rejects_placeholder(self) -> None:
        with self.assertRaises(module.LuckyTrashIOSError):
            module.validate_point("YES", [0, 0])

    def test_yes_is_not_tapped_when_detection_fails(self) -> None:
        device = Mock()
        device.config = Mock(delay_modifier=0)
        confirm = Image.new("RGB", (750, 1334), (40, 150, 110))
        device.screenshot.return_value = confirm
        with unittest.mock.patch.object(module, "reach_confirmation", return_value=confirm):
            with self.assertRaises(module.LuckyTrashIOSError):
                module.transfer_one(device, 0)
        device.tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
