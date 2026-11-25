"""Rank verified reciprocal legendary offers for an already-confirmed Lucky Friends pair."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from functools import lru_cache
from itertools import product
from pathlib import Path

from . import config_paths, raid_gamedata, raid_math

MAX_AGE = timedelta(days=14)
# Encounter-only Adventure Effects may be missing from the game master's Elite list.
ENCOUNTER_MOVES = {"ROAROFTIME", "SPACIALREND", "DRAGONASCENT"}


def _ordinary(keys, attacker):
    return [
        key for key in keys if key not in attacker.elite and key not in ENCOUNTER_MOVES
    ]


def load_state(path: Path | None = None) -> dict:
    path = path or config_paths.state_dir() / "collect-trades" / "raid-offers.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def _fresh(timestamp, now):
    try:
        observed = datetime.fromisoformat(timestamp).astimezone(now.tzinfo)
    except (ValueError, TypeError):
        return False
    return timedelta(0) <= now - observed <= MAX_AGE


@lru_cache(maxsize=1)
def raid_roles():
    """Ordinary-TM investment potential in the offered form, against same-type peers.

    A fused, transformed, shadow or Mega form never lends its ranking to the base
    Pokémon being sent. An Elite move already on a particular offer can instead
    be evaluated from its individually observed moves.
    """
    data = raid_gamedata.load()
    pool = [a for a in raid_math.attackers(data) if not a.shadow and not a.mega]
    roles = {}
    for attack_type in raid_gamedata.TYPES:
        ratings = []
        for attacker in pool:
            if attack_type not in attacker.types:
                continue
            ordinary = raid_math.best_rating(
                data,
                attacker,
                attack_type,
                fast=_ordinary(attacker.fast, attacker),
                charged=_ordinary(attacker.charged, attacker),
            )
            if ordinary:
                ratings.append(ordinary)
        ratings.sort(key=lambda r: r.er, reverse=True)
        if not ratings:
            continue
        best = ratings[0].er
        for rank, rating in enumerate(ratings, 1):
            roles.setdefault(rating.attacker.pokemon_id, []).append(
                {
                    "type": attack_type,
                    "rank": rank,
                    "relative": rating.er / best,
                    "moveset": rating.moveset,
                    "basis": "ordinary TMs at equal level",
                }
            )
    return roles


def role_for(form, observed_moves=()):
    """Use the observed moves when they materially improve the offered form's role."""
    roles = raid_roles().get(form, [])
    if not roles:
        return None
    best = max(roles, key=lambda r: r["relative"])
    if not observed_moves:
        return dict(best)
    data = raid_gamedata.load()
    actual = [data.move_by_name(name) for name in observed_moves]
    actual = [move for move in actual if move is not None]
    attacker = next(
        (
            a
            for a in raid_math.attackers(data)
            if a.pokemon_id == form and not a.mega and not a.shadow
        ),
        None,
    )
    if attacker is None:
        return dict(best)
    for role in roles:
        known = raid_math.best_rating(
            data,
            attacker,
            role["type"],
            fast=[m.key for m in actual if m.fast],
            charged=[m.key for m in actual if not m.fast],
        )
        if not known:
            continue
        ordinary = raid_math.best_rating(
            data,
            attacker,
            role["type"],
            fast=_ordinary(attacker.fast, attacker),
            charged=_ordinary(attacker.charged, attacker),
        )
        ratio = role["relative"] * known.er / ordinary.er
        if ratio >= best["relative"] - 0.001:
            best = {
                "type": role["type"],
                "rank": None,
                "relative": ratio,
                "moveset": known.moveset,
                "basis": "observed moves at equal level",
            }
    return dict(best)


def _value(offer, donor, recipient, now, *, for_refresh=False):
    if (
        not offer.get("live_verified")
        or offer.get("unique_count") != 1
        or (not for_refresh and not _fresh(offer.get("observed_at"), now))
    ):
        return None
    dex = str(offer["dex"])
    forms = [
        key
        for key, record in raid_gamedata.load().pokemon.items()
        if record["dex"] == int(dex)
    ]
    if (
        offer.get("form") not in forms
        or len(forms) > 1
        and not (offer.get("form_verified") and offer.get("form_token"))
    ):
        return None
    coverage = recipient.get("coverage", {}).get(dex, {})
    stock = donor.get("coverage", {}).get(dex, {})
    if offer.get("form_token"):
        coverage = coverage.get("forms", {}).get(offer["form"], {})
        stock = stock.get("forms", {}).get(offer["form"], {})
    if not for_refresh and not all(_fresh(row.get("observed_at"), now) for row in (coverage, stock)):
        return None
    if (
        coverage.get("owned") is None
        or coverage.get("hundo_count") is None
        or not stock.get("owned")
        or not offer.get("eligible_count")
        or coverage.get("hundo_levels_complete") is False
    ):
        return None
    role = offer.get("raid_role") or role_for(offer["form"], offer.get("moves", ()))
    if not role or role["relative"] < 0.72:
        return None
    if coverage.get("maxed_hundo") and coverage["owned"] >= 3:
        return None
    # Relative raid rating dominates. Among similarly strong attackers favor
    # low stock and the absence of an already-powered high-IV representative.
    strength = min(1.2, role["relative"])
    scarcity = 1 / math.sqrt(1 + coverage["owned"])
    score = 65 * strength + 26 * scarcity
    if coverage["hundo_count"]:
        score -= 8
    if coverage.get("maxed_hundo"):
        score -= 27
    elif (coverage.get("best_high_iv_level") or 0) >= 40:
        score -= 13
    elif (coverage.get("best_high_iv_level") or 0) >= 35:
        score -= 5
    if stock["owned"] == 1:
        score -= 14
    elif stock["owned"] == 2:
        score -= 5
    prepared = dict(offer, donor_owned=stock["owned"], raid_role=role)
    rationale = (
        f"{offer['name']} CP {offer['cp']}: {role['type']} role, {role['relative']:.0%} "
        f"of the ordinary-TM type leader at equal level; {coverage['owned']} owned, "
        f"{coverage['hundo_count']} perfect on recipient"
    )
    if coverage.get("maxed_hundo"):
        rationale += ", including a maxed perfect"
    if stock["owned"] <= 2:
        rationale += f"; donor retains {stock['owned'] - 1} of the species"
    return score, prepared, dict(coverage, dex=int(dex)), rationale


def rank_swaps(state, a, b, *, now=None, for_refresh=False):
    """Rank reciprocal pairs; the weaker direction receives equal weight to the pair average."""
    now = now or datetime.now().astimezone()
    devices = state.get("devices", {})
    if a == b or a not in devices or b not in devices:
        return []
    one, two = devices[a], devices[b]
    outgoing = [v for o in one.get("offers", []) if (v := _value(o, one, two, now, for_refresh=for_refresh))]
    returning = [v for o in two.get("offers", []) if (v := _value(o, two, one, now, for_refresh=for_refresh))]
    swaps = []
    for left, right in product(outgoing, returning):
        score = 0.5 * min(left[0], right[0]) + 0.25 * (left[0] + right[0])
        swaps.append(
            {
                "give": {a: left[1], b: right[1]},
                "coverage": {a: right[2], b: left[2]},
                "reasons": {a: right[3], b: left[3]},
                "score": round(score, 3),
            }
        )
    return sorted(swaps, key=lambda s: s["score"], reverse=True)


def invalidate_accounts(names, *, changed_dex=()):
    """Expire the exchanged species on both accounts and preserve other discoveries."""
    from . import raid_offer_scan

    raid_offer_scan.publish_accounts(
        config_paths.state_dir() / "collect-trades", {}, invalidate=names, changed_dex=changed_dex
    )


async def refresh_state(names, owners, machines, fleet, shots, *, pairs=None):
    """Refresh missing or stale live proposal evidence on the coordinator."""
    from . import raid_offer_scan

    directory = config_paths.state_dir() / "collect-trades"
    state = load_state()
    checkpoints = {}
    for name in names:
        path = directory / "raid-offer-accounts" / f"{name}.json"
        if path.is_file():
            checkpoints[name] = json.loads(path.read_text())
    if checkpoints:
        state = raid_offer_scan.publish_accounts(directory, checkpoints)
    from .collector_proposals import _eligible_swaps, proposal_itinerary

    requested = [
        tuple(pair) for pair in (pairs or ([names] if len(names) == 2 else []))
    ]
    picks, conflicts = proposal_itinerary(requested, state, fleet)
    if requested and len(picks) == len(requested) and not conflicts:
        print(
            "[raid-offers] Reusing fresh, individually verified reciprocal offers for this quote.",
            flush=True,
        )
        return state
    # Yesterday's exact offers are discovery hints. Recheck their species on
    # both accounts before considering a new sweep of the whole shortlist.
    known = {name: {o["dex"] for o in state.get("devices", {}).get(name, {}).get("offers", ())
                    if o.get("live_verified") and o.get("unique_count") == 1}
             for name in names}
    if requested and known and all(known.values()):
        dexes = set()
        for pair in requested:
            ranked = _eligible_swaps(pair, state, fleet, for_refresh=True)
            if ranked:
                dexes.update(ranked[0]["give"][n]["dex"] for n in pair)
            else:
                # Interrupted discovery may have individual offers without complete
                # recipient evidence. Seed a bounded first pass for those accounts.
                for n in pair:
                    dexes.update([d for d in raid_offer_scan.SHORTLIST if d in known[n]][:2])
        shortlist = [dex for dex in raid_offer_scan.SHORTLIST if dex in dexes]
        if 0 < len(shortlist) < len(raid_offer_scan.SHORTLIST):
            print(f"[raid-offers] Checking {len(shortlist)} previously identified candidate species.", flush=True)
            state = await raid_offer_scan.refresh(names, owners, machines, fleet, shots, shortlist=shortlist)
            picks, conflicts = proposal_itinerary(requested, state, fleet)
            if len(picks) == len(requested) and not conflicts:
                return state
    return await raid_offer_scan.refresh(names, owners, machines, fleet, shots)
