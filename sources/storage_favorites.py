"""Synchronize manual storage and donor trade searches on the connected fleet."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess

from . import collect_trades, config_paths, fleet_entrypoint, gbl_home_recovery, pokemon_fleet, remote_target
from . import storage_review, trade_favorite_names, trade_saved_search as saved, trading_accounts


ALIASES = {
    "FarTrade": {"faraway", "farawaytrade"},
    "FarNot98": {"farplus3s", "farawaynot98", "farawaynt98", "farawaynt98g", "faraway!98%"},
    "LocalTrade": {"totradenear", "totradenea", "totradelocal", "totradeloc"},
    "LocalNot98": {"localnot98", "not98lucky"},
    "HundoReview": {"hundogapreview"},
    "Non-Raid": {"non-raidreview"},
}


# Searches this tool once pinned and now takes back off, by role. Each is
# backed up to sync.jsonl before removal, freeing a slot under GO's cap of 12.
RETIRED = {
    "donor": ("HundoReview", "TradedLucky"),
    # A near-copy of LocalNot98 (same results); its slot holds ToPower.
    "receiver": ("LocalSpare",),
}
POWER = "ToPower"
# The tag the owner puts on Pokémon to invest in. No list that trades or
# transfers may show one.
POWER_GUARD = "!#ToPower"


def power_term(account):
    """ToPower: the account's candy targets from its latest raid scan, plus the tag."""
    from . import raid_inventory, raid_plan, raid_scan

    # A phone can move between computers, leaving an older scan behind on
    # the other one: use whichever copy was taken most recently.
    best = []
    try:
        machines = fleet_entrypoint.machine_configs()
        here = fleet_entrypoint.local_machine(machines)
    except (pokemon_fleet.FleetError, OSError):
        machines, here = {}, None
    for machine in [None, *(m for m in machines if m != here)]:
        try:
            rows, _ = raid_scan.read_scan(account, None, machine)
        except (pokemon_fleet.FleetError, OSError, subprocess.SubprocessError, ValueError):
            continue
        rows = [r for r in rows if isinstance(r, dict) and r.get("schema") == raid_inventory.SCHEMA]
        newest = max((r.get("scanned_at") or 0 for r in rows), default=0)
        if rows and newest > max((r.get("scanned_at") or 0 for r in best), default=0):
            best = rows
    scans = [raid_inventory.SpeciesScan.from_dict(r) for r in best]
    if not scans:
        return f"#{POWER}"
    return raid_plan.power_term(raid_plan.power_picks(raid_plan.build(account, scans)), tag=POWER)


# Trade lists that must never offer a garbage species (no raid, Mega, Max or
# PvP use): those go to SafeTrash instead (user, 23 Nov 2026).
NO_GARBAGE = ("LocalTrade",)


# A donor's local trade lists never offer a species missing from the receiver's Lucky
# Dex. Those belong in the dedicated receiver favorite. Perfect Dex and
# XXL/XXS species may still show in local trade lists.
NO_LUCKY_GAPS = ("LocalTrade", "LocalNot98")


def without_species(policy, excluded):
    """A native clause that drops the given species, written short."""
    from . import clear_pokemon

    excluded = sorted(set(excluded or ()))
    if not excluded:
        return None
    wanted = sorted(set(policy.names) - set(excluded))
    keep = clear_pokemon.format_dex_ranges(wanted)
    drop = "&".join("!" + part for part in clear_pokemon.format_dex_ranges(excluded).split(","))
    return keep if len(keep) <= len(drop) else drop


def without_garbage(policy):
    """A native clause that drops the policy's garbage species, written short."""
    return without_species(policy, getattr(policy, "trash_species", None))


def key(text):
    return "".join(text.casefold().split())


def _with_filters(term, filters):
    if term == storage_review.EMPTY:
        return term
    tokens = term.split("&")
    present = {t.casefold() for t in tokens}
    for value in filters:
        if value.casefold() not in present:
            tokens.append(value)
            present.add(value.casefold())
    return "&".join(tokens)


def wanted(device, policy):
    device = trading_accounts.account_for(device)
    cfg = json.loads(config_paths.default_config("storage-favorites.json").read_text())
    common = cfg["common"]
    if not common or any(not k.strip() or not v.strip() for k, v in common.items()):
        raise ValueError("The common favorite names and filters must be nonempty")
    roles = policy.favorites(device)
    roles[POWER] = power_term(device)
    if device == policy.receiver:
        trade = collect_trades.load_config()
        query = collect_trades.build_return_queries(
            return_filters=trade.get("return_filters", collect_trades.REQUIRED_RETURN_FILTERS),
            exclude_dex=trade.get("exclude_return_dex", ()),
            return_dex_tiers=trade.get("return_dex_tiers"),
        )[0]
        roles.update(cfg.get("receiver", {}))
        roles["TradeReturns"] = "&".join(query.split("&")[:-1])
    else:
        from . import ender_inventory

        roles.pop(storage_review.PRIORITY_TRADES, None)
        roles.pop("HundoReview", None)
        roles.update({trade_favorite_names.role_for(key): term for key, term in cfg.get("donor", {}).items()})
        roles["LuckyOffer"] = ender_inventory.lucky_offer_term()
    requested = {"".join(name.split()): term for name, term in {**common, **roles}.items()}
    garbage = set(getattr(policy, "trash_species", None) or ())
    lucky_gaps = set() if device == policy.receiver else set(getattr(policy, "lucky", None) or ())
    for label in dict.fromkeys((*NO_GARBAGE, *NO_LUCKY_GAPS)):
        if label not in requested or requested[label] == storage_review.EMPTY:
            continue
        no_garbage = label in NO_GARBAGE and bool(garbage)
        excluded = (garbage if no_garbage else set()) | (lucky_gaps if label in NO_LUCKY_GAPS else set())
        clause = without_species(policy, excluded)
        if not clause:
            continue
        # Any IV short of 98% (a trade rerolls IVs), but never a
        # two-perfect-stat spread that may be an unappraised 98.
        guard = storage_review.HIGH_IV_GUARD if no_garbage else ()
        requested[label] = _with_filters(requested[label], [*clause.split("&"), *guard])
    if device == policy.receiver:
        personal = tuple(cfg.get("receiver", {}))
        guarded = cfg.get(
            "receiver_keeper_lists",
            ["FarTrade", "FarNot98", "LocalTrade", "LocalNot98", "SafeTrash", "Non-Raid", "TradeReturns"],
        )
        for label in (*guarded, *personal):
            if label in requested:
                requested[label] = _with_filters(
                    requested[label],
                    cfg.get(
                        "receiver_keeper_filters",
                        ["!#KeepOne", "!favorite", "!lucky", "!4*", "!98Plus", "!dontrade"],
                    ),
                )
        outgoing = storage_review.receiver_outgoing_filter(policy)
        for label in ("FarTrade", "FarNot98", "LocalTrade", "LocalNot98", "TradeReturns", *personal):
            if label in requested:
                requested[label] = (
                    storage_review.EMPTY
                    if outgoing == storage_review.EMPTY
                    else _with_filters(requested[label], outgoing.split("&"))
                )
        requested["TradeReturns"] = _with_filters(requested["TradeReturns"], ["!#", "!#KeepOne", "!costume"])
    # The appraisal shortcut deliberately stays broad enough to find 98% candidates.
    # Every outgoing or disposal list excludes native perfect IVs and the
    # ToPower tag on every handset.
    return {label: (term if label in {"98%", POWER} else _with_filters(term, ["!4*", POWER_GUARD]))
            for label, term in requested.items()}


# Full native values of retired donor searches, including the original unlabeled
# sizes and traded Lucky filters. A clipped tile label alone cannot identify one.
RETIRED_DONOR_QUERIES = frozenset({
    "!98plus&!dontrade&!shiny&!gigantamax&!traded",
    "!98plus&xxl,xxs",
    "lucky&!98plus&traded&!legendary&!ultrabeast&!mythical&!98plus&!dontrade&!max mega&!topower&!mega",
    "!shiny&!mythical&!shadow&!legendary&distance-100&!98plus&!dontrade&!gigantamax&!traded",
})


def disposable(label, term, *, donor=False, priority_term=None):
    if re.fullmatch(r"T[0-9A-F]{7}", key(label), re.I) or term.strip() == "0*,1*,2*":
        return True
    if donor and key(label) == key(trade_favorite_names.label(storage_review.PRIORITY_TRADES)) and term == priority_term:
        return True
    if donor and ("." in label or "…" in label or key(label) == "generaluntraded"):
        if term.strip().casefold() in RETIRED_DONOR_QUERIES:
            return True
    # An unnamed saved copy of one of the collector's generated picker
    # queries may survive a failed rename. Preserve all other custom searches.
    if not (".." in label or "…" in label):
        return False
    tokens = set(term.casefold().split("&"))
    return {"!dontrade", "!98plus", "!favorite", "!shiny", "!4*", "!lucky", "!traded",
            "!legendary", "!mythical", "!ultra beasts", "!costume", "!background",
            "!dynamax", "!gigantamax"} <= tokens


def record(folder, event):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "sync.jsonl").open("a") as out:
        out.write(json.dumps({"at": datetime.now().astimezone().isoformat(), **event}) + "\n")


def _receipt_path(folder):
    return folder / "verified-favorites.json"


def _read_receipt(folder):
    path = _receipt_path(folder)
    return json.loads(path.read_text()) if path.exists() else {"favorites": {}}


async def verify_named(phone, folder, name, term):
    """Save the exact native query with its actual opened grid for offline preparation."""
    native = await saved.read_saved_name(phone, name)
    if native != name:
        if key(native) != key(name):
            raise ValueError(f"native favorite {native!r} is not {name!r}")
        record(folder, {"action": "backup", "label": native, "query": term})
        await saved.rename_favorite(phone, native, name, expected_term=term)
        native = await saved.read_saved_name(phone, name)
        if native != name:
            raise ValueError(f"saved native name stayed {native!r} instead of {name!r}")
    actual, grid, boxes = await saved._read_storage_results(phone, name, term)
    count = saved.clear_pokemon.result_count(boxes, grid.height)
    stamp = datetime.now().astimezone().isoformat()
    image_dir = folder / "verified-grids"
    image_dir.mkdir(parents=True, exist_ok=True)
    shot = image_dir / (name.replace("/", "-") + ".png")
    grid.save(shot)
    proof = {"name": native, "term": actual, "count": count, "verified_at": stamp, "screenshot": str(shot)}
    data = _read_receipt(folder)
    data.update(device=getattr(phone, "transport_name", phone.name), account=phone.name, verified_at=stamp)
    data["favorites"][name] = proof
    path = _receipt_path(folder)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    return proof


async def sync_phone(phone, policy, *, refresh=0):
    folder = config_paths.state_dir() / "storage-favorites" / getattr(phone, "transport_name", phone.name)
    if refresh:
        await storage_review.verify_anchors(phone, policy, limit=refresh)
    workflows = wanted(phone.name, policy)
    requested = {trade_favorite_names.label(role): term for role, term in workflows.items()}
    if len({key(label) for label in requested}) != len(workflows):
        raise ValueError("A receiver-specific favorite label duplicates another storage favorite")
    failures = []
    wanted_terms = {term.strip().casefold() for term in requested.values()}
    initial = await saved.list_favorites(phone)
    donor = trading_accounts.account_for(phone.name) != policy.receiver
    priority_term = policy.favorites(phone.name).get(storage_review.PRIORITY_TRADES) if donor else None
    retired = [name for name in RETIRED["donor" if donor else "receiver"] if name not in requested]
    for name in retired:
        old = await saved.read_favorite(phone, name)
        if old is not None:
            record(folder, {"action":"backup","label":name,"query":old})
            if not await saved.remove_favorite(phone,name,expected_term=old):
                raise ValueError(f"could not remove the retired {name} favorite")
            record(folder, {"action":"removed","label":name,"query":old})
    current = _read_receipt(folder)
    if [name for name in retired if current["favorites"].pop(name, None) is not None]:
        path = _receipt_path(folder)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(current,indent=2))
        os.replace(tmp,path)
    for label in initial:
        if label in requested:
            continue
        if not (re.fullmatch(r"t[0-9a-f]{7}", key(label)) or "*" in label or "." in label
                or "…" in label or (trade_favorite_names.role_for(label) == storage_review.PRIORITY_TRADES or key(label) == "generaluntraded")):
            continue
        try:
            term = await saved.read_favorite(phone, label)
            # Reuse a matching unnamed pin left by an interrupted rename.
            if term and term.strip().casefold() in wanted_terms and not re.fullmatch(r"t[0-9a-f]{7}", key(label)):
                continue
            if term and disposable(label, term, donor=donor, priority_term=priority_term):
                record(folder, {"action": "backup", "label": label, "query": term})
                if await saved.remove_favorite(phone, label, expected_term=term):
                    record(folder, {"action": "removed", "label": label})
        except (RuntimeError, ValueError, OSError, gbl_home_recovery.HomeRecoveryError) as exc:
            failures.append(f"{label}: {exc}")

    for name, term in requested.items():
        try:
            role = trade_favorite_names.role_for(name)
            aliases = (ALIASES.get(name, set()) | {key(other) for other in trade_favorite_names.names(role)}) - {key(name)}
            previous = [(label, await saved.read_favorite(phone, label))
                        for label in initial if key(label) in aliases]
            existing = await saved.read_favorite(phone, name)
            if existing and existing.strip().casefold() != term.strip().casefold():
                record(folder, {"action": "backup", "label": name, "query": existing})
            if existing is None:
                match = next((label for label, expr in previous
                              if expr and expr.strip().casefold() == term.strip().casefold()), None)
                if match:
                    await saved.rename_favorite(phone, match, name, expected_term=term)
                    previous = [(label, expr) for label, expr in previous if label != match]
            await saved.ensure_favorite(phone, name, term)
            proof = await verify_named(phone,folder,name,term)
            record(folder, {"action":"verified","label":name,"query":proof["term"],"count":proof["count"]})
            print(f"[favorites] {phone.name}: verified {name} ({proof['count']})", flush=True)
            for label, expr in previous:
                if expr:
                    record(folder, {"action": "backup", "label": label, "query": expr})
                    if await saved.remove_favorite(phone, label, expected_term=expr):
                        record(folder, {"action": "removed", "label": label})
        except (RuntimeError, ValueError, OSError, gbl_home_recovery.HomeRecoveryError) as exc:
            failures.append(f"{name}: {exc}")
            print(f"[favorites] {phone.name}: {failures[-1]}", flush=True)
    return failures


async def run(args):
    fleet_entrypoint.apply_local_environment()
    cfg = collect_trades.load_config()
    policy = storage_review.load_policy(config_paths.default_config(cfg["clear_config"]), cfg)
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    if args.devices and "all" in args.devices and len(args.devices) != 1:
        raise ValueError("Use --devices all by itself")
    default_scope = not args.devices or args.devices == ["all"]
    mapping = trading_accounts.aliases()
    names = list(dict.fromkeys([policy.receiver, *policy.donors] if default_scope else args.devices))
    if getattr(args, "keep_one", None):
        if not default_scope and (len(names) != 1 or mapping.get(names[0], names[0]) != policy.receiver):
            raise ValueError(f"Keep One can only run on the receiver, {policy.receiver}")
        if default_scope:
            names = [policy.receiver]
    unknown = set(names) - set(fleet.devices)
    if unknown:
        raise ValueError(f"Unknown device(s): {sorted(unknown)}")
    if args.plan:
        for name in names:
            print(f"{name}: {', '.join(wanted(name, policy))}")
        return 0
    machines = fleet_entrypoint.machine_configs()
    if args.local:
        local = fleet_entrypoint.local_machine(machines)
        machines = {local: machines[local]}
    owners, _ = remote_target.discover_attached_devices(fleet, machines)
    preferred = trading_accounts.transports(owners, mapping)
    # An explicitly requested alternate handset gets its own local favorites.
    # The receiver name automatically falls back to its attached alternate.
    devices = {name: name if name in mapping else preferred.get(name, name) for name in names}
    if args.local and default_scope:
        names = [name for name in names if devices[name] in owners]
    names = list({devices[n]: n for n in names}.values())
    shots = config_paths.state_dir() / "storage-favorites" / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    account_locks = {mapping.get(n, n): asyncio.Lock() for n in names}

    async def one(name, *, refresh=False):
        device = devices[name]
        account = mapping.get(name, name)
        if refresh and account != policy.receiver:
            return []
        if device not in owners:
            return [f"{device} offline"]
        routed, _ = trading_accounts.route_fleet(fleet, {device: owners[device]}, mapping)
        # Each handset has its own favorites, but both receiver handsets open
        # the same Pokémon GO account. Only one may work on it at a time.
        async with account_locks[account], remote_target.open_fleet_phone(
                account, shots, machine=owners[device], machines=machines, fleet=routed) as phone:
            if refresh:
                if getattr(args, "keep_one", None):
                    from . import storage_keep_one
                    limit = None if args.keep_one == "all" else int(args.keep_one)
                    rows = await storage_keep_one.prepare_living_dex(phone, policy, limit=limit)
                    return [f"#{row['dex']}: {row['error']}" for row in rows if row["status"] == "read deferred"]
                else:
                    await storage_review.verify_anchors(phone, policy, limit=args.refresh_anchors)
                return []
            failed = await sync_phone(phone, policy)
            if not failed and getattr(args, "prepare_trades", False):
                from . import ender_inventory
                await ender_inventory.prepare_phone(phone)
            return failed
    if args.refresh_anchors or getattr(args, "keep_one", None):
        # Finish the requested one-time storage preparation before pinning.
        receiver_names = [n for n in names if mapping.get(n, n) == policy.receiver]
        preparation = [policy.receiver] if policy.receiver in receiver_names else receiver_names[:1]
        refreshed = await asyncio.gather(*(one(n, refresh=True) for n in preparation), return_exceptions=True)
        for name, result in zip(preparation, refreshed):
            if result:
                print(f"[favorites] {name}: retained-copy refresh incomplete: {result}", flush=True)
    else:
        refreshed = []
    results = await asyncio.gather(*(one(n) for n in names), return_exceptions=True)
    for name, result in zip(names, results):
        if result:
            print(f"[favorites] {name}: {result}", flush=True)
    if getattr(args, "prepare_trades", False):
        from . import collector_proposals, ender_inventory
        ender_inventory.prepare()
        routed, routed_owners = trading_accounts.route_fleet(fleet, owners, mapping)
        prepared = {mapping.get(n, n) for n, result in zip(names, results)
                    if not result and mapping.get(n, n) in routed_owners}
        await collector_proposals.prepare_favorite_pairs(cfg, routed, machines, routed_owners, prepared, shots)
    return int(any(results) or any(refreshed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the account-specific favorite names offline")
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--local", action="store_true", help="use only phones attached to this computer")
    route.add_argument("--allmachines", action="store_true", help="use both computers (default)")
    parser.add_argument("--devices", nargs="+", help="limit work to named accounts")
    parser.add_argument("--prepare-trades", action="store_true",
                        help="verify named trades and index eligible Pokémon for Ender during setup")
    prepare = parser.add_mutually_exclusive_group()
    prepare.add_argument("--refresh-anchors", type=int, default=0, metavar="COUNT",
                        help="check up to COUNT additional species for a retained copy on the receiver")
    prepare.add_argument("--keep-one", metavar="COUNT|all",
                         help="explicitly tag one retained specimen of eligible duplicates before syncing")
    args = parser.parse_args()
    if args.refresh_anchors < 0:
        parser.error("--refresh-anchors must be nonnegative")
    if args.keep_one is not None and args.keep_one != "all" and not (
            args.keep_one.isdecimal() and int(args.keep_one) > 0):
        parser.error("--keep-one takes a positive number of candidate species or all")
    return asyncio.run(run(args))
