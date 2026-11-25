from __future__ import annotations

from tests import support as _test_support  # noqa: F401

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from scripts import tag_living_dex
from sources import gbl_vision, starter


def _box(text: str, x: int, y: int, w: int = 120, h: int = 30) -> gbl_vision.OCRBox:
    return gbl_vision.OCRBox(text, 1.0, x, y, w, h)


class LivingDexRangeAndScannerTests(unittest.TestCase):
    def test_range_term_pads_singletons_and_formats_ranges(self) -> None:
        self.assertEqual(tag_living_dex.range_term(6, 6), "006")
        self.assertEqual(tag_living_dex.range_term(1000, 1000), "1000")
        self.assertEqual(tag_living_dex.range_term(1, 16), "1-16")

    def test_nonpositive_block_size_is_rejected_before_running(self) -> None:
        for size in (0, -1):
            with self.subTest(size=size):
                with self.assertRaisesRegex(ValueError, "block size must be positive"):
                    tag_living_dex.dex_blocks(1, 16, size)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    tag_living_dex.parse_args(["--block-size", str(size)])
                self.assertEqual(stopped.exception.code, 2)

    def test_count_visible_cp_tiles_includes_level_one_pokemon(self) -> None:
        boxes = [
            _box("CP 14", 120, 420),
            _box("CP 99", 320, 420),
            _box("CP 2350", 520, 420),
            _box("CP 10", 120, 120),  # above search bar -> ignored
        ]
        self.assertEqual(tag_living_dex.count_visible_cp_tiles(boxes, min_y=350), 3)

    def test_range_field_shows_matches_plain_glyph_and_unicode_dash(self) -> None:
        img = Image.new("RGB", (720, 1600), (240, 240, 240))
        self.assertTrue(tag_living_dex.range_field_shows(img, [_box("1-16", 200, 220)], "1-16"))
        self.assertTrue(tag_living_dex.range_field_shows(img, [_box("Q 1-16", 200, 220)], "1-16"))
        self.assertTrue(tag_living_dex.range_field_shows(img, [_box("Q1–16", 200, 220)], "1-16"))
        # Result counter in header "(17)" must never count as search term "017" or "1-17"
        self.assertFalse(tag_living_dex.range_field_shows(img, [_box("(1-16)", 300, 120)], "1-16"))

    def test_scan_range_tree_skips_empty_keeps_sole_blocks_and_splits_multi(self) -> None:
        # Owned Pokemon counts by dex in 1..16:
        # - dex 3: 1 copy
        # - dex 10: 4 copies
        # - dex 11: 1 copy
        owned = {3: 1, 10: 4, 11: 1}
        queries: list[tuple[int, int]] = []

        def query_fn(lo: int, hi: int) -> int:
            queries.append((lo, hi))
            return sum(owned.get(d, 0) for d in range(lo, hi + 1))

        # Empty block 17..32: 1 query total.
        self.assertEqual(tag_living_dex.scan_range_tree(17, 32, query_fn), [])
        self.assertEqual(queries, [(17, 32)])

        # Sole-copy block 33..48 (dex 40 = 1): 1 query total, no split.
        owned[40] = 1
        queries.clear()
        self.assertEqual(tag_living_dex.scan_range_tree(33, 48, query_fn), [(33, 48)])
        self.assertEqual(queries, [(33, 48)])

        # Dense block 1..16 finds dex 1-8 (count 1!) + splits 9-16 down to (11, 12) (count 1), skipping dex 10.
        queries.clear()
        found = tag_living_dex.scan_range_tree(1, 16, query_fn)
        self.assertEqual(found, [(1, 8), (11, 12)])
        # Left half 1-8 returned immediately at 1-8 without recursing into 1..4 / 5..8.
        self.assertNotIn((1, 4), queries)

        # With dex 12 also having copies, (11, 12) splits further to isolate singleton (11, 11).
        owned[12] = 2
        queries.clear()
        self.assertEqual(tag_living_dex.scan_range_tree(1, 16, query_fn), [(1, 8), (11, 11)])

    def test_scan_range_tree_retries_ocr_mismatch_and_flags_evolution_toggle(self) -> None:
        calls = {"n": 0}

        def flaky_query(lo: int, hi: int) -> int:
            calls["n"] += 1
            if lo == 1 and hi == 2 and calls["n"] == 1:
                return 2
            if lo == 1 and hi == 1 and calls["n"] == 2:
                return 0  # transient misread! 0 + 0 != 2 -> retries
            return { (1, 2): 2, (1, 1): 1, (2, 2): 1 }[(lo, hi)]

        self.assertEqual(tag_living_dex.scan_range_tree(1, 2, flaky_query), [(1, 1), (2, 2)])

        # Persistent left + right > parent points to SHOW EVOLUTIONARY LINE being enabled.
        def inflated_query(lo: int, hi: int) -> int:
            if (lo, hi) == (1, 2):
                return 2
            return 2  # 2 + 2 = 4 > 2

        with self.assertRaises(RuntimeError) as ctx:
            tag_living_dex.scan_range_tree(1, 2, inflated_query, max_retries=2)
        self.assertIn("SHOW EVOLUTIONARY LINE", str(ctx.exception))

    def test_build_tag_chunks_caps_length_and_counts_all_ranges(self) -> None:
        sole = [(1, 8), (11, 11), (25, 25), (100, 112), (200, 200), (301, 316), (400, 405)]
        chunks = tag_living_dex.build_tag_chunks(sole, tag="dontrade", max_chars=36)
        self.assertGreater(len(chunks), 1)
        total_ranges = 0
        for plain, tagged, cnt in chunks:
            self.assertTrue(tagged.startswith("!dontrade&"))
            self.assertLessEqual(len(tagged), 36)
            self.assertEqual(len(plain.split(",")), cnt)
            total_ranges += cnt
        self.assertEqual(total_ranges, len(sole))

    def test_tag_sole_chunks_uses_pixel_coords_multiselect_tag_and_live_count(self) -> None:
        img = Image.new("RGB", (720, 1600), (240, 240, 240))
        adb_calls: list[tuple[str, ...]] = []
        taps: list[str] = []

        class FakeTarget:
            platform = "android"
            label = "example-android"
            front_package = None
            device = None

            def _adb(self, *args: str):
                adb_calls.append(tuple(args))
                return SimpleNamespace(stdout="", returncode=0, stderr="")

            async def close(self) -> None:
                return None

        grid_plain = [
            _box("POKÉMON", 300, 80),
            _box("(2)", 320, 140),
            _box("1-8,11", 200, 220),
            _box("CP 412", 140, 480),
            _box("CP 915", 360, 480),
        ]
        grid_untagged = [
            _box("POKÉMON", 300, 80),
            _box("(2)", 320, 140),
            _box("!dontrade&1-8,11", 200, 220),
            _box("CP 412", 140, 480),
            _box("CP 915", 360, 480),
        ]
        grid_nonfav = [
            _box("POKÉMON", 300, 80),
            _box("(2)", 320, 140),
            _box("!favorite&!dontrade&1-8,11", 200, 220),
            _box("CP 412", 140, 480),
            _box("CP 915", 360, 480),
        ]
        multiselect_bar = [
            _box("SELECT ALL", 560, 140),
            _box("TAG", 180, 1440),
            _box("TRANSFER", 540, 1440),
        ]
        multiselect_all = [
            _box("DESELECT ALL", 560, 140),
            _box("TAG", 180, 1440),
            _box("TRANSFER", 540, 1440),
        ]
        tag_sheet = [
            _box("TAG 2 POKÉMON", 300, 240),
            _box("• ToTrade", 300, 620),
            _box("• dontrade", 300, 740),
            _box("DONE", 320, 1440),
        ]
        grid_done = [
            _box("POKÉMON", 300, 80),
            _box("(0)", 320, 140),
            _box("!dontrade&1-8,11", 200, 220),
        ]
        reads = iter([
            (img, multiselect_bar),   # settle after long press
            (img, multiselect_all),   # after SELECT ALL
            (img, tag_sheet),         # _select_and_confirm_tag initial read
            (img, tag_sheet),         # after tapping dontrade -> find DONE
        ])

        apply_calls: list[str] = []

        async def fake_apply(_phone, query, _img=None, _boxes=None, *, require_exact=False):
            term = str(query)
            apply_calls.append(term)
            self.assertTrue(require_exact)
            if term in {"1-8", "11"}:
                return 1, img, grid_plain, 0.2
            if term == "1-8,11":
                return 2, img, grid_plain, 0.2
            if term == "!dontrade&1-8,11" and len(apply_calls) == 4:
                return 2, img, grid_untagged, 0.2
            if term == "!favorite&!dontrade&1-8,11":
                return 2, img, grid_nonfav, 0.2
            if term == "!dontrade&1-8,11":
                return 0, img, grid_done, 0.2
            raise AssertionError(f"Unexpected apply of {term}")

        real_phone_type = starter.Phone

        class FakePhone(real_phone_type):
            def __init__(self, target, shots: Path) -> None:
                super().__init__("example-android", target, shots, False)

            async def read(self):
                return next(reads)

            async def tap(self, point, _image=None):
                taps.append(f"tap:{point}")

            async def tap_box(self, b, _image=None, drop=0.0):
                taps.append(b.text)

            async def go_home(self):
                return img

        self.assertFalse(hasattr(real_phone_type, "pixel"))

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            spec = SimpleNamespace(name="example-android", platform="android")
            chunks = [("1-8,11", "!dontrade&1-8,11", 2)]
            fake_target = FakeTarget()
            with mock.patch.object(tag_living_dex.config_paths, "state_dir", return_value=state_dir), \
                    mock.patch.object(tag_living_dex.pokemon_fleet, "acquire_device_locks", return_value=contextlib.nullcontext()), \
                    mock.patch.object(tag_living_dex.home, "open_target", new_callable=mock.AsyncMock, return_value=fake_target), \
                    mock.patch.object(tag_living_dex.starter, "Phone", side_effect=lambda _n, t, s, _d=False: FakePhone(t, s)), \
                    mock.patch.object(tag_living_dex.starter, "open_storage", new_callable=mock.AsyncMock, return_value=(img, grid_plain)), \
                    mock.patch.object(tag_living_dex.clear_pokemon, "_apply_and_verify_query", side_effect=fake_apply), \
                    mock.patch.object(tag_living_dex.clear_pokemon, "evolutionary_line_off", return_value=True), \
                    mock.patch.object(tag_living_dex.clear_pokemon, "_clear_storage_pill_if_active", new_callable=mock.AsyncMock), \
                    mock.patch.object(tag_living_dex.trade_local, "POLL", 0), \
                    mock.patch.object(tag_living_dex.asyncio, "sleep", new_callable=mock.AsyncMock):
                tagged_chunks, remaining = asyncio.run(
                    tag_living_dex.tag_sole_chunks(spec, chunks, tag="dontrade", dry_run=False, shots=Path(tmp) / "shots")
                )

        self.assertEqual((tagged_chunks, remaining), (1, 0))
        self.assertEqual(
            apply_calls,
            ["1-8", "11", "1-8,11", "!dontrade&1-8,11", "!favorite&!dontrade&1-8,11", "!dontrade&1-8,11"],
        )
        # Long-press used pixel coordinates of tile 0 center + TILE_RISE.
        expected_y = grid_nonfav[3].center_y + int(1600 * tag_living_dex.clear_pokemon.TILE_RISE)
        self.assertEqual(adb_calls, [("shell", f"input swipe 200 {expected_y} 200 {expected_y} 1000")])
        self.assertEqual(taps, ["SELECT ALL", "TAG", "• dontrade", "DONE"])
        self.assertNotIn("TRANSFER", taps)

    def test_changed_range_stops_before_tagging_even_if_pooled_count_still_matches(self) -> None:
        image = Image.new("RGB", (720, 1600))
        target = mock.Mock(close=mock.AsyncMock())
        spec = SimpleNamespace(name="example-android", platform="android")
        counts = {"1-8": 2, "11": 0, "1-8,11": 2}

        async def read_grid(_phone, term, _image, _boxes, *, require_exact):
            self.assertTrue(require_exact)
            return counts[term], image, [], 0.2

        with (
            mock.patch.object(tag_living_dex.pokemon_fleet, "acquire_device_locks", return_value=contextlib.nullcontext()),
            mock.patch.object(tag_living_dex.home, "open_target", new_callable=mock.AsyncMock, return_value=target),
            mock.patch.object(tag_living_dex.starter, "open_storage", new_callable=mock.AsyncMock, return_value=(image, [])),
            mock.patch.object(tag_living_dex.clear_pokemon, "_apply_and_verify_query", side_effect=read_grid) as read,
            mock.patch.object(tag_living_dex.clear_pokemon, "evolutionary_line_off", return_value=True),
            mock.patch.object(tag_living_dex, "_select_and_confirm_tag", new_callable=mock.AsyncMock) as tag,
            mock.patch.object(tag_living_dex, "_record_tagged_chunk") as record,
        ):
            with self.assertRaisesRegex(tag_living_dex.clear_pokemon.ClearSafetyError, "saved range 1-8 now matches 2"):
                asyncio.run(tag_living_dex.tag_sole_chunks(
                    spec, [("1-8,11", "!dontrade&1-8,11", 2)],
                    tag="dontrade", dry_run=False, shots=Path("unused"),
                ))
        self.assertEqual([call.args[1] for call in read.call_args_list], ["1-8"])
        tag.assert_not_awaited()
        target._adb.assert_not_called()
        record.assert_not_called()
        target.close.assert_awaited_once()

    def test_weather_warning_is_detected_and_dismissed_by_i_am_safe(self) -> None:
        boxes = [
            _box("Weather warning", 360, 560),
            _box("Weather conditions are potentially dangerous-be aware of your surroundings!", 360, 700),
            _box("Check your local weather service for details.", 360, 760),
            _box("I AM SAFE", 360, 960, 200, 60),
        ]
        self.assertTrue(gbl_vision.safety_notice_visible(boxes))
        pt = gbl_vision.safety_notice_point(boxes)
        self.assertEqual(pt, [boxes[3].center_x, boxes[3].center_y])


if __name__ == "__main__":
    unittest.main()
