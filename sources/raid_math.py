#!/usr/bin/env python3
"""The arithmetic behind a raid-attacker spend: level, cost and damage.

Three questions, each answered from the game's own formulas rather than from a
ranking someone typed in:

- **What is this individual?**  CP and HP are exact functions of base stats,
  IVs and level.  The detail page shows both, plus the stardust a power-up
  costs, which pins the level to a band; the storage filters `4*` and `3*` pin
  the IVs to a band.  `solve` returns every (form, level, IV) combination that
  fits all of that, so an ambiguous read stays visibly ambiguous.
- **What does it cost to feed?**  The power-up tables, with the shadow and
  purified multipliers and per-species overrides.  Eternatus is the reason the
  override matters: its XL power-ups cost 100 to 890 XL each.
- **How good is it in a raid?**  The widely used comprehensive DPS estimate
  (GamePress), folded into one rating as (DPS^3 x TDO)^(1/4), against a generic
  boss that is weak to the attacking type.  Absolute numbers are not the point:
  the rating is used to rank attackers within one type and to measure how much
  a power-up moves an individual.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .raid_gamedata import TYPES, GameData, Move

# A generic raid boss: tier-5 bosses sit around 180-230 defence after the boss
# CP multiplier, and the planner only compares attackers within a type.
TARGET_DEFENSE = 200.0
SUPER_EFFECTIVE = 1.6
STAB = 1.2
SHADOW_ATTACK = 1.2
SHADOW_DEFENSE = 5.0 / 6.0
MEGA_SAME_TYPE = 1.3
MEGA_OTHER_TYPE = 1.1
MAX_LEVEL = 50.0
RANK_LEVEL = 40.0
HUNDO = (15, 15, 15)


# --- what an individual is -----------------------------------------------


def cp_of(stats: Sequence[int], ivs: Sequence[int], cpm: float) -> int:
    attack, defense, stamina = (stats[i] + ivs[i] for i in range(3))
    return max(10, int(attack * math.sqrt(defense) * math.sqrt(stamina) * cpm * cpm / 10))


def hp_of(stats: Sequence[int], ivs: Sequence[int], cpm: float) -> int:
    return max(10, int((stats[2] + ivs[2]) * cpm))


@dataclass(frozen=True)
class Fit:
    """One reading of an individual that agrees with everything on screen."""

    form: str
    level: float
    ivs: tuple[int, int, int]

    @property
    def percent(self) -> float:
        return round(sum(self.ivs) / 45 * 100, 1)


def solve(
    gd: GameData,
    forms: Iterable[dict],
    cp: int,
    hp: int | None,
    *,
    levels: Iterable[float] | None = None,
    min_iv_sum: int = 0,
    max_iv_sum: int = 45,
) -> list[Fit]:
    """Every (form, level, IVs) that reproduces this CP and HP exactly."""
    wanted_levels = list(levels) if levels is not None else gd.levels
    fits: list[Fit] = []
    for form in forms:
        stats = form["stats"]
        for level in wanted_levels:
            cpm = gd.cpm(level)
            for stamina_iv in range(16):
                if hp is not None and hp_of(stats, (0, 0, stamina_iv), cpm) != hp:
                    continue
                for attack_iv in range(16):
                    for defense_iv in range(16):
                        total = attack_iv + defense_iv + stamina_iv
                        if not min_iv_sum <= total <= max_iv_sum:
                            continue
                        ivs = (attack_iv, defense_iv, stamina_iv)
                        if cp_of(stats, ivs, cpm) == cp:
                            fits.append(Fit(form["id"], level, ivs))
    return fits


# --- what it costs --------------------------------------------------------


@dataclass(frozen=True)
class Cost:
    candy: int = 0
    xl: int = 0
    dust: int = 0

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(self.candy + other.candy, self.xl + other.xl, self.dust + other.dust)


def _scaled(value: int, factor: float) -> int:
    return int(math.ceil(value * factor - 1e-9))


def step_cost(
    gd: GameData, dex: int, level: float, *, shadow: bool = False,
    purified: bool = False, lucky: bool = False,
) -> Cost:
    """What one power-up from `level` to `level + 0.5` costs."""
    table = gd.upgrades_for(dex)
    index = int(level) - 1
    candy_factor = (table["shadow_candy"] if shadow
                    else table["purified_candy"] if purified else 1.0)
    dust_factor = (table["shadow_dust"] if shadow
                   else table["purified_dust"] if purified else 1.0)
    if lucky:
        dust_factor *= 0.5
    dust = _scaled(table["dust"][index], dust_factor) if index < len(table["dust"]) else 0
    if level >= 40:
        xl_index = int(level) - 40
        xl = table["xl"][xl_index] if xl_index < len(table["xl"]) else 0
        return Cost(xl=_scaled(xl, candy_factor), dust=dust)
    return Cost(candy=_scaled(table["candy"][index], candy_factor), dust=dust)


def cost_between(
    gd: GameData, dex: int, start: float, end: float, **variant: bool
) -> Cost:
    total = Cost()
    level = start
    while level < end - 1e-9:
        total = total + step_cost(gd, dex, level, **variant)
        level += 0.5
    return total


def reachable_level(
    gd: GameData, dex: int, start: float, candy: int, xl: int,
    *, cap: float = MAX_LEVEL, **variant: bool
) -> tuple[float, Cost]:
    """How far species candy and XL candy alone take an individual."""
    level = start
    spent = Cost()
    while level < cap - 1e-9:
        step = step_cost(gd, dex, level, **variant)
        if spent.candy + step.candy > candy or spent.xl + step.xl > xl:
            break
        spent = spent + step
        level += 0.5
    return level, spent


def levels_for_powerup(
    gd: GameData, dex: int, dust: int | None, candy: int | None,
) -> dict[str, list[float]]:
    """Which levels a power-up price fits, keyed by the variant it implies.

    The price is the only on-screen sign of a shadow or purified individual
    that OCR can see, so each variant's levels are kept apart.
    """
    out: dict[str, list[float]] = {}
    if dust is None:
        return out
    variants = {
        "normal": {}, "shadow": {"shadow": True}, "purified": {"purified": True},
        "lucky": {"lucky": True},
    }
    for name, flags in variants.items():
        matches = []
        for level in gd.levels:
            if level >= MAX_LEVEL:
                break
            step = step_cost(gd, dex, level, **flags)
            if step.dust != dust:
                continue
            if candy is not None and candy not in (step.candy, step.xl):
                continue
            matches.append(level)
        if matches:
            out[name] = matches
    return out


# --- how good it is in a raid --------------------------------------------


@dataclass(frozen=True)
class Attacker:
    """One thing a raid party can field: a form, possibly shadow or Mega."""

    pokemon_id: str
    name: str
    search: str
    dex: int
    stats: tuple[int, int, int]
    types: tuple[str, ...]
    fast: tuple[str, ...]
    charged: tuple[str, ...]
    elite: frozenset[str]
    shadow: bool = False
    mega: str = ""
    mega_types: tuple[str, ...] = ()
    mega_first_cost: int = 0
    legendary: bool = False

    @property
    def key(self) -> str:
        if self.mega:
            return f"{self.pokemon_id}:{self.mega.lower().replace(' ', '_')}"
        return f"{self.pokemon_id}:{'shadow' if self.shadow else 'normal'}"


def attackers(gd: GameData) -> list[Attacker]:
    out: list[Attacker] = []
    for p in gd.pokemon.values():
        base = dict(
            pokemon_id=p["id"], search=p["search"], dex=p["dex"],
            types=tuple(p["types"]), fast=tuple(p["fast"]), charged=tuple(p["charged"]),
            elite=frozenset(p["elite"]), legendary=p["legendary"],
        )
        out.append(Attacker(name=p["name"], stats=tuple(p["stats"]), **base))
        if p["shadow"]:
            out.append(Attacker(name=f"Shadow {p['name']}", stats=tuple(p["stats"]),
                                shadow=True, **base))
        for mega in p["megas"]:
            name = p["name"]
            label = mega["label"]
            out.append(Attacker(
                name=f"{label.split()[0]} {name}" + (f" {label.split()[1]}" if " " in label else ""),
                stats=tuple(mega["stats"]), mega=label,
                mega_types=tuple(mega["types"]), mega_first_cost=int(mega["first_cost"]),
                **base,
            ))
    return out


@dataclass(frozen=True)
class Rating:
    attacker: Attacker
    fast: Move
    charged: Move
    dps: float
    tdo: float
    level: float
    ivs: tuple[int, int, int]

    @property
    def er(self) -> float:
        return (max(self.dps, 0.0) ** 3 * max(self.tdo, 0.0)) ** 0.25

    @property
    def needs_elite(self) -> bool:
        return bool({self.fast.key, self.charged.key} & self.attacker.elite)

    @property
    def moveset(self) -> str:
        star = lambda m: m.name + ("*" if m.key in self.attacker.elite else "")  # noqa: E731
        return f"{star(self.fast)} / {star(self.charged)}"


def _damage(power: float, attack: float, multiplier: float) -> float:
    return math.floor(0.5 * power * attack / TARGET_DEFENSE * multiplier) + 1


def _move_multiplier(attacker: Attacker, move: Move, attack_type: str) -> float:
    multiplier = SUPER_EFFECTIVE if move.type == attack_type else 1.0
    own_types = attacker.mega_types or attacker.types
    if move.type in own_types:
        multiplier *= STAB
    if attacker.mega:
        multiplier *= MEGA_SAME_TYPE if move.type in attacker.mega_types else MEGA_OTHER_TYPE
    return multiplier


def rate(
    gd: GameData, attacker: Attacker, fast: Move, charged: Move, attack_type: str,
    level: float = RANK_LEVEL, ivs: Sequence[int] = HUNDO,
) -> Rating:
    cpm = gd.cpm(level)
    attack = (attacker.stats[0] + ivs[0]) * cpm * (SHADOW_ATTACK if attacker.shadow else 1.0)
    defense = (attacker.stats[1] + ivs[1]) * cpm * (SHADOW_DEFENSE if attacker.shadow else 1.0)
    hp = float(hp_of(attacker.stats, ivs, cpm))

    fast_damage = _damage(fast.power, attack, _move_multiplier(attacker, fast, attack_type))
    charged_damage = _damage(charged.power, attack, _move_multiplier(attacker, charged, attack_type))
    fast_energy = max(fast.energy, 1)
    charged_energy = -charged.energy if charged.energy < 0 else charged.energy
    fdps, feps = fast_damage / fast.duration, fast_energy / fast.duration
    if charged_energy >= 100:
        # A one-bar move wastes the energy that overflows the bar.
        charged_energy += 0.5 * fast_energy
    cdps, ceps = charged_damage / charged.duration, charged_energy / charged.duration
    incoming = 900.0 / defense
    leftover = 0.5 * charged_energy + 0.5 * fast_energy
    dps0 = (fdps * ceps + cdps * feps) / (ceps + feps)
    dps = dps0 + (cdps - fdps) / (ceps + feps) * (0.5 - leftover / hp) * incoming
    dps = min(max(dps, fdps), max(cdps, fdps))
    tdo = dps * hp / incoming
    return Rating(attacker, fast, charged, dps, tdo, level, tuple(ivs))


def best_rating(
    gd: GameData, attacker: Attacker, attack_type: str,
    level: float = RANK_LEVEL, ivs: Sequence[int] = HUNDO,
    *, fast: Sequence[str] | None = None, charged: Sequence[str] | None = None,
) -> Rating | None:
    """The attacker's best moveset whose charged move is of `attack_type`.

    `fast`/`charged` restrict the choice to moves an individual actually knows.
    """
    best: Rating | None = None
    for c_key in charged if charged is not None else attacker.charged:
        c_move = gd.move(c_key)
        if c_move is None or c_move.fast or c_move.type != attack_type:
            continue
        for f_key in fast if fast is not None else attacker.fast:
            f_move = gd.move(f_key)
            if f_move is None or not f_move.fast:
                continue
            rating = rate(gd, attacker, f_move, c_move, attack_type, level, ivs)
            if best is None or rating.er > best.er:
                best = rating
    return best


@dataclass
class TypeRanking:
    attack_type: str
    ratings: list[Rating] = field(default_factory=list)

    @property
    def top(self) -> Rating | None:
        return self.ratings[0] if self.ratings else None


def rank_type(gd: GameData, attack_type: str, pool: Sequence[Attacker] | None = None,
              top: int = 25) -> TypeRanking:
    ratings = []
    for attacker in pool if pool is not None else attackers(gd):
        rating = best_rating(gd, attacker, attack_type)
        if rating is not None:
            ratings.append(rating)
    ratings.sort(key=lambda r: r.er, reverse=True)
    return TypeRanking(attack_type, ratings[:top])


def rank_all(gd: GameData, top: int = 25) -> dict[str, TypeRanking]:
    pool = attackers(gd)
    return {t: rank_type(gd, t, pool, top) for t in TYPES}
