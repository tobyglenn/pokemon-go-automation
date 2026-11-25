"""Ride through an adb server that something else restarted.

On the M3, Unity's batch builds reset the adb server every few minutes ("adb
server killed by remote request" in $TMPDIR/adb.<uid>.log, 6 Nov: 53 times in
two days). Every ppadb call made in that gap failed with "Connect call failed
127.0.0.1:5037", and that one error stopped every Android phone at once, mid
gift run. A refused connection now starts the server again, waits for the
phones to finish reconnecting, and retries; only a server that will not come
back is still an error.

Installed once from `sources/__init__.py`, so every worker that talks to adb
through ppadb's ConnectionAsync gets it without changing how it connects.
"""

from __future__ import annotations

import asyncio
import time

RECOVERY_SECONDS = 20.0   # allow time for an adb restart and USB reauthorization
POLL_SECONDS = 0.5
_SETTLING_STATES = ("offline", "authorizing", "connecting")

_installed = False


async def _adb(*arguments: str) -> tuple[int, str]:
    from . import android_common, pokemon_fleet  # late: pokemon_fleet imports this package

    try:
        process = await asyncio.create_subprocess_exec(
            pokemon_fleet.default_adb_binary(), *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        return 1, ""
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except (OSError, asyncio.TimeoutError):
        await android_common.kill_and_reap(process)
        return 1, ""
    except asyncio.CancelledError:
        await android_common.kill_and_reap(process)
        raise
    return process.returncode or 0, output.decode(errors="replace")


def phones_settled(devices_output: str) -> bool:
    """True once `adb devices` lists a phone and none is still reconnecting."""
    states = [line.split()[1] for line in devices_output.splitlines()[1:] if len(line.split()) >= 2]
    return bool(states) and not any(state in _SETTLING_STATES for state in states)


async def restore_server(deadline: float) -> bool:
    """Start the adb server and wait for the phones; False if it never settles."""
    while time.monotonic() < deadline:
        code, output = await _adb("devices")  # starts the server when none is up
        if code == 0 and phones_settled(output):
            return True
        await asyncio.sleep(POLL_SECONDS)
    return False


def phone_ready(devices_output: str, serial: str) -> bool:
    """True once `adb devices` lists this phone as a usable device."""
    return any(
        line.split()[:2] == [serial, "device"] for line in devices_output.splitlines()[1:]
    )


async def wait_for_phone(serial: str, deadline: float) -> bool:
    """Wait for one phone to come back after a server restart."""
    while time.monotonic() < deadline:
        code, output = await _adb("devices")
        if code == 0 and phone_ready(output, serial):
            return True
        await asyncio.sleep(POLL_SECONDS)
    return False


def _phone_missing(error: Exception) -> bool:
    text = str(error)
    return "not found" in text or "offline" in text


def install() -> None:
    """Wrap ppadb's async connect with the restart-and-retry above."""
    global _installed
    if _installed:
        return
    try:
        from ppadb.connection_async import ConnectionAsync
    except ImportError:  # an iOS-only computer without ppadb has nothing to wrap
        return
    original = ConnectionAsync.connect

    async def connect(self):
        try:
            return await original(self)
        except RuntimeError as first:
            if "connecting to" not in str(first):
                raise
            deadline = time.monotonic() + RECOVERY_SECONDS
            while time.monotonic() < deadline:
                if await restore_server(deadline):
                    try:
                        return await original(self)
                    except RuntimeError:
                        pass
                await asyncio.sleep(POLL_SECONDS)
            raise first

    ConnectionAsync.connect = connect

    # Just after a restart the server is up but the phone has not reattached:
    # A device-not-found error interrupted a gift run while the phone reattached.
    from ppadb.device_async import DeviceAsync

    original_device_connection = DeviceAsync.create_connection

    async def create_connection(self, set_transport=True, timeout=None):
        try:
            return await original_device_connection(self, set_transport, timeout)
        except RuntimeError as first:
            if not set_transport or not _phone_missing(first):
                raise
            if not await wait_for_phone(self.serial, time.monotonic() + RECOVERY_SECONDS):
                raise first
            return await original_device_connection(self, set_transport, timeout)

    DeviceAsync.create_connection = create_connection
    _installed = True
