#!/usr/bin/env python3
"""Which species a raid scan has to look for, and how each type ranks them.

There is no hand-maintained roster.  The rankings are computed from the game
master by `raid_math` -- every released form, shadow and Mega, rated with its
best moveset of each attacking type -- and the scan list is simply every base
species that places in some type's top list.  A new Mega or a rebalanced move
reaches the plan on the next day's game-master refresh, and nothing can be
listed that the game does not contain (or listed under a species that cannot
become it: the old roster searched "kubfu" for Baxcalibur).
"""

from __future__ import annotations

from functools import lru_cache

from . import raid_gamedata, raid_inventory, raid_math
from .raid_gamedata import TYPES

# How deep each type's list goes.  Megas are counted separately because a
# party fields one Mega at a time: the non-Mega depth is what fills the rest.
TOP_MEGAS = 6
TOP_REGULAR = 15
# The scan only looks for what could be worth candy: a type's top ten, Megas
# included, and nothing under 60% of its #1.  Deeper than that -- Shadow
# Crawdaunt at #20 Water, 51% -- cost about a minute per species per phone
# for attackers nobody builds.
SCAN_DEPTH = 10
SCAN_FLOOR = 0.60


@lru_cache(maxsize=1)
def rankings() -> dict[str, list[raid_math.Rating]]:
    """Per type: the top Megas then the top non-Megas, each in rating order."""
    gd = raid_gamedata.load()
    out: dict[str, list[raid_math.Rating]] = {}
    pool = raid_math.attackers(gd)
    for attack_type in TYPES:
        ranked = raid_math.rank_type(gd, attack_type, pool, top=len(pool)).ratings
        megas = [r for r in ranked if r.attacker.mega][:TOP_MEGAS]
        regular = [r for r in ranked if not r.attacker.mega][:TOP_REGULAR]
        out[attack_type] = sorted(megas + regular, key=lambda r: r.er, reverse=True)
    return out


def scan_terms() -> list[tuple[str, str, bool]]:
    """`(search, display, shadow_eligible)` per species to scan.

    The search is the Pokédex number: the game matches it exactly, across every
    form, and it does not depend on the account's language or on nicknames.
    """
    gd = raid_gamedata.load()
    wanted: dict[int, tuple[str, bool]] = {}
    for ratings in rankings().values():
        top = ratings[0].er if ratings else 0.0
        for rating in ratings[:SCAN_DEPTH]:
            if rating.er < top * SCAN_FLOOR:
                continue
            attacker = rating.attacker
            name, shadow = wanted.get(attacker.dex, (attacker.search.title(), False))
            wanted[attacker.dex] = (name, shadow)
    for p in gd.pokemon.values():
        if p["dex"] in wanted and p["shadow"]:
            wanted[p["dex"]] = (wanted[p["dex"]][0], True)
    return [(raid_inventory.dex_term(dex), name, shadow)
            for dex, (name, shadow) in sorted(wanted.items())]
