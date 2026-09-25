#!/usr/bin/env python3
"""
luckyTrash — transfers every Pokémon in the search filter already applied in
the Pokémon storage list, one at a time.

The storage list has no multi-select for transferring, so each Pokémon has to
be walked through its own four screens:

    list -> tap the first tile
    detail -> tap the menu (the teal bars, bottom right)
    menu -> tap TRANSFER (the bottom row)
    confirm dialog -> tap YES
    second dialog, lucky Pokémon only -> tap YES again

and the list comes back with that Pokémon gone and the next one in its place,
so the tap point never moves.

There are two dialogs, not one. The professor's ("You cannot undo a transfer")
is followed by "Do you really want to transfer this Lucky Pokémon?", which is
shorter, so its buttons sit about 90px higher on the android-three. Rather than carry a
coordinate per dialog per phone, the YES pill is found in the frame: scanning
down the middle of a dialog crosses green background, the white card, the solid
pill, then white again, and the pill is the one wide non-white run inside the
card — the NO row is thin text and stays mostly white. That lands within 3px of
hand-measured centres on both phones and on both dialogs, and it does not care
how many lines of text a Pokémon's name pushes the card to.

WHAT KEEPS THIS SAFE

The only irreversible tap in the run is YES, and the game will not let it be
undone. So no tap is ever sent blind: the screen is read before each one and
has to be the screen that tap belongs on. In particular YES is only ever sent
when the confirm dialog is actually up. A missed tap earlier in the sequence
leaves the run somewhere unexpected, backs out, and never reaches YES.

The search filter is live app state — it lives in the running game, not in any
config file, and it is gone the moment the game restarts. So:

  * the search box is fingerprinted before the first transfer and re-checked
    before every single one. If it changes or empties, the run stops at once.
    Whatever is in the list then is not what the user picked, and transferring
    it would be unrecoverable.
  * the run refuses to start on an empty search box at all.

Ground truth that a transfer really landed is the "(241)" count beside the
POKÉMON tab: it drops by one and nothing else on that little band moves. If it
does not move, the run stops rather than tapping on into a screen it has
misread.

Nothing here restarts or force-stops the game — that would clear the filter.

CONFIG

Per-device, in /storage/self/primary/AutoTraderConfig.yaml, same file the other
scripts use. Needs FIRST_PKMN_BTN and X_BTN (already there for trade.py) plus
TILE_PITCH, TRASH_MENU_BTN, TRANSFER_BTN, TRANSFER_YES_BTN and
TRANSFER_NO_BTN. The screen-reading bands fall back to the values measured on
the compact-layout device and can be overridden per device.
"""

import asyncio
import re
import struct
from pathlib import Path

try:
    from ppadb.client_async import ClientAsync
    from ppadb.device_async import DeviceAsync
    import yaml
    from yaml.parser import ParserError
except ModuleNotFoundError as e:
    print(e)
    print('Run "pip install -r docs/requirements.txt" to install required packages.')
    exit(1)

CONFIG_FILE_DIR  = '/storage/self/primary/'
CONFIG_FILE_NAME = 'AutoTraderConfig.yaml'
TMP_FILE_NAME    = 'tmp_trash_{}.yaml'
CONFIG = dict[str, list[int]]

TRASH_KEYS = {'FIRST_PKMN_BTN', 'X_BTN', 'TILE_PITCH', 'TRASH_MENU_BTN',
              'TRANSFER_BTN', 'TRANSFER_YES_BTN', 'TRANSFER_NO_BTN'}

SLEEP_MODIFIER = 0

# Delays (seconds). Every one of these is followed by a screen read that has to
# agree before the next tap, so they only need to be long enough for the usual
# case; a slow frame costs a retry, not a wrong tap.
DETAIL_DELAY   = 2.5   # the tile expands into the detail screen
MENU_DELAY     = 1.5
DIALOG_DELAY   = 1.5
TRANSFER_DELAY = 4     # YES, the transfer animation, then back to the list
LIST_RETRIES   = 6     # polls waiting for the list to come back after a YES
LIST_POLL      = 1.5
BACKOUT_STEPS  = 5     # screens between the dialog and the list, plus slack
MAX_DIALOGS    = 4     # two are expected; the cap only bounds a misread

SCREENCAP_TIMEOUT = 20   # adb exec-out screencap wedges outright now and then

_ADB = Path(__file__).resolve().parent.parent / 'adb'
ADB_BINARY = str(_ADB) if _ADB.exists() else 'adb'

# --- Telling the four screens apart -----------------------------------------
# Measured on the compact-layout device (720x1600) across one saved frame of each screen:
#
#              top bright   top green   mid bright
#   list           235.6         8.7        237.1
#   detail         133.6         6.5        245.3
#   menu           118.1        65.4        110.1
#   confirm        118.1        65.4        240.9
#
# Green splits the two full-screen green ones off first, then mid brightness
# separates the menu from the white dialog card sitting on top of it, and top
# brightness separates the near-white list header from the detail screen's
# darker hero image. Every gap is wide; nothing here is a close call.
PROBE_X     = (0.10, 0.90)
TOP_BAND_Y  = (110, 200)   # per-mille of the screen height
MID_BAND_Y  = (420, 480)
PROBE_STEPS = 24
GREEN_ON    = 30    # G - (R + B) / 2 on the green screens; 9 or less elsewhere
TOP_BRIGHT  = 190   # list 236 against detail 134
MID_BRIGHT  = 190   # dialog 241 against menu 110

# --- The search box ---------------------------------------------------------
# Binarised into a crude glyph pattern, the same trick berry.py uses on the
# defender's name banner. Two frames five minutes apart, same filter, matched
# on every one of 640 cells, so the bar can sit high.
QUERY_X       = (0.25, 0.86)   # inside the pill, clear of the magnifier and X
QUERY_BAND_Y  = (183, 208)
QUERY_NX, QUERY_NY = 64, 10
QUERY_INK     = 140   # the pill reads 205 mean, its text is dark teal
QUERY_MATCH   = 0.98
QUERY_MIN_INK = 0.03  # a real filter measured 0.142; an empty box is bare
# "Bare" is not the same on every phone. On the android-one the magnifier and the
# greyed "Search" placeholder sit inside that x span, so an empty box reads
# 0.039 and clears the bar that exists to stop a run on an unfiltered list --
# the one check standing between this and transferring whatever happens to be
# on screen. QUERY_X_MILLE overrides the span per device, in per-mille of the
# width, to a stretch the placeholder does not reach: the android-one then reads
# 0.000 empty. The failure it introduces is a short filter not reaching the
# span either, which refuses to run. That is the direction to fail in.
#
# Before any of that is believed, the search box has to be there at all.
# screen_of() only reads colour, and the TAGS tab is the same near-white as the
# list, so it classifies as 'list' -- and its first tag card sits exactly where
# the search box should be, reading 0.041 of ink off the artwork and passing
# for a filter. Requiring the sampled span to be mostly flat pill background
# settles it: 1.000 on both android-one list screens, 0.692 and 0.745 on the Motorolas
# with long filters typed into them, 0.008 on the TAGS tab.
QUERY_PILL_BRIGHT = 200
QUERY_PILL_SAT    = 35
QUERY_PILL_MIN    = 0.40

# --- The tag view -----------------------------------------------------------
# A tag is a filter the search box knows nothing about: the box sits empty and
# the tag itself is what narrows the list, so the ink test above would refuse a
# perfectly good filter. The strip above the box is the only place that says a
# tag is applied. Scanning it for saturated columns on the android-one: the TrashLucky
# view gives 5 of 180, the TAGS tab 0, and the unfiltered POKEMON tab 0 -- the
# tag's colour dot against grey tab labels and dark text on white, with nothing
# in between. The whole strip is scanned rather than one point because the dot
# sits left of the name and so moves with how long the name is.
#
# The edges of that strip were measured too and thrown out: "TAGS" and "EGGS"
# render light enough to read as blank, so a tag view and the unfiltered tab
# both score 0.000 there. The dot is the only thing that actually separates
# them, so it is the only thing this trusts.
#
# Opt-in per device through TAG_BAND_Y. A phone without that key is search-only
# and behaves exactly as it did before.
TAG_BAND_Y     = (55, 75)    # per-mille of the screen height
TAG_XS         = (0.05, 0.95)
TAG_NX, TAG_NY = 180, 12
TAG_SAT        = 60   # channel spread; the dot clears it, grey text never does
TAG_DOT_COLS   = 2    # of TAG_NX. The dot gives 5, every other list screen 0.
TAG_DOT_FRAC   = 0.5  # of a column's samples, before it counts as a dot column
TAG_FP_XS      = (0.20, 0.80)   # the name, for the mid-run fingerprint
TAG_FP_NX, TAG_FP_NY = 64, 10

# --- The result count -------------------------------------------------------
# "(241)" under the POKEMON tab. Same two frames differed by 0 of 1024 cells,
# so any real movement stands clear of the noise floor.
COUNT_X      = (0.42, 0.56)
COUNT_BAND_Y = (120, 138)
COUNT_NX, COUNT_NY = 64, 16
COUNT_MOVED  = 3

# --- Finding the buttons on a transfer dialog -------------------------------
# Measured across the professor's dialog and the lucky one on the android-three and the
# professor's on the moto: the found centre landed +1, -3 and +0 px from the
# hand-measured ones, and NO sat 1.30, 1.32 and 1.34 pill heights below YES.
DIALOG_X      = (0.30, 0.70)   # inside the pill, which spans about 0.22 to 0.78
DIALOG_Y      = (0.20, 0.90)
DIALOG_STEPS  = 200
DIALOG_XSTEPS = 32
DIALOG_WHITE  = 225
DIALOG_CARD   = 0.90   # a blank card row is white right across that x span
DIALOG_PILL   = 0.20   # the pill leaves none of it white; NO leaves most
DIALOG_PILL_MIN = 0.03  # of the screen: real pills are 0.053 and 0.056
NO_DROP       = 1.32   # pill heights from the YES centre down to NO

# --- The tile grid ----------------------------------------------------------
# Only the first three rows are used: the lower ones sit under the close and
# sort buttons.
#
# Occupancy is read off the name label, a third of a row below the tap point,
# rather than off the sprite. The android-three's matches are CP 11-15 babies with tiny
# sprites and two-digit CP, and a box on the sprite scored 0.013 there against
# 0.060 on the moto -- no margin at all over a background that is not the clean
# zero it looks like (0.007 to 0.010 of a bare strip falls below the ink level).
# Every tile has a name and a bar under it whatever the sprite does: on the
# label the same tiles read 0.030 to 0.080 across both phones.
#
# The bar then sits well under that rather than midway, because the two ways of
# getting this wrong are not equal. Reading an occupied tile as empty ends the
# run early looking like success, which is the failure that hides; reading an
# empty one as occupied just taps nothing, skips, and stops with a message.
TILE_HALF_X, TILE_HALF_Y = 0.110, 0.022
TILE_DROP      = 0.33   # of a row's pitch, from the tap point down to the name
# 20 steps put ~14 px between samples across a android-three name label while the letter
# strokes are ~6 px wide, so a short name aliased away to almost nothing: Abra
# read 0.010 against a threshold of 0.005. At 60 the strokes cannot be stepped
# over, and measured on both phones real names hold 0.047-0.097 while genuinely
# empty background reads 0.000-0.002. 0.020 sits an order of magnitude clear of
# each. Nothing here is destructive if it misreads -- a slot called empty ends
# the run, and a slot wrongly called occupied gets tapped and finds no detail
# screen -- but at 20 steps a real Abra could have stopped a run.
TILE_STEPS     = 60
TILE_INK       = 150
TILE_OCCUPIED  = 0.020
TILE_COLS, TILE_ROWS = 3, 3


class DeviceAsyncWrapper(DeviceAsync):
    config: CONFIG
    display_id: str | None = None   # set by setup(); only foldables need it
    label: str = ''


class LuckyTrashError(Exception):
    pass


def log(device: DeviceAsyncWrapper, *parts):
    print(f'[{device.label}]', *parts)


async def tap(device: DeviceAsyncWrapper, point: list[int]):
    """Sends a tap at point to device."""
    x, y = point
    # Uses a tiny swipe over 100 ms for increased reliability, as in trade.py.
    await device.shell(f'input swipe {x} {y} {x+1} {y+1} 100')


async def wait(seconds: float, use_modifier: bool = True):
    await asyncio.sleep(max(seconds + (SLEEP_MODIFIER if use_modifier else 0), 0))


async def find_display_id(device: DeviceAsyncWrapper) -> str | None:
    """The display to capture, or None when the device only has one.

    Foldables report two displays, and `screencap` with no -d prints a warning
    onto stdout ahead of the framebuffer, so the header unpack reads that text
    as the width and height and every screen read fails.
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

    Returns (width, height, offset, data), or None if the screen can't be read.
    """
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
        print(f'    Screen read timed out after {SCREENCAP_TIMEOUT}s')
        proc.kill()
        await proc.wait()
        return None
    except OSError:
        return None
    if len(data) < 12:
        return None
    width, height, _fmt = struct.unpack('<III', data[:12])
    offset = 16 if len(data) >= width * height * 4 + 16 else 12
    if len(data) < width * height * 4 + offset:
        return None
    return width, height, offset, data


def band_of(device: DeviceAsyncWrapper, key: str, fallback: tuple[int, int]) -> tuple[float, float]:
    """This device's vertical band for `key`, as fractions of the screen."""
    y0, y1 = device.config.get(key, fallback)
    return y0 / 1000, y1 / 1000


def probe(frame, band: tuple[float, float]) -> tuple[float, float]:
    """Mean brightness and mean greenness over a horizontal band."""
    width, height, offset, data = frame
    y0, y1 = band
    x0, x1 = PROBE_X
    bright = green = 0.0
    for yi in range(PROBE_STEPS):
        y = int(height * (y0 + (y1 - y0) * yi / (PROBE_STEPS - 1)))
        for xi in range(PROBE_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / (PROBE_STEPS - 1)))
            i = offset + (y * width + x) * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            bright += (r + g + b) / 3
            green += g - (r + b) / 2
    n = PROBE_STEPS ** 2
    return bright / n, green / n


def screen_of(device: DeviceAsyncWrapper, frame) -> str:
    """Which of the four screens is showing: list, detail, menu, confirm.

    Returns 'unknown' for anything else — a raid banner, the map, a network
    error. Nothing is tapped on an unknown screen.
    """
    top_bright, top_green = probe(frame, band_of(device, 'TOP_BAND_Y', TOP_BAND_Y))
    mid_bright, _ = probe(frame, band_of(device, 'MID_BAND_Y', MID_BAND_Y))
    if top_green >= GREEN_ON:
        return 'confirm' if mid_bright >= MID_BRIGHT else 'menu'
    if top_bright >= TOP_BRIGHT:
        return 'list'
    if mid_bright >= MID_BRIGHT:
        return 'detail'
    return 'unknown'


def band_bits(device: DeviceAsyncWrapper, frame, x_span: tuple[float, float],
              key: str, fallback: tuple[int, int], nx: int, ny: int,
              x_key: str | None = None) -> list[bool]:
    """Binarises a band of dark-on-light text into a crude glyph pattern.

    `x_key` names an optional per-device horizontal span, in per-mille of the
    width the way the vertical bands are in per-mille of the height.
    """
    width, height, offset, data = frame
    y0, y1 = band_of(device, key, fallback)
    if x_key and x_key in device.config:
        x0, x1 = device.config[x_key]
        x_span = (x0 / 1000, x1 / 1000)
    bits = []
    for yi in range(ny):
        y = int(height * (y0 + (y1 - y0) * yi / (ny - 1)))
        for xi in range(nx):
            x = int(width * (x_span[0] + (x_span[1] - x_span[0]) * xi / (nx - 1)))
            i = offset + (y * width + x) * 4
            bits.append((data[i] + data[i + 1] + data[i + 2]) / 3 < QUERY_INK)
    return bits


def query_bits(device: DeviceAsyncWrapper, frame) -> list[bool]:
    return band_bits(device, frame, QUERY_X, 'QUERY_BAND_Y', QUERY_BAND_Y,
                     QUERY_NX, QUERY_NY, 'QUERY_X_MILLE')


def count_bits(device: DeviceAsyncWrapper, frame) -> list[bool]:
    return band_bits(device, frame, COUNT_X, 'COUNT_BAND_Y', COUNT_BAND_Y,
                     COUNT_NX, COUNT_NY)


def pill_fraction(device: DeviceAsyncWrapper, frame) -> float:
    """How much of the search box's span is flat pill background.

    Low means there is no search box under those coordinates, so nothing read
    out of them means anything.
    """
    width, height, offset, data = frame
    y0, y1 = band_of(device, 'QUERY_BAND_Y', QUERY_BAND_Y)
    x0, x1 = QUERY_X
    if 'QUERY_X_MILLE' in device.config:
        x0, x1 = (v / 1000 for v in device.config['QUERY_X_MILLE'])
    on = total = 0
    for yi in range(QUERY_NY):
        y = int(height * (y0 + (y1 - y0) * yi / (QUERY_NY - 1)))
        for xi in range(QUERY_NX):
            x = int(width * (x0 + (x1 - x0) * xi / (QUERY_NX - 1)))
            i = offset + (y * width + x) * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            total += 1
            on += ((r + g + b) / 3 >= QUERY_PILL_BRIGHT
                   and max(r, g, b) - min(r, g, b) <= QUERY_PILL_SAT)
    return on / total if total else 0.0


def tag_dot_columns(device: DeviceAsyncWrapper, frame) -> int:
    """Columns of the title strip carrying a saturated colour: a tag's dot."""
    width, height, offset, data = frame
    y0, y1 = band_of(device, 'TAG_BAND_Y', TAG_BAND_Y)
    columns = 0
    for xi in range(TAG_NX):
        x = int(width * (TAG_XS[0] + (TAG_XS[1] - TAG_XS[0]) * xi / (TAG_NX - 1)))
        on = 0
        for yi in range(TAG_NY):
            y = int(height * (y0 + (y1 - y0) * yi / (TAG_NY - 1)))
            i = offset + (y * width + x) * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            on += max(r, g, b) - min(r, g, b) >= TAG_SAT
        columns += on / TAG_NY >= TAG_DOT_FRAC
    return columns


def tag_bits(device: DeviceAsyncWrapper, frame) -> list[bool]:
    return band_bits(device, frame, TAG_FP_XS, 'TAG_BAND_Y', TAG_BAND_Y,
                     TAG_FP_NX, TAG_FP_NY)


def tile_point(device: DeviceAsyncWrapper, slot: int) -> list[int]:
    """Screen point of grid slot `slot`, counting across then down from 0.

    FIRST_TILE_BTN if the device has one, otherwise trade.py's FIRST_PKMN_BTN.
    The storage grid and the trade picker's grid line up on the moto, but they
    are different screens, so a device whose trade point is wrong here (or is
    only a guess) can carry its own without disturbing trade.py.
    """
    x, y = device.config.get('FIRST_TILE_BTN') or device.config['FIRST_PKMN_BTN']
    px, py = device.config['TILE_PITCH']
    return [x + px * (slot % TILE_COLS), y + py * (slot // TILE_COLS)]


def tile_ink(device: DeviceAsyncWrapper, frame, slot: int) -> float:
    """Fraction of dark pixels on a grid slot's name label: how an occupied
    tile is told from an empty one."""
    width, height, offset, data = frame
    cx, cy = tile_point(device, slot)
    cy += int(device.config['TILE_PITCH'][1] * TILE_DROP)
    on = total = 0
    for yi in range(TILE_STEPS):
        y = int(cy + height * TILE_HALF_Y * (2 * yi / (TILE_STEPS - 1) - 1))
        for xi in range(TILE_STEPS):
            x = int(cx + width * TILE_HALF_X * (2 * xi / (TILE_STEPS - 1) - 1))
            if not (0 <= x < width and 0 <= y < height):
                continue
            i = offset + (y * width + x) * 4
            total += 1
            on += (data[i] + data[i + 1] + data[i + 2]) / 3 < TILE_INK
    return on / total if total else 0.0


def dialog_pill(frame) -> tuple[int, int] | None:
    """Top and bottom of the YES pill on a transfer dialog, or None.

    Found rather than configured, because the professor's dialog and the lucky
    one are different heights and so are the two phones. See the module note.
    """
    width, height, offset, data = frame
    rows = []
    for yi in range(DIALOG_STEPS):
        y = int(height * (DIALOG_Y[0] + (DIALOG_Y[1] - DIALOG_Y[0])
                          * yi / (DIALOG_STEPS - 1)))
        white = 0
        for xi in range(DIALOG_XSTEPS):
            x = int(width * (DIALOG_X[0] + (DIALOG_X[1] - DIALOG_X[0])
                             * xi / (DIALOG_XSTEPS - 1)))
            i = offset + (y * width + x) * 4
            white += (data[i] + data[i + 1] + data[i + 2]) / 3 >= DIALOG_WHITE
        rows.append((y, white / DIALOG_XSTEPS))

    card = [y for y, w in rows if w >= DIALOG_CARD]
    if not card:
        return None
    top, bottom = min(card), max(card)
    best = run = None
    for y, w in rows:
        if top < y < bottom and w < DIALOG_PILL:
            run = (y, y) if run is None else (run[0], y)
            continue
        if run and (best is None or run[1] - run[0] > best[1] - best[0]):
            best = run
        run = None
    if run and (best is None or run[1] - run[0] > best[1] - best[0]):
        best = run
    if best is None or best[1] - best[0] < height * DIALOG_PILL_MIN:
        return None   # a few stray rows, not a button
    return best


def dialog_buttons(device: DeviceAsyncWrapper, frame) -> tuple[list[int], list[int]]:
    """(YES, NO) tap points on the dialog showing in `frame`.

    Falls back to the configured points, which are the professor's dialog, if
    the pill can't be found — but then the caller's screen check has already
    said a dialog is up, so a failed scan means something is off and the
    fallback is only there to keep NO reachable for backing out.
    """
    width = frame[0]
    pill = dialog_pill(frame)
    if pill is None:
        return device.config['TRANSFER_YES_BTN'], device.config['TRANSFER_NO_BTN']
    yes = (pill[0] + pill[1]) // 2
    return [width // 2, yes], [width // 2, yes + int((pill[1] - pill[0]) * NO_DROP)]


async def read_screen(device: DeviceAsyncWrapper) -> tuple[str, tuple]:
    """One framebuffer read, and which screen it is."""
    frame = await screencap_raw(device)
    if frame is None:
        return 'unreadable', ()
    return screen_of(device, frame), frame


async def back_out(device: DeviceAsyncWrapper) -> bool:
    """Walks back to the list from wherever the run got to.

    Each step reads the screen first, so this never taps its way deeper into
    something it has misread. NO on the dialog returns to the menu, not the
    list, which is why this loops instead of tapping a fixed sequence.
    """
    for _ in range(BACKOUT_STEPS):
        state, _frame = await read_screen(device)
        if state == 'list':
            return True
        if state == 'confirm':
            await tap(device, dialog_buttons(device, _frame)[1])
        elif state == 'menu':
            await tap(device, device.config['TRASH_MENU_BTN'])
        elif state == 'detail':
            await tap(device, device.config['X_BTN'])
        else:
            return False
        await wait(MENU_DELAY)
    return False


async def transfer_one(device: DeviceAsyncWrapper, slot: int):
    """Walks the Pokémon in `slot` from the list to the professor.

    Returns the list's frame once it is gone. Returns None when the sequence
    never reached a dialog — a Pokémon with no TRANSFER on its menu (a
    favourite, a buddy, one defending a gym) looks exactly like this, and so
    does a tap that missed. Nothing was sent in that case and the caller backs
    out.

    Raises once a YES has gone out and the outcome is not clear, because from
    that point on the run cannot tell what it is looking at, and the next taps
    would land on whatever came up.
    """
    await tap(device, tile_point(device, slot))
    await wait(DETAIL_DELAY)
    state, _frame = await read_screen(device)
    if state != 'detail':
        log(device, f'  no detail screen after tapping slot {slot + 1} (saw {state})')
        return None

    await tap(device, device.config['TRASH_MENU_BTN'])
    await wait(MENU_DELAY)
    state, _frame = await read_screen(device)
    if state != 'menu':
        log(device, f'  menu did not open (saw {state})')
        return None

    await tap(device, device.config['TRANSFER_BTN'])
    await wait(DIALOG_DELAY)
    state, frame = await read_screen(device)
    if state != 'confirm':
        # TRANSFER was not the row that got tapped, so this Pokémon has no
        # TRANSFER on its menu. Never guess at where it might have moved to.
        log(device, f'  no transfer dialog (saw {state}) — not transferable')
        return None

    # Answer dialogs until the list comes back. A lucky Pokémon gets a second
    # one; anything else that turns up is not something to tap at.
    for _ in range(MAX_DIALOGS):
        await tap(device, dialog_buttons(device, frame)[0])
        await wait(TRANSFER_DELAY)
        for _ in range(LIST_RETRIES):
            state, frame = await read_screen(device)
            if state in ('list', 'confirm'):
                break
            await wait(LIST_POLL, use_modifier=False)
        if state == 'list':
            return frame
        if state != 'confirm':
            raise LuckyTrashError(
                f'A YES was sent and the screen is now {state}, not the list. '
                'Stopped — check the phone before running again.')
    raise LuckyTrashError(
        f'Still on a dialog after {MAX_DIALOGS} YES taps. Stopped.')


async def trash_process(device: DeviceAsyncWrapper, limit: int):
    """Transfers up to `limit` Pokémon out of the applied filter."""
    frame = await screencap_raw(device)
    if frame is None:
        raise LuckyTrashError("Couldn't read the screen")
    if screen_of(device, frame) != 'list':
        raise LuckyTrashError(
            'Not on the Pokémon list. Open the storage list with your filter '
            'applied and try again.')

    # Whichever of the two the filter is expressed in, the same thing is being
    # asked: is anything actually narrowing this list, and is it still the same
    # thing on every pass. Only the band the answer is read out of differs.
    pill = pill_fraction(device, frame)
    if pill < QUERY_PILL_MIN:
        raise LuckyTrashError(
            f'No search box where one should be (pill {pill:.3f}, needs '
            f'{QUERY_PILL_MIN}). This is not the Pokémon list — the tag list '
            'is the same colour and passes the screen check. Refusing to tap.')

    reference = query_bits(device, frame)
    ink = sum(reference) / len(reference)
    dots = tag_dot_columns(device, frame) if 'TAG_BAND_Y' in device.config else 0
    if ink >= QUERY_MIN_INK:
        fingerprint = query_bits
        log(device, f'Filter locked in (search box ink {ink:.3f})')
    elif dots >= TAG_DOT_COLS:
        fingerprint = tag_bits
        reference = tag_bits(device, frame)
        log(device, f'Filter locked in (tag view, {dots} dot columns)')
    else:
        raise LuckyTrashError(
            f'The search box looks empty (ink {ink:.3f}, needs {QUERY_MIN_INK}) '
            f'and no tag colour was found above it ({dots} columns, needs '
            f'{TAG_DOT_COLS}). Refusing to transfer anything out of an '
            'unfiltered list.')

    # Pokémon that cannot be transferred stay where they are while everything
    # after them shifts up, so once a leading tile is known to be blocked the
    # run starts past it instead of paying a full cycle to rediscover it.
    done = slot = base = 0
    total = '' if limit >= 10 ** 6 else f' of {limit}'
    while done < limit:
        frame = await screencap_raw(device)
        if frame is None:
            raise LuckyTrashError('Screen read failed between transfers')
        if screen_of(device, frame) != 'list':
            raise LuckyTrashError('Left the Pokémon list unexpectedly')

        now = fingerprint(device, frame)
        match = sum(a == b for a, b in zip(reference, now)) / len(reference)
        if match < QUERY_MATCH:
            raise LuckyTrashError(
                f'The filter changed mid-run (band matches {match:.3f}). '
                'Stopped without transferring anything else — whatever is in '
                'the list now is not what you picked.')

        if tile_ink(device, frame, slot) < TILE_OCCUPIED:
            if slot == base == 0:
                log(device, 'Filter is empty — nothing left to transfer')
            else:
                log(device, f'Nothing left to transfer past slot {slot}')
            return done
        before = count_bits(device, frame)

        log(device, f'{done + 1}{total}: slot {slot + 1}')
        frame = await transfer_one(device, slot)
        if frame is None:
            if not await back_out(device):
                raise LuckyTrashError(
                    'Could not get back to the Pokémon list. Stopped here '
                    'rather than tapping on blind — check the phone.')
            base = slot = slot + 1
            if slot >= TILE_COLS * TILE_ROWS:
                raise LuckyTrashError(
                    f'Transferred {done}, then nothing in the first {slot} '
                    'slots would go. Either the filter is down to Pokémon that '
                    'cannot be transferred, or it is empty and those slots are '
                    'blank. Check the phone.')
            continue

        moved = sum(a != b for a, b in zip(before, count_bits(device, frame)))
        if moved < COUNT_MOVED:
            raise LuckyTrashError(
                f'The count beside POKÉMON did not move ({moved} cells). '
                'Nothing was transferred, so something is being misread. '
                'Stopped.')
        done += 1
        slot = base
        log(device, f'  transferred ({moved} cells of the count changed)')
    return done


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
        raise LuckyTrashError(f'Found no config file at {config_file_path}')
    config: CONFIG = yaml.safe_load(content)
    assert isinstance(config, dict), 'Incorrect config file format (should be an object with keys)'
    if not TRASH_KEYS <= set(config.keys()):
        raise LuckyTrashError(f'Missing config key(s): {TRASH_KEYS - set(config.keys())}')
    for coords in config.values():
        assert isinstance(coords, list) and len(coords) == 2 and all(isinstance(i, int) for i in coords),\
            'Invalid coords format in config (should be list with two integers)'
    device.config = config
    return config


async def setup(serials: list[str] | None = None) -> list[DeviceAsyncWrapper]:
    """Finds every connected device and loads a config from each.

    Phones run their own filters side by side, so what keeps a second one from
    trashing something nobody picked is not that it was excluded — it is that
    every device passes its own gate: its own config, its own list screen, and
    its own search box fingerprinted and re-checked before each transfer. A
    phone that is not on a filtered list never gets past the first check.

    A device without the transfer keys is reported and dropped rather than
    aborting the rest; not every phone here has been mapped for this.
    """
    client = ClientAsync()
    devices: list[DeviceAsyncWrapper] = await client.devices()
    if serials:
        devices = [d for d in devices if d.serial in serials]
        missing = set(serials) - {d.serial for d in devices}
        if missing:
            raise LuckyTrashError(f'No device with serial {", ".join(missing)}')
    if not devices:
        raise LuckyTrashError('No devices found')
    width = max(len(device.serial) for device in devices)
    ready: list[DeviceAsyncWrapper] = []
    for device in devices:
        device.label = device.serial.ljust(width)
        try:
            await get_config(device)
        except (LuckyTrashError, AssertionError, ParserError) as e:
            log(device, 'skipped —', ' '.join(map(str, e.args)))
            continue
        device.display_id = await find_display_id(device)
        extra = f', reading display {device.display_id}' if device.display_id else ''
        log(device, f'ready, {len(device.config)} config keys{extra}')
        ready.append(device)
    if not ready:
        raise LuckyTrashError('No devices with a usable config')
    return ready


async def trash_all(devices: list[DeviceAsyncWrapper], limit: int):
    """Runs every device at once, each against its own filter.

    One phone stopping does not stop the others: its reason is printed against
    its own serial and the rest carry on. Every line is tagged, because two
    phones logging into one terminal is otherwise unreadable.
    """
    results = await asyncio.gather(
        *(trash_process(device, limit) for device in devices),
        return_exceptions=True)
    for device, result in zip(devices, results):
        if isinstance(result, LuckyTrashError):
            log(device, 'stopped —', ' '.join(map(str, result.args)))
        elif isinstance(result, BaseException):
            raise result
        else:
            log(device, f'transferred {result}')


def interface(serials: list[str] | None = None):
    """Runs the main loop asking for user input."""
    global SLEEP_MODIFIER
    print(
        '\n'
        ' ##                          ## \n'
        '##         luckyTrash         ##\n'
        ' ##                          ## \n'
    )
    devices: list[DeviceAsyncWrapper] = asyncio.run(setup(serials))
    print(f'\nRunning on {len(devices)} device(s): '
          + ', '.join(d.serial for d in devices))
    print('\nTransfers cannot be undone. Leave every phone on the Pokémon list '
          'with its\nown filter applied — each one is checked separately, '
          'before every transfer.')
    print('\nCommands:')
    print('  <number>      transfer that many on each phone, from the first tile')
    print('  all           transfer every Pokémon in each phone\'s filter')
    print('  delay <val>   get/set the extra delay modifier')
    print('  q             quit')
    while True:
        i = input('\n> ').strip()
        if i in ('q', 'quit', 'exit'):
            return
        if i.startswith('delay'):
            arg = i[5:].strip()
            if not arg:
                print('Delay modifier:', SLEEP_MODIFIER)
                continue
            try:
                SLEEP_MODIFIER = float(arg)
            except ValueError:
                print('Give a number.')
                continue
            print('Delay modifier:', SLEEP_MODIFIER)
            continue
        if i == 'all':
            limit = 10 ** 6   # the empty filter is what actually ends the run
        else:
            try:
                limit = int(i)
            except ValueError:
                print('Give a number, or "all".')
                continue
            if limit < 1:
                continue
        print(f'\nTransferring {"every match" if i == "all" else limit} on '
              f'{len(devices)} device(s) (Ctrl+C to cancel)...')
        try:
            asyncio.run(trash_all(devices, limit))
        except KeyboardInterrupt:
            print('\nCancelled.')
        except LuckyTrashError as e:
            print('\n' + '\n'.join(map(str, e.args)))


def main():
    import sys
    try:
        interface(sys.argv[1:] or None)
    except LuckyTrashError as e:
        print('\n'.join(map(str, e.args)))
        exit(1)
    except (KeyboardInterrupt, EOFError):
        print()


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('delete', 'luckytrash.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == '__main__':
    main()
