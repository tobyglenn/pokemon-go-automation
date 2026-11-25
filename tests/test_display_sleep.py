import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sources import display_sleep, starter


class StarterDisplayTests(unittest.IsolatedAsyncioTestCase):
    def test_android_dozing_checks_the_physical_default_display(self):
        screens = ('mBaseDisplayInfo=DisplayInfo{"Main", displayId 0, state OFF, committedState OFF}\n'
                   'mOverrideDisplayInfo=DisplayInfo{"Main", displayId 0, state ON, committedState ON}')
        with patch.object(display_sleep, "android", side_effect=["mWakefulness=Dozing", screens]):
            self.assertIs(display_sleep.screen_on(SimpleNamespace(platform="android")), False)

    async def test_each_phone_sleeps_as_soon_as_its_own_final_work_finishes(self):
        fast_asleep = asyncio.Event()
        order = []

        async def finish_step(spec, args, steps):
            if spec.name == "slow":
                await asyncio.wait_for(fast_asleep.wait(), timeout=1)
            order.append(("finished", spec.name))
            return []

        async def lock(spec):
            order.append(("asleep", spec.name))
            if spec.name == "fast":
                fast_asleep.set()

        with patch.object(starter, "run_phone", side_effect=finish_step), patch.object(display_sleep, "sleep_phone", side_effect=lock):
            await asyncio.gather(*(starter.last_steps(SimpleNamespace(name=n), SimpleNamespace(dry_run=False),
                                                      {"buddy-again"}, True) for n in ("slow", "fast")))
        self.assertEqual(order, [("finished", "fast"), ("asleep", "fast"), ("finished", "slow"), ("asleep", "slow")])

    async def test_pair_only_run_sleeps_and_dry_or_buddy_only_workflows_skip_lock(self):
        spec = SimpleNamespace(name="phone")
        with patch.object(display_sleep, "sleep_phone", new_callable=AsyncMock) as lock:
            await starter.last_steps(spec, SimpleNamespace(dry_run=True), set(), True)
            await starter.last_steps(spec, SimpleNamespace(dry_run=False), set(), False)
            lock.assert_not_awaited()
            await starter.last_steps(spec, SimpleNamespace(dry_run=False), set(), True)
            lock.assert_awaited_once_with(spec)

    async def test_sleep_is_idempotent_and_verified_after_closing_ios_session(self):
        spec = SimpleNamespace(name="se", platform="ios")
        target = SimpleNamespace(device=SimpleNamespace(driver=SimpleNamespace(lock=Mock())), close=AsyncMock())
        with patch.object(display_sleep.pokemon_fleet, "acquire_device_locks", side_effect=lambda _: nullcontext()), \
             patch.object(display_sleep, "screen_on", side_effect=[True, False, False, False]), \
             patch.object(display_sleep.gbl_home_recovery, "open_target", new_callable=AsyncMock, return_value=target) as connect:
            await display_sleep.sleep_phone(spec)
            await display_sleep.sleep_phone(spec)
        connect.assert_awaited_once()
        target.device.driver.lock.assert_called_once_with()
        target.close.assert_awaited_once()

    async def test_ios_lock_timeout_uses_side_button_only_while_awake_and_unlocked(self):
        spec = SimpleNamespace(platform="ios", name="phone")
        driver = Mock()
        driver.lock.side_effect = RuntimeError("Timed out while waiting until the screen gets locked")
        driver.is_locked.return_value = False
        target = SimpleNamespace(device=SimpleNamespace(driver=driver))
        with patch.object(display_sleep, "screen_on", side_effect=[True, False, None, True]):
            await display_sleep.lock_display(spec, target)
            await display_sleep.lock_display(spec, target)
            with self.assertRaises(RuntimeError):
                await display_sleep.lock_display(spec, target)
            driver.is_locked.return_value = True
            with self.assertRaises(RuntimeError):
                await display_sleep.lock_display(spec, target)
        driver.execute_script.assert_called_once_with("mobile: performIoHidEvent",
                                                      {"page": 12, "usage": 48, "durationSeconds": .005})

    async def test_android_sleep_uses_non_toggling_keyevent(self):
        spec = SimpleNamespace(platform="android", name="phone")
        with patch.object(display_sleep.pokemon_fleet, "acquire_device_locks", return_value=nullcontext()), \
             patch.object(display_sleep, "screen_on", side_effect=[True, False]), \
             patch.object(display_sleep, "android") as adb:
            await display_sleep.sleep_phone(spec)
        adb.assert_called_once_with(spec, "input", "keyevent", "KEYCODE_SLEEP")

    async def test_night_skips_cleanup_with_an_unresolved_trade_and_still_sleeps(self):
        from sources import collector_night as night
        spec = SimpleNamespace(name="phone")
        fleet = SimpleNamespace(devices={spec.name: spec})
        with patch.object(night.fleet_entrypoint, "apply_local_environment"), \
             patch.object(night.pokemon_fleet, "load_fleet", return_value=fleet), \
             patch.object(night.clear_pokemon, "attached_devices", new_callable=AsyncMock, return_value=[spec]), \
             patch.object(night.clear_pokemon, "run_device", new_callable=AsyncMock) as clear, \
             patch.object(night.display_sleep, "sleep_phone", new_callable=AsyncMock) as lock:
            status = await night.worker(spec.name, {"pending": True}, plan=False)
        self.assertEqual(status, 1)
        clear.assert_not_awaited()
        lock.assert_awaited_once_with(spec)
