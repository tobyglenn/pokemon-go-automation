#!/usr/bin/env python3
"""
AutoGifter — Automates opening and sending gifts in Pokémon GO on Android.

Uses the same AutoTraderConfig.yaml as trade.py (with the gift keys appended).
Every connected device runs the cycle at once, each from its own config, so each
one needs every key in GIFT_KEYS. A device whose config is missing or incomplete
is skipped and the rest still run.

Gift cycle (start on an opened friend's gift screen, or on the friends list):
  1. Tap OPEN_BTN to open the gift that friend sent
  2. Tap SEND_GIFT_BTN, FIRST_GIFT_BTN and SEND_BTN to send one back
  3. Tap CLOSE_BTN to close
  4. Tap SORT_BTN twice to re-sort the friends list
  5. Tap NEXT_FRIEND_BTN to open the next friend, back to 1.

The cycle is defined by GIFT_STEPS below; reorder that list to match the
order the buttons appear in on your device.

Steps that only make sense on one screen are checked before they are tapped —
see "Friends list guard" below.
"""

from . import config_paths
import argparse
import asyncio
import re
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from ppadb.client_async import ClientAsync
    from ppadb.device_async import DeviceAsync
    from PIL import Image
    import yaml
    from yaml.parser import ParserError
except ModuleNotFoundError as e:
    print(e)
    print('Run "pip install -r requirements.txt" to install required packages.')
    exit(1)

# The map and the main menu are two of the screens a lost cycle ends up on, and
# gbl_home_recovery already finds both by shape on these handsets. Recovery
# reuses those detectors rather than growing a second set of thresholds.
from . import gbl_home_recovery

CONFIG_FILE_DIR  = '/storage/self/primary/'
CONFIG_FILE_NAME = 'AutoTraderConfig.yaml'
TMP_FILE_NAME    = 'tmp_gift_{}.yaml'  # per device: configs are pulled concurrently
CONFIG = dict[str, list[int]]

SLEEP_MODIFIER = 0


@dataclass
class Step:
    name: str
    delay_after: float
    use_delay_modifier: bool = False
    taps: int = 1
    tap_gap: float = 1.0   # pause between repeated taps on the same button


@dataclass(frozen=True)
class GiftCycleResult:
    opened: bool
    sent: bool
    outgoing_available: bool


# One full gift cycle, in order. Waits for the game server get
# SLEEP_MODIFIER added, so they can be tuned with the `delay` command.
GIFT_STEPS = [
    # Open the gift, then send one back
    Step('OPEN_BTN', 6, True),
    Step('SEND_GIFT_BTN', 2, True),
    Step('FIRST_GIFT_BTN', 1),
    Step('SEND_BTN', 6, True),
    # Close, back on the friends list
    Step('CLOSE_BTN', 2, True),
    # Re-sort by "can receive a gift". Each tap on the row flips the sort
    # direction and closes the menu, so the pair runs twice to end up
    # descending, with giftable friends at the top.
    Step('SORT_BTN', 1.5),
    Step('CAN_RECEIVE_GIFT_BTN', 1.5),
    Step('SORT_BTN', 1.5),
    Step('CAN_RECEIVE_GIFT_BTN', 1.5),
    # Open the friend at the top of the list
    Step('NEXT_FRIEND_BTN', 2, True),
]

GIFT_KEYS = {step.name for step in GIFT_STEPS}

# Used for the framebuffer grab in the friends list guard. This script lives in
# platform-tools, so prefer the adb binary sitting next to it.
_ADB = Path(__file__).resolve().parent.parent / 'adb'
ADB_BINARY = str(_ADB) if _ADB.exists() else 'adb'

# --- Friends list guard ---
# SORT_BTN and BATTLE land on the same point. On the android-one the config holds
# SORT_BTN [1130, 2237] and BATTLE_BTN [1127, 2166], and a friend profile's
# BATTLE button measures at (1117, 2250) — well inside the same touch target.
# The cycle is otherwise open loop and taps SORT_BTN twice per friend, so one
# tap that fails to register leaves a profile on screen, the next SORT_BTN opens
# "Choose your league", and every following tap is lost on the battle screens.
# Profile layouts make it intermittent rather than immediate: a 4 button profile
# (SEND GIFT / LOCAL TRADE / REMOTE TRADE / BATTLE) puts BATTLE ~13px from
# SORT_BTN, a 3 button one (SEND GIFT / TRADE / BATTLE) ~64px away.
# The game draws no accessibility nodes for 'uiautomator dump', so this reads
# the framebuffer. The friends list is the one screen whose top band is pure
# greyscale — the status bar plus the white ME/FRIENDS/SOCIAL header. Measured
# on that band: friends list brightness 224, saturation 0; friend profiles 160
# and 157, or 85 and 44; the open sort menu 195 and 50. Testing for "bright and
# unsaturated" therefore needs no per device reference image.
GUARD_SAMPLE_X       = (0.05, 0.95)  # sampled region, as a fraction of the picture
GUARD_SAMPLE_Y       = (0.03, 0.13)
GUARD_SAMPLE_STEPS   = 10            # grid is STEPS x STEPS points
GUARD_MIN_BRIGHTNESS = 210           # mean of the three channels
GUARD_MAX_SATURATION = 20            # white list header; friend profiles are pale blue
# Live compact-layout device calibration: the friends list scored 254 brightness / 0.3
# saturation; a pale friend profile scored 220 / 56.
# Those fractions are of the picture, not of the panel. Some phones letterbox
# the game between a black status bar and a black navigation bar — the compact-layout device
# 5G gives up rows 0-72 and 1530-1600 of 1600 — and a band measured from the
# panel slides up into that black. On the moto's friends list the y 0.03-0.13
# band scored 206.7 that way, just under the 210 threshold, so the guard called
# a perfectly good friends list "not the friends list" on every step. Measuring
# from the picture instead keeps one set of fractions right on every device;
# phones that do not letterbox are unaffected.
GUARD_LETTERBOX_LEVEL = 40   # a row this dark all the way across is a system bar
GUARD_MAX_LETTERBOX   = 0.1  # ...but never skip more than this much of the screen
GUARD_RECOVER_DELAY    = 3           # let the screen settle before re-checking

# --- The sort control ---
# The header band above is shared by all three tabs of the friends panel, so on
# its own it calls the SOCIAL tab the friends list. SORT_BTN itself settles it:
# on the friends list it is a dark teal disc, and the same point is white on the
# ME tab and bare grass on SOCIAL and on the map. Measured on the android-three over a
# box of this radius: friends list 0.96 teal, sort menu 1.00 (its field covers
# the whole screen), ME 0.00, SOCIAL 0.00, map 0.00. A radius rather than the
# single pixel because these buttons are gradients; a small one because the
# map's nearby-Pokemon bar creeps into the box by 0.07 of the width (0.48 teal).
SORT_FAB_RADIUS   = 0.025   # of screen width
SORT_FAB_STEP     = 4       # sample every Nth pixel; the disc is ~60px across
SORT_FAB_MIN_TEAL = 0.5
# A healthy screencap comes back in well under a second; this only fires when
# adb has wedged. The guard stands down on a timeout rather than gating the run.
SCREENCAP_TIMEOUT      = 4
MAX_IDLE_CYCLES        = 15

# Same availability test as the iPhone runner: active action buttons are
# colored, while unavailable buttons are grey. Android coordinates already use
# screenshot pixels.
ACTION_SAMPLE_RADIUS = 8
ACTION_SAMPLE_STEP = 4
ACTION_MIN_SATURATION = 35
# One grey SEND is not an empty gift bag. The confirm sheet SEND lives on is
# the last thing in the cycle to paint, and on the slowest phone it has been
# read before it arrived: a android-one stopped after 19 sends with gifts still in the
# bag, because the picker SEND_GIFT_BTN opens had not come up and the probe
# sampled the friend screen underneath. Measured at SEND_BTN's own point on
# that phone — friend screen 0.0, gift picker 45.6, confirm sheet 67.9 — so a
# missed step and an empty bag read the same way here, and only time or another
# cycle tells them apart. Hence both: the probe is polled, and the bag is
# called empty only when whole cycles in a row agree.
SEND_POLL_READS = 4
SEND_POLL_GAP = 0.8
EMPTY_BAG_CYCLES = 3
UNLIT_ARTIFACT_DIR = config_paths.state_dir() / 'gifts'
# --- The gift the game will not open ---
# Once the Item Bag is full, OPEN_BTN raises "Your Item Bag is full!" instead
# of opening the gift, and the notice sits there swallowing SEND_GIFT_BTN and
# FIRST_GIFT_BTN. SEND is then read on the notice, so the friend is passed
# over as if the gift bag were empty — which is how a android-one with four gifts
# still in it called itself done three cycles later. Its own OPEN opens the
# gift for the Stardust and hands the friend's screen back, so that is what
# gets pressed.
# Found by shape first, because OCR is half a second: the notice is the one
# screen in the cycle that floats a card over a dimmed background. Measured on
# the android-one (1316x2560) as (card_band, card_edges) — notice 0.75/0.30, friend
# profile 0.53/0.97 and 0.82/0.96, gift picker 0.21/0.01, confirm sheet
# 0.45/0.00, an unopened gift 0.55/0.00, a sponsor's card 0.00/0.00.
BAG_FULL_MIN_CARD_BAND = 0.65
BAG_FULL_MAX_CARD_EDGES = 0.40
BAG_FULL_PHRASE = 'item bag is full'
BAG_FULL_DELAY = 6
SEND_GIFT_SAMPLE_KEYS = (
    'SEND_GIFT_2_BUTTON_SAMPLE',
    'SEND_GIFT_3_BUTTON_SAMPLE',
)

# Steps that are only correct on the friends list. Tapping these anywhere else
# is what starts a battle, so they are never sent blind.
NEEDS_FRIENDS_LIST = {'SORT_BTN', 'NEXT_FRIEND_BTN'}
# The cycle's entry point, only correct on a friend's screen. Guarding it lets a
# run start from the friends list, which is what the prompt asks for.
NEEDS_FRIEND_SCREEN = {'OPEN_BTN'}
# CAN_RECEIVE_GIFT_BTN is deliberately unguarded: it is tapped while the sort
# menu covers the header, which reads as "not the friends list".

# --- Walking home ---
# Every screen the cycle can be left on, and the one tap a player would make on
# it to get nearer the friends list. The screen is named again after each tap,
# so a step that lands somewhere unexpected is simply the next screen to leave
# and the walk carries on from there.
#
# Naming the screen first is the whole point. The version before this one tapped
# CLOSE_BTN three times in a row without looking, and on the android-three that walks
# *away* from home — verified on device, one tap a screenshot:
#   friend's screen -> CLOSE_BTN -> friends list   (the recovery that was wanted)
#   friends list    -> CLOSE_BTN -> the map        (the X closes the whole panel)
#   map             -> CLOSE_BTN -> the main menu  (CLOSE_BTN [612,2830] is the
#                                                   android-three's pokeball, GBL_MENU_BTN
#                                                   [611,2820] — the same button)
# So a run whose first tap had already recovered spent the other two leaving,
# and ended parked on the main menu three screens from the friends list.
# The hardware back key, spelled as a config key so a step can ask for it
# alongside real buttons. Every phone has it, so it is never dropped as missing.
BACK_KEY = 'BACK'

FRIENDS_LIST  = 'the friends list'
FRIENDS_PANEL = 'the friends panel, on another tab'
FRIEND_SCREEN = "a friend's screen"
SORT_MENU     = 'the sort menu'
MAIN_MENU     = 'the main menu'
MAP           = 'the map'

# Each screen gets the steps worth trying on it, in order, and the walk moves on
# to the next one when it finds itself back on a screen it has already tried to
# leave. A step is one or more taps; a tap is the config keys that can name that
# button, and the first key the device carries wins. A tap whose keys the device
# has none of is dropped, and only a step left with no taps at all counts as
# unusable — so a half-mapped phone still gets the part of the step it can make.
STEP_HOME: dict[str, tuple[tuple[tuple[str, ...], ...], ...]] = {
    # The panel is up on ME or SOCIAL. One tap on the FRIENDS tab.
    FRIENDS_PANEL: (
        (('FRIENDS_TAB_BTN',),),
    ),
    # "A friend's screen" is really a stack, and every screen in it closes with
    # the same X: NEXT_FRIEND_BTN opens the *gift* over the profile, and the
    # profile's own header opens the friendship detail over that. So this is the
    # one step worth repeating — checked between taps, unlike the three blind
    # ones this replaced. Three covers the deepest stack the cycle can build.
    # The battle screens are named this too and nothing on them answers CLOSE_BTN,
    # so they spend the three and hand over to the battle exit.
    FRIEND_SCREEN: (
        (('CLOSE_BTN',),),
    ) * 3,
    # The row tap goes first because it is the useful one: it closes the menu by
    # choosing a sort, and "can receive a gift" is the sort the cycle wants
    # anyway. It does not always land — a android-three leg spent that one step, found
    # the menu still up and refused, which is what the second step is for. Back
    # dismisses the menu outright, and is safe here because this screen is named
    # positively: the teal sort disc showing with no panel header over it. It is
    # not offered on the neighbouring screens, where back closes the panel
    # (the friends list) or offers to quit the game (the map).
    SORT_MENU: (
        (('CAN_RECEIVE_GIFT_BTN',),),  # any sort row closes the menu
        ((BACK_KEY,),),
    ),
    MAIN_MENU: (
        (('GBL_MENU_BTN', 'CLOSE_BTN'),),
    ),
    # Two taps, and the second is the point. The avatar reopens the panel on
    # whichever tab it was last left on, so a phone that reached the map from the
    # ME tab gets the ME tab back — which reads as a friend's screen, closes to
    # the map, and comes round again. Verified on the android-three: the walk rode that
    # loop until it ran out of steps. Asking for the FRIENDS tab straight after
    # opening the panel ends it, and costs nothing when the tab is already up.
    MAP: (
        (('AVATAR_BTN',), ('FRIENDS_TAB_BTN',)),
    ),
}
# Long enough for the worst walk that has been measured — a battle screen, which
# spends two steps finding out that nothing on it responds, then the exit, the
# map and the panel — with a couple of slow repaints on top.
WALK_HOME_STEPS = 10

# --- Battle screen self-heal ---
# If SORT_BTN does land on BATTLE the phone ends up on "Choose your league",
# sometimes behind a "Challenge <friend> to a ... battle?" modal, and CLOSE_BTN
# does nothing there. The escape is ordered rather than blind because the
# obvious taps are destructive on the wrong screen: NOT YET's position sits
# inside the Ultra League card on the league list, and BATTLE_EXIT_YES_BTN's
# sits inside the Great League card.
# Measured on device:
#   back  — clears the modal, leaving the league list; on the league list it
#           opens "Exit the Trainer Battle?"
#   door  — opens that dialog from the league list, is swallowed by the modal,
#           and DISMISSES the dialog when it is already showing
# So back must only be pressed when the modal is actually up, otherwise it opens
# the dialog and the following door tap closes it again — which is how an
# earlier version of this ended up tapping YES's position on the league list and
# selecting Great League. Hence the modal check below rather than a fixed
# sequence. Confirming YES drops to the map, which the walk home recognises and
# carries on from — this only has to get off the battle screens, not all the way
# back, which is why AVATAR_BTN and FRIENDS_TAB_BTN are no longer its business.
#
# The door and its YES are the two keys wanted, and phones mapped for battle.py
# already carry them under that script's names. The android-three had EXIT_BTN/EXIT_YES_BTN
# and none of the BATTLE_* spelling, so escape_battle_screen() returned False on
# the one screen it exists for and the guard went straight to refusing. Either
# spelling is accepted now rather than asking every config to repeat itself.
BATTLE_EXIT_KEYS     = ('BATTLE_EXIT_BTN', 'EXIT_BTN')
BATTLE_EXIT_YES_KEYS = ('BATTLE_EXIT_YES_BTN', 'EXIT_YES_BTN')
BATTLE_ESCAPE_DELAY = 4  # these screens animate slowly
# The modal is a large white panel over the darkened league list. Measured over
# this band: modal 247, league list 64.
MODAL_SAMPLE_X       = (0.20, 0.80)
MODAL_SAMPLE_Y       = (0.27, 0.32)
MODAL_SAMPLE_STEPS   = 6
MODAL_MIN_BRIGHTNESS = 150

GUARD = True


class DeviceAsyncWrapper(DeviceAsync):
    config: CONFIG
    display_id: str | None = None   # set by setup(); only foldables need it
    label: str = ''                 # set by setup(); prefixes this device's output


class AutoGifterError(Exception):
    pass


def log(device: DeviceAsyncWrapper, *parts):
    """Prints one line tagged with the device it came from.

    Devices run their cycles concurrently, so untagged output interleaves into
    something unreadable as soon as more than one phone is plugged in.
    """
    print(f'[{device.label}]', *parts)


async def tap(device: DeviceAsyncWrapper, point: list[int]):
    """Sends a tap at point to device."""
    x, y = point
    await device.shell(f'input tap {x} {y}')


async def pointer(device: DeviceAsyncWrapper, on: bool):
    """Turns on/off pointer location setting on `device`."""
    try:
        await device.shell(f'settings put system pointer_location {int(on)}')
    except Exception:
        print(f'Failed to turn {"on" if on else "off"} pointer location on', device.serial)


async def wait(seconds: float, use_modifier: bool = True):
    await asyncio.sleep(max(seconds + (SLEEP_MODIFIER if use_modifier else 0), 0))


async def find_display_id(device: DeviceAsyncWrapper) -> str | None:
    """The display to capture, or None when the device only has one.

    Foldables report two displays, and `screencap` with no -d prints
    "[Warning] Multiple displays found, but no display id specified!" onto
    stdout ahead of the framebuffer. That warning is 351 bytes, so the header
    unpack below reads the words "[War"/"ning" as the width and height and every
    screen read fails. On the android-three that meant screencap_raw() returned None for
    the whole run, silently leaving the friends list guard switched off while
    the script tapped on regardless.

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


async def screencap_raw(device: DeviceAsyncWrapper):
    """Grabs the raw framebuffer via `adb exec-out screencap`.
    Returns (width, height, pixel_offset, data), or None if it can't be read."""
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
        # `adb exec-out screencap` wedges every so often on this phone: the
        # device stays responsive and later screencaps work, but this one never
        # returns. Without the timeout the run just stops dead, mid-cycle, with
        # no output at all. Kill it and let the caller carry on unguarded.
        log(device, f'  Screen read timed out after {SCREENCAP_TIMEOUT}s, skipping the check')
        proc.kill()
        await proc.wait()
        return None
    except OSError:
        return None
    if len(data) < 16:
        return None
    width, height, pixel_format = struct.unpack('<III', data[:12])
    # Android 9+ appends a colorspace field, making the header 16 bytes.
    for offset in (16, 12):
        if width * height * 4 + offset == len(data):
            break
    else:
        return None
    if pixel_format != 1:  # not RGBA_8888
        return None
    return width, height, offset, data


def frame_image(frame) -> Image.Image:
    """The raw framebuffer as a picture, for the tests that work on shapes."""
    width, height, offset, data = frame
    return Image.frombytes(
        'RGBA', (width, height), data[offset:offset + width * height * 4]
    ).convert('RGB')


async def open_through_bag_full_notice(device: DeviceAsyncWrapper) -> bool:
    """Press OPEN on "Your Item Bag is full!", if that is what OPEN_BTN got."""
    frame = await screencap_raw(device)
    if frame is None:
        return False
    image = frame_image(frame)
    from . import trade_ios_android as cross

    metrics = cross.screen_metrics(image, None)
    if (
        metrics['card_band'] < BAG_FULL_MIN_CARD_BAND
        or metrics['card_edges'] >= BAG_FULL_MAX_CARD_EDGES
    ):
        return False
    from . import gbl_vision

    try:
        boxes = gbl_vision.recognize(image)
    except (gbl_vision.VisionOCRError, OSError):
        # Unread, it is left alone: pressing a pill on a card nobody has read
        # is how a run answers a question it was never asked.
        return False
    if BAG_FULL_PHRASE not in gbl_vision.normalize(' '.join(gbl_vision.lines(boxes))):
        return False
    point = cross.find_dialog_button(image)
    if point is None:
        log(device, '  Item Bag is full, and the notice has no button to press')
        return False
    log(device, '  Item Bag is full; opening the gift for the Stardust only')
    await tap(device, list(point))
    await wait(BAG_FULL_DELAY, True)
    return True


def save_frame(device: DeviceAsyncWrapper, frame, tag: str) -> Path | None:
    """Write a raw framebuffer out as a PNG, for a decision worth checking."""
    width, height, offset, data = frame
    stamp = time.strftime('%Y%m%d-%H%M%S')
    path = UNLIT_ARTIFACT_DIR / f'{device.label}-{stamp}-{tag}.png'
    try:
        UNLIT_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        Image.frombytes(
            'RGBA', (width, height), data[offset:offset + width * height * 4]
        ).convert('RGB').save(path)
    except (OSError, ValueError):
        return None
    log(device, f'  Saved the screen it decided on: {path}')
    return path


def frame_point_saturation(
    frame: tuple[int, int, int, bytes],
    point: list[int],
    radius: int = ACTION_SAMPLE_RADIUS,
) -> float:
    """Mean RGB channel spread around one configured action-button point."""
    width, height, offset, data = frame
    center_x = min(max(int(point[0]), 0), width - 1)
    center_y = min(max(int(point[1]), 0), height - 1)
    spreads: list[int] = []
    for y in range(
        max(0, center_y - radius),
        min(height - 1, center_y + radius) + 1,
        ACTION_SAMPLE_STEP,
    ):
        for x in range(
            max(0, center_x - radius),
            min(width - 1, center_x + radius) + 1,
            ACTION_SAMPLE_STEP,
        ):
            index = offset + (y * width + x) * 4
            red, green, blue = data[index], data[index + 1], data[index + 2]
            spreads.append(max(red, green, blue) - min(red, green, blue))
    return sum(spreads) / len(spreads) if spreads else 0.0


async def colored_action_available(
    device: DeviceAsyncWrapper, point: list[int]
) -> bool | None:
    """True for a colored action, false for grey, None if no screen was read."""
    frame = await screencap_raw(device)
    if frame is None:
        return None
    return frame_point_saturation(frame, point) >= ACTION_MIN_SATURATION


async def send_button_available(
    device: DeviceAsyncWrapper, point: list[int]
) -> bool:
    """Wait for SEND to light, rather than deciding on one look.

    Polled because the sheet is slow to arrive on the slowest phone and a
    single read of it has been wrong. An unreadable screen counts as no answer
    and is looked at again; only a run of readable, unlit looks says no.

    The last look is kept when the answer is no. An unlit SEND is the one
    reading that ends a run, and it says nothing about which screen was
    actually up; the frame does, so it is written out to be looked at.
    """
    last = None
    for attempt in range(SEND_POLL_READS):
        if attempt:
            await asyncio.sleep(SEND_POLL_GAP)
        frame = await screencap_raw(device)
        if frame is None:
            continue
        last = frame
        if frame_point_saturation(frame, point) >= ACTION_MIN_SATURATION:
            return True
    if last is not None:
        save_frame(device, last, 'send-unlit')
    return False


async def send_gift_action_available(device: DeviceAsyncWrapper) -> bool | None:
    """Check the movable gift icon separately from the shared tap point."""
    points = [
        device.config[key]
        for key in SEND_GIFT_SAMPLE_KEYS
        if key in device.config
    ] or [device.config['SEND_GIFT_BTN']]
    unreadable = False
    for point in points:
        available = await colored_action_available(device, point)
        if available:
            return True
        if available is None:
            unreadable = True
    return None if unreadable else False


def picture_rows(width: int, height: int, offset: int, data: bytes) -> tuple[int, int]:
    """First and last row of actual game picture, skipping black system bars."""
    limit = int(height * GUARD_MAX_LETTERBOX)
    columns = [int(width * f) for f in (0.25, 0.5, 0.75)]

    def dark(y: int) -> bool:
        for x in columns:
            i = offset + (y * width + x) * 4
            if (data[i] + data[i + 1] + data[i + 2]) / 3 >= GUARD_LETTERBOX_LEVEL:
                return False
        return True

    top, bottom = 0, height - 1
    while top < limit and dark(top):
        top += 1
    while bottom > height - 1 - limit and dark(bottom):
        bottom -= 1
    return top, bottom


def is_friends_panel(width: int, height: int, offset: int, data: bytes) -> bool:
    """True if the framebuffer's top band is the friends panel header.

    The header is shared by the panel's ME, FRIENDS and SOCIAL tabs, so this
    says "the panel is up", not which tab. `name_screen` settles that with the
    sort control.
    """
    x0, x1 = GUARD_SAMPLE_X
    y0, y1 = GUARD_SAMPLE_Y
    last = GUARD_SAMPLE_STEPS - 1
    top, span = picture_rows(width, height, offset, data)
    span -= top
    brightness = saturation = 0
    for yi in range(GUARD_SAMPLE_STEPS):
        y = top + int(span * (y0 + (y1 - y0) * yi / last))
        for xi in range(GUARD_SAMPLE_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / last))
            i = offset + (y * width + x) * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            brightness += (r + g + b) / 3
            saturation += max(r, g, b) - min(r, g, b)
    n = GUARD_SAMPLE_STEPS ** 2
    return brightness / n >= GUARD_MIN_BRIGHTNESS and saturation / n <= GUARD_MAX_SATURATION


def _sort_control_teal(pixel: tuple[int, int, int]) -> bool:
    """The dark teal of the friends list's round sort control."""
    red, green, blue = pixel
    return blue > red + 25 and green > red + 15 and red < 140 and blue > 90


def sort_control_showing(
    frame: tuple[int, int, int, bytes], point: list[int]
) -> bool:
    """True when SORT_BTN's own point is the teal sort disc."""
    width, height, offset, data = frame
    radius = max(int(width * SORT_FAB_RADIUS), 1)
    center_x = min(max(int(point[0]), 0), width - 1)
    center_y = min(max(int(point[1]), 0), height - 1)
    teal = total = 0
    for y in range(max(0, center_y - radius),
                   min(height - 1, center_y + radius) + 1, SORT_FAB_STEP):
        for x in range(max(0, center_x - radius),
                       min(width - 1, center_x + radius) + 1, SORT_FAB_STEP):
            i = offset + (y * width + x) * 4
            total += 1
            teal += _sort_control_teal((data[i], data[i + 1], data[i + 2]))
    return bool(total) and teal / total >= SORT_FAB_MIN_TEAL


def frame_image(frame: tuple[int, int, int, bytes]) -> Image.Image:
    """The framebuffer as a PIL image, for the gbl_home_recovery detectors."""
    width, height, offset, data = frame
    return Image.frombytes('RGBA', (width, height), data[offset:]).convert('RGB')


def name_screen(frame: tuple[int, int, int, bytes], config: CONFIG) -> str:
    """Which screen the framebuffer is showing, as one of the names above.

    Ordered cheapest and most certain first. The two panel tests come before the
    map and menu ones because the panel covers them.

    FRIEND_SCREEN is the fallback rather than a test of its own. It covers the
    whole stack a friend opens — the gift, the profile under it, the friendship
    detail over it — and the ME tab, whose coloured header reads no differently
    from a friend's. All of them close with the same X, so one name is enough.

    The battle screens land here too, and get named wrong — deliberately: their
    step is CLOSE_BTN, which does nothing there, and the walk hands over to the
    battle exit once it has spent that screen's steps. That is cheaper than a
    league-list colour test nobody has measured on these phones.
    """
    panel = is_friends_panel(*frame)
    sort_control = ('SORT_BTN' in config
                    and sort_control_showing(frame, config['SORT_BTN']))
    if panel:
        # The sort control is the friends list's own button; the other two tabs
        # have no sort at all, which is what SOCIAL was passing this test on.
        return FRIENDS_LIST if sort_control else FRIENDS_PANEL
    if sort_control:
        # The menu's field covers the header as well as the button under it.
        return SORT_MENU
    image = frame_image(frame)
    if gbl_home_recovery.main_menu_open(image):
        return MAIN_MENU
    if gbl_home_recovery.on_map(image):
        return MAP
    # A friend's screen is the one the cycle actually wants when it is not on
    # the list, and the only remaining screen it reaches by itself.
    return FRIEND_SCREEN


async def current_screen(device: DeviceAsyncWrapper) -> str | None:
    """The name of the screen on `device`, or None when it can't be read."""
    frame = await screencap_raw(device)
    return None if frame is None else name_screen(frame, device.config)


def is_challenge_modal(width: int, height: int, offset: int, data: bytes) -> bool:
    """True if a white "Challenge ... to a battle?" panel covers the league list."""
    x0, x1 = MODAL_SAMPLE_X
    y0, y1 = MODAL_SAMPLE_Y
    last = MODAL_SAMPLE_STEPS - 1
    brightness = 0
    for yi in range(MODAL_SAMPLE_STEPS):
        y = int(height * (y0 + (y1 - y0) * yi / last))
        for xi in range(MODAL_SAMPLE_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / last))
            i = offset + (y * width + x) * 4
            brightness += (data[i] + data[i + 1] + data[i + 2]) / 3
    return brightness / MODAL_SAMPLE_STEPS ** 2 >= MODAL_MIN_BRIGHTNESS


async def on_challenge_modal(device: DeviceAsyncWrapper) -> bool:
    """False when the screen can't be read, so back is only pressed on a sure match."""
    frame = await screencap_raw(device)
    return False if frame is None else is_challenge_modal(*frame)


def config_point(
    config: CONFIG, keys: tuple[str, ...]
) -> list[int] | str | None:
    """The first of `keys` this device carries, so configs can differ in spelling.

    BACK_KEY names a key rather than a point, and every phone has it, so it
    answers for itself rather than being looked up and dropped as missing.
    """
    for key in keys:
        if key == BACK_KEY:
            return BACK_KEY
        if key in config:
            return config[key]
    return None


def step_points(
    config: CONFIG, step: tuple[tuple[str, ...], ...]
) -> list[list[int] | str]:
    """The taps of one step that this device can actually make, in order."""
    points = [config_point(config, keys) for keys in step]
    return [point for point in points if point is not None]


async def press_back(device: DeviceAsyncWrapper):
    """Sends the hardware back key."""
    await device.shell('input keyevent 4')


async def step_tap(device: DeviceAsyncWrapper, point: list[int] | str):
    """One tap of a step: a point, or the back key when the step asked for it."""
    if point == BACK_KEY:
        await press_back(device)
    else:
        await tap(device, point)


async def escape_battle_screen(device: DeviceAsyncWrapper) -> bool:
    """Backs out of the battle screens, as far as the map.

    Only as far as the map: the walk home names that screen and carries on from
    it, so this does not need to know the way back to the friends list.

    Returns False if the device config has neither spelling of the exit keys.
    """
    door = config_point(device.config, BATTLE_EXIT_KEYS)
    yes = config_point(device.config, BATTLE_EXIT_YES_KEYS)
    if door is None or yes is None:
        return False
    log(device, '  Trying the battle screen exit')
    if await on_challenge_modal(device):
        # Only when it is showing: the modal swallows the door icon, and on the
        # league list this same press opens the dialog the door would.
        log(device, '  Challenge modal is up, clearing it')
        await press_back(device)
        await asyncio.sleep(BATTLE_ESCAPE_DELAY)
    for point in (door,   # opens "Exit the Trainer Battle?"
                  yes):   # confirms it, dropping to the map
        await tap(device, point)
        await asyncio.sleep(BATTLE_ESCAPE_DELAY)
    return True


async def walk_home(device: DeviceAsyncWrapper) -> bool:
    """Walks the phone back to the friends list, naming the screen every step.

    One tap per screen, chosen for that screen and no other, then the screen is
    named again. What stops it walking in circles is that arriving back on a
    screen it has already tried to leave advances that screen to its next step
    rather than repeating the one that did not work — whether the tap changed
    nothing at all or sent it round a loop. A screen that runs out of steps, or
    whose steps the device has no keys for, hands over to the battle exit: that
    is the one screen with no visible way out, and it gets a single turn.
    """
    tried_battle_exit = False
    attempts: dict[str, int] = {}
    for _ in range(WALK_HOME_STEPS):
        screen = await current_screen(device)
        if screen is None:
            # Unreadable screen: the guard stands down here exactly as it does
            # in ensure_screen, rather than tapping on a picture it cannot see.
            return True
        if screen == FRIENDS_LIST:
            return True
        steps = STEP_HOME[screen]
        start = index = attempts.get(screen, 0)
        # A step this phone has none of the keys for is skipped rather than
        # ending the walk, so the sort menu's back step still gets its turn on a
        # config that never mapped a sort row. Only running out of steps
        # altogether counts as having no way off the screen.
        points: list[list[int] | str] = []
        while index < len(steps) and not points:
            points = step_points(device.config, steps[index])
            index += 1
        attempts[screen] = index
        if not points:
            # Only from FRIEND_SCREEN, the name the battle screens get. The
            # exit's two taps are sent without reading the screen first, and on
            # the map they would land in the middle of the world — on a Pokemon
            # or a stop, opening something new to be stuck on.
            if screen == FRIEND_SCREEN and not tried_battle_exit:
                tried_battle_exit = True
                if await escape_battle_screen(device):
                    continue
            missing = ' or '.join(
                key for step in steps[start:] for keys in step for key in keys)
            log(device, f'  Out of ways off {screen}'
                        + (f' (no {missing} in the config)' if missing else ''))
            return False
        log(device, f'  On {screen} — leaving it')
        for n, point in enumerate(points):
            if n:
                await asyncio.sleep(GUARD_RECOVER_DELAY)
            await step_tap(device, point)
        await asyncio.sleep(GUARD_RECOVER_DELAY)
    log(device, f'  Still not home after {WALK_HOME_STEPS} steps')
    return False


async def ensure_screen(device: DeviceAsyncWrapper, step_name: str):
    """Gets the right screen up before `step_name` is tapped.

    Both of the guarded steps are reached from the friends list — SORT_BTN and
    NEXT_FRIEND_BTN are on it, and OPEN_BTN wants the friend that
    NEXT_FRIEND_BTN opens — so recovery is one walk home, plus that one tap for
    the steps that want a friend's screen. Raises rather than tapping a button
    on the wrong screen.
    """
    want_list = step_name in NEEDS_FRIENDS_LIST
    screen = await current_screen(device)
    if screen is None:
        return
    if screen == (FRIENDS_LIST if want_list else FRIEND_SCREEN):
        return
    where = FRIENDS_LIST if want_list else FRIEND_SCREEN
    log(device, f'  Not on {where} for {step_name} — walking back')
    if await walk_home(device):
        if not want_list:
            # Home is the friends list either way; a friend's screen is one tap
            # further on, and it is the tap the cycle would have made anyway.
            await tap(device, device.config['NEXT_FRIEND_BTN'])
            await asyncio.sleep(GUARD_RECOVER_DELAY)
        log(device, f'  Back on {where}')
        return
    # Refusing here is the whole point: SORT_BTN on a friend's screen is BATTLE.
    raise AutoGifterError(
        f'Could not get back to {where} for {step_name} — stopping instead of '
        'tapping blind.',
        'If the phone is on a battle screen, leave it with the door icon '
        '(top left) > YES, then go back to the friends list and retry.',
        f'Setting {" or ".join(BATTLE_EXIT_KEYS)} and '
        f'{" or ".join(BATTLE_EXIT_YES_KEYS)} in the config lets that happen '
        'automatically, and AVATAR_BTN and FRIENDS_TAB_BTN let the walk home '
        'come back from the map.')


async def gift_sequence(
    device: DeviceAsyncWrapper,
    *,
    all_mode: bool = False,
    outgoing_available: bool = True,
) -> GiftCycleResult:
    """Run one cycle, detecting the same active actions as the iPhone runner."""
    opened = False
    sent = False
    skip_send = False
    for step in GIFT_STEPS:
        if GUARD and step.name in NEEDS_FRIENDS_LIST | NEEDS_FRIEND_SCREEN:
            await ensure_screen(device, step.name)

        if all_mode and step.name == 'OPEN_BTN':
            available = await colored_action_available(device, device.config[step.name])
            if available is None:
                await asyncio.sleep(0.5)
                available = await colored_action_available(device, device.config[step.name])
            if available is None:
                available = True
            if not available:
                log(device, 'No incoming gift on friend; continuing Send Gift')
                continue
            opened = True

        if all_mode and step.name == 'SEND_GIFT_BTN':
            available = await send_gift_action_available(device)
            if available is None:
                await asyncio.sleep(0.5)
                available = await send_gift_action_available(device)
            if available is None:
                available = True
            if not outgoing_available or not available:
                log(device, 'Friend cannot receive a gift; skipping send')
                skip_send = True
                continue

        if all_mode and step.name == 'FIRST_GIFT_BTN' and skip_send:
            continue

        if all_mode and step.name == 'SEND_BTN':
            if skip_send:
                continue
            available = await send_button_available(device, device.config[step.name])
            if not available:
                log(device, 'SEND is not lit; nothing sent this cycle')
                outgoing_available = False
                skip_send = True
                continue
            sent = True

        log(device, 'Sending', step.name, f'x{step.taps}' if step.taps > 1 else '')
        for n in range(step.taps):
            if n:
                await wait(step.tap_gap, use_modifier=False)
            await tap(device, device.config[step.name])
        await wait(step.delay_after, step.use_delay_modifier)

        if all_mode and step.name == 'OPEN_BTN':
            await open_through_bag_full_notice(device)
    return GiftCycleResult(opened, sent, outgoing_available)


async def gift_process_one(
    device: DeviceAsyncWrapper, n: int, *, all_mode: bool = False
) -> int:
    """Run a fixed count, or stop independently when this phone becomes idle."""
    done = 0
    idle_cycles = 0
    unlit_cycles = 0
    await pointer(device, True)
    try:
        for i in range(1, n + 1):
            label = str(i) if all_mode else f'{i} of {n}'
            log(device, f'Starting gift {label}')
            # Each cycle asks the phone again rather than carrying last cycle's
            # answer forward. A cycle that found SEND unlit is the one that has
            # to be repeated to find out why, and skipping the send in it would
            # guarantee the same answer.
            result = await gift_sequence(device, all_mode=all_mode)
            done += 1
            if all_mode:
                unlit_cycles = 0 if result.outgoing_available else unlit_cycles + 1
            if all_mode and unlit_cycles >= EMPTY_BAG_CYCLES:
                log(
                    device,
                    f'Stopping: SEND stayed unlit for {EMPTY_BAG_CYCLES} '
                    'cycles, so the gift bag is empty',
                )
                break
            if all_mode and unlit_cycles:
                log(
                    device,
                    f'Nothing sent ({unlit_cycles}/{EMPTY_BAG_CYCLES} cycles '
                    'with SEND unlit); trying the next friend',
                )
            if all_mode:
                if result.opened or result.sent:
                    idle_cycles = 0
                else:
                    idle_cycles += 1
                    log(device, f'No action completed ({idle_cycles}/{MAX_IDLE_CYCLES} idle cycles)')
                    if idle_cycles >= MAX_IDLE_CYCLES:
                        log(device, f'Stopping after {MAX_IDLE_CYCLES} idle cycles')
                        break
    finally:
        await pointer(device, False)
    return done


async def gift_process(
    devices: list[DeviceAsyncWrapper], n: int, *, all_mode: bool = False
):
    """Run every Android concurrently; idle phones stop independently.

    Concurrently rather than one phone after another: a cycle is almost all
    waiting on the game server, so several phones take about as long as one.
    adb multiplexes per serial, so the devices do not contend.

    Each device is isolated. One phone stopping — a stuck screen the guard will
    not tap through, a cable pulled — leaves the others running, and the failure
    is reported per device at the end rather than taking the whole run down.
    """
    if n < 1:
        return
    results = await asyncio.gather(*(
        gift_process_one(d, n, all_mode=all_mode) for d in devices
    ),
                                   return_exceptions=True)
    print()
    for device, result in zip(devices, results):
        if isinstance(result, KeyboardInterrupt):
            raise result
        if isinstance(result, BaseException):
            log(device, 'stopped:', ' '.join(map(str, result.args)) or repr(result))
        else:
            suffix = 'until idle' if all_mode else f'of {n}'
            log(device, f'finished {result} cycle(s) {suffix}')


async def get_config(device: DeviceAsyncWrapper) -> CONFIG:
    """Pulls config file from device and parses it. Sets the `config` attribute on success."""
    config_file_path = CONFIG_FILE_DIR + CONFIG_FILE_NAME
    tmp_file_path = Path(TMP_FILE_NAME.format(re.sub(r'\W', '_', device.serial)))
    try:
        await device.pull(config_file_path, tmp_file_path)
        content = tmp_file_path.read_text() if tmp_file_path.exists() else ''
    finally:
        tmp_file_path.unlink(missing_ok=True)
    if not content:
        raise AutoGifterError(f'Found no config file at {config_file_path}')
    config: CONFIG = yaml.safe_load(content)
    assert isinstance(config, dict), 'Incorrect config file format (should be an object with keys)'
    if not GIFT_KEYS <= set(config.keys()):
        raise AutoGifterError(f'Missing config key(s): {GIFT_KEYS - set(config.keys())}')
    for coords in config.values():
        assert isinstance(coords, list) and len(coords) == 2 and all(isinstance(i, int) for i in coords),\
            'Invalid coords format in config (should be list with two integers)'
    device.config = config
    return config


async def setup() -> list[DeviceAsyncWrapper]:
    """Finds every connected device and loads a config from each.

    Each phone carries its own config, because the coordinates are absolute
    pixels and no two models share a screen. A device whose config will not load
    is reported and dropped rather than aborting the rest — one phone with a
    stale config should not stop the others from running.
    """
    client = ClientAsync()
    devices: list[DeviceAsyncWrapper] = await client.devices()
    if not devices:
        raise AutoGifterError('No devices found')
    width = max(len(device.serial) for device in devices)
    ready: list[DeviceAsyncWrapper] = []
    for device in devices:
        device.label = device.serial.ljust(width)
        try:
            await get_config(device)
        except (AutoGifterError, AssertionError, ParserError) as e:
            log(device, 'skipped —', ' '.join(map(str, e.args)))
            continue
        device.display_id = await find_display_id(device)
        extra = f', reading display {device.display_id}' if device.display_id else ''
        log(device, f'ready, {len(device.config)} config keys{extra}')
        ready.append(device)
    if not ready:
        raise AutoGifterError('No devices with a usable config')
    return ready


def interface():
    """Runs the main loop asking for user input."""
    global SLEEP_MODIFIER, GUARD
    print(
        '\n'
        ' ##                          ## \n'
        '##   AutoGifter by jonaro00   ##\n'
        ' ##                          ## \n'
    )
    devices: list[DeviceAsyncWrapper] = asyncio.run(setup())
    print(f"\nRunning on {len(devices)} device(s): {', '.join(d.serial for d in devices)}")
    print("\nCommands:")
    print("  <number>     open and send that many gifts on every device"
          " (start each on the friends list)")
    print("  delay <val>  get/set the extra delay modifier")
    print("  guard [on|off]  get/set the friends list check (keeps SORT_BTN off BATTLE)")
    print("  q            quit")
    while True:
        print()
        try:
            i = input("Number of gifts? ('q' to quit) > ").strip()
            il = i.lower()
            if il == 'q':
                break
            if il.startswith('delay'):
                args = i.split()
                if len(args) == 2:
                    SLEEP_MODIFIER = float(args[1])
                print('Current extra delay:', SLEEP_MODIFIER)
                continue
            if il.startswith('guard'):
                args = il.split()
                if len(args) == 2 and args[1] in ('on', 'off'):
                    GUARD = args[1] == 'on'
                print('Friends list guard:', 'on' if GUARD else 'off')
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
            print(f'Starting {n} gift(s) on {len(devices)} device(s) (Ctrl+C to cancel)...')
            asyncio.run(gift_process(devices, n))
        except KeyboardInterrupt:
            continue
        except AutoGifterError as e:
            print('\n'.join(map(str, e.args)))
        except Exception as e:
            print(e)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Android AutoGifter")
    parser.add_argument("--config", type=Path, help="Path to device config yaml")
    parser.add_argument("--count", type=int, help="Number of gift cycles to run")
    parser.add_argument("--all", action="store_true", help="Run until no outgoing gifts remain")
    parser.add_argument("--max-cycles", type=int, default=100, help="Max cycles cap for --all")
    parser.add_argument("--devices", nargs="+", help="Devices filter (e.g. android-two)")
    parser.add_argument("--no-guard", action="store_true", help="Disable friends list guard")
    parser.add_argument("--allow-empty", action="store_true", help="Allow running with no devices attached")
    return parser.parse_args()


def main():
    global GUARD
    args = parse_args()
    if args.no_guard:
        GUARD = False

    if len(sys.argv) > 1:
        devices: list[DeviceAsyncWrapper] = asyncio.run(setup())
        if args.devices and "all" not in args.devices:
            devices = [
                d for d in devices
                if any(dev in d.serial or dev in getattr(d, "label", "") for dev in args.devices)
            ]
        if not devices:
            if args.allow_empty:
                print("No matching connected Android devices; exiting (--allow-empty set)")
                return
            print("No matching connected Android devices")
            sys.exit(1)

        all_mode = args.all
        n = args.max_cycles if all_mode else (args.count or 1)
        try:
            asyncio.run(gift_process(devices, n, all_mode=all_mode))
        except AutoGifterError as e:
            print('\n'.join(map(str, e.args)))
        except Exception as e:
            print('Unexpected error:', e.__class__.__name__, e.args)
        except KeyboardInterrupt:
            pass
    else:
        try:
            interface()
        except AutoGifterError as e:
            print('\n'.join(map(str, e.args)))
        except Exception as e:
            print('Unexpected error:', e.__class__.__name__, e.args)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('gifts', 'send_gifts.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == '__main__':
    main()
