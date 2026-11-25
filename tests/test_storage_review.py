import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests import support as _support
from sources import storage_review as review


def matches(term, dex, traits):
    """Evaluate these concrete test searches using GO's groups of OR terms."""
    def check(token):
        if token.startswith("!"):
            return not check(token[1:])
        buckets = re.fullmatch(r"(\d)-(\d?)(attack|defense|hp)", token)
        if buckets:
            return any(f"{v}{buckets[3]}" in traits for v in range(int(buckets[1]), int(buckets[2] or 4) + 1))
        if token[:1].isnumeric() and not token.endswith("*") and not any(c.isalpha() for c in token):
            bounds = token.split("-")
            return int(bounds[0]) <= dex <= int(bounds[-1])
        return token in traits
    return all(any(check(token) for token in group.split(",")) for group in term.split("&"))


class ManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.policy = review.ReviewPolicy(
            "receiver", {d: str(d) for d in range(1, 10)}, set(range(1, 9)), set(range(1, 8)),
            {2, 4}, {3, 4}, {5}, ("!#", "!favorite", "!4*", "!lucky", "!dynamax", "!gigantamax", "!costume"),
            ("!favorite", "!traded", "!4*"),
        )
        self.anchors = set(range(2, 10))

    def test_receiver_regional_spares_only_in_independently_proven_forms(self):
        forms = review.ORDINARY_FORM
        scopes = (forms + "&!alola&!galar", forms + "&alola", forms + "&galar")
        self.policy.living_forms = {1:scopes, 2:scopes, 3:(), 4:(forms,)}
        evidence = {}
        for dex,scope in ((1,scopes[0]),(1,scopes[1]),(2,scopes[2]),(4,forms)):
            evidence[dex,scope] = {"status":"kept", "count":1, "query":f"{dex}&{scope}&{review.KEPT}&{dex}"}
        with patch.object(review,"living_evidence",return_value=evidence):
            outgoing = review.receiver_outgoing_filter(self.policy)
            strict = self.policy.favorites("receiver")[review.STRICT]
        for query, extras in ((outgoing,set()), (strict,{"2*"})):
            self.assertTrue(matches(query,1,extras))
            self.assertTrue(matches(query,1,extras|{"alola"}))
            self.assertFalse(matches(query,1,extras|{"galar"}))
            self.assertFalse(matches(query,2,extras))
            self.assertFalse(matches(query,2,extras|{"alola"}))
            self.assertTrue(matches(query,2,extras|{"galar"}))
            self.assertFalse(matches(query,3,extras))
            self.assertTrue(matches(query,4,extras))
            self.assertFalse(matches(query,4,extras|{"costume"}))

    def test_tradeable_gaps_are_held_and_spent_gaps_are_reviewable(self):
        pub = self.policy.favorites("donor", self.anchors)
        strict = pub[review.STRICT]
        for dex in (2, 3, 4):
            self.assertFalse(matches(strict, dex, {"2*"}))
            self.assertTrue(matches(strict, dex, {"2*", "traded"}))
        self.assertTrue(matches(strict, 6, {"1*"}))
        for traits in [{"3*"}, {"2*", "favorite"}, {"1*", "#"}, {"0*", "dynamax"}]:
            self.assertFalse(matches(strict, 6, traits))
        for dex in (8, 9):
            self.assertFalse(matches(strict, dex, {"2*", "traded"}))
        self.assertTrue(matches(strict, 1, {"2*", "traded"}))

    def test_manual_trade_routes_use_only_direct_gaps_and_keep_perfects(self):
        pub = self.policy.favorites("donor", self.anchors)
        self.assertNotIn(review.HUNDO_REVIEW,pub)
        self.assertTrue(matches(pub[review.LUCKY_TRADES], 2, {"2*"}))
        self.assertTrue(matches(pub[review.HUNDO_TRADES], 3, {"2*"}))
        self.assertFalse(matches(pub[review.HUNDO_TRADES], 1, {"2*"}))
        self.assertFalse(matches(pub[review.HUNDO_TRADES], 3, {"4*"}))
        self.assertTrue(matches(pub[review.PRIORITY_TRADES], 5, {"2*"}))
        self.assertFalse(matches(pub[review.PRIORITY_TRADES], 5, {"2*", "traded"}))

    def test_garbage_trash_takes_any_iv_below_98_but_only_garbage_species(self):
        # Garbage 1-8 (Lucky gaps 2 and 4, priority 5); 9 is a useful species.
        self.policy.trash_species = set(range(1, 9))
        self.policy.registered_lucky = frozenset({3, 5, 6, 9})
        strict = self.policy.favorites("donor")[review.STRICT]
        iv = {"2attack", "3defense", "1hp"}   # an ordinary spread
        near = {"distance-100"} | iv
        # IVs reroll on trade: a 3-star garbage Pokemon goes like a 0-star one.
        for stars in ("0*", "2*", "3*"):
            self.assertTrue(matches(strict, 3, near | {stars}))
        # Never a useful species, at any IV or trade state.
        self.assertFalse(matches(strict, 9, near | {"1*"}))
        self.assertFalse(matches(strict, 9, {"1*", "traded"} | iv))
        # 98s and possible 98s stay: native 4*, the tag, two perfect stats.
        self.assertFalse(matches(strict, 3, near | {"4*"}))
        self.assertFalse(matches(strict, 3, {"distance-100", "3*", "4attack", "4defense", "1hp"}))
        # A Lucky Dex gap or priority species waits for the receiver.
        self.assertFalse(matches(strict, 2, near | {"2*"}))
        self.assertFalse(matches(strict, 5, near | {"2*"}))
        self.assertTrue(matches(strict, 2, {"2*", "traded"} | iv))
        # A far catch is FarTrade candy until it has been traded.
        self.assertFalse(matches(strict, 6, {"2*"} | iv))
        self.assertTrue(matches(strict, 6, {"2*", "traded"} | iv))
        self.assertFalse(matches(strict, 7, near | {"2*"}))
        self.assertTrue(matches(strict, 7, {"2*", "traded"} | iv))

    def test_missing_lucky_evidence_keeps_untraded_garbage(self):
        self.policy.trash_species = {6}
        strict = self.policy.favorites("donor")[review.STRICT]
        iv = {"2attack", "3defense", "1hp"}
        self.assertFalse(matches(strict, 6, {"2*", "distance-100"} | iv))
        self.assertTrue(matches(strict, 6, {"2*", "traded"} | iv))

    def test_receiver_garbage_trash_needs_a_kept_specimen_and_spares_return_pools(self):
        self.policy.trash_species = set(range(1, 10))
        self.policy.receiver_trash_species = set(range(1, 9))
        strict = self.policy.favorites("receiver", self.anchors)[review.STRICT]
        iv = {"2attack", "3defense", "1hp"}
        self.assertTrue(matches(strict, 6, {"3*"} | iv))
        self.assertTrue(matches(strict, 8, {"1*", "traded"} | iv))
        self.assertFalse(matches(strict, 9, {"1*"} | iv))        # return pool
        self.assertFalse(matches(strict, 1, {"1*"} | iv))        # no retained copy
        self.assertFalse(matches(strict, 6, {"3*", "4attack", "2defense", "4hp"}))

    def test_unclassified_dex_never_enters_untraded_safe_trash(self):
        self.policy.registered_both = {6}
        query = self.policy.favorites("donor")[review.STRICT]
        self.assertFalse(matches(query,1,{"1*"}))
        self.assertTrue(matches(query,1,{"1*","traded"}))
        self.assertTrue(matches(query,6,{"1*"}))
        self.assertFalse(matches(query,6,{"4*","traded"}))

    def test_even_custom_and_empty_role_filters_explicitly_protect_all_four_stars(self):
        self.policy.filters = ()
        self.policy.donor_filters = ()
        self.policy.priority.clear()
        for device in ("receiver","donor"):
            for query in self.policy.favorites(device,self.anchors).values():
                self.assertIn("!4*",query.split("&"))

    def test_donor_raid_review_spans_singletons_but_protects_high_iv(self):
        pub = self.policy.favorites("donor", self.anchors)
        for stats in ({"4attack","3defense","4hp"}, {"4attack","4defense","3hp"}, {"3attack","4hp","4defense"}):
            self.assertFalse(matches(pub[review.RAID_REVIEW], 8, {"3*"} | stats))
        self.assertTrue(matches(pub[review.RAID_REVIEW], 8, {"3*","4attack","3defense","3hp"}))
        empty = self.policy.favorites("donor", ())
        self.assertEqual(empty[review.STRICT], pub[review.STRICT])
        ordinary = {"2*", "2attack", "2defense", "2hp"}
        self.assertTrue(matches(empty[review.RAID_REVIEW], 2, ordinary))
        self.assertFalse(matches(empty[review.RAID_REVIEW], 5, ordinary))
        self.assertEqual(self.policy.favorites("receiver", ())[review.RAID_REVIEW], review.EMPTY)

    def test_receiver_offers_ordinary_surplus_outside_requested_trade_pools(self):
        pub = self.policy.favorites("receiver", self.anchors)
        self.assertEqual(set(pub), {review.STRICT,review.RAID_REVIEW})
        self.assertTrue(matches(pub[review.STRICT], 6, {"2*"}))
        self.assertTrue(matches(pub[review.STRICT], 6, {"2*", "traded"}))
        self.assertFalse(matches(pub[review.STRICT], 1, {"2*", "traded"}))

    def test_receiver_excludes_return_pool_species_even_when_peers_offline(self):
        self.policy.donors = ("a","b","c")
        self.policy.safe_species.remove(2)
        with patch.object(review, "load_verified_anchors", side_effect=AssertionError("unexpected peer read")):
            pub = self.policy.favorites("receiver", self.anchors)
        self.assertTrue(matches(pub[review.STRICT], 6, {"2*"}))
        self.assertFalse(matches(pub[review.STRICT], 2, {"2*"}))
        self.assertTrue(matches(pub[review.RAID_REVIEW], 2, {"2*","2attack","2defense","2hp"}))

    def test_donor_costume_fodder_still_respects_raid_and_lucky_goals(self):
        traits = {"2*", "costume","2attack","2defense","2hp"}
        donor = self.policy.favorites("donor")
        self.assertTrue(matches(donor[review.STRICT], 6, traits))
        self.assertFalse(matches(donor[review.STRICT], 2, traits))
        self.assertFalse(matches(donor[review.RAID_REVIEW], 9, traits))
        self.assertFalse(matches(self.policy.favorites("receiver", self.anchors)[review.STRICT], 6, traits))

    def test_candy_protection_includes_the_whole_evolution_family(self):
        graph = {440: [113], 113: [242], 265: [266,268], 266: [267], 268: [269], 234: [899]}
        self.assertEqual(review._families({113}, graph), {440,113,242})
        self.assertEqual(review._families({267}, graph), {265,266,267,268,269})

    def test_nundo_guard_keeps_mixed_zero_spreads_reviewable(self):
        self.assertFalse(matches(review.NUNDO_GUARD, 2, {"0attack", "0defense", "0hp"}))
        self.assertTrue(matches(review.NUNDO_GUARD, 2, {"0attack", "0defense", "1hp"}))

    def test_cached_anchor_proofs_only_apply_to_one_phone(self):
        with TemporaryDirectory() as tmp, patch.object(review.config_paths,"state_dir",return_value=Path(tmp)):
            proof = {str(d): {"count": n, "query": f"{d}&{review.ANCHOR}"} for d, n in [(1,0), (2,2)]}
            review._checkpoint("left", proof, predicate=review.ANCHOR)
            self.assertEqual(review.load_verified_anchors("left"), {2})
            self.assertEqual(review.load_verified_anchors("right"), set())
            proof["2"]["query"] = "2&favorite,lucky,4*,#"
            with self.assertRaises(review.pokemon_fleet.FleetError):
                review._checkpoint("left", proof, predicate=review.ANCHOR)

    def test_native_verified_anchor_accepts_ocr_punctuation_only_for_correct_dex(self):
        def box(text, y):
            return review.clear.gbl_vision.OCRBox(text, 1.0, 0, y, 100, 20)
        boxes = [box("Q (3)", 170), box(", 506&favorite‚lucky‚4*‚#", 330)]
        self.assertEqual(review._anchor_grid_count(boxes, 2400, 506), 3)
        self.assertIsNone(review._anchor_grid_count(boxes, 2400, 6))
        self.assertIsNone(review._anchor_grid_count(boxes, 2400, 507))

    def test_current_storage_audit_requires_matching_account_counts_and_species_receipt(self):
        with TemporaryDirectory() as tmp, patch.object(review.config_paths, "state_dir", return_value=Path(tmp)):
            audit = Path(tmp) / "perfect-audit" / "2026-11-21"
            audit.mkdir(parents=True)
            (audit / "summary.json").write_text(json.dumps({
                "receiver":"sample-receiver", "date":audit.name, "storage_perfect_species":2,
                "storage_perfect_individuals":3}))
            path = audit / "storage-counts.json"
            path.write_text(json.dumps({"total":3, "4":1, "6":2, "16":0}))
            (audit / "stock-004&4star.jpg").touch()
            (audit / "stock-016&4star.jpg").touch()
            self.assertEqual(review.load_verified_anchors("sample-receiver"), {4})
            self.assertEqual(review.load_verified_anchors("sample-android-b"), set())
            review._checkpoint("sample-receiver", {"4":{"count":0, "query": f"4&{review.ANCHOR}",
                                            "day":"2026-11-22"}}, predicate=review.ANCHOR)
            self.assertEqual(review.load_verified_anchors("sample-receiver"), set())
            path.write_text(json.dumps({"total":3, "6":2}))
            self.assertEqual(review._held_perfect("sample-receiver"), ("", set()))

    def test_reuse_tagged_singleton_needs_same_device_time_count_and_exact_scan_interval(self):
        with TemporaryDirectory() as tmp, patch.object(review.config_paths, "state_dir", return_value=Path(tmp)):
            directory = Path(tmp) / "living-dex"
            directory.mkdir()
            (directory / "sample-receiver-scan.json").write_text(json.dumps({
                "updated_at": "2026-11-20T13:39:14", "blocks": {"1-16":[[2,2], [4,5], [8,8]]}}))
            log = directory / "sample-receiver-tagged.jsonl"
            entry = {"device":"sample-receiver", "tag":"dontrade", "remaining_after":0, "expected":2,
                     "plain":"2,4-5", "query_chars":5, "at":"2026-11-20T15:26:06"}
            for invalid in ({"device":"other"}, {"remaining_after":1}, {"expected":3},
                            {"at":"2026-11-20T13:38:00"}, {"plain":"2,4,5", "expected":3}):
                log.write_text(json.dumps({**entry, **invalid}) + "\n")
                self.assertEqual(review.load_verified_anchors("sample-receiver"), set())
            log.write_text(json.dumps(entry) + "\n")
            self.assertEqual(review.load_verified_anchors("sample-receiver"), {2})
            self.assertEqual(review.load_verified_anchors("other"), set())
            trade = Path(tmp) / "trade"
            trade.mkdir()
            (trade / "history-2026-11-20.jsonl").write_text(json.dumps({
                "at":"2026-11-20T14:00:00", "pair":["sample-receiver","other"]}))
            self.assertEqual(review.load_verified_anchors("sample-receiver"), set())
