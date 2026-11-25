from __future__ import annotations

from tests import support as _test_support  # noqa: F401

import unittest

from sources import gbl_vision, trade_local, trade_plan
from sources.trade_plan import Family, PhoneCounts

PIKA = Family("pichu", "Mega Raichu Y (Electric #1)", ("pikachu", "raichu", "pichu"), 1.0)
RALTS = Family("ralts", "Mega Gardevoir (Fairy #1)", ("ralts", "kirlia", "gardevoir"), 1.0)


def box(text, x, y, w=80, h=30):
    return gbl_vision.OCRBox(text, 1.0, x, y, w, h)


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        trade_plan.KEEP_LAST.clear()
        trade_plan.KEEP_LAST["android-three"] = 1
        self.addCleanup(trade_plan.KEEP_LAST.clear)

    def test_a_family_both_phones_have_is_left_alone(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 1}, {"pikachu": 5})
        b = PhoneCounts("android-four", {"pichu": 2}, {"pikachu": 5})
        self.assertEqual(trade_plan.plan_pair(a, b, [PIKA]), [])

    def test_a_lucky_trade_is_read_off_the_screen(self) -> None:
        self.assertTrue(trade_local.lucky([box("LUCKY TRADE", 200, 300)]))
        self.assertFalse(trade_local.lucky([box("CONFIRM", 20, 810)]))

    def test_the_needing_phone_gives_back_from_the_same_family(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 0}, {"pikachu": 5, "raichu": 4})
        b = PhoneCounts("android-four", {"pichu": 3}, {"pikachu": 6})
        batches = trade_plan.plan_pair(a, b, [PIKA])
        self.assertEqual([(x.give, x.count, x.lucky_only) for x in batches],
                         [({"android-four": "pikachu", "android-two": "pikachu"}, 5, False),
                          ({"android-four": "pikachu", "android-two": "raichu"}, 1, True)])

    def test_with_nothing_of_the_family_it_sends_its_biggest_raid_spare(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 0, "ralts": 1}, {"ralts": 9})
        b = PhoneCounts("android-four", {"pichu": 1, "ralts": 1}, {"pikachu": 4})
        batches = trade_plan.plan_pair(a, b, [PIKA, RALTS])
        self.assertEqual([(x.give, x.count) for x in batches], [({"android-four": "pikachu", "android-two": "ralts"}, 4)])

    def test_a_few_spares_wait_for_a_lucky_trade(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 0, "ralts": 1}, {"ralts": 9})
        b = PhoneCounts("android-four", {"pichu": 1, "ralts": 1}, {"pikachu": 2})
        self.assertTrue(trade_plan.plan_pair(a, b, [PIKA, RALTS])[0].lucky_only)

    def test_a_return_copy_is_never_one_of_the_few(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 0, "ralts": 1}, {"ralts": 2})
        b = PhoneCounts("android-four", {"pichu": 1, "ralts": 1}, {"pikachu": 6})
        self.assertEqual(trade_plan.plan_pair(a, b, [PIKA, RALTS]), [])

    def test_receiver_keeps_its_last_of_each_species(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 0}, {"pikachu": 9})
        receiver = PhoneCounts("android-three", {"pichu": 0}, {"pikachu": 4}, {"pikachu": 4})
        self.assertEqual(sum(x.count for x in trade_plan.plan_pair(a, receiver, [PIKA])), 3)
        only = PhoneCounts("android-three", {"pichu": 0}, {"pikachu": 1}, {"pikachu": 1})
        self.assertEqual(trade_plan.plan_pair(a, only, [PIKA]), [])

    def test_other_phones_may_give_every_spare(self) -> None:
        self.assertEqual(trade_plan.can_give(PhoneCounts("android-two", {}, {"pikachu": 1}, {"pikachu": 1}), "pikachu"), 1)

    def test_the_day_stops_at_the_budget(self) -> None:
        a = PhoneCounts("android-two", {}, {"pikachu": 80, "ralts": 80})
        b = PhoneCounts("android-four", {}, {"pikachu": 80, "ralts": 80})
        batches = trade_plan.plan_pair(a, b, [PIKA, RALTS], budget=100)
        self.assertEqual(sum(x.count for x in batches), 100)

    def test_a_swap_is_suggested_only_when_the_other_mac_can_help(self) -> None:
        here = [PhoneCounts("android-two", {"pichu": 0}, {"pikachu": 0}), PhoneCounts("android-three", {"pichu": 1}, {"pikachu": 0})]
        away = [PhoneCounts("android-four", {"pichu": 1}, {"pikachu": 6})]
        hints = trade_plan.swap_hints(here, away, [PIKA])
        self.assertEqual(len(hints), 1)
        self.assertIn("android-two", hints[0])
        self.assertIn("android-four", hints[0])
        self.assertEqual(trade_plan.swap_hints(here, [], [PIKA]), [])

    def test_the_trade_search_leaves_tags_shinies_and_hundos_home(self) -> None:
        term = trade_plan.trade_term("pikachu")
        for part in (
            "!#", "!dontrade", "!xs", "!xl", "!xxs", "!xxl",
            "!favorite", "!shiny", "!costume", "!purified", "!4*", "!lucky", "!shadow", "!traded",
        ):
            self.assertIn(part, term)

    def test_glimmet_excluded_from_trade_plan_families(self) -> None:
        from types import SimpleNamespace
        from sources import raid_meta
        Rating = lambda pid, name, er, leg=False, shad=False: type("R", (), {  # noqa: E731
            "attacker": type("A", (), {"pokemon_id": pid, "name": name, "legendary": leg, "shadow": shad})(),
            "er": er,
        })()
        fake_rankings = {
            "Rock": [Rating("glimmora", "Glimmora", 20.0), Rating("tyranitar", "Tyranitar", 19.0)],
        }
        fake_gd = SimpleNamespace(pokemon={
            "glimmet": {"search": "glimmet", "legendary": False},
            "glimmora": {"search": "glimmora", "legendary": False},
            "larvitar": {"search": "larvitar", "legendary": False},
            "pupitar": {"search": "pupitar", "legendary": False},
            "tyranitar": {"search": "tyranitar", "legendary": False},
        })
        fake_tree = ({"glimmora": "glimmet", "pupitar": "larvitar", "tyranitar": "pupitar"}, {})
        with __import__("unittest.mock").mock.patch.object(raid_meta, "rankings", return_value=fake_rankings), \
                __import__("unittest.mock").mock.patch.object(trade_plan, "_evolution_tree", return_value=fake_tree):
            fams = trade_plan.expanded_families(fake_gd)
        self.assertEqual([f.key for f in fams], ["larvitar"])

    def test_three_tier_plan_fills_up_to_100_mirrors_first_and_respects_small(self) -> None:
        fams = [
            Family("pichu", "Mega Raichu Y", ("pikachu", "raichu", "pichu"), 1.0),
            Family("ralts", "Mega Gardevoir", ("ralts", "kirlia", "gardevoir"), 0.95),
            Family("beldum", "Mega Metagross", ("beldum", "metang", "metagross"), 0.90),
        ]
        # Tier 1: a has 0 pichu; Tier 2: b has 2 ralts (<6); Tier 3: both have >=6 beldum + spares;
        # `pichu` on b has 2 (<SMALL) so it stays a Lucky Trade suggestion and does not consume budget.
        a = PhoneCounts("android-one", {"pichu": 0, "ralts": 6, "beldum": 8},
                        {"pikachu": 10, "ralts": 25, "beldum": 75})
        b = PhoneCounts("android-two", {"pichu": 4, "ralts": 2, "beldum": 7},
                        {"pikachu": 2, "ralts": 25, "beldum": 80})
        batches = trade_plan.plan_pair(a, b, fams, budget=100, target_good=6, fill_budget=True)
        lucky = [x for x in batches if x.lucky_only]
        real = [x for x in batches if not x.lucky_only]
        self.assertEqual(sum(x.count for x in real), 100)
        self.assertTrue(any(x.family.key == "pichu" for x in lucky))
        # Tier 2 ralts mirror first, then Tier 2 pichu (<6 on b) before Tier 3 beldum bulk reroll.
        self.assertEqual(real[0].family.key, "ralts")
        self.assertEqual(real[0].count, 25)
        self.assertIn("mirror", real[0].reason)
        self.assertEqual(real[1].family.key, "pichu")
        self.assertEqual(real[1].count, 10)
        self.assertEqual(real[2].family.key, "beldum")
        self.assertEqual(real[2].count, 65)

    def test_expanded_families_include_single_stage_and_shadow_lines(self) -> None:
        from types import SimpleNamespace
        from sources import raid_meta
        Rating = lambda pid, name, er, leg=False, shad=False: type("R", (), {  # noqa: E731
            "attacker": type("A", (), {"pokemon_id": pid, "name": name, "legendary": leg, "shadow": shad})(),
            "er": er,
        })()
        fake_rankings = {
            "Bug": [Rating("heracross", "Heracross", 20.0), Rating("genesect", "Genesect", 18.0, leg=True)],
            "Rock": [Rating("gigalith_shadow", "Shadow Gigalith", 19.0, shad=True)],
        }
        fake_gd = SimpleNamespace(pokemon={
            "heracross": {"search": "heracross", "legendary": False},
            "roggenrola": {"search": "roggenrola", "legendary": False},
            "boldore": {"search": "boldore", "legendary": False},
            "gigalith": {"search": "gigalith", "legendary": False},
            "darumaka": {"search": "darumaka", "legendary": False},
            "darmanitan_standard": {"search": "darmanitan", "legendary": False},
        })
        fake_tree = ({"boldore": "roggenrola", "gigalith": "boldore", "darmanitan": "darumaka"}, {})
        with __import__("unittest.mock").mock.patch.object(raid_meta, "rankings", return_value=fake_rankings), \
                __import__("unittest.mock").mock.patch.object(trade_plan, "_evolution_tree", return_value=fake_tree):
            strict = trade_plan.families(fake_gd, expanded=False)
            self.assertEqual(strict, [])
            exp = trade_plan.expanded_families(fake_gd)
        keys = [f.key for f in exp]
        self.assertEqual(keys, ["heracross", "roggenrola"])
        self.assertEqual(trade_plan._search_name_for("darmanitan", fake_gd), "darmanitan")


class StarterAndDailyTradeTests(unittest.TestCase):
    def test_lucky_only_batches_do_not_raise_unbound_or_open_phones(self) -> None:
        from unittest import mock
        import asyncio
        from types import SimpleNamespace
        from sources import starter_trade

        a = PhoneCounts("android-one", {"pichu": 0, "ralts": 1}, {"pikachu": 0, "ralts": 5})
        b = PhoneCounts("android-two", {"pichu": 2, "ralts": 1}, {"pikachu": 2, "ralts": 0})
        spec_a = SimpleNamespace(name="android-one")
        spec_b = SimpleNamespace(name="android-two")
        args = SimpleNamespace(trades=100, reuse_counts=False, trade_plan_only=False, shots=_TMP)
        with mock.patch.object(trade_plan, "families", return_value=[PIKA, RALTS]), \
                mock.patch.object(starter_trade, "inventory", return_value=[a, b]), \
                mock.patch.object(starter_trade, "other_counts", return_value=[]), \
                mock.patch.object(starter_trade, "NEEDS_DIR", _TMP), \
                mock.patch.object(starter_trade, "pair_phones") as open_pair:
            report = asyncio.run(starter_trade.trade_step([spec_a, spec_b], args))
        open_pair.assert_not_called()
        self.assertIn("nothing to trade", report.detail)
        self.assertIn("Lucky Trade suggestion", report.detail)

        # Also verify truly empty batches (no lucky suggestions either)
        a_empty = PhoneCounts("android-one", {"pichu": 1}, {"pikachu": 0})
        b_empty = PhoneCounts("android-two", {"pichu": 1}, {"pikachu": 0})
        with mock.patch.object(trade_plan, "families", return_value=[PIKA]), \
                mock.patch.object(starter_trade, "inventory", return_value=[a_empty, b_empty]), \
                mock.patch.object(starter_trade, "other_counts", return_value=[]), \
                mock.patch.object(starter_trade, "NEEDS_DIR", _TMP), \
                mock.patch.object(starter_trade, "pair_phones") as open_pair_empty:
            report_empty = asyncio.run(starter_trade.trade_step([spec_a, spec_b], args))
        open_pair_empty.assert_not_called()
        self.assertEqual(report_empty.detail, "nothing to trade")

    def test_daily_history_and_saved_count_decrement(self) -> None:
        from datetime import datetime
        from unittest import mock
        from sources import starter_trade

        now = datetime(2026, 11, 19, 12, 0, 0)
        tmp = Path(tempfile.mkdtemp())
        c_a = PhoneCounts("android-one", {"pichu": 0}, {"pikachu": 5}, {"pikachu": 6})
        c_b = PhoneCounts("android-two", {"pichu": 1}, {"pikachu": 4})
        with mock.patch.object(starter_trade, "NEEDS_DIR", tmp):
            self.assertEqual(starter_trade.trades_done_today("android-one", now), 0)
            starter_trade.record_trade(["android-one", "android-two"],
                                       {"android-one": "pikachu", "android-two": "pikachu"},
                                       "pichu", "test", now=now)
            starter_trade.decrement_saved_counts([c_a, c_b],
                                                 {"android-one": "pikachu", "android-two": "pikachu"})
            self.assertEqual(starter_trade.trades_done_today("android-one", now), 1)
            self.assertEqual(c_a.tradable["pikachu"], 4)
            self.assertEqual(c_a.owned["pikachu"], 5)
            self.assertEqual(c_b.tradable["pikachu"], 3)

    def test_trade_once_accepts_discounted_normal_price_and_rejects_special(self) -> None:
        import asyncio
        from unittest import mock
        from sources import trade_local

        class FakePhone:
            def __init__(self, name: str, cost_text: str) -> None:
                self.name = name
                self.ios = False
                self._cost_text = cost_text

            async def read(self):
                from PIL import Image
                return Image.new("RGB", (720, 1600), (106, 188, 244)), [
                    box("CONFIRM", 20, 810, 120, 30),
                    box(self._cost_text, 300, 812, 50, 26),
                ]

            async def look(self):
                return _Image(720, 1600)

            def save(self, _img, label):
                return _TMP / f"{self.name}-{label}.png"

            async def tap_box(self, _box, _img):
                pass

        async def noop(*_a, **_kw):
            return None

        async def received(phone, _partner, _special_ok, receipts, _offer):
            receipts[phone.name] = trade_local.TradeReceipt(_TMP / "received.png", "Pikachu", None)
            return "CP120"

        a = FakePhone("android-one", "50")
        b = FakePhone("android-two", "50")
        with mock.patch.object(trade_local, "open_partner", side_effect=noop), \
                mock.patch.object(trade_local, "start_local", side_effect=noop), \
                mock.patch.object(trade_local, "choose", side_effect=noop), \
                mock.patch.object(trade_local, "press", side_effect=noop), \
                mock.patch.object(trade_local, "finish", side_effect=received):
            asyncio.run(trade_local.trade_once(
                a, b, {"android-one": "T1", "android-two": "T2"},
                {"android-one": "t", "android-two": "t"},
                {"android-one": "pikachu", "android-two": "pikachu"},
            ))

        bad_b = FakePhone("android-two", "800")
        with mock.patch.object(trade_local, "open_partner", side_effect=noop), \
                mock.patch.object(trade_local, "start_local", side_effect=noop), \
                mock.patch.object(trade_local, "choose", side_effect=noop), \
                mock.patch.object(trade_local, "press", side_effect=noop), \
                mock.patch.object(trade_local, "cancel", side_effect=noop):
            with self.assertRaises(trade_local.TradeRefused):
                asyncio.run(trade_local.trade_once(
                    a, bad_b, {"android-one": "T1", "android-two": "T2"},
                    {"android-one": "t", "android-two": "t"},
                    {"android-one": "pikachu", "android-two": "pikachu"},
                ))

    def test_daily_trade_routing_and_plan(self) -> None:
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest import mock
        import daily_trade as public_daily
        from sources import collector_day, daily_trade, fleet_entrypoint, trade_history

        with mock.patch.object(fleet_entrypoint, "run_all_machines", return_value=0) as fanout:
            self.assertEqual(public_daily.main(["--plan"]), 0)
        fanout.assert_called_once_with("daily_trade.py", ["--plan"])

        fake_specs = [
            SimpleNamespace(name="android-one", platform="android", identifier="SER1", enabled=True),
            SimpleNamespace(name="ios-one", platform="ios", identifier="UDID1", enabled=True),
        ]
        buf = io.StringIO()
        with mock.patch.object(daily_trade, "attached_pair", return_value=fake_specs), \
                mock.patch.object(trade_plan, "expanded_families", return_value=[PIKA, RALTS]), \
                mock.patch.object(trade_history, "current_day_counts", return_value=collector_day.DayCounts(
                    {"android-one": 12, "ios-one": 12}, set())), \
                mock.patch.object(daily_trade.starter_trade, "saved_counts", return_value=None), \
                redirect_stdout(buf):
            code = daily_trade.main(["--plan"])
        self.assertEqual(code, 0)
        self.assertIn("88/100 remaining today", buf.getvalue())


class ConfirmTests(unittest.TestCase):
    """The confirm screen as read on the android-two, 720x1600 (4 Nov)."""

    def test_the_price_beside_confirm(self) -> None:
        boxes = [box("CONFIRM", 20, 810, 120, 30), box("100", 300, 812, 50, 26), box("+2", 560, 830, 30, 20)]
        self.assertEqual(trade_local.price(_Image(720, 1600), boxes), 100)

    def test_a_special_trade_is_named(self) -> None:
        self.assertTrue(trade_local.special([box("SPECIAL TRADE", 200, 400)]))
        self.assertFalse(trade_local.special([box("SPECIAL TRADES REMAINING: 1", 200, 300)]))

    def test_a_picker_with_a_leftover_search(self) -> None:
        # ios-one, 750x1334, after a hand-made trade searched "ze" (4 Nov).
        boxes = [box("POKÉMON", 375, 91), box("Q (7)", 365, 136), box("ze", 198, 251, 30, 26),
                 box("CP 2862", 132, 391), box("Zekrom", 138, 559)]
        image = _Image(750, 1334)
        self.assertTrue(trade_local.picker(image, boxes))
        self.assertEqual(trade_local.picker_field(image, boxes).text, "ze")
        storage = boxes + [box("TAGS", 146, 93), box("EGGS", 605, 93)]
        self.assertFalse(trade_local.picker(image, storage))

    def test_a_picker_whose_heading_has_a_stray_glyph(self) -> None:
        # sample-android-b, 1080x2412, 24 Nov: OCR read the heading as "POKÉMON I" and
        # the open picker failed twice, blocking the pair for the whole run.
        image = _Image(1080, 2412)
        boxes = [box("POKÉMON I", 540, 131), box("Q (39)", 520, 196),
                 box("!dontrade&.98plus&xxl,xxs&!traded", 600, 363),
                 box("SPECIAL TRADES REMAINING: 1", 540, 454)]
        self.assertTrue(trade_local.picker(image, boxes))
        # The loosened heading only counts at the top of the screen.
        low = [box("POKÉMON I", 540, 900), *boxes[1:]]
        self.assertFalse(trade_local.picker(image, low))

    def test_own_name_sits_left_of_add_friend(self) -> None:
        boxes = [box("Example One", 60, 319), box("ADD FRIEND", 202, 316), box("SEARCH", 392, 316)]
        self.assertEqual(trade_local.own_name(_Image(720, 1600), boxes), "ExampleOne")
        merged = [box("ExampleTrainer ADD FRIEND", 39, 473, 600), box("SEARCH", 592, 469)]
        self.assertEqual(trade_local.own_name(_Image(1080, 2412), merged), "ExampleTrainer")


class UnreadTests(unittest.TestCase):
    """An unreadable search takes the safe value; a run of them refuses the count."""

    def run_count(self, results, unknown):
        from unittest import mock
        from types import SimpleNamespace
        from sources import starter_trade

        class Scanner:
            def ensure_grid(self):
                pass

            def search(self, term):
                return results[term], [], _Saveable(), []

        phone = SimpleNamespace(close=lambda: None)
        spec = SimpleNamespace(name="ios-one")
        with mock.patch.object(starter_trade, "_scanner", return_value=(phone, Scanner())), \
                mock.patch.object(starter_trade, "NEEDS_DIR", _TMP):
            return starter_trade._count_terms(spec, list(results), unknown)

    def test_one_unread_takes_the_safe_value(self) -> None:
        self.assertEqual(self.run_count({"a": 3, "b": None}, unknown=1), {"a": 3, "b": 1})

    def test_many_unread_refuse_the_count(self) -> None:
        from sources.starter import StepFailed
        with self.assertRaises(StepFailed):
            self.run_count({k: None for k in "abcd"}, unknown=0)


class _Saveable:
    def save(self, path) -> None:
        pass


import tempfile
from pathlib import Path
_TMP = Path(tempfile.mkdtemp())


class _Image:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height


if __name__ == "__main__":
    unittest.main()
