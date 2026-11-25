from __future__ import annotations

from tests import support as _test_support  # noqa: F401

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

import special_trade as special_trade_cli
from sources import gbl_vision, special_trade, trade_local


class SpecialTradeScopeTests(unittest.TestCase):
    def test_local_scope_survives_public_wrapper(self):
        with mock.patch.object(special_trade_cli.fleet_entrypoint, "apply_local_environment"):
            with mock.patch.object(special_trade, "main", return_value=0) as main:
                self.assertEqual(special_trade_cli.main(["--local", "--plan"]), 0)
        main.assert_called_once_with(["--plan", "--local"])


REPORT = """# gap

## How each gap is already covered

| 0001 | Ignored | Kanto | CATCH | PhoneA | not in the full list | yes |

## Full list

| dex | name | region | route | already on | detail | confirmed |
|---|---|---|---|---|---|---|
| 0086 | Seel | Kanto | CATCH | - | single stage - hunt the shiny | yes |
| 0122 | Mr-mime | Kanto | TRADE | PhoneA | needs a shiny Mime-jr (#439) first | yes |
| 0227 | Skarmory | Johto | CATCH | PhoneA+PhoneB | single stage - hunt the shiny | yes |
| 0245 | Suicune | Johto | RAID/RESEARCH | PhoneA | legendary or mythical - cannot be traded | yes |
| 0294 | Loudred | Hoenn | EVOLVE | PhoneA | evolve your shiny Whismur (#293) | yes |
| 0433 | Chingling | Sinnoh | TRADE | PhoneA | baby - hatch-and-trade only | yes |
| 0679 | Honedge | Kalos | CATCH | PhoneC | single stage - hunt shiny | yes |
"""

HOLDERS = {"PhoneA": "android-one", "PhoneB": "android-two", "PhoneC": "ios-one"}


def box(text, x, y, w=80, h=30):
    return gbl_vision.OCRBox(text, 1.0, x, y, w, h)


class SpecialTradeTests(unittest.TestCase):
    def setUp(self) -> None:
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for name, value in {"HOLDERS": HOLDERS, "RECEIVER": "android-three",
                            "STATE": Path(state.name)}.items():
            patcher = mock.patch.object(special_trade, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_base_forms_held_elsewhere_are_wanted(self) -> None:
        needs = special_trade.read_needs(REPORT)
        self.assertEqual([n.dex for n in needs], [227, 433, 679])
        self.assertEqual(needs[0].holders, ("android-one", "android-two"))

    def test_the_partner_searches_its_shiny_by_dex_number(self) -> None:
        need = special_trade.read_needs(REPORT)[0]
        self.assertEqual(need.give_term, "227&shiny&!4*&!dontrade&!#KeepOne&!shadow&!traded")

    def test_the_receiver_gives_an_untagged_spare_legendary(self) -> None:
        term = special_trade.give_term(2500, floor=1774)
        self.assertTrue(term.startswith("zamazenta,zacian,necrozma,mewtwo,kyurem,rayquaza&"))
        for part in ("2*", "!#", "!dontrade", "!favorite", "!costume", "!purified", "!buddy1-5",
                     "!shiny", "!lucky", "!traded", "cp1774-", "cp-2500"):
            self.assertIn(part, term.split("&"))

    def test_only_two_star_legendaries_from_the_prepared_pool(self) -> None:
        term = special_trade.pool_term(["kyurem", "mewtwo"])
        self.assertEqual(term.split("&")[:2], ["kyurem,mewtwo", "2*"])
        self.assertNotIn("3*", term)
        prepared = {"give": {"zacian": 0, "kyurem": 2}, "partners": {"android-one": [227, 433]}}
        self.assertEqual(special_trade.give_species(prepared), ["kyurem"])
        special_trade.after_trade(prepared, "android-one", 227)
        self.assertEqual(prepared["give"]["kyurem"], 1)
        self.assertEqual(prepared["partners"]["android-one"], [433])

    def test_the_trade_picks_the_prepared_queue_in_order(self) -> None:
        needs = special_trade.read_needs(REPORT)
        prepared = {"partners": {"android-two": [679, 227], "android-one": [433], "elsewhere": [227]}}
        with mock.patch.object(special_trade, "missing_recently", return_value=False):
            queue = special_trade.queued(prepared, needs, {"android-two", "android-one"})
        self.assertEqual([n.dex for n in queue["android-two"]], [679, 227])
        self.assertNotIn("elsewhere", queue)

    def test_an_attached_partner_never_prepared_is_searched_at_trade_time(self) -> None:
        needs = special_trade.read_needs(REPORT)
        prepared = {"partners": {"android-one": [227]}}
        fresh = special_trade.unprepared(prepared, needs, {"android-three", "android-one", "ios-one"})
        self.assertEqual(list(fresh), ["ios-one"])          # android-one already has its list
        self.assertEqual([n.dex for n in fresh["ios-one"]], [679])
        special_trade.mark_missing("ios-one", 679)
        self.assertEqual(special_trade.unprepared(prepared, needs, {"android-three", "ios-one"}), {})

    def test_the_fresh_search_joins_the_prepared_list(self) -> None:
        import asyncio
        needs = special_trade.read_needs(REPORT)
        special_trade.save_prepared({"give": {"kyurem": 1}, "partners": {"android-one": [433]}})
        fresh = special_trade.unprepared(special_trade.load_prepared(), needs, {"android-three", "ios-one"})

        async def found(spec, args, terms):
            return {terms[0]: 1}

        from sources import starter_trade
        with mock.patch.object(starter_trade, "count", found):
            data = asyncio.run(special_trade.prepare_partners({"ios-one": object()}, fresh, object()))
        self.assertEqual(data["partners"], {"android-one": [433], "ios-one": [679]})
        self.assertEqual(data["give"], {"kyurem": 1})
        queue = special_trade.queued(special_trade.load_prepared(), needs, {"android-three", "ios-one"})
        self.assertEqual([n.dex for n in queue["ios-one"]], [679])

    def test_a_traded_species_leaves_the_full_list_only(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.md"
            path.write_text(REPORT)
            self.assertTrue(special_trade.remove_need(path, 227))
            text = path.read_text()
            self.assertNotIn("Skarmory", text)
            self.assertIn("Chingling", text)
            self.assertFalse(special_trade.remove_need(path, 227))
            self.assertFalse(special_trade.remove_need(path, 1))   # outside the full list
            self.assertIn("Ignored", path.read_text())

    def test_attached_partners_get_their_needs_and_the_rest_wait(self) -> None:
        needs = special_trade.read_needs(REPORT)
        by_partner, elsewhere = special_trade.plan(needs, {"android-three", "android-two"})
        self.assertEqual([n.dex for n in by_partner["android-two"]], [227])
        self.assertEqual([n.dex for n in elsewhere], [433, 679])

    def test_a_missing_shiny_is_not_planned_again_this_week(self) -> None:
        special_trade.mark_missing("android-two", 227)
        needs = special_trade.read_needs(REPORT)
        by_partner, _ = special_trade.plan(needs, {"android-three", "android-two"})
        self.assertEqual(by_partner, {})

    def test_the_swap_names_the_phone_holding_the_most(self) -> None:
        needs = special_trade.read_needs(REPORT)
        hint = special_trade.swap_hint(needs, {"android-three", "android-two"})
        self.assertIn("android-one", hint)
        self.assertIn("2 shiny", hint)

    def test_no_swap_when_nothing_is_held_elsewhere(self) -> None:
        needs = special_trade.read_needs(REPORT)
        self.assertIsNone(special_trade.swap_hint(
            needs, {"android-three", "android-one", "android-two", "ios-one"}))

    def test_picker_tiles_are_ordered_lowest_cp_first(self) -> None:
        image = Image.new("RGB", (720, 1600))
        boxes = [box("CP 3912", 100, 500), box("CP 2201", 300, 500), box("CP 99", 300, 100),
                 box("Mewtwo", 100, 640)]
        self.assertEqual([cp for cp, _ in special_trade.cp_labels(image, boxes)], [2201, 3912])

    def test_a_bottom_row_tile_is_never_tapped(self) -> None:
        # In a tall viewport, CP 1589 at y=2105 of 2560 sat under the sort button.
        image = Image.new("RGB", (1316, 2560))
        self.assertFalse(special_trade.safe(image, box("CP 1589", 951, 2105, 224, 58)))
        self.assertTrue(special_trade.safe(image, box("CP 1999", 535, 677, 223, 61)))

    def test_special_trades_remaining_is_read(self) -> None:
        self.assertEqual(special_trade.special_left([box("0 Special Trades remaining", 10, 300)]), 0)
        self.assertEqual(special_trade.special_left([box("Special Trades remaining: 1", 10, 300)]), 1)
        self.assertIsNone(special_trade.special_left([box("POKEMON", 10, 300)]))


if __name__ == "__main__":
    unittest.main()


class SpareTagPoolTests(unittest.TestCase):
    """#ToTrade spares are given only after the 2-star pool is empty (user, 14 Nov)."""

    def test_spare_term_keeps_the_never_give_filters(self):
        from sources import special_trade
        term = special_trade.spare_term(["zacian", "mewtwo"])
        self.assertTrue(term.startswith("zacian,mewtwo&#ToTrade"))
        for word in ("!dontrade", "!favorite", "!costume", "!purified", "!buddy1-5",
                     "!shiny", "!lucky", "!4*", "!shadow", "!traded"):
            self.assertIn(word, term)
        self.assertNotIn("!#", term.split("&"))
        self.assertIn("!#KeepOne", term.split("&"))

    def test_two_star_pool_still_excludes_tagged(self):
        from sources import special_trade
        self.assertIn("&!#", special_trade.pool_term(["zacian"]))
        self.assertIn("&!dontrade", special_trade.pool_term(["zacian"]))

    def test_armored_mewtwo_and_dontrade_detection(self) -> None:
        self.assertTrue(special_trade.is_armored_mewtwo_detail(
            1773, [box("Mewtwo", 249, 274), box("Iron Tail", 220, 620), box("Dynamic Punch", 220, 680)]
        ))
        self.assertTrue(special_trade.is_armored_mewtwo_detail(1773, [box("Mewtwo", 249, 274)]))
        self.assertTrue(special_trade.is_armored_mewtwo_detail(None, [box("Mewtwo", 249, 274), box("Dynamic Punch", 220, 680)]))
        self.assertFalse(special_trade.is_armored_mewtwo_detail(
            2350, [box("Mewtwo", 249, 274), box("Psycho Cut", 220, 620), box("Psystrike", 220, 680)]
        ))
        self.assertFalse(special_trade.is_armored_mewtwo_detail(1773, [box("Zacian", 249, 274), box("Iron Head", 220, 680)]))
        self.assertTrue(special_trade.has_dontrade_tag([box("• dontrade", 150, 720)]))
        self.assertFalse(special_trade.has_dontrade_tag([box("• ToTrade", 150, 720)]))

    def test_choose_lowest_refuses_armored_mewtwo_and_raises_cp_floor(self) -> None:
        img = Image.new("RGB", (720, 1600))
        picker_1773 = [
            box("POKEMON", 300, 120),
            box("3 Special Trades remaining", 200, 180),
            box("(2)", 340, 220),
            box("CP 1773", 140, 500),
            box("CP 2350", 360, 500),
        ]
        detail_armored = [
            box("Mewtwo", 250, 270),
            box("CP 1773", 250, 320),
            box("Iron Tail", 220, 620),
            box("Dynamic Punch", 220, 680),
            box("NEXT", 360, 1300),
        ]
        picker_2350 = [
            box("POKEMON", 300, 120),
            box("3 Special Trades remaining", 200, 180),
            box("(1)", 340, 220),
            box("CP 2350", 140, 500),
        ]
        detail_regular = [
            box("Mewtwo", 250, 270),
            box("CP 2350", 250, 320),
            box("Psycho Cut", 220, 620),
            box("Psystrike", 220, 680),
            box("NEXT", 360, 1300),
        ]
        reads = iter([
            (img, picker_1773),
            (img, picker_1773),
            (img, detail_armored),
            (img, picker_1773),
            (img, picker_2350),
            (img, detail_regular),
        ])
        seen_terms: list[str] = []
        phone = SimpleNamespace(
            name="android-three",
            ios=False,
            target=SimpleNamespace(_adb=mock.MagicMock()),
            read=mock.AsyncMock(side_effect=lambda: next(reads)),
            tap=mock.AsyncMock(),
            say=mock.MagicMock(),
            save=lambda _i, label: f"/tmp/{label}.png",
        )
        with mock.patch.object(special_trade.trade_local, "search_picker", new_callable=mock.AsyncMock,
                               side_effect=lambda _p, t, force=False: seen_terms.append(t)), \
                mock.patch.object(special_trade.trade_local, "POLL", 0), \
                mock.patch.object(special_trade.asyncio, "sleep", new_callable=mock.AsyncMock):
            picked = asyncio.run(special_trade.choose_lowest(phone, special_trade.spare_term(["mewtwo"])))
        self.assertEqual(picked, "CP 2350")
        self.assertIn("cp1774-", seen_terms[-1])


class CrossHostAndAllModeTests(unittest.TestCase):
    def setUp(self) -> None:
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        for name, value in {"HOLDERS": HOLDERS, "RECEIVER": "android-three",
                            "STATE": Path(state.name)}.items():
            patcher = mock.patch.object(special_trade, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_discover_fleet_includes_remote_partners_and_local_restricts_to_local(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
                "android-one": SimpleNamespace(name="android-one", enabled=True, platform="android"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        remote_owners = {
            "android-three": "host-a",
            "ios-one": "host-a",
            "android-one": "host-b",
            "android-two": "host-b",
        }
        with mock.patch.object(special_trade.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                mock.patch.object(special_trade.fleet_entrypoint, "local_machine", return_value="host-a"), \
                mock.patch.object(special_trade.remote_target, "discover_attached_devices", return_value=(remote_owners, {})), \
                mock.patch.object(special_trade, "attached", new_callable=mock.AsyncMock, return_value={
                    "android-three": fake_fleet.devices["android-three"],
                    "ios-one": fake_fleet.devices["ios-one"],
                }):
            here_all, owners_all, _ = asyncio.run(special_trade.discover_fleet(fake_fleet, local_only=False))
            here_local, owners_local, _ = asyncio.run(special_trade.discover_fleet(fake_fleet, local_only=True))

        self.assertEqual(set(here_all), {"android-three", "ios-one", "android-one", "android-two"})
        self.assertEqual(owners_all["android-one"], "host-b")
        self.assertEqual(set(here_local), {"android-three", "ios-one"})
        self.assertEqual(owners_local, {"android-three": "host-a", "ios-one": "host-a"})

    def test_all_mode_runs_across_partners_skips_lucky_and_stops_when_last_special_used(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
                "android-one": SimpleNamespace(name="android-one", enabled=True, platform="android"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        owners = {
            "android-three": "host-a",
            "ios-one": "host-a",
            "android-one": "host-b",
            "android-two": "host-b",
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "gap.md"
            report_path.write_text(REPORT)
            special_trade.save_prepared({
                "give": {"kyurem": 3},
                "partners": {"ios-one": [679], "android-one": [227], "android-two": [227]},
            })
            # Alphabetical key order: android-one -> android-two -> ios-one.
            # Trade 1: android-one hits LuckyAvailable -> skipped cleanly without aborting others!
            # Trade 2: android-two gives #227 (receiver sees 2 special trades remaining -> continues!)
            # Trade 3: ios-one gives #679 (receiver sees 1 special trade remaining -> stops & marks done!)
            rem_seq = iter([2, 1])
            trades_made: list[tuple[str, str]] = []

            @contextlib.asynccontextmanager
            async def fake_open(name, *_a, **_kw):
                yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

            async def fake_choose_lowest(phone, pool, ceiling=None, seen_left=None):
                rem = next(rem_seq)
                if seen_left is not None:
                    seen_left.append(rem)
                return f"CP {1900 + rem}"

            async def fake_trade_once(a, b, _names, terms, species, max_special_price=None, choosers=None, **_kw):
                if b.name == "android-one":
                    raise trade_local.LuckyAvailable("lucky waiting")
                await choosers[a.name](a)
                trades_made.append((b.name, species[b.name]))

            args = SimpleNamespace(
                report=report_path,
                prepare=False,
                plan_only=False,
                local=False,
                devices=None,
                all_today=True,
                count=1,
                ignore_today=False,
                max_stardust=40000,
                shots=Path(tmp) / "shots",
            )
            with mock.patch.object(special_trade.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(special_trade.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(special_trade.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(special_trade.remote_target, "open_fleet_phone", side_effect=fake_open), \
                    mock.patch.object(special_trade.trade_local, "trainer_name", new_callable=mock.AsyncMock, side_effect=lambda p: f"t-{p.name}"), \
                    mock.patch.object(special_trade, "choose_lowest", side_effect=fake_choose_lowest), \
                    mock.patch.object(special_trade.trade_local, "trade_once", side_effect=fake_trade_once):
                rep = asyncio.run(special_trade.special_step(args))

            self.assertTrue(rep.ok)
            self.assertEqual(args.made, 2)
            self.assertEqual(trades_made, [("android-two", "Skarmory"), ("ios-one", "Honedge")])
            self.assertIsNotNone(special_trade.done_today())
            updated = report_path.read_text()
            self.assertNotIn("Honedge", updated)
            self.assertNotIn("Skarmory", updated)

    def test_unavailable_ios_partner_is_skipped_without_blocking_other_partners(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        owners = {"android-three": "host-a", "ios-one": "host-a", "android-two": "host-b"}
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "gap.md"
            report_path.write_text(REPORT)
            special_trade.save_prepared({
                "give": {"kyurem": 2},
                "partners": {"ios-one": [679], "android-two": [227]},
            })
            trades_made: list[tuple[str, str]] = []

            @contextlib.asynccontextmanager
            async def fake_open(name, *_a, **_kw):
                if name == "ios-one":
                    raise special_trade.gbl_ios.IOSGBLError("screen is off")
                yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

            async def fake_choose_lowest(_phone, _pool, ceiling=None, seen_left=None):
                if seen_left is not None:
                    seen_left.append(2)
                return "CP 1902"

            async def fake_trade_once(a, b, _names, _terms, species, max_special_price=None, choosers=None, **_kw):
                await choosers[a.name](a)
                trades_made.append((b.name, species[b.name]))

            args = SimpleNamespace(
                report=report_path,
                prepare=False,
                plan_only=False,
                local=False,
                devices=None,
                all_today=True,
                count=1,
                ignore_today=False,
                max_stardust=40000,
                shots=Path(tmp) / "shots",
            )
            with mock.patch.object(special_trade.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(special_trade.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(special_trade.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(special_trade.remote_target, "open_fleet_phone", side_effect=fake_open), \
                    mock.patch.object(special_trade.trade_local, "trainer_name", new_callable=mock.AsyncMock, side_effect=lambda p: f"t-{p.name}"), \
                    mock.patch.object(special_trade, "choose_lowest", side_effect=fake_choose_lowest), \
                    mock.patch.object(special_trade.trade_local, "trade_once", side_effect=fake_trade_once):
                rep = asyncio.run(special_trade.special_step(args))

        self.assertTrue(rep.ok)
        self.assertEqual(trades_made, [("android-two", "Skarmory")])
        self.assertTrue(any("ios-one: skipped (unavailable" in line for line in rep.lines))

    def test_all_partners_unavailable_reports_failure(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "android-one": SimpleNamespace(name="android-one", enabled=True, platform="android"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        owners = {"android-three": "host-a", "android-one": "host-b", "android-two": "host-b"}
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "gap.md"
            report_path.write_text(REPORT)
            special_trade.save_prepared({
                "give": {"kyurem": 2},
                "partners": {"android-one": [227], "android-two": [227]},
            })

            @contextlib.asynccontextmanager
            async def fake_open(name, *_a, **_kw):
                if name != "android-three":
                    raise special_trade.pokemon_fleet.FleetError(f"{name} is already controlled by pid 1234")
                yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

            args = SimpleNamespace(
                report=report_path,
                prepare=False,
                plan_only=False,
                local=False,
                devices=None,
                all_today=False,
                count=1,
                ignore_today=False,
                max_stardust=40000,
                shots=Path(tmp) / "shots",
            )
            with mock.patch.object(special_trade.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(special_trade.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(special_trade.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(special_trade.remote_target, "open_fleet_phone", side_effect=fake_open):
                rep = asyncio.run(special_trade.special_step(args))

        self.assertFalse(rep.ok)
        self.assertIn("unavailable", rep.detail)
        self.assertIn("android-one", rep.detail)
        self.assertIn("android-two", rep.detail)

    def test_greyed_out_shiny_without_confirmed_special_left_preserves_prepared_queue(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "android-one": SimpleNamespace(name="android-one", enabled=True, platform="android"),
            }
        )
        owners = {"android-three": "host-a", "android-one": "host-b"}
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "gap.md"
            report_path.write_text(REPORT)
            special_trade.save_prepared({
                "give": {"kyurem": 2},
                "partners": {"android-one": [227, 433]},
            })

            @contextlib.asynccontextmanager
            async def fake_open(name, *_a, **_kw):
                yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

            async def fake_trade_once(_a, b, _names, _terms, species, max_special_price=None, choosers=None, **_kw):
                raise trade_local.TradeOver(f"the {species[b.name]} left cannot be traded")

            args = SimpleNamespace(
                report=report_path,
                prepare=False,
                plan_only=False,
                local=False,
                devices=None,
                all_today=False,
                count=1,
                ignore_today=False,
                max_stardust=40000,
                shots=Path(tmp) / "shots",
            )
            with mock.patch.object(special_trade.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(special_trade.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(special_trade.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(special_trade.remote_target, "open_fleet_phone", side_effect=fake_open), \
                    mock.patch.object(special_trade.trade_local, "trainer_name", new_callable=mock.AsyncMock, side_effect=lambda p: f"t-{p.name}"), \
                    mock.patch.object(special_trade.trade_local, "trade_once", side_effect=fake_trade_once):
                rep = asyncio.run(special_trade.special_step(args))

            self.assertEqual(special_trade.load_prepared()["partners"]["android-one"], [227, 433])
            self.assertFalse(special_trade.missing_recently("android-one", 227))
            self.assertTrue(any("stopped before wiping prepared queue" in line for line in rep.lines))

    def test_trade_expired_dialog_is_recognized(self) -> None:
        self.assertTrue(trade_local.expired([
            box("Trade expired.", 360, 620),
            box("This trade was canceled.", 360, 720),
            box("OK", 360, 880),
        ]))
        self.assertTrue(trade_local.expired([
            box("This trade with BadMannas has", 360, 620),
            box("expired.", 360, 680),
            box("OK", 360, 880),
        ]))
        self.assertTrue(trade_local.max_move_warning([
            box("Are you sure you want to trade", 360, 560),
            box("this Pokémon?", 360, 620),
            box("Max Move levels will be reset", 360, 700),
            box("YES", 360, 840),
            box("NO", 360, 960),
        ]))
        self.assertFalse(trade_local.max_move_warning([
            box("Are you sure you want to trade", 360, 560),
            box("Buddy Level will be reset", 360, 700),
            box("YES", 360, 840),
        ]))

    def test_choose_lowest_exits_fast_on_settled_zero_without_research(self) -> None:
        img = Image.new("RGB", (720, 1600))
        picker_zero = [
            box("POKEMON", 300, 120),
            box("3 Special Trades remaining", 200, 180),
            box("(0)", 340, 220),
        ]
        reads = iter([(img, picker_zero), (img, picker_zero), (img, picker_zero)])
        searches: list[str] = []
        phone = SimpleNamespace(
            name="android-three",
            ios=False,
            target=SimpleNamespace(_adb=mock.MagicMock()),
            read=mock.AsyncMock(side_effect=lambda: next(reads)),
            tap=mock.AsyncMock(),
            say=mock.MagicMock(),
            save=lambda _i, label: f"/tmp/{label}.png",
        )
        with mock.patch.object(special_trade.trade_local, "search_picker", new_callable=mock.AsyncMock,
                               side_effect=lambda _p, t, force=False: searches.append(t)), \
                mock.patch.object(special_trade.trade_local, "POLL", 0), \
                mock.patch.object(special_trade.asyncio, "sleep", new_callable=mock.AsyncMock):
            with self.assertRaises(trade_local.TradeOver) as ctx:
                asyncio.run(special_trade.choose_lowest(phone, special_trade.pool_term(["zacian"])))
        self.assertIn("nothing for", str(ctx.exception))
        self.assertEqual(len(searches), 1)

    def test_choose_lowest_recovers_last_valid_tiles_when_downward_probe_is_empty(self) -> None:
        img = Image.new("RGB", (720, 1600))
        picker_many = [
            box("POKEMON", 300, 120),
            box("3 Special Trades remaining", 200, 180),
            box("(45)", 340, 220),
            box("CP 2350", 140, 500),
            box("CP 2688", 360, 500),
        ]
        picker_zero = [
            box("POKEMON", 300, 120),
            box("3 Special Trades remaining", 200, 180),
            box("(0)", 340, 220),
        ]
        picker_recovered = [
            box("POKEMON", 300, 120),
            box("3 Special Trades remaining", 200, 180),
            box("(2)", 340, 220),
            box("CP 2350", 140, 500),
        ]
        detail_regular = [
            box("Mewtwo", 250, 270),
            box("CP 2350", 250, 320),
            box("Psycho Cut", 220, 620),
            box("Psystrike", 220, 680),
            box("NEXT", 360, 1300),
        ]
        reads = iter([
            (img, picker_many),       # initial rem check
            (img, picker_many),       # round 0 settle
            (img, picker_zero),       # round 1 narrowed (0) settle
            (img, picker_zero),       # round 1 confirm (0)
            (img, picker_recovered),  # live recovery re-search of cp-2350
            (img, detail_regular),    # detail of recovered CP 2350
        ])
        searches: list[str] = []
        phone = SimpleNamespace(
            name="android-three",
            ios=False,
            target=SimpleNamespace(_adb=mock.MagicMock()),
            read=mock.AsyncMock(side_effect=lambda: next(reads)),
            tap=mock.AsyncMock(),
            say=mock.MagicMock(),
            save=lambda _i, label: f"/tmp/{label}.png",
        )
        with mock.patch.object(special_trade.trade_local, "search_picker", new_callable=mock.AsyncMock,
                               side_effect=lambda _p, t, force=False: searches.append(t)), \
                mock.patch.object(special_trade.trade_local, "POLL", 0), \
                mock.patch.object(special_trade.asyncio, "sleep", new_callable=mock.AsyncMock):
            picked = asyncio.run(special_trade.choose_lowest(phone, special_trade.spare_term(["mewtwo"])))
        self.assertEqual(picked, "CP 2350")
        self.assertEqual(len(searches), 3)
        self.assertIn("cp-2350", searches[1])
        self.assertIn("cp-2350", searches[2])
