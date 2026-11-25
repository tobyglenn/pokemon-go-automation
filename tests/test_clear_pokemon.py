from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image, ImageDraw

from tests import support as _test_support  # noqa: F401

import clear_pokemon as public_clear
from sources import clear_pokemon, fleet_entrypoint, gbl_vision, starter


def box(text: str, x: int, y: int, w: int = 120, h: int = 30) -> gbl_vision.OCRBox:
    return gbl_vision.OCRBox(text, 1.0, x, y, w, h)


class _Image:
    def __init__(self, width: int = 720, height: int = 1600) -> None:
        self.width = width
        self.height = height


class ManualGridTests(unittest.TestCase):
    def test_translucent_ios_location_banner_requires_agreement_on_both_storage_counters(self):
        frame = Image.new("RGB", (750, 1334))
        notice = box("Failed to detect location. (12)", 120, 80)
        originals = [notice, box("SHOW EVOLUTIONARY LINE", 210, 320)]
        three, four = box("Q (3)", 345, 130), box("Q (4)", 345, 130)
        eggs = box("12/12", 610, 130)
        for second, expected in [(three, 3), (four, None)]:
            with self.subTest(second=second.text), mock.patch.object(
                gbl_vision, "recognize", side_effect=[[three, eggs], [second, eggs]]
            ):
                out = starter._ios_storage_banner_counts(frame, originals)
                self.assertEqual(clear_pokemon.result_count(out, frame.height), expected)
                if expected is None:
                    self.assertIs(out, originals)

    def test_exact_species_requires_visible_unmarked_evolutionary_line_control(self):
        image = Image.new("RGB", (750, 1334), "white")
        label = box("SHOW EVOLUTIONARY LINE", 222, 328, 381, 28)
        draw = ImageDraw.Draw(image)
        ring = (149, 317, 199, 367)
        teal = (17, 136, 151)
        self.assertFalse(clear_pokemon.evolutionary_line_off(image, [label]))
        draw.ellipse(ring, outline=teal, width=4)
        self.assertTrue(clear_pokemon.evolutionary_line_off(image, [label]))
        self.assertFalse(clear_pokemon.evolutionary_line_off(image, []))
        draw.line([(160,341), (170,351), (186,331)], fill=teal, width=3)
        self.assertFalse(clear_pokemon.evolutionary_line_off(image, [label]))
        draw.ellipse(ring, fill=teal)
        self.assertFalse(clear_pokemon.evolutionary_line_off(image, [label]))

    def test_location_notice_does_not_replace_filtered_count_or_settled_storage_header(self):
        frame = _Image(750, 1600)
        notice = box("Failed to detect location. (12)", 120, 80)
        eggs = box("9 / 12", 575, 155)
        count = box("Q (7)", 310, 155)
        self.assertTrue(starter._storage_open_settled(frame, [notice, eggs, count]))
        self.assertEqual(clear_pokemon.result_count([notice, eggs, count], frame.height), 7)
        self.assertFalse(starter.storage_open([notice, eggs]))
        self.assertIsNone(clear_pokemon.result_count([notice, eggs], frame.height))
        self.assertFalse(starter._storage_open_settled(frame, [box("TAGS",60,700),box("EGGS",620,700)]))

    def test_keyboard_fallback_requires_visible_cyan_check(self):
        image = Image.new("RGB", (440, 956), "white")
        self.assertIsNone(clear_pokemon._ios_keyboard_check(image))
        draw = ImageDraw.Draw(image)
        draw.ellipse((310, 579, 356, 625), fill=(0, 144, 255))
        self.assertIsNone(clear_pokemon._ios_keyboard_check(image))
        draw.line([(325, 601), (331, 607), (342, 593)], fill="white", width=3)
        self.assertEqual(clear_pokemon._ios_keyboard_check(image), [333, 602])

    def test_plain_ios_keyboard_uses_return_or_stops_before_tapping_storage(self):
        from sources import trade_saved_search

        image = Image.new("RGB", (440, 956), "white")
        device = SimpleNamespace(tap_done=mock.AsyncMock(return_value=False))
        phone = SimpleNamespace(name="test", target=SimpleNamespace(device=device),
                                ios_keyboard=mock.AsyncMock(return_value=True),
                                look=mock.AsyncMock(return_value=image), tap=mock.AsyncMock())
        with mock.patch.object(trade_saved_search, "dismiss_ios_return",
                               new_callable=mock.AsyncMock) as dismiss:
            dismiss.return_value = True
            asyncio.run(clear_pokemon._ios_tap_done(phone))
            dismiss.assert_awaited_once_with(phone)
            dismiss.return_value = False
            with self.assertRaises(clear_pokemon.ClearSafetyError):
                asyncio.run(clear_pokemon._ios_tap_done(phone))
        phone.tap.assert_not_awaited()

    def test_exact_tag_pill_on_empty_manual_review(self):
        image = _Image(750, 1334)
        rows = [box("TAGS", 60, 75), box("POKÉMON", 310, 75), box("EGGS", 580, 75),
                box("Q (0)", 310, 121), box("#Keep One", 60, 234)]
        self.assertEqual(clear_pokemon._manual_grid_count(image, rows, "#Keep One"), 0)
        self.assertIsNone(clear_pokemon._manual_grid_count(image, rows, "#Other"))

    def test_clipped_se_query_needs_full_editor_and_actual_result_grid(self):
        image = _Image()
        rows = [box("TAGS", 70, 65), box("POKÉMON", 295, 65), box("EGGS", 555, 65),
                box("Q (1)", 300, 130), box("nse,!Ohp&0*,1*,2*&traded,138&138", 40, 282)]
        term = "!favorite&0*,1*,2*&traded,138&138"
        self.assertEqual(clear_pokemon._manual_grid_count(image, rows), 1)
        self.assertIsNone(clear_pokemon._manual_grid_count(image, [*rows, box("See More", 500, 510)]))
        phone = SimpleNamespace(ios=True, tap_box=mock.AsyncMock())
        with mock.patch("sources.trade_saved_search.editor_text", new_callable=mock.AsyncMock) as editor, \
             mock.patch.object(clear_pokemon.asyncio, "sleep", new_callable=mock.AsyncMock), \
             mock.patch.object(clear_pokemon, "_ios_tap_done", new_callable=mock.AsyncMock), \
             mock.patch.object(clear_pokemon.trade_local, "settle", new_callable=mock.AsyncMock) as settle:
            editor.return_value = "other query"
            self.assertIsNone(asyncio.run(clear_pokemon._manual_clipped_grid(phone, term, image, rows)))
            settle.assert_not_awaited()
            editor.return_value = term
            settle.return_value = (True, image, rows)
            result = asyncio.run(clear_pokemon._manual_clipped_grid(phone, term, image, rows))
            self.assertEqual(result, (1, image, rows))


class RangeAndQueryTests(unittest.TestCase):
    def test_range_parse_and_format_round_trip(self) -> None:
        raw = "10-12,19-24,128,132"
        nums = clear_pokemon.parse_dex_ranges(raw)
        self.assertEqual(nums, {10, 11, 12, 19, 20, 21, 22, 23, 24, 128, 132})
        self.assertEqual(clear_pokemon.format_dex_ranges(nums), raw)

    def test_parse_query_splits_dex_and_filters(self) -> None:
        dex, filters = clear_pokemon.parse_query("10-12,52&!shiny&!4*&!98Plus")
        self.assertEqual(dex, {10, 11, 12, 52})
        self.assertEqual(filters, ["!shiny", "!4*", "!98Plus"])

    def test_build_single_query_puts_filters_first_and_covers_all_species(self) -> None:
        dex = [10, 11, 12, 52, 128]
        names = {10: "caterpie", 11: "metapod", 12: "butterfree", 52: "meowth", 128: "tauros"}
        filters = list(clear_pokemon.REQUIRED_SAFETY_FILTERS)
        single = clear_pokemon.build_single_query(dex, filters, names)
        self.assertEqual(single.dex_nums, (10, 11, 12, 52, 128))
        self.assertEqual(single.dex_part, "10-12,52,128")
        self.assertTrue(single.term.startswith("!#&!98Plus&!dontrade&!shiny"))
        self.assertIn("!dontrade", single.term)
        self.assertIn("!xxs&!xxl&10-12,52,128", single.term)
        self.assertTrue(single.term.endswith(single.dex_part))
        self.assertEqual(single.allowed_names, frozenset(names.values()))


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "species.csv"
        self.csv.write_text(
            "id,identifier,evolves_from_species_id,is_baby,is_legendary,is_mythical\n"
            "10,caterpie,,0,0,0\n"
            "52,meowth,,0,0,0\n"
            "863,perrserker,52,0,0,0\n"
            "203,girafarig,,0,0,0\n"
            "981,farigiraf,203,0,0,0\n"
            "562,yamask,,0,0,0\n"
            "563,cofagrigus,562,0,0,0\n"
            "867,runerigus,562,0,0,0\n"
            "821,rookidee,,0,0,0\n"
            "822,corvisquire,821,0,0,0\n"
            "823,corviknight,822,0,0,0\n"
            "840,applin,,0,0,0\n"
            "1011,dipplin,840,0,0,0\n"
            "1019,hydrapple,1011,0,0,0\n"
            "302,sableye,,0,0,0\n"
            "287,slakoth,,0,0,0\n"
            "288,vigoroth,287,0,0,0\n"
            "289,slaking,288,0,0,0\n"
            "128,tauros,,0,0,0\n"
        )
        self.votes = self.tmp / "votes.json"
        self.votes.write_text(json.dumps({
            "10": [["lucky", 0.3, "a.png"]],
            "52": [["lucky", 0.3, "a.png"]],
            "203": [["lucky", 0.3, "a.png"]],
            "562": [["lucky", 0.3, "a.png"]],
            "821": [["lucky", 0.3, "a.png"]],
            "840": [["lucky", 0.3, "a.png"]],
            "302": [["lucky", 0.3, "a.png"]],
            "287": [["lucky", 0.3, "a.png"]],
            "288": [["lucky", 0.3, "a.png"]],
            "289": [["lucky", 0.3, "a.png"]],
            "128": [["lucky", 0.3, "a.png"]],
            "863": [["none", 0.0, "a.png"]],
            "981": [["none", 0.0, "a.png"]],
            "563": [["none", 0.0, "a.png"]],
            "867": [["none", 0.0, "a.png"]],
            "823": [["none", 0.0, "a.png"]],
            "1019": [["none", 0.0, "a.png"]],
        }))
        self.report = self.tmp / "report.json"
        self.report.write_text(json.dumps({
            "needed_lucky_list": [
                "863 Perrserker",
                "981 Farigiraf",
                "563 Cofagrigus",
                "867 Runerigus",
                "823 Corviknight",
                "1019 Hydrapple",
            ]
        }))

    def test_audit_removes_lucky_evolutions_megas_raids_and_adds_filters(self) -> None:
        fake_gd = SimpleNamespace(pokemon={"sableye": {"dex": 302, "megas": [{"name": "Mega Sableye"}]}})
        with mock.patch.object(clear_pokemon.raid_gamedata, "load", return_value=fake_gd), \
                mock.patch.object(clear_pokemon.raid_meta, "rankings", return_value={}):
            res = clear_pokemon.audit_query(
                "10,52,203,562,821,840,302,128&!shiny&!4*&!favorite&!98Plus",
                lucky_report=self.report,
                lucky_votes=self.votes,
                species_csv=self.csv,
            )
        self.assertEqual(set(res.evolves_into_need_lucky), {52, 203, 562, 821, 840})
        self.assertIn(302, res.mega_capable)
        self.assertIn(128, res.rare_collectibles)
        self.assertEqual(res.safe_dex, [10, 128])
        for tok in (
            "!#",
            "!dontrade",
            "!lucky",
            "!costume",
            "!shadow",
            "!dynamax",
            "!gigantamax",
            "!background",
            "!xxs",
            "!xxl",
        ):
            self.assertIn(tok, res.missing_safety_filters)
            self.assertIn(tok, res.safe_filters)

    def test_configured_filters_extend_mandatory_guards_for_every_audited_query(self) -> None:
        fake_gd = SimpleNamespace(pokemon={})
        config = self.tmp / "clear.json"
        for safety_filters in ([], ["!buddy1-5"]):
            config.write_text(json.dumps({
                "raw_query": "10", "species_csv": str(self.csv), "safety_filters": safety_filters,
            }))
            for query in ("10", None):
                with self.subTest(query=query, safety_filters=safety_filters), \
                        mock.patch.object(clear_pokemon.raid_gamedata, "load", return_value=fake_gd), \
                        mock.patch.object(clear_pokemon.raid_meta, "rankings", return_value={}):
                    planned, _, _ = clear_pokemon.build_clear_plan(config_file=config, query=query)
                    self.assertTrue(set(clear_pokemon.REQUIRED_SAFETY_FILTERS) <= set(planned.filters))
                    self.assertTrue(set(safety_filters) <= set(planned.filters))


class ScreenSafetyGuardTests(unittest.TestCase):
    def test_search_field_matches_prefix_or_suffix(self) -> None:
        term = (
            "!shiny&!4*&!favorite&!legendary&!mythical&!ultra beasts&!98Plus&!#&!lucky"
            "&!costume&!shadow&!dynamax&!gigantamax&!background&!xxs&!xxl&10-12,924-931,960-961,971-973,980,982"
        )
        img = _Image()
        self.assertTrue(
            clear_pokemon.search_field_matches_query([box("!shiny&!4*&!favorite&!legendary&!", 180, 226)], term, img.height)
        )
        self.assertTrue(
            clear_pokemon.search_field_matches_query([box("24-931,960-961,971-973,980,982", 180, 226)], term, img.height)
        )
        self.assertTrue(
            clear_pokemon.search_field_matches_query([box(",924-931,960-961,971-973,980,982", 180, 226)], term, img.height)
        )
        self.assertTrue(
            clear_pokemon.search_field_matches_query(
                [box("d&!dynamax&.gigantamax&xxs,xxl", 180, 226)],
                "!98Plus&!dontrade&!background&!dynamax&!gigantamax&xxs,xxl",
                img.height,
            )
        )
        self.assertFalse(
            clear_pokemon.search_field_matches_query([box("Q 60-62,72-73,77-78", 180, 226)], term, img.height)
        )

    def test_storage_search_field_stays_above_first_tile_row(self) -> None:
        img = _Image(width=750, height=1334)
        # Simulated iOS storage where row 1 CP is missed and only row 3 CP (~740) is visible.
        boxes = [
            box("TAGS", 108, 80, 76, 28),
            box("POKÉMON", 300, 76, 150, 36),
            box("EGGS", 564, 80, 80, 30),
            box("1458 / 1600", 308, 122, 136, 30),
            box("CP 704", 302, 740, 110, 37),
        ]
        field = clear_pokemon._storage_search_field(img, boxes)
        self.assertLessEqual(field.center_y, int(img.height * 0.215))
        self.assertGreaterEqual(field.center_y, int(img.height * 0.125))

    def test_detail_species_allowed_and_safety_blocks(self) -> None:
        img = _Image()
        allowed = frozenset({"caterpie", "nidoran", "nidoranf", "flabebe"})
        # Plain allowed species
        boxes = [box("CP 231", 320, 180), box("Caterpie", 300, 660), box("45 / 45 HP", 300, 740), box("STARDUST", 180, 920)]
        ok, found = clear_pokemon.detail_species_allowed(boxes, img, allowed)
        self.assertTrue(ok)
        self.assertEqual(found, "caterpie")
        self.assertIsNone(clear_pokemon.detail_safety_block(boxes, img))

        # Nidoran female symbol & accented Flabebe
        ok_nido, _ = clear_pokemon.detail_species_allowed([box("Nidoran♀", 300, 660), box("40 / 40 HP", 300, 740)], img, allowed)
        self.assertTrue(ok_nido)
        ok_flab, _ = clear_pokemon.detail_species_allowed([box("Flabébé", 300, 660), box("40 / 40 HP", 300, 740)], img, allowed)
        self.assertTrue(ok_flab)

        # Nicknamed or wrong species fails closed
        ok_wrong, _ = clear_pokemon.detail_species_allowed([box("PvPRank1", 300, 660), box("40 / 40 HP", 300, 740)], img, allowed)
        self.assertFalse(ok_wrong)

        # Protected tag pill / Lucky / Mega / Shadow / XXS / XXL blocks
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("98Plus", 120, 820)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("dontrade", 120, 820)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("LUCKY POKÉMON", 240, 700)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("MEGA EVOLVE", 240, 1020)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("PURIFY", 240, 980)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("SHADOW", 180, 820), box("PURIFY", 240, 980)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("XXS", 520, 790)], img))
        self.assertIsNotNone(clear_pokemon.detail_safety_block(boxes + [box("XXL", 520, 790)], img))

    def test_dialog_safety_block_rejects_special_warnings(self) -> None:
        safe_boxes = [
            box("Do you want to transfer Caterpie to the Professor?", 100, 500),
            box("You cannot undo a transfer.", 120, 580),
            box("YES", 320, 780),
            box("NO", 320, 900),
        ]
        self.assertIsNone(clear_pokemon.dialog_safety_block(safe_boxes))

        for bad_word in ("Shiny", "Lucky", "Legendary", "Event", "Costume", "XXL", "XXS", "Dynamax", "Gigantamax", "Background"):
            bad = [
                box(f"Do you want to transfer this {bad_word} Pokémon to the Professor?", 100, 500),
                box("You cannot undo a transfer.", 120, 580),
                box("YES", 320, 780),
                box("NO", 320, 900),
            ]
            self.assertIsNotNone(clear_pokemon.dialog_safety_block(bad), msg=f"failed to block {bad_word}")

    def test_enter_query_android_uses_bursts_and_polls_until_matched(self) -> None:
        dex = list(range(10, 260, 2))
        query = clear_pokemon.build_single_query(
            dex,
            clear_pokemon.REQUIRED_SAFETY_FILTERS,
            {i: f"mon{i}" for i in dex},
        )
        self.assertGreater(len(query.term), clear_pokemon.ANDROID_INPUT_BURST * 2)
        adb_calls: list[tuple[str, ...]] = []
        edit_texts = iter(["", query.term[:200], query.term])

        class FakeTarget:
            platform = "android"
            label = "sample-receiver"
            front_package = None
            device = None

            def _adb(self, *args: str):
                adb_calls.append(tuple(args))
                return SimpleNamespace(stdout="", returncode=0, stderr="")

        phone = starter.Phone("sample-receiver", FakeTarget(), Path("/tmp"))
        img = Image.new("RGB", (720, 1600), (240, 250, 240))
        boxes = [box("Q Search", 280, 230, 140, 40)]

        async def fake_tap_box(_box, _image=None):
            return None

        async def fake_edit_text(_phone):
            return next(edit_texts)

        async def noop(_seconds):
            return None

        with mock.patch.object(phone, "tap_box", side_effect=fake_tap_box), \
                mock.patch.object(clear_pokemon, "_android_edit_text", side_effect=fake_edit_text), \
                mock.patch.object(clear_pokemon.asyncio, "sleep", side_effect=noop):
            asyncio.run(clear_pokemon._enter_query_android(phone, query, img, boxes))

        input_calls = [c for c in adb_calls if len(c) >= 2 and c[0] == "shell" and c[1].startswith("input text ")]
        expected_bursts = (len(query.term) + clear_pokemon.ANDROID_INPUT_BURST - 1) // clear_pokemon.ANDROID_INPUT_BURST
        self.assertEqual(len(input_calls), expected_bursts)
        for _shell, cmd in input_calls:
            payload = cmd[len("input text '"):-1].replace("%s", " ")
            self.assertLessEqual(len(payload), clear_pokemon.ANDROID_INPUT_BURST)
        self.assertEqual(adb_calls[-1], ("shell", "input keyevent 66"))


class CliSafetyAndRoutingTests(unittest.TestCase):
    def test_count_and_all_require_confirm_delete(self) -> None:
        with self.assertRaises(SystemExit):
            clear_pokemon.parse_args(["--count", "1"])
        with self.assertRaises(SystemExit):
            clear_pokemon.parse_args(["--all"])

    def test_review_runs_locally_without_fanout(self) -> None:
        with mock.patch.object(fleet_entrypoint, "run_all_machines") as fanout, \
                mock.patch.object(clear_pokemon, "main", return_value=0) as worker:
            code = public_clear.main(["--review"])
        self.assertEqual(code, 0)
        fanout.assert_not_called()
        worker.assert_called_once()

    def test_open_mode_parses_and_leaves_storage_open_without_clearing_or_home(self) -> None:
        args = clear_pokemon.parse_args(["--open"])
        self.assertTrue(args.open)
        query = clear_pokemon.build_single_query([10], clear_pokemon.REQUIRED_SAFETY_FILTERS, {10: "caterpie"})
        spec = SimpleNamespace(name="sample-receiver")
        fake_target = SimpleNamespace(close=mock.AsyncMock())
        img = Image.new("RGB", (720, 1600), (240, 250, 240))
        boxes = [box("CP 100", 100, 500), box("Caterpie", 100, 620)]

        @mock.patch.object(clear_pokemon.pokemon_fleet, "acquire_device_locks")
        @mock.patch.object(clear_pokemon.home, "open_target", new_callable=mock.AsyncMock, return_value=fake_target)
        @mock.patch.object(clear_pokemon.starter, "open_storage", new_callable=mock.AsyncMock, return_value=(img, boxes))
        @mock.patch.object(clear_pokemon, "_apply_and_verify_query", new_callable=mock.AsyncMock, return_value=(12, img, boxes, 4.2))
        @mock.patch.object(clear_pokemon, "_clear_storage_pill_if_active", new_callable=mock.AsyncMock)
        @mock.patch.object(clear_pokemon.starter.Phone, "go_home", new_callable=mock.AsyncMock)
        def _run(mock_go_home, mock_clear_pill, *_unused):
            ok, detail = asyncio.run(clear_pokemon.run_device(spec, query, (), args))
            self.assertTrue(ok)
            self.assertIn("[left open; 0 transferred]", detail)
            mock_clear_pill.assert_not_called()
            mock_go_home.assert_not_called()
            fake_target.close.assert_awaited_once()

        _run()


class GoHomeLoadingAndNoticeTests(unittest.TestCase):
    def test_go_home_waits_loading_and_dismisses_startup_safety_notice(self) -> None:
        loading = Image.new("RGB", (750, 1334), (40, 30, 50))
        ImageDraw.Draw(loading).rectangle((100, 1245, 650, 1265), fill=(245, 145, 50))
        notice = Image.new("RGB", (750, 1334), (245, 250, 245))
        map_img = Image.new("RGB", (750, 1334), (180, 220, 160))

        taps: list[tuple[int, int]] = []

        class FakeTarget:
            platform = "ios"
            label = "sample-ios-secondary"
            front_package = None
            device = object()

            async def restore(self) -> bool:
                return False

        phone = starter.Phone("sample-ios-secondary", FakeTarget(), Path("/tmp"))
        screens = iter([loading, notice, map_img])
        on_map_vals = iter([False, False, True])
        ocr_vals = iter([
            [box("Collect your daily free Raid Pass", 120, 1100)],
            [
                box("Stay aware of your surroundings", 120, 400),
                box("Do not enter dangerous areas", 120, 500),
                box("OK", 350, 920, 80, 40),
            ],
            [],
        ])

        async def fake_look():
            return next(screens)

        async def fake_tap(point, _image=None):
            taps.append((point[0], point[1]))

        async def noop(_seconds):
            return None

        with mock.patch.object(phone, "look", side_effect=fake_look), \
                mock.patch.object(phone, "tap", side_effect=fake_tap), \
                mock.patch.object(starter.home, "on_map", side_effect=lambda _i: next(on_map_vals)), \
                mock.patch.object(starter.gbl_vision, "recognize", side_effect=lambda _i: next(ocr_vals)), \
                mock.patch.object(starter.asyncio, "sleep", side_effect=noop):
            res = asyncio.run(phone.go_home())

        self.assertIs(res, map_img)
        self.assertEqual(len(taps), 1)
        self.assertAlmostEqual(taps[0][0], 390, delta=30)
        self.assertAlmostEqual(taps[0][1], 935, delta=30)


if __name__ == "__main__":
    unittest.main()
