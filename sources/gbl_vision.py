#!/usr/bin/env python3
"""Small macOS Vision OCR bridge for GBL menu and opponent recognition."""

from __future__ import annotations

from . import config_paths

import atexit
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import threading
from typing import Iterable, Sequence
import unicodedata

from PIL import Image


CACHE_DIR = config_paths.state_dir() / "cache"


SWIFT_SOURCE = r'''
import AppKit
import Foundation
import Vision

// Two ways in. With a path argument it reads one image and exits, which is
// how this started and what every fallback still uses. With no arguments it
// serves: one image path per line on stdin, the same JSON lines back, then a
// terminator. Starting the process and warming Vision costs 0.40 s and the
// recognition itself only 0.05-0.15 s, so a battle that OCRs three times a
// read spent more than a second a cycle re-paying the start-up.
func recognize(_ path: String) {
    guard let image = NSImage(contentsOfFile: path),
          let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
    else {
        FileHandle.standardError.write(Data("unreadable image\n".utf8))
        return
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["en-US"]
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        FileHandle.standardError.write(Data("recognition failed\n".utf8))
        return
    }
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let payload: [String: Any] = [
            "text": candidate.string,
            "confidence": candidate.confidence,
            "x": box.origin.x,
            "y": box.origin.y,
            "width": box.size.width,
            "height": box.size.height,
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { continue }
        print(String(data: data, encoding: .utf8)!)
    }
}

setvbuf(stdout, nil, _IONBF, 0)

if CommandLine.arguments.count == 2 {
    recognize(CommandLine.arguments[1])
    exit(0)
}

while let line = readLine(strippingNewline: true) {
    if line.isEmpty { continue }
    recognize(line)
    print(SERVER_TERMINATOR)
}
'''


OCR_TERMINATOR = "<<<gbl-ocr-end>>>"
SWIFT_SOURCE = SWIFT_SOURCE.replace("SERVER_TERMINATOR", f'"{OCR_TERMINATOR}"')
# A hung Vision call has never been seen; this only stops one taking a leg
# down with it. It is the same bound the one-shot call has always used.
OCR_FIRST_LINE_TIMEOUT = 12.0
# The first recognition any given helper binary runs also pays for Vision's
# text model being compiled for the Neural Engine, and that compiled model is
# cached per binary. A macOS upgrade throws the cache away, so on an upgraded
# Mac every leg paid it again: 14.4 s measured on both Macs after macOS 27,
# which is over the bound above. Each leg was killed mid-compile, cached
# nothing, and the next leg started the same compile from scratch -- no phone
# ever got a read. A warm call costs 0.17 s. Paid once per helper per machine.
OCR_COLD_START_TIMEOUT = 180.0


class VisionOCRError(RuntimeError):
    pass


@dataclass(frozen=True)
class OCRBox:
    text: str
    confidence: float
    x: int
    y: int
    width: int
    height: int

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2


# The helper is named after its own source. Two versions of this file can be
# running at once -- a fleet upgrade only reaches a leg when that leg restarts
# -- and a single shared path had them overwriting each other's helper mid
# build ("input file was modified during the build"). Each build is also made
# under a private name and moved into place, so a half-written binary is never
# the one a leg picks up.
def ocr_paths() -> tuple[Path, Path]:
    stamp = hashlib.sha256(SWIFT_SOURCE.encode()).hexdigest()[:12]
    return (
        CACHE_DIR / f"gbl-vision-ocr-{stamp}",
        CACHE_DIR / f"gbl-vision-ocr-{stamp}.swift",
    )


def ensure_ocr_binary() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    binary, source = ocr_paths()
    if binary.is_file():
        return binary
    source.write_text(SWIFT_SOURCE)
    building = binary.with_suffix(f".{os.getpid()}")
    result = subprocess.run(
        [
            "xcrun", "swiftc", str(source),
            "-framework", "Vision", "-framework", "AppKit",
            "-o", str(building),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode:
        building.unlink(missing_ok=True)
        raise VisionOCRError(result.stderr.strip() or "Could not compile Vision OCR helper")
    building.replace(binary)
    return binary


# --- The OCR helper is kept alive between calls ---
# Starting it and warming Vision costs 0.40 s; recognizing a whole 1224x2992
# battle frame after that costs 0.05-0.15 s. Measured on a android-three frame: 0.45 s
# per one-shot call against 0.11 s once the process is up. A battle read OCRs
# the charged prompt, sometimes a shield prompt and every third read the
# opponent's name, so the phone spent about a second of every attack cycle
# waiting for processes to start rather than tapping. One server per process
# under a lock: the legs are separate processes, and inside a leg the calls
# come from `asyncio.to_thread`, so they must not interleave on the pipe.
_server_lock = threading.Lock()
_server: subprocess.Popen | None = None
_ocr_warmed = False


def _start_server() -> subprocess.Popen:
    return subprocess.Popen(
        [str(ensure_ocr_binary())],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )


def _stop_server() -> None:
    global _server
    process, _server = _server, None
    if process is None:
        return
    try:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=2)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        process.kill()


atexit.register(_stop_server)


def _server_read(process: subprocess.Popen, timeout: float) -> str:
    """Everything the server says about one image, up to its terminator.

    The wait for the *first* line is bounded, because that is where a stuck
    recognition would show; after it the rest of the response is already
    written and reading it must not consult select, which cannot see lines
    sitting in this end's buffer.
    """
    assert process.stdout is not None
    if not select.select([process.stdout], [], [], timeout)[0]:
        raise VisionOCRError("Vision OCR server stopped answering")
    lines: list[str] = []
    while True:
        line = process.stdout.readline()
        if not line:
            raise VisionOCRError("Vision OCR server exited")
        if line.strip() == OCR_TERMINATOR:
            return "".join(lines)
        lines.append(line)


def _recognize_file(path: str) -> str:
    """OCR one file, preferring the running server and falling back to a run.

    A server that misbehaves is torn down and the image is read the old way,
    so the worst case is the cost this had before it existed rather than a
    failed read in the middle of a battle.
    """
    global _server, _ocr_warmed
    budget = OCR_FIRST_LINE_TIMEOUT if _ocr_warmed else OCR_COLD_START_TIMEOUT
    if os.environ.get("GBL_OCR_SERVER") != "0":
        with _server_lock:
            for _ in range(2):
                if _server is None or _server.poll() is not None:
                    _server = _start_server()
                try:
                    assert _server.stdin is not None
                    _server.stdin.write(path + "\n")
                    _server.stdin.flush()
                    answer = _server_read(_server, budget)
                    _ocr_warmed = True
                    return answer
                except (OSError, ValueError, AssertionError, VisionOCRError):
                    _stop_server()
    result = subprocess.run(
        [str(ensure_ocr_binary()), path],
        capture_output=True,
        text=True,
        timeout=budget,
        check=False,
    )
    if result.returncode:
        raise VisionOCRError(result.stderr.strip() or "Vision OCR failed")
    _ocr_warmed = True
    return result.stdout


def recognize(image: Image.Image, crop: tuple[int, int, int, int] | None = None) -> list[OCRBox]:
    """Recognize positioned text, returning coordinates in the original image."""

    source = image.convert("RGB")
    left = top = 0
    if crop is not None:
        left, top, right, bottom = crop
        source = source.crop((left, top, right, bottom))
    with tempfile.NamedTemporaryFile(suffix=".png") as handle:
        source.save(handle.name)
        output = _recognize_file(handle.name)
    boxes: list[OCRBox] = []
    for line in output.splitlines():
        try:
            value = json.loads(line)
            x = left + round(float(value["x"]) * source.width)
            # Vision uses a bottom-left origin; screenshots use top-left.
            y = top + round((1.0 - float(value["y"]) - float(value["height"])) * source.height)
            width = round(float(value["width"]) * source.width)
            height = round(float(value["height"]) * source.height)
            boxes.append(
                OCRBox(
                    str(value["text"]), float(value["confidence"]),
                    x, y, width, height,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return boxes


def lines(boxes: Iterable[OCRBox], minimum_confidence: float = 0.25) -> list[str]:
    return [box.text for box in boxes if box.confidence >= minimum_confidence]


# How far from the middle an action label may sit, as a fraction of width. The
# moto's menu icons are 0.28 of the width off centre; GBL's pill labels are
# within 0.05 of it.
ACTION_CENTRE_SPAN = 0.18

ACTION_LABELS = {
    "battle",
    "use this party",
    "next battle",
    "let s go",
    "continue",
}

# The end-of-set buttons.  These are orange rather than green, so they are found
# by their own colour test and this set only grants permission to press them.
REWARD_LABELS = {
    "collect",
    "claim",
    "claim reward",
    "claim rewards",
    "claim rank rewards",
    "tap to claim",
}

# Green pills this loop must never press.  Pokemon GO draws several of them in
# the same slot as NEXT BATTLE: a friendship level-up arrives unprompted after a
# battle and offers USE LUCKY EGG exactly where the result pill was, and no
# colour or shape test can tell the two apart -- only the label can.  Anything
# that spends an item or opens a purchase belongs here.
BLOCKED_LABELS = {
    "use lucky egg",
    "use incense",
    "use star piece",
    "use lure module",
    "use daily adventure incense",
    "buy",
    "buy now",
    "purchase",
    "get more",
    "power up",
    "evolve",
    "transfer",
}

# The main menu, which is the way back into GBL from the map.
MENU_LABELS = {"pokedex", "shop", "items", "pokemon"}


def normalize(text: str) -> str:
    """Fold a label to bare lowercase words: "CLAIM RANK REWARDS!" -> "claim rank rewards".

    Accents are stripped so POKEDEX and POKÉDEX are one label; Vision returns
    either depending on the rendering.
    """
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    stripped = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(
        "".join(character if character.isalnum() else " " for character in stripped).split()
    )


def labels(boxes: Iterable[OCRBox], minimum_confidence: float = 0.25) -> set[str]:
    return {
        normalize(box.text)
        for box in boxes
        if getattr(box, "confidence", 1.0) >= minimum_confidence
    }


def blocked_label(boxes: Iterable[OCRBox]) -> str | None:
    """The text of anything on screen this loop is forbidden to press."""
    for box in boxes:
        if normalize(box.text) in BLOCKED_LABELS:
            return box.text
    return None


def reward_visible(boxes: Iterable[OCRBox]) -> bool:
    """Whether an end-of-set reward button is named on this screen."""
    return bool(labels(boxes) & REWARD_LABELS)


# Text that only the GO BATTLE LEAGUE card and its league chooser carry. These
# are matched as substrings because the line they sit in varies: "4/5 battles
# played" normalizes to "4 5 battles played".
GBL_CARD_MARKERS = (
    "battles played",
    "go battle league",
    "choose your league",
    "basic rewards",
    "premium rewards",
)


def season_info_visible(boxes: Iterable[OCRBox]) -> bool:
    """Recognize season article prose without depending on the season name."""
    seen = " ".join(normalize(box.text) for box in sorted(
        boxes, key=lambda box: (box.y, box.x)) if box.confidence >= 0.25)
    return (
        "dates and times below are listed in local" in seen
        or ("go battle league" in seen and any(marker in seen for marker in (
            "season schedule", "battle league schedule",
            "leagues will begin and end",
        )))
    )


def gbl_card_visible(boxes: Iterable[OCRBox]) -> bool:
    """Whether the GBL menu card is on screen.

    The card's teal close button sits at the same place, and is the same colour,
    as the post-battle checkmark drawn over the battlefield. Only the text
    behind them tells the two apart, and pressing the wrong one leaves GBL.
    """
    boxes = list(boxes)
    if season_info_visible(boxes):
        return False
    seen = " | ".join(labels(boxes))
    return any(marker in seen for marker in GBL_CARD_MARKERS)


# A Pokemon's own page: the one the game opens on the Pokemon a set's reward
# encounter just caught. Nothing on it belongs to GBL, but its provenance line
# reads "CAUGHT IN THE GBL MASTER LEAGUE: MEGA EDITION" -- which contains both
# "league" and a cup name, so the league test claims the page and the loop taps
# a move list as though it were a card. The SE did that every five seconds
# until it was closed by hand. POWER UP alone is not enough: it is a blocked
# label the game prints on other pages too, so the page must also carry one of
# its own headings.
POKEMON_DETAIL_MARKERS = (
    "stardust",
    "candy",
    "swap buddies",
    "gyms raids",
    "trainer battles",
)


def pokemon_detail_visible(boxes: Iterable[OCRBox]) -> bool:
    """Whether a Pokemon's detail page is covering the game."""
    seen = " | ".join(labels(boxes))
    if "power up" not in seen:
        return False
    return any(marker in seen for marker in POKEMON_DETAIL_MARKERS)


def set_completed(boxes: Iterable[OCRBox]) -> bool:
    """Whether all battles in the current set have been played (e.g. 5/5 battles played)."""
    seen = " | ".join(labels(boxes))
    return bool(re.search(r"\b5\s*[/5]?\s*5\s+battles\s+played\b", seen))


# The reward-tier chooser is the GO BATTLE LEAGUE card's first page, and it
# prints the season's leagues as a sentence: "Great League, Scroll Cup: Great
# League Edition, Competitors Cup".  Every substring test for a league or a cup
# matches that prose, so the chooser has to be able to name itself before any
# of them run.  Its prompt and its two ribbons are the only lines on it that a
# real league list never carries.
REWARD_TIER_MARKERS = (
    "choose a reward tier",
    "basic rewards",
    "premium rewards",
)


def reward_tier_prompt_visible(boxes: Iterable[OCRBox]) -> bool:
    """Whether the reward-tier chooser named itself on screen."""
    seen = " | ".join(labels(boxes))
    return any(marker in seen for marker in REWARD_TIER_MARKERS)


# Under the week's playable cards the chooser draws a divider -- "SEE WHAT'S
# COMING NEXT ON SEPTEMBER 22!" -- and beneath it next week's line-up, greyed
# out and inert.  Those previews read like ordinary cards to OCR, and a
# configured league that is out of rotation is usually sitting in exactly that
# section: on 16 Sep the SE scrolled past Great League to the Master League:
# Mega Edition preview and tapped it every five seconds all day, 0 battles.
UPCOMING_LEAGUE_MARKERS = ("coming next", "coming up next", "available next")


# A card the week is actually offering sits on an opaque white panel.  The
# previews below the divider are drawn over the map at roughly half opacity, so
# the same band reads about 145 instead of about 245.  Measured on the SE on
# 16 Sep: live cards 242-252 with 4% of their band below 200, previews 142-155
# with all of it below.  The divider itself scrolls off the top of the list, so
# its absence proves nothing -- this does not scroll away.
LIVE_CARD_LEVEL = 200
LIVE_CARD_SHARE = 0.5


def league_card_live(image, box: OCRBox) -> bool:
    """Whether this card is one of today's leagues rather than a preview."""
    top = max(0, box.y - box.height // 2)
    bottom = min(image.height, box.y + box.height + box.height // 2)
    # Right of the league emblem, inside the panel: the emblem is dark on every
    # card, live or not, and the margins either side of the panel are map.
    left = int(image.width * 0.40)
    right = int(image.width * 0.92)
    if bottom <= top or right <= left:
        return True
    band = image.crop((left, top, right, bottom)).convert("L")
    pixels = getattr(band, "get_flattened_data", band.getdata)()
    total = len(pixels)
    if not total:
        return True
    bright = sum(1 for value in pixels if value >= LIVE_CARD_LEVEL)
    return bright >= total * LIVE_CARD_SHARE


def live_league_boxes(image, boxes: Iterable[OCRBox]) -> list[OCRBox]:
    """The cards worth tapping, or all of them if none look like cards at all.

    In synthetic test environments with flat mock images (no bright pixels
    anywhere), preserve all boxes so unit tests can evaluate geometry without
    pixel rendering. On real screens, return only cards with opaque white panels
    to prevent untappable preview cards from ever being tapped.
    """
    box_list = list(boxes)
    extrema = image.convert("L").getextrema()
    if extrema[1] < LIVE_CARD_LEVEL:
        return box_list
    return [box for box in box_list if league_card_live(image, box)]


def upcoming_league_divider_y(boxes: Iterable[OCRBox]) -> int | None:
    """Top of the "what's coming next" divider, if the chooser is showing it.

    Cards below this line are previews of the next rotation.  They cannot be
    tapped, so nothing under it is worth choosing.
    """
    tops = [
        box.y
        for box in boxes
        if any(marker in normalize(box.text) for marker in UPCOMING_LEAGUE_MARKERS)
    ]
    return min(tops) if tops else None


def main_menu_visible(boxes: Iterable[OCRBox]) -> bool:
    """Whether this is Pokemon GO's main menu.

    Two labels rather than one: BATTLE alone also appears on the GBL card and on
    the map's own tab, and tapping a menu that is not there wakes something else.
    """
    return len(labels(boxes) & MENU_LABELS) >= 2


# How far below its label the main menu draws the icon it belongs to, as a
# fraction of screen height. The label is what OCR can find; the circle beneath
# it is what takes the tap. Measured on the moto at 720x1600: label at y=816,
# circle at y=905.
MENU_ICON_DROP = 0.055


def menu_battle_point(boxes: Iterable[OCRBox], image_height: int) -> list[int] | None:
    """Where the main menu's BATTLE icon sits, found by its own label."""
    for box in boxes:
        if normalize(box.text) == "battle":
            return [box.center_x, box.center_y + int(image_height * MENU_ICON_DROP)]
    return None


# How far above its label an earned reward draws the tile that takes the tap,
# as a fraction of screen height. The same shape as the main menu, the other way
# up: the word is what OCR can find, the graphic over it is the button.
# Measured on the moto at 720x1600: COLLECT label at y=1247, tile at y=1162.
REWARD_ICON_RISE = 0.053

# A reward label is either a tile's caption or a band's own text, and the two
# want opposite taps. Captions are narrow -- COLLECT measured 0.15 of the screen
# width on the moto and on the SE alike -- while CLAIM RANK REWARDS! is written
# across a band 0.64 wide, where the label is the button and lifting the tap off
# it lands on bare card. Anything under this is a caption with its tile above.
REWARD_BAND_WIDTH = 0.35


# What is left of COLLECT or SELECT when the card cuts the label. The tile row
# scrolls, and a claimable tile is regularly parked half off the left edge, so
# its button reads 'LLECT'; on the SE the card's teal close X lands on the same
# word and leaves 'CT'. Neither is a reward label, so the reward went unseen and
# the tile beside it -- locked, captioned "3 wins" -- took the tap instead. That
# opens the rank roster, which the loop closes and then taps again: the SE spent
# 20 minutes on that five-second circle with its set unclaimed behind it. Three
# letters is the shortest fragment no other word on the card can produce.
REWARD_FRAGMENT_WORDS = ("collect", "select")
REWARD_FRAGMENT_MIN = 3


def reward_fragment(text: str) -> bool:
    """Whether this label is a cut-off COLLECT or SELECT rather than a word."""
    word = "".join(character for character in normalize(text) if character.isalpha())
    if len(word) < REWARD_FRAGMENT_MIN:
        return False
    return any(word in full for full in REWARD_FRAGMENT_WORDS)


def reward_point(
    boxes: Iterable[OCRBox],
    image_width: int,
    image_height: int,
) -> list[int] | None:
    """Where a finished set's reward sits, found by its own label.

    The end-of-set card draws these tiles orange, but they are small enough that
    the orange scan walks between them, and the spent BATTLE pill below is grey
    rather than green -- so neither colour test names this screen while OCR
    still reads BATTLE off the dead pill. A live run tapped that pill eight
    times, hit the stall guard and stopped for the day with the set's rewards
    sitting unclaimed and the next set unreachable behind them.

    Left-most first: the card hands its tiles out in that order. The colour test
    cannot do this job -- run it along that row and it lands in the gap between
    two tiles, which is what left the SE pressing nothing eight times.
    """
    seen = " | ".join(labels(boxes))
    if "choose a reward tier" in seen:
        return None

    claimed = [
        box for box in boxes
        if normalize(box.text) in REWARD_LABELS
        or "claim" in normalize(box.text)
        or reward_fragment(box.text)
    ]
    if claimed:
        safe_x = int(image_width * 0.38)
        lower_band = [
            b for b in claimed
            if b.width >= image_width * REWARD_BAND_WIDTH
            or ("claim" in normalize(b.text) and b.center_y >= image_height * 0.70)
        ]
        if lower_band:
            box = min(lower_band, key=lambda candidate: candidate.center_y)
            return [safe_x, box.center_y]
        box = min(claimed, key=lambda candidate: candidate.center_x)
        if box.width >= image_width * REWARD_BAND_WIDTH:
            return [safe_x, box.center_y]
        return [box.center_x, box.center_y - int(image_height * REWARD_ICON_RISE)]

    # If the set is complete (5/5 battles played), the BATTLE pill is dead.
    # Tap the unclaimed reward bubbles above the 'N wins' captions or under BASIC REWARDS.
    if set_completed(boxes):
        win_boxes = [
            box for box in boxes
            if re.search(r"\b\d+\s+wins?\b", normalize(box.text))
        ]
        if win_boxes:
            box = min(win_boxes, key=lambda candidate: candidate.center_x)
            return [box.center_x, box.y - int(image_height * 0.04)]
        basic = [box for box in boxes if "basic rewards" in normalize(box.text)]
        if basic:
            return [int(image_width * 0.15), basic[0].center_y + int(image_height * 0.08)]
    return None


# A reward tile that has not been earned is captioned with the win it wants --
# "3 wins", "4 wins" -- where an earned one carries its COLLECT button instead.
# That caption is the only thing on the card that tells the two apart: both
# tiles are the same size, sit in the same row, and answer the colour scan the
# same way. Tapping an unearned one opens "Pokemon available at your rank",
# which is a modal, not progress.
LOCKED_TILE_CAPTION = re.compile(r"\b\d+\s+wins?\b")

# How far a caption may sit from the tap it disqualifies: half a tile across,
# and from just above the point to well below it. Measured on the SE at
# 750x1334, where the tile took a tap at (412, 928) and its caption read at
# (411, 993) -- 0.049h below.
LOCKED_TILE_SPREAD_X = 0.09
LOCKED_TILE_SPREAD_Y = (-0.01, 0.10)


def locked_tile_captions(boxes: Iterable[OCRBox]) -> list[OCRBox]:
    """Every "N wins" caption on the card, one per unearned reward tile."""
    return [box for box in boxes if LOCKED_TILE_CAPTION.search(normalize(box.text))]


# How near a reward point has to be to one that closed the GO BATTLE LEAGUE card
# to count as the same control, as a fraction of the screenshot's width.  The
# tiles in the reward row sit 0.22w apart on the SE, so this is loose enough to
# absorb the few pixels OCR moves a label between reads and still far too tight
# to write off the tile next door.
DISMISSING_REWARD_RADIUS = 0.05


def reward_refused(
    point: Sequence[int],
    refused: Sequence[Sequence[int]],
    image_width: int,
) -> bool:
    """Whether this end-of-set point has already been caught closing the card.

    The card's dismiss control is drawn over the reward row, so a point picked
    off that row can be the thing that throws the set away rather than the thing
    that collects it.  Which one it is cannot be settled from the picture -- the
    SE's teal X sits within a tile's width of tiles the loop is right to press,
    and the row scrolls, so no fixed exclusion zone tells them apart.  What does
    settle it is what happened last time: on 10 Sep 2026 the SE tapped [214, 596]
    on a finished set, landed on the map, walked back through the pokeball and
    the BATTLE disc, and tapped the same point again, over and over, until its
    leg was written off with the set unclaimed.  A point that put the phone on
    the map is not a reward, and this run does not spend a second set finding
    that out again.
    """
    limit = image_width * DISMISSING_REWARD_RADIUS
    return any(
        abs(point[0] - other[0]) <= limit and abs(point[1] - other[1]) <= limit
        for other in refused
    )


def locked_reward_tile(
    boxes: Iterable[OCRBox],
    point: list[int],
    image_width: int,
    image_height: int,
) -> bool:
    """Whether a tap found by colour has landed on a tile that is not earned yet."""
    for caption in locked_tile_captions(boxes):
        if abs(caption.center_x - point[0]) > image_width * LOCKED_TILE_SPREAD_X:
            continue
        offset = (caption.center_y - point[1]) / image_height
        if LOCKED_TILE_SPREAD_Y[0] <= offset <= LOCKED_TILE_SPREAD_Y[1]:
            return True
    return False


def reward_row_point(
    boxes: Iterable[OCRBox],
    image_width: int,
    image_height: int,
) -> list[int] | None:
    """The middle of the reward tile row, to drag it back to its first tile.

    The row fills left to right and scrolls, so the tile a finished set owes is
    the left-most one -- and it is regularly parked off the left edge with only
    locked tiles in view. There is nothing to press in that state: the fix is
    the one the phones were rescued with by hand, which is to pull the row
    right until the earned tile is whole.
    """
    captions = locked_tile_captions(boxes)
    if not captions:
        return None
    row = min(caption.center_y for caption in captions)
    return [image_width // 2, row - int(image_height * REWARD_ICON_RISE)]


# The name plate a catch screen draws across its middle: "<name> / CP<number>",
# and on the reward encounter it is the only text on the screen. The CP is what
# makes it unmistakable -- a bare species name appears on the party screen, the
# battle header and half the menus, but only a wild Pokemon is captioned with
# its CP in the middle of an otherwise empty screen.
ENCOUNTER_PLATE = re.compile(r"\bcp ?\d+\b")

# Where that plate sits, as a fraction of screen height. Measured on the moto at
# 720x1600: plate at y=562, so 0.35h, with room either side for taller layouts.
ENCOUNTER_PLATE_BAND = (0.25, 0.50)


def encounter_plate(boxes: Iterable[OCRBox], image_height: int) -> OCRBox | None:
    """The name/CP plate of a catch screen, if this is one.

    A finished set pays out as an encounter often enough to matter, drawn over
    the GO BATTLE LEAGUE card. Nothing on that screen is a button this loop
    knows, so it read as unrecognised and the run spent its recovery scrolling
    and pressing exit doors while the next set sat unreachable behind it.
    """
    for box in boxes:
        if not ENCOUNTER_PLATE_BAND[0] <= box.center_y / image_height <= ENCOUNTER_PLATE_BAND[1]:
            continue
        if ENCOUNTER_PLATE.search(normalize(box.text)):
            return box
    return None


def dismiss_point(boxes: Iterable[OCRBox]) -> list[int] | None:
    """Where a summary card's OK sits, found by its own label.

    The XP card after a catch is a green pill in the same slot as NEXT BATTLE
    and is deliberately not an ACTION_LABEL: OK is written on dialogs this loop
    has no business pressing blind. It is pressed only from inside the catch
    routine, which knows what it just did.
    """
    for box in boxes:
        if normalize(box.text) == "ok":
            return [box.center_x, box.center_y]
    return None


# Pokemon GO draws a safety warning before the map on every cold start: a white
# card, a yellow hazard triangle, and one OK.  Two markers are wanted rather
# than one because a single word is a thin reason to press a dialog.
SAFETY_NOTICE_MARKERS = ("stay aware", "surroundings", "dangerous areas")


def safety_notice_visible(boxes: Iterable[OCRBox]) -> bool:
    """Whether the start-up safety warning is covering the game."""
    seen = " | ".join(labels(boxes))
    return sum(marker in seen for marker in SAFETY_NOTICE_MARKERS) >= 2


def _notice_button(pixel) -> bool:
    """Whether this pixel belongs to the warning's green-to-teal OK pill."""
    red, green, blue = pixel[:3]
    return green > 180 and green > red + 30 and green > blue + 10


def safety_notice_point(boxes: Iterable[OCRBox], image=None) -> list[int] | None:
    """The OK on the start-up safety warning, or None when it is not up.

    Pressed by name rather than through `dismiss_point`, which finds any OK at
    all: this one is only safe to press because the words behind it say what
    the dialog is.  Nothing gets past it on its own, so a relaunched game waits
    here indefinitely -- the moto-g spent a whole leg's reads tapping map
    coordinates through this card on 22 Sep 2026.

    Vision often cannot read the button: its letters are spaced so widely that
    the moto-g's OCR returned the title and the body and nothing else.  So the
    text is tried first, and then the pill is found by its own colour, below
    the writing, in the middle of the card where it is always drawn.
    """
    boxes = list(boxes)
    if not safety_notice_visible(boxes):
        return None
    for box in boxes:
        # "O K" as often as "OK", for the same letter spacing.
        if normalize(box.text).replace(" ", "") == "ok":
            return [box.center_x, box.center_y]
    if image is None:
        return None
    return _notice_button_point(image, boxes)


def _notice_button_point(image, boxes: Sequence[OCRBox]) -> list[int] | None:
    written = [box.y + box.height for box in boxes if box.center_y > image.height * 0.3]
    top = max(written) if written else int(image.height * 0.6)
    pixels = image.convert("RGB").load()
    x = image.width // 2
    rows = [y for y in range(top, int(image.height * 0.95))
            if _notice_button(pixels[x, y])]
    if not rows:
        return None
    # The pill is one solid band; anything shorter is a stray pixel or an edge.
    band: list[int] = [rows[0]]
    best: list[int] = []
    for y in rows[1:]:
        if y - band[-1] <= 3:
            band.append(y)
        else:
            best, band = (max(best, band, key=len)), [y]
    best = max(best, band, key=len)
    if len(best) < image.height * 0.02:
        return None
    return [x, (best[0] + best[-1]) // 2]


# The highest a real action label can sit, as a fraction of screen height.
# The top tab's BATTLE sits at 0.102 and has to stay out, which is what this
# cut is for -- but 0.35 cut too deep. With the reward ribbon, the season panel
# and the NEARBY BATTLE card all drawn on the GBL card, the moto's own BATTLE
# pill measured 0.337, just under the old line, so `action_point` returned None
# on a perfectly good button. The loop then found the NEARBY BATTLE panel's
# teal X, correctly refused it as the card's close button, and read the same
# screen forever: 253 identical lines before it was noticed on 2026-08-31.
# The tab is also off-centre, so ACTION_CENTRE_SPAN rejects it independently.
ACTION_MIN_Y = 0.25


def action_point(
    boxes: Iterable[OCRBox],
    image_width: int,
    image_height: int,
) -> tuple[list[int], str] | None:
    """Find a lower-screen GBL action label and a close-button-safe tap.

    Pokemon GO also prints ``BATTLE`` in the top tab.  Restricting the search to
    the lower 65% keeps that label out.  The x coordinate intentionally lands
    left of center: some phone layouts put the teal close button over the middle
    of the entry pill.

    The label must also sit near the middle of the screen.  Every GBL action is
    a pill spanning the card, so its label is centred; the main menu's BATTLE
    icon carries the same word off to one side, and returning that one sent the
    forced x coordinate into POKEDEX instead.
    """

    candidates: list[OCRBox] = []
    for box in boxes:
        centred = abs(box.center_x - image_width / 2) <= image_width * ACTION_CENTRE_SPAN
        if normalize(box.text) in ACTION_LABELS and centred \
                and box.center_y >= image_height * ACTION_MIN_Y:
            candidates.append(box)
    if not candidates:
        return None
    box = max(candidates, key=lambda candidate: candidate.center_y)
    safe_x = int(image_width * 0.38)
    return [safe_x, box.center_y], box.text
