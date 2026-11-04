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

    def test_ph1_keeps_its_last_of_each_species(self) -> None:
        a = PhoneCounts("android-two", {"pichu": 0}, {"pikachu": 9})
        ph1 = PhoneCounts("android-three", {"pichu": 0}, {"pikachu": 4}, {"pikachu": 4})
        self.assertEqual(sum(x.count for x in trade_plan.plan_pair(a, ph1, [PIKA])), 3)
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
        for part in ("!#", "!shiny", "!costume", "!4*", "!shadow", "!traded"):
            self.assertIn(part, term)


class ConfirmTests(unittest.TestCase):
    """The confirm screen as read on the android-two, 720x1600 (4 Nov)."""

    def test_the_price_beside_confirm(self) -> None:
        boxes = [box("CONFIRM", 20, 810, 120, 30), box("100", 300, 812, 50, 26), box("+2", 560, 830, 30, 20)]
        self.assertEqual(trade_local.price(_Image(720, 1600), boxes), 100)

    def test_a_special_trade_is_named(self) -> None:
        self.assertTrue(trade_local.special([box("SPECIAL TRADE", 200, 400)]))
        self.assertFalse(trade_local.special([box("SPECIAL TRADES REMAINING: 1", 200, 300)]))

    def test_own_name_sits_left_of_add_friend(self) -> None:
        boxes = [box("Example One", 60, 319), box("ADD FRIEND", 202, 316), box("SEARCH", 392, 316)]
        self.assertEqual(trade_local.own_name(_Image(720, 1600), boxes), "ExampleOne")
        merged = [box("ExampleTrainer ADD FRIEND", 39, 473, 600), box("SEARCH", 592, 469)]
        self.assertEqual(trade_local.own_name(_Image(1080, 2412), merged), "ExampleTrainer")


class _Image:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height


if __name__ == "__main__":
    unittest.main()
