import asyncio
import contextlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import support as _support
from sources import (collector_night, ender, fleet_entrypoint, pokemon_fleet, remote_target,
                     special_trade, storage_favorites, storage_keep_one, storage_review, trade_local, trading_accounts)


class AlternateHandsetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mapping = {"alternate": "primary"}
        alias = patch.object(trading_accounts, "aliases", return_value=self.mapping)
        alias.start()
        self.addCleanup(alias.stop)
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        state = patch.object(storage_review.config_paths, "state_dir", return_value=self.directory)
        state.start()
        self.addCleanup(state.stop)
        self.fleet = pokemon_fleet.FleetConfig(Path("config"), {
            n: SimpleNamespace(name=n) for n in ["primary", "alternate", "donor"]
        })

    def test_prefers_primary_and_keeps_single_account_owner(self):
        for owners in ({"alternate":"M4","primary":"M3","donor":"M3"},
                       {"primary":"M3","alternate":"M4","donor":"M3"}):
            fleet, result = trading_accounts.route_fleet(self.fleet, owners, self.mapping)
            self.assertEqual(result, {"primary":"M3","donor":"M3"})
            self.assertEqual(fleet.transports, {})

    async def test_fallback_opens_real_phone_as_canonical_account(self):
        routed, owners = trading_accounts.route_fleet(self.fleet, {"alternate":"M4"}, self.mapping)
        self.assertEqual(owners, {"primary":"M4"})
        target = SimpleNamespace(platform="ios", close=AsyncMock())
        with patch.object(fleet_entrypoint, "local_machine", return_value="M3"), \
             patch.object(remote_target.RemoteTarget, "connect", AsyncMock(return_value=target)) as connect:
            async with remote_target.open_fleet_phone("primary", self.directory, machine="M4",
                                                      machines={"M4":{}}, fleet=routed) as phone:
                self.assertEqual((phone.name, phone.transport_name), ("primary","alternate"))
                self.assertEqual(phone.shots, self.directory / "alternate")
        self.assertEqual(connect.await_args.args[-1].name, "alternate")
        target.close.assert_awaited_once()

    async def test_night_verifies_account_policy_on_specific_transport(self):
        calls = {}
        async def spawn(*cmd, **kwargs):
            async def communicate(payload):
                calls[cmd[-1]] = json.loads(payload)
            return SimpleNamespace(communicate=communicate, returncode=0)
        policy = SimpleNamespace(favorites=lambda n: {storage_review.STRICT:"query-for-"+n})
        with patch.object(storage_review, "load_policy", return_value=policy), \
             patch.object(fleet_entrypoint, "local_machine", return_value="M4"), \
             patch.object(collector_night.asyncio, "create_subprocess_exec", spawn):
            result = await collector_night.finish({"primary":"M4"}, {}, ["primary"], Path("x"),
                        {"receiver":"primary"}, "primary", plan=True, transports={"primary":"alternate"})
        self.assertEqual(result, 0)
        self.assertEqual(calls["alternate"]["device"], "alternate")
        self.assertEqual(calls["alternate"]["account"], "primary")
        self.assertEqual(calls["alternate"]["term"], "query-for-primary")

    async def test_prepare_worker_skips_local_alternate_if_remote_primary_attached(self):
        physical = [self.fleet.devices[n] for n in ("alternate","donor")]
        prepare = AsyncMock(return_value=True)
        with patch.object(ender.starter,"parse_args"), patch.object(ender,"prepare_phone",prepare), \
             patch.object(ender.clear_pokemon,"attached_devices",AsyncMock(return_value=physical)), \
             patch.object(pokemon_fleet,"load_fleet",return_value=self.fleet), \
             patch.object(fleet_entrypoint,"machine_configs",return_value={}), \
             patch.object(remote_target,"discover_attached_devices",return_value=({"primary":"M3","alternate":"M4","donor":"M4"},{})) as survey:
            selected = ender.prepare_handsets()
            self.assertEqual(await ender.prepare_local(selected),0)
        survey.assert_called_once()
        self.assertEqual([call.args[0].name for call in prepare.await_args_list],["donor"])

    async def test_alternate_retained_proofs_stay_on_receiver_and_name_handset(self):
        phone = SimpleNamespace(name="alternate")
        policy = SimpleNamespace(receiver="primary")
        with patch.object(storage_keep_one, "_grid", AsyncMock(return_value=(2,"grid"))):
            row = await storage_keep_one._one(phone, policy, 25)
        self.assertEqual((row["device"],row["phone"]),("primary","alternate"))
        self.assertEqual(storage_review.load_verified_anchors("primary"),{25})
        self.assertEqual(storage_review.load_verified_anchors("alternate"),{25})
        self.assertEqual(storage_keep_one._receipts("alternate"),{})

    async def test_special_trade_discovers_one_account_and_local_counts_use_alternate(self):
        from sources import starter_trade
        with patch.object(fleet_entrypoint,"machine_configs",return_value={"M4":{}}), \
             patch.object(fleet_entrypoint,"local_machine",return_value="M4"), \
             patch.object(special_trade,"attached",AsyncMock(return_value={"alternate":self.fleet.devices["alternate"]})), \
             patch.object(starter_trade,"count",AsyncMock(return_value={"4*":1})) as count:
            here, owners, machines, fleet = await special_trade._discover_routes(self.fleet,local_only=True)
            self.assertEqual(set(here),{"primary"})
            self.assertEqual(owners,{"primary":"M4"})
            await special_trade._count_terms_on_device(here["primary"],["4*"],None,
                                                       machine="M4",machines=machines,fleet=fleet)
        self.assertEqual(count.await_args.args[0].name,"alternate")

    async def test_direct_trade_rejects_same_trainer_before_reading_phones(self):
        for pair in (("primary","alternate"), ("alternate","primary"), ("primary","primary")):
            with self.assertRaises(trade_local.TradeRefused):
                await trade_local.trade_once(*(SimpleNamespace(name=n) for n in pair), {}, {}, {})

    def test_legacy_alternate_pending_confirmation_blocks_primary(self):
        pending, = trade_local._pending_files(["alternate"])
        pending.parent.mkdir(parents=True)
        pending.touch()
        with self.assertRaises(trade_local.TradeOutcomeUnknown):
            trade_local._check_trainer_pending(["primary","donor"])

    async def test_sync_both_receiver_handsets_serializes_the_account(self):
        policy = SimpleNamespace(receiver="primary", donors=("donor",))
        owners = {"primary": "M3", "alternate": "M4"}
        active = set()
        visits = []

        @contextlib.asynccontextmanager
        async def opened(account, shots, *, machine, machines, fleet):
            self.assertNotIn(account, active)
            active.add(account)
            visits.append(fleet.transports.get(account, account))
            try:
                await asyncio.sleep(0)
                yield SimpleNamespace(name=account)
            finally:
                active.remove(account)

        args = SimpleNamespace(devices=["primary", "alternate"], plan=False, local=False,
                               refresh_anchors=0, keep_one=None)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(fleet_entrypoint, "apply_local_environment"))
            stack.enter_context(patch.object(storage_favorites.collect_trades, "load_config",
                                            return_value={"clear_config": "clear"}))
            stack.enter_context(patch.object(storage_review, "load_policy", return_value=policy))
            stack.enter_context(patch.object(pokemon_fleet, "load_fleet", return_value=self.fleet))
            stack.enter_context(patch.object(fleet_entrypoint, "machine_configs", return_value={}))
            stack.enter_context(patch.object(remote_target, "discover_attached_devices", return_value=(owners, {})))
            stack.enter_context(patch.object(remote_target, "open_fleet_phone", opened))
            stack.enter_context(patch.object(storage_favorites, "sync_phone", AsyncMock(return_value=[])))
            self.assertEqual(await storage_favorites.run(args), 0)
        self.assertCountEqual(visits, ["primary", "alternate"])

    async def test_explicit_alternate_favorites_write_physical_receipt(self):
        policy = SimpleNamespace(receiver="primary",donors=("donor",))
        owners = {"primary":"M3","alternate":"M4","donor":"M3"}
        @contextlib.asynccontextmanager
        async def opened(account, shots, *, machine, machines, fleet):
            self.assertEqual((account, fleet.transports[account], machine),("primary","alternate","M4"))
            yield SimpleNamespace(name=account,transport_name=fleet.transports[account])
        args=SimpleNamespace(devices=["alternate"],plan=False,local=False,refresh_anchors=0,keep_one=None)
        with patch.object(storage_favorites.fleet_entrypoint,"apply_local_environment"), \
             patch.object(storage_favorites.collect_trades,"load_config",return_value={"clear_config":"clear"}), \
             patch.object(storage_review,"load_policy",return_value=policy), \
             patch.object(pokemon_fleet,"load_fleet",return_value=self.fleet), \
             patch.object(fleet_entrypoint,"machine_configs",return_value={}), \
             patch.object(remote_target,"discover_attached_devices",return_value=(owners,{})), \
             patch.object(remote_target,"open_fleet_phone",opened), \
             patch.object(storage_favorites,"wanted",return_value={"SafeTrash":"query"}) as wanted, \
             patch.object(storage_favorites.saved,"list_favorites",AsyncMock(return_value=[])), \
             patch.object(storage_favorites.saved,"read_favorite",AsyncMock(return_value=None)), \
             patch.object(storage_favorites.saved,"ensure_favorite",AsyncMock()), \
             patch.object(storage_favorites,"verify_named",AsyncMock(return_value={"term":"query","count":2})):
            self.assertEqual(await storage_favorites.run(args),0)
        wanted.assert_called_once_with("primary",policy)
        receipt=self.directory/"storage-favorites"/"alternate"/"sync.jsonl"
        self.assertEqual(json.loads(receipt.read_text())["label"],"SafeTrash")
