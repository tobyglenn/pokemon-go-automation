from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from tests import support as _test_support  # noqa: F401

import collect_trades as public_collect
from sources import clear_pokemon, collect_trades, remote_target, starter, trade_local


def _box(text: str, x: int, y: int, width: int = 120, height: int = 36) -> clear_pokemon.OCRBox:
    return clear_pokemon.OCRBox(text, 1.0, x, y, width, height)


class _FakePipe:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.written: list[str] = []
        self._responses: list[str] = []
        self.closed = False

    def write(self, data: str) -> int:
        self.written.append(data)
        req = json.loads(data.strip())
        self._responses.append(json.dumps(self._handler(req)) + "\n")
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    def __init__(self, handler) -> None:
        self.pipe = _FakePipe(handler)
        self.stdin = self.pipe
        self.stdout = self._Out(self.pipe._responses)
        self._poll = None

    class _Out:
        def __init__(self, queue: list[str]) -> None:
            self._queue = queue
            self.closed = False

        def readline(self) -> str:
            return self._queue.pop(0) if self._queue else ""

        def close(self) -> None:
            self.closed = True

    def poll(self):
        return self._poll

    def wait(self, timeout=None):
        self._poll = 0
        return 0

    def terminate(self):
        self._poll = 0


class RemoteTargetTests(unittest.TestCase):
    def test_remote_target_android_and_ios_rpc_round_trip(self) -> None:
        img = Image.new("RGB", (750, 1334), (10, 20, 30))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        calls: list[dict] = []

        def handler(req: dict) -> dict:
            calls.append(req)
            cmd = req["cmd"]
            if cmd == "screenshot":
                return {"id": req["id"], "ok": True, "png": png_b64}
            if cmd == "adb":
                return {"id": req["id"], "ok": True, "returncode": 0, "stdout": "ok", "stderr": ""}
            if cmd == "front_package":
                return {"id": req["id"], "ok": True, "package": "com.nianticlabs.pokemongo"}
            if cmd == "restore":
                return {"id": req["id"], "ok": True, "acted": True, "restored_cold": False}
            if cmd == "ios_keyboard":
                return {"id": req["id"], "ok": True, "shown": True}
            return {"id": req["id"], "ok": True}

        android_proc = _FakeProc(handler)
        android_proxy = remote_target.RemoteTarget("android-two", "host-b", "android", android_proc)
        got_img = asyncio.run(android_proxy.screenshot())
        self.assertEqual(got_img.size, (750, 1334))
        asyncio.run(android_proxy.tap([100, 200]))
        proc = android_proxy._adb("shell", "echo ok")
        self.assertEqual(proc.stdout, "ok")
        self.assertEqual(android_proxy.front_package(), "com.nianticlabs.pokemongo")
        self.assertTrue(asyncio.run(android_proxy.restore()))
        asyncio.run(android_proxy.close())
        self.assertTrue(android_proc.stdin.closed)
        self.assertTrue(android_proc.stdout.closed)

        ios_proxy = remote_target.RemoteTarget(
            "ios-one",
            "host-b",
            "ios",
            _FakeProc(handler),
            ready={"viewport": {"width": 375, "height": 667}, "wda_port": 8101},
        )
        assert ios_proxy.device is not None
        self.assertEqual(ios_proxy.device.logical_point([750, 1334], got_img), [375, 667])
        self.assertTrue(ios_proxy.device.driver.is_keyboard_shown())
        asyncio.run(ios_proxy.device.wda_keys("!98Plus&rolycoly", frequency=120))
        asyncio.run(ios_proxy.tap_pixel([375, 667], got_img))
        asyncio.run(ios_proxy.close())

        cmds = [c["cmd"] for c in calls]
        self.assertIn("ios_wda_keys", cmds)
        self.assertIn("tap", cmds)
        self.assertIn("close", cmds)


class CollectTradesPlannerTests(unittest.TestCase):
    def test_build_donor_query_omits_bang_hash_and_keeps_98plus_exclusion(self) -> None:
        q = collect_trades.build_donor_query("rolycoly")
        self.assertTrue(q.endswith("&rolycoly"))
        tokens = q.split("&")
        self.assertIn("!98Plus", tokens)
        self.assertIn("!dontrade", tokens)
        self.assertIn("!purified", tokens)
        self.assertIn("!traded", tokens)
        self.assertIn("!shiny", tokens)
        self.assertIn("!4*", tokens)
        self.assertNotIn("!#", tokens)

    def test_build_return_queries_uses_curated_useful_tiers_and_enforces_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "clear-pokemon.json"
            cfg.write_text(json.dumps({
                "safe_dex_ranges": "10-12,931,960-961",
                "extra_exclude_dex": [837, 838, 839],
            }))
            # Legacy "!mega1-" in config must also be stripped so all non-Mega species stay eligible.
            queries = collect_trades.build_return_queries(
                cfg,
                return_filters=list(collect_trades.REQUIRED_RETURN_FILTERS) + ["!mega1-"],
            )
            compat_queries = collect_trades.build_return_slices(
                cfg,
                return_filters=list(collect_trades.REQUIRED_RETURN_FILTERS) + ["!mega1-"],
            )
        self.assertEqual(queries, compat_queries)
        self.assertGreaterEqual(len(queries), 4)
        expected_union = (
            clear_pokemon.parse_dex_ranges(collect_trades.CURATED_RETURN_TIER1_DEX)
            | clear_pokemon.parse_dex_ranges(collect_trades.CURATED_RETURN_TIER2_DEX)
        ) - {837, 838, 839, 969, 970}
        combined_dex: set[int] = set()
        for q in queries:
            self.assertFalse(q.startswith("!#"))
            self.assertLessEqual(len(q), 480)
            toks = q.split("&")
            for req in (
                "!98Plus", "!dontrade", "!xs", "!xl", "!xxs", "!xxl",
                "!buddy1-5", "!purified", "!year2016-2021", "!837-839", "!969-970", "!#",
            ):
                self.assertIn(req, toks)
            self.assertNotIn("!mega1-", toks)
            chunk_dex = clear_pokemon.parse_dex_ranges(q.rsplit("&", 1)[-1])
            self.assertTrue(combined_dex.isdisjoint(chunk_dex))
            combined_dex |= chunk_dex

        self.assertEqual(combined_dex, expected_union)
        # Complete useful evolution chains must be present (Beldum #374-376, Gastly #92-94, Machop #66-68).
        for wanted in (66, 67, 68, 92, 93, 94, 374, 375, 376):
            self.assertIn(wanted, combined_dex)
        # Excluded & trash fodder must NEVER be present (Rolycoly 837-839, Glimmet 969-970, Squawkabilly 931, Caterpie 10-12).
        for forbidden in (10, 11, 12, 837, 838, 839, 931, 969, 970):
            self.assertNotIn(forbidden, combined_dex)

    def test_build_return_queries_respects_exclude_dex_tiers_and_custom_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_cfg = Path(tmp) / "does-not-exist.json"
            queries = collect_trades.build_return_queries(
                missing_cfg,
                exclude_dex=[374, 375, 376, 837, 838, 839, 969, 970],
            )
            self.assertGreaterEqual(len(queries), 4)
            combined = set()
            for q in queries:
                combined |= clear_pokemon.parse_dex_ranges(q.rsplit("&", 1)[-1])
            for d in (374, 375, 376, 969, 970):
                self.assertNotIn(d, combined)

            custom_tiers = collect_trades.build_return_queries(
                missing_cfg,
                return_dex_tiers=["374-376,969-970", "92-94,374"],
            )
            self.assertEqual(len(custom_tiers), 2)
            self.assertTrue(custom_tiers[0].endswith("&!#&374-376"))
            self.assertTrue(custom_tiers[1].endswith("&!#&92-94"))

            custom = collect_trades.build_return_queries(missing_cfg, custom_return_query="distance100-")
            self.assertEqual(len(custom), 1)
            self.assertTrue(custom[0].endswith("&!#&distance100-"))

    def test_plan_collection_prioritizes_rolycoly_over_size_trophies_and_groups_by_donor(self) -> None:
        targets = collect_trades.resolve_targets({})
        counts = {
            "ios-one": {"rolycoly": 4, "xxs-xxl": 60},
            "android-one": {"rolycoly": 5, "xxs-xxl": 50},
            "android-two": {"rolycoly": 3, "xxs-xxl": 40},
        }
        batches = collect_trades.plan_collection(
            counts,
            ["ios-one", "android-one", "android-two"],
            targets,
            "android-three",
            budget=100,
        )
        # All 12 Rolycoly across all 3 donors must be allocated before XXS/XXL fills remaining 88.
        roly_total = sum(b.count for b in batches if b.target == "rolycoly")
        size_total = sum(b.count for b in batches if b.target == "xxs-xxl")
        self.assertEqual(roly_total, 12)
        self.assertEqual(size_total, 88)
        self.assertEqual(sum(b.count for b in batches), 100)
        # Batches are grouped by donor so each donor is only opened once.
        donors_order = [b.donor for b in batches]
        self.assertEqual(donors_order, ["ios-one", "ios-one", "android-one", "android-one", "android-two"])

    def test_plan_routes_cross_host_donors_to_receiver_without_error(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
                "android-one": SimpleNamespace(name="android-one", enabled=True, platform="android"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        owners = {
            "android-three": "host-a",
            "ios-one": "host-a",
            "android-one": "host-b",
            "android-two": "host-b",
        }
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            clear_cfg = Path(tmp) / "clear-pokemon.json"
            clear_cfg.write_text(json.dumps({"safe_dex_ranges": "10-12,960-961"}))
            collect_cfg = Path(tmp) / "collect-trades.json"
            collect_cfg.write_text(json.dumps({"receiver": "android-three", "clear_config": str(clear_cfg)}))
            with mock.patch.object(collect_trades.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(collect_trades.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(collect_trades.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(collect_trades.starter_trade, "trades_done_today", return_value=5), \
                    mock.patch("sys.stdout", out):
                code = public_collect.main(["--config-file", str(collect_cfg), "--targets-only", "--plan"])
        self.assertEqual(code, 0)
        rendered = out.getvalue()
        self.assertIn("android-three [host-a]", rendered)
        self.assertIn("android-one [host-b]", rendered)
        expected_queries = collect_trades.build_return_queries()
        self.assertIn(f"return queries: {len(expected_queries)}", rendered)
        self.assertIn("95/100", rendered)


class PickerSearchAndLiveFlowTests(unittest.TestCase):
    def setUp(self):
        # These regressions exercise the legacy executor with scripted picker
        # counts. Real reserve decisions are covered by test_collector_day.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(collect_trades, "ORDINARY_RESERVE", 0).start()

    def test_enter_picker_query_android_skips_clear_when_empty_and_uses_bursts(self) -> None:
        dex = list(range(10, 260, 2))
        term = "&".join(collect_trades.REQUIRED_RETURN_FILTERS) + "&" + clear_pokemon.format_dex_ranges(dex)
        self.assertGreater(len(term), clear_pokemon.ANDROID_INPUT_BURST * 2)
        adb_calls: list[tuple[str, ...]] = []
        # First _android_edit_text sees "Search" (placeholder), then partial, then full text.
        edit_texts = iter(["Search", term[:180], term])

        class FakeTarget:
            platform = "android"
            label = "android-three"
            front_package = None
            device = None

            def _adb(self, *args: str):
                adb_calls.append(tuple(args))
                return SimpleNamespace(stdout="", returncode=0, stderr="")

        phone = starter.Phone("android-three", FakeTarget(), Path("/tmp"))
        img = Image.new("RGB", (720, 1600), (240, 250, 240))
        boxes = [_box("POKEMON", 290, 120), _box("(150)", 320, 180), _box("Search", 280, 260, 160, 40)]

        async def fake_tap_box(_box_arg, _image=None):
            return None

        async def fake_edit_text(_phone):
            return next(edit_texts)

        async def noop(_seconds):
            return None

        with mock.patch.object(phone, "tap_box", side_effect=fake_tap_box), \
                mock.patch.object(phone, "read", new_callable=mock.AsyncMock, return_value=(img, [_box("Favorite", 10, 330)])), \
                mock.patch.object(clear_pokemon, "_android_edit_text", side_effect=fake_edit_text), \
                mock.patch.object(collect_trades.trade_local, "clear_field", new_callable=mock.AsyncMock) as clear_mock, \
                mock.patch.object(collect_trades.asyncio, "sleep", side_effect=noop):
            asyncio.run(collect_trades._enter_picker_query(phone, term, img, boxes))

        clear_mock.assert_not_awaited()
        input_calls = [c for c in adb_calls if len(c) >= 2 and c[0] == "shell" and c[1].startswith("input text ")]
        expected_bursts = (len(term) + clear_pokemon.ANDROID_INPUT_BURST - 1) // clear_pokemon.ANDROID_INPUT_BURST
        self.assertEqual(len(input_calls), expected_bursts)
        self.assertEqual(adb_calls[-1], ("shell", "input keyevent 66"))

    def test_choose_filtered_tile_returns_count_and_handles_transient_zero(self) -> None:
        img = Image.new("RGB", (720, 1600), (240, 250, 240))
        term = collect_trades.build_donor_query("rolycoly")
        visible_prefix = term[:30]
        empty_boxes = [_box("POKEMON", 290, 120), _box("(0)", 320, 180), _box(visible_prefix, 200, 260)]
        transient_boxes = [_box("POKEMON", 290, 120), _box("(0)", 320, 180), _box(visible_prefix, 200, 260)]
        ready_boxes = [
            _box("POKEMON", 290, 120),
            _box("(3)", 320, 180),
            _box(visible_prefix, 200, 260),
            _box("CP 412", 140, 520),
            _box("Rolycoly", 140, 640),
        ]
        detail_boxes = [_box("NEXT", 300, 1200)]

        phone = SimpleNamespace(
            name="android-one",
            ios=False,
            save=lambda _img, label: f"/tmp/{label}.png",
            read=mock.AsyncMock(side_effect=[
                (img, transient_boxes),
                (img, ready_boxes),
                (img, detail_boxes),
            ]),
            tap=mock.AsyncMock(),
        )
        with mock.patch.object(collect_trades, "_enter_picker_query", new_callable=mock.AsyncMock), \
                mock.patch.object(collect_trades.trade_local, "POLL", 0):
            count = asyncio.run(collect_trades.choose_filtered_tile(phone, term, "rolycoly"))
        self.assertEqual(count, 3)

        # True (0) after transient wait raises TradeOver.
        phone_zero = SimpleNamespace(
            name="android-one",
            ios=False,
            save=lambda _img, label: f"/tmp/{label}.png",
            read=mock.AsyncMock(side_effect=[(img, empty_boxes), (img, empty_boxes)]),
            tap=mock.AsyncMock(),
        )

        async def fast_settle(_phone, _pred, timeout=0):
            return False, img, empty_boxes

        async def enter_ok(_phone, _term, _img, _boxes):
            return None

        calls = 0

        async def settle_side(p, pred, timeout=0):
            nonlocal calls
            calls += 1
            if calls == 1:
                return True, img, empty_boxes
            return False, img, empty_boxes

        with mock.patch.object(collect_trades, "_enter_picker_query", side_effect=enter_ok), \
                mock.patch.object(collect_trades.trade_local, "settle", side_effect=settle_side):
            with self.assertRaises(collect_trades.trade_local.TradeOver):
                asyncio.run(collect_trades.choose_filtered_tile(phone_zero, term, "rolycoly"))

    def test_live_run_skips_pre_scan_orders_rolycoly_first_skips_lucky_and_falls_back_return_query(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
                "android-one": SimpleNamespace(name="android-one", enabled=True, platform="android"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        owners = {
            "android-three": "host-a",
            "ios-one": "host-a",
            "android-one": "host-b",
            "android-two": "host-b",
        }
        trade_events: list[tuple[str, str, str]] = []
        recorded: list[tuple[list[str], str]] = []

        @contextlib.asynccontextmanager
        async def fake_open_fleet_phone(name, *_args, **_kwargs):
            yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

        async def fake_trainer_name(phone):
            return f"trainer-{phone.name}"

        # Scenario:
        # - ios-one: has 1 rolycoly (count=1 -> stops immediately without extra empty trade), 1 xxs-xxl.
        # - android-one: LuckyAvailable on rolycoly -> skipped for BOTH rolycoly and xxs-xxl.
        # - android-two: 2 rolycoly (1st exhausts curated tier 1 -> falls back to curated tier 2), 0 xxs-xxl.
        roly_remaining = {"ios-one": 1, "android-two": 2}
        size_remaining = {"ios-one": 1, "android-two": 0}
        ret_query0_exhausted = False
        expected_queries = collect_trades.build_return_queries()
        expected_tier1 = expected_queries[0]
        expected_next = expected_queries[1]

        async def fake_trade_once(donor_phone, recv_phone, _names, terms, _species, choosers=None, on_confirmed=None, **_kwargs):
            nonlocal ret_query0_exhausted
            donor = donor_phone.name
            target = "rolycoly" if terms[donor].endswith("&rolycoly") else "xxs-xxl"
            if donor == "android-one":
                raise collect_trades.trade_local.LuckyAvailable("lucky friend waiting")
            pool = roly_remaining if target == "rolycoly" else size_remaining
            avail = pool.get(donor, 0)
            if avail <= 0:
                raise collect_trades.trade_local.TradeOver(f"no {target} left")

            chosen_ret: list[str] = []

            async def fake_choose(_p, term, _label, **_kw):
                nonlocal ret_query0_exhausted
                if _p is donor_phone:
                    return avail
                if not ret_query0_exhausted and term == expected_tier1:
                    ret_query0_exhausted = True
                    raise collect_trades.trade_local.TradeOver("no return-1 left")
                chosen_ret.append(term)
                return 25

            with mock.patch.object(collect_trades, "choose_filtered_tile", side_effect=fake_choose):
                await choosers[donor](donor_phone)
                await choosers[recv_phone.name](recv_phone)
            pool[donor] = avail - 1
            on_confirmed({})
            trade_events.append((donor, target, chosen_ret[-1]))

        with tempfile.TemporaryDirectory() as tmp:
            clear_cfg = Path(tmp) / "clear-pokemon.json"
            clear_cfg.write_text(json.dumps({"safe_dex_ranges": "10-12,960-961"}))
            collect_cfg = Path(tmp) / "collect-trades.json"
            collect_cfg.write_text(json.dumps({"receiver": "android-three", "clear_config": str(clear_cfg)}))

            with mock.patch.object(collect_trades.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(collect_trades.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(collect_trades.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(collect_trades.starter_trade, "trades_done_today", return_value=0), \
                    mock.patch.object(
                        collect_trades.starter_trade,
                        "record_trade",
                        side_effect=lambda pair, _g, key, _r, **_kw: recorded.append((list(pair), key)),
                    ), \
                    mock.patch.object(collect_trades, "_scan_fleet") as scan_mock, \
                    mock.patch.object(collect_trades.remote_target, "open_fleet_phone", side_effect=fake_open_fleet_phone), \
                    mock.patch.object(collect_trades.trade_local, "trainer_name", side_effect=fake_trainer_name), \
                    mock.patch.object(collect_trades.trade_local, "trade_once", side_effect=fake_trade_once):
                code = public_collect.main(["--config-file", str(collect_cfg), "--targets-only"])

        self.assertEqual(code, 0)
        scan_mock.assert_not_called()
        # All Rolycoly across android-two and ios-one ran BEFORE xxs-xxl!
        targets_order = [t for _, t, _ in trade_events]
        self.assertEqual(targets_order, ["rolycoly", "rolycoly", "rolycoly", "xxs-xxl"])
        # Exact 4 trades recorded: 2 android-two rolycoly + 1 ios-one rolycoly + 1 ios-one xxs-xxl.
        self.assertEqual(len(recorded), 4)
        # First trade fell back from return-1 to return-2, and subsequent trades used return-2 directly.
        self.assertEqual(trade_events[0][2], expected_next)
        self.assertEqual(trade_events[-1][2], expected_next)

    def test_open_partner_ios_clears_without_delete_key_and_ignores_search_box_rows(self) -> None:
        img = Image.new("RGB", (750, 1334), (255, 255, 255))
        # Screen sequence:
        # 1) Initial check (not yet on profile)
        # 2) Friends list showing stale search text instead of "SEARCH" placeholder
        # 3) Friends list after tapping teal X -> now shows "SEARCH"
        # 4) After search: OCR search box has concatenated/I-slip text at y=311 (0.233h); actual friend row at y=470
        # 5) After tapping friend row avatar -> partner profile with LOCAL TRADE
        initial_boxes = [_box("ME", 120, 80)]
        stale_friends = [_box("ME", 120, 80), _box("FRIENDS", 320, 80), _box("alphatrainold", 180, 300)]
        clean_friends = [_box("ME", 120, 80), _box("FRIENDS", 320, 80), _box("SEARCH", 180, 300)]
        after_search = [
            _box("FRIENDS", 320, 80),
            _box("alphatraalphatrain", 200, 300),  # search bar (~0.225h): must NOT be tapped as row!
            _box("AlphaTra|n", 260, 460),          # real friend row below search floor (with l->I OCR slip)
        ]
        profile_boxes = [_box("AlphaTrain", 260, 180), _box("LOCAL", 300, 900)]
        reads = iter([
            (img, initial_boxes),
            (img, stale_friends),
            (img, clean_friends),
            (img, after_search),
            (img, profile_boxes),
            (img, profile_boxes),
        ])
        tapped: list[list[int]] = []

        async def fake_read():
            return next(reads, (img, profile_boxes))

        async def fake_tap(point, _img=None):
            tapped.append(list(point))

        async def fake_tap_box(b, _img=None):
            tapped.append([b.center_x, b.center_y])

        hide_mock = mock.AsyncMock()
        phone = SimpleNamespace(
            name="ios-one",
            ios=True,
            target=SimpleNamespace(device=SimpleNamespace(hide_keyboard=hide_mock)),
            save=lambda _i, label: f"/tmp/{label}.png",
            say=lambda _msg: None,
            read=fake_read,
            tap=fake_tap,
            tap_box=fake_tap_box,
        )
        with mock.patch.object(trade_local, "walk_to_friends", new_callable=mock.AsyncMock), \
                mock.patch.object(trade_local, "clear_field", new_callable=mock.AsyncMock) as clear_field_mock, \
                mock.patch.object(trade_local, "_clear_ios_native_text", new_callable=mock.AsyncMock) as native_clear_mock, \
                mock.patch.object(trade_local, "type_text", new_callable=mock.AsyncMock) as type_mock, \
                mock.patch.object(trade_local, "submit", new_callable=mock.AsyncMock), \
                mock.patch.object(trade_local.asyncio, "sleep", new_callable=mock.AsyncMock):
            asyncio.run(trade_local.open_partner(phone, "AlphaTrain"))

        clear_field_mock.assert_not_awaited()
        native_clear_mock.assert_awaited()
        type_mock.assert_awaited_once()
        # First tap cleared stale friends search box at FRIENDS_SEARCH_CLEAR_X (~652),
        # and the avatar tap targeted row y=460 (> 460), NEVER y=300!
        self.assertEqual(tapped[0][0], int(750 * trade_local.FRIENDS_SEARCH_CLEAR_X))
        self.assertGreater(tapped[-1][1], 460)

    def test_cancel_ios_taps_search_cancel_instead_of_hide_keyboard_and_does_not_spin(self) -> None:
        img = Image.new("RGB", (750, 1334), (255, 255, 255))
        tapped: list[list[int]] = []
        hide_mock = mock.AsyncMock()
        reads = iter([
            (img, [_box("PICKER", 200, 100)]),
            (img, [_box("PICKER", 200, 100)]),
            (img, [_box("LOCAL", 300, 900)]),
        ])
        kb_states = iter([True, True, True, False, False])

        async def fake_read():
            return next(reads, (img, [_box("LOCAL", 300, 900)]))

        async def fake_tap(point, _img=None):
            tapped.append(list(point))

        phone = SimpleNamespace(
            name="ios-one",
            ios=True,
            target=SimpleNamespace(device=SimpleNamespace(hide_keyboard=hide_mock)),
            ios_keyboard=mock.AsyncMock(side_effect=lambda: next(kb_states, False)),
            read=fake_read,
            tap=fake_tap,
            tap_box=mock.AsyncMock(),
        )
        with mock.patch.object(trade_local.asyncio, "sleep", new_callable=mock.AsyncMock):
            asyncio.run(trade_local.cancel(phone))

        hide_mock.assert_not_awaited()
        cancel_pt = [int(750 * starter.SEARCH_CANCEL[0]), int(1334 * starter.SEARCH_CANCEL[1])]
        door_pt = [int(750 * trade_local.DOOR[0]), int(1334 * trade_local.DOOR[1])]
        self.assertEqual(tapped[0], cancel_pt)
        self.assertIn(door_pt, tapped)

    def test_finish_dismisses_new_weight_and_height_record_banners(self) -> None:
        img = Image.new("RGB", (1316, 2560), (13, 188, 241))
        tapped: list[list[int]] = []
        reads = iter([
            (img, [_box("CP279", 500, 120), _box("New Weight Record", 380, 1500)]),
            (img, [_box("CP279", 500, 120), _box("New Height Record", 380, 1500)]),
            (img, [_box("AlphaTrain", 260, 180), _box("LOCAL", 300, 900)]),
        ])

        async def fake_read():
            return next(reads, (img, [_box("AlphaTrain", 260, 180), _box("LOCAL", 300, 900)]))

        async def fake_tap(point, _img=None):
            tapped.append(list(point))

        phone = SimpleNamespace(
            name="android-three",
            ios=False,
            save=lambda _i, label: f"/tmp/{label}.png",
            read=fake_read,
            tap=fake_tap,
            tap_box=mock.AsyncMock(),
        )
        with mock.patch.object(trade_local, "close_button", return_value=None), \
                mock.patch.object(trade_local.asyncio, "sleep", new_callable=mock.AsyncMock):
            seen = asyncio.run(trade_local.finish(phone, "AlphaTrain"))

        self.assertEqual(seen, "CP279")
        expected = [int(1316 * trade_local.RECORD_CLOSE[0]), int(2560 * trade_local.RECORD_CLOSE[1])]
        self.assertEqual(tapped, [expected, expected])

    def test_press_refuses_buddy_warning_dialog_and_taps_no(self) -> None:
        img = Image.new("RGB", (1316, 2560), (255, 255, 255))
        tapped: list[str] = []
        warning_boxes = [
            _box("Are you sure you want to trade this Pokemon?", 650, 1100),
            _box("Buddy Level will be reset", 650, 1300),
            _box("YES", 650, 1500),
            _box("NO", 650, 1750),
            _box("NEXT", 650, 2100),
        ]
        reads = iter([
            (img, [_box("NEXT", 650, 2100)]),
            (img, warning_boxes),
        ])

        async def fake_read():
            return next(reads, (img, warning_boxes))

        async def fake_tap_box(b, _img=None):
            tapped.append(b.text)

        phone = SimpleNamespace(
            name="android-three",
            ios=False,
            save=lambda _i, label: f"/tmp/{label}.png",
            read=fake_read,
            tap=mock.AsyncMock(),
            tap_box=fake_tap_box,
        )
        with mock.patch.object(trade_local, "POLL", 0), \
                mock.patch.object(trade_local.asyncio, "sleep", new_callable=mock.AsyncMock):
            with self.assertRaises(trade_local.TradeRefused) as ctx:
                asyncio.run(trade_local.press(phone, "next", trade_local.confirm_screen))
        self.assertTrue(str(ctx.exception).startswith("android-three:"))
        self.assertEqual(tapped, ["NEXT", "NO"])

    def test_live_run_skips_receiver_slot_without_burning_donor_skip_index(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
            }
        )
        owners = {"android-three": "host-a", "ios-one": "host-a"}
        seen_skips: list[tuple[int, int]] = []
        recorded: list[str] = []

        @contextlib.asynccontextmanager
        async def fake_open_fleet_phone(name, *_args, **_kwargs):
            yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

        async def fake_trainer_name(phone):
            return f"trainer-{phone.name}"

        first_attempt = True

        async def fake_trade_once(donor_phone, recv_phone, _names, terms, _species, choosers=None, on_confirmed=None, **_kwargs):
            nonlocal first_attempt
            donor_skips: list[int] = []
            recv_skips: list[int] = []

            async def fake_choose(p, _term, _label, skip_index=0):
                nonlocal first_attempt
                if p is donor_phone:
                    donor_skips.append(skip_index)
                    return 2
                recv_skips.append(skip_index)
                if first_attempt and skip_index == 0:
                    first_attempt = False
                    raise collect_trades.trade_local.TradeRefused("android-three: trade warning dialog refused")
                return 20

            with mock.patch.object(collect_trades, "choose_filtered_tile", side_effect=fake_choose):
                await choosers[donor_phone.name](donor_phone)
                await choosers[recv_phone.name](recv_phone)
            on_confirmed({})
            seen_skips.append((donor_skips[-1], recv_skips[-1]))

        with tempfile.TemporaryDirectory() as tmp:
            collect_cfg = Path(tmp) / "collect-trades.json"
            collect_cfg.write_text(json.dumps({"receiver": "android-three"}))
            with mock.patch.object(collect_trades.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(collect_trades.fleet_entrypoint, "machine_configs", return_value={"host-a": {}}), \
                    mock.patch.object(collect_trades.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(collect_trades.starter_trade, "trades_done_today", return_value=0), \
                    mock.patch.object(
                        collect_trades.starter_trade,
                        "record_trade",
                        side_effect=lambda _pair, _g, key, _r, **_kw: recorded.append(key),
                    ), \
                    mock.patch.object(collect_trades.remote_target, "open_fleet_phone", side_effect=fake_open_fleet_phone), \
                    mock.patch.object(collect_trades.trade_local, "trainer_name", side_effect=fake_trainer_name), \
                    mock.patch.object(collect_trades.trade_local, "trade_once", side_effect=fake_trade_once):
                code = public_collect.main(["--config-file", str(collect_cfg), "--size-only", "--trades", "1"])

        self.assertEqual(code, 0)
        self.assertEqual(len(recorded), 1)
        # Receiver skipped slot 0 -> 1 while donor stayed on slot 0.
        self.assertEqual(seen_skips, [(0, 1)])

    def test_finish_handles_line_wrapped_special_trade_modal(self) -> None:
        img = Image.new("RGB", (720, 1600), (80, 140, 120))
        wrapped = [
            _box("Do you want to make a Special", 360, 620),
            _box("Trade?", 360, 670),
            _box("You have 3 Special Trades remaining.", 360, 740),
            _box("YES", 360, 880),
            _box("NO", 360, 1020),
        ]
        # special_ok=False taps NO and raises TradeRefused.
        tapped_false: list[str] = []
        phone_false = SimpleNamespace(
            name="android-one",
            ios=False,
            read=mock.AsyncMock(return_value=(img, wrapped)),
            tap=mock.AsyncMock(),
            tap_box=mock.AsyncMock(side_effect=lambda b, _i=None: tapped_false.append(b.text)),
        )
        with mock.patch.object(trade_local.asyncio, "sleep", new_callable=mock.AsyncMock):
            with self.assertRaises(trade_local.TradeRefused):
                asyncio.run(trade_local.finish(phone_false, "AlphaTrain", special_ok=False))
        self.assertEqual(tapped_false, ["NO"])

        # special_ok=True taps YES and finishes on profile_of(partner).
        tapped_true: list[str] = []
        reads = iter([
            (img, wrapped),
            (img, [_box("AlphaTrain", 260, 180), _box("LOCAL", 300, 900)]),
        ])
        phone_true = SimpleNamespace(
            name="android-one",
            ios=False,
            read=mock.AsyncMock(side_effect=lambda: next(reads)),
            tap=mock.AsyncMock(),
            tap_box=mock.AsyncMock(side_effect=lambda b, _i=None: tapped_true.append(b.text)),
        )
        with mock.patch.object(trade_local.asyncio, "sleep", new_callable=mock.AsyncMock):
            asyncio.run(trade_local.finish(phone_true, "AlphaTrain", special_ok=True))
        self.assertEqual(tapped_true, ["YES"])

    def test_unavailable_ios_donor_is_skipped_and_other_donors_still_trade(self) -> None:
        fake_fleet = SimpleNamespace(
            devices={
                "android-three": SimpleNamespace(name="android-three", enabled=True, platform="android"),
                "ios-one": SimpleNamespace(name="ios-one", enabled=True, platform="ios"),
                "android-two": SimpleNamespace(name="android-two", enabled=True, platform="android"),
            }
        )
        owners = {"android-three": "host-a", "ios-one": "host-a", "android-two": "host-b"}
        recorded: list[tuple[list[str], str]] = []

        @contextlib.asynccontextmanager
        async def fake_open_fleet_phone(name, *_args, **_kwargs):
            if name == "ios-one":
                raise collect_trades.gbl_ios.IOSGBLError("screen is off")
            yield SimpleNamespace(name=name, go_home=mock.AsyncMock())

        async def fake_trade_once(donor_phone, recv_phone, _names, _terms, _species, choosers=None, on_confirmed=None, **_kwargs):
            await choosers[donor_phone.name](donor_phone)
            await choosers[recv_phone.name](recv_phone)
            on_confirmed({})

        with tempfile.TemporaryDirectory() as tmp:
            collect_cfg = Path(tmp) / "collect-trades.json"
            collect_cfg.write_text(json.dumps({"receiver": "android-three"}))
            with mock.patch.object(collect_trades.pokemon_fleet, "load_fleet", return_value=fake_fleet), \
                    mock.patch.object(collect_trades.fleet_entrypoint, "machine_configs", return_value={"host-a": {}, "host-b": {}}), \
                    mock.patch.object(collect_trades.remote_target, "discover_attached_devices", return_value=(owners, {})), \
                    mock.patch.object(collect_trades.starter_trade, "trades_done_today", return_value=0), \
                    mock.patch.object(
                        collect_trades.starter_trade,
                        "record_trade",
                        side_effect=lambda pair, _g, key, _r, **_kw: recorded.append((list(pair), key)),
                    ), \
                    mock.patch.object(collect_trades.remote_target, "open_fleet_phone", side_effect=fake_open_fleet_phone), \
                    mock.patch.object(collect_trades.trade_local, "trainer_name", new_callable=mock.AsyncMock, side_effect=lambda p: f"t-{p.name}"), \
                    mock.patch.object(collect_trades, "choose_filtered_tile", new_callable=mock.AsyncMock, return_value=1), \
                    mock.patch.object(collect_trades.trade_local, "trade_once", side_effect=fake_trade_once):
                code = public_collect.main(["--config-file", str(collect_cfg), "--size-only", "--trades", "1"])

        self.assertEqual(code, 0)
        self.assertEqual(recorded, [(["android-two", "android-three"], "xxs-xxl")])

    def test_unreadable_preapplied_picker_count_does_not_exhaust_the_offer(self) -> None:
        phone = SimpleNamespace(
            name="android-one",
            read=mock.AsyncMock(return_value=(Image.new("RGB", (750, 1334)), [])),
            tap=mock.AsyncMock(),
            save=mock.Mock(return_value=Path("unverified.png")),
        )

        with self.assertRaisesRegex(starter.StepFailed, "unreadable picker count") as failure:
            asyncio.run(collect_trades.choose_filtered_tile(phone, "25", "Pikachu", preapplied=True))

        self.assertNotIsInstance(failure.exception, trade_local.TradeOver)
        phone.tap.assert_not_awaited()

    def test_choose_filtered_tile_raises_step_failed_not_trade_over_when_trade_expired(self) -> None:
        img = Image.new("RGB", (720, 1600), (240, 250, 240))
        term = "!98Plus&!dontrade&age0-30"
        ready_boxes = [
            _box("POKEMON", 290, 120),
            _box("(5)", 320, 180),
            _box(term, 200, 260),
            _box("CP 412", 140, 520),
        ]
        expired_boxes = [
            _box("Trade expired.", 360, 620),
            _box("This trade was canceled.", 360, 720),
            _box("OK", 360, 880),
        ]
        reads = iter([(img, ready_boxes), (img, expired_boxes)])
        tapped: list[str] = []
        phone = SimpleNamespace(
            name="android-three",
            ios=False,
            read=mock.AsyncMock(side_effect=lambda: next(reads)),
            tap=mock.AsyncMock(),
            tap_box=mock.AsyncMock(side_effect=lambda b, _i=None: tapped.append(b.text)),
            save=lambda _i, label: f"/tmp/{label}.png",
        )
        with mock.patch.object(collect_trades.trade_local, "POLL", 0):
            with self.assertRaises(starter.StepFailed) as ctx:
                asyncio.run(collect_trades.choose_filtered_tile(phone, term, "return-2"))
        self.assertNotIsInstance(ctx.exception, trade_local.TradeOver)
        self.assertIn("expired", str(ctx.exception).lower())
        self.assertEqual(tapped, ["OK"])

    def test_choose_filtered_tile_respects_keep_last_seen_and_allows_visible_duplicates(self) -> None:
        img = Image.new("RGB", (720, 1600), (240, 250, 240))
        term = "!98Plus&!dontrade&!year2016-2021"
        # Tile 0: lone "Beldum" (already traded once -> keep_last_seen skips it).
        # Tile 1: "Gastly" (not in keep_last_seen -> selected).
        grid_boxes = [
            _box("POKEMON", 290, 120),
            _box("(2)", 320, 180),
            _box(term, 200, 260),
            _box("CP 412", 140, 520),
            _box("Beldum", 140, 620),
            _box("CP 518", 380, 520),
            _box("Gastly", 380, 620),
        ]
        detail_boxes = [_box("NEXT", 300, 1200)]
        tapped_pts: list[list[int]] = []

        async def fake_tap(pt, _img=None):
            tapped_pts.append(list(pt))

        phone_one = SimpleNamespace(
            name="android-three",
            ios=False,
            read=mock.AsyncMock(side_effect=[(img, grid_boxes), (img, detail_boxes)]),
            tap=fake_tap,
            save=lambda _i, label: f"/tmp/{label}.png",
        )
        picked: list[str] = []
        with mock.patch.object(collect_trades.trade_local, "POLL", 0):
            cnt = asyncio.run(
                collect_trades.choose_filtered_tile(
                    phone_one,
                    term,
                    "return-2",
                    keep_last_seen={"beldum"},
                    picked_species=picked,
                )
            )
        self.assertEqual(cnt, 2)
        self.assertEqual(picked, ["gastly"])
        self.assertEqual(tapped_pts[0][0], grid_boxes[5].center_x)

        # When 2+ Beldum copies are visible, Beldum CAN be picked safely.
        dup_grid = [
            _box("POKEMON", 290, 120),
            _box("(2)", 320, 180),
            _box(term, 200, 260),
            _box("CP 412", 140, 520),
            _box("Beldum", 140, 620),
            _box("CP 518", 380, 520),
            _box("Beldum", 380, 620),
        ]
        tapped_dup: list[list[int]] = []
        phone_dup = SimpleNamespace(
            name="android-three",
            ios=False,
            read=mock.AsyncMock(side_effect=[(img, dup_grid), (img, detail_boxes)]),
            tap=mock.AsyncMock(side_effect=lambda pt, _img=None: tapped_dup.append(list(pt))),
            save=lambda _i, label: f"/tmp/{label}.png",
        )
        picked_dup: list[str] = []
        with mock.patch.object(collect_trades.trade_local, "POLL", 0):
            asyncio.run(
                collect_trades.choose_filtered_tile(
                    phone_dup,
                    term,
                    "return-2",
                    keep_last_seen={"beldum"},
                    picked_species=picked_dup,
                )
            )
        self.assertEqual(picked_dup, ["beldum"])
        self.assertEqual(tapped_dup[0][0], dup_grid[3].center_x)

    def test_scan_donor_phone_returns_zeroes_when_optional_donor_unavailable(self) -> None:
        targets = collect_trades.resolve_targets({}, size_only=True)

        @contextlib.asynccontextmanager
        async def failing_open(*_args, **_kwargs):
            raise collect_trades.gbl_ios.IOSGBLError("screen is off")
            yield None

        with mock.patch.object(
            collect_trades.remote_target,
            "open_fleet_phone",
            side_effect=failing_open,
        ):
            counts = asyncio.run(
                collect_trades._scan_donor_phone(
                    "ios-one",
                    "host-a",
                    {"host-a": {}},
                    SimpleNamespace(devices={}),
                    targets,
                    Path("/tmp"),
                    True,
                )
            )
        self.assertEqual(counts, {"xxs-xxl": 0})

    def test_trade_warning_catches_mega_level_and_max_move_prompts(self) -> None:
        self.assertTrue(trade_local.trade_warning([_box("Mega Level will be reset", 300, 600)]))
        self.assertTrue(trade_local.trade_warning([_box("Max Move levels will be reset", 300, 600)]))
        self.assertFalse(trade_local.trade_warning([_box("Do you want to trade?", 300, 600)]))


if __name__ == "__main__":
    unittest.main()
