from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import support as _test_support  # noqa: F401

import priority_trade as public_priority
from sources import collector_day, priority_trade as priority, storage_review, trade_local


class BudgetTests(unittest.TestCase):
    def apply(self, initial, receiver, plan):
        remaining = dict(initial)
        for donor, count in plan.phase1.items():
            self.assertNotEqual(donor, receiver)
            remaining[donor] -= count
            remaining[receiver] -= count
        for (a, b), count in plan.phase2.items():
            self.assertLess(a, b)
            self.assertNotIn(receiver, (a, b))
            self.assertGreater(count, 0)
            remaining[a] -= count
            remaining[b] -= count
        self.assertTrue(all(n >= 0 for n in remaining.values()))
        return remaining

    def test_all_100_reaches_at_least_three_and_can_cap_all_four(self):
        initial = dict.fromkeys(("r", "a", "b", "c"), 100)
        plan = priority.plan_budget(initial, "r")
        self.assertEqual(sum(plan.phase1.values()), 100)
        self.assertEqual(sum(plan.phase2.values()), 100)
        self.assertEqual(self.apply(initial, "r", plan), dict.fromkeys(initial, 0))

    def test_partial_day_prefers_the_donor_with_the_most_remaining(self):
        initial = {"r": 4, "a": 5, "b": 7, "c": 2}
        plan = priority.plan_budget(initial, "r")
        self.assertEqual(plan.phase1, {"a": 0, "b": 4, "c": 0})
        self.assertEqual(plan.phase2, {("a", "b"): 3, ("a", "c"): 2})
        self.assertEqual(self.apply(initial, "r", plan), dict.fromkeys(initial, 0))

    def test_offline_donor_is_omitted_and_receiver_pulls_are_balanced(self):
        initial = {"r": 100, "a": 100, "b": 100}
        plan = priority.plan_budget(initial, "r")
        self.assertEqual(plan.phase1, {"a": 50, "b": 50})
        self.assertEqual(plan.phase2, {("a", "b"): 50})
        self.assertEqual(self.apply(initial, "r", plan), dict.fromkeys(initial, 0))

    def test_odd_budgets_with_a_capped_receiver(self):
        initial = {"r": 0, "a": 3, "b": 3, "c": 3}
        plan = priority.plan_budget(initial, "r")
        self.assertEqual(sum(plan.phase1.values()), 0)
        self.assertEqual(sum(plan.phase2.values()), 4)
        self.assertEqual(sorted(self.apply(initial, "r", plan).values()), [0, 0, 0, 1])

    def test_replan_excludes_exhausted_priority_stock_and_a_blocked_peer_pair(self):
        initial = {"r": 2, "a": 3, "b": 3, "c": 3}
        plan = priority._plan_budget(initial, "r", {"c"}, {("a", "c"), ("b", "c")})
        self.assertEqual(plan.phase1, {"a": 0, "b": 0, "c": 2})
        self.assertEqual(sum(plan.phase2.values()), 1)
        self.assertNotIn(("a", "b"), plan.phase2)

    def test_replan_stays_with_the_open_donor_when_it_costs_nothing(self):
        # After one trade from "a", pulling from "b" would score the same; the
        # open pair must win so phones are not reopened after every trade.
        remaining = {"r": 99, "a": 99, "b": 100, "c": 100}
        donors = {"a", "b", "c"}
        edges = {("a", "b"), ("a", "c"), ("b", "c")}
        self.assertGreater(priority._plan_budget(remaining, "r", donors, edges, "a").phase1["a"], 0)
        peer = priority._plan_budget({"r": 0, "a": 50, "b": 51, "c": 50}, "r", set(), edges, ("a", "c"))
        self.assertIn(("a", "c"), peer.phase2)
        concurrent = priority._plan_budget(remaining, "r", donors, edges, "a", parallel=True)
        self.assertEqual(concurrent.first_donor, "a")
        self.assertGreater(concurrent.phase2["b", "c"], 0)

    def test_parallel_start_selects_a_donor_with_a_usable_complement(self):
        initial = dict.fromkeys(("r", "a", "b", "c"), 5)
        plan = priority._plan_budget(initial, "r", {"a", "b"}, {("a", "c")}, parallel=True)
        self.assertEqual(plan.first_donor, "b")
        self.assertEqual(plan.phase2, {("a", "c"): 5})
        self.assertEqual(self.apply(initial, "r", plan), dict.fromkeys(initial, 0))



class PeerValueTests(unittest.TestCase):
    def test_value_searches_drop_receiver_lucky_species(self):
        donor = [priority.collect.TargetSpec("value-1", "147-149,371-373", "!lucky&147-149,371-373"),
                 priority.collect.TargetSpec("value-2", "443", "!lucky&443")]
        with mock.patch.object(priority.collector_day, "valuable_queries", return_value=(donor, [])):
            kept = priority._value_targets(Path("clear.json"), {}, {371, 372, 373, 443})
        self.assertEqual([(t.name, t.query, t.term) for t in kept],
                         [("Valuable-1", "147-149", "!lucky&147-149")])

    def test_peer_lists_put_far_catches_first_and_local_trade_last(self):
        self.assertEqual(priority.PEER_TIERS, ("FarTrade", "FarNot98", "LocalNot98", "LocalTrade"))
        self.assertNotIn("ReceiverLucky", priority.PEER_TIERS)

    def test_a_pinned_favorite_is_opened_instead_of_typed(self):
        phone = SimpleNamespace(name="donor-a", transport_name="donor-a",
                                read=mock.AsyncMock(return_value=(None, [])))
        pool = priority.WholeFavorite(priority.collect.TargetSpec("LocalTrade", "", "distance-100&!4*"))
        with mock.patch("sources.trade_saved_search.open_named_picker", new=mock.AsyncMock()) as opened, \
                mock.patch.object(priority.collect, "choose_filtered_tile", new=mock.AsyncMock(return_value=9)) as choose:
            asyncio.run(pool.pick(phone))
        opened.assert_awaited_once()
        self.assertTrue(choose.await_args.kwargs["preapplied"])

    def test_an_unverified_favorite_falls_back_to_typing(self):
        priority._UNPINNED.clear()
        phone = SimpleNamespace(name="donor-b", transport_name="donor-b",
                                read=mock.AsyncMock(return_value=(None, [])))
        pool = priority.WholeFavorite(priority.collect.TargetSpec("FarNot98", "", "!distance-100&!4*"))
        failed = mock.AsyncMock(side_effect=priority.starter.StepFailed("did not match its expected saved filter"))
        with mock.patch("sources.trade_saved_search.open_named_picker", new=failed), \
                mock.patch.object(priority.collect, "choose_filtered_tile", new=mock.AsyncMock(return_value=9)) as choose, \
                contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(pool.pick(phone))
            asyncio.run(pool.pick(phone))
        failed.assert_awaited_once()
        self.assertNotIn("preapplied", choose.await_args.kwargs)
        priority._UNPINNED.clear()


@dataclass
class FakeFleet:
    devices: dict
    transports: dict = field(default_factory=dict)


class PriorityRunTests(unittest.TestCase):
    def exercise(self, *, plan_only=False, hundo=True, rendezvous=False, swap=False, peer_failure=None,
                 second_peer_failure=None, busy=None):
        for name, stub in (("_value_targets", mock.Mock(return_value=[])),
                           ("_open_named", mock.AsyncMock(return_value=False))):
            patcher = mock.patch.object(priority, name, new=stub)
            patcher.start()
            self.addCleanup(patcher.stop)
        roster = ["receiver", "donor-b", "donor-c", "donor-a"]
        r, a, b, c = roster
        alternate = "donor-alternate"
        fleet = FakeFleet({n: SimpleNamespace(config={"trade_keep_last": 2})
                           for n in [*roster, alternate]})
        machines = {"mac-a": {}, "mac-b": {}}
        owners = {alternate: "mac-a", a: "mac-b", b: "mac-a", c: "mac-b"}
        terms = {
            a: {"XXL/XXS": "!4*&xxl,xxs", "ReceiverLucky": storage_review.EMPTY,
                "ReceiverPerfect": "!4*&633" if hundo else storage_review.EMPTY,
                "LocalTrade": "!4*&distance-100", "LocalNot98": "!98Plus&distance-100"},
            b: {"XXL/XXS": "!4*&xxl,xxs", "ReceiverLucky": storage_review.EMPTY,
                "ReceiverPerfect": storage_review.EMPTY, "FarTrade": "!4*&distance100-"},
            c: {"XXL/XXS": storage_review.EMPTY, "ReceiverLucky": "!4*&371-373",
                "ReceiverPerfect": storage_review.EMPTY, "LocalTrade": "!4*&distance-100"},
        }
        history = {r: 1, a: 0, b: 0, c: 0}
        stock = Counter({
            (a, "XXL/XXS"): 1, (b, "XXL/XXS"): 1, (c, "ReceiverLucky"): 1,
            (a, "ReceiverPerfect"): 3 if hundo else 0, (r, "return-1"): 20,
            (a, "LocalTrade"): 1, (a, "LocalNot98"): 20,
            (b, "FarTrade"): 20, (c, "LocalTrade"): 20,
        })
        held = set()
        in_flight = set()
        first_pairs = {}
        raised = []
        completed = set()
        trace = []
        records = []
        attempts = []
        # Create the events inside the test loop below.
        together = receiver_released = None

        busy_left = {busy} if busy else set()

        @contextlib.asynccontextmanager
        async def opened(name, _shots, *, machine, machines, fleet, dry_run):
            if name in busy_left:
                busy_left.discard(name)
                raise priority.pokemon_fleet.FleetError(
                    f"{name} is already controlled by pid 1 (python -m starter --only goplus)")
            handset = fleet.transports.get(name, name)
            self.assertEqual(machine, owners[handset])
            self.assertNotIn(name, held, f"{name} opened by both lanes")
            self.assertFalse(dry_run)
            held.add(name)
            trace.append(("open", name))
            try:
                yield SimpleNamespace(name=name, still=mock.AsyncMock(),
                                      read=mock.AsyncMock(return_value=(None, [])))
            finally:
                self.assertFalse(any(name in pair for pair in in_flight),
                                 f"{name} closed mid-trade")
                held.remove(name)
                trace.append(("close", name))
                if name == r and swap:
                    receiver_released.set()

        async def trainer(phone):
            self.assertIn(phone.name, held)
            return f"trainer-{phone.name}"

        async def prepare(phone, term, *_frames, observed, min_count):
            label = "return-1" if phone.name == r else "ReceiverPerfect"
            self.assertEqual(min_count, 3)
            observed["count"] = stock[phone.name, label]

        async def choose(phone, term, label, *, skip_index, picked_species, keep_copies=0):
            self.assertEqual(skip_index, 0)
            self.assertEqual(term, "!#&147" if phone.name == r else terms[phone.name][label])
            count = stock[phone.name, label]
            if count == 0:
                raise trade_local.TradeOver(f"no {label} left")
            if label in {"ReceiverPerfect", "return-1"}:
                self.assertEqual(keep_copies, 2)
                self.assertGreater(count, keep_copies)
            else:
                self.assertEqual(keep_copies, 0)
            picked_species.append(f"picked-{phone.name}")
            return count

        async def exchange(left, right, names, _terms, labels, *, choosers, on_confirmed, **_kwargs):
            pair = (left.name, right.name)
            self.assertTrue(set(pair) <= held)
            self.assertTrue(all(set(pair).isdisjoint(other) for other in in_flight))
            lane = "R" if r in pair else "P"
            first = lane not in first_pairs
            first_pairs.setdefault(lane, pair)
            if second_peer_failure is not None and not raised and frozenset(pair) in completed:
                raised.append(pair)
                raise second_peer_failure
            in_flight.add(pair)
            trace.append(("enter", pair))
            attempts.append(({n: labels[n] for n in pair}, dict(history)))
            try:
                if first and rendezvous:
                    if len(in_flight) == 2:
                        self.assertEqual(held, set(roster))
                        trace.append(("overlap", None))
                        together.set()
                    await together.wait()
                if first and lane == "P":
                    if swap:
                        # R must finish and release its old pair while this
                        # exchange is still in flight on the next XXL donor.
                        await receiver_released.wait()
                        self.assertTrue(set(pair) <= held)
                        self.assertNotIn(r, held)
                    if peer_failure is not None:
                        raise peer_failure
                await trade_local.both(choosers[left.name](left), choosers[right.name](right))
                for n in pair:
                    stock[n, labels[n]] -= 1
                on_confirmed({left.name: trade_local.TradeReceipt(Path("received.png"), "Received", None)})
                completed.add(frozenset(pair))
            finally:
                in_flight.remove(pair)
                trace.append(("leave", pair))

        def counts(names, all_machines, _day):
            self.assertEqual(all_machines, machines)
            return collector_day.DayCounts({n: history[n] for n in names}, set())

        def record(pair, labels, tier, reason, *, offered_dex, receipts):
            self.assertEqual(reason, "priority")
            self.assertTrue(receipts)
            self.assertEqual(set(labels), set(pair))
            if r in pair:
                self.assertEqual(offered_dex[r], 147)
            if tier == "ReceiverPerfect":
                self.assertEqual(offered_dex[a], 633)
            for n in pair:
                history[n] += 1
                self.assertLessEqual(history[n], 5)
            records.append((pair, tier))

        async def invoke(args):
            nonlocal together, receiver_released
            together = asyncio.Event()
            receiver_released = asyncio.Event()
            # A serialized scheduler or a stuck handoff fails rather than
            # hanging the test suite.
            return await asyncio.wait_for(priority.run(args), timeout=5)

        try:
            with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as out, \
                    mock.patch.object(priority.collect, "load_config", return_value={"receiver": r}), \
                    mock.patch.object(priority.config_paths, "default_config", side_effect=lambda n: Path(tmp) / n), \
                    mock.patch.object(priority.pokemon_fleet, "load_fleet", return_value=fleet), \
                    mock.patch.object(priority.fleet_entrypoint, "machine_configs", return_value=machines), \
                    mock.patch.object(priority.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(priority.trading_accounts, "aliases", return_value={alternate: r}), \
                    mock.patch.object(priority.storage_review, "load_policy", return_value=SimpleNamespace(donors=(a, b, c))), \
                    mock.patch.object(priority.storage_favorites, "wanted", side_effect=lambda n, _policy: terms[n]), \
                    mock.patch.object(priority.collect, "build_return_queries", return_value=["!#&147"]), \
                    mock.patch.object(priority.collect, "prepare_saved_query", side_effect=prepare), \
                    mock.patch.object(priority.collect, "choose_filtered_tile", side_effect=choose), \
                    mock.patch.object(collector_day.starter, "open_storage", new_callable=mock.AsyncMock), \
                    mock.patch.object(collector_day, "day_counts", side_effect=counts), \
                    mock.patch.object(priority.remote_target, "open_fleet_phone", side_effect=opened) as connect, \
                    mock.patch.object(trade_local, "trainer_name", side_effect=trainer), \
                    mock.patch.object(trade_local, "trade_once", side_effect=exchange), \
                    mock.patch.object(priority.starter_trade, "record_trade", side_effect=record):
                argv = ["--trades", "5", "--shots", tmp] + (["--plan"] if plan_only else [])
                code = asyncio.run(invoke(priority.parse_args(argv)))
                if plan_only:
                    connect.assert_not_called()
                log = out.getvalue()
        finally:
            self.assertFalse(in_flight)
            self.assertFalse(held)
        return code, records, attempts, history, log, trace

    def test_receiver_tiers_run_in_order_across_all_donors(self):
        code, records, attempts, history, _, _ = self.exercise()
        self.assertEqual(code, 0)
        received = [(pair, tier) for pair, tier in records if "receiver" in pair]
        self.assertEqual([tier for _, tier in received],
                         ["XXL/XXS", "XXL/XXS", "ReceiverLucky", "ReceiverPerfect"])
        self.assertEqual({pair[0] for pair, _ in received[:2]}, {"donor-b", "donor-c"})
        self.assertTrue(any("receiver" not in pair for pair, _ in records))
        for selection, _ in attempts:
            if "receiver" not in selection:
                self.assertTrue(set(selection.values()) <= set(priority.PEER_TIERS))
        self.assertGreaterEqual(sum(count == 5 for count in history.values()), 3)

    def test_a_phone_briefly_held_by_another_command_rejoins_the_run(self):
        with mock.patch.object(priority, "BUSY_RETRY", 0.01):
            code, records, _, history, log, _ = self.exercise(busy="donor-b")
        self.assertEqual(code, 0)
        self.assertIn("donor-b: busy with another fleet command", log)
        self.assertNotIn("donor-b: unavailable", log)
        # The receiver phase waits for the resting donor instead of ending,
        # and donor trades leave that donor enough allowance for the receiver.
        received = [tier for pair, tier in records if set(pair) == {"donor-b", "receiver"}]
        self.assertEqual(received, ["XXL/XXS", "ReceiverPerfect"])
        self.assertEqual(history["receiver"], 5)
        self.assertLess(log.index("ReceiverPerfect: donor-b <-> receiver"), log.index("receiver phase finished"))

    def test_exhausted_priority_stock_frees_all_donors_before_receiver_cap(self):
        code, records, attempts, history, log, _ = self.exercise(hundo=False)
        self.assertEqual(code, 0)
        self.assertEqual(history["receiver"], 4)
        self.assertEqual([tier for pair, tier in records if "receiver" in pair],
                         ["XXL/XXS", "XXL/XXS", "ReceiverLucky"])
        self.assertIn("starting donor trades", log)
        moto_terms = [selection["donor-b"] for selection, _ in attempts
                      if "receiver" not in selection and "donor-b" in selection]
        # LocalTrade is the last resort (user, 24 Nov 2026).
        self.assertEqual(moto_terms[0], "LocalNot98")
        # LocalTrade only once LocalNot98 is spent, which this stock never is.
        self.assertNotIn("LocalTrade", moto_terms)

    def test_both_lanes_trade_simultaneously_with_exclusive_phone_ownership(self):
        code, records, _, _, _, trace = self.exercise(rendezvous=True)
        self.assertEqual(code, 0)
        overlap = trace.index(("overlap", None))
        starts = [pair for action, pair in trace[:overlap] if action == "enter"]
        self.assertEqual(len(starts), 2)
        self.assertTrue(set(starts[0]).isdisjoint(starts[1]))
        self.assertEqual(set(starts[0]) | set(starts[1]),
                         {"receiver", "donor-b", "donor-c", "donor-a"})
        self.assertFalse(any(action == "leave" for action, _ in trace[:overlap]))
        self.assertTrue(any("receiver" in pair for pair, _ in records))
        self.assertTrue(any("receiver" not in pair for pair, _ in records))

    def test_receiver_takes_next_priority_donor_after_peer_trade_finishes(self):
        # donor-b has no ReceiverLucky stock; that tier lives on donor-a,
        # initially in lane P. The earlier handoff to donor-c is forced to
        # happen while the first peer trade is still in flight.
        code, records, _, _, _, trace = self.exercise(rendezvous=True, swap=True)
        self.assertEqual(code, 0)
        first_peer = ("donor-a", "donor-c")
        first_receiver = ("donor-b", "receiver")
        next_receiver = ("donor-c", "receiver")
        r_end = trace.index(("leave", first_receiver))
        p_end = trace.index(("leave", first_peer))
        p_close = trace.index(("close", "donor-c"))
        r_start = trace.index(("enter", next_receiver))
        self.assertLess(r_end, p_end)
        self.assertLess(p_end, p_close)
        self.assertLess(p_close, r_start)
        received = [(pair[0], tier) for pair, tier in records if "receiver" in pair]
        self.assertEqual(received[:3], [("donor-b", "XXL/XXS"), ("donor-c", "XXL/XXS"),
                                        ("donor-a", "ReceiverLucky")])

    def test_recoverable_peer_exception_does_not_stop_receiver_lane(self):
        error = priority.starter.StepFailed("not on the friends list")
        _, records, _, history, log, trace = self.exercise(rendezvous=True, peer_failure=error)
        self.assertIn(("overlap", None), trace)
        self.assertIn("skipped (not on the friends list)", log)
        self.assertEqual(history["receiver"], 5)
        self.assertEqual([tier for pair, tier in records if "receiver" in pair],
                         ["XXL/XXS", "XXL/XXS", "ReceiverLucky", "ReceiverPerfect"])

    def test_a_pair_that_already_traded_rests_instead_of_being_blocked(self):
        # receiver 24 Nov: "(1)" over an undrawn friends row after 40 trades with
        # donor-c read as "not on the friends list" and blocked the pair.
        error = priority.starter.StepFailed("MewTwo is not on the friends list")
        with mock.patch.object(priority, "COOLDOWN", 0.0):
            _, records, _, history, log, _ = self.exercise(second_peer_failure=error)
        self.assertIn("resting", log)
        self.assertNotIn("skipped (MewTwo", log)
        self.assertEqual(history["receiver"], 5)

    def test_a_phone_that_cannot_type_a_list_moves_to_its_next_one(self):
        error = priority.starter.StepFailed(
            "donor-a: iOS editor did not show the requested query ending (x.png)")
        _, records, attempts, history, log, _ = self.exercise(second_peer_failure=error)
        self.assertIn("retrying (donor-a: iOS editor", log)
        self.assertNotIn("resting", log)
        self.assertEqual(history["receiver"], 5)

    def test_unexpected_peer_exception_is_reraised_after_phone_cleanup(self):
        with self.assertRaisesRegex(ValueError, "unexpected peer failure"):
            self.exercise(rendezvous=True, peer_failure=ValueError("unexpected peer failure"))

    def test_plan_routes_receiver_alias_without_opening_any_phones(self):
        code, records, attempts, history, log, _ = self.exercise(plan_only=True)
        self.assertEqual(code, 0)
        self.assertFalse(records or attempts)
        self.assertEqual(history["receiver"], 1)
        self.assertIn("receiver [mac-a]", log)
        self.assertIn("phase 1:", log)
        self.assertIn("phase 2:", log)
        self.assertIn("lane R: donor-b -> receiver", log)
        self.assertIn("lane P: donor-a <-> donor-c", log)

    def test_public_shim_passes_local_scope_to_mode(self):
        with mock.patch.object(public_priority.fleet_entrypoint, "apply_registry_override",
                               side_effect=lambda raw: raw), \
                mock.patch.object(public_priority.fleet_entrypoint, "clean_scope_flags",
                                  return_value=["--plan"]), \
                mock.patch.object(public_priority.fleet_entrypoint, "local_requested", return_value=True), \
                mock.patch.object(priority, "main", return_value=0) as main:
            self.assertEqual(public_priority.main(["--local", "--plan"]), 0)
        main.assert_called_once_with(["--plan", "--local"])


if __name__ == "__main__":
    unittest.main()
