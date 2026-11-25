from __future__ import annotations

from tests import support as _test_support

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from sources import claim_gopass as gopass
from sources.gbl_vision import OCRBox


TILE_ORANGE = (255, 174, 76)
CLAIMED_GREY = (205, 205, 205)
CARD_WHITE = (255, 255, 255)
TOGGLE_TEAL = (24, 134, 145)


def pass_image(
    *,
    size: tuple[int, int] = (480, 934),
    toggle: str | None = "all",
    orange_tiles: list[tuple[int, int, int, int]] = (),
    grey_tiles: list[tuple[int, int, int, int]] = (),
    claim_all_rect: tuple[int, int, int, int] | None = (24, 852, 130, 892),
) -> Image.Image:
    """Build a synthetic GO Pass frame matching the real two-column layout."""
    image = Image.new("RGB", size, CARD_WHITE)
    pixels = image.load()
    width, height = size

    def fill(rect: tuple[int, int, int, int], color: tuple[int, int, int]) -> None:
        x0, y0, x1, y1 = rect
        for y in range(max(0, y0), min(height, y1)):
            for x in range(max(0, x0), min(width, x1)):
                pixels[x, y] = color

    for rect in orange_tiles:
        fill(rect, TILE_ORANGE)
    for rect in grey_tiles:
        fill(rect, CLAIMED_GREY)
    if claim_all_rect is not None:
        fill(claim_all_rect, TILE_ORANGE)

    if toggle in {"all", "claimable"}:
        # Outer teal border + active half filled teal at x=355..437, y=184..219.
        tx0, ty0, tx1, ty1 = 355, 184, 437, 219
        mid = (tx0 + tx1) // 2
        for x in range(tx0, tx1):
            pixels[x, ty0] = TOGGLE_TEAL
            pixels[x, ty1 - 1] = TOGGLE_TEAL
        for y in range(ty0, ty1):
            pixels[tx0, y] = TOGGLE_TEAL
            pixels[tx1 - 1, y] = TOGGLE_TEAL
        if toggle == "all":
            fill((tx0, ty0, mid, ty1), TOGGLE_TEAL)
        else:
            fill((mid, ty0, tx1, ty1), TOGGLE_TEAL)

    return image


def header_boxes() -> list[OCRBox]:
    return [
        OCRBox("RANK", 1.0, 30, 184, 32, 12),
        OCRBox("Time left to claim rewards:", 1.0, 88, 180, 226, 22),
        OCRBox("BASIC", 1.0, 142, 248, 54, 16),
        OCRBox("DELUXE", 1.0, 322, 248, 68, 16),
    ]


class FakePhone:
    def __init__(self) -> None:
        self.label = "fake-pass"
        self.viewport = (480, 934)
        self.taps: list[list[int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []

    async def tap(self, point) -> None:
        self.taps.append(list(point))

    async def input_swipe(self, x0, y0, x1, y1, duration_ms) -> None:
        self.swipes.append((x0, y0, x1, y1, duration_ms))


class ReadPassScreenTests(unittest.TestCase):
    def read(self, image: Image.Image, boxes: list[OCRBox]) -> gopass.PassScreen | None:
        with patch.object(gopass.gbl_vision, "recognize", return_value=boxes):
            return gopass.read_pass_screen(image)

    def test_detects_both_basic_and_deluxe_orange_pokemon_tiles(self) -> None:
        image = pass_image(
            toggle="all",
            orange_tiles=[(82, 320, 254, 470), (270, 320, 442, 470)],
            grey_tiles=[(82, 495, 254, 580)],
        )
        boxes = header_boxes() + [
            OCRBox("POKÉMON APPEARS!", 1.0, 90, 330, 138, 14),
            OCRBox("POKÉMON APPEARS!", 1.0, 278, 330, 138, 14),
            OCRBox("STARDUST", 1.0, 90, 502, 72, 16),
            OCRBox("CLAIM ALL", 1.0, 38, 862, 82, 17),
        ]
        screen = self.read(image, boxes)
        self.assertIsNotNone(screen)
        assert screen is not None
        self.assertEqual(screen.toggle_state, "all")
        self.assertIsNotNone(screen.toggle_claimable_point)
        self.assertEqual(len(screen.tiles), 2)
        self.assertEqual([t.column for t in screen.tiles], ["basic", "deluxe"])
        self.assertFalse(screen.tiles[0].overlaps(screen.tiles[1]))
        self.assertFalse(screen.unclaimed_non_pokemon)
        self.assertIsNotNone(screen.claim_all_point)

    def test_detects_claimable_filter_toggle_state(self) -> None:
        image = pass_image(toggle="claimable")
        screen = self.read(image, header_boxes())
        self.assertIsNotNone(screen)
        assert screen is not None
        self.assertEqual(screen.toggle_state, "claimable")

    def test_ignores_claimed_grey_pokemon_tile(self) -> None:
        image = pass_image(
            toggle="all",
            grey_tiles=[(82, 320, 254, 470)],
        )
        boxes = header_boxes() + [
            OCRBox("POKÉMON APPEARS!", 1.0, 90, 330, 138, 14),
        ]
        screen = self.read(image, boxes)
        self.assertIsNotNone(screen)
        assert screen is not None
        self.assertEqual(screen.tiles, [])
        self.assertEqual(screen.all_tiles_count, 0)

    def test_skips_tile_clipped_behind_bottom_controls_but_counts_it(self) -> None:
        image = pass_image(
            toggle="claimable",
            orange_tiles=[(270, 845, 442, 930)],
        )
        boxes = header_boxes() + [
            OCRBox("POKÉMON APPEARS!", 1.0, 278, 858, 138, 14),
            OCRBox("CLAIM ALL", 1.0, 38, 862, 82, 17),
        ]
        screen = self.read(image, boxes)
        self.assertIsNotNone(screen)
        assert screen is not None
        self.assertEqual(screen.tiles, [])
        self.assertEqual(screen.all_tiles_count, 1)

    def test_detects_unclaimed_non_pokemon_orange_item_tile(self) -> None:
        image = pass_image(
            toggle="all",
            orange_tiles=[(82, 495, 254, 580)],
        )
        boxes = header_boxes() + [
            OCRBox("STARDUST", 1.0, 90, 502, 72, 16),
            OCRBox("CLAIM ALL", 1.0, 38, 862, 82, 17),
        ]
        screen = self.read(image, boxes)
        self.assertIsNotNone(screen)
        assert screen is not None
        self.assertTrue(screen.unclaimed_non_pokemon)

    def test_scales_tap_coordinates_back_to_native_resolution(self) -> None:
        image = pass_image(
            size=(480, 934),
            toggle="all",
            orange_tiles=[(82, 320, 254, 470)],
        ).resize((960, 1868))
        boxes = header_boxes() + [
            OCRBox("POKÉMON APPEARS!", 1.0, 90, 330, 138, 14),
        ]
        screen = self.read(image, boxes)
        self.assertIsNotNone(screen)
        assert screen is not None
        self.assertEqual(len(screen.tiles), 1)
        self.assertAlmostEqual(screen.tiles[0].point[0], 318, delta=6)
        self.assertAlmostEqual(screen.tiles[0].point[1], 698, delta=10)

    def test_non_pass_screen_returns_none(self) -> None:
        image = Image.new("RGB", (480, 934), CARD_WHITE)
        boxes = [
            OCRBox("TODAY", 1.0, 40, 60, 80, 20),
            OCRBox("CLAIM REWARD", 1.0, 140, 340, 160, 22),
        ]
        self.assertIsNone(self.read(image, boxes))


class ClaimOneTileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.args = gopass.parse_args(["--claim-wait", "5"])
        self.tile = gopass.PassTile(
            label="POKÉMON APPEARS!",
            point=[159, 349],
            left=90,
            top=330,
            right=228,
            bottom=464,
            column="basic",
        )

    async def test_taps_tile_and_catches_encounter(self) -> None:
        device = FakePhone()
        with patch.object(
            gopass.claim_research_android, "encounter_on_screen", AsyncMock(return_value=True)
        ), patch.object(
            gopass.catch_awarded_android, "catch_one", AsyncMock(return_value=True)
        ) as catch_one, patch.object(
            gopass.catch_awarded_android, "clear_catch_screens", AsyncMock()
        ), patch("asyncio.sleep", AsyncMock()):
            result = await gopass.claim_one_tile(
                device, self.tile, self.args, Path(tempfile.mkdtemp()) / "t1"
            )
        self.assertEqual(device.taps, [[159, 349]])
        self.assertEqual(result, (True, True, None))
        catch_one.assert_awaited_once()
        self.assertEqual(catch_one.await_args.kwargs.get("default_berry"), "pinap")

    async def test_waits_through_dynamax_intro_before_encounter_settles(self) -> None:
        device = FakePhone()
        encounters = [False, False, True]
        blank = Image.new("RGB", (480, 934), (40, 20, 60))

        async def encounter(_device):
            return encounters.pop(0) if encounters else True

        with patch.object(
            gopass.claim_research_android, "encounter_on_screen", encounter
        ), patch.object(
            gopass.catch_awarded_android, "capture_frame", AsyncMock(return_value=(blank, 0.0))
        ), patch.object(
            gopass.catch_awarded_android, "card_from_frame", lambda _i: None
        ), patch.object(
            gopass, "_dialog_notice", lambda _i: None
        ), patch.object(
            gopass, "read_pass_screen", lambda _i: None
        ), patch.object(
            gopass.catch_awarded_android, "clear_catch_screens", AsyncMock()
        ), patch.object(
            gopass.catch_awarded_android, "catch_one", AsyncMock(return_value=True)
        ) as catch_one, patch("asyncio.sleep", AsyncMock()):
            result = await gopass.claim_one_tile(
                device, self.tile, self.args, Path(tempfile.mkdtemp()) / "t2"
            )
        self.assertEqual(result, (True, True, None))
        catch_one.assert_awaited_once()

    async def test_stops_cleanly_on_pokemon_storage_full(self) -> None:
        device = FakePhone()
        blank = Image.new("RGB", (480, 934), CARD_WHITE)
        with patch.object(
            gopass.claim_research_android, "encounter_on_screen", AsyncMock(return_value=False)
        ), patch.object(
            gopass.catch_awarded_android, "capture_frame", AsyncMock(return_value=(blank, 0.0))
        ), patch.object(
            gopass.catch_awarded_android, "card_from_frame", lambda _i: None
        ), patch.object(
            gopass, "_dialog_notice", lambda _i: "storage_full"
        ), patch("asyncio.sleep", AsyncMock()):
            result = await gopass.claim_one_tile(
                device, self.tile, self.args, Path(tempfile.mkdtemp()) / "t3"
            )
        self.assertEqual(result, (False, False, "storage_full"))


class RunDeviceTests(unittest.IsolatedAsyncioTestCase):
    def tile(self, col: str, top: int = 330) -> gopass.PassTile:
        x = 159 if col == "basic" else 347
        return gopass.PassTile(
            label="POKÉMON APPEARS!",
            point=[x, top + 19],
            left=x - 69,
            top=top,
            right=x + 69,
            bottom=top + 134,
            column=col,
        )

    async def test_toggles_claimable_filter_then_claims_basic_and_deluxe(self) -> None:
        device = FakePhone()
        args = gopass.parse_args(["--artifacts", tempfile.mkdtemp(), "--scrolls", "1"])
        t_basic = self.tile("basic")
        t_deluxe = self.tile("deluxe")
        screens = [
            gopass.PassScreen([t_basic, t_deluxe], 2, [79, 870], "all", [416, 202], False),
            gopass.PassScreen([t_basic, t_deluxe], 2, [79, 870], "claimable", [416, 202], False),
            gopass.PassScreen([t_deluxe], 1, [79, 870], "claimable", [416, 202], False),
            gopass.PassScreen([], 0, None, "claimable", [416, 202], False),
            gopass.PassScreen([], 0, None, "claimable", [416, 202], False),
        ]
        claimed_cols: list[str] = []

        async def read_pass(_device):
            return screens.pop(0) if screens else screens[-1] if screens else None

        async def claim_tile(_device, tile, _args, _dir):
            claimed_cols.append(tile.column)
            return True, True, None

        with patch.object(gopass, "read_device_pass", read_pass), patch.object(
            gopass, "claim_one_tile", claim_tile
        ), patch.object(
            gopass.claim_research_android, "plate_on_screen", AsyncMock(return_value=False)
        ), patch.object(
            gopass.claim_research_android, "scroll_list", AsyncMock()
        ) as scroll, patch("asyncio.sleep", AsyncMock()):
            status = await gopass.run_device(device, args)

        self.assertEqual(status, 0)
        self.assertEqual(device.taps, [[416, 202]], "did not switch to Claimable Rewards")
        self.assertEqual(claimed_cols, ["basic", "deluxe"])
        self.assertEqual(scroll.await_count, 1)

    async def test_presses_claim_all_once_when_unclaimed_items_present(self) -> None:
        device = FakePhone()
        args = gopass.parse_args(["--artifacts", tempfile.mkdtemp(), "--scrolls", "0"])
        t_basic = self.tile("basic")
        screens = [
            gopass.PassScreen([t_basic], 1, [79, 870], "all", [416, 202], True),
            gopass.PassScreen([t_basic], 1, [79, 870], "claimable", [416, 202], False),
            gopass.PassScreen([], 0, None, "claimable", [416, 202], False),
        ]

        async def read_pass(_device):
            return screens.pop(0)

        with patch.object(gopass, "read_device_pass", read_pass), patch.object(
            gopass, "claim_one_tile", AsyncMock(return_value=(True, True, None))
        ) as claim_tile, patch.object(
            gopass.catch_awarded_android, "clear_catch_screens", AsyncMock()
        ), patch.object(
            gopass.claim_research_android, "plate_on_screen", AsyncMock(return_value=False)
        ), patch("asyncio.sleep", AsyncMock()):
            status = await gopass.run_device(device, args)

        self.assertEqual(status, 0)
        self.assertEqual(device.taps, [[79, 870]])
        claim_tile.assert_awaited_once()

    async def test_respects_count_limit(self) -> None:
        device = FakePhone()
        args = gopass.parse_args(["--artifacts", tempfile.mkdtemp(), "--count", "1"])
        t_basic = self.tile("basic")
        t_deluxe = self.tile("deluxe")
        screen = gopass.PassScreen([t_basic, t_deluxe], 2, None, "claimable", [416, 202], False)

        with patch.object(gopass, "read_device_pass", AsyncMock(return_value=screen)), patch.object(
            gopass, "claim_one_tile", AsyncMock(return_value=(True, True, None))
        ) as claim_tile, patch("asyncio.sleep", AsyncMock()):
            status = await gopass.run_device(device, args)

        self.assertEqual(status, 0)
        self.assertEqual(claim_tile.await_count, 1)

    async def test_award_handoffs_keep_the_remaining_count_local_to_each_phone(self) -> None:
        device = FakePhone()
        pass_screen = gopass.PassScreen([self.tile("basic")], 1, None, "claimable", [416, 202], False)
        blank = Image.new("RGB", (480, 934))
        award_card = gopass.catch_awarded_android.AwardCard(remaining=9, start_point=[240, 640])
        for from_tile in (False, True):
            for count in (None, 3):
                with self.subTest(from_tile=from_tile, count=count), tempfile.TemporaryDirectory() as directory:
                    flags = ["--count", str(count)] if count is not None else []
                    args = gopass.parse_args(["--artifacts", directory, *flags])
                    screens = [pass_screen, pass_screen if from_tile else None]
                    claims = [(True, True, None), (True, False, "award_card")]
                    with patch.object(gopass, "read_device_pass", AsyncMock(side_effect=screens)), \
                            patch.object(gopass, "claim_one_tile", AsyncMock(side_effect=claims)), \
                            patch.object(gopass.catch_awarded_android, "capture_frame", AsyncMock(return_value=(blank, 0.0))), \
                            patch.object(gopass.catch_awarded_android, "card_from_frame", return_value=award_card), \
                            patch.object(gopass.catch_awarded_android, "run_device", AsyncMock(return_value=0)) as award, \
                            patch("asyncio.sleep", AsyncMock()):
                        self.assertEqual(await gopass.run_device(device, args), 0)
                    award.assert_awaited_once()
                    self.assertIs(award.await_args.args[0], device)
                    self.assertEqual(award.await_args.args[1].count, None if count is None else count - 1)
                    self.assertEqual(args.count, count)

    async def test_dry_run_sends_no_touch(self) -> None:
        device = FakePhone()
        args = gopass.parse_args(["--artifacts", tempfile.mkdtemp(), "--dry-run"])
        screen = gopass.PassScreen([self.tile("basic")], 1, [79, 870], "all", [416, 202], True)
        with patch.object(gopass, "read_device_pass", AsyncMock(return_value=screen)):
            status = await gopass.run_device(device, args)
        self.assertEqual(status, 0)
        self.assertEqual(device.taps, [])

    async def test_dry_run_without_pass_leaves_an_open_encounter_alone(self) -> None:
        device = FakePhone()
        with tempfile.TemporaryDirectory() as directory:
            args = gopass.parse_args(["--mode", "pass", "--dry-run", "--artifacts", directory, "--count", "1"])
            blank = Image.new("RGB", (480, 934))
            with patch.object(gopass, "read_device_pass", AsyncMock(return_value=None)), \
                    patch.object(gopass.catch_awarded_android, "capture_frame", AsyncMock(return_value=(blank, 0.0))), \
                    patch.object(gopass.catch_awarded_android, "card_from_frame", return_value=None), \
                    patch.object(gopass.claim_research_android, "plate_on_screen", AsyncMock(return_value=True)), \
                    patch.object(gopass.catch_awarded_android, "catch_one", AsyncMock(return_value=True)) as catch, \
                    patch.object(gopass.catch_awarded_android, "clear_catch_screens", AsyncMock()) as clear:
                self.assertEqual(await gopass.run_device(device, args), 0)
        catch.assert_not_awaited()
        clear.assert_not_awaited()

    async def test_dry_run_does_not_handoff_to_an_award_queue(self) -> None:
        device = FakePhone()
        with tempfile.TemporaryDirectory() as directory:
            args = gopass.parse_args(["--mode", "pass", "--dry-run", "--artifacts", directory])
            blank = Image.new("RGB", (480, 934))
            card = gopass.catch_awarded_android.AwardCard(remaining=2, start_point=[240, 640])
            with patch.object(gopass, "read_device_pass", AsyncMock(return_value=None)), \
                    patch.object(gopass.catch_awarded_android, "capture_frame", AsyncMock(return_value=(blank, 0.0))), \
                    patch.object(gopass.catch_awarded_android, "card_from_frame", return_value=card), \
                    patch.object(gopass.catch_awarded_android, "run_device", AsyncMock()) as award:
                self.assertEqual(await gopass.run_device(device, args), 0)
        award.assert_not_awaited()
        self.assertEqual(device.taps, [])


if __name__ == "__main__":
    unittest.main()
