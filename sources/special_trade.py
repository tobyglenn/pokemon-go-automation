"""The receiver's daily special trade: a spare legendary for a shiny it is missing.

The shiny gap report (named in the private settings, written by the shiny
scan) lists every species the receiver has no shiny of and which other phone
already holds one.  Each run:

1. Reads the report and keeps the species worth trading for: held by another
   phone, and a base form.  An evolved form is never traded for, since its
   shiny comes from evolving the lower stage (Mr. Mime comes from a Mime Jr.);
   nor is anything the report calls untradeable.
2. Discovers attached phones across ALL configured computers (`local` and peer
   Macs over SSH via `sources/remote_target.py`, unless `--local` is passed).
   The settings map the report's phone labels to the fleet phones now holding
   those accounts.
3. Trades from the list `--prepare` built beforehand: the partner gives the next shiny on its list, the receiver
   an untagged legendary from its 2-star pool (the lowest appraisal raid
   legendaries reach; 3 and 4 stars are never given), falling back to its `#ToTrade`
   tagged 3-star spares once 2-star ones run out. `--all` continues through every
   partner until the receiver has 0 special trades left for the day.
4. Removes each traded species from the report, and logs it.

`--prepare` (its own run) counts the receiver's 2-star legendaries per
species and searches each attached partner for every shiny it is listed as
holding, and saves both in order.  A partner plugged in that --prepare never
searched is searched for its own listed shinies when the trade runs.  When the 2-star pool
is empty the trade stops and says the exact-IV assessment needs running.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from PIL import Image

from . import (
    collect_trades,
    config_paths,
    fleet_entrypoint,
    gbl_home_recovery as home,
    gbl_ios,
    pokemon_fleet,
    remote_target,
    screen_router,
    starter_trade,
    trade_local,
    trade_plan,
)
from .starter import Phone, Report, StepFailed

OCRBox = trade_local.OCRBox

# Who receives, the report naming its gaps, and which fleet phone now holds
# each account the report names by model: all private, from
# private_config_dir()/special-trade.json, e.g.
#   {"receiver": "android-three", "report": "~/.local/state/.../gap_report.md",
#    "holders": {"PhoneA": "android-one", "PhoneB": "android-two"}}
SETTINGS = "special-trade.json"
RECEIVER = ""
REPORT = Path()
HOLDERS: dict[str, str] = {}
STATE = config_paths.state_dir() / "special-trade"
# The receiver gives the lowest-CP eligible untagged specimen first.
LEGENDARIES = ("zamazenta", "zacian", "necrozma", "mewtwo", "kyurem", "rayquaza")
# A tagged spare is eligible once no untagged 2-star specimen remains.
SPARE_TAG = "ToTrade"
SPARE_FILTER = "&!#KeepOne&!dontrade&!favorite&!costume&!purified&!buddy1-5&!shiny&!lucky&!4*&!shadow&!traded"
# Never given: tagged (#), dontrade, favorite, costume, purified, buddy, shiny, lucky, perfect, shadow (cannot trade), traded (cannot again).
GIVE_FILTER = "&!#&!dontrade&!favorite&!costume&!purified&!buddy1-5&!shiny&!lucky&!4*&!shadow&!traded"
ARMORED_MEWTWO_MOVES: tuple[str, ...] = ("iron tail", "dynamic punch", "rock slide", "earthquake")
ARMORED_MEWTWO_MAX_CP = 2280
# Raid legendaries never appraise below 2 stars (IV floor 10/10/10 = 67%);
# only 2-star specimens are selected by the default pool.
GIVE_STARS = "2*"
PREPARED = "prepared.json"
# Best friends pay 800 for a shiny or legendary, 40,000 for a new Pokedex
# entry; anything dearer is a person's call.
MAX_PRICE = 40_000
# A species a partner was found not to hold is not counted again for a week.
MISSING_DAYS = 7
TILE_RISE = 0.055   # a picker tile's sprite sits this far (of height) under its CP label


class ReceiverSpent(trade_local.TradeOver):
    """The receiver can give nothing more today: no special trade left, or no legendary."""


class PartnerSpent(trade_local.TradeOver):
    """This partner has no special trade left today; another partner may."""


class PartnerUnavailable(StepFailed):
    """This partner phone could not be connected (e.g. asleep); another partner may."""


# --- the report ---------------------------------------------------------------------

@dataclass(frozen=True)
class Need:
    dex: int
    name: str
    route: str
    holders: tuple[str, ...]   # fleet phone names
    detail: str

    @property
    def give_term(self) -> str:
        """The partner's search for a shiny copy it can trade (never a 4* shundo)."""
        return f"{self.dex}&shiny&!4*&!dontrade&!#KeepOne&!shadow&!traded"


ROW = re.compile(r"^\|\s*(\d{4})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$")


def tradeable(route: str, detail: str) -> bool:
    """A base form held elsewhere: worth a special trade.

    CATCH rows are first or single stages; babies say so.  EVOLVE rows mean
    the receiver already has the lower stage, and "needs a shiny X first" rows that a
    lower stage is still missing -- trading for that stage covers both.
    """
    detail = detail.lower()
    if "cannot be traded" in detail:
        return False
    return route.strip().upper() == "CATCH" or "baby" in detail


def read_needs(text: str) -> list[Need]:
    needs: list[Need] = []
    in_list = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_list = line.strip().lower() == "## full list"
            continue
        match = ROW.match(line) if in_list else None
        if not match:
            continue
        dex, name, _region, route, on, detail, _confirmed = (g.strip() for g in match.groups())
        holders = tuple(HOLDERS[h] for h in on.split("+") if h.strip() in HOLDERS) if on != "-" else ()
        if holders and tradeable(route, detail):
            needs.append(Need(int(dex), name, route, holders, detail))
    return needs


def remove_need(path: Path, dex: int) -> bool:
    """Drop the species' row from the report's full list; True if it was there."""
    lines = path.read_text().splitlines(keepends=True)
    kept, removed, in_list = [], False, False
    for line in lines:
        if line.startswith("## "):
            in_list = line.strip().lower() == "## full list"
        match = ROW.match(line.rstrip("\n")) if in_list else None
        if match and int(match.group(1)) == dex:
            removed = True
            continue
        kept.append(line)
    if removed:
        path.write_text("".join(kept))
    return removed


# --- what has happened before --------------------------------------------------------

def _load(name: str) -> dict:
    with contextlib.suppress(Exception):
        return json.loads((STATE / name).read_text())
    return {}


def _store(name: str, data: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(json.dumps(data, indent=1, sort_keys=True))


def missing_recently(holder: str, dex: int, now: datetime | None = None) -> bool:
    seen = _load("missing.json").get(f"{holder}:{dex}")
    if not seen:
        return False
    with contextlib.suppress(ValueError):
        return (now or datetime.now()) - datetime.fromisoformat(seen) < timedelta(days=MISSING_DAYS)
    return False


def mark_missing(holder: str, dex: int) -> None:
    data = _load("missing.json")
    data[f"{holder}:{dex}"] = datetime.now().isoformat(timespec="seconds")
    _store("missing.json", data)


def done_today(now: datetime | None = None) -> str | None:
    """What the receiver's special trade for today already was, if it is made."""
    return _load("today.json").get((now or datetime.now()).date().isoformat())


def mark_done_today(what: str) -> None:
    today = datetime.now().date().isoformat()
    _store("today.json", {today: what})


def log_trade(need: Need, holder: str, gave: str, got: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "traded.jsonl").open("a") as out:
        out.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "dex": need.dex,
                              "name": need.name, "from": holder, "gave": gave, "got": got}) + "\n")


# --- the picker ---------------------------------------------------------------------

def pool_term(species: Sequence[str] | None = None) -> str:
    """The receiver's give pool: 2-star, untagged, never shiny/lucky/perfect/shadow/traded."""
    return ",".join(species or LEGENDARIES) + f"&{GIVE_STARS}" + GIVE_FILTER


def spare_term(species: Sequence[str] | None = None) -> str:
    """The receiver's tagged spares: its #ToTrade legendaries, never shiny/lucky/perfect/shadow/traded."""
    return ",".join(species or LEGENDARIES) + f"&#{SPARE_TAG}" + SPARE_FILTER


def give_term(ceiling: int | None, pool: str | None = None, floor: int | None = None) -> str:
    term = pool or pool_term()
    if floor:
        term += f"&cp{floor}-"
    if ceiling:
        term += f"&cp-{ceiling}"
    return term


def is_armored_mewtwo_detail(cp: int | None, boxes: Sequence[OCRBox]) -> bool:
    """True when an open detail or trade-preview card belongs to Armored Mewtwo.

    Pokémon GO's `!costume` search does not exclude Armored Mewtwo (form MEWTWO_A).
    Regular L20 raid Mewtwo starts at CP 2294+, whereas Armored Mewtwo caps around
    CP 1821 at L20 (2276 at L25) and learns Iron Tail / Dynamic Punch / Rock Slide / Earthquake.
    """
    joined = " ".join(trade_local.norm(b.text) for b in boxes)
    if not re.search(r"\bmewtwo\b", joined):
        return False
    if "armored" in joined or any(move in joined for move in ARMORED_MEWTWO_MOVES):
        return True
    return cp is not None and cp < ARMORED_MEWTWO_MAX_CP


def has_dontrade_tag(boxes: Sequence[OCRBox]) -> bool:
    """True when an open detail or trade-preview card carries the `dontrade` or `Keep One` tag pill."""
    return any(trade_local.norm(b.text).lstrip("•· ").strip() in {"dontrade", "keepone", "keep one"} for b in boxes)


def load_prepared() -> dict:
    return _load(PREPARED)


def save_prepared(data: dict) -> None:
    _store(PREPARED, data)


def after_trade_missing(prepared: dict, partner: str, dex: int) -> dict:
    queue = prepared.setdefault("partners", {}).get(partner, [])
    if dex in queue:
        queue.remove(dex)
    return prepared


def give_species(prepared: dict) -> list[str]:
    return [sp for sp, n in prepared.get("give", {}).items() if n > 0]


def after_trade(prepared: dict, partner: str, dex: int) -> dict:
    """One fewer 2-star legendary (which species the picker took is unread) and the shiny done."""
    give = prepared.setdefault("give", {})
    for species in give_species(prepared)[:1]:
        give[species] -= 1
    queue = prepared.setdefault("partners", {}).get(partner, [])
    if dex in queue:
        queue.remove(dex)
    return prepared


def cp_labels(image: Image.Image, boxes: Sequence[OCRBox]) -> list[tuple[int, OCRBox]]:
    out = []
    for box in boxes:
        match = re.fullmatch(r"cp\s*(\d+)", trade_local.norm(box.text))
        if match and box.center_y > image.height * 0.18:
            out.append((int(match.group(1)), box))
    return sorted(out, key=lambda pair: pair[0])


def special_left(boxes: Sequence[OCRBox]) -> int | None:
    """The picker's "N special trades remaining" figure, when it reads."""
    box = trade_local.find_box(boxes, "special trades remaining", "special trade remaining")
    if box is None:
        return None
    digits = re.search(r"\d+", box.text)
    return int(digits.group()) if digits else None


def result_count(image: Image.Image, boxes: Sequence[OCRBox]) -> int | None:
    counter = trade_local._counter(image, boxes)
    shown = re.search(r"\((\d+)\)", counter.text) if counter else None
    return int(shown.group(1)) if shown else None


async def _read_special_left(phone: Phone) -> int | None:
    """Read 'N Special Trades remaining', clearing an active picker filter if needed."""
    image, boxes = await phone.read()
    rem = special_left(boxes)
    if rem is not None:
        return rem
    active = collect_trades._active_picker_filter_box(image, boxes)
    if active is None and trade_local.find_box(boxes, "evolutionary line") is not None:
        active = trade_local.picker_field(image, boxes)
    if active is None:
        return None
    await phone.tap([int(image.width * trade_local.PICKER_CLEAR_X), active.center_y], image)
    await asyncio.sleep(1.0)
    image, boxes = await phone.read()
    rem = special_left(boxes)
    if rem is not None:
        return rem
    # On Android, tapping the pill (x) can open the search/keyboard overlay,
    # hiding the picker grid and its "Special Trades remaining" banner.
    if not phone.ios and not trade_local.picker(image, boxes):
        await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 4")
        await asyncio.sleep(1.0)
        image, boxes = await phone.read()
        rem = special_left(boxes)
    return rem


async def choose_lowest(
    phone: Phone,
    pool: str,
    ceiling: int | None = None,
    *,
    seen_left: list[int] | None = None,
) -> str:
    """Search the picker and open the lowest-CP tile; returns its CP label.

    The picker lists highest CP first, so the lowest may sit off the screen
    or under the sort button. The search is narrowed until the range
    left fits the top rows, and only a tile there is tapped.
    """
    rem = await _read_special_left(phone)
    if seen_left is not None and rem is not None:
        seen_left.append(rem)
    if rem == 0:
        raise trade_local.TradeOver("no special trades remaining today")
    floor: int | None = None
    for _attempt in range(4):
        inner_ceiling = ceiling
        tiles = []
        last_lowest: int | None = None
        for _round in range(3):
            term = give_term(inner_ceiling, pool, floor=floor)
            await trade_local.search_picker(phone, term, force=True)
            ok, image, boxes = await trade_local.settle(
                phone,
                lambda i, b: bool(cp_labels(i, b)) or result_count(i, b) == 0 or trade_local.expired(b),
                10,
            )
            if trade_local.expired(boxes):
                ok_btn = trade_local.find_box(boxes, "ok", exact=True)
                if ok_btn is not None:
                    await phone.tap_box(ok_btn, image)
                raise StepFailed("the trade expired")
            tiles = cp_labels(image, boxes)
            if not tiles and result_count(image, boxes) == 0:
                await asyncio.sleep(2.5)
                image, boxes = await phone.read()
                tiles = cp_labels(image, boxes)
            if not tiles and result_count(image, boxes) != 0 and not ok:
                await trade_local.search_picker(phone, term, force=True)
                ok, image, boxes = await trade_local.settle(
                    phone,
                    lambda i, b: bool(cp_labels(i, b)) or result_count(i, b) == 0 or trade_local.expired(b),
                    10,
                )
                if trade_local.expired(boxes):
                    ok_btn = trade_local.find_box(boxes, "ok", exact=True)
                    if ok_btn is not None:
                        await phone.tap_box(ok_btn, image)
                    raise StepFailed("the trade expired")
                tiles = cp_labels(image, boxes)
            if not tiles:
                if last_lowest is not None:
                    recovery_term = give_term(last_lowest, pool, floor=floor)
                    await trade_local.search_picker(phone, recovery_term, force=True)
                    _, image, boxes = await trade_local.settle(
                        phone,
                        lambda i, b: bool(cp_labels(i, b)) or trade_local.expired(b),
                        10,
                    )
                    if trade_local.expired(boxes):
                        ok_btn = trade_local.find_box(boxes, "ok", exact=True)
                        if ok_btn is not None:
                            await phone.tap_box(ok_btn, image)
                        raise StepFailed("the trade expired")
                    tiles = cp_labels(image, boxes)
                    if tiles:
                        break
                raise trade_local.TradeOver(f"nothing for {term!r} in the picker "
                                            f"(saved {phone.save(image, 'none-left')})")
            count = result_count(image, boxes)
            lowest = tiles[0][0]
            last_lowest = lowest
            if (count is not None and count <= len(tiles) and safe(image, tiles[0][1])) or lowest == inner_ceiling:
                break
            inner_ceiling = lowest
        usable = [(cp, label) for cp, label in tiles if safe(image, label)]
        retry_higher_floor = False
        for cp, label in usable[:3]:
            await phone.tap([label.center_x, tap_y(image, label)], image)
            ok_detail, detail_img, detail_boxes = await trade_local.settle(phone, trade_local.detail, 8)
            if ok_detail:
                protected_reason: str | None = None
                shot_label = ""
                if is_armored_mewtwo_detail(cp, detail_boxes):
                    protected_reason = f"refused Armored Mewtwo CP {cp}"
                    shot_label = "refused-armored-mewtwo"
                elif has_dontrade_tag(detail_boxes):
                    protected_reason = f"refused protected tag on CP {cp}"
                    shot_label = "refused-dontrade"
                if protected_reason is not None:
                    shot = phone.save(detail_img, shot_label)
                    phone.say(f"{protected_reason} (saved {shot})")
                    if not phone.ios:
                        await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 4")
                    else:
                        await phone.tap([int(detail_img.width * 0.08), int(detail_img.height * 0.07)], detail_img)
                    back_ok, _, _ = await trade_local.settle(phone, trade_local.picker, 6)
                    if not back_ok:
                        raise trade_local.TradeRefused(f"{phone.name}: {protected_reason} (saved {shot})")
                    floor = cp + 1
                    ceiling = None
                    retry_higher_floor = True
                    break
                phone.say(f"giving the CP {cp}")
                return f"CP {cp}"
            # A greyed tile opens nothing: a fused Kyurem or Necrozma cannot trade.
            image, boxes = await phone.read()
            if not trade_local.picker(image, boxes):
                raise StepFailed(f"a tile tap left the picker (saved {phone.save(image, 'off-picker')})")
        if not retry_higher_floor:
            break
    raise StepFailed(f"no low-CP legendary tile opened (saved {phone.save(image, 'untradeable')})")


def tap_y(image: Image.Image, label: OCRBox) -> int:
    return label.center_y + int(image.height * TILE_RISE)


def safe(image: Image.Image, label: OCRBox) -> bool:
    """A tile whose sprite is clear of the bottom row's sort and close buttons."""
    return tap_y(image, label) < image.height * 0.80


# --- the run --------------------------------------------------------------------------

async def attached(fleet: pokemon_fleet.FleetConfig) -> dict[str, pokemon_fleet.DeviceSpec]:
    specs = [spec for spec in fleet.devices.values() if spec.enabled]
    androids = await screen_router.attached_androids(specs)
    iphones = pokemon_fleet.ios_connected_udids()
    return {spec.name: spec for spec in specs
            if spec.identifier in (androids if spec.platform == "android" else iphones)}


async def _discover_routes(
    fleet: pokemon_fleet.FleetConfig,
    *,
    local_only: bool = False,
):
    from . import trading_accounts
    machines = fleet_entrypoint.machine_configs()
    if local_only:
        here = await attached(fleet)
        local_name = fleet_entrypoint.local_machine(machines)
        owners = {name: local_name for name in here}
    else:
        owners, _snapshots = remote_target.discover_attached_devices(fleet, machines)
    fleet, owners = trading_accounts.route_fleet(fleet, owners, trading_accounts.aliases())
    here = {name: fleet.devices[name] for name in owners if name in fleet.devices}
    return here, owners, machines, fleet


async def discover_fleet(fleet: pokemon_fleet.FleetConfig, *, local_only: bool = False):
    """Return one canonical trader spec and owning host per attached account."""
    here, owners, machines, _ = await _discover_routes(fleet, local_only=local_only)
    return here, owners, machines


async def _count_terms_on_device(
    spec: pokemon_fleet.DeviceSpec,
    terms: Sequence[str],
    args,
    *,
    machine: str | None = None,
    machines: dict | None = None,
    fleet: pokemon_fleet.FleetConfig | None = None,
) -> dict[str, int]:
    from . import starter_trade

    if (
        machine is None
        or machines is None
        or fleet is None
        or machine == fleet_entrypoint.local_machine(machines)
    ):
        physical = getattr(fleet, "transports", {}).get(spec.name) if fleet is not None else None
        return await starter_trade.count(fleet.devices[physical] if physical else spec, args, list(terms))
    async with remote_target.open_fleet_phone(
        spec.name, args.shots, machine=machine, machines=machines, fleet=fleet, dry_run=False
    ) as phone:
        return await collect_trades.count_queries_on_phone(phone, [(t, t) for t in terms], log_prefix="special scan")


def plan(needs: Sequence[Need], here: set[str]) -> tuple[dict[str, list[Need]], list[Need]]:
    """Each attached partner's needs, and the needs only a phone elsewhere can fill."""
    by_partner: dict[str, list[Need]] = {}
    elsewhere = []
    for need in needs:
        local = [h for h in need.holders if h in here and h != RECEIVER and not missing_recently(h, need.dex)]
        if local:
            by_partner.setdefault(local[0], []).append(need)
        elif not any(h in here for h in need.holders):
            elsewhere.append(need)
    return by_partner, elsewhere


def swap_hint(needs: Sequence[Need], here: set[str]) -> str | None:
    """Which phone to plug in beside the receiver next: the unattached one holding the most."""
    tally: dict[str, list[Need]] = {}
    for need in needs:
        for holder in need.holders:
            if holder not in here and holder != RECEIVER and not missing_recently(holder, need.dex):
                tally.setdefault(holder, []).append(need)
    if not tally:
        return None
    best = max(tally, key=lambda name: len(tally[name]))
    names = ", ".join(n.name for n in tally[best][:6]) + ("..." if len(tally[best]) > 6 else "")
    return (f"plug the {best} in beside {RECEIVER} (unplug a phone already traded out): "
            f"it holds {len(tally[best])} shiny(ies) {RECEIVER} needs -- {names}")


def notify(text: str) -> None:
    import subprocess
    text = text[:220].replace('"', "'")
    with contextlib.suppress(Exception):
        subprocess.run(["osascript", "-e",
                        f'display notification "{text}" with title "Special trade: swap phones"'],
                       timeout=10, check=False)


async def prepare(
    receiver: pokemon_fleet.DeviceSpec,
    partners: dict[str, pokemon_fleet.DeviceSpec],
    by_partner: dict[str, list[Need]],
    args,
    *,
    owners: dict[str, str] | None = None,
    machines: dict | None = None,
    fleet: pokemon_fleet.FleetConfig | None = None,
) -> dict:
    """Count the receiver's 2-star pool per species and which listed shinies each partner holds."""
    from . import starter_trade

    async def pool() -> dict[str, int]:
        terms = {species: pool_term([species]) for species in LEGENDARIES}
        found = await _count_terms_on_device(
            receiver,
            list(terms.values()),
            args,
            machine=(owners or {}).get(receiver.name),
            machines=machines,
            fleet=fleet,
        )
        return {species: found.get(term, 0) for species, term in terms.items()}

    async def holds(name: str) -> tuple[str, list[int]]:
        needs = by_partner[name]
        found = await _count_terms_on_device(
            partners[name],
            [n.give_term for n in needs],
            args,
            machine=(owners or {}).get(name),
            machines=machines,
            fleet=fleet,
        )
        have = []
        for need in needs:
            if found.get(need.give_term, 0) > 0:
                have.append(need.dex)
            else:
                print(f"[special] {name}: no tradeable shiny {need.name} (#{need.dex}); "
                      f"not looked for again for {MISSING_DAYS} days", flush=True)
                mark_missing(name, need.dex)
        return name, have

    results = await starter_trade.together(pool(), *(holds(n) for n in by_partner))
    old = load_prepared()
    data = {"prepared": datetime.now().isoformat(timespec="seconds"), "receiver": RECEIVER,
            "give": results[0], "partners": {**old.get("partners", {}), **dict(results[1:])}}
    save_prepared(data)
    return data


def unprepared(prepared: dict, needs: Sequence[Need], here: set[str]) -> dict[str, list[Need]]:
    """Attached partners --prepare never searched, with the shinies the report says they hold."""
    known = prepared.get("partners", {})
    out: dict[str, list[Need]] = {}
    for name in sorted(here):
        if name == RECEIVER or name in known:
            continue
        wanted = [n for n in needs if name in n.holders and not missing_recently(name, n.dex)]
        if wanted:
            out[name] = wanted
    return out


async def prepare_partners(
    partners: dict[str, pokemon_fleet.DeviceSpec],
    by_partner: dict[str, list[Need]],
    args,
    *,
    owners: dict[str, str] | None = None,
    machines: dict | None = None,
    fleet: pokemon_fleet.FleetConfig | None = None,
) -> dict:
    """Search just these partners' listed shinies and add them to the prepared list."""
    from . import starter_trade

    async def holds(name: str) -> tuple[str, list[int]]:
        needs = by_partner[name]
        print(f"[special] {name} has no prepared list; searching its {len(needs)} listed shiny(ies)",
              flush=True)
        found = await _count_terms_on_device(
            partners[name],
            [n.give_term for n in needs],
            args,
            machine=(owners or {}).get(name),
            machines=machines,
            fleet=fleet,
        )
        have = []
        for need in needs:
            if found.get(need.give_term, 0) > 0:
                have.append(need.dex)
            else:
                print(f"[special] {name}: no tradeable shiny {need.name} (#{need.dex}); "
                      f"not looked for again for {MISSING_DAYS} days", flush=True)
                mark_missing(name, need.dex)
        return name, have

    results = await starter_trade.together(*(holds(n) for n in by_partner))
    data = load_prepared()
    data.setdefault("receiver", RECEIVER)
    data.setdefault("partners", {}).update(dict(results))
    save_prepared(data)
    return data


@contextlib.asynccontextmanager
async def _open_pair(
    receiver: pokemon_fleet.DeviceSpec,
    partner: pokemon_fleet.DeviceSpec,
    args,
    *,
    owners: dict[str, str] | None = None,
    machines: dict | None = None,
    fleet: pokemon_fleet.FleetConfig | None = None,
):
    if owners is not None and machines is not None and fleet is not None:
        async with remote_target.open_fleet_phone(
            receiver.name,
            args.shots,
            machine=owners[receiver.name],
            machines=machines,
            fleet=fleet,
            dry_run=False,
        ) as a:
            partner_cm = remote_target.open_fleet_phone(
                partner.name,
                args.shots,
                machine=owners[partner.name],
                machines=machines,
                fleet=fleet,
                dry_run=False,
            )
            try:
                b = await partner_cm.__aenter__()
            except (gbl_ios.IOSGBLError, home.HomeRecoveryError, pokemon_fleet.FleetError, RuntimeError, OSError) as exc:
                raise PartnerUnavailable(f"{partner.name}: {exc}") from exc
            try:
                yield a, b
            except BaseException as exc:
                if not await partner_cm.__aexit__(type(exc), exc, exc.__traceback__):
                    raise
            else:
                await partner_cm.__aexit__(None, None, None)
        return
    from . import starter_trade
    async with starter_trade.pair_phones([receiver, partner], args) as (a, b):
        yield a, b


async def trade(
    receiver: pokemon_fleet.DeviceSpec,
    partner: pokemon_fleet.DeviceSpec,
    needs: list[Need],
    pool: str,
    args,
    report: Report,
    prepared: dict,
    *,
    owners: dict[str, str] | None = None,
    machines: dict | None = None,
    fleet: pokemon_fleet.FleetConfig | None = None,
    traded_dexes: set[int] | None = None,
    pool_state: dict[str, bool] | None = None,
    unavailable: list[str] | None = None,
) -> bool:
    """Trade down `needs` with one partner; False once the receiver can trade no more today."""
    term = pool
    limit = None if getattr(args, "all_today", False) else args.count
    pair_cm = _open_pair(receiver, partner, args, owners=owners, machines=machines, fleet=fleet)
    try:
        a, b = await pair_cm.__aenter__()
    except PartnerUnavailable as exc:
        msg = f"{partner.name}: skipped (unavailable -- {exc})"
        print(f"[special] {msg}", flush=True)
        report.lines.append(msg)
        if unavailable is not None:
            unavailable.append(partner.name)
        return True
    try:
        names = {a.name: await trade_local.trainer_name(a), b.name: await trade_local.trainer_name(b)}
        go_on = True
        refusals = 0
        for need in needs:
            if traded_dexes is not None and need.dex in traded_dexes:
                continue
            if limit is not None and args.made >= limit:
                go_on = False
                break
            gave = {"cp": "?"}
            recv_left: list[int] = []
            partner_left: list[int] = []

            async def receiver_pick(phone: Phone) -> None:
                if not (pool_state and pool_state.get("empty") and SPARE_TAG):
                    try:
                        gave["cp"] = await choose_lowest(phone, pool, seen_left=recv_left)
                        return
                    except trade_local.TradeOver as exc:
                        if "nothing for" not in str(exc) or not SPARE_TAG:
                            raise ReceiverSpent(f"{phone.name}: {exc}") from exc
                    if pool_state is not None:
                        pool_state["empty"] = True
                # No untagged 2-star left: the spares the user tagged for trading.
                phone.say(f"no {GIVE_STARS} spare left; giving from #{SPARE_TAG}")
                try:
                    gave["cp"] = await choose_lowest(phone, spare_term(), seen_left=recv_left)
                except trade_local.TradeOver as exc:
                    raise ReceiverSpent(f"{phone.name}: {exc}") from exc

            async def partner_pick(phone: Phone) -> None:
                rem = await _read_special_left(phone)
                if rem is not None:
                    partner_left.append(rem)
                if rem == 0:
                    raise PartnerSpent(f"{phone.name} has no special trades remaining today")
                await trade_local.choose(phone, need.give_term, need.name)

            print(f"[special] {partner.name} gives a shiny {need.name} (#{need.dex}); "
                  f"{RECEIVER} gives a {GIVE_STARS} spare legendary from its prepared pool", flush=True)
            try:
                def confirmed(receipts):
                    offered = {a.name: f"{GIVE_STARS} legendary ({gave['cp']})", b.name: need.name}
                    starter_trade.record_trade((a.name, b.name), offered, "shiny-gap", "special",
                                               offered_dex={b.name: need.dex}, receipts=receipts)

                await trade_local.trade_once(
                    a, b, names, {a.name: term, b.name: need.give_term}, {a.name: "", b.name: need.name},
                    max_special_price=args.max_stardust,
                    choosers={a.name: receiver_pick, b.name: partner_pick},
                    allow_max_move_reset={partner.name}, on_confirmed=confirmed, daily_cap=trade_plan.DAILY_TRADES)
            except PartnerSpent as exc:
                report.lines.append(f"{need.name}: {exc}")
                break                  # another partner may still trade
            except ReceiverSpent as exc:
                report.lines.append(f"{need.name}: {exc}")
                if "nothing for" in str(exc):
                    text = (f"{RECEIVER} has no {GIVE_STARS} legendaries left to give: "
                            f"run the exact-IV assessment before the next special trade")
                    print(f"[special] ASSESS: {text}", flush=True)
                    report.lines.append("ASSESS: " + text)
                    notify(text)
                if "no special trades remaining" in str(exc):
                    mark_done_today(f"the game says none remain ({datetime.now():%H:%M})")
                go_on = False
                break
            except trade_local.TradeOver as exc:
                text = str(exc)
                report.lines.append(f"{need.name}: {text}")
                print(f"[special] {need.name}: {text}", flush=True)
                if "trading limit" in text:
                    go_on = False
                    break
                # A greyed-out shiny ("cannot be traded") without a positive
                # "Special Trades remaining" reading usually means this partner
                # has 0 special trades left today; do not wipe its prepared queue.
                if "cannot be traded" in text and not (partner_left and partner_left[-1] > 0):
                    msg = (f"{partner.name}: stopped before wiping prepared queue "
                           f"(special trades remaining was not confirmed > 0)")
                    report.lines.append(msg)
                    print(f"[special] {msg}", flush=True)
                    break
                # The partner's pick: "no X left to offer" or confirmed untradeable tile.
                mark_missing(partner.name, need.dex)
                save_prepared(after_trade_missing(prepared, partner.name, need.dex))
                continue
            except trade_local.LuckyAvailable as exc:
                report.lines.append(f"{need.name} ({partner.name}): skipped Lucky Trade -- {exc}")
                print(f"[special] {partner.name} <-> {RECEIVER}: skipped Lucky Trade -- {exc}", flush=True)
                break
            except trade_local.TradeRefused as exc:
                refusals += 1
                report.lines.append(f"{need.name} ({partner.name}): refused -- {exc}")
                print(f"[special] {need.name} ({partner.name}): refused -- {exc}", flush=True)
                if refusals >= 3:
                    break
                continue
            except (StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
                report.ok = False
                report.lines.append(f"{need.name}: trade failed -- {exc}")
                go_on = False
                break
            refusals = 0
            args.made += 1
            if traded_dexes is not None:
                traded_dexes.add(need.dex)
            log_trade(need, partner.name, gave["cp"], need.name)
            save_prepared(after_trade(prepared, partner.name, need.dex))
            removed = remove_need(args.report, need.dex)
            line = f"traded: {RECEIVER} got a shiny {need.name} (#{need.dex}) from {partner.name} " \
                   f"for its {GIVE_STARS} {gave['cp']} legendary" + ("" if removed else " (row already gone from the report)")
            print(f"[special] {line}", flush=True)
            report.lines.append(line)
            if (recv_left and recv_left[-1] <= 1) or (limit is not None and args.made >= limit):
                mark_done_today(f"{need.name} #{need.dex} from {partner.name} for its {gave['cp']} legendary")
                go_on = False
                break
        for phone in (a, b):
            with contextlib.suppress(Exception):
                await phone.go_home()
    except BaseException as exc:
        if not await pair_cm.__aexit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        await pair_cm.__aexit__(None, None, None)
    return go_on


def queued(prepared: dict, needs: Sequence[Need], here: set[str]) -> dict[str, list[Need]]:
    """Each attached partner's prepared shinies, in order, still wanted by the report."""
    by_dex = {n.dex: n for n in needs}
    out = {}
    for name, dexes in prepared.get("partners", {}).items():
        if name in here and name != RECEIVER:
            wanted = [by_dex[d] for d in dexes if d in by_dex and not missing_recently(name, d)]
            if wanted:
                out[name] = wanted
    return out


async def special_step(args) -> Report:
    from . import trading_accounts
    report = Report("special-trade", True, "")
    if args.devices:
        aliases = trading_accounts.aliases()
        args.devices = list(dict.fromkeys(aliases.get(n, n) for n in args.devices))
    args.made = 0
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    if not args.report.exists():
        report.ok = False
        report.detail = f"no gap report at {args.report}"
        return report
    needs = read_needs(args.report.read_text())

    if args.prepare:
        here, owners, machines, fleet = await _discover_routes(fleet, local_only=getattr(args, "local", False))
        if RECEIVER not in here:
            report.ok = False
            report.detail = f"{RECEIVER} is not attached to any configured computer"
            return report
        by_partner, _elsewhere = plan(needs, set(here))
        if args.devices:
            by_partner = {n: v for n, v in by_partner.items() if n in args.devices}
        print(f"[special] preparing: {RECEIVER}'s {GIVE_STARS} legendaries; "
              + (", ".join(f"{n}: {len(v)} shiny(ies) to look for" for n, v in by_partner.items())
                 or "no partner attached"), flush=True)
        data = await prepare(
            here[RECEIVER],
            {n: here[n] for n in by_partner},
            by_partner,
            args,
            owners=owners,
            machines=machines,
            fleet=fleet,
        )
        report.lines.extend(describe(data, needs))
        report.detail = f"prepared; saved to {STATE / PREPARED}"
        return report

    prepared = load_prepared()
    if args.plan_only:
        report.lines.extend(describe(prepared, needs) if prepared else ["nothing prepared yet"])
        with contextlib.suppress(Exception):
            here, owners, _machines = await discover_fleet(fleet, local_only=getattr(args, "local", False))
            if owners:
                roster = ", ".join(f"{n} [{owners[n]}]" for n in sorted(here))
                report.lines.append(f"attached fleet: {roster}")
        report.detail = "--plan: touched no phone"
        return report
    done = None if getattr(args, "ignore_today", False) else done_today()
    if done:
        report.detail = f"already made today: {done}"
        return report
    # The prepared per-species counts are only a guess (after_trade cannot
    # read which species went) and new raid catches join the pool after
    # --prepare: on 14 Nov the counts said "mewtwo 1", the Mewtwo search found
    # none, and a 2* Zamazenta, Zacian and Kyurem sat untouched.  The picker
    # therefore always searches every giveable species; the ASSESS notice
    # comes only when that whole search finds nothing.
    here, owners, machines, fleet = await _discover_routes(fleet, local_only=getattr(args, "local", False))
    if RECEIVER not in here:
        report.detail = f"skipped: {RECEIVER} is not attached to any configured computer"
        return report
    # Whichever partner is attached across any configured computer trades: one
    # never searched by --prepare is searched now for its own listed shinies only.
    fresh = unprepared(prepared, needs, set(here))
    if args.devices:
        fresh = {n: v for n, v in fresh.items() if n in args.devices}
    if fresh:
        prepared = await prepare_partners(
            {n: here[n] for n in fresh},
            fresh,
            args,
            owners=owners,
            machines=machines,
            fleet=fleet,
        )
    holding = queued(prepared, needs, set(here))
    if args.devices:
        holding = {n: v for n, v in holding.items() if n in args.devices}
    if not holding:
        hint = swap_hint(needs, set(here))
        if hint:
            print(f"[special] SWAP: {hint}", flush=True)
            report.lines.append("swap: " + hint)
            notify(hint)
        report.detail = "nothing prepared to trade with the attached phones"
        return report
    pool = pool_term()
    print("[special] next: " + ", ".join(f"{v[0].name} #{v[0].dex} [{owners.get(n, '?')}] from {n}" for n, v in holding.items())
          + f"; {RECEIVER} gives a {GIVE_STARS} legendary from its whole pool", flush=True)
    traded_dexes: set[int] = set()
    pool_state = {"empty": False}
    unavailable: list[str] = []
    for name, wanted in holding.items():
        if not await trade(
            here[RECEIVER],
            here[name],
            wanted,
            pool,
            args,
            report,
            prepared,
            owners=owners,
            machines=machines,
            fleet=fleet,
            traded_dexes=traded_dexes,
            pool_state=pool_state,
            unavailable=unavailable,
        ):
            break
    if args.made == 0 and unavailable and not done_today():
        report.ok = False
        report.detail = f"0 special trade(s) made (unavailable: {', '.join(unavailable)})"
    else:
        report.detail = f"{args.made} special trade(s) made"
    return report


def describe(prepared: dict, needs: Sequence[Need]) -> list[str]:
    names = {n.dex: n.name for n in needs}
    lines = [f"prepared {prepared.get('prepared', '?')}; {RECEIVER} {GIVE_STARS} pool: "
             + (", ".join(f"{sp} {n}" for sp, n in prepared.get("give", {}).items() if n) or "EMPTY")]
    for name, dexes in prepared.get("partners", {}).items():
        lines.append(f"{name} next: " + (", ".join(f"{names.get(d, '?')} #{d}" for d in dexes) or "none"))
    return lines


def configure(path: Path | None = None) -> None:
    """Load the private settings into RECEIVER, REPORT and HOLDERS."""
    global RECEIVER, REPORT, HOLDERS, LEGENDARIES, SPARE_TAG
    path = path or config_paths.default_config(SETTINGS)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError(f"no special-trade settings at {path}") from None
    from . import trading_accounts
    mapping = trading_accounts.aliases()
    receiver = str(raw["receiver"])
    RECEIVER = mapping.get(receiver, receiver)
    REPORT = Path(config_paths.expand_user_path(str(raw["report"])))
    HOLDERS = {str(k): mapping.get(str(v), str(v)) for k, v in raw.get("holders", {}).items()}
    LEGENDARIES = tuple(raw.get("give", LEGENDARIES))
    SPARE_TAG = str(raw.get("spare_tag", SPARE_TAG))


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", nargs="+", help="only trade with these partner phones")
    parser.add_argument("--local", action="store_true", help="only use devices attached to this computer")
    parser.add_argument("--count", type=int, default=1,
                        help="most special trades this run (default: 1, the game's daily allowance)")
    parser.add_argument("--all", dest="all_today", action="store_true",
                        help="make all remaining special trades allowed today until 0 remain")
    parser.add_argument("--ignore-today", action="store_true",
                        help="ignore today.json and check live special trades remaining on the receiver")
    parser.add_argument("--max-stardust", type=int, default=MAX_PRICE,
                        help=f"refuse a trade dearer than this (default: {MAX_PRICE})")
    parser.add_argument("--report", type=Path, default=None, help="the shiny gap report (default: from the settings)")
    parser.add_argument("--plan", dest="plan_only", action="store_true",
                        help="print the prepared list; touch no phone")
    parser.add_argument("--prepare", action="store_true",
                        help="search the receiver's 2-star legendaries and each attached partner's shinies, "
                             "and save the ordered list the trades pick from")
    parser.add_argument("--list", dest="list_only", action="store_true",
                        help="print the report's tradeable species and touch no phone")
    parser.add_argument("--shots", type=Path, default=None, help="where to keep screenshots")
    args = parser.parse_args(arguments)
    configure()
    args.shots = (args.shots or config_paths.state_dir() / "special-trade" / "shots").expanduser()
    args.report = (args.report or REPORT).expanduser()
    return args


async def run(args: argparse.Namespace) -> int:
    if args.list_only:
        for need in read_needs(args.report.read_text()):
            print(f"#{need.dex:04d} {need.name:<12} on {', '.join(need.holders)}  ({need.detail})")
        return 0
    report = await special_step(args)
    for line in report.lines:
        print(f"[special]   {line}", flush=True)
    print(f"[special] {'done' if report.ok else 'PROBLEM'}: {report.detail}", flush=True)
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    from special_trade import main as public_main
    raise SystemExit(public_main())
