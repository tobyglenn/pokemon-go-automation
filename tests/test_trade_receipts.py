from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PIL import Image

from tests import support as _test_support  # noqa: F401
from sources import trade_local as trade


FRAME = Image.new("RGB", (750, 1334))


def box(text, x, y):
    return trade.OCRBox(text, 1.0, x, y, 90, 20)


class TradeReceiptTests(unittest.TestCase):
    def test_only_received_card_is_evidence(self):
        cp = box("CP279", 325, 110)
        hp = box("77 / 77 HP", 330, 680)
        name = box("Lechonk", 328, 614)
        for text in ("NEXT", "CONFIRM", "WAITING...", "Trade was canceled", "trade expired"):
            self.assertIsNone(trade._receipt_fields(FRAME, [cp, hp, name, box(text, 350, 1150)]))
        self.assertIsNone(trade._receipt_fields(FRAME, [cp]))
        self.assertIsNone(trade._receipt_fields(FRAME, [box("CP 105 - 300", 190, 340),
                                                        box("HP 52 - 73", 140, 400)]))
        self.assertEqual(trade._receipt_fields(FRAME, [cp, hp, name]), ("CP279", "Lechonk", None))
        self.assertEqual(trade._receipt_fields(FRAME, [cp, hp, name, box("Lucky Pokémon", 335, 645)]),
                         ("CP279", "Lechonk", True))

    def test_identical_price_does_not_authorize_different_offer(self):
        row = [box("CONFIRM", 30, 590), box("80", 275, 590)]
        first = box("CP 200 - 350", 110, 230)
        second = box("CP 70 - 101", 380, 1040)
        approved = trade._offer_signature(FRAME, row + [first, second])
        self.assertIsNotNone(approved)
        self.assertNotEqual(trade._offer_signature(FRAME, row + [first, box("CP 71 - 91", 380, 1040)]), approved)
        self.assertIsNone(trade._offer_signature(FRAME, row))
        self.assertIsNone(trade._offer_signature(FRAME, row + [first, second, box("Lucky Trade", 100, 220)]))

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(trade.config_paths, "state_dir", return_value=root))
        self.phones = []
        self.names = {"a": "AlphaTrain", "b": "BetaTrain"}
        self.species = {"a": "Gible", "b": "Dratini"}
        for key in self.names:
            p = mock.Mock()
            p.name = key
            p.read = mock.AsyncMock(return_value=(FRAME, [box("CONFIRM", 30, 590), box("80", 275, 590)]))
            p.tap_box = mock.AsyncMock()
            p.look = mock.AsyncMock(return_value=FRAME)
            p.save.return_value = root / (key + ".png")
            self.phones.append(p)
        for method in ("open_partner", "start_local", "choose", "press"):
            self.stack.enter_context(mock.patch.object(trade, method, new_callable=mock.AsyncMock))
        self.paths = trade._pending_files(self.names)

    def exchange(self, callback=None, **kwargs):
        return asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                            on_confirmed=callback, **kwargs))

    def test_shared_cap_is_verified_while_the_pair_is_reserved(self):
        def reached_cap(_names, _cap):
            self.assertTrue(all(path.is_file() for path in self.paths))
            return False

        with mock.patch.object(trade.trade_history, "has_budget", side_effect=reached_cap), \
                mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaisesRegex(trade.TradeOver, "shared trade count"):
                self.exchange(daily_cap=1)
        self.assertFalse(any(path.exists() for path in self.paths))
        for phone in self.phones:
            phone.tap_box.assert_not_awaited()

    def test_checks_current_confirm_screen_after_shared_state_wait(self):
        def expired_while_waiting(_names, _cap):
            self.phones[0].read.return_value = FRAME, [box("Trade expired", 250, 550), box("OK", 330, 700)]
            return True

        with mock.patch.object(trade.trade_history, "has_budget", side_effect=expired_while_waiting), \
                mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.TradeCancelled):
                self.exchange(daily_cap=1)
        self.assertFalse(any(path.exists() for path in self.paths))
        for phone in self.phones:
            phone.tap_box.assert_not_awaited()

    def test_profile_only_then_restart_never_redispatch(self):
        with mock.patch.object(trade, "finish", new=mock.AsyncMock(return_value="?")):
            with self.assertRaises(trade.TradeOutcomeUnknown):
                self.exchange()
            self.assertTrue(all(path.is_file() for path in self.paths))
            with self.assertRaises(trade.TradeOutcomeUnknown):
                self.exchange()
        for p in self.phones:
            p.tap_box.assert_awaited_once()

    def test_history_runs_with_receipt_before_unlock(self):
        async def finish(phone, _partner, _special_ok, receipts, _offer):
            if phone.name == "a":
                receipts["a"] = trade.TradeReceipt(phone.save.return_value, "Gible", None)
            return "?"

        def record(receipts):
            self.assertEqual(receipts["a"].label, "Gible")
            self.assertTrue(all(path.exists() for path in self.paths))

        with mock.patch.object(trade, "finish", side_effect=finish):
            result = self.exchange(record)
            self.assertEqual(set(result), {"a"})
            self.assertFalse(any(path.exists() for path in self.paths))
            with self.assertRaises(trade.TradeOutcomeUnknown):
                self.exchange(mock.Mock(side_effect=OSError("cannot append history")))
            self.assertTrue(all(path.exists() for path in self.paths))

    def lucky_screens(self, unmarked=None, price="80", nickname=False):
        self.details = {p.name: {"species": "Beldum", "dex": 374, "form": None, "cp": 200, "stock": 1,
                                  "reason": "receiver needs Lucky Beldum", "unique": True} for p in self.phones}
        for p in self.phones:
            peer = self.names["a" if p.name == "b" else "b"]
            profile = [box(peer, 80, 75), box("• Lucky Friend •", 80, 160), box("LOCAL", 320, 945)]
            selected = [box("Beldum", 68, 150), box("CP 200 > CP 160 - 220", 70, 200),
                        box("HP 62 > HP 50 - 65", 70, 242), box("Take Down", 70, 285),
                        box("8", 525, 285), box("7/28/2026", 70, 487), box("NEXT", 290, 1100)]
            screen = [box(peer + " Lv. 72", 380, 70), box("Beldum", 70, 243), box("CP 160 - 220", 70, 290),
                      box("HP 50 - 65", 70, 325), box("Beldum", 520, 898), box("CP 160 - 220", 520, 940),
                      box("HP 50 - 65", 520, 975), box("CONFIRM", 30, 600), box(price, 270, 600)]
            if p.name != unmarked:
                screen.append(box("Lucky Trade", 70 if nickname else 340, 248 if nickname else 480))
            p.read = mock.AsyncMock(side_effect=[(FRAME, profile), (FRAME, selected), (FRAME, screen), (FRAME, screen)])

    def test_reserve_requires_guarantee_on_both_confirm_screens(self):
        self.lucky_screens(unmarked="b")
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.LuckyRequired):
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details))
        for p in self.phones:
            p.tap_box.assert_not_awaited()

    def test_profile_matches_full_handle_across_the_observed_vertical_glyph_readings(self):
        profile = [box("QuartzTraveIer", 80, 75), box("•. Lucky Friend •", 80, 160), box("LOCAL", 320, 945)]
        self.assertTrue(trade.guaranteed_profile(FRAME, profile, "QuartzTraveler"))
        self.assertFalse(trade.guaranteed_profile(FRAME, profile, "QuartzTraveles"))
        self.assertFalse(trade.guaranteed_profile(FRAME, profile, "QuartzTravel"))

    def test_current_hp_survives_split_cells_or_bullet_arrow(self):
        split = [box("HP 152", 30, 275), box("HP 149 - 155", 220, 275)]
        self.assertEqual(trade._range_line(split, "HP", current=True)[0][1], [152, 149, 155])
        joined = [box("HP 128 • HP 125 - 131", 45, 190)]
        self.assertEqual(trade._range_line(joined, "HP", current=True)[0][1], [128, 125, 131])
        split[1] = box("HP 149 - 155", 220, 490)
        self.assertEqual(trade._range_line(split, "HP", current=True), [])

    def test_species_named_lucky_trade_does_not_spend_reserve(self):
        self.lucky_screens(nickname=True)
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.LuckyRequired):
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details))

    def test_special_trade_badge_is_not_masked_by_remaining_allowance(self):
        remaining = box("3 Special Trades remaining.", 190, 220)
        self.assertFalse(trade.special([remaining]))
        self.assertTrue(trade.special([remaining, box("SPECIAL TRADE", 110, 400)]))

    def test_verified_lucky_pair_still_requires_normal_price(self):
        self.lucky_screens(price="800")
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.TradeRefused):
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details))
        for p in self.phones:
            p.tap_box.assert_not_awaited()

    def test_verified_lucky_pair_records_only_after_receipt(self):
        self.lucky_screens()

        async def received(phone, _partner, _special_ok, receipts, _offer):
            receipts[phone.name] = trade.TradeReceipt(phone.save.return_value, "Beldum", None)
            return "CP110"

        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.trade_approval.ProposalReady) as parked:
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details,
                                             propose_only=True))
        token = parked.exception.proposal_id
        self.assertEqual(trade.trade_approval.awaiting_decision(("b", "a")), token)
        self.assertIsNone(trade.trade_approval.awaiting_decision(("a", "other")))
        for p in self.phones:
            p.tap_box.assert_not_awaited()
        approved = trade.trade_approval.approve(token)
        self.assertEqual(approved["participants"]["a"]["caught"], "7/28/2026")
        self.lucky_screens()
        with trade.trade_approval.claim_for_reselect(token), mock.patch.object(trade, "finish", side_effect=received):
            result = asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                                  require_guaranteed_lucky=True, guaranteed_details=self.details,
                                                  reselect_approval_id=token))
        with self.assertRaises(trade.trade_approval.ApprovalRequired):
            trade.trade_approval.approve(token)
        self.assertTrue(all(r.lucky is None for r in result.values()))
        for p in self.phones:
            p.tap_box.assert_awaited_once()

    def test_conflicting_lucky_result_preserves_evidence_for_reconciliation(self):
        token = self.parked()
        trade.trade_approval.approve(token)
        self.lucky_screens()
        journal = mock.Mock()

        async def received(phone, _partner, _special_ok, receipts, _offer):
            receipts[phone.name] = trade.TradeReceipt(phone.save.return_value, "Beldum", phone.name == "a")
            return "CP110"

        with trade.trade_approval.claim_for_reselect(token), mock.patch.object(trade, "finish", side_effect=received):
            with self.assertRaisesRegex(trade.TradeOutcomeUnknown, "conflicts with the Lucky guarantee"):
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details,
                                             reselect_approval_id=token, on_confirmed=journal))
        journal.assert_not_called()
        for path in self.paths:
            evidence = json.loads(path.read_text())
            self.assertTrue(evidence["guaranteed_lucky"])
            self.assertIs(evidence["receipts"]["b"]["lucky"], False)
        with self.assertRaises(trade.trade_approval.ApprovalRequired):
            trade.trade_approval.approve(token)

    def test_reserved_copy_stays_home_if_profile_does_not_match(self):
        self.lucky_screens()
        profile = [box("UnrelatedTrainer", 80, 75), box("Lucky Friend", 80, 160), box("LOCAL", 320, 945)]
        self.phones[1].read = mock.AsyncMock(return_value=(FRAME, profile))
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.LuckyRequired):
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details))
        trade.start_local.assert_not_awaited()

    def parked(self, *, special=False, price="80"):
        self.lucky_screens(price=price)
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.trade_approval.ProposalReady) as parked:
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details,
                                             propose_special=special, propose_only=True))
        return parked.exception.proposal_id

    def test_declined_approval_cannot_be_used(self):
        token = self.parked()
        self.assertEqual(trade.trade_approval.decline(token), 0)
        with self.assertRaises(trade.trade_approval.ApprovalRequired):
            trade.trade_approval.approve(token)

    def test_changed_cost_blocks_approved_trade(self):
        token = self.parked(special=True, price="800")
        offer = trade.trade_approval.approve(token)
        self.assertEqual(offer["costs"], {"a": 800, "b": 800})
        self.assertTrue(offer["special"])
        self.lucky_screens(price="1600")
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock):
            with self.assertRaises(trade.trade_approval.ApprovalRequired), trade.trade_approval.claim_for_reselect(token):
                asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                             require_guaranteed_lucky=True, guaranteed_details=self.details,
                                             propose_special=True, reselect_approval_id=token))
        for p in self.phones:
            p.tap_box.assert_not_awaited()

    def test_changed_individual_blocks_approval_even_with_same_cp_and_label(self):
        token = self.parked()
        offer = trade.trade_approval.approve(token)
        with self.assertRaises(trade.trade_approval.ApprovalRequired), trade.trade_approval.claim_for_reselect(token):
            offer["participants"]["b"]["caught"] = "7/29/2026"
            trade.trade_approval.consume(token, offer, reselected=True)
        for p in self.phones:
            p.tap_box.assert_not_awaited()

    def test_special_offer_has_to_be_approved_before_secondary_dialog(self):
        token = self.parked(special=True, price="80")
        trade.trade_approval.approve(token)
        self.lucky_screens()
        asked = []

        async def received(phone, _partner, special_ok, receipts, _offer):
            asked.append(special_ok)
            receipts[phone.name] = trade.TradeReceipt(phone.save.return_value, "Beldum", True)
            return "CP110"

        with trade.trade_approval.claim_for_reselect(token), mock.patch.object(trade, "finish", side_effect=received):
            asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                         require_guaranteed_lucky=True, guaranteed_details=self.details,
                                         propose_special=True, reselect_approval_id=token))
        self.assertEqual(asked, [True, True])

    def test_gold_board_without_text_is_reserved_even_with_normal_price(self):
        gold = FRAME.copy()
        samples = [(0.03, 0.30, (168, 127, 82)), (0.97, 0.30, (172, 125, 76)),
                   (0.03, 0.56, (203, 170, 98)), (0.97, 0.56, (148, 110, 80))]
        for x, y, rgb in samples:
            gold.putpixel((int(x * gold.width), int(y * gold.height)), rgb)
        self.assertTrue(trade.golden_trade_board(gold))
        self.assertFalse(trade.golden_trade_board(Image.new("RGB", FRAME.size, (106, 188, 244))))
        for p in self.phones:
            p.read = mock.AsyncMock(return_value=(gold, [box("CONFIRM", 30, 590), box("80", 275, 590)]))
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock), self.assertRaises(trade.LuckyAvailable):
            self.exchange()
        for p in self.phones:
            p.tap_box.assert_not_awaited()

    def test_unreadable_selector_is_retried_before_proposal(self):
        self.lucky_screens()
        phone = self.phones[0]
        profile, selected, offer, final = list(phone.read.side_effect)
        transient = (FRAME, [b for b in selected[1] if not b.text.startswith("HP")])
        phone.read.side_effect = [profile, transient, selected, offer, final]
        with mock.patch.object(trade, "cancel", new_callable=mock.AsyncMock), self.assertRaises(trade.trade_approval.ProposalReady):
            asyncio.run(trade.trade_once(*self.phones, self.names, self.species, self.species,
                                         require_guaranteed_lucky=True, guaranteed_details=self.details,
                                         propose_only=True))
        self.assertEqual(phone.read.await_count, 4)
        for p in self.phones:
            p.tap_box.assert_not_awaited()

    def test_gold_quote_survives_occluded_screen_edges(self):
        # The iPhone cost and both sprites cover four template-edge points.
        frame = Image.new("RGB", FRAME.size, (106, 188, 244))
        frame.paste((192, 144, 80), (75, 0, 675, frame.height))
        frame.putpixel((int(frame.width * 0.03), int(frame.height * 0.30)), (168, 127, 82))
        self.assertTrue(trade.golden_trade_board(frame))
        frame.paste((106, 188, 244), (0, int(frame.height * .40), frame.width, int(frame.height * .65)))
        self.assertFalse(trade.golden_trade_board(frame))
