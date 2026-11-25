"""Start the day on every attached phone: buddy, a battle, Mega Evolutions, gifts, GBL.

Each phone runs these steps in order, side by side with the other phones:

1. buddy  -- open the buddy, feed it up to 3 berries (Razz, then Nanab,
             then Pinap; one Golden Razz only when none of those is left
             and more than 25 are), take a snapshot and
             pet it until its mood ring is full, then read the profile's
             Give your buddy a treat / Play together / Take a snapshot rows.
2. battle -- BATTLE > Blanche > Master League with the buddy in the party >
             USE THIS PARTY > Run > OK: the buddy's "Battle together" heart
             without spending a GBL battle.  The buddy is kept maxed, so the
             top tile of a search for its name is the buddy.
3. special -- runs the receiver's daily special trade, a spare legendary for a
             shiny it lacks (sources/special_trade.py).  Across multiple
             configured computers, starter.py finishes earlier steps on every
             computer first so the coordinator can reach partner phones across
             hosts via SSH without lock conflicts.  With --local, runs only
             when a partner is attached beside the receiver on this computer.
4. mega   -- open the account's "mega" Pokemon tag and Mega Evolve every
             Pokemon whose MEGA EVOLVE button shows no energy cost.  Only one
             Pokemon can be Mega Evolved at a time, so each replaces the last;
             the point is the daily Mega Level progress.
5. bag    -- discard the items not kept.
6. gifts  -- walk to the friends list and hand the phone to `gift.py`.
7. gbl    -- walk back to the map and hand the phone to `gbl_day.py`.
8. buddy-again -- the buddy step once more after the day's battles.

The buddy and mega steps find their buttons by the words on screen (Vision OCR) and by
colours measured on the phones, not by per-phone coordinates.  Steps 3 and 4
are the existing commands, run for this one phone.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence

from PIL import Image

from . import starter_ios_session
from . import config_paths, gbl_home_recovery as home, gbl_vision, gift_android, pokemon_fleet, screen_router

ROOT = Path(__file__).resolve().parent.parent
# The buddy is fed before anything else; the battle right after it earns the
# buddy's "Battle together" heart (user's order, 5 Nov).
# The special trade comes after the buddy steps, before mega (user, 7 Nov).
STEPS = ("buddy", "battle", "special", "mega", "bag", "gifts", "trade", "gbl", "buddy-again", "goplus")
# Steps two phones on one computer take together; every phone there waits
# for the others to reach them.
PAIR_STEPS = ("special", "trade")
OCRBox = gbl_vision.OCRBox

POLL_GAP = 1.0
SETTLE_TIMEOUT = 12.0


# --- Colours and shapes, measured on the sample-android-b (1080x2412) and sample-receiver ---------

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
LEVEL_CARD_CLOSE_Y = 0.953   # the X under "Mega Level increased!" (sample-android-b)
# The sample-android-a's evolution animation is slow; 40s ran out on it.
MEGA_WAIT = 75
BERRY_CHIP_X = (0.139, 0.075)   # the X on the berry chip, top-left (sample-android-a, sample-android-b)
# Words of cards that lie over the map; their X closes them.  Never their
# USE LUCKY EGG.
# Pages the Poke Ball test also passes for.  Level Requirements (the profile's
# XP bar, opened by a mistimed FRIENDS tap on the SE, 4 Nov) has a teal X where
# the Poke Ball would be, and GBL then took its panels for league cards.
MAP_OVERLAY_WORDS = ("use lucky egg", "new bonuses unlocked", "see details",
                     "level requirements", "caught while using incense")
# Tutorial/info cards (e.g. Poke Lid "What is a stamp rally?") ignore Android
# BACK and close with the pale ringed X at the foot of the screen.
INFO_MODAL_WORDS = (
    "what is a stamp rally",
    "stamp rally",
    "poke lid",
    "stamp card",
)
LEAGUE_DOOR = (0.104, 0.094)  # the league chooser's exit door (sample-android-a 720x1600)
GOLDEN_RAZZ = "golden razz"   # its tile wraps: "GOLDEN RAZZ" / "BERRY"
GOLDEN_KEEP = 25     # Golden Razz feeds the buddy only above this many (user, 5 Nov)
# A treat heart needs the hunger meter run down first: feeding sooner than
# this after the last treat heart only tops it up (user, 9 Nov).
TREAT_GAP_SECONDS = 30 * 60
TREAT_HEARTS_MAX = 3
MAX_BERRIES = 3      # most berries in one buddy session; three fills a hungry buddy (user, 7 Nov)
# Fed to the buddy in this order; a Golden Razz only when none is left (user, 7 Nov).
FEED_BERRIES = ("razz berry", "nanab berry", "pinap berry")
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


def main_menu_close_button(image: Image.Image) -> list[int] | None:
    """The centre of the main menu's pale teal-ringed X disc at the foot of the screen."""
    points = _matching(
        image,
        _band(image, CLOSE_BAND_X, (0.85, 0.96)),
        lambda p: p[1] > p[0] + 20 and p[2] > p[0] + 15,
    )
    if not points or _share(points, image) < 0.0002:
        return None
    return _centroid(points)


def pale_close_button(image: Image.Image) -> list[int] | None:
    """Pale/white bottom-centre X disc with a teal ring (modals, Send Gift, Dynamax)."""
    rgb = image.convert("RGB")
    w, h = rgb.size
    px = rgb.load()

    def is_pale(p: tuple[int, int, int]) -> bool:
        return min(p) >= 200 and max(p) - min(p) <= 35

    def is_teal(p: tuple[int, int, int]) -> bool:
        return p[1] > p[0] + 25 and p[2] > p[0] + 25 and p[0] < 140 and p[2] > 90

    cx = w // 2
    r_in = max(6, int(w * 0.032))
    r_max = int(w * 0.095)
    r_x = max(3, int(w * 0.014))
    tol = max(6, int(w * 0.015))
    step_y = max(2, h // 400)
    dirs8 = [(math.cos(k * math.pi / 4), math.sin(k * math.pi / 4)) for k in range(8)]
    hits: list[int] = []
    for cy in range(int(h * 0.87), int(h * 0.97), step_y):
        if cy - r_in < 0 or cy + r_in >= h:
            continue
        if not any(is_teal(px[cx + dx, cy + dy]) for dx in (-4, 0, 4) for dy in (-4, 0, 4)):
            continue
        if sum(is_pale(px[cx + dx, cy + dy]) for dx, dy in ((0, -r_in), (0, r_in), (-r_in, 0), (r_in, 0))) < 4:
            continue
        if sum(
            any(is_teal(px[cx + sx * r_x + dx, cy + sy * r_x + dy]) for dx in (-2, 0, 2) for dy in (-2, 0, 2))
            for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1))
        ) < 3:
            continue
        radii: list[int] = []
        for ux, uy in dirs8:
            found = None
            for r in range(r_in, r_max + 1, 2):
                x, y = int(round(cx + ux * r)), int(round(cy + uy * r))
                if 0 <= x < w and 0 <= y < h and is_teal(px[x, y]):
                    found = r
                    break
            if found is None:
                break
            radii.append(found)
        if len(radii) == 8 and max(radii) - min(radii) <= tol:
            hits.append(cy)
    if not hits:
        return None
    return [cx, (hits[0] + hits[-1]) // 2]


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


def buddy_present(image: Image.Image) -> list[int] | None:
    """Green gift/souvenir bag with blue flower on the play screen floor."""
    region = _band(image, (0.44, 0.56), (0.65, 0.85))
    blues = _matching(image, region, lambda p: p[2] > 170 and p[2] > p[0] + 60 and p[2] > p[1] + 35)
    if len(blues) < 5:
        return None
    greens = _matching(
        image,
        region,
        lambda p: p[0] < 145 and p[2] < 90 and p[1] >= 110 and p[1] > p[0] + 25 and p[1] > p[2] + 65,
    )
    if len(greens) < 10:
        return None
    bx, by = _centroid(blues)
    max_dx, max_dy = image.width * 0.06, image.height * 0.06
    near_bag = [p for p in greens if abs(p[0] - bx) <= max_dx and abs(p[1] - by) <= max_dy]
    if len(near_bag) < 8:
        return None
    near_flower = [p for p in blues if abs(p[0] - bx) <= max_dx and abs(p[1] - by) <= max_dy]
    return _centroid(near_bag + near_flower)


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


def _storage_counters(boxes):
    eggs = [b for b in boxes if re.fullmatch(r"\s*\d{1,2}\s*/\s*12\s*", b.text)]
    stock = [b for b in boxes if re.fullmatch(r"\D{0,4}\(\s*\d{1,5}\s*\)\s*|\s*[\d,]+\s*/\s*[\d,]{3,}\s*", b.text)]
    return next(((n, egg) for n in stock for egg in eggs if n.center_x < egg.center_x
                 and abs(n.center_y - egg.center_y) < max(n.height, egg.height)), None)


def storage_open(boxes: Sequence[OCRBox]) -> bool:
    text = text_of(boxes)
    if "tags" in text and "eggs" in text:
        return True
    if not any("location" in b.text.casefold() and "detect" in b.text.casefold() for b in boxes):
        return False
    # A red location notice can hide the tabs but leave both aligned counters visible.
    return _storage_counters(boxes) is not None


def _ios_storage_banner_counts(image, boxes):
    """Read the native counters beneath the translucent red location notice."""
    if _storage_counters(boxes) or not any(
        b.text.strip().casefold() == "search" or "evolutionary line" in b.text.casefold()
        or re.fullmatch(r"cp\s*\d+", b.text, re.I) for b in boxes
    ):
        return boxes
    w, h = image.size
    rect = (0, round(.045*h), w, round(.12*h))
    band = image.crop(rect)
    readings = []
    for channel, low, high in (("R", 221, 243), ("G", 75, 94)):
        enhanced = image.copy()
        contrast = band.getchannel(channel).point(
            lambda pixel: max(0, min(255, round((pixel - low) * 255 / (high - low))))
        )
        enhanced.paste(contrast, rect[:2])
        pair = _storage_counters(gbl_vision.recognize(enhanced, rect))
        if not pair or not (.3*w < pair[0].center_x < .7*w < pair[1].center_x):
            return boxes
        readings.append(pair)
    if [re.sub(r"[^\d/()]", "", b.text) for b in readings[0]] != [
        re.sub(r"[^\d/()]", "", b.text) for b in readings[1]
    ]:
        return boxes
    return [*boxes, *readings[0]]


def _storage_open_settled(image, boxes):
    if not storage_open(boxes):
        return False
    return any(b.center_y < .15 * image.height and
               (gbl_vision.normalize(b.text) in {"tags", "eggs"}
                or re.fullmatch(r"\s*\d{1,2}\s*/\s*12\s*", b.text)) for b in boxes)


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
    cost: int | None   # None: free; UNREAD_COST: something is printed there
    beside: tuple[int, int, int, int] | None = None   # where a cost would print


UNREAD_COST = -1   # text beside the pill that is not a clean number: not free


COST_INK_SHARE = 0.001   # free pills: 0.0001 of the strip; a lone "8": 0.005


def _cost_ink(pixel: Sequence[int]) -> bool:
    """The dark grey of the printed cost; the teal close/menu discs are not grey."""
    return max(pixel) < 130 and max(pixel) - min(pixel) < 45


def cost_printed(image: Image.Image, button: MegaButton) -> bool:
    """Dark digits right of the pill's energy icon, whatever OCR made of them.

    Whole-screen OCR can miss a lone small digit, or read a 6 as a letter, and
    zoomed crops read nothing at all: a 6-energy Blaziken went through as free
    (sample-android-b, 5 Nov).  The free pill draws the icon and no number.  Measured:
    free Tyranitar and Houndour100 (sample-android-a) 2 dark pixels; costs of 8 to 60
    on sample-android-a and sample-android-b, 178 to 280.
    """
    if button.beside is None:
        return False
    left, top, right, bottom = button.beside
    area = max(1, (right - left) * (bottom - top))
    return len(_matching(image, button.beside, _cost_ink, step=1)) / area > COST_INK_SHARE


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
        if box.x > pill.x + pill.width and abs(box.center_y - row_y) < reach * 0.6:
            label = badge_text(gbl_vision.normalize(box.text)).replace(" ", "")
            if not label:
                continue
            # Any text on the pill's row is a cost; a misread digit ("G" for
            # 6) is still not free.
            cost = int(label.replace(",", "")) if re.fullmatch(r"\d[\d,]*", label) else UNREAD_COST
            break
    beside = (min(width, pill.x + pill.width + int(width * 0.02)), max(0, int(row_y - reach * 0.6)),
              min(width, pill.x + pill.width + int(width * 0.36)), min(height, int(row_y + reach * 0.6)))
    return MegaButton([pill.center_x, row_y], cost, beside)


TREAT_ROW_LABELS = (
    "give your buddy a treat",
    "give your buddy",
    "give your buddv",
    "give your bud",
    "buddv a treat",
    "a treat",
)


def _treat_row_center_y(boxes: Sequence[OCRBox]) -> int | None:
    """Locate the 'Give your buddy a treat' row even when fast OCR skips its text."""
    box = find_box(boxes, *TREAT_ROW_LABELS)
    if box is not None:
        return box.center_y
    play = find_box(boxes, "play together")
    if play is None:
        return None
    battle = find_box(boxes, "battle together")
    if battle is not None and battle.center_y > play.center_y:
        step = battle.center_y - play.center_y
    else:
        snap = find_box(boxes, "take a snapshot")
        if snap is None or snap.center_y <= play.center_y:
            return None
        step = (snap.center_y - play.center_y) // 2
    candidate = play.center_y - step
    walk = find_box(boxes, "walk together")
    if candidate <= 0 or (walk is not None and walk.center_y >= candidate):
        return None
    return candidate


def _heart_row_region(width: int, height: int, center_y: int) -> tuple[int, int, int, int]:
    half = int(height * HEART_ROW_HALF)
    return (int(width * HEART_BAND_X[0]), max(0, center_y - half),
            int(width * HEART_BAND_X[1]), min(height, center_y + half))


def heart_rows(image: Image.Image, boxes: Sequence[OCRBox],
               rows: dict[str, tuple[str, ...]] | None = None) -> dict[str, bool | None]:
    """Whether each daily row we care about shows a filled heart (None: row not seen)."""
    # The close button can cut a label in two ("Give your bud" / "treat"),
    # and Vision .fast can read "buddy" as "buddv".
    rows = rows or {"treat": TREAT_ROW_LABELS,
                    "play": ("play together",), "snapshot": ("take a snapshot",)}
    width, height = image.size
    found: dict[str, bool | None] = {}
    for key, labels in rows.items():
        if key == "treat":
            center_y = _treat_row_center_y(boxes)
        else:
            box = find_box(boxes, *labels)
            center_y = box.center_y if box is not None else None
        if center_y is None:
            found[key] = None
            continue
        region = _heart_row_region(width, height, center_y)
        pink = _matching(image, region, lambda p: near(p, HEART_PINK, 30))
        found[key] = _share(pink, image) >= HEART_MIN_SHARE
    return found


def treat_hearts(image: Image.Image, boxes: Sequence[OCRBox]) -> int | None:
    """How many filled hearts the "Give your buddy a treat" row shows (None: row not seen)."""
    return filled_hearts(image, _treat_row_center_y(boxes))


def filled_hearts(image: Image.Image, center_y: int | None) -> int | None:
    """Count the separate filled hearts in one visible buddy activity row."""
    if center_y is None:
        return None
    width, height = image.size
    region = _heart_row_region(width, height, center_y)
    xs = sorted({x for x, _y in _matching(image, region, lambda p: near(p, HEART_PINK, 30))})
    # Filled hearts sit side by side with a thin gap; count the runs of pink
    # columns, ignoring specks narrower than a third of a heart.
    hearts, start, last = 0, None, None
    for x in xs + [None]:
        if x is not None and last is not None and x - last <= width * 0.008:
            last = x
            continue
        if start is not None and last - start >= width * 0.02:
            hearts += 1
        start = last = x
    return hearts


def treat_stamp(name: str) -> Path:
    """Where the time of this phone's last new treat heart is kept."""
    return config_paths.state_dir() / "starter" / f"treat-heart-{name}.txt"


def treat_wait(name: str, now: float | None = None) -> float:
    """Seconds until a feed can earn this phone another treat heart (0: now)."""
    try:
        last = float(treat_stamp(name).read_text().strip())
    except (OSError, ValueError):
        return 0.0
    now = time.time() if now is None else now
    if datetime.fromtimestamp(last).date() != datetime.fromtimestamp(now).date():
        return 0.0   # the rows reset each day
    return max(0.0, last + TREAT_GAP_SECONDS - now)


def mark_treat_heart(name: str, now: float | None = None) -> None:
    path = treat_stamp(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{time.time() if now is None else now:.0f}\n")


# --- The phone ------------------------------------------------------------------

class StepFailed(RuntimeError):
    """A step could not reach the screen it needed; the phone's later steps still run."""


LOADING_MARKERS = (
    "photo disc",
    "remember to be alert",
    "do not enter dangerous",
    "niantic",
    "the pokemon company",
    "collect your daily",
    "sign out",
)


def _looks_loading(image: Image.Image, boxes: Sequence[OCRBox]) -> bool:
    """Whether `image`/`boxes` shows Pokemon GO's startup splash or loading bar."""
    text = text_of(boxes)
    if any(marker in text for marker in LOADING_MARKERS):
        return True
    width, height = image.size
    if width < 20 or height < 20:
        return False
    pixels = image.convert("RGB").load()
    x0, x1 = int(width * 0.20), int(width * 0.80)
    y0, y1 = int(height * 0.91), int(height * 0.98)
    cols = list(range(x0, x1, max(1, (x1 - x0) // 40)))
    if not cols:
        return False
    for y in range(y0, y1, max(1, (y1 - y0) // 10)):
        hits = 0
        for x in cols:
            r, g, b = pixels[x, y][:3]
            if r > 200 and 90 <= g <= 190 and b < 110:
                hits += 1
        if hits >= len(cols) * 0.45:
            return True
    return False


class Phone:
    """A fleet phone reduced to look, tap, swipe and back, in screenshot pixels."""

    def __init__(self, name: str, target: home.Target, shots: Path, dry_run: bool = False) -> None:
        self.name = name
        self.target = target
        self.shots = shots
        self.dry_run = dry_run
        self.ios = isinstance(target, home.IOSTarget) or getattr(target, "platform", None) == "ios"

    def say(self, message: str) -> None:
        print(f"[{self.name}] {message}", flush=True)

    async def look(self) -> Image.Image:
        return await self.target.screenshot()

    async def read(self) -> tuple[Image.Image, list[OCRBox]]:
        image = await self.look()
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        if self.ios and any("detect location" in b.text.casefold() and b.center_y < .14 * image.height
                            for b in boxes):
            boxes = await asyncio.to_thread(_ios_storage_banner_counts, image, boxes)
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
                    seconds: float = 0.4, scroll: bool = False, stroke: bool = False) -> None:
        if self.ios:
            device = self.target.device
            a = device.logical_point(list(start), image)
            b = device.logical_point(list(end), image)
            if scroll:
                await device.scroll(a, b, seconds)
                await self.still()
            elif stroke:
                # Petting: the finger has to travel for the whole `seconds`.
                await device.stroke(a, b, seconds)
            else:
                # WDA's drag holds for `duration` before moving; a short hold
                # makes the move itself a stroke or a flick.
                await device.drag(a, b, 0.05)
            return
        x1, y1 = start
        x2, y2 = end
        # A scroll is dragged slowly: a quick one flings the Item Bag on for a
        # second or two after the finger lifts (sample-android-b), under reads and taps.
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
        point = close_button(image) or pale_close_button(image) or exit_door(image) or (
            main_menu_close_button(image) if home.main_menu_open(image) else None
        )
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
        loading_deadline = time.monotonic() + 60.0
        last_back_small: Image.Image | None = None
        attempt = 0
        while attempt < 9:
            prev_back_small = last_back_small
            last_back_small = None
            front = getattr(self.target, "front_package", None)
            if front is not None and await asyncio.to_thread(front) != home.GAME_PACKAGE:
                # The game closed (a BACK too many) and the menu taps then
                # opened Chrome from the launcher (sample-android-a, 4 Nov).
                self.say("Pokemon GO is not in front; bringing it back")
                # A dark or swipe-locked phone reads the same way: sample-receiver sat on
                # its lock screen for all nine tries on 23 Nov 2026.
                wake = getattr(self.target, "_wake", None)
                if callable(wake):
                    await asyncio.to_thread(wake)
                await self.target.restore()
                await asyncio.sleep(8)
            image = await self.look()
            if home.on_map(image):
                # A friendship level-up ("now Great/Ultra/Best/Forever Friends!
                # ... USE LUCKY EGG") lies over the map, which still reads as
                # the map; its X is where the Poke Ball would be (sample-receiver, 3 Nov).
                boxes = await asyncio.to_thread(gbl_vision.recognize, image)
                if storage_open(boxes):
                    # A red and white Pokémon in the bottom storage row can look
                    # exactly like the map button. Leave storage before using it.
                    await self.back(image)
                    attempt += 1
                    await asyncio.sleep(1.5)
                    continue
                if (friends_list(image, boxes) or trainer_screen(image, boxes)
                        or find_box(boxes, "friendship level", "total activity")):
                    # An avatar or a buddy on the social screens can resemble
                    # the map's Poké Ball. A tap there opens another friend.
                    self.say("closing the trainer or friends screen")
                    point = close_button(image)
                    if point:
                        await self.tap(point, image)
                    else:
                        await self.back(image)
                    attempt += 1
                    await asyncio.sleep(1.5)
                    continue
                # The Item Bag passes the Poke Ball test too: the Charmander
                # sticker's red over the pale page reads as the ball.  bag_step
                # then "went home" with the bag still open, and the gift step's
                # trainer-avatar tap opened the bag's Gift tile instead, so no
                # gift was sent (sample-receiver, 15 Nov).
                if items_page(boxes):
                    self.say("the Item Bag is still open; closing it")
                    await self.tap(close_button(image) or [image.width // 2, int(image.height * 0.933)], image)
                    await asyncio.sleep(2)
                    attempt += 1
                    continue
                if detail_page(boxes):
                    # A Pokémon detail page with a colourful fusion banner (e.g. Necrozma)
                    # can pass the bottom-centre Poké Ball check. Close it before returning.
                    self.say("closing a Pokemon's page")
                    point = close_button(image)
                    if point is not None:
                        await self.tap(point, image)
                    else:
                        await self.back(image)
                    await asyncio.sleep(2)
                    attempt += 1
                    continue
                if find_box(boxes, *MAP_OVERLAY_WORDS) is None:
                    return image
                self.say("dismissing a card over the map")
                await self.tap(close_button(image) or pale_close_button(image) or [image.width // 2, int(image.height * 0.933)], image)
                await asyncio.sleep(2)
                attempt += 1
                continue
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
            # The Local Trade Pokémon picker can ignore Android BACK. Its X
            # leaves the picker, then the game may ask to cancel the trade.
            yes = find_box(boxes, "yes", exact=True)
            if yes and find_box(boxes, "cancel this trade", "won t trade"):
                await self.tap_box(yes, image)
                await asyncio.sleep(1.5)
                attempt += 1
                continue
            if find_box(boxes, "pokemon", exact=True) and find_box(boxes, "special trades remaining"):
                point = close_button(image)
                if point and not keyboard_up(boxes):
                    await self.tap(point, image)
                    await asyncio.sleep(1.5)
                    attempt += 1
                    continue
            # Pokemon GO's startup safety warning ("Stay aware of your
            # surroundings...") covers the map until OK is tapped.
            notice = gbl_vision.safety_notice_point(boxes, image)
            if notice is not None:
                self.say("dismissing startup safety notice")
                await self.tap(notice, image)
                await asyncio.sleep(2.5)
                attempt += 1
                continue
            if _looks_loading(image, boxes) and time.monotonic() < loading_deadline:
                self.say("Pokemon GO is still loading; waiting for the map")
                await asyncio.sleep(4)
                continue
            # Cards whose only way off is their own button: the Weekly
            # Challenge (SE, 4 Nov), GO Battle League welcome card, and similar
            # progress cards say LET'S GO!.
            go = (
                find_box(boxes, "let s go", "lets go")
                if find_box(boxes, "weekly challenge", "challenge", "battle league")
                else None
            )
            if go is not None:
                self.say("pressing LET'S GO! on a challenge card")
                await self.tap_box(go, image)
                await asyncio.sleep(2)
                attempt += 1
                continue
            if find_box(boxes, *INFO_MODAL_WORDS) is not None:
                point = close_button(image) or pale_close_button(image)
                self.say("dismissing a stamp rally / info modal")
                if point is not None:
                    await self.tap(point, image)
                else:
                    btn = find_box(boxes, "ok", "got it", "close", exact=True)
                    if btn is not None:
                        await self.tap_box(btn, image)
                    else:
                        await self.tap([image.width // 2, int(image.height * 0.935)], image)
                await asyncio.sleep(2)
                attempt += 1
                continue
            # A Pokemon's own page: its origin box says "HATCHED <place>" for
            # an egg hatch, which read as a hatching egg and tapped mid-page
            # forever (SE, 19 Nov).  Its X closes it.
            if (find_box(boxes, "was caught on", "was hatched on")
                    and find_box(boxes, "attack", exact=True) and find_box(boxes, "defense", exact=True)):
                # The appraisal leader's final dialogue covers the detail
                # sheet's X. Android BACK leaves the appraisal on screen.
                await self.tap([image.width // 2, int(image.height * 0.92)], image)
                await asyncio.sleep(1)
                attempt += 1
                continue
            if detail_page(boxes):
                self.say("closing a Pokemon's page")
                point = close_button(image)
                if point is not None:
                    await self.tap(point, image)
                else:
                    await self.back(image)
                await asyncio.sleep(2)
                attempt += 1
                continue
            # An egg hatching ("Oh?", then the Pokemon) waits for taps, and
            # BACK does nothing to it (sample-android-b, 4 Nov).
            # Not the search panel's "Hatched" filter chip (SE trade picker, 4 Nov).
            search_drawer = find_box(boxes, "recommended", "favorite", "recent", "see less", "see more")
            if find_box(boxes, "oh", exact=True) or (
                    find_box(boxes, "hatched") and not search_drawer and not storage_open(boxes)):
                if keyboard_up(boxes) and not self.ios:
                    await asyncio.to_thread(self.target._adb, "shell", "input keyevent 4")
                self.say("an egg is hatching; tapping through it")
                for _ in range(6):
                    await asyncio.sleep(3)
                    await self.tap([image.width // 2, int(image.height * 0.45)], image)
                attempt += 1
                continue
            if attempt == 3:
                await self.target.restore()
                await asyncio.sleep(3)
                attempt += 1
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
                            # prompt instead (sample-android-b, 4 Nov).  Stopping the
                            # keyboard app is the one thing that took it down.
                            await asyncio.to_thread(self.target._adb, "shell",
                                                    "am force-stop com.google.android.inputmethod.latin")
                    await asyncio.sleep(1.5)
                    attempt += 1
                    continue
                # "This trade with ... has expired." queues up after a cancelled
                # trade, one card per trade; each takes its OK (SE, 4 Nov).
                ok = find_box(boxes, "ok", exact=True)
                if ok is not None and find_box(boxes, "expired") is not None:
                    self.say("closing an expired-trade notice")
                    await self.tap_box(ok, image)
                    await asyncio.sleep(1.5)
                    attempt += 1
                    continue
                # "Do you want to exit Pokemon GO?" answers to CANCEL; its OK quits.
                no = find_box(boxes, "no", exact=True) or (
                    find_box(boxes, "cancel", exact=True) if find_box(boxes, "exit pokemon go") else None)
                if no is not None and find_box(boxes, "do you") is not None:
                    await self.tap_box(no, image)
                    await asyncio.sleep(1.5)
                    attempt += 1
                    continue
            # A battle step ends on GBL's "CHOOSE YOUR LEAGUE" page, which BACK
            # does not leave; its exit door, top-left, does (sample-android-a, 5 Nov).
            # The door asks "Exit the Trainer Battle?"; YES there only leaves
            # the chooser, so it is pressed only right after that door.
            # (exit_door() finds the trainer's dark hair here, not the door.)
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
            # The battle-party picker: CANCEL drops back to the party screen.
            cancel = find_box(boxes, "cancel", exact=True)
            if cancel is not None and find_box(boxes, "done", exact=True) is not None:
                self.say("closing the Pokemon picker")
                await self.tap_box(cancel, image)
                await asyncio.sleep(2)
                attempt += 1
                continue
            if await cancel_effect(self, image, boxes):
                attempt += 1
                continue
            # The same prompt on its own, left by BACK from a party screen.
            if find_box(boxes, "exit the trainer battle") is not None:
                yes = find_box(boxes, "yes", exact=True)
                if yes is not None:
                    self.say("leaving the trainer battle")
                    await self.tap_box(yes, image)
                    await asyncio.sleep(2)
                    attempt += 1
                    continue
            if find_box(boxes, "choose your league") is not None:
                self.say("leaving the league chooser")
                await self.tap([int(image.width * LEAGUE_DOOR[0]), int(image.height * LEAGUE_DOOR[1])], image)
                ok, image, boxes = await self.settle(
                    lambda _i, b: find_box(b, "exit the trainer battle") is not None, timeout=5)
                yes = find_box(boxes, "yes", exact=True) if ok else None
                if yes is not None:
                    await self.tap_box(yes, image)
                await asyncio.sleep(2)
                attempt += 1
                continue
            if home.main_menu_open(image):
                point = close_button(image) or main_menu_close_button(image) or [
                    image.width // 2, int(image.height * 0.915),
                ]
                await self.tap(point, image)
                await asyncio.sleep(1.5)
                attempt += 1
                continue
            if not self.ios and prev_back_small is not None:
                now_small = image.convert("L").resize((60, 120))
                diff = sum(abs(a - b) for a, b in zip(now_small.getdata(), prev_back_small.getdata())) / (60 * 120)
                if diff < 2.0:
                    point = close_button(image) or pale_close_button(image)
                    if point is not None:
                        self.say("BACK did not leave this screen; tapping its close button")
                        await self.tap(point, image)
                        await asyncio.sleep(2)
                        attempt += 1
                        continue
            if not await self.back(image):
                if attempt >= 4:
                    break
                if attempt == 2:
                    await self.target.restore()
                await asyncio.sleep(3)
                attempt += 1
                continue
            if not self.ios:
                last_back_small = image.convert("L").resize((60, 120))
            await asyncio.sleep(2)
            attempt += 1
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
        # A tap the map swallowed (sample-receiver, sample-android-b, 4 Nov): look again and retry.
        phone.say(f"the main menu did not open; trying again ({attempt + 1}/3)")
    else:
        raise StepFailed(f"the main menu did not open (screen saved: {phone.save(image, 'no-menu')})")
    label = find_box(boxes, "pokemon", exact=True)
    if label is None:
        raise StepFailed("no POKEMON button on the main menu")
    await phone.tap_box(label, image, gbl_vision.MENU_ICON_DROP)
    ok, image, boxes = await phone.settle(_storage_open_settled)
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
    last_labels: list[str] = []
    for _ in range(12):
        ok, image, boxes = await phone.settle(lambda _i, b: tag_title(b, tag) is not None, timeout=4)
        title = tag_title(boxes, tag)
        if title is not None:
            break
        # The TAGS tap did not take (the SE, 19 Nov): the swipe below then
        # landed on the Pokemon grid as a tap and opened a Pokemon's page.
        # Check where we are before scrolling anything.
        if detail_page(boxes):
            phone.say("mega: a Pokemon's page opened instead of TAGS; going back")
            await phone.back(image)
            await asyncio.sleep(2)
            image, boxes = await phone.read()
        if grid_tiles(boxes, image.width, image.height):
            tags = find_box(boxes, "tags", exact=True)
            if tags is None:
                raise StepFailed(f"left storage looking for TAGS (screen saved: {phone.save(image, 'no-tags-tab')})")
            phone.say("mega: still on the Pokemon grid; tapping TAGS again")
            await phone.tap_box(tags, image)
            last_labels = []
            continue
        labels = [gbl_vision.normalize(box.text) for box in boxes if box.y > image.height * 0.18]
        if labels and labels == last_labels:
            break
        last_labels = labels
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
    if not grid_tiles(boxes, image.width, image.height):
        return False
    if tag_grid_count(boxes, tag) is not None:
        return True
    # A tag holding one Pokemon shows a lone "1" under its title, and Vision
    # reads no lone digit (SE, 24 Nov: 'MaxMega' with one Charizard was
    # called "did not open").  The title as the page header will do.
    title = tag_title(boxes, tag)
    return title is not None and title.center_y < image.height * 0.12


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

    The free card ("Do you want to Mega Evolve Heracross?", sample-receiver 3 Nov) shows
    only the two CPs and "lasts for 8 hours"; any other number is a cost.
    """
    if "energy" in text:
        return True
    # A nickname's digits split off by OCR ("Electrike100" -> "electrike 100",
    # sample-android-b 19 Nov) sit between the name and the free card's CP pair; a
    # cost figure never does.
    rest = re.sub(r"\bmega evolve ([a-z'.-]+) \d+%?(?= cp \d)", r"mega evolve \1", text)
    # ... and on the replacement card, before "will no longer be" ("your
    # electrike 100 will no longer be mega evolved", sample-android-b 19 Nov).
    rest = re.sub(r"\byour (mega )?([a-z'.-]+) \d+%?(?= will no longer)", r"your \1\2", rest)
    rest = re.sub(r"\bcp \d+\b|\b\d+ hours?\b", " ", rest)
    return re.search(r"\b\d[\d,]*\b", rest) is not None


def free_card(text: str) -> bool:
    """The first "Do you want to Mega Evolve X?" card, asking nothing in return."""
    return "do you want to mega evolve" in text and "lasts for" in text and not card_spends_energy(text)


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
    retaps = 0
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
            # The free first card still up: YES did not take (sample-receiver Swampert,
            # Blaziken, 19 Nov).  Press it again; a costing card still gets NO.
            if replacement_card(text) or (free_card(text) and retaps < 2):
                retaps += free_card(text)
                await phone.tap_box(again, image)
                await asyncio.sleep(2)
                continue
            # The first card fading out over the page as the evolution starts
            # (sample-android-b Aerodactyl/Sableye, 19 Nov): OCR mixes in the page's
            # stardust and weight, so it reads as neither free nor a
            # replacement.  It already passed the cost check before YES, and
            # pressing on a faded card could land on POWER UP: wait for it to go.
            normalized = gbl_vision.normalize(question.text)
            fading = "do you want to" in normalized and "really" not in normalized
            prompt_up = any("tap to mega evolve" in gbl_vision.normalize(b.text) for b in boxes)
            if fading and not prompt_up:
                await asyncio.sleep(1.5)
                continue
            if not fading:
                phone.say(f"mega: declined a card (saved {phone.save(image, 'mega-declined')}): {text[:160]}")
                await decline(phone, boxes, image)
                return False
            # Fading card with the prompt already up: fall through to tap the disc.
        # "Beldum100%'s Mega Level increased! ... SEE DETAILS" follows some
        # evolutions; its X sits at the foot of the card.  Not SEE DETAILS:
        # that page offers LEVEL UP, which spends Mega Energy.
        if find_box(boxes, "mega level") is not None and find_box(boxes, "increased") is not None:
            await phone.tap([image.width // 2, int(image.height * LEVEL_CARD_CLOSE_Y)], image)
            await asyncio.sleep(2)
            continue
        # OCR can fold the prompt into its neighbours ("| 21,9 TAP TO MEGA
        # EVOLVE! 66", sample-receiver), so match inside the text and tap mid-screen.
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
    words this waits for -- until it is dismissed (Houndour100%, sample-android-b).
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
    # BACK meanwhile (seen on the sample-android-b), so look before every press: one
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


# PvPoke's Mega Master League ranking (rankings/mega/overall/rankings-10000),
# 23 Nov 2026: the Mega and Primal forms, best first, by the species name the
# page's "<SPECIES> MEGA ENERGY" caption gives.
ML_MEGA_RANK = (
    "kyogre", "gyarados", "mewtwo", "garchomp", "rayquaza", "dragonite", "groudon",
    "tyranitar", "metagross", "latias", "latios", "heracross", "swampert",
    "salamence", "chesnaught", "aggron", "delphox", "scizor", "charizard",
    "gallade", "gengar",
)


def ml_mega_rank(name: str) -> int | None:
    """Where `name` sits in the Mega Master League ranking; None when it is not there."""
    text = name.casefold()
    found = next((i for i, kind in enumerate(ML_MEGA_RANK) if kind in text), None)
    if found is not None:
        return found
    # A nickname cut to fit ("Tyranit", sample-receiver 24 Nov) names its species by its
    # start; five letters are enough to tell the ranked species apart.
    word = re.sub(r"[^a-z]", "", text)
    if len(word) >= 5:
        return next((i for i, kind in enumerate(ML_MEGA_RANK) if kind.startswith(word)), None)
    return None


def page_ml_rank(name: str, boxes: Sequence[OCRBox]) -> int | None:
    """The Master League rank from the name, or from the page's own captions.

    A nicknamed Pokemon whose MEGA ENERGY caption was not read came out as
    "K" or "On" (sample-receiver, 23 Nov 2026) and a Max Mega Kyogre risked being skipped
    as outside Master League; "<SPECIES> CANDY" still names it.
    """
    rank = ml_mega_rank(name)
    if rank is not None or len(name.strip("? ")) >= 4:
        # A readable species name is the answer; a Gardevoir must not borrow
        # Gallade's rank from their shared RALTS CANDY.
        return rank
    for box in boxes:
        text = gbl_vision.normalize(box.text)
        if "candy" in text or "energy" in text:
            for base, kind in ML_CANDY_FAMILY.items():
                text = text.replace(base, kind)
            rank = ml_mega_rank(text)
            if rank is not None:
                return rank
    return None


# Candy is named for the line's first stage ("GIBLE CANDY" on a Garchomp).
ML_CANDY_FAMILY = {
    "magikarp": "gyarados", "gible": "garchomp", "dratini": "dragonite",
    "larvitar": "tyranitar", "beldum": "metagross", "mudkip": "swampert",
    "bagon": "salamence", "chespin": "chesnaught", "aron": "aggron",
    "fennekin": "delphox", "scyther": "scizor", "charmander": "charizard",
    "ralts": "gallade", "gastly": "gengar",
}


def max_mega(boxes: Sequence[OCRBox]) -> bool:
    """The account's own MaxMega tag on the page (every phone has one)."""
    return any(gbl_vision.normalize(b.text).replace(" ", "") == "maxmega" for b in boxes)


@dataclass(frozen=True)
class MLPick:
    tile: str
    occurrence: int
    name: str
    rank: int
    maxed: bool
    tag: str = "mega"
    cp: int | None = None


# Walked after the main tag: the ones being powered up for Master League
# (user, 23 Nov 2026), then the strong Max Megas that were never put in
# "mega" (sample-receiver's Kyogre and Groudon).  MegaPower goes first: a free levelling
# Master League pick there makes the MaxMega walk pointless.  A phone without
# one just skips it.
EXTRA_MEGA_TAGS = ("MegaPower", "MaxMega")
MAX_MEGA_TAG = "maxmega"

# Mega-capable species outside the Mega Master League ranking.  A MaxMega
# tile whose grid name is one of these can never be the day's pick, so its
# page is not opened (sample-receiver, 24 Nov: 42 Max Megas opened one by one, about
# ten seconds each, before GBL).  A nickname that names none is still opened.
NON_ML_MEGA_SPECIES = (
    "venusaur", "blastoise", "beedrill", "pidgeot", "alakazam", "slowbro",
    "kangaskhan", "pinsir", "aerodactyl", "ampharos", "steelix", "houndoom",
    "sceptile", "blaziken", "gardevoir", "sableye", "mawile", "medicham",
    "manectric", "sharpedo", "camerupt", "altaria", "banette", "absol",
    "glalie", "lopunny", "lucario", "abomasnow", "audino", "diancie",
    "malamar", "victreebel", "starmie", "clefable", "froslass", "greninja",
    "skarmory", "emboar", "excadrill", "scolipede", "scrafty", "eelektross",
    "chandelure", "hawlucha", "falinks", "floette", "zygarde", "raichu",
    "feraligatr", "meganium", "dragalge", "pyroar", "barbaracle", "drampa",
)


def max_tile_rank(tile: str) -> int | None:
    """The Master League rank a MaxMega grid name gives; candy-family names count."""
    text = tile
    for base, kind in ML_CANDY_FAMILY.items():
        text = text.replace(base, kind)
    return ml_mega_rank(text)


def max_tile_worth_opening(tile: str, best_rank: int | None) -> bool:
    """Whether a MaxMega tile could beat the best Master League pick held so far.

    A Max Mega gains nothing from evolving; it is worth a look only when it
    could end up the active Mega.  Names that read as a ranked species are
    opened only when they rank better; names of other Mega species are not
    opened; anything else (a nickname, a misread) is opened to be safe.
    """
    rank = max_tile_rank(tile)
    if rank is not None:
        return best_rank is None or rank < best_rank
    return not any(kind in tile for kind in NON_ML_MEGA_SPECIES)


def page_cp(boxes: Sequence[OCRBox]) -> int | None:
    for box in boxes:
        match = re.search(r"\bcp\s*(\d{2,5})\b", gbl_vision.normalize(box.text))
        if match:
            return int(match.group(1))
    return None


def ml_pick_order(picks: Sequence[MLPick]) -> list[MLPick]:
    """Least wanted first, so the best is evolved last and is the one left active.

    No energy is ever spent (user, 23 Nov 2026).  A free Mega still levelling
    beats a free Max Mega; within each, the higher Master League rank wins.
    """
    return sorted(picks, key=lambda p: (not p.maxed, -p.rank))


async def walk_tag(phone: Phone, tag: str, visit, wanted=None) -> tuple[int, int]:
    """Open each tile of the tag in turn, from the grid; `visit` handles its page.

    Sideways swipes between Pokemon pages were tried first and do not follow the
    tag's order (Metagross -> Sableye, then stuck), so every Pokemon is opened
    from the grid and the grid is scrolled once its visible tiles are done.
    `wanted(tile, occurrence)` limits which tiles are opened; each walk starts
    at the top of the tag and counts same-named tiles the same way, so a tile
    named in one walk is found again in the next.  Answers (count, looked).
    """
    count = await open_tag(phone, tag)
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
        if wanted is not None and not wanted(tile, done[tile]):
            continue
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
        stop = await visit(tile, done[tile], ok, image, boxes)
        image, boxes = await back_to_grid(phone, tag)
        if stop:
            break
    return count, looked


async def mega_step(phone: Phone, tag: str) -> Report:
    """Mega Evolve every free Pokemon in the tag, the best Master League one last.

    Only one Pokemon is Mega Evolved at a time, so the last one stays active
    for the day's GBL.  Free ones that rank in Mega Master League are held back
    in the first walk and evolved after it, least wanted first (`ml_pick_order`).
    Nothing that costs Mega Energy is ever evolved.
    """
    report = Report("mega", True, "")
    evolved = 0
    picks: list[MLPick] = []
    seen: set[tuple[str, int | None]] = set()
    current = tag

    async def survey(tile: str, occurrence: int, ok: bool, image: Image.Image, boxes: list[OCRBox]) -> None:
        nonlocal evolved
        if not ok:
            line = f"{tile}: its page did not open"
            report.ok = False
            phone.say(f"mega: {line}")
            report.lines.append(line)
            return
        kind = species(boxes)
        name = kind if len(kind) >= 3 else tile.title()   # "CHARIZARD X MEGA ENERGY" splits
        cp = page_cp(boxes)
        if (name, cp) in seen and cp is not None:
            return   # in an earlier tag too
        seen.add((name, cp))
        if phone.dry_run:
            phone.save(image, f"tile-{tile}-{occurrence}")
        caption_rank = ml_mega_rank(kind)
        if mega_active(boxes):
            line = f"{name}: Mega Evolved already"
        elif (current.casefold() == MAX_MEGA_TAG and len(kind.strip("? ")) >= 4
              and (caption_rank is None or any(p.rank <= caption_rank for p in picks))):
            # The caption names the species: no need to find the button of a
            # Max Mega that cannot become the pick (Sol 6.2, 24 Nov).
            line = f"{name}: a Max Mega that cannot beat the pick held; skipped"
        else:
            maxed = max_mega(boxes)
            image, boxes, button = await reveal_mega_button(phone)
            rank = page_ml_rank(name, boxes)
            if button is None:
                line = f"{name}: no MEGA EVOLVE button"
            elif button.cost is None and cost_printed(image, button):
                line = f"{name}: a cost is printed beside MEGA EVOLVE (unread); skipped"
            elif button.cost is not None:
                shown = button.cost if button.cost != UNREAD_COST else "some"
                line = f"{name}: costs {shown} Mega Energy; skipped"
            elif rank is None and (maxed or max_mega(boxes)):
                # A Max Mega has no level left to earn, and it is not a Master
                # League pick: evolving it only swaps the active Mega for one
                # nobody will use (Lopunny on sample-receiver, 23 Nov 2026).
                line = f"{name}: free, but a Max Mega outside Master League; skipped"
            elif rank is not None:
                picks.append(MLPick(tile, occurrence, name, rank, maxed or max_mega(boxes), current, cp))
                line = (f"{name}: free; held for last (Mega Master League #{rank + 1}"
                        f"{', Max Mega' if picks[-1].maxed else ''})")
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

    count, looked = await walk_tag(phone, tag, survey)
    phone.say(f"mega: the {tag!r} tag holds {count} Pokemon")
    for extra in EXTRA_MEGA_TAGS:
        if extra.casefold() == tag.casefold():
            continue
        wanted = None
        if extra.casefold() == MAX_MEGA_TAG:
            # Which held picks are Max Megas is read off this tag's grid, not
            # their pages: the page's MaxMega chip is not always in view (sample-receiver,
            # 24 Nov: Swampert read as levelling, the MaxMega walk was skipped
            # and #13 Swampert stayed active where Gyarados #2 was free).  The
            # grid pass opens no page, so it costs a few scrolls.
            names: set[str] = set()

            def note(tile: str, _occurrence: int) -> bool:
                names.add(tile)
                return False

            await phone.go_home()
            try:
                await walk_tag(phone, extra, survey, note)
            except StepFailed as exc:
                if "no tag named" in str(exc):
                    phone.say(f"mega: no {extra!r} tag on this phone")
                    continue
                names = set()
            if names:
                picks[:] = [replace(p, maxed=p.maxed or p.tile in names) for p in picks]
            if names and any(not p.maxed for p in picks):
                # A levelling pick outranks every Max Mega (ml_pick_order).
                phone.say(f"mega: skipping {extra!r}; a free levelling Master League pick is held")
                continue

            def wanted(tile: str, _occurrence: int) -> bool:
                best = min((p.rank for p in picks), default=None)
                if max_tile_worth_opening(tile, best):
                    return True
                phone.say(f"mega: {tile}: a Max Mega that cannot beat the pick held; not opened")
                return False
        current = extra
        await phone.go_home()
        try:
            more, seen_here = await walk_tag(phone, extra, survey, wanted)
        except StepFailed as exc:
            if "no tag named" in str(exc):
                phone.say(f"mega: no {extra!r} tag on this phone")
                continue
            # An extra tag going wrong must not cost the picks already held:
            # the SE's 'MaxMega' misread ended the step with Tyranitar held and
            # nothing Mega Evolved for the day's GBL (24 Nov).
            report.ok = False
            line = f"the {extra!r} tag could not be walked ({exc}); evolving the picks held"
            phone.say(f"mega: {line}")
            report.lines.append(line)
            continue
        looked += seen_here
        phone.say(f"mega: the {extra!r} tag holds {more} Pokemon")

    ordered = ml_pick_order(picks)
    # A Max Mega gains nothing from evolving, so of the Max Megas only the one
    # that ends up active is worth a walk; every still-levelling pick is
    # evolved for its level, and the best of all goes last (sample-receiver, 23 Nov 2026:
    # seven held Max Megas would each have been evolved in turn).
    ordered = [p for p in ordered[:-1] if not p.maxed] + ordered[-1:]
    for pick in ordered:
        async def evolve_pick(tile: str, occurrence: int, ok: bool, image: Image.Image,
                              boxes: list[OCRBox], pick: MLPick = pick) -> None:
            nonlocal evolved
            if not ok:
                line = f"{pick.name}: its page did not open the second time"
                report.ok = False
            else:
                image, boxes, button = await reveal_mega_button(phone)
                if button is None or button.cost is not None or cost_printed(image, button):
                    # Free a few minutes ago; a cost now is still never paid.
                    line = f"{pick.name}: no longer free; skipped"
                elif phone.dry_run:
                    line = f"{pick.name}: would Mega Evolve (dry run, Master League pick)"
                elif await mega_evolve(phone, button, image):
                    evolved += 1
                    line = f"{pick.name}: Mega Evolved (free, Master League pick)"
                else:
                    line = f"{pick.name}: free, but the Mega Evolution did not finish"
                    report.ok = False
            phone.say(f"mega: {line}")
            report.lines.append(line)
            return True

        await phone.go_home()
        await walk_tag(phone, pick.tag, evolve_pick,
                       wanted=lambda tile, occ, pick=pick: (tile, occ) == (pick.tile, pick.occurrence))

    best = ml_pick_order(picks)[-1].name if picks else None
    report.detail = (f"{evolved} free Mega Evolution(s) of {looked} Pokemon looked at"
                     + (f"; Master League pick: {best}" if best else ""))
    await phone.go_home()
    return report


# --- Step 2: buddy -----------------------------------------------------------------

def buddy_profile(_image: Image.Image, boxes: list[OCRBox]) -> bool:
    if find_box(boxes, "today s activities", "todays activities", "todavs activities") is not None:
        return True
    return find_box(boxes, "play", exact=True) is not None and find_box(boxes, "walk together") is not None


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


@dataclass
class Fed:
    """What one feed() call did."""
    problem: str | None = None   # why no berry was fed; None when one was
    full: bool = False           # the buddy is full: no more berries wanted today
    golden: bool = False         # the berry fed was a Golden Razz
    refused: bool = False        # a guard stopped the throw: feed no more today
    unavailable: bool = False    # QUICK TREAT opened nothing: try the play screen instead
    effect: bool = False         # QUICK TREAT opened an Adventure Effect card instead


# Never fed to the buddy, whatever else goes wrong (user, 7 Nov).
FORBIDDEN_BERRY_WORDS = ("silver", "poffin")
# The held berry fills the screen's centre: share of strongly coloured and of
# grey pixels there.  Measured 7 Nov: Golden Razz 0.76/0.01, Razz 0.91/0.00,
# no berry 0.21/0.03.  A Silver Pinap is grey.
HELD_BERRY_BOX = ((0.40, 0.60), (0.52, 0.70))
HELD_MIN_COLOURED = 0.5
HELD_MAX_GREY = 0.2


def first_feed_berry(boxes: Sequence[OCRBox]) -> OCRBox | None:
    """The picker's Razz label, else Nanab, else Pinap: in that order, never by screen order."""
    for name in FEED_BERRIES:
        box = find_box(boxes, name, exact=True)
        if box is not None:
            return box
    return None


def berry_allowed(name: str, golden_ok: bool) -> bool:
    """Razz, Nanab or Pinap, or a Golden Razz when one is allowed."""
    if any(word in name for word in FORBIDDEN_BERRY_WORDS):
        return False
    if name == GOLDEN_RAZZ or name == "golden razz berry":
        return golden_ok
    return name in FEED_BERRIES


def forbidden_tile_near(boxes: Sequence[OCRBox], point: Sequence[int], image: Image.Image,
                        allow_golden: bool) -> OCRBox | None:
    """A Silver Pinap / Poffin (or unwanted Golden Razz) label on the tile being tapped."""
    words = FORBIDDEN_BERRY_WORDS + (() if allow_golden else ("golden",))
    for box in boxes:
        text = gbl_vision.normalize(box.text)
        if not any(word in text for word in words):
            continue
        # Tiles are a third of the width; the label sits under its berry.
        if abs(box.center_x - point[0]) < image.width / 6 and \
                -image.height * 0.02 < box.center_y - point[1] < image.height * 0.12:
            return box
    return None


def held_berry_coloured(image: Image.Image) -> bool:
    """A brightly coloured berry is held up in front of the buddy."""
    box = _band(image, *HELD_BERRY_BOX)
    coloured = grey = total = 0
    for y in range(box[1], box[3], 4):
        for x in range(box[0], box[2], 4):
            pixel = image.getpixel((x, y))[:3]
            spread = max(pixel) - min(pixel)
            total += 1
            if spread >= 90:
                coloured += 1
            elif spread <= 30 and 70 <= max(pixel) <= 225:
                grey += 1
    return total > 0 and coloured / total >= HELD_MIN_COLOURED and grey / total <= HELD_MAX_GREY


async def refuse(phone: Phone, image: Image.Image, why: str, holding: bool = False) -> Fed:
    """Feed nothing: put any held berry back, close the picker, and say why."""
    shot = phone.save(image, "berry-refused")
    if holding:
        # The berry chip's X, top-left, drops the berry without throwing it.
        await phone.tap([int(image.width * BERRY_CHIP_X[0]), int(image.height * BERRY_CHIP_X[1])], image)
        await asyncio.sleep(1.5)
    elif not phone.ios:
        await phone.back(image)
    return Fed(f"{why}; nothing fed (screen saved: {shot})", refused=True)


def effect_card(boxes: Sequence[OCRBox]) -> OCRBox | None:
    """CANCEL on a "Use <Adventure Effect>" card, e.g. Kyurem's Ice Burn (5,000 energy).

    A QUICK TREAT tap opened one on the SE (8 Nov).  USE is never pressed.
    """
    if find_box(boxes, "use", exact=True) is None:
        return None
    if not any(norm.startswith("use ") for norm in (gbl_vision.normalize(b.text) for b in boxes)):
        return None
    return find_box(boxes, "cancel", exact=True)


async def cancel_effect(phone: Phone, image: Image.Image, boxes: Sequence[OCRBox]) -> bool:
    cancel = effect_card(boxes)
    if cancel is None:
        return False
    phone.save(image, "effect-card")
    await phone.tap_box(cancel, image)
    await asyncio.sleep(2)
    return True


async def steady_quick_treat(phone: Phone) -> tuple[Image.Image, OCRBox] | None:
    """QUICK TREAT once the profile has stopped moving: two reads agree on where it is.

    The profile fades in and slides; a tap from a moving frame hit the
    Adventure Effects USE below it on the SE (8 Nov).
    """
    await asyncio.sleep(2)
    for _ in range(3):
        _first, boxes = await phone.read()
        before = find_box(boxes, "quick treat")
        await asyncio.sleep(1.2)
        image, boxes = await phone.read()
        now = find_box(boxes, "quick treat")
        if (before is not None and now is not None and buddy_profile(image, boxes)
                and abs(before.center_y - now.center_y) < image.height * 0.01
                and abs(before.center_x - now.center_x) < image.width * 0.02):
            return image, now
    return None


async def feed(phone: Phone, golden_ok: bool = True) -> Fed:
    """One berry, start to finish, and whether the buddy is full afterwards.

    The treat heart comes only when the hunger meter fills.  One Golden Razz
    fills it; one Razz Berry does not (sample-receiver, 7 Nov: ate it, still hungry, no
    heart), so buddy_step() calls this until the buddy is full.
    """
    where, image = await feed_ready(phone)
    if where is None:
        image, boxes = await phone.read()
        if await cancel_effect(phone, image, boxes):
            return Fed("an Adventure Effect card opened instead of the berries; cancelled",
                       refused=True, effect=True)
        # A greyed QUICK TREAT opens nothing.  Once the buddy walks on the map
        # the game says "You can only use Quick Treat when your buddy is not on
        # the map" (sample-android-a, 9 Nov): the play screen's berry button still works.
        if find_box(boxes, "only use quick treat", "not on the map") is not None:
            return Fed("QUICK TREAT is off while the buddy is on the map", unavailable=True)
        if buddy_profile(image, boxes):
            return Fed("QUICK TREAT opened nothing", unavailable=True)
        return Fed("the play screen's buttons are not up")
    if where == "play":
        await phone.tap(berry_button(image) or [int(image.width * BERRY_FALLBACK[0]),
                                                int(image.height * BERRY_FALLBACK[1])], image)
    # The picker slides up: its header can read before the berry labels are
    # drawn ("ERRIE" and nothing else, sample-android-b), so wait for the label itself.
    golden = gbl_vision.normalize(GOLDEN_RAZZ)
    ok, image, boxes = await phone.settle(
        lambda _i, b: find_box(b, *FEED_BERRIES, golden, exact=True) is not None, timeout=8)
    if not ok and not berry_picker(image, boxes):
        if await cancel_effect(phone, image, boxes):
            return Fed("an Adventure Effect card opened instead of the berries; cancelled",
                       refused=True, effect=True)
        if buddy_profile(image, boxes):
            # A greyed QUICK TREAT leaves the profile up.  That is a full buddy
            # (sample-receiver, 8 Nov) or one walking on the map (sample-android-b, 9 Nov): the
            # play screen's berry button tells the two apart.
            return Fed("QUICK TREAT opened nothing", unavailable=True)
        return Fed(f"the berry picker did not open (screen saved: {phone.save(image, 'no-berry-picker')})")
    # Razz, then Nanab, then Pinap.  A Golden Razz stands in only when none
    # of the three is left, at most one a session, and only while more than
    # GOLDEN_KEEP are in the bag; an unread count spends none.  Silver Pinap
    # and Poffin never (user, 7 Nov).
    label = first_feed_berry(boxes)
    gold = None
    if label is None:
        # The picker may still be sliding: look once more before reaching for gold.
        await asyncio.sleep(1)
        image, boxes = await phone.read()
        label = first_feed_berry(boxes)
    if label is None and golden_ok:
        gold = find_box(boxes, golden, exact=True)
        count = None
        # Its "x107" badge can be missed while the picker slides (sample-android-b, 5 Nov).
        for attempt in range(3):
            if gold is not None:
                count = item_count(boxes, gold, image.width, image.height) or badge_count(image, gold)
            if count is not None:
                break
            await asyncio.sleep(1)
            image, boxes = await phone.read()
            gold = find_box(boxes, golden, exact=True)
        if gold is not None and count is not None and count > GOLDEN_KEEP:
            label = gold
            phone.say(f"buddy: no Razz, Nanab or Pinap left; feeding one Golden Razz ({count} in the bag)")
        else:
            gold = None
    if label is None:
        if not phone.ios:
            await phone.back(image)  # close the picker
        return Fed(f"no Razz, Nanab or Pinap left, and no Golden Razz to spare (needs more than "
                   f"{GOLDEN_KEEP}, one a session); none fed (screen saved: {phone.save(image, 'berry-picker')})")
    if label is not gold:
        phone.say(f"buddy: feeding a {gbl_vision.normalize(label.text).title()}")
    fed_golden = label is gold
    # Guards against a Silver Pinap, a Poffin or a second Golden Razz (sample-receiver,
    # 7 Nov: a Silver Pinap was fed after the Nanab tile ran out and the
    # picker redrew).  Any doubt feeds nothing and ends the feeding.
    name = gbl_vision.normalize(label.text)
    if not berry_allowed(name, golden_ok and fed_golden):
        return await refuse(phone, image, f"refused to feed {label.text!r}: not an allowed berry")
    # The picker redraws when a tile runs out: read it again and insist the
    # label has not moved, so the tap lands on the tile that was read.
    # A picker still sliding up moves between reads (sample-receiver, 7 Nov): keep
    # reading until two reads agree, and refuse if they never do.
    for _ in range(4):
        await asyncio.sleep(1.2)
        image, again = await phone.read()
        fresh = find_box(again, name, exact=True)
        if fresh is not None and abs(fresh.center_x - label.center_x) <= image.width * 0.03 \
                and abs(fresh.center_y - label.center_y) <= image.height * 0.02:
            break
        if fresh is None:
            # The tile is gone or renamed: start the choice again from scratch.
            return await refuse(phone, image, f"{label.text!r} vanished from the berry picker")
        label = fresh
    else:
        return await refuse(phone, image, f"the berry picker kept moving while choosing {label.text!r}")
    label = fresh
    point = [label.center_x, label.center_y - int(image.height * BERRY_ICON_RISE)]
    near_forbidden = forbidden_tile_near(again, point, image, allow_golden=fed_golden)
    if near_forbidden is not None:
        return await refuse(phone, image, f"{near_forbidden.text!r} sits by the tap for {label.text!r}")
    await phone.tap(point, image)
    await asyncio.sleep(2)
    image = await phone.look()
    # Last look before the throw: a held Razz, Nanab, Pinap or Golden Razz is
    # brightly coloured; a Silver Pinap is grey.  Not convinced: no throw.
    if not held_berry_coloured(image):
        return await refuse(phone, image, "the berry in hand does not look like the one chosen", holding=True)
    await phone.swipe([int(image.width * FLICK_FROM[0]), int(image.height * FLICK_FROM[1])],
                      [int(image.width * FLICK_TO[0]), int(image.height * FLICK_TO[1])],
                      image, FLICK_SECONDS)
    # The buttons hide while the buddy eats.  A buddy still hungry gets the
    # berry picker back by itself (sample-receiver, sample-android-a); a full one gets the play
    # screen with the berry button greyed out (sample-android-a after a Golden Razz).
    await asyncio.sleep(2)
    where, image = await feed_ready(phone, timeout=30)
    if where is None:
        return Fed("the buddy never finished eating", golden=fed_golden)
    full = where == "play" and berry_button(image) is None
    return Fed(None, full=full, golden=fed_golden)


async def close_picker(phone: Phone) -> None:
    """Shut a berry picker the game reopened after the last berry (sample-android-a)."""
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
    camera_only = None
    while True:
        image = await phone.look()
        if berry_button(image) is not None and camera_button(image) is not None:
            return "play", image
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        if berry_picker(image, boxes):
            return "picker", image
        # Same rule as play_screen: camera and door up for a few seconds with
        # no red berry drawn is still the play screen.  Insisting on the red
        # icon fed nothing on sample-android-a, sample-android-b and SE (5 Nov), the sample-android-b with
        # 7 Razz in the bag; feed() taps BERRY_FALLBACK and checks the picker.
        if camera_button(image) is not None and exit_door(image) is not None:
            camera_only = camera_only or time.monotonic()
            if time.monotonic() - camera_only >= 4:
                return "play", image
        else:
            camera_only = None
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
        # Stroke across the buddy's solid core pixels at torso/chest height
        # so pointer_down and pointer_up never fall on empty backdrop around
        # narrow sprites (e.g. White Kyurem on iPhone SE).
        y = top + (bottom - top) // (3 if rounds % 2 == 1 else 2)
        row_xs = [x for x in range(left, right + 1, 4) if not _background(image.getpixel((x, y)))]
        if len(row_xs) >= 6:
            trim = len(row_xs) // 6
            x1 = max(5, row_xs[trim])
            x2 = min(image.width - 5, row_xs[-trim - 1])
        else:
            span = max(right - left, image.width // 5)
            x1 = max(5, (left + right) // 2 - span // 3)
            x2 = min(image.width - 5, (left + right) // 2 + span // 3)
        if x2 - x1 < image.width // 10:
            mid = (x1 + x2) // 2
            pad = image.width // 16
            lo = row_xs[0] if len(row_xs) >= 6 else 5
            hi = row_xs[-1] if len(row_xs) >= 6 else image.width - 5
            x1, x2 = max(lo, mid - pad), min(hi, mid + pad)
        for stroke in range(PET_STROKES):
            dy = int((stroke % 4 - 1.5) * image.height * 0.012)
            a, b = ([x1, y + dy], [x2, y + dy]) if stroke % 2 == 0 else ([x2, y + dy], [x1, y + dy])
            await phone.swipe(a, b, image, PET_STROKE_SECONDS, stroke=True)
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
        # snapshot leads to (sample-android-b).
        door = exit_door(image)
        if door is None:
            if phone.ios:
                break
            await phone.back(image)
        else:
            await phone.tap(door, image)
        await asyncio.sleep(3)


async def feed_berries(phone: Phone, berries: int, report: Report,
                       golden_ok: bool = True) -> tuple[int, bool, bool]:
    """Up to `berries` berries from an open (or opening) picker, stopping once the buddy is full.

    Returns (berries fed, try the play screen instead, a Golden Razz still allowed).
    """
    fed = 0
    # At most one Golden Razz, and only with no Razz, Nanab or Pinap left.
    for _ in range(berries):
        result = await feed(phone, golden_ok)
        if result.golden:
            golden_ok = False
        if result.problem and result.full:
            report.lines.append(f"berries: fed {fed}; the buddy is full")
            return fed, False, golden_ok
        if result.problem:
            report.lines.append(f"berries: stopped after {fed}: {result.problem}")
            return fed, fed == 0 and (result.unavailable or result.effect), golden_ok
        fed += 1
        phone.say(f"buddy: fed {fed} ({'full' if result.full else 'still hungry'})")
        if result.full:
            report.lines.append(f"berries: fed {fed}; the buddy is full")
            return fed, False, golden_ok
    if berries:
        report.lines.append(f"berries: fed {fed}, the {berries}-berry limit")
    return fed, False, golden_ok


async def open_buddy_present(phone: Phone, image: Image.Image, report: Report) -> Image.Image:
    """Open a buddy gift/souvenir bag on the play screen so berries and petting unlock."""
    point = buddy_present(image)
    if point is None:
        return image
    phone.say("buddy: opening a present on the play screen")
    await phone.tap(point, image)
    for _ in range(6):
        await asyncio.sleep(2.5)
        after = await phone.look()
        if camera_button(after) is not None and exit_door(after) is not None:
            still = buddy_present(after)
            if still is None:
                report.lines.append("present: opened")
                return after
            await phone.tap(still, after)
            continue
        btn = close_button(after)
        await phone.tap(btn or [after.width // 2, int(after.height * 0.50)], after)
    report.lines.append("present: tapped")
    return await play_screen(phone) or await phone.look()


async def buddy_step(phone: Phone, berries: int) -> Report:
    report = Report("buddy", True, "")
    image, boxes = await phone.tap_first(BUDDY_PORTRAIT, buddy_profile, "the buddy profile")
    # The treat hearts before feeding: a berry session is judged by whether
    # it added one (user, 9 Nov).
    await asyncio.sleep(2)   # the profile fades in over the map
    image, boxes = await phone.read()
    treat_before = treat_hearts(image, boxes)
    wait = treat_wait(phone.name)
    if berries and treat_before is not None and treat_before >= TREAT_HEARTS_MAX:
        report.lines.append(f"berries: none; the treat row is complete ({treat_before} hearts)")
        berries = 0
    elif berries and wait > 0:
        ready = datetime.fromtimestamp(time.time() + wait)
        report.lines.append(f"berries: none; the last treat heart was under 30 minutes ago "
                            f"(next from {ready:%H:%M})")
        berries = 0
    # The play screen's berry button can stay greyed and open nothing (sample-receiver,
    # sample-android-b and SE, 8 Nov, with 20 Razz in the bag); the profile's QUICK
    # TREAT opens the same picker and the same held-berry flick.
    steady = None if phone.dry_run or not berries else await steady_quick_treat(phone)
    fed_quick = steady is not None
    quick_fed, golden_ok = 0, True
    if fed_quick:
        image, quick = steady
        await phone.tap_box(quick, image)
        quick_fed, fallback, golden_ok = await feed_berries(phone, berries, report)
        fed_quick = not fallback
        await close_picker(phone)
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
        image = await open_buddy_present(phone, image, report)
        if not fed_quick:
            # No QUICK TREAT: the play screen's berry button.  A greyed one
            # (SE, 4 Nov) still opened the picker on a later try, so it is
            # pressed anyway; feed() says if nothing opens.
            await feed_berries(phone, berries - quick_fed, report, golden_ok)
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
    await asyncio.sleep(2)   # the profile fades in over the map (sample-android-b, 5 Nov)
    image, boxes = await phone.read()
    hearts = heart_rows(image, boxes)
    if hearts["treat"] is None and hearts["play"] is not None:
        # "Give your buddy a treat" sits above "Play together"; if "play" is
        # already in view, an animation/banner briefly hid the treat row, so
        # re-read once rather than scrolling the treat row off the top.
        await asyncio.sleep(1.5)
        image, boxes = await phone.read()
        more = heart_rows(image, boxes)
        hearts = {key: value if value is not None else more[key] for key, value in hearts.items()}
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
    treat_after = treat_hearts(image, boxes)
    if treat_after is None:
        for _ in range(2):   # the rows may have scrolled; read the treat row once more
            image, boxes = await phone.read()
            treat_after = treat_hearts(image, boxes)
            if treat_after is not None:
                break
    gained = None
    if treat_before is not None and treat_after is not None and not phone.dry_run and berries:
        gained = treat_after > treat_before or treat_before >= TREAT_HEARTS_MAX
        if treat_after > treat_before:
            mark_treat_heart(phone.name)
        report.lines.append(f"treat hearts: {treat_before} -> {treat_after}"
                            + ("" if gained else " (no new treat heart)"))
        phone.say(f"buddy: {report.lines[-1]}")
    names = {"treat": "Give your buddy a treat", "play": "Play together", "snapshot": "Take a snapshot"}
    summary = ", ".join(
        f"{names[key]} {'has a heart' if value else ('NO heart' if value is False else 'not read')}"
        for key, value in hearts.items())
    report.ok = all(hearts.values()) and gained is not False
    if treat_before is not None and treat_after is not None:
        summary += f"; treat hearts {treat_before} -> {treat_after}"
    report.detail = summary
    await phone.go_home()
    return report


# --- Step 2b: a Team Leader battle with the buddy ----------------------------------

# The user's recipe (5 Nov): BATTLE > Blanche > Master League > put the buddy in
# the party > USE THIS PARTY > Run > OK.  It earns the buddy's "Battle together"
# heart without spending a GBL battle.  The buddy is kept maxed out, so it is
# the highest-CP one of its kind: the picker, sorted by CP, shows it first.
# Measured on sample-receiver (1316x2560): the run button sits 0.167w below the CP header.
RUN_BELOW_HEADER = (0.167, 0.14, 0.195)
RUN_X = 0.085
BATTLE_SETTLE_SECONDS = 6   # past the "GO" countdown before running (sample-android-a, 7 Nov)
BATTLE_SCROLLS = 4


NAME_BANNER_RETRIES = 4
NAME_BANNER_WAIT_SECONDS = 3.0
NOTICE_BANNER_WORDS = ("from your gift", "received", "sent you a gift", "sent you")


def notice_banner(image: Image.Image, boxes: Sequence[OCRBox]) -> bool:
    """Whether a slide-down notice covers the top of the screen."""
    return any(b.center_y < 0.15 * image.height
               and any(w in b.text.lower() for w in NOTICE_BANNER_WORDS)
               for b in boxes)


def buddy_name(image: Image.Image, boxes: Sequence[OCRBox]) -> str | None:
    """The buddy's name at the top-left of its profile."""
    names = [b for b in boxes
             if 0.035 * image.height < b.center_y < 0.1 * image.height
             and b.center_x < image.width * 0.5 and re.search(r"[A-Za-z]{3}", b.text)]
    if not names:
        return None
    name = min(names, key=lambda b: b.center_y).text.strip()
    return clean_buddy_name(re.sub(r"\s*[›>]\s*$", "", name))


def clean_buddy_name(name: str) -> str:
    """The searchable name: no badge read as a stray letter, the "100%" rejoined.

    The sample-android-a's "Garchomp100%" carries a sun badge that reads as
    "Garchomp 100% g", and a search for that finds nothing (7 Nov).
    """
    words = name.split()
    # The badge reads as a stray letter or symbol ("g", "*").
    while len(words) > 1 and (len(words[-1]) == 1 or not re.search(r"[A-Za-z0-9]", words[-1])):
        words.pop()
    joined: list[str] = []
    for word in words:
        if joined and re.fullmatch(r"\d+%", word):
            joined[-1] += word
        else:
            joined.append(word)
    return " ".join(joined)


# The buddy-level hearts sit under the name on the profile (sample-receiver, 1316x2560:
# name at 0.063h, hearts at 0.094h, one heart 0.05w wide).  Filled ones are
# orange (255,97..109,56..71); empty ones grey.
LEVEL_HEART_DROP = 0.031
LEVEL_HEART_HALF = 0.010


def _level_orange(pixel: Sequence[int]) -> bool:
    r, g, b = pixel[:3]
    return r > 235 and 70 < g < 170 and b < 110


def buddy_level(image: Image.Image, boxes: Sequence[OCRBox]) -> int | None:
    """How many buddy-level hearts are filled under the name (None: no name read)."""
    names = [b for b in boxes
             if 0.035 * image.height < b.center_y < 0.1 * image.height
             and b.center_x < image.width * 0.5 and re.search(r"[A-Za-z]{3}", b.text)]
    if not names:
        return None
    name = min(names, key=lambda b: b.center_y)
    width, height = image.size
    y = name.center_y + int(height * LEVEL_HEART_DROP)
    half = int(height * LEVEL_HEART_HALF)
    region = (0, max(0, y - half), int(width * 0.45), min(height, y + half))
    xs = sorted({x for x, _y in _matching(image, region, _level_orange)})
    hearts, start, last = 0, None, None
    for x in xs + [None]:
        if x is not None and last is not None and x - last <= width * 0.008:
            last = x
            continue
        if start is not None and last - start >= width * 0.015:
            hearts += 1
        start = last = x
    return min(hearts, 4)


def buddy_searches(name: str, level: int | None) -> list[str]:
    """Picker searches that find the buddy itself, narrowest first.

    The species name alone is not enough: sample-receiver's buddy is a shiny Kingambit,
    and a regular Kingambit of higher CP led Blanche's battle instead, so the
    battle never earned a heart (22-23 Nov).  The game's buddyN filter is the
    buddy level: buddy1 a buddy with no level yet, buddy2 Good ... buddy5 Best.
    """
    searches = []
    if level is not None:
        searches.append(f"{name}&buddy{level + 1}")
    searches.append(f"{name}&buddy1-5")
    searches.append(name)
    return searches


def header_cp(image: Image.Image, boxes: Sequence[OCRBox]) -> OCRBox | None:
    """The player's own CP plate, top-left, once the battle is drawn."""
    if not gbl_vision.battle_header_visible(boxes, image.width, image.height):
        return None
    # The loading frame draws placeholder plates: "CP 50000" and PROMPT_MESSAGE.
    if find_box(boxes, "prompt message", "prompt_message") or find_box(boxes, "cp 50000"):
        return None
    plates = [b for b in boxes if b.center_y < image.height * gbl_vision.BATTLE_HEADER_BOTTOM
              and re.search(r"cp\s*\d", gbl_vision.normalize(b.text))]
    return min(plates, key=lambda b: b.center_x) if plates else None


# A hungry buddy earns no Battle together heart even with its treat row full:
# the sample-android-b, re-run at 16:24 on 14 Nov, led Blanche's battle with its
# Metagross and got nothing; its profile showed the berry bubble where the
# fed morning runs showed a face.  Measured: 836 berry-red pixels hungry, 0-5
# fed, on sample-android-b, sample-android-a and sample-receiver profiles.
HUNGRY_BAND = ((0.62, 0.92), (0.06, 0.20))
HUNGRY_MIN_SHARE = 0.0004


def buddy_hungry(image: Image.Image) -> bool:
    """The profile's mood bubble shows berries: the buddy wants feeding."""
    points = _matching(image, _band(image, *HUNGRY_BAND), _berry_red)
    return _share(points, image) >= HUNGRY_MIN_SHARE


async def feed_before_battle(phone: Phone, report: Report) -> None:
    """Fill a hungry buddy (same berry rules as buddy_step) so the battle counts."""
    steady = await steady_quick_treat(phone)
    fallback = True
    golden_ok = True
    if steady is not None:
        image, quick = steady
        await phone.tap_box(quick, image)
        _fed, fallback, golden_ok = await feed_berries(phone, MAX_BERRIES, report)
        await close_picker(phone)
    if fallback:
        # QUICK TREAT greys while the buddy walks on the map: the play screen.
        image, boxes = await phone.tap_first(BUDDY_PORTRAIT, buddy_profile, "the buddy profile")
        play = find_box(boxes, "play", exact=True)
        if play is not None:
            await phone.tap_box(play, image)
            if await play_screen(phone) is not None:
                await feed_berries(phone, MAX_BERRIES, report, golden_ok)
                await close_picker(phone)
    await phone.go_home()


# The buddy can already sit in the recommended party but not lead it; running
# at once then means it never battled (SE, 14 Nov: Kyurem third behind two
# Zacian, no heart).  It is switched in from the bench first.  The bench tile's
# picture sits just under its CP label (SE and sample-android-b: +0.025h).
BENCH_TILE_DROP = 0.025
SWITCH_WAIT_SECONDS = 8


def bench_labels(image: Image.Image, boxes: Sequence[OCRBox]) -> list[OCRBox]:
    """The bench tiles' CP labels down the right edge, top first."""
    return sorted((b for b in boxes if b.center_x > image.width * 0.75
                   and image.height * 0.3 < b.center_y < image.height * 0.85
                   and re.search(r"cp\s*\d", gbl_vision.normalize(b.text))),
                  key=lambda b: b.center_y)


def lead_name(image: Image.Image, boxes: Sequence[OCRBox]) -> str:
    """The text on the player's own header plate, top-left (name and CP)."""
    return " ".join(gbl_vision.normalize(b.text) for b in boxes
                    if b.center_y < image.height * gbl_vision.BATTLE_HEADER_BOTTOM
                    and b.center_x < image.width * 0.45)


def party_index(image: Image.Image, boxes: Sequence[OCRBox], wanted: str) -> int | None:
    """Which party slot (0 = lead) holds the buddy, by the name under each CP slot."""
    slots = sorted((b for b in boxes if re.fullmatch(r"cp\s*\d+", gbl_vision.normalize(b.text))
                    and b.center_y > image.height * 0.4), key=lambda b: b.center_x)
    if not slots:
        return None
    for box in boxes:
        if box.center_y > image.height * 0.4 and wanted in gbl_vision.normalize(box.text):
            return min(range(len(slots)), key=lambda i: abs(slots[i].center_x - box.center_x))
    return None


def plate_fallback(image: Image.Image, boxes: Sequence[OCRBox]) -> OCRBox:
    """Where the CP plate sits when a frame's read missed it (sample-receiver: 0.086h)."""
    return OCRBox("cp", 0.0, 0, int(image.height * 0.086) - 15, 10, 30)


async def battle_step(phone: Phone) -> Report:
    from . import trade_local
    report = Report("battle", True, "")
    image, boxes = await phone.tap_first(BUDDY_PORTRAIT, buddy_profile, "the buddy profile")
    name = buddy_name(image, boxes)
    # A friend's gift notice ("X received 5 items from your Gift!") slides
    # over the name for a few seconds (sample-android-b, 12 Nov); wait it out.
    for _ in range(NAME_BANNER_RETRIES):
        if name is not None and not notice_banner(image, boxes):
            break
        await asyncio.sleep(NAME_BANNER_WAIT_SECONDS)
        image, boxes = await phone.read()
        name = None if notice_banner(image, boxes) else buddy_name(image, boxes)
    # The profile fades in over the map: a mid-fade frame read the sample-android-b's
    # "Beldum100%" as "Iduf100%" and the party search found nothing (14 Nov).
    # Two reads in a row must agree.
    for _ in range(NAME_BANNER_RETRIES):
        await asyncio.sleep(1.0)
        image, boxes = await phone.read()
        again = None if notice_banner(image, boxes) else buddy_name(image, boxes)
        if again is not None and again == name:
            break
        name = again or name
    if name is None:
        raise StepFailed(f"could not read the buddy's name (screen saved: {phone.save(image, 'no-buddy-name')})")
    await asyncio.sleep(2)   # the profile fades in over the map
    image = await phone.look()
    level = buddy_level(image, boxes)
    phone.say(f"battle: buddy is {name}" + (f" ({level} level heart(s))" if level is not None else ""))
    if buddy_hungry(image) and not phone.dry_run:
        phone.say("battle: the buddy is hungry; feeding it first so the battle earns a heart")
        await feed_before_battle(phone, report)

    for attempt in range(3):
        image = await phone.go_home()
        ball = home.find_pokeball(image)
        if ball is None:
            raise StepFailed("no Poke Ball button on the map")
        await phone.tap(ball, image)
        ok, image, boxes = await phone.settle(
            lambda _i, b: gbl_vision.main_menu_visible(b) and find_box(b, "battle", exact=True) is not None)
        if ok:
            break
        phone.say(f"the main menu did not open; trying again ({attempt + 1}/3)")
    else:
        raise StepFailed(f"the main menu did not open (screen saved: {phone.save(image, 'no-menu')})")
    await phone.tap_box(find_box(boxes, "battle", exact=True), image, gbl_vision.MENU_ICON_DROP)
    ok, image, boxes = await phone.settle(
        lambda _i, b: find_box(b, "go battle league", "nearby battle", "training") is not None)
    if not ok:
        raise StepFailed(f"the Battle page did not open (screen saved: {phone.save(image, 'no-battle-page')})")
    for _ in range(BATTLE_SCROLLS):
        if find_box(boxes, "blanche", exact=True) is not None:
            break
        x = image.width // 2
        await phone.swipe([x, int(image.height * 0.82)], [x, int(image.height * 0.12)], image, 0.5, scroll=True)
        await asyncio.sleep(2)
        image, boxes = await phone.read()
    blanche = find_box(boxes, "blanche", exact=True)
    if blanche is None:
        raise StepFailed(f"no Blanche on the Battle page (screen saved: {phone.save(image, 'no-blanche')})")
    # The name label does not answer; her portrait just above it does (sample-receiver).
    await phone.tap([blanche.center_x, blanche.center_y - int(image.height * 0.058)], image)

    def lower(word: str):
        return lambda i, b: any(gbl_vision.normalize(x.text) == word and x.center_y > i.height * 0.5 for x in b)
    ok, image, boxes = await phone.settle(lower("battle"), timeout=8)
    if not ok:
        raise StepFailed(f"Blanche's card did not open (screen saved: {phone.save(image, 'no-leader')})")
    # The card slides up; a tap before it settles is lost (sample-receiver, 5 Nov).
    for _ in range(3):
        await asyncio.sleep(1.5)
        image, boxes = await phone.read()
        if find_box(boxes, "master league", exact=True) is not None:
            break
        buttons = [b for b in boxes if gbl_vision.normalize(b.text) == "battle" and b.center_y > image.height * 0.5]
        if buttons:
            await phone.tap_box(max(buttons, key=lambda b: b.center_y), image)
    ok, image, boxes = await phone.settle(lambda _i, b: find_box(b, "master league", exact=True) is not None)
    if not ok:
        raise StepFailed(f"the league list did not open (screen saved: {phone.save(image, 'no-leagues')})")
    await phone.tap_box(find_box(boxes, "master league", exact=True), image)
    ok, image, boxes = await phone.settle(lambda _i, b: find_box(b, "use this party") is not None)
    if not ok:
        raise StepFailed(f"the party screen did not open (screen saved: {phone.save(image, 'no-party')})")

    wanted = gbl_vision.normalize(name)
    # Party names can read joined ("Garchomp100% Kyurem100%", sample-android-a), so a name
    # counts when it is part of a label.
    def in_party(b: list[OCRBox]) -> bool:
        return any(wanted in gbl_vision.normalize(x.text) for x in b if x.center_y > image.height * 0.4)

    picked_cp = ""
    # Always put the buddy itself in the first slot.  A name match on the
    # card is no proof: the recommended party can hold another of the same
    # species (sample-receiver: a regular Kingambit where the buddy is a shiny one).
    slots = sorted((b for b in boxes if re.fullmatch(r"cp\s*\d+", gbl_vision.normalize(b.text))
                    and b.center_y > image.height * 0.5), key=lambda b: b.center_x)
    if not slots:
        raise StepFailed(f"no party slots read (screen saved: {phone.save(image, 'no-slots')})")
    await phone.tap([slots[0].center_x, slots[0].center_y + int(image.height * 0.068)], image)
    ok, image, boxes = await phone.settle(lambda _i, b: find_box(b, "done", exact=True) is not None)
    if not ok:
        raise StepFailed(f"the Pokemon picker did not open (screen saved: {phone.save(image, 'no-picker')})")
    def tiles(i: Image.Image, b: list[OCRBox]) -> list[OCRBox]:
        return sorted((x for x in b if re.fullmatch(r"cp\s*\d+", gbl_vision.normalize(x.text))
                       and x.center_y > i.height * 0.3), key=lambda x: (x.center_y // 40, x.center_x))
    for term in buddy_searches(name, level):
        await trade_local.search_picker(phone, term, force=True)
        ok, image, boxes = await phone.settle(lambda i, b: bool(tiles(i, b)), timeout=10)
        if ok:
            break
        phone.say(f"battle: nothing in the picker for {term}")
    else:
        raise StepFailed(f"no {name} in the picker (screen saved: {phone.save(image, 'no-buddy-tile')})")
    first = tiles(image, boxes)[0]
    picked_cp = re.sub(r"\D", "", first.text)
    phone.save(image, "buddy-picker")
    phone.say(f"battle: picking {name} {first.text} (search {term})")
    await phone.tap([first.center_x, first.center_y + int(image.height * 0.075)], image)
    await asyncio.sleep(1.5)
    image, boxes = await phone.read()
    done = find_box(boxes, "done", exact=True)
    if done is None:
        raise StepFailed(f"no DONE in the picker (screen saved: {phone.save(image, 'no-done')})")
    await phone.tap_box(done, image)
    # The picker slides away over the party card; a frame mid-slide reads
    # the picker's grid and "USE THIS PARTY" together (sample-android-b, 14 Nov),
    # so wait for the buddy's name on the card, then take what is there.
    ok, image, boxes = await phone.settle(
        lambda _i, b: find_box(b, "use this party") is not None and in_party(b), timeout=10)
    if not ok:
        ok, image, boxes = await phone.settle(lambda _i, b: find_box(b, "use this party") is not None)
    if not ok:
        raise StepFailed(f"the party screen did not come back (screen saved: {phone.save(image, 'no-party')})")
    if not in_party(boxes):
        report.ok = False
        report.lines.append(f"{name} is not in the party after the swap (screen saved: "
                            f"{phone.save(image, 'buddy-not-in-party')})")
    slot = party_index(image, boxes, wanted)
    await phone.tap_box(find_box(boxes, "use this party"), image)

    ok, image, boxes = await phone.settle(lambda i, b: header_cp(i, b) is not None, timeout=30)
    if not ok:
        raise StepFailed(f"the battle never started (screen saved: {phone.save(image, 'no-battle')})")
    phone.save(image, "battle-started")
    # The run button is drawn a moment after the header.  Running during the
    # "GO" countdown earned no Battle together heart on the slow sample-android-a
    # (7 Nov), so the battle is let start properly first.
    await asyncio.sleep(BATTLE_SETTLE_SECONDS)
    image, boxes = await phone.read()
    if slot and wanted not in lead_name(image, boxes):
        bench = bench_labels(image, boxes)
        if len(bench) >= slot:
            tile = bench[slot - 1]
            # The header's name does not always read (sample-android-b, 14 Nov); the
            # tile's CP moving onto the header plate proves the switch too.
            tile_cp = re.sub(r"\D", "", tile.text)
            phone.say(f"battle: switching {name} (CP {tile_cp}) in from the bench")
            await phone.tap([tile.center_x, tile.center_y + int(image.height * BENCH_TILE_DROP)], image)
            ok, image, boxes = await phone.settle(
                lambda i, b: header_cp(i, b) is not None and (
                    wanted in lead_name(i, b) or (tile_cp and tile_cp in lead_name(i, b).replace(" ", ""))),
                timeout=SWITCH_WAIT_SECONDS)
            if ok:
                await asyncio.sleep(BATTLE_SETTLE_SECONDS)
                image, boxes = await phone.read()
            else:
                report.lines.append(f"{name} did not come in from the bench (screen saved: "
                                    f"{phone.save(image, 'no-switch')})")
        else:
            report.lines.append(f"no bench tile for {name} (screen saved: {phone.save(image, 'no-bench')})")
    lead = lead_name(image, boxes).replace(" ", "")
    if picked_cp and lead and picked_cp not in lead:
        report.lines.append(f"the lead is not the CP {picked_cp} buddy picked ({lead}; screen saved: "
                            f"{phone.save(image, 'lead-not-buddy')})")
    plate = header_cp(image, boxes) or plate_fallback(image, boxes)
    for below in RUN_BELOW_HEADER:
        await phone.tap([int(image.width * RUN_X), plate.center_y + int(image.width * below)], image)
        ok, image, boxes = await phone.settle(lambda _i, b: find_box(b, "surrender and exit") is not None, timeout=4)
        if ok:
            break
    else:
        raise StepFailed(f"the run button did not answer (screen saved: {phone.save(image, 'no-run')})")
    phone.say("battle: running from it")
    # A tap while the card is still sliding in is lost (sample-receiver, 5 Nov): wait for
    # it to sit still, and press OK until the card goes.
    for _ in range(4):
        await asyncio.sleep(1.2)
        image, boxes = await phone.read()
        prompt = find_box(boxes, "surrender and exit")
        if prompt is None:
            break
        ok_box = find_box(boxes, "ok", exact=True)
        if ok_box is None:
            continue
        await phone.tap_box(ok_box, image)
    else:
        raise StepFailed(f"the Surrender card would not close (screen saved: {phone.save(image, 'no-surrender')})")
    ok, image, boxes = await phone.settle(lambda _i, b: find_box(b, "good effort", "effort") is not None, timeout=15)
    if ok:
        await asyncio.sleep(3)
        image = await phone.look()
        await phone.tap(close_button(image) or [image.width // 2, int(image.height * 0.932)], image)
        await asyncio.sleep(2)

    image, boxes = await phone.tap_first(BUDDY_PORTRAIT, buddy_profile, "the buddy profile")
    # The profile fades in over the map; read it once it is opaque (sample-android-b).
    await asyncio.sleep(2)
    image, boxes = await phone.read()
    for _ in range(2):
        battle_row = find_box(boxes, "battle together", "battle togeth")
        if battle_row is not None:
            break
        x = int(image.width * 0.2)
        await phone.swipe([x, int(image.height * 0.80)], [x, int(image.height * 0.40)], image, 0.6, scroll=True)
        await asyncio.sleep(1.5)
        image, boxes = await phone.read()
    hearts = heart_rows(image, boxes, {"battle": ("battle together", "battle togeth")})
    phone.save(image, "battle-hearts")
    if hearts["battle"] is False:
        report.ok = False
    report.detail = f"Blanche, Master League, with {name}; Battle together " + (
        "has a heart" if hearts["battle"] else "NO heart" if hearts["battle"] is False else "not read")
    await phone.go_home()
    return report


# --- Step 3: the Item Bag ------------------------------------------------------------

# Everything of these is thrown away.  Kept: Max Potion, Max Revive, Golden Razz
# Berry, Silver Pinap Berry -- and anything not named here.  Names are matched
# whole, and the discard dialog's own title must name the same item before YES,
# so "Pinap Berry" can never take a Silver Pinap Berry with it.
DISCARD_ITEMS = ("potion", "super potion", "hyper potion", "revive",
                 "razz berry", "nanab berry", "pinap berry")
# The trash can sits above-right of an item's name (sample-receiver, 1316x2560: name
# (234, 892), can (370, 556)).  Both offsets are in screen WIDTHS: the item
# grid is three tiles across on every phone, so it scales with the width; in
# heights it missed on the narrower-looking sample-android-b.  The grey glyph is then
# looked for near that point.
TRASH_FROM_LABEL = (0.103, -0.255)
TRASH_SEARCH = 0.06
COUNT_PATTERN = re.compile(r"[x×]+\s*(\d[\d,]*)", re.IGNORECASE)
PLUS_ROUNDS = 4    # read-back corrections of the quantity
PLUS_GAP = 0.45   # 0.12s between presses lost some on sample-receiver
PLUS_AT = (0.731, 0.453)   # the + disc (sample-receiver, 1316x2560: 962, 1160)
BAG_SCROLLS = 12


def items_page(boxes: Sequence[OCRBox], height: int | None = None) -> bool:
    """The Item Bag, settled: it slides up from the foot, and was read
    mid-slide with its header at 85% of the screen (sample-android-b)."""
    fill = next((box for box in boxes if re.search(r"\d{3,5}\s*/\s*\d{3,5}", box.text)), None)
    if fill is None:
        return False
    # A full bag draws "Your Item Bag is full. Upgrade Item Bag?" over the
    # ITEMS header; the n/m count still shows (sample-android-a).  Its upgrade costs
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
# differently at each size (sample-receiver's Pinap: "x2", "×2 7", "+->", nothing), so
# several looks vote.
# In screen WIDTHS, like the trash can (sample-receiver: name (238, 894), badge (152, 755)).
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
            # there ("x3" read as "3" on the sample-android-b's Revive).
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
    stops reading the + itself -- it did after a press on the sample-android-b.  Whole
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
    """The dialog's + disc: read when OCR finds it, else where it sat on sample-receiver."""
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
    # The field starts at 1 and + WRAPS past the most you hold (sample-receiver: 5 -> 1
    # -> 2), so it is pressed exactly count - 1 times, slowly enough for each
    # press to land, and read back before YES.  A lone "1" often does not
    # read at all; with nothing pressed that is the starting value.
    # The dialog zooms in; a press during that lands outside it and closes it
    # (sample-android-b).  Wait for a still frame and read the + from it.
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
    for attempt in range(3):
        image = await phone.go_home()
        ball = home.find_pokeball(image)
        if ball is None:
            raise StepFailed("no Poke Ball button on the map")
        await phone.tap(ball, image)
        ok, image, boxes = await phone.settle(
            lambda _i, b: gbl_vision.main_menu_visible(b) and find_box(b, "items", exact=True) is not None)
        if ok:
            break
        # A friendship level-up card can land on the map after go_home looked,
        # and the Poke Ball tap closes it instead (sample-receiver, 5 Nov, after gifting).
        phone.say(f"the main menu did not open; trying again ({attempt + 1}/3)")
    else:
        raise StepFailed(f"the main menu did not open (screen saved: {phone.save(image, 'no-menu')})")
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
        # a frame from mid-slide put Nanab's trash can on Revive's (sample-android-b).
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
                # ITEMS header (sample-receiver: Pinap's name at y=554 of 2560); near the
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
    try:
        return await process.wait()
    except asyncio.CancelledError:
        await pokemon_fleet.stop_child(name, process)
        raise


# --- One phone, all steps -----------------------------------------------------------

async def with_phone(spec: pokemon_fleet.DeviceSpec, args: argparse.Namespace,
                     work: Callable[[Phone], Awaitable[Report | None]]) -> Report | None:
    """Hold the phone's lock and an open connection for one stretch of work."""
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        try:
            relaunch = getattr(getattr(target, "device", None), "relaunch_if_dead", None)
            if spec.platform == "ios" and asyncio.iscoroutinefunction(relaunch):
                await relaunch()
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

    if "buddy" in wanted:
        await step("buddy", lambda phone: buddy_step(phone, args.berries))
    if "battle" in wanted and not args.dry_run:
        await step("battle", battle_step)
    if "mega" in wanted:
        await step("mega", lambda phone: mega_step(phone, args.tag))
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
            if cleared and not all(report.ok for report in cleared):
                reports.append(Report("gifts", False, "the Item Bag is full and clearing it failed"))
                break
            if not any("discarded" in line for report in cleared for line in report.lines):
                reports.append(Report("gifts", False,
                                      "the Item Bag is full of things kept; no more gifts can be opened"))
                break
        else:
            reports.append(Report("gifts", False, f"stopped after {GIFT_BAG_ROUNDS} bag clears"))
    if "gbl" in wanted and not args.dry_run:
        from . import gbl_day
        async def to_map(phone: Phone) -> None:
            await phone.go_home()
        if gbl_day.cap_reached_today(name) and not args.replay_gbl:
            # The game's battle cap was already shown on this phone today:
            # its other steps still run, the other phones still play.
            detail = "skipped: the daily battle cap was already reached today"
            print(f"[{name}] gbl {detail}", flush=True)
            reports.append(Report("gbl", True, detail))
        elif await step("walk to map", to_map):
            code = await launch(name, gbl_command(name))
            reports.append(Report("gbl", code == 0, f"gbl_day.py exited {code}"))
    # A second round with the buddy once the day's battles are done: berry,
    # play, snapshot (user, 5 Nov).
    if "buddy-again" in wanted:
        await step("buddy-again", lambda phone: buddy_step(phone, args.berries))
    if "goplus" in wanted and not args.dry_run:
        from . import goplus_android, goplus_ios

        async def connect_plus(phone: Phone) -> Report:
            if spec.platform == "ios":
                detail = await goplus_ios.reconnect(phone, timeout=args.goplus_timeout)
            else:
                devices = await screen_router.attached_androids([spec])
                device = devices.get(spec.identifier)
                if device is None:
                    raise StepFailed("Android phone disconnected")
                detail = await goplus_android.reconnect(phone, device, timeout=args.goplus_timeout)
            return Report("goplus", True, detail)

        await step("goplus", connect_plus)
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


async def special_here(
    here: Sequence[pokemon_fleet.DeviceSpec],
    args: argparse.Namespace,
    *,
    local_only: bool | None = None,
) -> Report:
    """The receiver's daily special trade (sources/special_trade.py)."""
    from . import special_trade
    forwarded = ["--shots", str(args.shots / "special-trade")]
    if getattr(args, "local_only", False) if local_only is None else local_only:
        forwarded.append("--local")
    if getattr(args, "plan", False):
        forwarded.append("--plan")
    if args.devices:
        forwarded += ["--devices", *args.devices]
    try:
        report = await special_trade.special_step(special_trade.parse_args(forwarded))
    except (StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError, ValueError, OSError) as exc:
        report = Report("special-trade", False, str(exc))
    except Exception as exc:  # the trade's surprise must not stop the rest of the day
        report = Report("special-trade", False, f"{type(exc).__name__}: {exc}")
    for line in report.lines:
        print(f"[special]   {line}", flush=True)
    print(f"[special] {'done' if report.ok else 'PROBLEM'}: {report.detail}", flush=True)
    return report


def split_around_special(steps: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split `steps` into the steps before and after `special`."""
    index = list(steps).index("special")
    return list(steps[:index]), list(steps[index + 1:])


def phase_arguments(
    forwarded: Sequence[str],
    phase_steps: Sequence[str],
    *,
    no_sleep: bool = False,
) -> list[str]:
    """Rewrite starter CLI flags to run only `phase_steps`, preserving all other options."""
    out: list[str] = []
    skip_values = False
    for token in forwarded:
        if token in {"--only", "--skip"}:
            skip_values = True
            continue
        if token.startswith("--only=") or token.startswith("--skip=") or token == "--no-sleep":
            skip_values = False
            continue
        if skip_values:
            if token.startswith("-"):
                skip_values = False
            else:
                continue
        out.append(token)
    out.extend(["--only", *phase_steps])
    if no_sleep:
        out.append("--no-sleep")
    return out


def special_selected(args: argparse.Namespace) -> tuple[bool, str]:
    """Whether the coordinator should run the cross-machine special trade, and why not if skipped."""
    if "special" not in args.steps or args.dry_run:
        return False, ""
    receiver = special_receiver()
    if not receiver:
        return False, ""
    if args.devices:
        from . import trading_accounts
        aliases = trading_accounts.aliases()
        selected = {aliases.get(name, name) for name in args.devices}
        if receiver not in selected:
            return False, f"{receiver} is not in --devices"
        if len(selected) <= 1:
            return False, f"no partner phone included with {receiver} in --devices"
    from . import special_trade
    done = special_trade.done_today()
    if done:
        return False, f"already made today: {done}"
    return True, ""


def run_special_coordinator(args: argparse.Namespace) -> Report:
    """Run the daily special trade once on the fleet coordinator across all configured machines."""
    return asyncio.run(special_here([], args, local_only=False))


def special_receiver() -> str:
    """The special-trade receiver's name, or "" when no settings are configured."""
    from . import special_trade
    try:
        special_trade.configure()
    except (ValueError, OSError, KeyError) as exc:
        print(f"[special] skipped: {exc}", flush=True)
        return ""
    return special_trade.RECEIVER


def pair_steps(here: Sequence[pokemon_fleet.DeviceSpec], args: argparse.Namespace) -> list[str]:
    """The pair steps this computer actually takes, so its phones wait for each other there."""
    if args.dry_run:
        return []
    names = {spec.name for spec in here}
    active = []
    if "special" in args.steps:
        receiver = special_receiver()
        if receiver in names and len(here) > 1:
            active.append("special")
        else:
            print(f"[special] skipped: {receiver or 'the receiver'} has no partner attached beside it here",
                  flush=True)
    if "trade" in args.steps:
        if len(here) == 2:
            active.append("trade")
        else:
            print(f"[trade] skipped: {len(here)} attached here; trading needs exactly two", flush=True)
    return active


def phases(steps: Sequence[str], barriers: Sequence[str]) -> list[tuple[set[str], str | None]]:
    """Split the day at each pair step: (the steps each phone runs alone, then the pair step)."""
    out: list[tuple[set[str], str | None]] = []
    alone: set[str] = set()
    for step in steps:
        if step in barriers:
            out.append((alone, step))
            alone = set()
        elif step not in PAIR_STEPS:
            alone.add(step)
    out.append((alone, None))
    return out


async def last_steps(spec, args, steps, sleep_after):
    try:
        reports = await run_phone(spec, args, steps) if steps else []
    except Exception as exc:
        reports = [Report("final steps", False, f"{type(exc).__name__}: {exc}")]
    if sleep_after and not args.dry_run:
        from . import display_sleep
        try:
            await display_sleep.sleep_phone(spec)
            reports.append(Report("display", True, "verified screen off"))
        except Exception as exc:
            reports.append(Report("display", False, str(exc)))
        print(f"[{spec.name}] display: {reports[-1].detail}", flush=True)
    return reports


async def run(args: argparse.Namespace, *, sleep_after: bool = True, local_only: bool = False) -> int:
    args.local_only = local_only
    goplus_only = args.steps == ["goplus"]
    effective_sleep = sleep_after and not getattr(args, "no_sleep", False) and not goplus_only
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    wanted = set(args.devices or [])
    specs = [spec for spec in fleet.devices.values() if spec.enabled and (not wanted or spec.name in wanted)]
    androids = await screen_router.attached_androids(specs)
    iphones = pokemon_fleet.ios_connected_udids()
    here = [spec for spec in specs if spec.identifier in (androids if spec.platform == "android" else iphones)]
    if not here:
        print("No fleet phone is attached to this computer")
        return int(bool(wanted) and not args.allow_empty)
    print(f"Starting the day on {len(here)} phone(s): " + ", ".join(spec.name for spec in here)
          + f"; steps: {' -> '.join(args.steps)}" + (" (dry run)" if args.dry_run else ""), flush=True)
    if args.plan:
        for spec in here:
            print(f"[{spec.name}] would run: {' -> '.join(args.steps)}")
            if effective_sleep and not args.dry_run:
                print(f"[{spec.name}] would switch its display off after its last step")
            if "special" in args.steps:
                print(f"[{spec.name}]   special: daily special trade (cross-machine via coordinator, or local-only with --local)")
            if "gifts" in args.steps:
                print(f"[{spec.name}]   then " + " ".join(gifts_command(spec.name)))
            if "gbl" in args.steps:
                from . import gbl_day
                if gbl_day.cap_reached_today(spec.name) and not args.replay_gbl:
                    print(f"[{spec.name}]   gbl: skipped, today's battle cap is already reached")
                else:
                    print(f"[{spec.name}]   then " + " ".join(gbl_command(spec.name)))
        return 0
    if not args.dry_run and not goplus_only:
        # Once at the start of the day: sample-receiver's low-memory killer took the game
        # mid-catch on 16 Nov 2026.  See android_memory.
        from . import android_memory
        await asyncio.to_thread(android_memory.free_named, [spec.name for spec in here])
    # Reserve each iPhone across step and subprocess boundaries. A busy phone
    # sits this run out while the other phones continue normally.
    with ExitStack() as cleanup:
        available = []
        reservations = {}
        for spec in here:
            if spec.platform == "ios":
                reservation = ExitStack()
                try:
                    reservation.enter_context(starter_ios_session.reserve(spec))
                except pokemon_fleet.FleetError as exc:
                    print(f"[{spec.name}] starter could not reserve phone: {exc}", flush=True)
                    continue
                reservations[spec.name] = reservation
                cleanup.callback(reservation.close)
            available.append(spec)
        code = await run_steps(available, args, effective_sleep, reservations) if available else 1
        return code or int(len(available) != len(here))


async def run_steps(here, args, sleep_after, reservations):
    async def finish(spec, steps):
        reports = await last_steps(spec, args, steps, sleep_after)
        reservation = reservations.get(spec.name)
        if reservation is not None:
            try:
                await asyncio.to_thread(reservation.close)
            except Exception as exc:
                reports.append(Report("iPhone session cleanup", False, str(exc)))
        return reports

    # Without a pair step each phone goes straight through its day: waiting
    # at a barrier left sample-android-a idle on a friend profile while sample-receiver still
    # gifted (7 Nov).
    results: list[list[Report]] = [[] for _ in here]
    for alone, pair_step in phases(args.steps, pair_steps(here, args)):
        if alone or pair_step is None:
            work = (finish(spec, alone) if pair_step is None
                    else run_phone(spec, args, alone) for spec in here)
            more = await asyncio.gather(*work, return_exceptions=True)
            for reports, extra in zip(results, more):
                if isinstance(extra, Exception):
                    reports.append(Report("starter steps", False, str(extra)))
                elif isinstance(extra, BaseException):
                    raise extra
                else:
                    reports.extend(extra)
        if pair_step is None:
            continue
        pair_report = await (special_here(here, args) if pair_step == "special" else trade_here(here, args))
        if pair_report is not None:
            for reports in results:
                reports.append(pair_report)
    print("\nSummary", flush=True)
    failed = False
    for spec, reports in zip(here, results):
        for report in reports:
            failed |= not report.ok
            print(f"  [{spec.name}] {report.step}: {'ok' if report.ok else 'PROBLEM'} - {report.detail}", flush=True)
    return 1 if failed else 0


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", nargs="+", help="all or these fleet device names")
    parser.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--skip", nargs="+", choices=STEPS, default=[], help="steps to leave out")
    parser.add_argument("--only", nargs="+", choices=STEPS, help="run just these steps")
    parser.add_argument("--tag", default="mega", help="the Pokemon tag to Mega Evolve from (default: mega)")
    parser.add_argument("--berries", type=int, default=MAX_BERRIES,
                        help=f"most Razz Berries to feed the buddy in this session; stops early once it is full "
                             f"(default and most: {MAX_BERRIES})")
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
    parser.add_argument("--replay-gbl", action="store_true",
                        help="launch GBL even on phones that already reached today's battle cap")
    parser.add_argument("--no-sleep", action="store_true",
                        help="leave phone displays on after the last step")
    parser.add_argument("--dry-run", action="store_true",
                        help="walk the mega and buddy screens and report, but evolve, feed, "
                             "photograph and pet nothing; gifts and GBL are skipped")
    from .goplus_android import DEFAULT_TIMEOUT

    parser.add_argument("--goplus-timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds to verify a Go Plus connection")
    args = parser.parse_args(arguments)
    if args.devices and "all" in args.devices:
        if args.devices != ["all"]:
            parser.error("use --devices all by itself")
        args.devices = None
    if not 0 <= args.berries <= MAX_BERRIES:
        parser.error(f"--berries must be 0 to {MAX_BERRIES}")
    if not args.goplus_timeout >= 1:
        parser.error("--goplus-timeout must be at least one second")
    # Go Plus runs on its own hourly schedule. Trading remains opt-in.
    chosen = args.only or [step for step in STEPS if step != "goplus" and (step != "trade" or args.trade)]
    args.steps = [step for step in STEPS if step in chosen and step not in args.skip]
    if not args.steps:
        parser.error("no steps left to run")
    args.shots = (args.shots or config_paths.state_dir() / "starter").expanduser()
    return args


def main(argv: Sequence[str] | None = None, *, sleep_after: bool = True, local_only: bool = False) -> int:
    return asyncio.run(run(parse_args(argv), sleep_after=sleep_after, local_only=local_only))
