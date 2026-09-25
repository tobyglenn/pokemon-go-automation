"""Hermetic test runtime: never inherit a contributor's fleet or launch devices."""
from __future__ import annotations
import atexit
import os
from pathlib import Path
import subprocess
import tempfile

_TEMP = tempfile.TemporaryDirectory(prefix="pogo-tests-")
atexit.register(_TEMP.cleanup)
os.environ["POGO_CONFIG_DIR"] = str(Path(_TEMP.name) / "config")
os.environ["POGO_STATE_DIR"] = str(Path(_TEMP.name) / "state")
os.environ["POGO_MACHINE"] = "test-host"
os.environ.pop("POGO_FLEET_CONFIG", None)
os.environ["POKEMON_WATCHDOG"] = "0"

_REAL_POPEN = subprocess.Popen
_DEVICE_TOOLS = {"adb", "ssh", "appium", "idevice_id", "ideviceinfo", "iproxy", "xcrun"}

def _guarded_popen(command, *args, **kwargs):
    words = command if isinstance(command, (list, tuple)) else str(command).split()
    executable = Path(str(words[0])).name if words else ""
    workers = {"gift", "send_gifts", "add_friends", "gbl", "battle_league", "gbl_day", "trade", "trade_pokemon", "luckytrash", "transfer_pokemon", "berry", "feed_berries", "fleet", "pokemon_fleet", "catch", "remove_friends", "battle", "play", "excellent_throw"}
    # A path (`scripts/fleet.py`) or a module (`scripts.fleet`) names the same worker.
    worker_command = executable.startswith("python") and any(
        str(word).removesuffix(".py").replace("/", ".").rsplit(".", 1)[-1] in workers for word in words[1:]
    )
    if executable in _DEVICE_TOOLS or worker_command:
        raise AssertionError("Unit tests must mock device discovery and automation subprocesses")
    return _REAL_POPEN(command, *args, **kwargs)

subprocess.Popen = _guarded_popen
