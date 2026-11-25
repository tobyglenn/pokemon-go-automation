"""Regression coverage for reciprocal Lucky Friends inventory recommendations."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import AsyncMock, patch
import asyncio

from tests import support as _support  # noqa: F401
from sources import lucky_raid_offers as offers, raid_offer_scan as scan


class RankingTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 17, tzinfo=timezone.utc)
        self.stamp = self.now.isoformat()
        self.game = patch.object(offers.raid_gamedata, "load").start()
        self.addCleanup(patch.stopall)
        self.game.return_value.pokemon = {
            "kyogre": {"dex": 382},
            "groudon": {"dex": 383},
            "palkia": {"dex": 484},
            "palkia_origin": {"dex": 484},
        }

    def stock(self, n):
        return {
            "owned": n,
            "hundo_count": 0,
            "observed_at": self.stamp,
            "best_high_iv_level": None,
        }

    def offer(self, dex, name):
        return {
            "dex": dex,
            "name": name.title(),
            "form": name,
            "cp": 2211,
            "live_verified": True,
            "unique_count": 1,
            "eligible_count": 2,
            "observed_at": self.stamp,
            "raid_role": {"relative": 0.9, "type": "Ground"},
        }

    def test_fresh_reciprocal_and_recipient_quality(self):
        left = {
            "offers": [self.offer(382, "kyogre"), self.offer(383, "groudon")],
            "coverage": {"382": self.stock(10), "383": self.stock(8)},
        }
        right = {
            "offers": [self.offer(383, "groudon")],
            "coverage": {
                "382": self.stock(2),
                "383": dict(self.stock(12), maxed_hundo=True, hundo_count=1),
            },
        }
        state = {"devices": {"left": left, "right": right}}
        ranked = offers.rank_swaps(state, "left", "right", now=self.now)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["give"]["left"]["form"], "kyogre")
        self.assertEqual(ranked[0]["coverage"]["right"]["owned"], 2)
        right["coverage"]["382"]["observed_at"] = (
            self.now - timedelta(days=15)
        ).isoformat()
        self.assertEqual(offers.rank_swaps(state, "left", "right", now=self.now), [])
        hints = offers.rank_swaps(state, "left", "right", now=self.now, for_refresh=True)
        self.assertEqual(hints[0]["give"]["left"]["dex"], 382)

    def test_form_specific_pair_requires_independent_form_evidence(self):
        offer = self.offer(484, "palkia_origin")
        a = {"coverage": {"484": self.stock(3)}}
        self.assertIsNone(offers._value(offer, a, a, self.now))
        offer.update(form_verified=True, form_token="origin")
        self.assertIsNone(offers._value(offer, a, a, self.now))
        a["coverage"]["484"]["forms"] = {"palkia_origin": self.stock(2)}
        self.assertIsNotNone(offers._value(offer, a, a, self.now))


class CacheTests(TestCase):
    def test_old_inventory_targets_known_species_before_a_wider_scan(self):
        state = {"devices": {n: {"offers": [{"dex": dex, "unique_count": 1, "live_verified": True}]}
                             for n, dex in [("a", 382), ("b", 383)]}}
        with TemporaryDirectory() as tmp, patch.object(offers.config_paths, "state_dir", return_value=Path(tmp)), \
             patch.object(offers, "load_state", return_value=state), \
             patch.object(offers, "rank_swaps", return_value=[]), \
             patch("sources.collector_proposals.proposal_itinerary", side_effect=[({}, []), ({("a", "b"): {}}, [])]), \
             patch.object(scan, "refresh", AsyncMock(return_value=state)) as refresh:
            asyncio.run(offers.refresh_state(["a", "b"], {}, {}, None, None, pairs=[("a","b")]))
        self.assertEqual(refresh.await_count, 1)
        self.assertEqual(refresh.await_args.kwargs["shortlist"], [382, 383])

    def test_only_exchanged_species_expire_and_stale_scan_cannot_restore_them(self):
        account = {"offers": [{"dex": 382}, {"dex": 383}], "coverage": {"382": {}, "383": {}},
                   "checked": {"382": "first", "383": "second"}}
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            scan.publish_accounts(folder, {"a": account, "b": account})
            scan.publish_accounts(folder, {}, invalidate=["a","b"], changed_dex={382})
            result = scan.publish_accounts(folder, {"a": account})
            for saved in result["devices"].values():
                self.assertEqual(saved["offers"], [{"dex": 383}])
                self.assertEqual(saved["coverage"], {"383": {}})
                self.assertEqual(saved["checked"], {"383": "second"})

    def test_exchange_expires_pair_and_rejects_inflight_old_checkpoint(self):
        with (
            TemporaryDirectory() as tmp,
            patch.object(offers.config_paths, "state_dir", return_value=Path(tmp)),
        ):
            directory = Path(tmp) / "collect-trades"
            old = {"coverage": {"382": {"owned": 5}}}
            scan.publish_accounts(directory, {"a": old, "b": old, "c": old})
            offers.invalidate_accounts(["a", "b"])
            checkpoint = json.loads(
                (directory / "raid-offer-accounts/a.json").read_text()
            )
            self.assertEqual(checkpoint["coverage"], {})
            state = scan.publish_accounts(directory, {"a": old, "b": old})
            self.assertEqual(state["devices"], {"c": old})
            checkpoint["coverage"] = {"382": {"owned": 4}}
            state = scan.publish_accounts(directory, {"a": checkpoint})
            self.assertEqual(state["devices"]["a"]["coverage"]["382"]["owned"], 4)


class MovePanelTests(TestCase):
    def test_teach_dragon_ascent_button_requires_damage_on_same_row(self):
        from PIL import Image
        from sources import raid_gamedata, vision_ocr

        image = Image.new("RGB", (720, 1600))
        box = vision_ocr.TextBox
        rows = [
            box("GYMS & RAIDS", 100, 800, 90, 20),
            box("Air Slash", 100, 860, 90, 20),
            box("12", 625, 860, 35, 20),
            box("Outrage", 100, 920, 100, 20),
            box("110", 625, 920, 37, 20),
            box("DRAGON ASCENT", 70, 1020, 145, 20),
            box("NEW ATTACK", 100, 1100, 130, 20),
        ]
        self.assertEqual(
            scan.learned_moves(raid_gamedata.load(), [(image, rows)]),
            ["Air Slash", "Outrage"],
        )
        rows.append(box("140", 625, 1020, 35, 20))
        self.assertIn(
            "DRAGON ASCENT", scan.learned_moves(raid_gamedata.load(), [(image, rows)])
        )
