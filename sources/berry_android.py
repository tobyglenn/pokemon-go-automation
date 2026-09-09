#!/usr/bin/env python3
from __future__ import annotations

"""
AutoBerry — Feeds berries to every Pokémon defending a gym in Pokémon GO.

Uses the same AutoTraderConfig.yaml as trade.py and gift.py, with the berry
keys appended. Every connected device feeds its own gym at once, each from its
own config, so each one needs every key in BERRY_KEYS. A device without them is
skipped and the rest still feed.

Start each phone with its gym's berry feeding screen already open — the card showing one
defender, with the berry sitting in front of it. It does not matter which
defender: the list is a carousel, so the run starts wherever it finds itself
and keeps going until it has seen every defender. Per defender:
  1. Tap BERRY_BTN until the game stops offering a berry (the defender is full)
  2. Swipe from NEXT_MON_FROM to NEXT_MON_TO to reach the next defender

NEXT_MON_FROM/TO must sit on a row clear of the Pokémon model, around y 620.
The model takes the drag itself and spins in place instead of paging: at
y 1400 none of five swipes moved the card, at y 620 all four did.

The game picks the next berry type itself when a stack runs out, so nothing
here tracks berry stock.
"""

import asyncio
import random
import re
import struct
import sys
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
TMP_FILE_NAME    = 'tmp_berry_{}.yaml'  # per device: configs are pulled concurrently
CONFIG = dict[str, list[int]]

BERRY_KEYS = {'BERRY_BTN', 'NEXT_MON_FROM', 'NEXT_MON_TO'}

# Keys for moving on to the next gym once this one is fed. Optional: a phone
# without them feeds the gym it was parked on and stops, exactly as before, so
# only the phones that have been mapped for hopping do it.
#
# The route out of a fed gym, measured on the moto on 2026-08-10:
#
#   GYM_CLOSE_BTN       the X, tapped twice -- feeding screen -> gym -> Research
#                         (not gift.py's CLOSE_BTN, which is a different X)
#   TODAY_SCROLL_FROM   one scroll down the Research/Today page reaches the
#   TODAY_SCROLL_TO       "Pokemon in Gyms" card
#   TODAY_GYM_FIRST     centre of the first gym's Pokemon in that card
#   TODAY_GYM_STEP      offset to the next one along; the grid runs across in
#                         TODAY_GYM_COLS columns, so this is an x step
#   GYM_DEFENDER_BTN    a defender in the gym view -- tapping one opens the
#                         feeding screen directly, there is no berry-icon step
GYM_HOP_KEYS = {'GYM_CLOSE_BTN', 'TODAY_SCROLL_FROM', 'TODAY_SCROLL_TO',
                'TODAY_GYM_FIRST', 'TODAY_GYM_STEP', 'GYM_DEFENDER_BTN'}
TODAY_GYM_COLS = 3        # gyms per row in the Pokemon in Gyms grid
MAX_GYMS = 20             # the game will not hold your Pokemon in more than 20
HOP_SETTLE = 3            # each screen animates in before it can be tapped
TODAY_SCROLL_MS = 300
TODAY_SCROLL_SETTLE = 2
TODAY_SCROLL_FLINGS = 3   # enough to reach the bottom stop from the top
# Reading a gym's Pokemon off the white Today card -- see sprite_share().
SPRITE_HALF = 26          # box half-width, inside one column's 209px pitch
SPRITE_STEP = 4
SPRITE_COLOUR = 40        # channel spread that counts a pixel as coloured
TODAY_GYM_SPRITE_MIN = 0.20   # measured 0.265 lowest with a sprite, 0.168 without

SLEEP_MODIFIER = 0
# Berries each device may spend in one run, or None for no cap. A cap used to
# be the only thing standing between a run and a golden razz stock, because
# nothing on screen could say "that one is a golden razz, stop". The picker can
# -- see the note above PICKER_COL_X -- so the run now names the berry it is
# about to spend and stops on its own when only golden razz and silver pinap
# are left. A cap is just an extra bound for a run you want kept short.
SPEND_CAP: int | None = None
# Per-serial overrides. Phones do not hold the same stocks -- the android-one had 61
# cheap berries the day the moto had 142 -- so one number applied to both either
# cuts the fuller phone short or does nothing to the emptier one. A serial with
# no entry falls back to SPEND_CAP.
SPEND_CAPS: dict[str, int] = {}

BERRY_TAP_GAP   = 1.5   # each berry plays a feed animation before the next lands
# Feeding stops when the game refuses, never on a count. A defender's appetite
# depends on how much motivation it has lost, and taps get dropped besides: two
# runs that each tapped ten times left the card reading 6 and 1 berries fed, so
# a counted run both overstates what it did and leaves motivation on the table.
FEED_RUNAWAY = 50       # only bounds a misread; the refusal is the real stop
                        # 20 was itself a limit: hand-feeding took 30 in a row
                        # without a refusal, so the bound was cutting runs short
FEED_SETTLE     = 4     # the "+10 STARDUST" toast overlaps the name banner
SWIPE_SETTLE    = 3     # the card slides across before it can be read
MAX_DEFENDERS   = 6     # a gym holds at most 6; a hard stop if detection fails
SWIPE_RETRIES   = 3
# The carousel wants a fling, not a drag. Measured on the android-three at a fixed row:
# 150ms paged 4 of 4, 300ms 3 of 4, 600ms 0 of 4 — held long enough and the card
# just follows the finger back. The android-one agrees, 5 of 5 at 150ms against 4 of 5
# at the 300ms it was originally written with.
SWIPE_MS        = 150
IDLE_TURNS      = 4     # swipes in a row showing nothing new = the whole gym
MAX_TURNS_PER_DEFENDER = 3   # the carousel revisits cards, so allow slack

# This script lives in platform-tools, so prefer the adb binary next to it.
SWIPE_PROFILES = ((1.0, 150), (0.78, 250), (0.60, 400))

_ADB = Path(__file__).resolve().parent.parent / 'adb'
ADB_BINARY = str(_ADB) if _ADB.exists() else 'adb'

# --- Defender card fingerprint ---
# The fingerprint identifies which defender is on screen. It is used to tell a
# card that has genuinely changed from one that has not, because swipes drop
# often on this phone and a dropped swipe otherwise looks exactly like the end
# of the list -- an earlier walk of a 5-defender gym reported 3.
#
# One band is sampled and binarised: the Pokémon name (y 0.055-0.077), white
# text on the solid dark header bar. Measured within a gym across eleven real
# cards from two gyms: the same defender scores 1.000, different defenders 0.913
# at worst, so CARD_MATCH sits in a 0.087 gap.
#
# What is deliberately NOT used:
#   - The shield timer. It looks ideal, being unique per defender, but it ticks:
#     the same Chansey a minute apart scored 0.995 on a name+timer signature and
#     was counted as a new defender.
#   - The CP. Feeding raises it, and motivation decay lowers it again between
#     passes: one Blissey read 1081, 1078, 1077, 1076 over nine minutes untouched.
#   - The owner's name. This was in here and measurably made things worse, so it
#     came back out. It has no fixed home -- beside the CP on most cards, but
#     centred under the timer when the owner's avatar is drawn centre-stage, so
#     no single band catches both layouts. And every card in a gym shares the
#     same animated backdrop photo, so a band that misses the text samples
#     identical scenery on every defender. Adding it pulled different-defender
#     scores UP from 0.913 to 0.936 while pulling same-defender DOWN from 1.000
#     to 0.996, more than halving the usable gap.
#
# The cost of name-only: two defenders of the same species in one gym read as
# identical. feed_all() is arranged so that costs a skipped defender at worst,
# never a double feed.
#
# The game draws no accessibility nodes for 'uiautomator dump', so this reads
# the framebuffer, exactly as gift.py's friends list guard does.
CARD_BAND_X = (0.15, 0.85)
CARD_BAND_Y = (55, 77)   # per-mille of the screen height; see PER-DEVICE BANDS
CARD_NX, CARD_NY = 160, 8
CARD_BRIGHT      = 190   # a sampled point counts as ink at or above this
CARD_MIN_INK     = 20    # fewer lit points than this and the band holds no name
CARD_MATCH       = 0.60  # ink overlap; see below for the measured gap

# Two cards are compared by how much their INK overlaps, not by how many sampled
# points agree. Agreement was the original measure and it silently cost a
# defender: only ~4-8% of the band is text (53-103 lit points out of 1280 across
# both phones), so two different names already agree on the ~93% that is shared
# dark header before a single letter is compared. That squeezed every score into
# the top of the range, and the ranges then overlapped BETWEEN phones -- so no
# single threshold could work:
#
#                       agreement        ink overlap
#   same defender       0.993 - 1.000    0.913 - 1.000
#   different defender  0.924 - 0.950    0.164 - 0.297
#
# Measured on real frames: the moto's Blitzle and Chansey scored 0.950 agreement,
# exactly the old CARD_MATCH, so a three-defender gym was walked as two and
# Blitzle was never fed. Ink overlap puts 0.6 of clear air between the two cases.
# The same-defender floor of 0.913 is a android-one pair with the berry sheet open over
# the card, which shifts the antialiasing on the name; untouched cards score
# 1.000. scratchpad/card_test.py pins all of this to the frames it came from.

# --- PER-DEVICE BANDS ---
# These vertical fractions do NOT transfer between phones, so both are optional
# config keys (in per-mille of the screen height, since a config value has to be
# a pair of integers) and the constants here are only the fallback:
#
#   CARD_BAND_Y:  [55, 77]
#   BERRY_DISC_Y: [788, 845]
#
# The reason is aspect ratio, not resolution. The android-one is 1316x2560 (1:1.95) and
# the android-three's inner display is 1224x2992 (1:2.44); the game lays the card out to
# the width, so everything below the header sits at a different fraction of the
# panel. Measured on one feeding screen each: the name banner is at y
# 0.061-0.075 on the android-one but 0.048-0.059 on the android-three, and the android-one's berry disc
# band scored 95.6 on the android-three — the guard's range is 120-172, so berry.py would
# have refused to run there while insisting the screen was wrong.
#
# To map a new phone, park it on a feeding screen and sweep: the disc band is
# the one that reads inside BERRY_DISC_RANGE (the pale item disc against the
# darkened vignette), and the card band is the one that brackets the run of
# white name text with a little margin, ignoring the Android status bar above it.

# --- Feeding screen guard ---
# Checked once before any tapping, so a mistimed run cannot blind-tap BERRY_BTN
# into whatever else is on screen. Two independent conditions, both measured
# across every feeding screenshot and a spread of others (friends list, battle
# screens, league list, trade menus):
#   - the berry item disc at the bottom left: feeding 143.9-163.3, everything
#     else 7.7 or 175.3 and up
#   - ink in the name band: feeding 80-182, everything else 0 or 545 and up
#
# The disc is the weaker of the two and its floor has had to come down. The disc
# is translucent, so what shows through it is the gym behind, and the 143.9
# floor was measured in daylight. On the android-one at night, parked on a feeding
# screen with a berry plainly on offer, it read 113.7 -- and sweeping the band
# 740-947 per-mille found no brighter row anywhere near it (105-123 the whole
# way), so this is the disc being dim, not the band being misplaced. The guard
# refused to start there and the run died on a gym it could have fed. The floor
# is now 60, which is still eight times the 7.7 that every non-feeding screen
# scored, and the name-band ink -- which does not depend on the background --
# carries the weight.
BERRY_DISC_X = (0.063, 0.185)
BERRY_DISC_Y = (788, 845)   # per-mille of the screen height; see PER-DEVICE BANDS
BERRY_DISC_STEPS = 10
BERRY_DISC_RANGE = (60, 172)
NAME_INK_RANGE   = (40, 300)

# --- Berry type lock (superseded) ---
# None of this decides anything any more. The pink/gold split below cannot see
# a nanab turn into a pinap or a pinap into a golden razz, because all three
# read gold, so it could only ever catch razz -> nanab and it let the run walk
# into the golden razz stock the rest of the time. The berry is now named off
# the picker instead -- see the note above PICKER_COL_X. What is left here is
# kept because gold_share_of is still what berry_offered reports alongside the
# berry it locates, and it is useful to have in a log; berry_gold_share below it
# now has no callers.
#
# The item disc shows whichever berry is selected, and the game quietly moves to
# another type once the current one runs out. That is how a run meaning to spend
# razz ends up spending golden razz, so the type in use is read at the start and
# rechecked before each defender, and the device stops as soon as it moves.
#
# This locks to the berry the run started on rather than naming the types: it
# needs no per-berry colour table, and it stops on the thing actually worth
# stopping on -- the stock you meant to spend running out. The cost is that it
# cannot object to a run that *starts* on golden razz, which is still on you.
#
# Hue, not brightness. The disc dims and brightens between frames, so absolute
# channels swing by up to 72 across reads of one unchanged screen while G-B
# holds. Measured over four reads on a android-three feeding screen: G-B -23.5 to -17.4,
# a spread of 6.1. Types sit far further apart -- a razz is red-side (G-B
# negative) and the yellow berries, golden razz among them, are gold-side -- so
# the tolerance clears the noise several times over and still leaves the gap.
BERRY_HUE_STEPS     = 30
BERRY_HUE_SAT_TOP   = 10   # keep the most saturated tenth: the berry, not the mon
BERRY_HUE_TOLERANCE = 20
# A berry counts as gold-toned when this share of it is gold. Razz measured a
# flat 0.000 and nanab 0.414..0.864, so the split sits in an empty gap. It
# separates pink berries from gold ones; it does NOT tell a nanab from a golden
# razz, which are both gold-toned.
BERRY_GOLD_HUE      = 20    # G - B above this is a gold pixel rather than pink
BERRY_GOLD_SPLIT    = 0.20
BERRY_HUE_CONFIRMS  = 2    # a lone odd read is a mid-animation frame, not a new berry

# --- Is a berry being offered? ---
# The game stops drawing the berry in front of a defender once that defender is
# full, so its presence is what says whether another one can be fed. The box
# hangs off BERRY_BTN, which sits at the berry's top edge, and covers only the
# floor below it -- the defender itself is well clear, so a pink Chansey does
# not read as a razz.
# The berry bobs further than it is wide, so a fixed tap point misses it most of
# the time: 42 taps at one left a moto's stock untouched at 47, while the two
# that happened to catch the berry fed instantly. The berry is therefore located
# in each frame and the tap follows it. BERRY_BTN is only the anchor the search
# box hangs off, so it wants to sit mid-bob rather than on the berry exactly.
#
# Warm pixels count -- red well above blue. Saturation was the first test and it
# was wrong: it was tuned on a vivid magenta razz, but the gym floor is a
# saturated blue-grey that clears the same gate, and a paler berry does not. When
# the android-three ran out of razz and moved to nanab, the nanab's pink-and-yellow scored
# 0.041 against a bare floor's 0.465 and the run stopped with a berry plainly on
# screen. Warmth separates what saturation could not, because every berry is warm
# -- razz magenta, nanab pink and yellow, golden razz gold -- while the floor and
# the sky above it are blue. Scored over the same box on saved frames of both
# states, at this gate:
#   bare floor, mon full   0.000
#   nanab / razz / razz    0.126, 0.089, 0.385
# so the floor contributes nothing at all and the weakest berry still clears the
# threshold twofold.
#
# Warm is necessary but not sufficient, because the defenders are pink: Chansey
# and Blissey are warm too, and they stand in this box. Warmth alone reported a
# berry on a android-one floor that was plainly bare -- greyed disc, no stock count --
# and the run tapped that empty floor until it hit the cap and called the gym
# done. Saturation is what parts them: a berry is vivid, a defender is washed
# out. Scored on controlled pairs, a berry frame and a bare-floor frame at the
# same anchor on each phone:
#   bare      android-one 0.000   moto 0.000   android-three 0.000
#   berry     android-one 0.459   moto 0.359   android-three 0.041
# so no defender survives the pair of gates and the weakest berry still clears
# the threshold twofold.
BERRY_FIND_HALF_X = 0.11
BERRY_FIND_HALF_Y = 0.075   # has to span the whole bob
BERRY_TAP_POINTS  = 7       # taps down the bob; 40px apart on the moto
BERRY_FIND_STEPS  = 34
BERRY_WARM        = 40      # R - B: berries are warm, the gym floor is blue
BERRY_VIVID       = 90      # saturation: berries are vivid, pink defenders are not
# A bare floor is not merely low, it is a hard zero: four bare frames across all
# three phones scored 0 hits of 1156, while berries scored 58, 415 and 531. The
# threshold used to sit at 23 hits, which suited the moto's bright razz but left
# the android-three's dim one only 2.5x clear of being called absent -- and three absent
# reads is what ends a defender. Set against the zero instead, so the weakest
# berry seen is ten times clear.
BERRY_PRESENT     = 0.005   # ~6 hits of 1156
# Absent reads in a row before a mon is called full. Three was too hasty: a
# thrown berry is measurably gone for 1.8-2.8s and back by 3.6s, but reads land
# roughly every 2.1s, so a fixed cadence can beat against the bob and put two or
# three reads on the same unlucky phase in a row. That ended a android-three gym at 3, 1
# and 1 berries with berries plainly still on offer and stock still in the bag.
# Declaring a defender finished is the one decision that cannot be walked back,
# and it is paid only once per defender, so it is worth ten seconds of looking.
BERRY_OFFER_READS = 5
BERRY_RESPAWN     = 1.5    # a taken berry is replaced by the next fading in
BERRY_RESPAWN_JITTER = 0.9  # break the cadence so reads do not alias with the bob

SCREENCAP_TIMEOUT = 20   # adb exec-out screencap wedges outright now and then


class DeviceAsyncWrapper(DeviceAsync):
    config: CONFIG
    display_id: str | None = None   # set by setup(); only foldables need it
    label: str = ''                 # set by setup(); prefixes this device's output
    stopped: str | None = None      # set by berry_process_one(); why it gave up


class AutoBerryError(Exception):
    pass


def log(device: DeviceAsyncWrapper, *parts):
    """Prints one line tagged with the device it came from.

    Devices feed their gyms concurrently, so untagged output interleaves into
    something unreadable as soon as more than one phone is plugged in.
    """
    print(f'[{device.label}]', *parts)


async def tap(device: DeviceAsyncWrapper, point: list[int]):
    """Sends a tap at point to device."""
    x, y = point
    # Uses a tiny swipe over 100 ms for increased reliability, as in trade.py.
    await device.shell(f'input swipe {x} {y} {x+1} {y+1} 100')


async def swipe(device: DeviceAsyncWrapper, start: list[int], end: list[int], ms: int = SWIPE_MS):
    """Sends a swipe from start to end over `ms` milliseconds."""
    x0, y0 = start
    x1, y1 = end
    await device.shell(f'input swipe {x0} {y0} {x1} {y1} {ms}')


def scaled_swipe(start: list[int], end: list[int], scale: float) -> tuple[list[int], list[int]]:
    """Returns a centred swipe using a fraction of the configured span."""
    cx = (start[0] + end[0]) / 2
    cy = (start[1] + end[1]) / 2
    dx = (start[0] - end[0]) * scale / 2
    dy = (start[1] - end[1]) * scale / 2
    return [round(cx + dx), round(cy + dy)], [round(cx - dx), round(cy - dy)]


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
        # The device stays healthy and later screencaps work, but this one never
        # returns. Without the timeout the run stops dead with no output at all.
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


def band_bits(width: int, height: int, offset: int, data: bytes,
              band: tuple[float, float]) -> list[bool]:
    """Binarises the name band into a crude glyph pattern."""
    x0, x1 = CARD_BAND_X
    y0, y1 = band
    bits = []
    for yi in range(CARD_NY):
        y = int(height * (y0 + (y1 - y0) * yi / (CARD_NY - 1)))
        for xi in range(CARD_NX):
            x = int(width * (x0 + (x1 - x0) * xi / (CARD_NX - 1)))
            i = offset + (y * width + x) * 4
            bits.append((data[i] + data[i + 1] + data[i + 2]) / 3 >= CARD_BRIGHT)
    return bits


async def card_fingerprint(device: DeviceAsyncWrapper):
    """The current defender's fingerprint, or None when the screen can't be read."""
    frame = await screencap_raw(device)
    if frame is None:
        return None
    bits = band_bits(*frame, band_of(device, 'CARD_BAND_Y', CARD_BAND_Y))
    # A band with no text in it carries no identity, and comparing two of them
    # by ink overlap is meaningless. Real cards run 53-103 lit points on these
    # phones, so this only catches a band that missed the name entirely.
    if sum(bits) < CARD_MIN_INK:
        return None
    return bits


def cards_match(a: list[bool], b: list[bool]) -> bool:
    """True if two fingerprints are the same defender.

    Jaccard over the lit points: shared background cannot inflate the score,
    because points that are dark in both cards count towards neither term.
    """
    inter = sum(1 for p, q in zip(a, b) if p and q)
    union = sum(1 for p, q in zip(a, b) if p or q)
    if union == 0:
        return False    # two blank bands name nothing; never call them the same
    return inter / union >= CARD_MATCH


def feed_screen_metrics(
        device: DeviceAsyncWrapper,
        frame: tuple[int, int, int, bytes]) -> tuple[float, int, bool]:
    """Return disc brightness, name ink, and guarded screen match."""
    width, height, offset, data = frame
    x0, x1 = BERRY_DISC_X
    y0, y1 = band_of(device, 'BERRY_DISC_Y', BERRY_DISC_Y)
    total = 0
    for yi in range(BERRY_DISC_STEPS):
        y = int(height * (y0 + (y1 - y0) * yi / (BERRY_DISC_STEPS - 1)))
        for xi in range(BERRY_DISC_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / (BERRY_DISC_STEPS - 1)))
            i = offset + (y * width + x) * 4
            total += (data[i] + data[i + 1] + data[i + 2]) / 3
    disc = total / BERRY_DISC_STEPS ** 2
    ink = sum(band_bits(*frame, band_of(device, 'CARD_BAND_Y', CARD_BAND_Y)))
    # There used to be a second pass here for a full defender, whose disc greys
    # out: 100..172 was allowed through so long as no berry was on offer. The
    # wider floor above covers that case and more, and the extra pass actively
    # hurt -- it refused precisely the screens worth running on, a dim disc
    # WITH a berry.
    matched = (BERRY_DISC_RANGE[0] <= disc <= BERRY_DISC_RANGE[1]
               and NAME_INK_RANGE[0] <= ink <= NAME_INK_RANGE[1])
    return disc, ink, matched


async def on_feed_screen(device: DeviceAsyncWrapper) -> bool:
    """True if a gym feeding screen is showing.

    Says why when it says no. This guard refusing is how a run ends before it
    starts, and "does not look like a gym feeding screen" on its own sent us
    hunting the wrong bug for an hour -- the numbers name the band that missed.
    """
    frame = await screencap_raw(device)
    if frame is None:
        raise AutoBerryError("Couldn't read the screen to check for a gym card")
    disc, ink, matched = feed_screen_metrics(device, frame)
    if not matched:
        at, share = find_berry_share(*frame, device.config['BERRY_BTN'])
        log(device, f'  Not a feeding screen: disc {disc:.1f} of'
                    f' {BERRY_DISC_RANGE}, name ink {ink} of {NAME_INK_RANGE},'
                    f' berry share {share:.4f}'
                    + ('' if at is None else f' at {at}'))
    return matched


def disc_hue(width: int, height: int, offset: int, data: bytes,
             band: tuple[float, float]) -> float:
    """Which berry the item disc is showing, as green-minus-blue across its icon.

    Only the saturated pixels count. The disc behind the icon is near-grey and
    covers most of the band, so averaging the whole thing washes the hue out to
    nothing whichever berry is loaded.
    """
    x0, x1 = BERRY_DISC_X
    y0, y1 = band
    pixels = []
    for yi in range(BERRY_HUE_STEPS):
        y = int(height * (y0 + (y1 - y0) * yi / (BERRY_HUE_STEPS - 1)))
        for xi in range(BERRY_HUE_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / (BERRY_HUE_STEPS - 1)))
            i = offset + (y * width + x) * 4
            pixels.append((data[i], data[i + 1], data[i + 2]))
    pixels.sort(key=lambda p: max(p) - min(p), reverse=True)
    icon = pixels[: len(pixels) // BERRY_HUE_SAT_TOP]
    return sum(p[1] - p[2] for p in icon) / len(icon)


async def read_berry_hue(device: DeviceAsyncWrapper) -> float | None:
    """The hue of the berry being offered, or None if there isn't one to read.

    Read off the berry on the floor, not the item disc at the bottom left. The
    disc does not track what is actually being thrown: a android-three ran its razz out
    mid-run and moved on to nanab -- the floor berry plainly changed and the
    stock jumped 12 to 27 -- while the disc carried on looking razz-pink and the
    lock never fired.

    Only the most saturated warm pixels are averaged, not every warm one. The
    defenders are pink -- Chansey, Blissey -- and they stand in the same box as
    the berry, so a flat average is a blend of berry and belly that moves as the
    berry bobs across the body. That read a android-one nanab at +9.2 where the android-three
    read the same berry type at +98.8, and the lock tripped on its own noise
    before a single berry was thrown. A berry is vivid where a defender is
    washed out, so the top slice by saturation is berry.
    """
    frame = await screencap_raw(device)
    if frame is None:
        return None
    width, height, offset, data = frame
    bx, by = device.config['BERRY_BTN']
    half = (BERRY_FIND_STEPS - 1) / 2
    warm = []
    for yi in range(BERRY_FIND_STEPS):
        y = int(by + height * BERRY_FIND_HALF_Y * (yi / half - 1))
        for xi in range(BERRY_FIND_STEPS):
            x = int(bx + width * BERRY_FIND_HALF_X * (xi / half - 1))
            if not (0 <= x < width and 0 <= y < height):
                continue
            i = offset + (y * width + x) * 4
            if is_berry_pixel(data, i):
                warm.append((data[i], data[i + 1], data[i + 2]))
    if not warm:
        return None
    warm.sort(key=lambda p: max(p) - min(p), reverse=True)
    berry_px = warm[: max(1, len(warm) // BERRY_HUE_SAT_TOP)]
    hues = sorted(p[1] - p[2] for p in berry_px)
    return hues[len(hues) // 2]


def is_berry_pixel(data: bytes, i: int) -> bool:
    """Whether the pixel at byte offset i belongs to a berry.

    Warm parts it from the blue gym floor, vivid parts it from the pink
    defenders standing in the same box. Either test alone lets something
    through: the floor is a saturated blue-grey, and Chansey is warm.
    """
    return ((data[i] - data[i + 2]) >= BERRY_WARM
            and (max(data[i], data[i + 1], data[i + 2])
                 - min(data[i], data[i + 1], data[i + 2])) >= BERRY_VIVID)


def gold_share_of(width: int, height: int, offset: int, data: bytes,
                  point: list[int]) -> float | None:
    """The gold share of one already-captured frame. See berry_gold_share."""
    bx, by = point
    half = (BERRY_FIND_STEPS - 1) / 2
    gold = warm = 0
    for yi in range(BERRY_FIND_STEPS):
        y = int(by + height * BERRY_FIND_HALF_Y * (yi / half - 1))
        for xi in range(BERRY_FIND_STEPS):
            x = int(bx + width * BERRY_FIND_HALF_X * (xi / half - 1))
            if not (0 <= x < width and 0 <= y < height):
                continue
            i = offset + (y * width + x) * 4
            if is_berry_pixel(data, i):
                warm += 1
                gold += (data[i + 1] - data[i + 2]) > BERRY_GOLD_HUE
    return gold / warm if warm else None


async def berry_gold_share(device: DeviceAsyncWrapper) -> float | None:
    """What share of the berry is gold-toned, or None if no berry is on screen.

    A proportion rather than an average hue, because a nanab is two berries by
    colour -- pink bananas under a yellow crown -- and any single number
    describing it depends on which of the two the sample happened to catch.
    Measured live over eight reads, a hue median swung 152..177 on the android-one and a
    tight box round the located berry swung 199, both far past any workable
    tolerance, purely from the berry bobbing. The share of gold pixels does not
    care which part of the berry is in view:

      moto razz    0.000 on all eight reads
      android-one nanab   0.414 .. 0.864

    so the two sides never come close to meeting even at the nanab's noisiest.
    """
    frame = await screencap_raw(device)
    if frame is None:
        return None
    return gold_share_of(*frame, device.config['BERRY_BTN'])


async def turn_card(device: DeviceAsyncWrapper, reverse: bool = False) -> bool:
    """Swipes to the next defender. False if the card refuses to change.

    A swipe that changes nothing is retried rather than believed, because a
    dropped swipe is indistinguishable from one that had nothing to do.

    `reverse` walks the carousel the other way, which is not merely the same
    ring read backwards -- see the sweep-back note in feed_all.
    """
    start, end = device.config['NEXT_MON_FROM'], device.config['NEXT_MON_TO']
    if reverse:
        start, end = end, start
    before = await card_fingerprint(device)
    for attempt, (scale, duration) in enumerate(SWIPE_PROFILES, 1):
        gesture_start, gesture_end = scaled_swipe(start, end, scale)
        await swipe(device, gesture_start, gesture_end, duration)
        await wait(SWIPE_SETTLE, use_modifier=False)
        after = await card_fingerprint(device)
        if before is None or after is None:
            return True   # can't read the screen; MAX_DEFENDERS still bounds the run
        if not cards_match(before, after):
            return True
        if attempt < len(SWIPE_PROFILES):
            print(
                f'    Card unchanged, retrying the swipe '
                f'({attempt}/{len(SWIPE_PROFILES)}) with a different gesture'
            )
    return False


async def feed_berry(device: DeviceAsyncWrapper, point: list[int], height: int):
    """Throws one berry at the defender.

    A plain 'input tap', not the tiny swipe the rest of these scripts use: this
    screen treats a press as picking the berry up, so a 1px swipe drags it and
    drops it in place, feeding nothing and consuming nothing.

    Tapped as a vertical column rather than a single point, because the berry
    never stops moving and a screencap costs the better part of a second. A android-three
    berry was tracked over 2176..2467 -- 291px of bob -- so by the time a tap
    lands on the spot it was located at, it has often gone, which is what made
    feeding come out 3, 0, 1 across three defenders of the same gym.

    The column has to cover the whole bob, and at first it did not. It was three
    taps at half the search span -- on the moto, y 1076/1136/1196 -- while the
    berry's measured bob on that phone was 1085..1217 with a median of 1195, so
    the column sat high and its 60px gaps were wide enough to fall between.
    Feeding came out 3, 4, 5 and 1 on four defenders that were not finished:
    hand-feeding the same gym, re-finding the berry before every tap, took 30
    berries in a row without a single refusal. So the column now spans the full
    find_berry range at BERRY_TAP_POINTS steps, which is the range the berry was
    just detected in and cannot have left.

    The whole column is walked in one shell round trip to keep it inside a single
    bob. Only the first tap to land on the berry feeds it -- the berry then
    disappears for about two seconds and does not return for ~3.4s (measured), so
    the rest of the column hits bare floor and costs nothing.
    """
    x, anchor_y = point[0], device.config['BERRY_BTN'][1]
    span = height * BERRY_FIND_HALF_Y
    n = BERRY_TAP_POINTS
    taps = '; '.join(
        f'input tap {x} {int(anchor_y + span * (2 * i / (n - 1) - 1))}'
        for i in range(n))
    await device.shell(taps)


def find_berry(width: int, height: int, offset: int, data: bytes,
               point: list[int]) -> list[int] | None:
    """Where the berry is right now, or None if none is being offered."""
    return find_berry_share(width, height, offset, data, point)[0]


def find_berry_share(width: int, height: int, offset: int, data: bytes,
                     point: list[int]) -> tuple[list[int] | None, float]:
    """find_berry, plus the share that decided it, for logging a miss.

    A miss is worth telling apart: a share of 0.000 is bare floor and a genuine
    refusal, while anything up to BERRY_PRESENT is a berry the search nearly
    saw -- a berry mid-fade, or the anchor drifted off it.
    """
    bx, by = point
    half = (BERRY_FIND_STEPS - 1) / 2
    hits, total = [], 0
    for yi in range(BERRY_FIND_STEPS):
        y = int(by + height * BERRY_FIND_HALF_Y * (yi / half - 1))
        for xi in range(BERRY_FIND_STEPS):
            x = int(bx + width * BERRY_FIND_HALF_X * (xi / half - 1))
            if not (0 <= x < width and 0 <= y < height):
                continue
            i = offset + (y * width + x) * 4
            total += 1
            if is_berry_pixel(data, i):
                hits.append((x, y))
    share = len(hits) / total if total else 0.0
    if share < BERRY_PRESENT:
        return None, share
    return [sum(p[0] for p in hits) // len(hits),
            sum(p[1] for p in hits) // len(hits)], share


# --- Naming the berry: the picker ---
# The floor berry cannot be named, only called pink or gold, and that split is
# blind exactly where it costs: nanab, pinap and golden razz all read gold, so a
# run that ate through the nanabs and pinaps walked on into the golden razz
# without the split ever moving. The only changeover it ever caught was razz ->
# nanab, which is why the one stop it did produce looked like a false alarm.
#
# Naming the floor berry was tried, with the six-bin hue makeup used below, and
# it does not hold there: the defenders are pink and stand inside the box, so
# two reads of one unchanged berry drifted 0.47 apart in total share while razz
# and nanab sit only 0.58 apart. No tolerance fits in that.
#
# The picker does hold. Each type is a large flat icon on a near-white sheet
# with nothing behind it, and a type that runs out disappears from the sheet
# altogether -- so "only golden razz and silver pinap are left" is readable
# directly, which is the stop that was actually wanted. Measured over three
# sheets on two phones, 1316x2560 and 720x1600, as a share of each icon's
# coloured pixels:
#
#   razz     magenta 0.583..0.633
#   nanab    magenta 0.396..0.422  yellow 0.133..0.150
#   pinap    magenta 0.000         yellow 0.421..0.433  orange 0.123..0.137
#   golden   magenta 0.000         yellow 0.061..0.069  orange 0.544..0.597
#   silver   magenta 0.000         yellow 0.000         grey   0.344..0.375
#
# Every gate below sits in an empty gap many times the spread of the readings
# either side of it, and the pair worth being careful about -- nanab against
# golden razz, the confusion that would spend the stock -- is parted by 0.40 of
# magenta against a flat 0.000.
#
# The sheet is found by scanning rather than configured, and the grid is in
# sheet-relative fractions that landed on both phones unchanged, so this needs
# no per-device band on any phone it ever runs on.
PICKER_COL_X = (1 / 6, 1 / 2, 5 / 6)    # three icons across the sheet
# Icon row centres, measured DOWN FROM THE SHEET TOP in screen widths -- not as
# a share of the sheet's height, which is what they were first written as. The
# sheet is only as tall as it needs to be, so a three-berry picker is one row
# and a shorter sheet: sharing out its height put the second row on the text
# labels, and a live android-one read came back
# ['pinap','golden','silver','unknown','unknown','silver']. Junk rows are not a
# cosmetic problem -- one that happened to read as a cheap berry would keep a
# run feeding while the game served golden razz. In width units the two rows
# land at 0.2226/0.5319 on the android-one and 0.2236/0.5361 on the moto, so the grid
# is the same on both and a row that does not exist falls off the sheet and
# reads empty.
PICKER_ROW_DY = (0.2226, 0.5319)
PICKER_CELL  = 0.085                    # half-box, share of screen width

# The game always lists berries in this order and never repeats one, so a read
# that is not a strictly increasing subsequence of it did not come off a
# picker. That is the check that catches a grid landing somewhere it should
# not, whatever the reason -- and it fails towards not feeding.
PICKER_ORDER = ('razz', 'nanab', 'pinap', 'golden', 'silver')
PICKER_SHEET_LIGHT = 195   # the sheet is near-white the whole way across
PICKER_SHEET_HITS  = 19    # of 20 columns sampled, to call a row part of it
PICKER_SLOT_INK    = 0.15  # less coloured area than this and the slot is empty
PICKER_SLOT_SHEET  = 0.15  # and this much bare sheet, or the box is off it
PICKER_SETTLE      = 1.5   # the sheet slides up, it does not appear

CHEAP_BERRIES   = ('razz', 'nanab', 'pinap')

# What select_cheap_berry says when it could not look: the disc is greyed out,
# so this defender is done. Distinct from None, which means it looked and the
# cheap berries are gone -- one moves to the next defender, the other stops the
# device.
PICKER_UNAVAILABLE = 'unavailable'

# How many berries may be fed between one picker read and the next. The picker
# is the only thing that can name what is selected, so this is the width of the
# window in which the game could have run a stock out and moved on without the
# run knowing: at most PICKER_RECHECK - 1 berries of the next type, and only
# once per stock that runs dry. Set to 1 to close the window completely, at the
# cost of a picker round trip per berry.
PICKER_RECHECK = 3
# ...unless the stock is plainly deep enough that it cannot run dry inside the
# window. Each berry on the picker carries its count as "xN" in a dark teal
# pill, and the pill grows by a digit's width for every digit, so how wide it is
# says how many digits N has without anything having to recognise them. Two
# digits means at least ten, which is more than a defender will take in one
# visit -- so a full picker round trip per three berries becomes one per
# defender. Measured on the android-one at 1316 wide: x33 is 119 across, x127 149,
# x182 153, so a digit is worth about 32 and a one-digit pill would be near 87.
# The threshold sits close to the two-digit end on purpose. Reading a deep stock
# as shallow only costs the old three-berry window; reading a shallow one as
# deep would keep feeding after the type ran out, which is how golden razz gets
# spent.
PICKER_WIDE       = 10      # berries between reads once the stock is x10 or more
PICKER_BADGE_WIDE = 0.082   # pill this wide, as a share of screen width, is x10+
PICKER_BADGE_TOP  = 0.005   # the band the pill sits in, below the slot centre,
PICKER_BADGE_BOT  = 0.080   # both as a share of screen width
PICKER_BADGE_LEFT = 0.16    # the pill hangs off to the left of the slot centre
PICKER_BADGE_RIGHT = 0.02
# How many picker round trips in a row may end with no readable berry before
# the defender is called finished. Only a backstop: the read at the top of the
# feeding loop is the one that decides a refusal, and it runs in between, so
# this fires only if the picker itself is what the berry keeps hiding behind.
PICKER_BLIND_LIMIT = 3
# How many looks open_berry_picker gets. It taps the disc only on a look that
# found no sheet at all, so this is a budget for the sheet's slide-up animation
# as much as for the tap, and the two share it.
PICKER_OPEN_TRIES = 5


def picker_sheet_top(width: int, height: int, offset: int, data: bytes) -> int | None:
    """The first row of the picker sheet, or None if no sheet is up."""
    for y in range(height * 55 // 100, height):
        light = 0
        for step in range(20):
            x = width * (10 + step * 4) // 100
            i = offset + (y * width + x) * 4
            light += (data[i] > PICKER_SHEET_LIGHT
                      and data[i + 1] > PICKER_SHEET_LIGHT
                      and data[i + 2] > PICKER_SHEET_LIGHT)
        if light >= PICKER_SHEET_HITS:
            return y
    return None


def picker_badge_width(frame: tuple, at: list[int]) -> int:
    """How wide this slot's "xN" count pill is, in pixels. 0 if there is none.

    The widest teal run anywhere in the band, not the run through the middle of
    it: the white digits cut the pill in two on every row they cross, so a row
    chosen for being central measures the left half of the pill and calls a deep
    stock shallow. The rows just above and below the text are unbroken, and
    taking the maximum finds them without having to know where they are.
    """
    width, height, offset, data = frame
    cx, cy = at
    x0 = max(0, cx - int(width * PICKER_BADGE_LEFT))
    x1 = min(width, cx + int(width * PICKER_BADGE_RIGHT))
    widest = 0
    for y in range(cy + int(width * PICKER_BADGE_TOP),
                   min(height, cy + int(width * PICKER_BADGE_BOT))):
        run = 0
        for x in range(x0, x1):
            i = offset + (y * width + x) * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            if b > r + 25 and g > r + 15 and r < 110 and b > 60:
                run += 1
                widest = max(widest, run)
            else:
                run = 0
    return widest


def picker_recheck_for(frame: tuple, at: list[int]) -> int:
    """How many berries may be fed off this slot before the picker is read again."""
    if picker_badge_width(frame, at) >= frame[0] * PICKER_BADGE_WIDE:
        return PICKER_WIDE
    return PICKER_RECHECK


def picker_slot_kind(frame: tuple, cx: int, cy: int, half: int) -> str | None:
    """Which berry is drawn in this slot: a name, 'unknown', or None if empty.

    'unknown' is not the same as empty and is never treated as cheap. A slot
    holding something this cannot name is a reason to stop, not to guess.
    """
    width, height, offset, data = frame
    cells = ink = 0
    bins = dict(magenta=0, pink=0, orange=0, yellow=0, green=0, grey=0, other=0)
    for y in range(cy - half, cy + half, 2):
        if not 0 <= y < height:
            continue
        for x in range(cx - half, cx + half, 2):
            if not 0 <= x < width:
                continue
            i = offset + (y * width + x) * 4
            r, g, b = data[i], data[i + 1], data[i + 2]
            cells += 1
            spread = max(r, g, b) - min(r, g, b)
            if spread < 26 and r > PICKER_SHEET_LIGHT:
                continue                        # bare sheet
            ink += 1
            rg, gb = r - g, g - b
            if spread < 26:
                bins['grey'] += 1               # silver pinap is the only grey berry
            elif g >= r and gb > 20:
                bins['green'] += 1              # leaves, which every berry has some of
            elif rg > 120 and gb < 0:
                bins['magenta'] += 1
            elif rg > 60 and gb < 20:
                bins['pink'] += 1
            elif rg > 40 and gb >= 20:
                bins['orange'] += 1
            elif -20 <= rg <= 60 and gb > 40:
                bins['yellow'] += 1
            else:
                bins['other'] += 1
    if not cells or ink / cells < PICKER_SLOT_INK:
        return None
    # An icon sits in bare sheet, so a real slot is 0.29..0.44 white around the
    # artwork. A box that has none of that is not on the sheet at all: the row
    # below a one-row picker lands on the navigation bar, whose dark grey is
    # unsaturated and was being read as three silver pinaps.
    if 1 - ink / cells < PICKER_SLOT_SHEET:
        return None
    share = {k: v / ink for k, v in bins.items()}
    if share['magenta'] >= 0.25:
        return 'razz' if share['magenta'] >= 0.50 else 'nanab'
    if share['orange'] >= 0.35 and share['yellow'] <= 0.20:
        return 'golden'
    if share['yellow'] >= 0.25 and share['orange'] <= 0.25:
        return 'pinap'
    if share['grey'] >= 0.20 and share['orange'] < 0.05:
        return 'silver'
    return 'unknown'


def picker_read(frame: tuple) -> list[tuple[str, list[int]]] | None:
    """Every berry on the open picker, in the order the game lists them.

    None means no picker is up. A sheet with nothing nameable on it counts as no
    picker: the gym floor is a pale blue-grey that can pass the near-white row
    test on its own, and calling that a picker would read an empty berry list
    off a feeding screen. Requiring a named icon as well is what tells them
    apart, and it is the safe way round -- a picker missed costs a retry, a
    feeding screen mistaken for an empty picker would report the stock gone.
    """
    width, height, offset, data = frame
    top = picker_sheet_top(width, height, offset, data)
    if top is None:
        return None
    half = int(width * PICKER_CELL)
    found = []
    for row_dy in PICKER_ROW_DY:
        cy = top + int(width * row_dy)
        for col_f in PICKER_COL_X:
            cx = int(width * col_f)
            kind = picker_slot_kind(frame, cx, cy, half)
            if kind is not None:
                found.append((kind, [cx, cy]))
    if not found:
        return None
    rank = [PICKER_ORDER.index(k) if k in PICKER_ORDER else -1
            for k, _ in found]
    if -1 in rank or any(a >= b for a, b in zip(rank, rank[1:])):
        return None
    return found


def disc_point(device: DeviceAsyncWrapper, width: int, height: int) -> list[int]:
    """Where to tap to open the berry picker: the item disc.

    The disc band's lower edge, not its middle. That band was cut to lie across
    the top of the disc for a brightness reading, so its middle lands just above
    the circle and taps the gym floor instead.
    """
    x0, x1 = BERRY_DISC_X
    _, y1 = band_of(device, 'BERRY_DISC_Y', BERRY_DISC_Y)
    return [int(width * (x0 + x1) / 2), int(height * y1)]


async def open_berry_picker(
        device: DeviceAsyncWrapper) -> tuple[list[tuple[str, list[int]]] | None, tuple | None]:
    """Opens the berry picker and reads it, with the frame it was read off.

    The frame comes back because the count badges are on it and nothing else
    ever sees the sheet: by the time the caller has tapped a berry, the sheet
    has slid away again. None for the list means the picker will not open.

    Not an error. The game greys the item disc out once a defender has taken
    its ten berries for the window, and a disabled disc does not open anything
    -- so "the picker will not open" is the game saying this defender is
    finished, which is exactly what a refused berry says. Raising on it stopped
    a android-one run dead on its first defender and threw away the count of what it
    had already fed.

    Two taps used to be enough and were not. The disc is a toggle: the old loop
    read once, tapped, read once more, and tapped AGAIN on a failed read, which
    closed the sheet it had just opened and then reported the picker
    unavailable -- a silent end to a defender at exactly PICKER_RECHECK berries
    fed, which is what stopped every defender of a android-one gym at 3. So a sheet
    that is up but not yet readable is waited out, never tapped.
    """
    sheet_seen = False
    for _ in range(PICKER_OPEN_TRIES):
        frame = await screencap_raw(device)
        if frame is None:
            await wait(PICKER_SETTLE, use_modifier=False)
            continue
        listed = picker_read(frame)
        if listed is not None:
            return listed, frame
        if picker_sheet_top(*frame) is not None:
            # Mid-slide, or a sheet the reader cannot make sense of. Either
            # way tapping the disc now would shut it.
            sheet_seen = True
            await wait(PICKER_SETTLE, use_modifier=False)
            continue
        await tap(device, disc_point(device, frame[0], frame[1]))
        await wait(PICKER_SETTLE, use_modifier=False)
    log(device, '  The berry picker did not open in'
                f' {PICKER_OPEN_TRIES} tries'
                + (' -- a sheet came up but could not be read'
                   if sheet_seen else ' -- the item disc is greyed out'))
    return None, None


async def select_cheap_berry(device: DeviceAsyncWrapper) -> tuple[str | None, int]:
    """Selects the cheapest berry still in stock, and how long it is good for.

    Returns the berry and the number of feeds that may follow before the picker
    has to be read again -- see PICKER_RECHECK. None for the berry means only
    premium ones are left.

    The picker lists types in the game's own order -- razz, nanab, pinap, golden
    razz, silver pinap -- and a type that has run out is simply not on the
    sheet, so the first cheap entry is the cheapest one left. Anything this
    cannot name counts as not cheap, which stops the run rather than spending
    into whatever it was.
    """
    listed, frame = await open_berry_picker(device)
    if listed is None:
        return PICKER_UNAVAILABLE, PICKER_RECHECK
    cheap = [(kind, at) for kind, at in listed if kind in CHEAP_BERRIES]
    if not cheap:
        log(device, '  Picker holds only',
            ', '.join(kind for kind, _ in listed) + ' -- no cheap berries left')
        return None, PICKER_RECHECK
    kind, at = cheap[0]
    recheck = picker_recheck_for(frame, at)
    await tap(device, at)
    await wait(PICKER_SETTLE, use_modifier=False)
    frame = await screencap_raw(device)
    if frame is not None and picker_read(frame) is not None:
        raise AutoBerryError(
            'The berry picker did not close',
            f'Tapped the {kind} at {at} and the sheet is still up. Nothing was '
            f'fed, because what is selected is no longer known.')
    return kind, recheck


async def berry_offered(
        device: DeviceAsyncWrapper) -> tuple[list[int], int, float | None] | None:
    """Is the defender still being offered a berry, i.e. will it take one?

    Absence has to repeat before it is believed. A berry that has just been
    taken is replaced by the next one fading in, so a single read timed into
    that gap calls a defender full when it would happily have taken nine more --
    which is exactly what happened on a live run, feeding one berry and moving
    on from a defender still showing a berry afterwards. An unreadable frame is
    likewise not a refusal; screencap wedges too often to trust one.
    """
    blind = 0
    best = 0.0
    for attempt in range(BERRY_OFFER_READS):
        frame = await screencap_raw(device)
        if frame is None:
            blind += 1
        else:
            at, share = find_berry_share(*frame, device.config['BERRY_BTN'])
            best = max(best, share)
            if at is not None:
                return at, frame[1], gold_share_of(*frame, device.config['BERRY_BTN'])
        if attempt < BERRY_OFFER_READS - 1:
            await wait(BERRY_RESPAWN + random.uniform(0, BERRY_RESPAWN_JITTER),
                       use_modifier=False)
    # Say which kind of miss it was. "Refused" and "screencap wedged" and
    # "the anchor is off the berry" all end a defender the same way otherwise,
    # and telling them apart from the log is the difference between a defender
    # that is finished and a run that is broken.
    log(device, f'  No berry after {BERRY_OFFER_READS} reads'
                f' (best {best:.3f} of {BERRY_PRESENT} needed'
                + (f', {blind} screencaps failed' if blind else '') + ')')
    return None


async def feed_defender(device: DeviceAsyncWrapper, chosen: str | None,
                        budget: int | None) -> tuple[int, str | None, str | None]:
    """Feeds the current defender until the game stops offering a berry.

    Returns what was fed, why it stopped ('golden', 'cap' or None), and which
    berry is selected now.

    The berry is named off the picker rather than the floor, and re-read every
    PICKER_RECHECK berries -- or every PICKER_WIDE, when the count badge says
    the stock is deep enough that it cannot run out in between. Checking only
    between defenders left a hole exactly the width of this loop: the android-one had 5
    nanabs, fed 8 times, and the 3 taps after the stack ran dry spent whatever
    the game reached for next.
    """
    fed = 0
    refused = False
    stop: str | None = None
    blind_after_picker = 0
    recheck = PICKER_RECHECK            # widened by select_cheap_berry on a deep stock
    since_check = recheck               # always read the picker for a new defender
    for _ in range(FEED_RUNAWAY):
        if budget is not None and fed >= budget:
            stop = 'cap'
            break
        offer = await berry_offered(device)
        if offer is None:
            refused = True
            break
        # Only once a berry is on offer: a full defender has no berry to feed
        # and its picker will not open either, so asking first would raise on a
        # gym that is simply finished.
        if chosen is None or since_check >= recheck:
            was = chosen
            chosen, recheck = await select_cheap_berry(device)
            since_check = 0
            if chosen is PICKER_UNAVAILABLE:
                # A greyed disc used to end the defender here, on the reading
                # that it had had its ten and the berry still on screen was the
                # tail of the last animation. Mid-defender that reading is
                # wrong, and it contradicts itself: the read at the top of this
                # loop just said a berry is on offer, and the disc goes grey for
                # a moment after every feed while the motivation ring fills. On
                # the android-one this ended every defender at exactly 3 fed -- one
                # PICKER_RECHECK -- and a swipe away and back found the same
                # defender still hungry and fed it three more, over and over.
                # So wait the animation out and look again; only a defender that
                # keeps the disc grey across PICKER_BLIND_LIMIT looks is done.
                chosen = was
                # Ask again next time round: nothing was read, so the window
                # this recheck was meant to close is still open.
                since_check = recheck
                blind_after_picker += 1
                if blind_after_picker >= PICKER_BLIND_LIMIT:
                    refused = True
                    break
                log(device, '  Item disc still greyed out after feeding;',
                            'waiting for the animation and looking again')
                await wait(PICKER_SETTLE, use_modifier=False)
                continue
            if chosen is None:
                stop = 'golden'
                break
            if chosen != was:
                log(device, f'  Feeding {chosen} berries'
                            + ('' if was is None else f' -- the {was} ran out')
                            + (f' ({recheck} before the next picker check)'
                               if recheck != PICKER_RECHECK else ''))
            # The sheet slid up over the gym floor and back down, so where the
            # berry was is stale -- and for as long as that animation runs the
            # berry often cannot be read at all. A miss HERE is not a refusal.
            # On the android-one every defender of a gym stopped on exactly this read,
            # at 0, 3 and 12 berries -- always a multiple of PICKER_RECHECK --
            # and a swipe away and straight back found the same defender still
            # being offered a berry and fed it three more. So go round again
            # and let the top of the loop, which is the read that does mean
            # refused, have its own look.
            offer = await berry_offered(device)
            if offer is None:
                blind_after_picker += 1
                if blind_after_picker >= PICKER_BLIND_LIMIT:
                    refused = True
                    break
                log(device, '  No berry readable as the picker closed;',
                            'looking again')
                continue
            # A round trip that named a berry and found one clears the tally:
            # PICKER_BLIND_LIMIT is about looks that fail in a row, and a
            # ten-berry defender takes three rechecks, so counting one stray
            # grey frame from each of them would end it a berry short.
            blind_after_picker = 0
        at, height, _ = offer
        await feed_berry(device, at, height)
        fed += 1
        since_check += 1
        await wait(BERRY_TAP_GAP, use_modifier=False)
    if not refused and stop is None:
        # Running out the cap is not the same thing as the defender refusing,
        # and it used to be reported as though it were: the android-one had its anchor
        # below the berry, so twenty taps in a row hit bare floor, the loop hit
        # this cap, and the gym was called done with every defender still
        # hungry. Say so loudly -- the cap only ever fires when something is
        # wrong with where we are tapping.
        log(device, f'  WARNING: hit the {FEED_RUNAWAY}-tap cap with a berry '
                    f'still on screen -- this defender is NOT finished')
    await wait(FEED_SETTLE, use_modifier=True)
    return fed, stop, chosen


async def berry_process_one(device: DeviceAsyncWrapper) -> int:
    """Feeds every defender in the gym, starting from the one on screen."""
    # Pointer location draws its readout across y 128-175, right over the name
    # banner the fingerprint reads, so it stays off here — unlike gift.py, which
    # turns it on for tap visibility.
    await pointer(device, False)
    await wait(1, False)
    device.stopped = None

    if not await on_feed_screen(device):
        raise AutoBerryError(
            'This does not look like a gym feeding screen',
            'Open a gym, tap a defender, then the berry icon, and try again.')

    # Nothing is read up front. The picker only opens over a defender that is
    # still being offered a berry, and a run that starts on an already-full one
    # has none -- refusing there threw away the rest of a gym whose other
    # defenders were still hungry. So the first defender that will take a berry
    # is where the berry gets chosen.
    budget = SPEND_CAPS.get(device.serial, SPEND_CAP)
    chosen: str | None = None
    if budget is not None:
        log(device, f'Spending at most {budget} berries')

    # Every defender seen so far, fed exactly once each. The run does not stop
    # at the first card it recognises, because the carousel bounces: a fling can
    # settle back on the card it came from, so a walk of a 3-defender gym came
    # out as Happiny, Blissey, Happiny, Chansey, Blissey. Stopping on the first
    # repeat ended that run after one defender. Instead this keeps swiping while
    # new defenders keep turning up, and gives up once IDLE_TURNS swipes in a
    # row have shown nothing new -- which is also how a one-defender gym ends.
    # One direction is not enough to see a whole gym. Measured on the moto's
    # four-defender gym on 2026-08-09, swiping one way and the other do not walk
    # the same ring:
    #
    #   forward   Chansey -> Nidoking -> Blitzle -> Chansey        (3 of 4)
    #   reverse   Chansey -> Blissey  -> Blitzle -> Nidoking -> .. (4 of 4)
    #
    # Going forward the carousel jumps Blitzle straight to Chansey, clean over
    # Blissey, at every swipe speed tried: 150ms, 300ms, and a 500ms drag that
    # was too gentle to move the card at all. Vertical swipes do not change the
    # card in either direction. So a defender that the forward ring skips is
    # reachable only by sweeping back, and Blissey -- the phone owner's own
    # Pokemon, the one actually down on motivation -- was never fed by any run.
    # Hence: go idle in one direction, turn round and sweep the other before
    # calling the gym done.
    seen: list[list[bool]] = []
    idle = 0
    reverse = False
    swept_back = False
    for _ in range(MAX_DEFENDERS * MAX_TURNS_PER_DEFENDER * 2):
        card = await card_fingerprint(device)

        already_seen = card is not None and any(cards_match(card, s) for s in seen)
        should_feed = not already_seen
        if already_seen:
            if await berry_offered(device) is None:
                idle += 1
            else:
                should_feed = True
                log(device, ' Previously seen defender still offers a berry; feeding again')
        if should_feed:
            # An unreadable card is fed but not recorded: better a defender fed
            # twice than one missed, and the swipe cap still bounds the run.
            if already_seen:
                n = 'recheck'
            else:
                n = str(len(seen) + 1) if card is not None else '?'
                if card is not None:
                    seen.append(card)
            if already_seen:
                log(device, 'Rechecking previously seen defender')
            else:
                log(device, f'Defender {n}: feeding')
            count, stop, chosen = await feed_defender(device, chosen, budget)
            log(device, f'  sent {count} feed attempt{"" if count == 1 else "s"}')
            if already_seen and count == 0:
                idle += 1
            else:
                idle = 0
            if budget is not None:
                budget -= count
            if stop == 'golden':
                device.stopped = 'golden'
                log(device, 'Reached the golden razz -- every cheap berry is',
                            'spent, so stopping here. The picker is left open',
                            'showing what is left.')
                break
            if stop == 'cap' or budget == 0:
                device.stopped = 'cap'
                log(device, 'Spent the berry cap, stopping here')
                break

        # The roster cap ends the run only once the gym has been swept both
        # ways. Reaching MAX_DEFENDERS going forward does not mean every
        # defender was reached: the forward ring skips cards -- seen live on
        # the android-one, where forward found 5 and the sweep back found the 6th --
        # and a card fingerprinted twice mid-slide counts twice, so the cap can
        # be reached with a defender still unvisited. Breaking here cancelled
        # the sweep back that exists to catch exactly that, and left the phone
        # parked on a defender still offering a berry with cheap berries still
        # in the bag. Reaching the cap now turns the run round instead of
        # ending it.
        full_roster = len(seen) >= MAX_DEFENDERS
        if full_roster and swept_back:
            log(device, f'Fed the full roster of {MAX_DEFENDERS}')
            break
        if full_roster or idle >= IDLE_TURNS:
            if swept_back:
                log(device, f'Nothing new in {IDLE_TURNS} swipes either way,',
                            'that is the whole gym')
                break
            reverse, swept_back, idle = True, True, 0
            log(device, f'Saw {len(seen)} defenders going forward,'
                if full_roster else
                f'Nothing new in {IDLE_TURNS} swipes,',
                'sweeping back the other way in case the carousel skipped one')

        log(device, '  Swiping to the next defender')
        if not await turn_card(device, reverse):
            if not swept_back:
                reverse, swept_back, idle = True, True, 0
                log(device, '  Forward swipe did not move; trying the other direction')
                if await turn_card(device, reverse):
                    continue
            log(device, '  The card will not change in either direction, stopping here')
            break
    else:
        log(device, 'Stopped at the swipe cap')

    return len(seen)


def sprite_share(frame: tuple[int, int, int, bytes], point: list[int]) -> float:
    """Share of coloured pixels in a box round point, 0 on a blank card.

    Tells a gym's Pokemon in the Today list apart from the white card it sits
    on, which is both how the list is found and how its end is spotted: an
    entry that is not there reads as bare card. Measured on the moto with the
    page scrolled to its bottom stop, across the three columns:

        sprite row      0.265  0.434  0.270
        every other row 0.168 at worst in any one column, mostly 0.000

    Colour rather than brightness because the card behind the sprites is white
    and its text is a single dark teal -- both flat, whichever way you sample
    them -- while a Pokemon is never either.
    """
    width, height, offset, data = frame
    cx, cy = point
    hits = total = 0
    for y in range(cy - SPRITE_HALF, cy + SPRITE_HALF + 1, SPRITE_STEP):
        for x in range(cx - SPRITE_HALF, cx + SPRITE_HALF + 1, SPRITE_STEP):
            if not (0 <= x < width and 0 <= y < height):
                continue
            i = offset + (y * width + x) * 4
            total += 1
            if max(data[i:i + 3]) - min(data[i:i + 3]) > SPRITE_COLOUR:
                hits += 1
    return hits / total if total else 0.0


def gym_slot(device: DeviceAsyncWrapper, index: int) -> list[int] | None:
    """Where the index-th gym sits in the Today view's grid, None if unmapped.

    The grid runs across in TODAY_GYM_COLS columns and wraps. Only the first row
    is mapped: the moto, the only phone free when this was measured, holds
    Pokemon in three gyms and so has no second row to measure. Rather than guess
    a row height, a phone with more gyms needs TODAY_GYM_ROW_STEP measured on it.
    """
    row, col = divmod(index, TODAY_GYM_COLS)
    x, y = device.config['TODAY_GYM_FIRST']
    step = device.config['TODAY_GYM_STEP']
    x, y = x + step[0] * col, y + step[1] * col
    if row:
        if 'TODAY_GYM_ROW_STEP' not in device.config:
            return None
        row_step = device.config['TODAY_GYM_ROW_STEP']
        x, y = x + row_step[0] * row, y + row_step[1] * row
    return [x, y]


async def hop_to_gym(device: DeviceAsyncWrapper, index: int) -> bool:
    """Leaves the gym on screen and opens the index-th gym. False if it didn't.

    Closing twice lands on the Research/Today page rather than the map, and its
    "Pokemon in Gyms" card lists every gym holding one of your Pokemon, so the
    walk is over that list rather than over the map. Tapping an entry opens that
    gym even when it is out of walking range, which is what makes the hop work
    at all from a phone sitting on a desk.

    Every tap after the first is aimed at a screen this cannot see coming, so
    the return value is the only claim made: on_feed_screen() has to agree we
    arrived. Anything else -- a gym that would not open, a list that ran out --
    comes back False and ends the run rather than tapping on blindly.
    """
    point = gym_slot(device, index)
    if point is None:
        log(device, f'Gym {index + 1} would be on a second row of the gym list,',
                    'which this phone has no TODAY_GYM_ROW_STEP for')
        return False

    # The picker sheet covers the X, and on the moto it covers it with the
    # silver pinap column -- so closing blind over an open picker would spend a
    # premium berry instead of leaving. A run that stopped on 'golden' leaves
    # the picker open by design, hence this check before any tapping.
    frame = await screencap_raw(device)
    if frame is not None and picker_sheet_top(*frame) is not None:
        log(device, 'The berry picker is open over the close button; not tapping past it')
        return False

    close = device.config['GYM_CLOSE_BTN']
    await tap(device, close)                                # feeding -> gym
    await wait(HOP_SETTLE)
    await tap(device, close)                                # gym -> Research
    await wait(HOP_SETTLE)

    # Scrolled to the end rather than by a measured amount. One drag settles
    # wherever its fling runs out -- the first attempt at this put the gym card
    # at y 730 by hand and y 450 when the script did it, and the tap went into
    # the card below. The bottom of the page is a hard stop, so flinging into it
    # lands the same way every time; flings after it has stopped do nothing.
    for _ in range(TODAY_SCROLL_FLINGS):
        await swipe(device, device.config['TODAY_SCROLL_FROM'],
                    device.config['TODAY_SCROLL_TO'], TODAY_SCROLL_MS)
        await wait(TODAY_SCROLL_SETTLE)

    frame = await screencap_raw(device)
    if frame is None:
        log(device, "Couldn't read the screen to find the gym list")
        return False
    share = sprite_share(frame, point)
    if share < TODAY_GYM_SPRITE_MIN:
        log(device, f'No Pokemon in slot {index + 1} of the gym list',
                    f'({share:.3f} colour, want {TODAY_GYM_SPRITE_MIN})')
        return False

    await tap(device, point)                                # Research -> gym
    await wait(HOP_SETTLE)
    await tap(device, device.config['GYM_DEFENDER_BTN'])    # gym -> feeding
    await wait(HOP_SETTLE)
    return await on_feed_screen(device)


async def berry_process_gyms(device: DeviceAsyncWrapper) -> int:
    """Feeds the gym on screen, then every other gym the Today view lists.

    The list is walked from the top, which revisits the gym the phone was parked
    on -- it is somewhere in the same list, and on the moto it was the middle
    entry, not the first. Feeding it twice costs one carousel walk and no
    berries, since its defenders refuse; skipping it would mean recognising
    which entry it is, which nothing on screen says.
    """
    total = await berry_process_one(device)
    if not GYM_HOP_KEYS <= set(device.config):
        return total
    for index in range(MAX_GYMS):
        if device.stopped:
            log(device, 'Staying put: there is nothing left to feed with')
            break
        if not await hop_to_gym(device, index):
            log(device, f'No feeding screen for gym {index + 1}; that is the last one')
            break
        log(device, f'Gym {index + 1}: feeding')
        total += await berry_process_one(device)
    return total


async def berry_process(devices: list[DeviceAsyncWrapper]):
    """Feeds each device's gym at once.

    Concurrently rather than one phone after another: most of a run is waiting
    out feed animations, so several phones take about as long as one, and adb
    multiplexes per serial so the devices do not contend.

    Each device is isolated. One phone not being on a feeding screen, or losing
    its cable, leaves the others feeding and is reported per device at the end.
    """
    results = await asyncio.gather(*(berry_process_gyms(d) for d in devices),
                                   return_exceptions=True)
    print()
    for device, result in zip(devices, results):
        if isinstance(result, KeyboardInterrupt):
            raise result
        if isinstance(result, BaseException):
            log(device, 'stopped:', ' '.join(map(str, result.args)) or repr(result))
        else:
            log(device, f'processed {result} defender(s)')


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
        raise AutoBerryError(f'Found no config file at {config_file_path}')
    config: CONFIG = yaml.safe_load(content)
    assert isinstance(config, dict), 'Incorrect config file format (should be an object with keys)'
    if not BERRY_KEYS <= set(config.keys()):
        raise AutoBerryError(f'Missing config key(s): {BERRY_KEYS - set(config.keys())}')
    for coords in config.values():
        assert isinstance(coords, list) and len(coords) == 2 and all(isinstance(i, int) for i in coords),\
            'Invalid coords format in config (should be list with two integers)'
    device.config = config
    return config


async def setup() -> list[DeviceAsyncWrapper]:
    """Finds every connected device and loads a config from each.

    Each phone carries its own config, because the coordinates are absolute
    pixels and no two models share a screen. A device without the berry keys is
    reported and dropped rather than aborting the rest — not every phone here
    has been mapped for feeding.
    """
    client = ClientAsync()
    devices: list[DeviceAsyncWrapper] = await client.devices()
    if not devices:
        raise AutoBerryError('No devices found')
    width = max(len(device.serial) for device in devices)
    ready: list[DeviceAsyncWrapper] = []
    for device in devices:
        device.label = device.serial.ljust(width)
        try:
            await get_config(device)
        except (AutoBerryError, AssertionError, ParserError) as e:
            log(device, 'skipped —', ' '.join(map(str, e.args)))
            continue
        device.display_id = await find_display_id(device)
        extra = f', reading display {device.display_id}' if device.display_id else ''
        log(device, f'ready, {len(device.config)} config keys{extra}')
        ready.append(device)
    if not ready:
        raise AutoBerryError('No devices with a usable config')
    return ready


def run_go(devices: list[DeviceAsyncWrapper]):
    """One pass over every phone's gym."""
    print(f'\nFeeding up to {MAX_DEFENDERS} defenders on {len(devices)} device(s)'
          ' (Ctrl+C to cancel)...')
    try:
        asyncio.run(berry_process(devices))
    except KeyboardInterrupt:
        print('\nCancelled.')
    except AutoBerryError as e:
        print('\n'.join(map(str, e.args)))


def interface(auto_go: bool = True):
    """Runs the main loop asking for user input."""
    global SLEEP_MODIFIER, SPEND_CAP, SPEND_CAPS
    print(
        '\n'
        ' ##                          ## \n'
        '##          AutoBerry         ##\n'
        ' ##                          ## \n'
    )
    devices: list[DeviceAsyncWrapper] = asyncio.run(setup())
    print(f"\nRunning on {len(devices)} device(s): {', '.join(d.serial for d in devices)}")
    print('\nCommands:')
    print('  go            feed every defender in each gym (start each on a feeding screen)')
    print('  spend <n>     an extra cap on berries each phone may spend;')
    print('                "spend off" for no cap, which is the default -- runs')
    print('                stop on their own when only golden razz is left')
    print('  delay <val>   get/set the extra delay modifier')
    print('  q             quit')

    # Starting the tool is itself the instruction to feed. Every run has begun
    # by typing go, so the prompt was pure ceremony -- and scripting a run meant
    # piping "go" in on stdin. The prompt still opens afterwards for a second
    # pass. Start with --no-go to get the prompt first, which is the only way to
    # set a spend cap before anything is spent.
    if auto_go:
        run_go(devices)

    while True:
        i = input('\n> ').strip()
        if i in ('q', 'quit', 'exit'):
            return
        if i.startswith('spend'):
            arg = i[5:].strip()
            if not arg:
                print('Spend cap:', 'off' if SPEND_CAP is None else SPEND_CAP)
                for serial, n in SPEND_CAPS.items():
                    print(f'  {serial}: {n}')
                continue
            if arg in ('off', 'none', '0'):
                SPEND_CAP, SPEND_CAPS = None, {}
                print('Spend cap: off -- runs stop when only golden razz is left')
                continue
            if '=' in arg:
                # Per phone: spend <serial>=<n> [<serial>=<n> ...]
                bad = False
                for pair in arg.split():
                    serial, _, n = pair.partition('=')
                    try:
                        SPEND_CAPS[serial] = int(n)
                    except ValueError:
                        print(f'Not a number of berries: {pair}')
                        bad = True
                if not bad:
                    for serial, n in SPEND_CAPS.items():
                        print(f'Spend cap: {serial} may spend {n} berries')
                continue
            try:
                SPEND_CAP = int(arg)
            except ValueError:
                print('Give a whole number of berries, "<serial>=<n>", or "off".')
                continue
            print(f'Spend cap: {SPEND_CAP} berries per phone, on top of stopping '
                  f'at the golden razz. Runs stop at whichever comes first.')
            continue
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
        if i in ('go', 'g', ''):
            run_go(devices)
            continue
        print('Unknown command.')


def main():
    try:
        interface(auto_go='--no-go' not in sys.argv)
    except AutoBerryError as e:
        print('\n'.join(map(str, e.args)))
        exit(1)
    except (KeyboardInterrupt, EOFError):
        print()


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('berries', 'feed_berries.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == '__main__':
    main()
