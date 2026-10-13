#!/usr/bin/env python3
"""The game facts raid planning needs, derived from the published game master.

Two public sources, each used for what it is authoritative on:

- PvPoke's game master names every released species and form in English, tags
  shadow eligibility, legendaries and Megas, and lists each movepool with its
  Elite TM moves.  It is the list of what exists.
- PokeMiners' decoded game master carries the numbers PvPoke does not: the
  gyms-and-raids move stats (PvPoke's are the Trainer Battle ones), the CP
  multiplier table, the power-up cost tables -- including per-species overrides
  such as Eternatus, whose XL power-ups cost up to 890 XL each -- and the
  energy a first Mega Evolution costs.

Both downloads are cached in private state for a day, and the derived dataset
is cached beside them, so a planning run is offline after the first.  A stale
cache is used rather than nothing when a download fails.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config_paths

CACHE_DIR = config_paths.state_dir() / "cache" / "raid"
PVPOKE_URL = (
    "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/gamemaster.json"
)
POKEMINERS_URL = (
    "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/latest.json"
)
CACHE_SECONDS = 24 * 60 * 60
# Bumped whenever `build` changes shape, so an old derived cache is rebuilt.
SCHEMA = 3

TYPES = (
    "Normal", "Fire", "Water", "Electric", "Grass", "Ice",
    "Fighting", "Poison", "Ground", "Flying", "Psychic", "Bug",
    "Rock", "Ghost", "Dragon", "Dark", "Steel", "Fairy",
)

# PokeMiners orders `attackScalar` the way the game's type enum does, which is
# not the order above.
_ENUM_ORDER = (
    "Normal", "Fighting", "Flying", "Poison", "Ground", "Rock", "Bug", "Ghost",
    "Steel", "Fire", "Water", "Grass", "Electric", "Psychic", "Ice", "Dragon",
    "Dark", "Fairy",
)

# Variant suffixes PvPoke gives a temporary evolution, and the label the planner
# shows for each.
_TEMP_EVO = {"_mega_x": "Mega X", "_mega_y": "Mega Y", "_mega": "Mega", "_primal": "Primal"}


class GameDataError(RuntimeError):
    """The game master could not be downloaded or understood."""


def move_key(move_id: str) -> str:
    """One spelling for a move across both sources.

    PvPoke writes FUTURE_SIGHT and LICK; PokeMiners writes FUTURESIGHT and
    LICK_FAST.  Dropping the underscores and the fast suffix makes them agree.
    """
    key = str(move_id).upper()
    if key.endswith("_FAST"):
        key = key[: -len("_FAST")]
    return key.replace("_", "")


def _type_name(value: str | None) -> str | None:
    if not value or str(value).lower() == "none":
        return None
    return str(value).replace("POKEMON_TYPE_", "").title()


def _fetch(name: str, url: str, *, refresh: bool = False) -> Any:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    fresh = path.is_file() and time.time() - path.stat().st_mtime < CACHE_SECONDS
    if refresh or not fresh:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "PokemonAutomation/1"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
            # Parsed before it replaces a cache that still works.
            json.loads(payload)
            path.write_bytes(payload)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the cache
            if not path.is_file():
                raise GameDataError(f"could not download {url} ({exc})") from exc
    return json.loads(path.read_text())


# --- building the compact dataset ----------------------------------------


def _upgrade_table(raw: dict) -> dict:
    return {
        "candy": [int(v) for v in raw.get("candyCost", [])],
        "xl": [int(v) for v in raw.get("xlCandyCost", [])],
        "dust": [int(v) for v in raw.get("stardustCost", [])],
        "shadow_candy": float(raw.get("shadowCandyMultiplier", 1.2)),
        "shadow_dust": float(raw.get("shadowStardustMultiplier", 1.2)),
        "purified_candy": float(raw.get("purifiedCandyMultiplier", 0.9)),
        "purified_dust": float(raw.get("purifiedStardustMultiplier", 0.9)),
    }


_DEX_TEMPLATE = re.compile(r"_V(\d{4})_POKEMON_")


def build(pvpoke: dict, miners: list) -> dict:
    """The compact dataset, from the two parsed game masters."""
    by_id = {t.get("templateId"): t.get("data", {}) for t in miners if isinstance(t, dict)}

    player = by_id.get("PLAYER_LEVEL_SETTINGS", {}).get("playerLevel", {})
    whole = [float(v) for v in player.get("cpMultiplier", [])][:51]
    if len(whole) < 51:
        raise GameDataError("the game master has no CP multiplier table")

    upgrades = _upgrade_table(by_id.get("POKEMON_UPGRADE_SETTINGS", {}).get("pokemonUpgrades", {}))
    overrides: dict[str, dict] = {}
    first_mega_cost: dict[str, int] = {}
    effectiveness: dict[str, dict[str, float]] = {}
    pve_moves: dict[str, dict] = {}
    for template_id, data in by_id.items():
        template_id = str(template_id)
        if "pokemonUpgrades" in data and template_id.startswith("POKEMON_UPGRADE_OVERRIDE"):
            match = _DEX_TEMPLATE.search(template_id)
            if match:
                overrides[str(int(match.group(1)))] = _upgrade_table(data["pokemonUpgrades"])
        elif "megaEvoLevelSettings" in data and template_id.startswith("MEGA_EVOLUTION_LEVEL_0"):
            cost = data["megaEvoLevelSettings"].get("cooldown", {}).get("bypassCostInitial")
            match = _DEX_TEMPLATE.search(template_id)
            if cost:
                first_mega_cost[str(int(match.group(1))) if match else "default"] = int(cost)
        elif "typeEffective" in data:
            attack = _type_name(data["typeEffective"].get("attackType"))
            scalars = data["typeEffective"].get("attackScalar", [])
            if attack and len(scalars) == len(_ENUM_ORDER):
                effectiveness[attack] = {
                    defender: float(value) for defender, value in zip(_ENUM_ORDER, scalars)
                }
        elif "moveSettings" in data:
            move = data["moveSettings"]
            move_id = str(move.get("movementId", ""))
            if move_id.isdigit():
                # Newer moves carry a number; the template still names them
                # (V0593_MOVE_DIVE).  Mega-signature moves are skipped: they
                # are not in any movepool a planner can choose.
                if not template_id.startswith("V"):
                    continue
                move_id = template_id.split("_MOVE_", 1)[-1]
            if not move_id or not isinstance(move.get("durationMs"), (int, float)):
                continue
            pve_moves[move_key(move_id)] = {
                "type": _type_name(move.get("pokemonType")) or "Normal",
                "power": float(move.get("power", 0.0)),
                "duration": float(move.get("durationMs", 0)) / 1000.0,
                "energy": int(move.get("energyDelta", 0)),
                "fast": move_id.upper().endswith("_FAST"),
            }

    # Every Hidden Power is one fast move whose type the individual decides.
    hidden = pve_moves.get("HIDDENPOWER")
    if hidden:
        for type_name in _ENUM_ORDER:
            pve_moves.setdefault("HIDDENPOWER" + type_name.upper(), {**hidden, "type": type_name})

    names = {
        move_key(m["moveId"]): str(m.get("name") or m["moveId"]).strip()
        for m in pvpoke.get("moves", [])
        if isinstance(m, dict) and m.get("moveId")
    }

    entries = [p for p in pvpoke.get("pokemon", []) if isinstance(p, dict)]
    by_species = {p["speciesId"]: p for p in entries if p.get("speciesId")}
    pokemon: dict[str, dict] = {}
    for entry in entries:
        sid = str(entry.get("speciesId", ""))
        tags = set(entry.get("tags") or ())
        if not entry.get("released", True) or "shadow" in tags or sid.endswith("_shadow"):
            continue
        if any(sid.endswith(suffix) for suffix in _TEMP_EVO):
            continue
        stats = entry.get("baseStats") or {}
        if not all(k in stats for k in ("atk", "def", "hp")):
            continue
        dex = str(int(entry.get("dex", 0)))
        megas = []
        for suffix, label in _TEMP_EVO.items():
            mega = by_species.get(sid + suffix)
            if not mega or not mega.get("released", True):
                continue
            mstats = mega.get("baseStats") or {}
            megas.append({
                "label": label,
                "id": sid + suffix,
                "stats": [int(mstats["atk"]), int(mstats["def"]), int(mstats["hp"])],
                "types": [t.title() for t in mega.get("types", []) if t and t != "none"],
                "first_cost": first_mega_cost.get(dex, first_mega_cost.get("default", 200)),
            })
        species_name = str(entry.get("speciesName", sid))
        pokemon[sid] = {
            "id": sid,
            "name": species_name,
            "search": species_name.split(" (")[0].strip().lower(),
            "dex": int(dex),
            "types": [t.title() for t in entry.get("types", []) if t and t != "none"],
            "stats": [int(stats["atk"]), int(stats["def"]), int(stats["hp"])],
            "fast": [move_key(m) for m in entry.get("fastMoves", [])],
            "charged": [move_key(m) for m in entry.get("chargedMoves", [])],
            "elite": [move_key(m) for m in entry.get("eliteMoves", [])],
            "shadow": "shadoweligible" in tags,
            "legendary": bool(tags & {"legendary", "mythical", "ultrabeast"}),
            "megas": megas,
        }

    return {
        "schema": SCHEMA,
        "pvpoke_timestamp": str(pvpoke.get("timestamp", "")),
        "cpm": whole,
        "upgrades": upgrades,
        "upgrade_overrides": overrides,
        "effectiveness": effectiveness,
        "moves": pve_moves,
        "move_names": names,
        "pokemon": pokemon,
    }


# --- the loaded view -----------------------------------------------------


@dataclass(frozen=True)
class Move:
    key: str
    name: str
    type: str
    power: float
    duration: float
    energy: int
    fast: bool


@dataclass
class GameData:
    raw: dict
    _cpm: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        whole = self.raw["cpm"]
        # Half levels are the root mean square of their neighbours, which is how
        # the game defines them.
        table = []
        for index in range(len(whole) - 1):
            table.append(whole[index])
            table.append(math.sqrt((whole[index] ** 2 + whole[index + 1] ** 2) / 2))
        table.append(whole[-1])
        self._cpm = table

    @property
    def pokemon(self) -> dict[str, dict]:
        return self.raw["pokemon"]

    def cpm(self, level: float) -> float:
        return self._cpm[int(round((level - 1) * 2))]

    @property
    def levels(self) -> list[float]:
        return [1 + index / 2 for index in range(len(self._cpm))]

    def move(self, key: str) -> Move | None:
        data = self.raw["moves"].get(move_key(key))
        if data is None:
            return None
        return Move(
            key=move_key(key),
            name=self.raw["move_names"].get(move_key(key), key.title()),
            **data,
        )

    def move_by_name(self, text: str) -> Move | None:
        """A move from the name the detail page prints, e.g. 'Dazzling Gleam'."""
        wanted = re.sub(r"[^a-z]", "", text.lower())
        if not wanted:
            return None
        for key, name in self.raw["move_names"].items():
            if re.sub(r"[^a-z]", "", name.lower()) == wanted:
                return self.move(key)
        return None

    def effectiveness(self, attack: str, defenders: list[str]) -> float:
        value = 1.0
        for defender in defenders:
            value *= self.raw["effectiveness"].get(attack, {}).get(defender, 1.0)
        return value

    def upgrades_for(self, dex: int) -> dict:
        return self.raw["upgrade_overrides"].get(str(dex), self.raw["upgrades"])

    def forms_for_search(self, term: str) -> list[dict]:
        """Every released form a storage search for `term` can return."""
        return [p for p in self.pokemon.values() if p["search"] == term.lower()]


def _derived_path() -> Path:
    return CACHE_DIR / "raid_gamedata.json"


@lru_cache(maxsize=1)
def load(refresh: bool = False) -> GameData:
    """The dataset, rebuilt from fresh downloads at most once a day."""
    derived = _derived_path()
    if not refresh and derived.is_file():
        age = time.time() - derived.stat().st_mtime
        try:
            cached = json.loads(derived.read_text())
        except (OSError, json.JSONDecodeError):
            cached = None
        if cached and cached.get("schema") == SCHEMA and age < CACHE_SECONDS:
            return GameData(cached)
    try:
        raw = build(
            _fetch("pvpoke-gamemaster.json", PVPOKE_URL, refresh=refresh),
            _fetch("pokeminers-latest.json", POKEMINERS_URL, refresh=refresh),
        )
    except GameDataError:
        if derived.is_file():
            cached = json.loads(derived.read_text())
            if cached.get("schema") == SCHEMA:
                return GameData(cached)
        raise
    derived.write_text(json.dumps(raw))
    return GameData(raw)


def from_dict(raw: dict) -> GameData:
    """A dataset built elsewhere -- tests use this with a small fixture."""
    return GameData(raw)
