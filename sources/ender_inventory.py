"""Prepare Ender's choices from verified handset favorites before the evening run."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import clear_pokemon as clear, collect_trades as collect, config_paths, trade_favorite_names, trading_accounts

SNAPSHOT_VERSION = 3

TRADE_FAVORITES = ("TradeReturns", trade_favorite_names.LUCKY, trade_favorite_names.HUNDO, "PeerTrades", "LuckyOffer", "RaidSwap",
                   "FarTrade", "LocalTrade", "FarNot98", "LocalNot98")


def lucky_offer_term():
    """Native union of eligible two-star Legendaries and explicitly marked Legendary spares."""
    return ("legendary&2*,#ToTrade&!#,#ToTrade&!98Plus&!#KeepOne&!dontrade&!favorite"
            "&!costume&!purified&!buddy1-5&!shiny&!lucky&!4*&!shadow&!traded&!background"
            "&!dynamax&!gigantamax")


def path():
    return config_paths.state_dir() / "collect-trades" / "ender-prepared.json"


def _dex_manifest_versions(receiver):
    root = config_paths.state_dir()
    regional = root / "dex-registrations" / receiver
    versions = {}
    for key, folder, filename in (("lucky", regional, "lucky.json"),
                                  ("perfect", regional, "perfect.json"),
                                  ("perfect_base", root / "perfect-audit", "summary.json")):
        file = max(folder.glob(f"*/{filename}"), default=None)
        if file is not None:
            info = file.stat()
            versions[key] = [file.parent.name, info.st_size, info.st_mtime_ns]
    return versions


def load():
    file = path()
    if not file.is_file():
        return {}
    data = json.loads(file.read_text())
    if data.get("version") != SNAPSHOT_VERSION or not isinstance(data.get("handsets"), dict):
        return {}
    targets = data.get("dex_targets", {})
    receiver = targets.get("receiver")
    if receiver and targets.get("manifest_versions") != _dex_manifest_versions(receiver):
        data.pop("dex_targets", None)
        for handset in data["handsets"].values():
            for label in (*trade_favorite_names.names(trade_favorite_names.LUCKY),
                          *trade_favorite_names.names(trade_favorite_names.HUNDO), "SafeTrash"):
                handset.get("favorites", {}).pop(label, None)
    raid = path().with_name("ender-raid-pairs.json")
    data["raid_pairs"] = json.loads(raid.read_text()) if raid.is_file() else {}
    return data


def visible_tiles(image, boxes):
    for tile in clear.safe_grid_tiles(image, boxes):
        cp = int(re.search(r"\d+", tile.text).group())
        yield collect._tile_species_label(image, boxes, tile), cp, tile


def prepare():
    """Offline preparation only. Ender never builds or refreshes this file."""
    from . import collector_day, dex_registration, pokemon_fleet, storage_favorites, storage_review
    cfg = collect.load_config()
    clear_path = config_paths.default_config(cfg["clear_config"])
    versions = _dex_manifest_versions(cfg["receiver"])
    policy = storage_review.load_policy(clear_path, cfg)
    root = config_paths.state_dir() / "storage-favorites"
    handsets = {}
    for file in sorted(root.glob("*/verified-favorites.json")):
        data = json.loads(file.read_text())
        handset = file.parent.name
        name = trading_accounts.account_for(handset)
        if name not in (policy.receiver, *policy.donors):
            continue
        if data.get("device") != handset or data.get("account") != name:
            continue
        wanted = storage_favorites.wanted(name, policy)
        favorites = {}
        stock = read_stock(file.parent, handset, name)
        for label, row in data["favorites"].items():
            term = row.get("term", "")
            if not term or clauses(term) != clauses(wanted.get(trade_favorite_names.role_for(label), "")):
                continue
            # Only the upfront native Dex check can turn a displayed name into a trade choice.
            proof = stock.get(label, {})
            if proof.get("term") != term or proof.get("receipt") != row["verified_at"]:
                proof = {}
            favorites[label] = {"term": term, "pages": proof.get("pages", []), "verified_at": row["verified_at"],
                                "stock_count": proof.get("total"), "individuals": proof.get("individuals", [])}
        handsets[handset] = {"account": name, "favorites": favorites}
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    lucky = collector_day.LuckyGaps.load(clear_path, policy.receiver, fleet.devices[policy.receiver].identifier)
    result = {"version": SNAPSHOT_VERSION, "handsets": handsets, "dex_targets": {
        "receiver": policy.receiver, "manifest_versions": versions,
        "lucky": sorted(policy.lucky), "lucky_journal": lucky.path.name,
        "perfect": sorted(policy.hundo), "perfect_audit_day": dex_registration.perfect_status(cfg).audit_day,
    }}
    if versions != _dex_manifest_versions(policy.receiver):
        raise RuntimeError("Pokédex evidence changed during preparation. Refresh the saved targets.")
    save(path(), result)
    return result


def save(dest, result):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dest.parent, delete=False) as out:
        try:
            json.dump(result, out)
            out.flush()
            os.fsync(out.fileno())
            os.replace(out.name, dest)
        finally:
            Path(out.name).unlink(missing_ok=True)


def account_favorites(snapshot, name, handset):
    record = snapshot.get("handsets", {}).get(handset, {})
    return record.get("favorites", {}) if record.get("account") == name else {}


@dataclass(frozen=True)
class Choice:
    name: str  # Native favorite name
    query: str  # Exact prepared Dex number
    term: str  # Full native favorite expression
    species: str
    cps: tuple[int, ...]
    page: int = 0
    stock_count: int | None = None


def clauses(term):
    return {part.strip().casefold() for part in term.split("&") if part.strip()}


def covers(term, restrictions, dex):
    """Require every native eligibility clause. A broad distance favorite alone cannot authorize a trade."""
    tokens = clauses(term)
    for rule in restrictions:
        rule = rule.casefold()
        if rule in tokens or (rule.startswith("!#") and "!#" in tokens):
            continue
        if re.fullmatch(r"![0-9,-]+", rule) and dex not in clear.parse_dex_ranges(rule[1:]):
            continue
        return False
    return True


def candidates(favorites, targets, keep):
    choices = []
    seen = set()
    for target in targets:
        rules = target.term.split("&")
        if not re.fullmatch(r"[\d,-]+", rules[-1]):
            continue
        allowed = clear.parse_dex_ranges(rules.pop())
        for role in TRADE_FAVORITES:
            label = next((name for name in trade_favorite_names.names(role) if name in favorites), role)
            row = favorites.get(label, {})
            for page, entries in enumerate(row.get("pages", ())):
                for number, stock in entries.items():
                    dex = int(number)
                    if (dex not in allowed or dex in seen or len(stock["cps"]) <= keep
                            or not covers(row["term"], rules, dex)):
                        continue
                    choices.append(Choice(label, number, row["term"], stock["species"], tuple(stock["cps"]), page))
                    seen.add(dex)
    if targets and all(t.name.startswith(("lucky-gap-", "perfect-gap-")) for t in targets):
        choices.sort(key=lambda item: (item.page, -len(item.cps)))
    return choices


def incoming_since_audit(histories, receiver, since):
    """Hold rerolls until the next Perfect Dex audit after a matching or unidentified receipt."""
    result = set()
    for event in histories:
        pair = [trading_accounts.account_for(n) for n in event.get("pair", ())]
        if len(pair) != 2 or receiver not in pair or not event.get("at"):
            continue
        if datetime.fromisoformat(event["at"]).date().isoformat() < since:
            continue
        peer = pair[1 - pair.index(receiver)]
        offered = {trading_accounts.account_for(n): d for n, d in event.get("offered_dex", {}).items()}
        result.add((receiver, int(offered[peer]) if offered.get(peer) else None))
    return result


def _available_tiles(grid, boxes, choice):
    remaining = Counter(choice.cps)
    matches = []
    for species, cp, tile in visible_tiles(grid, boxes):
        if species == choice.species and remaining[cp] > 0:
            matches.append((cp, tile))
            remaining[cp] -= 1
    return matches


async def prepare_picker_options(phone, choices, keep):
    """Locate ranked candidates in this partner's real picker in one pass, without offering."""
    from . import starter, trade_local, trade_saved_search as saved
    proofs = {}
    if not choices:
        return proofs
    image, boxes = await phone.read()
    term = lucky_offer_term()
    grid, boxes = await saved.open_named_picker(phone, "LuckyOffer", term, image, boxes)
    total = clear.result_count(boxes, grid.height)
    if total is None or not clear.evolutionary_line_off(grid, boxes):
        raise starter.StepFailed(f"{phone.name}: exact-species stock unreadable in the trade picker")
    previous = None
    for page in range(total + 1):
        tiles = list(visible_tiles(grid, boxes))
        frame = [(species, cp) for species, cp, _ in tiles]
        if not frame or frame == previous:
            break
        previous = frame
        screenshot = None
        for identity, choice in choices.items():
            if identity in proofs or choice.name != "LuckyOffer" or clauses(choice.term) != clauses(term):
                continue
            dex, form, exact_cp = identity
            cps = list((Counter(cp for species, cp in frame if species == choice.species) & Counter(choice.cps)).elements())
            if cps.count(exact_cp) == 1 and len(cps) > keep:
                screenshot = screenshot or str(phone.save(grid, "prepared-legendary-picker"))
                proofs[identity] = {"count":total, "page":page, "cps":cps, "dex":dex,
                                    "cp":exact_cp, "term":choice.term, "screenshot":screenshot}
        if len(proofs) == len(choices) or total <= len(tiles):
            break
        await scroll_page(phone, grid)
        grid, boxes = await phone.read()
        if (trade_local.expired(boxes) or not saved._picker_or_storage_grid(grid, boxes)
                or clear.result_count(boxes, grid.height) != total):
            raise starter.StepFailed(f"{phone.name}: trade picker changed during setup")
    return proofs


async def pick(phone, choice, *, keep, exact_cp=None):
    """Tap a prepared choice in the verified native picker favorite, without typing or paging through inventory."""
    from . import starter, trade_local, trade_saved_search as saved
    image, boxes = await phone.read()
    grid, boxes = await saved.open_named_picker(phone, choice.name, choice.term, image, boxes)
    if not clear.evolutionary_line_off(grid, boxes):
        raise starter.StepFailed(f"{phone.name}: could not verify exact-species mode in {choice.name}")
    if exact_cp is not None and (choice.stock_count is None or
                                 clear.result_count(boxes, grid.height) != choice.stock_count):
        raise starter.StepFailed(f"{phone.name}: size of the prepared Legendary favorite has changed")
    if choice.page:
        for _ in range(choice.page):
            await scroll_page(phone, grid)
        grid, boxes = await phone.read()
        if trade_local.expired(boxes) or not saved._picker_or_storage_grid(grid, boxes):
            raise starter.StepFailed(f"{phone.name}: prepared page unavailable in {choice.name}")
    matches = _available_tiles(grid, boxes, choice)
    if len(matches) <= keep:
        raise starter.StepFailed(f"{phone.name}: {choice.name} no longer shows the prepared spare and reserve")
    choices = [(cp, tile) for cp, tile in matches if exact_cp is None or cp == exact_cp]
    if not choices or (exact_cp is not None and len(choices) != 1):
        raise starter.StepFailed(f"{phone.name}: prepared individual unavailable in {choice.name}")
    selected_cp, tile = choices[0]
    await phone.tap([tile.center_x, tile.center_y + int(grid.height * clear.TILE_RISE)], grid)
    ok, frame, detail = await trade_local.settle(phone, trade_local.detail, 8)
    if not ok or trade_local.trade_warning(detail):
        raise starter.StepFailed(f"{phone.name}: prepared choice did not reach a clean trade preview")
    readings = trade_local._selected_range(frame, detail, "CP")
    label = trade_local._offer_label(frame, detail, readings[0][0]) if len(readings) == 1 else None
    if (not label or clear.normalize_species_token(label) != choice.species
            or readings[0][1][0] != selected_cp):
        raise starter.StepFailed(f"{phone.name}: selected CP or species label did not match {choice.name} preparation")
    return len(matches)


def read_stock(folder, handset, account):
    path = folder / "prepared-stock.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if data.get("handset") != handset or data.get("account") != account:
        raise ValueError(f"Incorrect prepared-stock owner in {path}")
    return data["favorites"]


def save_stock(folder, handset, account, favorites):
    path = folder / "prepared-stock.json"
    with tempfile.NamedTemporaryFile("w", dir=folder, delete=False) as out:
        try:
            json.dump({"handset": handset, "account": account, "favorites": favorites}, out)
            out.flush()
            os.fsync(out.fileno())
            os.replace(out.name, path)
        finally:
            Path(out.name).unlink(missing_ok=True)


async def scroll_page(phone, image):
    x = round(image.width * .45)
    await phone.swipe([x, round(image.height*.75)], [x, round(image.height*.38)], image, .55, scroll=True)
    await asyncio.sleep(.45)


async def index_favorite(phone, label, term, catalog, *, sought=None, reserve=0):
    from . import starter, trade_saved_search as saved
    grid, boxes = await saved.open_named_storage(phone, label, term)
    total = clear.result_count(boxes, grid.height)
    if total is None or not clear.evolutionary_line_off(grid, boxes):
        raise starter.StepFailed(f"{phone.name}: {label} count or exact-species mode unavailable")
    pages = []
    remaining = set(sought) if sought is not None else None
    previous = None
    for _ in range(total + 1):
        tiles = [(species, cp) for species, cp, tile in visible_tiles(grid, boxes)]
        if total and not tiles:
            raise starter.StepFailed(f"{phone.name}: unreadable Pokémon tiles in {label}")
        if not total or tiles == previous:
            break
        previous = tiles
        entries = {}
        for species, cp in tiles:
            if species in catalog:
                entries.setdefault(str(catalog[species]), []).append(cp)
        pages.append(entries)
        if remaining is not None:
            for number, cps in entries.items():
                if len(cps) > reserve:
                    remaining.difference_update((number, cp) for cp in cps)
            if not remaining:
                break
        if total <= len(tiles):
            break
        await scroll_page(phone, grid)
        grid, boxes = await phone.read()
        if not saved._picker_or_storage_grid(grid, boxes) or clear.result_count(boxes, grid.height) != total:
            raise starter.StepFailed(f"{phone.name}: {label} changed during upfront preparation")
    return total, pages


async def certify_cps(phone, number, species, cps):
    """Prove native Dex, label and CP. Rule out overlapping nicknames in the rest of storage."""
    group = ",".join(f"cp{cp}" for cp in sorted(cps))
    image, boxes = await phone.read()
    count, image, boxes, _ = await clear._apply_and_verify_query(
        phone, f"{number}&{group}&{number}", image, boxes, require_exact=True)
    if not clear.evolutionary_line_off(image, boxes):
        raise clear.ClearSafetyError(f"{phone.name}: exact-species mode unavailable for #{number}")
    native = Counter(cp for label, cp, _ in visible_tiles(image, boxes) if label == species)
    if not count or not native:
        return []
    count, image, boxes, _ = await clear._apply_and_verify_query(
        phone, f"!{number}&{group}&!{number}", image, boxes, require_exact=True)
    if not clear.evolutionary_line_off(image, boxes):
        raise clear.ClearSafetyError(f"{phone.name}: exact-species comparison unavailable for #{number}")
    if count:
        others = list(visible_tiles(image, boxes))
        if len(others) != count:
            return []
        for label, cp, _ in others:
            if not label or label == species:
                native.pop(cp, None)
    return list(native.elements())


async def _exact_count(phone, query):
    count, frame, boxes, _ = await clear._apply_and_verify_query(phone, query, require_exact=True)
    if not clear.evolutionary_line_off(frame, boxes):
        raise clear.ClearSafetyError(f"{phone.name}: exact-species mode unavailable")
    return count


async def eligible_legendary_species(phone, shortlist, catalog):
    """Resolve a shortlist against the native union once, including nicknamed Pokémon."""
    term = lucky_offer_term()
    admitted = set()
    async def inspect(numbers):
        if not numbers:
            return
        group = clear.format_dex_ranges(sorted(numbers))
        count, grid, boxes, _ = await clear._apply_and_verify_query(phone, f"{term}&{group}", require_exact=True)
        if not clear.evolutionary_line_off(grid, boxes):
            raise clear.ClearSafetyError(f"{phone.name}: exact-species mode unavailable in LuckyOffer")
        if not count:
            return
        if len(numbers) == 1:
            admitted.update(numbers)
            return
        labels = {label for label, _, _ in visible_tiles(grid, boxes)}
        hints = {dex for dex in numbers if catalog[dex] in labels}
        if hints:
            # A nickname can admit an extra number. The independent search of
            # the remainder still includes every species actually present.
            admitted.update(hints)
            await inspect(numbers - hints)
            return
        ordered = sorted(numbers)
        split = len(ordered) // 2
        await inspect(set(ordered[:split]))
        await inspect(set(ordered[split:]))
    await inspect(set(shortlist))
    return admitted


async def prepare_legendary(phone, offers, reserve):
    """Prove ranked individuals inside the exact saved union and cache their visible page."""
    from . import collector_day, collector_proposals, raid_offer_scan
    handset = getattr(phone, "transport_name", phone.name)
    account = trading_accounts.account_for(handset)
    folder = config_paths.state_dir() / "storage-favorites" / handset
    receipt = json.loads((folder / "verified-favorites.json").read_text())
    if receipt["device"] != handset or receipt["account"] != account:
        raise ValueError("Legendary favorite belongs to another handset")
    row = receipt["favorites"]["LuckyOffer"]
    term = row["term"]
    if clauses(term) != clauses(lucky_offer_term()):
        raise ValueError("LuckyOffer changed since preparation")
    cfg = collect.load_config()
    names = collector_day.species_catalog(config_paths.default_config(cfg["clear_config"]))
    targets = {}
    for offer in offers:
        if not offer.get("live_verified") or offer.get("unique_count") != 1 or not offer.get("form_verified"):
            continue
        dex, cp = str(int(offer["dex"])), int(offer["cp"])
        exact, _ = collector_proposals.identity_query(offer)
        tokens = {dex, f"cp{cp}"}
        if offer.get("form_token"):
            tokens.add(offer["form_token"].casefold())
        if not any(clauses(exact) == clauses(pool) | tokens
                   for pool in (raid_offer_scan.EXCLUSIONS, raid_offer_scan.SPARES)):
            continue
        targets.setdefault(dex, []).append(offer)
    sought = {(dex, int(offer["cp"])) for dex, individuals in targets.items() for offer in individuals}
    total, pages = None, []
    if sought:
        catalog = {species: dex for dex, species in names.items()}
        total, pages = await index_favorite(phone, "LuckyOffer", term, catalog, sought=sought, reserve=reserve)
    record = {"term": term, "receipt": row["verified_at"], "total": total, "pages": [{} for _ in pages],
              "individuals": []}
    stock = read_stock(folder, handset, account)
    stock["LuckyOffer"] = record
    save_stock(folder, handset, account, stock)
    for dex, individuals in targets.items():
        species = names[int(dex)]
        candidate_cps = {int(o["cp"]) for o in individuals}
        visible = [p[dex] for p in pages if dex in p and candidate_cps.intersection(p[dex]) and len(p[dex]) > reserve]
        if not visible:
            continue
        cps = {cp for page in visible for cp in page if reserve or cp in candidate_cps}
        proven = Counter(await certify_cps(phone, dex, species, cps))
        for page, entries in enumerate(pages):
            overlap = Counter(entries.get(dex, ())) & proven
            if sum(overlap.values()) > reserve:
                record["pages"][page][dex] = {"species": species, "cps": list(overlap.elements())}
        for offer in individuals:
            cp = int(offer["cp"])
            if cp not in proven:
                continue
            base = f"{term}&cp{cp}&{dex}"
            if await _exact_count(phone, base) != 1:
                continue
            form = offer.get("form_token")
            if form and (await _exact_count(phone, f"{base}&{form}") != 1
                         or await _exact_count(phone, f"{base}&!{form}") != 0):
                continue
            record["individuals"].append({k:offer.get(k) for k in ("dex","cp","form","query","observed_at")})
        save_stock(folder, handset, account, stock)
    return len(record["individuals"])


async def prepare_phone(phone, *, labels=None):
    """Explicit, restartable handset setup. Ender never enters this path."""
    from . import collector_day, pokemon_fleet
    cfg = collect.load_config()
    clear_path = config_paths.default_config(cfg["clear_config"])
    numbers = collector_day.species_catalog(clear_path)
    catalog = {species: dex for dex, species in numbers.items()}
    handset = getattr(phone, "transport_name", phone.name)
    account = trading_accounts.account_for(handset)
    folder = config_paths.state_dir() / "storage-favorites" / handset
    receipt = json.loads((folder / "verified-favorites.json").read_text())
    if receipt["device"] != handset or receipt["account"] != account:
        raise ValueError("Favorites belong to a different handset")
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    reserve = max(2, int(fleet.devices[account].config.get("trade_keep_last", 0)))
    priority = set(cfg.get("collect_all_dex", ())) if account != cfg["receiver"] else set()
    def keep(dex):
        return 0 if dex in priority else reserve
    prepared = read_stock(folder, handset, account)
    default = ("TradeReturns",) if account == cfg["receiver"] else tuple(
        trade_favorite_names.label(role) for role in (trade_favorite_names.LUCKY, trade_favorite_names.HUNDO)
    )
    for label in labels or (*default, *(n for n in ("PeerTrades",) if n in receipt["favorites"])):
        row = receipt["favorites"][label]
        term = row["term"]
        record = prepared.get(label, {})
        if record.get("receipt") != row["verified_at"] or record.get("reserve", reserve) > reserve:
            total, indexed = await index_favorite(phone, label, term, catalog)
            record = {"receipt": row["verified_at"], "term": term, "reserve": reserve, "total": total,
                      "index": indexed, "proofs": {}, "pages": []}
            prepared[label] = record
            save_stock(folder, handset, account, prepared)
        if "index" not in record:
            continue
        index, proofs = record["index"], record["proofs"]
        eligible = {n for page in index for n, cps in page.items() if len(cps) > keep(int(n))}
        for number in sorted(eligible - proofs.keys(), key=int):
            cps = {cp for page in index for cp in page.get(number, ())}
            proofs[number] = await certify_cps(phone, number, numbers[int(number)], cps)
            save_stock(folder, handset, account, prepared)
        for page in index:
            ready = {}
            for number, cps in page.items():
                certified = Counter(cps) & Counter(proofs.get(number, ()))
                if sum(certified.values()) > keep(int(number)):
                    ready[number] = {"species": numbers[int(number)], "cps": list(certified.elements())}
            record["pages"].append(ready)
        del record["index"], record["proofs"]
        save_stock(folder, handset, account, prepared)
        print(f"[prepare] {handset}: {label}, {sum(map(len, record['pages']))} verified page choices", flush=True)
    return prepared


if __name__ == "__main__":
    result = prepare()
    for handset, record in result["handsets"].items():
        print(f"{handset}: {len(record['favorites'])} verified favorites prepared")
