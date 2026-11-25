#!/usr/bin/env python3
"""Reconnect the Pokémon GO Plus from the map on each Android phone."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from typing import Any, Sequence

from PIL import Image

from . import config_paths, gbl_vision, pokemon_fleet, screen_router

GAME_PACKAGE = 'com.nianticlabs.pokemongo'

# Use a handset calibration when the map is visible but the disc is obscured.
ACCESSORY_KEY = 'ACCESSORY_BTN'

# --- The accessory disc ---
# The disc is a cap of red over a dark base, and the game draws it in two
# ways: a muted dusty rose on a phone whose wearable is not connected, and
# the classic bright red over white with a green centre on one that is.  The
# cap is read in both cases, because both are red over a dark base.
#
# Only the strip the disc can be in is read: the pink research badge further
# down the right-hand column is the same colour and sits lower than the band.
ACCESSORY_BAND_X = (0.85, 0.98)
ACCESSORY_BAND_Y = (0.08, 0.18)
ACCESSORY_MIN_PIXELS = 200     # a cap this size is a disc, not a map pin
ACCESSORY_MIN_ASPECT = 1.4      # a cap is much wider than it is tall
ACCESSORY_MAX_ASPECT = 6.0
ACCESSORY_MAX_WIDTH = 0.09     # of the screen width, so a badge cannot pass

# The Android pairing activities seen on the sample Android handsets.
DIALOG_PACKAGES = {'com.android.settings', 'com.android.bluetooth', 'com.oplus.wirelesssettings'}
DIALOG_MARKERS = ('go plus', 'pokeball plus', 'poké ball plus')
DIALOG_BUTTONS = ('pair', 'pair & connect')

POLL_GAP = 2.0
DEFAULT_TIMEOUT = 180.0
CONNECTION_SETTLE = 5.0
UI_DUMP_TIMEOUT = 8

ADDRESS = re.compile(r'\b([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\b')
# Every Pokemon GO Plus in the fleet reports the same vendor prefix.  It is a
# fallback: where the system names the device, the name is used instead.
GO_PLUS_PREFIX = '98:B6:E9'
GO_PLUS_NAMES = ('pokemon go plus', 'pokémon go plus', 'pokeball plus', 'poké ball plus')


class GoPlusError(Exception):
    """A phone could not be paired, or could not be read."""


@dataclass
class BluetoothState:
    """Live bonds and the game's current Bluetooth LE connections."""

    bonded: dict[str, str] = field(default_factory=dict)
    game_connections: set[str] = field(default_factory=set)

    def connected(self) -> set[str]:
        return {
            address for address in self.game_connections
            if address in self.bonded
            and (address.startswith(GO_PLUS_PREFIX) or is_go_plus_name(self.bonded[address]))
        }


def is_go_plus_name(name: str) -> bool:
    return any(marker in name.casefold() for marker in GO_PLUS_NAMES)


def parse_bluetooth(dump: str) -> BluetoothState:
    """Read the current tables. Android logs may be redacted or newest-first."""
    state = BluetoothState()
    in_bonded = False
    in_game_client = False
    in_connections = False
    in_gatt = False
    for line in dump.splitlines():
        value = line.strip()
        if value.startswith('--- BEGIN:BTSNOOP'):
            break
        if value == 'Bonded devices:':
            in_bonded = True
            continue
        if in_bonded:
            entry = ADDRESS.match(value)
            if entry:
                name = value.rsplit(']', 1)[-1].strip() if ']' in value else ''
                state.bonded[entry.group(1).upper()] = name
                continue
            in_bonded = False
        if value == 'GATT Client Map':
            in_gatt = True
            continue
        if not in_gatt:
            continue
        if value.startswith(('GATT Server Map', 'GATT Handle Map')):
            in_gatt = in_game_client = in_connections = False
            continue
        if re.fullmatch(r'  \S+(?: \(Registered\))?', line):
            in_game_client = value.split()[0] == GAME_PACKAGE
            in_connections = False
        elif in_game_client:
            if value.startswith('Connections:'):
                in_connections = True
            elif in_connections:
                entry = ADDRESS.search(value)
                if entry:
                    state.game_connections.add(entry.group(1).upper())
                else:
                    in_connections = False
    return state


def _is_cap(pixel: tuple[int, int, int]) -> bool:
    """The accessory disc's red cap, in either of the game's two drawings."""
    red, green, blue = pixel
    return red > 110 and green < 140 and blue < 170 and red > green + 25


def find_accessory_disc(image: Image.Image) -> list[int] | None:
    """The map's accessory disc, in image pixels, or None when it is not there.

    The cap is a wide, flat-topped red arc: wider than it is tall, and the
    only red thing in the strip of the map's top-right corner.  The disc's
    centre is the cap's middle across and its lower edge down, because the
    cap is the disc's upper half.
    """
    width, height = image.size
    pixels = image.convert('RGB').load()
    x_from, x_to = int(width * ACCESSORY_BAND_X[0]), int(width * ACCESSORY_BAND_X[1])
    y_from, y_to = int(height * ACCESSORY_BAND_Y[0]), int(height * ACCESSORY_BAND_Y[1])
    xs: list[int] = []
    ys: list[int] = []
    for y in range(y_from, y_to):
        for x in range(x_from, x_to):
            if _is_cap(pixels[x, y]):
                xs.append(x)
                ys.append(y)
    if len(xs) < ACCESSORY_MIN_PIXELS:
        return None
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    cap_width = right - left
    cap_height = max(bottom - top, 1)
    aspect = cap_width / float(cap_height)
    if not ACCESSORY_MIN_ASPECT <= aspect <= ACCESSORY_MAX_ASPECT:
        return None
    if cap_width > ACCESSORY_MAX_WIDTH * width:
        return None
    return [(left + right) // 2, bottom]


def accessory_point(image: Image.Image, mapped: list[int] | None = None) -> list[int] | None:
    """Where to press the accessory disc: the picture first, the config next."""
    found = find_accessory_disc(image)
    if found is not None:
        return found
    if mapped is not None and len(mapped) == 2:
        return list(mapped)
    return None


def game_connection_error(image: Image.Image) -> bool:
    """Recognize the game's immediate Go Plus connection error sheet."""
    lower_half = image.crop((0, image.height // 2, image.width, image.height))
    text = ' '.join(box.text.casefold() for box in gbl_vision.recognize(lower_half))
    return 'having trouble' in text and 'connecting to your pok' in text


def pairing_dialog_button(xml: str) -> list[int] | None:
    """The centre of the Android pairing dialog's button, or None.

    The dialog is only acted on when it names the wearable, so a "Pair" or
    "Allow" belonging to anything else in the system is left alone.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    nodes = [node for node in root.iter('node') if node.get('package') in DIALOG_PACKAGES]
    text = ' '.join(
        ' '.join(filter(None, (node.get('text'), node.get('content-desc')))).lower()
        for node in nodes
    )
    if not any(marker in text for marker in DIALOG_MARKERS):
        return None
    for node in nodes:
        label = (node.get('text') or node.get('content-desc') or '').strip().lower()
        if label not in DIALOG_BUTTONS:
            continue
        bounds = node.get('bounds') or ''
        match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
        if not match:
            continue
        left, top, right, bottom = (int(value) for value in match.groups())
        return [(left + right) // 2, (top + bottom) // 2]
    return None


async def bluetooth_state(device: Any) -> BluetoothState:
    """The phone's Bluetooth state, read from the system."""
    dump = await device.shell('dumpsys bluetooth_manager')
    return parse_bluetooth(dump or '')


async def accept_pairing_dialog(device: Any, tap: Any) -> bool:
    """Read a fresh Android dialog before tapping its Pair button."""
    path = '/data/local/tmp/pogo-goplus-pairing.xml'
    xml = await device.shell(
        f'rm -f {path}; timeout {UI_DUMP_TIMEOUT} uiautomator dump {path} >/dev/null '
        f'&& cat {path}; rm -f {path}'
    )
    point = pairing_dialog_button(xml or '')
    if point is None:
        return False
    await tap(point)
    return True


async def reconnect(phone: Any, device: Any, *, timeout: float) -> str:
    """Connect from the map and confirm a stable live game connection."""
    state = await bluetooth_state(device)
    active = state.connected()
    if active:
        return f'already connected to {", ".join(sorted(active))}'

    target = phone.target
    # Finish a pairing confirmation left open by a prior attempt before
    # navigation brings the game back over it.
    accepted = await accept_pairing_dialog(device, target.tap)
    if accepted:
        phone.say('accepted the pending Android Go Plus pairing dialog')
    else:
        for attempt in range(2):
            image = await phone.go_home()
            point = accessory_point(image, target.mapped(ACCESSORY_KEY, image))
            if point is None:
                raise GoPlusError('Go Plus button is missing from the map')
            # The accessory can reconnect while the map is loading.
            state = await bluetooth_state(device)
            active = state.connected()
            if active:
                return f'already connected to {", ".join(sorted(active))}'
            await target.tap(point)
            phone.say(f'pressed the Go Plus button at {point}')
            if attempt:
                break
            # One handset showed this sheet immediately when the running game
            # stopped scanning. A fresh game session restored pairing.
            await asyncio.sleep(0.2)
            image = await target.screenshot()
            if not await asyncio.to_thread(game_connection_error, image):
                break
            phone.say('the game reported a Go Plus connection error; restarting it once')
            await device.shell(f'am force-stop {GAME_PACKAGE}')

    deadline = time.monotonic() + timeout
    stable_since = None
    pending: set[str] = set()
    while True:
        state = await bluetooth_state(device)
        active = state.connected()
        now = time.monotonic()
        if active:
            if active != pending:
                stable_since = now
                pending = active
            if now - stable_since >= CONNECTION_SETTLE:
                return f'connected to {", ".join(sorted(active))}'
        else:
            pending.clear()
            if now >= deadline:
                break
            if await accept_pairing_dialog(device, target.tap):
                accepted = True
                phone.say('accepted the Android Go Plus pairing dialog')
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_GAP)

    if accepted:
        raise GoPlusError('Android pairing was confirmed, but the game did not keep a Bluetooth connection')
    if pending:
        raise GoPlusError('the game connection did not stay up long enough to verify')
    raise GoPlusError('the game did not connect after the map tap')


async def pair_phone(spec: pokemon_fleet.DeviceSpec, device: Any, args: argparse.Namespace) -> str:
    """Run the same reconnect used by starter."""
    if args.plan:
        state = await bluetooth_state(device)
        return ('already connected' if state.connected()
                else 'would press the Go Plus button and check for a live connection')

    from . import starter

    async def work(phone: starter.Phone) -> starter.Report:
        detail = await reconnect(phone, device, timeout=args.timeout)
        return starter.Report('goplus', True, detail)

    report = await starter.with_phone(spec, args, work)
    return report.detail


def _wanted(args: argparse.Namespace, specs: Sequence[pokemon_fleet.DeviceSpec]) -> list[pokemon_fleet.DeviceSpec]:
    """The phones to pair: every Android phone here unless names are given.

    The fleet hands every computer the same arguments, so a run without
    `--devices` means "the phones plugged into this computer", and each
    computer answers for its own.
    """
    wanted = set(args.devices or [])
    if not wanted or 'all' in wanted:
        return list(specs)
    return [spec for spec in specs if spec.name in wanted]


async def run(args: argparse.Namespace) -> int:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config('pokemon-fleet.yaml'))
    specs = [
        spec for spec in fleet.devices.values()
        if spec.enabled and spec.platform == 'android'
    ]
    chosen = _wanted(args, specs)
    androids = await screen_router.attached_androids(chosen)
    here = [spec for spec in chosen if spec.identifier in androids]
    skipped = [spec.name for spec in chosen if spec.identifier not in androids]
    for name in skipped:
        print(f"[{name}] not attached to this computer; skipped", flush=True)
    if not here:
        print('No Android phone is attached to this computer')
        return 0
    print(f'Pairing the Pokemon GO Plus on {len(here)} phone(s): '
          + ', '.join(spec.name for spec in here), flush=True)

    failures = 0
    for spec in here:
        try:
            outcome = await pair_phone(spec, androids[spec.identifier], args)
        except Exception as exc:
            outcome = f'failed: {exc}'
            failures += 1
        print(f'[{spec.name}] {outcome}', flush=True)
    return 1 if failures else 0


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--devices', nargs='+', help='fleet device names to pair (default: every Android phone here)')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help='seconds to wait for a pairing')
    parser.add_argument('--plan', action='store_true', help='say what would be pressed; pair nothing')
    args = parser.parse_args(arguments)
    if not args.timeout >= 1:
        parser.error('--timeout must be at least one second')
    args.shots = config_paths.state_dir() / 'goplus'
    args.dry_run = False
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Cancelled', file=sys.stderr)
        raise SystemExit(130)
