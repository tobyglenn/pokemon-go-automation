#!/usr/bin/env python3
"""Walk a phone parked on the map back to the GO BATTLE LEAGUE card.

Pokemon GO drops a GBL run back to the map often enough that recovery decides
whether a day's battles get played: a leg that cannot find its way back stands
on the map until the supervisor gives up on it.  The two taps a player makes are
the pokeball at the bottom of the map and the BATTLE disc in the main menu, and
this walks exactly those two, verifying each screen before it presses anything.

The existing recovery in ``gbl_android.recover_to_gbl`` taps blind fractions
that were both measured on the compact-layout device:

* ``MAIN_MENU_POINT = (0.50, 0.883)`` is the compact-layout device's pokeball and nothing
  else's.  The android-three is 1:2.44 against the moto's 1:2.22, which puts its pokeball
  at 0.942 -- 175px below where the fraction lands, and the disc's radius is 72,
  so the tap misses the button outright and the menu never opens.
* ``MAIN_MENU_BATTLE_POINT = (0.50, 0.565)`` is wrong on *every* handset,
  including the compact-layout device.  The menu's BATTLE disc sits in the right-hand column at
  x 0.777; the centre column at that height is the gap between POKEDEX and SHOP.
  The compact-layout device recovers anyway because ``gbl_vision.menu_battle_point`` reads the
  BATTLE label by OCR first and the blind fraction is never reached.

So neither button is assumed here.  Both are distinctive enough to find in the
picture -- the pokeball is the only red-over-white disc at the bottom of the map,
and the menu's discs are the only strongly teal rings on a pale green field --
and a located button is right on a handset nobody has measured.  The measured
fractions per device stay in the configs as a fallback for the case where
detection comes back empty.
"""

from __future__ import annotations

from . import config_paths

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import io
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

from PIL import Image
import yaml

from . import gbl_vision


class HomeRecoveryError(Exception):
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET_CONFIG = config_paths.default_config("pokemon-fleet.yaml")
_ADB = REPO_ROOT / 'adb'
ADB_BINARY = str(_ADB) if _ADB.exists() else 'adb'

SCREENCAP_TIMEOUT = 20  # adb exec-out screencap wedges outright now and then

GAME_PACKAGE = 'com.nianticlabs.pokemongo'
# The launcher intent is what a tap on the icon sends: it brings a backgrounded
# game forward and cold-starts a dead one, so one command covers both.
LAUNCH_INTENT = ('shell', 'monkey', '-p', GAME_PACKAGE,
                 '-c', 'android.intent.category.LAUNCHER', '1')
FOREGROUND_TIMEOUT = 45.0  # a cold start takes its time before it draws
COLD_START_SETTLE = 180.0  # ... and much longer before the map is up

# Config keys a mapped handset carries, used only when detection finds nothing.
BALL_KEY = 'GBL_MENU_BTN'
BATTLE_KEY = 'GBL_MENU_BATTLE_BTN'
# Deliberately not GBL_BATTLE_BTN: that key is the green BATTLE pill on the
# GO BATTLE LEAGUE card (see Moto.yaml), a different button on a different
# screen, and the recovery flow pressing it from the main menu taps whatever
# happens to sit at those coordinates.


# --- The pokeball on the map ---
# Red over white, bottom centre.  Only the centre columns are read: the trainer
# avatar, the nearby-Pokemon bar and the calendar and binoculars discs are all
# white too, and all of them sit out at the edges.  Only the bottom fifth is
# read, which puts the buddy Pokemon -- orange enough to pass a loose red test
# on a Charmander -- above the band.
BALL_BAND_X = (0.35, 0.65)
BALL_BAND_Y = (0.78, 1.00)
BALL_MIN_RED = 10       # px of red in a column before it counts as the ball
BALL_MIN_COLUMNS = 20   # px across, so a red map pin cannot pass for the ball

# --- The main menu's action discs ---
# Each action is a pale disc with a dark teal ring on a light green gradient, and
# the ring is the only strongly teal thing anywhere near it.
MENU_BAND_X = (0.55, 1.00)   # the right-hand column, where BATTLE and ITEMS live
MENU_BAND_Y = (0.25, 0.92)
MENU_DISC_MIN = 0.045   # of screen width; the labels above the discs are thinner
# A GBL battlefield is the other screen this test gets pointed at, and a teal
# Pokemon standing on one passes every shape test above: on the android-one an opponent
# measured 0.45 of the screen across, which is three times the disc it was read
# as.  The menu's discs measure 0.140 of the width on the compact-layout device and 0.141 on the
# android-three, so a cap well above both throws the Pokemon out without coming near a
# real button.
MENU_DISC_MAX = 0.24
MENU_DISC_ASPECT = (0.72, 1.38)
# A disc is a ring: teal around its rim and pale inside, which fills 0.14 of its
# own box on both handsets.  A Pokemon is filled in, and the same opponent
# measured 0.52 to 0.78.  Hollowness is what "ring" means here, and it is the one
# property the battlefield cannot imitate.
MENU_DISC_FILL_MAX = 0.35
MENU_ROW_GAP = 12       # px of clear space that separates a label from its disc
# BATTLE sits directly over ITEMS, so the two discs share an x: 1px apart on the
# compact-layout device and 10px on the android-three.  Two shapes on a battlefield have no reason to
# line up, and the pair that stopped a android-one run stood 170px apart.
MENU_COLUMN_TOLERANCE = 0.04   # of screen width


def _ball_red(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red > 200 and green < 110 and blue < 125


def _near_white(pixel: tuple[int, int, int]) -> bool:
    return min(pixel) > 225 and max(pixel) - min(pixel) < 25


def _menu_teal(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return blue > red + 25 and green > red + 15 and red < 140 and blue > 90


def find_pokeball(image: Image.Image) -> list[int] | None:
    """The map's pokeball, in image pixels, or None when it is not on screen.

    A column belongs to the ball when it holds a run of the ball's red and then
    white below it.  The ball's white lower half is split in two by the grey
    band across its middle, so the bottom edge is taken as the last white pixel
    in the column rather than the end of a contiguous run.
    """
    width, height = image.size
    pixels = image.load()
    x_from, x_to = int(width * BALL_BAND_X[0]), int(width * BALL_BAND_X[1])
    y_from, y_to = int(height * BALL_BAND_Y[0]), height

    columns: dict[int, tuple[int, int]] = {}
    for x in range(x_from, x_to):
        red_top = red_bottom = None
        white_bottom = None
        for y in range(y_from, y_to):
            pixel = pixels[x, y]
            if _ball_red(pixel):
                red_top = y if red_top is None else red_top
                red_bottom = y
            elif _near_white(pixel) and red_bottom is not None:
                white_bottom = y
        if red_top is None or red_bottom - red_top < BALL_MIN_RED:
            continue
        if white_bottom is None:
            continue
        columns[x] = (red_top, white_bottom)

    if not columns:
        return None

    # Widest contiguous run of qualifying columns, so that a red map pin with
    # something white under it cannot outvote the ball.
    runs: list[list[int]] = []
    for x in sorted(columns):
        if runs and x - runs[-1][-1] <= 2:
            runs[-1].append(x)
        else:
            runs.append([x])
    run = max(runs, key=len)
    if len(run) < BALL_MIN_COLUMNS:
        return None

    centre_x = (run[0] + run[-1]) // 2
    nearest = min(run, key=lambda x: abs(x - centre_x))
    red_top, white_bottom = columns[nearest]
    return [centre_x, (red_top + white_bottom) // 2]


def menu_discs(image: Image.Image) -> list[list[int]]:
    """Centres of the main menu's right-hand action discs, top to bottom."""
    width, height = image.size
    pixels = image.load()
    x_from, x_to = int(width * MENU_BAND_X[0]), int(width * MENU_BAND_X[1])
    y_from, y_to = int(height * MENU_BAND_Y[0]), int(height * MENU_BAND_Y[1])

    rows: dict[int, list[int]] = {}
    for y in range(y_from, y_to):
        hits = [x for x in range(x_from, x_to) if _menu_teal(pixels[x, y])]
        if hits:
            rows[y] = hits

    if not rows:
        return []

    # Group the teal rows into bands.  A disc's ring is continuous down its
    # height, so a gap of clear rows separates one disc from the next and from
    # the label above it.
    bands: list[list[int]] = []
    for y in sorted(rows):
        if bands and y - bands[-1][-1] <= MENU_ROW_GAP:
            bands[-1].append(y)
        else:
            bands.append([y])

    minimum = width * MENU_DISC_MIN
    maximum = width * MENU_DISC_MAX
    discs: list[list[int]] = []
    for band in bands:
        xs = [x for y in band for x in rows[y]]
        left, right = min(xs), max(xs)
        top, bottom = band[0], band[-1]
        disc_width, disc_height = right - left, bottom - top
        if disc_width < minimum or disc_height < minimum:
            continue
        if disc_width > maximum or disc_height > maximum:
            continue
        if not MENU_DISC_ASPECT[0] <= disc_width / disc_height <= MENU_DISC_ASPECT[1]:
            continue
        filled = sum(1 for y in band for x in rows[y] if left <= x <= right)
        if filled / ((disc_width * disc_height) or 1) > MENU_DISC_FILL_MAX:
            continue
        discs.append([(left + right) // 2, (top + bottom) // 2])
    return discs


def menu_column(discs: Sequence[Sequence[int]], width: int) -> list[list[int]]:
    """The largest run of discs stacked in one vertical column, top to bottom.

    The menu is a grid, so its discs line up; a battlefield's teal patches do
    not.  Taking the biggest column rather than counting discs is what keeps a
    Pokemon and a reserve portrait from being read together as BATTLE over
    ITEMS -- which is how a android-one mid-battle got walked back to a menu it was
    never on, tapping a portrait once a read until the battle timed out.
    """
    tolerance = width * MENU_COLUMN_TOLERANCE
    best: list[list[int]] = []
    for anchor in discs:
        aligned = [list(disc) for disc in discs if abs(disc[0] - anchor[0]) <= tolerance]
        if len(aligned) > len(best):
            best = aligned
    return sorted(best, key=lambda point: point[1])


def find_menu_battle(image: Image.Image) -> list[int] | None:
    """The main menu's BATTLE disc, in image pixels, or None.

    The menu's right-hand column is BATTLE over ITEMS, in that order on every
    handset, so the upper of the two discs is the one wanted.  Taking the upper
    one by position rather than by its label keeps this working when OCR cannot
    read the label -- which is the case the blind fallback exists for.
    """
    column = menu_column(menu_discs(image), image.size[0])
    if len(column) < 2:
        return None
    return column[0]


def main_menu_open(image: Image.Image) -> bool:
    """Whether the main menu is up, by its own discs rather than by OCR."""
    return len(menu_column(menu_discs(image), image.size[0])) >= 2


def on_map(image: Image.Image) -> bool:
    return find_pokeball(image) is not None


def gbl_card_reached(image: Image.Image) -> bool:
    """Whether the GO BATTLE LEAGUE card is up.

    This screen is all text, so OCR names it far better than any colour test.
    A missing OCR bridge is not a verdict, so the fallback only claims the
    screen when the phone has visibly left both the map and the menu.
    """
    try:
        boxes = gbl_vision.recognize(image)
    except Exception:
        return not on_map(image) and not main_menu_open(image)
    return gbl_vision.gbl_card_visible(boxes)


def gbl_card_confirmed(image: Image.Image) -> bool:
    """The GBL card, and only when OCR positively says so.

    `gbl_card_reached` treats "not the map and not the menu" as the card when
    the OCR bridge is missing.  That is the right guess for a walk that has
    just pressed BATTLE and is asking whether it landed, and the wrong one for
    deciding to do nothing at all: an exit confirmation, a level-up or the
    GO Battle League welcome screen would each be read as a phone that is
    already where it belongs, and the leg after it would find the same screen.
    """
    try:
        boxes = gbl_vision.recognize(image)
    except Exception:
        return False
    return gbl_vision.gbl_card_visible(boxes)


def safety_notice_ok(image: Image.Image) -> list[int] | None:
    """Where the start-up safety warning's OK sits, or None if it is not up."""
    try:
        boxes = gbl_vision.recognize(image)
    except Exception:
        return None
    return gbl_vision.safety_notice_point(boxes, image)


# --- Devices ---

def package_in_front(dumped: str) -> str:
    """The package owning the focused window, per `dumpsys window`, or ''.

    The line reads `mCurrentFocus=Window{326ab68 u0 <package>/<activity>}`, and
    says `null` between apps and on the lock screen, where nothing is focused.
    """
    for line in dumped.splitlines():
        if 'mCurrentFocus' not in line:
            continue
        for token in line.split('=', 1)[1].strip().rstrip('}').split():
            if '/' in token:
                return token.split('/', 1)[0]
    return ''


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    shot: Path | None = None


class Target:
    """One phone, reduced to the four things this walk needs of it."""

    label: str
    # Set by restore() when it had to start the game from nothing, so the walk
    # knows to wait out a title screen rather than a resume.
    restored_cold: bool = False

    async def screenshot(self) -> Image.Image:
        raise NotImplementedError

    async def tap(self, point: list[int]) -> None:
        raise NotImplementedError

    def mapped(self, key: str, image: Image.Image) -> list[int] | None:
        """The config's own point for key, in image pixels, or None."""
        raise NotImplementedError

    async def restore(self) -> bool:
        """Put the game back in front where the platform can.  True if it acted."""
        return False

    async def close(self) -> None:
        """Hand the phone back.

        gbl_day runs this walk between legs, and an Appium session still
        holding an iPhone fails the next leg's own session before it starts.
        """
        return None


class AndroidTarget(Target):
    def __init__(self, label: str, serial: str) -> None:
        self.label = label
        self.serial = serial
        self.display_id: str | None = None
        self.config: dict[str, Any] = {}

    def _adb(self, *arguments: str, binary: bool = False, timeout: float = 30):
        return subprocess.run(
            [ADB_BINARY, '-s', self.serial, *arguments],
            capture_output=True, timeout=timeout,
            text=not binary, check=False,
        )

    async def setup(self) -> None:
        # Foldables report two displays, and screencap with no -d writes a
        # warning onto stdout ahead of the framebuffer, corrupting the PNG.
        listed = self._adb('shell', 'dumpsys', 'SurfaceFlinger', '--display-id').stdout or ''
        ids = [line.split()[1] for line in listed.splitlines() if line.startswith('Display ')]
        if len(ids) >= 2:
            self.display_id = ids[0]
        self.config = await asyncio.to_thread(self._read_handset_config)

    def _read_handset_config(self) -> dict[str, Any]:
        """The handset's own AutoTraderConfig.yaml, which is where GBL reads."""
        result = self._adb('shell', 'cat', '/storage/self/primary/AutoTraderConfig.yaml')
        try:
            loaded = yaml.safe_load(result.stdout or '')
        except yaml.YAMLError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    async def screenshot(self) -> Image.Image:
        arguments = ['exec-out', 'screencap', '-p']
        if self.display_id:
            arguments += ['-d', self.display_id]
        for attempt in range(2):
            try:
                result = await asyncio.to_thread(
                    self._adb, *arguments, binary=True, timeout=SCREENCAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                continue
            if result.stdout[:8] == b'\x89PNG\r\n\x1a\n':
                return Image.open(io.BytesIO(result.stdout)).convert('RGB')
        raise HomeRecoveryError(f'{self.label}: could not read the screen')

    async def tap(self, point: list[int]) -> None:
        x, y = point
        # A tiny swipe over 100 ms for reliability, as in trade.py.
        await asyncio.to_thread(
            self._adb, 'shell', f'input swipe {x} {y} {x + 1} {y + 1} 100')

    def mapped(self, key: str, image: Image.Image) -> list[int] | None:
        value = self.config.get(key)
        if isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value):
            return list(value)
        return None

    def front_package(self) -> str:
        return package_in_front(self._adb('shell', 'dumpsys', 'window').stdout or '')

    def game_running(self) -> bool:
        return bool((self._adb('shell', 'pidof', GAME_PACKAGE).stdout or '').strip())

    async def restore(self) -> bool:
        """Bring Pokemon GO back, whether it is behind something or gone.

        Android can do what iOS will not: a game that has died is restarted
        here rather than left for a human, because the walk below starts from
        the map and a cold start ends on it.  The alternative is a leg that
        taps map coordinates into whatever notification took the screen --
        seen on the moto-g, which sat in Google Messages for an hour with the
        game still running behind it.
        """
        if await asyncio.to_thread(self.front_package) == GAME_PACKAGE:
            return False  # In front already; an unrecognised screen is its own.
        self.restored_cold = not await asyncio.to_thread(self.game_running)
        await asyncio.to_thread(self._adb, *LAUNCH_INTENT)
        deadline = time.monotonic() + FOREGROUND_TIMEOUT
        while await asyncio.to_thread(self.front_package) != GAME_PACKAGE:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_GAP)
        # True either way: the launch was sent, so the caller must judge the
        # screen that follows rather than tap the one it had.
        return True


class IOSTarget(Target):
    def __init__(self, label: str, device: Any) -> None:
        self.label = label
        self.device = device

    async def screenshot(self) -> Image.Image:
        image = await self.device.screenshot()
        if image is None:
            raise HomeRecoveryError(f'{self.label}: could not read the screen')
        return image

    async def tap(self, point: list[int]) -> None:
        # Detection works in screenshot pixels; WebDriverAgent taps in logical
        # points, and on these phones the two differ by the retina scale.
        await self.device.tap(point)

    async def tap_pixel(self, point: list[int], image: Image.Image) -> None:
        await self.device.tap(self.device.logical_point(point, image))

    async def close(self) -> None:
        await self.device.quit()

    def mapped(self, key: str, image: Image.Image) -> list[int] | None:
        value = self.device.config.coordinates.get(key)
        if isinstance(value, list) and len(value) == 2:
            return self.device.image_point(list(value), image)
        return None

    async def restore(self) -> bool:
        # A leg that died took its WebDriverAgent runner down with it and left
        # the phone showing the app switcher, with the game still the active app
        # underneath the overlay.  No amount of tapping the map's buttons reaches
        # the game through that, and the switcher is where recovery most often
        # has to start.
        return await self.device.restore_foreground()


def fleet_devices() -> dict[str, Any]:
    loaded = yaml.safe_load(FLEET_CONFIG.read_text())
    devices = (loaded or {}).get('devices')
    if not isinstance(devices, dict):
        raise HomeRecoveryError(f'{FLEET_CONFIG} has no devices')
    return devices


async def open_target(name: str) -> Target:
    devices = fleet_devices()
    entry = devices.get(name)
    if not isinstance(entry, dict):
        known = ', '.join(sorted(devices))
        raise HomeRecoveryError(f'Unknown device {name!r}; the fleet has: {known}')

    if entry.get('platform') == 'android':
        serial = entry.get('serial')
        if not isinstance(serial, str):
            raise HomeRecoveryError(f'{name} has no serial')
        target = AndroidTarget(name, serial)
        await target.setup()
        return target

    if entry.get('platform') == 'ios':
        from . import gbl_ios
        gbl_config = ((entry.get('operations') or {}).get('gbl') or {}).get('config')
        if not isinstance(gbl_config, str):
            raise HomeRecoveryError(f'{name} has no gbl config')
        runtime = gbl_ios.load_runtime_config(config_paths.find_config(gbl_config, FLEET_CONFIG.parent, "GBL profile"))
        device = await asyncio.to_thread(gbl_ios.IOSGBLDevice.connect, runtime)
        return IOSTarget(name, device)

    raise HomeRecoveryError(f'{name}: unsupported platform {entry.get("platform")!r}')


# --- The walk ---

SETTLE_TIMEOUT = 12.0   # the menu and the GBL card both animate in
POLL_GAP = 1.0


async def _settle(target: Target, predicate, timeout: float | None = None) -> tuple[bool, Image.Image]:
    """Polls until predicate holds, or until it has waited long enough.

    Polled rather than slept through: the menu opens in well under a second on
    the android-three and takes several on the android-one, and a fixed sleep either wastes the
    fast phone's time or reads the slow one mid-animation.
    """
    deadline = time.monotonic() + (SETTLE_TIMEOUT if timeout is None else timeout)
    image = await target.screenshot()
    while True:
        if predicate(image):
            return True, image
        if time.monotonic() >= deadline:
            return False, image
        await asyncio.sleep(POLL_GAP)
        image = await target.screenshot()


def _save(image: Image.Image, shots: Path, name: str) -> Path:
    shots.mkdir(parents=True, exist_ok=True)
    path = shots / f'{name}.png'
    image.save(path)
    return path


async def _press(target: Target, point: list[int], image: Image.Image) -> None:
    if isinstance(target, IOSTarget):
        await target.tap_pixel(point, image)
    else:
        await target.tap(point)


async def _dismiss_safety_notice(target: Target, image: Image.Image) -> tuple[str, Image.Image]:
    """Press OK on the start-up safety warning when that is what is showing.

    Nothing dismisses this card by itself, and the game draws nothing else
    until it goes, so a walk that steps over it is walking over a wall.
    Answers '' when the card was not up at all, which is not a failure.
    """
    point = safety_notice_ok(image)
    if point is None:
        return '', image
    await _press(target, point, image)
    gone, image = await _settle(target, lambda shot: safety_notice_ok(shot) is None)
    return ('cleared' if gone else 'stuck'), image


def _home_or_notice(image: Image.Image) -> bool:
    """Whether the game has drawn anything the walk knows how to leave.

    The OCR is only reached when both cheap pixel tests fail, which during a
    cold start's splash is every poll -- the price of noticing the safety card
    the moment it appears rather than after the whole patience budget.
    """
    return (on_map(image) or main_menu_open(image)
            or safety_notice_ok(image) is not None)


async def recover(target: Target, shots: Path, *, dry_run: bool = False) -> list[Step]:
    """Map -> pokeball -> main menu -> BATTLE -> GO BATTLE LEAGUE."""
    steps: list[Step] = []
    stamp = datetime.now().strftime('%H%M%S')

    image = await target.screenshot()
    path = _save(image, shots, f'{target.label}-{stamp}-0-start')

    # Already where the walk was going.  A leg can come back empty with the
    # card still up -- the daily cap draws a dead BATTLE pill -- and walking a
    # phone that has arrived is not free: the pokeball's coordinates land on the
    # X that closes the card.
    if gbl_card_confirmed(image):
        steps.append(Step('GO BATTLE LEAGUE', True,
                          'GBL card was already up; nothing to walk', path))
        return steps

    # Neither screen recognised means the game may not be the thing on top.
    if not on_map(image) and not main_menu_open(image):
        if await target.restore():
            # A resume draws the map in a second; a cold start runs a splash,
            # a login and a world load first, and the old 12s gave up during
            # the splash and called a game that was coming back a failure.
            patience = COLD_START_SETTLE if target.restored_cold else None
            acted, image = await _settle(target, _home_or_notice, patience)
            path = _save(image, shots, f'{target.label}-{stamp}-0-restored')
            started = 'restarted' if target.restored_cold else 'brought forward'
            steps.append(Step('foreground', acted,
                              f'Pokemon GO {started} and back in front' if acted
                              else 'asked for Pokemon GO, still nothing recognised', path))
            if not acted:
                return steps

    # A cold start ends here rather than on the map: one OK stands between the
    # game and everything this walk does next.  Checked whether or not the
    # restore above ran, because a game that restarted on its own is found
    # sitting on the card with nobody having asked it to.
    outcome, image = await _dismiss_safety_notice(target, image)
    if outcome:
        path = _save(image, shots, f'{target.label}-{stamp}-0-safety')
        steps.append(Step('safety notice', outcome == 'cleared',
                          'pressed OK on the start-up warning' if outcome == 'cleared'
                          else 'pressed OK; the start-up warning is still up', path))
        if outcome == 'stuck':
            return steps

    width, height = image.size

    if dry_run:
        steps.append(Step('dry-run', True,
                          f'{image.size} on_map={on_map(image)} '
                          f'menu_open={main_menu_open(image)} '
                          f'pokeball={find_pokeball(image)} '
                          f'discs={menu_discs(image)}', path))
        return steps

    # A phone already sitting on the menu needs the second tap only: pressing
    # the pokeball there would close the menu and undo the progress.
    if main_menu_open(image):
        steps.append(Step('pokeball', True, 'main menu was already up; skipped', path))
        return steps + await _press_battle(target, image, shots, stamp)

    ball = find_pokeball(image)
    if ball is None:
        # No pokeball, no menu and no card: the game is showing something this
        # walk cannot name, and BALL_KEY is no way out of it.  A mapped point
        # pressed on an unrecognised screen presses whatever happens to sit
        # there, and on the GBL card that spot is the X that closes it -- which
        # is how a phone that had stopped with the card still up got sent to the
        # map by the walk meant to rescue it.  The runners do blind-tap here, on
        # purpose: their alternative is stranding a phone mid-run.  This walk
        # runs between legs, so it can afford to say what it saw and stop.
        steps.append(Step('pokeball', False,
                          'no pokeball, main menu or GBL card on screen; '
                          'leaving this screen alone', path))
        return steps
    steps.append(Step('pokeball', True,
                      f'{ball} found in the picture '
                      f'(fraction {ball[0] / width:.4f}, {ball[1] / height:.4f})', path))

    await _press(target, ball, image)
    opened, image = await _settle(target, main_menu_open)
    if not opened:
        steps.append(Step('main menu', False, 'menu never opened',
                          _save(image, shots, f'{target.label}-{stamp}-1-menu')))
        return steps
    steps.append(Step('main menu', True, 'main menu is up', None))

    return steps + await _press_battle(target, image, shots, stamp)


async def _press_battle(
    target: Target, image: Image.Image, shots: Path, stamp: str,
) -> list[Step]:
    """The second half of the walk: the menu's BATTLE disc, then the GBL card."""
    steps: list[Step] = []
    width, height = image.size
    path = _save(image, shots, f'{target.label}-{stamp}-1-menu')

    battle = find_menu_battle(image)
    source = 'found in the picture'
    if battle is None:
        battle = target.mapped(BATTLE_KEY, image)
        source = f'from {BATTLE_KEY}'
    if battle is None:
        steps.append(Step('battle disc', False,
                          f'no BATTLE disc found and no {BATTLE_KEY} mapped', path))
        return steps
    steps.append(Step('battle disc', True,
                      f'{battle} {source} '
                      f'(fraction {battle[0] / width:.4f}, {battle[1] / height:.4f})', path))

    await _press(target, battle, image)
    reached, image = await _settle(target, gbl_card_reached)
    path = _save(image, shots, f'{target.label}-{stamp}-2-gbl')
    steps.append(Step('GO BATTLE LEAGUE', reached,
                      'GBL card is up' if reached
                      else 'BATTLE did not lead to the GBL card', path))
    return steps


async def recover_device(
    name: str,
    shots: Path,
    *,
    dry_run: bool = False,
) -> tuple[bool, list[Step]]:
    """Walk one fleet device back to the GBL card and let go of it again.

    The release is the point of the wrapper: callers that hold a phone past the
    end of the walk -- gbl_day between legs, above all -- strand the next leg on
    a device that is already claimed.
    """
    target = await open_target(name)
    try:
        steps = await recover(target, shots, dry_run=dry_run)
    finally:
        await target.close()
    return bool(steps) and steps[-1].ok, steps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Walk a phone from the map back to the GO BATTLE LEAGUE card.')
    parser.add_argument('devices', nargs='+',
                        help='fleet keys, e.g. android-three android-two ios-one')
    parser.add_argument('--shots', default=None,
                        help='directory for the per-step screenshots')
    parser.add_argument('--dry-run', action='store_true',
                        help='locate the buttons and report, pressing nothing')
    return parser


async def run(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    shots = Path(args.shots).expanduser() if args.shots else (
        config_paths.state_dir() / 'home-recovery')

    failures = 0
    for name in args.devices:
        print(f'--- {name} ---', flush=True)
        try:
            _, steps = await recover_device(name, shots, dry_run=args.dry_run)
        except HomeRecoveryError as exc:
            print(f'  FAIL  {exc}', flush=True)
            failures += 1
            continue
        for step in steps:
            mark = 'ok  ' if step.ok else 'FAIL'
            print(f'  {mark}  {step.name}: {step.detail}', flush=True)
            if step.shot:
                print(f'        {step.shot}', flush=True)
        if not steps or not steps[-1].ok:
            failures += 1
    return 1 if failures else 0


def main(arguments: Sequence[str] | None = None) -> int:
    return asyncio.run(run(arguments))


if __name__ == '__main__':
    raise SystemExit(main())
