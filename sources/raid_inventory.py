#!/usr/bin/env python3
"""What one account holds of each raid attacker, read off its storage screen.

This is the platform-neutral half of the raid scan: the per-species sequence
and the detail-page parser.  The Android and iOS workers only supply the hands
-- reach the grid, type a search, tap a cell, scroll, leave a page -- so the
two platforms cannot measure the game differently.

Per species the scan runs a handful of the game's own storage filters, all
read-only:

- `94`             every Gengar.  A Pokédex number is matched exactly, so a
                   nickname or a prefix (mew / mewtwo) cannot leak in.
- `94&shadow`      which of them are shadows (1.2x attack, 1.2x cost).
- `94&4*`          hundos.
- `94&3*,4*`       82-100% individuals: the `3*` band is 37-44 of 45, so a
                   98% individual is in it.

and then opens the best few individuals' detail pages.  A detail page carries
the max HP, the next power-up's stardust and candy price (which pins the level
to a band), the moves, and the species' pools: candy, XL candy, Mega Energy
and the account's stardust.  `raid_math.solve` turns CP + HP + price + IV band
into level and IVs; nothing here guesses them.

Every search is confirmed against the text the search field itself shows.  A
search that silently did not take leaves the previous species on screen, and
an earlier version of this scan recorded those stale results under the next
species' name.

Nothing here changes game state.  No power up, no new attack, no transfer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol, Sequence

from PIL import Image

from . import vision_ocr

SCHEMA = 2
# Individuals opened per species.  Each costs a detail read and a scroll, and
# the plan only ever recommends the best one or two.
MAX_OPENED = 3

_HP_LINE = re.compile(r"(\d+)\s*/\s*(\d+)\s*hp", re.I)
_TYPE_WORDS = (
    "normal|fire|water|electric|grass|ice|fighting|poison|ground|flying|"
    "psychic|bug|rock|ghost|dragon|dark|steel|fairy"
)
_TYPES_LINE = re.compile(rf"^\s*({_TYPE_WORDS})(\s*/\s*({_TYPE_WORDS}))?\s*$", re.I)
_PAREN_COUNT = re.compile(r"\((\d{1,5})\)")


class Hands(Protocol):
    """What a platform worker provides to the protocol."""

    def log(self, message: str) -> None: ...
    def ensure_grid(self) -> None: ...
    def search(self, term: str) -> tuple[int | None, list, Image.Image, list]: ...
    def regrid(self) -> tuple[int | None, list, Image.Image, list]: ...
    def open_cell(self, cp: int, image: Image.Image, entries: Sequence) -> bool: ...
    def read_detail(self) -> list[tuple[Image.Image, list]]: ...
    def leave_detail(self) -> None: ...


# --- records -------------------------------------------------------------


@dataclass
class Individual:
    """One opened detail page."""

    cp: int
    hp: int | None = None
    dust_cost: int | None = None
    candy_cost: int | None = None
    can_power: bool | None = None
    moves: list[str] = field(default_factory=list)
    lucky: bool = False
    shadow: bool | None = None
    iv_band: str = "?"   # "4*", "3*" (37-44), "<3*", or "?" when unsearched
    types_line: str = ""
    name: str = ""
    note: str = ""


@dataclass
class SpeciesScan:
    """One species, as this account holds it."""

    search: str
    display: str = ""
    owned: int = 0
    cps: list[int] = field(default_factory=list)
    shadow_cps: list[int] | None = None
    hundo_cps: list[int] | None = None
    good_cps: list[int] | None = None
    shadow_count: int | None = None
    hundo_count: int | None = None
    good_count: int | None = None
    candy: int | None = None
    xl_candy: int | None = None
    mega_energy: int | None = None
    # True when the page has a Mega/Primal Energy cell at all.  No cell means
    # this account cannot Mega Evolve the species, whatever the game master says.
    mega_shown: bool | None = None
    # The energy the next Mega Evolution costs: 0 once a Mega level makes it
    # free, the full first-evolution price on a species never evolved.
    mega_cost: int | None = None
    stardust: int | None = None
    individuals: list[Individual] = field(default_factory=list)
    note: str = ""
    error: str = ""
    scanned_at: float = 0.0
    schema: int = SCHEMA

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict) -> "SpeciesScan":
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in row.items() if k in known}
        values["individuals"] = [
            Individual(**{k: v for k, v in item.items() if k in Individual.__dataclass_fields__})
            for item in row.get("individuals", [])
        ]
        return cls(**values)

    @property
    def complete(self) -> bool:
        """Read well enough that a resumed run need not repeat it."""
        if self.error or self.schema != SCHEMA:
            return False
        if self.owned == 0:
            return "no matches" in self.note
        return self.candy is not None and any(i.hp for i in self.individuals)


# --- the detail page -------------------------------------------------------


def _norm(text: str) -> str:
    from .raid_scan_android import normalise

    return normalise(text).strip()


def _row_neighbours(boxes: Sequence[vision_ocr.TextBox], anchor: vision_ocr.TextBox):
    return [
        b for b in boxes
        if b is not anchor and abs(b.cy - anchor.cy) <= max(anchor.h, b.h) * 0.8
    ]


def _rows(boxes: Sequence[vision_ocr.TextBox]) -> dict[int, int]:
    """Row index per box: a new row starts where the vertical gap opens up.

    Rounding y by each box's own height looks equivalent and is not: two labels
    of different heights on two rows can sort in the wrong order.
    """
    rows: dict[int, int] = {}
    row, previous = -1, None
    for box in sorted(boxes, key=lambda b: b.cy):
        if previous is None or box.cy - previous.cy > max(box.h, previous.h) * 0.6:
            row += 1
        rows[id(box)] = row
        previous = box
    return rows


def parse_resources(boxes: Sequence[vision_ocr.TextBox]) -> dict:
    """Stardust, candy, XL candy and Mega Energy, from their labelled cells.

    Each cell is a number over a label.  Candy and XL candy share a label stem
    ("GASTLY CANDY" / "GASTLY CANDY XL"), and Vision regularly drops the XL --
    sometimes onto its own line below, sometimes not at all.  The game always
    draws the XL cell after the candy cell in reading order, so when two candy
    labels arrive with no XL marker the later one is the XL pool.
    """
    out: dict = {"stardust": None, "candy": None, "xl_candy": None,
                 "mega_energy": None, "mega_shown": False}
    labels = []
    for box in boxes:
        text = _norm(box.text).lower()
        if re.search(r"\bstardust\b", text) and not re.search(r"\d", text):
            labels.append(("stardust", box))
        elif (re.search(r"\b(mega|primal)\b(\s*en\w*(\s*\w*)?)?$", text)
              or re.search(r"\benergy\b", text)) and not re.search(r"evolve|level", text):
            # "GENGAR MEGA ENERGY", cut short at the edge to "ENUSAUR MEGA".
            labels.append(("energy", box))
        elif re.search(r"\bcandy\b", text) and not re.search(r"\d|found|rare", text):
            # The L is read as I, 1 or | often enough to allow for it.
            labels.append(("xl" if re.search(r"candy\s*x[li1|]?$|\bx[li1]\b", text) else "candy", box))
    rows = _rows([box for _, box in labels])
    reading_order = lambda box: (rows[id(box)], box.x)  # noqa: E731
    labels.sort(key=lambda item: reading_order(item[1]))

    # A bare "XL" just under a candy label belongs to it.
    for index, (kind, label) in enumerate(labels):
        if kind != "candy":
            continue
        for marker in boxes:
            if _norm(marker.text).lower().replace(" ", "") != "xl":
                continue
            if 0 <= marker.y - label.bottom <= label.h * 1.5 and label.contains_horizontal(marker, label.w * 0.3):
                labels[index] = ("xl", label)
    candies = [label for kind, label in labels if kind == "candy"]
    if len(candies) >= 2 and not any(kind == "xl" for kind, _ in labels):
        later = max(candies, key=reading_order)
        labels = [("xl", b) if b is later else (k, b) for k, b in labels]

    for kind, label in labels:
        value = vision_ocr.find_number_above(boxes, label)
        if kind == "stardust" and out["stardust"] is None:
            out["stardust"] = value
        elif kind == "candy" and out["candy"] is None:
            out["candy"] = value
        elif kind == "xl" and out["xl_candy"] is None:
            out["xl_candy"] = value
        elif kind == "energy":
            out["mega_shown"] = True
            # Vision reliably drops a lone "0" beside the energy icon; any
            # count that matters (a Mega costs 100-400) is three digits and
            # reads cleanly.
            if out["mega_energy"] is None:
                out["mega_energy"] = value if value is not None else 0
    return out


def parse_mega_cost(boxes: Sequence[vision_ocr.TextBox]) -> int | None:
    """The energy price printed beside the MEGA EVOLVE button, if there is one."""
    evolve = next((b for b in boxes if re.fullmatch(r"(mega\s*)?evolve", _norm(b.text).lower())), None)
    if evolve is None:
        return None
    mega = next((b for b in boxes if _norm(b.text).lower() == "mega"
                 and abs(b.cx - evolve.cx) < evolve.w and 0 < evolve.y - b.y < evolve.h * 3), None)
    top = mega.y if mega is not None else evolve.y
    bottom = evolve.bottom
    numbers = [
        b for b in boxes
        if b.x > evolve.x + evolve.w * 0.6 and top - evolve.h <= b.cy <= bottom + evolve.h
        and re.fullmatch(r"[\d,]+", _norm(b.text).strip())
    ]
    if not numbers:
        return None
    return vision_ocr.number_in(min(numbers, key=lambda b: b.x).text)


def parse_power_up(boxes: Sequence[vision_ocr.TextBox]) -> dict:
    """The POWER UP row: the next level's stardust and candy price."""
    out: dict = {"dust_cost": None, "candy_cost": None, "can_power": None}
    button = next(
        (b for b in boxes if re.fullmatch(r"(to\s*)?[pd]ower\s*u[pd]", _norm(b.text).lower())),
        None,
    )
    if button is None:
        if any(_norm(b.text).lower() in {"max", "maxed"} for b in boxes):
            out["can_power"] = False
        return out
    out["can_power"] = True
    numbers = sorted(
        (b for b in _row_neighbours(boxes, button)
         if b.x > button.x and vision_ocr.number_in(b.text) is not None),
        key=lambda b: b.x,
    )
    values = [vision_ocr.number_in(b.text) for b in numbers]
    # The stardust icon is sometimes read as a leading "1" ("113000").  No
    # single power-up costs more than 18,000 (15,000 for a shadow, x1.2).
    if values and values[0] and values[0] > 18000 and str(values[0]).startswith("1"):
        values[0] = int(str(values[0])[1:])
    if values:
        out["dust_cost"] = values[0]
    if len(values) > 1:
        out["candy_cost"] = values[1]
    return out


def parse_moves(boxes: Sequence[vision_ocr.TextBox], width: float) -> list[str]:
    """Move names, read down the left column under the GYMS & RAIDS tab."""
    header = next(
        (b for b in boxes if "gyms" in _norm(b.text).lower() and "raid" in _norm(b.text).lower()),
        None,
    )
    if header is None:
        return []
    stop = min(
        (b.y for b in boxes
         if b.y > header.bottom
         and re.search(r"new attack|swap buddies|caught|hatched|appraise|received",
                       _norm(b.text).lower())),
        default=float("inf"),
    )
    moves = []
    for box in sorted(boxes, key=lambda b: b.y):
        text = _norm(box.text).lstrip("•| ").strip()
        if not header.bottom < box.y < stop or box.cx > width * 0.6:
            continue
        if re.search(r"[A-Za-z]{3}", text) and not re.search(r"\d", text):
            moves.append(text)
    return moves


def parse_detail_page(screens: Sequence[tuple[Image.Image, list]]) -> dict:
    """Everything one detail page says, merged across its scrolled reads."""
    out: dict = {
        "hp": None, "lucky": False, "types_line": "", "name": "", "moves": [],
        "dust_cost": None, "candy_cost": None, "can_power": None,
        "stardust": None, "candy": None, "xl_candy": None,
        "mega_energy": None, "mega_shown": False, "mega_cost": None,
    }
    for image, boxes in screens:
        if out["mega_cost"] is None:
            out["mega_cost"] = parse_mega_cost(boxes)
        for box in boxes:
            text = _norm(box.text)
            hp = _HP_LINE.search(text)
            if hp and out["hp"] is None:
                out["hp"] = int(hp.group(2))
            if "lucky pok" in text.lower():
                out["lucky"] = True
            if _TYPES_LINE.match(text) and "/" in text or (
                _TYPES_LINE.match(text) and not out["types_line"] and text.isupper()
            ):
                out["types_line"] = out["types_line"] or text.upper().replace(" ", "")
        for key, value in parse_resources(boxes).items():
            if key == "mega_shown":
                out[key] = out[key] or value
            elif out[key] is None:
                out[key] = value
        for key, value in parse_power_up(boxes).items():
            if out[key] is None:
                out[key] = value
        # The first read often cuts the move list off at the screen's edge.
        moves = parse_moves(boxes, image.width)
        if len(moves) > len(out["moves"]):
            out["moves"] = moves
        if not out["name"]:
            hp_line = next((b for b in boxes if _HP_LINE.search(_norm(b.text))), None)
            if hp_line is not None:
                above = [b for b in boxes if b.bottom <= hp_line.y and re.search(r"[A-Za-z]{3}", b.text)
                         and not re.search(r"lucky|cp\s*\d|^cp", _norm(b.text).lower())
                         and not _TYPES_LINE.match(_norm(b.text))]
                if above:
                    out["name"] = _norm(max(above, key=lambda b: b.bottom).text).lstrip("• ")
    return out


# --- pages a scan must back out of --------------------------------------------


def is_mega_level_page(boxes: Sequence[vision_ocr.TextBox]) -> bool:
    """The "<species>'s Mega Level" page, which carries a LEVEL UP button.

    LEVEL UP spends Mega Energy, so nothing on this page is ever tapped except
    its close disc.  A press on the detail page's Mega pips opens it.
    """
    # Matched on the page's title line, not anywhere in the text: an ordinary
    # detail page's help copy says "...at least one Mega Level unlocked".
    return any(re.search(r"'s mega level$", _norm(b.text).lower().replace("’", "'"))
               for b in boxes)


# --- scrolling a detail page --------------------------------------------------

_STATIC_LABELS = re.compile(r"^(weight|height|heaviest|lightest|tallest|shortest)$", re.I)
SCROLL_FRACTION = 0.40


def scroll_anchor(boxes: Sequence[vision_ocr.TextBox], image: Image.Image) -> tuple[float, float] | None:
    """A point on the detail page that does nothing when touched, to drag from.

    A drag that starts on a control can be taken as a tap on it: an iOS drag
    with a press-and-hold opened the Mega Level page (whose LEVEL UP spends
    Mega Energy) from the Mega pips, and the POWER UP button sits on the same
    page.  So the drag never starts at a fixed point -- it starts on the
    WEIGHT/HEIGHT caption read off this very screenshot, and with no caption
    in view the page is not scrolled at all.
    """
    labels = [b for b in boxes if _STATIC_LABELS.match(_norm(b.text))]
    if not labels:
        return None
    label = min(labels, key=lambda b: b.y)
    y = label.cy / image.height
    if not 0.25 <= y <= 0.85:
        return None
    return label.cx / image.width, y


def settled_anchor(read, image: Image.Image, boxes, attempts: int = 3):
    """The scroll anchor, once two consecutive reads agree on where it is.

    The detail page slides up as it opens.  An anchor measured mid-slide has
    moved by the time the drag lands, and on android-three the press came down on the
    Mega pips instead -- which opened the Mega Level page.  `read` returns a
    fresh (image, boxes); the latest pair is returned with the anchor so the
    caller keeps an up-to-date screen.
    """
    anchor = scroll_anchor(boxes, image)
    for _ in range(attempts):
        if anchor is None:
            return None, image, boxes
        time.sleep(0.5)
        image, boxes = read()
        again = scroll_anchor(boxes, image)
        if again is not None and abs(again[1] - anchor[1]) < 0.005:
            return again, image, boxes
        anchor = again
    return None, image, boxes


def scroll_target(anchor: tuple[float, float]) -> tuple[float, float]:
    return anchor[0], max(anchor[1] - SCROLL_FRACTION, 0.08)


# --- the search field --------------------------------------------------------


_GLYPHS = set("q9aog@")


def _compact(text: str) -> str:
    return re.sub(r"[^0-9a-z&,*!]", "", _norm(text).lower())


def search_text_matches(boxes: Sequence[vision_ocr.TextBox], term: str, height: float) -> bool:
    """True when the search field is showing `term`.

    The field shows a magnifier glyph that Vision reads as "Q", and the
    asterisk and comma are small; so the comparison is on the term's
    characters with the glyph allowed in front.
    """
    wanted = _compact(term)
    loose = re.sub(r"[*,&!]", "", wanted)
    for box in boxes:
        if box.y > height * 0.35:
            continue
        seen = _compact(box.text)
        # The magnifier comes back as "Q", "9", "a"... -- one glyph, at most.
        candidates = {seen}
        if seen[:1] in _GLYPHS:
            candidates.add(seen[1:])
        for candidate in candidates:
            if candidate == wanted or (loose and re.sub(r"[*,&!]", "", candidate) == loose):
                return True
    return False


def field_shows(image: Image.Image, boxes: Sequence[vision_ocr.TextBox], term: str) -> bool:
    """`search_text_matches`, with a closer look when the full frame misses.

    Vision skips a lone small digit -- a search for Blastoise ("9") returns no
    box for the field at all -- so the strip between the result counter and
    the SHOW EVOLUTIONARY LINE toggle is cropped, enlarged and read again.
    The left edge is tried at a few offsets because the magnifier glyph either
    side of it reads as a stray character.
    """
    if search_text_matches(boxes, term, image.height):
        return True
    counter = next(
        (b for b in sorted(boxes, key=lambda b: b.y)
         if b.y < image.height * 0.2 and _PAREN_COUNT.search(_norm(b.text))),
        None,
    )
    if counter is None:
        return False
    toggle = next((b for b in boxes if "evolution" in _norm(b.text).lower()), None)
    top = counter.bottom + counter.h * 0.3
    bottom = toggle.y - toggle.h * 0.3 if toggle is not None else top + image.height * 0.07
    if bottom - top < 8:
        return False
    from PIL import ImageOps

    for left in (0.22, 0.18, 0.25):
        strip = image.crop((int(image.width * left), int(top), int(image.width * 0.80), int(bottom)))
        strip = ImageOps.expand(strip.resize((strip.width * 3, strip.height * 3)),
                                border=60, fill=strip.getpixel((2, 2)))
        if search_text_matches(vision_ocr.ocr(strip), term, float("inf")):
            return True
    return False


def result_count(boxes: Sequence[vision_ocr.TextBox], height: float) -> int | None:
    for box in sorted(boxes, key=lambda b: b.y):
        if box.y > height * 0.2:
            break
        match = _PAREN_COUNT.search(_norm(box.text))
        if match:
            return int(match.group(1))
    return None


# --- the per-species sequence ------------------------------------------------


def dex_term(dex: int) -> str:
    """The storage search for one Pokédex number.

    Zero-padded to three digits: the game accepts "006" for Charizard, and
    Vision does not reliably see a lone "6" in the search field, which is
    what proves the search took.
    """
    return f"{int(dex):03d}"


def filters(term: str, shadow_eligible: bool) -> dict[str, str]:
    out = {"plain": term}
    if shadow_eligible:
        out["shadow"] = f"{term}&shadow"
    out["hundo"] = f"{term}&4*"
    out["good"] = f"{term}&3*,4*"
    return out


def scan_species(hands: Hands, term: str, display: str = "",
                 shadow_eligible: bool = True) -> SpeciesScan:
    record = SpeciesScan(search=term, display=display or term, scanned_at=time.time())
    try:
        hands.ensure_grid()
        wanted = filters(term, shadow_eligible)
        count, entries, image, boxes = hands.search(wanted["plain"])
        if count is None:
            record.error = f"search '{term}' did not take"
            return record
        record.owned = count
        record.cps = [e.cp for e in entries]
        if count == 0 or not entries:
            record.owned = 0
            record.note = "no matches"
            return record

        for name in ("shadow", "hundo", "good"):
            if name not in wanted:
                continue
            count, entries, image, boxes = hands.search(wanted[name])
            if count is None:
                record.note += f" {name} filter unread;"
                continue
            setattr(record, f"{name}_count", count)
            setattr(record, f"{name}_cps", [e.cp for e in entries] if count else [])

        # Open from the high-IV list when there is one, otherwise from all of
        # them.  The grid is already showing the `good` filter at this point.
        if not record.good_count:
            count, entries, image, boxes = hands.search(wanted["plain"])
            limit = 2
        else:
            limit = MAX_OPENED
        targets = sorted({e.cp for e in entries}, reverse=True)[:limit]
        pools: list[dict] = []
        for cp in targets:
            if not hands.open_cell(cp, image, entries):
                record.note += f" cell CP {cp} not located;"
                continue
            # A scroll that opened the Mega Level page must not be read as the
            # detail page: its energy and price are a different thing.
            screens = [s for s in hands.read_detail() if not is_mega_level_page(s[1])]
            parsed = parse_detail_page(screens)
            hands.leave_detail()
            pools.append(parsed)
            _resolve_pools(record, pools)
            record.individuals.append(Individual(
                cp=cp, hp=parsed["hp"], dust_cost=parsed["dust_cost"],
                candy_cost=parsed["candy_cost"], can_power=parsed["can_power"],
                moves=parsed["moves"], lucky=parsed["lucky"],
                shadow=(cp in record.shadow_cps) if record.shadow_cps is not None else None,
                iv_band=_band(cp, record), types_line=parsed["types_line"],
                name=parsed["name"],
            ))
            # Back on the filtered grid, laid out again now the keyboard is down.
            count, entries, image, boxes = hands.regrid()
    except Exception as exc:  # noqa: BLE001 - one bad species must not end the run
        record.error = f"{type(exc).__name__}: {exc}"
    record.note = record.note.strip()
    return record


def _resolve_pools(record: SpeciesScan, pools: list[dict]) -> None:
    """The species-wide counts, from every detail page that showed them.

    Each page shows the same pools, so disagreement is a misread.  The most
    common reading wins; Mega Energy takes the largest, because a missing
    number there is recorded as 0 and a real count is never under-read to
    another real count.
    """
    from collections import Counter

    for key in ("candy", "xl_candy", "stardust", "mega_cost"):
        seen = [p[key] for p in pools if p[key] is not None]
        if seen:
            setattr(record, key, Counter(seen).most_common(1)[0][0])
    energy = [p["mega_energy"] for p in pools if p["mega_energy"] is not None]
    record.mega_energy = max(energy) if energy else None
    record.mega_shown = any(p["mega_shown"] for p in pools)


def _band(cp: int, record: SpeciesScan) -> str:
    if record.hundo_cps is not None and cp in record.hundo_cps:
        return "4*"
    if record.good_cps is not None:
        return "3*" if cp in record.good_cps else "<3*"
    return "?"


# --- a whole account -----------------------------------------------------------


def load_checkpoint(path: Path | None) -> list[SpeciesScan]:
    if not path or not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [SpeciesScan.from_dict(r) for r in rows if isinstance(r, dict) and "search" in r]


def scan_account(
    hands: Hands,
    terms: Sequence[tuple[str, str, bool]],
    *,
    label: str,
    checkpoint: Path | None = None,
    resume: bool = False,
    progress: Callable[[int, int, SpeciesScan], None] | None = None,
) -> list[SpeciesScan]:
    """Scan every `(term, display, shadow_eligible)` and checkpoint as it goes.

    The checkpoint is rewritten after every species, keeping the better of the
    old and new row, so a failed retry never erases a good earlier read.
    """
    previous = {r.search: r for r in load_checkpoint(checkpoint)}
    done = {k for k, r in previous.items() if r.complete} if resume else set()
    pending = [t for t in terms if t[0] not in done]
    if done:
        print(f"[{label}] resuming: {len(terms) - len(pending)} already scanned", flush=True)
    results = dict(previous)
    consecutive = 0
    for index, (term, display, shadow_ok) in enumerate(pending, start=1):
        record = scan_species(hands, term, display, shadow_ok)
        old = results.get(term)
        if old is None or record.complete or not old.complete:
            results[term] = record
        consecutive = consecutive + 1 if record.error else 0
        best = max((i.cp for i in record.individuals), default=None)
        print(
            f"[{label}] [{index}/{len(pending)}] {display:<22} owned={record.owned:<3}"
            f" good={record.good_count if record.good_count is not None else '-':<3}"
            f" hundo={record.hundo_count if record.hundo_count is not None else '-':<2}"
            f" candy={record.candy if record.candy is not None else '-':<5}"
            f" xl={record.xl_candy if record.xl_candy is not None else '-':<5}"
            f" mega={record.mega_energy if record.mega_shown else '-'}"
            f" opened={len(record.individuals)} best={best or '-'}"
            + (f"  ERROR {record.error}" if record.error else "")
            + (f"  ({record.note})" if record.note else ""),
            flush=True,
        )
        if checkpoint:
            checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            checkpoint.write_text(json.dumps([r.as_dict() for r in results.values()], indent=1))
        if progress:
            progress(index, len(pending), record)
        if consecutive >= 3:
            raise RuntimeError(
                f"{label}: 3 consecutive failures ({record.error}). Stopped rather "
                "than scanning blind; check the handset, then re-run with --resume."
            )
    return list(results.values())
