"""Free memory on each Android handset once, before a run starts.

sample-receiver (3.8GB) lost every award catch on 16 Nov 2026: the low-memory killer took
Pokemon GO -- the top, foreground app -- the moment a ball was flicked, while
Maps, YouTube, Android Auto, Play Store and the Google app kept respawning
behind it.  The ball left the bag, the catch never registered, and the game came
back on the award card with the same Pokemon.  With those stopped, the same
encounter caught on the first throw.

Called once per phone when catch, gbl and starter load.  Only apps the game
never needs are stopped; Phone, Messages and the keyboard are left alone (a
force-stopped app gets no broadcasts until it is opened again).  The game itself
is never touched.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from typing import Any, Iterable

BACKGROUND_PACKAGES = (
    "com.google.android.apps.maps",
    "com.google.android.youtube",
    "com.google.android.videos",
    "com.google.android.projection.gearhead",
    "com.google.android.apps.photos",
    "com.google.android.tts",
    "com.android.vending",
    "com.google.android.googlequicksearchbox",
    "com.android.chrome",
    "com.google.android.apps.wellbeing",
)

SHELL_TIMEOUT = 15
_AVAILABLE = re.compile(r"^MemAvailable:\s+(\d+)\s*kB", re.MULTILINE)
# Serials already freed by this run or the run that launched it (starter ->
# gbl_day -> gbl.py legs), so a day frees each phone once, not once per leg.
FREED_ENV = "POGO_MEMORY_FREED"
ENABLE_ENV = "POGO_FREE_BACKGROUND_MEMORY"


def _already_freed(serial: str) -> bool:
    return serial in os.environ.get(FREED_ENV, "").split(",")


def _mark_freed(serial: str) -> None:
    done = [value for value in os.environ.get(FREED_ENV, "").split(",") if value]
    os.environ[FREED_ENV] = ",".join([*done, serial])


def parse_available_kb(meminfo: str) -> int | None:
    match = _AVAILABLE.search(meminfo or "")
    return int(match.group(1)) if match else None


def free_commands() -> str:
    """One shell line: stop each background app, then drop cached processes."""
    stops = " && ".join(f"am force-stop {package}" for package in BACKGROUND_PACKAGES)
    return f"{stops} && am kill-all"


def _adb() -> str:
    from . import android_common

    return android_common.adb_binary()


def _shell(serial: str, command: str) -> str:
    result = subprocess.run(
        [_adb(), "-s", serial, "shell", command],
        capture_output=True, text=True, timeout=SHELL_TIMEOUT, check=True,
    )
    return result.stdout


def free_memory(serial: str, label: str | None = None) -> None:
    """Stop background apps only on opt-in, once per serial after success."""
    if os.environ.get(ENABLE_ENV) != "1" or _already_freed(serial):
        return
    name = (label or serial).strip()
    try:
        before = parse_available_kb(_shell(serial, "cat /proc/meminfo"))
        _shell(serial, free_commands())
        _mark_freed(serial)
        after = parse_available_kb(_shell(serial, "cat /proc/meminfo"))
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[{name}] Could not free memory: {exc}")
        return
    if before is not None and after is not None:
        print(f"[{name}] Freed memory: {before // 1024}MB -> {after // 1024}MB available")
    else:
        print(f"[{name}] Stopped background apps")


def free_memory_all(devices: Iterable[Any]) -> None:
    for device in devices:
        serial = getattr(device, "serial", None)
        if serial:
            free_memory(serial, getattr(device, "label", None))


async def free_memory_async(devices: Iterable[Any]) -> None:
    await asyncio.to_thread(free_memory_all, list(devices))


def free_named(names: Iterable[str]) -> None:
    """Free the named fleet phones that are Androids; anything else is skipped."""
    from . import config_paths, pokemon_fleet

    try:
        fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    except Exception as exc:  # noqa: BLE001 - never stop a run over this
        print(f"[memory] Could not read the fleet: {exc}")
        return
    for name in names:
        spec = fleet.devices.get(name)
        if spec is not None and spec.platform == "android":
            free_memory(spec.identifier, spec.name)
