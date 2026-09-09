#!/usr/bin/env python3
"""PvPoke-backed roster ranking for automatic GBL party preparation."""

from __future__ import annotations

from . import config_paths

from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable
import urllib.request

from . import gbl_strategy


CACHE_DIR = config_paths.state_dir() / "cache" / "pvpoke"
GAMEMASTER_URL = (
    "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/gamemaster.json"
)
RANKING_URL = (
    "https://raw.githubusercontent.com/pvpoke/pvpoke/master/"
    "src/data/rankings/all/overall/rankings-{cap}.json"
)
CACHE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RosterPokemon:
    name: str
    cp: int
    x: int = 0
    y: int = 0


@dataclass(frozen=True)
class PvPMetaPokemon:
    species_id: str
    name: str
    types: tuple[str, ...]
    score: float
    bulk: float
    fast_move: str
    charged_moves: tuple[str, ...]

    def team_member(self, cp: int, cp_cap: int = 1500) -> gbl_strategy.TeamMember:
        # PvPoke assumes the species is built at the league cap.  Scale both
        # rating and bulk for visibly under-levelled owned copies so a 1,300 CP
        # meta name does not beat a battle-ready 1,495 CP alternative.
        readiness = min(1.0, cp / (cp_cap * 0.985))
        return gbl_strategy.TeamMember(
            self.name,
            cp,
            self.types,
            self.fast_move,
            self.charged_moves,
            self.bulk * readiness,
            self.score * readiness,
        )


@dataclass(frozen=True)
class RankedTeam:
    team: gbl_strategy.BattleTeam
    score: float
    explanation: str


def _display_move(move_id: str) -> str:
    return move_id.replace("_", " ").title()


def _meta_key(value: str) -> str:
    marker = " shadow" if "shadow" in value.casefold() else ""
    return gbl_strategy.canonical_name(value) + marker


def _cache_json(name: str, url: str) -> Any:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    fresh = path.is_file() and time.time() - path.stat().st_mtime < CACHE_SECONDS
    if not fresh:
        request = urllib.request.Request(url, headers={"User-Agent": "PokemonAutomation/1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read()
        # A successful parse happens before replacing the useful old cache.
        json.loads(payload)
        path.write_bytes(payload)
    return json.loads(path.read_text())


def build_meta_index(gamemaster: dict[str, Any], rankings: list[dict[str, Any]]) -> dict[str, PvPMetaPokemon]:
    pokemon = {
        str(item.get("speciesId")): item
        for item in gamemaster.get("pokemon", [])
        if isinstance(item, dict) and isinstance(item.get("speciesId"), str)
    }
    index: dict[str, PvPMetaPokemon] = {}
    for rank in rankings:
        if not isinstance(rank, dict):
            continue
        species_id = str(rank.get("speciesId", ""))
        master = pokemon.get(species_id)
        if master is None:
            # Rankings occasionally suffix a shadow form while the game master
            # keeps the base record plus a tag.
            master = pokemon.get(re.sub(r"_(?:shadow|xl)$", "", species_id))
        if master is None:
            continue
        name = str(rank.get("speciesName") or master.get("speciesName") or species_id)
        stats = rank.get("stats") if isinstance(rank.get("stats"), dict) else {}
        moveset = rank.get("moveset") if isinstance(rank.get("moveset"), list) else []
        types = tuple(
            str(value).title()
            for value in master.get("types", [])
            if value and str(value).casefold() != "none"
        )
        if not types:
            continue
        record = PvPMetaPokemon(
            species_id,
            name,
            types,
            float(rank.get("score", 0)),
            float(stats.get("product", 0)) / 22.0,
            _display_move(str(moveset[0])) if moveset else "Fast Move",
            tuple(_display_move(str(value)) for value in moveset[1:]),
        )
        keys = {_meta_key(name), _meta_key(species_id.replace("_", " "))}
        if not any(marker in species_id for marker in ("_shadow", "_xl")):
            keys.add(gbl_strategy.canonical_name(str(master.get("speciesName", ""))))
        for key in keys:
            if key and (key not in index or record.score > index[key].score):
                index[key] = record
    return index


@lru_cache(maxsize=4)
def load_meta_index(cp_cap: int = 1500) -> dict[str, PvPMetaPokemon]:
    gamemaster = _cache_json("gamemaster.json", GAMEMASTER_URL)
    rankings = _cache_json(
        f"rankings-{cp_cap}.json",
        RANKING_URL.format(cap=cp_cap),
    )
    if not isinstance(gamemaster, dict) or not isinstance(rankings, list):
        raise ValueError("PvPoke returned an unexpected data shape")
    return build_meta_index(gamemaster, rankings)


@lru_cache(maxsize=1)
def load_move_types() -> dict[str, str]:
    """Move name -> attack type, from the cached game master.

    Keyed on both the raw moveId and the display name a PvPMetaPokemon carries,
    each normalised the way gbl_strategy normalises species names, so callers
    can look up "Dragon Tail", "DRAGON_TAIL" or "dragon tail" alike.  Used by
    gbl_strategy.threat_types: a Pokemon's own typing does not tell you what it
    hits you with, and Lugia -- Psychic/Flying, so 1.0x into Dragon/Ice -- leads
    with Dragon Tail.
    """

    gamemaster = _cache_json("gamemaster.json", GAMEMASTER_URL)
    if not isinstance(gamemaster, dict):
        raise ValueError("PvPoke returned an unexpected data shape")
    types: dict[str, str] = {}
    for move in gamemaster.get("moves", []):
        if not isinstance(move, dict):
            continue
        move_type = str(move.get("type") or "").title()
        if not move_type or move_type.casefold() == "none":
            continue
        for label in (move.get("moveId"), move.get("name")):
            key = gbl_strategy.canonical_name(str(label or ""))
            if key:
                types[key] = move_type
    return types


LEAGUE_CAPS = (1500, 2500, 10000)

# The cap of the league actually being played, set from the strategy config by
# gbl_strategy.load_strategy_profile().  It stays at the Great League default
# until a profile is loaded so anything importing this module standalone keeps
# its previous behaviour.
ACTIVE_CAP = 1500


def cap_for_league(league: str) -> int:
    """PvPoke ranking cap for a league name, Great League when unrecognised."""
    name = str(league).strip().casefold()
    if "master" in name:
        return 10000
    if "ultra" in name:
        return 2500
    return 1500


def set_active_cap(cap: int) -> None:
    global ACTIVE_CAP
    ACTIVE_CAP = cap


def load_species_index(cp_cap: int | None = None) -> dict[str, PvPMetaPokemon]:
    """Every ranked species, with the played league's own records winning.

    Name and type resolution must recognise anything that can appear on screen,
    but a species carries league-specific ratings, so the active cap's record is
    the one that has to survive the merge.  Reading only the 1500 rankings while
    playing Master League left Zacian unknown to both the party reader and the
    matchup lookup, which is why a Master roster was ranked as if it were Great
    League.
    """
    cap = ACTIVE_CAP if cp_cap is None else cp_cap
    merged: dict[str, PvPMetaPokemon] = {}
    for other in LEAGUE_CAPS:
        if other == cap:
            continue
        try:
            merged.update(load_meta_index(other))
        except (OSError, TypeError, ValueError):
            continue
    merged.update(load_meta_index(cap))
    return merged


def match_meta_name(text: str, index: dict[str, PvPMetaPokemon]) -> PvPMetaPokemon | None:
    normalized = _meta_key(text)
    if normalized in index:
        return index[normalized]
    # Pokemon GO adds symbols/tags around names; exact contained species names
    # are safe only when reasonably long (avoid matching "Muk" inside UI text).
    matches = [record for key, record in index.items() if len(key) >= 5 and key in normalized]
    return max(matches, key=lambda record: len(record.name)) if matches else None


def parse_roster_page(boxes: Iterable[Any], index: dict[str, PvPMetaPokemon]) -> list[RosterPokemon]:
    """Pair each visible name with the nearest CP label above it in its column."""

    names: list[tuple[Any, PvPMetaPokemon]] = []
    cp_boxes: list[tuple[Any, int]] = []
    for box in boxes:
        text = str(getattr(box, "text", ""))
        match = re.fullmatch(
            r"\s*(?:[cC]?[pP]|[^A-Za-z0-9]{0,3})?\s*([0-9]{3,4})\s*",
            text,
        )
        if match:
            cp_boxes.append((box, int(match.group(1))))
            continue
        record = match_meta_name(text, index)
        if record is not None:
            names.append((box, record))

    found: list[RosterPokemon] = []
    for name_box, record in names:
        above = [
            (cp_box, cp)
            for cp_box, cp in cp_boxes
            if cp_box.center_y < name_box.center_y
            and abs(cp_box.center_x - name_box.center_x) < max(180, name_box.width * 2)
        ]
        if not above:
            continue
        cp_box, cp = min(
            above,
            key=lambda item: (
                abs(name_box.center_x - item[0].center_x),
                name_box.center_y - item[0].center_y,
            ),
        )
        if name_box.center_y - cp_box.center_y > 420:
            continue
        found.append(RosterPokemon(record.name, cp, name_box.center_x, name_box.center_y))
    return found


def _team_coverage_score(members: tuple[gbl_strategy.TeamMember, ...]) -> float:
    shared_penalty = 0.0
    offensive = set()
    for attack_type in gbl_strategy.gbl_evaluator.TYPES:
        weak = 0
        for member in members:
            weakness = 1.0
            for own_type in member.types:
                weakness *= gbl_strategy.gbl_evaluator.TYPE_CHART.get(attack_type, {}).get(own_type, 1.0)
            if weakness > 1.2:
                weak += 1
        if weak >= 2:
            shared_penalty += 8.0 if weak == 2 else 22.0
    for member in members:
        offensive.update(member.types)
    return min(18.0, len(offensive) * 3.0) - shared_penalty


def recommend_team(
    roster: Iterable[RosterPokemon],
    index: dict[str, PvPMetaPokemon],
    *,
    candidate_limit: int = 30,
    always_include: tuple[str, ...] = (),
) -> RankedTeam:
    """Choose a high-ranking, bulk-aware team without shared hard weaknesses."""

    best_owned: dict[str, RosterPokemon] = {}
    for item in roster:
        record = match_meta_name(item.name, index)
        if record is None:
            continue
        key = record.species_id
        previous = best_owned.get(key)
        if previous is None or item.cp > previous.cp:
            best_owned[key] = item
    ranked = sorted(
        best_owned.values(),
        key=lambda item: match_meta_name(item.name, index).score,  # type: ignore[union-attr]
        reverse=True,
    )[:candidate_limit]

    required_keys = {gbl_strategy.canonical_name(name) for name in always_include if name}
    owned_must_have = [
        item for item in best_owned.values()
        if gbl_strategy.canonical_name(item.name) in required_keys
    ]
    ranked_set = set(ranked)
    for item in owned_must_have:
        if item not in ranked_set:
            ranked.append(item)

    if len(ranked) < 3:
        raise ValueError("Roster scan found fewer than three PvPoke-ranked Pokemon")

    candidate_trios = [
        trio for trio in combinations(ranked, 3)
        if all(
            gbl_strategy.canonical_name(req.name) in {gbl_strategy.canonical_name(item.name) for item in trio}
            for req in owned_must_have
        )
    ]
    trios_to_evaluate = candidate_trios if candidate_trios else list(combinations(ranked, 3))

    best_members: tuple[gbl_strategy.TeamMember, ...] | None = None
    best_score = -1e9
    for trio in trios_to_evaluate:
        members = tuple(
            match_meta_name(item.name, index).team_member(item.cp)  # type: ignore[union-attr]
            for item in trio
        )
        base = sum(member.rating for member in members) / 3.0
        bulk = sum(member.bulk for member in members) / 3.0
        score = base * 0.75 + min(100.0, bulk) * 0.10 + _team_coverage_score(members)
        if score > best_score:
            best_members, best_score = members, score
    assert best_members is not None

    # Put a sturdy generalist first, the bulkiest member second as safe switch,
    # and preserve the remaining coverage specialist as closer.
    lead = max(best_members, key=lambda item: item.rating + item.bulk * 0.08)
    remaining = [item for item in best_members if item is not lead]
    safe = max(remaining, key=lambda item: item.bulk)
    closer = next(item for item in remaining if item is not safe)
    team = gbl_strategy.BattleTeam((lead, safe, closer))
    explanation = (
        f"PvPoke/coverage score {best_score:.1f}: "
        f"{lead.name} lead, {safe.name} safe switch, {closer.name} closer"
    )
    return RankedTeam(team, round(best_score, 1), explanation)
