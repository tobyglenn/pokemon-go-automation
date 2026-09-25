from __future__ import annotations

from tests import support as _test_support

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from sources import remove_friends_android as remove
from sources.gbl_vision import OCRBox


# --- The heart strip, in the colours the razr draws it in ---
# Measured on the razr (1224x2992, rows 391px) on 22 Sep 2026: five 36px
# circles on a 40px pitch, 0.72-0.82 of the way down each row.
DISC_RED = (250, 85, 45)
HEART_YELLOW = (245, 225, 160)
PROGRESS_AMBER = (250, 145, 45)
RING_GREY = (210, 210, 210)
BEST_PURPLE = (170, 90, 230)
ROW_RULE_GREY = (228, 228, 228)
WIDTH, HEIGHT = 1224, 2992
LIST_TOP = 673
ROW_PITCH = 391
CIRCLE_X = 102
CIRCLE_PITCH = 40
CIRCLE_RADIUS = 18

REACHED, PROGRESS, EMPTY, FOREIGN = "reached", "progress", "empty", "foreign"


def friends_list(rows: list[list[str]]) -> Image.Image:
    """A friends list whose rows carry these five circle states each."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for index, circles in enumerate(rows):
        top = LIST_TOP + index * ROW_PITCH
        for rule in (top, top + ROW_PITCH):
            draw.line([(int(WIDTH * 0.04), rule), (int(WIDTH * 0.93), rule)], fill=ROW_RULE_GREY, width=3)
        cy = top + round(ROW_PITCH * 0.77)
        for k, state in enumerate(circles):
            cx = CIRCLE_X + k * CIRCLE_PITCH
            box = [cx - CIRCLE_RADIUS, cy - CIRCLE_RADIUS, cx + CIRCLE_RADIUS, cy + CIRCLE_RADIUS]
            inner = [cx - 9, cy - 9, cx + 9, cy + 9]
            if state == REACHED:
                draw.ellipse(box, fill=DISC_RED)
                draw.ellipse(inner, fill=HEART_YELLOW)
            elif state == FOREIGN:
                draw.ellipse(box, fill=BEST_PURPLE)
                draw.ellipse(inner, fill=HEART_YELLOW)
            else:
                colour = PROGRESS_AMBER if state == PROGRESS else RING_GREY
                draw.ellipse(box, outline=colour, width=5)
                draw.ellipse(inner, outline=RING_GREY, width=2)
    return image


def rows_of(image: Image.Image) -> list[tuple[int, int]]:
    return remove.gift_android.friend_rows(remove.as_frame(image))


def strip(*states: str) -> list[str]:
    return list(states) + [EMPTY] * (5 - len(states))


class HeartCountTests(unittest.TestCase):
    """Hearts are the solid discs, never the lit rings."""

    def count(self, circles: list[str]) -> int | None:
        image = friends_list([circles])
        return remove.count_hearts(image, rows_of(image)[0])

    def test_five_grey_rings_are_zero_hearts(self) -> None:
        self.assertEqual(self.count(strip()), 0)

    def test_one_disc_is_one_heart(self) -> None:
        self.assertEqual(self.count(strip(REACHED)), 1)

    def test_a_closed_progress_ring_is_not_a_heart(self) -> None:
        # Boggy2600: one disc, one fully lit amber ring -- a one-heart friend.
        self.assertEqual(self.count(strip(REACHED, PROGRESS)), 1)

    def test_two_discs_and_a_ring_are_two_hearts(self) -> None:
        self.assertEqual(self.count(strip(REACHED, REACHED, PROGRESS)), 2)

    def test_three_discs_are_three_hearts(self) -> None:
        self.assertEqual(self.count(strip(REACHED, REACHED, REACHED)), 3)

    def test_an_unknown_colour_cannot_be_read(self) -> None:
        # A higher level drawn in a colour nobody has measured must not be
        # counted as "not reached", or a best friend reads as two hearts.
        self.assertIsNone(self.count(strip(REACHED, REACHED, FOREIGN, FOREIGN)))

    def test_a_disc_after_a_gap_cannot_be_read(self) -> None:
        self.assertIsNone(self.count(strip(REACHED, EMPTY, REACHED)))

    def test_a_row_with_no_strip_cannot_be_read(self) -> None:
        image = friends_list([[]])
        self.assertIsNone(remove.count_hearts(image, rows_of(image)[0]))

    def test_a_short_top_row_is_still_read(self) -> None:
        # M4C7R1CK5's black hood read as the top rule, 97px into the row,
        # which slid the heart band down off the circles.
        image = friends_list([strip(), strip(REACHED)])
        draw = ImageDraw.Draw(image)
        draw.line([(0, LIST_TOP), (WIDTH, LIST_TOP)], fill=(255, 255, 255), width=5)
        hood = LIST_TOP + 97
        draw.line([(int(WIDTH * 0.04), hood), (int(WIDTH * 0.93), hood)], fill=ROW_RULE_GREY, width=3)
        self.assertAlmostEqual(rows_of(image)[0][0], hood, delta=2)
        rows = remove.read_rows(image, [], 1.0)
        self.assertEqual([row.hearts for row in rows], [0, 1])


def row(**changes) -> remove.FriendRow:
    values = dict(
        name="Trainer", point=(500, 800), hearts=0, gift=False, halo=False,
        trade=False, top=673, bottom=1064,
    )
    values.update(changes)
    return remove.FriendRow(**values)


class KeepReasonTests(unittest.TestCase):
    def test_zero_hearts_with_nothing_else_goes(self) -> None:
        self.assertIsNone(row().keep_reason(0))

    def test_hearts_above_the_limit_stay(self) -> None:
        self.assertEqual(row(hearts=1).keep_reason(0), "1 heart(s)")
        self.assertIsNone(row(hearts=1).keep_reason(1))

    def test_a_gift_keeps_a_friend(self) -> None:
        self.assertEqual(row(gift=True).keep_reason(3), "gift waiting")

    def test_the_halo_keeps_a_friend(self) -> None:
        self.assertEqual(row(halo=True).keep_reason(3), "blue halo")

    def test_anything_unreadable_stays(self) -> None:
        self.assertEqual(row(hearts=None).keep_reason(3), "hearts unreadable")
        self.assertEqual(row(name=None).keep_reason(3), "name unreadable")
        self.assertEqual(row(point=None).keep_reason(3), "under the header or buttons")


def box(text: str, x: int, y: int, width: int = 120, height: int = 24) -> OCRBox:
    return OCRBox(text=text, confidence=1.0, x=x, y=y, width=width, height=height)


class ReadingTests(unittest.TestCase):
    def test_the_dialog_names_the_friend(self) -> None:
        boxes = [
            box("Are you sure you want to", 130, 500),
            box("unfriend IAmArc3us?", 160, 540),
            box("If you remove this friend in this", 110, 620),
            box("YES", 290, 860),
        ]
        self.assertEqual(remove.dialog_name(boxes), "iamarc3us")

    def test_no_dialog_no_name(self) -> None:
        self.assertIsNone(remove.dialog_name([box("REMOVE FRIEND", 200, 1280)]))

    def test_names_match_through_ocr_lookalikes(self) -> None:
        # What Vision read off the razr's dialog for IAmArc3us.
        self.assertTrue(remove.same_name("IAmArc3us", "lamarcus"))
        self.assertTrue(remove.same_name("Steve135x", "steve 135x"))

    def test_a_neighbour_is_not_a_match(self) -> None:
        self.assertFalse(remove.same_name("Imafukinidi8", "lamarcus"))
        self.assertFalse(remove.same_name("KECHZANO", "ItalianDeGe29"))

    def test_the_friend_count_is_read_under_the_tab(self) -> None:
        boxes = [box("ME", 40, 30, 40), box("FRIENDS", 260, 30, 100), box("649", 290, 60, 40)]
        self.assertEqual(remove.friend_count(boxes), 649)


class RemovalTests(unittest.IsolatedAsyncioTestCase):
    """YES is only pressed for the friend whose row was chosen."""

    def device(self):
        device = AsyncMock()
        device.viewport = (WIDTH, HEIGHT)
        device.label = "phone"
        return device

    async def run_removal(self, screens):
        device = self.device()
        reads = iter(screens)

        async def read_screen(_device):
            boxes = next(reads)
            return Image.new("RGB", (8, 8)), boxes, 1.0

        with patch.object(remove, "read_screen", read_screen), \
                patch.object(remove, "back_to_list", AsyncMock(return_value=True)), \
                patch.object(remove.excellent_throw_android, "capture_frame",
                             AsyncMock(return_value=(friends_list([strip()]), 0.0))), \
                patch.object(remove.asyncio, "sleep", AsyncMock()):
            result = await remove.remove_friend(device, row(name="IAmArc3us"))
        return device, result

    profile = [box("REMOVE FRIEND", 300, 2560, 600, 30)]
    dialog_for = staticmethod(lambda name: [
        box(f"unfriend {name}?", 300, 1100, 600, 30),
        box("YES", 560, 1740, 100, 30),
        box("NO", 580, 1940, 60, 30),
    ])

    async def test_the_right_name_is_confirmed(self) -> None:
        dialog = self.dialog_for("IAmArc3us")
        device, (removed, _what) = await self.run_removal([self.profile, dialog, dialog])
        self.assertTrue(removed)
        taps = [call.args[0] for call in device.tap.await_args_list]
        self.assertEqual(taps[-1], (610, 1755))

    async def test_another_name_gets_no(self) -> None:
        dialog = self.dialog_for("KECHZANO")
        device, (removed, what) = await self.run_removal([self.profile, dialog, dialog])
        self.assertFalse(removed)
        self.assertIn("pressed NO", what)
        taps = [call.args[0] for call in device.tap.await_args_list]
        self.assertNotIn((610, 1755), taps)
        self.assertEqual(taps[-1], (610, 1955))

    async def test_a_gift_postcard_is_left_alone(self) -> None:
        postcard = [box("OPEN", 560, 2610, 100, 30)]
        device, (removed, what) = await self.run_removal([postcard])
        self.assertFalse(removed)
        self.assertIn("gift", what)
        self.assertEqual(len(device.tap.await_args_list), 1)


if __name__ == "__main__":
    unittest.main()
