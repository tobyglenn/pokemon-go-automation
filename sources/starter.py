"""Start the day on every attached phone: Mega Evolutions, buddy, gifts, GBL.

Each phone runs four steps in order, side by side with the other phones:

1. mega   -- open the account's "mega" Pokemon tag and Mega Evolve every
             Pokemon whose MEGA EVOLVE button shows no energy cost.  Only one
             Pokemon can be Mega Evolved at a time, so each replaces the last;
             the point is the daily Mega Level progress.
2. buddy  -- open the buddy, feed it up to three berries, take a snapshot and
             pet it until its mood ring is full, then read the profile's
             Give your buddy a treat / Play together / Take a snapshot rows.
3. gifts  -- walk to the friends list and hand the phone to `gift.py`.
4. gbl    -- walk back to the map and hand the phone to `gbl_day.py`.

Steps 1 and 2 find their buttons by the words on screen (Vision OCR) and by
colours measured on the phones, not by per-phone coordinates.  Steps 3 and 4
are the existing commands, run for this one phone.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence

from PIL import Image

from . import config_paths, gbl_home_recovery as home, gbl_vision, gift_android, pokemon_fleet, screen_router

ROOT = Path(__file__).resolve().parent.parent
STEPS = ("mega", "buddy", "bag", "gifts", "trade", "gbl")
# Run on every phone before the pair trades, and after it.
BEFORE_TRADE = {"mega", "buddy", "bag", "gifts"}
AFTER_TRADE = {"gbl"}
OCRBox = gbl_vision.OCRBox

POLL_GAP = 1.0
SETTLE_TIMEOUT = 12.0


# --- Colours and shapes, measured on the oneplus (1080x2412) and ph-1 ---------

# The round close button at the foot of storage, a Pokemon's page, the buddy
# profile and the friends list.  Its fill is the same teal on every one.
CLOSE_FILL = (28, 133, 148)
CLOSE_TOLERANCE = 22
CLOSE_BAND_X = (0.38, 0.62)
CLOSE_BAND_Y = (0.80, 1.00)
CLOSE_MIN_SHARE = 0.0012          # of the screen's pixels; the disc is ~0.3%

# The buddy's mood ring, drawn at the top of the play screen once the buddy
# reacts: six segments, pink when earned and grey when not.
RING_PINK = (239, 57, 129)
RING_BAND_X = (0.38, 0.62)
RING_BAND_Y = (0.0, 0.13)
RING_MIN_SHARE = 0.0004

# A filled heart on the buddy profile's activity rows; empty ones are pale blue.
HEART_PINK = (237, 49, 108)
HEART_BAND_X = (0.66, 0.99)
HEART_ROW_HALF = 0.018            # of screen height, either side of the label
HEART_MIN_SHARE = 0.0006

# The map's buddy portrait sits right of the trainer's avatar.  Several rows
# are tried because the bottom bar's height differs between phones.
BUDDY_PORTRAIT = [(0.235, 0.935), (0.235, 0.92), (0.235, 0.95)]
TRAINER_AVATAR = [(0.11, 0.925), (0.11, 0.91), (0.11, 0.94)]

# Play screen: the berry button bottom-left, the camera bottom-centre and the
# exit door top-left.  Searched for in these bands rather than tapped blind.
BERRY_BUTTON_BAND = ((0.02, 0.28), (0.82, 0.99))
BERRY_FALLBACK = (0.135, 0.898)   # the greyed button's centre (SE, 750x1334: 101, 1198)
CAMERA_BAND = ((0.38, 0.62), (0.82, 0.99))
THUMBNAIL_BAND = ((0.74, 0.99), (0.82, 0.99))
DOOR_BAND = ((0.02, 0.22), (0.02, 0.13))

# The berry is held up in front of the buddy and flicked upward to feed it.
FLICK_FROM = (0.5, 0.62)
FLICK_TO = (0.5, 0.36)
FLICK_SECONDS = 0.15

# How far above its label the berry picker draws the berry, and how far above
# "TAP TO MEGA EVOLVE!" the disc is, as fractions of screen height.
BERRY_ICON_RISE = 0.06
MEGA_DISC_RISE = 0.14

SEARCH_CANCEL = (0.893, 0.604)   # storage search's X above the keyboard (SE)
LEVEL_CARD_CLOSE_Y = 0.953   # the X under "Mega Level increased!" (oneplus)
# The moto-g's evolution animation is slow; 40s ran out on it.
MEGA_WAIT = 75
BERRY_CHIP_X = (0.139, 0.075)   # the X on the berry chip, top-left (moto-g, oneplus)
# Words of cards that lie over the map; their X closes them.  Never their
# USE LUCKY EGG.
# Pages the Poke Ball test also passes for.  Level Requirements (the profile's
# XP bar, opened by a mistimed FRIENDS tap on the SE, 4 Nov) has a teal X where
# the Poke Ball would be, and GBL then took its panels for league cards.
MAP_OVERLAY_WORDS = ("use lucky egg", "new bonuses unlocked", "see details",
                     "level requirements", "caught while using incense")
GOLDEN_RAZZ = "golden razz"   # its tile wraps: "GOLDEN RAZZ" / "BERRY"
GOLDEN_KEEP = 15     # Golden Razz feeds the buddy only above this many
GIFT_BAG_ROUNDS = 6   # bag clears between gift runs before giving up
PET_ROUNDS = 6
PET_STROKES = 6
PET_STROKE_SECONDS = 0.8
MEGA_LIMIT = 60

MEGA_TIMER = re.compile(r"\b\d{1,2}:\d{2}\s*left\b")
DIGITS = re.compile(r"\d")


# --- Pure screen readers ------------------------------------------------------

def near(pixel: Sequence[int], colour: Sequence[int], tolerance: int) -> bool:
    return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(pixel[:3], colour))


def _band(image: Image.Image, xs: tuple[float, float], ys: tuple[float, float]) -> tuple[int, int, int, int]:
    width, height = image.size
    return int(width * xs[0]), int(height * ys[0]), int(width * xs[1]), int(height * ys[1])


def _matching(image: Image.Image, box: tuple[int, int, int, int], test: Callable, step: int = 2) -> list[tuple[int, int]]:
    rgb = image.convert("RGB")
    left, top, right, bottom = box
    pixels = rgb.load()
    return [
        (x, y)
        for y in range(top, bottom, step)
        for x in range(left, right, step)
        if test(pixels[x, y])
    ]


def _centroid(points: list[tuple[int, int]]) -> list[int]:
    return [sum(p[0] for p in points) // len(points), sum(p[1] for p in points) // len(points)]


def _share(points: list[tuple[int, int]], image: Image.Image, step: int = 2) -> float:
    return len(points) * step * step / float(image.width * image.height)


def close_button(image: Image.Image) -> list[int] | None:
    """The centre of the round teal close button at the foot of the screen."""
    points = _matching(image, _band(image, CLOSE_BAND_X, CLOSE_BAND_Y),
                       lambda p: near(p, CLOSE_FILL, CLOSE_TOLERANCE))
    if not points or _share(points, image) < CLOSE_MIN_SHARE:
        return None
    return _centroid(points)


def _dark(pixel: Sequence[int]) -> bool:
    """The door glyph: slate (64,96,96) on the play screen, teal (16,128,144) on the photo screen."""
    r, g, b = pixel[:3]
    return r + g + b < 400 and b >= r


def exit_door(image: Image.Image) -> list[int] | None:
    """The play screen's and photo screen's exit door, top-left."""
    points = _matching(image, _band(image, *DOOR_BAND), _dark)
    if len(points) < 40:
        return None
    return _centroid(points)


def mood_ring(image: Image.Image) -> tuple[int, int] | None:
    """(pink, grey) pixel counts of the buddy's mood ring, or None when it is not up."""
    box = _band(image, RING_BAND_X, RING_BAND_Y)
    pink = _matching(image, box, lambda p: near(p, RING_PINK, 24))
    grey = _matching(image, box, lambda p: 95 <= p[0] <= 150 and abs(p[0] - p[1]) < 10
                     and abs(p[1] - p[2]) < 12)
    if _share(pink, image) + _share(grey, image) < RING_MIN_SHARE:
        return None
    return len(pink), len(grey)


def buddy_excited(image: Image.Image) -> bool:
    """Every segment of the mood ring is pink."""
    ring = mood_ring(image)
    if ring is None:
        return False
    pink, grey = ring
    # The face inside the ring adds a little grey even when every segment is
    # pink: 7% measured full, 66% at four segments of six.
    return pink > 0 and grey <= pink * 0.15


def _berry_red(pixel: Sequence[int]) -> bool:
    r, g, b = pixel[:3]
    return r > 170 and g < 110 and 60 < b < 170


def berry_button(image: Image.Image) -> list[int] | None:
    """The play screen's berry button, found by the berry drawn on it."""
    points = _matching(image, _band(image, *BERRY_BUTTON_BAND), _berry_red)
    if _share(points, image) < 0.0004:
        return None
    return _centroid(points)


def _camera_teal(pixel: Sequence[int]) -> bool:
    r, g, b = pixel[:3]
    return r < 140 and g > 170 and b > 140 and g - r > 70


def camera_button(image: Image.Image) -> list[int] | None:
    """The play screen's camera button, found by its teal camera glyph and ring."""
    points = _matching(image, _band(image, *CAMERA_BAND), _camera_teal)
    if _share(points, image) < 0.0004:
        return None
    return _centroid(points)


def _background(pixel: Sequence[int]) -> bool:
    """The play screen's pale green-to-white backdrop."""
    r, g, b = pixel[:3]
    return g >= 200 and g >= r - 8 and g >= b - 8 and min(r, b) >= 150


def buddy_body(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of whatever is not backdrop between the top icons and the buttons."""
    points = _matching(image, _band(image, (0.05, 0.95), (0.18, 0.80)),
                       lambda p: not _background(p), step=6)
    if len(points) < 30:
        return None
    xs = sorted(p[0] for p in points)
    ys = sorted(p[1] for p in points)
    # Trim the stray 5% at each end: shadows and sparkles.
    cut = len(points) // 20
    return xs[cut], ys[cut], xs[-cut - 1], ys[-cut - 1]


def keyboard_up(boxes: Sequence[OCRBox]) -> bool:
    """An on-screen keyboard: its single-letter keys read as one-letter boxes."""
    letters = {gbl_vision.normalize(box.text) for box in boxes if len(box.text.strip()) == 1}
    return len(letters & set("qwertyuiopasdfghjkl")) >= 8


def text_of(boxes: Iterable[OCRBox]) -> str:
    return " | ".join(gbl_vision.normalize(box.text) for box in boxes)


def find_box(boxes: Iterable[OCRBox], *words: str, exact: bool = False) -> OCRBox | None:
    """The first box whose words are (or contain) any of `words`."""
    for box in boxes:
        label = gbl_vision.normalize(box.text)
        for word in words:
            if (label == word) if exact else (word in label):
                return box
    return None


def storage_open(boxes: Sequence[OCRBox]) -> bool:
    text = text_of(boxes)
    return "tags" in text and "eggs" in text


def tag_title(boxes: Sequence[OCRBox], tag: str) -> OCRBox | None:
    """The tag card's own title on the TAGS page, not a Pokemon nicknamed alike."""
    wanted = gbl_vision.normalize(tag)
    return find_box(boxes, wanted, exact=True)


def tag_grid_count(boxes: Sequence[OCRBox], tag: str) -> int | None:
    """The number under the tag's name at the top of its grid ("Mega" / "19")."""
    title = tag_title(boxes, tag)
    if title is None:
        return None
    below = [
        box for box in boxes
        if re.fullmatch(r"\d{1,4}", gbl_vision.normalize(box.text))
        and 0 < box.center_y - title.center_y < title.height * 3
    ]
    return int(gbl_vision.normalize(below[0].text)) if below else None


def tile_name(text: str) -> str:
    """A grid label as a key: "• Mega Heracross" and "Heracross" are one Pokemon."""
    name = gbl_vision.normalize(text)
    name = name[5:] if name.startswith("mega ") else name
    # OCR reads "Beldum100%" as "beldum 100" one pass and "beldum100" the next.
    return name.replace(" ", "")


# Below its "CP n" plate a tile draws the sprite (the tap) and then the name.
TILE_SPRITE_DROP = 0.058
TILE_NAME_DROP = (0.03, 0.14)
TILE_LOWEST = 0.86        # middle column: further down sits the close button
TILE_LOWEST_SIDE = 0.89   # outer columns, above the sort and new-tag buttons


def grid_tiles(boxes: Sequence[OCRBox], width: int, height: int) -> list[tuple[str, list[int]]]:
    """(name, sprite point) for every whole tile on a storage grid, in reading order."""
    plates = [box for box in boxes if re.match(r"^cp ?\d", gbl_vision.normalize(box.text))]
    tiles = []
    for plate in plates:
        names = [
            box for box in boxes
            if box is not plate
            and abs(box.center_x - plate.center_x) < width * 0.13
            and height * TILE_NAME_DROP[0] < box.center_y - plate.center_y < height * TILE_NAME_DROP[1]
            and not re.match(r"^cp ?\d", gbl_vision.normalize(box.text))
        ]
        point = [plate.center_x, plate.center_y + int(height * TILE_SPRITE_DROP)]
        # The close button covers the middle column's foot; the outer columns
        # stay tappable lower down, which is where the tag's last row ends up.
        lowest = TILE_LOWEST if abs(point[0] - width / 2) < width * 0.12 else TILE_LOWEST_SIDE
        if not names or point[1] > height * lowest:
            continue
        label = min(names, key=lambda box: box.center_y)
        tiles.append((tile_name(label.text), point))
    row = height * 0.04
    return sorted(tiles, key=lambda tile: (round(tile[1][1] / row), tile[1][0]))


def next_tile(tiles: Sequence[tuple[str, list[int]]], done: dict[str, int]) -> tuple[str, list[int]] | None:
    """The first tile not yet handled.  Same-named tiles are told apart by order."""
    shown: dict[str, int] = {}
    for name, point in tiles:
        shown[name] = shown.get(name, 0) + 1
        if shown[name] > done.get(name, 0):
            return name, point
    return None


def detail_page(boxes: Sequence[OCRBox]) -> bool:
    """A Pokemon's own page.  Its Mega Level page also names Mega Energy (and
    offers a LEVEL UP that spends it), so the words here are the page's own."""
    text = text_of(boxes)
    return ("stardust" in text or "power up" in text) and not mega_level_page(boxes)


def mega_level_page(boxes: Sequence[OCRBox]) -> bool:
    """"<name>'s Mega Level": where the Mega Level notice's OK leads."""
    text = text_of(boxes)
    return "to reach max level" in text or ("level up" in text and "mega level" in text)


def mega_active(boxes: Sequence[OCRBox]) -> bool:
    """The page shows "07:59 left" under a Pokemon that is Mega Evolved now."""
    return any(MEGA_TIMER.search(box.text.lower()) for box in boxes)


def species(boxes: Sequence[OCRBox]) -> str:
    """The species, read from the "<SPECIES> MEGA ENERGY" caption."""
    box = find_box(boxes, "mega energy", "primal energy")
    if box is None:
        return "?"
    name = gbl_vision.normalize(box.text).replace("mega energy", "").replace("primal energy", "").strip()
    return name.title() or "?"


@dataclass
class MegaButton:
    point: list[int]
    cost: int | None   # None: free


def mega_button(boxes: Sequence[OCRBox], width: int, height: int) -> MegaButton | None:
    """The MEGA EVOLVE pill and the energy cost printed beside it, if any.

    A free Mega Evolution draws the energy icon with no number; one that costs
    energy prints the amount to the right of the pill on the same row.
    """
    # Kyogre and Groudon draw PRIMAL / REVERSION in the same pill.
    evolve = [box for box in boxes if gbl_vision.normalize(box.text)
              in {"evolve", "mega evolve", "reversion", "primal reversion"}]
    if not evolve:
        return None
    pill = evolve[0]
    # The pill's own MEGA sits straight above EVOLVE.  The grey "Mega" tag chip
    # near the name also reads "mega", so only a box hugging the pill counts.
    above = [
        box for box in boxes
        if gbl_vision.normalize(box.text) in {"mega", "primal"}
        and 0 < pill.y - box.y < pill.height * 2.2
        and abs(box.center_x - pill.center_x) < width * 0.08
    ]
    top = min([pill.y] + [box.y for box in above])
    bottom = pill.y + pill.height
    row_y = (top + bottom) // 2
    reach = max(bottom - top, pill.height * 2)
    cost = None
    for box in boxes:
        label = gbl_vision.normalize(box.text).replace(" ", "")
        if (box.x > pill.x + pill.width and abs(box.center_y - row_y) < reach * 0.6
                and re.fullmatch(r"\d[\d,]*", label)):
            cost = int(label.replace(",", ""))
            break
    return MegaButton([pill.center_x, row_y], cost)


def heart_rows(image: Image.Image, boxes: Sequence[OCRBox]) -> dict[str, bool | None]:
    """Whether each daily row we care about shows a filled heart (None: row not seen)."""
    # The close button can cut a label in two ("Give your bud" / "treat").
    rows = {"treat": ("give your buddy a treat", "give your bud", "a treat"),
            "play": ("play together",), "snapshot": ("take a snapshot",)}
    width, height = image.size
    found: dict[str, bool | None] = {}
    for key, labels in rows.items():
        box = find_box(boxes, *labels)
        if box is None:
            found[key] = None
            continue
        half = int(height * HEART_ROW_HALF)
        region = (int(width * HEART_BAND_X[0]), max(0, box.center_y - half),
                  int(width * HEART_BAND_X[1]), min(height, box.center_y + half))
        pink = _matching(image, region, lambda p: near(p, HEART_PINK, 30))
        found[key] = _share(pink, image) >= HEART_MIN_SHARE
    return found


# --- The phone ------------------------------------------------------------------

class StepFailed(RuntimeError):
    """A step could not reach the screen it needed; the phone's later steps still run."""


class Phone:
    """A fleet phone reduced to look, tap, swipe and back, in screenshot pixels."""

    def __init__(self, name: str, target: home.Target, shots: Path, dry_run: bool = False) -> None:
        self.name = name
        self.target = target
        self.shots = shots
        self.dry_run = dry_run
        self.ios = isinstance(target, home.IOSTarget)

    def say(self, message: str) -> None:
        print(f"[{self.name}] {message}", flush=True)

    async def look(self) -> Image.Image:
        return await self.target.screenshot()

    async def read(self) -> tuple[Image.Image, list[OCRBox]]:
        image = await self.look()
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        return image, boxes

    def save(self, image: Image.Image, label: str) -> Path:
        self.shots.mkdir(parents=True, exist_ok=True)
        path = self.shots / f"{self.name}-{datetime.now():%H%M%S}-{label}.png"
        image.save(path)
        return path

    async def tap(self, point: Sequence[int], image: Image.Image) -> None:
        await home._press(self.target, list(point), image)

    async def tap_box(self, box: OCRBox, image: Image.Image, drop: float = 0.0) -> None:
        await self.tap([box.center_x, box.center_y + int(image.height * drop)], image)

    async def swipe(self, start: Sequence[int], end: Sequence[int], image: Image.Image,
                    seconds: float = 0.4, scroll: bool = False) -> None:
        if self.ios:
            device = self.target.device
            a = device.logical_point(list(start), image)
            b = device.logical_point(list(end), image)
            if scroll:
                await device.scroll(a, b, seconds)
                await self.still()
            else:
                # WDA's drag holds for `duration` before moving; a short hold
                # makes the move itself a stroke or a flick.
                await device.drag(a, b, 0.05)
            return
        x1, y1 = start
        x2, y2 = end
        # A scroll is dragged slowly: a quick one flings the Item Bag on for a
        # second or two after the finger lifts (oneplus), under reads and taps.
        duration = max(seconds, 1.2) if scroll else seconds
        await asyncio.to_thread(
            self.target._adb, "shell", f"input swipe {x1} {y1} {x2} {y2} {int(duration * 1000)}")
        if scroll:
            await self.still()

    async def still(self, timeout: float = 5.0) -> Image.Image:
        """Wait until two looks in a row match: a list has stopped moving."""
        deadline = time.monotonic() + timeout
        previous = None
        while True:
            image = await self.look()
            small = image.convert("L").resize((60, 120))
            if previous is not None:
                diff = sum(abs(a - b) for a, b in zip(small.getdata(), previous.getdata())) / (60 * 120)
                if diff < 1.5 or time.monotonic() >= deadline:
                    return image
            previous = small
            await asyncio.sleep(0.4)

    async def back(self, image: Image.Image) -> bool:
        """Leave the current screen: BACK on Android, the close button or door on iOS."""
        if not self.ios:
            await asyncio.to_thread(self.target._adb, "shell", "input keyevent 4")
            return True
        point = close_button(image) or exit_door(image)
        if point is None:
            return False
        await self.tap(point, image)
        return True

    async def settle(self, predicate: Callable[[Image.Image, list[OCRBox]], bool],
                     timeout: float = SETTLE_TIMEOUT) -> tuple[bool, Image.Image, list[OCRBox]]:
        deadline = time.monotonic() + timeout
        while True:
            image, boxes = await self.read()
            if predicate(image, boxes):
                return True, image, boxes
            if time.monotonic() >= deadline:
                return False, image, boxes
            await asyncio.sleep(POLL_GAP)

    async def ios_keyboard(self) -> bool:
        """The SE's dark keyboard draws no key OCR can read; WDA knows it is up."""
        if not self.ios:
            return False
        try:
            return bool(await asyncio.to_thread(lambda: self.target.device.driver.is_keyboard_shown()))
        except Exception:
            return False

    async def go_home(self) -> Image.Image:
        """Back out to the map; a few tries, then the game's own restore."""
        for attempt in range(7):
            image = await self.look()
            if home.on_map(image):
                # A friendship level-up ("now Great/Ultra/Best/Forever Friends!
                # ... USE LUCKY EGG") lies over the map, which still reads as
                # the map; its X is where the Poke Ball would be (ph-1, 3 Nov).
                boxes = await asyncio.to_thread(gbl_vision.recognize, image)
                if find_box(boxes, *MAP_OVERLAY_WORDS) is None:
                    return image
                self.say("dismissing a card over the map")
                await self.tap(close_button(image) or [image.width // 2, int(image.height * 0.933)], image)
                await asyncio.sleep(2)
                continue
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
            # Cards whose only way off is their own button: the Weekly
            # Challenge (SE, 4 Nov) and similar progress cards say LET'S GO!.
            go = find_box(boxes, "let s go", "lets go") if find_box(boxes, "weekly challenge", "challenge") else None
            if go is not None:
                self.say("pressing LET'S GO! on a challenge card")
                await self.tap_box(go, image)
                await asyncio.sleep(2)
                continue
            # An egg hatching ("Oh?", then the Pokemon) waits for taps, and
            # BACK does nothing to it (oneplus, 4 Nov).
            if find_box(boxes, "oh", exact=True) or find_box(boxes, "hatched"):
                if keyboard_up(boxes) and not self.ios:
                    await asyncio.to_thread(self.target._adb, "shell", "input keyevent 4")
                self.say("an egg is hatching; tapping through it")
                for _ in range(6):
                    await asyncio.sleep(3)
                    await self.tap([image.width // 2, int(image.height * 0.45)], image)
                continue
            if attempt == 3:
                await self.target.restore()
                await asyncio.sleep(3)
                continue
            if attempt in (0, 2):
                # A confirmation card left by an interrupted run covers the
                # close button; its NO is the safe way off it.
                boxes = await asyncio.to_thread(gbl_vision.recognize, image)
                if keyboard_up(boxes) or await self.ios_keyboard():
                    # Storage's search field took focus (seen on the SE); the
                    # keyboard covers the close button until it goes.
                    if self.ios:
                        # WDA refuses hide_keyboard in the game; the grey X
                        # beside the search field cancels it (SE, 375x667).
                        # A hide WDA reports as done can leave it up (4 Nov), so look again.
                        hidden = await self.target.device.hide_keyboard()
                        if hidden:
                            await asyncio.sleep(1)
                            image = await self.look()
                            hidden = not (keyboard_up(await asyncio.to_thread(gbl_vision.recognize, image))
                                          or await self.ios_keyboard())
                        if not hidden:
                            await self.tap([int(image.width * SEARCH_CANCEL[0]),
                                            int(image.height * SEARCH_CANCEL[1])], image)
                    else:
                        await asyncio.to_thread(self.target._adb, "shell", "input keyevent 4")
                        await asyncio.sleep(1.5)
                        if keyboard_up(await asyncio.to_thread(gbl_vision.recognize, await self.look())):
                            # Gboard can stay drawn over the game with nothing
                            # focused, and BACK then reaches the game's exit
                            # prompt instead (oneplus, 4 Nov).  Stopping the
                            # keyboard app is the one thing that took it down.
                            await asyncio.to_thread(self.target._adb, "shell",
                                                    "am force-stop com.google.android.inputmethod.latin")
                    await asyncio.sleep(1.5)
                    continue
                # "Do you want to exit Pokemon GO?" answers to CANCEL; its OK quits.
                no = find_box(boxes, "no", exact=True) or (
                    find_box(boxes, "cancel", exact=True) if find_box(boxes, "exit pokemon go") else None)
                if no is not None and find_box(boxes, "do you") is not None:
                    await self.tap_box(no, image)
                    await asyncio.sleep(1.5)
                    continue
            if home.main_menu_open(image):
                point = close_button(image)
                if point is not None:
                    await self.tap(point, image)
                    await asyncio.sleep(1.5)
                    continue
            if not await self.back(image):
                break
            await asyncio.sleep(2)
        image = await self.look()
        if home.on_map(image):
            return image
        raise StepFailed(f"could not get back to the map (screen saved: {self.save(image, 'not-home')})")

    async def tap_first(self, points: Sequence[tuple[float, float]],
                        reached: Callable[[Image.Image, list[OCRBox]], bool], what: str) -> tuple[Image.Image, list[OCRBox]]:
        """Tap each fractional point from the map until `reached` holds."""
        for fraction in points:
            image = await self.go_home()
            point = [int(image.width * fraction[0]), int(image.height * fraction[1])]
            await self.tap(point, image)
            ok, image, boxes = await self.settle(reached, timeout=8)
            if ok:
                return image, boxes
        raise StepFailed(f"tapping the map never opened {what} (screen saved: {self.save(image, what.replace(' ', '-'))})")


# --- Step 1: Mega Evolutions ----------------------------------------------------

@dataclass
class Report:
    step: str
    ok: bool
    detail: str
    lines: list[str] = field(default_factory=list)


async def open_storage(phone: Phone) -> tuple[Image.Image, list[OCRBox]]:
    for attempt in range(3):
        image = await phone.go_home()
        ball = home.find_pokeball(image)
        if ball is None:
            raise StepFailed("no Poke Ball button on the map")
        await phone.tap(ball, image)
        # The menu animates in: wait for the POKEMON label itself, not just any two.
        ok, image, boxes = await phone.settle(
            lambda _i, b: gbl_vision.main_menu_visible(b) and find_box(b, "pokemon", exact=True) is not None)
        if ok:
            break
        # A tap the map swallowed (ph-1, oneplus, 4 Nov): look again and retry.
        phone.say(f"the main menu did not open; trying again ({attempt + 1}/3)")
    else:
        raise StepFailed(f"the main menu did not open (screen saved: {phone.save(image, 'no-menu')})")
    label = find_box(boxes, "pokemon", exact=True)
    if label is None:
        raise StepFailed("no POKEMON button on the main menu")
    await phone.tap_box(label, image, gbl_vision.MENU_ICON_DROP)
    ok, image, boxes = await phone.settle(lambda _i, b: storage_open(b))
    if not ok:
        raise StepFailed(f"Pokemon storage did not open (screen saved: {phone.save(image, 'no-storage')})")
    return image, boxes


async def open_tag(phone: Phone, tag: str) -> int:
    """Storage -> TAGS -> the tag's grid.  Answers the tag's Pokemon count."""
    image, boxes = await open_storage(phone)
    tags = find_box(boxes, "tags", exact=True)
    if tags is None:
        raise StepFailed("no TAGS tab in storage")
    await phone.tap_box(tags, image)
    title = None
    for _ in range(6):
        ok, image, boxes = await phone.settle(lambda _i, b: tag_title(b, tag) is not None, timeout=4)
        title = tag_title(boxes, tag)
        if title is not None:
            break
        # Tags further down the list: scroll the cards up.
        await phone.swipe([image.width // 2, int(image.height * 0.75)],
                          [image.width // 2, int(image.height * 0.35)], image, 0.6, scroll=True)
        await asyncio.sleep(1.5)
    if title is None:
        raise StepFailed(f"no tag named {tag!r} on the TAGS page "
                         f"(screen saved: {phone.save(image, 'no-tag')})")
    await phone.tap_box(title, image)
    ok, image, boxes = await phone.settle(lambda _i, b: tag_grid(_i, b, tag))
    if not ok:
        raise StepFailed(f"the {tag!r} tag's grid did not open (screen saved: {phone.save(image, 'no-grid')})")
    return tag_grid_count(boxes, tag) or 0


def tag_grid(image: Image.Image, boxes: list[OCRBox], tag: str) -> bool:
    return (tag_grid_count(boxes, tag) is not None
            and bool(grid_tiles(boxes, image.width, image.height)))


async def reveal_mega_button(phone: Phone) -> tuple[Image.Image, list[OCRBox], MegaButton | None]:
    """Scroll the page until MEGA EVOLVE sits clear of the close button."""
    image, boxes = await phone.read()
    for _ in range(3):
        button = mega_button(boxes, image.width, image.height)
        if button is not None and button.point[1] < image.height * 0.82:
            return image, boxes, button
        await phone.swipe([int(image.width * 0.5), int(image.height * 0.80)],
                          [int(image.width * 0.5), int(image.height * 0.55)], image, 0.6, scroll=True)
        await asyncio.sleep(1.5)
        image, boxes = await phone.read()
    return image, boxes, mega_button(boxes, image.width, image.height)


def card_text(boxes: Sequence[OCRBox], question: OCRBox, yes: OCRBox) -> str:
    """The words of a YES/NO card: everything from its question down to YES."""
    top, bottom = question.y - question.height, yes.y + yes.height
    # Nicknames carry digits after the name ("Beldum100%", "Beldum 100%"): not an amount.
    return " ".join(gbl_vision.normalize(re.sub(r"[^\W\d_]+\s?\d+\s?%|[^\W\d_]+\d+", "name", box.text))
                    for box in boxes if top <= box.center_y <= bottom)


def card_spends_energy(text: str) -> bool:
    """A card that names Mega Energy, or carries a bare amount, costs something.

    The free card ("Do you want to Mega Evolve Heracross?", ph-1 3 Nov) shows
    only the two CPs and "lasts for 8 hours"; any other number is a cost.
    """
    if "energy" in text:
        return True
    rest = re.sub(r"\bcp \d+\b|\b\d+ hours?\b", " ", text)
    return re.search(r"\b\d[\d,]*\b", rest) is not None


def replacement_card(text: str) -> bool:
    """The only second card YES is pressed on: the old Mega (or Primal) ends."""
    return ("will no longer be" in text or "primal form" in text) and not card_spends_energy(text)


async def decline(phone: Phone, boxes: Sequence[OCRBox], image: Image.Image) -> None:
    no = find_box(boxes, "no", exact=True) or find_box(boxes, "cancel", exact=True)
    if no is not None:
        await phone.tap_box(no, image)
        await asyncio.sleep(1.5)


async def mega_evolve(phone: Phone, button: MegaButton, image: Image.Image) -> bool:
    await phone.tap(button.point, image)
    # The card fades in: wait until both the question and its YES are read.
    ok, image, boxes = await phone.settle(
        lambda _i, b: find_box(b, "do you want to", "are you sure", "do you really want") is not None
        and find_box(b, "yes", exact=True) is not None)
    if not ok:
        phone.save(image, "mega-no-prompt")
        return False
    question = find_box(boxes, "do you want to", "are you sure", "do you really want")
    yes = find_box(boxes, "yes", exact=True)
    if yes is None or question is None:
        return False
    # Fail closed: the MEGA EVOLVE pill read as free, but if the card itself
    # mentions energy or an amount, the cost number was missed.  Say NO.
    text = card_text(boxes, question, yes)
    if card_spends_energy(text):
        phone.say(f"mega: the card asks for energy, declined (saved {phone.save(image, 'mega-costs')}): {text[:160]}")
        await decline(phone, boxes, image)
        return False
    await phone.tap_box(yes, image)
    # "TAP TO MEGA EVOLVE!" waits for a tap on the disc above it.
    deadline = time.monotonic() + MEGA_WAIT
    frame = 0
    while time.monotonic() < deadline:
        image, boxes = await phone.read()
        if os.environ.get("STARTER_TRACE"):
            frame += 1
            phone.save(image, f"trace-{frame:02d}")
            phone.say("trace: " + " | ".join(box.text for box in boxes)[:300])
        if mega_active(boxes) and detail_page(boxes):
            return True
        # With another Pokemon already Mega Evolved a second card asks "Do you
        # really want to Mega Evolve X? Your Mega Y will no longer be Mega
        # Evolved!" -- replacing it is the point, so YES.
        # Worded "Do you really want to Mega Evolve" or "Are you sure you want
        # to revert ... to its primal form?"; both warn the old one ends.
        # Any other card -- one asking to spend Mega Energy -- gets NO.
        question = find_box(boxes, "do you really want", "are you sure", "do you want to", "will no longer be")
        again = find_box(boxes, "yes", exact=True)
        if question is not None and again is not None:
            text = card_text(boxes, question, again)
            if replacement_card(text):
                await phone.tap_box(again, image)
                await asyncio.sleep(2)
                continue
            phone.say(f"mega: declined a card (saved {phone.save(image, 'mega-declined')}): {text[:160]}")
            await decline(phone, boxes, image)
            return False
        # "Beldum100%'s Mega Level increased! ... SEE DETAILS" follows some
        # evolutions; its X sits at the foot of the card.  Not SEE DETAILS:
        # that page offers LEVEL UP, which spends Mega Energy.
        if find_box(boxes, "mega level") is not None and find_box(boxes, "increased") is not None:
            await phone.tap([image.width // 2, int(image.height * LEVEL_CARD_CLOSE_Y)], image)
            await asyncio.sleep(2)
            continue
        # OCR can fold the prompt into its neighbours ("| 21,9 TAP TO MEGA
        # EVOLVE! 66", ph-1), so match inside the text and tap mid-screen.
        prompt = next((box for box in boxes
                       if "tap to mega evolve" in gbl_vision.normalize(box.text)
                       or "primal reversion" in gbl_vision.normalize(box.text)
                       and "tap to" in gbl_vision.normalize(box.text)), None)
        if prompt is not None:
            await phone.tap([image.width // 2, prompt.center_y - int(image.height * MEGA_DISC_RISE)], image)
        await asyncio.sleep(2)
    phone.save(image, "mega-unfinished")
    return False


async def open_page(phone: Phone, timeout: float = 20) -> tuple[bool, Image.Image, list[OCRBox]]:
    """Wait for a Pokemon's page, pressing OK on notices drawn over it.

    "New Mega Level available! ... OK" covers the page's lower half -- and the
    words this waits for -- until it is dismissed (Houndour100%, oneplus).
    """
    deadline = time.monotonic() + timeout
    while True:
        image, boxes = await phone.read()
        if detail_page(boxes):
            return True, image, boxes
        if mega_level_page(boxes):
            # Never its LEVEL UP: that spends Mega Energy.
            phone.say("mega: leaving the Mega Level page")
            await phone.back(image)
            await asyncio.sleep(2)
            continue
        notice = find_box(boxes, "ok", exact=True)
        if notice is not None and find_box(boxes, "mega level", "primal level") is not None:
            phone.say("mega: pressing OK on a Mega Level notice")
            await phone.tap_box(notice, image)
            await asyncio.sleep(1.5)
            continue
        if time.monotonic() >= deadline:
            return False, image, boxes
        await asyncio.sleep(POLL_GAP)


async def back_to_grid(phone: Phone, tag: str) -> tuple[Image.Image, list[OCRBox]]:
    # The game sometimes sits on a heavy page for over ten seconds and ignores
    # BACK meanwhile (seen on the oneplus), so look before every press: one
    # BACK too many leaves the tag for the TAGS page.
    for _ in range(4):
        image, boxes = await phone.read()
        if tag_grid(image, boxes, tag):
            return image, boxes
        await phone.back(image)
        ok, image, boxes = await phone.settle(lambda _i, b: tag_grid(_i, b, tag), timeout=8)
        if ok:
            return image, boxes
    raise StepFailed(f"could not get back to the {tag!r} grid (screen saved: {phone.save(image, 'no-grid')})")


async def mega_step(phone: Phone, tag: str) -> Report:
    """Open each tile of the tag in turn, from the grid.

    Sideways swipes between Pokemon pages were tried first and do not follow the
    tag's order (Metagross -> Sableye, then stuck), so every Pokemon is opened
    from the grid and the grid is scrolled once its visible tiles are done.
    """
    report = Report("mega", True, "")
    count = await open_tag(phone, tag)
    phone.say(f"mega: the {tag!r} tag holds {count} Pokemon")
    evolved = 0
    done: dict[str, int] = {}
    looked = 0
    image, boxes = await phone.read()
    last_screen: list[str] = []
    while looked < min(count or MEGA_LIMIT, MEGA_LIMIT):
        tiles = grid_tiles(boxes, image.width, image.height)
        chosen = next_tile(tiles, done)
        if chosen is None:
            names = [name for name, _ in tiles]
            if names == last_screen:
                break  # scrolling showed nothing new: the end of the tag
            last_screen = names
            await phone.swipe([image.width // 2, int(image.height * 0.75)],
                              [image.width // 2, int(image.height * 0.42)], image, 0.6, scroll=True)
            await asyncio.sleep(1.5)
            image, boxes = await phone.read()
            continue
        tile, point = chosen
        done[tile] = done.get(tile, 0) + 1
        looked += 1
        grid_image = image
        ok = False
        for _attempt in range(2):
            # A tap straight after BACK can land while the grid is still
            # redrawing and open nothing; one more tap, from a fresh look.
            await phone.tap(point, grid_image)
            ok, image, boxes = await open_page(phone)
            if ok:
                break
            grid_image, grid_boxes = await phone.read()
            if not tag_grid(grid_image, grid_boxes, tag):
                break
            again = next((p for n, p in grid_tiles(grid_boxes, grid_image.width, grid_image.height)
                          if n == tile), None)
            if again is None:
                break
            point = again
        if ok and not any(tile_name(box.text) == tile for box in boxes):
            phone.say(f"mega: tapped {tile}, but the page names someone else "
                      f"(saved {phone.save(image, 'mismatch-' + tile)})")
        if not ok:
            line = f"{tile}: its page did not open"
            report.ok = False
            phone.say(f"mega: {line}")
            report.lines.append(line)
            image, boxes = await back_to_grid(phone, tag)
            continue
        kind = species(boxes)
        name = kind if len(kind) >= 3 else tile.title()   # "CHARIZARD X MEGA ENERGY" splits
        if phone.dry_run:
            phone.save(image, f"tile-{looked:02d}-{tile}")
        if mega_active(boxes):
            line = f"{name}: Mega Evolved already"
        else:
            image, boxes, button = await reveal_mega_button(phone)
            if button is None:
                line = f"{name}: no MEGA EVOLVE button"
            elif button.cost is not None:
                line = f"{name}: costs {button.cost} Mega Energy; skipped"
            elif phone.dry_run:
                line = f"{name}: free; would Mega Evolve (dry run)"
            elif await mega_evolve(phone, button, image):
                evolved += 1
                line = f"{name}: Mega Evolved (free)"
            else:
                line = f"{name}: free, but the Mega Evolution did not finish"
                report.ok = False
        phone.say(f"mega: {line}")
        report.lines.append(line)
        image, boxes = await back_to_grid(phone, tag)
    report.detail = f"{evolved} free Mega Evolution(s) of {looked} Pokemon looked at"
    await phone.go_home()
    return report


# --- Step 2: buddy -----------------------------------------------------------------

def buddy_profile(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return find_box(boxes, "today s activities", "todays activities") is not None


def berry_picker(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return find_box(boxes, "berries", exact=True) is not None


async def play_screen(phone: Phone, timeout: float = 15) -> Image.Image | None:
    """Wait for the play screen's berry and camera buttons to be drawn."""
    deadline = time.monotonic() + timeout
    camera_only = None
    while time.monotonic() < deadline:
        image = await phone.look()
        if berry_button(image) is not None and camera_button(image) is not None:
            return image
        if camera_button(image) is not None and exit_door(image) is not None:
            camera_only = camera_only or time.monotonic()
            # The berry button can draw greyed out (SE, 4 Nov, with 161 Golden
            # Razz in the bag): the play screen is up all the same.
            if time.monotonic() - camera_only >= 4:
                return image
        await asyncio.sleep(POLL_GAP)
    return None


async def feed(phone: Phone, berry: str) -> str | None:
    """One berry, start to finish.  None when fed; otherwise why not."""
    where, image = await feed_ready(phone)
    if where is None:
        return "the play screen's buttons are not up"
    if where == "play":
        await phone.tap(berry_button(image) or [int(image.width * BERRY_FALLBACK[0]),
                                                int(image.height * BERRY_FALLBACK[1])], image)
    # The picker slides up: its header can read before the berry labels are
    # drawn ("ERRIE" and nothing else, oneplus), so wait for the label itself.
    wanted = gbl_vision.normalize(berry)
    golden = gbl_vision.normalize(GOLDEN_RAZZ)
    ok, image, boxes = await phone.settle(
        lambda _i, b: find_box(b, wanted, golden, exact=True) is not None, timeout=8)
    if not ok and not berry_picker(image, boxes):
        return "the berry picker did not open"
    # Golden Razz is spent here and nowhere else, and only while more than
    # GOLDEN_KEEP are left (the operator's rule, 4 Nov).  An unread count
    # spends none.
    label = None
    gold = find_box(boxes, golden, exact=True)
    if gold is not None:
        count = item_count(boxes, gold, image.width, image.height) or badge_count(image, gold)
        if count is not None and count > GOLDEN_KEEP:
            label, berry = gold, "golden razz berry"
            phone.say(f"buddy: {count} Golden Razz in the bag; feeding one")
    if label is None:
        label = find_box(boxes, wanted, exact=True)
    if label is None:
        if not phone.ios:
            await phone.back(image)  # close the picker
        return f"no {berry.title()} in the bag (and Golden Razz not over {GOLDEN_KEEP})"
    await phone.tap_box(label, image, -BERRY_ICON_RISE)
    await asyncio.sleep(2)
    image = await phone.look()
    await phone.swipe([int(image.width * FLICK_FROM[0]), int(image.height * FLICK_FROM[1])],
                      [int(image.width * FLICK_TO[0]), int(image.height * FLICK_TO[1])],
                      image, FLICK_SECONDS)
    # The buttons hide while the buddy eats.  Afterwards the oneplus shows the
    # play screen again and the moto-g reopens the berry picker by itself.
    await asyncio.sleep(2)
    where, _image = await feed_ready(phone, timeout=30)
    if where is None:
        return "the buddy never finished eating"
    return None


async def close_picker(phone: Phone) -> None:
    """Shut a berry picker the game reopened after the last berry (moto-g)."""
    where, image = await feed_ready(phone, timeout=4)
    if where != "picker":
        return
    if phone.ios:
        # The berry chip's X, top-left, ends berry mode and drops the picker.
        await phone.tap([int(image.width * BERRY_CHIP_X[0]), int(image.height * BERRY_CHIP_X[1])], image)
    else:
        await phone.back(image)
    await asyncio.sleep(2)


async def feed_ready(phone: Phone, timeout: float = 15) -> tuple[str | None, Image.Image]:
    """("play" | "picker", screen) once either is up, or (None, last screen)."""
    deadline = time.monotonic() + timeout
    while True:
        image = await phone.look()
        if berry_button(image) is not None and camera_button(image) is not None:
            return "play", image
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        if berry_picker(image, boxes):
            return "picker", image
        if time.monotonic() >= deadline:
            return None, image
        await asyncio.sleep(POLL_GAP)


async def pet(phone: Phone) -> str:
    excited = False
    rounds = 0
    for rounds in range(1, PET_ROUNDS + 1):
        image = await phone.look()
        body = buddy_body(image)
        if body is None:
            return "could not find the buddy on screen"
        left, top, right, bottom = body
        # Head and chest, slowly: what earned the moto-g's Play together heart
        # (0.8s strokes at a third of the way down).  Lower down the strokes
        # also hit a present the buddy may hold at its feet.
        y = top + (bottom - top) // 3
        span = max(right - left, image.width // 5)
        x1 = max(5, (left + right) // 2 - span // 3)
        x2 = min(image.width - 5, (left + right) // 2 + span // 3)
        for stroke in range(PET_STROKES):
            dy = int((stroke % 4 - 1.5) * image.height * 0.012)
            a, b = ([x1, y + dy], [x2, y + dy]) if stroke % 2 == 0 else ([x2, y + dy], [x1, y + dy])
            await phone.swipe(a, b, image, PET_STROKE_SECONDS)
        await asyncio.sleep(0.5)
        if buddy_excited(await phone.look()):
            excited = True
            break
    return f"excited after {rounds} round(s) of petting" if excited else (
        f"petted {rounds} rounds; the mood ring was not seen full")


async def leave_play(phone: Phone) -> None:
    """Exit the play screen, and the photo screen a snapshot opens after it."""
    for _ in range(3):
        image = await phone.look()
        if home.on_map(image):
            return
        # The door, not Android BACK: BACK does nothing on the photo screen a
        # snapshot leads to (oneplus).
        door = exit_door(image)
        if door is None:
            if phone.ios:
                break
            await phone.back(image)
        else:
            await phone.tap(door, image)
        await asyncio.sleep(3)


async def buddy_step(phone: Phone, berries: int, berry: str) -> Report:
    report = Report("buddy", True, "")
    image, boxes = await phone.tap_first(BUDDY_PORTRAIT, buddy_profile, "the buddy profile")
    play = find_box(boxes, "play", exact=True)
    if play is None:
        raise StepFailed("no PLAY! button on the buddy profile")
    await phone.tap_box(play, image)
    image = await play_screen(phone)
    if image is None:
        raise StepFailed(f"the play screen did not open (screen saved: {phone.save(await phone.look(), 'no-play')})")

    if phone.dry_run:
        report.lines.append(
            f"play screen: berry button {berry_button(image)}, camera {camera_button(image)}, "
            f"buddy {buddy_body(image)}; nothing pressed (dry run)")
    else:
        fed = 0
        # A greyed berry button (SE, 4 Nov) still opened the picker on a
        # later try, so it is pressed anyway; feed() says if nothing opens.
        for _ in range(berries):
            problem = await feed(phone, berry)
            if problem:
                report.lines.append(f"berries: stopped after {fed}: {problem}")
                break
            fed += 1
            phone.say(f"buddy: fed {fed}/{berries}")
        else:
            report.lines.append(f"berries: fed {fed}")

        await close_picker(phone)
        image = await play_screen(phone) or await phone.look()
        camera = camera_button(image)
        if camera is None:
            report.lines.append("snapshot: no camera button")
        else:
            await phone.tap(camera, image)
            await asyncio.sleep(3)
            report.lines.append("snapshot: taken")

        report.lines.append("petting: " + await pet(phone))
    for line in report.lines:
        phone.say(f"buddy: {line}")

    await leave_play(phone)
    image, boxes = await phone.tap_first(BUDDY_PORTRAIT, buddy_profile, "the buddy profile")
    hearts = heart_rows(image, boxes)
    for _ in range(3):
        if all(value is not None for value in hearts.values()):
            break
        # On the iPhone SE an Adventure Effect section pushes the rows below
        # the fold and the close button covers one: lift the list, beside the
        # button rather than through it.
        x = int(image.width * 0.2)
        await phone.swipe([x, int(image.height * 0.80)], [x, int(image.height * 0.40)],
                          image, 0.6, scroll=True)
        await asyncio.sleep(1.5)
        image, boxes = await phone.read()
        more = heart_rows(image, boxes)
        hearts = {key: value if value is not None else more[key] for key, value in hearts.items()}
    phone.save(image, "buddy-hearts")
    names = {"treat": "Give your buddy a treat", "play": "Play together", "snapshot": "Take a snapshot"}
    summary = ", ".join(
        f"{names[key]} {'has a heart' if value else ('NO heart' if value is False else 'not read')}"
        for key, value in hearts.items())
    report.ok = all(hearts.values())
    report.detail = summary
    await phone.go_home()
    return report


# --- Step 3: the Item Bag ------------------------------------------------------------

# Everything of these is thrown away.  Kept: Max Potion, Max Revive, Golden Razz
# Berry, Silver Pinap Berry -- and anything not named here.  Names are matched
# whole, and the discard dialog's own title must name the same item before YES,
# so "Pinap Berry" can never take a Silver Pinap Berry with it.
DISCARD_ITEMS = ("potion", "super potion", "hyper potion", "revive",
                 "razz berry", "nanab berry", "pinap berry")
# The trash can sits above-right of an item's name (ph-1, 1316x2560: name
# (234, 892), can (370, 556)).  Both offsets are in screen WIDTHS: the item
# grid is three tiles across on every phone, so it scales with the width; in
# heights it missed on the narrower-looking oneplus.  The grey glyph is then
# looked for near that point.
TRASH_FROM_LABEL = (0.103, -0.255)
TRASH_SEARCH = 0.06
COUNT_PATTERN = re.compile(r"[x×]+\s*(\d[\d,]*)", re.IGNORECASE)
PLUS_ROUNDS = 4    # read-back corrections of the quantity
PLUS_GAP = 0.45   # 0.12s between presses lost some on ph-1
PLUS_AT = (0.731, 0.453)   # the + disc (ph-1, 1316x2560: 962, 1160)
BAG_SCROLLS = 12


def items_page(boxes: Sequence[OCRBox], height: int | None = None) -> bool:
    """The Item Bag, settled: it slides up from the foot, and was read
    mid-slide with its header at 85% of the screen (oneplus)."""
    fill = next((box for box in boxes if re.search(r"\d{3,5}\s*/\s*\d{3,5}", box.text)), None)
    if fill is None:
        return False
    # A full bag draws "Your Item Bag is full. Upgrade Item Bag?" over the
    # ITEMS header; the n/m count still shows (moto-g).  Its upgrade costs
    # coins and is never pressed.
    header = find_box(boxes, "items", exact=True) or (
        fill if find_box(boxes, "item bag is full", "upgrade item bag") else None)
    if header is None:
        return False
    return height is None or header.center_y < height * 0.12


def bag_fill(boxes: Sequence[OCRBox]) -> str:
    return next((box.text.replace(" ", "") for box in boxes if re.search(r"\d{3,5}\s*/\s*\d{3,5}", box.text)), "?")


# Vision sometimes reads the badge in Cyrillic: "хЗ" for "x3" (the SE).
LOOKALIKES = str.maketrans({"х": "x", "Х": "x", "З": "3", "О": "0", "о": "0", "б": "6"})


def badge_text(text: str) -> str:
    return text.strip().translate(LOOKALIKES)


def item_count(boxes: Sequence[OCRBox], label: OCRBox, width: int, height: int) -> int | None:
    """The "x12" printed on the item's tile, above-left of its name."""
    best = None
    for box in boxes:
        match = COUNT_PATTERN.fullmatch(badge_text(box.text))
        if not match:
            continue
        dx = box.center_x - label.center_x
        dy = label.center_y - box.center_y
        if -width * 0.12 < dx < width * 0.04 and 0 < dy < height * 0.09:
            best = int(match.group(1).replace(",", ""))
    return best


# Crops around where a tile's "x2" badge sits, as fractions of the screen
# from the item's name: (left, top, right, bottom).  Vision reads a small badge
# differently at each size (ph-1's Pinap: "x2", "×2 7", "+->", nothing), so
# several looks vote.
# In screen WIDTHS, like the trash can (ph-1: name (238, 894), badge (152, 755)).
BADGE_CROPS = ((-0.120, -0.144, -0.005, -0.076), (-0.134, -0.158, 0.050, -0.060))
BADGE_SCALES = (1, 2, 3, 4)


def badge_count(image: Image.Image, label: OCRBox) -> int | None:
    """The tile's count read from zoomed crops, when whole-screen OCR missed it."""
    from collections import Counter

    width, height = image.size
    votes: Counter[int] = Counter()
    for left, top, right, bottom in BADGE_CROPS:
        box = (max(0, label.center_x + int(width * left)), max(0, label.center_y + int(width * top)),
               min(width, label.center_x + int(width * right)), max(1, label.center_y + int(width * bottom)))
        for scale in BADGE_SCALES:
            crop = image.crop(box)
            crop = crop.resize((crop.width * scale, crop.height * scale))
            try:
                read = gbl_vision.recognize(crop)
            except Exception:
                continue
            # The tight crop holds the badge alone, so a bare number counts
            # there ("x3" read as "3" on the oneplus's Revive).
            pattern = r"[x×]*\s*(\d[\d,]*)" if (left, top) == BADGE_CROPS[0][:2] else r"[x×]+\s*(\d[\d,]*)"
            for found in read:
                match = re.fullmatch(pattern, badge_text(found.text), re.IGNORECASE)
                if match:
                    votes[int(match.group(1).replace(",", ""))] += 1
    if not votes:
        return None
    # One look is enough when no other disagrees: the dialog's read-back
    # checks any count above 1, and a wrong 1 only throws away one of an item
    # that was going anyway.
    ranked = votes.most_common(2)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def discard_targets(boxes: Sequence[OCRBox]) -> list[tuple[str, OCRBox]]:
    found = []
    for box in boxes:
        name = gbl_vision.normalize(box.text)
        if name in DISCARD_ITEMS:
            found.append((name, box))
    return found


def dialog_quantity(boxes: Sequence[OCRBox], title: OCRBox | None,
                    image: Image.Image | None = None,
                    plus_at: Sequence[int] | None = None) -> tuple[int | None, OCRBox | None]:
    """The quantity between - and + under the dialog title, and the + box.

    `plus_at`, where + was found when the dialog opened, stands in when OCR
    stops reading the + itself -- it did after a press on the oneplus.  Whole
    screen OCR also drops a lone small digit, so the field is read again from
    zoomed crops.
    """
    if title is None:
        return None, None
    plus = next((box for box in boxes if box.text.strip() in {"+", "†"} and box.center_y > title.center_y), None)
    if plus is not None:
        px, py = plus.x, plus.center_y
    elif plus_at is not None:
        px, py = plus_at[0] - (image.width // 40 if image is not None else 0), plus_at[1]
    else:
        return None, None
    reach = (py - title.center_y) * 1.5
    for box in boxes:
        text = box.text.strip().lstrip("-–— ").strip()
        if re.fullmatch(r"\d[\d,]*", text) and 0 < box.center_y - title.center_y < reach:
            return int(text.replace(",", "")), plus
    if image is None:
        return None, plus
    width, height = image.size
    for left, half, scale in ((0.45, 0.035, 1), (0.38, 0.03, 2), (0.45, 0.035, 3), (0.30, 0.025, 2)):
        field = image.crop((max(0, px - int(width * left)), max(0, py - int(height * half)),
                            max(1, px - int(width * 0.03)), min(height, py + int(height * half))))
        field = field.resize((field.width * scale, field.height * scale))
        try:
            read = gbl_vision.recognize(field)
        except Exception:
            continue
        for box in read:
            text = box.text.strip().lstrip("-–— ").strip()
            if re.fullmatch(r"\d[\d,]*", text):
                return int(text.replace(",", "")), plus
    return None, plus


def plus_point(boxes: Sequence[OCRBox], title: OCRBox, image: Image.Image) -> list[int]:
    """The dialog's + disc: read when OCR finds it, else where it sat on ph-1."""
    plus = next((box for box in boxes if box.text.strip() in {"+", "†"} and box.center_y > title.center_y), None)
    if plus is not None:
        return [plus.center_x, plus.center_y]
    return [int(image.width * PLUS_AT[0]), int(image.height * PLUS_AT[1])]


def trash_point(image: Image.Image, label: OCRBox) -> list[int]:
    """The item's trash can: the grey glyph near where the grid puts it."""
    width, height = image.size
    guess = [label.center_x + int(width * TRASH_FROM_LABEL[0]),
             label.center_y + int(width * TRASH_FROM_LABEL[1])]
    reach = int(width * TRASH_SEARCH)
    box = (max(0, guess[0] - reach), max(0, guess[1] - reach),
           min(width, guess[0] + reach), min(height, guess[1] + reach))

    def glyph(pixel: Sequence[int]) -> bool:
        r, g, b = pixel[:3]
        return 60 <= r <= 175 and abs(r - g) < 14 and abs(g - b) < 14

    points = _matching(image, box, glyph)
    if len(points) < 12:
        return guess
    return _centroid(points)


async def discard_one(phone: Phone, name: str, label: OCRBox, count: int | None, image: Image.Image) -> str:
    await phone.tap(trash_point(image, label), image)
    wanted = f"discard {name}"
    if not count:
        return f"{name}: the tile's count was not read; left alone"
    ok, image, boxes = await phone.settle(
        lambda _i, b: find_box(b, "discard") is not None and find_box(b, "yes", exact=True) is not None,
        timeout=8)
    if not ok:
        return f"{name}: the discard dialog did not open"
    title = find_box(boxes, "discard")
    no = find_box(boxes, "no", exact=True)
    if gbl_vision.normalize(title.text) != wanted:
        if no is not None:
            await phone.tap_box(no, image)
        return f"{name}: the dialog said {title.text!r}; pressed NO"
    # The field starts at 1 and + WRAPS past the most you hold (ph-1: 5 -> 1
    # -> 2), so it is pressed exactly count - 1 times, slowly enough for each
    # press to land, and read back before YES.  A lone "1" often does not
    # read at all; with nothing pressed that is the starting value.
    # The dialog zooms in; a press during that lands outside it and closes it
    # (oneplus).  Wait for a still frame and read the + from it.
    image = await phone.still()
    boxes = await asyncio.to_thread(gbl_vision.recognize, image)
    title = find_box(boxes, "discard") or title
    if gbl_vision.normalize(title.text) != wanted:
        return f"{name}: the dialog went before any press; left alone"
    quantity = 1
    presses = count - 1
    plus = plus_point(boxes, title, image)
    for _ in range(PLUS_ROUNDS):
        for _tap in range(presses):
            await phone.tap(plus, image)
            await asyncio.sleep(PLUS_GAP)
        await asyncio.sleep(0.8)
        read = None
        for _look in range(3):
            image, boxes = await phone.read()
            read, _plus = dialog_quantity(boxes, find_box(boxes, "discard") or title, image, plus)
            if read is not None:
                break
            await asyncio.sleep(0.5)
        quantity = read
        if quantity is None or quantity == count:
            break
        presses = (count - quantity) % count
    # The title is the safety: only the named item is touched, and all of it
    # is meant to go.  An unread field is trusted to the exact presses, and a
    # field that never reached the count still throws away part of it.
    unread = " (quantity not read back)" if quantity is None else ""
    if quantity is not None and quantity != count:
        unread = f" (quantity {quantity}, tile said {count})"
    held = (quantity or count)
    yes = find_box(boxes, "yes", exact=True)
    no = find_box(boxes, "no", exact=True) or no
    if phone.dry_run or yes is None:
        if no is not None:
            await phone.tap_box(no, image)
        if yes is None:
            return (f"{name}: YES not read after the presses; pressed NO "
                    f"(screen saved: {phone.save(image, 'bag-' + name.replace(' ', '-'))})")
        return f"{name}: would discard {held} (dry run){unread}"
    await phone.tap_box(yes, image)
    await asyncio.sleep(2.5)
    return f"{name}: discarded {held}{unread}"


async def open_items(phone: Phone) -> tuple[Image.Image, list[OCRBox]]:
    image = await phone.go_home()
    ball = home.find_pokeball(image)
    if ball is None:
        raise StepFailed("no Poke Ball button on the map")
    await phone.tap(ball, image)
    ok, image, boxes = await phone.settle(
        lambda _i, b: gbl_vision.main_menu_visible(b) and find_box(b, "items", exact=True) is not None)
    if not ok:
        raise StepFailed("the main menu did not open")
    await phone.tap_box(find_box(boxes, "items", exact=True), image, gbl_vision.MENU_ICON_DROP)
    ok, image, boxes = await phone.settle(lambda _i, b: items_page(b, _i.height))
    if not ok:
        raise StepFailed(f"the Item Bag did not open (screen saved: {phone.save(image, 'no-items')})")
    return image, boxes


async def bag_step(phone: Phone) -> Report:
    report = Report("bag", True, "")
    image, boxes = await open_items(phone)
    before = bag_fill(boxes)
    done: set[str] = set()
    nudged: set[str] = set()
    last_screen = ""
    for _ in range(BAG_SCROLLS + len(DISCARD_ITEMS)):
        # A discard closes the item's tile and the grid slides up into the gap;
        # a frame from mid-slide put Nanab's trash can on Revive's (oneplus).
        image = await phone.still()
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        targets = [(name, box) for name, box in discard_targets(boxes) if name not in done]
        if targets:
            name, label = targets[0]
            count = item_count(boxes, label, image.width, image.height) or badge_count(image, label)
            can = trash_point(image, label)
            edge = not (image.height * 0.13 < can[1] < image.height * 0.86) or label.center_y > image.height * 0.93
            if (not count or edge) and name not in nudged:
                # A tile near the top has its trash can and badge under the
                # ITEMS header (ph-1: Pinap's name at y=554 of 2560); near the
                # foot, under the close button.  Bring it to mid-screen.
                nudged.add(name)
                shift = int(image.height * 0.5) - label.center_y
                await phone.swipe([image.width // 2, int(image.height * 0.45)],
                                  [image.width // 2, int(image.height * 0.45) + shift], image, 0.8, scroll=True)
                await asyncio.sleep(1.5)
                continue
            done.add(name)
            line = await discard_one(phone, name, label, count, image)
            if "discarded" not in line and "would" not in line:
                report.ok = False
            phone.say(f"bag: {line}")
            report.lines.append(line)
            continue
        screen = text_of(boxes)
        if screen == last_screen or len(done) == len(DISCARD_ITEMS):
            break
        last_screen = screen
        await phone.swipe([image.width // 2, int(image.height * 0.78)],
                          [image.width // 2, int(image.height * 0.38)], image, 0.6, scroll=True)
        await asyncio.sleep(1.5)
    _image, boxes = await phone.read()
    after = bag_fill(boxes)
    if not report.lines:
        report.lines.append("nothing to throw away")
    report.detail = f"bag {before} -> {after}; " + "; ".join(report.lines)
    await phone.go_home()
    return report


# --- Steps 4 and 5: the existing commands -----------------------------------------

def friends_list(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return find_box(boxes, "add friend") is not None and find_box(boxes, "friends") is not None


def trainer_screen(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    return find_box(boxes, "friends", exact=True) is not None and find_box(boxes, "me", exact=True) is not None


async def still_tab(phone: Phone, image: Image.Image, boxes: list[OCRBox]) -> tuple[Image.Image, OCRBox | None]:
    """The FRIENDS tab once the profile has stopped sliding in.

    On the SE (4 Nov) a tap read off a frame mid-slide landed on the XP bar
    and opened the Level Requirements page instead.
    """
    tab = find_box(boxes, "friends", exact=True)
    for _ in range(6):
        await asyncio.sleep(0.6)
        image, boxes = await phone.read()
        now = find_box(boxes, "friends", exact=True)
        if tab is not None and now is not None and abs(now.center_y - tab.center_y) <= 4 \
                and abs(now.center_x - tab.center_x) <= 4:
            return image, now
        tab = now
    return image, tab


async def walk_to_friends(phone: Phone) -> None:
    image, boxes = await phone.tap_first(TRAINER_AVATAR, trainer_screen, "the trainer profile")
    if friends_list(image, boxes):
        return
    for _attempt in range(3):
        image, tab = await still_tab(phone, image, boxes)
        if tab is None:
            break
        await phone.tap_box(tab, image)
        ok, image, boxes = await phone.settle(friends_list)
        if ok:
            return
        # A wrong page (Level Requirements, a badge): close it and look again.
        phone.say(f"walk to friends: the FRIENDS tap opened something else (saved {phone.save(image, 'not-friends')})")
        point = close_button(image)
        if point is None:
            break
        await phone.tap(point, image)
        ok, image, boxes = await phone.settle(trainer_screen, timeout=8)
        if not ok:
            break
    raise StepFailed(f"the FRIENDS tab did not open (screen saved: {phone.save(image, 'no-friends')})")


def gifts_command(name: str) -> list[str]:
    return ["gift.py", "--local", "--all", "--gifts-only", "--devices", name]


def gbl_command(name: str) -> list[str]:
    return ["gbl_day.py", "--local", "--devices", name]


async def launch(name: str, arguments: list[str]) -> int:
    print(f"[{name}] starting: " + " ".join(arguments), flush=True)
    environment = {**os.environ, gift_android.CLEAR_BAG_ENV: "1"}
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", *arguments, cwd=str(ROOT), env=environment)
    return await process.wait()


# --- One phone, all steps -----------------------------------------------------------

async def with_phone(spec: pokemon_fleet.DeviceSpec, args: argparse.Namespace,
                     work: Callable[[Phone], Awaitable[Report | None]]) -> Report | None:
    """Hold the phone's lock and an open connection for one stretch of work."""
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        try:
            return await work(Phone(spec.name, target, args.shots, args.dry_run))
        finally:
            await target.close()


async def run_phone(spec: pokemon_fleet.DeviceSpec, args: argparse.Namespace,
                    phase: set[str] | None = None) -> list[Report]:
    name = spec.name
    wanted = [step for step in args.steps if phase is None or step in phase]
    reports: list[Report] = []

    async def step(label: str, work: Callable[[Phone], Awaitable[Report | None]]) -> bool:
        try:
            report = await with_phone(spec, args, work)
        except (StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
            report = Report(label, False, str(exc))
        except Exception as exc:  # one phone's surprise must not stop the others
            report = Report(label, False, f"{type(exc).__name__}: {exc}")
        if report is not None:
            reports.append(report)
            mark = "done" if report.ok else "PROBLEM"
            print(f"[{name}] {label} {mark}: {report.detail}", flush=True)
            return report.ok
        return True

    if "mega" in wanted:
        await step("mega", lambda phone: mega_step(phone, args.tag))
    if "buddy" in wanted:
        await step("buddy", lambda phone: buddy_step(phone, args.berries, args.berry))
    if "bag" in wanted:
        await step("bag", bag_step)
    if "gifts" in wanted and not args.dry_run:
        async def to_friends(phone: Phone) -> None:
            await walk_to_friends(phone)
        # gift.py stops when OPEN meets a full Item Bag and leaves a marker;
        # the bag is cleared and gifting goes on, until a clear frees nothing.
        marker = gift_android.bag_full_marker(spec.identifier)
        for round_ in range(1, GIFT_BAG_ROUNDS + 1):
            marker.unlink(missing_ok=True)
            if not await step("walk to friends", to_friends):
                break
            code = await launch(name, gifts_command(name))
            if not marker.exists():
                reports.append(Report("gifts", code == 0, f"gift.py exited {code}"))
                break
            marker.unlink(missing_ok=True)
            print(f"[{name}] gifts stopped on a full Item Bag; clearing it (round {round_})", flush=True)
            before = len(reports)
            await step("bag", bag_step)
            cleared = reports[before:] if len(reports) > before else []
            if not any("discarded" in line for report in cleared for line in report.lines):
                reports.append(Report("gifts", False,
                                      "the Item Bag is full of things kept; no more gifts can be opened"))
                break
        else:
            reports.append(Report("gifts", False, f"stopped after {GIFT_BAG_ROUNDS} bag clears"))
    if "gbl" in wanted and not args.dry_run:
        async def to_map(phone: Phone) -> None:
            await phone.go_home()
        if await step("walk to map", to_map):
            code = await launch(name, gbl_command(name))
            reports.append(Report("gbl", code == 0, f"gbl_day.py exited {code}"))
    return reports


async def trade_here(here: Sequence[pokemon_fleet.DeviceSpec], args: argparse.Namespace) -> Report | None:
    """The two phones on this computer trade each other (sources/starter_trade.py)."""
    if len(here) != 2:
        print(f"[trade] skipped: {len(here)} phone(s) on this computer; trading needs exactly two", flush=True)
        return Report("trade", True, f"skipped: {len(here)} phone(s) here, trading needs a pair")
    from . import starter_trade
    print(f"[trade] {here[0].name} + {here[1].name}: counting raid families", flush=True)
    try:
        report = await starter_trade.trade_step(here, args)
    except (StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
        report = Report("trade", False, str(exc))
    except Exception as exc:  # the pair's surprise must not stop GBL
        report = Report("trade", False, f"{type(exc).__name__}: {exc}")
    for line in report.lines:
        print(f"[trade]   {line}", flush=True)
    print(f"[trade] {'done' if report.ok else 'PROBLEM'}: {report.detail}", flush=True)
    return report


async def run(args: argparse.Namespace) -> int:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    wanted = set(args.devices or [])
    specs = [spec for spec in fleet.devices.values() if spec.enabled and (not wanted or spec.name in wanted)]
    androids = await screen_router.attached_androids(specs)
    iphones = pokemon_fleet.ios_connected_udids()
    here = [spec for spec in specs if spec.identifier in (androids if spec.platform == "android" else iphones)]
    if not here:
        print("No fleet phone is attached to this computer")
        return 0
    print(f"Starting the day on {len(here)} phone(s): " + ", ".join(spec.name for spec in here)
          + f"; steps: {' -> '.join(args.steps)}" + (" (dry run)" if args.dry_run else ""), flush=True)
    if args.plan:
        for spec in here:
            print(f"[{spec.name}] would run: {' -> '.join(args.steps)}")
            if "gifts" in args.steps:
                print(f"[{spec.name}]   then " + " ".join(gifts_command(spec.name)))
            if "gbl" in args.steps:
                print(f"[{spec.name}]   then " + " ".join(gbl_command(spec.name)))
        return 0
    results = await asyncio.gather(*(run_phone(spec, args, BEFORE_TRADE) for spec in here))
    if "trade" in args.steps and not args.dry_run:
        pair_report = await trade_here(here, args)
        if pair_report is not None:
            for reports in results:
                reports.append(pair_report)
    after = await asyncio.gather(*(run_phone(spec, args, AFTER_TRADE) for spec in here))
    for reports, more in zip(results, after):
        reports.extend(more)
    print("\nSummary", flush=True)
    failed = False
    for spec, reports in zip(here, results):
        for report in reports:
            failed |= not report.ok
            print(f"  [{spec.name}] {report.step}: {'ok' if report.ok else 'PROBLEM'} - {report.detail}", flush=True)
    return 1 if failed else 0


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", nargs="+", help="only these fleet device names")
    parser.add_argument("--skip", nargs="+", choices=STEPS, default=[], help="steps to leave out")
    parser.add_argument("--only", nargs="+", choices=STEPS, help="run just these steps")
    parser.add_argument("--tag", default="mega", help="the Pokemon tag to Mega Evolve from (default: mega)")
    parser.add_argument("--berries", type=int, default=3, help="berries to feed the buddy (default: 3)")
    parser.add_argument("--berry", default="razz berry", help="which berry to feed (default: razz berry)")
    parser.add_argument("--shots", type=Path, default=None, help="where to keep step screenshots")
    parser.add_argument("--plan", action="store_true", help="list the phones and steps; touch nothing")
    parser.add_argument("--trade", action="store_true",
                        help="include the trade step (off by default until proven live)")
    parser.add_argument("--trades", type=int, default=100,
                        help="most trades the pair makes today (default: 100, the game's limit)")
    parser.add_argument("--reuse-counts", action="store_true",
                        help="trade from the counts an earlier run saved in the last two hours")
    parser.add_argument("--trade-plan", dest="trade_plan_only", action="store_true",
                        help="count and print the trades, but make none")
    parser.add_argument("--dry-run", action="store_true",
                        help="walk the mega and buddy screens and report, but evolve, feed, "
                             "photograph and pet nothing; gifts and GBL are skipped")
    args = parser.parse_args(arguments)
    if not 0 <= args.berries <= 3:
        parser.error("--berries must be 0 to 3")
    # Trading stays opt-in until a whole trade has been seen through live.
    chosen = args.only or [step for step in STEPS if step != "trade" or args.trade]
    args.steps = [step for step in STEPS if step in chosen and step not in args.skip]
    if not args.steps:
        parser.error("no steps left to run")
    args.shots = (args.shots or config_paths.state_dir() / "starter").expanduser()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))
