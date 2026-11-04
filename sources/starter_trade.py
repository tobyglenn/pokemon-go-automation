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
from typing import Sequence

from . import config_paths, gbl_home_recovery as home, pokemon_fleet, trade_local, trade_plan
from .starter import Phone, Report, StepFailed, open_storage

NEEDS_DIR = config_paths.state_dir() / "trade"


# --- counting ---------------------------------------------------------------------------

def _scanner(spec: pokemon_fleet.DeviceSpec):
    from . import raid_scan_android, raid_scan_ios
    if spec.platform == "ios":
        phone = raid_scan_ios.IOSPhone.connect(spec)
        return phone, raid_scan_ios.IOSStorageScanner(phone)
    phone = raid_scan_android.AndroidPhone(spec.identifier, spec.name)
    return phone, raid_scan_android.StorageScanner(phone, verbose=False)


def _count_terms(spec: pokemon_fleet.DeviceSpec, terms: Sequence[str]) -> dict[str, int]:
    phone, scanner = _scanner(spec)
    out: dict[str, int] = {}
    try:
        scanner.ensure_grid()
        for number, term in enumerate(terms, 1):
            count = scanner.search(term)[0]
            out[term] = int(count or 0)
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
                walk: bool = True) -> dict[str, int]:
    if walk:
        await _storage_first(spec, args)
    with pokemon_fleet.acquire_device_locks([spec]):
        return await asyncio.to_thread(_count_terms, spec, list(terms))


async def together(*jobs):
    """Every phone's job runs to its end before any failure is raised.

    A count runs in a thread, which nothing can cancel: letting one phone's
    failure end the step released the other phone's lock while its thread was
    still typing, and the next run then typed into the same field at the same
    time (android-three searched "9988pplluuss", 4 Nov).
    """
    results = await asyncio.gather(*jobs, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


async def inventory(pair: Sequence[pokemon_fleet.DeviceSpec], args,
                    fams: Sequence[trade_plan.Family]) -> list[trade_plan.PhoneCounts]:
    good_terms = [trade_plan.good_term(f) for f in fams]
    goods = await together(*(count(spec, args, good_terms) for spec in pair))
    counts = [trade_plan.PhoneCounts(spec.name, {f.key: g[trade_plan.good_term(f)] for f in fams})
              for spec, g in zip(pair, goods)]
    needed = [f for f in fams if any(c.good.get(f.key, 0) == 0 for c in counts)]
    # Every species of a needed family, and each family's first species as
    # something to send back (trade_plan.filler).
    species = list(dict.fromkeys([s for f in needed for s in f.species] + [f.species[0] for f in fams]))
    if not needed:
        species = []

    async def spares(spec, phone_counts):
        terms = [trade_plan.trade_term(s) for s in species]
        if spec.name in trade_plan.KEEP_LAST:
            terms += species
        # Still on the storage grid from the first pass.
        found = await count(spec, args, terms, walk=False)
        phone_counts.tradable = {s: found[trade_plan.trade_term(s)] for s in species}
        if spec.name in trade_plan.KEEP_LAST:
            phone_counts.owned = {s: found[s] for s in species}

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
    fams = trade_plan.families()
    counts = saved_counts(pair) if getattr(args, "reuse_counts", False) else None
    if counts is None:
        counts = await inventory(pair, args, fams)
    else:
        print("[trade] using the counts saved in the last two hours", flush=True)
    save_needs(counts)
    batches = trade_plan.plan_pair(counts[0], counts[1], fams, args.trades)
    hints = trade_plan.swap_hints(counts, other_counts({s.name for s in pair}), fams)
    for hint in hints:
        print(f"[trade] SWAP: {hint}", flush=True)
        report.lines.append("swap: " + hint)
    notify(hints)
    if not batches:
        report.detail = ("nothing to trade" + (f"; {len(suggestions)} Lucky Trade suggestion(s)" if suggestions else ""))
        return report
    # Few-copies trades are never made here: they are suggested, for a person
    # to make as Lucky Trades (the operator's rule, 4 Nov).
    suggestions = [b for b in batches if b.lucky_only]
    batches = [b for b in batches if not b.lucky_only]
    for batch in suggestions:
        line = f"suggest as a Lucky Trade: {batch.count} x {batch.label} ({batch.reason})"
        print(f"[trade] {line}", flush=True)
        report.lines.append(line)
    save_suggestions(suggestions)
    for batch in batches:
        print(f"[trade] plan: {batch.count} x {batch.label} ({batch.reason})", flush=True)
    done = 0
    if args.trade_plan_only:
        report.detail = f"planned {sum(b.count for b in batches)} trade(s); --trade-plan made none"
        return report
    async with pair_phones(pair, args) as (a, b):
        names = {a.name: await trade_local.trainer_name(a), b.name: await trade_local.trainer_name(b)}
        stop = False
        for batch in batches:
            terms = {name: trade_plan.trade_term(sp) for name, sp in batch.give.items()}
            made = failures = 0
            while made < batch.count and not stop:
                try:
                    await trade_local.trade_once(a, b, names, terms, batch.give)
                    made += 1
                    done += 1
                    print(f"[trade] {batch.label}: {made}/{batch.count} (day {done})", flush=True)
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
