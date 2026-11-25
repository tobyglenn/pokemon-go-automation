"""Plan and execute Lucky Dex collection followed by daily valuable trades."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from pathlib import Path

from . import clear_pokemon, collect_trades as collect, config_paths, fleet_entrypoint, pokemon_fleet
from . import remote_target, starter, starter_trade, trade_approval, trade_local, trading_accounts

# The ten three-stage, 600-BST pseudo-legendary families through Paldea.
PSEUDO_DEX = "147-149,246-248,371-376,443-445,633-635,704-706,782-784,885-887,996-998"


class DayCounts(dict):
    def __init__(self, counts, capped_pairs):
        super().__init__(counts)
        self.capped_pairs = capped_pairs


def history_records(machines, day, *, since=None):
    """Merge a date range of exchange receipts and game-reported caps across hosts."""
    local = fleet_entrypoint.local_machine(machines) if machines else None
    last = f"history-{day}.jsonl"
    first = f"history-{since or day}.jsonl"
    directory = starter_trade.NEEDS_DIR
    histories = [p.read_text() for p in sorted(directory.glob("history-*.jsonl")) if first <= p.name <= last]
    script = (
        "from sources.config_paths import state_dir; "
        f"files=sorted((state_dir()/'trade').glob('history-*.jsonl')); "
        f"print('\\n'.join(p.read_text() for p in files if {first!r} <= p.name <= {last!r}))"
    )
    for machine, cfg in machines.items():
        if machine == local:
            continue
        cmd = fleet_entrypoint.remote_shell_command(machine, cfg, ["-c", script])
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=35, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise pokemon_fleet.FleetError(f"could not read trade history on {machine}: {exc}") from exc
        histories.append(result.stdout)
    unique = {}
    aliases = trading_accounts.aliases()
    for history in histories:
        for line in history.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise pokemon_fleet.FleetError("Invalid record in shared trade history")
            event_id = record.get("id")
            if isinstance(event_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
                key = ("id", event_id)
            else:
                key = ("legacy", json.dumps(record, sort_keys=True))
            identity = tuple(tuple(sorted(aliases.get(name, name) for name in record.get(field, ())))
                             for field in ("pair", "capped_pair", "capped_devices"))
            if key in unique and unique[key][0] != identity:
                raise pokemon_fleet.FleetError(f"Conflicting account identities in trade receipt {event_id!r}")
            unique.setdefault(key, (identity, record))
    return [record for _, record in unique.values()]


def day_counts(names, machines, day):
    """Read the distinct trade receipts on every configured coordinator."""
    mapping = trading_accounts.aliases()
    account = lambda name: mapping.get(name, name)
    counts = Counter()
    capped = set()
    capped_pairs = set()
    for event in history_records(machines, day):
        counts.update({account(n) for n in event["pair"]})
        capped.update(account(n) for n in event.get("capped_devices", ()))
        if event.get("capped_pair"):
            capped_pairs.add(tuple(sorted(account(n) for n in event["capped_pair"])))
    return DayCounts({n: max(counts[account(n)], collect.trade_plan.DAILY_TRADES if account(n) in capped else 0)
                      for n in names}, capped_pairs)


@dataclass
class LuckyGaps:
    names: dict[int, str]
    path: Path
    registered: set[int]

    @classmethod
    def load(cls, clear_config, receiver, identifier=None):
        settings = clear_pokemon.load_clear_config(clear_config)
        if not settings.get("lucky_report"):
            raise pokemon_fleet.FleetError(f"{clear_config} needs the receiver's lucky_report for daily collection")
        report = config_paths.find_config(settings["lucky_report"], clear_config.parent, "Lucky Dex audit")
        data = json.loads(report.read_text())
        device = str(data.get("summary", {}).get("device", "")).strip()
        if identifier and device:
            serial = re.search(r"\(([^()]+)\)\s*$", device)
            expected = serial.group(1) if serial else device
            normalize = clear_pokemon.normalize_species_token
            if normalize(expected) not in {normalize(receiver), normalize(identifier)}:
                raise pokemon_fleet.FleetError(f"Lucky Dex audit device {device!r} does not match receiver {receiver!r}")
        if "needed_lucky_list" not in data and "needed_lucky_query" not in data:
            raise pokemon_fleet.FleetError(f"Missing needed_lucky_list or needed_lucky_query in {report}")
        if settings.get("lucky_votes"):
            from . import dex_registration
            votes = config_paths.find_config(settings["lucky_votes"], clear_config.parent, "Lucky Pokédex observations")
            needed = dex_registration.lucky_gaps(report, votes, receiver, identifier)
        elif device or identifier:
            needed = set()
        else:
            _, needed = clear_pokemon._load_lucky_sets(report, None)
        names = {dex: "" for dex in sorted(needed)}
        for entry in data.get("needed_lucky_list", []):
            dex, _, title = str(entry).partition(" ")
            if int(dex) in needed:
                names[int(dex)] = title
        identity = hashlib.sha256(receiver.encode() + b'\0' + report.read_bytes()).hexdigest()[:20]
        path = config_paths.state_dir() / "collect-trades" / f"registered-{identity}.jsonl"
        return cls.from_journal(names, path, receiver)

    @classmethod
    def from_journal(cls, names, path, receiver):
        confirmed = {int(line) for line in path.read_text().splitlines() if line} if path.is_file() else set()
        gaps = cls(names, path, confirmed)
        gaps.restore_receipts(receiver)
        return gaps

    def restore_receipts(self, receiver):
        # The exchange is durably journalled before trade_once releases the
        # confirmation lock. A stop before register() can replay that evidence.
        for ledger in sorted(starter_trade.NEEDS_DIR.glob("history-*.jsonl")):
            for line in ledger.read_text().splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                if record.get("lucky_audit") != self.path.stem or receiver not in record.get("pair", []):
                    continue
                if not any(r.get("lucky") is True for r in record.get("receipts", {}).values()):
                    continue
                for donor, dex in record.get("offered_dex", {}).items():
                    if donor != receiver and donor in record["pair"] and dex in self.names:
                        self.registered.add(dex)

    def targets(self, filters):
        chunks = collect._split_dex_ranges(sorted(self.names.keys() - self.registered))
        return [collect.TargetSpec(f"lucky-gap-{i}", chunk, collect.build_donor_query(chunk, filters))
                for i, chunk in enumerate(chunks, 1)]

    def register(self, dex):
        if dex in self.registered:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as out:
            out.write(f"{dex}\n")
            out.flush()
            os.fsync(out.fileno())
        self.registered.add(dex)

    async def already_lucky(self, phone, dex):
        count, _, _, _ = await clear_pokemon._apply_and_verify_query(phone, f"lucky&{dex}")
        if count > 0:
            self.register(dex)
            print(f"[collect] {phone.name} already owns Lucky #{dex} {self.names.get(dex, '')}", flush=True)
            return True
        return False


def species_catalog(clear_path):
    """Optional OCR hints. All keep-last decisions still use exact dex counts."""
    if not clear_path.is_file():
        return {}
    value = clear_pokemon.load_clear_config(clear_path).get("species_csv")
    if not value:
        return {}
    path = Path(config_paths.expand_user_path(value))
    if not path.is_absolute():
        path = clear_path.parent / path
    if not path.is_file():
        return {}
    with path.open(newline="") as stream:
        return {int(row["id"]): clear_pokemon.normalize_species_token(row["identifier"]) for row in csv.DictReader(stream)}


@dataclass
class Offers:
    targets: list[collect.TargetSpec]
    keep: int
    catalog: dict[int, str] = field(default_factory=dict)
    index: int = 0
    skip: int = 0
    count: int = 0
    labels: list[str] = field(default_factory=list)
    held: dict[str, int] = field(default_factory=dict)
    ready_term: str | None = None

    def __post_init__(self):
        # Splitting a query for one phone must never modify another phone's pool.
        self.targets = list(self.targets)

    async def narrow(self, phone):
        """Split a broad reserve filter; nickname OCR is only a search-order hint."""
        current = self.current
        labels = []
        count = await collect.choose_filtered_tile(phone, current.term, current.name, inspect_labels=labels)
        if count <= self.keep:
            self.held[current.query] = count
            print(f"[collect] {phone.name}: keeping {count} eligible match(es) across dex {current.query}", flush=True)
            self.advance()
            return
        numeric = re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", current.query)
        if numeric:
            candidates = clear_pokemon.parse_dex_ranges(current.query)
            prefix = current.term.rsplit("&", 1)[0]
        else:
            # Keep an arbitrary custom selection intact, including size or tag restrictions.
            candidates = set(self.catalog)
            prefix = current.term
        if not candidates:
            raise starter.StepFailed(f"{phone.name}: no dex catalog for keep-last search {current.name}")
        frequency = Counter(clear_pokemon.normalize_species_token(label) for label in labels)
        # A species visible just once on this page can have duplicates below it.
        # Test observed species before bisecting unseen dex groups.
        hints = sorted((n for n in candidates if frequency[self.catalog.get(n)]),
                       key=lambda n: (-frequency[self.catalog[n]], n))
        groups = [{n} for n in hints]
        other = sorted(candidates.difference(hints))
        if not groups and len(other) > 1:
            middle = len(other) // 2
            groups.extend([other[:middle], other[middle:]])
        elif other:
            groups.append(other)
        replacements = []
        for group in groups:
            for query in collect._split_dex_ranges(sorted(group)):
                replacements.append(collect.TargetSpec(current.name, query, f"{prefix}&{query}"))
        self.targets[self.index:self.index + 1] = replacements
        self.skip = 0

    @property
    def available(self):
        return self.index < len(self.targets)

    @property
    def current(self):
        return self.targets[self.index]

    def advance(self):
        self.index += 1
        self.skip = 0
        self.ready_term = None

    async def prepare(self, phone):
        """Find an eligible single-dex offer and save it before opening the trade."""
        if not self.available:
            raise EmptyOffers(f"{phone.name}: exhausted eligible offers")
        if self.ready_term == self.current.term:
            return
        await starter.open_storage(phone)
        await phone.still()
        while self.available:
            try:
                if not self.current.query.isdecimal():
                    await self.narrow(phone)
                    continue
                image, boxes = await phone.read()
                info = {}
                await collect.prepare_saved_query(phone, self.current.term, image, boxes,
                                                  observed=info, min_count=self.keep + 1)
                count = info.get("count")
                if count is None:
                    raise starter.StepFailed(f"{phone.name}: no verified storage count for dex {self.current.query}")
                if count > self.keep:
                    self.ready_term = self.current.term
                    print(f"[collect] {phone.name}: offering dex {self.current.query}, {count} eligible, keep {self.keep}", flush=True)
                    return
                if count:
                    self.held[self.current.query] = count
                self.advance()
            except trade_local.TradeOver as exc:
                if "limit" in str(exc).lower():
                    raise
                self.advance()
        raise EmptyOffers(f"{phone.name}: exhausted eligible offers")

    async def pick(self, phone):
        self.labels.clear()
        self.count = 0
        while self.available:
            try:
                if not self.current.query.isdecimal():
                    await self.narrow(phone)
                    continue
                self.count = await collect.choose_filtered_tile(
                    phone, self.current.term, self.current.name, skip_index=self.skip,
                    keep_copies=self.keep, picked_species=self.labels,
                )
                return
            except trade_local.TradeOver as exc:
                prepared = self.ready_term is not None
                if "limit" in str(exc).lower():
                    raise
                if isinstance(exc, collect.ReserveHeld):
                    self.held[self.current.query] = exc.count
                print(f"[collect] {phone.name} {self.current.name} ({self.current.query}): {exc}", flush=True)
                self.advance()
                if prepared:
                    raise OffersChanged(f"{phone.name}: select the next offer in storage") from exc
        raise EmptyOffers(f"{phone.name}: exhausted eligible offers")

    def used(self):
        if self.count <= self.keep + 1:
            self.advance()


class PreparedOffers(Offers):
    """Execute one cached, single-species choice without entering Pokémon storage."""

    async def prepare(self, phone):
        if not self.available or not self.current.query.isdecimal():
            raise EmptyOffers(f"{phone.name}: no prepared exact offer")

    async def pick(self, phone):
        from . import ender_inventory
        self.labels.clear()
        self.count = await ender_inventory.pick(phone, self.current, keep=self.keep)
        self.labels.append(self.current.species)


class OffersChanged(starter.StepFailed):
    pass


class EmptyOffers(trade_local.TradeOver):
    pass


def choose_pair(edges, remaining):
    """Choose the two most under-budget phones, giving scarce partners priority."""
    edges = [pair for pair in edges if all(remaining[name] > 0 for name in pair)]
    if not edges:
        return None
    partners = Counter(name for pair in edges for name in pair)
    # Avoid consuming two well-connected phones while another has only one partner.
    return max(edges, key=lambda pair: (
        sum(1 / partners[name] for name in pair),
        max(remaining[name] for name in pair), min(remaining[name] for name in pair),
    ))


def batch_pairs(edges, remaining):
    """Group a fair feasible allocation into short disjoint sessions for this wave."""
    projected = dict(remaining)
    allocation = Counter()
    while pair := choose_pair(edges, projected):
        allocation[pair] += 1
        for name in pair:
            projected[name] -= 1
    batches = {}
    busy = set()
    for pair, capacity in allocation.items():
        if not busy.intersection(pair):
            batches[pair] = min(capacity, 10)
            busy.update(pair)
    return batches


def gap_surplus_pairs(gap_wave, free_edges, remaining, receiver):
    """Spend only budget above the whole current receiver deficit on disjoint peers."""
    if not gap_wave:
        return {}
    reserved = remaining[receiver]
    busy = {n for pair in gap_wave for n in pair}
    allowed_edges = [pair for pair in free_edges if not busy.intersection(pair)]
    surplus = {n: max(0, count - reserved) for n, count in remaining.items()}
    grants = {}
    projected = dict(remaining)
    for pair, count in batch_pairs(allowed_edges, surplus).items():
        # Draining two small accounts can leave a large third account with no
        # trading partner, even if the receiver's budget itself was protected.
        # Charge the peer allocation against today's initial budgets; assume
        # zero successful receiver trades until a receipt proves otherwise.
        others = [count for n, count in projected.items() if n not in pair]
        balanced_cap = sum(projected.values()) // 2 - max(others, default=0)
        count = min(count, balanced_cap, *gap_wave.values())
        if count > 0:
            grants[pair] = count
            for n in pair:
                projected[n] -= count
    return grants


def without_pending_gaps(valuable, pending):
    """Keep every pending Dex species available for collection into the receiver."""
    targets = []
    for target in valuable:
        allowed = clear_pokemon.parse_dex_ranges(target.query) - pending
        prefix = target.term.rsplit("&", 1)[0]
        for chunk in collect._split_dex_ranges(sorted(allowed)):
            targets.append(collect.TargetSpec(target.name, chunk, f"{prefix}&{chunk}"))
    return targets


def perfect_trade_dex(cfg, *, require_audit=False):
    """Audited species eligible for ordinary IV rerolls on the configured receiver."""
    numbers = cfg.get("perfect_trade_dex", [])
    if not isinstance(numbers, list) or any(type(n) is not int or n <= 0 for n in numbers):
        raise pokemon_fleet.FleetError("perfect_trade_dex must contain positive Pokédex numbers")
    if numbers and not cfg.get("receiver"):
        raise pokemon_fleet.FleetError("perfect_trade_dex needs a named receiver")
    from . import dex_registration
    status = dex_registration.perfect_status(cfg)
    if cfg.get("clear_config") or require_audit or status.registered or status.missing:
        return set(status.missing)
    return set(numbers)


def valuable_queries(clear_path, cfg, custom):
    excluded = set(cfg.get("exclude_return_dex", collect.DEFAULT_EXCLUDE_RETURN_DEX))
    excluded.update(cfg.get("collect_all_dex", []))
    return_filters = [*cfg.get("return_filters", collect.REQUIRED_RETURN_FILTERS),
                      *(f"!{dex}" for dex in cfg.get("collect_all_dex", []))]
    kwargs = dict(exclude_dex=excluded, return_filters=return_filters)
    tiers = cfg.get("return_dex_tiers") or collect.DEFAULT_CURATED_RETURN_TIERS
    allowed = set().union(*(clear_pokemon.parse_dex_ranges(t) for t in tiers))
    pseudos = clear_pokemon.format_dex_ranges(allowed & clear_pokemon.parse_dex_ranges(PSEUDO_DEX))
    queries = collect.build_return_queries(clear_path, **kwargs, return_dex_tiers=[pseudos, *tiers])
    return_queries = collect.build_return_queries(clear_path, **kwargs, custom_return_query=custom) if custom else queries
    def targets(terms):
        return [collect.TargetSpec(f"value-{i}", q.rsplit("&", 1)[-1], q) for i, q in enumerate(terms, 1)]
    donor_queries = ["&".join(part for part in q.split("&") if part.casefold() != "!costume") for q in queries]
    return targets(donor_queries), targets(return_queries)


async def guaranteed_profiles(pair, phones, names):
    a, b = pair
    await trade_local.both(trade_local.open_partner(phones[a], names[b]), trade_local.open_partner(phones[b], names[a]))
    async def check(device, partner):
        phone = phones[device]
        found, frame, _ = await trade_local.settle(
            phone, lambda image, boxes: trade_local.guaranteed_profile(image, boxes, partner), timeout=5)
        if not found:
            print(f"[collect] {device}: Lucky Friend badge for {partner} unverified "
                  f"(saved {phone.save(frame, 'lucky-profile-unverified')})", flush=True)
        return found

    return all(await trade_local.both(check(a, names[b]), check(b, names[a])))


async def run(args, cfg, fleet, machines, owners, receiver, donors, clear_path):
    day = date.today()
    roster = [receiver, *donors]
    history = {n: 0 for n in roster} if args.ignore_history else day_counts(roster, machines, day)
    remaining = {n: max(0, args.trades - history[n]) for n in roster}
    blocked = set(getattr(history, "capped_pairs", ()))
    for n in roster:
        print(f"[collect] {n} [{owners.get(n, 'offline')}]: {history[n]} used, {remaining[n]}/{args.trades} remaining", flush=True)
    possible = [n for n in roster if n in owners and remaining[n]]
    if not any(tuple(sorted(p)) not in blocked for p in combinations(possible, 2)):
        print("[collect] No connected pair has daily trades left together. Skipping trade inventory.", flush=True)
        return int(not args.plan and any(remaining.values()))
    if cfg.get("receiver") and cfg["receiver"] != receiver:
        raise pokemon_fleet.FleetError(f"The configured Lucky Dex audit belongs to {cfg['receiver']!r}; "
                                     f"select a matching --config-file for {receiver!r}")
    prepared_only = getattr(args, "prepared_only", False)
    if prepared_only:
        from . import ender_inventory
        snapshot = ender_inventory.load()
        targets = snapshot.get("dex_targets", {})
        journal = str(targets.get("lucky_journal", ""))
        if targets.get("receiver") != receiver or not re.fullmatch(r"registered-[a-f0-9]{20}\.jsonl", journal):
            print(f"[ender] Missing prepared Pokédex targets for {receiver}. Skipping collector trades.", flush=True)
            return 0
        names = {int(dex): "" for dex in targets["lucky"]}
        gaps = LuckyGaps.from_journal(names, config_paths.state_dir() / "collect-trades" / journal, receiver)
    else:
        gaps = LuckyGaps.load(clear_path, receiver, getattr(fleet.devices[receiver], "identifier", None))
    filters = cfg.get("donor_filters", collect.REQUIRED_DONOR_FILTERS)
    requested = cfg.get("collect_all_dex", [])
    if not isinstance(requested, list) or any(type(dex) is not int or dex < 1 for dex in requested):
        raise pokemon_fleet.FleetError("collect_all_dex must contain positive Pokédex numbers")
    collect_all = [collect.TargetSpec(f"dex-{dex}", str(dex), collect.build_donor_query(str(dex), filters))
                   for dex in dict.fromkeys(requested)]
    only_collect_all = getattr(args, "collect_all_only", False)
    if only_collect_all and not collect_all:
        raise pokemon_fleet.FleetError("No collect_all_dex targets configured")
    perfect_numbers = set()
    if not only_collect_all:
        perfect_numbers = set(targets["perfect"]) if prepared_only else perfect_trade_dex(cfg)
    perfect_targets = [collect.TargetSpec(f"perfect-gap-{i}", chunk, collect.build_donor_query(chunk, filters))
                       for i, chunk in enumerate(collect._split_dex_ranges(sorted(perfect_numbers)), 1)]
    needed = gaps.targets(filters)
    valuable, returns = valuable_queries(clear_path, cfg, args.return_query or cfg.get("custom_return_query"))
    if collect_all:
        print(f"[collect] priority: send all eligible copies of dex {', '.join(t.query for t in collect_all)} "
              f"to {receiver}, including a donor's last copy. Guaranteed pairs still require approval.", flush=True)
    if perfect_targets and not getattr(args, "prepared_only", False):
        print(f"[collect] {len(perfect_numbers)} perfect-IV targets into {receiver}; "
              "verify 4-star storage for each offer and keep ordinary donor reserves.", flush=True)
    if not only_collect_all:
        print(f"[collect] {len(gaps.names.keys() - gaps.registered)} audited Lucky Dex gaps into {receiver}, "
              f"{len(needed)} initial donor search groups.", flush=True)
        print("[collect] Daily valuable trades follow the receiver collections.", flush=True)
    print("[collect] other ordinary offers require at least three eligible copies per exact species; "
          "guaranteed Lucky pairs are held for an individually approved raid-attacker proposal.", flush=True)
    print(f"[collect] receiver return queries: {len(returns)}; peer value queries: {len(valuable)}", flush=True)
    if args.plan:
        if only_collect_all:
            return 0
        print(f"[collect] disjoint peers can trade non-gap valuable spares during collection only from "
              f"budget above {receiver}'s entire remaining deficit", flush=True)
        for n in (donors if receiver in owners else []):
            print(f"[collect] gap route: {n} -> {receiver}", flush=True)
        for a, b in combinations([n for n in roster if n in owners], 2):
            print(f"[collect] value route: {a} <-> {b}", flush=True)
        return 0
    if args.dry_run and prepared_only:
        raise ValueError("Ender cannot run an inventory scan")
    if args.dry_run:
        async def scan(n):
            targets = returns if n == receiver else (collect_all if only_collect_all else [*collect_all, *needed, *perfect_targets, *valuable])
            async with remote_target.open_fleet_phone(n, args.shots, machine=owners[n], machines=machines,
                                                      fleet=fleet, dry_run=True) as phone:
                await collect.count_queries_on_phone(phone, [(t.name, t.term) for t in targets])
        results = await asyncio.gather(*(scan(n) for n in roster if remaining[n] and n in owners), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                print(f"[collect] incomplete storage scan: {result}", flush=True)
        return int(any(isinstance(r, BaseException) for r in results))
    reserves = {n: int(getattr(fleet.devices[n], "config", {}).get("trade_keep_last", 0)) for n in roster}
    incoming = set()
    if prepared_only:
        audit_day = targets.get("perfect_audit_day")
        if not audit_day:
            incoming.add((receiver, None))
        else:
            incoming = ender_inventory.incoming_since_audit(
                await asyncio.to_thread(history_records, machines, day, since=audit_day), receiver, audit_day)
        def pool(name, targets, keep):
            handset = getattr(fleet, "transports", {}).get(name, name)
            favorites = ender_inventory.account_favorites(snapshot, name, handset)
            return PreparedOffers(ender_inventory.candidates(favorites, targets, keep), keep)
    else:
        catalog = species_catalog(clear_path)
        def pool(name, targets, keep):
            return Offers(targets, keep, catalog)
    values = {n: pool(n, returns if n == receiver else valuable, max(2, reserves[n])) for n in roster}
    priority = {n: pool(n, collect_all, 0) for n in donors}
    lucky = {n: pool(n, needed, max(2, reserves[n])) for n in donors}
    perfect = {n: pool(n, perfect_targets, max(2, reserves[n])) for n in donors}
    verified_perfect = set()
    peer_targets = without_pending_gaps(valuable, (gaps.names.keys() - gaps.registered) | perfect_numbers)
    surplus_pools = {n: pool(n, peer_targets, max(2, reserves[n])) for n in donors}
    if prepared_only:
        for n in roster:
            print(f"[ender] {n}: {len(values[n].targets)} prepared ordinary return/value species", flush=True)
    profiles_checked = set()
    configured_proposals = {tuple(sorted(pair)) for pair in cfg.get("guaranteed_proposal_pairs", ())
                            if receiver not in pair and len(pair) == 2}
    requested_proposals = set()
    queued_proposals = []
    checked_gaps = set()
    names = {}
    phase_blocked = set()
    unavailable = {n for n in roster if n not in owners}
    completed = Counter()
    reasons = []

    def report_block(detail):
        reasons.append(detail)
        print(f"[collect] {detail}", flush=True)

    def hold_lucky(pair):
        scoped = tuple(sorted(pair))
        blocked.add(scoped)
        if scoped in configured_proposals and scoped not in requested_proposals:
            requested_proposals.add(scoped)
            pending_id = trade_approval.awaiting_decision(pair)
            if pending_id:
                report_block(f"{pair}: guaranteed Lucky held for existing proposal {pending_id}")
                return
            queued_proposals.append(pair)
            report_block(f"{pair}: guaranteed Lucky held; preparing one specific raid-attacker proposal after releasing locks")
        else:
            report_block(f"{pair}: guaranteed Lucky held. Request a specific proposal with "
                         f"--propose-guaranteed {pair[0]} {pair[1]}")

    def accepted_exchange(pair, receipts):
        for n in pair:
            remaining[n] -= 1
            completed[n] += 1
        blocked.discard(tuple(sorted(pair)))

    def retire_gap(dex):
        gaps.register(dex)
        for pool in lucky.values():
            pending = []
            for target in pool.targets[pool.index:]:
                numbers = clear_pokemon.parse_dex_ranges(target.query)
                if dex not in numbers:
                    pending.append(target)
                elif len(numbers) > 1:
                    query = clear_pokemon.format_dex_ranges(numbers - {dex})
                    pending.append(collect.TargetSpec(target.name, query, collect.build_donor_query(query, filters)))
            pool.targets, pool.index, pool.skip = pending, 0, 0
        print(f"[collect] retired Lucky #{dex} {gaps.names[dex]} from {receiver}'s requested list", flush=True)

    async def session(pair, pools, limit, phase, reserve_budget=0):
        scoped = tuple(sorted(pair))
        print(f"[collect] {phase}: {pair[0]} <-> {pair[1]}, up to {limit} exchange(s)", flush=True)
        try:
            async with contextlib.AsyncExitStack() as stack:
                phones = {}
                for n in pair:
                    cm = collect._open_optional_donor(n, args.shots, machine=owners[n], machines=machines, fleet=fleet)
                    phone = await stack.enter_async_context(cm)
                    if phone is None:
                        unavailable.add(n)
                        report_block(f"{n}: connection unavailable")
                        return
                    phones[n] = phone
                # Another collector may have completed exchanges since this
                # batch was planned. Recheck while both device locks are held.
                if not args.ignore_history:
                    fresh = await asyncio.to_thread(day_counts, pair, machines, day)
                    if scoped in getattr(fresh, "capped_pairs", ()):
                        blocked.add(scoped)
                        return
                    for n in pair:
                        history[n] = max(history[n], fresh[n] - completed[n])
                        remaining[n] = max(0, args.trades - history[n] - completed[n])
                    limit = min(limit, *(remaining[n] - reserve_budget for n in pair))
                    if limit <= 0:
                        return
                for n in pair:
                    if n not in names:
                        names[n] = await trade_local.trainer_name(phones[n])
                if scoped not in profiles_checked:
                    if await guaranteed_profiles(pair, phones, names):
                        hold_lucky(pair)
                        return
                    # Manually completed trades can be absent from the journal.
                    # Verify admission before preparing ordinary offers in storage.
                    try:
                        if not prepared_only:
                            async with trade_local.check_trade_lobby(*(phones[n] for n in pair)):
                                pass
                    except trade_local.DailyLimit as exc:
                        blocked.add(scoped)
                        report_block(f"{pair}: {exc}")
                        return
                    profiles_checked.add(scoped)
                attempts = 0
                while limit and all(pools[n].available for n in pair) and date.today() == day:
                    try:
                        if phase == "perfect-gap":
                            donor = next(n for n in pair if n != receiver)
                            await pools[donor].prepare(phones[donor])
                            dex = int(pools[donor].current.query)
                            if prepared_only and ((receiver, dex) in incoming or (receiver, None) in incoming):
                                pools[donor].advance()
                                continue
                            if not prepared_only and dex not in verified_perfect:
                                count, *_ = await clear_pokemon._apply_and_verify_query(phones[receiver], f"{dex}&4*")
                                pools[receiver].ready_term = None
                                if count:
                                    verified_perfect.add(dex)
                            if dex in verified_perfect:
                                pools[donor].advance()
                                continue
                            await pools[receiver].prepare(phones[receiver])
                        else:
                            await trade_local.both(*(pools[n].prepare(phones[n]) for n in pair))
                        if not prepared_only and phase == "lucky-gap":
                            donor = next(n for n in pair if n != receiver)
                            dex = int(pools[donor].current.query)
                            if dex not in checked_gaps:
                                already_lucky = await gaps.already_lucky(phones[receiver], dex)
                                checked_gaps.add(dex)
                                if already_lucky:
                                    retire_gap(dex)
                                    continue

                        def confirmed(receipts):
                            give = {n: pools[n].labels[-1] if pools[n].labels else pools[n].current.name for n in pair}
                            dex = {n: int(pools[n].current.query) for n in pair}
                            starter_trade.record_trade(pair, give, phase, "collect", offered_dex=dex,
                                                      receipts=receipts, lucky_audit=gaps.path.stem)
                            for n in pair:
                                remaining[n] -= 1
                                completed[n] += 1
                            print(f"[collect] completed {pair[0]} ({give[pair[0]]}) <-> {pair[1]} ({give[pair[1]]}); "
                                  + ", ".join(f"{n} {history[n]+completed[n]}/{args.trades}" for n in pair), flush=True)
                        receipts = await trade_local.trade_once(
                            *[phones[n] for n in pair], names, {n: pools[n].current.term for n in pair},
                            {n: pools[n].current.name for n in pair}, choosers={n: pools[n].pick for n in pair},
                            on_confirmed=confirmed, daily_cap=None if args.ignore_history else args.trades,
                        )
                        offered_dex = {n: pools[n].current.query for n in pair}
                        if prepared_only and receiver in pair:
                            donor = next(n for n in pair if n != receiver)
                            incoming.add((receiver, int(offered_dex[donor])))
                        for n in pair:
                            pools[n].used()
                        limit -= 1
                        attempts = 0
                        if receiver in pair and any(r.lucky for r in receipts.values()):
                            donor = next(n for n in pair if n != receiver)
                            dex = int(offered_dex[donor])
                            if dex in gaps.names and dex not in gaps.registered:
                                # Exact donor dex query + positive Lucky receipt identifies the
                                # registration even when the offered Pokémon had a nickname.
                                retire_gap(dex)
                    except OffersChanged:
                        continue
                    except EmptyOffers:
                        return
                    except trade_local.TradeOutcomeUnknown:
                        raise
                    except trade_local.LuckyAvailable:
                        hold_lucky(pair)
                        return
                    except trade_local.TradeOver as exc:
                        blocked.add(scoped)
                        report_block(f"{pair}: {exc}")
                        return
                    except trade_local.TradeRefused as exc:
                        if prepared_only:
                            blocked.add(scoped)
                            report_block(f"{pair}: prepared exchange refused: {exc}")
                            return
                        affected = next((n for n in pair if str(exc).startswith(f"{n}:")), None)
                        if affected and "warning dialog" in str(exc):
                            pool = pools[affected]
                            pool.skip += 1
                            if pool.skip >= collect.DEFAULT_MAX_SKIPS:
                                pool.advance()
                            continue
                        raise
                    except starter.StepFailed as exc:
                        if prepared_only:
                            blocked.add(scoped)
                            report_block(f"{pair}: prepared selection unavailable: {exc}")
                            return
                        attempts += 1
                        if attempts >= 2:
                            raise
                        print(f"[collect] {pair}: retrying interrupted selection: {exc}", flush=True)
        except trade_local.TradeOutcomeUnknown as exc:
            pending_devices = set()
            for path in trade_local._pending_files(pair):
                if path.exists():
                    try:
                        pending_devices.update(json.loads(path.read_text())["pair"])
                    except (OSError, ValueError, KeyError, TypeError):
                        pending_devices.update(pair)
            unavailable.update(pending_devices or pair)
            report_block(f"{pair}: reconciliation required: {exc}")
        except (starter.StepFailed, collect.home.HomeRecoveryError, pokemon_fleet.FleetError, collect.gbl_ios.IOSGBLError) as exc:
            phase_blocked.add(scoped)
            report_block(f"{pair}: {exc}")

    phases = [("collect-all", {**priority, receiver: values[receiver]})] if collect_all else []
    if not only_collect_all:
        phases.append(("lucky-gap", {**lucky, receiver: values[receiver]}))
        if perfect_targets:
            phases.append(("perfect-gap", {**perfect, receiver: values[receiver]}))
        phases.append(("daily-value", values))
    if prepared_only and not only_collect_all:
        from . import collector_proposals, lucky_raid_offers
        state = lucky_raid_offers.load_state()
        for pair in sorted(configured_proposals):
            if (pair in blocked or any(n not in possible for n in pair)
                    or trade_approval.awaiting_decision(pair)):
                continue
            # Reading an existing pair of offers is cheap. Only contact the
            # trainers when a reciprocal cached offer can actually be quoted.
            if collector_proposals.cached_pair_present(state, pair, fleet, snapshot):
                await collector_proposals.run(args, cfg, fleet, machines, owners,
                                              held_pair=pair, on_exchange=accepted_exchange)
                requested_proposals.add(pair)
    for phase, pools in phases:
        phase_blocked.clear()
        edges = list(combinations(roster, 2)) if phase == "daily-value" else [(n, receiver) for n in donors]
        while date.today() == day:
            eligible = [pair for pair in edges if tuple(sorted(pair)) not in (blocked | phase_blocked) and
                        all(n not in unavailable and pools[n].available and remaining[n] for n in pair)]
            wave = batch_pairs(eligible, remaining)
            if not wave:
                break
            jobs = [session(pair, pools, count, phase) for pair, count in wave.items()]
            if phase == "lucky-gap":
                available_peers = [n for n in donors if n not in unavailable and surplus_pools[n].available]
                peer_edges = [pair for pair in combinations(available_peers, 2)
                              if tuple(sorted(pair)) not in (blocked | phase_blocked)]
                extra = gap_surplus_pairs(wave, peer_edges, remaining, receiver)
                floor = remaining[receiver]
                jobs.extend(session(pair, surplus_pools, count, "surplus-value", floor)
                            for pair, count in extra.items())
            await asyncio.gather(*jobs)
        while queued_proposals and date.today() == day:
                from . import collector_proposals
                pair = queued_proposals.pop(0)
                await collector_proposals.run(args, cfg, fleet, machines, owners,
                                              held_pair=pair, on_exchange=accepted_exchange)
    print(f"[collect] verified {sum(completed.values())//2} exchange(s) this run", flush=True)
    if only_collect_all:
        unfinished = [n for n in donors if priority[n].available]
        if unfinished:
            print(f"[collect] eligible targets may remain on {', '.join(unfinished)}", flush=True)
        return int(bool(unfinished))
    for n in roster:
        print(f"[collect] {n}: {history[n]+completed[n]}/{args.trades} today, {remaining[n]} short", flush=True)
    if any(remaining.values()) and not reasons:
        print("[collect] no eligible pairs with both offers and remaining daily budget", flush=True)
    for n, pool in lucky.items():
        for query, count in pool.held.items():
            print(f"[collect] {n}: reserved {count} scarce eligible match(es), dex {query}", flush=True)
    for reason in dict.fromkeys(reasons):
        print(f"[collect] {reason}", flush=True)
    return int(any(remaining.values()))
