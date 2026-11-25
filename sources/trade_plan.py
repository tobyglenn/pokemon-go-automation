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
# and dontrade included; `!dontrade`, `!xs`, `!xl`, `!xxs`, `!xxl`, `!favorite`, `!purified`, and `!4*` keep protected ones home.
TRADE_FILTER = "!#&!dontrade&!xs&!xl&!xxs&!xxl&!favorite&!shiny&!costume&!purified&!4*&!lucky&!shadow&!traded&"
# Never propose trading these families out via raid trade plans (e.g. collector exclusives on receiver).
EXCLUDED_FAMILIES: frozenset[str] = frozenset({"glimmet"})
# Legendary and mythical families are never on the list, so they need no filter;
# every character counts, since the search box shows only so many.
# How deep into each type's ranking a family counts as "highly rated".
RANK_DEPTH = raid_meta.SCAN_DEPTH
RANK_FLOOR = raid_meta.SCAN_FLOOR
EXPANDED_DEPTH = 15
EXPANDED_FLOOR = 0.55


@dataclass(frozen=True)
class Family:
    key: str                     # the family's base, e.g. "pichu"
    label: str                   # what to call it, e.g. "Pikachu (Mega Raichu Y)"
    species: tuple[str, ...]     # search names, most-caught first
    best: float                  # best share of a type's #1 any member reaches


def good_term(family: Family) -> str:
    return f"{GOOD_TAG}&+{family.species[0]}"


def trade_term(species: str, *, allow_costume: bool = False) -> str:
    # The species goes last: a long search overflows the field, which then
    # shows only its end, and the end has to say which species it is.
    filters = TRADE_FILTER.replace("!costume&", "") if allow_costume else TRADE_FILTER
    return f"{filters}{species}"


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


def _search_name_for(member: str, gd: raid_gamedata.GameData) -> str | None:
    """Resolve a PokeMiners species id (including multi-form bases) to its search term."""
    entry = gd.pokemon.get(member)
    if entry is not None:
        return entry["search"]
    prefix = f"{member}_"
    for key in sorted(gd.pokemon):
        if key.startswith(prefix):
            return gd.pokemon[key]["search"]
    return None


def families(
    gd: raid_gamedata.GameData | None = None,
    *,
    expanded: bool = False,
    depth: int | None = None,
    floor: float | None = None,
) -> list[Family]:
    """Highly rated, tradeable raid families, strongest first.

    With `expanded=True`, uses deeper rankings (`depth=15`, `floor=0.55`),
    includes families whose Shadow forms place high (ordinary copies roll IVs
    and earn XL candy), and includes single-stage top Mega/raid attackers.
    """
    gd = gd or raid_gamedata.load()
    parent, _dex = _evolution_tree()
    parent_bases = set(parent.values())
    max_depth = depth if depth is not None else (EXPANDED_DEPTH if expanded else RANK_DEPTH)
    min_share = floor if floor is not None else (EXPANDED_FLOOR if expanded else RANK_FLOOR)
    best: dict[str, tuple[float, str]] = {}
    for attack_type, ratings in raid_meta.rankings().items():
        if not ratings:
            continue
        top = ratings[0].er
        for rating in ratings[:max_depth]:
            share = rating.er / top
            if share < min_share:
                continue
            attacker = rating.attacker
            if attacker.legendary or (not expanded and attacker.shadow):
                # In strict mode, Shadow-only placements are excluded; in expanded
                # mode, ordinary family copies still farm XL candy and 98%+ lucky/hundo rolls.
                continue
            pid = attacker.pokemon_id.split("_")[0] if attacker.pokemon_id not in parent else attacker.pokemon_id
            base = _base(pid, parent)
            if base in EXCLUDED_FAMILIES:
                continue
            if not expanded and base == pid and pid not in parent_bases:
                continue  # does not evolve: no family to trade for (e.g. Kangaskhan)
            label = f"{attacker.name} ({attack_type} #{ratings.index(rating) + 1})"
            if base not in best or share > best[base][0]:
                best[base] = (share, label)
    out = []
    for base, (share, label) in best.items():
        if gd.pokemon.get(base, {}).get("legendary"):
            continue
        species = [s for m in _members(base, parent) if (s := _search_name_for(m, gd))]
        if not species:
            continue
        if species[0] in BABIES and len(species) > 1:
            species = species[1:] + species[:1]
        unique = tuple(dict.fromkeys(species))
        out.append(Family(base, label, unique, share))
    return sorted(out, key=lambda f: f.best, reverse=True)


def expanded_families(
    gd: raid_gamedata.GameData | None = None,
    *,
    depth: int = EXPANDED_DEPTH,
    floor: float = EXPANDED_FLOOR,
) -> list[Family]:
    """Expanded high-value raid, Mega, and Shadow-line families for daily 100-trade runs."""
    return families(gd, expanded=True, depth=depth, floor=floor)


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


def filler(
    phone: PhoneCounts,
    fams: Sequence[Family],
    left: dict[str, int],
    avoid: str = "",
    preferred_needs: Mapping[str, int] | None = None,
) -> str | None:
    """What a phone sends back when it has nothing of the family it needs:
    a plentiful raid-family spare (`>= SMALL`), never one of the few-copies kind.

    When `preferred_needs` (the partner's 98Plus counts) is provided, prefers
    families where the partner still has fewer 98Plus copies, strongest family first.
    """
    best = None
    best_rank: tuple[int, int, float, int] | None = None
    for family in fams:
        partner_good = preferred_needs.get(family.key, 0) if preferred_needs is not None else 0
        need_tier = 2 if (preferred_needs is not None and partner_good == 0) else (
            1 if (preferred_needs is not None and partner_good < 6) else 0
        )
        for species in family.species:
            if species == avoid:
                continue
            spare = left.get(species, 0)
            if spare < SMALL:
                continue
            if preferred_needs is None:
                if best is None or spare > left.get(best, 0):
                    best = species
                continue
            rank = (need_tier, 1 if spare >= SMALL else 0, family.best, spare)
            if best_rank is None or rank > best_rank:
                best = species
                best_rank = rank
    return best


def _record_batch(
    out: list[Batch],
    left: dict[str, dict[str, int]],
    family: Family,
    donor_name: str,
    donor_species: str,
    other_name: str,
    other_species: str,
    budget: int,
    reason: str,
    *,
    count_lucky_in_budget: bool,
) -> int:
    mine = left[donor_name].get(donor_species, 0)
    theirs = left[other_name].get(other_species, 0)
    if mine <= 0 or theirs <= 0 or budget <= 0:
        return budget
    lucky = mine < SMALL or theirs < SMALL
    count = min(mine, theirs, budget)
    out.append(Batch(family, {donor_name: donor_species, other_name: other_species}, count, reason, lucky))
    if lucky and not count_lucky_in_budget:
        # Suggestions do not execute automatically; only consume <SMALL sides so
        # the >=SMALL partner keeps its real tradable spares for other batches.
        if mine < SMALL:
            left[donor_name][donor_species] -= count
        if theirs < SMALL:
            left[other_name][other_species] -= count
        return budget
    left[donor_name][donor_species] -= count
    left[other_name][other_species] -= count
    return budget - count


def _mirror_pair(
    a: PhoneCounts,
    b: PhoneCounts,
    family: Family,
    left: dict[str, dict[str, int]],
    min_spares: int = SMALL,
) -> tuple[str, str] | None:
    """Two species in `family` both phones hold >= `min_spares` of (same species first)."""
    for sp in family.species:
        if left[a.name].get(sp, 0) >= min_spares and left[b.name].get(sp, 0) >= min_spares:
            return sp, sp
    sa = next((s for s in family.species if left[a.name].get(s, 0) >= min_spares), None)
    sb = next((s for s in family.species if left[b.name].get(s, 0) >= min_spares), None)
    return (sa, sb) if sa is not None and sb is not None else None


def _next_surplus(
    phone: PhoneCounts,
    fams: Sequence[Family],
    left: dict[str, dict[str, int]],
    min_spares: int = SMALL,
) -> tuple[Family, str] | None:
    for family in fams:
        for species in family.species:
            if left[phone.name].get(species, 0) >= min_spares:
                return family, species
    return None


def plan_pair(
    a: PhoneCounts,
    b: PhoneCounts,
    fams: Sequence[Family],
    budget: int = DAILY_TRADES,
    *,
    target_good: int = 1,
    fill_budget: bool = False,
) -> list[Batch]:
    """Trades for one pair, strongest family first, never past the budget.

    Tier 1 (`good == 0`): either phone has no 98Plus copy of the family.
    Tier 2 (`0 < good < target_good`): either phone wants deeper 98Plus coverage;
        mirror trades within the family run first, then cross-family fills.
    Tier 3 (`fill_budget=True`): fill remaining daily budget up to 100 with
        high-value mirror IV/XL rerolls first, then cross-family IV/XL rerolls.
    """
    left = {p.name: {s: can_give(p, s) for f in fams for s in f.species} for p in (a, b)}
    out: list[Batch] = []
    # Tier 1: families with zero 98Plus on either phone.
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
                    back = next((s for s in family.species if left[other.name].get(s, 0) > 0), None)
                    if back is None:
                        back = filler(
                            other,
                            fams,
                            left[other.name],
                            avoid=species,
                            preferred_needs=donor.good if fill_budget else None,
                        )
                    if back is None:
                        break
                    prev_budget = budget
                    prev_mine = left[donor.name][species]
                    budget = _record_batch(
                        out,
                        left,
                        family,
                        donor.name,
                        species,
                        other.name,
                        back,
                        budget,
                        f"no 98Plus {family.species[0]} on {other.name}",
                        count_lucky_in_budget=not fill_budget,
                    )
                    if budget == prev_budget and left[donor.name][species] == prev_mine:
                        break

    # Tier 2: deepen 98Plus coverage up to `target_good` (mirrors first, then cross-fills).
    if target_good > 1 and budget > 0:
        for family in fams:
            if not any(p.good.get(family.key, 0) < target_good for p in (a, b)):
                continue
            while budget > 0:
                pair_sp = _mirror_pair(a, b, family, left, SMALL)
                if pair_sp is None:
                    break
                sa, sb = pair_sp
                budget = _record_batch(
                    out,
                    left,
                    family,
                    a.name,
                    sa,
                    b.name,
                    sb,
                    budget,
                    f"mirror (<{target_good} 98Plus {family.species[0]})",
                    count_lucky_in_budget=True,
                )
        for family in fams:
            needers = [p for p in (a, b) if p.good.get(family.key, 0) < target_good]
            if not needers:
                continue
            for donor in (a, b):
                other = b if donor is a else a
                if other not in needers:
                    continue
                for species in family.species:
                    while budget > 0 and left[donor.name].get(species, 0) >= SMALL:
                        back = next((s for s in family.species if left[other.name].get(s, 0) >= SMALL), None)
                        if back is None:
                            back = filler(
                                other,
                                fams,
                                left[other.name],
                                avoid=species,
                                preferred_needs=donor.good,
                            )
                        if back is None:
                            break
                        budget = _record_batch(
                            out,
                            left,
                            family,
                            donor.name,
                            species,
                            other.name,
                            back,
                            budget,
                            f"<{target_good} 98Plus {family.species[0]} on {other.name}",
                            count_lucky_in_budget=True,
                        )

    # Tier 3: fill remaining daily trades with high-value IV/XL rerolls.
    if fill_budget and budget > 0:
        for family in fams:
            while budget > 0:
                pair_sp = _mirror_pair(a, b, family, left, SMALL)
                if pair_sp is None:
                    break
                sa, sb = pair_sp
                budget = _record_batch(
                    out,
                    left,
                    family,
                    a.name,
                    sa,
                    b.name,
                    sb,
                    budget,
                    f"mirror IV/XL reroll ({family.species[0]})",
                    count_lucky_in_budget=True,
                )
        while budget > 0:
            pick_a = _next_surplus(a, fams, left, SMALL)
            pick_b = _next_surplus(b, fams, left, SMALL)
            if pick_a is None or pick_b is None:
                break
            fam_a, sa = pick_a
            fam_b, sb = pick_b
            top_fam = fam_a if fam_a.best >= fam_b.best else fam_b
            budget = _record_batch(
                out,
                left,
                top_fam,
                a.name,
                sa,
                b.name,
                sb,
                budget,
                "high-value IV/XL reroll",
                count_lucky_in_budget=True,
            )
    return out


def shortfall_hints(
    here: Sequence[PhoneCounts],
    elsewhere: Iterable[PhoneCounts],
    fams: Sequence[Family],
    planned: int,
    budget: int,
) -> list[str]:
    """Explain when this computer's pair falls short of `budget` and another host holds spares."""
    if planned >= budget:
        return []
    totals = [
        (o.name, sum(can_give(o, s) for f in fams for s in f.species if can_give(o, s) >= SMALL))
        for o in elsewhere
    ]
    rich = [f"{name} ({n} spares)" for name, n in totals if n > 0]
    if not rich:
        return []
    names = " + ".join(p.name for p in here)
    return [
        f"{names} planned {planned}/{budget} trades; other host holds {', '.join(rich)}"
    ]


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
