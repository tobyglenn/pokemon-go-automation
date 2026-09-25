#!/usr/bin/env python3
from __future__ import annotations

from . import config_paths

"""
AutoGBL — Plays Go Battle League sets in Pokémon GO on every connected Android
phone at once.

Uses the same AutoTraderConfig.yaml as trade.py, gift.py and berry.py, with the
GBL keys appended. Unlike battle.py, which pairs two phones against each other
for friendly battles, every phone here plays its own set against whoever the
game matches it with, so the devices are independent and one dropping out does
not stop the others. A device without GBL_MOVE_BTN is reported and skipped.

Start each phone on the GO BATTLE LEAGUE screen — the one with the green BATTLE
pill and "n/5 battles played" under it — and the run walks the loop:

    BATTLE -> CHOOSE YOUR LEAGUE -> bottom card -> USE THIS PARTY
           -> tap out the battle -> NEXT BATTLE -> CHOOSE YOUR LEAGUE -> ...

Rather than a fixed list of steps, each turn reads the screen and decides what
it is looking at, because the screens between battles are not fixed: a win, a
loss, a rank-up and an end-of-set reward all put a different number of screens
in the way. Four states cover the whole loop:

  * an orange reward button (COLLECT, CLAIM RANK REWARDS!)    -> tap it. These
    only come up when a set of five ends, and until they are cleared the BATTLE
    pill is greyed out, so this is what lets one run play set after set
  * a green action pill (BATTLE, USE THIS PARTY, NEXT BATTLE) -> tap the pill
  * the league list (white cards, no pill)                    -> tap the bottom
    card, which is the current cup; it is found in the frame rather than read
    from a coordinate, because the list changes every season
  * none of those                                             -> a battle is on
    screen, so tap the fast move

The teal X can overlap the centre of the entry BATTLE pill on a short screen.
Green pills are therefore tapped left of centre, inside the button but outside
the X, so the card cannot be closed accidentally. Once the party screen has
armed the battle loop, bottom-centre probes dismiss the teal result tick and
fire charged moves. A lit charged move gets a full-screen sweep through its
minigame. Safe lower-screen probes use shields before and between fast-attack
batches and select a reserve as soon as those battle overlays appear.

The one thing it will not do is tap at an unrecognised screen. Fast attacks are
only sent once a party screen has been tapped through (see `armed` in
play_device), so a phone that has fallen out to the map stops instead of
tapping at the map.
"""

import asyncio
import math
import re
import shlex
import struct
import sys
import time
from pathlib import Path
from typing import Sequence

from PIL import Image

from . import (
    excellent_throw_android,
    excellent_throw_ios,
    gbl_home_recovery,
    gbl_meta,
    gbl_strategy,
    gbl_vision,
    legendary_pokemon,
)

try:
    from ppadb.client_async import ClientAsync
    from ppadb.device_async import DeviceAsync
    import yaml
    from yaml.parser import ParserError
except ModuleNotFoundError as e:
    print(e)
    print('Run "pip install -r docs/requirements.txt" to install required packages.')
    exit(1)

CONFIG_FILE_DIR = '/storage/self/primary/'
CONFIG_FILE_NAME = 'AutoTraderConfig.yaml'
TMP_FILE_NAME = str(config_paths.temporary_file("tmp_gbl_{}.yaml"))   # per device: configs are pulled concurrently
CONFIG = dict[str, list[int]]

# The only coordinate the loop cannot find for itself. Every button it taps is
# located in the frame; this one is a patch of battlefield, which has no
# markings to find it by.
GBL_KEYS = {'GBL_MOVE_BTN'}

SLEEP_MODIFIER = 0
BATTLES_PER_SET = 5     # the game's own limit on one set

SCREENCAP_TIMEOUT = 20  # adb exec-out screencap wedges outright now and then

MENU_SETTLE = 1.2       # menu screens slide in; a tap during the slide misses
# A game started from nothing runs a splash, a login and a world load before it
# draws the map. Waiting that out beats reading the splash a dozen times.
GAME_COLD_START = 60.0
# Waiting out the slide is only half of it: the *coordinates* still come from
# the frame read before the wait, and the league chooser's three cards are a
# card apart. A frame caught while the sheet was still rising put Master where
# Ultra ended up, and the run played whole sets in the wrong league while its
# log said "taking the easiest card (Master League: Mega)" every time. The card
# is therefore found again after the wait, and only tapped where two reads
# agree, which costs one read per set.
LEAGUE_SETTLE_TOLERANCE = 0.01   # of screen height
# The door out of an open set, top left on every handset measured.
GBL_EXIT_DOOR = (0.104, 0.094)
LEAGUE_EXIT_LIMIT = 3
# Exits allowed once the configured league has been played this leg. Having
# played it proves it is on today's list, so a set in another league is a
# stray tap -- a mid-set reward misread, or NEXT BATTLE opening the featured
# cup -- and not a reason to adopt that league. On 23 Sep the moto-g's exit
# door failed three times on a Retro Cup party screen after four Master
# League: Mega battles, and the fallback played Retro Cup for the rest of the
# day. Past this many exits the leg stops instead.
LEAGUE_EXIT_LIMIT_PLAYED = 9
# League-list reads that name a card other than the configured league before
# the preference is given up. Pokemon GO rotates the list, and on a day it
# offers no Master League at all the run used to enter a Great League set, read
# its name on the party screen, back out, and do it again until it stopped with
# nothing played. Two reads rather than one because `settled_league_point`
# already agreed with itself on the label, so a second is a second chooser pass,
# not a second frame of the same one.
LEAGUE_ABSENT_READS = 2
POLL_GAP = 1.0          # between screen reads while walking the menus
# Header reads allowed for a swap to appear before it counts as refused. The
# animation outlasts a single POLL_GAP, so one read reports false failures.
SWITCH_VERIFY_READS = 4
# A GBL battle is capped at 4 minutes of play, but the clock starts after a
# countdown and stops over charged moves and switches, so the wall clock runs
# longer. This only bounds a misread of the end of a battle.
BATTLE_RUNAWAY = 330
# Reads of a screen that is neither a pill nor the league list, before a battle
# has been reached. That is the map, or a dialog nothing here knows: stop rather
# than tap at it. Generous because matchmaking shows plain screens too.
UNKNOWN_LIMIT = 40
# An unrecognised screen with no party tapped through is most often the league
# chooser scrolled past its cards. android-one kept ending runs parked on the "SEE
# WHAT'S COMING NEXT ON AUGUST 11!" preview at the bottom of that list, where
# the last playable card is a 20px sliver of white, the preview cards below it
# are greyed rather than white, and nothing on screen reads as anything -- so
# the loop waited out all 40 reads and stopped without playing. A drag is safe
# on a screen a tap is not, so it scrolls back toward the cards a few times
# before giving up.
UNKNOWN_SCROLL_EVERY = 4
UNKNOWN_SCROLLS = 4
# When scrolling has not helped either, there is one more thing to try before
# giving up: the door-shaped exit button GBL draws in the top-left corner, which
# is what a human reaches for to get a stuck GBL screen back to the main one.
# This breaks the rule about never tapping an unrecognised screen, and does it
# knowingly: by this point the alternative is stopping, and stopping leaves the
# phone parked on the screen that beat us, which is how two runs in a row began
# on a dead preview page and played nothing. The corner is the safest pixel to
# spend -- PoGo puts close or back there on every screen in this flow.
UNKNOWN_EXIT_AT = (20, 30)
EXIT_DOOR_X = 0.104
EXIT_DOOR_Y = 0.090
# Scrolling and the exit door both assume the phone is still somewhere in GBL.
# When it is not -- the exit door works, and what it opens onto is the world map
# -- neither does anything, and the loop used to spend its remaining reads
# waiting on a map that was never going to turn back into a league card. Live,
# that ended a day at 8 of 11 battles with the card still offering 2/5 played.
# From here on the reads are spent walking back the way a player would, through
# the main menu to BATTLE, which is the same route `recover_to_gbl` already
# takes for a screen that is merely unpressable.
UNKNOWN_RECOVER_FROM = 32
# Reads on an unrecognised screen before asking Android which app is in front.
# Not the first: a fade between screens reads as unrecognised too, and the
# question costs a `dumpsys window` the common case does not need.
FOREGROUND_CHECK_FROM = 2
# --- Screens off the battle flow ---
# Reads spent on a screen with nothing pressable before the run is written off.
# Pokemon GO interrupts the loop with things no amount of waiting clears -- a
# friendship level-up, a level-up, a mistaken close back to the map -- and a run
# that cannot get past them stops for the day with battles unplayed.
STRAY_LIMIT = 25
# Reads a stray screen gets while a battle is in progress before the battle is
# written off. Battlefields are green and the pill test is broad, so a single
# misread frame mid-battle is ordinary and must not disarm the loop.
STRAY_SETTLE = 4
# The main menu button: bottom centre on the map, clear of the item bar to its
# right and the buddy to its left. Measured on the moto at 720x1600.
MAIN_MENU_POINT = (0.50, 0.883)
# Main menu BATTLE icon sits above Pokéball on similar layouts after
# opening the main menu.
MAIN_MENU_BATTLE_POINT = (0.50, 0.565)
# Identical taps in a row before giving up. A menu tap that works changes the
# screen, so the next tap differs; the same tap landing over and over means it
# is hitting nothing. Comfortably above the few repeats a slow screen costs.
STALL_LIMIT = 8
# How many times a stalled screen is walked back to GO BATTLE LEAGUE before the
# leg is written off. A dead button is not worth a battle, but it is worth
# closing and reopening the battle screen over -- that is what a person does,
# and on 2026-08-31 the android-three sat on an action button at [465, 2157] needing
# exactly that. Bounded, because unbounded recovery is a silent forever-loop.
STALL_RECOVERY_LIMIT = 3
# Reads spent refusing the GBL card's teal close disc before the card's own
# action pill is taken instead. Refusing that disc is right -- pressing it left
# a live run on the map after one battle -- but the branch only ever said the
# pill would be taken on the next read, and the next read is the same screen,
# so it said so 253 times in a row on the moto on 2026-08-31. A refusal that
# cannot escalate is a forever-loop wearing a guard's clothes.
CARD_DISC_LIMIT = 3

# --- Fast attack ---
# Fast moves take 0.5-1.1s each, and every tap costs a shell round trip, so taps
# go out in batches down one shell call, the way berry.py walks its berry
# column. Tapping faster than the animation costs nothing: surplus taps land
# during the move and the game drops them.
MOVE_TAPS_PER_BATCH = 8
MOVE_TAP_GAP = 0.0      # input tap already imposes device-side latency
# The batch size above is a tap count, but what actually matters is how long the
# batch blocks, and that is set by the handset. Every `input` is a separate JVM
# start, so one tap costs 37ms on the android-three, 83ms on the compact-layout device and 673ms on the
# android-one, whose load average sits above 12. Eight taps is 0.3s of blindness on the
# android-three and 5.4s on the android-one -- long enough to sleep through a shield prompt, a
# switch and the end of the battle. Batches are therefore trimmed to fit this
# wall-clock budget, which leaves the two fast phones untouched at eight.
MOVE_BATCH_BUDGET = 1.0
MOVE_TAPS_MINIMUM = 2   # below this the burst stops generating useful energy
# The screen is only read between batches, so this window is time spent tapping
# blind. It used to be two batches of ten, about 3.2s, which was long enough for
# a battle to end unnoticed: the surplus taps dismissed the result screen, went
# on through NEXT BATTLE into the league list, and scrolled it down to the
# greyed-out "coming next" cards, which no test recognises. Once there the loop
# read 'battle' -- the fallback -- and kept tapping until the runaway timer. One
# short batch keeps the overrun to about half a second, inside the result screen
# the loop needs to see. A read costs ~0.3s, so the attack rate barely moves.
MOVE_BATCHES_PER_READ = 1
# Consecutive armed reads with a visually identical screen before concluding
# this is not a battle at all. A real battle animates constantly; a menu the
# tests cannot name sits still. Without this the only backstop was the 330s
# runaway, and the phone tapped at that menu for the whole five and a half
# minutes.
BATTLE_STATIC_LIMIT = 6

# The result screen's teal checkmark and the charged-move button are both near
# bottom-centre, but they are not in the same place and this cost android-one every
# battle of a set: the tick sits a whole disc lower than the charged button.
# Measured on the 1316x2560 panel it spans 0.444-0.556w and 0.846-0.904h, so a
# grid aimed at the charged button's 0.81-0.85h band clips only its top edge at
# one point in nine and never reaches the 0.20 fraction. Sample the tick's own
# middle, and tap RESULT_TAP_Y rather than CHARGED_Y, which lands above it.
RESULT_TAP_X = 0.50
RESULT_TAP_Y = 0.87
# The moto's tick sits lower than the android-one's.  Measured over a traced set on
# its 720x1600 panel the disc spans 0.869-0.909h, centred 0.889, so the shared
# 0.87 lands on the very top rim of the circle -- where the disc is barely a
# pixel wide -- and the sample rows above straddle its top edge instead of its
# middle.  Scored across that set the rows below read the real tick at 0.89
# against the shared rows' 0.44, and drop the in-battle false positive on the
# charged-move button (MOTO_CHARGED_Y 0.855) from 0.22 to 0.00.
MOTO_RESULT_TAP_Y = 0.889
MOTO_TEAL_SAMPLE_Y = (0.880, 0.889, 0.898)
CHARGED_X = 0.50
CHARGED1_X = 0.35
CHARGED2_X = 0.65
CHARGED_Y = 0.915
COMPACT_ASPECT_MAX = 2.05
COMPACT_CHARGED_Y = 0.84
# compact-layout device family is 19.5:9 and 20:9, so 2.22 at the tallest.  The band stops
# short of 2.4: taller than that is the ordinary layout, whose charged-move
# discs sit lower than the letterboxed Moto rows, and reading a taller phone
# with Moto rows samples the grass above the buttons and never sees one.
MOTO_ASPECT_MAX = 2.30
MOTO_CHARGED_Y = 0.855
TEAL_SAMPLE_X = (0.465, 0.50, 0.535)
TEAL_SAMPLE_Y = (0.858, 0.875, 0.892)
TEAL_READY_FRACTION = 0.20

# The weekly report's close disc sits below the card's stat rows, and it is a
# disc rather than a hairline: the SE's measures 75px on a 1334px frame, while
# the ring above it measures 8.
WEEKLY_DISC_MIN_Y = 0.75
WEEKLY_DISC_MIN_SPAN = 0.03

# The rank sheet has no close disc of its own: it is shut from a point below
# it, measured on the android-three at 1224x2992 where the tap that cleared it landed at
# 612,2827.  Kept as fractions because that is the only form that survives the
# jump to the iPhones, which draw the same sheet over the same card.
RANK_MODAL_CLOSE_X = 0.50
RANK_MODAL_CLOSE_Y = 0.945

# --- Is the charged move ready? ---
CHARGED_DISC_ROWS = (0.900, 0.915, 0.930)
COMPACT_CHARGED_DISC_ROWS = (0.800, 0.840, 0.880)
MOTO_CHARGED_DISC_ROWS = (0.820, 0.855, 0.890)
CHARGED_DISC_X = (0.44, 0.56)
CHARGED_DISC_STEPS = 9
CHARGED_DISC_OUTSIDE_X = (0.30, 0.34, 0.66, 0.70)
CHARGED_DISC1_X = (0.29, 0.41)
CHARGED_DISC1_OUTSIDE_X = (0.16, 0.20, 0.49, 0.53)
CHARGED_DISC2_X = (0.59, 0.71)
CHARGED_DISC2_OUTSIDE_X = (0.47, 0.51, 0.79, 0.84)
CHARGED_DISC_CONTRAST = 100

# The rim is what says "tappable", not the fill.  Three handsets were traced
# mid-battle to settle this, and the disc turns out to be one game-wide shape
# measured in screen *width*: centres 0.3086 and 0.6903, radius 0.0965 on the
# android-one (1316x2560), the compact-layout device (720x1600) and the android-three (1224x2992) alike.  Only
# the row moves with the layout.
#
# Every colour test tried before this one failed on real frames:
#   * "all three rows stand out from beside them" (disc_present) never fired on
#     the android-one at all -- its third row sat below the buttons -- and read 0 in 14
#     compact-layout device battles, so both phones played whole sets on fast moves.
#   * "the disc is one flat colour" looked right until a ready move with spare
#     energy turned up: it draws a second, darker band up from the bottom, so a
#     ready Fusion Flare reads 255,172,114 over 255,125,1 -- as two-tone as a
#     half-charged one.
#   * "the disc is opaque all the way up" is the true energy test (the unfilled
#     part is transparent and the battlefield shows through), but it cannot
#     separate charged-and-greyed from charged-and-tappable: during an opponent
#     animation a full disc dims to within a few levels of a half-full one, and
#     over dirt rather than grass the two orders swap.
# The white rim only appears on the button the game will accept a tap on, and it
# scored 1.00 on all 9 ready discs against 0.00-0.15 on the 33 that were not,
# across all three phones.
CHARGED_DISC_CX = (0.3086, 0.6903, 0.5000)
CHARGED_DISC_RADIUS = 0.0965
CHARGED_DISC_CY = 0.9052
COMPACT_CHARGED_DISC_CY = 0.8245
MOTO_CHARGED_DISC_CY = 0.8431
# A little slack around the measured row, so a layout that shifts by a few
# pixels is still read instead of silently going blind for a whole set.
CHARGED_DISC_CY_SEARCH = (-0.008, -0.004, 0.0, 0.004, 0.008)
# The rim band, as a fraction of the radius: white runs 0.90-0.99 and the
# battlefield resumes by 1.02.
CHARGED_RIM_FRACTIONS = (0.93, 0.96)
CHARGED_RIM_ANGLES = 24
CHARGED_RIM_READY = 0.75
# A white screen would satisfy the ring on its own, so the ring only counts
# while the ground just outside it is not white too.
CHARGED_RIM_OUTSIDE = 1.14
CHARGED_RIM_OUTSIDE_MAX = 0.40
CHARGED_LAUNCH_ATTEMPTS = 2
CHARGED_LAUNCH_CONFIRM = 0.22
# Reads per tap before the tap counts as dropped. Every poll is a whole frame
# and an OCR pass, about 1.5s on the android-one, and three of them behind three
# attempts is fifteen seconds of a battle spent tapping one point and reading --
# no fast moves, no shield answered. That is the pause a watcher sees. A tap
# that lands is now visible on the first or second poll (the button goes dark),
# and one that does not is better abandoned than paid for.
CHARGED_LAUNCH_POLLS = 2
CHARGED_MINIGAME_SETTLE = 0.58
# The bubble field is swept in whole passes of eight gestures. Six passes cover
# the spawn window on a fast phone, but the sweep is one blocking shell call, so
# on a slow handset it runs long past the window and the loop wakes up somewhere
# else entirely: six passes cost 4.2s on the android-three and 34.7s on the android-one, against
# a window of about 6.5-7.0s. Passes are trimmed to fit the budget so a slow
# phone gets fewer complete rasters rather than one that overruns fivefold.
# Every pass covers the whole playfield, so the first is worth the most.
MINIGAME_SWEEPS = 6
MINIGAME_GESTURES_PER_SWEEP = 8
MINIGAME_SWIPE_MS = 50
MINIGAME_BUDGET = 6.0

# --- Fast-phone path: more taps and sweeps per batch ---
# Named for the measurement, not the handset. These were written for the compact-layout device
# and the android-three both, but the gate was `moto_layout`, an *aspect* test matching
# 2.05 < aspect <= 2.30 for the letterboxed Moto battlefield. The android-three is
# 1224x2992, aspect 2.44, so it failed that test and silently took the
# conservative defaults -- 8 taps and 6 sweeps -- despite measuring 35ms per
# `input`, the quickest phone in the fleet, against the compact-layout device's 61ms. It built
# energy slower than a phone nearly twice its `input` cost.
# Layout decides *where* to tap and must stay keyed on aspect; how *fast* to tap
# is a property of input cost, so it is keyed on the measurement instead.
# Measured: android-three 35ms, compact-layout device 61ms, android-one 713ms.
FAST_INPUT_MAX = 0.10
FAST_MOVE_TAPS_PER_BATCH = 14
FAST_MOVE_BATCH_BUDGET = 1.45
FAST_MOVE_BATCHES_PER_READ = 2
FAST_MINIGAME_SWEEPS = 8
FAST_MINIGAME_GESTURES_PER_SWEEP = 8
FAST_MINIGAME_SWIPE_MS = 40
FAST_MINIGAME_BUDGET = 7.0
# --- How often a charged probe interrupts the fast attacks ---
# `fast_attack` round-robined the battlefield point with every charged disc, and
# `charged_move_points` returns three of them, so one tap in four was a fast
# move: a 14-tap batch on the android-three bought three quick attacks and eleven taps at
# buttons that stay dead until the energy those quick attacks generate arrives.
# Spending taps on the probes is spending the thing the probes are waiting for.
# One probe every few fast moves still walks all three discs inside a single
# batch, and leaves the burst doing what it is named for.
FAST_TAPS_PER_CHARGED_PROBE = 3
# The vertical extent of the bubble field itself; see minigame_segments.
MINIGAME_TOP = 0.50
MINIGAME_BOTTOM = 0.92

# --- How expensive is one `input` on this handset? ---
# Measured once per phone at setup with a keycode the game ignores, so the probe
# taps nothing. KEYCODE_UNKNOWN is 0. Assume a fast phone until measured.
INPUT_COST_DEFAULT = 0.05
INPUT_COST_SAMPLES = 8
INPUT_COST_CEILING = 1.5    # a wilder reading than this is a stall, not a cost

# --- Slow phones: overlap the JVM starts instead of chaining them ---
# The cost of an `input` is a zygote fork and a class load, and on the android-one it
# is paid against a phone with 56MB free and its swap entirely full, so the
# start is spent faulting pages back off flash rather than running. That waits
# on I/O, and waits overlap: eight chained `input` cost 5524ms on the android-one and
# 1314ms backgrounded, with the CPU 357% of 800% idle throughout.
# Backgrounding them all at once would land eight touch events in the same
# instant, which the game reads as one, so the launches are staggered instead:
# the stagger sets how far apart the events land and the start cost becomes a
# one-off tail on the batch rather than a per-tap price. Only worth doing where
# a start is expensive -- on the android-three and the compact-layout device a start is cheaper than the
# stagger, so chaining already spaces the taps better than this would.
PARALLEL_INPUT_MIN = 0.15
# Fast moves take 0.5-1.1s and surplus taps are dropped, so tapping much faster
# than this buys nothing; this is about half a fast move.
PARALLEL_TAP_STAGGER = 0.25
# A backgrounded batch is only as long as the read it runs alongside, and on the
# android-one that read is 1.1-1.5s while the batch was sized to 1.0s: the phone
# finished tapping and then stood still waiting for a frame it had already
# started. Sized to the read instead, the same window buys six taps rather than
# four, which is five fast moves a cycle rather than three.
PARALLEL_MOVE_BATCH_BUDGET = 1.5
# The minigame on a phone where an `input` start is expensive.
#
# The sweep is the one place the batch trimmer had cut a phone to a single pass:
# at 738ms a start plus a 50ms flick, one gesture cost 0.79s against a per-sweep
# budget of 0.75s, so the android-one played the whole bubble window with nine flicks
# spread over 7.1s -- 0.45s of finger actually on the glass. The compact-layout device and the
# android-three get eight sweeps in the same window and it is exactly the difference a
# watcher sees.
#
# Two things fix it, and both come from the gesture being a swipe rather than a
# tap. A swipe's cost is a start *and* its duration, and the duration is the
# useful part -- the game pops what the finger crosses, so a long drag samples
# many more MOVE events per expensive start than a flick does. And backgrounding
# the starts pipelines them behind the drags, so the drags run back to back.
# Measured on the android-one mid-battle: 18 backgrounded 240ms swipes staggered 0.12s
# took 4.33s, and 27 took 5.71s -- 0.21s a gesture against 0.79s chained. The
# drag is set just under that so the gestures run nose to tail and the finger is
# on the glass for essentially the whole bubble window.
SLOW_MINIGAME_SWIPE_MS = 200
PARALLEL_SWIPE_STAGGER = 0.12
# What a backgrounded gesture actually costs once the pipeline is full. The
# stagger above is what the shell sleeps between launches; the phone cannot
# sustain that rate, and this measured figure is what batches are sized from.
PARALLEL_SWIPE_INTERVAL = 0.22
CHARGED_START_AFTER_GET_READY = 0.28

# --- A cheaper way to touch a slow phone: `monkey --port` ---
# An `input` costs a JVM start, and on the android-one that start is 650ms against a
# phone whose swap is full -- eighteen times the compact-layout device's, and it shows in the
# win column rather than in the log: the leg lands three fast attacks in the
# window the compact-layout device lands fourteen, and loses matches it was winning.
#
# `monkey --port` pays that start once. It listens on a device-side socket and
# takes one command per line, so a tap is a line of text and a round trip:
# measured on the same android-one, 5.2ms a tap sequentially and 3.3ms pipelined,
# against 650ms. It injects through InputManager under the shell uid, exactly
# as `input` does, so SELinux has nothing to say about it -- unlike sendevent,
# which this phone blocks outright.
#
# Three things about monkey's network mode shape the code below.
#
# It serves one client for its lifetime: when the socket drops it tries to
# restart its listener and dies of an IllegalThreadStateException. So the
# connection is opened once per run and held for the whole of it, and a broken
# channel means a new process rather than a reconnect.
#
# It has no swipe. A drag is a `touch down`, a run of `touch move` and a
# `touch up`, paced from the host -- which samples the path more finely than
# `input swipe` does, since each move is a real MOVE event.
#
# And its `press` will not take KEYCODE_UNKNOWN, so the cost probe presses
# keycode 0 numerically instead. The game ignores it either way.
MONKEY_DEVICE_PORT = 1080
MONKEY_PROCESS = 'com.android.commands.monkey'
MONKEY_START_TRIES = 15
MONKEY_START_GAP = 0.4
MONKEY_REPLY_TIMEOUT = 5.0
# One move event per frame of the drag: finer than this is spending round trips
# on a path the game samples at 60Hz anyway.
MONKEY_DRAG_STEP_MS = 16
MONKEY_DRAG_STEPS_MAX = 40
MONKEY_DEFAULT_SWIPE_MS = 300
# What one command costs on the wire, measured on the android-one: subtracted from the
# pauses inside a drag so the gesture still lasts about as long as it asked to.
MONKEY_WIRE_MS = 5
# Over a socket a tap costs nothing, so the spacing a JVM start gave the compact-layout device
# for free has to be asked for: touches landing in the same instant are read as
# one gesture rather than as several taps. This is the compact-layout device's own measured
# cadence, which lands every tap of a burst.
MONKEY_TAP_GAP = 0.06

# --- "Attack incoming! Use a Protect Shield?" ---
# This was the last blind probe, described as landing on the shield during the
# prompt and on empty battlefield otherwise. The second half was false in the
# way that matters: it went out on every armed read, and when a battle ended
# mid-batch the remaining probes walked the game forward, because 0.73h is also
# the height of NEXT BATTLE on the result screen (0.723h) and of the bottom
# league card (which spans 0.695-0.865h). Two taps later the phone sat on the
# party screen -- where the same point lands on the middle Pokemon, opening the
# party editor's picker -- and that is where sets kept coming to rest, looking
# for all the world like a missed USE THIS PARTY.
#
# So detect it, as the iPhone already does. Measured on android-one's 1316x2560 panel
# the hexagon's body spans 0.395-0.612w and 0.712-0.826h, centred at
# 0.502w/0.769h -- the old point was inside it, but near its top edge. Scored
# across 277 traced frames the real prompt reaches 128 and the next frame down
# reaches 60, so the shared disc threshold clears it with room to spare.
SHIELD_TAP_X = 0.502
SHIELD_TAP_Y = 0.835
SHIELD_DISC_ROWS = (0.820, 0.830, 0.840)
COMPACT_SHIELD_DISC_ROWS = (0.740, 0.770, 0.800)
# Shield placement shifts substantially with aspect ratio: the android-one disc is
# centred near 0.77h while the tall-layout device is near 0.86h. Find the visible hexagon's
# full vertical contrast band instead of sharing either handset's fixed y.
SHIELD_SCAN_Y = (0.60, 0.93)
SHIELD_MIN_HEIGHT = 0.045

# --- CHOOSE YOUR PARTY ---
# Measured across every menu frame in the traces; see party_screen().
PARTY_PILL_Y = 0.8434
PARTY_PILL_TOLERANCE = 0.006
PARTY_TOP_Y = 0.055
PARTY_TOP_X = (0.20, 0.35, 0.50, 0.65, 0.80)
PARTY_TOP_MAX = 120
PARTY_PANEL_Y = 0.62
PARTY_PANEL_X = (0.12, 0.88)
PARTY_PANEL_STEPS = 20
PARTY_PANEL_WHITE = 205
PARTY_PANEL_FRACTION = 0.95

# --- "Switch in a new Pokemon?" ---
# A second blind probe used to sit here, at 0.34w/0.82h, described as landing on
# the left reserve during a forced switch and on empty battlefield otherwise. It
# does not: it lands on the swap button, so every armed read opened this sheet.
# The sheet covers the whole bottom of the screen -- fast-attack area and
# charged-move buttons with it -- so once it was up the loop spent the rest of
# the battle tapping into it while the Pokemon stood still, which is what the
# runaway timer kept catching.
#
# So the sheet is detected instead. It is a flat slate panel below a hard edge
# at 0.795h, measured on the Pixel at rgb(40, 46, 59): dark, but blue-tinted,
# and the tint is what separates it from the near-black bottom of the league
# chooser (8, 8, 8) and that screen's dark green backdrop (20, 39, 28).
SHEET_ABOVE_Y = 0.750        # battlefield above the tall tall-layout device switch sheet
SHEET_SCAN_Y = (0.800, 0.805, 0.810)  # flat slate below the panel edge
SHEET_SCAN_X = (0.15, 0.85)
SHEET_STEPS = 24
SHEET_FRACTION = 0.85
SHEET_MIN_RED = 26
SHEET_MAX_BLUE = 90
SHEET_BLUE_OVER_RED = 10
# Where the offered Pokemon sit. A single reserve is centred and two sit side by
# side, so a tap that finds no card is followed by the next position on the next
# read rather than by a guess at how many are left.
SHEET_CARD_Y = 0.90
SHEET_CARD_X = (0.50, 0.32, 0.68)
SWITCH_RESERVE_X = 0.90
SWITCH_RESERVE_Y = (0.605, 0.695)
SWITCH_ONLY_Y = 0.69
OPPONENT_CROP = (0.50, 0.02, 1.0, 0.32)

# --- Party picker / owned-roster scan ---
# These are screen fractions, not handset pixels.  Pokemon GO draws this sheet
# at the same fractions on the tall Android and iPhone layouts.  Every action
# is preceded by OCR verification in the helpers below; these coordinates are
# never used from an unverified screen.
PARTY_SLOT_POINT = (0.23, 0.715)
PICKER_SEARCH_POINT = (0.50, 0.22)
PICKER_CLEAR_POINT = (0.92, 0.22)
PICKER_SEARCH_BACK_POINT = (0.095, 0.22)
PICKER_CANCEL_POINT = (0.28, 0.94)
PICKER_DONE_POINT = (0.73, 0.94)
PICKER_SCROLL = ((0.50, 0.80), (0.50, 0.38))
PICKER_SETTLE = 1.0

# --- The green action pill ---
# BATTLE, USE THIS PARTY and NEXT BATTLE are all the same pill, and finding it
# in the frame is what lets one loop drive screens whose layouts differ.
#
# The pill is a left-to-right gradient, not a colour. Sampled straight across
# one on both phones, it runs mint -> teal:
#   0.30w (162, 218, 148)   0.43w (138, 216, 152)   0.57w ( 90, 209, 160)
#   0.37w (155, 218, 148)   0.50w (114, 214, 155)   0.70w ( 36, 203, 168)
# Red falls from 162 to 36 across the same button, so any floor on red splits
# the pill down the middle. What actually holds all the way across is green:
# high, above blue, and far above red.
PILL_GREEN = 195             # green 203-218 across the whole gradient
PILL_GREEN_OVER_RED = 25     # margin 56 at the mint end, 167 at the teal end
PILL_BLUE = 130              # blue 148-168; excludes yellow-greens
PILL_RED_DROP = 15           # mint-to-teal gradient must darken left-to-right (18 on tall-layout device)
# What that leaves out, all sampled off these screens:
#   grass on the battlefield  (110, 180,  90) — green below 195
#   the card behind the pill  (248, 254, 239) — green-over-red is only 6
#   sky                       (150, 200, 240) — blue above green
# Normal action pills live near the bottom. Keeping this scan narrow is an
# important map-screen guard: bright horizontal scenery near mid-screen can
# otherwise resemble the mint-to-teal gradient. Reward-tier screens get a
# separate, wider scan only after their white panel has been identified.
# The lower bound reaches the last row on purpose. A 0.96 floor was set on
# 19.5:9 Android screens, where the BATTLE pill lands around 0.91; the 16:9
# iPhone draws the same pill at 0.965-0.999 and it fell straight through the
# scan, so the chooser read as "button below the fold" and scrolled forever.
HOME_CHECK_EVERY = 6  # armed reads between map/menu checks inside a battle
PHANTOM_LIMIT = 6  # home screens read as a battlefield before the run gives up
PILL_SCAN_Y = (0.60, 0.999)
REWARD_PILL_SCAN_Y = (0.08, 0.999)
PILL_SCAN_X = (0.05, 0.95)
PILL_SCAN_STEPS = 40
PILL_TAP_X = 0.36            # avoids the centred X overlapping entry buttons
# The pill spans 0.30-0.70 of the width on both phones. The floor is set below
# that with room for a narrower pill on a screen not yet seen, and above the
# widest HP bar (0.29) in case one ever falls inside the scan.
PILL_MIN_WIDTH = 0.30
# Between its first and last green pixel a row must be unbroken. The pill's
# white label ("BATTLE", "USE THIS PARTY") is counted as part of the button
# rather than as a gap: it covers about 30% of the row it crosses, which is
# enough to fail any honest fill threshold on its own -- measured 0.66-0.72 on
# the BATTLE pill, flickering either side of a 0.70 cut, so no band ever formed.
# Only a real gap, showing something that is neither pill nor label, counts
# against a row. That is what still rejects the pair of charged-move buttons,
# which reach as wide as a pill but have battlefield between them.
PILL_ROW_FILL = 0.85
# A row must also be mostly the colour itself, ignoring the label. Without this
# a line of coloured text on a white card passes everything above: its first and
# last letters are far apart and the white between them reads as label. A button
# measured 0.69-0.75 of its own colour with the text across it; a text line is
# nearer 0.25.
BUTTON_MIN_DENSITY = 0.55
# Pill heights measured 0.037 (android-one) and 0.039 (moto) of the screen.
PILL_MIN_HEIGHT = 0.020

# The Basic/Premium chooser's pink ribbon is a stronger signal than the white
# panel outline on android-one, where the outline can be clipped by the viewport. If
# this ribbon is visible we scan the whole screen and always take the highest
# green BATTLE pill: Basic Rewards is above Premium Rewards.
BASIC_BANNER_RED = 210
BASIC_BANNER_GREEN = (70, 170)
BASIC_BANNER_BLUE = (110, 210)
BASIC_BANNER_RED_OVER_GREEN = 45
BASIC_BANNER_BLUE_OVER_GREEN = 15
BASIC_BANNER_MIN_WIDTH = 0.28
BASIC_BANNER_MIN_HEIGHT = 0.012
BASIC_BANNER_SCAN_Y = (0.05, 0.85)

# --- The orange buttons at the end of a set ---
# After the fifth battle the set is over and the BATTLE pill is disabled until
# the rewards are taken. What blocks the next set is an orange button in the
# pill's own slot -- COLLECT for a basic reward, then CLAIM RANK REWARDS! --
# and neither is green, so the pill test cannot see them. Sampled across:
# (254, 186, 90) ... (254, 155, 57), with white text through it.
ORANGE_RED = 240
ORANGE_GREEN = (140, 205)
ORANGE_BLUE = 115
# Rejects the pink "Basic Rewards" banner (233, 90, 140), whose blue is above
# its green, and the card's own cream (250, 250, 248).
ORANGE_MIN_MARGIN = 50       # red over green, and green over blue
# CLAIM RANK REWARDS! spans 0.80 of the width; a COLLECT tile, being one reward
# in a row of them, measured only 0.138. The rows that cross the whole reward
# row are wider still but only 0.28 orange, so the density test rejects them and
# the floor can sit this low.
ORANGE_MIN_WIDTH = 0.10

# --- The reward tile a finished set leaves highlighted ---
# `find_orange` needs a run of rows that are orange right across the button, and
# a reward *tile* is not that. The SE's next unclaimed tile is an orange square
# whose middle is a white "?" disc over green grass, so only its border and
# corners are orange: rows through it covered 1.00 of their span at the top and
# bottom edges and 0.33-0.67 through the artwork, breaking every run well short
# of PILL_MIN_HEIGHT. `find_orange` returned None, control fell through to the
# BATTLE label OCR still reads on the same card, and that pill is *dead* until
# the tile is taken -- the SE tapped it 8 times, three recoveries deep, and
# finished its day with 0 battles played.
# So the tile is found by column instead of by row: orange pixels anywhere in
# the band under the pink banner, grouped into vertical runs. Measured there,
# the real tile is 0.171 (SE) and 0.183 (moto) of the width, while the badges
# and label fragments sharing the band reach only 0.07.
REWARD_TILE_SCAN_STEP = 2      # every other column; the tile is ~130px wide
REWARD_TILE_BAND = 0.135       # of the height, below the banner
REWARD_TILE_GAP = 0.012        # of the width; wider merges neighbouring tiles
REWARD_TILE_WIDTH = (0.10, 0.30)
REWARD_TILE_MIN_HEIGHT = 0.040
# Down from the tile's top edge. The card's teal close X overlaps the tile's
# bottom-left corner -- its centre sits *below* the tile on the SE -- so the tap
# is kept high in the tile rather than at its centre, which put it 33px from the
# X's edge. At 0.30 the gap is 123px.
REWARD_TILE_TAP_Y = 0.30

# --- The league list ---
# Cards are near-white bars. They are scanned down a narrow band at the right of
# the card, clear of the league icon and of the title text: across the full card
# width the icon and text break the run, and the scan found only the blank strip
# under the text -- two cards of the three on the android-one, and the bottom of a card
# rather than its middle.
CARD_SCAN_X = (0.86, 0.93)
CARD_SCAN_Y = (0.28, 0.80)   # below the "CHOOSE YOUR LEAGUE" heading, above bottom nav bar
CARD_SCAN_STEPS = 16
CARD_ROW_WHITE = 0.90
WHITE_LEVEL = 228            # per-channel floor for "near-white"
WHITE_SPREAD = 22            # max channel spread: white, not a saturated pastel
# Real cards measured 0.125-0.154 (android-one) and 0.112-0.135 (moto) of the screen
# height. Next season's cup is drawn peeking in below the "SEE WHAT'S COMING
# NEXT" line, clipped to 0.037, and the GO BATTLE LEAGUE screen's own card has
# blank strips of 0.038 in this band. The floor sits between the two groups.
CARD_MIN_HEIGHT = 0.080
# The GO BATTLE LEAGUE screen also shows two tall white strips in this band, so
# a count alone cannot tell it from the league list -- but it has a pill on it
# and the league list has none, which is what actually separates them. See
# read_screen_state.
CARD_MIN_COUNT = 2

# A fresh season/account can open on the reward-tier chooser with its free
# BATTLE pill below the fold. In that position the only feature visible to the
# detector is one unusually tall white reward panel beginning near mid-screen.
# League lists have at least CARD_MIN_COUNT separate cards. A bounded upward
# swipe exposes the free button without tapping the premium tier.
REWARD_PANEL_MIN_HEIGHT = 0.14
REWARD_PANEL_TOP = (0.40, 0.62)
REWARD_SCROLL_LIMIT = 3

# This script lives in platform-tools, so prefer the adb binary next to it.
_ADB = Path(__file__).resolve().parent.parent / 'adb'
ADB_BINARY = str(_ADB) if _ADB.exists() else 'adb'


class DeviceAsyncWrapper(DeviceAsync):
    config: CONFIG
    display_id: str | None = None   # set by setup(); only foldables need it
    label: str = ''                 # set by setup(); prefixes this device's output
    input_cost: float = INPUT_COST_DEFAULT  # seconds per `input`; set by setup()


class AutoGBLError(Exception):
    pass


def frame_signature(frame) -> tuple:
    """A coarse fingerprint of the screen, for telling a battle from a menu.

    Sampled sparsely on purpose: this only has to answer "did anything move",
    and a battle is never still for long -- the Pokemon breathe, the timer
    counts, the energy bars fill.
    """
    width, height, offset, data = frame
    return tuple(
        pixel(width, offset, data,
              int(width * (0.1 + 0.8 * x / 7)), int(height * (0.1 + 0.8 * y / 7)))
        for y in range(8) for x in range(8)
    )


def loading_screen(frame) -> bool:
    """Recognize Pokémon GO's nearly-black transition/spinner frame."""
    width, height, offset, data = frame
    dark = 0
    total = 0
    for row in range(1, 9):
        for column in range(1, 7):
            red, green, blue = pixel(
                width,
                offset,
                data,
                int(width * column / 7),
                int(height * row / 10),
            )
            total += 1
            dark += max(red, green, blue) < 35
    return dark / total >= 0.88


TRACE_DIR: Path | None = None
_TRACE_SEQ: dict[str, int] = {}


def save_trace(device: DeviceAsyncWrapper, frame, state: str, point) -> None:
    """Writes the frame a decision was made on, named for that decision.

    Read-only diagnosis aid: the log says which state was picked, this says what
    the phone was actually showing when it was picked.
    """
    from PIL import Image

    width, height, offset, data = frame
    index = _TRACE_SEQ.get(device.serial, 0)
    _TRACE_SEQ[device.serial] = index + 1
    pixels = bytes(data[offset:offset + width * height * 4])
    where = 'none' if point is None else f'{point[0]}x{point[1]}'
    Image.frombytes('RGBA', (width, height), pixels).save(
        TRACE_DIR / f'{index:03d}-{state}-{where}.png')


def log(device: DeviceAsyncWrapper, *parts):
    """Prints one line tagged with the device it came from.

    Phones battle concurrently, so untagged output interleaves into something
    unreadable as soon as more than one is plugged in. Flushed, so a run whose
    output is redirected to a file can still be watched as it goes.
    """
    print(f'[{device.label}]', *parts, flush=True)


class MonkeyGone(Exception):
    """The monkey channel stopped answering; the caller falls back to `input`."""


class MonkeyChannel:
    """A held-open command socket into `monkey --port` on one handset.

    Two adb streams. One carries the `shell:monkey` service and is never read
    from: it exists so that the monkey process lives exactly as long as this
    object does. The other is the command channel, reached through adb's `tcp:`
    service, which connects straight to a device-side port without a host-side
    `adb forward` and so cannot collide with another leg.
    """

    def __init__(self, process, connection):
        self.process = process
        self.connection = connection

    async def run(self, lines: list[str]) -> None:
        """Send commands in order and wait for monkey to acknowledge each.

        A `sleep <seconds>` line is honoured here rather than sent on: monkey's
        own sleep would hold the socket, and the pauses in a burst are there to
        space touches out on the glass, which the host clock times just as well.
        Everything between two sleeps is written in one go and its replies read
        afterwards, so a batch of taps costs one round trip rather than one per
        tap.
        """
        batch: list[str] = []
        for line in lines:
            if line.startswith('sleep '):
                await self._flush(batch)
                await asyncio.sleep(float(line.split()[1]))
                continue
            batch.append(line)
        await self._flush(batch)

    async def _flush(self, batch: list[str]) -> None:
        """Write one run of commands and read back one reply for each."""
        if not batch:
            return
        payload = ('\n'.join(batch) + '\n').encode()
        del batch[:]
        try:
            self.connection.writer.write(payload)
            await self.connection.writer.drain()
            for _ in payload.splitlines():
                reply = await asyncio.wait_for(
                    self.connection.reader.readline(), MONKEY_REPLY_TIMEOUT)
                if not reply.startswith(b'OK'):
                    raise MonkeyGone(f'monkey answered {reply!r}')
        except (OSError, asyncio.TimeoutError, AttributeError) as exc:
            raise MonkeyGone(repr(exc)) from exc

    async def close(self) -> None:
        """Drop both streams, which is what stops the monkey process."""
        for stream in (self.connection, self.process):
            try:
                await stream.close()
            except (OSError, RuntimeError):
                pass


def monkey_channel(device: DeviceAsyncWrapper) -> MonkeyChannel | None:
    """This phone's open monkey channel, or None if it is touched by `input`.

    Read defensively for the reason `input_cost` is: devices arrive from ppadb
    as plain `DeviceAsync`, so only whichever setup path ran can have set it.
    """
    return getattr(device, 'monkey', None)


async def open_monkey(device: DeviceAsyncWrapper) -> MonkeyChannel | None:
    """Start `monkey --port` on the phone and connect to it, or None.

    `listvar` is the probe: it answers OK without injecting anything, unlike
    every key or touch command. Monkey needs a second or two to be listening
    and refuses connections until it is, so a run of failed connects is normal
    and only a wrong answer is fatal -- at which point there is nothing to
    salvage, because closing the socket of a live monkey kills it.
    """
    try:
        # A monkey whose client went away dies, but not instantly and not
        # quietly: it holds the port while it unwinds, and a second one started
        # over the top of it cannot bind, so the connect lands on a listener
        # that is on its way out. The leg holds this phone's lock, so any
        # monkey still on it is a leftover of an earlier run.
        await device.shell(f'pkill -f {MONKEY_PROCESS}')
        process = await device.create_connection()
        await process.send(f'shell:monkey --port {MONKEY_DEVICE_PORT}')
    except (AttributeError, RuntimeError, OSError):
        # AttributeError: a device that never came from ppadb has no streams to
        # open, and a phone touched by `input` is slow, not broken.
        return None
    for _ in range(MONKEY_START_TRIES):
        await asyncio.sleep(MONKEY_START_GAP)
        connection = None
        try:
            connection = await device.create_connection()
            await connection.send(f'tcp:{MONKEY_DEVICE_PORT}')
            connection.writer.write(b'listvar\n')
            await connection.writer.drain()
            reply = await asyncio.wait_for(
                connection.reader.readline(), MONKEY_REPLY_TIMEOUT)
        except (RuntimeError, OSError, asyncio.TimeoutError):
            if connection is not None:
                await _close_quietly(connection)
            continue
        if reply.startswith(b'OK'):
            return MonkeyChannel(process, connection)
        await _close_quietly(connection)
        break
    await _close_quietly(process)
    return None


async def _close_quietly(stream) -> None:
    try:
        await stream.close()
    except (OSError, RuntimeError):
        pass


async def close_monkey(device: DeviceAsyncWrapper) -> None:
    """Shut this phone's channel down, if it has one."""
    channel = monkey_channel(device)
    if channel is None:
        return
    device.monkey = None
    await channel.close()


def monkey_drag(x1: int, y1: int, x2: int, y2: int, duration_ms: float) -> list[str]:
    """The command run that draws one swipe, paced to last about as long.

    Monkey has no swipe, so the path is walked explicitly. Each move is a real
    MOVE event, which is finer sampling than `input swipe` gives, and the sleep
    between them is short by the wire cost of sending one so that the whole
    gesture still takes roughly the duration asked for.
    """
    steps = max(1, min(MONKEY_DRAG_STEPS_MAX, int(duration_ms / MONKEY_DRAG_STEP_MS)))
    pause = max(0.0, (duration_ms / steps - MONKEY_WIRE_MS) / 1000)
    lines = [f'touch down {x1} {y1}']
    for step in range(1, steps + 1):
        lines.append(f'sleep {pause:.3f}')
        lines.append(f'touch move {x1 + (x2 - x1) * step // steps} '
                     f'{y1 + (y2 - y1) * step // steps}')
    lines.append(f'touch up {x2} {y2}')
    return lines


def monkey_commands(command: str) -> list[str] | None:
    """Translate a shell line made of `input` calls into monkey commands.

    None when any part of it has no monkey equivalent -- `input text`, which
    the picker types search terms with, is the one that matters -- so the whole
    line goes back to the shell rather than splitting one gesture across two
    transports.

    `&` separates just as `;` does. A backgrounded burst exists to overlap
    expensive JVM starts, and over a socket there are none to overlap, so what
    is left of it is the staggers -- which is what the burst wanted all along:
    taps spaced out on the glass.
    """
    lines: list[str] = []
    for part in re.split(r'[;&]', command):
        words = shlex.split(part)
        if not words or words == ['wait']:
            continue
        try:
            if words[0] == 'sleep' and len(words) == 2:
                lines.append(f'sleep {float(words[1]):.3f}')
                continue
            if words[0] != 'input':
                return None
            verb, arguments = words[1], words[2:]
            if verb == 'tap' and len(arguments) == 2:
                lines.append(f'tap {int(arguments[0])} {int(arguments[1])}')
            elif verb == 'swipe' and len(arguments) in (4, 5):
                duration = (float(arguments[4]) if len(arguments) == 5
                            else MONKEY_DEFAULT_SWIPE_MS)
                lines.extend(monkey_drag(
                    *(int(argument) for argument in arguments[:4]), duration))
            elif verb == 'keyevent' and len(arguments) == 1:
                lines.append(f'press {arguments[0]}')
            else:
                return None
        except (IndexError, ValueError):
            return None
    return lines


async def send_input(device: DeviceAsyncWrapper, command: str) -> None:
    """Run a shell line made of `input` calls, over monkey where there is one.

    Every gesture in the loop goes through here so that one phone's cheaper
    transport does not need a second copy of the gesture code, and so that a
    channel which stops answering costs one slow gesture rather than the run:
    it is dropped and the same line is sent to the shell instead.
    """
    channel = monkey_channel(device)
    if channel is not None:
        lines = monkey_commands(command)
        if lines is not None:
            try:
                await channel.run(lines)
                return
            except MonkeyGone as exc:
                device.monkey = None
                log(device, f'  Monkey channel lost ({exc}); back to `input`')
                await channel.close()
    await device.shell(command)


async def measure_input_cost(device: DeviceAsyncWrapper) -> float:
    """Time one `input` invocation on this handset, in seconds.

    Every `input` starts its own JVM, so the cost is a property of the phone and
    its current load rather than of the gesture. A phone with a monkey channel
    is measured through it instead, because that is what its taps will cost. The batch sizes that keep the
    loop watching the screen depend on it, so it is measured rather than
    assumed. KEYCODE_UNKNOWN does nothing on the way through.
    """
    channel = monkey_channel(device)
    start = asyncio.get_event_loop().time()
    try:
        if channel is not None:
            # `press` will not take KEYCODE_UNKNOWN by name, and the number is
            # the same do-nothing key `input keyevent 0` presses.
            await channel.run(['press 0'] * INPUT_COST_SAMPLES)
        else:
            await device.shell('; '.join(['input keyevent 0'] * INPUT_COST_SAMPLES))
    except (RuntimeError, OSError, MonkeyGone):
        return INPUT_COST_DEFAULT
    cost = (asyncio.get_event_loop().time() - start) / INPUT_COST_SAMPLES
    if not 0 < cost <= INPUT_COST_CEILING:
        return INPUT_COST_DEFAULT
    return cost


def batch_size(cost: float, budget: float, most: int, least: int = 1) -> int:
    """How many chained `input` calls fit in budget seconds on this phone."""
    return max(least, min(most, int(budget / cost) if cost > 0 else most))


def input_cost(device: DeviceAsyncWrapper) -> float:
    """This handset's measured cost per `input`, or the safe default.

    Devices arrive from ppadb as plain `DeviceAsync`, so the class default on
    the wrapper never applies to a real phone; only whichever setup path ran
    can have set the attribute. Reading it defensively keeps an unprepared
    device slow-but-alive instead of raising in the middle of a battle.
    """
    return getattr(device, 'input_cost', INPUT_COST_DEFAULT)


def parallel_taps(device: DeviceAsyncWrapper) -> bool:
    """Whether a tap burst is backgrounded rather than chained on this phone."""
    return input_cost(device) >= PARALLEL_INPUT_MIN


def gesture_interval(device: DeviceAsyncWrapper, swipe_ms: float) -> float:
    """Seconds a minigame sweep spends per swipe on this handset.

    Chained, a gesture costs a JVM start and then its own duration. Backgrounded
    the starts overlap, so what is left is the phone's sustained launch rate --
    measured, not the stagger asked for.
    """
    if parallel_taps(device):
        return max(PARALLEL_SWIPE_INTERVAL, swipe_ms / 1000)
    return input_cost(device) + swipe_ms / 1000


def swipe_burst(device: DeviceAsyncWrapper, commands: list[str]) -> str:
    """The shell command that runs every gesture, in order and without overlap.

    Backgrounded on a slow phone for the reason `tap_burst` is. The stagger is
    shorter than a gesture, but the launches are not what lands: each `input`
    spends its start faulting pages back off flash and only then injects, so the
    injections come out at the phone's sustained rate -- 0.21s measured, just
    over the 200ms drag -- and the drags run nose to tail rather than at once.
    Two overlapping drags would be read as a pinch.
    """
    if parallel_taps(device):
        return f' & sleep {PARALLEL_SWIPE_STAGGER:g}; '.join(commands) + ' & wait'
    return '; '.join(commands)


def tap_interval(device: DeviceAsyncWrapper) -> float:
    """Seconds a burst spends per tap on this handset.

    A chained burst pays a JVM start for every tap, so the interval is the
    measured cost; over a monkey channel it is the stagger the burst asks for,
    since the transport itself costs nothing worth counting. A backgrounded burst pays them concurrently, so the interval
    is the stagger between launches and the cost is a tail on the batch. Batch
    sizes are set from this rather than from the cost, so a slow phone gets more
    taps into the same window of blindness instead of a longer window.
    """
    if monkey_channel(device) is not None:
        return MONKEY_TAP_GAP
    if parallel_taps(device):
        return PARALLEL_TAP_STAGGER
    return input_cost(device) + MOVE_TAP_GAP


async def prepare_device_timing(device: DeviceAsyncWrapper) -> None:
    """Measure phone's `input` cost report batch sizes it buys.
    
    There are two device setup paths -- `setup()` `sources.gbl_android`
    run directly, and `pokemon_fleet.connected_android_gbl_devices()` for the
    `gbl.py` fleet entrypoint -- so this lives in one function call.
    """
    device.input_cost = await measure_input_cost(device)
    if device.input_cost >= PARALLEL_INPUT_MIN:
        # A phone this slow is slow at starting JVMs, not at touching. Give it
        # a socket and it joins the fast phones: everything below is keyed on
        # the cost, so the batch sizes follow on their own.
        channel = await open_monkey(device)
        if channel is None:
            log(device, '  No monkey channel here; taps stay on `input`')
        else:
            device.monkey = channel
            through = await measure_input_cost(device)
            log(device, f'  `input` costs {device.input_cost * 1000:.0f}ms here; '
                        f'monkey answers in {through * 1000:.0f}ms, tapping '
                        f'through that instead')
            device.input_cost = through

    move_batch_budget = MOVE_BATCH_BUDGET
    move_taps_per_batch = MOVE_TAPS_PER_BATCH
    move_batches_per_read = MOVE_BATCHES_PER_READ
    minigame_budget = MINIGAME_BUDGET
    minigame_gestures_per_sweep = MINIGAME_GESTURES_PER_SWEEP
    minigame_swipes = MINIGAME_SWEEPS
    minigame_sweep_ms = MINIGAME_SWIPE_MS

    if input_cost(device) <= FAST_INPUT_MAX:
        move_batch_budget = FAST_MOVE_BATCH_BUDGET
        move_taps_per_batch = FAST_MOVE_TAPS_PER_BATCH
        move_batches_per_read = FAST_MOVE_BATCHES_PER_READ
        minigame_budget = FAST_MINIGAME_BUDGET
        minigame_gestures_per_sweep = FAST_MINIGAME_GESTURES_PER_SWEEP
        minigame_swipes = FAST_MINIGAME_SWEEPS
        minigame_sweep_ms = FAST_MINIGAME_SWIPE_MS
    elif parallel_taps(device):
        # A phone slow enough to background its starts: the batch is sized to
        # the read it overlaps rather than to a tap count, and the minigame
        # drags rather than flicks.
        move_batch_budget = PARALLEL_MOVE_BATCH_BUDGET
        minigame_sweep_ms = SLOW_MINIGAME_SWIPE_MS

    device.fast_attack_batch_budget = move_batch_budget
    device.fast_attack_taps_per_batch = move_taps_per_batch
    device.fast_attack_batches_per_read = move_batches_per_read
    device.minigame_batch_budget = minigame_budget
    device.minigame_gestures_per_sweep = minigame_gestures_per_sweep
    device.minigame_swipes = minigame_swipes
    device.minigame_sweep_ms = minigame_sweep_ms

    taps = batch_size(
        tap_interval(device),
        device.fast_attack_batch_budget,
        device.fast_attack_taps_per_batch,
        MOVE_TAPS_MINIMUM,
    )
    sweeps = batch_size(
        gesture_interval(device, device.minigame_sweep_ms),
        device.minigame_batch_budget / device.minigame_gestures_per_sweep,
        device.minigame_swipes,
    )
    how = 'backgrounded' if parallel_taps(device) else 'chained'
    log(device, f'  input costs {input_cost(device) * 1000:.0f}ms here: '
                f'{taps} {how} tap(s) per batch, {sweeps} minigame sweep(s)')
async def tap(device: DeviceAsyncWrapper, point: list[int]):
    """Sends a tap at point to device."""
    x, y = point
    # A tiny swipe over 100 ms for reliability, as in trade.py.
    await send_input(device, f'input swipe {x} {y} {x+1} {y+1} 100')


async def wait(seconds: float, use_modifier: bool = True):
    await asyncio.sleep(max(seconds + (SLEEP_MODIFIER if use_modifier else 0), 0))


async def find_display_id(device: DeviceAsyncWrapper) -> str | None:
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


async def screencap_raw(device: DeviceAsyncWrapper):
    """Grabs the raw framebuffer via `adb exec-out screencap`.

    Returns (width, height, offset, data), or None if the screen can't be read.
    Raw rather than PNG because nothing here needs Pillow, which the Android
    side of requirements.txt does not carry.
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
        log(device, '   Screen read timed out after', SCREENCAP_TIMEOUT, 's')
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


async def screencap_band(device: DeviceAsyncWrapper, frame, y0: int, y1: int):
    """Grabs only rows y0..y1 of the framebuffer, as a frame the tests can read.

    The bill for a read is the USB transfer of a 13-15MB frame, not the capture,
    so a question about one band of the screen is answered several times more
    cheaply by only pulling that band: 2.3MB and ~0.6s on the android-one against
    13.5MB and ~1.3s. `head`/`tail` do the slicing on the phone.

    The frame returned carries the *full* height and a negative offset, so every
    fraction in this module still means what it meant -- `pixel` indexes into
    the band as if the rows above it were still there. Only rows inside the band
    may be sampled; anything else reads another row's bytes.
    """
    width, height, header, _ = frame
    y0 = max(0, min(height, y0))
    y1 = max(y0, min(height, y1))
    start = header + y0 * width * 4
    count = (y1 - y0) * width * 4
    if count <= 0:
        return None
    display = f'-d {device.display_id} ' if device.display_id else ''
    piped = f'screencap {display}| tail -c +{start + 1} | head -c {count}'
    args = [ADB_BINARY, '-s', device.serial, 'exec-out', piped]
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
        proc.kill()
        await proc.wait()
        return None
    except OSError:
        return None
    if len(data) < count:
        return None
    return width, height, -y0 * width * 4, data


async def read_screen(device: DeviceAsyncWrapper):
    """One screencap, or None. Retried once: a wedged read is not a verdict."""
    for _ in range(2):
        frame = await screencap_raw(device)
        if frame is not None:
            return frame
    return None


async def drop_prefetched(read: asyncio.Task) -> None:
    """Throw away a prefetched frame that the battle has already overtaken.

    Awaited rather than cancelled. It is a live `adb exec-out` child, and
    everything that discards one -- a charged minigame, a recovery walk -- has
    taken seconds longer than the read, so by here it has long since finished
    and this costs nothing. Cancelling would leave the child writing into a
    closed pipe instead.
    """
    try:
        await read
    except Exception:
        pass


def pixel(width: int, offset: int, data: bytes, x: int, y: int) -> tuple[int, int, int]:
    i = offset + (y * width + x) * 4
    return data[i], data[i + 1], data[i + 2]


def is_white(rgb: tuple[int, int, int]) -> bool:
    """Near-white: bright on every channel and close to neutral.

    The spread test keeps the game's mint pills and pink banners out of the
    count; without it a pill and a card score alike.
    """
    return min(rgb) >= WHITE_LEVEL and max(rgb) - min(rgb) <= WHITE_SPREAD


def is_pill(rgb: tuple[int, int, int]) -> bool:
    """Anywhere along the mint-to-teal gradient of an action pill.

    Deliberately loose on red, which the gradient runs all the way down. See
    PILL_GREEN for the sampled values this is drawn around.
    """
    red, green, blue = rgb
    return (
        green >= PILL_GREEN
        and blue >= PILL_BLUE
        and green - red >= PILL_GREEN_OVER_RED
        and green >= blue
    )


def is_orange(rgb: tuple[int, int, int]) -> bool:
    """The orange of the end-of-set buttons. See ORANGE_RED."""
    red, green, blue = rgb
    return (
        red >= ORANGE_RED
        and ORANGE_GREEN[0] <= green <= ORANGE_GREEN[1]
        and blue <= ORANGE_BLUE
        and red - green >= ORANGE_MIN_MARGIN
        and green - blue >= ORANGE_MIN_MARGIN
    )


def is_basic_banner(rgb: tuple[int, int, int]) -> bool:
    """Pink Basic/Premium chooser ribbon colour."""
    red, green, blue = rgb
    return (
        red >= BASIC_BANNER_RED
        and BASIC_BANNER_GREEN[0] <= green <= BASIC_BANNER_GREEN[1]
        and BASIC_BANNER_BLUE[0] <= blue <= BASIC_BANNER_BLUE[1]
        and red - green >= BASIC_BANNER_RED_OVER_GREEN
        and blue - green >= BASIC_BANNER_BLUE_OVER_GREEN
    )


def bands(frame, scan_y, row_test, min_height: float, max_gap: int = 4):
    """Runs of consecutive rows passing `row_test`, as (top, bottom) rows.

    The shared shape of both screen tests: a button and a card are each a run of
    rows that look alike right across their width. Only the per-row test
    differs, because a card is a solid block and a pill is a wide span with its
    label punched through it. Small gaps (up to max_gap rows) caused by text or
    anti-aliasing are bridged so high-DPI screens do not drop the button.
    """
    height = frame[1]
    y0, y1 = scan_y
    found: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    last = int(height * y1) - 1
    for y in range(int(height * y0), last + 1):
        if row_test(frame, y):
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                end = y - gap
                if end - start >= height * min_height:
                    found.append((start, end))
                start = None
                gap = 0
    if start is not None:
        end = (last + 1) - gap
        if end - start >= height * min_height:
            found.append((start, end))
    return found



def button_row(frame, y: int, test, min_width: float) -> bool:
    """Whether row y is a wide, mostly-unbroken span of one button colour.

    Measured between the first and last matching sample rather than over a fixed
    window, so it holds for buttons of different widths. Inside that span the
    button's own white label counts as part of it, so only a real gap -- neither
    button nor label -- breaks the row.

    Two thresholds, because the label alone is not enough to tell a button from
    a line of text in the same colour: the span must be nearly all button-or-
    label, *and* enough of it must be the colour itself. Text passes the first
    (its letters are far apart, with white card between) and fails the second.
    """
    width, _height, offset, data = frame
    x0, x1 = PILL_SCAN_X
    hit: list[bool] = []
    label: list[bool] = []
    for xi in range(PILL_SCAN_STEPS):
        x = int(width * (x0 + (x1 - x0) * xi / (PILL_SCAN_STEPS - 1)))
        rgb = pixel(width, offset, data, x, y)
        hit.append(test(rgb))
        label.append(is_white(rgb))
    if not any(hit):
        return False
    first = hit.index(True)
    last = len(hit) - 1 - hit[::-1].index(True)
    step = (x1 - x0) / (PILL_SCAN_STEPS - 1)
    if (last - first) * step < min_width:
        return False
    span = last - first + 1
    # White counts only inside the span, where it is the label. Outside it is
    # the white card the button sits on, which would otherwise let a row of card
    # with a stray coloured pixel at each end pass as a button.
    covered = sum(1 for i in range(first, last + 1) if hit[i] or label[i])
    density = sum(1 for i in range(first, last + 1) if hit[i]) / span
    return covered / span >= PILL_ROW_FILL and density >= BUTTON_MIN_DENSITY


def pill_row(frame, y: int) -> bool:
    """Whether row y is a green action pill."""
    if not button_row(frame, y, is_pill, PILL_MIN_WIDTH):
        return False
    width, _height, offset, data = frame
    x0, x1 = PILL_SCAN_X
    colours = []
    for xi in range(PILL_SCAN_STEPS):
        x = int(width * (x0 + (x1 - x0) * xi / (PILL_SCAN_STEPS - 1)))
        rgb = pixel(width, offset, data, x, y)
        if is_pill(rgb):
            colours.append(rgb)
    # Real buttons run mint -> teal, with red falling sharply. The map's wide
    # weather/nearby gradients meet the loose colour test but stay flat or get
    # redder from left to right, which was enough to drive a phone off course.
    return bool(colours) and colours[0][0] - colours[-1][0] >= PILL_RED_DROP


def orange_row(frame, y: int) -> bool:
    """Whether row y is one of the end-of-set orange buttons."""
    return button_row(frame, y, is_orange, ORANGE_MIN_WIDTH)


def basic_banner_row(frame, y: int) -> bool:
    """Whether row y crosses a pink reward-tier chooser ribbon."""
    return button_row(frame, y, is_basic_banner, BASIC_BANNER_MIN_WIDTH)


def white_row(frame, y: int) -> bool:
    """Whether row y is near-white right across the card band."""
    width, _height, offset, data = frame
    x0, x1 = CARD_SCAN_X
    hits = 0
    for xi in range(CARD_SCAN_STEPS):
        x = int(width * (x0 + (x1 - x0) * xi / (CARD_SCAN_STEPS - 1)))
        if is_white(pixel(width, offset, data, x, y)):
            hits += 1
    return hits / CARD_SCAN_STEPS >= CARD_ROW_WHITE


def find_pill(frame, scan_y=PILL_SCAN_Y) -> list[int] | None:
    """Centre of the action pill, or None if no pill is on screen.

    The highest pill when more than one is drawn. The reward-tier chooser can
    show both the free/basic and premium BATTLE buttons at once; the free one
    is first, and selecting the lower one would spend a Premium Battle Pass.
    Other screens in the loop have a single action pill.
    """
    width = frame[0]
    found = bands(frame, scan_y, pill_row, PILL_MIN_HEIGHT)
    if not found:
        return None
    top, bottom = found[0]
    return [int(width * PILL_TAP_X), (top + bottom) // 2]


def find_orange(frame) -> list[int] | None:
    """Centre of an end-of-set orange button, or None.

    The lowest again: COLLECT and CLAIM RANK REWARDS! come up one at a time, and
    the lower of anything orange is the one in the button slot.
    """
    found = bands(frame, PILL_SCAN_Y, orange_row, PILL_MIN_HEIGHT)
    if not found:
        return None
    top, bottom = found[-1]
    y = (top + bottom) // 2
    width, _height, offset, data = frame
    x0, x1 = PILL_SCAN_X
    xs = []
    for xi in range(PILL_SCAN_STEPS):
        x = int(width * (x0 + (x1 - x0) * xi / (PILL_SCAN_STEPS - 1)))
        if is_orange(pixel(width, offset, data, x, y)):
            xs.append(x)
    if not xs:
        return None
    span = xs[-1] - xs[0]
    # Narrow COLLECT tiles need their own horizontal centre. Wide claim buttons
    # are tapped left of centre so the overlaid teal X cannot steal the tap.
    x = ((xs[0] + xs[-1]) // 2 if span < width * 0.40
         else int(xs[0] + span * 0.35))
    return [x, y]


def banner_bottom_row(frame) -> int | None:
    """The last row of the pink reward ribbon, or None.

    `bands` cannot supply this: the white "BASIC REWARDS" label punches the
    middle rows below PILL_ROW_FILL, so the ribbon reads as two thin runs (the
    SE's measured 998-1002 and 1030-1034) rather than one. `basic_banner_visible`
    already counts rows instead of requiring a run, and this takes the lowest.
    """
    height = frame[1]
    start = int(height * BASIC_BANNER_SCAN_Y[0])
    end = int(height * BASIC_BANNER_SCAN_Y[1])
    rows = [y for y in range(start, end) if basic_banner_row(frame, y)]
    return max(rows) if rows else None


def reward_tile_columns(frame, top: int, bottom: int) -> list[tuple[int, int, int, int]]:
    """Orange column-runs between rows top and bottom, as (x0, y0, x1, y1)."""
    width, _height, offset, data = frame
    columns: dict[int, list[int]] = {}
    for y in range(top, bottom):
        for x in range(0, width, REWARD_TILE_SCAN_STEP):
            if is_orange(pixel(width, offset, data, x, y)):
                columns.setdefault(x, []).append(y)
    if not columns:
        return []
    gap = int(width * REWARD_TILE_GAP)
    groups: list[list[int]] = [[]]
    for x in sorted(columns):
        if groups[-1] and x - groups[-1][-1] > gap:
            groups.append([])
        groups[-1].append(x)
    found = []
    for group in groups:
        ys = [y for x in group for y in columns[x]]
        found.append((group[0], min(ys), group[-1], max(ys)))
    return found


def find_reward_tile(frame) -> list[int] | None:
    """Where to tap the unclaimed reward tile under the pink banner, or None.

    The leftmost tile-sized block is the one to take: the row fills left to
    right, so anything left of it is already claimed and shows a grey tick.
    """
    banner = banner_bottom_row(frame)
    if banner is None:
        return None
    width, height, _offset, _data = frame
    top = banner + int(height * 0.004)
    bottom = min(height, banner + int(height * REWARD_TILE_BAND))
    if top >= bottom:
        return None
    low, high = REWARD_TILE_WIDTH
    for x0, y0, x1, y1 in reward_tile_columns(frame, top, bottom):
        if not low <= (x1 - x0) / width <= high:
            continue
        if (y1 - y0) / height < REWARD_TILE_MIN_HEIGHT:
            continue
        return [(x0 + x1) // 2, y0 + int((y1 - y0) * REWARD_TILE_TAP_Y)]
    return None


# How close to the left edge an orange run has to start to count as a tile the
# screen has cut in half rather than a tile of its own.
CLIPPED_TILE_EDGE = 0.02


def clipped_reward_tile(frame) -> bool:
    """Whether the reward row is holding a tile half off the left edge.

    This is the difference between a row that has scrolled past the tile a set
    earned and a row whose rewards are simply all taken. Both show nothing but
    locked tiles to the tile scan, and only one of them is worth dragging: the
    SE spent three scrolls and then ended its leg with 0 battles on a card that
    had already paid out, because "no earned tile in view" was read as "the
    earned tile must be off-screen".
    """
    banner = banner_bottom_row(frame)
    if banner is None:
        return False
    width, height, _offset, _data = frame
    top = banner + int(height * 0.004)
    bottom = min(height, banner + int(height * REWARD_TILE_BAND))
    if top >= bottom:
        return False
    low = REWARD_TILE_WIDTH[0]
    for x0, y0, x1, y1 in reward_tile_columns(frame, top, bottom):
        if x0 > width * CLIPPED_TILE_EDGE:
            continue
        if (x1 - x0) / width >= low:
            continue
        if (y1 - y0) / height < REWARD_TILE_MIN_HEIGHT:
            continue
        return True
    return False


def league_cards(frame) -> list[tuple[int, int]]:
    """Every league card in the frame, as (top, bottom) pixel rows."""
    return bands(frame, CARD_SCAN_Y, white_row, CARD_MIN_HEIGHT)


def party_screen(frame, point, pill_y=PARTY_PILL_Y, top_max=PARTY_TOP_MAX) -> bool:
    """Whether this is CHOOSE YOUR PARTY, with USE THIS PARTY as its pill.

    Arming used to be inferred from history -- the party screen counted as
    ready only because the league list or a result screen came before it. A
    phone already sitting on this screen therefore tapped its pill as an
    ordinary action button, reached the battlefield unarmed, and then refused
    to touch it, so the whole set stopped at UNKNOWN_LIMIT having played
    nothing. Recognising the screen itself is what makes that recoverable.

    The screen next to this one is the party editor's picker, which must never
    arm: fast taps there select Pokemon. Its DONE pill sits only 0.023h below
    this one, so height alone is not enough and all three tests have to pass.
    Across 23 traced menu frames the party screen puts its pill at 0.8434h
    every single time, against 0.8668h for the picker and 0.8578h for the
    welcome card, and it is the only one of the three that is both backed by an
    unbroken white panel and still showing the trainer photo up top -- the
    picker's list is bright there (150 against 72-79).

    Both of those numbers are per-handset, so they are arguments: the iPhone
    puts this pill at 0.8936h and reads 111-130 up top, where Android reads
    72-79, and a shared threshold would reject one phone or the other.
    """
    if point is None:
        return False
    width, height, offset, data = frame
    if abs(point[1] / height - pill_y) > PARTY_PILL_TOLERANCE:
        return False
    top = [sum(pixel(width, offset, data, int(width * xf), int(height * PARTY_TOP_Y))) / 3
           for xf in PARTY_TOP_X]
    if sum(top) / len(top) >= top_max:
        return False
    panel = 0
    for step in range(PARTY_PANEL_STEPS):
        xf = PARTY_PANEL_X[0] + (PARTY_PANEL_X[1] - PARTY_PANEL_X[0]) \
            * step / (PARTY_PANEL_STEPS - 1)
        red, green, blue = pixel(
            width, offset, data, int(width * xf), int(height * PARTY_PANEL_Y))
        if min(red, green, blue) > PARTY_PANEL_WHITE:
            panel += 1
    return panel / PARTY_PANEL_STEPS >= PARTY_PANEL_FRACTION


def bottom_league_point(frame) -> list[int] | None:
    """Centre of the bottom-most league card, or None if this is not the list."""
    width = frame[0]
    cards = league_cards(frame)
    if len(cards) < CARD_MIN_COUNT:
        return None
    top, bottom = cards[-1]
    return [width // 2, (top + bottom) // 2]


def reward_scroll_needed(frame) -> bool:
    """Whether the free reward-tier BATTLE button is probably below the fold."""
    height = frame[1]
    cards = league_cards(frame)
    if len(cards) != 1:
        return False
    top, bottom = cards[0]
    top_fraction = top / height
    return (
        REWARD_PANEL_TOP[0] <= top_fraction <= REWARD_PANEL_TOP[1]
        and bottom - top >= height * REWARD_PANEL_MIN_HEIGHT
    )


def reward_scroll_back_needed(frame) -> bool:
    """Whether an end-of-set page is scrolled below CLAIM RANK REWARDS."""
    height = frame[1]
    cards = league_cards(frame)
    if len(cards) < 2:
        return False
    # The season-summary and Nearby Battle panels are far taller than league
    # choices. On the android-one they measured 0.274h and 0.372h after battle 5.
    return max((bottom - top) / height for top, bottom in cards) >= 0.25


def basic_banner_visible(frame) -> bool:
    """Whether a pink Basic/Premium reward-tier ribbon is visible."""
    height = frame[1]
    start = int(height * BASIC_BANNER_SCAN_Y[0])
    end = int(height * BASIC_BANNER_SCAN_Y[1])
    # The white "Basic Rewards" label interrupts otherwise matching rows, so
    # count wide pink rows across the scan rather than requiring one unbroken
    # vertical run.
    hits = sum(1 for y in range(start, end) if basic_banner_row(frame, y))
    return hits >= height * BASIC_BANNER_MIN_HEIGHT


def reward_chooser_visible(frame) -> bool:
    """Whether white reward-tier panels justify scanning high for BATTLE."""
    if basic_banner_visible(frame):
        return True
    # After a rank-up android-one can show both Basic and Premium BATTLE pills while
    # clipping the Basic ribbon and enough panel edges to defeat the older
    # chooser tests. Two full-width action pills are definitive: Basic is the
    # upper one, so force the whole-screen scan.
    if len(bands(
            frame, REWARD_PILL_SCAN_Y, pill_row, PILL_MIN_HEIGHT)) >= 2:
        return True
    height = frame[1]
    cards = league_cards(frame)
    if not cards:
        return False
    heights = [(bottom - top) / height for top, bottom in cards]
    # Before scrolling there is one 0.14h+ panel. Afterwards two may be
    # visible, but one is much taller than a real 0.11-0.15h league card.
    return (
        (len(cards) == 1 and heights[0] >= REWARD_PANEL_MIN_HEIGHT)
        or max(heights) >= 0.20
    )


def read_screen_state(frame) -> tuple[str, list[int] | None]:
    """What is on screen, and where to tap for it.

    Returns one of 'orange', 'pill', 'league', 'scroll', 'scroll_back' or
    'battle', with the point to tap. 'scroll' means the free reward-tier button
    is below the fold; 'scroll_back' means the completed-set reward is above
    the current scroll position. 'battle' uses the configured battlefield.

    Order carries the distinctions. Orange comes first because the screen at the
    end of a set shows both an orange button and the BATTLE pill greyed out, and
    the pill is the one that does nothing. The pill comes before the cards
    because the GO BATTLE LEAGUE screen shows a pill *and* card-like white
    strips, while the league list shows cards and no pill.
    """
    orange = find_orange(frame)
    if orange is not None:
        return 'orange', orange
    # A reward *tile* is not a solid orange row, so `find_orange` cannot see one
    # whose middle is artwork rather than colour. It ranks with orange and above
    # the pill for the same reason: while it is unclaimed the pill is dead.
    tile = find_reward_tile(frame)
    if tile is not None:
        return 'orange', tile
    scroll_back = reward_scroll_back_needed(frame)
    if reward_chooser_visible(frame):
        # Scan the whole chooser and select its first (free/basic) button even
        # if a lower premium button also happens to fall in the normal band.
        pill = find_pill(frame, REWARD_PILL_SCAN_Y)
    else:
        pill = find_pill(frame)
    if pill is not None:
        # Nearby Battle/Training cards are huge white panels and can contain
        # unrelated green actions. The real free reward-tier button is high in
        # the viewport after scrolling; a lower pill on huge panels means the
        # card is scrolled too far and must go back, never be tapped.
        if scroll_back and pill[1] > frame[1] * 0.25:
            return 'scroll_back', None
        return 'pill', pill
    if scroll_back:
        return 'scroll_back', None
    card = bottom_league_point(frame)
    if card is not None:
        return 'league', card
    if reward_scroll_needed(frame):
        return 'scroll', None
    return 'battle', None


def frame_image(frame) -> Image.Image:
    width, height, offset, data = frame
    size = width * height * 4
    return Image.frombytes("RGBA", (width, height), data[offset:offset + size]).convert("RGB")


def save_unknown_frame(device: DeviceAsyncWrapper, frame) -> Path | None:
    """Keeps the screen a leg refused to tap, so it can be named later.

    A leg that stops on the unknown limit leaves nothing behind but the count:
    three android-one legs ended that way on 2026-09-07, one after another, and there
    was no way afterwards to say what the phone had been showing. Failing to
    write the frame is never worth failing the stop over, so it is swallowed.
    """
    artifacts = config_paths.state_dir() / 'gbl' / 'unrecognised'
    path = artifacts / f"{time.strftime('%Y%m%d-%H%M%S')}-{device.label}.png"
    try:
        artifacts.mkdir(parents=True, exist_ok=True)
        frame_image(frame).save(path)
    except (OSError, ValueError):
        return None
    return path


def mapped_point(value) -> list[int] | None:
    """A config coordinate as a point, or None when it is not one.

    [0, 0] is how the fleet config spells "not calibrated on this handset", so
    it is refused here rather than tapped at the top left corner.
    """
    if not isinstance(value, list) or len(value) != 2:
        return None
    if not all(isinstance(item, int) for item in value):
        return None
    return list(value) if any(value) else None


def recovery_menu_point(device: "DeviceAsyncWrapper", frame) -> list[int]:
    """The map's pokeball, in screenshot pixels.

    MAIN_MENU_POINT is the compact-layout device's pokeball and no other handset's.  The android-three
    is 1:2.44 against the moto's 1:2.22, which puts its pokeball at 0.942 --
    175px below where the fraction lands, against a disc of radius 72.  The tap
    misses the button outright, the menu never opens, and the phone stands on
    the map for the rest of the day.  So the ball is located in the picture,
    which is also right on a handset nobody has measured.
    """
    point = gbl_home_recovery.find_pokeball(frame_image(frame))
    if point is not None:
        return point

    configured = mapped_point(device.config.get(gbl_home_recovery.BALL_KEY))
    if configured is not None:
        return configured

    return [int(frame[0] * MAIN_MENU_POINT[0]), int(frame[1] * MAIN_MENU_POINT[1])]


def recovery_menu_battle_point(
    device: "DeviceAsyncWrapper",
    frame,
    boxes: list[gbl_vision.OCRBox],
) -> list[int] | None:
    """The main menu's BATTLE disc, in screenshot pixels.

    The disc is looked for in the picture first.  It is the only strongly teal
    ring of its size in the menu's right-hand column, and it was located to the
    pixel on four handsets across three aspect ratios -- where the OCR path
    below reads the BATTLE *label* and drops a fixed fraction of the screen
    height to guess at the disc underneath it.

    MAIN_MENU_BATTLE_POINT, the last resort, is wrong on every handset
    including the compact-layout device: the discs sit in the right-hand column at x 0.777, and
    the centre column at that height is the gap between POKEDEX and SHOP.  The
    compact-layout device only recovers today because the OCR path answers before it.
    """
    point = gbl_home_recovery.find_menu_battle(frame_image(frame))
    if point is not None:
        return point

    point = gbl_vision.menu_battle_point(boxes, frame[1])
    if point is not None:
        return point

    # Neither BATTLE_BTN nor GBL_BATTLE_BTN is consulted here.  Both name a
    # BATTLE button on some other screen -- the friend screen's icon and the GBL
    # card's green pill -- and pressing either one's coordinates from the main
    # menu taps whatever happens to sit there.
    configured = mapped_point(device.config.get(gbl_home_recovery.BATTLE_KEY))
    if configured is not None:
        return configured

    return [
        int(frame[0] * MAIN_MENU_BATTLE_POINT[0]),
        int(frame[1] * MAIN_MENU_BATTLE_POINT[1]),
    ]


# The main menu and the GBL card both animate in, and how long they take is a
# property of the handset: the android-three opens the menu in well under a second and
# the android-one takes several.  Polled rather than slept through, so the fast phone
# is not made to wait and the slow one is not read mid-animation.
HOME_WALK_SETTLE = 12.0
HOME_WALK_POLL = 1.0


async def _settle_for(device: "DeviceAsyncWrapper", predicate, timeout: float = HOME_WALK_SETTLE):
    """Polls the phone until `predicate` holds, or until it has waited enough.

    `gbl_card_reached` runs OCR, which is a Swift helper and blocks, so the
    predicate is handed to a thread rather than run on the event loop.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    frame = await read_screen(device)
    while True:
        if await asyncio.to_thread(predicate, frame_image(frame)):
            return True, frame
        if asyncio.get_event_loop().time() >= deadline:
            return False, frame
        await wait(HOME_WALK_POLL, use_modifier=False)
        frame = await read_screen(device)


async def game_in_front(device: "DeviceAsyncWrapper") -> bool:
    """Whether Pokemon GO owns the focused window right now.

    A question the pictures cannot answer: a notification shade, a messaging
    app or the launcher all read as `unknown screen`, and so does a game screen
    this run has never seen.  Only the first three are worth relaunching for.
    """
    try:
        dumped = await device.shell('dumpsys window')
    except Exception as exc:  # noqa: BLE001 - an unreadable phone is not a verdict
        log(device, f'  Could not ask which app is in front ({exc}); assuming the game')
        return True
    front = gbl_home_recovery.package_in_front(dumped)
    if front and front != gbl_home_recovery.GAME_PACKAGE:
        log(device, f'  {front} is in front of Pokemon GO')
    return front == gbl_home_recovery.GAME_PACKAGE or not front


async def relaunch_game(device: "DeviceAsyncWrapper") -> None:
    """Send the launcher intent and give the game time to draw.

    Resume or cold start, the same intent covers both; which one happened only
    changes the wait.  The battle loop picks the walk back up from whatever is
    on screen afterwards, so nothing here needs to reach the GBL card itself.
    """
    running = bool((await device.shell(f'pidof {gbl_home_recovery.GAME_PACKAGE}')).strip())
    log(device, '  Bringing Pokemon GO forward' if running
        else '  Pokemon GO is not running; starting it')
    await device.shell(' '.join(gbl_home_recovery.LAUNCH_INTENT[1:]))
    await wait(MENU_SETTLE if running else GAME_COLD_START, use_modifier=False)


async def restart_game(device: "DeviceAsyncWrapper") -> None:
    """Stop Pokemon GO outright and cold-start it: the way off a screen that
    no recovery tap moves.  `relaunch_game` only brings a running game forward,
    which leaves it on that same screen."""
    await device.shell(f'am force-stop {gbl_home_recovery.GAME_PACKAGE}')
    await wait(MENU_SETTLE, use_modifier=False)
    await relaunch_game(device)


async def walk_home_to_gbl(device: "DeviceAsyncWrapper", frame) -> bool:
    """Map -> pokeball -> main menu -> BATTLE -> GO BATTLE LEAGUE, verified.

    `gbl_home_recovery` walks exactly these two buttons between legs, finding
    each one in the picture and checking the screen it opened before pressing
    the next.  Its detectors are reused here so a leg punted to the map mid-run
    walks back the same way, instead of by the parity ladder in
    `recover_to_gbl`: that ladder chose its tap from the attempt number alone,
    so a walk that had already opened the menu pressed the pokeball again and
    closed it, and the phone bounced between those two taps until the run gave
    up on it -- with the day's remaining battles unplayed.

    Returns whether the GBL card is up at the end of it.
    """
    image = frame_image(frame)
    if not gbl_home_recovery.main_menu_open(image):
        ball = gbl_home_recovery.find_pokeball(image)
        source = 'found in the picture'
        if ball is None:
            ball = mapped_point(device.config.get(gbl_home_recovery.BALL_KEY))
            source = f'from {gbl_home_recovery.BALL_KEY}'
        if ball is None:
            log(device, '  Map recovery: no pokeball on screen and none mapped')
            return False
        log(device, f'  Map recovery: pokeball {source} at {ball}')
        await tap(device, ball)
        opened, frame = await _settle_for(device, gbl_home_recovery.main_menu_open)
        if not opened:
            log(device, '  Map recovery: the pokeball did not open the main menu')
            return False
        image = frame_image(frame)
        log(device, '  Map recovery: the main menu is up')

    battle = gbl_home_recovery.find_menu_battle(image)
    source = 'found in the picture'
    if battle is None:
        battle = mapped_point(device.config.get(gbl_home_recovery.BATTLE_KEY))
        source = f'from {gbl_home_recovery.BATTLE_KEY}'
    if battle is None:
        log(device,
            '  Map recovery: no BATTLE disc on screen and no '
            f'{gbl_home_recovery.BATTLE_KEY} mapped')
        return False
    log(device, f'  Map recovery: menu BATTLE {source} at {battle}')
    await tap(device, battle)

    reached, _ = await _settle_for(device, gbl_home_recovery.gbl_card_reached)
    log(device, '  Map recovery: the GBL card is up' if reached
        else '  Map recovery: BATTLE did not lead to the GBL card')
    return reached


async def smart_screen_state(
    frame,
    profile: gbl_strategy.StrategyProfile,
    *,
    probe_unknown: bool = False,
    allow_row_scroll: bool = True,
    refused_rewards: Sequence[Sequence[int]] = (),
) -> tuple[str, list[int] | None, str | None, list[gbl_vision.OCRBox]]:
    """``refused_rewards`` are end-of-set points already caught not paying out.

    ``locked_reward_tile`` reads the caption, and on the moto-g it did not catch
    every locked tile, so the row was pressed rather than pulled back.  What the
    tap did is the check the caption cannot fail.
    """
    state, point = read_screen_state(frame)
    boxes: list[gbl_vision.OCRBox] = []
    if state != 'battle' or probe_unknown:
        image = frame_image(frame)
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        if gbl_vision.season_info_visible(boxes):
            return 'season_info', None, 'GBL season information', boxes
        blocked = gbl_vision.blocked_label(boxes)
        if blocked is not None:
            # Something on this screen must not be pressed, so nothing on it is.
            # A friendship level-up lands on top of the result screen with USE
            # LUCKY EGG in the pill's own slot, and the colour test reads it as
            # NEXT BATTLE: a live run tapped it eight times and only the stall
            # guard stopped it. The screen is named here and closed by the
            # caller, which knows the phone's own close button.
            return 'blocked', None, blocked, boxes
        # An unclaimed reward outranks any action label on the same card. At the
        # end of a set the card carries both, and the BATTLE it carries is spent
        # -- greyed out until the rewards beside it are taken. Reading the pill
        # first is what left a run tapping a dead button until the stall guard
        # stopped it, so the reward is looked for first and pressed by its own
        # label, the only thing on that screen either colour test can see.
        reward = gbl_vision.reward_point(boxes, image.width, image.height)
        if reward is not None and gbl_vision.gbl_card_visible(boxes):
            return 'orange', reward, None, boxes
        # OCR cannot always name it. A tile whose label is overlapped by the
        # card's close X leaves a fragment no reward word matches, and then the
        # dead pill wins by default again. `read_screen_state` has already found
        # the tile by colour, so prefer its verdict over an action label.
        if state == 'orange' and point is not None and gbl_vision.gbl_card_visible(boxes):
            reward_headers = [box.center_y for box in boxes
                              if gbl_vision.normalize(box.text) in (
                                  'basic rewards', 'premium rewards')]
            if reward_headers and point[1] < min(reward_headers):
                # The orange season article link sits above the reward row.
                # Color alone must never turn that link into a reward tap.
                tile = find_reward_tile(frame)
                if tile is None or tile[1] <= min(reward_headers):
                    return 'unconfirmed', None, None, boxes
                point = tile
            # ...but only if that tile is one this set has earned. The colour
            # scan cannot tell an earned tile from a locked one, and the row
            # regularly holds the earned tile off the left edge with two locked
            # ones in view: a leg took the locked "3 wins" tile, which opens the
            # rank roster, closed it, and took the same tile again every five
            # seconds until the day ran out.
            if not gbl_vision.locked_reward_tile(
                    boxes, point, image.width, image.height
            ) and not reward_refused(point, refused_rewards, image.width):
                return 'orange', point, None, boxes
            # Locked tiles in view, no earned one found, and a tile cut off
            # by the left edge: the row is scrolled past what this set earned,
            # so pull it back rather than pressing anything. Without the cut-off
            # tile this is just a card whose rewards are all taken, and the
            # BATTLE pill below is the way on.
            row = gbl_vision.reward_row_point(boxes, image.width, image.height)
            if row is not None and allow_row_scroll and clipped_reward_tile(frame):
                return 'tiles', row, None, boxes
        action = gbl_vision.action_point(boxes, image.width, image.height)
        if action is not None:
            # reward-tier chooser contains two identically labelled BATTLE
            # buttons. OCR quite reasonably returns lower one (Premium),
            # but colour/shape detector deliberately returns highest
            # visible pill (Basic/free). Keep detector's safe choice while
            # still using OCR to confirm actionable screen.
            # The detector's point is only worth preferring when it really is
            # a pill.  `read_screen_state` falls back to the centre of a white
            # card when it finds none, and handing that back as a pill taps the
            # middle of a panel that is not a button: the SE pressed the text of
            # the GO Battle League welcome card 24 times over three recoveries,
            # ended its leg having played nothing, and the day was called done
            # with battles still owed to it.
            tier_pill = (
                find_pill(frame, REWARD_PILL_SCAN_Y)
                if reward_chooser_visible(frame)
                else None
            )
            if tier_pill is not None:
                return 'pill', tier_pill, action[1], boxes
            return 'pill', action[0], action[1], boxes
        if state == 'pill' and point is not None and gbl_vision.gbl_card_visible(boxes):
            if gbl_vision.set_completed(boxes):
                return 'unconfirmed', None, None, boxes
            # The GBL card or reward chooser's green BATTLE pill can have its center
            # obscured by the teal close button (splitting "BATTLE" into "B" and "LE" in OCR).
            # Because gbl_card_visible confirms this is the GBL card, the detected green pill is safe.
            return 'pill', point, 'battle', boxes
        if state == 'pill':
            # Without OCR action label, never tap raw pill geometry.
            # This prevents menu card false positives stealing tap.
            if (
                point is None
                or point[1] <= int(image.height * 0.45)
                or reward_chooser_visible(frame)
            ):
                return 'unconfirmed', None, None, boxes
        # The reward-tier chooser prints the season's leagues as a sentence --
        # "Great League, Scroll Cup: Great League Edition, Competitors Cup" --
        # and the tests below match on any box holding "league" or "cup", so
        # they name the chooser a league list and the tap lands on the prose.
        # Handing the screen back to the shape detector's own verdict scrolls
        # the tier button into view, which is the way off this screen.
        if gbl_vision.reward_tier_prompt_visible(boxes):
            return state, point, None, boxes
        is_league_screen = state == 'league' or any(
            gbl_vision.normalize(b.text) == 'choose your league'
            or 'league' in gbl_vision.normalize(b.text)
            or 'cup' in gbl_vision.normalize(b.text)
            for b in boxes
        )
        if is_league_screen:
            preferred = getattr(getattr(profile, 'settings', None), 'preferred_league', None)
            team = getattr(profile, 'team', None)
            preferred_choice = [
                box for box in boxes
                if preferred and gbl_strategy.league_matches(
                    preferred, str(getattr(box, 'text', '')))]
            # Anything under the "what's coming next" divider is next week's
            # line-up: drawn like a card, greyed out, and inert to every tap.
            max_y = int(image.height * 0.90)
            upcoming = gbl_vision.upcoming_league_divider_y(boxes)
            if upcoming is not None:
                max_y = min(max_y, upcoming)
            choice = gbl_strategy.choose_easiest_league(
                preferred_choice if preferred_choice else boxes,
                preferred,
                team,
                min_y=int(image.height * 0.20),
                max_y=max_y,
            )
            if choice is None and preferred_choice:
                # Every card naming the configured league was in the preview
                # below the divider -- it is next week's league, not today's.
                # Handing back the shape detector's verdict here taps the
                # bottom-most card on screen, which is one of those previews.
                choice = gbl_strategy.choose_easiest_league(
                    boxes,
                    preferred,
                    team,
                    min_y=int(image.height * 0.20),
                    max_y=max_y,
                )
            if choice is not None:
                return 'league', [choice.x, choice.y], choice.name, boxes
        # OCR could read this screen and found no GBL action on it, so a button
        # shape here is a button belonging to something else. The colour tests
        # are deliberately broad -- the orange one matches the map's "+10,000
        # XP" toast, the green one matches any pill Pokemon GO cares to draw --
        # and broad is safe only while a label has the last word. Refusing here
        # costs one poll on a screen whose text was merely missed; allowing it
        # costs a Lucky Egg.
        if state in ('orange', 'pill'):
            if state == 'orange' and gbl_vision.reward_visible(boxes):
                return state, point, None, boxes
            return 'unconfirmed', None, None, boxes
    return state, point, None, boxes


async def settled_league_point(
    device: DeviceAsyncWrapper,
    profile,
    point: list[int],
    label: str | None,
) -> list[int] | None:
    """Where the wanted league card is once the list has stopped moving.

    Returns None while the two reads disagree -- on the card, or on which
    card it is -- so the caller can simply read the screen again rather than
    tap a position nothing is at any more.
    """
    frame = await read_screen(device)
    if frame is None:
        return None
    state, again, again_label, _boxes = await smart_screen_state(frame, profile)
    if state != 'league' or again is None:
        return None
    if label is not None and again_label != label:
        return None
    if abs(again[1] - point[1]) > int(frame[1] * LEAGUE_SETTLE_TOLERANCE):
        return None
    return again


def party_league_name(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """The league a CHOOSE YOUR PARTY screen says its set belongs to.

    The screen names it above the party -- `MASTER LEAGUE: MEGA EDITION`,
    `COMPETITORS CUP` -- and it is the only place the game says which league
    the taps actually entered, as opposed to which card was aimed at.
    """
    best: gbl_vision.OCRBox | None = None
    for box in boxes:
        text = gbl_strategy.canonical_name(str(getattr(box, "text", "")))
        words = text.split()
        if "league" not in words and "cup" not in words:
            continue
        if best is None or box.confidence > best.confidence:
            best = box
    if best is None:
        return None
    return gbl_strategy.canonical_name(str(best.text))


def wants_named_league(profile) -> bool:
    """Whether the config names a league instead of asking for the easiest."""
    wanted = gbl_strategy.canonical_name(
        getattr(getattr(profile, 'settings', None), 'preferred_league', '') or ''
    )
    return bool(wanted) and wanted != "auto"


async def leave_to_league_list(device: DeviceAsyncWrapper, frame) -> None:
    """Leaves the set that is open so the league list comes back.

    The door is the same top-left icon the iPhones use, and this is the same
    recovery: a set in the wrong league is five battles that do not count
    towards the configured one, and backing out costs one screen.
    """
    door = [int(frame[0] * GBL_EXIT_DOOR[0]), int(frame[1] * GBL_EXIT_DOOR[1])]
    log(device, f'  Leaving the open set by the exit door at {door}')
    await tap(device, door)


async def recognize_battle_context(frame) -> tuple[str | None, bool]:
    """Read the opponent and any outgoing NOT VERY EFFECTIVE cue together."""
    image = frame_image(frame)
    crop = (
        int(image.width * 0.40),
        int(image.height * OPPONENT_CROP[1]),
        int(image.width * OPPONENT_CROP[2]),
        int(image.height * 0.56),
    )
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    index = await asyncio.to_thread(gbl_meta.load_species_index)
    opponent = gbl_strategy.identify_pokemon(
        gbl_vision.lines(boxes), (meta.name for meta in index.values())
    )
    not_effective, _super_effective = battle_effectiveness(boxes)
    return opponent, not_effective


async def recognize_opponent(frame) -> str | None:
    """Compatibility wrapper for diagnostics which only need the name."""
    opponent, _not_effective = await recognize_battle_context(frame)
    return opponent


def shield_prompt_text(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Whether read text is the in-battle "Use a Protect Shield?" prompt.

    The prompt is worth naming from words alone: it is drawn nowhere but on a
    battlefield, so it is proof combat is running even on a frame whose shapes
    say nothing.
    """
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    return "protect shield" in text or ("attack incoming" in text and "not now" in text)


def weekly_report_visible(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Whether the read text is the weekly adventure-sync report.

    Pokemon GO draws this over the map once a week, and it is worth naming from
    words alone because every shape test on it answers wrong: no orange, and a
    white slab wide enough to read as a league card.  Two independent phrases
    are accepted because the headline carries the walked distance and OCR
    splits it unpredictably, while the stat rows at the foot do not move.
    """
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    if "last week" in text:
        return True
    return "eggs hatched" in text and ("calories" in text or "candy found" in text)


def daily_cap_reached(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Whether the card says the day's battles are already spent.

    The cap does not grey the pill out: BATTLE stays drawn in full green and
    the tier chooser stays up, so every shape test says the card is live and
    the taps are simply swallowed.  The SE burned its stall budget on this
    after the rank sheet was cleared off it.  The pink banner across the card
    is the only thing that says so, and it says it in words.
    """
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    if "maximum number of battles" in text:
        return True
    return "reached the maximum" in text


DISMISSING_REWARD_RADIUS = gbl_vision.DISMISSING_REWARD_RADIUS
reward_refused = gbl_vision.reward_refused


def rank_modal_visible(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Whether the roster sheet Pokemon GO draws over the GBL card is up.

    Invisible to every shape test, for the same reason the tap under it is
    wasted: the card is still drawn, still the right green and still the right
    shape, only dimmed and no longer listening.  Live, the android-three read BATTLE
    through this sheet and tapped it 36 times, spent all three flow refreshes
    on a screen that was never off the battle flow, and stopped with 32 of the
    day's battles unplayed.  The sheet's own words are the only thing on the
    frame that the card does not also carry.
    """
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    if "available at your rank" in text:
        return True
    return "keep winning to unlock" in text


def is_close_disc(rgb: tuple[int, int, int]) -> bool:
    """Teal enough to be a close disc rather than card, pill or system bar."""
    red, green, blue = rgb
    return blue > red + 30 and green > red + 20


def rank_modal_close_point(frame) -> list[int]:
    """Where to tap to shut the rank sheet, in screenshot pixels.

    The fraction was measured on the android-three, which has no navigation bar.  The
    Moto draws the same sheet on a 720x1600 screen whose bottom 60px are the
    system bar, so 0.945 lands at 1512 -- off the disc, on the bar, reading a
    flat (51, 51, 51).  Live on 2026-09-01 that is exactly where the Moto
    stopped: the sheet was named correctly on every read and logged as being
    closed, and not one of those taps ever touched it.

    The fraction keeps priority where the pixel under it is teal, so the android-three
    goes on tapping the point that has always worked there, and the search
    only runs on the frames where the measured point is demonstrably not on
    the disc.  That way a handset this was never measured on is recovered from
    its own frame rather than from another phone's screen geometry.
    """
    width, height, offset, data = frame
    measured = [
        round(width * RANK_MODAL_CLOSE_X),
        min(round(height * RANK_MODAL_CLOSE_Y), height - 1),
    ]
    if is_close_disc(pixel(width, offset, data, measured[0], measured[1])):
        return measured
    return teal_close_disc_point(frame) or measured


def teal_close_disc_point(frame) -> list[int] | None:
    """Centre of a teal close disc low on the frame, in screenshot pixels.

    The disc is a teal run down the middle of the card.  The map showing below
    the card is teal too -- it is sea, which is what puts two samples into
    teal_result_ready's grid and made the SE dismiss a checkmark that was not
    there.  The two are told apart by the one thing that cannot coincide: the
    sea reaches the bottom edge of the screen and the disc never does.
    """
    width, height, offset, data = frame
    x = width // 2
    runs: list[tuple[int, int]] = []
    start = None
    for y in range(int(height * WEEKLY_DISC_MIN_Y), height):
        if is_close_disc(pixel(width, offset, data, x, y)):
            if start is None:
                start = y
        elif start is not None:
            runs.append((start, y - 1))
            start = None
    if start is not None:
        runs.append((start, height - 1))
    for top, bottom in runs:
        if bottom >= height - 1:
            continue
        if bottom - top + 1 >= int(height * WEEKLY_DISC_MIN_SPAN):
            return [x, (top + bottom) // 2]
    return None


def weekly_report_close_point(frame) -> list[int] | None:
    """Centre of the weekly report's close disc, in screenshot pixels."""
    return teal_close_disc_point(frame)


async def recognized_shield_point(frame) -> list[int] | None:
    """Resolve a shifted shield prompt from its text and visible hexagon."""
    point = shield_point(frame)
    if point is not None:
        return point
    point = shield_candidate_point(frame)
    if point is None:
        return None
    image = frame_image(frame)
    crop = (
        int(image.width * 0.04),
        int(image.height * 0.45),
        int(image.width * 0.96),
        int(image.height * 0.96),
    )
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    if shield_prompt_text(boxes):
        return point
    return None


async def encounter_behind_battle(frame) -> str | None:
    """Whether a battle read is really the reward catch a set ended on.

    The home check below only knows the red Poke Ball, so an encounter holding
    an Ultra Ball passes for a battlefield: on 24 Sep the razr fast-attacked a
    reward Rufflet for three minutes, timed the battle out, and then "started"
    battle 3 on the same catch screen.

    'plate' is proof: no battlefield prints a CP in the plate band.  But the
    attack loop's bottom-centre probes press the throw ball, and a pressed ball
    hides the plate, so 'ball' covers that: a throw ball with no CP in the top
    band, where a battlefield always draws both players' plates.  The charged
    minigame is the one battle frame measured with a ball-like shape, and its
    top plates stay up.  'ball' is weaker, so the caller wants it twice.
    """
    from . import excellent_throw_ios

    image = frame_image(frame)
    top, bottom = gbl_vision.ENCOUNTER_PLATE_BAND
    crop = (0, int(image.height * top), image.width, int(image.height * bottom))
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    if gbl_vision.encounter_plate(boxes, image.height) is not None:
        return 'plate'
    ball = excellent_throw_ios.locate_throw_ball(image, image.size)
    if ball is None or ball.score < 0.55:
        return None
    header = (0, 0, image.width, int(image.height * 0.15))
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, header)
    if any(gbl_vision.ENCOUNTER_PLATE.search(gbl_vision.normalize(b.text)) for b in boxes):
        return None
    return 'ball'


async def recognize_player(frame, names: tuple[str, ...]) -> str | None:
    image = frame_image(frame)
    crop = (0, int(image.height * 0.02), int(image.width * 0.50), int(image.height * 0.32))
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    return gbl_strategy.identify_pokemon(gbl_vision.lines(boxes), names)


async def confirm_switch(
    device: DeviceAsyncWrapper,
    names: tuple[str, ...],
    expected: str,
    move_point: list[int] | None = None,
) -> str | None:
    """Watch the battle header until the incoming Pokemon appears.

    The swap animation runs for about two seconds, so reading the header a
    single POLL_GAP after the tap reports a failure that has not happened yet
    and the loop taps the same reserve again.  Returns the last name read so
    the caller can say what it saw.

    Sleeping through the gap left the phone visibly idle for four seconds on
    every switch, which is what a watcher sees as the bot doing nothing after
    a swap.  Given the fast-move point the wait is spent attacking instead:
    the taps land on the open battlefield either way, and the moment the new
    Pokemon is in they are already building energy.
    """
    seen: str | None = None
    for _ in range(SWITCH_VERIFY_READS):
        if move_point is None:
            await wait(POLL_GAP, use_modifier=False)
        else:
            await fast_attack(device, move_point)
        frame = await read_screen(device)
        if frame is None:
            continue
        seen = await recognize_player(frame, names)
        if gbl_strategy.canonical_name(seen or "") == gbl_strategy.canonical_name(expected):
            return seen
    return seen


async def fast_attack(
    device: DeviceAsyncWrapper,
    point: list[int],
    charged_point: list[int] | list[list[int]] | None = None,
):
    """Send a short burst alternating fast attacks and charged probes.

    Alternating the open battlefield point with the measured disc centres keeps
    energy generation continuous while probing both 1st and 2nd charged moves.
    """
    points = [point]
    if charged_point is not None:
        if isinstance(charged_point[0], list):
            points.extend(charged_point)  # type: ignore[arg-type]
        else:
            points.append(charged_point)  # type: ignore[arg-type]
    count = batch_size(
        tap_interval(device),
        getattr(device, "fast_attack_batch_budget", MOVE_BATCH_BUDGET),
        getattr(device, "fast_attack_taps_per_batch", MOVE_TAPS_PER_BATCH),
        MOVE_TAPS_MINIMUM,
    )
    # Fast moves are the default and the probes are the interruption, rather
    # than all four points taking equal turns.
    charged = points[1:]
    every = FAST_TAPS_PER_CHARGED_PROBE + 1
    sequence: list[list[int]] = []
    probed = 0
    for index in range(count):
        if charged and index % every == FAST_TAPS_PER_CHARGED_PROBE:
            sequence.append(charged[probed % len(charged)])
            probed += 1
        else:
            sequence.append(point)
    await send_input(device, tap_burst(device, sequence))


def tap_burst(device: DeviceAsyncWrapper, sequence: list[list[int]]) -> str:
    """The shell command that taps every point in sequence, in order.

    Chained on a phone where a start is cheap. On a slow one each `input` is
    launched into the background and the shell sleeps the stagger before
    launching the next, so the starts run concurrently and the events still land
    spaced out; the trailing `wait` holds the call open until the last one has
    injected, which keeps the batch as bounded a window of blindness as the
    chained form is. A phone with a monkey channel has no starts to overlap and
    no spacing to inherit from them, so its taps are chained around an explicit
    stagger instead.
    """
    commands = [f'input tap {target[0]} {target[1]}' for target in sequence]
    if monkey_channel(device) is not None:
        return f'; sleep {MONKEY_TAP_GAP:g}; '.join(commands)
    if parallel_taps(device):
        return f' & sleep {PARALLEL_TAP_STAGGER:g}; '.join(commands) + ' & wait'
    separator = f'; sleep {MOVE_TAP_GAP:g}; ' if MOVE_TAP_GAP > 0 else '; '
    return separator.join(commands)


def compact_layout(frame) -> bool:
    """Whether this is the shorter android-one-style battlefield layout."""
    return frame[1] / frame[0] <= COMPACT_ASPECT_MAX


def moto_layout(frame) -> bool:
    """Whether this is the letterboxed compact-layout device battlefield layout."""
    aspect = frame[1] / frame[0]
    return COMPACT_ASPECT_MAX < aspect <= MOTO_ASPECT_MAX


def charged_move_point(frame, slot: int = 0) -> list[int]:
    """Return the measured charged-move button point.
    slot=0: single/center (0.50)
    slot=1: left (0.35)
    slot=2: right (0.65)
    """
    width, height = frame[0], frame[1]
    if compact_layout(frame):
        y_fraction = COMPACT_CHARGED_Y
    elif moto_layout(frame):
        y_fraction = MOTO_CHARGED_Y
    else:
        y_fraction = CHARGED_Y
    x_fraction = CHARGED1_X if slot == 1 else (CHARGED2_X if slot == 2 else CHARGED_X)
    return [int(width * x_fraction), int(height * y_fraction)]


def charged_move_points(frame) -> list[list[int]]:
    """Return all candidate charged-move tap targets (Move 1, Move 2, Single)."""
    width, height = frame[0], frame[1]
    if compact_layout(frame):
        y_fraction = COMPACT_CHARGED_Y
    elif moto_layout(frame):
        y_fraction = MOTO_CHARGED_Y
    else:
        y_fraction = CHARGED_Y
    y = int(height * y_fraction)
    return [
        [int(width * CHARGED1_X), y],
        [int(width * CHARGED2_X), y],
        [int(width * CHARGED_X), y],
    ]


def disc_row_contrast(frame, y: int, inside_x, outside_x) -> float:
    """How sharply one row of the button stands out from beside it."""
    width, offset, data = frame[0], frame[2], frame[3]
    x0, x1 = inside_x
    inside = [
        pixel(width, offset, data,
              int(width * (x0 + (x1 - x0) * step / (CHARGED_DISC_STEPS - 1))), y)
        for step in range(CHARGED_DISC_STEPS)
    ]
    middle = [sum(rgb[channel] for rgb in inside) / len(inside) for channel in range(3)]
    return min(
        max(abs(rgb[channel] - middle[channel]) for channel in range(3))
        for rgb in (pixel(width, offset, data, int(width * xf), y) for xf in outside_x)
    )


def disc_present(frame, rows, inside_x=CHARGED_DISC_X,
                 outside_x=CHARGED_DISC_OUTSIDE_X) -> bool:
    """Whether a round button fills the given rows at the target position."""
    height = frame[1]
    return all(
        disc_row_contrast(frame, int(height * yf), inside_x, outside_x)
        >= CHARGED_DISC_CONTRAST
        for yf in rows
    )


def charged_rim_fraction(frame, cx_fraction: float, cy_fraction: float) -> float:
    """How much of the button's white rim is drawn at this position."""
    width, height, offset, data = frame
    radius = width * CHARGED_DISC_RADIUS
    cx, cy = width * cx_fraction, height * cy_fraction
    white = total = 0
    for step in range(CHARGED_RIM_ANGLES):
        angle = 2 * math.pi * step / CHARGED_RIM_ANGLES
        for band in CHARGED_RIM_FRACTIONS:
            x = int(cx + math.cos(angle) * band * radius)
            y = int(cy - math.sin(angle) * band * radius)
            if not (0 <= x < width and 0 <= y < height):
                continue
            total += 1
            if is_white(pixel(width, offset, data, x, y)):
                white += 1
    return white / total if total else 0.0


def charged_outside_fraction(frame, cx_fraction: float, cy_fraction: float) -> float:
    """How much of the ground just outside the button is white as well."""
    width, height, offset, data = frame
    radius = width * CHARGED_DISC_RADIUS
    cx, cy = width * cx_fraction, height * cy_fraction
    white = total = 0
    for step in range(CHARGED_RIM_ANGLES):
        angle = 2 * math.pi * step / CHARGED_RIM_ANGLES
        x = int(cx + math.cos(angle) * CHARGED_RIM_OUTSIDE * radius)
        y = int(cy - math.sin(angle) * CHARGED_RIM_OUTSIDE * radius)
        if not (0 <= x < width and 0 <= y < height):
            continue
        total += 1
        if is_white(pixel(width, offset, data, x, y)):
            white += 1
    return white / total if total else 0.0


def charged_disc_centre_y(frame) -> float:
    """The measured height of the charged-move row for this layout."""
    if compact_layout(frame):
        return COMPACT_CHARGED_DISC_CY
    if moto_layout(frame):
        return MOTO_CHARGED_DISC_CY
    return CHARGED_DISC_CY


def charged_disc_ready(frame, cx_fraction: float) -> bool:
    """Whether the button at this column is lit and will accept a tap."""
    centre = charged_disc_centre_y(frame)
    for shift in CHARGED_DISC_CY_SEARCH:
        cy = centre + shift
        if charged_rim_fraction(frame, cx_fraction, cy) < CHARGED_RIM_READY:
            continue
        if charged_outside_fraction(frame, cx_fraction, cy) <= CHARGED_RIM_OUTSIDE_MAX:
            return True
    return False


def ready_charged_discs(frame) -> list[float]:
    """The columns whose charged move is ready, Move 1 first."""
    return [
        cx_fraction
        for cx_fraction in CHARGED_DISC_CX
        if charged_disc_ready(frame, cx_fraction)
    ]


def charged_disc_band(frame) -> tuple[int, int]:
    """The rows a charged-disc rim test can reach, for a band read.

    Every sample `charged_disc_ready` takes lies within the outside ring at
    CHARGED_RIM_OUTSIDE radii of the centre, shifted by the widest entry in
    CHARGED_DISC_CY_SEARCH. A row either side keeps rounding out of it.
    """
    width, height = frame[0], frame[1]
    reach = width * CHARGED_DISC_RADIUS * CHARGED_RIM_OUTSIDE
    shift = height * max(abs(entry) for entry in CHARGED_DISC_CY_SEARCH)
    centre = height * charged_disc_centre_y(frame)
    return int(centre - reach - shift) - 1, int(centre + reach + shift) + 2


async def fresh_charged_discs(device: DeviceAsyncWrapper, frame) -> list[float] | None:
    """Re-read just the button row and say which charged moves are lit now.

    The frame the battle loop decides on is deliberately old: it is started
    alongside the tap burst and then has two or three OCR passes run over it, so
    by the time anything acts on it the screen is a second or two further on.
    That is the right trade for everything else in the loop, and wrong for this
    one decision -- the burst itself taps both disc centres, so the move the
    stale frame shows lit is quite often the move the burst has already fired,
    and the launch that follows spends its whole confirmation budget tapping a
    spent button. Asking the phone again costs one band read; a wrong answer
    costs ten to fifteen seconds of standing still.

    None if the read failed, which the caller should treat as "go ahead".
    """
    y0, y1 = charged_disc_band(frame)
    band = await screencap_band(device, frame, y0, y1)
    if band is None:
        return None
    return ready_charged_discs(band)


def charged_move_ready(frame, rows=None, inside_x=CHARGED_DISC_X,
                       outside_x=CHARGED_DISC_OUTSIDE_X) -> bool:
    """Whether any charged-move button is lit.

    `rows` is the iPhone path: gbl_ios passes its own measured rows and keeps
    the older contrast test.  Android reads the rim instead.
    """
    if rows is None:
        return bool(ready_charged_discs(frame))
    return (
        disc_present(frame, rows, inside_x, outside_x)
        or disc_present(frame, rows, CHARGED_DISC1_X, CHARGED_DISC1_OUTSIDE_X)
        or disc_present(frame, rows, CHARGED_DISC2_X, CHARGED_DISC2_OUTSIDE_X)
    )


def charged_move_target_point(frame, rows=None) -> list[int]:
    """Return the centre of the lit charged move, favouring Move 1 then Move 2."""
    width, height = frame[0], frame[1]
    if rows is None:
        ready = ready_charged_discs(frame)
        y = int(height * charged_disc_centre_y(frame))
        x_fraction = ready[0] if ready else CHARGED_DISC_CX[0]
        return [int(width * x_fraction), y]

    if compact_layout(frame):
        y_fraction = COMPACT_CHARGED_Y
    elif moto_layout(frame):
        y_fraction = MOTO_CHARGED_Y
    else:
        y_fraction = CHARGED_Y
    y = int(height * y_fraction)

    if disc_present(frame, rows, CHARGED_DISC1_X, CHARGED_DISC1_OUTSIDE_X):
        return [int(width * CHARGED1_X), y]
    if disc_present(frame, rows, CHARGED_DISC2_X, CHARGED_DISC2_OUTSIDE_X):
        return [int(width * CHARGED2_X), y]
    if disc_present(frame, rows, CHARGED_DISC_X, CHARGED_DISC_OUTSIDE_X):
        return [int(width * CHARGED_X), y]

    return [int(width * CHARGED1_X), y]



def dark_overlay_row(frame, y_fraction: float, threshold: int = 120) -> bool:
    """Whether a wide row is covered by a dark battle decision panel."""
    width, height, offset, data = frame
    samples = 17
    dark = 0
    for step in range(samples):
        x = int(width * (0.10 + 0.80 * step / (samples - 1)))
        if max(pixel(width, offset, data, x, int(height * y_fraction))) < threshold:
            dark += 1
    return dark / samples >= 0.75


def shield_prompt(frame, rows=SHIELD_DISC_ROWS, panel_y: float = 0.75) -> bool:
    """Detect the shield hex only while the dark Protect Shield panel is up."""
    return shield_point(frame, rows=rows, panel_y=panel_y) is not None


def shield_point(frame, rows=None, panel_y: float | None = None) -> list[int] | None:
    """Return the centre of the visible Protect Shield hexagon.

    The prompt is much higher on the android-one than on a tall-layout device or iPhone. A fixed y
    can therefore log a shield attempt while tapping below the button. The
    hexagon produces one long contrast band at screen centre; short text and
    sparkle runs are ignored.
    """
    width, height = frame[0], frame[1]
    if rows is None:
        rows = COMPACT_SHIELD_DISC_ROWS if compact_layout(frame) else SHIELD_DISC_ROWS
    if panel_y is None:
        panel_y = 0.68 if compact_layout(frame) else 0.72
    if not dark_overlay_row(frame, panel_y) or not disc_present(frame, rows):
        return None
    step = 4
    start = int(height * max(0.55, min(rows) - 0.06))
    end = int(height * min(0.95, max(rows) + 0.06))
    runs: list[list[int]] = []
    for y in range(start, end, step):
        contrast = disc_row_contrast(
            frame, y, CHARGED_DISC_X, CHARGED_DISC_OUTSIDE_X)
        if contrast < CHARGED_DISC_CONTRAST:
            continue
        if not runs or y - runs[-1][-1] > step * 2:
            runs.append([y])
        else:
            runs[-1].append(y)
    candidates = [
        run for run in runs
        if run[-1] - run[0] >= height * SHIELD_MIN_HEIGHT
    ]
    if not candidates:
        return None
    run = max(candidates, key=lambda item: item[-1] - item[0])
    return [int(width * SHIELD_TAP_X), (run[0] + run[-1]) // 2]


def shield_candidate_point(frame) -> list[int] | None:
    """Find a shield-sized centre disc anywhere in the lower battle panel."""
    width, height = frame[0], frame[1]
    step = 4
    hits: list[int] = []
    for y in range(
            int(height * SHIELD_SCAN_Y[0]),
            int(height * SHIELD_SCAN_Y[1]), step):
        if disc_row_contrast(
                frame, y, CHARGED_DISC_X,
                CHARGED_DISC_OUTSIDE_X) >= CHARGED_DISC_CONTRAST:
            hits.append(y)
    runs: list[list[int]] = []
    for y in hits:
        if not runs or y - runs[-1][-1] > step * 2:
            runs.append([y])
        else:
            runs[-1].append(y)
    candidates = [
        run for run in runs
        if run[-1] - run[0] >= height * SHIELD_MIN_HEIGHT
    ]
    if not candidates:
        return None
    run = max(candidates, key=lambda item: item[-1] - item[0])
    return [int(width * SHIELD_TAP_X), (run[0] + run[-1]) // 2]


def result_tap_point(frame) -> list[int]:
    """The middle of the teal tick, which is not at one height on every phone."""
    y_fraction = MOTO_RESULT_TAP_Y if moto_layout(frame) else RESULT_TAP_Y
    return [int(frame[0] * RESULT_TAP_X), int(frame[1] * y_fraction)]


def teal_result_ready(frame, sample_x=TEAL_SAMPLE_X, sample_y=TEAL_SAMPLE_Y) -> bool:
    """Whether the bottom action is the teal post-battle checkmark.

    This is only consulted after screen-state detection found no menu action.
    It sends one tap per frame. The previous loop sent both a charged probe and
    a result probe from the same stale frame, dismissing GOOD EFFORT with the
    first tap and then closing the GBL card with the second.

    Takes its sample grid for the same reason charged_move_ready does, and
    picks a per-handset default the same way -- an explicit grid, as iOS
    passes, is left alone.
    """
    if sample_y is TEAL_SAMPLE_Y and moto_layout(frame):
        sample_y = MOTO_TEAL_SAMPLE_Y
    width, height, offset, data = frame
    teal = 0
    total = 0
    for xf in sample_x:
        for yf in sample_y:
            red, green, blue = pixel(
                width, offset, data, int(width * xf), int(height * yf))
            total += 1
            if red <= 70 and 115 <= green <= 190 and 115 <= blue <= 195 \
                    and abs(green - blue) <= 35:
                teal += 1
    return teal / total >= TEAL_READY_FRACTION


def sheet_row(frame, y: int) -> bool:
    """Whether one row is the flat slate of the switch sheet."""
    width, offset, data = frame[0], frame[2], frame[3]
    x0, x1 = SHEET_SCAN_X
    slate = 0
    for step in range(SHEET_STEPS):
        xf = x0 + (x1 - x0) * step / (SHEET_STEPS - 1)
        red, green, blue = pixel(width, offset, data, int(width * xf), y)
        if red >= SHEET_MIN_RED and blue <= SHEET_MAX_BLUE \
                and blue - red >= SHEET_BLUE_OVER_RED:
            slate += 1
    return slate / SHEET_STEPS >= SHEET_FRACTION


def switch_sheet(frame, above_y=SHEET_ABOVE_Y, scan_y=SHEET_SCAN_Y) -> bool:
    """Whether "Switch in a new Pokemon?" is covering the battle controls.

    Two slate rows under the sheet's top edge, and a battlefield row above it.
    The row above is what keeps this off the league chooser, whose bottom is
    just as dark and starts at the same height.

    The heights are arguments for the reason every other battle fraction here
    is: they are measured on a 19.5:9 Android panel and the sheet does not sit
    at the same fraction of a 16:9 iPhone.
    """
    height = frame[1]
    return (
        not sheet_row(frame, int(height * above_y))
        and all(sheet_row(frame, int(height * yf)) for yf in scan_y)
    )


def sheet_card_point(frame, tries: int, card_y=SHEET_CARD_Y, card_x=SHEET_CARD_X) -> list[int]:
    """The next reserve position to offer a tap during a switch sheet."""
    width, height = frame[0], frame[1]
    return [int(width * card_x[tries % len(card_x)]), int(height * card_y)]


def minigame_segments(
    width: int, height: int, sweeps: int = MINIGAME_SWEEPS,
) -> list[tuple[int, int, int, int]]:
    """Dense time-spread coverage for every charged-move bubble pattern.

    A single slow pass only catches icons which already exist. Repeating a
    tight raster keeps a finger crossing the complete playfield while later
    waves spawn. Offset passes and diagonals also cover spiral patterns.
    """
    left, right = int(width * 0.02), int(width * 0.98)
    # The bubble field is the lower half of the screen, not the whole of it.
    # Measured off traced charge-result frames on two handsets, the hexagons
    # run 0.516-0.947h on the compact-layout device and 0.559h down past 0.9h on the android-one.
    # Sweeping from 0.14h spent more than half of every wave in empty sky, and
    # its top-left corner sat next to the flee button; the bottom stops short
    # of the navigation bar.
    top, bottom = int(height * MINIGAME_TOP), int(height * MINIGAME_BOTTOM)
    rows = 7
    segments: list[tuple[int, int, int, int]] = []
    # Six temporally-spaced passes cover the full ~6.5-7.0s charged move bubble
    # spawning window on Android and iOS, reaching Excellent/Great ratings.
    for sweep in range(sweeps):
        offset = (bottom - top) / (rows * 2) if sweep % 2 else 0
        ys = [
            min(int(top + (bottom - top) * row / (rows - 1) + offset), bottom)
            for row in range(rows)
        ]
        if sweep % 2:
            ys.reverse()
        for index, y in enumerate(ys):
            start, end = (
                (left, right) if (index + sweep) % 2 == 0 else (right, left)
            )
            segments.append((start, y, end, y))
        # Cross the diagonal/spiral paths once per wave as well as the rows.
        segments.append(
            (left, bottom, right, top)
            if sweep % 2 == 0
            else (right, bottom, left, top)
        )
    return segments


async def charged_minigame(device: DeviceAsyncWrapper, frame):
    """Continuously sweep the bubble field long enough to target Excellent."""
    minigame_sweep_ms = getattr(device, "minigame_sweep_ms", MINIGAME_SWIPE_MS)
    minigame_budget = getattr(device, "minigame_batch_budget", MINIGAME_BUDGET)
    minigame_gestures_per_sweep = getattr(
        device, "minigame_gestures_per_sweep", MINIGAME_GESTURES_PER_SWEEP
    )
    minigame_sweeps = getattr(device, "minigame_swipes", MINIGAME_SWEEPS)
    sweeps = batch_size(
        gesture_interval(device, minigame_sweep_ms),
        minigame_budget / minigame_gestures_per_sweep,
        minigame_sweeps,
    )
    segments = minigame_segments(frame[0], frame[1], sweeps)
    commands = [
        # Sub-frame swipes are effectively just endpoints on Android and only
        # earned NICE.  Fifty milliseconds gives the game several sampled
        # MOVE events across each complete row while 24 gestures still fit; a
        # phone that pays for every start drags for longer instead, so that the
        # start it has already paid for buys more of the playfield.
        f'input swipe {x1} {y1} {x2} {y2} {int(minigame_sweep_ms)}'
        for x1, y1, x2, y2 in segments
    ]
    await send_input(device, swipe_burst(device, commands))
    # Different type animations finish a few frames apart.  Do not let the
    # normal attack loop tap through a still-active bubble phase.
    await wait(CHARGED_MINIGAME_SETTLE, use_modifier=False)


async def launch_charged_move(
    device: DeviceAsyncWrapper,
    frame,
    attempts: int = CHARGED_LAUNCH_ATTEMPTS,
    target_point: list[int] | None = None,
    column: float | None = None,
) -> tuple[bool, object, int, str | None]:
    """Tap a lit charged move and prove the charged sequence actually began.

    A disappearing/obscured button is not proof of a launch: damage flashes
    and battle animation caused that test to report success while the Pokemon
    simply stood idle for the entire prompt timeout.  Only the game's GET
    READY or SWIPE text latches a launch now -- or, given the column that was
    tapped, that column going dark, which is the same fact read off the button
    instead of off a caption that is only up for about a second.  Watching the
    button as well matters most on the phone that cannot read quickly: the
    prompt window can close inside a single android-one screencap.

    A missed input remains nonfatal so the caller immediately resumes fast
    attacks.
    """
    if target_point is None:
        target_point = charged_move_target_point(frame)
    current = frame
    for attempt in range(1, attempts + 1):
        await tap(device, target_point)
        # One read CHARGED_LAUNCH_CONFIRM after the tap is not long enough for
        # GET READY to be on screen.  Across one traced set every launch on
        # every phone reported a miss here, and 12 of them were then found by
        # the caller's live-frame recovery a few reads later -- the move had
        # gone out and the confirmation simply arrived early.  Poll the prompt
        # instead, and only re-tap once the window has closed: a second tap
        # after a launch that did land goes into the minigame playfield.
        for _ in range(CHARGED_LAUNCH_POLLS):
            await wait(CHARGED_LAUNCH_CONFIRM, use_modifier=False)
            label, refreshed = await read_charged_prompt(device)
            if refreshed is not None:
                current = refreshed
            if label is not None:
                return True, current, attempt, label
            if (
                column is not None
                and refreshed is not None
                and shield_candidate_point(refreshed) is None
                and not charged_disc_ready(refreshed, column)
            ):
                # The buttons are also gone behind a shield prompt, and a
                # prompt is the one thing that both hides them and swallows the
                # tap that was meant for them, so a visible hexagon means the
                # move did not go out rather than that it did.
                return True, current, attempt, None
    return False, current, attempts, None


# The catch screen a finished set hands over. Nothing about the throw itself is
# measured here any more: it is `scripts/excellent_throw.py`'s, geometry included.
# How long to wait for the ball to be found on a reward encounter that is
# already on screen. Two agreeing detections is what `detect_encounter_ball`
# wants, and the android-one pays 4.1s a frame, so this is generous rather than tight.
ENCOUNTER_BALL_WAIT = 20.0
ENCOUNTER_THROWS = 4            # Great Balls worth spending on a reward mon
# How many times a reward encounter may be given up on before the routine stops
# paying for it. `take_reward_encounter` is bounded per call, but the main loop
# re-enters it from the top every time it still sees the plate, which hands it a
# fresh budget -- so "four Great Balls, not a morning" silently became both. The
# SE hit this first; the same loop is here. Past this many, flee and never throw.
ABANDONED_ENCOUNTER_LIMIT = 1
ENCOUNTER_READS = 24


def excellent_thrower(device: DeviceAsyncWrapper, image):
    """This phone wrapped as the excellent-throw routine's device.

    The GBL run already holds the adb connection and the fleet lock, so the
    routine is driven over them rather than shelled out, the same way the iOS
    side drives it over its live WDA session.
    """
    from . import excellent_throw_android

    config = excellent_throw_android.load_config(
        None, excellent_throw_android.parse_args([])
    )
    return excellent_throw_android.AndroidDevice(
        device=device,
        serial=device.serial,
        label=device.label or device.serial,
        viewport=image.size,
        config=config,
        display_id=device.display_id,
    )


async def throw_ball(
    device: DeviceAsyncWrapper, frame, berry: str = 'nanab'
) -> bool | None:
    """Throw one ball at the reward Pokemon.  Says whether the encounter ended.

    The throw itself belongs to `scripts/excellent_throw.py`'s routine, which is where
    catching is tuned -- the Nanab, the held ball, the measured catch circle,
    the single `input swipe` and the read-back of the result.  This used to be
    a second implementation that borrowed a few of its helpers and reinvented
    the rest, and it inherited none of the tuning: it never fed a berry at all,
    and it threw a fixed length at every distance.  A phone that catches under
    `scripts/excellent_throw.py` now catches here.

    `scripts/excellent_throw.py` cannot be shelled out to from inside a GBL run -- it
    would open its own adb connection and take the fleet lock this run already
    holds -- so the routine is driven over ours, the same way the iOS side
    drives it over its live WDA session.
    """
    # None means no throw was sent; False is a sent throw without a catch.
    from . import excellent_throw_android

    image = frame_image(frame)
    thrower = excellent_thrower(device, image)
    artifacts = (
        config_paths.state_dir() / 'gbl' / 'reward-catch'
        / time.strftime('%Y%m%d-%H%M%S')
    )
    artifacts.mkdir(parents=True, exist_ok=True)

    # A berry every throw, not once an encounter: a Nanab's calm lasts only
    # until the Pokemon breaks out of a ball, so the throw after a break-out is
    # aimed at a Pokemon that is moving again unless it is fed a second one.
    for use_nanab in (True, False):
        try:
            return await excellent_throw_android.run_once(
                thrower,
                wait_seconds=ENCOUNTER_BALL_WAIT,
                artifact_dir=artifacts,
                dry_run=False,
                use_nanab=use_nanab,
                berry=berry,
                # A held-ring calibration releases its touch before the real
                # swipe. On slow input backends the ball is still settling,
                # so that swipe can miss it. Use the direct catch path here.
                ring_hold=False,
            )
        except excellent_throw_android.NoEncounterError as exc:
            # The retry below is a retry of the *berry*. Nothing was on screen
            # to throw at, so a second wait only spends the catch budget twice
            # over -- 40 seconds, against a target of 30 for the whole catch.
            log(device, f'  Throw refused ({exc}); leaving the encounter alone')
            return None
        except excellent_throw_android.AndroidExcellentThrowError as exc:
            if use_nanab:
                # An empty berry pocket is not a reason to keep the ball.
                log(device, f'  {exc}; throwing without a berry')
                continue
            log(device, f'  Throw refused ({exc}); leaving the encounter alone')
            return None


async def take_reward_encounter(
    device: DeviceAsyncWrapper, spend_balls: bool = True
) -> bool:
    """Catch what a finished set pays out, and get back to the card behind it.

    Returns whether the GBL card came back. Bounded rather than persistent: a
    reward Pokemon that will not stay in the ball is worth four Great Balls, not
    a morning, and whatever is left over is handed to the ordinary recovery,
    which is where the run was going anyway.

    Three screens follow a catch and all three are answered here rather than by
    the main loop, which must not learn to press either of the last two: OK is
    written on dialogs that spend things, and the caught Pokemon's own page
    carries POWER UP in the pill slot -- already a blocked label, and it stays
    blocked. Pressing them is safe only for the routine that just threw a ball.
    """
    throws = 0
    berry = 'nanab'
    for _read in range(ENCOUNTER_READS):
        frame = await read_screen(device)
        if frame is None:
            continue
        image = frame_image(frame)
        try:
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        except gbl_vision.VisionOCRError:
            return False

        if gbl_vision.gbl_card_visible(boxes):
            log(device, f'  Back on the GBL card after {throws} throw(s)')
            return True

        plate = gbl_vision.encounter_plate(boxes, image.height)
        # The map test has to come second: `on_map` is only "a pokeball is on
        # this picture", and an encounter has one at the bottom centre over the
        # same grass.  Tested first it named every reward catch a map, walked
        # off to tap a ball that was really the throw ball, and came straight
        # back -- a android-three sat on one CP 405 for two minutes that way.  The plate
        # is a positive sighting of an encounter, so it settles the screen.
        if plate is None and gbl_home_recovery.on_map(image):
            log(device, '  Returned to map after catch; walking back to GBL')
            await walk_home_to_gbl(device, frame)
            return True

        if plate is not None:
            if throws >= ENCOUNTER_THROWS or not spend_balls:
                # BACK walks out of a wild encounter, so unlike the iOS side
                # there is no coordinate to get wrong here. Returning instead
                # just hands the plate back to the caller, which calls in again
                # with `throws` reset to zero.
                reason = ('will not stay in the ball' if spend_balls
                          else 'was already given up on')
                log(device, f'  {plate.text.strip()} {reason}; fleeing')
                await send_input(device, 'input keyevent KEYCODE_BACK')
                await wait(MENU_SETTLE)
                continue
            # Every box, not just the plate: the plate box is sometimes only
            # "CP 2056", with the name read as a box of its own.
            legendary = legendary_pokemon.legendary_named(box.text for box in boxes)
            if legendary is not None and berry != 'silver':
                log(device, f'  {legendary} is legendary; feeding it Silver Pinaps')
                berry = 'silver'
            log(device, f'  Set reward is a catch: {plate.text.strip()}, preparing throw {throws + 1}')
            # The routine waits out the throw and presses through what a catch
            # leaves standing, so there is nothing left to settle for here.
            outcome = await throw_ball(device, frame, berry)
            if outcome is None:
                raise AutoGBLError(
                    'Reward throw was not sent; leaving the encounter open '
                    f'after {throws} sent throw(s)'
                )
            throws += 1
            continue

        summary = gbl_vision.dismiss_point(boxes)
        if summary is not None:
            log(device, f'  Catch summary, taking OK at {summary}')
            await tap(device, summary)
            await wait(MENU_SETTLE)
            continue

        blocked = gbl_vision.blocked_label(boxes)
        if blocked is not None and gbl_vision.normalize(blocked) == 'power up':
            close = device.config['CLOSE_BTN']
            log(device, f'  Caught Pokemon page, closing it at {close}')
            await tap(device, close)
            await wait(MENU_SETTLE)
            continue

        await wait(POLL_GAP, use_modifier=False)
    return False


async def recover_to_gbl(
    device: DeviceAsyncWrapper,
    frame,
    boxes: list[gbl_vision.OCRBox],
    attempt: int,
) -> None:
    """One step back toward GO BATTLE LEAGUE screen, or nothing.

    Called once per read while phone off battle flow. Every step
    one player would take -- close panel, open main menu, press BATTLE
    -- every target located on screen belongs to, so step can
    only happen on screen actually it. Where nothing recognised
    menu button one blind tap, same reason exit door is:
    by then alternative stopping, stopping strands phone.
    """
    if gbl_vision.season_info_visible(boxes):
        log(device, '  Closing GBL season information with Android Back')
        await send_input(device, 'input keyevent KEYCODE_BACK')
        return
    if picker_screen(boxes) or picker_body_screen(boxes):
        # most specific screen this be standing on, so it tried
        # first. Nothing below recognises picker: sheet covers
        # whole screen, generic close button blind menu tap
        # both land inside roster only scroll it.
        log(device, ' Party picker still open; cancelling out of it')
        await cancel_verified_picker(device)
        return

    if gbl_vision.gbl_card_visible(boxes):
        if attempt > 1:
            shut = [int(frame[0] * 0.5), int(frame[1] * 0.94)]
            log(device, f'  Stalled on GBL card; refreshing via close button at {shut}')
            await tap(device, shut)
            return
        log(device, '  Already on the GBL card; leaving it for the battle loop')
        return

    if gbl_vision.main_menu_visible(boxes):
        point = recovery_menu_battle_point(device, frame, boxes)
        if point is not None:
            log(device, f' Main menu is up; taking BATTLE back to GBL at {point}')
            await tap(device, point)
            return

    if teal_result_ready(frame):
        result_boxes = boxes
        if not result_boxes:
            try:
                result_boxes = await asyncio.to_thread(
                    gbl_vision.recognize, frame_image(frame)
                )
            except gbl_vision.VisionOCRError:
                result_boxes = []
        if gbl_vision.gbl_card_visible(result_boxes):
            log(device, '  Teal disc here is the GBL card close button, not a checkmark; leaving it')
            return
        point = result_tap_point(frame)
        log(device, f' Closing panel teal button at {point}')
        await tap(device, point)
        return

    # A screen that names itself is worth more than a counter.  The ladder
    # below picks its tap from `attempt` alone, which is only ever right by
    # luck: it cannot tell the map from the menu, so it presses the pokeball on
    # a menu that is already open and closes it again.  When either screen is
    # actually detected, walk them in order and check each one.
    recovery_image = frame_image(frame)
    if gbl_vision.encounter_plate(boxes, recovery_image.height) is not None:
        # An encounter carries a pokeball at the bottom centre, so `on_map`
        # below says yes to one and the walk home taps the throw ball instead
        # of the menu.  BACK is the one press that leaves a wild encounter, and
        # the reward catch uses it to flee for the same reason.
        log(device, '  Encounter still up in recovery; backing out of it')
        await send_input(device, 'input keyevent KEYCODE_BACK')
        await wait(MENU_SETTLE)
        return

    if gbl_home_recovery.main_menu_open(recovery_image) \
            or gbl_home_recovery.on_map(recovery_image):
        await walk_home_to_gbl(device, frame)
        return

    # A game that has just started sits behind its safety warning, and no
    # amount of waiting or tapping elsewhere moves it.
    point = gbl_vision.safety_notice_point(boxes, recovery_image)
    if point is not None:
        log(device, f'  Start-up safety warning; pressing OK at {point}')
        await tap(device, point)
        await wait(MENU_SETTLE)
        return

    # Nothing on this screen belongs to the game, which is worth asking Android
    # about before pressing anything: the taps below are the game's own
    # coordinates, and a phone showing a messaging app takes them all the same.
    if attempt >= FOREGROUND_CHECK_FROM and not await game_in_front(device):
        await relaunch_game(device)
        return

    recovery_step: int | None = None
    if attempt <= 2:
        recovery_step = (attempt - 1) % 2
    elif attempt >= BATTLE_STATIC_LIMIT:
        recovery_step = (attempt - BATTLE_STATIC_LIMIT) % 2
    elif attempt >= UNKNOWN_RECOVER_FROM:
        recovery_step = (attempt - UNKNOWN_RECOVER_FROM) % 2

    menu = recovery_menu_point(device, frame)
    if recovery_step is not None:
        if recovery_step == 0:
            log(device, f' Main-screen recovery: opening pokeball at {menu}')
            await tap(device, menu)
        else:
            point = recovery_menu_battle_point(device, frame, boxes)
            log(device, f' Main-screen recovery: BATTLE via menu flow at {point}')
            await tap(device, point)
        return

    close = device.config.get('CLOSE_BTN')
    if close is not None and attempt <= 4:
        log(device, f' Closing panel mapped CLOSE_BTN at {close}')
        await tap(device, close)
        return

    log(device, f' Opening main menu at {menu} find way back')
    await tap(device, menu)
async def scroll_reward_tiers(device: DeviceAsyncWrapper, frame, *, back: bool = False):
    """Scrolls the reward chooser toward its entry or completed-set button."""
    width, height = frame[0], frame[1]
    x = width // 2
    # Move the chooser only a small amount at a time. A large upward swipe can
    # push Basic Rewards off android-one and leave Premium as the first visible pill.
    start, end = ((0.46, 0.72) if back else (0.72, 0.52))
    await send_input(
        device,
        f'input swipe {x} {int(height * start)} {x} {int(height * end)} 500')


async def scroll_reward_row(device: DeviceAsyncWrapper, frame, point) -> None:
    """Drags the end-of-set reward row right, back toward its first tile.

    Sideways, not down: the tiles a set pays out sit in one horizontal row and
    the earned one is left-most, so a row scrolled on is a row with nothing
    pressable in view. This is the gesture the fleet was rescued with by hand.
    """
    width = frame[0]
    y = point[1]
    await send_input(
        device,
        f'input swipe {int(width * 0.30)} {y} {int(width * 0.92)} {y} 500')


def picker_body_screen(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Recognize the picker body even when the keyboard hides its footer."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    body_signals = sum(
        marker in text
        for marker in ("show evolutionary line", "pokemon", "tags")
    )
    return body_signals >= 2


def picker_screen(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Return true only for the Pokemon party editor's full roster picker."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    footer = "cancel" in text and "done" in text
    return footer and picker_body_screen(boxes)


def party_screen_ocr(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Recognize the party card across aspect ratios from its two labels."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    return "choose your party" in text and "use this party" in text


def battle_outcome(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """Return an evidence-backed result label from the post-battle artwork."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    if "you win" in text or "victory" in text:
        return "WIN"
    if "good effort" in text:
        return "LOSS"
    return None


def charged_minigame_rating(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """Read the game's own charged-swipe grade for traceable calibration."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    for rating in ("excellent", "great", "nice"):
        if rating in text:
            return rating.upper()
    return None


def battle_effectiveness(boxes: list[gbl_vision.OCRBox]) -> tuple[bool, bool]:
    """Return outgoing resistance and incoming weakness cues from battle text."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    return "not very effective" in text, "super effective" in text


async def capture_charged_rating(device: DeviceAsyncWrapper) -> None:
    """Save and OCR the transient rating frame without affecting decisions."""
    rating_frame = await read_screen(device)
    if rating_frame is None:
        return
    image = frame_image(rating_frame)
    if TRACE_DIR is not None:
        save_trace(device, rating_frame, "charge-result", None)
    try:
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
    except gbl_vision.VisionOCRError:
        return
    rating = charged_minigame_rating(boxes)
    if rating is not None:
        log(device, f"  Charged minigame: {rating}")


def charged_prompt_label(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """Return the charged-sequence prompt visible in OCR, if any."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    if "swipe" in text:
        return "swipe"
    if "get ready" in text:
        return "get ready"
    return None


async def read_charged_prompt(device: DeviceAsyncWrapper):
    """Read GET READY/SWIPE and return it with the current frame."""
    current = await read_screen(device)
    if current is None:
        return None, None
    image = frame_image(current)
    crop = (
        int(image.width * 0.08),
        int(image.height * 0.12),
        int(image.width * 0.92),
        int(image.height * 0.64),
    )
    try:
        boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    except gbl_vision.VisionOCRError:
        return None, current
    return charged_prompt_label(boxes), current


async def charged_prompt_from_frame(frame) -> str | None:
    """Recognize a charged prompt in an already-captured combat frame."""
    image = frame_image(frame)
    crop = (
        int(image.width * 0.08),
        int(image.height * 0.12),
        int(image.width * 0.92),
        int(image.height * 0.64),
    )
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    return charged_prompt_label(boxes)


async def wait_for_charged_swipe(
        device: DeviceAsyncWrapper, frame, timeout: float = 2.2):
    """Wait for the real SWIPE cue so the raster starts at the right time."""
    deadline = asyncio.get_event_loop().time() + timeout
    current = frame
    while asyncio.get_event_loop().time() < deadline:
        label, refreshed = await read_charged_prompt(device)
        if refreshed is not None:
            current = refreshed
        if label == "swipe":
            log(device, "  Charged minigame SWIPE prompt detected")
            return current
        await wait(0.06, use_modifier=False)
    log(device, "  Charged minigame SWIPE prompt timed out; using fallback timing")
    return current


async def probe_charged_after_fast_attacks(
    device: DeviceAsyncWrapper, frame, *, observed_frame=None) -> bool:
    """Handle a charged prompt observed during the attack burst.

    The battle loop supplies its concurrent screen capture; charged centres
    were already tapped inside that burst. Standalone callers can still use
    the explicit centre-tap/read fallback when they have no observed frame.
    """
    point = charged_move_point(frame)
    # A tap can launch near the end of the preceding shell burst.  At 140 ms
    # the screenshot was often taken before GET READY was drawn; the next loop
    # then landed inside the dots with ordinary taps.  This delay samples the
    # stable prompt instead.
    if observed_frame is None:
        await tap(device, point)
        await wait(0.32, use_modifier=False)
        label, current = await read_charged_prompt(device)
    else:
        # The battle loop already probes all charged centres in its tap burst.
        # Inspect the frame captured alongside that burst instead of stopping
        # attacks for another full framebuffer transfer and settle delay.
        current = observed_frame
        try:
            label = await charged_prompt_from_frame(current)
        except gbl_vision.VisionOCRError:
            return False
    if label is None:
        return False
    log(device, f"  Charged centre probe at {point} launched ({label})")
    launch_frame = current or frame
    if label != "swipe":
        # OCR already consumed part of GET READY.  A short calibrated delay
        # starts the gesture stream with SWIPE instead of doing another costly
        # screenshot/OCR round trip after bubbles have begun spawning.
        await wait(CHARGED_START_AFTER_GET_READY, use_modifier=False)
        log(device, "  Charged minigame starting from GET READY timing")
    await charged_minigame(device, launch_frame)
    await capture_charged_rating(device)
    return True


def battle_animation(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Reject menu-color false positives while a battle animation is visible."""
    text = f" {gbl_strategy.canonical_name(' '.join(gbl_vision.lines(boxes)))} "
    return any(
        marker in text
        for marker in (
            " used ",
            " attack incoming ",
            " super effective ",
            " not very effective ",
            " no protect shields remaining ",
        )
    )


def matchmaking_status(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """Identify pre-combat matching frames that must never count as battle."""
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    if "finding opponent" in text:
        return "finding opponent"
    if "battle starting" in text:
        return "battle starting"
    return None


def challenge_expired(boxes: list[gbl_vision.OCRBox]) -> bool:
    text = gbl_strategy.canonical_name(" ".join(gbl_vision.lines(boxes)))
    return "challenge expired" in text


async def picker_snapshot(
    device: DeviceAsyncWrapper,
) -> tuple[tuple[int, int, int, bytes], list[gbl_vision.OCRBox]]:
    frame = await read_screen(device)
    if frame is None:
        raise AutoGBLError("Could not read the party picker")
    boxes = await asyncio.to_thread(gbl_vision.recognize, frame_image(frame))
    if not picker_screen(boxes):
        if TRACE_DIR is not None:
            save_trace(device, frame, "picker-guard-failed", None)
        raise AutoGBLError("Party picker OCR guard did not match")
    return frame, boxes


def picker_cancel_point(
    boxes: list[gbl_vision.OCRBox],
    frame,
) -> list[int]:
    """Where the picker's CANCEL button is, found by its own label.

    The mapped fraction below was measured on a taller layout.  On the moto it
    resolves to y=1504 while CANCEL is drawn at y=1406, so every cancel tap
    landed in blank sheet, did nothing, and left the picker open across the
    rest of the run.  Whenever this is called the label itself is on screen,
    so read the button's position instead of assuming it.
    """
    for box in boxes:
        if gbl_vision.normalize(box.text) == "cancel":
            return [box.center_x, box.center_y]
    return [
        int(frame[0] * PICKER_CANCEL_POINT[0]),
        int(frame[1] * PICKER_CANCEL_POINT[1]),
    ]


async def cancel_verified_picker(device: DeviceAsyncWrapper) -> None:
    """Back out of the picker only when OCR proves that it is still open."""
    for _attempt in range(3):
        try:
            frame, boxes = await picker_snapshot(device)
        except (AutoGBLError, gbl_vision.VisionOCRError):
            # The keyboard hides CANCEL/DONE.  Only dismiss it if the visible
            # OCR still proves this is the picker, then retry the footer guard.
            frame = await read_screen(device)
            if frame is None:
                return
            try:
                boxes = await asyncio.to_thread(
                    gbl_vision.recognize, frame_image(frame)
                )
            except gbl_vision.VisionOCRError:
                return
            if not picker_body_screen(boxes):
                return
            await send_input(device, "input keyevent KEYCODE_BACK")
            await wait(PICKER_SETTLE, use_modifier=False)
            continue
        await tap(device, picker_cancel_point(boxes, frame))
        await wait(PICKER_SETTLE, use_modifier=False)


def roster_choice(
    boxes: list[gbl_vision.OCRBox],
    index: dict[str, gbl_meta.PvPMetaPokemon],
    member: gbl_strategy.TeamMember,
) -> gbl_meta.RosterPokemon | None:
    """Find the exact searched owned copy, preferring the recommended CP."""
    wanted = gbl_strategy.canonical_name(member.name)
    matches = [
        item
        for item in gbl_meta.parse_roster_page(boxes, index)
        if gbl_strategy.canonical_name(item.name) == wanted
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: abs(item.cp - member.cp))


async def prepare_best_party(
    device: DeviceAsyncWrapper,
    party_frame,
    party_boxes: list[gbl_vision.OCRBox],
    profile: gbl_strategy.StrategyProfile,
) -> gbl_strategy.StrategyProfile | None:
    """Scan the owned 1500-CP roster and safely install the ranked team.

    The helper is deliberately transactional: open/scan/cancel first, compute
    away from the UI, then reopen and select.  Any failed OCR guard cancels the
    picker and leaves ``USE THIS PARTY`` untouched, so a bad read cannot start
    a battle or spend a pass.
    """
    if not (profile.settings.auto_build_team and profile.settings.auto_select_team):
        return None

    configured = gbl_strategy.team_from_party_ocr(
        party_boxes, (member.name for member in profile.team.members)
    )
    if configured is not None and tuple(
        gbl_strategy.canonical_name(member.name) for member in configured.members
    ) == tuple(
        gbl_strategy.canonical_name(member.name) for member in profile.team.members
    ):
        log(device, "  Visible party already matches the ranked device profile")
        return profile

    slot = [
        int(party_frame[0] * PARTY_SLOT_POINT[0]),
        int(party_frame[1] * PARTY_SLOT_POINT[1]),
    ]
    # The Moto occasionally drops a zero-duration ``input tap`` on a party
    # card.  The same tiny 100 ms gesture used for guarded UI buttons is much
    # more reliable here.
    await wait(0.65, use_modifier=False)
    await tap(device, slot)
    await wait(PICKER_SETTLE, use_modifier=False)

    picker_open = True
    try:
        # The picker persists its previous search across CANCEL.  Clear it
        # before scanning or every page would be the same one-result filter.
        initial_frame = None
        _initial_boxes = None
        for open_attempt in range(3):
            try:
                initial_frame, _initial_boxes = await picker_snapshot(device)
                break
            except AutoGBLError:
                if open_attempt >= 2:
                    raise
                retry_frame = await read_screen(device)
                if retry_frame is None:
                    raise AutoGBLError("Could not verify party screen before picker retry")
                retry_boxes = await asyncio.to_thread(
                    gbl_vision.recognize, frame_image(retry_frame)
                )
                if picker_body_screen(retry_boxes):
                    # The picker is already on its way up. The guard below reads
                    # the party card behind it, and a half-covered card fails
                    # that test -- CHOOSE YOUR PARTY is still visible while USE
                    # THIS PARTY has gone under the rising sheet. Aborting here
                    # left the picker open on the phone with the party unset,
                    # which is a worse place to stand than simply waiting for
                    # the animation to finish.
                    await wait(PICKER_SETTLE, use_modifier=False)
                    continue
                if not party_screen_ocr(retry_boxes):
                    raise AutoGBLError("Picker retry guard no longer shows party screen")
                retry_y = 0.62 if open_attempt == 0 else PARTY_SLOT_POINT[1]
                retry_x = 0.50 if open_attempt == 0 else PARTY_SLOT_POINT[0]
                retry_slot = [
                    int(retry_frame[0] * retry_x),
                    int(retry_frame[1] * retry_y),
                ]
                log(device, f"  Party card did not open; retrying at {retry_slot}")
                await tap(device, retry_slot)
                await wait(PICKER_SETTLE, use_modifier=False)
        if initial_frame is None:
            raise AutoGBLError("Party picker did not open")
        await send_input(
            device,
            "input tap "
            f"{int(initial_frame[0] * PICKER_CLEAR_POINT[0])} "
            f"{int(initial_frame[1] * PICKER_CLEAR_POINT[1])}"
        )
        await wait(0.25, use_modifier=False)
        await send_input(device, "input keyevent KEYCODE_BACK")
        await wait(0.5, use_modifier=False)
        # Back above only hides the IME.  The blank-search Favorites overlay
        # still covers the roster; use its verified in-app arrow to leave it.
        await send_input(
            device,
            "input tap "
            f"{int(initial_frame[0] * PICKER_SEARCH_BACK_POINT[0])} "
            f"{int(initial_frame[1] * PICKER_SEARCH_BACK_POINT[1])}"
        )
        await wait(PICKER_SETTLE, use_modifier=False)
        index = await asyncio.to_thread(
            gbl_meta.load_meta_index,
            gbl_meta.cap_for_league(profile.settings.preferred_league),
        )
        roster: list[gbl_meta.RosterPokemon] = []
        current = gbl_strategy.team_from_party_ocr(
            party_boxes, (meta.name for meta in index.values())
        )
        if current is not None:
            roster.extend(
                gbl_meta.RosterPokemon(member.name, member.cp)
                for member in current.members
            )

        previous: tuple[tuple[str, int], ...] | None = None
        pages = max(1, profile.settings.roster_scan_pages)
        for page in range(pages):
            frame, boxes = await picker_snapshot(device)
            visible = gbl_meta.parse_roster_page(boxes, index)
            roster.extend(visible)
            summary = ", ".join(f"{item.name} {item.cp}" for item in visible)
            log(device, f"  Roster page {page + 1}: {summary or 'no ranked names'}")
            fingerprint = tuple(sorted((item.name, item.cp) for item in visible))
            if fingerprint and fingerprint == previous:
                break
            previous = fingerprint
            if page + 1 < pages:
                x = int(frame[0] * PICKER_SCROLL[0][0])
                await send_input(
                    device,
                    "input swipe "
                    f"{x} {int(frame[1] * PICKER_SCROLL[0][1])} "
                    f"{x} {int(frame[1] * PICKER_SCROLL[1][1])} 550"
                )
                await wait(PICKER_SETTLE, use_modifier=False)

        if len({(item.name, item.cp) for item in roster}) < 3:
            raise AutoGBLError("Roster scan found fewer than three Pokemon")
        always_include = gbl_strategy.active_always_include(
            profile.settings,
            profile.settings.preferred_league,
            gbl_meta.cap_for_league(profile.settings.preferred_league),
        )
        ranked = await asyncio.to_thread(
            gbl_meta.recommend_team,
            roster,
            index,
            always_include=always_include,
        )
        names = " / ".join(member.name for member in ranked.team.members)
        log(device, f"  Ranked party {names} ({ranked.explanation})")

        await cancel_verified_picker(device)
        picker_open = False

        if current is not None and tuple(
            gbl_strategy.canonical_name(member.name) for member in current.members
        ) == tuple(
            gbl_strategy.canonical_name(member.name) for member in ranked.team.members
        ):
            return gbl_strategy.StrategyProfile(ranked.team, profile.settings)

        # Reopen on slot one.  Selecting a result advances the highlighted
        # party position, so the three searches establish lead/safe/closer.
        fresh = await read_screen(device)
        if fresh is None:
            raise AutoGBLError("Could not re-read party screen")
        state, action = read_screen_state(fresh)
        fresh_boxes = await asyncio.to_thread(
            gbl_vision.recognize, frame_image(fresh)
        )
        if state != "pill" or not (
            party_screen(fresh, action) or party_screen_ocr(fresh_boxes)
        ):
            raise AutoGBLError("Party screen guard failed before team selection")
        await send_input(
            device,
            "input tap "
            f"{int(fresh[0] * PARTY_SLOT_POINT[0])} "
            f"{int(fresh[1] * PARTY_SLOT_POINT[1])}"
        )
        await wait(PICKER_SETTLE, use_modifier=False)
        picker_open = True

        for position, member in enumerate(ranked.team.members, start=1):
            frame = await read_screen(device)
            if frame is None:
                raise AutoGBLError("Could not read picker during team selection")
            body_boxes = await asyncio.to_thread(
                gbl_vision.recognize, frame_image(frame)
            )
            if not picker_body_screen(body_boxes):
                raise AutoGBLError("Picker body guard failed during team selection")
            # Pokemon GO remembers the last picker search even after CANCEL,
            # so clear on slot one as well as between subsequent selections.
            await send_input(
                device,
                "input tap "
                f"{int(frame[0] * PICKER_CLEAR_POINT[0])} "
                f"{int(frame[1] * PICKER_CLEAR_POINT[1])}"
            )
            await wait(0.25, use_modifier=False)
            await send_input(
                device,
                "input tap "
                f"{int(frame[0] * PICKER_SEARCH_POINT[0])} "
                f"{int(frame[1] * PICKER_SEARCH_POINT[1])}"
            )
            # Wait for the IME to own the field.  Sending text immediately
            # after the tap is silently dropped on the tall-layout device.
            await wait(1.2, use_modifier=False)
            encoded = member.name.replace(" ", "%s")
            await send_input(device, f"input text {shlex.quote(encoded)}")
            await wait(1.0, use_modifier=False)
            typed_frame = await read_screen(device)
            if typed_frame is None:
                raise AutoGBLError("Could not verify picker search text")
            typed_boxes = await asyncio.to_thread(
                gbl_vision.recognize, frame_image(typed_frame)
            )
            typed_text = gbl_strategy.canonical_name(
                " ".join(gbl_vision.lines(typed_boxes))
            )
            if gbl_strategy.canonical_name(member.name) not in typed_text:
                raise AutoGBLError(
                    f"Search text for {member.name} did not enter the focused field"
                )
            # Select while the keyboard is still visible.  On the tall-layout device, BACK
            # can leave search mode as well as hide the keyboard, restoring
            # the unfiltered roster before the result is tapped.
            choice = roster_choice(typed_boxes, index, member)
            if choice is None:
                visible = gbl_meta.parse_roster_page(typed_boxes, index)
                raise AutoGBLError(
                    f"Search result for {member.name} CP {member.cp} was not verified; "
                    f"visible={[(item.name, item.cp) for item in visible]}"
                )
            log(device, f"  Party slot {position}: {choice.name} CP {choice.cp}")
            await tap(device, [choice.x, choice.y])
            await wait(PICKER_SETTLE, use_modifier=False)

        frame = await read_screen(device)
        if frame is None:
            raise AutoGBLError("Could not read picker before DONE")
        post_boxes = await asyncio.to_thread(
            gbl_vision.recognize, frame_image(frame)
        )
        if picker_screen(post_boxes):
            pass
        elif picker_body_screen(post_boxes):
            await send_input(device, "input keyevent KEYCODE_BACK")
            await wait(PICKER_SETTLE, use_modifier=False)
            frame, post_boxes = await picker_snapshot(device)
        else:
            raise AutoGBLError("Picker guard failed before DONE")
        await tap(
            device,
            [
                int(frame[0] * PICKER_DONE_POINT[0]),
                int(frame[1] * PICKER_DONE_POINT[1]),
            ],
        )
        picker_open = False
        await wait(PICKER_SETTLE, use_modifier=False)

        verified_frame = await read_screen(device)
        if verified_frame is None:
            raise AutoGBLError("Could not verify selected party")
        verified_boxes = await asyncio.to_thread(
            gbl_vision.recognize, frame_image(verified_frame)
        )
        action = find_pill(verified_frame)
        if not (
            party_screen(verified_frame, action)
            or party_screen_ocr(verified_boxes)
        ):
            raise AutoGBLError("Selected-party screen guard failed")
        verified = gbl_strategy.team_from_party_ocr(
            verified_boxes, (member.name for member in ranked.team.members)
        )
        wanted = tuple(
            gbl_strategy.canonical_name(member.name) for member in ranked.team.members
        )
        got = (
            tuple(gbl_strategy.canonical_name(member.name) for member in verified.members)
            if verified is not None
            else ()
        )
        if got != wanted:
            raise AutoGBLError(f"Selected party verification failed: {got or 'no OCR team'}")
        return gbl_strategy.StrategyProfile(ranked.team, profile.settings)
    except (AutoGBLError, gbl_vision.VisionOCRError, OSError, ValueError) as exc:
        log(device, f"  Automatic party preparation stopped safely ({exc})")
        if picker_open:
            await cancel_verified_picker(device)
        return None


async def play_device(
    device: DeviceAsyncWrapper,
    count: int,
    profile: gbl_strategy.StrategyProfile | None = None,
) -> int:
    """Plays up to `count` battles on one phone. Returns how many it finished.

    One loop over the screen rather than a sequence of steps -- see the module
    docstring. `armed` is the safety catch: fast attacks only go out once a pill
    has been tapped *after a league card was selected*, which identifies the
    party screen. A BATTLE or NEXT BATTLE pill alone is not enough to arm fast
    taps, so a slow transition cannot cause taps on an unrelated menu.
    """
    profile = profile or gbl_strategy.load_strategy_profile(device_name=device.label.strip())
    move = device.config['GBL_MOVE_BTN']
    wanted_league = (
        profile.settings.preferred_league if wants_named_league(profile) else None
    )
    # A battle written off partway through is not a battle played.  Counting
    # the two together is what let a leg report a full day it never had: every
    # interruption moved `played` on, and the run finished by arithmetic.  The
    # tally below is only for the report -- `played` still ends the set, since
    # an interrupted battle does spend an entry.
    played = 0
    interrupted = 0
    phantom = 0
    home_seen = 0
    encounter_seen = 0
    game_restarted = False
    armed = False
    attacking = False
    league_confirmed = False
    league_played = False
    league_exits = 0
    league_absent = 0
    unknown = 0
    abandoned = 0
    batches = 0
    deadline = None
    last_tap: tuple[str, int, int] | None = None
    repeats = 0
    stalls = 0
    card_disc_reads = 0
    saw_league = False
    # The end-of-set point this leg is waiting to see the result of, and the
    # tiles that turned out to be locked rather than earned.  Locked is only
    # true of where the row is scrolled to, so the list is dropped whenever the
    # row is reset or the phone leaves the card.
    reward_tap: list[int] | None = None
    locked_rewards: list[list[int]] = []
    reward_scrolls = 0
    # Set when the reward row has had its scrolls and stayed put.
    row_scroll_spent = False
    battle_reads = 0
    saw_result = False
    static = 0
    switch_taps = 0
    signature: tuple | None = None
    memory = gbl_strategy.BattleMemory()
    sheet_seen = False
    pending_reads = 0
    ocr_failed = False
    stray = 0
    party_preparation_attempted = False
    matchmaking_failures = 0
    last_matchmaking_status: str | None = None
    pending_outcome: str | None = None
    prefetched: asyncio.Task | None = None

    while played < count:
        # An end-of-set tap gets exactly this read to show what it opened, so a
        # roster sheet that turns up later is not blamed on a stale point.
        roster_suspect, reward_tap = reward_tap, None
        if prefetched is not None:
            # Read while the last burst was tapping; see where it is started.
            frame = await prefetched
            prefetched = None
        else:
            frame = await read_screen(device)
        if frame is None:
            continue
        try:
            # Full-screen OCR is useful while navigating menus and waiting for
            # matchmaking, but it is far too expensive in the live attack
            # loop.  Once combat is confirmed, the shape detectors handle the
            # common battle frame; OCR is still used when a menu/result shape
            # appears and in the small targeted opponent/prompt crops below.
            quick_state, quick_point = read_screen_state(frame)
            if attacking and quick_state == 'battle':
                state, point = quick_state, quick_point
                vision_label = None
                menu_boxes = []
            else:
                state, point, vision_label, menu_boxes = await smart_screen_state(
                    frame,
                    profile,
                    probe_unknown=not armed,
                    allow_row_scroll=not row_scroll_spent,
                    refused_rewards=locked_rewards,
                )
        except gbl_vision.VisionOCRError as exc:
            if not ocr_failed:
                log(device, f'  Vision OCR unavailable ({exc}); using shape detectors')
                ocr_failed = True
            state, point = read_screen_state(frame)
            vision_label = None
            menu_boxes = []
        if TRACE_DIR is not None:
            save_trace(device, frame, state, point)

        # The black transition frame carries no pill, no cards and no text, so
        # every test falls through to 'battle' -- and an attacking loop reads
        # that as permission to keep tapping into whatever the game draws next.
        # Live, what it drew next was the map: battle one ended, the taps aimed
        # at the battlefield opened the main menu instead, and the run spent its
        # recovery climbing back into GBL. Waiting the fade out costs a few
        # reads and cannot press anything. The battlefield never goes this dark;
        # over 105 traced frames only the two transition frames matched.
        if loading_screen(frame):
            await wait(0.20, use_modifier=False)
            continue

        # Nothing left to play today.  Stopping here is the whole point: the
        # pill is live-looking and the taps go nowhere, so the alternative is
        # the stall budget and then a stop with a misleading reason.
        if not attacking and menu_boxes and daily_cap_reached(menu_boxes):
            log(device, f'Daily battle cap reached; stopping after {played} battle(s)')
            break

        # The rank sheet leaves the card drawn underneath it, so the pill test
        # still finds BATTLE and taps it into a button that is no longer
        # listening.  Named from its words and shut before anything else reads
        # the screen.  Never drawn over a battle, so an attacking leg is left
        # alone.
        if not attacking and menu_boxes and rank_modal_visible(menu_boxes):
            shut = rank_modal_close_point(frame)
            log(device, f'  Rank roster sheet over the card, closing it at {shut}')
            if roster_suspect is not None:
                # The tile just taken opened the roster rather than paying out,
                # and closing the sheet puts the row back exactly as it was, so
                # the same tile goes again on the next read.  That is the loop
                # the moto-g sat in until it was scrolled back by hand.  A point
                # that does not collect is not the reward, whatever the caption
                # said, so write it off and let the row scroll have its turn.
                locked_rewards.append(roster_suspect)
                log(device, f'  The reward tap at {roster_suspect} only opened '
                            'the roster, so it is locked, not earned')
            else:
                # Only an unprompted sheet is progress; one the reward tap just
                # opened has to keep the stall count, or it hides the loop.
                repeats = 0
                last_tap = None
            unknown = 0
            stray = 0
            await tap(device, shut)
            await wait(MENU_SETTLE)
            continue

        if attacking and state != 'battle' and battle_animation(menu_boxes):
            state = 'battle'
            point = None

        # A one-panel white overlay during/resulting from combat is dismissed
        # by the same safe battlefield taps as other result screens. Scrolling
        # is only useful before the party screen has armed the battle loop.
        # 'scroll_back' needs the same guard. Matchmaking draws two tall panels
        # of its own, which measure like an end-of-set page scrolled past its
        # rewards. Live, that misread landed one read after USE THIS PARTY: it
        # disarmed the loop, scrolled the battlefield, and battle 8 was then an
        # unrecognised screen the loop was right to refuse to tap -- it waited
        # out a real battle it could have played, and the run stopped there.
        if state in ('scroll', 'scroll_back', 'tiles') and (armed or attacking):
            state = 'battle'

        if attacking and state == "battle" and teal_result_ready(frame):
            result_boxes = menu_boxes
            if not result_boxes:
                try:
                    result_boxes = await asyncio.to_thread(
                        gbl_vision.recognize, frame_image(frame)
                    )
                except gbl_vision.VisionOCRError:
                    result_boxes = []
            outcome = battle_outcome(result_boxes) or pending_outcome
            pending_outcome = None
            if outcome is not None:
                log(device, f'Battle {played + 1}/{count} over ({outcome})')
            else:
                # Same rule as the other write-off sites: a battle whose result
                # was never read is not evidence the game counted one.
                interrupted += 1
                log(device, f'Battle {played + 1}/{count} over (result OCR unavailable)')
            await tap(device, result_tap_point(frame))
            played += 1
            if played >= count:
                break
            attacking = False
            unknown = 0
            stray = 0
            await wait(POLL_GAP, use_modifier=False)
            continue

        if point is not None:
            here = (state, point[0], point[1])
            same_place = (
                last_tap is not None
                and here[0] == last_tap[0]
                and abs(here[1] - last_tap[1]) <= 5
                and abs(here[2] - last_tap[2]) <= 5
            )
            repeats = repeats + 1 if same_place else 0
            last_tap = here
            if repeats >= STALL_LIMIT:
                # Every state gets the walk back, not just 'league'. The stop
                # below used to sit after a `continue` inside the league
                # branch, so it could never run and neither could recovery for
                # anything else: a stalled action button was detected and then
                # left alone, and the leg tapped a dead point until the
                # supervisor killed it hours later. `repeats` is cleared here
                # for the same reason iOS clears it -- surviving the branch
                # sends every later read straight back into it.
                stalled_at = point
                repeats = 0
                last_tap = None
                stalls += 1
                if stalls > STALL_RECOVERY_LIMIT:
                    log(device, f'  Tapped {stalled_at} {STALL_LIMIT} times with nothing '
                        f'changing, {STALL_RECOVERY_LIMIT} recoveries deep; stopping')
                    break
                log(device, f'  {state} tap at {stalled_at} changed nothing; refreshing '
                    f'the battle flow ({stalls}/{STALL_RECOVERY_LIMIT})')
                await recover_to_gbl(device, frame, menu_boxes, STALL_LIMIT + stalls)
                await wait(MENU_SETTLE)
                continue
        # 'blocked' names something forbidden on screen, 'unconfirmed' a button
        # shape OCR would not vouch for. Neither is pressed where it stands.
        # What they share is that the phone is no longer in the battle flow, and
        # the day's remaining battles are lost unless it can get back, so both
        # go through the same bounded recovery instead of stopping the run.
        if state == 'season_info':
            stray += 1
            if stray >= STRAY_LIMIT:
                log(device, '  Season information did not close; stopping safely')
                break
            armed = False
            await recover_to_gbl(device, frame, menu_boxes, stray)
            await wait(MENU_SETTLE)
            continue
        if state in ('blocked', 'unconfirmed'):
            stray += 1
            if stray == 1:
                detail = f' ({vision_label})' if vision_label else ''
                log(device, f'  Nothing on this screen may be pressed{detail}; leaving it alone')
            if (attacking or armed) and stray < STRAY_SETTLE:
                # Green grass under a broad pill test misreads now and again;
                # a battle in progress is given a few reads to come back before
                # it is written off.  An armed leg gets the same grace for a
                # harder reason: the frames the game draws between "battle
                # starting" and the first battlefield it will vouch for match
                # nothing at all.  Live, one such read walked the android-three off a
                # battle it had already been matched into -- the recovery then
                # tapped the map menu at a phone that was fighting, and the
                # battle ran its clock out untouched while the log said it was
                # recovering.
                await wait(POLL_GAP, use_modifier=False)
                continue
            if attacking:
                played += 1
                interrupted += 1
                log(device, f'Battle {played}/{count} over (interrupted)')
                attacking = False
                if played >= count:
                    break
            armed = False
            if stray >= STRAY_LIMIT:
                shot = save_unknown_frame(device, frame)
                where = f' (frame: {shot})' if shot else ''
                if not game_restarted:
                    # 24 Sep: ph-1 and the moto-g each sat on a screen whose
                    # every recovery tap changed nothing, and each fresh leg
                    # found the same screen and quit on it, until gbl_day gave
                    # up on both phones.  A cold start always lands on the map,
                    # which the walk back to GBL does know.
                    log(device, f'  Off the battle flow for {stray} reads{where}; '
                        'restarting Pokemon GO')
                    game_restarted = True
                    stray = 0
                    await restart_game(device)
                    continue
                log(device, f'  Off the battle flow for {stray} reads{where}; stopping')
                break
            await recover_to_gbl(device, frame, menu_boxes, stray)
            await wait(MENU_SETTLE)
            continue
        stray = 0

        if state != 'battle':
            if armed and not attacking and challenge_expired(menu_boxes):
                matchmaking_failures += 1
                if matchmaking_failures >= 3:
                    log(device, '  Matchmaking expired three times; stopping safely')
                    break
                log(
                    device,
                    f'  Matchmaking expired; retrying ({matchmaking_failures}/3)',
                )
                armed = False
            # Any screen with a button on it means the battle is behind us.
            if attacking:
                outcome = battle_outcome(menu_boxes) or pending_outcome
                pending_outcome = None
                attacking = False
                if outcome is None and battle_reads == 0:
                    # No battlefield was ever read between the party screen and
                    # this one, so there was no battle to be over.  Counting it
                    # spends an entry the phone never played, and five of them
                    # report a finished set from a phone standing still.
                    phantom += 1
                    log(device, '  No battlefield was ever read; not counting that as a battle')
                    armed = False
                    saw_league = False
                    saw_result = False
                    if phantom >= PHANTOM_LIMIT:
                        log(device, f'  {phantom} battles began on a menu screen; stopping')
                        break
                else:
                    played += 1
                    if outcome is None:
                        # No WIN or LOSS was ever read off this battle, so
                        # nothing here says the game counted it.  The day now
                        # ends when the league list is refused, not when this
                        # tally reaches a number, so an uncounted battle costs
                        # one more attempt and a wrongly counted one costs the
                        # rest of the day: the SE reported a full 25 this way
                        # and was still owed battles.
                        interrupted += 1
                    suffix = f" ({outcome})" if outcome else " (result OCR unavailable)"
                    log(device, f'Battle {played}/{count} over{suffix}')
                    if played >= count:
                        break
            unknown = 0
            if state == 'league':
                await wait(MENU_SETTLE)
                settled = await settled_league_point(
                    device, profile, point, vision_label
                )
                if settled is None:
                    log(device, '  League list is still moving; reading it again')
                    continue
                point = settled
                armed = False
                saw_league = True
                saw_result = False
                reward_scrolls = 0
                row_scroll_spent = False
                locked_rewards.clear()
                mismatched = (
                    wanted_league is not None
                    and vision_label is not None
                    and not gbl_strategy.league_matches(wanted_league, vision_label)
                )
                if mismatched:
                    # The chooser had every card on this list to pick from and
                    # still came back with another league, so the configured one
                    # is not on offer today.
                    league_absent += 1
                    if league_absent >= LEAGUE_ABSENT_READS:
                        log(
                            device,
                            f'  {wanted_league} is not on today\'s league list; '
                            f'playing {vision_label} instead',
                        )
                        profile = gbl_strategy.adopt_league(profile, vision_label)
                        # The party this leg may already have built was built to
                        # the old league's CP cap.
                        party_preparation_attempted = False
                        wanted_league = None
                        league_confirmed = True
                detail = f' ({vision_label})' if vision_label else ''
                taking = (
                    'the easiest card'
                    if wanted_league is None or mismatched
                    else f'{wanted_league}'
                )
                log(device, f'  League list, taking {taking} at {point}{detail}')
                await tap(device, point)
                await wait(1.5)
                continue
            elif state == 'orange':
                # End of a set: the rewards have to be taken before the BATTLE
                # pill comes back.
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls = 0
                row_scroll_spent = False
                # Held until the next read says what it opened.
                reward_tap = list(point) if point is not None else None
                log(device, f'  Set finished, taking the reward button at {point}')
            elif state == 'tiles':
                # The reward row is scrolled past the tile this set earned.
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls += 1
                if reward_scrolls > REWARD_SCROLL_LIMIT:
                    # A row that will not move is not a reason to end the day.
                    # Leaving the row alone and pressing on is only any good if
                    # something else on the card pays out; with every tile in
                    # view locked there is nothing to press, and the card comes
                    # back scrolled the same way every read.  Closing it drops
                    # the phone on the map, and walking back in rebuilds the
                    # card with the row at its start, which is where the earned
                    # tile is.
                    row_scroll_spent = True
                    reward_scrolls = 0
                    # The row is about to go back to its start, so what was
                    # locked at these points no longer says anything.
                    locked_rewards.clear()
                    shut = rank_modal_close_point(frame)
                    log(device, '  Reward row will not move; closing the card '
                                f'at {shut} to come back in with it reset')
                    await tap(device, shut)
                    await wait(MENU_SETTLE)
                    continue
                log(
                    device,
                    '  Only locked reward tiles in view; scrolling the row back '
                    f'to the earned one ({reward_scrolls}/{REWARD_SCROLL_LIMIT})',
                )
                await scroll_reward_row(device, frame, point)
                await wait(MENU_SETTLE)
                continue
            elif state in ('scroll', 'scroll_back'):
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls += 1
                if reward_scrolls > REWARD_SCROLL_LIMIT:
                    log(device, '  Reward chooser did not reveal its action button; stopping')
                    break
                if state == 'scroll_back':
                    log(device, '  Completed-set reward is above the fold; scrolling back to it')
                else:
                    log(device, '  Free reward-tier button is below the fold; scrolling it into view')
                await scroll_reward_tiers(device, frame, back=state == 'scroll_back')
                await wait(MENU_SETTLE)
                continue
            else:
                # The party screen is the only pill worth arming on, and there
                # are two ways to reach it: picking a league, or taking NEXT
                # BATTLE off a result screen. Only the first battle of a set
                # comes through the league list, so arming on that alone left
                # battles two to five of every set standing still on the
                # battlefield -- entered, never attacked, lost on the timer.
                #
                # A result screen is the pill with no white sheet behind it:
                # GOOD EFFORT! and VICTORY! draw their button straight onto the
                # battlefield, while every menu pill (the reward tiers, the GBL
                # card, the welcome card) sits on a white panel.
                # Moto's main BATTLE button occupies the same vertical band as
                # USE THIS PARTY.  With OCR available, require the party labels
                # so the main menu cannot arm combat by geometry alone.
                on_party = (
                    party_screen_ocr(menu_boxes)
                    if menu_boxes
                    else party_screen(frame, point)
                )
                if on_party and not party_preparation_attempted:
                    party_preparation_attempted = True
                    prepared = await prepare_best_party(
                        device, frame, menu_boxes, profile
                    )
                    if prepared is not None:
                        profile = prepared
                    # Preparation never taps USE THIS PARTY. Re-read and
                    # re-verify the party screen before the battle can start.
                    continue
                if on_party and wanted_league is not None:
                    # The party screen is the first screen that says which
                    # league the taps actually entered. A stale card position
                    # or a set that was already open both land here, and both
                    # cost five battles in the wrong league if this is taken
                    # on trust.
                    open_league = party_league_name(menu_boxes)
                    if gbl_strategy.league_matches(wanted_league, open_league):
                        # An edition of the wanted league counts as that
                        # league: the card chooser accepts them, and on a day
                        # the plain league is not on the list an edition is the
                        # only way to play it at all.
                        if not league_confirmed:
                            log(device, f'  The open set is {open_league}; playing it')
                        league_confirmed = True
                        league_played = True
                        league_exits = 0
                    elif open_league is not None:
                        league_confirmed = False
                        league_exits += 1
                        if league_played and league_exits > LEAGUE_EXIT_LIMIT_PLAYED:
                            log(
                                device,
                                f'  Could not leave this {open_league} set after '
                                f'{league_exits} tries; stopping rather than '
                                f'playing a league other than {wanted_league}',
                            )
                            break
                        if not league_played and league_exits > LEAGUE_EXIT_LIMIT:
                            # Backing out has stopped working, or every card on
                            # the list leads here. Stopping leaves the day
                            # unplayed; the open set is at least battles.
                            log(
                                device,
                                f'  Could not get back to the league list after '
                                f'{league_exits} tries; playing this {open_league} '
                                f'set rather than none at all',
                            )
                            profile = gbl_strategy.adopt_league(profile, open_league)
                            # The party already built, if any, was built to the
                            # old league's CP cap; read the screen again so it
                            # is rebuilt for this one.
                            party_preparation_attempted = False
                            wanted_league = None
                            league_confirmed = True
                            continue
                        limit = LEAGUE_EXIT_LIMIT_PLAYED if league_played else LEAGUE_EXIT_LIMIT
                        log(
                            device,
                            f'  The open set is {open_league}, not {wanted_league}; '
                            'backing out to the league list '
                            f'({league_exits}/{limit})',
                        )
                        if league_exits % 2 == 0:
                            # The door alone left the moto-g on the same party
                            # screen three times running; Android's own back
                            # key is the other way off it.
                            log(device, '  Pressing Android back instead of the exit door')
                            await send_input(device, 'input keyevent KEYCODE_BACK')
                        else:
                            await leave_to_league_list(device, frame)
                        await wait(MENU_SETTLE)
                        continue
                actual_team = gbl_strategy.team_from_party_ocr(
                    menu_boxes, (member.name for member in profile.team.members)
                )
                if on_party and actual_team is not None:
                    profile = gbl_strategy.StrategyProfile(actual_team, profile.settings)
                    names = ', '.join(member.name for member in actual_team.members)
                    log(device, f'  Verified visible party: {names}')
                armed = on_party or saw_league or saw_result
                saw_league = False
                saw_result = not armed and not league_cards(frame)
                reward_scrolls = 0
                row_scroll_spent = False
                locked_rewards.clear()
                if armed:
                    log(device, f'  Party ready, using it at {point}')
                elif saw_result:
                    log(device, f'  Result screen, taking NEXT BATTLE at {point}')
                else:
                    log(device, f'  Action button at {point}')
            await wait(MENU_SETTLE)
            await tap(device, point)   # type: ignore[arg-type]
            if state == 'league':
                await wait(1.5)
            else:
                await wait(POLL_GAP, use_modifier=False)
            continue

        # GOOD EFFORT!/VICTORY! draw a teal tick and nothing else -- no pill, no
        # white sheet -- so the tests above cannot name the screen and the
        # battle above cannot see that it ended. It is checked before the armed
        # guard because both ways of arriving here have to be handled: mid-set,
        # a battle ends and the loop would otherwise attack the tick until the
        # runaway timer, playing nothing; and at start-up the phone can already
        # be parked on it from a previous run, where the guard below refuses to
        # tap and the set dies at UNKNOWN_LIMIT. The exit door does not exist on
        # this screen, so the recovery scrolls and door taps went nowhere.
        #
        # It is also tested ahead of the charged move, since a teal tick is a
        # high-contrast bottom-centre disc too and that branch would answer it
        # with minigame swipes across the result screen.
        if teal_result_ready(frame):
            result = result_tap_point(frame)
            result_boxes = menu_boxes
            if not result_boxes:
                try:
                    result_boxes = await asyncio.to_thread(
                        gbl_vision.recognize, frame_image(frame)
                    )
                except gbl_vision.VisionOCRError:
                    result_boxes = []
            # The GBL card's close button is the same teal disc, at the same
            # place, as the post-battle checkmark: on the moto both are the
            # phone's own CLOSE_BTN at [359, 1421]. Pressing it here is what
            # ended a live run -- it left GBL for the map after one battle, and
            # everything that followed was the loop tapping at a map. Only the
            # text behind the disc separates them, so where the card is legible
            # the disc is left alone and the card's own BATTLE pill is taken on
            # the next read.
            if gbl_vision.gbl_card_visible(result_boxes):
                card_disc_reads += 1
                escape = gbl_vision.action_point(result_boxes, frame[0], frame[1])
                if card_disc_reads >= CARD_DISC_LIMIT and escape is not None:
                    escape_point, escape_label = escape
                    log(device, f'  GBL card kept its close disc up for '
                                f'{card_disc_reads} reads; taking {escape_label} '
                                f'at {escape_point} instead')
                    card_disc_reads = 0
                    await tap(device, escape_point)
                    await wait(MENU_SETTLE)
                    continue
                log(device, '  Teal disc here is the GBL card close button, not a checkmark; leaving it')
                await wait(POLL_GAP, use_modifier=False)
                continue
            pending_outcome = battle_outcome(result_boxes) or pending_outcome
            detail = f" ({pending_outcome})" if pending_outcome else ""
            log(device, f'  Post-battle checkmark{detail}, dismissing it at {result}')
            unknown = 0
            await tap(device, result)
            await wait(POLL_GAP, use_modifier=False)
            continue

        # Neither a pill nor the league list, so this screen is unrecognised.
        # It is only a battlefield if a party screen was tapped through to get
        # here; otherwise it is a menu this module cannot read, and fast taps
        # would go into whatever it is drawing. The party editor's picker sits
        # one stray tap off the league flow, and taps landing there select
        # Pokemon, so an unrecognised screen has to be waited out, never tapped.
        # The black spinner between BATTLE and the league chooser (and around
        # matchmaking) is a transition, never evidence that combat started; it
        # is waited out at the top of the loop, before anything can attack it.

        # A finished set pays out as a catch screen drawn over the card. Every
        # test above falls through it -- no pill, no cards, no league list --
        # and the tests are right to: there is nothing on it they should press.
        # It is named by its own name plate instead, and answered by the routine
        # that knows the three screens a catch leaves behind.  `armed` does not
        # veto it: a battle that timed out onto the catch screen leaves the loop
        # armed, and the razr then "started" a battle on a reward Rufflet and
        # fast-attacked its Ultra Ball.  No battlefield carries the plate.
        if not attacking and menu_boxes \
                and gbl_vision.encounter_plate(menu_boxes, frame[1]) is not None:
            armed = False
            if await take_reward_encounter(
                    device, spend_balls=abandoned < ABANDONED_ENCOUNTER_LIMIT):
                unknown = 0
                abandoned = 0
                await wait(MENU_SETTLE)
            else:
                abandoned += 1
            continue

        # The shield prompt is the one battlefield screen that names itself, and
        # it covers the discs every other battle test reads. Arming on it is the
        # way back from any misread that dropped the safety catch on the way in
        # -- without it the loop sits out the battle it is standing in, which is
        # exactly how a live run ended after battle 7 with 18 battles unplayed.
        if not armed and not attacking and menu_boxes \
                and shield_prompt_text(menu_boxes):
            shield = await recognized_shield_point(frame)
            log(device, '  Shield prompt: this is a battlefield, arming the loop')
            if shield is not None:
                log(device, f'  Shield offered, taking it at {shield}')
                await tap(device, shield)
            armed = True
            unknown = 0
            await wait(POLL_GAP, use_modifier=False)
            continue

        if not armed:
            unknown += 1
            if unknown >= UNKNOWN_LIMIT:
                shot = save_unknown_frame(device, frame)
                where = f' (frame: {shot})' if shot else ''
                log(device, f'  Unrecognised screen for {unknown} reads; stopping without tapping{where}')
                break
            # The map is the screen a punted run lands on, and it is worth
            # naming before anything else here: walked back it costs two taps,
            # and left unnamed it costs the rest of the day.  Waiting it out is
            # what the reads below do, and the map never stops being the map.
            # The safety warning is worth naming here rather than waiting for a
            # recovery attempt at read 32: it is what a game that restarted
            # mid-leg is sitting behind, and one OK is the whole of the way
            # past it.  Left to the ladder, a phone spent 40 reads tapping map
            # coordinates through it (moto-g, 22 Sep 2026).
            unknown_image = frame_image(frame)
            safety = gbl_vision.safety_notice_point(menu_boxes, unknown_image)
            if safety is not None:
                log(device, f'  Start-up safety warning; pressing OK at {safety}')
                await tap(device, safety)
                await wait(MENU_SETTLE)
                continue
            if gbl_home_recovery.main_menu_open(unknown_image) \
                    or gbl_home_recovery.on_map(unknown_image):
                await walk_home_to_gbl(device, frame)
                await wait(MENU_SETTLE)
                continue
            if unknown <= 2:
                await recover_to_gbl(device, frame, menu_boxes, unknown)
                await wait(MENU_SETTLE)
                continue
            if unknown == 1:
                log(device, '  Unrecognised screen and no party tapped through; waiting, not tapping')
            if unknown % UNKNOWN_SCROLL_EVERY == 0 \
                    and unknown // UNKNOWN_SCROLL_EVERY <= UNKNOWN_SCROLLS:
                log(device, '  Still unrecognised; scrolling back toward the league cards')
                await scroll_reward_tiers(device, frame, back=True)
            elif unknown in UNKNOWN_EXIT_AT:
                door = [int(frame[0] * EXIT_DOOR_X), int(frame[1] * EXIT_DOOR_Y)]
                log(device, f'  Still unrecognised; taking the exit door at {door}')
                await tap(device, door)
            elif unknown >= UNKNOWN_RECOVER_FROM:
                await recover_to_gbl(device, frame, menu_boxes, unknown)
                await wait(MENU_SETTLE)
                continue
            await wait(POLL_GAP, use_modifier=False)
            continue

        if not attacking:
            battle_boxes = menu_boxes
            if not battle_boxes:
                try:
                    battle_boxes = await asyncio.to_thread(
                        gbl_vision.recognize, frame_image(frame)
                    )
                except gbl_vision.VisionOCRError:
                    battle_boxes = []
            match_status = matchmaking_status(battle_boxes)
            if match_status is not None:
                if match_status != last_matchmaking_status:
                    log(device, f'  Matchmaking: {match_status}')
                    last_matchmaking_status = match_status
                await wait(POLL_GAP, use_modifier=False)
                continue
            # `state` is 'battle' only because none of the menu shapes matched:
            # it is a fallback, not a sighting of a battlefield.  A phone parked
            # on the map or on an open main menu fits that description too, and
            # `armed` carries over from the previous screen, so nothing else here
            # stops a standing phone from reporting a set it never played.  Name
            # those two screens and walk back instead.
            image = frame_image(frame)
            if gbl_home_recovery.on_map(image) or gbl_home_recovery.main_menu_open(image):
                log(device, '  Home screen behind that battle read; walking back to GBL')
                armed = False
                saw_league = False
                saw_result = False
                phantom += 1
                if phantom >= PHANTOM_LIMIT:
                    log(device, f'  Punted home {phantom} times without a battle; stopping')
                    break
                await walk_home_to_gbl(device, frame)
                await wait(MENU_SETTLE)
                continue
            attacking = True
            # A battle starting is proof the menus answered; the stall budget
            # is for a phone that is stuck, not one that is slow once.
            stalls = 0
            card_disc_reads = 0
            last_matchmaking_status = None
            batches = 0
            battle_reads = 0
            static = 0
            switch_taps = 0
            signature = None
            memory = gbl_strategy.BattleMemory()
            sheet_seen = False
            pending_reads = 0
            home_seen = 0
            encounter_seen = 0
            deadline = asyncio.get_event_loop().time() + BATTLE_RUNAWAY
            log(device, f'  Battle {played + 1}/{count} started, attacking')

        # An armed loop taps without reading anything back, so a screen the
        # tests cannot name has to be caught by whether it moves at all.
        here_signature = frame_signature(frame)
        static = static + 1 if here_signature == signature else 0
        if static >= BATTLE_STATIC_LIMIT:
            outcome = battle_outcome(menu_boxes) or pending_outcome
            pending_outcome = None
            suffix = f' ({outcome})' if outcome else ' (interrupted)'
            if outcome is None:
                interrupted += 1
            log(device, f'Battle {played + 1}/{count} over{suffix}')
            played += 1
            if played >= count:
                break
            unknown = 0
            stray = 0
            attacking = False
            await recover_to_gbl(device, frame, menu_boxes, static)
            await wait(MENU_SETTLE)
            continue

        if deadline is not None and asyncio.get_event_loop().time() > deadline:
            outcome = battle_outcome(menu_boxes) or pending_outcome
            pending_outcome = None
            suffix = f' ({outcome})' if outcome else ' (interrupted)'
            if outcome is None:
                interrupted += 1
            log(device, f'Battle {played + 1}/{count} over{suffix}')
            played += 1
            if played >= count:
                break
            unknown = 0
            stray = 0
            attacking = False
            await recover_to_gbl(device, frame, menu_boxes, BATTLE_STATIC_LIMIT)
            await wait(MENU_SETTLE)
            continue
        # A battlefield is not the world map.  When the game drops the run home
        # mid-fight nothing below finds a control to press, the map keeps moving
        # so the static test never fires, and the only thing that ends the
        # battle is the runaway timer: five and a half minutes of tapping the
        # map before the recovery walk even begins.  One cheap pixel scan every
        # few reads names the screen instead, and two in a row are required so a
        # transition frame cannot abandon a real battle.  Only the pokeball is
        # looked for: it needs 20 columns of red over white in the bottom fifth
        # of the centre, which a battlefield has nowhere, while the menu's teal
        # rings share a screen region with the reserve portraits and are not
        # worth risking a real battle over.  A phone punted to an open menu is
        # one tap from the map, and the next read catches it there.
        if battle_reads % HOME_CHECK_EVERY == 0:
            try:
                encounter = await encounter_behind_battle(frame)
            except gbl_vision.VisionOCRError:
                encounter = None
            encounter_seen = encounter_seen + 1 if encounter else 0
            if encounter == 'plate' or encounter_seen >= 2:
                # The set is over and paid out.  Stand down and let the next
                # read hand the plate to `take_reward_encounter`.
                outcome = pending_outcome
                pending_outcome = None
                attacking = False
                armed = False
                stray = 0
                played += 1
                if outcome is None:
                    interrupted += 1
                suffix = f' ({outcome})' if outcome else ' (interrupted)'
                encounter_seen = 0
                log(device, f'Battle {played}/{count} over{suffix}, reward catch on screen')
                if played >= count:
                    break
                continue
            image = frame_image(frame)
            if gbl_home_recovery.on_map(image):
                home_seen += 1
            else:
                home_seen = 0
        if home_seen >= 2:
            outcome = battle_outcome(menu_boxes) or pending_outcome
            pending_outcome = None
            attacking = False
            stray = 0
            if outcome is None and battle_reads == 0:
                phantom += 1
                log(device, '  Punted home before any battlefield read; not counting that as a battle')
                if phantom >= PHANTOM_LIMIT:
                    log(device, f'  {phantom} battles began on a menu screen; stopping')
                    break
            else:
                played += 1
                if outcome is None:
                    interrupted += 1
                suffix = f' ({outcome})' if outcome else ' (interrupted)'
                log(device, f'Battle {played}/{count} over{suffix}, back on the home screen')
                if played >= count:
                    break
            armed = False
            home_seen = 0
            await walk_home_to_gbl(device, frame)
            await wait(MENU_SETTLE)
            continue
        # The switch sheet hides every control this loop drives, so nothing else
        # in the battle is worth doing until a reserve has been picked and it is
        # gone. Attacking through it is what stood the Pokemon still.
        if switch_sheet(frame):
            if memory.pending_index is None:
                if memory.opponent_name is None:
                    try:
                        forced_opponent, _not_effective = await recognize_battle_context(frame)
                    except gbl_vision.VisionOCRError:
                        forced_opponent = None
                    if forced_opponent is not None:
                        memory.opponent_name = forced_opponent
                        log(device, f'  Opponent recognized before forced switch: {forced_opponent}')
                memory.begin_forced_switch()
                decision = gbl_strategy.choose_switch(
                    profile.team,
                    memory,
                    memory.opponent_name,
                    profile.settings,
                    forced=True,
                )
                if decision.target_index is not None:
                    memory.request_switch(decision.target_index)
                    log(device, f'  {decision.reason}')
            target = memory.pending_index
            if target is None:
                card = sheet_card_point(frame, switch_taps)
            else:
                slot = gbl_strategy.reserve_sheet_slot(memory, target)
                visible = sum(
                    alive and index != memory.active_index
                    for index, alive in enumerate(memory.alive)
                )
                xf = 0.50 if visible == 1 else (0.32, 0.68)[slot]
                # Moto's forced-switch cards span roughly 0.79-0.87h; 0.90h
                # lands below them and needed repeated taps until timeout.
                card_y = 0.83 if moto_layout(frame) else SHEET_CARD_Y
                card = [int(frame[0] * xf), int(frame[1] * card_y)]
            log(device, f'  Switch sheet up, choosing the planned reserve at {card}')
            switch_taps += 1
            sheet_seen = True
            await tap(device, card)
            await wait(POLL_GAP, use_modifier=False)
            continue
        switch_taps = 0
        if sheet_seen and memory.pending_index is not None:
            chosen = memory.commit_switch()
            log(device, f'  Switched to {profile.team.members[chosen].name}')
            sheet_seen = False
            pending_reads = 0
        elif memory.pending_index is not None:
            pending_reads += 1
            if pending_reads > 3:
                log(device, '  Switch button did not open; cancelling pending switch')
                memory.pending_index = None
                pending_reads = 0

        # The shield prompt covers the fast-attack area, so like the switch
        # sheet it is answered on its own and nothing else is done this read.
        try:
            shield = await recognized_shield_point(frame)
        except gbl_vision.VisionOCRError:
            shield = shield_point(frame)
        if shield is not None:
            log(device, f'  Shield offered, taking it at {shield}')
            await tap(device, shield)
            await wait(POLL_GAP, use_modifier=False)
            continue

        # A charged move can launch from one of the interleaved centre taps
        # after the preceding probe captured too early.  Inspect the current
        # frame before sending any more ordinary taps; this caught the exact
        # live Frustration failure where GET READY was visible but untouched.
        try:
            charged_label = await charged_prompt_from_frame(frame)
        except gbl_vision.VisionOCRError:
            charged_label = None
        if charged_label is not None:
            log(device, f'  Charged sequence recovered from live frame ({charged_label})')
            if charged_label != "swipe":
                await wait(CHARGED_START_AFTER_GET_READY, use_modifier=False)
            await charged_minigame(device, frame)
            await capture_charged_rating(device)
            battle_reads += 1
            continue

        if (
            memory.pending_index is None
            and battle_reads % profile.settings.opponent_ocr_every_reads == 0
            ):
                decision = None
                try:
                    opponent, not_effective = await recognize_battle_context(frame)
                except gbl_vision.VisionOCRError as exc:
                    opponent = None
                    not_effective = False
                    if not ocr_failed:
                        log(device, f'  Opponent OCR unavailable ({exc})')
                        ocr_failed = True
                opponent = opponent or memory.opponent_name
                if opponent is not None:
                    if opponent != memory.opponent_name:
                        memory.opponent_name = opponent
                        log(device, f'  Opponent recognized: {opponent}')
                    if not_effective:
                        log(device, '  Outgoing attack is not very effective; seeking counter')
                    decision = gbl_strategy.choose_switch(
                        profile.team,
                        memory,
                        opponent,
                        profile.settings,
                        urgent=not_effective,
                    )
                if decision is not None and not_effective and decision.target_index is None:
                    log(device, f'  Counter-switch unavailable: {decision.reason}')
                if decision is not None and decision.target_index is not None:
                    target = decision.target_index
                    slot = gbl_strategy.reserve_sheet_slot(memory, target)
                    visible = sum(
                        alive and index != memory.active_index
                        for index, alive in enumerate(memory.alive)
                    )
                    yf = SWITCH_ONLY_Y if visible == 1 else SWITCH_RESERVE_Y[slot]
                    memory.request_switch(target)
                    swap = [
                        int(frame[0] * SWITCH_RESERVE_X),
                        int(frame[1] * yf),
                    ]
                    log(device, f'  {decision.reason}; tapping reserve at {swap}')
                    await tap(device, swap)
                    expected = profile.team.members[target].name
                    active = await confirm_switch(
                        device,
                        tuple(member.name for member in profile.team.members),
                        expected,
                        move_point=move,
                    )
                    if gbl_strategy.canonical_name(active or '') == gbl_strategy.canonical_name(expected):
                        memory.commit_switch()
                        log(device, f'  Verified switch to {expected}')
                    else:
                        failures = memory.record_failed_switch(target)
                        seen = f'read {active}' if active else 'read no team name'
                        log(
                            device,
                            f'  Reserve switch to {expected} was not verified '
                            f'({seen}, attempt {failures})',
                        )
                        if memory.switch_blocked(target):
                            log(
                                device,
                                f'  Giving up on {expected} for this battle; '
                                f'the header never showed it',
                            )
                    continue

        lit = ready_charged_discs(frame)
        if lit:
            # This frame was started alongside the last tap burst, and that
            # burst taps the disc centres itself, so a lit button here is quite
            # often one the phone has already spent. Ask the button row again
            # before committing to the launch: one band read against a whole
            # confirmation budget aimed at a dead point.
            again = await fresh_charged_discs(device, frame)
            if again is not None:
                lit = again
            if not lit:
                log(device, '  Charged move had gone off before the read; '
                            'back to fast attacks')
        if lit:
            column = lit[0]
            charged_target = [
                int(frame[0] * column),
                int(frame[1] * charged_disc_centre_y(frame)),
            ]
            log(device, f'  Charged move ready at {charged_target}; launching')
            launched, launch_frame, attempts, launch_label = await launch_charged_move(
                device, frame, target_point=charged_target, column=column
            )
            if launched:
                seen = launch_label or 'the button going dark'
                log(device, f'  Charged move launch verified after {attempts} '
                            f'tap(s) by {seen}')
                if launch_label == "get ready":
                    await wait(CHARGED_START_AFTER_GET_READY, use_modifier=False)
                    log(device, "  Charged minigame starting from GET READY timing")
                await charged_minigame(device, launch_frame)
                await capture_charged_rating(device)
                battle_reads += 1
                continue
            log(device, '  Charged move tap did not launch; resuming fast attacks')

        # Alternate fast-move space with charged move centres (0.35, 0.65, 0.50).
        # This keeps quick moves firing while probing both charged moves.
        charged_probes = charged_move_points(frame)
        # The burst and the read that follows it cost about the same -- a batch
        # is 1.4s on the android-one against a 0.95s screencap, 0.6s on the android-three
        # against 1.2s -- and the phone taps nothing for the whole of the read.
        # That stare is the pause: half of every cycle spent not attacking.
        # Started here, the read runs alongside the taps and is waiting at the
        # top of the loop, so the loop reaches its decision a whole screencap
        # sooner. The frame is then contemporaneous with the burst rather than
        # after it, which makes it up to a batch older; that is the right trade,
        # because what matters is how long after an event the loop acts on it,
        # and this drops the read out of that path. Anything verifying what a
        # tap *did* -- the charged probe below, switch confirmation -- still
        # takes its own read afterwards and must not use this one.
        prefetched = asyncio.create_task(read_screen(device))
        for _ in range(
            getattr(device, "fast_attack_batches_per_read", MOVE_BATCHES_PER_READ)
        ):
            await fast_attack(device, move, charged_probes)
            batches += 1
        observed_frame = await prefetched
        if observed_frame is not None and await probe_charged_after_fast_attacks(
            device, frame, observed_frame=observed_frame
        ):
            # A minigame has been swiped since that read; the screen behind it
            # is gone.
            await drop_prefetched(prefetched)
            prefetched = None
            battle_reads += 1
            continue
        battle_reads += 1

    if prefetched is not None:
        await drop_prefetched(prefetched)
    detail = f' ({interrupted} of them interrupted)' if interrupted else ''
    log(device, f'Finished: {played} battle(s){detail}')
    return played


async def gbl_process(devices: list[DeviceAsyncWrapper], count: int):
    """Runs a set on every device at once."""
    try:
        await asyncio.gather(*(play_device(device, count) for device in devices))
    finally:
        # A monkey outlives the leg that started it only until its socket goes,
        # so this is tidiness rather than rescue -- but a phone left with a
        # crashed monkey in its log is a phone that looks broken later.
        await asyncio.gather(*(close_monkey(device) for device in devices))


async def probe(devices: list[DeviceAsyncWrapper]):
    """Read-only: prints what the screen tests make of the current screen.

    The calibration tool. Put a phone on a screen, run `probe`, and the numbers
    behind the verdict are printed without a single tap being sent.
    """
    for device in devices:
        frame = await read_screen(device)
        if frame is None:
            log(device, 'Screen unreadable')
            continue
        width, height = frame[0], frame[1]
        profile = gbl_strategy.load_strategy_profile(device_name=device.label.strip())
        try:
            state, point, vision_label, _ = await smart_screen_state(
                frame,
                profile,
                probe_unknown=True,
            )
        except gbl_vision.VisionOCRError as exc:
            state, point = read_screen_state(frame)
            vision_label = f'OCR unavailable: {exc}'
        log(device, f'{width}x{height} -> {state} at {point}')
        if vision_label:
            log(device, f'  Vision decision: {vision_label}')
        log(device, f'  pill: {find_pill(frame)}   orange: {find_orange(frame)}')
        cards = league_cards(frame)
        if cards:
            spans = ', '.join(f'{top}-{bottom} ({(bottom - top) / height:.3f}h)'
                              for top, bottom in cards)
            log(device, f'  white cards: {spans}')
        else:
            log(device, '  white cards: none')
        move = device.config.get('GBL_MOVE_BTN')
        if move:
            red, green, blue = 0.0, 0.0, 0.0
            rgb = pixel(width, frame[2], frame[3], move[0], move[1])
            red, green, blue = rgb
            log(device, f'  GBL_MOVE_BTN {move}: rgb=({red}, {green}, {blue})')


async def get_config(device: DeviceAsyncWrapper) -> CONFIG:
    config_file_path = CONFIG_FILE_DIR + CONFIG_FILE_NAME
    tmp_file_path = Path(TMP_FILE_NAME.format(re.sub(r'\W', '_', device.serial)))
    try:
        await device.pull(config_file_path, tmp_file_path)
        content = tmp_file_path.read_text() if tmp_file_path.exists() else ''
    finally:
        tmp_file_path.unlink(missing_ok=True)
    if not content:
        raise AutoGBLError(f'Found no config file at {config_file_path}')
    config: CONFIG = yaml.safe_load(content)
    assert isinstance(config, dict), 'Incorrect config file (should be an object of keys)'
    if not GBL_KEYS <= set(config.keys()):
        raise AutoGBLError(f'Missing config key(s): {GBL_KEYS - set(config.keys())}')
    for key in GBL_KEYS:
        coords = config[key]
        assert isinstance(coords, list) and len(coords) == 2 \
            and all(isinstance(i, int) for i in coords), \
            f'Invalid coords format for {key} (should be a list of two integers)'
    device.config = config
    return config


async def setup() -> list[DeviceAsyncWrapper]:
    """Finds every connected device and loads a config for each.

    Each carries its own config, because the coordinates are absolute pixels and
    no two models share a screen. A device without the GBL keys is reported and
    dropped -- not a failure of the run, just a phone that has not been mapped.
    """
    client = ClientAsync()
    devices: list[DeviceAsyncWrapper] = await client.devices()
    if not devices:
        raise AutoGBLError('No devices found')
    width = max(len(device.serial) for device in devices)
    ready: list[DeviceAsyncWrapper] = []
    for device in devices:
        device.label = device.serial.ljust(width)
        try:
            await get_config(device)
        except (AutoGBLError, AssertionError, ParserError) as e:
            log(device, 'Skipped:', ' '.join(map(str, e.args)))
            continue
        device.display_id = await find_display_id(device)
        extra = f', reading display {device.display_id}' if device.display_id else ''
        log(device, f'Loaded config, {len(device.config)} keys{extra}')
        await prepare_device_timing(device)
        ready.append(device)
    if not ready:
        raise AutoGBLError('No device has GBL_MOVE_BTN in its config')
    return ready


def run_go(devices: list[DeviceAsyncWrapper], count: int):
    print(f'\nPlaying up to {count} battle(s) per phone (Ctrl+C to stop)...')
    try:
        asyncio.run(gbl_process(devices, count))
    except KeyboardInterrupt:
        print('\nStopped. The phones are left wherever they were.')
    except AutoGBLError as e:
        print('\n'.join(map(str, e.args)))


def interface(auto_go: bool):
    """Runs a loop asking the user for input."""
    global SLEEP_MODIFIER
    print(
        '\n'
        '  ##      ##  \n'
        '##  AutoGBL  ##\n'
        '  ##      ##  \n'
    )
    devices: list[DeviceAsyncWrapper] = asyncio.run(setup())
    print(f"\nRunning on {len(devices)} device(s): {', '.join(d.serial for d in devices)}")
    print('\nCommands:')
    print(f'  go [n]      play n battles per phone (default {BATTLES_PER_SET}, one set);')
    print('              start each phone on the GO BATTLE LEAGUE screen')
    print('  probe       read-only: what the screen tests make of each screen now')
    print('  delay <val> get/set the extra delay modifier')
    print('  q           quit')

    if auto_go:
        run_go(devices, BATTLES_PER_SET)

    while True:
        try:
            i = input('\n> ').strip()
        except EOFError:
            return
        if i in ('q', 'quit', 'exit'):
            return
        if i == 'probe':
            try:
                asyncio.run(probe(devices))
            except KeyboardInterrupt:
                pass
            continue
        if i.startswith('delay'):
            arg = i[5:].strip()
            if arg:
                try:
                    SLEEP_MODIFIER = float(arg)
                except ValueError:
                    print('Enter a number')
                    continue
            print('Delay modifier:', SLEEP_MODIFIER)
            continue
        if i.startswith('go'):
            arg = i[2:].strip()
            count = BATTLES_PER_SET
            if arg:
                try:
                    count = int(arg)
                    assert count > 0
                except (ValueError, AssertionError):
                    print('Enter a positive integer')
                    continue
            run_go(devices, count)
            continue
        if i:
            print('Unknown command')


def main():
    global TRACE_DIR
    args = sys.argv[1:]
    if '--trace' in args:
        # Diagnosis aid: every frame a decision was made on is written out named
        # for that decision, so a misread screen can be looked at afterwards
        # instead of guessed at from the log.
        TRACE_DIR = Path(args[args.index('--trace') + 1])
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        print(f'Tracing frames to {TRACE_DIR}')
    try:
        interface(auto_go='--no-go' not in args)
    except AutoGBLError as e:
        print('\n'.join(map(str, e.args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('gbl', 'gbl.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == '__main__':
    main()
