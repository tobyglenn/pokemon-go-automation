"""Which Mega Master League picks each phone owns, and which to power up (user, 23 Nov 2026).

For every Mega-capable species near the top of PvPoke's Mega Master League
ranking, search storage, open the highest-CP one and read its CP, its Mega
Energy, its tags, and whether MEGA EVOLVE is free right now.  Nothing is
evolved and no energy is ever spent: the MEGA EVOLVE button is only looked at.

``--tag SPECIES...`` then puts the ``MegaPower`` tag (created when missing) on
the highest-CP one of each species named -- the ones worth powering up so a
free Mega is worth using when Master League: Mega Edition comes round.

    python scripts/survey_ml_megas.py --device sample-receiver
    python scripts/survey_ml_megas.py --device sample-receiver --tag mewtwo metagross
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from sources import clear_pokemon as clear  # noqa: E402
from sources import config_paths, gbl_vision, pokemon_fleet, special_trade, starter, trade_local  # noqa: E402
from sources import trade_saved_search as saved  # noqa: E402
from sources.gbl_vision import OCRBox  # noqa: E402

TAG = "MegaPower"
# PvPoke Mega Master League (rankings/mega/overall/rankings-10000), 23 Nov 2026,
# Mega and Primal forms in rank order, searched by their storage name.
SPECIES = (
    "kyogre", "gyarados", "mewtwo", "garchomp", "rayquaza", "dragonite", "groudon",
    "tyranitar", "metagross", "latias", "latios", "heracross", "swampert",
    "salamence", "chesnaught", "aggron", "delphox", "scizor", "charizard",
    "gallade", "gengar",
)
# Base stats (atk, def, sta) of the plain forms, for the level-50 hundo CP.
BASE = {
    "kyogre": (270, 228, 205), "gyarados": (237, 186, 216), "mewtwo": (300, 182, 214),
    "garchomp": (261, 193, 239), "rayquaza": (284, 170, 213), "dragonite": (263, 198, 209),
    "groudon": (270, 228, 205), "tyranitar": (251, 207, 225), "metagross": (257, 228, 190),
    "latias": (228, 246, 190), "latios": (268, 212, 190), "heracross": (234, 179, 190),
    "swampert": (208, 175, 225), "salamence": (277, 168, 216), "chesnaught": (201, 204, 204),
    "aggron": (198, 257, 172), "delphox": (230, 189, 181), "scizor": (236, 181, 172),
    "charizard": (223, 173, 186), "gallade": (237, 195, 169), "gengar": (261, 149, 155),
}
CPM_50 = 0.84029999
LOG = config_paths.state_dir() / "ml-megas" / "survey.jsonl"


def max_cp(species: str) -> int:
    atk, dfn, sta = BASE[species]
    return int((atk + 15) * math.sqrt(dfn + 15) * math.sqrt(sta + 15) * CPM_50 ** 2 / 10)


def detail_cp(boxes: list[OCRBox]) -> int | None:
    for box in boxes:
        match = re.search(r"\bcp\s*(\d{2,5})\b", gbl_vision.normalize(box.text))
        if match:
            return int(match.group(1))
    return None


def energies(boxes: list[OCRBox], height: int) -> dict[str, int]:
    """Each '... MEGA ENERGY [X|Y]' label with the number printed above it."""
    found: dict[str, int] = {}
    labels = [b for b in boxes if "energy" in gbl_vision.normalize(b.text)
              and "candy" not in gbl_vision.normalize(b.text)]
    numbers = [b for b in boxes if re.fullmatch(r"[\d,]{1,7}", b.text.strip())]
    for label in labels:
        text = gbl_vision.normalize(label.text)
        # "MEWTWO MEGA" and "ENERGY X" can split; the suffix rides on the last line.
        form = "x" if text.rstrip().endswith(" x") else "y" if text.rstrip().endswith(" y") else "-"
        above = [n for n in numbers if 0 < label.center_y - n.center_y < height * 0.07
                 and abs(n.center_x - label.center_x) < max(label.width * 0.5, 120)]
        if above:
            best = min(above, key=lambda n: label.center_y - n.center_y)
            found.setdefault(form, int(best.text.replace(",", "")))
    return found


def tags_on(boxes: list[OCRBox]) -> list[str]:
    known = {"mega", "max mega", "maxmega", "megapower", "dontrade", "98plus", "silverbottlepot"}
    return sorted({gbl_vision.normalize(b.text) for b in boxes if gbl_vision.normalize(b.text) in known})


def best_tile(image: Image.Image, boxes: list[OCRBox], skip: int = 0) -> OCRBox | None:
    tiles = clear.safe_grid_tiles(image, boxes)
    ranked = sorted(tiles, key=lambda b: -int(re.search(r"\d+", b.text)[0]))
    return ranked[skip] if len(ranked) > skip else None


def disc_level(image: Image.Image, y: int) -> tuple[int, int]:
    """(filled, total) Mega Level discs in the row at `y`, left half of the card.

    Filled discs are the saturated pink-red of the MEGA EVOLVE pill; locked
    ones are grey (sample-receiver Mewtwo, 23 Nov: X 4/4, Y 0/4).
    """
    rgb = image.convert("RGB")
    px = rgb.load()
    filled = total = 0
    run_kind: str | None = None
    run = 0
    min_run = int(image.width * 0.022)   # a disc is ~0.042w across
    for x in range(int(image.width * 0.10), int(image.width * 0.43) + 1):
        # The disc's white DNA mark splits any single row, so a column counts
        # as disc when any pixel within a third of a disc above or below does.
        kind = None
        for dy in range(-int(image.height * 0.008), int(image.height * 0.008) + 1, 3):
            r, g, b = px[min(x, image.width - 1), min(max(y + dy, 0), image.height - 1)]
            if r > 190 and g < 130 and b < 170 and r - g > 80:
                kind = "red"
                break
            if abs(r - g) < 14 and abs(g - b) < 14 and 160 < r < 222:
                kind = "grey"
        if kind == run_kind:
            run += 1
            continue
        if run_kind is not None and run >= min_run:
            total += 1
            filled += run_kind == "red"
        run_kind, run = kind, 1
    return filled, total


def mega_rows(image: Image.Image, boxes: list[OCRBox]) -> list[dict]:
    """One entry per MEGA EVOLVE row: form label, cost beside it, level discs."""
    rows = []
    pills = [b for b in boxes if gbl_vision.normalize(b.text) in ("evolve", "mega evolve", "reversion", "primal reversion")]
    for pill in pills:
        right = [b for b in boxes if abs(b.center_y - pill.center_y) < image.height * 0.03
                 and b.center_x > image.width * 0.5]
        numbers = [b for b in right if re.fullmatch(r"[\d,]{1,6}", b.text.strip())]
        cost: int | str = int(numbers[0].text.replace(",", "")) if numbers else ("free" if not right else "unread")
        below = [b for b in boxes if 0 < b.center_y - pill.center_y < image.height * 0.09
                 and re.fullmatch(r"(m?ega|primal) [a-z]+( [xy])?", gbl_vision.normalize(b.text))]
        label = gbl_vision.normalize(below[0].text) if below else ""
        disc_y = below[0].center_y if below else pill.center_y + int(image.height * 0.065)
        filled, total = disc_level(image, disc_y)
        rows.append({"form": label, "cost": cost, "level": f"{filled}/{total}"})
    return rows


class Survey:
    def __init__(self, phone: starter.Phone):
        self.phone = phone

    async def search(self, term: str) -> tuple[Image.Image, list[OCRBox]]:
        await trade_local.search_picker(self.phone, term, force=True)
        await asyncio.sleep(3.5)
        return await self.phone.read()

    async def open_tile(self, tile: OCRBox, image: Image.Image) -> tuple[bool, Image.Image, list[OCRBox]]:
        await self.phone.tap([tile.center_x, tile.center_y + int(image.height * clear.TILE_RISE)], image)
        return await self.phone.settle(clear.detail_page, timeout=10)

    async def close(self) -> None:
        await clear.close_detail(self.phone)
        await asyncio.sleep(1.0)

    async def open_best(self, species: str) -> tuple[bool, Image.Image, list[OCRBox], int]:
        """The strongest one of `species` open on its page; Armored Mewtwo passed over."""
        image, boxes = await self.search(species)
        for skip in range(3):
            tile = best_tile(image, boxes, skip)
            if tile is None:
                return False, image, boxes, 0
            ok, page, page_boxes = await self.open_tile(tile, image)
            if not ok:
                return False, page, page_boxes, 0
            cp = detail_cp(page_boxes) or 0
            if species == "mewtwo" and special_trade.is_armored_mewtwo_detail(cp, page_boxes):
                await self.close()
                image, boxes = await self.phone.read()
                continue
            return True, page, page_boxes, cp
        return False, image, boxes, 0

    async def look(self, species: str) -> dict:
        row: dict = {"device": self.phone.name, "species": species, "max_cp": max_cp(species)}
        ok, image, boxes, cp = await self.open_best(species)
        if not ok:
            row["owned"] = False
            return row
        row.update(owned=True, cp=cp, energy=energies(boxes, image.height), tags=tags_on(boxes),
                   active=starter.mega_active(boxes))
        if not row["active"]:
            image, boxes, button = await starter.reveal_mega_button(self.phone)
            if button is None:
                row["mega"] = "no button"
            elif button.cost is None and not starter.cost_printed(image, button):
                row["mega"] = "free"
            elif button.cost is None or button.cost == starter.UNREAD_COST:
                row["mega"] = "cost unread"
            else:
                row["mega"] = button.cost
            row["rows"] = mega_rows(image, boxes)
            row.setdefault("energy", {}).update(
                {k: v for k, v in energies(boxes, image.height).items() if k not in row["energy"]})
            row["tags"] = sorted(set(row["tags"]) | set(tags_on(boxes)))
        row["shot"] = str(self.phone.save(image, f"mlmega-{species}"))
        await self.close()
        return row

    async def tag(self, species: str, ensure: tuple[str, ...] = (TAG,)) -> str:
        """Check every tag in `ensure` on the strongest `species`, leaving the rest as they were.

        The list is scrolled by a drag that starts on a row, and on the SE
        (23 Nov 2026) the drag was taken as a tap: it unticked "Mega" on two
        Pokemon.  So every row's tick is remembered when first seen, checked
        again after each scroll, and put back if it changed.
        """
        ok, image, boxes, cp = await self.open_best(species)
        if not ok:
            return "not owned"
        wanted = {t.casefold() for t in ensure}
        if wanted <= set(tags_on(boxes)) | {b for b in wanted if starter.find_box(boxes, b, exact=True)}:
            await self.close()
            return f"CP {cp} already tagged"
        await self.phone.tap([int(image.width * clear.MENU_POINT[0]), int(image.height * clear.MENU_POINT[1])], image)
        ok, image, boxes = await self.phone.settle(
            lambda _i, b: starter.find_box(b, "appraise", exact=True) is not None
            and starter.find_box(b, "tag", exact=True) is not None, timeout=7)
        if not ok:
            raise starter.StepFailed(f"no detail menu (saved {self.phone.save(image, 'mlmega-no-menu')})")
        await self.phone.tap_box(starter.find_box(boxes, "tag", exact=True), image)
        await asyncio.sleep(1.5)
        image, boxes = await self.phone.read()
        expected: dict[str, bool] = {}
        last_names: list[str] = []
        created = False
        for _ in range(14):
            rows = sheet_rows(image, boxes)
            changed = False
            for row in rows:
                name = row_name(row)
                state = ticked(image, row)
                want = True if name in wanted else expected.setdefault(name, state)
                if state != want:
                    self.phone.say(f"tag sheet: {'ticking' if want else 'unticking'} {name!r}")
                    await self.phone.tap_box(row, image)
                    await asyncio.sleep(0.8)
                    changed = True
                    break
            if changed:
                image, boxes = await self.phone.read()
                continue
            names = [row_name(r) for r in rows]
            at_end = names == last_names
            last_names = names
            if wanted <= set(expected) | {n for n in names if n in wanted} and all(
                    expected.get(n, True) for n in wanted) and at_end:
                break
            if at_end and not created and TAG.casefold() not in set(expected) | set(names):
                create = next((r for n in ("add new tag", "new tag", "create new tag", "create tag")
                               if (r := tag_row(boxes, n))), None)
                if create is None:
                    raise starter.StepFailed(f"no {TAG} row and no create control "
                                             f"(saved {self.phone.save(image, 'mlmega-no-row')})")
                await self.create(create, image)
                created = True
                image, boxes = await self.phone.read()
                last_names = []
                continue
            if at_end:
                break
            x = image.width // 2
            await self.phone.swipe([x, int(0.72 * image.height)], [x, int(0.38 * image.height)], image, 0.5, scroll=True)
            await asyncio.sleep(0.8)
            image, boxes = await self.phone.read()
        self.phone.save(image, f"mlmega-sheet-{species}")
        done = starter.find_box(boxes, "done", exact=True)
        if done is not None and not clear.detail_page(image, boxes):
            await self.phone.tap_box(done, image)
        ok, image, boxes = await self.phone.settle(
            lambda i, b: clear.detail_page(i, b)
            and all(starter.find_box(b, t, exact=True) is not None for t in wanted), timeout=8)
        self.phone.save(image, f"mlmega-tagged-{species}")
        await self.close()
        return f"CP {cp} tagged {sorted(ensure)}" if ok else f"CP {cp} NOT confirmed"

    async def create(self, create: OCRBox, image: Image.Image) -> None:
        await self.phone.tap_box(create, image)
        ok, image, boxes = await self.phone.settle(
            lambda i, b: starter.find_box(b, "enter tag name", exact=True) is not None, timeout=6)
        if not ok:
            raise starter.StepFailed(f"no tag creation sheet (saved {self.phone.save(image, 'mlmega-no-create')})")
        await self.phone.tap_box(starter.find_box(boxes, "enter tag name", exact=True), image)
        await asyncio.sleep(0.5)
        await trade_local.type_text(self.phone, TAG)
        typed = (await saved.editor_text(self.phone) or "").strip()
        if typed.casefold() != TAG.casefold():
            raise starter.StepFailed(f"the tag name read back as {typed!r}")
        if self.phone.ios:
            await clear._ios_tap_done(self.phone)
        else:
            await trade_local.submit(self.phone)
        await asyncio.sleep(1.0)
        image, boxes = await self.phone.read()
        done = starter.find_box(boxes, "done", exact=True)
        if done is not None and starter.find_box(boxes, TAG.casefold(), exact=True) is not None:
            await self.phone.tap_box(done, image)
            await asyncio.sleep(1.0)


def sheet_rows(image: Image.Image, boxes: list[OCRBox]) -> list[OCRBox]:
    """The tag rows of a "TAG 1 POKEMON" sheet (the leading dot reads as a bullet)."""
    skip = {"remote trade", "visible to forever friends", "done", "tag 1 pokemon", "ag 1 pokemon"}
    rows = []
    for b in boxes:
        name = row_name(b)
        if (name and name not in skip and image.height * 0.2 < b.center_y < image.height * 0.85
                and b.x < image.width * 0.35 and not re.search(r"\d{1,2}:\d{2}|location|service", name)):
            rows.append(b)
    return rows


def row_name(box: OCRBox) -> str:
    return gbl_vision.normalize(box.text).strip(" •·+")


def ticked(image: Image.Image, row: OCRBox) -> bool:
    """The green tick at the row's right end."""
    rgb = image.convert("RGB")
    px = rgb.load()
    hits = 0
    for x in range(int(image.width * 0.80), int(image.width * 0.96), 2):
        for y in range(row.center_y - int(image.height * 0.015), row.center_y + int(image.height * 0.015), 2):
            r, g, b = px[x, max(0, min(y, image.height - 1))]
            if g > 170 and g - r > 40 and b > 110:
                hits += 1
    return hits >= 8


def tag_row(boxes: list[OCRBox], name: str) -> OCRBox | None:
    return next((b for b in boxes if gbl_vision.normalize(b.text).strip(" •·+") == name.casefold()), None)


def log(row: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(json.dumps(row) + "\n")


async def main(args: argparse.Namespace) -> int:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    spec = fleet.devices[args.device]
    opts = starter.parse_args(["--only", "mega", "--shots", str(args.shots)] if args.shots else ["--only", "mega"])

    async def work(phone: starter.Phone) -> None:
        survey = Survey(phone)
        await phone.go_home()
        await starter.open_storage(phone)
        if args.tag:
            for species in args.tag:
                try:
                    result = await survey.tag(species, tuple(args.ensure))
                except starter.StepFailed as exc:
                    result = f"FAILED: {exc}"
                print(f"[{phone.name}] tag {species}: {result}", flush=True)
                log({"device": phone.name, "species": species, "tag": result})
        else:
            for species in args.species:
                try:
                    row = await survey.look(species)
                except starter.StepFailed as exc:
                    row = {"device": phone.name, "species": species, "error": str(exc)}
                    await survey.close()
                print("ROW " + json.dumps(row), flush=True)
                log(row)
        await phone.go_home()

    await starter.with_phone(spec, opts, work)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", required=True)
    parser.add_argument("--species", nargs="+", default=list(SPECIES))
    parser.add_argument("--tag", nargs="+", metavar="SPECIES", help=f"put {TAG} on these species' strongest")
    parser.add_argument("--ensure", nargs="+", default=[TAG],
                        help=f"tags that must end up ticked (default: {TAG}); others are left as found")
    parser.add_argument("--shots", type=Path)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
