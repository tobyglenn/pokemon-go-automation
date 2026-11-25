"""Safe, single-query, Vision-OCR verified Pokemon clearing across Android and iOS fleets.

Why this exists alongside `luckytrash.py`:
- Audits the dex list against PH1's Lucky Dex, evolution chains, Mega evolutions,
  and raid rankings, and enforces all safety exclusions (`!#`, `!98Plus`,
  `!shiny`, `!4*`, `!favorite`, `!lucky`, `!legendary`, `!mythical`,
  `!ultra beasts`, `!costume`, `!shadow`, `!dynamax`, `!gigantamax`,
  `!background`, `!xxs`, `!xxl`).
- Runs the entire audited filter as one verified search query per phone instead
  of slow multi-chunk searching.
- Every transfer verifies: (1) the single search filter in the search box and
  result counter, (2) the detail page's species name and absence of protected
  tags/Mega/Lucky/XXS/XXL status, (3) the menu's exact TRANSFER label, (4) the
  confirmation dialog's safe wording before tapping YES, (5) no second warning
  dialog, and (6) the storage counter dropping by 1.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import math
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from PIL import Image, ImageChops

from . import (
    config_paths,
    gbl_home_recovery as home,
    gbl_vision,
    ios_wda_cleanup,
    pokemon_fleet,
    raid_gamedata,
    raid_meta,
    screen_router,
    starter,
    trade_local,
    trade_plan,
)

OCRBox = gbl_vision.OCRBox
CONFIG_NAME = "clear-pokemon.json"
DEFAULT_MAX_TRANSFERS = 200
ANDROID_INPUT_BURST = 120
IOS_WDA_KEYS_FREQUENCY = 120
SEARCH_CLEAR_X = 0.90
MENU_POINT = (0.86, 0.932)
TILE_RISE = 0.055

ULTRA_BEASTS: frozenset[int] = frozenset(set(range(793, 800)) | set(range(803, 807)))
REQUIRED_SAFETY_FILTERS: tuple[str, ...] = (
    "!#",
    "!98Plus",
    "!dontrade",
    "!shiny",
    "!4*",
    "!favorite",
    "!lucky",
    "!legendary",
    "!mythical",
    "!ultra beasts",
    "!costume",
    "!shadow",
    "!dynamax",
    "!gigantamax",
    "!background",
    "!xxs",
    "!xxl",
)
DEFAULT_PROTECTED_TAGS: tuple[str, ...] = ("98Plus", "dontrade", "mega", "ToTrade", "SilverBottlePot")

# Species in PoGO whose main-series evolution either does not evolve from the
# pre-evolution in PoGO or is raid-only; reported for awareness rather than blocked.
NON_EVOLVING_POGO_RELATIVES: dict[int, tuple[int, str]] = {
    234: (899, "Wyrdeer is raid-only in Pokemon GO (Stantler cannot evolve)"),
    290: (292, "Shedinja does not evolve from Nincada in Pokemon GO"),
}

RARE_COLLECTIBLE_DEX: frozenset[int] = frozenset({
    128, 132, 201, 213, 225, 235, 241, 324, 352, 417, 455, 556,
    561, 626, 676, 741, 775, 778, 780, 924, 931, 973,
})

NOTABLE_PVP_DEX: frozenset[int] = frozenset({
    36, 40, 62, 87, 105, 108, 171, 184, 195, 197, 206, 207,
    226, 279, 288, 302, 308, 340, 346, 411, 423, 472, 560,
    593, 598, 618, 630, 660, 663, 671, 748, 752, 823, 862,
    877, 980,
})

DANGER_DIALOG_WORDS: tuple[str, ...] = (
    "shiny",
    "lucky",
    "legendary",
    "mythical",
    "event",
    "costume",
    "xxl",
    "xxs",
    "dynamax",
    "gigantamax",
    "special",
    "background",
    "shadow",
    "ultra beast",
    "favorite",
    "buddy",
)


class ClearSafetyError(starter.StepFailed):
    """Raised when a screen check fails closed rather than risking an unsafe tap."""


# --- dex range parsing & formatting ---------------------------------------------------


def parse_dex_ranges(expr: str) -> set[int]:
    """Parse a Pokemon GO Pokedex number expression like `10-12,19-24,128`."""
    out: set[int] = set()
    for part in expr.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            lo, hi = int(left.strip()), int(right.strip())
            if lo > hi:
                lo, hi = hi, lo
            out.update(range(lo, hi + 1))
        else:
            out.add(int(token))
    return out


def format_dex_ranges(nums: Iterable[int]) -> str:
    """Compress sorted integers into Pokemon GO range notation `10-12,19-24,128`."""
    ordered = sorted(set(nums))
    if not ordered:
        return ""
    parts: list[str] = []
    start = end = ordered[0]
    for value in ordered[1:]:
        if value == end + 1:
            end = value
        else:
            parts.append(str(start) if start == end else f"{start}-{end}")
            start = end = value
    parts.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(parts)


_DEX_SEGMENT_RE = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")


def parse_query(raw: str) -> tuple[set[int], list[str]]:
    """Split a Pokemon GO search query into (dex_set, filter_tokens)."""
    dex: set[int] = set()
    filters: list[str] = []
    for clause in raw.split("&"):
        token = clause.strip()
        if not token:
            continue
        if _DEX_SEGMENT_RE.fullmatch(token.replace(" ", "")):
            dex |= parse_dex_ranges(token)
        else:
            filters.append(token)
    return dex, filters


def normalize_species_token(text: str) -> str:
    """Fold accents/punctuation into lowercase alphanumeric species token."""
    folded = unicodedata.normalize("NFKD", text)
    ascii_text = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


def species_aliases(raw_name: str) -> set[str]:
    """Normalized name variants OCR might return on a Pokemon's detail page."""
    token = normalize_species_token(raw_name)
    if not token:
        return set()
    aliases = {token}
    if token in {"nidoranf", "nidoranm"}:
        aliases.add("nidoran")
    return aliases


# --- evolution & audit graph ----------------------------------------------------------


def load_evolution_graph(
    species_csv: Path | None = None,
    gd: raid_gamedata.GameData | None = None,
) -> tuple[dict[int, str], dict[int, list[int]], set[int]]:
    """Return `(dex_to_name, dex_to_children, legendary_mythical_ub_dex)`."""
    names: dict[int, str] = {}
    children: dict[int, list[int]] = {}
    special: set[int] = set(ULTRA_BEASTS)

    if species_csv is not None and species_csv.is_file():
        with species_csv.open(newline="") as fh:
            for row in csv.DictReader(fh):
                dex = int(row["id"])
                names[dex] = row["identifier"].strip()
                pre = row.get("evolves_from_species_id", "").strip()
                if pre:
                    children.setdefault(int(pre), []).append(dex)
                if row.get("is_legendary", "").strip() == "1" or row.get("is_mythical", "").strip() == "1":
                    special.add(dex)

    # Supplement from PokeMiners / GameData so environments without species.csv still work.
    try:
        parent_pid, pid_dex = trade_plan._evolution_tree()
        gd = gd or raid_gamedata.load()
        for pid, dex in pid_dex.items():
            names.setdefault(dex, pid.replace("_", "-"))
            if gd.pokemon.get(pid, {}).get("legendary"):
                special.add(dex)
        for child_pid, up_pid in parent_pid.items():
            cd = pid_dex.get(child_pid)
            pd = pid_dex.get(up_pid)
            if cd and pd and cd not in children.get(pd, []):
                children.setdefault(pd, []).append(cd)
    except Exception:
        pass
    return names, children, special


def descendants_of(dex: int, children: Mapping[int, Sequence[int]]) -> list[int]:
    out: list[int] = []
    queue = list(children.get(dex, ()))
    while queue:
        cur = queue.pop(0)
        if cur not in out:
            out.append(cur)
            queue.extend(children.get(cur, ()))
    return out


def _load_lucky_sets(
    lucky_report: Path | None,
    lucky_votes: Path | None,
) -> tuple[set[int], set[int]]:
    """Return `(confirmed_lucky, needed_lucky)` from available private PH1 audit files."""
    confirmed: set[int] = set()
    needed: set[int] = set()

    if lucky_votes is not None and lucky_votes.is_file():
        raw_votes = json.loads(lucky_votes.read_text())
        for key, items in raw_votes.items():
            dex = int(key)
            lc = sum(1 for entry in items if entry[0] == "lucky")
            nc = sum(1 for entry in items if entry[0] == "none")
            if lc > nc and lc >= 1:
                confirmed.add(dex)
            elif nc > lc:
                needed.add(dex)

    if lucky_report is not None and lucky_report.is_file():
        rep = json.loads(lucky_report.read_text())
        if "needed_lucky_list" in rep:
            for item in rep["needed_lucky_list"]:
                d = int(str(item).split()[0])
                if d not in confirmed:
                    needed.add(d)
        elif "needed_lucky_query" in rep:
            for d in parse_dex_ranges(rep["needed_lucky_query"]):
                if d not in confirmed:
                    needed.add(d)

    return confirmed, needed


def _raid_protection_sets(
    threshold: float = 0.50,
    gd: raid_gamedata.GameData | None = None,
    children: Mapping[int, Sequence[int]] | None = None,
) -> tuple[dict[int, str], set[int]]:
    """Return `(raid_reasons_by_dex, mega_capable_dex)`."""
    raid_hits: dict[int, str] = {}
    mega_dex: set[int] = set()
    try:
        gd = gd or raid_gamedata.load()
        _parent, pid_dex = trade_plan._evolution_tree()
        for pid, info in gd.pokemon.items():
            d = info.get("dex") or pid_dex.get(pid.split("_")[0])
            if d and info.get("megas"):
                mega_dex.add(int(d))
        for attack_type, ratings in raid_meta.rankings().items():
            if not ratings:
                continue
            top = ratings[0].er
            for idx, rating in enumerate(ratings[:30], 1):
                share = rating.er / top
                if share < threshold:
                    continue
                pid = rating.attacker.pokemon_id
                d = gd.pokemon.get(pid, {}).get("dex") or pid_dex.get(pid.split("_")[0])
                if d:
                    raid_hits.setdefault(int(d), f"{rating.attacker.name} ({attack_type} #{idx}, {share:.1%})")
    except Exception:
        pass
    if children:
        # Mark pre-evolutions of mega-capable species too.
        mega_parents = set(mega_dex)
        changed = True
        while changed:
            changed = False
            for parent_d, kids in children.items():
                if any(k in mega_parents for k in kids) and parent_d not in mega_parents:
                    mega_parents.add(parent_d)
                    changed = True
        mega_dex = mega_parents
    return raid_hits, mega_dex


@dataclass(frozen=True)
class ClearQuery:
    dex_nums: tuple[int, ...]
    dex_part: str
    filters: tuple[str, ...]
    term: str
    allowed_names: frozenset[str]


@dataclass
class AuditResult:
    raw_dex: set[int]
    raw_filters: list[str]
    direct_need_lucky: dict[int, str] = field(default_factory=dict)
    evolves_into_need_lucky: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    informational_relatives: dict[int, tuple[int, str]] = field(default_factory=dict)
    mega_capable: dict[int, str] = field(default_factory=dict)
    raid_protected: dict[int, str] = field(default_factory=dict)
    legendary_mythical_ub: dict[int, str] = field(default_factory=dict)
    rare_collectibles: dict[int, str] = field(default_factory=dict)
    pvp_notables: dict[int, str] = field(default_factory=dict)
    missing_safety_filters: list[str] = field(default_factory=list)
    safe_dex: list[int] = field(default_factory=list)
    safe_filters: list[str] = field(default_factory=list)
    safe_query: str = ""
    query: ClearQuery | None = None
    names: dict[int, str] = field(default_factory=dict)


def merge_safety_filters(existing: Sequence[str], extra: Sequence[str] = REQUIRED_SAFETY_FILTERS) -> tuple[list[str], list[str]]:
    """Return `(merged_filters, added_missing_filters)` preserving order case-insensitively."""
    seen = {f.strip().lower() for f in existing if f.strip()}
    merged = [f.strip() for f in existing if f.strip()]
    missing: list[str] = []
    for req in extra:
        if req.lower() not in seen:
            missing.append(req)
            merged.append(req)
            seen.add(req.lower())
    return merged, missing


def build_single_query(
    dex_nums: Sequence[int],
    filters: Sequence[str],
    names: Mapping[int, str],
) -> ClearQuery:
    """Build one complete clearing query with all safety filters placed first."""
    ordered = tuple(sorted(set(dex_nums)))
    if not ordered:
        raise pokemon_fleet.FleetError("No safe dex species remain after auditing")
    clean_filters = tuple(f.strip() for f in filters if f.strip())
    dex_part = format_dex_ranges(ordered)
    prefix = "&".join(clean_filters)
    term = f"{prefix}&{dex_part}" if prefix else dex_part
    allowed: set[str] = set()
    for d in ordered:
        allowed.update(species_aliases(names.get(d, "")))
    return ClearQuery(
        dex_nums=ordered,
        dex_part=dex_part,
        filters=clean_filters,
        term=term,
        allowed_names=frozenset(allowed),
    )


def audit_query(
    raw_query: str,
    *,
    lucky_report: Path | None = None,
    lucky_votes: Path | None = None,
    species_csv: Path | None = None,
    raid_threshold: float = 0.50,
    exclude_rare: bool = False,
    extra_exclude_dex: Iterable[int] = (),
    safety_filters: Sequence[str] = REQUIRED_SAFETY_FILTERS,
) -> AuditResult:
    """Audit a clearing query against PH1's Lucky Dex, evolutions, Megas, and raid meta."""
    raw_dex, raw_filters = parse_query(raw_query)
    names, children, special = load_evolution_graph(species_csv)
    confirmed_lucky, needed_lucky = _load_lucky_sets(lucky_report, lucky_votes)
    raid_hits, mega_dex = _raid_protection_sets(raid_threshold, children=children)

    res = AuditResult(raw_dex=set(raw_dex), raw_filters=list(raw_filters), names=names)
    excluded = set(extra_exclude_dex)

    for d in sorted(raw_dex):
        sp_name = names.get(d, f"#{d}")
        if d in special:
            res.legendary_mythical_ub[d] = sp_name
            excluded.add(d)
        if d in needed_lucky and d not in confirmed_lucky:
            res.direct_need_lucky[d] = sp_name
            excluded.add(d)

        rel_info = NON_EVOLVING_POGO_RELATIVES.get(d)
        for desc in descendants_of(d, children):
            if rel_info and rel_info[0] == desc and desc in needed_lucky and desc not in confirmed_lucky:
                res.informational_relatives[d] = rel_info
                continue
            if desc in needed_lucky and desc not in confirmed_lucky:
                res.evolves_into_need_lucky.setdefault(d, []).append((desc, names.get(desc, f"#{desc}")))
                excluded.add(d)

        if d in mega_dex:
            res.mega_capable[d] = sp_name
            excluded.add(d)

        if d in raid_hits:
            res.raid_protected[d] = raid_hits[d]
            excluded.add(d)
        else:
            for desc in descendants_of(d, children):
                if desc in raid_hits:
                    res.raid_protected[d] = f"evolves to {raid_hits[desc]}"
                    excluded.add(d)
                    break

        if d in RARE_COLLECTIBLE_DEX:
            res.rare_collectibles[d] = sp_name
            if exclude_rare:
                excluded.add(d)
        if d in NOTABLE_PVP_DEX and d not in excluded:
            res.pvp_notables[d] = sp_name

    merged_filters, missing = merge_safety_filters(raw_filters, (*safety_filters, *REQUIRED_SAFETY_FILTERS))
    res.missing_safety_filters = missing
    res.safe_filters = merged_filters
    res.safe_dex = sorted(raw_dex - excluded)
    if res.safe_dex:
        res.query = build_single_query(res.safe_dex, merged_filters, names)
        res.safe_query = res.query.term
    else:
        res.safe_query = "&".join(merged_filters)
    return res


def format_audit_report(res: AuditResult) -> str:
    """Human-readable audit summary for review before any deletion."""
    lines = [
        f"Clearing List Audit ({len(res.raw_dex)} input species -> {len(res.safe_dex)} safe species, "
        f"1 query, {len(res.safe_query)} chars)",
        "",
    ]
    if res.direct_need_lucky:
        lines.append(f"BLOCKED (PH1 directly needs Lucky: {len(res.direct_need_lucky)}):")
        lines.append("  " + ", ".join(f"#{d} {n}" for d, n in sorted(res.direct_need_lucky.items())))
    else:
        lines.append("Direct Lucky Dex check: all input species confirmed Lucky on PH1 (0 direct gaps).")

    if res.evolves_into_need_lucky:
        lines.append(f"\nREMOVED (evolves into species PH1 STILL NEEDS LUCKY: {len(res.evolves_into_need_lucky)}):")
        for d, evos in sorted(res.evolves_into_need_lucky.items()):
            evo_str = ", ".join(f"#{ed} {en}" for ed, en in evos)
            lines.append(f"  - #{d:03d} {res.names.get(d, '?')} -> evolves to {evo_str}")

    if res.mega_capable:
        lines.append(f"\nREMOVED (Mega-capable line: {len(res.mega_capable)}):")
        lines.append("  " + ", ".join(f"#{d} {n}" for d, n in sorted(res.mega_capable.items())))

    if res.raid_protected:
        lines.append(f"\nREMOVED (raid-protected >=50% line: {len(res.raid_protected)}):")
        for d, why in sorted(res.raid_protected.items()):
            lines.append(f"  - #{d:03d} {res.names.get(d, '?')}: {why}")

    if res.legendary_mythical_ub:
        lines.append(f"\nREMOVED (legendary/mythical/UB: {len(res.legendary_mythical_ub)}):")
        lines.append("  " + ", ".join(f"#{d} {n}" for d, n in sorted(res.legendary_mythical_ub.items())))

    if res.missing_safety_filters:
        lines.append(f"\nADDED missing safety exclusions ({len(res.missing_safety_filters)}):")
        lines.append("  " + " & ".join(res.missing_safety_filters))

    if res.informational_relatives:
        lines.append("\nINFO (needed-lucky relatives that do NOT evolve from these in PoGO):")
        for d, (rd, note) in sorted(res.informational_relatives.items()):
            lines.append(f"  - #{d:03d} {res.names.get(d, '?')} / #{rd:03d} {res.names.get(rd, '?')}: {note}")

    if res.rare_collectibles:
        lines.append(f"\nINFO (regional/rare collectibles still in list unless --keep-rare: {len(res.rare_collectibles)}):")
        lines.append("  " + ", ".join(f"#{d} {n}" for d, n in sorted(res.rare_collectibles.items())))

    if res.pvp_notables:
        lines.append(f"\nINFO (notable GBL species — ensure PvP builds are tagged or favorited: {len(res.pvp_notables)}):")
        lines.append("  " + ", ".join(f"#{d} {n}" for d, n in sorted(res.pvp_notables.items())))

    lines.append(f"\nSafe dex ranges ({len(res.safe_dex)} species):")
    lines.append(f"  {format_dex_ranges(res.safe_dex)}")
    lines.append(f"Safety filters ({len(res.safe_filters)}):")
    lines.append(f"  {'&'.join(res.safe_filters)}")
    return "\n".join(lines)


# --- config loading -------------------------------------------------------------------


def load_clear_config(path: Path | None = None) -> dict[str, Any]:
    target = path or config_paths.default_config(CONFIG_NAME)
    if not target.is_file():
        raise pokemon_fleet.FleetError(
            f"Missing clearing config: {target}. Create it from examples/clear-pokemon.example.json "
            "or pass --query."
        )
    loaded = json.loads(target.read_text())
    if not isinstance(loaded, dict):
        raise pokemon_fleet.FleetError(f"{target} must contain a JSON object")
    return loaded


def build_clear_plan(
    *,
    config_file: Path | None = None,
    query: str | None = None,
    exclude_rare: bool = False,
) -> tuple[ClearQuery, AuditResult | None, list[str]]:
    """Build `(single_query, optional_audit_result, protected_tags)`."""
    cfg = {}
    if query is None:
        cfg = load_clear_config(config_file)
    elif config_file is not None and config_file.is_file():
        cfg = load_clear_config(config_file)

    protected_tags = [str(t) for t in cfg.get("protected_tags", DEFAULT_PROTECTED_TAGS)]
    keep_rare = bool(exclude_rare or cfg.get("exclude_rare", False))
    extra_exclude = {int(x) for x in cfg.get("extra_exclude_dex", ())}

    def _opt_path(key: str) -> Path | None:
        val = cfg.get(key)
        if not val:
            return None
        return Path(config_paths.expand_user_path(str(val)))

    lucky_report = _opt_path("lucky_report")
    lucky_votes = _opt_path("lucky_votes")
    species_csv = _opt_path("species_csv")

    if query is not None:
        audit = audit_query(
            query,
            lucky_report=lucky_report,
            lucky_votes=lucky_votes,
            species_csv=species_csv,
            exclude_rare=keep_rare,
            extra_exclude_dex=extra_exclude,
            safety_filters=cfg.get("safety_filters", REQUIRED_SAFETY_FILTERS),
        )
        if audit.query is None:
            raise pokemon_fleet.FleetError("No safe dex species remain after auditing")
        return audit.query, audit, protected_tags

    # Prefer explicit pre-audited `safe_dex_ranges` + `safety_filters` so hosts
    # without PH1's private audit files run the exact same audited single query.
    safe_expr = cfg.get("safe_dex_ranges", "")
    raw_query = cfg.get("raw_query", "")
    if safe_expr:
        safe_dex = parse_dex_ranges(str(safe_expr)) - extra_exclude
        if keep_rare:
            safe_dex -= RARE_COLLECTIBLE_DEX
        filters, _ = merge_safety_filters(
            list(cfg.get("safety_filters", REQUIRED_SAFETY_FILTERS)),
            REQUIRED_SAFETY_FILTERS,
        )
        names, _, _ = load_evolution_graph(species_csv)
        single = build_single_query(sorted(safe_dex), filters, names)
        audit = None
        if raw_query:
            audit = audit_query(
                str(raw_query),
                lucky_report=lucky_report,
                lucky_votes=lucky_votes,
                species_csv=species_csv,
                exclude_rare=keep_rare,
                extra_exclude_dex=extra_exclude,
                safety_filters=filters,
            )
        return single, audit, protected_tags

    if raw_query:
        audit = audit_query(
            str(raw_query),
            lucky_report=lucky_report,
            lucky_votes=lucky_votes,
            species_csv=species_csv,
            exclude_rare=keep_rare,
            extra_exclude_dex=extra_exclude,
            safety_filters=cfg.get("safety_filters", REQUIRED_SAFETY_FILTERS),
        )
        if audit.query is None:
            raise pokemon_fleet.FleetError("No safe dex species remain after auditing")
        return audit.query, audit, protected_tags

    raise pokemon_fleet.FleetError(f"{CONFIG_NAME} needs safe_dex_ranges or raw_query")


# --- OCR safety verification ----------------------------------------------------------


def _compact_search(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = ascii_text.replace("|", "l").replace("—", "-").replace("–", "-")
    return re.sub(r"[^0-9a-z,&!-]", "", folded)


def result_count(boxes: Sequence[OCRBox], height: int) -> int | None:
    for box in sorted(boxes, key=lambda b: b.y):
        if box.y > height * 0.22:
            break
        if "location" in box.text.casefold() and "detect" in box.text.casefold():
            continue
        match = re.search(r"\((\d{1,5})\)", box.text)
        if match:
            return int(match.group(1))
    return None


def search_field_matches_query(
    boxes: Sequence[OCRBox],
    term: str,
    height: float,
) -> bool:
    """Confirm the search bar displays `term`'s left-aligned prefix OR right-aligned suffix."""
    wanted = _compact_search(term)
    if not wanted:
        return False
    tail_digits = re.sub(r"[^0-9]", "", wanted.rsplit("&", 1)[-1])
    for box in boxes:
        if box.y > height * 0.35:
            continue
        seen = _compact_search(box.text)
        if not seen:
            continue
        candidates = {seen, seen.lstrip(",")}
        # Vision OCR sometimes misreads `&!` as `&.` / `&:` / `&;` on narrow search bars.
        punct_fixed = _compact_search(re.sub(r"&[.:;](?=[a-z0-9])", "&!", box.text))
        if punct_fixed:
            candidates.add(punct_fixed)
            candidates.add(punct_fixed.lstrip(","))
        if seen[:1] in set("q9aog@"):
            candidates.add(seen[1:])
        for cand in candidates:
            if cand == wanted:
                return True
            if len(cand) >= 14:
                if wanted.startswith(cand) or wanted.endswith(cand):
                    return True
                if "!" in cand and wanted.startswith("!" + cand):
                    return True
            # When trailing dex slice has minor punctuation slips, verify at least
            # 8 trailing digits of the query's dex range with range punctuation.
            seen_digits = re.sub(r"[^0-9]", "", cand)
            min_digits = min(8, len(tail_digits))
            if (
                len(cand) >= 10
                and ("," in cand or "-" in cand)
                and min_digits > 0
                and len(seen_digits) >= min_digits
                and tail_digits.endswith(seen_digits)
            ):
                return True
    return False


def detail_page(image: Image.Image, boxes: Sequence[OCRBox]) -> bool:
    text = starter.text_of(boxes)
    has_hp = any(re.search(r"\d+\s*/\s*\d+\s*hp", b.text, re.I) for b in boxes)
    return ("stardust" in text or "power up" in text) and has_hp and not starter.mega_level_page(boxes)


def detail_species_candidates(boxes: Sequence[OCRBox], image: Image.Image) -> list[str]:
    """Candidate normalized species/name tokens above the HP bar on a detail page."""
    hp_box = next((b for b in boxes if re.search(r"\d+\s*/\s*\d+\s*hp", b.text, re.I)), None)
    ceiling = hp_box.y if hp_box is not None else image.height * 0.62
    floor = image.height * 0.25
    out: list[str] = []
    for b in sorted(boxes, key=lambda x: x.center_y, reverse=True):
        if not (floor <= b.center_y <= ceiling):
            continue
        raw = gbl_vision.normalize(b.text)
        if re.search(r"\bcp\s*\d|^cp|lucky|stardust|candy", raw):
            continue
        tok = normalize_species_token(b.text)
        if len(tok) >= 3:
            out.append(tok)
    return out


def detail_species_allowed(
    boxes: Sequence[OCRBox],
    image: Image.Image,
    allowed_names: frozenset[str],
) -> tuple[bool, str]:
    """Verify the open Pokemon's displayed name belongs to `allowed_names` (fails closed)."""
    if not allowed_names:
        return False, "no allowed species names for chunk"
    candidates = detail_species_candidates(boxes, image)
    for tok in candidates:
        if tok in allowed_names:
            return True, tok
        if "nidoran" in allowed_names and tok.startswith("nidoran") and len(tok) <= 9:
            return True, tok
    return False, f"detail name {candidates[:2] or ['<unread>']} not in chunk allowed list"


def detail_safety_block(
    boxes: Sequence[OCRBox],
    image: Image.Image,
    protected_tags: Sequence[str] = DEFAULT_PROTECTED_TAGS,
) -> str | None:
    """Return a refusal reason if the detail page shows protected tags, Mega, or Lucky status."""
    norm_lines = [gbl_vision.normalize(b.text) for b in boxes]
    joined = " ".join(norm_lines)
    if "lucky pok" in joined:
        return "detail page shows Lucky Pokemon"
    if "mega evolve" in joined or "primal reversion" in joined or "mega energy" in joined:
        return "detail page shows Mega/Primal evolution"
    if "purify" in joined:
        return "detail page shows Shadow Pokemon"
    if re.search(r"\b(?:xxs|xxl)\b", joined):
        return "detail page shows XXS/XXL size"
    norm_tags = {normalize_species_token(t) for t in protected_tags if t.strip()}
    for line in norm_lines:
        tok = normalize_species_token(line)
        if tok in norm_tags:
            return f"detail page carries protected tag '{tok}'"
    return None


def dialog_safety_block(boxes: Sequence[OCRBox]) -> str | None:
    """Return None ONLY when a plain professor transfer confirmation dialog is safe to confirm."""
    text = " ".join(gbl_vision.normalize(b.text) for b in boxes)
    if "transfer" not in text:
        return "dialog does not mention transfer"
    if "professor" not in text and "cannot undo" not in text and "can t undo" not in text:
        return "dialog missing professor/undo wording"
    if starter.find_box(boxes, "yes", exact=True) is None:
        return "dialog missing YES button"
    if starter.find_box(boxes, "no", exact=True) is None:
        return "dialog missing NO button"
    for danger in DANGER_DIALOG_WORDS:
        if re.search(rf"\b{re.escape(danger)}\b", text):
            return f"dialog mentions protected keyword '{danger}'"
    return None


def safe_grid_tiles(image: Image.Image, boxes: Sequence[OCRBox]) -> list[OCRBox]:
    tiles = []
    for b in boxes:
        if not re.fullmatch(r"cp\s*\d+", trade_local.norm(b.text)):
            continue
        fy = b.center_y / image.height
        fx = b.center_x / image.width
        if not (0.22 <= fy <= 0.88):
            continue
        if fy > 0.82 and fx > 0.72:  # sort control
            continue
        if fy > 0.84 and abs(fx - 0.5) < 0.14:  # close button
            continue
        tiles.append(b)
    return sorted(tiles, key=lambda b: (b.center_y // 40, b.center_x))


def sample_visible_species(image: Image.Image, boxes: Sequence[OCRBox], limit: int = 6) -> list[str]:
    """Read sample displayed species names beneath visible storage tiles."""
    samples: list[str] = []
    for tile in safe_grid_tiles(image, boxes):
        labels = [
            b for b in boxes
            if abs(b.center_x - tile.center_x) < image.width * 0.14
            and image.height * 0.03 < (b.center_y - tile.center_y) < image.height * 0.14
            and not re.fullmatch(r"cp\s*\d+", trade_local.norm(b.text))
        ]
        if not labels:
            continue
        label = min(labels, key=lambda b: b.center_y).text.strip().lstrip("•· ").strip()
        if label and label not in samples:
            samples.append(label)
        if len(samples) >= limit:
            break
    return samples


def _active_filter_box(image: Image.Image, boxes: Sequence[OCRBox]) -> OCRBox | None:
    """Return the active filter pill text box (never the `N / Max` storage capacity header)."""
    return next(
        (
            b for b in boxes
            if image.height * 0.12 <= b.center_y < image.height * 0.23
            and "/" not in b.text
            and (
                any(ch in b.text for ch in "!&,")
                or re.fullmatch(r"\d[\d,\-]*[,\-][\d,\-]+", _compact_search(b.text))
            )
        ),
        None,
    )


def evolutionary_line_off(image: Image.Image, boxes: Sequence[OCRBox]) -> bool:
    """Require a hollow teal ring beside the native Show Evolutionary Line label."""
    labels = [b for b in boxes if gbl_vision.normalize(b.text).endswith("show evolutionary line")]
    if len(labels) != 1:
        return False
    label = labels[0]
    # Vision sometimes includes the circle in the label's left boundary.
    # Its right edge and the row center are stable on Android and iOS.
    right, y, width = label.x + label.width, label.center_y, image.width
    area = (int(right - .65*width), int(y - .05*width),
            int(right - .51*width), int(y + .05*width))
    if area[0] < 0 or area[1] < 0 or area[2] > width or area[3] > image.height:
        return False
    region = image.crop(area).convert("RGB").resize((70, 50), Image.Resampling.LANCZOS)
    rgb = region.load()
    def teal(x, y):
        r, g, b = rgb[x, y]
        return g-r > 50 and b-r > 50 and abs(g-b) < 55
    outline = [(x,y) for y in range(region.height) for x in range(region.width) if teal(x,y)]
    if not outline:
        return False
    xs, ys = zip(*outline)
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    dx, dy = right-left, bottom-top
    if not (28 <= dx <= 38 and 28 <= dy <= 38 and abs(dx-dy) <= 3):
        return False
    cx, cy = (right+left)/2, (bottom+top)/2
    # An unchecked control has a continuous colored rim and a pale,
    # unmarked interior. A fill, check mark, or partial icon fails closed.
    radius = (dx+dy)/4
    for step in range(24):
        angle = math.tau * step/24
        if not any(teal(round(cx + radius*k*math.cos(angle)), round(cy + radius*k*math.sin(angle)))
                   for k in (.86, .92, .98)):
            return False
    return all(min(rgb[x,y]) >= 220 for y in range(top,bottom+1) for x in range(left,right+1)
               if (x-cx)**2 + (y-cy)**2 < (.76*radius)**2)


def _storage_search_field(image: Image.Image, boxes: Sequence[OCRBox]) -> OCRBox:
    """Locate the storage search bar without ever straying onto row-1 Pokemon tiles."""
    active = _active_filter_box(image, boxes)
    if active is not None:
        return active

    ocr_search = next(
        (
            b for b in boxes
            if image.height * 0.12 <= b.center_y < image.height * 0.23
            and trade_local.norm(b.text).endswith("search")
        ),
        None,
    )
    if ocr_search is not None:
        return ocr_search

    picker = trade_local.picker_field(image, list(boxes))
    if picker is not None and image.height * 0.12 <= picker.center_y < image.height * 0.23:
        return picker

    tab_boxes = [
        b for b in boxes
        if b.center_y < image.height * 0.12
        and (
            trade_local.norm(b.text) in {"pokemon", "tags", "eggs"}
            or re.search(r"\d+\s*/\s*\d+|\(\d+\)", b.text)
        )
    ]
    counter_boxes = [b for b in tab_boxes if re.search(r"\d+\s*/\s*\d+|\(\d+\)", b.text)]
    if counter_boxes:
        anchor_bottom = max(b.y + b.height for b in counter_boxes)
        est_y = anchor_bottom + int(image.height * 0.065)
    elif tab_boxes:
        anchor_bottom = max(b.y + b.height for b in tab_boxes)
        est_y = anchor_bottom + int(image.height * 0.095)
    else:
        anchor_bottom = int(image.height * 0.105)
        est_y = int(image.height * 0.165)

    toggle = next(
        (b for b in boxes if "evolutionary" in trade_local.norm(b.text) and anchor_bottom < b.y < image.height * 0.28),
        None,
    )
    if toggle is not None:
        est_y = int((anchor_bottom + toggle.y) / 2)

    floor_y = int(image.height * 0.125)
    ceil_y = int(image.height * 0.215)
    cy = min(max(est_y, floor_y), ceil_y)
    cx = image.width // 2
    return OCRBox("search", 1.0, max(0, cx - 20), max(0, cy - 10), 40, 20)


# --- screen interaction & history -----------------------------------------------------


def history_path(now: datetime | None = None) -> Path:
    day = (now or datetime.now()).date().isoformat()
    return config_paths.state_dir() / "clear-pokemon" / f"history-{day}.jsonl"


def record_transfer(device_name: str, query: ClearQuery, species: str, now: datetime | None = None) -> None:
    path = history_path(now)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": (now or datetime.now()).isoformat(timespec="seconds"),
        "device": device_name,
        "query_chars": len(query.term),
        "species": species,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


async def close_detail(phone: starter.Phone) -> None:
    for _ in range(3):
        image, boxes = await phone.read()
        if starter.storage_open(boxes):
            return
        no_btn = starter.find_box(boxes, "no", exact=True)
        if no_btn is not None and starter.find_box(boxes, "transfer") is not None:
            await phone.tap_box(no_btn, image)
            await asyncio.sleep(1.5)
            continue
        point = starter.close_button(image)
        if point is not None:
            await phone.tap(point, image)
        elif not phone.ios:
            await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 4")
        else:
            await phone.tap([image.width // 2, int(image.height * 0.932)], image)
        await asyncio.sleep(1.8)


def _extract_android_edit_text(dump_xml: str) -> str | None:
    """Return text from the focused/search Android EditText in a uiautomator XML dump."""
    start = dump_xml.find("<hierarchy")
    end = dump_xml.rfind("</hierarchy>")
    if start < 0 or end < 0:
        return None
    try:
        root = ET.fromstring(dump_xml[start:end + len("</hierarchy>")])
    except ET.ParseError:
        return None
    for node in root.iter("node"):
        if "EditText" in node.attrib.get("class", ""):
            return node.attrib.get("text", "")
    return None


async def _android_edit_text(phone: starter.Phone) -> str | None:
    proc = await asyncio.to_thread(phone.target._adb, "exec-out", "uiautomator", "dump", "/dev/tty")
    return _extract_android_edit_text(getattr(proc, "stdout", "") or "")


def _post_wda_keys(wda_port: int, text: str, frequency: int = IOS_WDA_KEYS_FREQUENCY) -> None:
    with urllib.request.urlopen(f"http://127.0.0.1:{wda_port}/status", timeout=8) as resp:
        status = json.loads(resp.read().decode("utf-8"))
    wda_sid = status.get("sessionId")
    url = (
        f"http://127.0.0.1:{wda_port}/session/{wda_sid}/wda/keys"
        if wda_sid
        else f"http://127.0.0.1:{wda_port}/wda/keys"
    )
    payload = json.dumps({"value": list(text), "frequency": int(frequency)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def _ios_keyboard_check(image):
    """Locate Unity's cyan Done disc and its white check on the native keyboard toolbar."""
    w, h = image.size
    left, top, right, bottom = round(.65*w), round(.41*h), round(.88*w), round(.75*h)
    crop = image.convert("RGB").crop((left, top, right, bottom))
    r, g, b = crop.split()
    blue = ImageChops.multiply(
        r.point(lambda x: 255 if x < 24 else 0), g.point(lambda x: 255 if 119 < x < 170 else 0))
    blue = ImageChops.multiply(blue, b.point(lambda x: 255 if x > 239 else 0))
    bounds = blue.getbbox()
    if not bounds:
        return None
    x1, y1, x2, y2 = bounds
    size = x2 - x1
    if not .07*w < size < .145*w or abs((y2-y1)-size) > .12*size:
        return None
    if blue.histogram()[255] < size * size * .55:
        return None
    cx, cy = (x1+x2)//2, (y1+y2)//2
    radius = max(1, round(size*.26))
    centre = crop.crop((cx-radius,cy-radius,cx+radius,cy+radius))
    red, green, blue_channel = centre.split()
    darkest = ImageChops.darker(red, ImageChops.darker(green, blue_channel))
    white = sum(darkest.histogram()[226:])
    if not size*size*.006 <= white <= size*size*.18:
        return None
    return [left+cx, top+cy]


async def _ios_tap_done(phone: starter.Phone) -> None:
    dev = phone.target.device
    if hasattr(dev, "tap_done"):
        if await dev.tap_done():
            return
    else:
        try:
            from appium.webdriver.common.appiumby import AppiumBy
            driver = dev.driver
            for btn in await asyncio.to_thread(driver.find_elements, AppiumBy.ACCESSIBILITY_ID, "Done"):
                if await asyncio.to_thread(btn.is_displayed):
                    await asyncio.to_thread(btn.click)
                    await asyncio.sleep(2.0)
                    return
        except Exception:
            pass
    if not await phone.ios_keyboard():
        return
    image = await phone.look()
    check = _ios_keyboard_check(image)
    if check is None:
        from . import trade_saved_search
        if await trade_saved_search.dismiss_ios_return(phone):
            return
        raise ClearSafetyError(f"{phone.name}: the search keyboard's Done or Return button was not visible")
    await phone.tap(check, image)
    await asyncio.sleep(1)


async def _clear_storage_pill_if_active(phone: starter.Phone, image: Image.Image, boxes: Sequence[OCRBox]) -> tuple[Image.Image, list[OCRBox]]:
    active = _active_filter_box(image, boxes)
    if active is None:
        return image, list(boxes)
    await phone.tap([int(image.width * SEARCH_CLEAR_X), active.center_y], image)
    await asyncio.sleep(1.0)
    return await phone.read()


async def _enter_query_android(phone: starter.Phone, query: ClearQuery | str, image: Image.Image, boxes: Sequence[OCRBox], *, require_exact: bool = False) -> None:
    term = query.term if isinstance(query, ClearQuery) else str(query)
    image, boxes = await _clear_storage_pill_if_active(phone, image, boxes)
    field = _storage_search_field(image, boxes)
    await phone.tap_box(field, image)
    await asyncio.sleep(1.2)
    existing = await _android_edit_text(phone)
    if existing is None or (existing.strip() and existing.strip().casefold() != "search"):
        await trade_local.clear_field(phone, max(len(existing or "") + 20, 150))
        await asyncio.sleep(0.4)
        remaining = await _android_edit_text(phone)
        if remaining and remaining.strip().casefold() != "search":
            await trade_local.clear_field(phone, max(len(remaining) + 40, 150))

    expected = term.strip().casefold()
    for attempt in range(2):
        for pos in range(0, len(term), ANDROID_INPUT_BURST):
            burst = term[pos:pos + ANDROID_INPUT_BURST]
            escaped = burst.replace(" ", "%s").replace("'", "")
            await asyncio.to_thread(phone.target._adb, "shell", f"input text '{escaped}'")
            if pos + ANDROID_INPUT_BURST < len(term):
                await asyncio.sleep(0.25)

        deadline = time.monotonic() + 6.0
        typed: str | None = ""
        prev: str | None = None
        stable = 0
        while True:
            typed = await _android_edit_text(phone)
            if (typed is None and not require_exact) or (typed is not None and typed.strip().casefold() == expected):
                break
            if typed == prev:
                stable += 1
            else:
                stable = 0
                prev = typed
            if stable >= 2 or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.6)

        if (typed is None and not require_exact) or (typed is not None and typed.strip().casefold() == expected):
            break
        phone.say(f"Android search input mismatch (got {len(typed or '')}/{len(term)} chars); retrying ({attempt + 1}/2)")
        await trade_local.clear_field(phone, max(len(typed or "") + 20, 120))
    else:
        raise ClearSafetyError(f"{phone.name}: Android search box did not retain full {len(term)}-char query")

    await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 66")
    await asyncio.sleep(2.0)


async def _enter_query_ios(phone: starter.Phone, query: ClearQuery | str, image: Image.Image, boxes: Sequence[OCRBox], *, require_exact: bool = False) -> None:
    term = query.term if isinstance(query, ClearQuery) else str(query)
    image, boxes = await _clear_storage_pill_if_active(phone, image, boxes)
    field = _storage_search_field(image, boxes)
    await phone.tap_box(field, image)
    await asyncio.sleep(1.2)
    await trade_local._clear_ios_native_text(phone)
    dev = phone.target.device
    try:
        if hasattr(dev, "wda_keys"):
            await dev.wda_keys(term, IOS_WDA_KEYS_FREQUENCY)
        else:
            wda_port = int(dev.config.ios_device.get("wda_local_port") or ios_wda_cleanup.WDA_DEFAULT_PORT)
            await asyncio.to_thread(_post_wda_keys, wda_port, term, IOS_WDA_KEYS_FREQUENCY)
    except Exception as exc:
        raise ClearSafetyError(f"{phone.name}: iOS WDA fast typing failed: {exc}") from exc
    await asyncio.sleep(0.8)
    if require_exact:
        try:
            from . import trade_saved_search as saved
        except ImportError as exc:
            raise ClearSafetyError(f"{phone.name}: exact iOS review verification is unavailable") from exc
        typed = await saved.editor_text(phone)
        if typed is None:
            # Unity can draw its keyboard toolbar before the underlying field is focused.
            await asyncio.sleep(.6)
            typed = await saved.editor_text(phone)
        if typed is None or typed.casefold() != term.casefold():
            detail = "no native value" if typed is None else (
                f"{len(typed)} of {len(term)} characters, exact prefix={term.startswith(typed)}")
            phone.say(f"iOS review input returned {detail}; retrying with Paste")
            if not await saved.paste_ios_editor(phone, term):
                raise ClearSafetyError(f"{phone.name}: could not verify the full manual review expression ({detail})")
    await _ios_tap_done(phone)


def _manual_grid_count(image, boxes, term=None):
    if not starter.storage_open(boxes):
        return None
    if _active_filter_box(image, boxes) is None and not (
        term and any(.12 * image.height <= b.center_y <= .23 * image.height
                     and b.text.strip().casefold() == term.strip().casefold() for b in boxes)
    ):
        return None
    if any(starter.find_box(boxes, title, exact=True) for title in
           ("see more", "see less", "recent", "recommended", "favorite")):
        return None
    return result_count(boxes, image.height)


async def _manual_clipped_grid(phone, term, image, boxes):
    """Confirm a clipped result by reopening its native query, then leave its grid displayed."""
    count = _manual_grid_count(image, boxes, term)
    if count is None:
        return None
    await phone.tap_box(_storage_search_field(image, boxes), image)
    await asyncio.sleep(.7)
    if phone.ios:
        from .trade_saved_search import editor_text
        value = await editor_text(phone)
    else:
        value = await _android_edit_text(phone)
    if not value or value.casefold() != term.casefold():
        return None
    if phone.ios:
        await _ios_tap_done(phone)
    else:
        await trade_local.submit(phone)
    ok, image, boxes = await trade_local.settle(
        phone, lambda i, b: _manual_grid_count(i, b, term) == count, timeout=4)
    if ok:
        return count, image, boxes
    # Done on an unchanged iOS search can leave its Favorite drawer visible.
    # Reenter the verified text from a fresh grid and require the same count.
    image, boxes = await starter.open_storage(phone)
    enter = _enter_query_ios if phone.ios else _enter_query_android
    await enter(phone, term, image, boxes, require_exact=True)
    ok, image, boxes = await trade_local.settle(
        phone, lambda i, b: _manual_grid_count(i, b, term) == count, timeout=8)
    return (count, image, boxes) if ok else None


async def _apply_and_verify_query(
    phone: starter.Phone,
    query: ClearQuery | str,
    image: Image.Image | None = None,
    boxes: Sequence[OCRBox] | None = None,
    *, require_exact: bool = False,
) -> tuple[int, Image.Image, list[OCRBox], float]:
    """Enter one storage search query and fail closed unless OCR confirms it."""
    term = query.term if isinstance(query, ClearQuery) else str(query)
    started = time.monotonic()
    for attempt in range(2):
        if attempt or image is None or boxes is None:
            image, boxes = await starter.open_storage(phone)
        if phone.ios:
            await _enter_query_ios(phone, term, image, boxes, require_exact=require_exact)
        else:
            await _enter_query_android(phone, term, image, boxes, require_exact=require_exact)
        previous_count = None

        def submitted(i, b):
            nonlocal previous_count
            if not require_exact:
                return result_count(b, i.height) is not None and search_field_matches_query(b, term, i.height)
            count = _manual_grid_count(i, b, term)
            settled = count is not None and count == previous_count
            previous_count = count
            return settled

        ok, img, bx = await trade_local.settle(phone, submitted, timeout=10.0)
        if ok:
            if require_exact and not search_field_matches_query(bx, term, img.height):
                confirmed = await _manual_clipped_grid(phone, term, img, bx)
                if confirmed is not None:
                    return (*confirmed, time.monotonic() - started)
            else:
                count = result_count(bx, img.height)
                if count is not None:
                    return count, img, bx, time.monotonic() - started
        image, boxes = img, bx
    raise ClearSafetyError(
        f"{phone.name}: single-query filter ({len(term)} chars) could not be verified "
        f"(saved {phone.save(image, 'unverified-filter')})"
    )


async def run_device(
    spec: pokemon_fleet.DeviceSpec,
    query: ClearQuery | str,
    protected_tags: Sequence[str],
    args: argparse.Namespace,
    *, on_review=None,
) -> tuple[bool, str]:
    if isinstance(query, str) and (not args.open or not query.strip()):
        raise ClearSafetyError("A raw storage search is allowed only for a nonempty manual review")
    term = query if isinstance(query, str) else query.term
    transferred = 0
    skipped = 0
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        phone = starter.Phone(spec.name, target, args.shots, args.dry_run or args.open)
        try:
            image, boxes = await starter.open_storage(phone)
            count, image, boxes, elapsed = await _apply_and_verify_query(
                phone, query, image, boxes, require_exact=isinstance(query, str))
            samples = sample_visible_species(image, boxes)
            sample_note = f"; visible: {', '.join(samples)}" if samples else ""

            if args.open:
                phone.say(f"1 query ({len(term)} chars, {elapsed:.1f}s): {count} match(es){sample_note} [left open for review]")
                if on_review is not None:
                    await on_review(phone, image)
                return True, f"{count} match(es) in 1 query ({len(term)} chars, {elapsed:.1f}s){sample_note} [left open; 0 transferred]"

            if args.dry_run:
                phone.say(f"1 query ({len(query.term)} chars, {elapsed:.1f}s): {count} match(es){sample_note} [dry-run]")
                await _clear_storage_pill_if_active(phone, image, boxes)
                await phone.go_home()
                return True, f"{count} match(es) in 1 query ({len(query.term)} chars, {elapsed:.1f}s){sample_note} [dry-run; 0 transferred]"

            if count == 0:
                phone.say(f"1 query ({len(query.term)} chars, {elapsed:.1f}s): 0 matches")
                await phone.go_home()
                return True, f"0 matches in 1 query ({len(query.term)} chars, {elapsed:.1f}s)"

            phone.say(f"1 query ({len(query.term)} chars, {elapsed:.1f}s): {count} match(es){sample_note}")
            skip_index = 0
            while count > 0 and transferred < args.limit:
                image, boxes = await phone.read()
                if not starter.storage_open(boxes):
                    raise ClearSafetyError(f"{phone.name}: left storage unexpectedly")
                if not search_field_matches_query(boxes, query.term, image.height):
                    raise ClearSafetyError(
                        f"{phone.name}: search filter changed mid-run (saved {phone.save(image, 'filter-changed')})"
                    )
                cur_count = result_count(boxes, image.height)
                if cur_count is not None:
                    count = cur_count
                    if count == 0:
                        break
                tiles = safe_grid_tiles(image, boxes)
                if skip_index >= len(tiles):
                    break
                tile = tiles[skip_index]
                await phone.tap([tile.center_x, tile.center_y + int(image.height * TILE_RISE)], image)
                ok, image, boxes = await phone.settle(detail_page, timeout=8.0)
                if not ok:
                    await close_detail(phone)
                    skip_index += 1
                    skipped += 1
                    continue

                allowed, species_note = detail_species_allowed(boxes, image, query.allowed_names)
                if not allowed:
                    phone.say(f"REFUSED slot {skip_index + 1}: {species_note} (saved {phone.save(image, 'refused-species')})")
                    await close_detail(phone)
                    skip_index += 1
                    skipped += 1
                    continue

                blocked = detail_safety_block(boxes, image, protected_tags)
                if blocked:
                    phone.say(f"REFUSED slot {skip_index + 1}: {blocked} (saved {phone.save(image, 'refused-detail')})")
                    await close_detail(phone)
                    skip_index += 1
                    skipped += 1
                    continue

                await phone.tap([int(image.width * MENU_POINT[0]), int(image.height * MENU_POINT[1])], image)
                ok, image, boxes = await phone.settle(
                    lambda _i, b: starter.find_box(b, "appraise", exact=True) is not None,
                    timeout=6.0,
                )
                transfer_btn = starter.find_box(boxes, "transfer", exact=True) if ok else None
                if transfer_btn is None:
                    phone.say(f"skipped slot {skip_index + 1} ({species_note}): no TRANSFER menu row")
                    await close_detail(phone)
                    skip_index += 1
                    skipped += 1
                    continue

                await phone.tap_box(transfer_btn, image)
                ok, image, boxes = await phone.settle(
                    lambda _i, b: starter.find_box(b, "yes", exact=True) is not None
                    and starter.find_box(b, "no", exact=True) is not None,
                    timeout=6.0,
                )
                if not ok:
                    await close_detail(phone)
                    skip_index += 1
                    skipped += 1
                    continue

                dialog_block = dialog_safety_block(boxes)
                if dialog_block is not None:
                    phone.say(f"REFUSED dialog slot {skip_index + 1} ({species_note}): {dialog_block} "
                              f"(saved {phone.save(image, 'refused-dialog')})")
                    no_btn = starter.find_box(boxes, "no", exact=True)
                    if no_btn is not None:
                        await phone.tap_box(no_btn, image)
                        await asyncio.sleep(1.5)
                    await close_detail(phone)
                    skip_index += 1
                    skipped += 1
                    continue

                yes_btn = starter.find_box(boxes, "yes", exact=True)
                assert yes_btn is not None
                await phone.tap_box(yes_btn, image)
                ok, image, boxes = await phone.settle(
                    lambda i, b: starter.storage_open(b)
                    or (starter.find_box(b, "yes", exact=True) is not None
                        and starter.find_box(b, "no", exact=True) is not None),
                    timeout=10.0,
                )
                if not ok:
                    raise ClearSafetyError(
                        f"{phone.name}: unexpected screen after YES (saved {phone.save(image, 'after-yes')})"
                    )
                if not starter.storage_open(boxes):
                    no_btn = starter.find_box(boxes, "no", exact=True)
                    shot = phone.save(image, "second-dialog")
                    if no_btn is not None:
                        await phone.tap_box(no_btn, image)
                        await asyncio.sleep(1.5)
                    await close_detail(phone)
                    raise ClearSafetyError(
                        f"{phone.name}: second warning dialog appeared after YES ({shot}); stopped"
                    )

                dropped, image, boxes = await trade_local.settle(
                    phone,
                    lambda i, b: (result_count(b, i.height) is not None and result_count(b, i.height) < count),
                    timeout=6.0,
                )
                if not dropped:
                    raise ClearSafetyError(
                        f"{phone.name}: storage count did not drop after transfer "
                        f"(saved {phone.save(image, 'count-unchanged')})"
                    )
                count = result_count(boxes, image.height) or 0
                transferred += 1
                record_transfer(spec.name, query, species_note)
                phone.say(f"transferred #{transferred} ({species_note}); {count} remaining")
            await close_detail(phone)
            await phone.go_home()
        finally:
            await target.close()

    return True, f"transferred {transferred} (skipped {skipped})"


# --- CLI & fleet dispatch -------------------------------------------------------------


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", nargs="+", help="only these fleet device names")
    parser.add_argument("--exclude-devices", nargs="+", default=[], help="fleet device names to skip")
    parser.add_argument("--config-file", type=Path, default=None, help=f"path to {CONFIG_NAME}")
    parser.add_argument("--query", default=None, help="override clearing query to audit and run")
    parser.add_argument("--keep-rare", action="store_true", help="also exclude regional/rare collectible dex numbers")
    parser.add_argument("--shots", type=Path, default=None, help="directory for safety screenshots")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--review", action="store_true", help="audit clearing list locally and print report; touch no phone")
    action.add_argument("--plan", action="store_true", help="list attached devices and single-query plan; touch no phone")
    action.add_argument("--dry-run", action="store_true", help="apply single query in storage and count matches; delete nothing")
    action.add_argument("--open", action="store_true", help="apply full verified query in storage and leave it open on every phone for review; delete nothing")
    action.add_argument("--count", type=int, help="transfer at most this many matches per phone")
    action.add_argument("--all", action="store_true", help="transfer all safe matches up to --max-transfers per phone")

    parser.add_argument("--confirm-delete", action="store_true",
                        help="required with --count or --all because transfers cannot be undone")
    parser.add_argument("--max-transfers", type=int, default=DEFAULT_MAX_TRANSFERS,
                        help=f"safety cap per phone for --all (default: {DEFAULT_MAX_TRANSFERS})")
    args = parser.parse_args(arguments)

    if args.count is not None and args.count < 1:
        parser.error("--count must be at least 1")
    if args.max_transfers < 1:
        parser.error("--max-transfers must be at least 1")
    if (args.count is not None or args.all) and not args.confirm_delete:
        parser.error("Transfers cannot be undone. Re-run with --confirm-delete after --review and --dry-run.")
    args.limit = args.count if args.count is not None else args.max_transfers
    args.shots = (args.shots or config_paths.state_dir() / "clear-pokemon" / "shots").expanduser()
    return args


async def attached_devices(args: argparse.Namespace) -> list[pokemon_fleet.DeviceSpec]:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    wanted = set(args.devices or [])
    excluded = set(args.exclude_devices or [])
    if wanted:
        unknown = sorted(wanted - set(fleet.devices))
        if unknown:
            raise pokemon_fleet.FleetError(f"Unknown configured device(s): {', '.join(unknown)}")
    specs = [
        spec for spec in fleet.devices.values()
        if spec.enabled and (not wanted or spec.name in wanted) and spec.name not in excluded
    ]
    androids = await screen_router.attached_androids(specs)
    iphones = pokemon_fleet.ios_connected_udids()
    return [
        spec for spec in specs
        if spec.identifier in (androids if spec.platform == "android" else iphones)
    ]


async def run(args: argparse.Namespace) -> int:
    query, audit, protected_tags = build_clear_plan(
        config_file=args.config_file,
        query=args.query,
        exclude_rare=args.keep_rare,
    )
    if args.review:
        if audit is not None:
            print(format_audit_report(audit), flush=True)
        else:
            print(
                f"Pre-audited single-query clear plan: {len(query.dex_nums)} species, "
                f"{len(query.term)} chars, {len(query.filters)} safety filters; "
                f"protected tags: {', '.join(protected_tags)}",
                flush=True,
            )
        return 0

    here = await attached_devices(args)
    if not here:
        print("No fleet phone is attached to this computer", flush=True)
        return 0

    if args.plan:
        names = ", ".join(f"{s.name} [{s.platform}]" for s in here)
        print(f"[clear] devices ({len(here)}): {names}", flush=True)
        print(
            f"[clear] query: 1 query ({len(query.term)} chars); species: {len(query.dex_nums)}; "
            f"filters: {len(query.filters)} ({'&'.join(query.filters)})",
            flush=True,
        )
        print(f"[clear] protected tags: {', '.join(protected_tags)}", flush=True)
        return 0

    async def _run_one(spec: pokemon_fleet.DeviceSpec) -> tuple[str, bool, str]:
        try:
            ok, detail = await run_device(spec, query, protected_tags, args)
            return spec.name, ok, detail
        except (ClearSafetyError, starter.StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
            return spec.name, False, str(exc)
        except Exception as exc:
            return spec.name, False, f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(_run_one(spec) for spec in here))
    failed = False
    for name, ok, detail in results:
        failed |= not ok
        print(f"[{name}] {'done' if ok else 'PROBLEM'}: {detail}", flush=True)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    from . import fleet_entrypoint

    argv = fleet_entrypoint.apply_registry_override(sys.argv[1:])
    review_or_help = any(v in {"--help", "-h", "--review"} for v in argv)
    if not fleet_entrypoint.local_requested(argv) and not review_or_help:
        raise SystemExit(
            fleet_entrypoint.run_all_machines("clear_pokemon.py", fleet_entrypoint.clean_scope_flags(argv))
        )
    if not review_or_help:
        fleet_entrypoint.apply_local_environment()
    raise SystemExit(main(fleet_entrypoint.clean_scope_flags(argv)))
