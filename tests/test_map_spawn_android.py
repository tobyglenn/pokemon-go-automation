from __future__ import annotations

from tests import support as _test_support  # noqa: F401

import math
import unittest

from PIL import Image, ImageDraw

from sources import map_spawn_android as spawn


REFERENCE_WIDTH = 1316
REFERENCE_HEIGHT = 2560

GRASS = (110, 190, 150)
# HSV approximately (40 degrees, 0.632, 0.745): inside the gold thresholds.
GOLD = (190, 150, 70)
# HSV approximately (48.3 degrees, 0.72, 0.98): bright Pikachu-like yellow.
LEMON = (250, 215, 70)
# HSV approximately (240 degrees, 0.032, 0.608): exposed silver body.
GREY = (150, 150, 155)
RED = (220, 30, 30)
WHITE = (250, 250, 250)
BLUE = (35, 90, 220)

TAP_TOLERANCE = 40
SCALE_TOLERANCE = 4


def _coordinate(image: Image.Image, value: float) -> int:
    return round(value * image.width / REFERENCE_WIDTH)


def _rectangle(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    colour: tuple[int, int, int],
) -> None:
    """Draw reference-coordinate rectangles with exclusive right/bottom edges."""
    left, top, right, bottom = (
        _coordinate(image, value) for value in bounds
    )
    ImageDraw.Draw(image).rectangle(
        (left, top, right - 1, bottom - 1), fill=colour,
    )


def _ball(
    image: Image.Image,
    radius: int = 70,
    upper_colour: tuple[int, int, int] = RED,
) -> None:
    centre_x = _coordinate(image, 658)
    centre_y = _coordinate(image, 2380)
    radius = _coordinate(image, radius)
    bounds = (
        centre_x - radius,
        centre_y - radius,
        centre_x + radius,
        centre_y + radius,
    )
    draw = ImageDraw.Draw(image)
    draw.ellipse(bounds, fill=WHITE)
    draw.pieslice(bounds, 180, 360, fill=upper_colour)


def _map(width: int = REFERENCE_WIDTH) -> Image.Image:
    height = round(REFERENCE_HEIGHT * width / REFERENCE_WIDTH)
    image = Image.new("RGB", (width, height), GRASS)
    _ball(image)
    return image


def _meltan(
    image: Image.Image,
    body_colour: tuple[int, int, int] = GREY,
) -> None:
    # A 30x45 nut directly above a same-width 30x24 body. The body is
    # >=0.015w tall and covers the new ceil(0.012w)-deep sampling box.
    _rectangle(image, (825, 1620, 855, 1644), body_colour)
    _rectangle(image, (825, 1575, 855, 1620), GOLD)


def _distance(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


class OnMapTests(unittest.TestCase):
    def test_plain_grass_is_not_map(self) -> None:
        image = Image.new(
            "RGB", (REFERENCE_WIDTH, REFERENCE_HEIGHT), GRASS,
        )

        self.assertFalse(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_meltan_colours_cannot_bypass_map_gate(self) -> None:
        image = Image.new(
            "RGB", (REFERENCE_WIDTH, REFERENCE_HEIGHT), GRASS,
        )
        _meltan(image)

        self.assertFalse(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_encounter_sized_ball_is_not_map(self) -> None:
        for upper_colour in (RED, BLUE):
            with self.subTest(upper_colour=upper_colour):
                image = Image.new(
                    "RGB", (REFERENCE_WIDTH, REFERENCE_HEIGHT), GRASS,
                )
                # Diameter 330 pixels is approximately 0.25 reference width.
                _ball(image, radius=165, upper_colour=upper_colour)
                _meltan(image)

                self.assertFalse(spawn.on_map(image))
                self.assertIsNone(spawn.find_meltan(image))

    def test_small_blue_ball_does_not_match_map_menu(self) -> None:
        image = _map()
        _ball(image, upper_colour=BLUE)

        self.assertFalse(spawn.on_map(image))


class FindMeltanTests(unittest.TestCase):
    def test_detects_mustard_nut_and_grey_body(self) -> None:
        for mode in ("RGB", "RGBA"):
            with self.subTest(mode=mode):
                image = _map()
                _meltan(image)
                image = image.convert(mode)

                self.assertTrue(spawn.on_map(image))
                point = spawn.find_meltan(image)

                if point is None:
                    self.fail("Expected a Meltan tap point")
                self.assertLess(
                    _distance(point, (840, 1600)), TAP_TOLERANCE,
                )

    def test_detects_with_half_width_body_support(self) -> None:
        image = _map()
        _meltan(image)
        # Leave approximately half the tight body box grey. This exercises
        # acceptance with ~0.5 support rather than requiring a solid body.
        _rectangle(image, (840, 1620, 855, 1644), GRASS)

        self.assertTrue(spawn.on_map(image))
        point = spawn.find_meltan(image)

        if point is None:
            self.fail("Expected detection with half-width grey body support")
        self.assertLess(_distance(point, (840, 1600)), TAP_TOLERANCE)

    def test_rejects_lemon_buddy_with_red_cap(self) -> None:
        image = _map()
        _rectangle(image, (810, 1575, 880, 1615), LEMON)
        _rectangle(image, (820, 1545, 865, 1575), RED)

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_red_hat_vetoes_gold_with_plausible_grey_body(self) -> None:
        # This gold certainly enters the mask. The otherwise valid candidate
        # must be rejected specifically by its hat.
        image = _map()
        _meltan(image)
        _rectangle(image, (820, 1545, 865, 1575), RED)

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_rejects_long_thin_mustard_line(self) -> None:
        image = _map()
        _rectangle(image, (350, 1580, 950, 1588), GOLD)

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_coordinates_scale_with_image_width(self) -> None:
        full = _map()
        half = _map(REFERENCE_WIDTH // 2)
        _meltan(full)
        _meltan(half)

        self.assertTrue(spawn.on_map(full))
        self.assertTrue(spawn.on_map(half))
        full_point = spawn.find_meltan(full)
        half_point = spawn.find_meltan(half)

        if full_point is None:
            self.fail("Expected full-size Meltan detection")
        if half_point is None:
            self.fail("Expected half-size Meltan detection")
        self.assertLess(
            _distance(
                half_point,
                (full_point[0] / 2, full_point[1] / 2),
            ),
            SCALE_TOLERANCE,
        )
        self.assertLess(
            _distance(half_point, (420, 800)), TAP_TOLERANCE / 2,
        )

    def test_gold_without_grey_body_is_rejected(self) -> None:
        image = _map()
        _rectangle(image, (825, 1575, 855, 1620), GOLD)

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_body_requires_substantial_grey_support(self) -> None:
        image = _map()
        _meltan(image, body_colour=GRASS)
        # A narrow grey sliver is not sufficient evidence of an exposed body.
        _rectangle(image, (825, 1620, 832, 1644), GREY)

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_white_avatar_clothes_do_not_count_as_body(self) -> None:
        # Regression for the m2-style occlusion: a valid nut above bright,
        # neutral clothing must not become a tappable Meltan candidate.
        image = _map()
        _meltan(image, body_colour=WHITE)

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))

    def test_dark_shadow_does_not_count_as_body(self) -> None:
        image = _map()
        _meltan(image, body_colour=(80, 80, 85))

        self.assertTrue(spawn.on_map(image))
        self.assertIsNone(spawn.find_meltan(image))


if __name__ == "__main__":
    unittest.main()
