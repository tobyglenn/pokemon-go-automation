"""A legendary reward gets Silver Pinaps; everything else keeps the Nanab."""

from __future__ import annotations

from tests import support as _test_support

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import berry_android, gbl_android as gbl, legendary_pokemon
from sources import excellent_throw_android as throw
from sources import gbl_vision
from sources import catch_awarded_android as awarded


class LegendaryNameTests(unittest.TestCase):
    def test_the_encounter_terrakion_is_legendary(self) -> None:
        # 24 Sep: the plate box read "CP 2056", the name was its own box.
        self.assertEqual(
            legendary_pokemon.legendary_named(["CP 2056", "Terrakion"]), "Terrakion"
        )

    def test_names_run_into_other_plate_text(self) -> None:
        self.assertEqual(legendary_pokemon.legendary_named(["• Registeel / CP 1830"]),
                         "Registeel")
        self.assertEqual(legendary_pokemon.legendary_named(["Tapu Koko"]), "Tapu Koko")
        self.assertEqual(legendary_pokemon.legendary_named(["• Ho-Oh"]), "Ho-Oh")

    def test_ordinary_rewards_are_not(self) -> None:
        for text in ("• Rufflet / CP 174", "Meowth", "CP 1064"):
            self.assertIsNone(legendary_pokemon.legendary_named([text]), text)

    def test_a_short_name_must_be_a_whole_word(self) -> None:
        self.assertEqual(legendary_pokemon.legendary_named(["Mew"]), "Mew")
        self.assertIsNone(legendary_pokemon.legendary_named(["Mewing"]))


class PickerChoiceTests(unittest.TestCase):
    def setUp(self) -> None:
        berry_android.forget_empty_pockets()
        self.addCleanup(berry_android.forget_empty_pockets)

    def test_silver_is_taken_when_listed(self) -> None:
        listed = [("razz", [1, 1]), ("nanab", [2, 2]), ("silver", [5, 5])]
        self.assertEqual(
            berry_android.pick_from_picker("phone", listed, "silver"), ("silver", [5, 5])
        )

    def test_no_silver_falls_back_to_a_nanab_and_is_remembered(self) -> None:
        listed = [("razz", [1, 1]), ("nanab", [2, 2])]
        self.assertEqual(
            berry_android.pick_from_picker("phone", listed, "silver"), ("nanab", [2, 2])
        )
        self.assertEqual(berry_android.berry_to_feed("phone", "silver"), "nanab")
        self.assertEqual(berry_android.berry_to_feed("other", "silver"), "silver")

    def test_neither_listed_is_none(self) -> None:
        self.assertIsNone(
            berry_android.pick_from_picker("phone", [("razz", [1, 1])], "silver")
        )
        self.assertTrue(berry_android.pocket_known_empty("phone", "nanab"))


class RewardBerryTests(unittest.IsolatedAsyncioTestCase):
    async def berry_for(self, names: list[str]) -> str:
        device = SimpleNamespace(label="example-phone", config={}, shell=AsyncMock())
        boxes = [gbl_vision.OCRBox(text, 1.0, 300, 700, 200, 30) for text in names]
        with (
            patch.object(gbl, "read_screen", AsyncMock(return_value=object())),
            patch.object(gbl, "frame_image", return_value=Image.new("RGB", (100, 200))),
            patch.object(gbl.gbl_vision, "recognize", return_value=boxes),
            patch.object(gbl.gbl_vision, "gbl_card_visible", side_effect=[False, True]),
            patch.object(gbl.gbl_vision, "encounter_plate",
                         return_value=SimpleNamespace(text="CP 2056")),
            patch.object(gbl, "throw_ball", AsyncMock(return_value=True)) as thrown,
            patch.object(gbl, "log", lambda *_args: None),
        ):
            await gbl.take_reward_encounter(device)
        return thrown.await_args.args[2]

    async def test_a_legendary_reward_is_fed_silver_pinaps(self) -> None:
        self.assertEqual(await self.berry_for(["Terrakion", "CP 2056"]), "silver")

    async def test_an_ordinary_reward_keeps_the_nanab(self) -> None:
        self.assertEqual(await self.berry_for(["Rufflet", "CP 174"]), "nanab")

    async def test_throw_ball_hands_the_berry_to_the_thrower(self) -> None:
        with (
            TemporaryDirectory() as tmp,
            patch.object(gbl.config_paths, "state_dir", return_value=Path(tmp)),
            patch.object(gbl, "frame_image", return_value=object()),
            patch.object(gbl, "excellent_thrower", return_value=object()),
            patch.object(throw, "run_once", AsyncMock(return_value=True)) as run,
        ):
            await gbl.throw_ball(SimpleNamespace(label="example-phone"), object(), "silver")
        self.assertEqual(run.await_args.kwargs["berry"], "silver")


def plate(*texts: str) -> list:
    return [SimpleNamespace(text=text, confidence=1.0) for text in texts]


class AwardCatchLegendaryTests(unittest.IsolatedAsyncioTestCase):
    """catch.py feeds a legendary a Silver Pinap and throws Ultra Balls at it."""

    async def catch(self, boxes: list, results: list) -> AsyncMock:
        device = SimpleNamespace(label="fake-g", viewport=(720, 1600))
        args = SimpleNamespace(nanab=True, throws=4, wait=1.0, ring_hold=False)
        thrown = AsyncMock(side_effect=results)
        with patch.object(
            awarded, "capture_frame",
            AsyncMock(return_value=(Image.new("RGB", (720, 1600)), 0.0)),
        ), patch.object(awarded.gbl_vision, "recognize", return_value=boxes), patch.object(
            awarded, "throw_once", thrown
        ), patch("asyncio.sleep", AsyncMock()), TemporaryDirectory() as directory:
            await awarded.catch_one(device, args, Path(directory))
        return thrown

    async def test_a_legendary_gets_silver_on_every_throw(self) -> None:
        thrown = await self.catch(plate("Regidrago", "CP 2056"), [False, False, True])
        self.assertEqual(
            [(c.kwargs["berry"], c.kwargs["use_nanab"]) for c in thrown.await_args_list],
            [("silver", True)] * 3,
        )

    async def test_an_ordinary_award_keeps_one_nanab(self) -> None:
        thrown = await self.catch(plate("Smoliv", "CP 412"), [False, True])
        self.assertEqual(
            [(c.kwargs["berry"], c.kwargs["use_nanab"]) for c in thrown.await_args_list],
            [("nanab", True), ("nanab", False)],
        )


class UltraBallTests(unittest.IsolatedAsyncioTestCase):
    """A legendary takes the Ultra Ball even with no Silver Pinap left to feed."""

    async def test_an_empty_silver_pocket_still_switches_to_ultra(self) -> None:
        device = SimpleNamespace(label="fake-g", viewport=(720, 1600))
        swapped = AsyncMock(return_value=True)
        with patch.object(throw, "berry_in_hand", return_value=False), patch.object(
            berry_android, "berry_to_feed", return_value="nanab"
        ), patch.object(berry_android, "pocket_known_empty", return_value=True), patch.object(
            throw, "ball_in_hand", return_value=True
        ), patch.object(throw, "swap_to_ball", swapped), TemporaryDirectory() as directory:
            await throw.use_nanab_berry(
                device, None, Image.new("RGB", (720, 1600)), Path(directory), berry="silver"
            )
        self.assertEqual(swapped.await_args.args[2], throw.ULTRA_CHOICE_X)

    async def test_a_refused_silver_pinap_closes_the_sheet(self) -> None:
        """6 Oct 2026: android-three's Latios already had one, and the sheet stayed up."""
        device = SimpleNamespace(label="fake-g", viewport=(720, 1600), tap=AsyncMock())
        swapped = AsyncMock(return_value=True)
        fed = AsyncMock(return_value=True)
        frame = AsyncMock(return_value=(Image.new("RGB", (720, 1600)), 0.0))
        with patch.object(throw, "berry_in_hand", return_value=False), patch.object(
            berry_android, "berry_to_feed", return_value="silver"
        ), patch.object(berry_android, "pocket_known_empty", return_value=False), patch.object(
            berry_android, "picker_read", return_value=[("silver", [360, 1400])]
        ), patch.object(berry_android, "picker_sheet_top", return_value=1200), patch.object(
            throw, "capture_frame", frame
        ), patch.object(throw, "feed_berry", fed), patch.object(
            throw, "swap_to_ball", swapped
        ), patch("asyncio.sleep", AsyncMock()), TemporaryDirectory() as directory:
            await throw.use_nanab_berry(
                device, None, Image.new("RGB", (720, 1600)), Path(directory), berry="silver"
            )
        fed.assert_not_awaited()
        self.assertEqual(device.tap.await_args_list[-1].args[0], [360, 720])
        self.assertEqual(swapped.await_args.args[2], throw.ULTRA_CHOICE_X)


if __name__ == "__main__":
    unittest.main()
