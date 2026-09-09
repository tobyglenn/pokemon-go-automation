#!/usr/bin/env python3
"""
AutoTrader, a script for automating trading in Pokémon GO on Android.
Author: jonaro00
"""

from __future__ import annotations

from . import config_paths

import asyncio
from dataclasses import dataclass
import re
import struct
import time
from pathlib import Path

try:
    from ppadb.client_async import ClientAsync
    from ppadb.device_async import DeviceAsync
    import yaml
    from yaml.parser import ParserError
except ModuleNotFoundError as e:
    print(e)
    print('Run "pip install -r requirements.txt" to install required packages.')
    exit(1)

CONFIG_FILE_DIR  = '/storage/self/primary/'
CONFIG_FILE_NAME = 'AutoTraderConfig.yaml'
TMP_FILE_PATH    = Path('tmp.yaml')
CONFIG = dict[str, list[int]]


@dataclass
class Button:
    name: str
    delay_after: float
    use_delay_modifier: bool
    default_coords: list[int] | None = None


BUTTONS = [
    Button(
        'TRADE_BTN',
        6,
        True,
    ),
    Button(
        'FIRST_PKMN_BTN',
        1,
        False,
    ),
    Button(
        'NEXT_BTN',
        3.0,
        True,
    ),
    Button(
        'MAX_LEVEL_RESET_BTN',
        4,
        True,
        default_coords=[620, 1520],
    ),
    Button(
        'CONFIRM_BTN',
        15,
        True,
    ),
    Button(
        'X_BTN',
        1,
        False,
    ),
]
# Buttons with default coords are optional in the device config.
BUTTON_NAMES = set(btn.name for btn in BUTTONS if btn.default_coords is None)
SLEEP_MODIFIER = 0

# Used for the framebuffer grab in dismiss_record_screen. This script lives in
# platform-tools, so prefer the adb binary sitting next to it.
_ADB = Path(__file__).resolve().parent.parent / 'adb'
ADB_BINARY = str(_ADB) if _ADB.exists() else 'adb'

# --- Size record card ---
# After a trade, the game sometimes slides a full width "New Height Record" /
# "New Weight Record" card (the XXL/XXS ones) up over the received Pokémon's
# detail screen. Its close button sits exactly on top of X_BTN, so the single
# X_BTN tap only dismisses the card and leaves the detail screen open, which
# desyncs every following trade.
# Tapping X_BTN twice unconditionally is NOT safe: with no card, the first tap
# returns to the friend screen, whose own close button is at that same
# coordinate, so the second tap would leave the friend screen too. The card has
# to actually be detected, and the game draws no accessibility nodes for
# 'uiautomator dump' to find, so this reads the framebuffer instead.
# The card's signature is that the lower half of the screen becomes one large,
# near uniform panel of a flat, vivid colour. Matching that shape rather than a
# specific colour keeps it working across the different card variants.
# The saturation floor is what separates a card from the trade lobby, which is
# also a big uniform panel but a muted one: the measured card colour is
# (2, 209, 255), saturation 253, while the lobby only reaches saturation 72.
# Erring high is deliberate — a miss just leaves the old behaviour, whereas a
# false positive taps X on a screen that needed no tap and desyncs the loop.
RECORD_SAMPLE_X       = (0.05, 0.95)  # sampled region, as a fraction of the screen
RECORD_SAMPLE_Y       = (0.58, 0.90)
RECORD_SAMPLE_STEPS   = 9             # grid is STEPS x STEPS points
RECORD_MIN_SATURATION = 140           # max-min channel, ignores white/grey/muted UI
RECORD_COLOR_TOLERANCE = 45           # per channel, distance to the dominant colour
RECORD_MIN_SHARE      = 0.65          # share of samples that must be that colour
RECORD_DISMISS_DELAY  = 2             # let the card animate away before X_BTN
RECORD_MAX_ATTEMPTS   = 2

# --- Screen capture ---
# An 'exec-out screencap' occasionally never returns: the child sits on a
# transport the adb server never feeds, holding no lock and printing nothing.
# A fresh screencap against the same device still succeeds while it hangs, so
# the cure is to stop waiting on the wedged one. Without a bound the hang is
# silent and total — the screen guard stops printing mid-count and the run
# never taps again. A whole capture is ~13 MB over USB and lands in about a
# second, so the timeout is loose enough to never fire on a healthy read.
SCREENCAP_TIMEOUT     = 20            # seconds one screencap may take
SCREENCAP_ATTEMPTS    = 3
SCREENCAP_RETRY_DELAY = 1


class DeviceAsyncWrapper(DeviceAsync):
    config: CONFIG
    display_id: str | None = None   # set by setup(); only foldables need it


class AutoTraderError(Exception):
    pass


async def tap(device: DeviceAsyncWrapper, point: list[int]):
    """Sends a tap at point to device."""
    x, y = point
    # Uses a tiny swipe over 100 ms for increased reliability
    # and visibility on the Pointer Location tool.
    await device.shell(f'input swipe {x} {y} {x+1} {y+1} 100')


async def dismiss_power_up_screen(devices: list[DeviceAsyncWrapper]):
    """Check each device for the post-trade Power Up screen and cancel it if found."""
    for device in devices:
        if 'POWER_UP_CANCEL_BTN' not in device.config:
            continue
        output = await device.shell('uiautomator dump /dev/stdout')
        if 'power up' in output.lower():
            print('    Power Up screen detected on', device.serial, '— cancelling')
            await tap(device, device.config['POWER_UP_CANCEL_BTN'])
            await asyncio.sleep(1)


async def find_display_id(device: DeviceAsyncWrapper) -> str | None:
    """The display to capture, or None when the device only has one.

    Foldables report two displays, and `screencap` with no -d prints
    "[Warning] Multiple displays found, but no display id specified!" onto
    stdout ahead of the framebuffer, so the header unpack reads that text as the
    width and height and every screen read fails. Seen on the android-three, where it
    left gift.py's guard switched off for a whole run.

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


async def _screencap_once(device: DeviceAsyncWrapper):
    """One `adb exec-out screencap`. Returns its stdout, or None if the child
    could not be started or wedged and had to be killed."""
    args = [ADB_BINARY, '-s', device.serial, 'exec-out', 'screencap']
    if device.display_id:
        args += ['-d', device.display_id]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), SCREENCAP_TIMEOUT)
    except asyncio.TimeoutError:
        # SIGKILL, not terminate: a wedged child is blocked in the transport
        # read and does not act on a catchable signal.
        print(f'    Screen read timed out after {SCREENCAP_TIMEOUT}s')
        proc.kill()
        await proc.wait()
        return None
    return data


def _parse_framebuffer(data: bytes):
    """Splits a screencap's stdout into (width, height, pixel_offset, data),
    or None if it isn't a framebuffer this can read."""
    if len(data) < 16:
        return None
    width, height, pixel_format = struct.unpack('<III', data[:12])
    # Android 9+ appends a colorspace field, making the header 16 bytes.
    # The size check also rejects anything with junk prepended, e.g. the
    # "[Warning] Multiple displays" line foldables print without a display id.
    for offset in (16, 12):
        if width * height * 4 + offset == len(data):
            break
    else:
        return None
    if pixel_format != 1:  # not RGBA_8888
        return None
    return width, height, offset, data


async def screencap_raw(device: DeviceAsyncWrapper):
    """Grabs the raw framebuffer via `adb exec-out screencap`.
    Returns (width, height, pixel_offset, data), or None if it can't be read.

    Retries, unlike gift.py and berry.py, which return None on the first
    timeout and let the caller carry on with that step unguarded. This frame
    also feeds trade_ios_android.py, where a None *aborts the run* rather than
    skipping a check, so one wedge would end a session mid-trade. Both failure
    modes are transient — a killed wedge and a truncated read alike clear on
    the next try against the same device."""
    for attempt in range(1, SCREENCAP_ATTEMPTS + 1):
        data = await _screencap_once(device)
        frame = _parse_framebuffer(data) if data is not None else None
        if frame is not None:
            return frame
        if attempt < SCREENCAP_ATTEMPTS:
            await asyncio.sleep(SCREENCAP_RETRY_DELAY)
    return None


RECORD_FRAMES_DIR = config_paths.state_dir() / 'trades' / 'record-cards'


def save_record_card_frame(frame, serial: str) -> Path:
    """Keep the framebuffer a size-record dismissal fired on, as a PNG."""
    from PIL import Image

    width, height, offset, data = frame
    RECORD_FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORD_FRAMES_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{serial}.png"
    Image.frombytes(
        'RGBA', (width, height), data[offset:offset + width * height * 4]
    ).convert('RGB').save(path)
    return path


def has_record_card(width: int, height: int, offset: int, data: bytes) -> bool:
    """True if the lower half of the screen is one large panel of a single
    strong colour, which is what a size record card looks like."""
    def pixel(x, y):
        i = offset + (y * width + x) * 4
        return data[i], data[i+1], data[i+2]

    def axis(bounds, size):
        lo, hi = bounds
        step = (hi - lo) / (RECORD_SAMPLE_STEPS - 1)
        return [int(size * (lo + step * i)) for i in range(RECORD_SAMPLE_STEPS)]

    samples = [pixel(x, y)
               for y in axis(RECORD_SAMPLE_Y, height)
               for x in axis(RECORD_SAMPLE_X, width)]
    # The card carries big white text, so use the median of the coloured
    # samples as the panel colour rather than requiring every sample to match.
    coloured = sorted(p for p in samples if max(p) - min(p) >= RECORD_MIN_SATURATION)
    if not coloured:
        return False
    dominant = coloured[len(coloured) // 2]
    hits = sum(1 for p in samples
               if all(abs(p[c] - dominant[c]) <= RECORD_COLOR_TOLERANCE for c in range(3)))
    return hits / len(samples) >= RECORD_MIN_SHARE


async def dismiss_record_screen(devices: list[DeviceAsyncWrapper]):
    """Check each device for a size record card and close it, so that the
    X_BTN step that follows lands on the Pokémon detail screen as usual."""
    async def check(device: DeviceAsyncWrapper):
        # Re-checks after tapping, so the card is confirmed gone before the
        # X_BTN step runs rather than assuming the tap landed.
        for _ in range(RECORD_MAX_ATTEMPTS):
            frame = await screencap_raw(device)
            if frame is None or not has_record_card(*frame):
                return
            # A false positive here taps X on a screen that needed no tap and
            # desyncs the loop, so keep the frame that triggered the tap: the
            # stop it causes lands somewhere else entirely.
            saved = save_record_card_frame(frame, device.serial)
            print('    Size record card detected on', device.serial,
                  f'— dismissing (frame: {saved})')
            await tap(device, device.config['X_BTN'])
            await asyncio.sleep(RECORD_DISMISS_DELAY)
    await asyncio.gather(*(check(dev) for dev in devices))


async def trade_sequence(devices: list[DeviceAsyncWrapper]):
    """Sends taps to devices in a sequence with delays
    to complete a trade process. Device must have
    button coordinates stored in attribute `config`."""
    global SLEEP_MODIFIER
    for btn in BUTTONS:
        delay = max(btn.delay_after + (SLEEP_MODIFIER if btn.use_delay_modifier else 0), 0)
        commands = (tap(dev, dev.config.get(btn.name, btn.default_coords)) for dev in devices)
        print('    Sending', btn.name)
        await asyncio.gather(*commands)
        await asyncio.sleep(delay)
        if btn.name == 'CONFIRM_BTN':
            await dismiss_power_up_screen(devices)
            await dismiss_record_screen(devices)


async def trade_process(devices: list[DeviceAsyncWrapper], n_trades: int):
    """Executes `n_trades` trading sequences."""
    if n_trades < 1:
        return
    await pointer(devices, True)
    try:
        for i in range(1, n_trades+1):
            print(f'  Starting trade {i} of {n_trades}')
            await trade_sequence(devices)
    finally:
        await pointer(devices, False)


async def get_config(device: DeviceAsyncWrapper) -> CONFIG:
    """Pulls config file from device and parses it. Sets the `config` attribute on success."""
    config_file_path = CONFIG_FILE_DIR + CONFIG_FILE_NAME
    await device.pull(config_file_path, TMP_FILE_PATH)
    content = TMP_FILE_PATH.read_text()
    TMP_FILE_PATH.unlink()
    if not content:
        raise AutoTraderError(f'Found no config file at {config_file_path}')
    config: CONFIG = yaml.safe_load(content)
    assert isinstance(config, dict), f'Incorrect config file format (should be an object with keys)'
    if not BUTTON_NAMES <= set(config.keys()):
        raise AutoTraderError(f'Missing config key(s): {BUTTON_NAMES - set(config.keys())}')
    for coords in config.values():
        assert isinstance(coords, list) and all(map(lambda i: isinstance(i, int), coords)),\
            f'Invalid coords format in config (should be list with two integers)'
    device.config = config
    return config


async def set_setting(device: DeviceAsyncWrapper, namespace_and_key: str, value):
    """Wraps 'settings put' in adb shell. Sets key in namespace to value."""
    await device.shell(f'settings put {namespace_and_key} {value}')


async def pointer(devices: list[DeviceAsyncWrapper], on: bool):
    """Turns on/off pointer location setting on all `devices`."""
    for device in devices:
        try:
            await set_setting(device, 'system pointer_location', int(on))
        except Exception:
            print(f'Failed to turn {"on" if on else "off"} pointer location on', device.serial)


async def setup() -> list[DeviceAsyncWrapper]:
    """Checks for devices and loads config files from devices."""
    client = ClientAsync()
    devices: list[DeviceAsyncWrapper] = await client.devices()
    if not devices:
        raise AutoTraderError('No devices found')
    print('Found devices:')
    for device in devices:
        print(' ', device.serial)
    print()
    try:
        for device in devices:
            await get_config(device)
            print('Successfully loaded config from', device.serial)
    except (AutoTraderError, AssertionError, ParserError) as e:
        raise AutoTraderError(f'Failed to load config from {device.serial}', *e.args) from e
    for device in devices:
        device.display_id = await find_display_id(device)
        if device.display_id:
            print(f'{device.serial}: multiple displays, reading the active one:', device.display_id)
    return devices


def interface():
    """Runs the main loop asking for user input."""
    global SLEEP_MODIFIER
    print(
        '\n'
        ' ##                          ## \n'
        '##   AutoTrader by jonaro00   ##\n'
        ' ##                          ## \n'
    )
    devices: list[DeviceAsyncWrapper] = asyncio.run(setup())
    while True:
        print()
        try:
            i = input("Number of trades? ('q' to quit) > ").strip()
            il = i.lower()
            if il == 'q':
                break
            if il.startswith('delay'):
                args = i.split()
                args_l = len(args)
                if args_l == 2:
                    SLEEP_MODIFIER = float(args[1])
                print('Current extra delay:', SLEEP_MODIFIER)
                continue
            assert (n := int(i)) > 0
        except KeyboardInterrupt:
            print('\nDouble press interrupt to quit')
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                break
            continue
        except EOFError:
            break
        except (ValueError, AssertionError):
            print('Enter a positive integer')
            continue
        try:
            print(f'Starting {n} trades (Ctrl+C to cancel)...')
            asyncio.run(trade_process(devices, n))
        except KeyboardInterrupt:
            continue
        except Exception as e:
            print(e)


def main():
    try:
        interface()
    except AutoTraderError as e:
        print('\n'.join(map(str, e.args)))
    except Exception as e:
        print('Unexpected error:', e.__class__.__name__, e.args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('trade', 'trade_pokemon.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == '__main__':
    main()

# https://github.com/encode/httpx/issues/914#issuecomment-622586610
# https://github.com/aio-libs/aiohttp/issues/4324
# https://github.com/aio-libs/aiohttp/issues/4324#issuecomment-733884349
