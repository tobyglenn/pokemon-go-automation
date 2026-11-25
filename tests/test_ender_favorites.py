import asyncio
import unittest
from contextlib import ExitStack, asynccontextmanager, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sources import collect_trades as collect, collector_day as day, collector_night as night
from sources import collector_proposals as proposals, ender_inventory as inv, lucky_raid_offers, starter
from sources import storage_review, trade_saved_search


def favorite(term, dex, cps):
    return {"term": term, "pages": [{str(dex): {"species": "pikachu", "cps": cps}}]}


class EnderFavoritesTests(unittest.TestCase):
    def test_broad_search_does_not_prove_species_stock_or_protection(self):
        targets = [collect.TargetSpec("gap", "25", "!favorite&!dontrade&25")]
        favorites = {"ReceiverLucky": favorite("!favorite&!dontrade&1-50", 25, [50, 63]),
                     "FarTrade": favorite("!distance-100", 25, [12, 13, 14, 15])}
        self.assertEqual(inv.candidates(favorites, targets, 2), [])
        favorites["ReceiverPerfect"] = favorite("!dontrade&!favorite&1-50", 25, [11, 20, 30])
        self.assertEqual([c.name for c in inv.candidates(favorites, targets, 2)], ["ReceiverPerfect"])
        self.assertFalse(inv.account_favorites({"handsets": {"alt": {"account":"other"}}}, "receiver", "alt"))

    def test_lucky_offer_requires_exact_union_individual_and_current_receipt(self):
        from sources import raid_offer_scan
        offer = {"dex": 383, "cp": 2250, "form": "groudon", "observed_at": "first",
                 "query": raid_offer_scan.EXCLUSIONS + "&cp2250&383", "picker_receipt": "current"}
        row = favorite(inv.lucky_offer_term(), 383, [2250])
        row.update(verified_at="current", stock_count=4, individuals=[offer.copy()])
        fleet = SimpleNamespace(devices={"donor": SimpleNamespace(config={})})
        snapshot = {"handsets": {"donor": {"account": "donor", "favorites": {"LuckyOffer": row}}}}
        def choice():
            return proposals.favorite_choice(snapshot, fleet, "donor", offer)
        self.assertEqual(choice().name, "LuckyOffer")
        self.assertIn("!4*", row["term"].split("&"))
        for field, value in (("term", row["term"].replace("&!4*", "")),
                             ("verified_at", "later"), ("individuals", [])):
            with self.subTest(field=field), patch.dict(row, {field: value}):
                self.assertIsNone(choice())
        proof = {"count":3, "page":1, "dex":383, "cp":2250, "term":row["term"], "cps":[2250]}
        with patch.dict(offer, {"picker_favorite":"LuckyOffer", "picker_proof":proof}):
            self.assertEqual((choice().stock_count, choice().page), (3, 1))
            with patch.dict(proof, {"cps":[2250,2250]}):
                self.assertIsNone(choice())
        with patch.dict(offer, {"picker_favorite":"LuckyOffer"}):
            self.assertIsNone(choice())
        with patch.dict(offer, {"form":"another"}):
            self.assertIsNone(choice())

    def test_changed_dex_manifest_retires_prepared_targets_without_discovery(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp, patch.object(inv.config_paths, "state_dir", return_value=Path(tmp)):
            regional = Path(tmp) / "dex-registrations" / "receiver" / "2026-01-01"
            regional.mkdir(parents=True)
            manifest = regional / "lucky.json"
            manifest.write_text("{}")
            data = {"version": inv.SNAPSHOT_VERSION, "handsets": {"d": {"favorites": dict.fromkeys(("ReceiverLucky", "SafeTrash", "98%"))}},
                    "dex_targets": {"receiver": "receiver", "manifest_versions": inv._dex_manifest_versions("receiver")}}
            inv.save(inv.path(), data)
            self.assertIn("dex_targets", inv.load())
            manifest.write_text('{"total": 9}')
            stale = inv.load()
            self.assertNotIn("dex_targets", stale)
            self.assertEqual(list(stale["handsets"]["d"]["favorites"]), ["98%"])

    def test_legendary_setup_resolves_hidden_nickname_and_empty_subsets(self):
        catalog = {1:"one", 2:"two", 3:"three", 4:"four"}
        async def search(phone, term, *, require_exact):
            self.assertTrue(require_exact)
            numbers = inv.clear.parse_dex_ranges(term.rsplit("&", 1)[1])
            labels = ["nickname" if n == 3 else catalog[n] for n in sorted(numbers & {1, 3})]
            return len(labels), labels, [], 0
        with patch.object(inv.clear, "_apply_and_verify_query", search), \
             patch.object(inv.clear, "evolutionary_line_off", return_value=True), \
             patch.object(inv, "visible_tiles", side_effect=lambda grid, _: [(label, 500, None) for label in grid]):
            found = asyncio.run(inv.eligible_legendary_species(SimpleNamespace(name="donor"), catalog, catalog))
        self.assertEqual(found, {1, 3})

    def test_capped_run_exits_before_reading_any_inventory(self):
        args = collect.parse_args([])
        args.prepared_only = True
        with patch.object(day, "day_counts", return_value={"a":100,"b":100}), \
             patch.object(inv, "load", side_effect=AssertionError), \
             patch.object(day.LuckyGaps, "load", side_effect=AssertionError), redirect_stdout(StringIO()):
            status = asyncio.run(day.run(args, {}, {}, {}, {"a":"host","b":"host"}, "a", ["b"], Path("unused")))
        self.assertEqual(status, 0)

    def test_uncapped_prepared_evening_uses_both_named_pools_without_discovery(self):
        receiver, donor = "receiver", "donor"
        pair = [receiver, donor]
        cfg = {"receiver":receiver,"return_dex_tiers":["26"],"collect_all_dex":[25]}
        args = collect.parse_args(["--collect-all-only", "--trades", "1"])
        args.prepared_only = True
        given = collect.build_donor_query("25").rsplit("&",1)[0] + "&25,26"
        return_term = day.valuable_queries(Path("unused"),cfg,None)[1][0].term
        fleet = SimpleNamespace(devices={n:SimpleNamespace(config={}) for n in pair})
        snapshot = {"dex_targets": {"receiver":receiver, "lucky_journal":"registered-" + "0"*20 + ".jsonl",
                                     "perfect":[], "lucky":[]}, "handsets": {
            receiver:{"account":receiver,"favorites":{"TradeReturns":favorite(return_term,26,[1,2,3])}},
            donor:{"account":donor,"favorites":{"ReceiverLucky":favorite(given,25,[54])}}}}
        @asynccontextmanager
        async def phone(name, *args, **kwargs):
            yield SimpleNamespace(name=name)
        opened = []
        async def choose(phone, choice, *, keep):
            opened.append((phone.name,choice.name,keep))
            return len(choice.cps)
        async def exchange(a,b,names,terms,labels,*,choosers,on_confirmed,daily_cap=None):
            await asyncio.gather(choosers[a.name](a),choosers[b.name](b))
            receipts = {p.name:SimpleNamespace(lucky=False) for p in (a,b)}
            on_confirmed(receipts)
            return receipts
        with ExitStack() as stack:
            for obj, name in ((day.Offers,"prepare"), (day.Offers,"narrow"), (day.LuckyGaps,"already_lucky"),
                              (collect,"choose_filtered_tile"), (collect.clear_pokemon,"_apply_and_verify_query"),
                              (lucky_raid_offers,"refresh_state")):
                stack.enter_context(patch.object(obj,name,side_effect=AssertionError))
            stack.enter_context(patch.object(day,"day_counts",return_value=dict.fromkeys(pair,0)))
            stack.enter_context(patch.object(day.LuckyGaps,"load",side_effect=AssertionError))
            stack.enter_context(patch.object(storage_review,"_held_perfect",return_value=("",set())))
            stack.enter_context(patch.object(inv,"load",return_value=snapshot))
            stack.enter_context(patch.object(inv,"pick",choose))
            stack.enter_context(patch.object(day,"guaranteed_profiles",AsyncMock(return_value=False)))
            stack.enter_context(patch.object(collect,"_open_optional_donor",phone))
            stack.enter_context(patch.object(day.trade_local,"trainer_name",AsyncMock(return_value="trainer")))
            stack.enter_context(patch.object(day.trade_local,"trade_once",exchange))
            stack.enter_context(patch.object(day.starter_trade,"record_trade"))
            stack.enter_context(redirect_stdout(StringIO()))
            asyncio.run(day.run(args,cfg,fleet,{},dict.fromkeys(pair,"host"),receiver,[donor],Path("unused")))
        self.assertCountEqual(opened,[(receiver,"TradeReturns",2),(donor,"ReceiverLucky",0)])

    def test_missing_named_choice_never_enters_search_or_rebuilds_it(self):
        phone = SimpleNamespace(name="phone",read=AsyncMock(return_value=(object(),[])),tap=AsyncMock())
        with patch.object(trade_saved_search,"open_named_picker",AsyncMock(side_effect=starter.StepFailed("missing"))), \
             patch.object(collect, "_use_query",side_effect=AssertionError), \
             patch.object(trade_saved_search,"ensure_favorite",side_effect=AssertionError):
            with self.assertRaisesRegex(starter.StepFailed,"missing"):
                asyncio.run(inv.pick(phone,inv.Choice("ReceiverLucky","25","25","pikachu",(10,)),keep=0))
        phone.tap.assert_not_awaited()

    def test_missing_cached_legendary_pair_never_refreshes_or_opens_phones(self):
        args = collect.parse_args([])
        args.prepared_only = True
        pair = ("a","b")
        fleet = SimpleNamespace(devices={n:SimpleNamespace(enabled=True,config={}) for n in pair})
        with patch.object(inv,"load",return_value={}), patch.object(lucky_raid_offers,"load_state",return_value={}), \
             patch.object(lucky_raid_offers,"refresh_state",side_effect=AssertionError), \
             patch.object(proposals,"eligible_pairs",side_effect=AssertionError), redirect_stdout(StringIO()):
            status=asyncio.run(proposals.run(args,{},fleet,{},dict.fromkeys(pair,"h"),held_pair=pair))
        self.assertEqual(status,1)

    def test_published_raid_choice_survives_date_rollover_and_expires_on_used_stock(self):
        pair = ("a", "b")
        stamp = "2025-10-21T18:00:00+00:00"
        participants = {"a":{"dex":25}, "b":{"dex":26}}
        snapshot = {"raid_pairs":{proposals.pair_key(pair):{
            "seen_since":stamp, "participants":participants}}}
        after = "2025-10-22T02:00:00+00:00"
        unrelated = {"at":after, "pair":["a","c"], "offered_dex":{"a":50,"c":51}}
        for changes, expected in (([unrelated],[pair]),
                                  ([{"at":after,"pair":["a","c"],"offered_dex":{"a":25,"c":51}}],[]),
                                  ([{"at":after,"pair":["a","c"]}],[])):
            with self.subTest(changes=changes), patch.object(day,"history_records",return_value=changes):
                result = asyncio.run(proposals.unspent_preparations([pair],snapshot,{},date(2025,10,22)))
                self.assertEqual(result,expected)

    def test_prepared_guaranteed_pair_never_ranks_or_refreshes_in_ender(self):
        pair = ("a","b")
        args = collect.parse_args([])
        args.prepared_only = True
        fleet = SimpleNamespace(devices={n:SimpleNamespace(enabled=True,config={}) for n in pair})
        p = {"species":"Groudon","dex":383,"stock":2,"cp":2600}
        snapshot = {"raid_pairs":{proposals.pair_key(pair):{"participants":{n:dict(p) for n in pair}}}}
        with patch.object(inv,"load",return_value=snapshot), patch.object(lucky_raid_offers,"load_state",return_value={}), \
             patch.object(proposals,"cached_pair_present",return_value=True), \
             patch.object(proposals,"unspent_preparations",AsyncMock(return_value=[pair])), \
             patch.object(proposals,"eligible_pairs",AsyncMock(return_value=[pair])) as checked, \
             patch.object(proposals,"proposal_itinerary",side_effect=AssertionError), \
             patch.object(lucky_raid_offers,"refresh_state",side_effect=AssertionError), \
             patch.object(proposals,"review_pair",AsyncMock(return_value={"a":object(),"b":object()})) as quote, \
             redirect_stdout(StringIO()):
            status=asyncio.run(proposals.run(args,{},fleet,{},dict.fromkeys(pair,"h"),held_pair=pair))
        self.assertEqual(status,0)
        checked.assert_awaited_once()
        quote.assert_awaited_once()

    def test_nightly_review_reads_physical_receipt_without_rebuilding_filter(self):
        device, account = "alternate","receiver"
        payloads = []
        async def spawn(*args, **kwargs):
            import json
            async def collect_payload(data):
                payloads.append(json.loads(data))
            return SimpleNamespace(communicate=collect_payload,returncode=0)
        snapshot = {"handsets":{device:{"account":account,"favorites":{"SafeTrash":{"term":"!#&25"}}}}}
        with patch.object(inv,"load",return_value=snapshot), patch.object(storage_review,"load_policy",side_effect=AssertionError), \
             patch("sources.trading_accounts.aliases",return_value={device:account}), \
             patch.object(night.fleet_entrypoint,"local_machine",return_value="local"), \
             patch.object(night.asyncio,"create_subprocess_exec",spawn):
            status=asyncio.run(night.finish({account:"local"}, {},[account],Path("unused"),{"receiver":account},
                                            account,transports={account:device},prepared_only=True))
        self.assertEqual(status,0)
        self.assertTrue(payloads[0]["favorite_only"])
        self.assertEqual((payloads[0]["device"],payloads[0]["label"],payloads[0]["term"]),
                         (device,"SafeTrash","!#&25"))
