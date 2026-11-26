import asyncio
import unittest
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import ender
from sources import collect_trades as collect, collector_day as day, collector_night as night, ender as evening


class EveningTests(unittest.TestCase):
    def test_plan_never_contacts_devices(self):
        with patch.object(ender.fleet_entrypoint, "run_all_machines", side_effect=AssertionError), \
                patch.object(collect, "run", side_effect=AssertionError), redirect_stdout(StringIO()):
            self.assertEqual(ender.main(["--plan"]), 0)

    def test_goplus_only_delegates_without_running_evening_preparation(self):
        with patch("starter.main", return_value=1) as reconnect, \
             patch.object(evening, "prepare_handsets", side_effect=AssertionError), \
             patch.object(evening, "trade_and_review", side_effect=AssertionError):
            self.assertEqual(ender.main(["--goplus-only", "--local", "--plan"]), 1)
        reconnect.assert_called_once_with(["--local", "--plan", "--only", "goplus"])

    def test_one_trade_coordinator_after_both_hosts_prepare(self):
        events = []
        def prepare(script, flags):
            events.append((script, flags))
            return 0
        async def trade(local):
            self.assertFalse(local)
            events.append("trade then manual review")
            return 0
        with patch.object(ender.fleet_entrypoint, "local_requested", return_value=False), \
                patch.object(evening, "prepare_handsets", return_value=["primary", "donor"]), \
                patch.object(ender.fleet_entrypoint, "run_all_machines", prepare), \
                patch.object(collect, "run", side_effect=AssertionError), \
                patch.object(ender.fleet_entrypoint, "apply_local_environment"), \
                patch.object(evening, "trade_and_review", trade):
            self.assertEqual(ender.main([]), 0)
        self.assertEqual(events, [("ender.py", ["--prepare-worker", "--prepare-handsets", "primary", "donor"]),
                                  "trade then manual review"])

    def test_trading_uses_live_favorites_then_opens_pinned_review(self):
        from sources import priority_trade
        events = []

        async def trade(args):
            self.assertTrue(args.local)
            self.assertFalse(args.plan)
            events.append("priority trades")
            return 0

        async def review(owners, machines, roster, clear_path, cfg, receiver, **kwargs):
            self.assertTrue(kwargs["pinned"])
            self.assertFalse(kwargs.get("prepared_only", False))
            self.assertEqual(roster, ["receiver", "donor"])
            events.append("pinned review")
            return 0

        roster = (({"receiver": "receiver"}, SimpleNamespace(devices={}, transports={}), {},
                   {"receiver": "local", "donor": "local"}, Path("clear")))
        with patch.object(priority_trade, "run", trade), patch.object(evening, "_review_roster", return_value=roster), \
                patch.object(night, "finish", review):
            self.assertEqual(asyncio.run(evening.trade_and_review(True)), 0)
        self.assertEqual(events, ["priority trades", "pinned review"])

    def test_a_failed_trading_run_still_reviews_and_sleeps_displays(self):
        from sources import priority_trade
        roster = (({"receiver": "receiver"}, SimpleNamespace(devices={}, transports={}), {},
                   {"receiver": "local"}, Path("clear")))
        review = AsyncMock(return_value=0)
        with patch.object(priority_trade, "run", AsyncMock(side_effect=RuntimeError("lost phone"))), \
                patch.object(evening, "_review_roster", return_value=roster), patch.object(night, "finish", review), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(asyncio.run(evening.trade_and_review()), 1)
        review.assert_awaited_once()
        self.assertIn("trading incomplete", out.getvalue())

    def test_only_missing_battle_hearts_are_earned(self):
        with patch.object(evening, "heart_counts", AsyncMock(side_effect=[(3,1),(3,2),(3,3)])), \
             patch.object(evening.starter, "battle_step", new_callable=AsyncMock) as battle:
            self.assertTrue(asyncio.run(evening.finish_battles(object())).ok)
        self.assertEqual(battle.await_count, 2)

    @patch("sources.trade_local.check_trade_lobby", new=lambda a, b: nullcontext())
    def test_priority_sends_last_copy_only_to_receiver_even_if_already_lucky(self):
        pair = ["receiver", "donor"]
        args = collect.parse_args(["--collect-all-only"])
        cfg = {"collect_all_dex":[631], "return_dex_tiers":["147,631"]}
        fleet = SimpleNamespace(devices={n:SimpleNamespace(config={"trade_keep_last":1}) for n in pair})
        gaps = day.LuckyGaps({631:"Heatmor"},Path("unused"),{631})
        @asynccontextmanager
        async def opened(n, *args, **kwargs):
            yield SimpleNamespace(name=n)
        calls = []
        async def pick(phone, term, label, *, keep_copies, picked_species, **kwargs):
            dex = term.rsplit("&",1)[-1]
            calls.append((phone.name, dex, keep_copies))
            picked_species.append(dex)
            return 1 if phone.name == "donor" else 4
        async def exchange(a,b,trainers,terms,species,*,choosers,on_confirmed, **_kwargs):
            await asyncio.gather(choosers[a.name](a), choosers[b.name](b))
            receipts = {a.name:SimpleNamespace(lucky=True)}
            on_confirmed(receipts)
            return receipts
        with patch.object(day.LuckyGaps,"load",return_value=gaps), \
             patch.object(day,"day_counts",return_value=dict.fromkeys(pair,0)), \
             patch.object(day,"guaranteed_profiles",AsyncMock(return_value=False)), \
             patch.object(collect,"_open_optional_donor",opened), \
             patch.object(day.trade_local,"trainer_name",AsyncMock(return_value="trainer")), \
             patch.object(day.Offers,"prepare",AsyncMock()), patch.object(collect,"choose_filtered_tile",pick), \
             patch.object(day.trade_local,"trade_once",exchange), patch.object(day.starter_trade,"record_trade"), \
             redirect_stdout(StringIO()):
            result = asyncio.run(day.run(args,cfg,fleet,{},dict.fromkeys(pair,"host"),pair[0],pair[1:],Path("unused")))
        self.assertEqual(result,0)
        self.assertCountEqual(calls,[("donor","631",0),("receiver","147",2)])

    @patch("sources.trade_local.check_trade_lobby", new=lambda a, b: nullcontext())
    def test_perfect_pool_rechecks_after_a_reroll_and_only_sends_to_receiver(self):
        pair = ["receiver", "donor"]
        args = collect.parse_args(["--ignore-history", "--trades", "2"])
        cfg = {"receiver": "receiver", "perfect_trade_dex": [25], "return_dex_tiers": ["147"]}
        fleet = SimpleNamespace(devices={n: SimpleNamespace(config={"trade_keep_last": 3}) for n in pair})
        gaps = day.LuckyGaps({}, Path("unused"), set())
        offers = []

        @asynccontextmanager
        async def opened(name, *args, **kwargs):
            yield SimpleNamespace(name=name)

        async def pick(phone, term, label, *, keep_copies, **kwargs):
            self.assertEqual(keep_copies, 3)
            self.assertIn("!4*", term.split("&"))
            return 10

        async def exchange(a, b, trainers, terms, species, *, choosers, on_confirmed, **_kwargs):
            await asyncio.gather(choosers[a.name](a), choosers[b.name](b))
            offers.append({n: term.rsplit("&", 1)[-1] for n, term in terms.items()})
            receipts = {a.name: SimpleNamespace(lucky=False)}
            on_confirmed(receipts)
            return receipts

        checks = AsyncMock(side_effect=[(0, None, [], 0), (1, None, [], 0)])
        with patch.object(day.LuckyGaps, "load", return_value=gaps), \
             patch.object(day, "guaranteed_profiles", AsyncMock(return_value=False)), \
             patch.object(collect, "_open_optional_donor", opened), \
             patch.object(day.trade_local, "trainer_name", AsyncMock(return_value="trainer")), \
             patch.object(day.Offers, "prepare", AsyncMock()), patch.object(collect, "choose_filtered_tile", pick), \
             patch.object(day.clear_pokemon, "_apply_and_verify_query", checks), \
             patch.object(day.trade_local, "trade_once", exchange), patch.object(day.starter_trade, "record_trade"), \
             redirect_stdout(StringIO()):
            rc = asyncio.run(day.run(args, cfg, fleet, {}, dict.fromkeys(pair, "host"),
                                     pair[0], pair[1:], Path("unused")))
        self.assertEqual(rc, 0)
        self.assertEqual(offers, [{"donor": "25", "receiver": "147"}, {"donor": "147", "receiver": "147"}])
        self.assertEqual([(a.args[0].name, a.args[1]) for a in checks.await_args_list], [("receiver", "25&4*")] * 2)

    def test_night_dispatches_the_same_account_specific_safe_trash_as_favorites(self):
        import json
        from sources import storage_review

        roster = ["receiver", "donor"]
        terms = {"receiver": "!favorite&traded&10", "donor": "!favorite&!lucky&13"}
        policy = SimpleNamespace(favorites=lambda device: {storage_review.STRICT: terms[device]})
        sent = {}

        async def spawn(*cmd, **kwargs):
            async def communicate(raw):
                sent[cmd[-1]] = json.loads(raw)
            return SimpleNamespace(communicate=communicate, returncode=0)

        with patch.object(storage_review, "load_policy", return_value=policy), \
             patch.object(night.fleet_entrypoint, "local_machine", return_value="local"), \
             patch.object(night.asyncio, "create_subprocess_exec", spawn):
            result = asyncio.run(night.finish(dict.fromkeys(roster, "local"), {}, roster,
                                              Path("clear"), {"receiver": "receiver"}, "receiver"))
        self.assertEqual(result, 0)
        for device in roster:
            self.assertEqual(sent[device]["device"], device)
            self.assertEqual(sent[device]["label"], storage_review.STRICT)
            self.assertEqual(sent[device]["term"], terms[device])
            self.assertFalse(sent[device]["favorite_only"])

        from sources import storage_favorites
        synced = {n: terms[n] + "&!#ToPower" for n in roster}
        with patch.object(storage_review, "load_policy", return_value=policy), \
             patch.object(storage_favorites, "wanted", side_effect=lambda n, _p: {storage_review.STRICT: synced[n]}), \
             patch.object(night.fleet_entrypoint, "local_machine", return_value="local"), \
             patch.object(night.asyncio, "create_subprocess_exec", spawn):
            asyncio.run(night.finish(dict.fromkeys(roster, "local"), {}, roster,
                                     Path("clear"), {"receiver": "receiver"}, "receiver", pinned=True))
        for device in roster:
            # The pinned favorite is checked against the synced term, guards included.
            self.assertTrue(sent[device]["favorite_only"])
            self.assertEqual(sent[device]["term"], synced[device])

    def test_review_query_requires_open_mode_before_connecting_to_a_phone(self):
        args = night.clear_pokemon.parse_args(["--all", "--confirm-delete"])
        with patch.object(night.pokemon_fleet, "acquire_device_locks") as lock:
            with self.assertRaises(night.clear_pokemon.ClearSafetyError):
                asyncio.run(night.clear_pokemon.run_device(SimpleNamespace(name="phone"), "!favorite&10", (), args))
        lock.assert_not_called()

    def test_final_review_never_passes_delete_flags_and_locks_from_open_grid(self):
        spec=SimpleNamespace(name="phone")
        term="!favorite&!4*&!lucky&10"
        order=[]
        async def open_grid(spec, query, tags, args, *, on_review):
            self.assertTrue(args.open)
            self.assertFalse(args.all or args.confirm_delete)
            self.assertEqual(query, term)
            order.append("review")
            await on_review(SimpleNamespace(target="existing session",save=lambda *a:"screenshot"),None)
            return True,"left open; 0 transferred"
        async def lock(spec,target):
            self.assertEqual(target,"existing session")
            order.append("sleep")
        with patch.object(night.fleet_entrypoint,"apply_local_environment"), \
             patch.object(night.pokemon_fleet,"load_fleet",return_value=SimpleNamespace(devices={"phone":spec})), \
             patch.object(night.clear_pokemon,"attached_devices",AsyncMock(return_value=[spec])), \
             patch.object(night.clear_pokemon,"run_device",open_grid), patch.object(night.display_sleep,"lock_display",lock), \
             patch.object(night.display_sleep,"wait_until_off",AsyncMock()), redirect_stdout(StringIO()):
            self.assertEqual(asyncio.run(night.worker("phone",{"pending":False,"device":"phone","label":"Safe Trash","term":term},False)),0)
        self.assertEqual(order,["review","sleep"])
