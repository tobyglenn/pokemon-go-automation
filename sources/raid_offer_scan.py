"""Read-only legendary inventory and exact individual evidence for Lucky Friends proposals."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime

from . import (
    clear_pokemon,
    collect_trades as collect,
    config_paths,
    raid_gamedata,
    raid_inventory,
    raid_math,
    remote_target,
    special_trade,
    starter,
    trade_local,
    vision_ocr,
)

# Raid-relevant tradeable legendaries. Never mix a fusion or transformation into its base form.
SHORTLIST = (
    382,
    383,
    384,
    639,
    643,
    644,
    717,
    483,
    484,
    487,
    645,
    642,
    894,
    150,
    250,
    485,
    646,
    800,
    888,
    889,
    641,
)
EXCLUSIONS = (
    "legendary&2*&!98Plus&!background&!dynamax&!gigantamax" + special_trade.GIVE_FILTER
)
SPARES = (
    "legendary&#ToTrade&!98Plus&!background&!dynamax&!gigantamax"
    + special_trade.SPARE_FILTER
)

FORM_TOKENS = {
    "palkia_origin": "origin",
    "dialga_origin": "origin",
    "giratina_origin": "origin",
    "giratina_altered": "altered",
    "landorus_therian": "therian",
    "thundurus_therian": "therian",
    "tornadus_therian": "therian",
    "landorus_incarnate": "incarnate",
    "thundurus_incarnate": "incarnate",
    "tornadus_incarnate": "incarnate",
    "zacian_hero": "hero",
    "zamazenta_hero": "hero",
}


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as out:
        json.dump(value, out, indent=1)
        out.write("\n")
    os.replace(out.name, path)


def _boxes(boxes):
    return [vision_ocr.TextBox(b.text, b.x, b.y, b.width, b.height) for b in boxes]


def _inventory_matches(image, boxes, term):
    """Strip a detached search icon before checking the read-only storage field."""
    labels = [
        replace(box, text=re.sub(r"^[Qq]\s+", "", box.text))
        if box.y < 0.35 * image.height
        else box
        for box in boxes
    ]
    return collect._filtered_grid(image, labels, term)


async def inventory_grid(phone, term):
    for attempt in range(2):
        try:
            image, boxes = await phone.read()
            if not starter.storage_open(boxes) or attempt:
                await starter.open_storage(phone)
                await phone.still()
                image, boxes = await phone.read()
            await collect._use_query(phone, term, image, boxes)
            _, image, boxes = await trade_local.settle(
                phone, lambda i, b: _inventory_matches(i, b, term), 12
            )
            count = clear_pokemon.result_count(boxes, image.height)
            if count == 0:
                await asyncio.sleep(1.2)
                image, boxes = await phone.read()
                count = clear_pokemon.result_count(boxes, image.height)
            if count is None or not _inventory_matches(image, boxes, term):
                raise starter.StepFailed("ambiguous inventory count")
            return count, image, boxes
        except starter.StepFailed:
            if attempt:
                raise


def _items(image, boxes):
    return [
        (
            collect._tile_species_label(image, boxes, tile),
            int(re.search(r"\d+", tile.text).group()),
        )
        for tile in clear_pokemon.safe_grid_tiles(image, boxes)
    ]


def _hundo_levels(data, forms, cps):
    return sorted(
        {
            level
            for cp in cps
            for form in forms
            for level in data.levels
            if raid_math.cp_of(form["stats"], (15, 15, 15), data.cpm(level)) == cp
        }
    )


async def read_coverage(phone, dex, form_token=None):
    data = raid_gamedata.load()
    forms = [form for form in data.pokemon.values() if form["dex"] == dex]
    if form_token:
        forms = [form for form in forms if FORM_TOKENS.get(form["id"]) == form_token]
        if len(forms) != 1:
            raise ValueError(f"Unknown or ambiguous form for #{dex} {form_token}")
    # Long, equivalent leading filters make clipped search text recognizable on every phone.
    suffix = f"{form_token}&{dex}" if form_token else str(dex)
    stock, image, boxes = await inventory_grid(phone, f"legendary&!shadow&{suffix}")
    hundo, image, boxes = await inventory_grid(phone, f"legendary&!shadow&4*&{suffix}")
    hundo_cps = [cp for _, cp in _items(image, boxes)]
    levels = _hundo_levels(data, forms, hundo_cps)
    count_hi, image, boxes = await inventory_grid(
        phone, f"legendary&!shadow&#98Plus&{suffix}"
    )
    quality_cps = [cp for _, cp in _items(image, boxes)]
    # 98Plus is an operator-maintained high-IV tag. It can contain hundos and
    # a broader set of good individuals. Only an independently matched 4* CP
    # proves a perfect. Infer conservative level bounds for visible high CPs.
    best = max(hundo_cps + quality_cps, default=None)
    high_floor = None
    if best:
        high_floor = min(
            (
                lv
                for form in forms
                for lv in data.levels
                if raid_math.cp_of(form["stats"], (15, 15, 15), data.cpm(lv)) >= best
            ),
            default=None,
        )
    return {
        "owned": stock,
        "hundo_count": hundo,
        "hundo_cps": hundo_cps,
        "maxed_hundo": any(
            all(lv >= 50 for lv in matches)
            for cp in hundo_cps
            if (matches := _hundo_levels(data, forms, [cp]))
        ),
        "hundo_levels_complete": len(hundo_cps) == hundo,
        "hundo_levels": levels,
        "high_iv_tag_count": count_hi,
        "best_high_iv_cp": best,
        "best_high_iv_level": high_floor,
        "observed_at": _now(),
    }


def learned_moves(data, screens):
    """Recognize learned raid attacks and exclude the Meteorite teaching button."""
    from .lucky_raid_offers import ENCOUNTER_MOVES

    learned = []
    for image, boxes in screens:
        for name in raid_inventory.parse_moves(boxes, image.width):
            move = data.move_by_name(name)
            if move is None or name in learned:
                continue
            if move.key in ENCOUNTER_MOVES:
                rows = [
                    b
                    for b in boxes
                    if b.text.strip().casefold() == name.strip().casefold()
                    and b.cx < image.width * 0.60
                ]
                if not any(
                    abs(row.cy - power.cy) < max(row.h, power.h)
                    for row in rows
                    for power in boxes
                    if power.cx > image.width * 0.68
                    and re.fullmatch(r"\d{1,3}", power.text.strip())
                ):
                    continue
            learned.append(name)
    return learned


class _UnsupportedFormToken(Exception):
    """Raised when Pokémon GO's search bar does not recognize a multi-form token."""


def _has_verifiable_form(dex: int) -> bool:
    data = raid_gamedata.load()
    forms = [p["id"] for p in data.pokemon.values() if p["dex"] == dex]
    if len(forms) <= 1:
        return True
    return any(FORM_TOKENS.get(form) for form in forms)


async def read_exact(phone, dex, cp, *, eligible_count=None, pool=EXCLUSIONS):
    from scripts import tag_spare_legendaries as appraisals
    from . import lucky_raid_offers as ranking

    base = f"{pool}&{dex}"
    if eligible_count is None:
        eligible_count, _, _ = await inventory_grid(phone, base)
    query = f"{pool}&cp{cp}&{dex}"
    count, image, boxes = await inventory_grid(phone, query)
    items = _items(image, boxes)
    if count != 1 or len(items) != 1 or items[0][1] != cp:
        return None
    tagger = appraisals.Tagger(phone, dry_run=True)
    image, boxes = await tagger.open_first(image, boxes)
    await phone.still()
    image, boxes = await phone.read()
    cp_text = [
        int(d.group(1))
        for b in boxes
        if (d := re.fullmatch(r"cp\s*(\d+)", trade_local.norm(b.text)))
    ]
    if (cp_text and cp_text != [cp]) or special_trade.is_armored_mewtwo_detail(
        cp, boxes
    ):
        return None
    initial_shot = str(phone.save(image, "legendary-detail"))
    screens = [(image, _boxes(boxes))]
    moves_shot = None
    anchor = raid_inventory.scroll_anchor(_boxes(boxes), image)
    if anchor is not None and not raid_inventory.parse_moves(
        _boxes(boxes), image.width
    ):
        end = raid_inventory.scroll_target(anchor)
        await phone.swipe(
            [round(image.width * anchor[0]), round(image.height * anchor[1])],
            [round(image.width * end[0]), round(image.height * end[1])],
            image,
            scroll=True,
        )
        image, boxes = await phone.read()
        screens.append((image, _boxes(boxes)))
        moves_shot = str(phone.save(image, "legendary-moves"))
    readings = None
    appraisal_shot = None
    with contextlib.suppress(starter.StepFailed):
        image, boxes = await tagger.open_menu("appraise")
        # Advance only the leader's introduction. The menu itself may still be
        # visible on a slow phone; never tap through a menu generically.
        for _ in range(3):
            words = {trade_local.norm(b.text) for b in boxes}
            if {"attack", "defense"} <= words:
                await asyncio.sleep(0.7)
                image, boxes = await phone.read()
                readings = appraisals.appraisal_readings(image, boxes)
                appraisal_shot = str(phone.save(image, "legendary-appraisal"))
                break
            if {"appraise", "transfer"} <= words:
                break
            # POWER UP is still visible behind the appraisal leader on Android.
            # Only the leader's actual introduction authorizes the speech tap.
            if not any(
                fragment in " ".join(words)
                for fragment in (
                    "take a look",
                    "appraise your",
                    "analysis",
                    "analyze",
                    "evaluate",
                )
            ):
                break
            await phone.tap([image.width // 2, round(0.90 * image.height)], image)
            await asyncio.sleep(1)
            image, boxes = await phone.read()
    detail = raid_inventory.parse_detail_page(screens)
    if detail.get("hp") is None:
        return None
    data = raid_gamedata.load()
    detail["moves"] = learned_moves(data, screens)
    all_forms = [p for p in data.pokemon.values() if p["dex"] == dex]
    levels = raid_math.levels_for_powerup(
        data, dex, detail.get("dust_cost"), detail.get("candy_cost")
    ).get("normal")
    fits = raid_math.solve(
        data,
        all_forms,
        cp,
        detail.get("hp"),
        levels=levels,
        min_iv_sum=30 if pool == EXCLUSIONS else 0,
        max_iv_sum=36 if pool == EXCLUSIONS else 44,
    )
    if not cp_text and not any(
        trade_local.norm(detail["name"])
        in {trade_local.norm(p["search"]), trade_local.norm(p["name"])}
        for p in all_forms
    ):
        return None
    if readings:
        # The rounded cap extends beyond the integer IV tick. Keep every fit
        # within the calibrated bar tolerance; ambiguous bars are not exact IVs.
        partial = [
            fit
            for fit in fits
            if all(
                readings.get(key) is None or -0.25 <= readings[key] - iv <= 0.8
                for key, iv in zip(("attack", "defense", "hp"), fit.ivs)
            )
        ]
        if partial:
            fits = partial
    # Close appraisal only if the bars were displayed, then return through
    # the recognized detail page. Do not leave the next query under a leader.
    if appraisal_shot:
        await phone.tap([image.width // 2, round(0.93 * image.height)], image)
        await asyncio.sleep(0.8)
    ids = {fit.form for fit in fits}
    if len(ids) != 1:
        return None
    form = ids.pop()
    if any(
        k in form
        for k in (
            "armored",
            "crowned",
            "dawn_wings",
            "dusk_mane",
            "kyurem_white",
            "kyurem_black",
        )
    ):
        return None
    form_token = None
    if len(all_forms) > 1:
        form_token = FORM_TOKENS.get(form)
        if not form_token:
            return None
        # Check both halves. An unsupported or ignored form token must never
        # authorize a proposal that accidentally selects another form.
        query = f"{pool}&cp{cp}&{form_token}&{dex}"
        yes, _, _ = await inventory_grid(phone, query)
        no, _, _ = await inventory_grid(phone, f"{pool}&cp{cp}&!{form_token}&{dex}")
        if yes != 1 or no != 0:
            raise _UnsupportedFormToken(f"{dex}:{form_token}")
        eligible_count, _, _ = await inventory_grid(phone, f"{pool}&{form_token}&{dex}")
    possible_ivs = {fit.ivs for fit in fits}
    form_record = data.pokemon[form]
    return {
        "dex": dex,
        "name": form_record["search"].title(),
        "form": form,
        "form_name": form_record["name"],
        "cp": cp,
        "query": query,
        "form_token": form_token,
        "form_verified": True,
        "eligible_count": eligible_count,
        "unique_count": count,
        "live_verified": True,
        "hp": detail.get("hp"),
        "ivs": list(next(iter(possible_ivs))) if len(possible_ivs) == 1 else None,
        "levels": sorted({fit.level for fit in fits}),
        "iv_band": "2-star" if pool == EXCLUSIONS else "ToTrade spare; non-perfect",
        "moves": detail.get("moves", []),
        "raid_role": ranking.role_for(form, detail.get("moves", [])),
        "detail_shot": initial_shot,
        "moves_shot": moves_shot,
        "appraisal_shot": appraisal_shot,
        "observed_at": _now(),
    }


async def _account(name, owner, machines, fleet, shots, path, shortlist, epoch):
    from . import lucky_raid_offers as ranking

    state = json.loads(path.read_text()) if path.is_file() else {}
    if state.get("epoch", 0) != epoch:
        state = {}
    state["epoch"] = epoch
    state.setdefault("offers", [])
    state.setdefault("coverage", {})
    state.setdefault("checked", {})
    date = datetime.now().astimezone()
    async with remote_target.open_fleet_phone(
        name, shots, machine=owner, machines=machines, fleet=fleet
    ) as phone:
        for dex in shortlist:
            key = str(dex)
            if ranking._fresh(state["checked"].get(key), date):
                continue
            try:
                state["coverage"][key] = await read_coverage(phone, dex)
                offers = {}
                count = 0
                for pool in (EXCLUSIONS, SPARES):
                    eligible, image, boxes = await inventory_grid(
                        phone, f"{pool}&{dex}"
                    )
                    count += eligible
                    seen = [cp for _, cp in _items(image, boxes)]
                    for previous in state["offers"]:
                        if (
                            previous["dex"] == dex
                            and previous["query"].startswith(pool + "&")
                            and ranking._fresh(previous.get("observed_at"), date)
                            and seen.count(previous["cp"]) == 1
                        ):
                            offers[previous["query"]] = previous
                    if not _has_verifiable_form(dex):
                        continue
                    pool_has_fresh = any(q.startswith(pool + "&") for q in offers)
                    if pool_has_fresh:
                        continue
                    for cp in dict.fromkeys(seen):
                        if seen.count(cp) != 1:
                            continue
                        try:
                            offer = await read_exact(
                                phone, dex, cp, eligible_count=eligible, pool=pool
                            )
                        except _UnsupportedFormToken:
                            break
                        if offer:
                            offers[offer["query"]] = offer
                            break
                state["offers"] = [
                    offer for offer in state["offers"] if offer["dex"] != dex
                ] + list(offers.values())
                state["checked"][key] = _now()
                print(
                    f"[raid-offers] {name} #{dex}: {count} eligible, {len(offers)} verified offers",
                    flush=True,
                )
            except (starter.StepFailed, RuntimeError, ValueError) as exc:
                print(f"[raid-offers] {name} #{dex}: unread ({exc})", flush=True)
            _write(path, state)
    return state


async def _form_stock(name, state, form_offers, owner, machines, fleet, shots, path):
    from . import lucky_raid_offers as ranking

    now = datetime.now().astimezone()
    pending = [
        (dex, form, token)
        for dex, form, token in form_offers
        if not ranking._fresh(
            state.get("coverage", {})
            .get(str(dex), {})
            .get("forms", {})
            .get(form, {})
            .get("observed_at"),
            now,
        )
    ]
    if not pending:
        return
    async with remote_target.open_fleet_phone(
        name, shots, machine=owner, machines=machines, fleet=fleet
    ) as phone:
        for dex, form, token in pending:
            try:
                stock = await read_coverage(phone, dex, token)
            except (starter.StepFailed, RuntimeError, ValueError) as exc:
                print(f"[raid-offers] {name} {form}: unread ({exc})", flush=True)
                continue
            state.setdefault("coverage", {}).setdefault(str(dex), {}).setdefault(
                "forms", {}
            )[form] = stock
            _write(path, state)


def publish_accounts(dir_path, accounts, *, invalidate=(), changed_dex=()):
    """Publish under one lock. A scan started before an exchange cannot revive its old inventory."""
    target = dir_path / "raid-offers.json"
    dir_path.mkdir(parents=True, exist_ok=True)
    with (dir_path / "raid-offers.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        state = json.loads(target.read_text()) if target.is_file() else {}
        epochs = state.setdefault("epochs", {})
        devices = state.setdefault("devices", {})
        for name, account in accounts.items():
            if account.get("epoch", 0) == epochs.get(name, 0):
                devices[name] = account
        changed = set(map(str, changed_dex))
        for name in set(invalidate):
            epoch = epochs.get(name, 0)
            path = dir_path / "raid-offer-accounts" / f"{name}.json"
            account = devices.pop(name, {})
            if changed and path.is_file():
                checkpoint = json.loads(path.read_text())
                if checkpoint.get("epoch", 0) == epoch:
                    account = checkpoint
            epochs[name] = epoch + 1
            retained = {"epoch": epoch + 1, "offers": [], "coverage": {}, "checked": {}}
            if changed:
                retained["offers"] = [o for o in account.get("offers", ()) if str(o["dex"]) not in changed]
                for field in ("coverage", "checked"):
                    retained[field] = {k: v for k, v in account.get(field, {}).items() if k not in changed}
                devices[name] = retained
            _write(path, retained)
        _write(target, state)
        return state


async def refresh(names, owners, machines, fleet, shots, *, shortlist=SHORTLIST):
    from . import lucky_raid_offers as ranking

    dir_path = config_paths.state_dir() / "collect-trades"
    state = ranking.load_state()
    devices = state.setdefault("devices", {})
    now = datetime.now().astimezone()

    async def device(name):
        checkpoint = dir_path / "raid-offer-accounts" / f"{name}.json"
        current = devices.get(name, {})
        if all(
            ranking._fresh(current.get("checked", {}).get(str(dex)), now)
            for dex in shortlist
        ):
            return current
        if current and (not checkpoint.exists() or
                        json.loads(checkpoint.read_text()).get("epoch", 0) != current.get("epoch", 0)):
            _write(checkpoint, current)
        return await _account(
            name,
            owners[name],
            machines,
            fleet,
            shots,
            checkpoint,
            shortlist,
            state.get("epochs", {}).get(name, 0),
        )

    results = await asyncio.gather(
        *(device(name) for name in names), return_exceptions=True
    )
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            print(f"[raid-offers] {name}: could not refresh ({result})", flush=True)
            checkpoint = dir_path / "raid-offer-accounts" / f"{name}.json"
            if checkpoint.is_file():
                devices[name] = json.loads(checkpoint.read_text())
        else:
            devices[name] = result
    active = {name: devices[name] for name in names if name in devices}
    forms = {
        (offer["dex"], offer["form"], offer["form_token"])
        for account in active.values()
        for offer in account.get("offers", [])
        if offer.get("form_token") and ranking._fresh(offer.get("observed_at"), now)
    }
    form_results = await asyncio.gather(
        *(
            _form_stock(
                name,
                account,
                forms,
                owners[name],
                machines,
                fleet,
                shots,
                dir_path / "raid-offer-accounts" / f"{name}.json",
            )
            for name, account in active.items()
        ),
        return_exceptions=True,
    )
    for name, result in zip(active, form_results):
        if isinstance(result, Exception):
            print(
                f"[raid-offers] {name}: form coverage unavailable ({result})",
                flush=True,
            )
    return publish_accounts(dir_path, active)
