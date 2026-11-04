"""ADB plumbing shared by the Android workers.

Each worker used to carry its own copy of these helpers.  They live here once
so a fix (a foldable's second display, a config pulled into the wrong folder)
reaches every workflow.  Workers import the names into their own module, so
tests that patch `gift_android.find_display_id` and the like keep working.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Collection, Iterable

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
