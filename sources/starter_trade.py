"""starter.py's trade step: the two phones on this computer trade each other.

1. Count, by storage search on both phones at once, each raid family's
   98Plus copies (`trade_plan.families`).
2. For every family either phone has no 98Plus copy of, count what each phone
   can give, species by species (and, on a phone set to keep its last, every copy).
3. Trade those, strongest family first, up to the game's 100 a day.
4. Write what this pair still needs to state_dir()/trade/, and say -- in the
   log and as a macOS notification -- when a phone on the other computer holds
   what this pair cannot give each other, so the phones can be swapped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from . import config_paths, gbl_home_recovery as home, pokemon_fleet, trade_history, trade_local, trade_plan
from .starter import Phone, Report, StepFailed, open_storage

NEEDS_DIR = config_paths.state_dir() / "trade"


# --- daily history ----------------------------------------------------------------------

def trades_done_today(device_name: str, now: datetime | None = None) -> int:
    """Read this account's fleet-wide receipts for the local game date."""
    return trade_history.current_day_counts((device_name,), (now or datetime.now()).date())[device_name]


def record_trade(
    pair_names: Sequence[str],
    give: Mapping[str, str],
    family_key: str,
    reason: str,
    now: datetime | None = None,
    *,
    offered_dex: Mapping[str, int] | None = None,
    receipts: Mapping[str, trade_local.TradeReceipt] | None = None,
    lucky_audit: str | None = None,
) -> None:
    """Append one completed trade to today's private trade history log."""
    payload = {
        "pair": list(pair_names),
        "give": dict(give),
        "family": family_key,
        "reason": reason,
    }
    if offered_dex:
        payload["offered_dex"] = dict(offered_dex)
    if receipts:
        payload["receipts"] = {
            device: {"screenshot": str(receipt.screenshot), "label": receipt.label, "lucky": receipt.lucky}
            for device, receipt in receipts.items()
        }
    if lucky_audit:
        payload["lucky_audit"] = lucky_audit
    trade_history.append_event(payload, now, NEEDS_DIR)


def decrement_saved_counts(
    counts: Sequence[trade_plan.PhoneCounts],
    give: Mapping[str, str],
) -> None:
    """Update cached counts after a trade completes so `--reuse-counts` stays accurate."""
    for phone_counts in counts:
        species = give.get(phone_counts.name)
        if not species:
            continue
        if species in phone_counts.tradable:
            phone_counts.tradable[species] = max(0, phone_counts.tradable[species] - 1)
        if species in phone_counts.owned:
            phone_counts.owned[species] = max(0, phone_counts.owned[species] - 1)
    save_needs(counts)


# --- counting ---------------------------------------------------------------------------

def _scanner(spec: pokemon_fleet.DeviceSpec):
    from . import raid_scan_android, raid_scan_ios
    if spec.platform == "ios":
        phone = raid_scan_ios.IOSPhone.connect(spec)
        return phone, raid_scan_ios.IOSStorageScanner(phone)
    phone = raid_scan_android.AndroidPhone(spec.identifier, spec.name)
    return phone, raid_scan_android.StorageScanner(phone, verbose=False)


MAX_UNREAD = 3


def _count_terms(spec: pokemon_fleet.DeviceSpec, terms: Sequence[str],
                 unknown: int) -> dict[str, int]:
    """Each term's result count; an unreadable one takes the safe `unknown`.

    The safe value never leads to a trade: an unread 98Plus count reads as
    "has a good one" and an unread spare count as "none to give".  A screen
    that will not read more than MAX_UNREAD times is refused outright, since
    that is a settings or screen problem rather than one bad OCR read
    (ios-one read every species as 0 with SHOW EVOLUTIONARY LINE on, 4 Nov).
    """
    phone, scanner = _scanner(spec)
    out: dict[str, int] = {}
    unread = 0
    try:
        scanner.ensure_grid()
        for number, term in enumerate(terms, 1):
            count = scanner.search(term)[0]
            if count is None:
                count, _, image, _ = scanner.search(term)
                if count is None:
                    unread += 1
                    if unread > MAX_UNREAD:
                        raise StepFailed(f"{spec.name}: {unread} searches could not be read, "
                                         f"the last {term!r}")
                    NEEDS_DIR.mkdir(parents=True, exist_ok=True)
                    shot = NEEDS_DIR / f"unread-{spec.name}-{number}.png"
                    with contextlib.suppress(Exception):
                        image.save(shot)
                    print(f"[{spec.name}] could not read {term!r}; taking {unknown} (saved {shot})",
                          flush=True)
                    count = unknown
            out[term] = int(count)
            if number % 10 == 0 or number == len(terms):
                print(f"[{spec.name}] counted {number}/{len(terms)} searches", flush=True)
    finally:
        with contextlib.suppress(Exception):
            phone.close()
    return out


async def _storage_first(spec: pokemon_fleet.DeviceSpec, args) -> None:
    """The scanners only walk back from nearby screens; the starter walks from anywhere."""
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        try:
            await open_storage(Phone(spec.name, target, args.shots))
        finally:
            await target.close()


async def count(spec: pokemon_fleet.DeviceSpec, args, terms: Sequence[str],
                walk: bool = True, unknown: int = 0) -> dict[str, int]:
    if walk:
        await _storage_first(spec, args)
    with pokemon_fleet.acquire_device_locks([spec]):
        return await asyncio.to_thread(_count_terms, spec, list(terms), unknown)


async def together(*jobs):
    """Every phone's job runs to its end before any failure is raised.

    A count runs in a thread, which nothing can cancel: letting one phone's
    failure end the step released the other phone's lock while its thread was
    still typing, and the next run then typed into the same field at the same
    time (android-three searched "9988pplluuss", 4 Nov).
    """
    async def told(job):
        # Say so now: the partner may run on for many minutes before the raise.
        try:
            return await job
        except Exception as exc:
            print(f"[trade] PROBLEM (waiting for the other phone to finish): {exc}", flush=True)
            raise

    results = await asyncio.gather(*(told(job) for job in jobs), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def _spare_species_to_count(
    fams: Sequence[trade_plan.Family],
    counts: Sequence[trade_plan.PhoneCounts],
    *,
    target_good: int = 1,
    fill_budget: bool = False,
    fast_scan: bool = False,
) -> list[str]:
    """Ordered, deduplicated species list for Pass 2 spare counting."""
    needed = [f for f in fams if any(c.good.get(f.key, 0) == 0 for c in counts)]
    if not needed and target_good <= 1 and not fill_budget:
        return []
    if fast_scan:
        terms = [s for f in needed for s in f.species]
        for fam in fams:
            if fam.species:
                terms.append(fam.species[0])
            if len(fam.species) > 1 and fam.species[1] in trade_plan.BABIES:
                terms.append(fam.species[1])
        return list(dict.fromkeys(terms))
    if target_good > 1 or fill_budget:
        return list(dict.fromkeys(s for f in fams for s in f.species))
    return list(dict.fromkeys([s for f in needed for s in f.species] + [f.species[0] for f in fams]))


def donor_allows_costumes(name):
    from . import collect_trades, trading_accounts
    cfg = collect_trades.load_config()
    account = trading_accounts.aliases().get(name, name)
    return bool(cfg.get("receiver")) and account != cfg["receiver"]


async def inventory(
    pair: Sequence[pokemon_fleet.DeviceSpec],
    args,
    fams: Sequence[trade_plan.Family],
    *,
    target_good: int = 1,
    fill_budget: bool = False,
    fast_scan: bool = False,
) -> list[trade_plan.PhoneCounts]:
    good_terms = [trade_plan.good_term(f) for f in fams]
    goods = await together(*(count(spec, args, good_terms, unknown=1) for spec in pair))
    counts = [trade_plan.PhoneCounts(spec.name, {f.key: g[trade_plan.good_term(f)] for f in fams})
              for spec, g in zip(pair, goods)]
    species = _spare_species_to_count(
        fams, counts, target_good=target_good, fill_budget=fill_budget, fast_scan=fast_scan
    )
    if not species:
        return counts

    async def spares(spec, phone_counts):
        allow_costume = donor_allows_costumes(spec.name)
        terms = [trade_plan.trade_term(s, allow_costume=allow_costume) for s in species]
        # Still on the storage grid from the first pass.
        found = await count(spec, args, terms, walk=False)
        phone_counts.tradable = {name: found[term] for name, term in zip(species, terms)}
        if spec.name in trade_plan.KEEP_LAST:
            keep_species = [s for s in species if phone_counts.tradable.get(s, 0) > 0]
            if keep_species:
                owned_found = await count(spec, args, keep_species, walk=False)
                phone_counts.owned = {s: owned_found[s] for s in keep_species}

    await together(*(spares(spec, c) for spec, c in zip(pair, counts)))
    return counts


# --- the other computer ---------------------------------------------------------------------

def save_needs(counts: Sequence[trade_plan.PhoneCounts]) -> None:
    NEEDS_DIR.mkdir(parents=True, exist_ok=True)
    for c in counts:
        (NEEDS_DIR / f"counts-{c.name}.json").write_text(
            json.dumps({"at": datetime.now().isoformat(timespec="seconds"), **asdict(c)}, indent=1))


def saved_counts(pair: Sequence[pokemon_fleet.DeviceSpec], max_age: float = 2 * 3600) -> list | None:
    """This pair's counts from an earlier run today, if both are recent."""
    out = []
    for spec in pair:
        path = NEEDS_DIR / f"counts-{spec.name}.json"
        try:
            raw = json.loads(path.read_text())
            age = (datetime.now() - datetime.fromisoformat(raw["at"])).total_seconds()
        except Exception:
            return None
        if age > max_age:
            return None
        out.append(trade_plan.PhoneCounts(raw["name"], raw.get("good", {}),
                                          raw.get("tradable", {}), raw.get("owned", {})))
    return out


def save_suggestions(suggestions: Sequence[trade_plan.Batch]) -> None:
    """Lucky Trade suggestions, for a person: state_dir()/trade/lucky-suggestions.txt."""
    NEEDS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{datetime.now():%Y-%m-%d %H:%M}  {b.count} x {b.label} ({b.reason})" for b in suggestions]
    (NEEDS_DIR / "lucky-suggestions.txt").write_text("\n".join(lines) + ("\n" if lines else ""))


def other_counts(here: set[str]) -> list[trade_plan.PhoneCounts]:
    """The latest counts of phones not on this computer: local files, then the other Mac's."""
    texts: dict[str, str] = {}
    for path in NEEDS_DIR.glob("counts-*.json"):
        texts[path.stem[len("counts-"):]] = path.read_text()
    from . import fleet_entrypoint
    for config in fleet_entrypoint.machine_configs().values():
        host = config.get("ssh_host")
        if not isinstance(host, str) or not host:
            continue
        try:
            listing = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", host,
                 "for f in ~/.local/state/pokemon-go-automation/trade/counts-*.json; do "
                 "[ -f \"$f\" ] && cat \"$f\" && echo && echo '%%%%'; done"],
                capture_output=True, text=True, timeout=60).stdout
        except Exception:
            continue
        for chunk in listing.split("%%%%"):
            with contextlib.suppress(Exception):
                raw = json.loads(chunk)
                texts[raw["name"]] = chunk
    out = []
    for name, text in texts.items():
        if name in here:
            continue
        with contextlib.suppress(Exception):
            raw = json.loads(text)
            out.append(trade_plan.PhoneCounts(raw["name"], raw.get("good", {}),
                                              raw.get("tradable", {}), raw.get("owned", {})))
    return out


def notify(lines: Sequence[str]) -> None:
    if not lines:
        return
    text = "; ".join(lines)[:220].replace('"', "'")
    with contextlib.suppress(Exception):
        subprocess.run(["osascript", "-e",
                        f'display notification "{text}" with title "Pokemon trades: swap phones"'],
                       timeout=10, check=False)


# --- trading ----------------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def pair_phones(pair: Sequence[pokemon_fleet.DeviceSpec], args):
    with pokemon_fleet.acquire_device_locks(list(pair)):
        targets = [await home.open_target(spec.name) for spec in pair]
        try:
            yield [Phone(spec.name, t, args.shots) for spec, t in zip(pair, targets)]
        finally:
            for t in targets:
                with contextlib.suppress(Exception):
                    await t.close()


async def trade_step(pair: Sequence[pokemon_fleet.DeviceSpec], args) -> Report:
    report = Report("trade", True, "")
    trade_plan.KEEP_LAST.update(trade_plan.load_keep_last())
    expanded = bool(getattr(args, "expanded", False))
    target_good = int(getattr(args, "target_good", 1))
    fill_budget = bool(getattr(args, "fill_budget", False))
    fast_scan = bool(getattr(args, "fast_scan", False))
    max_age = float(getattr(args, "max_count_age", 2 * 3600))
    ignore_history = bool(getattr(args, "ignore_history", False))

    pair_names = [spec.name for spec in pair]
    counts_today = (None if ignore_history
                    else await asyncio.to_thread(trade_history.current_day_counts, pair_names))
    if counts_today is not None and trade_history.pair_capped(pair_names, counts_today):
        report.detail = "the game reported no daily trades left for this pair"
        return report
    already = max(counts_today.values(), default=0) if counts_today is not None else 0
    budget = max(0, int(args.trades) - already)
    if budget <= 0:
        report.detail = f"daily trade budget already reached today ({already}/{args.trades})"
        return report

    fams = trade_plan.expanded_families() if expanded else trade_plan.families()
    counts = saved_counts(pair, max_age=max_age) if getattr(args, "reuse_counts", False) else None
    if counts is None:
        counts = await inventory(
            pair, args, fams, target_good=target_good, fill_budget=fill_budget, fast_scan=fast_scan
        )
    else:
        print("[trade] using the counts saved recently", flush=True)
    save_needs(counts)
    all_batches = trade_plan.plan_pair(
        counts[0], counts[1], fams, budget, target_good=target_good, fill_budget=fill_budget
    )
    away = other_counts({s.name for s in pair})
    hints = trade_plan.swap_hints(counts, away, fams)
    # Few-copies trades are never made here: they are suggested, for a person
    # to make as Lucky Trades (the operator's rule, 4 Nov).
    suggestions = [b for b in all_batches if b.lucky_only]
    batches = [b for b in all_batches if not b.lucky_only]
    planned_total = sum(b.count for b in batches)
    if fill_budget:
        hints.extend(trade_plan.shortfall_hints(counts, away, fams, planned_total, budget))
    for hint in hints:
        print(f"[trade] SWAP: {hint}", flush=True)
        report.lines.append("swap: " + hint)
    notify(hints)
    for batch in suggestions:
        line = f"suggest as a Lucky Trade: {batch.count} x {batch.label} ({batch.reason})"
        print(f"[trade] {line}", flush=True)
        report.lines.append(line)
    save_suggestions(suggestions)
    if not batches:
        report.detail = (
            "nothing to trade"
            + (f"; {len(suggestions)} Lucky Trade suggestion(s)" if suggestions else "")
        )
        return report
    for batch in batches:
        print(f"[trade] plan: {batch.count} x {batch.label} ({batch.reason})", flush=True)
    done = 0
    if args.trade_plan_only:
        report.detail = f"planned {planned_total} trade(s); --trade-plan made none"
        return report
    async with pair_phones(pair, args) as (a, b):
        names = {a.name: await trade_local.trainer_name(a), b.name: await trade_local.trainer_name(b)}
        stop = False
        donor_costumes = {n: donor_allows_costumes(n) for n in names}
        donor_max_move = {n for n, is_donor in donor_costumes.items() if is_donor}
        for batch in batches:
            terms = {n: trade_plan.trade_term(sp, allow_costume=donor_costumes[n]) for n, sp in batch.give.items()}
            made = failures = 0
            while made < batch.count and not stop:
                try:
                    def confirmed(receipts):
                        record_trade([a.name, b.name], batch.give, batch.family.key, batch.reason, receipts=receipts)

                    await trade_local.trade_once(
                        a, b, names, terms, batch.give,
                        allow_max_move_reset=donor_max_move, on_confirmed=confirmed,
                        daily_cap=None if ignore_history else args.trades,
                    )
                    made += 1
                    done += 1
                    decrement_saved_counts(counts, batch.give)
                    print(f"[trade] {batch.label}: {made}/{batch.count} (session {done}, day {already + done})",
                          flush=True)
                except trade_local.LuckyAvailable as exc:
                    line = f"stopped: a Lucky Trade is available between this pair -- {exc}"
                    print(f"[trade] {line}", flush=True)
                    report.lines.append(line)
                    notify([f"{a.name} + {b.name}: a Lucky Trade is available; trade it by hand"])
                    stop = True
                except trade_local.TradeRefused as exc:
                    report.ok = False
                    report.lines.append(f"{batch.label}: refused -- {exc}")
                    stop = True   # a price that is not 100 is worth a person's look
                except trade_local.TradeOver as exc:
                    print(f"[trade] {batch.label}: over -- {exc}", flush=True)
                    report.lines.append(f"{batch.label}: {exc}")
                    if "limit" in str(exc):
                        stop = True
                    break
                except (StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
                    failures += 1
                    print(f"[trade] {batch.label}: trade failed ({exc}); retry {failures}/2", flush=True)
                    if failures >= 2:
                        report.ok = False
                        report.lines.append(f"{batch.label}: gave up -- {exc}")
                        break
            report.lines.append(f"{batch.label}: traded {made} of {batch.count}")
            if stop:
                break
        for phone in (a, b):
            with contextlib.suppress(Exception):
                await phone.go_home()
    report.detail = f"{done} trade(s) made" + ("; stopped" if stop else "")
    return report
