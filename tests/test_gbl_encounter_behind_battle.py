from __future__ import annotations

from tests import support as _test_support

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sources import excellent_throw_ios
from sources import gbl_android as gbl
from sources import gbl_vision
from tests.test_gbl_screens import frame


def box(text: str, fraction: float, height: int = 1600) -> gbl_vision.OCRBox:
    return gbl_vision.OCRBox(text, 1.0, 300, int(height * fraction), 200, 30)


class EncounterBehindBattleTests(unittest.IsolatedAsyncioTestCase):
    """24 Sep: a set ended straight onto a reward Rufflet holding an Ultra Ball.

    The attack loop's home check only knows a red Poke Ball, so the razr
    fast-attacked the catch screen for three minutes -- its bottom-centre
    probes pressing the ball, which hid the plate -- timed the battle out, and
    then "started" battle 3 on the same screen.
    """

    async def read(self, plate_band, header, ball_score):
        ball = None if ball_score is None else SimpleNamespace(score=ball_score)
        reads = iter([plate_band, header])
        with (
            patch.object(gbl.gbl_vision, "recognize",
                         side_effect=lambda *_args: next(reads)),
            patch.object(excellent_throw_ios, "locate_throw_ball",
                         return_value=ball),
        ):
            return await gbl.encounter_behind_battle(frame())

    async def test_a_visible_plate_is_proof(self) -> None:
        self.assertEqual(
            await self.read([box("Rufflet", 0.33), box("CP 823", 0.36)], [], None),
            "plate",
        )

    async def test_a_pressed_ball_with_no_header_is_a_hidden_plate(self) -> None:
        self.assertEqual(await self.read([], [], 1.0), "ball")

    async def test_the_charged_minigame_keeps_its_header(self) -> None:
        # Measured: the minigame's shape scores 1.0 as a throw ball, but both
        # players' CP plates stay up across the top.
        header = [box("CP 5193", 0.05), box("CP 2728", 0.05)]
        self.assertIsNone(await self.read([], header, 1.0))

    async def test_a_battlefield_with_no_ball_is_left_alone(self) -> None:
        self.assertIsNone(await self.read([box("Kyurem", 0.40)], [], None))


if __name__ == "__main__":
    unittest.main()
