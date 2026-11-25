"""Tag the receiver's spare 3-star legendaries for special trades (user, 14 Nov 2026).

Two rules apply to the selected phone and species:

1. Any with two stats at 15 (and not a hundo) gets ``SilverBottlePot`` (the
   game's 15-letter cap on "SilverBottlePotential").
   The in-game search finds these exactly: ``4attack`` is an IV of 15.
2. Untagged 3-star ones (never shiny, lucky, shadow or traded) whose Attack is
   13 or below get ``ToTrade``.  Search only grades Attack 11-14 as one band,
   so each candidate is appraised and its Attack bar measured.

The appraisal bar is three segments of five; its filled length gives the IV.
In a sample appraised card, a 13/15/13 Zamazenta read 13.26 / 15.00 / 13.29, so a
reading is taken as IV + ~0.25.  Readings within the band between 13 and 14
are skipped rather than guessed.

Menus are tapped by their own label only; TRANSFER sits right under APPRAISE
and is never pressed.  ``--dry-run`` appraises and prints, and tags nothing.

    python scripts/tag_spare_legendaries.py --dry-run --limit 5
    python scripts/tag_spare_legendaries.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from sources import config_paths, gbl_vision, pokemon_fleet, special_trade, starter, trade_local  # noqa: E402
from sources.gbl_vision import OCRBox  # noqa: E402

SPECIES = "zacian,zamazenta,mewtwo"
# The game caps tag names at 15 letters: "SilverBottlePotential" would not fit.
SILVER_TAG = "SilverBottlePot"
TRADE_TAG = "ToTrade"
TWO_FIFTEENS = "4attack,4defense&4attack,4hp&4defense,4hp"
# The special trade's own filter: untagged, never dontrade/favorite/costume/purified/buddy/shiny/lucky/hundo/shadow/traded.
GIVE_FILTER = "&!#&!dontrade&!favorite&!costume&!purified&!buddy1-5&!shiny&!lucky&!4*&!shadow&!traded"
MENU_POINT = (0.86, 0.932)        # the detail page's menu button, bottom right
TEXT_BOX_POINT = (0.5, 0.93)      # the appraisal's speech box: a tap closes it
BAR_DROP = 0.021                  # the bar sits this far (of height) below its label
# Reading = IV + ~0.25 (rounded bar end).  A 13 reads ~13.25, a 14 ~14.25.
ATTACK_MAX_READING = 13.6
ATTACK_MIN_FOURTEEN = 13.9
LOG = config_paths.state_dir() / "special-trade" / "iv-tags.jsonl"


def bar_reading(image: Image.Image, label: OCRBox, drop: float = BAR_DROP) -> float | None:
    """15 x the filled share of the appraisal bar under `label` (None when no bar is found)."""
    rgb = image.convert("RGB")
    pixels = rgb.load()
    width, height = rgb.size
    best: tuple[int, float] | None = None
    for dy in range(int(height * (drop - 0.008)), int(height * (drop + 0.008)), 2):
        y = label.center_y + dy
        if not 0 <= y < height:
            continue
        kinds: list[tuple[int, bool]] = []
        for x in range(max(0, label.x - int(width * 0.05)), int(width * 0.5)):
            r, g, b = pixels[x, y]
            spread = max(r, g, b) - min(r, g, b)
            if spread > 60:
                kinds.append((x, True))            # filled: orange or pink
            elif min(r, g, b) >= 200 and max(r, g, b) <= 240 and spread < 18:
                kinds.append((x, False))           # the empty grey track
        if len(kinds) < width * 0.15:
            continue
        left, right = kinds[0][0], kinds[-1][0]
        filled = [x for x, fill in kinds if fill]
        share = 0.0 if not filled else (max(filled) - left + 1) / (right - left + 1)
        if best is None or right - left > best[0]:
            best = (right - left, share)
    return None if best is None else round(15 * best[1], 2)


def appraisal_readings(image: Image.Image, boxes: list[OCRBox]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for key in ("attack", "defense", "hp"):
        labels = [b for b in boxes if gbl_vision.normalize(b.text) == key and b.center_y > image.height * 0.5]
        out[key] = bar_reading(image, labels[0]) if labels else None
    return out


def attack_verdict(readings: dict[str, float | None]) -> str:
    """"trade", "keep" or "unsure" for the ToTrade rule."""
    attack, defense, hp = readings.get("attack"), readings.get("defense"), readings.get("hp")
    if attack is None or defense is None or hp is None:
        return "unsure"
    if defense >= 14.9 and hp >= 14.9:
        return "keep"                  # two 15s: the silver-bottle rule's, not a trade
    if attack <= ATTACK_MAX_READING:
        return "trade"
    if attack >= ATTACK_MIN_FOURTEEN:
        return "keep"
    return "unsure"


def tile_cps(image: Image.Image, boxes: list[OCRBox]) -> list[OCRBox]:
    return sorted((b for b in boxes if re.fullmatch(r"cp\s*\d+", gbl_vision.normalize(b.text))
                   and b.center_y > image.height * 0.22), key=lambda b: (b.center_y // 40, b.center_x))


def result_count(boxes: list[OCRBox]) -> int | None:
    for box in boxes:
        found = re.search(r"\((\d+)\)", box.text)
        if found:
            return int(found.group(1))
    return None


def identity(boxes: list[OCRBox], image: Image.Image) -> str:
    """Name, CP and HP line: what tells one detail page from the next."""
    parts = [b.text for b in boxes if image.height * 0.06 < b.center_y < image.height * 0.56
             and (re.search(r"cp\s*\d", b.text.lower()) or "hp" in b.text.lower()
                  or (b.center_y > image.height * 0.42 and re.fullmatch(r"[A-Za-z' .-]{3,}", b.text.strip())))]
    return " | ".join(parts)


def detail_page(image: Image.Image, boxes: list[OCRBox]) -> bool:
    return any("stardust" in b.text.lower() for b in boxes) and any(re.search(r"\d+\s*/\s*\d+\s*hp", b.text.lower()) for b in boxes)


async def back_to_map(phone: starter.Phone) -> None:
    """Close detail pages and storage with their X first: starter's go_home
    cannot leave a Pokemon detail page (sample-receiver, 14 Nov)."""
    for _ in range(4):
        image, boxes = await phone.read()
        if not (detail_page(image, boxes) or starter.storage_open(boxes)
                or starter.find_box(boxes, "tag 1 pokemon", "tag 1 pokémon")):
            break
        point = starter.close_button(image)
        await phone.tap(point or [image.width // 2, int(image.height * 0.932)], image)
        await asyncio.sleep(2)
    await phone.go_home()


class Tagger:
    def __init__(self, phone: starter.Phone, dry_run: bool):
        self.phone = phone
        self.dry_run = dry_run

    async def search(self, term: str) -> tuple[Image.Image, list[OCRBox], int | None]:
        await trade_local.search_picker(self.phone, term, force=True)
        await asyncio.sleep(3.5)
        image, boxes = await self.phone.read()
        return image, boxes, result_count(boxes)

    async def open_first(self, image: Image.Image, boxes: list[OCRBox]) -> tuple[Image.Image, list[OCRBox]]:
        tiles = tile_cps(image, boxes)
        if not tiles:
            raise starter.StepFailed(f"no tile in the search (saved {self.phone.save(image, 'ivtag-no-tile')})")
        await self.phone.tap([tiles[0].center_x, tiles[0].center_y + int(image.height * 0.06)], image)
        ok, image, boxes = await self.phone.settle(detail_page, timeout=10)
        if not ok:
            raise starter.StepFailed(f"the Pokemon did not open (saved {self.phone.save(image, 'ivtag-no-detail')})")
        return image, boxes

    async def open_menu(self, word: str) -> tuple[Image.Image, list[OCRBox]]:
        """The detail menu, then exactly the item `word` (never TRANSFER)."""
        image, boxes = await self.phone.read()
        await self.phone.tap([int(image.width * MENU_POINT[0]), int(image.height * MENU_POINT[1])], image)
        ok, image, boxes = await self.phone.settle(
            lambda _i, b: starter.find_box(b, "appraise", exact=True) is not None
            and starter.find_box(b, "tag", exact=True) is not None, timeout=6)
        if not ok:
            raise starter.StepFailed(f"the detail menu did not open (saved {self.phone.save(image, 'ivtag-no-menu')})")
        item = starter.find_box(boxes, word, exact=True)
        await self.phone.tap_box(item, image)
        await asyncio.sleep(2)
        return await self.phone.read()

    async def appraise(self) -> dict[str, float | None]:
        image, boxes = await self.open_menu("appraise")
        for _ in range(5):
            if sum(gbl_vision.normalize(b.text) in ("attack", "defense", "hp") for b in boxes) >= 3:
                break
            # The leader's opening lines: tap through them.
            await self.phone.tap([int(image.width * TEXT_BOX_POINT[0]), int(image.height * 0.85)], image)
            await asyncio.sleep(2)
            image, boxes = await self.phone.read()
        else:
            raise starter.StepFailed(f"no appraisal bars (saved {self.phone.save(image, 'ivtag-no-bars')})")
        await asyncio.sleep(1.0)            # the bars animate in
        image, boxes = await self.phone.read()
        readings = appraisal_readings(image, boxes)
        self.last_appraisal = self.phone.save(image, "ivtag-appraisal")
        await self.phone.tap([int(image.width * TEXT_BOX_POINT[0]), int(image.height * TEXT_BOX_POINT[1])], image)
        ok, _image, _boxes = await self.phone.settle(
            lambda i, b: detail_page(i, b) and not any(gbl_vision.normalize(x.text) == "defense" for x in b), timeout=8)
        if not ok:
            raise starter.StepFailed(f"the appraisal did not close (saved {self.phone.save(_image, 'ivtag-appraisal-open')})")
        return readings

    async def tag(self, name: str) -> bool:
        """Put `name` on the open Pokemon; True once its pill shows on the page."""
        if self.dry_run:
            return True
        image, boxes = await self.open_menu("tag")
        if starter.find_box(boxes, "tag 1 pokemon", "tag 1 pokémon") is None:
            raise starter.StepFailed(f"the tag list did not open (saved {self.phone.save(image, 'ivtag-no-list')})")
        wanted = name.lower()
        row = None
        for _ in range(4):
            row = next((b for b in boxes if gbl_vision.normalize(b.text).lstrip("•· ").strip() == wanted), None)
            if row is not None:
                break
            x = image.width // 2
            await self.phone.swipe([x, int(image.height * 0.58)], [x, int(image.height * 0.30)], image, 0.6, scroll=True)
            await asyncio.sleep(1.5)
            image, boxes = await self.phone.read()
        if row is None:
            raise starter.StepFailed(f"no {name} in the tag list (saved {self.phone.save(image, 'ivtag-no-row')})")
        await self.phone.tap_box(row, image)
        await asyncio.sleep(1.2)
        image, boxes = await self.phone.read()
        done = starter.find_box(boxes, "done", exact=True)
        if done is None:
            raise starter.StepFailed(f"no DONE on the tag list (saved {self.phone.save(image, 'ivtag-no-done')})")
        await self.phone.tap_box(done, image)
        ok, image, boxes = await self.phone.settle(
            lambda i, b: detail_page(i, b) and any(gbl_vision.normalize(x.text) == wanted for x in b), timeout=8)
        if not ok:
            self.phone.save(image, "ivtag-no-pill")
        return ok

    async def close_detail(self) -> None:
        image = await self.phone.look()
        point = starter.close_button(image)
        await self.phone.tap(point or [image.width // 2, int(image.height * 0.932)], image)
        await asyncio.sleep(2)

    async def next_pokemon(self, before: str) -> str | None:
        """Swipe the detail page to the next Pokemon; None at the end of the list."""
        image, boxes = await self.phone.read()
        # Higher up the swipe turns the Pokemon's model instead (sample-receiver, 14 Nov).
        y = int(image.height * 0.55)
        await self.phone.swipe([int(image.width * 0.88), y], [int(image.width * 0.12), y], image, 0.35)
        await asyncio.sleep(2.5)
        after, boxes = await self.phone.read()
        if not detail_page(after, boxes):
            return None
        now = identity(boxes, after)
        # Two Pokemon can share a CP and HP (sample-receiver: two CP 2149 Zacian), so
        # the end of the list is judged by the page not changing at all.
        if now == before and not page_changed(image, after):
            return None
        return now


def page_changed(before: Image.Image, after: Image.Image) -> bool:
    """Whether a swipe moved to another Pokemon: the card area differs."""
    size = (60, 120)
    a = before.convert("L").resize(size)
    b = after.convert("L").resize(size)
    diff = sum(abs(x - y) for x, y in zip(a.getdata(), b.getdata())) / (size[0] * size[1])
    return diff > 6.0


def log(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as out:
        out.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **entry}) + "\n")


async def silver_phase(t: Tagger, species: str, limit: int) -> int:
    term = f"{species}&{TWO_FIFTEENS}&!4*&!#{SILVER_TAG}"
    tagged = 0
    for _ in range(limit):
        image, boxes, count = await t.search(term)
        print(f"[silver] {count} to tag", flush=True)
        if not count:
            break
        image, boxes = await t.open_first(image, boxes)
        who = identity(boxes, image)
        if t.dry_run:
            print(f"[silver] would tag {who}", flush=True)
            await t.close_detail()
            break
        ok = await t.tag(SILVER_TAG)
        log({"phase": "silver", "who": who, "tagged": ok})
        print(f"[silver] {'tagged' if ok else 'NOT confirmed'}: {who}", flush=True)
        await t.close_detail()
        if not ok:
            break
        tagged += 1
    return tagged


def _detail_cp(boxes: list[OCRBox]) -> int | None:
    for box in boxes:
        match = re.search(r"\bcp\s*(\d+)", gbl_vision.normalize(box.text))
        if match:
            return int(match.group(1))
    return None


async def trade_phase(t: Tagger, species: str, limit: int) -> dict[str, int]:
    term = f"{species}&3*&0-3attack{GIVE_FILTER}"
    image, boxes, count = await t.search(term)
    print(f"[trade] {count} candidate(s) to appraise", flush=True)
    tally = {"trade": 0, "keep": 0, "unsure": 0, "skip_protected": 0}
    if not count:
        return tally
    image, boxes = await t.open_first(image, boxes)
    who = identity(boxes, image)
    for index in range(min(count, limit)):
        detail_cp = _detail_cp(boxes)
        if special_trade.is_armored_mewtwo_detail(detail_cp, boxes) or special_trade.has_dontrade_tag(boxes):
            tally["skip_protected"] += 1
            log({"phase": "trade", "who": who, "verdict": "skip_protected", "tagged": False, "dry_run": t.dry_run})
            print(f"[trade] {index + 1}/{count} {who}: skip_protected (Armored Mewtwo or dontrade)", flush=True)
        else:
            readings = await t.appraise()
            verdict = attack_verdict(readings)
            tagged = False
            if verdict == "trade":
                tagged = await t.tag(TRADE_TAG)
            tally[verdict] += 1
            log({"phase": "trade", "who": who, "readings": readings, "verdict": verdict,
                 "tagged": tagged, "dry_run": t.dry_run, "shot": str(t.last_appraisal)})
            print(f"[trade] {index + 1}/{count} {who}: {readings} -> {verdict}"
                  + (" (tagged)" if tagged and not t.dry_run else ""), flush=True)
            if verdict == "trade" and not tagged:
                print("[trade] the ToTrade pill did not show; stopping", flush=True)
                break
        nxt = await t.next_pokemon(who)
        if nxt is None:
            break
        who = nxt
        image, boxes = await t.phone.read()
    await t.close_detail()
    return tally


async def main(args: argparse.Namespace) -> int:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    spec = fleet.devices[args.device]
    opts = starter.parse_args(["--only", "battle"])

    async def work(phone: starter.Phone) -> None:
        t = Tagger(phone, args.dry_run)
        await back_to_map(phone)
        await starter.open_storage(phone)
        if "silver" in args.phases:
            print(f"[silver] done: {await silver_phase(t, args.species, args.limit)} tagged", flush=True)
        if "trade" in args.phases:
            print(f"[trade] done: {await trade_phase(t, args.species, args.limit)}", flush=True)
        await back_to_map(phone)

    await starter.with_phone(spec, opts, work)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", default=starter.special_receiver(),
                        help="fleet alias (defaults to the private special-trade receiver)")
    parser.add_argument("--species", default=SPECIES, help=f"comma-separated search names (default: {SPECIES})")
    parser.add_argument("--phases", nargs="+", choices=("silver", "trade"), default=["silver", "trade"])
    parser.add_argument("--limit", type=int, default=200, help="most Pokemon per phase")
    parser.add_argument("--dry-run", action="store_true", help="appraise and print; tag nothing")
    args = parser.parse_args(argv)
    if not args.device:
        parser.error("specify --device or configure a special-trade receiver")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
