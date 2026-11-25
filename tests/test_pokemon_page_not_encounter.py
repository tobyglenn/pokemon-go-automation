"""A caught Pokemon's page, and the size record card over it, are not encounters.

The boxes are the SE's own OCR of the New Size Record frame it stood on for
twelve minutes on 14 Nov, at 750x1334.
"""

import unittest

from sources import gbl_vision
from sources.gbl_vision import OCRBox

WIDTH, HEIGHT = 750, 1334


def box(text: str, cx: float, cy: float, w: int = 150, h: int = 50) -> OCRBox:
    return OCRBox(text, 1.0, int(cx * WIDTH - w / 2), int(cy * HEIGHT - h / 2), w, h)


RECORD_PAGE = [
    box("SOS only •", 0.111, 0.017),
    box("9:49 AM", 0.5, 0.016),
    box("CP 767", 0.468, 0.088),
    box("Honedge", 0.505, 0.516),
    box("New Size Record", 0.492, 0.606),
    box("• 0.2 m", 0.719, 0.658),
    box("4 0.4 m", 0.444, 0.708),
    box("+ 0.11 kg", 0.717, 0.755),
    box("0.55 kg", 0.567, 0.809),
]

# The same page once the card is closed: header, name, weight, POWER UP.
CAUGHT_PAGE = [
    box("CP 767", 0.468, 0.088),
    box("Honedge", 0.505, 0.45),
    box("0.55kg", 0.25, 0.55),
    box("POWER UP", 0.5, 0.80),
]

# A tall flyer's real encounter: the plate high and centred, the ball count.
HIGH_ENCOUNTER = [
    box("Lugia CP 2056", 0.5, 0.12),
    box("37", 0.65, 0.96, w=40),
]


class PokemonPageTest(unittest.TestCase):
    def test_record_card_is_named(self):
        self.assertTrue(gbl_vision.size_record_visible(RECORD_PAGE))
        self.assertTrue(gbl_vision.pokemon_page_visible(RECORD_PAGE))

    def test_record_card_header_is_not_a_plate(self):
        self.assertIsNone(gbl_vision.encounter_plate(RECORD_PAGE, HEIGHT, WIDTH))
        self.assertIsNone(gbl_vision.encounter_ball_count(RECORD_PAGE, WIDTH, HEIGHT))

    def test_caught_page_header_is_not_a_plate(self):
        self.assertFalse(gbl_vision.size_record_visible(CAUGHT_PAGE))
        self.assertTrue(gbl_vision.pokemon_page_visible(CAUGHT_PAGE))
        self.assertIsNone(gbl_vision.encounter_plate(CAUGHT_PAGE, HEIGHT, WIDTH))

    def test_real_high_encounter_still_reads(self):
        self.assertFalse(gbl_vision.pokemon_page_visible(HIGH_ENCOUNTER))
        plate = gbl_vision.encounter_plate(HIGH_ENCOUNTER, HEIGHT, WIDTH)
        self.assertIsNotNone(plate)
        self.assertEqual(plate.text, "Lugia CP 2056")
        self.assertIsNotNone(
            gbl_vision.encounter_ball_count(HIGH_ENCOUNTER, WIDTH, HEIGHT)
        )

    def test_other_record_wordings(self):
        for text in ("New Size Record", "NEW RECORD", "New XXL Record"):
            self.assertTrue(gbl_vision.size_record_visible([box(text, 0.5, 0.6)]))


if __name__ == "__main__":
    unittest.main()
