"""Collect the receiver's audited Lucky Dex gaps, then trade valuable Pokémon across the fleet.

Use --targets-only, --rolycoly-only, or --size-only for the configured collector targets.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from PIL import Image

from . import (
    clear_pokemon,
    config_paths,
    fleet_entrypoint,
    gbl_home_recovery as home,
    gbl_ios,
    pokemon_fleet,
    remote_target,
    starter,
    starter_trade,
    trade_local,
    trade_saved_search,
    trade_plan,
)

OCRBox = clear_pokemon.OCRBox
CONFIG_NAME = "collect-trades.json"
DEFAULT_MAX_SKIPS = 6
ORDINARY_RESERVE = 2

# Intentionally omits `!#` on donors so non-98Plus tagged tradeables (e.g. ToTrade)
# are still eligible, while keeping 98Plus and every protected/special category home.
REQUIRED_DONOR_FILTERS: tuple[str, ...] = (
    "!98Plus",
    "!dontrade",
    "!year2016-2021",
    "!#KeepOne",
    "!favorite",
    "!shiny",
    "!4*",
    "!lucky",
    "!legendary",
    "!mythical",
    "!ultra beasts",
    "!shadow",
    "!purified",
    "!traded",
    "!background",
    "!dynamax",
    "!gigantamax",
)

# Return fodder from receiver must never consume non-safe, dontrade, 98Plus, XS/XL/XXS/XXL, tagged,
# vintage, shiny, hundo, lucky, already-traded, Rolycoly-line, or Glimmet/Glimmora Pokémon.
# Deliberately omits `!mega1-` (whose game filter drops non-Mega species down to Mega-capable ones);
# `!#` already protects tagged `mega` attackers and `trade_local.trade_warning` catches Mega-level reset prompts.
# Putting `!#` last keeps readable flags (`!98Plus&!dontrade&!xs&!xl&!xxs&!xxl...`) at the left edge for OCR.
REQUIRED_RETURN_FILTERS: tuple[str, ...] = (
    "!98Plus",
    "!dontrade",
    "!xs",
    "!xl",
    "!xxs",
    "!xxl",
    "!buddy1-5",
    "!favorite",
    "!shiny",
    "!4*",
    "!lucky",
    "!legendary",
    "!mythical",
    "!ultra beasts",
    "!shadow",
    "!purified",
    "!traded",
    "!costume",
    "!dynamax",
    "!gigantamax",
    "!background",
    "!year2016-2021",
    "!837-839",
    "!969-970",
    "!#",
)

DEFAULT_ROLYCOLY_DEX: tuple[int, ...] = (837, 838, 839)
DEFAULT_EXCLUDE_RETURN_DEX: tuple[int, ...] = (837, 838, 839, 969, 970)
CURATED_RETURN_TIER1_DEX: str = (
    "1-9,25-26,56-57,63-71,81-82,88-89,92-95,98-99,111-115,120-121,123,125,127,"
    "129-131,133-137,142-143,147-149,152-160,172,175-176,179-181,196-197,208-212,"
    "214-217,220-221,227-229,233,239,242,246-248,252-260,280-282,303-306,309-310,"
    "315,318-319,333-334,349-350,353-354,359,361-362,371-376,387-398,403-409,"
    "427-428,440,443-448,453-454,459-462,464-466,468,470-471,473-475,478,495-503,"
    "524-526,529-530,532-534,543-545,554-555,588-589,607-612,624-625,633-637,"
    "650-658,679-681,696-700,704-706,722-730,736-738,759-760,767-768,782-784,"
    "810-818,840-842,848-849,859-861,884-887,901,903-904,906-914,935-937,957-959,"
    "974-975,979,983,996-1000,1011,1019"
)
CURATED_RETURN_TIER2_DEX: str = (
    "43-45,74-76,79-80,102-103,182-184,194-195,198-200,207,287-289,296-298,302,"
    "307-308,322-323,341-342,363-365,410-411,429-430,472,551-553,559-560,566-567,"
    "592-593,597-598,618,622-623,629-630,661-663,669-671,703,821-823,856-858,877,"
    "900,965-966,980"
)
DEFAULT_CURATED_RETURN_TIERS: tuple[str, ...] = (
    CURATED_RETURN_TIER1_DEX,
    CURATED_RETURN_TIER2_DEX,
)
DEFAULT_MAX_RETURN_DEX_CHARS = 220
DEFAULT_PRIMARY_RETURN_QUERY = "age0-30"
DEFAULT_FALLBACK_RETURN_QUERY = "!year2016-2021"
DEFAULT_TARGETS: tuple[dict[str, str], ...] = (
    {"name": "rolycoly", "query": "rolycoly"},
    {"name": "xxs-xxl", "query": "xxs,xxl"},
)


@dataclass(frozen=True)
class TargetSpec:
    name: str
    query: str
    term: str


@dataclass(frozen=True)
class CollectionBatch:
    donor: str
    receiver: str
    target: str
    term: str
    count: int


def load_config(path: Path | None = None) -> dict:
    target = path or config_paths.default_config(CONFIG_NAME)
    if not target.is_file():
        return {}
    loaded = json.loads(target.read_text())
    if not isinstance(loaded, dict):
        raise pokemon_fleet.FleetError(f"{target} must contain a JSON object")
    return loaded


def build_donor_query(target_query: str, filters: Sequence[str] = REQUIRED_DONOR_FILTERS) -> str:
    """Place safety exclusions before `target_query` for reliable prefix/suffix OCR checks."""
    donor = [part for part in filters if part.strip().casefold() != "!costume"]
    merged, _ = clear_pokemon.merge_safety_filters(donor, REQUIRED_DONOR_FILTERS)
    clean_target = target_query.strip().lstrip("&")
    prefix = "&".join(merged)
    return f"{prefix}&{clean_target}" if clean_target else prefix


def _split_dex_ranges(dex_numbers: Sequence[int], max_chars: int = DEFAULT_MAX_RETURN_DEX_CHARS) -> list[str]:
    """Pack sorted Pokédex numbers into compressed comma/dash chunks bounded by `max_chars`."""
    if not dex_numbers:
        return []
    tokens = clear_pokemon.format_dex_ranges(sorted(dex_numbers)).split(",")
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for tok in tokens:
        added = len(tok) + (1 if cur else 0)
        if cur and cur_len + added > max_chars:
            chunks.append(",".join(cur))
            cur = [tok]
            cur_len = len(tok)
        else:
            cur.append(tok)
            cur_len += added
    if cur:
        chunks.append(",".join(cur))
    return chunks


def build_return_queries(
    clear_config_path: Path | None = None,
    *,
    exclude_dex: Iterable[int] = DEFAULT_EXCLUDE_RETURN_DEX,
    return_filters: Sequence[str] = REQUIRED_RETURN_FILTERS,
    return_dex_tiers: Sequence[str] | None = None,
    custom_return_query: str | None = None,
    fallback_return_query: str = DEFAULT_FALLBACK_RETURN_QUERY,
    max_dex_chars: int = DEFAULT_MAX_RETURN_DEX_CHARS,
) -> list[str]:
    """Build fast chunked curated useful-species return filters (evolution-complete lines)."""
    del clear_config_path, fallback_return_query  # kept for signature compatibility; !year2016-2021 is in REQUIRED_RETURN_FILTERS
    cleaned_input = [tok for tok in return_filters if tok.strip().lower() != "!mega1-"]
    merged, _ = clear_pokemon.merge_safety_filters(cleaned_input, REQUIRED_RETURN_FILTERS)
    merged = [tok for tok in merged if tok.strip().lower() != "!mega1-"]
    if "!#" in merged:
        merged = [tok for tok in merged if tok != "!#"] + ["!#"]
    prefix = "&".join(merged)
    if custom_return_query:
        clean = custom_return_query.strip().lstrip("&")
        return [f"{prefix}&{clean}" if clean else prefix]

    excluded = set(DEFAULT_EXCLUDE_RETURN_DEX) | {int(x) for x in exclude_dex}
    tiers = tuple(return_dex_tiers) if return_dex_tiers else DEFAULT_CURATED_RETURN_TIERS
    seen_dex: set[int] = set()
    queries: list[str] = []
    for raw_expr in tiers:
        tier_dex = clear_pokemon.parse_dex_ranges(str(raw_expr)) - excluded - seen_dex
        if not tier_dex:
            continue
        seen_dex |= tier_dex
        for chunk in _split_dex_ranges(sorted(tier_dex), max_chars=max_dex_chars):
            queries.append(f"{prefix}&{chunk}")
    if not queries:
        raise pokemon_fleet.FleetError("No curated return dex species remain after exclusions")
    return queries


def build_return_slices(
    clear_config_path: Path | None = None,
    *,
    exclude_dex: Iterable[int] = DEFAULT_EXCLUDE_RETURN_DEX,
    return_filters: Sequence[str] = REQUIRED_RETURN_FILTERS,
    return_dex_tiers: Sequence[str] | None = None,
    custom_return_query: str | None = None,
    fallback_return_query: str = DEFAULT_FALLBACK_RETURN_QUERY,
    max_dex_chars: int = DEFAULT_MAX_RETURN_DEX_CHARS,
) -> list[str]:
    """Backwards-compatible alias returning chunked curated receiver return filters."""
    return build_return_queries(
        clear_config_path,
        exclude_dex=exclude_dex,
        return_filters=return_filters,
        return_dex_tiers=return_dex_tiers,
        custom_return_query=custom_return_query,
        fallback_return_query=fallback_return_query,
        max_dex_chars=max_dex_chars,
    )


def resolve_targets(
    cfg: Mapping,
    *,
    rolycoly_only: bool = False,
    size_only: bool = False,
) -> list[TargetSpec]:
    donor_filters = [str(x) for x in cfg.get("donor_filters", REQUIRED_DONOR_FILTERS)]
    raw_targets = cfg.get("targets") or list(DEFAULT_TARGETS)
    out: list[TargetSpec] = []
    for item in raw_targets:
        name = str(item["name"]).strip()
        query = str(item["query"]).strip()
        if rolycoly_only and name != "rolycoly":
            continue
        if size_only and name != "xxs-xxl":
            continue
        out.append(TargetSpec(name=name, query=query, term=build_donor_query(query, donor_filters)))
    if not out:
        raise pokemon_fleet.FleetError("No collection trade targets matched configuration/flags")
    return out


def plan_collection(
    counts: Mapping[str, Mapping[str, int]],
    donors: Sequence[str],
    targets: Sequence[TargetSpec],
    receiver: str,
    budget: int,
) -> list[CollectionBatch]:
    """Allocate `budget` by target priority first (Rolycoly across all donors, then XXS/XXL), grouped by donor."""
    remaining = max(0, int(budget))
    alloc: dict[tuple[str, str], int] = {}
    for target in targets:
        if remaining <= 0:
            break
        for donor in donors:
            avail = max(0, int(counts.get(donor, {}).get(target.name, 0)))
            take = min(avail, remaining)
            if take > 0:
                alloc[(donor, target.name)] = take
                remaining -= take
            if remaining <= 0:
                break

    batches: list[CollectionBatch] = []
    for donor in donors:
        for target in targets:
            take = alloc.get((donor, target.name), 0)
            if take > 0:
                batches.append(
                    CollectionBatch(
                        donor=donor,
                        receiver=receiver,
                        target=target.name,
                        term=target.term,
                        count=take,
                    )
                )
    return batches


async def count_queries_on_phone(
    phone: starter.Phone,
    queries: Sequence[tuple[str, str]],
    *,
    stop_after_total: int | None = None,
    log_prefix: str = "scan",
    log_top_species: bool = False,
) -> dict[str, int]:
    """Count result matches for each `(label, term)` using verified storage searches."""
    out: dict[str, int] = {}
    image, boxes = await starter.open_storage(phone)
    total = 0
    logged_top = False
    for label, term in queries:
        if stop_after_total is not None and total >= stop_after_total:
            break
        count, image, boxes, elapsed = await clear_pokemon._apply_and_verify_query(phone, term, image, boxes)
        out[label] = count
        total += count
        phone.say(f"{log_prefix} {label}: {count} match(es) ({elapsed:.1f}s)")
        if log_top_species and count > 0 and not logged_top:
            tiles = clear_pokemon.safe_grid_tiles(image, boxes)
            species = [sp for sp in (_tile_species_label(image, boxes, t) for t in tiles[:6]) if sp]
            if species:
                phone.say(f"{log_prefix} {label} top species: {', '.join(species)}")
                logged_top = True
    await clear_pokemon._clear_storage_pill_if_active(phone, image, boxes)
    await phone.go_home()
    return out


async def _scan_donor_phone(
    name: str,
    machine: str,
    machines: dict,
    fleet: pokemon_fleet.FleetConfig,
    targets: Sequence[TargetSpec],
    shots: Path,
    dry_run: bool,
) -> dict[str, int]:
    queries = [(t.name, t.term) for t in targets]
    async with _open_optional_donor(
        name, shots, machine=machine, machines=machines, fleet=fleet, dry_run=dry_run
    ) as phone:
        if phone is None:
            return {t.name: 0 for t in targets}
        return await count_queries_on_phone(phone, queries, log_prefix="scan")


async def _scan_receiver_fodder(
    receiver: str,
    machine: str,
    machines: dict,
    fleet: pokemon_fleet.FleetConfig,
    return_queries: Sequence[str],
    budget: int,
    shots: Path,
    dry_run: bool,
) -> dict[str, int]:
    queries = [(f"return-{idx + 1}", term) for idx, term in enumerate(return_queries)]
    async with remote_target.open_fleet_phone(
        receiver, shots, machine=machine, machines=machines, fleet=fleet, dry_run=dry_run
    ) as phone:
        return await count_queries_on_phone(
            phone,
            queries,
            stop_after_total=budget,
            log_prefix="scan fodder",
            log_top_species=True,
        )


def _active_picker_filter_box(image: Image.Image, boxes: Sequence[OCRBox]) -> OCRBox | None:
    """Return the active filter pill text box inside the trade picker."""
    return next(
        (
            b for b in boxes
            if image.height * 0.12 <= b.center_y < image.height * 0.34
            and "/" not in b.text
            and "special trades" not in b.text.lower()
            and "evolutionary" not in b.text.lower()
            and not re.fullmatch(r"cp\s*\d+", trade_local.norm(b.text))
            and (
                any(ch in b.text for ch in "!&,")
                or re.fullmatch(r"\d[\d,\-]*[,\-][\d,\-]+", clear_pokemon._compact_search(b.text))
            )
        ),
        None,
    )


def _picker_search_field(image: Image.Image, boxes: Sequence[OCRBox]) -> OCRBox | None:
    """Locate the trade picker search bar robustly."""
    active = _active_picker_filter_box(image, boxes)
    if active is not None:
        return active
    ocr_search = next(
        (
            b for b in boxes
            if image.height * 0.12 <= b.center_y < image.height * 0.34
            and trade_local.norm(b.text).endswith("search")
        ),
        None,
    )
    if ocr_search is not None:
        return ocr_search
    return trade_local.picker_field(image, list(boxes)) or trade_local._storage_field_fallback(image, list(boxes))


async def _raise_if_expired(
    phone: starter.Phone,
    image: Image.Image,
    boxes: Sequence[OCRBox],
) -> None:
    """Dismiss and raise StepFailed (never TradeOver) when a Trade Expired modal is up."""
    if not trade_local.expired(list(boxes)):
        return
    ok_btn = trade_local.find_box(boxes, "ok", exact=True)
    if ok_btn is not None:
        await phone.tap_box(ok_btn, image)
    raise starter.StepFailed("the trade expired")


async def _enter_picker_query(
    phone: starter.Phone,
    term: str,
    image: Image.Image,
    boxes: Sequence[OCRBox],
) -> bool:
    """Apply the requested filter, returning whether a verified Recent entry supplied it."""
    await _raise_if_expired(phone, image, boxes)
    return await _use_query(phone, term, image, boxes)


async def _type_focused_query(phone: starter.Phone, term: str) -> None:
    if phone.ios:
        if not await phone.ios_keyboard():
            raise starter.StepFailed(f"{phone.name}: the search keyboard did not open")
        await trade_local._clear_ios_native_text(phone)
        dev = phone.target.device
        try:
            if hasattr(dev, "wda_keys"):
                await dev.wda_keys(term, clear_pokemon.IOS_WDA_KEYS_FREQUENCY)
            else:
                wda_port = int(
                    dev.config.ios_device.get("wda_local_port") or clear_pokemon.ios_wda_cleanup.WDA_DEFAULT_PORT
                )
                await asyncio.to_thread(clear_pokemon._post_wda_keys, wda_port, term, clear_pokemon.IOS_WDA_KEYS_FREQUENCY)
        except Exception as exc:
            raise trade_local.TradeRefused(f"{phone.name}: iOS WDA fast typing failed: {exc}") from exc
        await asyncio.sleep(0.8)
        image, boxes = await phone.read()
        if not _focused_suffix(boxes, term, image.height):
            raise starter.StepFailed(f"{phone.name}: iOS editor did not show the requested query ending ({phone.save(image, 'query-ending')})")
        await clear_pokemon._ios_tap_done(phone)
        return

    existing = await clear_pokemon._android_edit_text(phone)
    if existing is not None and existing.casefold() == term.casefold():
        # Identical clipped pills can contain different dex lists. Reuse only
        # after reading the entire Android EditText. On some devices Enter
        # dismisses the keyboard but leaves the drawer, so prefer native OK.
        image, boxes = await phone.read()
        done = next((box for box in boxes if trade_local.norm(box.text) == "ok"
                     and box.center_x > .72 * image.width and box.center_y > .48 * image.height), None)
        if done is not None:
            await phone.tap_box(done, image)
        else:
            await trade_local.submit(phone)
        ok, image, boxes = await trade_local.settle(
            phone, lambda i,b: trade_local.expired(b) or _filtered_grid(i,b,term), 3.5)
        await _raise_if_expired(phone, image, boxes)
        if ok:
            phone.say("reused exact native search without typing")
            return
        # Resume normal verified input only when the unchanged text failed to
        # reopen the grid. The drawer may now have lost its native edit focus.
        existing = await clear_pokemon._android_edit_text(phone)
        if existing is None:
            field = _picker_search_field(image, boxes)
            if field is None or not (trade_saved_search.heading(boxes,"see more")
                                     or trade_saved_search.heading(boxes,"see less")):
                raise starter.StepFailed(f"{phone.name}: couldn't apply the retained search")
            await phone.tap_box(field, image)
            await asyncio.sleep(.6)
            existing = await clear_pokemon._android_edit_text(phone)
    if existing is None or (existing.strip() and existing.strip().casefold() != "search"):
        await trade_local.clear_field(phone, max(len(existing or "") + 20, 150))
        await asyncio.sleep(0.4)
        remaining = await clear_pokemon._android_edit_text(phone)
        if remaining and remaining.strip().casefold() != "search":
            await trade_local.clear_field(phone, max(len(remaining) + 40, 150))

    expected = term.strip().casefold()
    for attempt in range(2):
        for pos in range(0, len(term), clear_pokemon.ANDROID_INPUT_BURST):
            burst = term[pos:pos + clear_pokemon.ANDROID_INPUT_BURST]
            escaped = burst.replace(" ", "%s").replace("'", "")
            await asyncio.to_thread(phone.target._adb, "shell", f"input text '{escaped}'")
            if pos + clear_pokemon.ANDROID_INPUT_BURST < len(term):
                await asyncio.sleep(0.40)

        deadline = time.monotonic() + 6.0
        typed: str | None = ""
        prev: str | None = None
        stable = 0
        while True:
            typed = await clear_pokemon._android_edit_text(phone)
            if typed is not None and typed.strip().casefold() == expected:
                break
            if typed == prev:
                stable += 1
            else:
                stable = 0
                prev = typed
            if stable >= 2 or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.6)

        if typed is not None and typed.strip().casefold() == expected:
            break
        phone.say(
            f"picker search input mismatch (got {len(typed or '')}/{len(term)} chars); retrying ({attempt + 1}/2)"
        )
        await trade_local.clear_field(phone, max(len(typed or "") + 20, 120))
    else:
        raise trade_local.TradeRefused(
            f"{phone.name}: Android picker search box did not retain full {len(term)}-char query"
        )

    await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 66")
    await asyncio.sleep(2.0)


def _focused_suffix(boxes, term, height):
    # The active editor shows its right edge. Include the complete final species
    # token (and preceding separators for short tokens) to disambiguate chunks.
    target = clear_pokemon._compact_search(term)
    min_chars = min(len(target), 14)
    return any(
        len(part) >= min_chars and part.endswith(target[-min_chars:])
        for box in boxes if box.center_y >= height*.12
        for text in [box.text, box.text.rstrip("|")]
        for part in [clear_pokemon._compact_search(text)]
    )


def _exact_visible_query(image, boxes, term):
    # Preserve all operators, including *, # and !. A shared prefix does not
    # identify a specific species list when opening an already filtered picker.
    return any(
        box.text.strip().casefold() == term.casefold()
        for box in boxes if .12*image.height <= box.center_y <= .34*image.height
    )


async def _editor_matches(phone, term, image, boxes):
    native = await trade_saved_search.editor_text(phone)
    return native is not None and native.casefold() == term.casefold()


_recent_fallback = {}


async def _use_query(phone, term, image, boxes):
    """Reuse a verified Recent entry when possible, otherwise type the full query."""
    field = _picker_search_field(image, boxes)
    if field is None:
        raise starter.StepFailed(f"{phone.name}: could not locate the search field")
    await phone.tap_box(field, image)
    await asyncio.sleep(.8)
    drawing, labels = await phone.read()
    await _raise_if_expired(phone, drawing, labels)
    drawer_headings = ("see more", "see less", "recommended", "favorite", "recent")
    if not any(trade_saved_search.heading(labels, heading) for heading in drawer_headings):
        # The SE can finish opening storage after the first tap. Retry only
        # on a still-visible grid, using the field from the current frame.
        if starter.storage_open(labels) or trade_local.picker(drawing, labels):
            fresh = _picker_search_field(drawing, labels)
            if fresh is not None:
                await phone.tap_box(fresh, drawing)
                _, drawing, labels = await trade_local.settle(phone, lambda i, b: (
                    trade_local.expired(b) or any(trade_saved_search.heading(b, h) for h in drawer_headings)), 3)
                await _raise_if_expired(phone, drawing, labels)
    if not any(trade_saved_search.heading(labels, heading) for heading in drawer_headings):
        raise starter.StepFailed(f"{phone.name}: search drawer did not open (saved {phone.save(drawing, 'no-search-drawer')})")
    if _recent_fallback.get(phone.name) == term:
        # Recent can change between inventory preparation and the timed trade.
        # Check the entry's filter rather than trusting its position or count.
        image_for_recent, labels_for_recent = drawing, labels
        if trade_saved_search.heading(labels, "see more"):
            image_for_recent, labels_for_recent = await trade_saved_search._toggle(
                phone, drawing, labels, "see more", "recent")
        recent = trade_saved_search.icons(image_for_recent, labels_for_recent, "recent")
        if recent:
            count = await asyncio.to_thread(trade_saved_search.recent_tile_count, image_for_recent,
                                            labels_for_recent, recent[0])
            if count is not None:
                verified = await trade_saved_search._verified_recent(
                    phone, term, count, image_for_recent, labels_for_recent, _editor_matches)
                if verified is not None:
                    point, menu_image, _, _ = verified
                    await phone.tap(point, menu_image)
                    phone.say("reused verified Recent trade search")
                    return True
        _recent_fallback.pop(phone.name, None)
        drawing, labels = await phone.read()
        await _raise_if_expired(phone, drawing, labels)
        field = _picker_search_field(drawing, labels) or field
        await phone.tap_box(field, drawing)
        await asyncio.sleep(.6)
    await _type_focused_query(phone, term)
    return False


def _filtered_grid(image, boxes, term):
    return (not trade_saved_search.heading(boxes, "see more")
            and not trade_saved_search.heading(boxes, "see less")
            and clear_pokemon.search_field_matches_query(boxes, term, image.height)
            and clear_pokemon.result_count(boxes, image.height) is not None)


async def _wait_query_grid(phone, term):
    ok, image, boxes = await trade_local.settle(
        phone, lambda i, b: trade_local.expired(b) or _filtered_grid(i, b, term), 12)
    await _raise_if_expired(phone, image, boxes)
    if not ok:
        raise starter.StepFailed(f"{phone.name}: filtered grid did not open (saved {phone.save(image, 'picker-unverified')})")
    return image, boxes


async def prepare_saved_query(phone, term, image=None, boxes=None, *, observed=None, min_count=0, max_count=None):
    """Verify an offer outside the timed trade and remember it for Recent lookup."""
    if image is None:
        await starter.open_storage(phone)
        await phone.still()
        image, boxes = await phone.read()
    used = await _use_query(phone, term, image, boxes)
    image, boxes = await _wait_query_grid(phone, term)
    count = clear_pokemon.result_count(boxes, image.height)
    if count == 0:
        # Inventory initially renders a transient zero while evaluating the filter.
        ok, settled, results = await trade_local.settle(
            phone, lambda i,b: trade_local.expired(b) or (clear_pokemon.result_count(b, i.height) or 0) > 0,
            timeout=3.5)
        await _raise_if_expired(phone, settled, results)
        if ok:
            image, boxes = settled, results
            count = clear_pokemon.result_count(boxes, image.height)
    if observed is not None:
        observed.update(count=count, labels=[_tile_species_label(image, boxes, tile)
                                             for tile in clear_pokemon.safe_grid_tiles(image, boxes)])
    if count is not None and count >= min_count and (max_count is None or count <= max_count):
        _recent_fallback[phone.name] = term
    else:
        _recent_fallback.pop(phone.name, None)
    return used


def _tile_species_label(image: Image.Image, boxes: Sequence[OCRBox], tile: OCRBox) -> str:
    """Normalized species label printed directly beneath one grid tile."""
    labels = [
        b for b in boxes
        if abs(b.center_x - tile.center_x) < image.width * 0.14
        and image.height * 0.03 < (b.center_y - tile.center_y) < image.height * 0.14
        and not re.fullmatch(r"cp\s*\d+", trade_local.norm(b.text))
    ]
    if not labels:
        return ""
    raw = min(labels, key=lambda b: b.center_y).text.strip().lstrip("•· ").strip()
    return clear_pokemon.normalize_species_token(raw)


class ReserveHeld(trade_local.TradeOver):
    def __init__(self, count, label):
        self.count = count
        super().__init__(f"keeping the remaining {count} copies of {label}")


async def choose_filtered_tile(
    phone: starter.Phone,
    term: str,
    label: str,
    *,
    skip_index: int = 0,
    keep_last_seen: set[str] | None = None,
    keep_copies: int = 0,
    picked_species: list[str] | None = None,
    inspect_labels: list[str] | None = None,
    preapplied: bool = False,
) -> int:
    """Apply `term` inside the trade picker, confirm count > 0, and open tile `skip_index`.

    `preapplied` means the caller already opened `term` as a verified pinned
    favorite (trade_saved_search.open_named_picker): a long query never fits
    the visible field, so it must not be retyped."""
    image, boxes = await phone.read()
    await _raise_if_expired(phone, image, boxes)
    cur_count = clear_pokemon.result_count(boxes, image.height)
    if not preapplied and not (_exact_visible_query(image, boxes, term) and cur_count is not None and cur_count > 0):
        await _enter_picker_query(phone, term, image, boxes)
        image, boxes = await _wait_query_grid(phone, term)

    count = clear_pokemon.result_count(boxes, image.height)
    if count == 0:
        _, image, boxes = await trade_local.settle(
            phone,
            lambda i, b: trade_local.expired(b) or (clear_pokemon.result_count(b, i.height) or 0) > 0,
            timeout=3.5,
        )
        await _raise_if_expired(phone, image, boxes)
        count = clear_pokemon.result_count(boxes, image.height)
    if count is None:
        raise starter.StepFailed(
            f"{phone.name}: unreadable picker count for {label} (saved {phone.save(image, 'picker-count-unverified')})"
        )
    if count <= 0:
        await _raise_if_expired(phone, image, boxes)
        raise trade_local.TradeOver(f"no {label} left to offer")

    if not clear_pokemon.safe_grid_tiles(image, boxes):
        _, image, boxes = await trade_local.settle(
            phone,
            lambda i, b: trade_local.expired(b) or bool(clear_pokemon.safe_grid_tiles(i, b)),
            timeout=6.0,
        )
        await _raise_if_expired(phone, image, boxes)

    if inspect_labels is not None:
        inspect_labels.extend(_tile_species_label(image, boxes, t) for t in clear_pokemon.safe_grid_tiles(image, boxes))
        return int(count)
    if keep_copies:
        if not re.fullmatch(r"[1-9]\d*", term.rsplit("&", 1)[-1]):
            raise starter.StepFailed(f"{phone.name}: copy reserve requires a single Pokédex number")
        if count <= keep_copies:
            raise ReserveHeld(count, label)
    cand_cursor = skip_index
    attempts_tried = 0
    while attempts_tried < 3:
        if attempts_tried:
            image, boxes = await phone.read()
            await _raise_if_expired(phone, image, boxes)
        tiles = clear_pokemon.safe_grid_tiles(image, boxes)
        if cand_cursor >= len(tiles):
            await _raise_if_expired(phone, image, boxes)
            raise trade_local.TradeOver(f"no selectable {label} tiles left (skip={skip_index})")
        tile_species = (
            [_tile_species_label(image, boxes, t) for t in tiles]
            if keep_last_seen is not None or picked_species is not None
            else [""] * len(tiles)
        )
        visible_counts: dict[str, int] = {}
        if keep_last_seen is not None:
            for sp in tile_species:
                if sp:
                    visible_counts[sp] = visible_counts.get(sp, 0) + 1
        while cand_cursor < len(tiles):
            sp = tile_species[cand_cursor]
            if keep_last_seen is not None and sp and sp in keep_last_seen and visible_counts.get(sp, 0) <= 1:
                cand_cursor += 1
                continue
            break
        if cand_cursor >= len(tiles):
            await _raise_if_expired(phone, image, boxes)
            raise trade_local.TradeOver(f"no selectable {label} tiles left (skip={skip_index})")
        tile = tiles[cand_cursor]
        chosen_sp = tile_species[cand_cursor]
        attempts_tried += 1
        cand_cursor += 1
        await phone.tap([tile.center_x, tile.center_y + int(image.height * clear_pokemon.TILE_RISE)], image)
        ok, image, boxes = await trade_local.settle(
            phone,
            lambda i, b: trade_local.detail(i, b) or trade_local.expired(b),
            8.0,
        )
        await _raise_if_expired(phone, image, boxes)
        if ok:
            if picked_species is not None and chosen_sp:
                picked_species.append(chosen_sp)
            return int(count)
    await _raise_if_expired(phone, image, boxes)
    raise trade_local.TradeOver(f"{label} tiles could not be opened (saved {phone.save(image, 'untradeable')})")


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--local", action="store_true", help="use phones attached to this computer only")
    parser.add_argument("--finish-night", action="store_true",
                        help="after daily trading ends, leave the audited storage list open for manual review and sleep the displays")
    parser.add_argument("--receiver", default=None, help="target receiver fleet device (default from collect-trades.json)")
    parser.add_argument("--from", dest="donors", nargs="+", help="only pull from these donor fleet device names")
    parser.add_argument("--exclude-devices", nargs="+", default=[], help="donor fleet device names to skip")
    parser.add_argument("--config-file", type=Path, default=None, help=f"path to {CONFIG_NAME}")
    parser.add_argument("--return-query", default=None, help="override receiver return-fodder query")
    parser.add_argument("--trades", type=int, default=trade_plan.DAILY_TRADES,
                        help=f"daily trade cap per phone (default: {trade_plan.DAILY_TRADES})")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--collect-all-only", action="store_true",
                       help="only send the configured collect_all_dex species to the receiver, including the last eligible copy")
    scope.add_argument("--rolycoly-only", action="store_true", help="only collect Rolycoly")
    scope.add_argument("--targets-only", action="store_true", help="collect configured targets into the receiver only")
    scope.add_argument("--size-only", action="store_true", help="only collect non-98Plus XXS/XXL")
    scope.add_argument("--propose-guaranteed", nargs="*", metavar="DEVICE", default=None,
                       help="quote and park one legendary swap for this pair (or all configured proposal pairs)")
    scope.add_argument("--approve-guaranteed", metavar="ID", help="approve only the named guaranteed offer")
    scope.add_argument("--decline-guaranteed", metavar="ID", help="decline the named guaranteed offer")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="discover phones across hosts and print plan; touch no phone")
    mode.add_argument("--dry-run", "--trade-plan", dest="dry_run", action="store_true",
                      help="scan live phones across all hosts and print trade allocation; make no trades")
    parser.add_argument("--ignore-history", action="store_true", help="ignore today's recorded trade history")
    parser.add_argument("--shots", type=Path, default=None, help="directory for diagnostic screenshots")
    args = parser.parse_args(arguments)
    if any(token is not None and not token.strip() for token in (args.approve_guaranteed, args.decline_guaranteed)):
        parser.error("a specific nonempty proposal ID is required")
    if args.propose_guaranteed is not None and len(args.propose_guaranteed) not in (0, 2):
        parser.error("--propose-guaranteed takes two device names, or none for configured pairs")
    guaranteed_action = args.propose_guaranteed is not None or args.approve_guaranteed or args.decline_guaranteed
    if args.finish_night and (guaranteed_action or args.dry_run or args.size_only or args.targets_only
                              or args.rolycoly_only or args.return_query):
        parser.error("--finish-night is for ordinary daily trading or its read-only --plan")
    if guaranteed_action and (args.donors or args.receiver or args.exclude_devices or args.ignore_history or args.return_query):
        parser.error("select guaranteed pairs with --propose-guaranteed or by proposal ID")
    if guaranteed_action and (args.dry_run or (args.plan and args.propose_guaranteed is None)):
        parser.error("use --propose-guaranteed --plan for ranked inventory without opening a trade")
    if args.trades < 1 or args.trades > trade_plan.DAILY_TRADES:
        parser.error(f"--trades must be between 1 and {trade_plan.DAILY_TRADES}")
    args.shots = (args.shots or config_paths.state_dir() / "collect-trades" / "shots").expanduser()
    return args


def _resolve_roster(
    args: argparse.Namespace,
    cfg: Mapping,
    fleet: pokemon_fleet.FleetConfig,
    owners: Mapping[str, str],
) -> tuple[str, list[str]]:
    receiver = args.receiver or str(cfg.get("receiver", "")).strip()
    if not receiver:
        raise pokemon_fleet.FleetError(
            f"No receiver configured: pass --receiver <device> or set `receiver` in {CONFIG_NAME}"
        )
    if receiver not in fleet.devices:
        raise pokemon_fleet.FleetError(f"Unknown receiver device {receiver!r}")
    if receiver not in owners and (args.targets_only or args.rolycoly_only or args.size_only):
        raise pokemon_fleet.FleetError(f"Receiver {receiver!r} is not attached to any configured computer")

    excluded = set(args.exclude_devices or ()) | {receiver}
    if args.donors:
        unknown = sorted(set(args.donors) - set(fleet.devices))
        if unknown:
            raise pokemon_fleet.FleetError(f"Unknown donor device(s): {', '.join(unknown)}")
        missing = sorted(d for d in args.donors if d != receiver and d not in owners)
        if missing:
            raise pokemon_fleet.FleetError(f"Donor device(s) not attached to any configured computer: {', '.join(missing)}")
        donors = list(dict.fromkeys(d for d in args.donors if d not in excluded and d in owners))
    else:
        donors = sorted(name for name in owners if name not in excluded)
    if receiver not in owners and len(donors) < 2:
        raise pokemon_fleet.FleetError(f"{receiver} is offline and fewer than two selected peers can trade")
    if not donors:
        raise pokemon_fleet.FleetError("No attached donor devices remain after selection/exclusions")
    return receiver, donors


async def _run_plan(
    args: argparse.Namespace,
    receiver: str,
    donors: Sequence[str],
    owners: Mapping[str, str],
    targets: Sequence[TargetSpec],
    return_queries: Sequence[str],
    budget: int,
    already: int,
) -> int:
    print(f"[collect] receiver: {receiver} [{owners[receiver]}] | remaining budget: {budget}/{args.trades} ({already} today)", flush=True)
    donor_list = ", ".join(f"{d} [{owners[d]}]" for d in donors)
    print(f"[collect] donors ({len(donors)}): {donor_list}", flush=True)
    for t in targets:
        print(f"[collect]   target {t.name}: {t.term}", flush=True)
    print(f"[collect] return queries: {len(return_queries)}", flush=True)
    for idx, q in enumerate(return_queries, 1):
        preview = q if len(q) <= 120 else f"{q[:90]}...{q[-25:]}"
        print(f"[collect]   return-{idx} ({len(q)} chars): {preview}", flush=True)
    return 0


async def _scan_fleet(
    donors: Sequence[str],
    receiver: str,
    owners: Mapping[str, str],
    machines: dict,
    fleet: pokemon_fleet.FleetConfig,
    targets: Sequence[TargetSpec],
    return_queries: Sequence[str],
    budget: int,
    shots: Path,
    dry_run: bool,
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    tasks = [
        _scan_donor_phone(d, owners[d], machines, fleet, targets, shots, dry_run)
        for d in donors
    ] + [
        _scan_receiver_fodder(receiver, owners[receiver], machines, fleet, return_queries, budget, shots, dry_run)
    ]
    results = await asyncio.gather(*tasks)
    donor_counts = dict(zip(donors, results[:-1]))
    fodder_counts = results[-1]
    return donor_counts, fodder_counts


@contextlib.asynccontextmanager
async def _open_optional_donor(
    donor: str,
    shots: Path,
    *,
    machine: str,
    machines: dict,
    fleet: pokemon_fleet.FleetConfig,
    dry_run: bool = False,
):
    try:
        cm = remote_target.open_fleet_phone(
            donor, shots, machine=machine, machines=machines, fleet=fleet, dry_run=dry_run
        )
        phone = await cm.__aenter__()
    except (gbl_ios.IOSGBLError, home.HomeRecoveryError, pokemon_fleet.FleetError, RuntimeError) as exc:
        print(f"[collect] {donor}: skipped (could not connect -- {exc})", flush=True)
        yield None
        return
    try:
        yield phone
    except BaseException as exc:
        if not await cm.__aexit__(type(exc), exc, exc.__traceback__):
            raise
    else:
        await cm.__aexit__(None, None, None)


async def run(args: argparse.Namespace, approved=None, *, evening_date=None) -> int:
    args.prepared_only = evening_date is not None
    if args.prepared_only and (args.dry_run or args.targets_only or args.rolycoly_only
                               or args.size_only or args.ignore_history):
        raise ValueError("Ender requires prepared choices and the daily trade journal")
    cfg = load_config(args.config_file)
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    machines = fleet_entrypoint.machine_configs()

    if args.propose_guaranteed is not None and args.plan:
        from . import collector_proposals
        return await collector_proposals.run(args, cfg, fleet, machines, {})
    clear_cfg_name = cfg.get("clear_config")
    clear_cfg_path = (
        Path(config_paths.expand_user_path(str(clear_cfg_name)))
        if clear_cfg_name and ("/" in str(clear_cfg_name) or str(clear_cfg_name).startswith("~"))
        else config_paths.default_config(str(clear_cfg_name or clear_pokemon.CONFIG_NAME))
    )
    survey_machines = machines
    if args.local:
        local = fleet_entrypoint.local_machine(machines)
        survey_machines = {local: machines[local]}
    owners, snapshots = remote_target.discover_attached_devices(fleet, survey_machines)
    from . import trading_accounts
    aliases = trading_accounts.aliases()
    fleet, owners = trading_accounts.route_fleet(fleet, owners, aliases)
    if args.receiver:
        args.receiver = aliases.get(args.receiver, args.receiver)
    if args.donors:
        args.donors = list(dict.fromkeys(aliases.get(n, n) for n in args.donors))
    args.exclude_devices = [aliases.get(n, n) for n in (args.exclude_devices or [])]
    for machine, snapshot in snapshots.items():
        for error in snapshot.errors:
            print(f"[collect] {machine} discovery incomplete: {error}", flush=True)
    if not args.donors:
        absent = sorted(n for n, spec in fleet.devices.items() if spec.enabled and n not in aliases
                        and n not in owners and n not in args.exclude_devices)
        if absent:
            print(f"[collect] configured phones absent from selected hosts: {', '.join(absent)}", flush=True)
    if approved is not None or args.propose_guaranteed is not None:
        from . import collector_proposals
        return await collector_proposals.run(args, cfg, fleet, machines, owners, approved)
    if evening_date is not None and (len(owners) < 2 or date.today() != evening_date):
        from . import collector_night
        print("[ender] No pair available or the evening's trade date has ended; opening storage review.", flush=True)
        result = await collector_night.finish(owners, machines, list(owners), clear_cfg_path, cfg, cfg["receiver"],
                                              transports=getattr(fleet, "transports", {}), devices=fleet.devices,
                                              prepared_only=args.prepared_only)
        return result or int(not owners)
    receiver, donors = _resolve_roster(args, cfg, fleet, owners)
    if not (args.targets_only or args.rolycoly_only or args.size_only):
        from . import collector_day
        configured_night = (cfg.get("finish_night") is True and not args.collect_all_only and args.trades == trade_plan.DAILY_TRADES
                            and not (args.dry_run or args.donors or args.exclude_devices or args.receiver
                                     or args.return_query or args.ignore_history or args.local))
        review_after = args.finish_night or configured_night
        try:
            status = await collector_day.run(args, cfg, fleet, machines, owners, receiver, donors, clear_cfg_path)
        except Exception as exc:
            if not review_after:
                raise
            print(f"[collect] trading incomplete: {type(exc).__name__}: {exc}", flush=True)
            status = 1
        if review_after:
            from . import collector_night
            finished = await collector_night.finish(owners, machines, [receiver, *donors], clear_cfg_path,
                                                    cfg, receiver, plan=args.plan, transports=getattr(fleet, "transports", {}), devices=fleet.devices,
                                              prepared_only=args.prepared_only)
            return status or finished
        return status
    targets = resolve_targets(cfg, rolycoly_only=args.rolycoly_only, size_only=args.size_only)
    exclude_return_dex = cfg.get("exclude_return_dex", DEFAULT_EXCLUDE_RETURN_DEX)
    return_filters = [str(x) for x in cfg.get("return_filters", REQUIRED_RETURN_FILTERS)]
    return_dex_tiers = cfg.get("return_dex_tiers")
    custom_ret = args.return_query if args.return_query is not None else cfg.get("custom_return_query")
    fallback_ret = str(cfg.get("fallback_return_query", DEFAULT_FALLBACK_RETURN_QUERY))
    return_queries = build_return_queries(
        clear_cfg_path,
        exclude_dex=[int(x) for x in exclude_return_dex],
        return_filters=return_filters,
        return_dex_tiers=[str(x) for x in return_dex_tiers] if return_dex_tiers else None,
        custom_return_query=str(custom_ret) if custom_ret else None,
        fallback_return_query=fallback_ret,
    )

    already = 0 if args.ignore_history else await asyncio.to_thread(starter_trade.trades_done_today, receiver)
    budget = max(0, int(args.trades) - already)

    if args.plan:
        return await _run_plan(args, receiver, donors, owners, targets, return_queries, budget, already)

    if budget <= 0:
        print(f"[collect] {receiver}: daily trade cap already reached ({already}/{args.trades})", flush=True)
        return 0

    if args.dry_run:
        donor_counts, fodder_counts = await _scan_fleet(
            donors, receiver, owners, machines, fleet, targets, return_queries, budget, args.shots, True
        )
        batches = plan_collection(donor_counts, donors, targets, receiver, budget)
        fodder_total = sum(fodder_counts.values())
        print(
            f"[collect] receiver fodder ready: {fodder_total} across {len(fodder_counts)} query(ies)",
            flush=True,
        )
        for b in batches:
            print(
                f"[collect] plan: {b.count} x {b.donor} [{owners[b.donor]}] ({b.target}) -> {b.receiver} [{owners[b.receiver]}]",
                flush=True,
            )
        planned_total = sum(b.count for b in batches)
        print(
            f"[collect] dry-run complete: planned {planned_total} trade(s) from {len(donors)} donor(s); 0 trades made",
            flush=True,
        )
        return 0

    done = 0
    ok_all = True
    stop_all = False
    ret_idx = 0
    recv_skip_idx = 0
    reserves = {n: max(ORDINARY_RESERVE, int(getattr(fleet.devices[n], "config", {}).get("trade_keep_last", 0)))
                for n in [receiver, *donors]}
    from .collector_day import Offers, OffersChanged, species_catalog
    catalog = species_catalog(clear_cfg_path) if any(reserves.values()) else {}
    recv_pool = Offers([TargetSpec(f"return-{i}", term.rsplit("&", 1)[-1], term)
                        for i, term in enumerate(return_queries, 1)], reserves[receiver], catalog)
    donor_history = ({} if args.ignore_history else dict(zip(donors, await asyncio.gather(
        *(asyncio.to_thread(starter_trade.trades_done_today, name) for name in donors)))))
    donor_left = {n: max(0, args.trades - donor_history.get(n, 0)) for n in donors}
    donor_trainers: dict[str, str] = {}
    skipped_donors: set[str] = set()

    async with remote_target.open_fleet_phone(
        receiver, args.shots, machine=owners[receiver], machines=machines, fleet=fleet, dry_run=False
    ) as recv_phone:
        recv_trainer = await trade_local.trainer_name(recv_phone)
        for target in targets:
            if stop_all or done >= budget:
                break
            for donor in donors:
                if stop_all or done >= budget:
                    break
                if donor in skipped_donors or donor_left[donor] <= 0:
                    continue
                async with _open_optional_donor(
                    donor, args.shots, machine=owners[donor], machines=machines, fleet=fleet
                ) as donor_phone:
                    if donor_phone is None:
                        skipped_donors.add(donor)
                        continue
                    try:
                        donor_trainer = donor_trainers.get(donor)
                        if not donor_trainer:
                            donor_trainer = await trade_local.trainer_name(donor_phone)
                            donor_trainers[donor] = donor_trainer
                        names = {donor: donor_trainer, receiver: recv_trainer}
                        made = 0
                        skip_idx = 0
                        donor_pool = Offers([target], reserves[donor], catalog)
                        failures = 0
                        while not stop_all and done < budget and donor_left[donor] > 0:
                            if ret_idx >= len(return_queries):
                                print(f"[collect] {receiver}: exhausted all return-fodder queries", flush=True)
                                stop_all = True
                                break
                            last_donor_count: list[int] = []

                            async def _pick_donor(p: starter.Phone) -> None:
                                if donor_pool.keep:
                                    await donor_pool.pick(p)
                                    return
                                last_donor_count.append(
                                    await choose_filtered_tile(p, target.term, target.name, skip_index=skip_idx)
                                )

                            async def _pick_receiver(p: starter.Phone) -> None:
                                nonlocal ret_idx, recv_skip_idx
                                if recv_pool.keep:
                                    await recv_pool.pick(p)
                                    return
                                while ret_idx < len(return_queries):
                                    try:
                                        await choose_filtered_tile(
                                            p,
                                            return_queries[ret_idx],
                                            f"return-{ret_idx + 1}",
                                            skip_index=recv_skip_idx,
                                        )
                                        return
                                    except trade_local.TradeOver:
                                        ret_idx += 1
                                        recv_skip_idx = 0
                                raise trade_local.TradeOver(f"{receiver}: no return fodder remaining")

                            try:
                                await trade_local.both(*(pool.prepare(phone) for pool, phone in (
                                    (donor_pool, donor_phone), (recv_pool, recv_phone)) if pool.keep))
                                def record(receipts):
                                    dex = {p: int(pool.current.query) for p, pool in (
                                        (donor, donor_pool), (receiver, recv_pool)) if pool.keep}
                                    labels = {donor: donor_pool.labels[-1] if donor_pool.labels else target.name,
                                              receiver: recv_pool.labels[-1] if recv_pool.labels else "fodder"}
                                    starter_trade.record_trade([donor, receiver], labels, target.name, "collect",
                                                              offered_dex=dex, receipts=receipts)
                                await trade_local.trade_once(
                                    donor_phone,
                                    recv_phone,
                                    names,
                                    {donor: target.term, receiver: return_queries[ret_idx]},
                                    {donor: target.name, receiver: "fodder"},
                                    choosers={donor: _pick_donor, receiver: _pick_receiver},
                                    on_confirmed=record, daily_cap=None if args.ignore_history else args.trades,
                                )
                                donor_left[donor] -= 1
                                made += 1
                                done += 1
                                failures = 0
                                print(
                                    f"[collect] {donor} ({target.name}) -> {receiver}: "
                                    f"#{made} (session {done}, day {already + done})",
                                    flush=True,
                                )
                                if recv_pool.keep:
                                    recv_pool.used()
                                    if not recv_pool.available:
                                        stop_all = True
                                if donor_pool.keep:
                                    donor_pool.used()
                                    if not donor_pool.available:
                                        break
                                if last_donor_count and last_donor_count[-1] <= 1:
                                    print(
                                        f"[collect] {donor} ({target.name}): final match traded; moving on",
                                        flush=True,
                                    )
                                    break
                            except OffersChanged:
                                continue
                            except trade_local.LuckyAvailable as exc:
                                print(f"[collect] {donor} <-> {receiver}: skipped Lucky Trade -- {exc}", flush=True)
                                skipped_donors.add(donor)
                                break
                            except trade_local.TradeRefused as exc:
                                if str(exc).startswith(f"{receiver}:"):
                                    recv_skip_idx += 1
                                    if recv_pool.keep:
                                        recv_pool.skip = recv_skip_idx
                                    print(
                                        f"[collect] {receiver}: refused return slot ({exc}); "
                                        f"skipping slot -> {recv_skip_idx}/{DEFAULT_MAX_SKIPS}",
                                        flush=True,
                                    )
                                    if recv_skip_idx >= DEFAULT_MAX_SKIPS:
                                        ret_idx += 1
                                        recv_skip_idx = 0
                                        if recv_pool.keep:
                                            recv_pool.advance()
                                        if ret_idx >= len(return_queries):
                                            stop_all = True
                                            break
                                else:
                                    skip_idx += 1
                                    if donor_pool.keep:
                                        donor_pool.skip = skip_idx
                                    print(
                                        f"[collect] {donor} ({target.name}): refused trade ({exc}); "
                                        f"skipping slot -> {skip_idx}/{DEFAULT_MAX_SKIPS}",
                                        flush=True,
                                    )
                                    if skip_idx >= DEFAULT_MAX_SKIPS:
                                        break
                            except trade_local.TradeOver as exc:
                                print(f"[collect] {donor} ({target.name}): over -- {exc}", flush=True)
                                if "limit" in str(exc).lower() or receiver in str(exc):
                                    stop_all = True
                                break
                            except (starter.StepFailed, remote_target.home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
                                if "not on the friends list" in str(exc).lower():
                                    print(f"[collect] {donor} <-> {receiver}: skipped -- {exc}", flush=True)
                                    skipped_donors.add(donor)
                                    break
                                failures += 1
                                print(f"[collect] {donor} ({target.name}): error ({exc}); retry {failures}/2", flush=True)
                                if failures >= 2:
                                    ok_all = False
                                    break
                    finally:
                        with contextlib.suppress(Exception):
                            await donor_phone.go_home()
        with contextlib.suppress(Exception):
            await recv_phone.go_home()

    ok_all = ok_all and done > 0
    print(f"[collect] {'done' if ok_all else 'PROBLEM'}: completed {done} trade(s) to {receiver}", flush=True)
    return 0 if ok_all else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.approve_guaranteed or args.decline_guaranteed:
        from . import trade_approval
        try:
            if args.decline_guaranteed:
                return trade_approval.decline(args.decline_guaranteed)
            if trade_approval.approve(args.approve_guaranteed) is None:
                return 0
            with trade_approval.claim_for_reselect(args.approve_guaranteed) as approved:
                fleet_entrypoint.apply_local_environment()
                return asyncio.run(run(args, approved))
        except trade_approval.ApprovalRequired as exc:
            print(f"[guaranteed] {exc}", flush=True)
            return 1
    fleet_entrypoint.apply_local_environment()
    return asyncio.run(run(args))


if __name__ == "__main__":
    from collect_trades import main as public_main
    raise SystemExit(public_main())
