"""Which Pokemon two phones should trade each other, and how many.

Trading re-rolls a Pokemon's IVs, so a pair of accounts can turn spare,
ordinary copies of a good raid attacker into fresh rolls at a 98% one.  This
module decides what is worth that, from three things:

1. **The species.**  Every family whose evolved form places in some type's
   top raid attackers (`raid_meta.rankings`, the same list the raid planner
   uses), legendary and mythical families excluded.  Each family is traded as
   its own members, base first: Pikachu, Raichu, Pichu -- not as "+pikachu",
   because a phone can be set to keep the last of each species.
2. **Whether it is needed.**  A family is traded when either phone holds no
   Pokemon of it tagged 98Plus (the account's own tag for 98%+ IVs).
3. **What can go.**  Only untagged, non-shiny, non-legendary, non-costumed,
   non-100% copies -- `TRADE_FILTER`.  A costume or a form the other account
   has not registered would make the trade a special or a new-Pokedex one;
   the trade worker's 100-Stardust check refuses those anyway.

Counts come from storage searches on each phone; nothing here touches a phone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

from . import raid_gamedata, raid_meta

# Copies a phone must keep of each species.  Set per phone in the private
# fleet config (`trade_keep_last: 1` under the device); a test may set it here.
KEEP_LAST: dict[str, int] = {}


def load_keep_last() -> dict[str, int]:
    """`trade_keep_last` of every fleet device, from the private config."""
    from . import config_paths, pokemon_fleet
    try:
        fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    except Exception:
        return {}
    out = {}
    for spec in fleet.devices.values():
        value = spec.config.get("trade_keep_last")
        if isinstance(value, int) and value > 0:
            out[spec.name] = value
    return out
DAILY_TRADES = 100
GOOD_TAG = "98Plus"
# Put in front of a species name.  `!#` leaves out every tagged Pokemon, 98Plus
# included; `!4*` keeps an untagged hundo home.
TRADE_FILTER = "!#&!shiny&!costume&!4*&!lucky&!shadow&!traded&"
# Legendary and mythical families are never on the list, so they need no filter;
# every character counts, since the search box shows only so many.
# How deep into each type's ranking a family counts as "highly rated".
RANK_DEPTH = raid_meta.SCAN_DEPTH
RANK_FLOOR = raid_meta.SCAN_FLOOR


@dataclass(frozen=True)
class Family:
    key: str                     # the family's base, e.g. "pichu"
    label: str                   # what to call it, e.g. "Pikachu (Mega Raichu Y)"
    species: tuple[str, ...]     # search names, most-caught first
    best: float                  # best share of a type's #1 any member reaches


def good_term(family: Family) -> str:
    return f"{GOOD_TAG}&+{family.species[0]}"


def trade_term(species: str) -> str:
    # The species goes last: a long search overflows the field, which then
    # shows only its end, and the end has to say which species it is.
    return f"{TRADE_FILTER}{species}"


@lru_cache(maxsize=1)
def _evolution_tree() -> tuple[dict[str, str], dict[str, int]]:
    """child -> parent, and id -> dex, from the PokeMiners game master."""
    miners = raid_gamedata._fetch("pokeminers-latest.json", raid_gamedata.POKEMINERS_URL)
    parent: dict[str, str] = {}
    dex: dict[str, int] = {}
    for template in miners:
        settings = (template.get("data") or {}).get("pokemonSettings")
        if not settings:
            continue
        pid = str(settings.get("pokemonId", "")).lower()
        match = re.match(r"V(\d{4})_POKEMON_", template.get("templateId", ""))
        if match:
            dex.setdefault(pid, int(match.group(1)))
        for branch in settings.get("evolutionBranch") or []:
            child = str(branch.get("evolution", "")).lower()
            if child and child != pid:
                parent.setdefault(child, pid)
    return parent, dex


def _members(base: str, parent: Mapping[str, str]) -> list[str]:
    children: dict[str, list[str]] = {}
    for child, up in parent.items():
        children.setdefault(up, []).append(child)
    out, queue = [], [base]
    while queue:
        node = queue.pop(0)
        if node not in out:
            out.append(node)
            queue.extend(sorted(children.get(node, [])))
    return out


def _base(pid: str, parent: Mapping[str, str]) -> str:
    seen = set()
    while pid in parent and pid not in seen:
        seen.add(pid)
        pid = parent[pid]
    return pid


# Babies are hatched, not caught: the common member of their family is the
# one after them, so it goes first in the trade order.
BABIES = {"pichu", "cleffa", "igglybuff", "togepi", "tyrogue", "smoochum", "elekid",
          "magby", "azurill", "wynaut", "budew", "chingling", "bonsly", "mime_jr",
          "happiny", "munchlax", "riolu", "mantyke", "toxel"}


def families(gd: raid_gamedata.GameData | None = None) -> list[Family]:
    """Highly rated, tradeable raid families, strongest first."""
    gd = gd or raid_gamedata.load()
    parent, _dex = _evolution_tree()
    best: dict[str, tuple[float, str]] = {}
    for attack_type, ratings in raid_meta.rankings().items():
        if not ratings:
            continue
        top = ratings[0].er
        for rating in ratings[:RANK_DEPTH]:
            share = rating.er / top
            if share < RANK_FLOOR:
                continue
            attacker = rating.attacker
            if attacker.legendary or attacker.shadow:
                # A shadow cannot be traded, so its family's ordinary copies
                # are not what makes it rate.
                continue
            pid = attacker.pokemon_id.split("_")[0] if attacker.pokemon_id not in parent else attacker.pokemon_id
            base = _base(pid, parent)
            if base == pid and pid not in {p for p in parent.values()}:
                continue  # does not evolve: no family to trade for (e.g. Kangaskhan)
            label = f"{attacker.name} ({attack_type} #{ratings.index(rating) + 1})"
            if base not in best or share > best[base][0]:
                best[base] = (share, label)
    out = []
    for base, (share, label) in best.items():
        members = [m for m in _members(base, parent) if m in gd.pokemon]
        species = [gd.pokemon[m]["search"] for m in members]
        if gd.pokemon.get(base, {}).get("legendary"):
            continue
        if species and species[0] in BABIES and len(species) > 1:
            species = species[1:] + species[:1]
        unique = tuple(dict.fromkeys(species))
        out.append(Family(base, label, unique, share))
    return sorted(out, key=lambda f: f.best, reverse=True)


# --- deciding counts ------------------------------------------------------------


@dataclass
class PhoneCounts:
    """What one phone's storage searches said."""

    name: str
    good: dict[str, int] = field(default_factory=dict)       # family key -> 98Plus count
    tradable: dict[str, int] = field(default_factory=dict)   # species -> TRADE_FILTER count
    owned: dict[str, int] = field(default_factory=dict)      # species -> every copy


# Fewer spares than this, and the copies wait for a Lucky Trade rather than an
# ordinary one: a lucky roll is worth more than a fresh one when so few are left.
SMALL = 3


@dataclass
class Batch:
    """`count` trades, each phone giving one of its `give` species."""

    family: Family
    give: dict[str, str]          # phone name -> species it sends
    count: int
    reason: str
    lucky_only: bool = False

    @property
    def label(self) -> str:
        return " for ".join(f"{name}'s {species}" for name, species in self.give.items())


def can_give(phone: PhoneCounts, species: str) -> int:
    spare = phone.tradable.get(species, 0)
    keep = KEEP_LAST.get(phone.name, 0)
    if keep:
        # The last `keep` copies stay, whichever copies they are.
        spare = min(spare, max(0, phone.owned.get(species, 0) - keep))
    return max(0, spare)


def filler(phone: PhoneCounts, fams: Sequence[Family], left: dict[str, int],
           avoid: str = "") -> str | None:
    """What a phone sends back when it has nothing of the family it needs:
    its most plentiful raid-family spare, never one of the few-copies kind."""
    best = None
    for family in fams:
        for species in family.species:
            if species == avoid:
                continue
            spare = left.get(species, 0)
            if spare >= SMALL and (best is None or spare > left.get(best, 0)):
                best = species
    return best


def plan_pair(a: PhoneCounts, b: PhoneCounts, fams: Sequence[Family],
              budget: int = DAILY_TRADES) -> list[Batch]:
    """Trades for one pair, strongest family first, never past the budget.

    For each family either phone has no 98Plus copy of, the phone that has
    spares gives them.  The other gives the same family when it can, or
    else its most plentiful raid-family spare (`filler`).  A species a phone
    has fewer than SMALL spares of goes only in a Lucky Trade.
    """
    left = {p.name: {s: can_give(p, s) for f in fams for s in f.species} for p in (a, b)}
    out: list[Batch] = []
    for family in fams:
        needers = [p for p in (a, b) if p.good.get(family.key, 0) == 0]
        if not needers:
            continue
        for donor in (a, b):
            other = b if donor is a else a
            if other not in needers:
                continue  # nothing of this family needs sending to that phone
            for species in family.species:
                while budget > 0 and left[donor.name].get(species, 0) > 0:
                    mine = left[donor.name][species]
                    # The other side gives the same family if it can, else a filler.
                    back = next((s for s in family.species if left[other.name].get(s, 0) > 0), None)
                    if back is None:
                        back = filler(other, fams, left[other.name])
                    if back is None:
                        break
                    count = min(mine, left[other.name][back], budget)
                    lucky = mine < SMALL or left[other.name][back] < SMALL
                    out.append(Batch(family, {donor.name: species, other.name: back}, count,
                                     f"no 98Plus {family.species[0]} on {other.name}", lucky))
                    left[donor.name][species] -= count
                    left[other.name][back] -= count
                    budget -= count
    return out


def swap_hints(here: Sequence[PhoneCounts], elsewhere: Iterable[PhoneCounts],
               fams: Sequence[Family]) -> list[str]:
    """Needs this Mac's pair cannot meet but a phone on the other Mac could."""
    others = list(elsewhere)
    hints = []
    for family in fams:
        for phone in here:
            if phone.good.get(family.key, 0):
                continue
            partner = next((p for p in here if p is not phone), None)
            if partner and any(can_give(partner, s) for s in family.species):
                continue
            donors = [o.name for o in others if any(can_give(o, s) for s in family.species)]
            if donors:
                hints.append(f"{phone.name} has no 98Plus {family.species[0]} and "
                             f"{partner.name if partner else 'its partner'} has none to trade; "
                             f"{' or '.join(donors)} could -- swap them onto one Mac to trade")
    return hints
