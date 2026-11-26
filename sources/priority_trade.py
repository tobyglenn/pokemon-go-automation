"""Fill the fleet's daily trade budgets, giving the receiver first pick."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Sequence

from . import (
    collect_trades as collect,
    collector_day,
    config_paths,
    fleet_entrypoint,
    pokemon_fleet,
    remote_target,
    starter,
    starter_trade,
    storage_favorites,
    storage_review,
    trade_favorite_names,
    trade_local,
    trade_plan,
    trading_accounts,
)

PRIORITY_TIERS = ("XXL/XXS", trade_favorite_names.LUCKY, trade_favorite_names.HUNDO)
# Donor-to-donor order: valuable spares first (the
# collector's daily-value lists: pseudo-legendaries and curated valuable
# species, low IV, for the reroll and Lucky chance), then far catches for the
# distance candy, and the plain local lists only to fill the daily cap.
VALUE_TIER = "Valuable"
# LocalTrade fills only the final remaining slots.
PEER_TIERS = ("FarTrade", "FarNot98", "LocalNot98", "LocalTrade")
EXACT_DEX = re.compile(r"[1-9]\d*")
# A pair that fails for a passing reason (a friend row not drawn yet, a
# picker that opened late) rests this long, then is tried again.
COOLDOWN = 600.0
# A device lock held by another fleet command is brief; check back soon.
BUSY_SIGN = "already controlled by"
BUSY_RETRY = 120.0
# Messages meaning one phone's search text never arrived intact.
TYPING_FAILURES = ("did not show the requested query ending", "did not retain full")


@dataclass(frozen=True)
class Plan:
    phase1: dict[str, int]
    phase2: dict[tuple[str, str], int]
    first_donor: str | None = None


def _pair(a: str, b: str) -> tuple[str, str]:
    return min(a, b), max(a, b)


def _allocations(caps, total):
    """Enumerate allocations that spend the full feasible receiver budget."""
    if not caps:
        if total == 0:
            yield ()
        return
    head, *tail = caps
    for take in range(min(head, total), max(0, total - sum(tail)) - 1, -1):
        for rest in _allocations(tail, total - take):
            yield (take, *rest)


def _peer_counts(residual, opposite, prefer=None):
    """Best peer allocation on a graph of at most three donors.

    For T peer trades, donor i spends T minus the trades on its opposite
    edge. That edge therefore needs at least max(0, T - residual[i])
    trades. If the edge is absent, T cannot exceed residual[i].
    """
    if not any(opposite):
        return {}
    stock = sum(residual)
    total = min(stock // 2, stock - max(residual),
                *(r for r, edge in zip(residual, opposite) if edge is None))
    lower = [max(0, total - r) for r in residual]
    slack = total - sum(lower)
    if slack:
        # Put the slack on one edge. Prefer a donor that cannot reach its
        # cap anyway, then use the requested edge if it costs no capped account.
        allowed = [i for i, edge in enumerate(opposite) if edge is not None]
        best = [i for i in allowed if residual[i] > total] or allowed
        i = next((i for i in best if opposite[i] == prefer), best[0])
        lower[i] += slack
    return {edge: n for edge, n in zip(opposite, lower) if edge is not None and n}


def _plan_budget(remaining, receiver, priority_donors, peer_edges, prefer=None, *, parallel=False, starters=None):
    donors = sorted((n for n in remaining if n != receiver), key=lambda n: (-remaining[n], n))
    if len(donors) > 3:
        raise ValueError("priority trading supports at most three donors")
    names = donors + [None] * (3 - len(donors))
    opposite = []
    for i in range(3):
        a, b = (names[j] for j in range(3) if j != i)
        edge = _pair(a, b) if a is not None and b is not None else None
        opposite.append(edge if edge in peer_edges else None)

    caps = [remaining[n] if n in priority_donors else 0 for n in donors]
    first_total = min(remaining[receiver], sum(caps))
    best_score = None
    best = Plan({}, {})
    for pulls in _allocations(caps, first_total):
        first = dict(zip(donors, pulls))
        balances = {n: remaining[n] - take for n, take in first.items()}
        residual = [balances.get(n, 0) for n in names]
        # starters limits which donor lane R may open now (the current tier);
        # priority_donors still reserves budget for every later tier.
        starts = ([n for n, take in first.items() if take and (starters is None or n in starters)]
                  if parallel and first_total else [None])
        if not starts:
            continue
        for donor in starts:
            complement = opposite[donors.index(donor)] if donor is not None else None
            peer = _peer_counts(residual, opposite, complement if donor is not None else prefer)
            spent = dict.fromkeys(donors, 0)
            for (a, b), count in peer.items():
                spent[a] += count
                spent[b] += count
            capped = int(first_total == remaining[receiver]) + sum(
                balances[n] == spent[n] for n in donors
            )
            # Keep both lanes working when it doesn't cost a cap or a trade.
            # Among equally good assignments, keep the open pair.
            concurrent = int(complement in peer)
            sticky = int(prefer is not None and (
                donor == prefer if donor is not None else first.get(prefer, 0) > 0 or prefer in peer
            ))
            score = (capped, first_total + sum(peer.values()), concurrent, sticky, pulls)
            if best_score is None or score > best_score:
                best_score = score
                best = Plan(first, peer, donor)
    return best


def plan_budget(remaining: dict[str, int], receiver: str) -> Plan:
    """Spend the feasible receiver budget first, then maximize capped accounts.

    Break ties by total trades, then by pulls from donors in descending order
    of remaining allowance. Omit offline donors; include capped accounts as 0.
    This inventory-blind plan assumes every included pair can trade.
    """
    if receiver not in remaining or any(type(n) is not int or n < 0 for n in remaining.values()):
        raise ValueError("remaining must include the receiver and contain nonnegative integer budgets")
    donors = sorted(set(remaining) - {receiver})
    return _plan_budget(remaining, receiver, set(donors), set(combinations(donors, 2)))


@dataclass
class WholeFavorite:
    """An intact, non-single-species favorite; its count is not a reserve count."""

    current: collect.TargetSpec
    available: bool = True
    skip: int = 0
    count: int = 0
    labels: list[str] = field(default_factory=list)

    async def prepare(self, phone):
        pass

    async def pick(self, phone):
        self.labels.clear()
        named = await _open_named(phone, self.current.name, self.current.term)
        self.count = await collect.choose_filtered_tile(
            phone, self.current.term, self.current.name,
            skip_index=self.skip, picked_species=self.labels,
            **({"preapplied": True} if named else {}),
        )

    def advance(self):
        self.available = False

    def used(self):
        if self.count == 1:
            self.advance()


# (handset, favorite) pairs whose pinned copy could not be verified; those
# fall back to typing for the rest of the run.
_UNPINNED: set[tuple[str, str]] = set()


async def _open_named(phone, label, term):
    """Open the pinned, verified favorite instead of typing a long query.

    The SE's WDA typing truncated or garbled 600-1,000 character searches
    (24 Nov 2026: LocalTrade read 0 with a ton in storage). The pinned copy is
    checked against `term` in full before any tile is touched."""
    from . import trade_saved_search as saved

    handset = getattr(phone, "transport_name", phone.name)
    if (handset, label) in _UNPINNED:
        return False
    try:
        image, boxes = await phone.read()
        await saved.open_named_picker(phone, trade_favorite_names.label(label), term, image, boxes)
        return True
    except (starter.StepFailed, RuntimeError, ValueError) as exc:
        if isinstance(exc, trade_local.TradeOver):
            raise
        _UNPINNED.add((handset, label))
        _log(f"{phone.name}: pinned {label} unavailable ({exc}); typing it instead")
        return False


def _favorite_pool(label, term, keep):
    dex = next((part.strip() for part in term.split("&")
                if EXACT_DEX.fullmatch(part.strip())), None)
    if dex is None:
        return WholeFavorite(collect.TargetSpec(label, "", term))
    # Offers' reserve check requires a trailing exact Dex clause. If needed,
    # repeat an existing intersection clause without changing the result set.
    query = term if term.endswith(f"&{dex}") or term == dex else f"{term}&{dex}"
    return collector_day.Offers([collect.TargetSpec(label, dex, query)], keep)


def _first_available(pools):
    return next((pool for pool in pools if pool.available), None)


class _SideOver(trade_local.TradeOver):
    def __init__(self, side, exc):
        super().__init__(str(exc))
        self.side = side


class _SideRefused(trade_local.TradeRefused):
    def __init__(self, side, exc):
        super().__init__(str(exc))
        self.side = side


async def _on_side(phone, action):
    try:
        return await action(phone)
    except trade_local.TradeOver as exc:
        raise _SideOver(phone.name, exc) from exc
    except trade_local.TradeRefused as exc:
        raise _SideRefused(phone.name, exc) from exc


def _exception_side(exc, pair):
    return getattr(exc, "side", None) or next(
        (n for n in pair if str(exc).startswith(f"{n}:")), None,
    )


def _return_pool(cfg, clear_path, keep):
    terms = collect.build_return_queries(
        clear_path,
        exclude_dex=cfg.get("exclude_return_dex", collect.DEFAULT_EXCLUDE_RETURN_DEX),
        return_filters=cfg.get("return_filters", collect.REQUIRED_RETURN_FILTERS),
        return_dex_tiers=cfg.get("return_dex_tiers"),
        custom_return_query=cfg.get("custom_return_query"),
        fallback_return_query=cfg.get("fallback_return_query", collect.DEFAULT_FALLBACK_RETURN_QUERY),
    )
    targets = [collect.TargetSpec(f"return-{i}", term.rsplit("&", 1)[-1], term)
               for i, term in enumerate(terms, 1)]
    return collector_day.Offers(targets, keep, collector_day.species_catalog(clear_path))


def _value_targets(clear_path, cfg, lucky):
    """Keep the receiver's Lucky Dex targets out of donor-to-donor value searches."""
    from . import clear_pokemon

    donor_targets, _ = collector_day.valuable_queries(clear_path, cfg, None)
    lucky = set(lucky)
    kept = []
    for target in donor_targets:
        prefix, _, ranges = target.term.rpartition("&")
        dex = clear_pokemon.parse_dex_ranges(ranges) - lucky
        if not prefix or not dex:
            continue
        query = clear_pokemon.format_dex_ranges(sorted(dex))
        kept.append(collect.TargetSpec(f"{VALUE_TIER}-{len(kept) + 1}", query, f"{prefix}&{query}"))
    return kept


def _log(message):
    print(f"[priority] {message}", flush=True)


@dataclass(frozen=True)
class _Job:
    pair: tuple[str, str]
    pools: dict[str, WholeFavorite | collector_day.Offers]
    tier: str
    peer_pair: tuple[str, str] | None = None
    peer_until: int = 0


@dataclass
class _Lane:
    held: tuple[str, str] | None = None
    active: _Job | None = None


async def run(args: argparse.Namespace) -> int:
    day = date.today()
    cfg = collect.load_config()
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    machines = fleet_entrypoint.machine_configs()
    survey = machines
    if args.local:
        local = fleet_entrypoint.local_machine(machines)
        survey = {local: machines[local]}
    owners, snapshots = remote_target.discover_attached_devices(fleet, survey)
    aliases = trading_accounts.aliases()
    fleet, owners = trading_accounts.route_fleet(fleet, owners, aliases)
    for machine, snapshot in snapshots.items():
        for error in snapshot.errors:
            _log(f"{machine} discovery incomplete: {error}")

    receiver = args.receiver or cfg.get("receiver")
    receiver = aliases.get(receiver, receiver)
    if receiver not in fleet.devices:
        raise pokemon_fleet.FleetError(f"Unknown or unconfigured receiver: {receiver!r}")
    cfg = {**cfg, "receiver": receiver}
    clear_name = str(cfg.get("clear_config") or collect.clear_pokemon.CONFIG_NAME)
    clear_path = (Path(config_paths.expand_user_path(clear_name))
                  if "/" in clear_name or clear_name.startswith("~")
                  else config_paths.default_config(clear_name))
    policy = storage_review.load_policy(clear_path, cfg)
    excluded = {aliases.get(n, n) for n in args.exclude_devices}
    if receiver in excluded:
        raise pokemon_fleet.FleetError("The selected receiver cannot be excluded")
    donors = list(dict.fromkeys(aliases.get(n, n) for n in policy.donors))
    donors = [n for n in donors if n != receiver and n not in excluded]
    unknown = set(donors) - fleet.devices.keys()
    if unknown or len(donors) > 3:
        raise pokemon_fleet.FleetError(f"Expected up to three configured donors; got {donors!r}")
    roster = [receiver, *donors]

    # --local restricts the handset survey, not the shared account journal.
    history = await asyncio.to_thread(collector_day.day_counts, roster, machines, day)
    totals = dict(history)
    blocked = set(getattr(history, "capped_pairs", ()))
    cooling: dict[tuple[str, str], float] = {}
    traded_pairs: set[tuple[str, str]] = set()
    typing_failures: dict[tuple[str, str], int] = {}

    def held_back():
        now = time.monotonic()
        return blocked | {p for p, until in cooling.items() if until > now}

    def cooling_pending():
        now = time.monotonic()
        return any(until > now for until in cooling.values())

    def next_wake():
        """Seconds until the next resting pair returns (at most 30), or None."""
        now = time.monotonic()
        waits = [until - now for until in cooling.values() if until > now]
        return min(30.0, max(0.01, min(waits))) if waits else None
    offline = set(roster) - owners.keys()
    unavailable = set(offline)
    for n in roster:
        _log(f"{n} [{owners.get(n, 'offline')}]: {totals[n]}/{args.trades} today")

    favorite_terms = {n: storage_favorites.wanted(n, policy) for n in donors}
    reserves = {n: max(0, int(fleet.devices[n].config.get("trade_keep_last", 0))) for n in roster}
    returns = _return_pool(cfg, clear_path, max(collect.ORDINARY_RESERVE, reserves[receiver]))

    def make_pools(n, labels):
        return [_favorite_pool(label, favorite_terms[n][label], reserves[n])
                for label in labels
                if favorite_terms[n].get(label) not in (None, "", storage_review.EMPTY)]

    priority = {tier: {n: make_pools(n, (tier,)) for n in donors} for tier in PRIORITY_TIERS}
    value_targets = _value_targets(clear_path, cfg, getattr(policy, "lucky", ()) or ())
    catalog = collector_day.species_catalog(clear_path)
    peers = {n: ([collector_day.Offers(value_targets, max(2, reserves[n]), catalog)] if value_targets else [])
                + make_pools(n, PEER_TIERS) for n in donors}

    def budgets():
        return {n: max(0, args.trades - totals[n]) if n not in unavailable else 0 for n in roster}

    def peer_offers():
        return {n: pool for n in donors if n not in unavailable
                if (pool := _first_available(peers[n])) is not None}

    def peer_edges(offers):
        return set(combinations(sorted(offers), 2)) - held_back()

    def receiver_pending(remaining):
        """Donors still holding receiver-tier stock the receiver can take today.

        A pair that is only resting (a passing failure) still counts: the
        receiver comes back to it, so its stock keeps first claim.
        """
        if not remaining[receiver] or not returns.available:
            return set()
        return {d for d in donors if remaining[d] and _pair(d, receiver) not in blocked
                and any(_first_available(priority[t][d]) is not None for t in PRIORITY_TIERS)}

    def complement(donor):
        others = sorted(n for n in donors if n != donor)
        return (others[0], others[1]) if len(others) == 2 else None

    if args.plan:
        for n in donors:
            _log(f"{n} priority: {', '.join(t for t in PRIORITY_TIERS if priority[t][n]) or 'none'}; "
                 f"peer: {', '.join(p.current.name for p in peers[n]) or 'none'}")
        remaining = budgets()
        edges = peer_edges(peer_offers())
        # Show the same initial tier assignment as live mode, while retaining
        # the inventory-blind budget forecast across all configured tiers.
        choices = [{n for n in donors if remaining[n] and _pair(n, receiver) not in held_back()
                    and priority[tier][n]} for tier in PRIORITY_TIERS] if returns.available else []
        active = set().union(*choices)
        plan = _plan_budget(remaining, receiver, active, edges, parallel=True)
        initial = _plan_budget(remaining, receiver, active, edges, parallel=True,
                               starters=next((c for c in choices if c), None))
        donor = initial.first_donor
        if donor is not None:
            peer = complement(donor)
            if peer not in initial.phase2:
                peer = None
        else:
            peer = next(iter(initial.phase2), None)
        _log("budget only; favorite counts, friendship and trade admission have not been checked")
        _log("phase 1: " + (", ".join(f"{n} -> {receiver}: {c}" for n, c in plan.phase1.items() if c) or "none"))
        _log("phase 2: " + (", ".join(f"{a} <-> {b}: {c}" for (a, b), c in plan.phase2.items()) or "none"))
        _log("lane R: " + (f"{donor} -> {receiver}" if donor is not None else "none"))
        _log("lane P: " + (" <-> ".join(peer) if peer is not None else "none"))
        return 0

    names = {}
    failures = {}
    donor_phase = False
    lane_r, lane_p = _Lane(), _Lane()
    busy = set()
    changed = asyncio.Event()
    stopping = False
    fatal = None

    def wake():
        nonlocal changed
        # A waiter captures this generation before deciding whether it can
        # act. Replacing it rather than clearing it avoids lost notifications.
        changed.set()
        changed = asyncio.Event()

    def assignments():
        nonlocal donor_phase
        remaining = budgets()
        offers = peer_offers()
        edges = peer_edges(offers)

        # Keep an active receiver step pinned through opening, admission,
        # selection and confirmation. The peer grant is bounded by the
        # complement's allocation in the plan that started that step.
        r_job = lane_r.active
        if r_job is None and not donor_phase and remaining[receiver] and returns.available:
            for tier in PRIORITY_TIERS:
                choices = {n: pool for n in donors if remaining[n] and _pair(n, receiver) not in held_back()
                           if (pool := _first_available(priority[tier][n])) is not None}
                if not choices:
                    continue
                prefer = next((n for n in lane_r.held or () if n != receiver), None)
                # Reserve the receiver's budget on donors holding this tier or a later tier.
                later = PRIORITY_TIERS[PRIORITY_TIERS.index(tier):]
                reserved = {d for d in donors if remaining[d] and _pair(d, receiver) not in held_back()
                            and any(_first_available(priority[t][d]) is not None for t in later)}
                plan = _plan_budget(remaining, receiver, reserved, edges, prefer,
                                    parallel=True, starters=set(choices))
                if plan.first_donor is None:
                    plan = _plan_budget(remaining, receiver, set(choices), edges, prefer, parallel=True)
                n = plan.first_donor
                assert n is not None
                peer = complement(n)
                grant = plan.phase2.get(peer, 0) if peer is not None else 0
                if peer is not None:
                    # A donor still holding this or a later tier keeps enough
                    # allowance for the receiver's remaining trades. Lane
                    # P only spends the rest (stock counts are unknown here).
                    free = min(remaining[d] - (remaining[receiver] if d in reserved else 0) for d in peer)
                    grant = max(0, min(grant, free))
                r_job = _Job(
                    (n, receiver), {n: choices[n], receiver: returns}, tier,
                    peer if grant else None, totals[peer[0]] + grant if peer and grant else 0,
                )
                break

        if r_job is not None:
            edge = r_job.peer_pair
            if (edge is None or edge not in edges or totals[edge[0]] >= r_job.peer_until
                    or not all(remaining[n] for n in edge)):
                edge = None
        else:
            pending = set() if donor_phase else receiver_pending(remaining)
            if pending:
                # The receiver still wants stock that only a resting donor
                # holds. Donor trades go on meanwhile, but never spend that
                # donor's allowance below what the receiver still needs.
                edges = {e for e in edges
                         if all(remaining[d] > (remaining[receiver] if d in pending else 0) for d in e)}
            elif not donor_phase:
                donor_phase = True
                _log("receiver phase finished; starting donor trades")
            plan = _plan_budget(remaining, receiver, set(), edges, lane_p.held)
            edge = lane_p.held if lane_p.held in plan.phase2 else next(iter(plan.phase2), None)

        p_job = None
        if edge is not None:
            a, b = edge
            tier = "/".join(dict.fromkeys((offers[a].current.name, offers[b].current.name)))
            p_job = _Job(edge, {a: offers[a], b: offers[b]}, tier)
        return r_job, p_job

    async def exchange(job, phones):
        pair, pools, tier = job.pair, job.pools, job.tier
        scoped = _pair(*pair)
        try:
            for n in pair:
                if n not in names:
                    names[n] = await trade_local.trainer_name(phones[n])
            await trade_local.both(*(_on_side(phones[n], pools[n].prepare) for n in pair))

            def chooser(n):
                async def pick(phone):
                    await _on_side(phone, pools[n].pick)
                return pick

            def confirmed(receipts):
                labels = {n: pools[n].labels[-1] if pools[n].labels else pools[n].current.name
                          for n in pair}
                dex = {n: int(pools[n].current.query) for n in pair if pools[n].current.query.isdecimal()}
                starter_trade.record_trade(list(pair), labels, tier, "priority", offered_dex=dex, receipts=receipts)
                for n in pair:
                    totals[n] += 1
                _log(f"{tier}: {pair[0]} <-> {pair[1]}; "
                     + ", ".join(f"{n} {totals[n]}/{args.trades}" for n in pair))

            await trade_local.trade_once(
                *(phones[n] for n in pair), names,
                {n: pools[n].current.term for n in pair},
                {n: pools[n].current.name for n in pair},
                choosers={n: chooser(n) for n in pair}, on_confirmed=confirmed, daily_cap=args.trades,
            )
            for n in pair:
                pools[n].used()
            failures.pop(scoped, None)
            traded_pairs.add(scoped)
        except collector_day.OffersChanged as exc:
            _log(str(exc))
        except trade_local.LuckyAvailable as exc:
            blocked.add(scoped)
            _log(f"{pair}: skipped Lucky Trade ({exc})")
        except trade_local.TradeOver as exc:
            side = _exception_side(exc, pair)
            _log(f"{pair}: {exc}")
            if "limit" in str(exc).lower():
                if side in pair:
                    totals[side] = max(totals[side], trade_plan.DAILY_TRADES)
                else:
                    # trade_local's unattributed DailyLimit is pair-level.
                    blocked.add(scoped)
            elif side in pair:
                if pools[side].available:
                    pools[side].advance()
            else:
                blocked.add(scoped)
        except trade_local.TradeRefused as exc:
            side = _exception_side(exc, pair)
            if side not in pair:
                blocked.add(scoped)
                _log(f"{pair}: skipped unattributed refusal ({exc})")
                return
            pool = pools[side]
            pool.skip += 1
            _log(f"{side}: refused slot {pool.skip}/{collect.DEFAULT_MAX_SKIPS} ({exc})")
            if pool.skip >= collect.DEFAULT_MAX_SKIPS:
                pool.advance()
        except trade_local.TradeOutcomeUnknown as exc:
            unavailable.update(pair)
            _log(f"{pair}: reconciliation required ({exc})")
        except (starter.StepFailed, collect.home.HomeRecoveryError,
                pokemon_fleet.FleetError, collect.gbl_ios.IOSGBLError) as exc:
            text = str(exc).lower()
            side = _exception_side(exc, pair)
            if side in pair and any(sign in text for sign in TYPING_FAILURES):
                # One phone cannot type this list (the SE's WDA cut long
                # searches short, 24 Nov 2026). Retrying the same list only
                # rests the pair forever: after two misses that phone moves to
                # its next list, where pinned favorites open without typing.
                key = (side, pools[side].current.name)
                typing_failures[key] = typing_failures.get(key, 0) + 1
                if typing_failures[key] >= 2:
                    pool = pools[side]
                    if isinstance(pool, collector_day.Offers):
                        pool.index = len(pool.targets)
                    else:
                        pool.advance()
                    _log(f"{side}: cannot type {key[1]}; moving to its next list ({exc})")
                    return
                _log(f"{pair}: retrying ({exc})")
                return
            failures[scoped] = failures.get(scoped, 0) + 1
            stranger = "not on the friends list" in text
            if stranger and scoped not in traded_pairs:
                # Never traded in this run: believe the friends list.
                blocked.add(scoped)
                _log(f"{pair}: skipped ({exc})")
            elif stranger or failures[scoped] >= 2:
                # Pairs that already traded are friends; the row was not
                # drawn yet. Rest the pair and try again later.
                failures.pop(scoped, None)
                cooling[scoped] = time.monotonic() + COOLDOWN
                _log(f"{pair}: resting {int(COOLDOWN // 60)} min ({exc})")
            else:
                _log(f"{pair}: retrying ({exc})")

    def stop(exc):
        nonlocal stopping, fatal
        stopping = True
        if fatal is None:
            fatal = exc
        wake()

    async def work(lane, index):
        phones = {}
        async with contextlib.AsyncExitStack() as stack:
            async def release():
                if lane.held is None:
                    return
                scoped = lane.held
                assert busy.issuperset(scoped)
                try:
                    await stack.aclose()
                finally:
                    phones.clear()
                    lane.held = None
                    busy.difference_update(scoped)
                    wake()

            try:
                while not stopping and date.today() == day:
                    event = changed
                    jobs = assignments()
                    job = jobs[index]
                    scoped = _pair(*job.pair) if job is not None else None
                    if lane.held is not None and lane.held != scoped:
                        # Reassign only at a trade boundary. Keep both accounts
                        # reserved until all phone contexts have closed.
                        await release()
                        continue

                    opening = scoped is not None and lane.held is None
                    if job is None or (opening and not busy.isdisjoint(scoped)):
                        if (not any(jobs) and lane_r.active is None and lane_p.active is None
                                and not cooling_pending()):
                            return
                        # A resting pair comes back on a timer, not an event.
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(event.wait(), next_wake())
                        continue

                    # No await between scheduling, reserving both accounts and
                    # publishing the active step to the other lane.
                    lane.active = job
                    if opening:
                        assert busy.isdisjoint(scoped)
                        busy.update(scoped)
                        lane.held = scoped
                    else:
                        assert lane.held == scoped and busy.issuperset(scoped)
                    wake()

                    try:
                        if opening:
                            for n in scoped:
                                try:
                                    phones[n] = await stack.enter_async_context(remote_target.open_fleet_phone(
                                        n, args.shots, machine=owners[n], machines=machines, fleet=fleet,
                                        dry_run=False,
                                    ))
                                except (collect.gbl_ios.IOSGBLError, collect.home.HomeRecoveryError,
                                        pokemon_fleet.FleetError, RuntimeError) as exc:
                                    if BUSY_SIGN in str(exc):
                                        # Another fleet command (an hourly Go Plus
                                        # reconnect) holds this phone for a minute or
                                        # two. Retry the pair later; never drop it.
                                        cooling[scoped] = time.monotonic() + BUSY_RETRY
                                        _log(f"{n}: busy with another fleet command; retrying in "
                                             f"{int(BUSY_RETRY)} s ({exc})")
                                        break
                                    _log(f"{n}: unavailable ({exc})")
                                    offline.add(n)
                                    unavailable.add(n)
                                    break
                            if len(phones) != 2:
                                await release()
                                continue

                            # Read the shared journal under both device locks
                            # before acting on a newly opened pair.
                            fresh = await asyncio.to_thread(collector_day.day_counts, scoped, machines, day)
                            stale = scoped in getattr(fresh, "capped_pairs", ())
                            if stale:
                                blocked.add(scoped)
                            for n in scoped:
                                if fresh[n] > totals[n]:
                                    totals[n] = fresh[n]
                                    stale = True
                            if stale:
                                continue

                        if not stopping and date.today() == day:
                            await exchange(job, phones)
                    finally:
                        lane.active = None
                        wake()
            except BaseException as exc:
                stop(exc)
                raise
            finally:
                await release()

    async def supervised(lane, index):
        try:
            await work(lane, index)
        except BaseException as exc:
            # Also publish failures from closing a phone. Let an already
            # in-flight exchange in the other lane reach its boundary.
            stop(exc)
            raise

    await asyncio.gather(supervised(lane_r, 0), supervised(lane_p, 1), return_exceptions=True)
    assert not busy
    if fatal is not None:
        raise fatal

    for n in roster:
        _log(f"{n}: {totals[n]}/{args.trades} today" + (" (offline/unreachable)" if n in offline else ""))
    capped = [n for n in roster if totals[n] >= args.trades]
    required = min(3, max(0, len(roster) - 1 - len(offline)))
    _log(f"reached {args.trades}: {', '.join(capped) or 'none'}; need {required} attached account(s)")
    return int(sum(n not in offline for n in capped) < required)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="use only phones attached to this computer")
    parser.add_argument("--trades", type=int, default=trade_plan.DAILY_TRADES, metavar="N",
                        help=f"daily cap per account (default: {trade_plan.DAILY_TRADES})")
    parser.add_argument("--receiver", help="receiver account (default: collect-trades.json)")
    parser.add_argument("--exclude-devices", nargs="+", default=[], help="donor names or handset aliases to skip")
    parser.add_argument("--shots", type=Path, help="directory for diagnostic screenshots")
    parser.add_argument("--plan", action="store_true", help="show the budget plan without touching phones")
    args = parser.parse_args(argv)
    if not 1 <= args.trades <= trade_plan.DAILY_TRADES:
        parser.error(f"--trades must be between 1 and {trade_plan.DAILY_TRADES}")
    args.shots = (args.shots or config_paths.state_dir() / "priority-trade" / "shots").expanduser()
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    fleet_entrypoint.apply_local_environment()
    return asyncio.run(run(args))
