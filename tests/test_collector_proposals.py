from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from copy import deepcopy
from datetime import datetime
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests import support as _support  # noqa: F401
import collect_trades as public
from sources import collect_trades as collect, collector_proposals as review, lucky_raid_offers, trade_approval


class CollectorProposalTests(unittest.TestCase):
    def setUp(self):
        self.names = ["shared", "peer-one", "peer-two"]
        self.cfg = {"guaranteed_proposal_pairs":[self.names[:2], self.names[::2]]}
        self.fleet = SimpleNamespace(devices={n: SimpleNamespace(enabled=True, config={}) for n in self.names})
        self.args = collect.parse_args(["--propose-guaranteed"])
        refreshing = patch.object(lucky_raid_offers, "refresh_state", AsyncMock(return_value={}), create=True)
        self.refresh = refreshing.start()
        self.addCleanup(refreshing.stop)

    def ranked(self, pair, state=None):
        return {name: {"species":"Groudon", "form":"normal", "dex":383, "cp":2300,
                       "stock":2, "query":"!favorite&cp2300&383", "reason":"Recipient owns no perfect"}
                for name in pair}

    def fake_itinerary(self, pairs, state, fleet):
        return {pair: self.ranked(pair) for pair in pairs}, [pairs] if len(pairs) > 1 else []

    def raid_inventory(self):
        now = datetime.now().astimezone().isoformat()
        def offer(dex, name, cp, rating, eligible=1):
            return {"name":name, "dex":dex, "cp":cp, "form":name.lower(),
                    "query":f"legendary&cp{cp}&{dex}", "eligible_count":eligible,
                    "unique_count":1, "live_verified":True, "observed_at":now,
                    "raid_role":{"type":"Dark","relative":rating}}
        offerings = [[offer(644, "Zekrom", 2817, 1), offer(383, "Groudon", 2273, .9)],
                     [offer(717, "Yveltal", 2113, .95)], [offer(717, "Yveltal", 2094, .95)]]
        devices = {n: {"offers":offers, "coverage":{str(dex):{"owned":2, "hundo_count":0, "observed_at":now}
                                                    for dex in (644, 383, 717)}}
                   for n, offers in zip(self.names,offerings)}
        return {"devices":devices}

    def test_joint_value_can_use_first_pairs_second_choice_and_project_the_second_return(self):
        pairs = [tuple(self.names[:2]), tuple(self.names[::2])]
        state = self.raid_inventory()
        state["devices"]["peer-two"]["coverage"]["383"].update(owned=4,hundo_count=1,maxed_hundo=True)
        original = deepcopy(state)
        solo, _ = review.proposal_itinerary(pairs[:1], state, self.fleet)
        self.assertEqual(solo[pairs[0]]["shared"]["cp"], 2817)
        joint, conflicts = review.proposal_itinerary(pairs, state, self.fleet)
        self.assertFalse(conflicts)
        self.assertEqual([joint[p]["shared"]["cp"] for p in pairs], [2273, 2817])
        returned = joint[pairs[1]]["peer-two"]
        self.assertEqual(returned["recipient_coverage"]["owned"], 3)
        self.assertEqual(returned["recipient_coverage"]["hundo_count"], 0)
        self.assertIn("ownership is projected", returned["reason"])
        self.assertEqual(state, original)

    def test_same_species_different_cp_can_be_used_subject_to_eligible_reserve(self):
        state = self.raid_inventory()
        offers = state["devices"]["shared"]["offers"]
        first = offers[0]
        first["eligible_count"] = 2
        offers[1] = dict(first, cp=2900, query="legendary&cp2900&644")
        pairs = [tuple(self.names[:2]), tuple(self.names[::2])]
        plan, conflicts = review.proposal_itinerary(pairs, state, self.fleet)
        self.assertFalse(conflicts)
        self.assertEqual({plan[p]["shared"]["cp"] for p in pairs}, {2817,2900})
        self.assertEqual(plan[pairs[1]]["shared"]["stock"], 1)
        self.fleet.devices["shared"].config["trade_keep_last"] = 1
        _, conflicts = review.proposal_itinerary(pairs, state, self.fleet)
        self.assertEqual(conflicts, [pairs])

    def test_plan_explicitly_displays_mutual_exclusion_when_only_one_individual_exists(self):
        state = self.raid_inventory()
        state["devices"]["shared"]["offers"].pop()
        out = io.StringIO()
        args = collect.parse_args(["--propose-guaranteed", "--plan"])
        with redirect_stdout(out), patch.object(lucky_raid_offers, "load_state", return_value=state):
            self.assertEqual(asyncio.run(review.run(args,self.cfg,self.fleet,{},{})), 0)
        self.assertLess(out.getvalue().index("Mutually exclusive"), out.getvalue().index("shared offers"))
        self.assertEqual(out.getvalue().count("shared offers Zekrom #644 CP 2817"), 2)
        self.refresh.assert_not_awaited()

    def test_projection_updates_exact_form_and_species_without_adding_a_tradeable_copy(self):
        state = self.raid_inventory()
        a,b = self.names[:2]
        first = state["devices"][a]["offers"][0]
        back = state["devices"][b]["offers"][0]
        first.update(form="origin", form_token="origin", query="legendary&cp2817&origin&644")
        for n in (a,b):
            state["devices"][n]["coverage"]["644"]["forms"] = {"origin":{"owned":1}, "other":{"owned":1}}
        after = review._after_swap(state,(a,b),{"give":{a:first,b:back}})
        for n, owned, form in ((a,1,0),(b,3,2)):
            row = after["devices"][n]["coverage"]["644"]
            self.assertEqual((row["owned"],row["forms"]["origin"]["owned"],row["forms"]["other"]["owned"]),
                             (owned,form,1))
            self.assertFalse(any(o["cp"] == first["cp"] for o in after["devices"][n]["offers"]))

    @patch("sources.trade_local.check_trade_lobby", new=lambda a, b: nullcontext())
    def test_held_configured_pair_queues_once_and_quotes_after_releasing_locks(self):
        from sources import collector_day as day
        roster = ["receiver", "companion", "peer-a", "peer-b"]
        fleet = SimpleNamespace(devices={n: SimpleNamespace(config={}) for n in roster})
        cfg = {"receiver":"receiver","guaranteed_proposal_pairs":[["peer-a","peer-b"]]}
        args = collect.parse_args(["--trades","1"])
        locked = set()
        requests = []
        async def profiles(pair, phones, trainers):
            return set(pair) == {"peer-a","peer-b"}
        @asynccontextmanager
        async def opened(n,*args,**kwargs):
            self.assertNotIn(n,locked)
            locked.add(n)
            try:
                yield SimpleNamespace(name=n)
            finally:
                locked.remove(n)
        async def prepare(pool, phone):
            pool.targets[pool.index] = collect.TargetSpec("value","147","!#&147")
        async def exchange(a,b,names,terms,species,*,choosers,on_confirmed, **_kwargs):
            self.assertNotEqual({a.name,b.name},{"peer-a","peer-b"})
            evidence = {a.name: SimpleNamespace(lucky=None)}
            on_confirmed(evidence)
            return evidence
        async def quote(*args, held_pair, on_exchange):
            self.assertEqual(locked,set())
            self.assertEqual(set(held_pair),{"peer-a","peer-b"})
            requests.append(held_pair)
            on_exchange(held_pair,{"receipt":True})
            return 0
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            gaps = day.LuckyGaps({},Path(tmp)/"registrations",set())
            with patch.object(day.LuckyGaps,"load",return_value=gaps), \
                    patch.object(day,"day_counts",side_effect=lambda names,*_: dict.fromkeys(names,0)), \
                    patch.object(collect,"_open_optional_donor",opened), patch.object(day.Offers,"prepare",prepare), \
                    patch.object(day,"guaranteed_profiles",profiles), patch.object(day.trade_local,"trainer_name",AsyncMock(return_value="trainer")), \
                    patch.object(day.trade_local,"trade_once",exchange), patch.object(day.starter_trade,"record_trade"), \
                    patch.object(review,"run",quote):
                code = asyncio.run(day.run(args,cfg,fleet,{},dict.fromkeys(roster,"local"),roster[0],roster[1:],Path(tmp)/"clear.json"))
        self.assertEqual(code,0)
        self.assertEqual(len(requests),1)

    @patch.object(review, "check_pair", new_callable=AsyncMock)
    @patch.object(review.collector_day, "day_counts", side_effect=lambda names, *_: dict.fromkeys(names, 0))
    def test_two_quotes_share_one_phone_and_never_execute(self, counts, check):
        pairs = []
        busy = set()
        async def park(pair, details, args, fleet, machines, owners, token, **kwargs):
            self.assertIsNone(token)
            self.assertFalse(busy)
            busy.update(pair)
            pairs.append(pair)
            await asyncio.sleep(0)
            for n in pair:
                details[n]["special_remaining"] = 1
            busy.clear()
            raise trade_approval.ProposalReady("proposal-" + str(len(pairs)))
        out = io.StringIO()
        with redirect_stdout(out), patch.object(review, "proposal_itinerary", side_effect=self.fake_itinerary), \
                patch.object(review, "review_pair", park):
            code = asyncio.run(review.run(self.args,self.cfg,self.fleet,{},dict.fromkeys(self.names,"local")))
        self.assertEqual(pairs,[tuple(self.names[:2]),tuple(self.names[::2])])
        self.assertEqual(code,0)
        self.assertIn("Parked 2 specific proposal(s)",out.getvalue())
        self.assertIn("share a phone",out.getvalue())
        self.assertIn("Mutually exclusive",out.getvalue())
        self.assertEqual(self.refresh.await_args.kwargs["pairs"], pairs)
        self.assertIn("same Groudon CP 2300",out.getvalue())
        self.assertIn("only one",out.getvalue())

    @patch.object(review, "check_pair", new_callable=AsyncMock)
    @patch.object(review.collector_day, "day_counts", side_effect=lambda names, *_: dict.fromkeys(names, 0))
    def test_zero_allowance_defers_second_pair_independently_of_ranking(self, counts, check):
        out = io.StringIO()
        with redirect_stdout(out), patch.object(review,"proposal_itinerary",side_effect=self.fake_itinerary) as rank, \
                patch.object(review,"review_pair",AsyncMock(side_effect=review.SpecialUnavailable("shared: no Special Trades remain today"))):
            code = asyncio.run(review.run(self.args,self.cfg,self.fleet,{},dict.fromkeys(self.names,"local")))
        self.assertEqual(code,1)
        self.assertEqual(rank.call_count,1)
        self.assertIn("deferred, no Special Trade allowance left on shared",out.getvalue())

    def test_approval_resumes_only_its_exact_pair(self):
        args = collect.parse_args(["--approve-guaranteed","abc"])
        pair = tuple(self.names[::2])
        original = {"participants":self.ranked(pair)}
        with redirect_stdout(io.StringIO()), patch.object(review,"proposal_itinerary") as rank, \
                patch.object(review,"review_pair",AsyncMock()) as exchange:
            code = asyncio.run(review.run(args,self.cfg,self.fleet,{},dict.fromkeys(self.names,"local"),original))
        self.assertEqual(code,0)
        rank.assert_not_called()
        self.refresh.assert_not_awaited()
        self.assertEqual(exchange.await_args.args[0],pair)
        self.assertIs(exchange.await_args.args[1],original["participants"])
        self.assertEqual(exchange.await_args.args[-1],"abc")

    def test_either_daily_cap_prevents_all_phone_and_inventory_work(self):
        pair = tuple(self.names[:2])
        for capped in pair:
            counts = {n: 100 if n == capped else 1 for n in pair}
            args = collect.parse_args(["--propose-guaranteed", *pair])
            with patch.object(review.collector_day, "day_counts", return_value=counts), \
                 patch.object(review, "check_pair", new_callable=AsyncMock) as phones, redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(review.run(args, self.cfg, self.fleet, {}, dict.fromkeys(pair, "host"))), 1)
            phones.assert_not_awaited()
            self.refresh.assert_not_awaited()

    def test_both_badges_and_both_live_allowances_are_required_before_refresh(self):
        pair = tuple(self.names[:2])
        args = collect.parse_args(["--propose-guaranteed", *pair])
        @asynccontextmanager
        async def opened(name, *unused, **kwargs):
            yield SimpleNamespace(name=name)
        for badges, allowance in ((False, 1), (True, 0), (True, None)):
            def check(phone):
                if phone.name == pair[1] and allowance != 1:
                    if allowance == 0:
                        raise review.SpecialUnavailable(f"{phone.name}: no Special Trades remain")
                    raise review.starter.StepFailed("unread allowance")
                return 1
            with patch.object(review.collector_day, "day_counts", return_value=dict.fromkeys(pair, 0)), \
                 patch.object(review.remote_target, "open_fleet_phone", opened), \
                 patch.object(review.trade_local, "trainer_name", AsyncMock(return_value="trainer")), \
                 patch.object(review.collector_day, "guaranteed_profiles", AsyncMock(return_value=badges)), \
                 patch.object(review.trade_local, "check_trade_lobby", return_value=nullcontext()) as lobby, \
                 patch.object(review, "special_allowance", AsyncMock(side_effect=check)) as counters, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(review.run(args, self.cfg, self.fleet, {}, dict.fromkeys(pair, "host"))), 1)
            self.assertEqual(lobby.call_count, int(badges))
            self.assertEqual(counters.await_count, 2 if badges else 0)
            self.refresh.assert_not_awaited()

    def test_approved_reselection_reads_game_allowance_before_stock(self):
        phone = SimpleNamespace(name="shared", read=AsyncMock(return_value=(None, [])))
        participant = self.ranked(("shared","peer"))["shared"]
        with patch.object(review.special_trade,"special_left",return_value=0), \
                patch.object(collect,"choose_filtered_tile",AsyncMock()) as pick:
            with self.assertRaisesRegex(review.SpecialUnavailable,"no Special Trades remain"):
                asyncio.run(review.select_individual(phone,participant,reselect=True))
            pick.assert_not_awaited()
        with patch.object(review.special_trade,"special_left",return_value=1), \
                patch.object(collect,"choose_filtered_tile",AsyncMock(side_effect=[2,1])) as pick:
            asyncio.run(review.select_individual(phone,participant,reselect=True,reserve=1))
            self.assertEqual([call.args[1] for call in pick.await_args_list],["!favorite&383&!#KeepOne","!favorite&cp2300&383&!#KeepOne"])
            self.assertTrue(participant["unique"])
            self.assertEqual(participant["special_remaining"],1)

    def test_verified_exchange_expires_both_inventory_scans_and_accepts_same_ocr_handle(self):
        pair = tuple(self.names[:2])
        participants = self.ranked(pair)
        for item in participants.values():
            item["trainer"] = "TrainerIris"
        receipts = {"shared":object()}
        @asynccontextmanager
        async def opened(name, *args, **kwargs):
            yield SimpleNamespace(name=name)
        async def exchange(*args, on_confirmed, **kwargs):
            on_confirmed(receipts)
            return receipts
        with patch.object(review.remote_target, "open_fleet_phone", opened), \
                patch.object(review.collector_day, "day_counts", return_value=dict.fromkeys(pair,0)), \
                patch.object(review.trade_local, "trainer_name", AsyncMock(return_value="Trainer|ris")), \
                patch.object(review.collector_day, "guaranteed_profiles", AsyncMock(return_value=True)), \
                patch.object(review.trade_local, "trade_once", exchange), \
                patch.object(review.starter_trade, "record_trade") as record, \
                patch.object(lucky_raid_offers, "invalidate_accounts") as expire:
            result = asyncio.run(review.review_pair(pair, participants, self.args, self.fleet, {},
                                                    dict.fromkeys(pair,"local"), token="approved"))
        self.assertIs(result, receipts)
        self.assertIs(record.call_args.kwargs["receipts"], receipts)
        expire.assert_called_once_with(pair, changed_dex={383})

    def test_profile_failure_preserves_missed_frame_from_settle(self):
        pair = tuple(self.names[:2])
        phones = {n: Mock() for n in pair}
        frames = {n: object() for n in pair}
        async def settle(phone, predicate, timeout):
            device = next(n for n in pair if phone is phones[n])
            self.assertEqual(timeout, 5)
            return device == pair[0], frames[device], []
        with redirect_stdout(io.StringIO()), patch.object(review.trade_local, "open_partner", AsyncMock()), \
                patch.object(review.trade_local, "settle", settle):
            self.assertFalse(asyncio.run(review.collector_day.guaranteed_profiles(pair, phones, dict(zip(pair,pair)))))
        phones[pair[0]].save.assert_not_called()
        phones[pair[1]].save.assert_called_once_with(frames[pair[1]], "lucky-profile-unverified")

    def test_decline_or_existing_live_lobby_cannot_start_a_fleet_job(self):
        with patch.object(collect,"run",AsyncMock()) as run, patch.object(trade_approval,"decline",return_value=0), \
                patch.object(trade_approval,"approve",return_value=None):
            self.assertEqual(public.main(["--decline-guaranteed","specific"]),0)
            self.assertEqual(public.main(["--approve-guaranteed","specific"]),0)
            run.assert_not_called()

    def test_rank_binds_recipient_coverage_in_both_directions(self):
        a,b = self.names[:2]
        give = {n:{"name":"Groudon","dex":383,"cp":2300,"form":"normal","query":"legendary&cp2300&383",
                   "eligible_count":2,"unique_count":1,"live_verified":True} for n in (a,b)}
        ranked = {"give":give,"reasons":{a:"a needs this",b:"b needs this"},
                  "coverage":{a:{"owned":1},b:{"owned":0}}}
        details = review.participants_for((a,b),ranked)
        self.assertEqual(details[a]["recipient_coverage"],{"owned":0})
        self.assertEqual(details[b]["reason"],"a needs this")
        self.assertEqual(review.identity_query(details[a]),("legendary&cp2300&383&!#KeepOne","legendary&383&!#KeepOne"))
