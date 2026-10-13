#!/usr/bin/env python3
"""Read an Android phone's Pokémon GO storage for the raid candy planner.

This is the Android hands for `raid_inventory`, which owns the per-species
sequence and the detail-page parser: reaching the storage grid without
leaving the game, typing a search and proving it took, tapping a cell by the
CP token read off the screen, scrolling a detail page from a caption that does
nothing when touched, and leaving with the one BACK that is known to be safe.

The screen-identification helpers here (`classify`, the grid parser, the
authorised-dialog list) are shared with the iOS worker.

Nothing here changes game state.  No transfer, no power up, no purchase.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

from . import pokemon_fleet, vision_ocr


def _adb_binary() -> str:
    """The adb the rest of the fleet drives phones with.

    The bundled binary beside the checkout, else whatever `adb` the machine's
    configured `path_prefix` puts first on PATH.  Never a second install found
    by searching: two adb versions on one computer restart each other's
    server, and that drops every other run's connection to its phones.
    """
    bundled = pokemon_fleet.default_adb_binary()
    if bundled != "adb":
        return bundled
    return shutil.which("adb") or "adb"


ADB_BINARY = _adb_binary()

# The storage search field is located from the screen rather than fixed here:
# the same control sits at a different fraction of the height on a 720x1600
# handset than on a 1316x2560 one.  See `search_bar_fraction`.

# A grid cell prints "CP 4442"; Vision sometimes returns the caption as Cyrillic
# "ср" (visually identical), and trailing junk ("Kyurem100%") must not parse.
# The caption is two letters, and the class carries the Latin/Cyrillic pairs so
# normalisation and this pattern agree.
_CP_TOKEN = re.compile(r"^\W*[CcсPpр]{1,2}\s*([0-9]{1,4})\W*$")
_COUNT_TOKEN = re.compile(r"^\(?([0-9]{1,4})\)?$")
# Vision reads the magnifier glyph as a letter and returns "Q (17)" for the
# result counter, so the parentheses are what actually identify it.
_PAREN_COUNT = re.compile(r"\((\d{1,5})\)")
# "142 / 142 HP" sits under the Pokémon's name on every detail page.
_HP_LINE = re.compile(r"\d+\s*/\s*\d+\s*hp")
# A stat *label* ending in CANDY, not the bare word: the weekly adventure sync
# card reports "8 Candy Found", and matching that would classify a reward card
# as a detail page -- which sends the recovery loop into pressing BACK on it.
_CANDY_LABEL = re.compile(r"candy\s*xl?\b|candy$")
# TAGS / POKÉMON / EGGS sit above storage and nowhere else, and are present
# however many results a filter matched.
_GRID_TABS = re.compile(r"tags.*pokemon|pokemon.*eggs", re.S)


class RaidScanError(RuntimeError):
    """The phone could not be read or was not where the scan needs it."""


class AndroidPhone:
    """A single Android handset, addressed by serial."""

    def __init__(self, serial: str, label: str = "", *, adb: str = ADB_BINARY):
        self.serial = serial
        self.label = label or serial
        self.adb = adb
        self.display_id: str | None = None
        self.viewport = self.read_viewport()
        self.display_id = self.find_display_id()

    # --- plumbing -----------------------------------------------------------

    def _run(self, *args: str, binary: bool = False, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.adb, "-s", self.serial, *args],
            capture_output=True,
            timeout=timeout,
            text=not binary,
            check=False,
        )

    def read_viewport(self) -> tuple[int, int]:
        result = self._run("shell", "wm", "size", timeout=15)
        sizes = re.findall(r"(\d+)x(\d+)", result.stdout or "")
        if sizes:
            width, height = sizes[-1]
            return int(width), int(height)
        return 1080, 1920

    def find_display_id(self) -> str | None:
        """The display id `screencap` wants, or None when the device has one.

        A foldable reports two displays, and `screencap` with no `-d` writes
        "[Warning] Multiple displays found, but no display id specified!"
        onto stdout *ahead of the framebuffer* -- the PNG no longer starts with
        its magic bytes and every screen read fails as "unreadable screencap".
        A foldable is exactly this.

        The id is the long SurfaceFlinger unique id, not the small display
        number: `screencap -d 0` answers "Display Id '0' is not valid" and
        captures nothing.  It is chosen by which display is *active*, so folding
        the handset shut follows the handover to the cover display instead of
        reading a different size and coordinate space.
        """
        listed = self._run("shell", "dumpsys", "SurfaceFlinger", "--display-id",
                           timeout=25).stdout or ""
        # "Display 4630947043778501779 (HWC display 0): port=147 ..."
        pairs = re.findall(r"^Display (\S+)\s+\(HWC display (\d+)\)", listed, re.M)
        if len(pairs) < 2:
            return None  # a single display; -d is neither needed nor wanted
        active = self.active_display_number()
        for unique_id, hwc in pairs:
            if hwc == active:
                return unique_id
        return pairs[0][0]

    def active_display_number(self) -> str | None:
        """The HWC display number currently showing, from the viewport list."""
        listed = self._run("shell", "dumpsys", "display", timeout=25).stdout or ""
        for viewport in listed.split("DisplayViewport{")[1:]:
            if "isActive=true" not in viewport:
                continue
            match = re.search(r"displayId=(\d+)", viewport)
            if match:
                return match.group(1)
        return None

    def point(self, fraction: tuple[float, float]) -> tuple[int, int]:
        width, height = self.viewport
        return int(width * fraction[0]), int(height * fraction[1])

    # --- input --------------------------------------------------------------

    def tap(self, fraction: tuple[float, float], settle: float = 1.1) -> None:
        x, y = self.point(fraction)
        self._run("shell", "input", "tap", str(x), str(y), timeout=20)
        time.sleep(settle)

    def back(self, settle: float = 1.1) -> None:
        self._run("shell", "input", "keyevent", "KEYCODE_BACK", timeout=20)
        time.sleep(settle)

    def guarded_back(self, settle: float = 1.6) -> None:
        """One BACK, then make sure it did not open the exit prompt.

        A single BACK is safe: on an in-game popup the game consumes it to
        close the popup, and it only reaches "Do you want to exit Pokémon GO?"
        from the root screen.  What is never safe is pressing it again without
        looking, because the prompt's default action is OK.  So this sends one
        press, reads the result, and cancels the prompt if it appeared.

        This is what makes retreating actually useful: the game's own back
        arrow is a fixed coordinate that only exists on some screens, and a
        friends list scrolled under the status bar has none to hit.

        CANCEL leaves the handset exactly where it was, which is why cancelling
        is a recovery step here rather than an error: the prompt only ever
        appears because a press came from the root screen, so CANCEL returns to
        that root screen and the caller carries on from a known one.
        """
        self._run("shell", "input", "keyevent", "KEYCODE_BACK", timeout=20)
        time.sleep(settle)
        try:
            image, boxes = self.read()
        except RaidScanError:
            return
        cancel = exit_prompt_tap(boxes, image)
        if cancel is not None:
            self.tap(cancel, settle=2.0)

    def type_text(self, text: str, settle: float = 1.4) -> None:
        # `input text` treats a literal space as an argument separator, and the
        # device shell would read `&` and `*` in a filter like `94&3*,4*` --
        # backgrounding the command and globbing -- so the term is quoted.
        escaped = text.strip().lower().replace(" ", "%s").replace("'", "")
        self._run("shell", "input", "text", f"'{escaped}'", timeout=25)
        time.sleep(settle)

    def drag(self, start: tuple[float, float], end: tuple[float, float],
             settle: float = 1.4) -> None:
        """Drag between two screen fractions; the motion starts at once."""
        x0, y0 = self.point(start)
        x1, y1 = self.point(end)
        self._run("shell", "input", "swipe", str(x0), str(y0), str(x1), str(y1), "400",
                  timeout=20)
        time.sleep(settle)

    def clear_field(self, settle: float = 0.6) -> None:
        """Empty the focused search field.

        MOVE_END then a run of DELs, in one adb call.  Tapping the circular
        clear glyph would need its position, and that position is not the same
        fraction of the screen on a 720x1600 handset as on a 1316x2560 one.
        """
        keys = ["KEYCODE_MOVE_END"] + ["KEYCODE_DEL"] * 48
        self._run("shell", "input", "keyevent", *keys, timeout=25)
        time.sleep(settle)

    # --- screen -------------------------------------------------------------

    def screenshot(self) -> Image.Image:
        result = self._run(
            "exec-out", "screencap",
            *(("-d", self.display_id) if self.display_id else ()),
            "-p", binary=True, timeout=40,
        )
        import io

        if not result.stdout:
            raise RaidScanError(f"{self.label}: empty screencap")
        try:
            return Image.open(io.BytesIO(result.stdout)).convert("RGB")
        except OSError as exc:
            raise RaidScanError(f"{self.label}: unreadable screencap") from exc

    def read(self) -> tuple[Image.Image, list[vision_ocr.TextBox]]:
        image = self.screenshot()
        return image, vision_ocr.ocr(image)


# --- parsing ---------------------------------------------------------------


def normalise(text: str) -> str:
    """Fold the characters Vision confuses, and drop accents.

    Accents matter for menu navigation: the map menu shows POKÉMON next to
    POKÉDEX, and Vision may or may not return the acute accent, so both have to
    reduce to the same ASCII form before they can be told apart.
    """
    folded = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return (
        stripped.replace("с", "c").replace("С", "C")
        .replace("р", "p").replace("Р", "P")
        .replace("’", "'")
    )


@dataclass(frozen=True)
class GridEntry:
    """One "CP nnnn" token and where it sat in the grid."""

    cp: int
    x: float
    y: float
    row: int


def parse_grid_entries(boxes: Sequence[vision_ocr.TextBox]) -> list[GridEntry]:
    """Every CP in a storage grid, with its position, grouped into rows.

    Rows are found by clustering the vertical positions rather than assuming a
    column count, so a phone packing a different number of columns still reads
    correctly.
    """
    raw: list[tuple[int, float, float]] = []
    for box in boxes:
        match = _CP_TOKEN.match(normalise(box.text).strip())
        if not match:
            continue
        value = int(match.group(1))
        if 100 <= value <= 6000:
            raw.append((value, box.cx, box.cy))
    if not raw:
        return []

    heights = [box.h for box in boxes]
    tolerance = max((sum(heights) / len(heights)) if heights else 20.0, 20.0)
    raw.sort(key=lambda item: item[2])
    entries: list[GridEntry] = []
    row_anchor: float | None = None
    row_index = -1
    for value, x, y in raw:
        if row_anchor is None or abs(y - row_anchor) > tolerance:
            row_anchor = y
            row_index += 1
        entries.append(GridEntry(cp=value, x=x, y=y, row=row_index))
    return entries


def parse_grid_cps(boxes: Sequence[vision_ocr.TextBox]) -> list[int]:
    """Every CP printed in a storage grid, highest first."""
    return sorted((entry.cp for entry in parse_grid_entries(boxes)), reverse=True)


def grid_is_cp_sorted(entries: Sequence[GridEntry]) -> bool:
    """True when each visible row starts at or below the row above it.

    The scan picks the individual's best CP to open, so it relies on the grid
    being sorted by CP descending.  Checking the visible rows costs nothing and
    turns that assumption into something the run can report instead of hide.
    """
    if not entries:
        return True
    rows: dict[int, list[int]] = {}
    for entry in entries:
        rows.setdefault(entry.row, []).append(entry.cp)
    # One row gives nothing to compare against, and says nothing either way.
    if len(rows) < 2:
        return True
    ordered = [sorted(rows[row], reverse=True) for row in sorted(rows)]
    for upper, lower in zip(ordered, ordered[1:]):
        if lower and lower[0] > upper[0]:
            return False
    return True


def parse_result_count(boxes: Sequence[vision_ocr.TextBox], height: int) -> int | None:
    """The "(17)" the search header shows for the current filter.

    Also the reliable signal that a filter is active at all: the unfiltered grid
    shows its cap as "4050 / 4750", which carries no parentheses.
    """
    ceiling = height * 0.2
    for box in sorted(boxes, key=lambda b: b.y):
        if box.y > ceiling:
            break
        match = _PAREN_COUNT.search(normalise(box.text))
        if match:
            return int(match.group(1))
    return None


def image_height(boxes: Sequence[vision_ocr.TextBox]) -> float:
    """Fallback ceiling for the name search when no HP line was recognised."""
    return max((box.y + box.h for box in boxes), default=0.0)


def classify(boxes: Sequence[vision_ocr.TextBox]) -> str:
    """Which storage screen is showing: "grid", "detail" or "unknown".

    A detail page cannot be identified by the Max Moves banner alone: that
    banner is only there once both charged moves are unlocked, so an ordinary
    Lv40 attacker would read as "unknown" and send the scanner off looking for a
    way back that was never needed.  The HP line and the candy labels are on
    every detail page, so those carry the test.
    """
    text = " ".join(normalise(box.text).lower() for box in boxes)
    if "max moves" in text or _CANDY_LABEL.search(text):
        return "detail"
    if _HP_LINE.search(text):
        return "detail"
    if len(parse_grid_cps(boxes)) >= 3:
        return "grid"
    # The tab row is the reliable grid marker. Counting CP cells is not: a
    # filter matching one or two copies shows one or two of them, and reading
    # that as "lost the storage screen" sent the recovery into a BACK from an
    # ordinary filtered grid -- which lands on the map, raises the exit prompt,
    # and then walks all the way back. Every species held once or twice paid for
    # that trip.
    if _GRID_TABS.search(text):
        return "grid"
    return "unknown"


# --- Leaving screens the scan did not expect -------------------------------

# Android's BACK reaches the game's own "Do you want to exit Pokémon GO?"
# prompt, and the next BACK accepts it.  A recovery loop that presses BACK on an
# unrecognised screen therefore closes the game.  Everything below taps a target
# it can actually see instead, and the only BACK the scanner ever sends is one
# taken from a storage detail page it has just confirmed.
MENU_BALL = (0.50, 0.938)
# The game's own back affordance, top-left of any pushed screen.  This is the
# safe way to retreat one level: unlike Android's BACK key it is an ordinary
# in-app control, so there is no path by which pressing it can reach the
# "Do you want to exit Pokémon GO?" prompt.
IN_APP_BACK = (0.052, 0.042)
GAME_PACKAGE = "com.nianticlabs.pokemongo"

_EXIT_PROMPT = re.compile(r"exit\s+pok")


def _label_centre(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image, wanted: str
) -> tuple[float, float] | None:
    """Centre of a box whose normalised text equals `wanted`."""
    for box in boxes:
        if normalise(box.text).strip().lower() == wanted:
            return box.cx / image.width, box.cy / image.height
    return None


def recovery_tap(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[float, float] | None:
    """A visible affordance to tap to get back toward storage, if there is one.

    Returns a screen fraction, or None when nothing recognisable is showing.
    Never a BACK: an unrecognised screen is exactly the case where BACK is
    dangerous.
    """
    text = " ".join(normalise(box.text).lower() for box in boxes)

    # "Do you want to exit Pokémon GO?" -- CANCEL is the safe side of it.
    if _EXIT_PROMPT.search(text):
        return _label_centre(boxes, image, "cancel")

    # The map's main menu carries an OCR-visible POKÉMON button, which beats a
    # hard-coded coordinate on any screen size.  The label is not the button:
    # the game draws the caption above a circular icon, and a tap on the caption
    # just closes the menu again.  Drop to the icon's centre below it.
    menu_pokemon = _label_centre(boxes, image, "pokemon")
    if menu_pokemon is not None and "pokedex" in text:
        label = next(
            b for b in boxes if normalise(b.text).strip().lower() == "pokemon"
        )
        icon_y = label.y + label.h + image.height * 0.045
        return (label.cx / image.width, min(icon_y / image.height, 0.97))

    button = _button_label(boxes)
    if button is not None:
        return (button.cx / image.width, button.cy / image.height)
    return None

    return None


def looks_like_loading(boxes: Sequence[vision_ocr.TextBox]) -> bool:
    """The game's cold-start screen: a coach tip over artwork and a progress bar.

    Detected by its shape rather than its wording, because the tip changes every
    launch: a long sentence, no CP, no stat labels, no menu words.  Tapping this
    screen hits artwork, which is how a scan ends up somewhere unrelated -- and
    a cold start on the taller handset runs for well over the settle a naive
    relaunch would allow.
    """
    if parse_grid_cps(boxes):
        return False
    text = " ".join(normalise(box.text).lower() for box in boxes)
    if any(word in text for word in ("candy", "pokedex", "items", "max moves")):
        return False
    # A dialog is not a loading screen.  The coach tip over the loading artwork
    # is a sentence with nothing to press; a modal always offers at least one
    # button, and calling a modal "loading" made the scan wait forever for
    # artwork that was never coming.
    if _button_label(boxes) is not None:
        return False
    for box in boxes:
        candidate = normalise(box.text).strip()
        if candidate.endswith(".") and len(candidate.split()) >= 5:
            return True
    return False


# Buttons this workflow may press to clear an informational dialog.
_DISMISS_LABELS = {"cancel", "close", "dismiss", "not now", "ok", "okay", "got it", "later"}
# Words that mean the dialog is asking for something, not merely reporting one.
_RISKY_WORDS = ("buy", "purchase", "price", "coins", "avatars", "premium",
                "spend", "confirm", "delete", "transfer", "release",
                "level up", "purify")


def _button_label(boxes: Sequence[vision_ocr.TextBox]) -> vision_ocr.TextBox | None:
    """A dismiss-capable button on screen, if there is one.

    Deliberately conservative about OK: the game's exit prompt, a purchase and a
    transfer all have a button that would read the same way, so any dialog
    carrying a word about spending or deleting is left for a human.
    """
    text = " ".join(normalise(box.text).lower() for box in boxes)
    if any(word in text for word in _RISKY_WORDS):
        return None
    for box in boxes:
        label = normalise(box.text).strip().lower().rstrip("!.")
        if label in _DISMISS_LABELS:
            return box
    return None


# Overlays the scan is authorised to clear on its own.
#
# The account owner has standing permission to tap through exactly these two:
# the weekly adventure sync card, whose only exit is "CLAIM REWARD!" and which
# grants a reward the game has already offered and will otherwise let expire;
# and the "Continue Party Play?" prompt, where REJOIN PARTY restores the
# session the account was already in.  Both were confirmed by the owner, who
# asked not to be interrupted about them again.
#
# Anything else with no recognisable way out is reported rather than guessed at.
AUTHORISED_DISMISSALS: tuple[tuple[str, str, str], ...] = (
    (
        "claim reward",
        "weekly adventure sync card",
        "claiming the weekly reward grants stardust and candy; authorised by the owner",
    ),
    (
        "rejoin party",
        "Continue Party Play? prompt",
        "restores the Party Play session the account was already in; authorised by the owner",
    ),
)


def exit_prompt_tap(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[float, float] | None:
    """CANCEL on the "Do you want to exit Pokémon GO?" prompt, if it is up.

    The prompt's primary action is OK, so anything automating BACK has to be
    able to find the safe side of it.  Matching is on the prompt's own wording
    rather than on a CANCEL label alone, because CANCEL appears on plenty of
    dialogs this workflow would rather dismiss differently.
    """
    text = " ".join(normalise(box.text).lower() for box in boxes)
    if "exit pok" not in text and "want to exit" not in text:
        return None
    return _label_centre(boxes, image, "cancel")


def authorised_dismissal(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[tuple[float, float], str] | None:
    """A tap this workflow is permitted to make on its own, if one is showing.

    Returning the button itself, found by OCR, rather than a fixed coordinate:
    these dialogs sit at different heights on the two handsets in this fleet.
    """
    for needle, what, why in AUTHORISED_DISMISSALS:
        for box in boxes:
            # The game renders these with an exclamation mark ("CLAIM REWARD!"),
            # so the label is compared without trailing punctuation.
            text = normalise(box.text).strip().lower().rstrip("!.").strip()
            if text != needle:
                continue
            return (box.cx / image.width, box.cy / image.height), f"{what} ({why})"
    return None


def blocking_screen(boxes: Sequence[vision_ocr.TextBox]) -> str | None:
    """A screen the scan must not clear on its own, described for the operator.

    Kept separate from the authorised list above: this is the one path where the
    workflow stops and says so rather than acting.  The game's exit prompt is
    deliberately *not* here -- CANCEL is the only safe side of it and answering
    it leaves the handset where it was, so `exit_prompt_tap` handles that.
    """
    return None


def search_bar_fraction(
    boxes: Sequence[vision_ocr.TextBox], image: Image.Image
) -> tuple[float, float] | None:
    """Where the storage search field is, derived from what is on screen.

    The field cannot be a fixed fraction: the same control sits at 0.148 of the
    screen height on a 720x1600 handset and 0.161 on a 1316x2560 one, because
    the game lays it out in density-independent units.  Anchoring between the
    tab row above it and the first CP row below it holds on both.
    """
    tab_box = next(
        (b for b in boxes if normalise(b.text).strip().lower() == "pokemon"), None
    )
    if tab_box is None:
        return None
    entries = parse_grid_entries(boxes)
    if not entries:
        # No CP row to interpolate against: the previous filter matched nothing,
        # or matched once. Refusing to answer here did not skip a species, it
        # silently reported every following species as "not owned" -- 18 good
        # records overwritten by an empty search. The tab row is always there,
        # and the field sits a near-constant fraction of the screen height
        # below it (133px of 1600, 224px of 2560), so that is the fallback.
        return (0.5, min(tab_box.y + tab_box.h + image.height * 0.085,
                         image.height * 0.30) / image.height)
    first_cp_top = min(entry.y for entry in entries)
    # 0.46 of the way from the bottom of the tab row to the top of the first CP
    # row is where the field sits on both handsets measured here (413px of
    # 2560, and 226px of 1600).  A midpoint guess overshoots by 19-35px, which
    # on the taller phone is enough to land below the control.
    tab_bottom = tab_box.y + tab_box.h
    y = tab_bottom + 0.46 * (first_cp_top - tab_bottom)
    floor = tab_bottom + image.height * 0.01
    ceiling = image.height * 0.30
    return (0.5, min(max(y, floor), ceiling) / image.height)


def _shows_term(image: Image.Image, boxes: Sequence[vision_ocr.TextBox], term: str) -> bool:
    from .raid_inventory import field_shows

    return field_shows(image, boxes, term)


class StorageScanner:
    """Drive one phone's storage screen and collect per-species records."""

    def __init__(self, phone: AndroidPhone, *, verbose: bool = True):
        self.phone = phone
        self.verbose = verbose
        # Whether the search field still holds focus from the previous species.
        # It does: the keyboard stays up and the field keeps the caret between
        # filters, so the tap that risks missing -- and walking off the storage
        # screen -- is only ever needed after something interrupted us.
        self._field_focused = False

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[{self.phone.label}] {message}", flush=True)

    def ensure_grid(self, attempts: int = 18) -> None:
        """Return to the storage grid, or fail without leaving the game.

        Retreat is done with `guarded_back`, which sends one BACK and cancels
        the exit prompt if that is what the press raised.  That prompt is
        useful as well as dangerous: the game only offers it from the root
        screen, so a cancelled prompt is proof that this handset is sitting on
        the map -- which is exactly the place the menu button works from.  A
        phone stuck anywhere else is reported, not poked at blind.
        """
        menu_pressed = False
        backed_out = 0
        unsettled = 0
        # Whatever focused the field before is no longer to be trusted: BACK may
        # have dismissed the keyboard along with the detail page.
        self._field_focused = False
        for _ in range(attempts):
            image, boxes = self.phone.read()
            # Checked before classify: a card the operator has to clear can look
            # like a detail page (the adventure sync card counts candy), and
            # classifying it as one would send a BACK straight at it.
            allowed = authorised_dismissal(boxes, image)
            if allowed is not None:
                target, why = allowed
                self.log(f"clearing an authorised dialog: {why}")
                self.phone.tap(target, settle=2.4)
                continue
            cancel = exit_prompt_tap(boxes, image)
            if cancel is not None:
                # CANCEL is the only safe side of this prompt, and answering it
                # is strictly better than stopping: the handset is left on the
                # screen it was already on.
                self.log("cancelling the game's exit prompt")
                self.phone.tap(cancel, settle=2.2)
                continue
            blocked = blocking_screen(boxes)
            if blocked is not None:
                raise RaidScanError(f"{self.phone.label}: {blocked}")
            from .raid_inventory import is_mega_level_page

            if is_mega_level_page(boxes):
                # BACK closes this page and only this page.
                self.log("closing a Mega Level page")
                self.phone.back(settle=1.6)
                continue
            state = classify(boxes)
            if state == "grid":
                return
            if state == "detail":
                # A detail page was just read by this workflow; BACK from here
                # lands on the storage grid and never raises an exit prompt.
                self.phone.back()
                continue
            if looks_like_loading(boxes):
                unsettled += 1
                if unsettled > 8:
                    raise RaidScanError(
                        f"{self.phone.label}: the game is still on its loading "
                        "screen; check the handset and re-run"
                    )
                time.sleep(8.0)
                continue
            target = recovery_tap(boxes, image)
            if target is None and unsettled < 3:
                # Reloading the map throws up coach tips and progress cards
                # that clear themselves.  Tapping through one of those is how
                # a scan ends up somewhere unrelated, so the first few
                # unrecognised reads only wait.
                unsettled += 1
                time.sleep(3.0)
                continue
            unsettled = 0
            if target is not None:
                self.log("dismissing an unexpected screen")
                self.phone.tap(target, settle=1.8)
                continue
            if not menu_pressed:
                menu_pressed = True
                self.log("opening the map menu to reach storage")
                self.phone.tap(MENU_BALL, settle=2.2)
                continue
            if backed_out < 6:
                backed_out += 1
                self.log("retreating one screen")
                self.phone.guarded_back()
                continue
            raise RaidScanError(
                f"{self.phone.label}: could not reach the Pokémon storage grid; "
                "the phone is on a screen this workflow will not tap through"
            )
        raise RaidScanError(f"{self.phone.label}: could not reach the Pokémon storage grid")

    def search(self, term: str):
        """Filter storage to `term` and read the filtered grid.

        The field is cleared with MOVE_END plus a run of DELs rather than by
        tapping the circular clear glyph: the glyph's position is a second thing
        that differs between screen sizes, and a keyevent run cannot miss.

        The keyboard is deliberately left up.  Pressing BACK dismisses it in
        this build, and the grid it then shows cannot be told from the
        unfiltered one by cell count -- a seventeen-Groudon result fills the
        same fifteen cells as the whole collection.  The parenthesised result
        counter is the only reliable "a filter is active" signal, and it stays
        visible with the keyboard up.

        Where the field sits is estimated from the tab row and the first CP row,
        which lands within a few tens of pixels on every handset measured -- but
        a miss there is expensive rather than harmless, because a tap that
        misses the field lands on the storage grid and navigates somewhere
        unrelated, and getting back costs a whole recovery cycle.  So a miss is
        retried on a short ladder around the estimate instead of once, and each
        attempt is judged by whether the filter actually took.
        """
        image, boxes = self.phone.read()
        if self._field_focused and classify(boxes) == "grid":
            # Still ours: clear and retype without touching the screen.
            self.phone.clear_field()
            self.phone.type_text(term)
            image, boxes = self.phone.read()
            entries = parse_grid_entries(boxes)
            count = parse_result_count(boxes, image.height)
            if count is not None and _shows_term(image, boxes, term):
                return count, entries, image, boxes
            self.log(f"filter '{term}' did not take on the focused field; "
                     "re-acquiring it")
            self._field_focused = False

        base = search_bar_fraction(boxes, image)
        if base is None:
            self.log(f"cannot locate the search field for '{term}'")
            return None, [], image, boxes

        for attempt, nudge in enumerate((0.0, -0.018, 0.018, -0.034)):
            if attempt:
                # A tap that misses the field does not just do nothing: it can
                # land on the tab row and walk off storage entirely. Re-tapping
                # the next offset at whatever is now on screen is how one miss
                # became a trek across the map and back, once per species.
                if classify(boxes) != "grid":
                    self.log(f"filter '{term}' left the storage screen; "
                             "going back and retrying")
                    self.ensure_grid()
                    image, boxes = self.phone.read()
            target = (base[0], max(0.02, base[1] + nudge))
            self.phone.tap(target, settle=1.2)
            self.phone.clear_field()
            self.phone.type_text(term)
            image, boxes = self.phone.read()
            entries = parse_grid_entries(boxes)
            count = parse_result_count(boxes, image.height)
            # Only the counter proves a filter is active. An empty screen also
            # has no CP tokens, so "no entries" on its own would read as a
            # species the account does not own -- a quiet false negative, and
            # the reason a species went missing from the plan for no reason.
            # The counter alone is not proof: a search that did not take
            # leaves the previous species' filter, counter and all, on screen.
            if count is not None and _shows_term(image, boxes, term):
                self._field_focused = True
                return count, entries, image, boxes
            if attempt < 3:
                self.log(f"filter '{term}' missed the field; retrying "
                         f"{'lower' if nudge < 0 else 'higher'}")
        return None, entries, image, boxes

    def open_cell(self, cp: int, image: Image.Image, entries: Sequence[GridEntry]) -> bool:
        """Tap the grid cell showing `cp`.

        Anchored on the recognised token rather than a fixed column, so a phone
        whose grid packs a different number of columns still works.  The band is
        wide because the scan types with the keyboard still up: that compresses
        the grid into the top half of the screen, and a guard tuned for the
        keyboard-down layout rejects every cell.  The ceiling only has to stay
        clear of the CP sort button in the bottom corner.
        """
        for entry in entries:
            if entry.cp != cp:
                continue
            fraction = (entry.x / image.width, entry.y / image.height)
            if not 0.12 <= fraction[1] <= 0.90:
                continue
            # The CP sort control sits in the bottom-right corner.  Tapping it
            # opens a sort menu, which is a popup with no recognisable way out
            # and is where the scan kept wandering off to.
            if fraction[1] > 0.82 and fraction[0] > 0.72:
                continue
            self.phone.tap(fraction, settle=1.9)
            return True
        return False

    # --- the hands `raid_inventory` drives -------------------------------

    def regrid(self):
        """Re-read the filtered grid after leaving a detail page."""
        image, boxes = self.phone.read()
        if classify(boxes) != "grid":
            self.ensure_grid()
            image, boxes = self.phone.read()
        return (parse_result_count(boxes, image.height), parse_grid_entries(boxes),
                image, boxes)

    def read_detail(self) -> list[tuple[Image.Image, list[vision_ocr.TextBox]]]:
        """The detail page, then the same page scrolled to its moves.

        Only a page `classify` calls a detail page is scrolled: a vertical drag
        there scrolls, where on other screens it could move the map.
        """
        image, boxes = self.phone.read()
        for _ in range(2):
            # "New Mega Level available!" and similar cards drop over the page
            # and hide the candy cells.  Only an informational button is
            # pressed: `_button_label` refuses any card about spending.
            promo = _button_label(boxes) if classify(boxes) == "detail" else None
            if promo is None:
                break
            self.log(f"clearing a card over the detail page ({promo.text.strip()})")
            self.phone.tap((promo.cx / image.width, promo.cy / image.height), settle=2.0)
            image, boxes = self.phone.read()
        if classify(boxes) != "detail":
            raise RaidScanError(f"{self.phone.label}: the detail page did not open")
        from .raid_inventory import settled_anchor, scroll_target

        anchor, image, boxes = settled_anchor(self.phone.read, image, boxes)
        screens = [(image, boxes)]
        if anchor is not None:
            self.phone.drag(anchor, scroll_target(anchor))
            screens.append(self.phone.read())
        return screens

    def leave_detail(self) -> None:
        # BACK from a detail page this scan opened lands on the storage grid
        # and never reaches the exit prompt.
        self.phone.back(settle=1.6)
        self._field_focused = False


def scan_phone(
    serial: str,
    terms: Sequence[tuple[str, str, bool]],
    *,
    label: str = "",
    checkpoint: Path | None = None,
    resume: bool = False,
) -> list:
    """Scan every `(term, display, shadow_eligible)` on one handset."""
    from . import raid_inventory

    phone = AndroidPhone(serial, label)
    return raid_inventory.scan_account(
        StorageScanner(phone), terms, label=label or serial,
        checkpoint=checkpoint, resume=resume,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan an Android phone's Pokémon GO storage for raid attackers."
    )
    parser.add_argument("--serial", required=True, help="adb serial")
    parser.add_argument("--label", default="", help="name for progress output")
    parser.add_argument("--terms", type=Path, required=True,
                        help="JSON list of [search_term, display, shadow_eligible]")
    parser.add_argument("--out", type=Path, help="checkpoint and result file")
    parser.add_argument("--limit", type=int, default=0, help="only scan the first N terms")
    parser.add_argument("--resume", action="store_true",
                        help="keep species already complete in --out and scan the rest")
    args = parser.parse_args(argv)

    terms = [(str(t[0]), str(t[1]), bool(t[2]) if len(t) > 2 else True)
             for t in json.loads(args.terms.read_text())]
    if args.limit:
        terms = terms[: args.limit]
    scan_phone(args.serial, terms, label=args.label, checkpoint=args.out, resume=args.resume)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
