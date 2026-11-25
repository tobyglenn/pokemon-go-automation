from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from tests import support as _support  # noqa: F401
from sources import collector_day as day, collect_trades as collect, trade_local


class FleetDayTests(unittest.TestCase):
    def test_local_remote_receipts_merge_with_reported_caps_only_for_their_date(self):
        from datetime import datetime, timedelta
        from sources import trade_history
        now = datetime(2026, 5, 4, 23, 59)
        machines = {"local": {}, "remote": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(day.starter_trade, "NEEDS_DIR", Path(tmp)), \
             patch.object(day.fleet_entrypoint, "local_machine", return_value="local"), \
             patch.object(day.fleet_entrypoint, "remote_shell_command", return_value=[]), \
             patch.object(day.subprocess, "run") as remote:
            trade_history.append_event({"id":"shared", "pair":["a","b"], "reason":"trade.py"}, now, Path(tmp))
            local = (Path(tmp) / f"history-{now.date()}.jsonl").read_text()
            trade_history.append_event({"pair":[], "capped_devices":["a"]}, now, Path(tmp))
            remote.return_value.stdout = local + json.dumps({"id":"remote", "pair":["b","c"]}) + "\n" + json.dumps(
                {"pair":[], "capped_pair":["c","b"]})
            counts = day.day_counts(["a","b","c"], machines, now.date())
            self.assertEqual(counts, {"a":100,"b":2,"c":1})
            self.assertEqual(counts.capped_pairs, {("b","c")})
            remote.return_value.stdout = ""
            self.assertEqual(day.day_counts(["a","b","c"], machines, (now + timedelta(days=1)).date()),
                             {"a":0,"b":0,"c":0})

    def test_transaction_id_deduplicates_distinct_serializations_but_rejects_account_conflict(self):
        from datetime import datetime
        from sources import trade_history
        now = datetime(2026, 5, 4, 10)
        machines = {"local": {}, "remote": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(day.starter_trade, "NEEDS_DIR", Path(tmp)), \
             patch.object(day.fleet_entrypoint, "local_machine", return_value="local"), \
             patch.object(day.fleet_entrypoint, "remote_shell_command", return_value=[]), \
             patch.object(day.trading_accounts, "aliases", return_value={"spare": "a"}), \
             patch.object(day.subprocess, "run") as remote:
            trade_history.append_event({"id": "one-exchange", "pair": ["a", "b"]}, now, Path(tmp))
            remote.return_value.stdout = json.dumps({"id": "one-exchange", "pair": ["b", "spare"],
                                                      "evidence": "peer copy"})
            self.assertEqual(day.day_counts(["a", "b", "spare"], machines, now.date()),
                             {"a": 1, "b": 1, "spare": 1})
            remote.return_value.stdout = json.dumps({"id": "one-exchange", "pair": ["a", "c"]})
            with self.assertRaisesRegex(day.pokemon_fleet.FleetError, "Conflicting account identities"):
                day.day_counts(["a", "b", "c"], machines, now.date())

    def test_demand_pairing_leaves_no_stranded_phone(self):
        from itertools import combinations
        remaining = {"r": 91, "a": 99, "b": 94, "c": 98}
        for edges in [[(n, "r") for n in ("a", "b", "c")], list(combinations(remaining, 2))]:
            while wave := day.batch_pairs(edges, remaining):
                phones = [n for p in wave for n in p]
                self.assertEqual(len(phones), len(set(phones)))
                for pair, count in wave.items():
                    self.assertGreater(count, 0)
                    for n in pair:
                        remaining[n] -= count
                        self.assertGreaterEqual(remaining[n], 0)
        self.assertEqual(sum(remaining.values()), 0)

    def test_disjoint_peer_batches_preserve_full_receiver_budget_and_inventory(self):
        gaps = {147, 148, 633, 634, 635}
        terms = [collect.TargetSpec("pseudos", "147-149,633-635", "!98Plus&!#&147-149,633-635")]
        retained, = day.without_pending_gaps(terms, gaps)
        self.assertEqual(retained.term, "!98Plus&!#&149")
        r = {"receiver": 91, "a": 99, "b": 94, "c": 98}
        primary = {("a", "receiver"): 10}
        all_peers = [("a", "b"), ("a", "c"), ("b", "c")]
        self.assertEqual(day.gap_surplus_pairs(primary, all_peers, r, "receiver"), {("b", "c"): 3})
        self.assertEqual(r, {"receiver":91, "a":99, "b":94, "c":98})
        self.assertEqual(day.gap_surplus_pairs(primary, all_peers[:2], r, "receiver"), {})
        cap = {**r, "c":91}
        self.assertFalse(day.gap_surplus_pairs(primary, all_peers, cap, "receiver"))
        self.assertEqual(day.gap_surplus_pairs({("a","receiver"):1}, all_peers, r, "receiver"), {("b","c"):1})
        # A nearly stranded account must retain partners even if the receiver has only
        # one slot left. Such a pair has no disposable fleet capacity.
        self.assertFalse(day.gap_surplus_pairs(primary, all_peers,
                                             {"receiver":1,"a":5,"b":2,"c":2},"receiver"))

    @patch("sources.trade_local.check_trade_lobby", new=lambda a, b: nullcontext())
    def test_parallel_gap_and_peer_execution_holds_no_phone_twice(self):
        names = ["receiver", "a", "b", "c"]
        fleet = SimpleNamespace(devices={n:SimpleNamespace(config={}) for n in names})
        args = SimpleNamespace(ignore_history=False, trades=5, return_query=None, plan=False, dry_run=False, shots=None)
        initial = dict(zip(names, [3,0,1,0]))
        totals = dict(initial)
        locks = set()
        trades_in_progress = set()
        overlaps = []
        phases = []

        @asynccontextmanager
        async def opened(name, *unused, **kwargs):
            self.assertNotIn(name, locks)
            locks.add(name)
            try:
                yield SimpleNamespace(name=name)
            finally:
                locks.remove(name)

        async def prepare(pool, phone):
            first = min(day.clear_pokemon.parse_dex_ranges(pool.current.query))
            prefix = pool.current.term.rsplit("&", 1)[0]
            pool.targets[pool.index] = collect.TargetSpec(pool.current.name, str(first), f"{prefix}&{first}")

        async def pick(phone, term, label, *, picked_species, keep_copies, **kwargs):
            self.assertEqual(keep_copies, 2)
            picked_species.append("nickname")
            return 20

        async def exchange(a,b,trainers,terms,species,*,choosers,on_confirmed, **_kwargs):
            pair = (a.name,b.name)
            trades_in_progress.add(pair)
            await asyncio.sleep(0)
            if len(trades_in_progress) > 1:
                overlaps.append(set(trades_in_progress))
            await asyncio.gather(choosers[a.name](a),choosers[b.name](b))
            receipts = {a.name: SimpleNamespace(lucky=None, label="Nickname")}
            on_confirmed(receipts)
            trades_in_progress.remove(pair)
            return receipts

        def record(pair, give, phase, reason, **proof):
            phases.append(phase)
            for n in pair:
                totals[n] += 1
                self.assertLessEqual(totals[n],5)
                if phase == "surplus-value":
                    self.assertNotEqual(n, "receiver")
                    self.assertNotEqual(proof["offered_dex"][n],633)
                    self.assertGreaterEqual(5 - totals[n], 5 - totals["receiver"])

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            gaps = day.LuckyGaps({633:"Deino"},Path(tmp)/"audit",set())
            cfg = {"return_dex_tiers":["147,633"]}
            with patch.object(day, "day_counts", side_effect=lambda roster, *_: {n:totals[n] for n in roster}), \
                    patch.object(day.LuckyGaps, "load", return_value=gaps), \
                    patch.object(day.LuckyGaps, "already_lucky", AsyncMock(return_value=False)), \
                    patch.object(day,"guaranteed_profiles",AsyncMock(return_value=False)), \
                    patch.object(collect,"_open_optional_donor",opened), patch.object(day.Offers,"prepare",prepare), \
                    patch.object(trade_local,"trainer_name",AsyncMock(return_value="trainer")), \
                    patch.object(collect,"choose_filtered_tile",pick),patch.object(trade_local,"trade_once",exchange), \
                    patch.object(day.starter_trade,"record_trade",record):
                rc = asyncio.run(day.run(args, cfg, fleet, {}, dict.fromkeys(names,"host"),
                                         names[0],names[1:],Path(tmp)/"clear.json"))
        self.assertEqual(rc,0)
        self.assertIn("surplus-value",phases)
        self.assertTrue(overlaps)
        for concurrent in overlaps:
            phones = [n for pair in concurrent for n in pair]
            self.assertEqual(len(set(phones)),len(phones))
        self.assertEqual(totals,dict.fromkeys(names,5))

    def test_gaps_and_valuable_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            report = directory / "audit.json"
            report.write_text(json.dumps({"needed_lucky_list": ["443 Gible", "633 Deino"]}))
            cfg = directory / "clear.json"
            cfg.write_text(json.dumps({"lucky_report": "audit.json"}))
            with patch.object(day.config_paths, "state_dir", return_value=directory):
                gaps = day.LuckyGaps.load(cfg, "receiver")
                self.assertEqual(gaps.targets([])[0].query, "443,633")
                phone = SimpleNamespace(name="receiver")
                with patch.object(day.clear_pokemon, "_apply_and_verify_query", AsyncMock(return_value=(0,None,None,0))):
                    self.assertFalse(asyncio.run(gaps.already_lucky(phone, 443)))
                with patch.object(day.clear_pokemon, "_apply_and_verify_query", AsyncMock(return_value=(1,None,None,0))) as scan:
                    self.assertTrue(asyncio.run(gaps.already_lucky(phone, 443)))
                    scan.assert_awaited_once_with(phone, "lucky&443")
                self.assertEqual(day.LuckyGaps.load(cfg, "receiver").targets([])[0].query, "633")
            value, _ = day.valuable_queries(cfg, {}, None)
            self.assertEqual(day.clear_pokemon.parse_dex_ranges(value[0].query), day.clear_pokemon.parse_dex_ranges(day.PSEUDO_DEX))
            self.assertTrue(all("!dontrade" in v.term and "!98Plus" in v.term and "!#" in v.term for v in value))

    def test_verified_history_replays_registration_after_interruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gaps = day.LuckyGaps({633:"Deino", 371:"Bagon"}, root / "registered-account-a.jsonl", set())
            donor, receiver = "alpha", "collector"
            receipt = trade_local.TradeReceipt(root / "capture.png", "Nickname", True)
            with patch.object(day.starter_trade, "NEEDS_DIR", root):
                day.starter_trade.record_trade([donor, receiver], {donor:"nickname",receiver:"other"},
                                               "guaranteed-gap", "collect", offered_dex={donor:633, receiver:147},
                                               receipts={receiver:receipt}, lucky_audit=gaps.path.stem)
                # Legacy history and another receiver's audit cannot retire a gap.
                day.starter_trade.record_trade([donor,receiver], {donor:"bagon"}, "lucky-gap", "collect")
                day.starter_trade.record_trade([donor,receiver], {}, "lucky-gap", "collect", offered_dex={donor:371},
                                               receipts={receiver:receipt}, lucky_audit="registered-account-b")
                ledger, = root.glob("history-*.jsonl")
                first, legacy, _ = map(json.loads, ledger.read_text().splitlines())
                self.assertEqual(first["offered_dex"], {donor:633, receiver:147})
                self.assertEqual(first["receipts"][receiver], {"screenshot":str(receipt.screenshot),"label":"Nickname","lucky":True})
                self.assertNotIn("receipts", legacy)
                self.assertFalse(gaps.path.exists())
                gaps.restore_receipts(receiver)
                self.assertEqual(gaps.registered, {633})
                self.assertEqual(day.starter_trade.trades_done_today(receiver), 3)

    def test_exact_dex_counts_reject_duplicate_nicknames_and_preserve_two(self):
        async def exercise():
            pool = day.Offers([collect.TargetSpec("pseudos", "147,371", "!#&147,371")], 2,
                              {147: "dratini", 371: "bagon"})
            phone = SimpleNamespace(name="demo", read=AsyncMock(return_value=(None, [])), still=AsyncMock())
            async def inspect(_p, term, _label, *, inspect_labels):
                # Every Pokemon has the same "Dratini" nickname. Both exact
                # species counts are below three despite a group of four.
                self.assertEqual(term, "!#&147,371")
                inspect_labels.extend(["Dratini"] * 4)
                return 4
            async def prepare(_p, term, *unused, observed, min_count):
                self.assertIn(term, {"!#&147", "!#&371"})
                self.assertEqual(min_count, 3)
                observed["count"] = 2
                return False
            with patch.object(day.starter, "open_storage", AsyncMock()), \
                    patch.object(collect, "choose_filtered_tile", inspect), patch.object(collect, "prepare_saved_query", prepare):
                with self.assertRaises(day.EmptyOffers):
                    await pool.prepare(phone)
            self.assertEqual(pool.held, {"147": 2, "371": 2})
        asyncio.run(exercise())

    @patch("sources.trade_local.check_trade_lobby", new=lambda a, b: nullcontext())
    def test_ordinary_collector_holds_all_guaranteed_pairs_and_scarce_offers(self):
        r, d = "receiver", "donor"
        owners = {r: "host", d: "host"}
        args = SimpleNamespace(ignore_history=False, trades=1, return_query=None, plan=False, dry_run=False, shots=None)
        fleet = SimpleNamespace(devices={n: SimpleNamespace(config={}) for n in owners})

        async def scenario(tmp, guaranteed, stock, absolute_reserve=0):
            fleet.devices[d].config["trade_keep_last"] = absolute_reserve
            exchanges = []
            gaps = day.LuckyGaps({633:"Deino"}, Path(tmp) / "registered", set())
            @asynccontextmanager
            async def opened(name, *unused, **kwargs):
                yield SimpleNamespace(name=name, read=AsyncMock(return_value=(None, [])), still=AsyncMock())

            async def favorite(phone, term, *unused, observed, min_count, **kwargs):
                dex = int(term.rsplit("&", 1)[-1])
                observed["count"] = stock if phone.name == d and dex == 633 else 4 if phone.name == r and dex == 147 else 0
                return True

            async def chooser(phone, term, name, **kwargs):
                if "inspect_labels" in kwargs:
                    candidates = day.clear_pokemon.parse_dex_ranges(term.rsplit("&", 1)[-1])
                    if phone.name == r and 147 in candidates:
                        kwargs["inspect_labels"].extend(["Dratini"] * 4)
                        return 4
                    raise trade_local.TradeOver("group empty")
                dex = int(term.rsplit("&", 1)[-1])
                if phone.name == d:
                    self.assertEqual(dex, 633)
                    self.assertGreater(stock, kwargs["keep_copies"])
                    kwargs["picked_species"].append("nickname")
                    return stock
                self.assertEqual(dex, 147)
                self.assertEqual(kwargs["keep_copies"], 2)
                kwargs["picked_species"].append("dratini")
                return 4

            async def exchange(a, b, names, terms, species, *, choosers, on_confirmed, require_guaranteed_lucky=False, **_kwargs):
                self.assertEqual(require_guaranteed_lucky, guaranteed)
                await asyncio.gather(choosers[a.name](a), choosers[b.name](b))
                exchanges.append(terms)
                receipts = {r: SimpleNamespace(lucky=guaranteed, label="Nickname")}
                on_confirmed(receipts)
                return receipts

            with patch.object(day, "day_counts", return_value={n: 0 for n in owners}), \
                    patch.object(day.LuckyGaps, "load", return_value=gaps), \
                    patch.object(day, "species_catalog", return_value={147:"dratini",633:"deino"}), \
                    patch.object(day, "guaranteed_profiles", AsyncMock(return_value=guaranteed)), \
                    patch.object(day.clear_pokemon, "_apply_and_verify_query", AsyncMock(return_value=(0,None,None,0))), \
                    patch.object(collect, "_open_optional_donor", opened), \
                    patch.object(trade_local, "trainer_name", AsyncMock(return_value="Trainer")), \
                    patch.object(day.starter, "open_storage", AsyncMock()), patch.object(collect, "prepare_saved_query", favorite), \
                    patch.object(collect, "choose_filtered_tile", chooser), patch.object(trade_local, "trade_once", exchange), \
                    patch.object(day.starter_trade, "record_trade"):
                code = await day.run(args, {"guaranteed_lucky_receiver":r}, fleet, {}, owners, r, [d], Path(tmp) / "clear.json")
            return code, len(exchanges), gaps.registered

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(scenario(tmp, True, 2)), (1, 0, set()))
            self.assertEqual(asyncio.run(scenario(tmp, False, 2)), (1, 0, set()))
            self.assertEqual(asyncio.run(scenario(tmp, True, 1, absolute_reserve=1)), (1, 0, set()))
            self.assertEqual(asyncio.run(scenario(tmp, True, 4)), (1, 0, set()))

    @patch("sources.trade_local.check_trade_lobby", new=lambda a, b: nullcontext())
    def test_only_receipted_trades_count_and_unavailable_pair_can_reroute(self):
        roster = ["receiver", "alpha", "bravo", "charlie"]
        owners = {n: "host" for n in roster}
        fleet = SimpleNamespace(devices={n: SimpleNamespace(config={"trade_keep_last": int(n == "receiver")}) for n in roster})
        args = SimpleNamespace(ignore_history=False, trades=2, return_query=None, plan=False, dry_run=False, shots=None)
        history = {n: 0 for n in roster}
        records = []
        attempted = []
        gap_used = set()

        @asynccontextmanager
        async def opened(name, *unused, **kwargs):
            yield SimpleNamespace(name=name)

        async def pick(phone, term, name, *, keep_copies=0, picked_species=None, inspect_labels=None, **kw):
            if inspect_labels is not None:
                inspect_labels.extend(["unknown"] * 12)
                return 12
            if keep_copies:
                self.assertTrue(term.rsplit("&", 1)[1].isdecimal())
            self.assertEqual(keep_copies, 2)
            if name.startswith("lucky-gap"):
                if phone.name != "alpha" or phone.name in gap_used:
                    raise trade_local.TradeOver("no eligible targets")
                picked_species.append("deino")
                return 3
            picked_species.append("gible")
            return 12

        async def exchange(a, b, names, terms, species, *, choosers, on_confirmed, **_kwargs):
            attempted.append((a.name, b.name))
            if {a.name, b.name} == {"alpha", "bravo"}:
                raise trade_local.LuckyAvailable("Reserved Lucky Friends trade")
            await asyncio.gather(choosers[a.name](a), choosers[b.name](b))
            if species[a.name].startswith("lucky-gap"):
                gap_used.add(a.name)
            receipts = {a.name: SimpleNamespace(lucky=None, label="gible")}
            on_confirmed(receipts)
            return receipts

        def record(pair, give, phase, reason, **evidence):
            self.assertEqual(set(evidence["offered_dex"]), set(pair))
            self.assertTrue(evidence["receipts"])
            records.append((pair, give, phase))

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            gaps = day.LuckyGaps({633: "Deino"}, Path(tmp) / "seen", set())
            with patch.object(day.LuckyGaps, "already_lucky", AsyncMock(return_value=False)), \
                 patch.object(day.Offers, "prepare", AsyncMock()), patch.object(day, "guaranteed_profiles", AsyncMock(return_value=False)), \
                 patch.object(day, "day_counts", return_value=history), patch.object(day.LuckyGaps, "load", return_value=gaps), \
                 patch.object(collect, "_open_optional_donor", opened), \
                 patch.object(trade_local, "trainer_name", AsyncMock(return_value="Trainer")), \
                 patch.object(collect, "choose_filtered_tile", pick), patch.object(trade_local, "trade_once", exchange), \
                 patch.object(day.starter_trade, "record_trade", record):
                code = asyncio.run(day.run(args, {}, fleet, {}, owners, roster[0], roster[1:], Path(tmp)))
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0][2], "lucky-gap")
        self.assertEqual(records[0][1]["alpha"], "deino")
        for n in roster:
            self.assertEqual(sum(n in r[0] for r in records), 2)
        self.assertGreater(len(attempted), len(records))


if __name__ == "__main__":
    unittest.main()
