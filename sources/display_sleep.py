"""Put an idle fleet phone to sleep and verify its physical display state."""
from __future__ import annotations

import asyncio
import re
import subprocess
import time

from . import gbl_home_recovery, ios_wda_cleanup, pokemon_fleet


def android(spec, *args):
    return subprocess.check_output([pokemon_fleet.default_adb_binary(), "-s", spec.identifier, "shell", *args],
                                   text=True, timeout=15)


def screen_on(spec):
    if spec.platform == "ios":
        return ios_wda_cleanup.display_backlight(spec.identifier)
    power = android(spec, "dumpsys", "power")
    state = re.search(r"(?m)^\s*Display Power: state=(ON|OFF|DOZE(?:_SUSPEND)?)\b", power)
    if state:
        return state[1] != "OFF"
    wake = re.search(r"(?m)^\s*mWakefulness=(Awake|Asleep)\s*$", power)
    if wake:
        return wake[1] == "Awake"
    # Android can report Dozing after physically switching the built-in panel off.
    display = android(spec, "dumpsys", "display")
    panel = re.search(r"mBaseDisplayInfo=[^\n]*?\bdisplayId 0,[^\n]*?\bcommittedState (ON|OFF|DOZE(?:_SUSPEND)?)\b", display)
    return (panel[1] != "OFF") if panel else None


async def wait_until_off(spec):
    until = time.monotonic() + 20
    while await asyncio.to_thread(screen_on, spec) is not False:
        if time.monotonic() >= until:
            raise RuntimeError(f"{spec.name}: could not verify display off")
        await asyncio.sleep(1)


async def lock_display(spec, target):
    if spec.platform == "android":
        await asyncio.to_thread(android, spec, "input", "keyevent", "KEYCODE_SLEEP")
    else:
        driver = target.device.driver
        try:
            await asyncio.to_thread(driver.lock)
        except Exception as exc:
            if "timed out while waiting until the screen gets locked" not in str(exc).casefold():
                raise
            lit = await asyncio.to_thread(screen_on, spec)
            if lit is False:
                return
            if lit is not True or await asyncio.to_thread(driver.is_locked):
                raise
            # A single Consumer Power press locks newer iPhones when the
            # WDA lock request times out. Only press on a lit, unlocked device.
            await asyncio.to_thread(driver.execute_script, "mobile: performIoHidEvent",
                                    {"page": 12, "usage": 48, "durationSeconds": .005})


async def sleep_phone(spec):
    with pokemon_fleet.acquire_device_locks([spec]):
        if await asyncio.to_thread(screen_on, spec) is not False:
            if spec.platform == "android":
                await lock_display(spec, None)
            else:
                target = await gbl_home_recovery.open_target(spec.name)
                try:
                    await lock_display(spec, target)
                finally:
                    await target.close()
        await wait_until_off(spec)
