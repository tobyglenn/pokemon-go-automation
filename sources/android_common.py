"""ADB plumbing shared by the Android workers.

Each worker used to carry its own copy of these helpers.  They live here once
so a fix (a foldable's second display, a config pulled into the wrong folder)
reaches every workflow.  Workers import the names into their own module, so
tests that patch `gift_android.find_display_id` and the like keep working.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import struct
from pathlib import Path
from typing import Any, Callable, Collection, Iterable

import yaml

from . import config_paths


def adb_binary() -> str:
    """The adb this tree drives phones with, the same one the fleet layer asks.

    Two adb versions on one computer restart each other's server and drop every
    other run's connection, so the workers must not fall back to a different
    `adb` than `pokemon_fleet.default_adb_binary()` picks.
    """
    from . import pokemon_fleet

    return pokemon_fleet.default_adb_binary()


async def find_display_id(device: Any) -> str | None:
    """The display to capture, or None when the device only has one.

    Foldables report two displays, and `screencap` with no -d prints
    "[Warning] Multiple displays found, but no display id specified!" onto
    stdout ahead of the framebuffer, so the header unpack reads that text as the
    width and height and every screen read fails.

    The *active* display, not simply the first: folding the phone shut hands
    over to the cover display, which is a different size and coordinate space.
    """
    try:
        listed = await device.shell('dumpsys SurfaceFlinger --display-id')
        ids = re.findall(r'^Display (\d+)', listed, re.M)
        if len(ids) < 2:
            return None
        for viewport in (await device.shell('dumpsys display')).split('DisplayViewport{')[1:]:
            if 'isActive=true' not in viewport:
                continue
            match = re.search(r"uniqueId='local:(\d+)'", viewport)
            if match and match.group(1) in ids:
                return match.group(1)
        return ids[0]
    except Exception:
        return None


Frame = tuple[int, int, int, bytes]


# How long a killed adb client gets to be reaped.  When adb has to restart its
# server (Unity kills it), the forked server inherits the client's stdout
# pipe and keeps it open, and asyncio's Process.wait() waits for every pipe
# to close as well as for the exit: sample-receiver's gift leg hung there for good after
# "Screen read timed out" (24 Nov, server restarted 08:36:25).
REAP_TIMEOUT = 3.0


async def kill_and_reap(proc: Any, timeout: float = REAP_TIMEOUT) -> None:
    """Kill an adb client and wait for it, never longer than `timeout`."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        # The child is gone; only a pipe held by the adb server keeps the
        # transport open.  Close it so no fd or waiter is left behind.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


async def capture_screen(
    device: Any,
    adb_binary: str,
    timeout: float,
    on_timeout: Callable[[], None] | None = None,
) -> bytes | None:
    """The raw stdout of `adb exec-out screencap`, or None if it could not be read.

    `adb exec-out screencap` wedges outright now and then: the device stays
    healthy and later reads work, but this one never returns.  It is killed
    after `timeout` seconds (SIGKILL: a wedged child is blocked in the transport
    read and ignores catchable signals) and `on_timeout` reports it.
    """
    args = [adb_binary, '-s', device.serial, 'exec-out', 'screencap']
    display_id = getattr(device, 'display_id', None)
    if display_id:
        args += ['-d', display_id]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        if on_timeout is not None:
            on_timeout()
        await kill_and_reap(proc)
        return None
    except OSError:
        return None
    return data


def parse_framebuffer(data: bytes, *, exact: bool = False) -> Frame | None:
    """Split a screencap's stdout into (width, height, pixel_offset, data).

    Android 9+ appends a colorspace field, making the header 16 bytes rather
    than 12.  `exact` also demands the size match to the byte and RGBA_8888
    pixels, which rejects anything with junk prepended, such as the
    "[Warning] Multiple displays" line foldables print without a display id.
    """
    if len(data) < (16 if exact else 12):
        return None
    width, height, pixel_format = struct.unpack('<III', data[:12])
    size = width * height * 4
    if exact:
        for offset in (16, 12):
            if size + offset == len(data):
                break
        else:
            return None
        if pixel_format != 1:  # not RGBA_8888
            return None
        return width, height, offset, data
    offset = 16 if len(data) >= size + 16 else 12
    if len(data) < size + offset:
        return None
    return width, height, offset, data


async def swipe_tap(device: Any, point: list[int]) -> None:
    """A tap sent as a tiny 100 ms swipe, which the game registers more reliably."""
    x, y = point
    await device.shell(f'input swipe {x} {y} {x+1} {y+1} 100')


def _is_point(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    )


def validate_config(
    config: Any,
    required: Collection[str],
    error: type[Exception],
    *,
    coordinate_keys: Iterable[str] | None = None,
) -> dict:
    """Check a parsed on-phone config, raising `error` rather than asserting.

    `assert` disappears under `python -O`, which let a malformed file through
    to the first tap.  `coordinate_keys` limits the [x, y] check to those keys;
    by default every value must be a point.
    """
    if not isinstance(config, dict):
        raise error('Incorrect config file format (should be an object with keys)')
    missing = set(required) - set(config)
    if missing:
        raise error(f'Missing config key(s): {missing}')
    keys = config.keys() if coordinate_keys is None else coordinate_keys
    for key in keys:
        if not _is_point(config[key]):
            raise error(f'Invalid coords format for {key} (should be a list of two integers)')
    return config


async def pull_config(
    device: Any,
    remote_path: str,
    tmp_name: str,
    required: Collection[str],
    error: type[Exception],
    *,
    coordinate_keys: Iterable[str] | None = None,
) -> dict:
    """Pull the phone's calibration file, parse and validate it, and set `device.config`.

    The copy lands in the private state directory under a per-device name, so
    concurrent pulls never share a file and nothing is written into the
    working directory.  `tmp_name` carries one `{}` for the device serial.
    """
    tmp_file_path = Path(config_paths.temporary_file(
        tmp_name.format(re.sub(r'\W', '_', device.serial))
    ))
    try:
        await device.pull(remote_path, tmp_file_path)
        content = tmp_file_path.read_text() if tmp_file_path.exists() else ''
    finally:
        tmp_file_path.unlink(missing_ok=True)
    if not content:
        raise error(f'Found no config file at {remote_path}')
    config = validate_config(
        yaml.safe_load(content), required, error, coordinate_keys=coordinate_keys
    )
    device.config = config
    return config
