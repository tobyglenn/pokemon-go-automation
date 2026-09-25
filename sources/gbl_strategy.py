#!/usr/bin/env python3
"""Pure strategy decisions shared by the Android and iOS GBL drivers.

This module deliberately contains no phone I/O.  Menu OCR, taps, and screenshots
belong to the platform drivers; type effectiveness, league ranking, team loading,
and switch decisions live here so they can be tested without risking a battle.
"""

from __future__ import annotations

from . import config_paths

from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
import math
from pathlib import Path
import re
import time
from typing import Any, Iterable, Sequence

import yaml

from . import gbl_evaluator


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STRATEGY_CONFIG = config_paths.default_config("gbl-strategy.yaml")


@dataclass(frozen=True)
class TeamMember:
    name: str
    cp: int
    types: tuple[str, ...]
    fast_move: str
    charged_moves: tuple[str, ...]
    bulk: float
    rating: float


@dataclass(frozen=True)
class BattleTeam:
    members: tuple[TeamMember, TeamMember, TeamMember]

    def index_of(self, name: str) -> int | None:
        wanted = canonical_name(name)
        for index, member in enumerate(self.members):
            if canonical_name(member.name) == wanted:
                return index
        return None


@dataclass(frozen=True)
class StrategySettings:
    enabled: bool = True
    minimum_score_gain: float = 12.0
    switch_cooldown_seconds: float = 62.0
    opponent_ocr_every_reads: int = 3
    preferred_league: str = "auto"
    auto_build_team: bool = True
    auto_select_team: bool = True
    roster_scan_pages: int = 8
    always_include: tuple[str, ...] = ("Mewtwo",)
    always_include_leagues: tuple[str, ...] = ("master", "mega")


@dataclass(frozen=True)
class StrategyProfile:
    team: BattleTeam
    settings: StrategySettings = StrategySettings()


@dataclass(frozen=True)
class SwitchDecision:
    target_index: int | None
    opponent_name: str | None
    current_score: float
    target_score: float
    reason: str

    @property
    def should_switch(self) -> bool:
        return self.target_index is not None


# How many times a reserve may be tapped without the battle header confirming
# it before the loop gives up on that target for the rest of the battle.
SWITCH_VERIFY_ATTEMPTS = 2


@dataclass
class BattleMemory:
    """Small amount of state the screen alone cannot reliably provide."""

    active_index: int = 0
    alive: list[bool] = field(default_factory=lambda: [True, True, True])
    opponent_name: str | None = None
    pending_index: int | None = None
    last_switch_at: float = -1e9
    failed_switches: dict[int, int] = field(default_factory=dict)

    def cooldown_ready(self, settings: StrategySettings, now: float | None = None) -> bool:
        here = time.monotonic() if now is None else now
        return here - self.last_switch_at >= settings.switch_cooldown_seconds

    def begin_forced_switch(self) -> None:
        self.alive[self.active_index] = False
        self.pending_index = None

    def request_switch(self, index: int) -> None:
        self.pending_index = index

    def record_failed_switch(self, index: int) -> int:
        """Count a switch that was tapped but never showed up on screen."""
        self.pending_index = None
        count = self.failed_switches.get(index, 0) + 1
        self.failed_switches[index] = count
        return count

    def switch_blocked(self, index: int) -> bool:
        """Whether this reserve has refused often enough to stop asking.

        Nothing in the battle loop reacts to a failed switch, so without a cap
        the same decision is recomputed on every OCR cycle and the phone spends
        the whole battle tapping a reserve card instead of attacking.
        """
        return self.failed_switches.get(index, 0) >= SWITCH_VERIFY_ATTEMPTS

    def commit_switch(self, index: int | None = None, now: float | None = None) -> int:
        target = self.pending_index if index is None else index
        if target is None:
            raise ValueError("No pending switch to commit")
        self.active_index = target
        self.pending_index = None
        self.failed_switches.clear()
        self.last_switch_at = time.monotonic() if now is None else now
        return target


@dataclass(frozen=True)
class LeagueChoice:
    name: str
    score: float
    x: int
    y: int
    cp_cap: int | None


def canonical_name(value: str) -> str:
    value = value.casefold().replace("pokémon", "pokemon")
    value = re.sub(r"\b(?:shadow|purified|lucky)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def league_matches(wanted: str, name: str | None) -> bool:
    """Whether a league card or open set counts as the configured league.

    The game rotates editions of a league -- ``MASTER LEAGUE: MEGA EDITION``
    stands in for Master League on the days plain Master League is not on the
    list -- and a configured ``Master League`` means that league's CP rules,
    not that exact wording. The wanted name has to appear as whole words, so
    a hypothetical ``Grandmaster League`` is not mistaken for Master League.
    """
    if not name:
        return False
    wanted_words = canonical_name(wanted).split()
    if not wanted_words:
        return False
    words = canonical_name(name).split()
    span = len(wanted_words)
    return any(
        words[start:start + span] == wanted_words
        for start in range(len(words) - span + 1)
    )


def adopt_league(profile: "StrategyProfile", league_name: str) -> "StrategyProfile":
    """Re-aim a profile at the league the game is actually offering.

    The configured league is a preference, not a promise.  Pokemon GO rotates
    its list, and on a day Master League is nowhere on it a phone that insists
    on Master plays nothing at all -- which is how three phones spent a whole
    run entering Great League sets, backing out of them, and stopping.  Playing
    what is on offer is worth more than playing nothing.

    The name is not the only thing that has to move: the party builder and
    every meta lookup read the CP cap off these settings, so a Master team
    carried into a Great League set is three Pokemon the game will not let in.
    """
    settings = replace(profile.settings, preferred_league=league_name)
    try:
        from . import gbl_meta
        gbl_meta.set_active_cap(gbl_meta.cap_for_league(league_name))
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return StrategyProfile(profile.team, settings)


def _member_from_mapping(value: Any, label: str) -> TeamMember:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise ValueError(f"{label} needs a Pokemon name")
    name = value["name"].strip()
    cp = value.get("cp", 1500)
    if type(cp) is not int or cp < 10:
        raise ValueError(f"{label}.cp must be a positive integer")
    meta = gbl_evaluator.lookup_meta_pokemon(name)
    configured_types = value.get("types")
    types = configured_types if isinstance(configured_types, list) else meta.types
    if not types or not all(isinstance(item, str) and item in gbl_evaluator.TYPES for item in types):
        raise ValueError(f"{label}.types contains an unknown Pokemon type")
    fast_move = value.get("fast_move", meta.fast_move)
    charged_moves = value.get("charged_moves", meta.charged_moves)
    if not isinstance(fast_move, str) or not isinstance(charged_moves, list):
        raise ValueError(f"{label} has invalid move data")
    return TeamMember(
        name=meta.name,
        cp=cp,
        types=tuple(types),
        fast_move=fast_move,
        charged_moves=tuple(str(item) for item in charged_moves),
        bulk=float(value.get("bulk", meta.bulk_rating)),
        rating=float(value.get("score", value.get("rating", meta.auto_battle_rating))),
    )


def load_strategy_profile(
    path: Path = DEFAULT_STRATEGY_CONFIG,
    device_name: str | None = None,
) -> StrategyProfile:
    """Load a team, optionally using a per-device override.

    ``devices.<name>.team`` overrides the root team.  This matters because the
    two accounts do not necessarily own the same roster.
    """

    try:
        root = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ValueError(f"GBL strategy config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid GBL strategy YAML in {path}: {exc}") from exc
    if not isinstance(root, dict):
        raise ValueError(f"GBL strategy config must be an object: {path}")

    selected = root
    devices = root.get("devices")
    if device_name and isinstance(devices, dict) and isinstance(devices.get(device_name), dict):
        selected = {**root, **devices[device_name]}
    team = selected.get("team")
    if not isinstance(team, dict):
        raise ValueError(f"{path} needs team.lead, team.safe_switch and team.closer")
    order = ("lead", "safe_switch", "closer")
    members = tuple(_member_from_mapping(team.get(role), f"team.{role}") for role in order)

    switch = root.get("switching") if isinstance(root.get("switching"), dict) else {}
    if isinstance(selected.get("switching"), dict):
        switch = {**switch, **selected["switching"]}
    building = (
        root.get("team_building")
        if isinstance(root.get("team_building"), dict)
        else {}
    )
    if isinstance(selected.get("team_building"), dict):
        building = {**building, **selected["team_building"]}
    raw_include = building.get("always_include", ("Mewtwo",))
    if isinstance(raw_include, str):
        always_include = (raw_include,)
    elif isinstance(raw_include, (list, tuple)):
        always_include = tuple(str(x) for x in raw_include)
    else:
        always_include = ("Mewtwo",)

    raw_leagues = building.get("always_include_leagues", ("master", "mega"))
    if isinstance(raw_leagues, str):
        always_include_leagues = (raw_leagues,)
    elif isinstance(raw_leagues, (list, tuple)):
        always_include_leagues = tuple(str(x) for x in raw_leagues)
    else:
        always_include_leagues = ("master", "mega")

    settings = StrategySettings(
        enabled=bool(switch.get("enabled", True)),
        minimum_score_gain=float(switch.get("minimum_score_gain", 12.0)),
        switch_cooldown_seconds=float(switch.get("cooldown_seconds", 62.0)),
        opponent_ocr_every_reads=max(1, int(switch.get("opponent_ocr_every_reads", 3))),
        preferred_league=str(selected.get("preferred_league", root.get("preferred_league", "auto"))),
        auto_build_team=bool(building.get("enabled", True)),
        auto_select_team=bool(building.get("select_team", True)),
        roster_scan_pages=max(
            1,
            int(building.get("roster_scan_pages", 8)),
        ),
        always_include=always_include,
        always_include_leagues=always_include_leagues,
    )
    # Every meta lookup downstream is league-specific, and this is the one place
    # the configured league is known to both platforms.
    try:
        from . import gbl_meta
        gbl_meta.set_active_cap(gbl_meta.cap_for_league(settings.preferred_league))
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return StrategyProfile(BattleTeam(members), settings)  # type: ignore[arg-type]


def active_always_include(
    settings: StrategySettings,
    league_name: str = "",
    cp_cap: int | None = None,
) -> tuple[str, ...]:
    """Return Pokemon that must be included for the current league.

    Specifically, Mewtwo and other mega/master specialists should only be
    forced into the rotation when playing in Mega Master League / Master League
    (cp_cap >= 10000 or league name matching 'master' or 'mega').
    """
    if not settings.always_include:
        return ()

    league_str = str(league_name or settings.preferred_league or "").casefold()
    is_mega_or_master = (cp_cap is not None and cp_cap >= 10000) or any(
        kw in league_str for kw in ("master", "mega")
    )
    allowed_leagues = [kw.casefold() for kw in settings.always_include_leagues]
    if allowed_leagues:
        if not any(kw in league_str for kw in allowed_leagues) and not (
            "master" in allowed_leagues and is_mega_or_master
        ):
            return ()
    elif not is_mega_or_master:
        return ()

    return settings.always_include


def team_from_party_ocr(
    boxes: Iterable[Any], expected_names: Iterable[str] = ()
) -> BattleTeam | None:
    """Read the three visible names and CP values from CHOOSE YOUR PARTY."""

    names: list[tuple[int, str]] = []
    cps: list[tuple[int, int]] = []
    known = list(gbl_evaluator.POKEMON_DATABASE)
    try:
        from . import gbl_meta
        known.extend(meta.name for meta in gbl_meta.load_species_index().values())
    except (OSError, TypeError, ValueError):
        pass
    # Rankings often contain only named forms (for example Deoxys (Defense))
    # while the party card displays the base species name.  Make those base
    # labels OCR candidates too.
    for candidate in tuple(known):
        base = re.sub(r"\s*\([^)]*\)\s*$", "", candidate).strip()
        if base and canonical_name(base) not in {
            canonical_name(item) for item in known
        }:
            known.append(base)
    for expected in expected_names:
        if canonical_name(expected) not in {canonical_name(name) for name in known}:
            known.append(expected)
    for box in boxes:
        text = str(getattr(box, "text", ""))
        x = int(getattr(box, "center_x"))
        cp_match = re.fullmatch(r"\s*[cC][pP]\s*([0-9]{2,4})\s*", text)
        if cp_match:
            cps.append((x, int(cp_match.group(1))))
            continue
        # Nicknames such as ``Abra100%`` are common on real party cards.  The
        # trailing IV marker is not part of the species name.
        cleaned = re.sub(r"\s*[0-9]{2,3}\s*%\s*$", "", text).strip()
        name = identify_pokemon([cleaned], known)
        if name is None and cleaned != text and re.fullmatch(
            r"[A-Za-z][A-Za-z .'-]{1,24}", cleaned
        ):
            name = cleaned
        if name is not None and canonical_name(cleaned) == canonical_name(name):
            names.append((x, name))
    if len(names) != 3 or len(cps) != 3:
        return None
    names.sort()
    cps.sort()
    members: list[TeamMember] = []
    for (_, name), (_, cp) in zip(names, cps):
        meta = lookup_matchup_pokemon(name)
        bulk = float(getattr(meta, "bulk_rating", getattr(meta, "bulk", 80.0)))
        rating = float(getattr(
            meta, "auto_battle_rating", getattr(meta, "score", 80.0)))
        members.append(
            TeamMember(
                meta.name, cp, tuple(meta.types), meta.fast_move,
                tuple(meta.charged_moves), bulk, rating,
            )
        )
    return BattleTeam(tuple(members))  # type: ignore[arg-type]


def type_multiplier(attacking_types: Iterable[str], defending_types: Sequence[str]) -> float:
    """Best same-type attack multiplier available to one Pokemon."""

    best = 1.0
    for attack_type in attacking_types:
        multiplier = 1.0
        for defend_type in defending_types:
            multiplier *= gbl_evaluator.TYPE_CHART.get(attack_type, {}).get(defend_type, 1.0)
        best = max(best, multiplier)
    return best


SPECIES_COVERAGE_THREATS: dict[str, tuple[str, ...]] = {
    "mewtwo": ("Electric", "Ice", "Fighting", "Fire"),
    "dialga": ("Electric",),
    "palkia": ("Fire",),
    "kyogre": ("Ice", "Electric"),
    "groudon": ("Fire",),
    "zacian": ("Electric", "Fighting"),
    "ho oh": ("Ground", "Grass"),
    "yveltal": ("Fighting",),
}


def threat_types(pokemon) -> tuple[str, ...]:
    """What a Pokemon can actually hit you with: its typing plus its moves.

    Typing alone reads a matchup as free whenever the danger is off-type, and
    that is not a corner case in the open meta -- a run switched into Kyurem
    against Lugia scoring +22.7, because Psychic/Flying is 1.0x into Dragon/Ice
    and nothing in the score knew Lugia leads Dragon Tail for 2x.

    The typing is always included, so this only ever widens what the incoming
    term considers; it cannot make a genuinely bad matchup look safe.  Falls
    back to bare typing when the game master is unavailable, which is the old
    behaviour.
    """

    types = [str(value).title() for value in getattr(pokemon, "types", ()) if value]
    species_key = canonical_name(
        getattr(pokemon, "name", "") or getattr(pokemon, "species_id", "") or ""
    )
    if species_key in SPECIES_COVERAGE_THREATS:
        types.extend(SPECIES_COVERAGE_THREATS[species_key])
    moves = [getattr(pokemon, "fast_move", "") or ""]
    moves.extend(getattr(pokemon, "charged_moves", ()) or ())
    try:
        from . import gbl_meta  # Imported late: gbl_meta imports this module.

        move_types = gbl_meta.load_move_types()
    except (ImportError, OSError, TypeError, ValueError):
        return tuple(dict.fromkeys(types))
    for move in moves:
        move_type = move_types.get(canonical_name(str(move)))
        if move_type:
            types.append(move_type)
    return tuple(dict.fromkeys(types))


def member_threat_types(member: TeamMember) -> tuple[str, ...]:
    """What a team member can hit the opponent with: typing + moves."""
    types = [str(value).title() for value in member.types if value]
    moves = [member.fast_move, *member.charged_moves]
    try:
        from . import gbl_meta
        move_types = gbl_meta.load_move_types()
        for move in moves:
            m_type = move_types.get(canonical_name(str(move)))
            if m_type:
                types.append(m_type)
    except (ImportError, OSError, TypeError, ValueError):
        pass
    return tuple(dict.fromkeys(types))


def matchup_score(member: TeamMember, opponent: gbl_evaluator.MetaPokemon) -> float:
    """Score offense, incoming damage, bulk and general PvP quality."""
    outgoing = type_multiplier(member_threat_types(member), opponent.types)
    incoming = type_multiplier(threat_types(opponent), member.types)
    return (
        34.0 * math.log2(outgoing)
        - 45.0 * math.log2(incoming)
        + 0.12 * member.bulk
        + 0.08 * member.rating
    )


def lookup_matchup_pokemon(name: str):
    """Resolve an opponent against the full PvPoke species/type index.

    The small evaluator shortlist is useful for team recommendations but its
    generic fallback labels unknown species as Normal. That made common OCR
    results such as ``Rapidash (Shadow)`` lose their real Fire typing and could
    select Togetic instead of the available Water counter. The full index is
    already cached and used by battle OCR, so prefer its exact canonical match.
    """
    try:
        from . import gbl_meta
        wanted = canonical_name(name)
        candidates = [
            candidate for candidate in gbl_meta.load_species_index().values()
            if canonical_name(candidate.name) == wanted
        ]
        exact = [
            candidate for candidate in candidates
            if candidate.name.casefold() == name.strip().casefold()
        ]
        if exact:
            return exact[0]
        if candidates:
            return candidates[0]
    except (OSError, TypeError, ValueError):
        pass
    base_fallbacks = {
        "deoxys": (["Psychic"], "Zen Headbutt", ["Psycho Boost", "Thunderbolt"]),
        "abra": (["Psychic"], "Zen Headbutt", ["Psyshock", "Shadow Ball"]),
    }
    fallback = base_fallbacks.get(canonical_name(name))
    if fallback is not None:
        types, fast_move, charged_moves = fallback
        return gbl_evaluator.MetaPokemon(
            name=name,
            types=types,
            pvp_rank_score=55.0,
            fast_move=fast_move,
            charged_moves=charged_moves,
            bulk_rating=55.0,
            auto_battle_rating=55.0,
            primary_role="Flex",
            double_move_cost=50000,
        )
    return gbl_evaluator.lookup_meta_pokemon(name)


def identify_pokemon(lines: Iterable[str], allowed_names: Iterable[str] | None = None) -> str | None:
    """Resolve noisy OCR text to the longest plausible Pokemon name."""

    names = list(allowed_names or gbl_evaluator.POKEMON_DATABASE.keys())
    line_values = list(lines)
    raw_lines = [
        " ".join(re.sub(r"[^a-z0-9]+", " ", line.casefold()).split())
        for line in line_values
    ]
    raw_joined = " ".join(raw_lines)
    display_exact = [
        name for name in names
        if " ".join(re.sub(
            r"[^a-z0-9]+", " ", name.casefold()).split()) in raw_joined
    ]
    if display_exact:
        return max(display_exact, key=len)
    canonical = {name: canonical_name(name) for name in names}
    text_lines = [
        canonical_name(line) for line in line_values if canonical_name(line)
    ]
    joined = " ".join(text_lines)

    exact = [name for name, normalized in canonical.items() if normalized and normalized in joined]
    if exact:
        return max(exact, key=lambda name: len(canonical[name]))

    best_name: str | None = None
    best_ratio = 0.0
    for line in text_lines:
        for name, normalized in canonical.items():
            if abs(len(line) - len(normalized)) > max(4, len(normalized) // 2):
                continue
            ratio = SequenceMatcher(None, line, normalized).ratio()
            if ratio > best_ratio:
                best_name, best_ratio = name, ratio
    return best_name if best_ratio >= 0.76 else None


def choose_switch(
    team: BattleTeam,
    memory: BattleMemory,
    opponent_name: str | None,
    settings: StrategySettings,
    *,
    forced: bool = False,
    urgent: bool = False,
    now: float | None = None,
) -> SwitchDecision:
    """Choose the living reserve with the strongest meaningful matchup."""

    available = [
        index for index, alive in enumerate(memory.alive)
        if alive and index != memory.active_index
    ]
    # A reserve that has already refused to come in is not a candidate again --
    # unless the active Pokemon fainted, where refusing to name anyone would
    # leave the switch sheet up for the rest of the battle.
    if not forced:
        unblocked = [index for index in available if not memory.switch_blocked(index)]
        if unblocked:
            available = unblocked
        elif available:
            return SwitchDecision(
                None, opponent_name, 0.0, 0.0,
                "every reserve failed to switch in; attacking instead",
            )
    if not available:
        return SwitchDecision(None, opponent_name, 0.0, 0.0, "no living reserve")

    if opponent_name is None:
        if not forced:
            return SwitchDecision(None, None, 0.0, 0.0, "opponent not recognized")
        target = max(
            available,
            key=lambda index: team.members[index].bulk + team.members[index].rating,
        )
        return SwitchDecision(
            target, None, 0.0, team.members[target].bulk,
            f"forced switch; safest reserve is {team.members[target].name}",
        )

    opponent = lookup_matchup_pokemon(opponent_name)
    opp_threats = threat_types(opponent)
    active_member = team.members[memory.active_index]
    active_incoming = type_multiplier(opp_threats, active_member.types)
    current_score = matchup_score(active_member, opponent)

    if not forced:
        if not settings.enabled:
            return SwitchDecision(None, opponent.name, current_score, current_score, "smart switching disabled")
        if not memory.cooldown_ready(settings, now):
            return SwitchDecision(None, opponent.name, current_score, current_score, "switch cooldown active")
        if active_incoming < 1.35:
            return SwitchDecision(
                None,
                opponent.name,
                current_score,
                current_score,
                f"active {active_member.name} taking non-supereffective damage ({active_incoming:.2f}x); staying in battle",
            )

    safe_reserves = [
        index for index in available
        if type_multiplier(opp_threats, team.members[index].types) < 1.35
    ]

    candidates = safe_reserves if safe_reserves else (available if forced else [])
    if not candidates:
        return SwitchDecision(
            None,
            opponent.name,
            current_score,
            current_score,
            "no living reserve avoids supereffective damage; staying in battle",
        )

    target = max(
        candidates,
        key=lambda index: matchup_score(team.members[index], opponent),
    )
    target_score = matchup_score(team.members[target], opponent)
    gain = target_score - current_score

    if not forced and gain < settings.minimum_score_gain:
        return SwitchDecision(
            None, opponent.name, current_score, target_score,
            f"best safe reserve gain {gain:.1f} is below {settings.minimum_score_gain:.1f}",
        )

    reason = f"{team.members[target].name} resists/neutralizes incoming damage and improves matchup by {gain:.1f}"
    return SwitchDecision(
        target,
        opponent.name,
        current_score,
        target_score,
        reason,
    )


def reserve_sheet_slot(memory: BattleMemory, target_index: int) -> int:
    """Return the left-to-right slot for a target on the switch sheet."""

    visible = [
        index for index, alive in enumerate(memory.alive)
        if alive and index != memory.active_index
    ]
    if target_index not in visible:
        raise ValueError(f"Pokemon index {target_index} is not a visible living reserve")
    return visible.index(target_index)


def league_score(text: str, preferred: str = "auto") -> tuple[float, int | None]:
    normalized = canonical_name(text)
    if "great league" in normalized:
        score, cap = (100.0 if "cup" not in normalized else 90.0), 1500
    elif "weather cup" in normalized or "competitors cup" in normalized:
        score, cap = 88.0, 1500
    elif "ultra league" in normalized:
        score, cap = 68.0, 2500
    elif "master league" in normalized:
        score, cap = 42.0, None
    elif "cup" in normalized:
        score, cap = 75.0, 1500
    else:
        return -1e9, None
    wanted = canonical_name(preferred)
    if wanted and wanted != "auto" and league_matches(wanted, normalized):
        score += 1000.0
    return score, cap


def choose_easiest_league(
    boxes: Iterable[Any],
    preferred: str = "auto",
    team: BattleTeam | None = None,
    min_y: int | None = None,
    max_y: int | None = None,
) -> LeagueChoice | None:
    """Pick the easiest recognized league/cup from positioned OCR results."""

    box_list = list(boxes)
    coming_next_y = None
    for b in box_list:
        if "coming next" in canonical_name(str(getattr(b, "text", ""))):
            coming_next_y = int(getattr(b, "center_y", 0))
            break

    choices: list[LeagueChoice] = []
    for box in box_list:
        center_y = int(getattr(box, "center_y", 0))
        if min_y is not None and center_y < min_y:
            continue
        if max_y is not None and center_y > max_y:
            continue
        if coming_next_y is not None and center_y > coming_next_y:
            continue
        text = str(getattr(box, "text", ""))
        score, cap = league_score(text, preferred)
        if score <= -1e8:
            continue
        normalized = canonical_name(text)
        if team is not None and "weather cup" in normalized:
            # The current Weather Cup permits the weather-boost groups below.
            # Penalize it unless the configured account actually has a full
            # legal team; blindly choosing a 1500-CP cup is not easier if two
            # party members are rejected by its eligibility filter.
            allowed = {"Normal", "Fire", "Water", "Ice", "Rock"}
            eligible = sum(bool(set(member.types) & allowed) for member in team.members)
            score -= (3 - eligible) * 45.0
        choices.append(
            LeagueChoice(
                text,
                score,
                int(getattr(box, "center_x")),
                center_y,
                cap,
            )
        )
    return max(choices, key=lambda choice: choice.score) if choices else None
