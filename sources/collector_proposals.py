"""Review one exact legendary swap for a Lucky Friends pair, or execute its one-use approval."""

from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from datetime import date

from . import collect_trades as collect, collector_day, pokemon_fleet, remote_target
from . import special_trade, starter, starter_trade, trade_approval, trade_local


class SpecialUnavailable(starter.StepFailed):
    pass


def pair_list(request, cfg, fleet):
    configured = [request] if request else cfg.get("guaranteed_proposal_pairs", [])
    if not configured:
        raise pokemon_fleet.FleetError("Specify two phones or configure guaranteed_proposal_pairs")
    pairs = []
    for pair in configured:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2 or pair[0] == pair[1]:
            raise pokemon_fleet.FleetError(f"Invalid guaranteed proposal pair: {pair!r}")
        unknown = [n for n in pair if n not in fleet.devices or not fleet.devices[n].enabled]
        if unknown:
            raise pokemon_fleet.FleetError(f"Unknown or disabled proposal phone(s): {', '.join(unknown)}")
        if not any(set(pair) == set(existing) for existing in pairs):
            pairs.append(tuple(pair))
    return pairs


def participants_for(pair, choice, preceding=()):
    details = {}
    for donor, recipient in (pair, pair[::-1]):
        offer = choice["give"][donor]
        if not offer.get("live_verified") or offer.get("unique_count") != 1:
            raise starter.StepFailed(f"{donor}: top raid offer has no uniquely verified live candidate")
        details[donor] = {
            **offer, "species": offer["name"], "stock": int(offer["eligible_count"]),
            # A participant's outgoing offer benefits the opposite recipient.
            "reason": choice["reasons"][recipient], "recipient_coverage": choice["coverage"][recipient],
        }
        if preceding:
            previous = ", ".join(" ↔ ".join(p) for p in preceding)
            details[donor]["reason"] = (
                f"Assuming {previous} completes first, ownership is projected. "
                f"Perfect counts are from the scan and incoming IVs are unknown. {details[donor]['reason']}"
            )
    return details


def identity_query(participant):
    dex, cp = int(participant["dex"]), int(participant["cp"])
    tokens = participant["query"].split("&")
    cp_token = f"cp{cp}"
    folded = [t.strip().casefold() for t in tokens]
    if dex < 1 or cp < 1 or str(dex) not in folded or folded.count(cp_token) != 1:
        raise starter.StepFailed("An exact Dex and CP query is required to select this proposal")
    if participant.get("form_token") and participant["form_token"].casefold() not in folded:
        raise starter.StepFailed("The verified form restriction is absent from this proposal's search")
    # Re-check older quotes against protected copies even when their original
    # inventory scan predates the keeper tag.
    if "!#" not in folded and "!#keepone" not in folded:
        tokens.append("!#KeepOne")
    return "&".join(tokens), "&".join(t for t in tokens if t.strip().casefold() != cp_token)


def favorite_choice(snapshot, fleet, name, offer):
    """Use a prepared handset favorite only if it enforces the quoted individual's eligibility."""
    from . import ender_inventory
    dex, cp = str(offer["dex"]), int(offer["cp"])
    _, stock_query = identity_query(offer)
    rules = [part for part in stock_query.split("&") if part != dex]
    handset = getattr(fleet, "transports", {}).get(name, name)
    favorites = ender_inventory.account_favorites(snapshot, name, handset)
    reserve = int(fleet.devices[name].config.get("trade_keep_last", 0))
    for label in ender_inventory.TRADE_FAVORITES:
        row = favorites.get(label, {})
        if offer.get("picker_receipt") and offer["picker_receipt"] != row.get("verified_at"):
            continue
        if label == "LuckyOffer":
            if ender_inventory.clauses(row.get("term","")) != ender_inventory.clauses(ender_inventory.lucky_offer_term()):
                continue
            if not any(all(item.get(k) == offer.get(k) for k in ("dex","cp","form","query","observed_at"))
                       for item in row.get("individuals", ())):
                continue
        elif not ender_inventory.covers(row.get("term",""), rules, int(dex)):
            continue
        for page, entries in enumerate(row.get("pages", ())):
            stock = entries.get(dex)
            if stock and stock["cps"].count(cp) == 1 and len(stock["cps"]) > reserve:
                cps = stock["cps"]
                count = row.get("stock_count")
                if offer.get("picker_favorite"):
                    from collections import Counter
                    proof = offer.get("picker_proof", {})
                    if (offer["picker_favorite"] != label or proof.get("term") != row["term"]
                            or proof.get("cp") != cp or proof.get("dex") != int(dex)
                            or type(proof.get("count")) is not int or proof["count"] < 1
                            or type(proof.get("page")) is not int or not 0 <= proof["page"] < proof["count"]
                            or not isinstance(proof.get("cps"), list)
                            or proof["cps"].count(cp) != 1 or len(proof["cps"]) <= reserve
                            or Counter(proof["cps"]) - Counter(cps)):
                        continue
                    cps, page, count = proof["cps"], proof["page"], proof["count"]
                return ender_inventory.Choice(label, dex, row["term"], stock["species"], tuple(cps), page, count)
    return None


def pair_key(pair):
    import json
    return json.dumps(sorted(pair), separators=(",", ":"))


def cached_pair_present(state, pair, fleet, snapshot):
    record = snapshot.get("raid_pairs", {}).get(pair_key(pair), {})
    participants = record.get("participants", {})
    if set(participants) != set(pair):
        return False
    for name, offer in participants.items():
        if record["epochs"].get(name) != state.get("epochs", {}).get(name, 0):
            return False
        if offer.get("picker_proof", {}).get("partner") != next(n for n in pair if n != name):
            return False
        if not favorite_choice(snapshot, fleet, name, offer):
            return False
        if not any(_individual(other) == _individual(offer) and other.get("observed_at") == offer["observed_at"]
                   for other in state.get("devices", {}).get(name, {}).get("offers", ())):
            return False
    return True


async def prepare_favorite_pairs(cfg, fleet, machines, owners, names, shots):
    """Explicit setup only: check caps, badges and special allowances before preparing a reciprocal quote."""
    import fcntl
    import json
    from datetime import datetime
    from . import config_paths, ender_inventory as inv, lucky_raid_offers, raid_offer_scan
    configured = cfg.get("guaranteed_proposal_pairs", ())
    snapshot = inv.load()
    pairs = [pair for pair in pair_list(None, cfg, fleet) if set(pair) <= set(names)] if configured else []
    def sources(name):
        handset = getattr(fleet, "transports", {}).get(name, name)
        favorites = inv.account_favorites(snapshot, name, handset)
        return inv.clauses(favorites.get("LuckyOffer", {}).get("term","")) == inv.clauses(inv.lucky_offer_term())
    supported = {name: sources(name) for pair in pairs for name in pair}
    checked = [pair for pair in pairs if all(supported[n] for n in pair)]
    for pair in pairs:
        if pair not in checked:
            print(f"[prepare] {pair}: no matching saved Legendary favorite on both phones", flush=True)
    args = collect.parse_args([])
    args.shots = shots
    day = date.today()
    eligible = await eligible_pairs(checked, args, fleet, machines, owners, day) if checked else []
    if not eligible or date.today() != day:
        return 0
    existing = lucky_raid_offers.load_state()
    if all(snapshot.get("raid_pairs", {}).get(pair_key(pair), {}).get("shortlist") == list(raid_offer_scan.SHORTLIST)
           and cached_pair_present(existing, pair, fleet, snapshot) for pair in eligible):
        usable = await unspent_preparations(eligible, snapshot, machines, day)
        if len(usable) == len(eligible):
            print(f"[prepare] Reusing {len(usable)} verified Lucky Friends proposal(s).", flush=True)
            return len(usable)
    replacements = {}
    if date.today() == day:
        eligible_names = list(dict.fromkeys(n for pair in eligible for n in pair))
        catalog = collector_day.species_catalog(config_paths.default_config(cfg["clear_config"]))
        async def available(name):
            async with remote_target.open_fleet_phone(name, shots, machine=owners[name], machines=machines, fleet=fleet) as phone:
                numbers = await inv.eligible_legendary_species(phone, raid_offer_scan.SHORTLIST, catalog)
                print(f"[prepare] {name}: {len(numbers)} raid shortlist species in LuckyOffer", flush=True)
                return numbers
        present = dict(zip(eligible_names, await asyncio.gather(*(available(n) for n in eligible_names))))
        viable = [pair for pair in eligible if all(present[n] for n in pair)]
        active_names = list(dict.fromkeys(n for pair in viable for n in pair))
        shortlist = tuple(d for d in raid_offer_scan.SHORTLIST if any(d in present[n] for n in active_names))
        state = (await raid_offer_scan.refresh(active_names, owners, machines, fleet, shots, shortlist=shortlist)
                 if shortlist else {"devices": {}})
        now = datetime.now().astimezone()
        checked = {name: {dex for dex in shortlist if lucky_raid_offers._fresh(
            state.get("devices", {}).get(name, {}).get("checked", {}).get(str(dex)), now)}
            for name in active_names}
        ranked = {name: {} for name in eligible_names}
        for pair in viable:
            for swap in _eligible_swaps(pair, state, fleet):
                for name, offer in swap["give"].items():
                    if offer["dex"] in present[name]:
                        ranked[name][_individual(offer)] = offer

        async def certify(name):
            offers = ranked[name].values()
            if not offers:
                return
            reserve = int(fleet.devices[name].config.get("trade_keep_last", 0))
            async with remote_target.open_fleet_phone(name, shots, machine=owners[name], machines=machines, fleet=fleet) as phone:
                count = await inv.prepare_legendary(phone, offers, reserve)
                print(f"[prepare] {name}: {count} verified LuckyOffer individuals", flush=True)
        await asyncio.gather(*(certify(name) for name in eligible_names))
        snapshot = inv.prepare()
        ranked_state = deepcopy(state)
        for name in eligible_names:
            record = ranked_state["devices"].get(name, {})
            record["offers"] = [o for o in record.get("offers", ()) if _individual(o) in ranked[name] and favorite_choice(snapshot, fleet, name, o)]
        picker_options = {}
        for pair in viable:
            options = {n: {_individual(o): favorite_choice(snapshot, fleet, n, o)
                           for o in ranked_state["devices"].get(n, {}).get("offers", ())} for n in pair}
            if not all(options.values()):
                continue
            counts = await asyncio.to_thread(collector_day.day_counts, pair, machines, day)
            if (date.today() != day or any(counts[n] >= args.trades for n in pair)
                    or tuple(sorted(pair)) in getattr(counts, "capped_pairs", ())):
                continue
            async def locate(phones):
                async def one(n):
                    keep = int(fleet.devices[n].config.get("trade_keep_last", 0))
                    return await inv.prepare_picker_options(phones[n], options[n], keep)
                return dict(zip(pair, await trade_local.both(*(one(n) for n in pair))))
            try:
                picker_options[pair] = await check_pair(pair, args, fleet, machines, owners, observe=locate)
            except (RuntimeError, OSError) as exc:
                print(f"[prepare] {pair}: could not verify the native picker: {exc}", flush=True)
        planned, conflicts = proposal_itinerary(list(picker_options), ranked_state, fleet, picker_options=picker_options)
        for pair, participants in planned.items():
            timestamps = []
            for n, offer in participants.items():
                timestamps.append(offer["observed_at"])
                for account in pair:
                    coverage = state["devices"][account]["coverage"][str(offer["dex"])]
                    if offer.get("form_token"):
                        coverage = coverage["forms"][offer["form"]]
                    timestamps.append(coverage["observed_at"])
            observed = min(timestamps, key=lambda value: datetime.fromisoformat(value).timestamp())
            for name, p in participants.items():
                proof = picker_options[pair][name][_individual(p)]
                p["picker_favorite"] = "LuckyOffer"
                p["picker_proof"] = dict(proof, partner=next(n for n in pair if n != name))
                handset = getattr(fleet, "transports", {}).get(name, name)
                favorites = inv.account_favorites(snapshot, name, handset)
                p["picker_receipt"] = favorites[p["picker_favorite"]]["verified_at"]
                p["reason"] += f" Recipient coverage and ranking prepared on {day}."
            relevant = present[pair[0]] | present[pair[1]]
            completed = relevant & checked[pair[0]] & checked[pair[1]]
            if completed != relevant:
                for participant in participants.values():
                    participant["reason"] += f" Checked both accounts for {len(completed)} of {len(relevant)} available shortlist species."
            replacements[pair_key(pair)] = {
                "participants": participants, "seen_since": observed,
                "shortlist": list(raid_offer_scan.SHORTLIST) if completed == relevant else None,
                "eligible_species": sorted(relevant), "checked_species": sorted(completed),
                "epochs": {n:state.get("epochs", {}).get(n,0) for n in pair},
                "prepared_at": datetime.now().astimezone().isoformat()}
    dest = inv.path().with_name("ender-raid-pairs.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = json.loads(dest.read_text()) if dest.is_file() else {}
        for pair in eligible:
            records.pop(pair_key(pair), None)
        inv.save(dest, {**records, **replacements})
    return len(replacements)


async def unspent_preparations(pairs, snapshot, machines, day):
    from datetime import datetime
    from . import trading_accounts
    rows = snapshot.get("raid_pairs", {})
    since = min(datetime.fromisoformat(rows[pair_key(p)]["seen_since"]).date() for p in pairs)
    events = await asyncio.to_thread(collector_day.history_records, machines, day, since=since)
    usable = []
    for pair in pairs:
        start = datetime.fromisoformat(rows[pair_key(pair)]["seen_since"]).astimezone()
        relevant = {p["dex"] for p in rows[pair_key(pair)]["participants"].values()}
        for event in events:
            names = event.get("pair", ())
            if not set(pair).intersection(trading_accounts.account_for(n) for n in names):
                continue
            if datetime.fromisoformat(event["at"]).astimezone() < start:
                continue
            dex = event.get("offered_dex", {})
            if any(n not in dex for n in names) or relevant.intersection(dex.values()):
                break
        else:
            usable.append(pair)
    return usable


def _individual(offer):
    return int(offer["dex"]), offer.get("form"), int(offer["cp"])


def _after_swap(state, pair, swap):
    """Project ownership and remove the spent original. Received Pokémon cannot be traded again."""
    state = deepcopy(state)
    for donor, recipient in (pair, pair[::-1]):
        offer = swap["give"][donor]
        dex, form, _ = _individual(offer)
        device = state["devices"][donor]
        _, stock_query = identity_query(offer)
        device["offers"] = [other for other in device["offers"] if _individual(other) != _individual(offer)]
        for other in device["offers"]:
            if identity_query(other)[1] == stock_query:
                other["eligible_count"] -= 1
        for name, delta in ((donor, -1), (recipient, 1)):
            row = state["devices"][name]["coverage"].get(str(dex), {})
            for coverage in (row, row.get("forms", {}).get(form, {})):
                if coverage.get("owned") is not None:
                    coverage["owned"] += delta
    return state


def _eligible_swaps(pair, state, fleet, *, for_refresh=False):
    from . import lucky_raid_offers
    return [swap for swap in lucky_raid_offers.rank_swaps(state, *pair, for_refresh=for_refresh)
            if all(swap["give"][n]["eligible_count"] > int(fleet.devices[n].config.get("trade_keep_last", 0))
                   for n in pair)]


def _picker_swaps(pair, state, fleet, picker_options):
    swaps = _eligible_swaps(pair, state, fleet)
    if picker_options is None:
        return swaps
    return [swap for swap in swaps if all(_individual(swap["give"][n]) in picker_options[pair][n] for n in pair)]


def _best_sequence(pairs, state, fleet, initial, picker_options=None):
    """Optimize total reciprocal value in configured order, with the weaker swap breaking ties."""
    best = None
    best_value = None

    def visit(index, inventory, choices):
        nonlocal best, best_value
        pair = pairs[index]
        options = initial[pair] if index == 0 else _picker_swaps(pair, inventory, fleet, picker_options)
        if index == len(pairs) - 1:
            if options:
                sequence = choices + [options[0]]
                scores = [option["score"] for option in sequence]
                value = (sum(scores), min(scores))
                if best_value is None or value > best_value:
                    best, best_value = sequence, value
            return
        for option in options:
            visit(index + 1, _after_swap(inventory, pair, option), choices + [option])

    visit(0, state, [])
    return best


def proposal_itinerary(pairs, state, fleet, *, picker_options=None):
    """Choose an executable sequence independently for each set of overlapping pairs."""
    initial = {p: _picker_swaps(p, state, fleet, picker_options) for p in pairs}
    details, conflicts = {}, []
    pending = [p for p in pairs if initial[p]]
    while pending:
        names = set(pending[0])
        while True:
            connected = [p for p in pending if names.intersection(p)]
            expanded = names.union(*connected)
            if expanded == names:
                break
            names = expanded
        pending = [p for p in pending if p not in connected]
        if len(connected) == 1:
            pair = connected[0]
            details[pair] = participants_for(pair, initial[pair][0])
            continue
        sequence = _best_sequence(connected, state, fleet, initial, picker_options)
        if sequence is None:
            conflicts.append(connected)
            for pair in connected:
                details[pair] = participants_for(pair, initial[pair][0])
            continue
        for index, (pair, swap) in enumerate(zip(connected, sequence)):
            preceding = [p for p in connected[:index] if set(p).intersection(pair)]
            details[pair] = participants_for(pair, swap, preceding)
    return details, conflicts


async def prepare_individual(phone, participant, reserve):
    """Verify current eligible stock and an exact individual outside the timed lobby."""
    exact, stock_query = identity_query(participant)
    await trade_local.leave_previous_trade(phone)
    await starter.open_storage(phone)
    await phone.still()
    stock = await collect.choose_filtered_tile(phone, stock_query, "legendary stock", inspect_labels=[])
    if stock <= reserve:
        raise trade_local.TradeRefused(f"{phone.name}: {stock} eligible, configured reserve {reserve}")
    image, boxes = await phone.read()
    info = {}
    await collect.prepare_saved_query(phone, exact, image, boxes, observed=info, min_count=1, max_count=1)
    if info.get("count") != 1:
        raise trade_local.TradeRefused(
            f"{phone.name}: {participant['species']} CP {participant['cp']} has {info.get('count')} eligible exact matches")
    participant["stock"] = stock
    participant["unique"] = True


async def special_allowance(phone):
    image, boxes = await phone.read()
    left = special_trade.special_left(boxes)
    if left is None:
        active = collect._active_picker_filter_box(image, boxes)
        if active:
            await phone.tap([int(image.width * trade_local.PICKER_CLEAR_X), active.center_y], image)
        ok, image, boxes = await trade_local.settle(phone, lambda _i, b: special_trade.special_left(b) is not None, 4)
        if not ok:
            from . import trade_saved_search as saved
            if any(saved.heading(boxes, title) for title in ("favorite", "see more", "see less", "recent")):
                value = await saved.editor_text(phone)
                if value is not None:
                    if value.strip() and value.strip().casefold() != "search":
                        if phone.ios:
                            await trade_local._clear_ios_native_text(phone)
                        else:
                            await trade_local.clear_field(phone, len(value) + 10)
                    cleared = await saved.editor_text(phone)
                    if cleared is not None and cleared.strip().casefold() in ("", "search"):
                        await trade_local.submit(phone)
                        ok, image, boxes = await trade_local.settle(phone, lambda _i, b: special_trade.special_left(b) is not None, 4)
                        if not ok and saved.heading(boxes, "favorite"):
                            field = collect._picker_search_field(image, boxes)
                            if field is not None:
                                await phone.tap([round(image.width * .095), field.center_y], image)
                                _, image, boxes = await trade_local.settle(phone, lambda _i, b: special_trade.special_left(b) is not None, 5)
        left = special_trade.special_left(boxes)
    if left == 0:
        raise SpecialUnavailable(f"{phone.name}: no Special Trades remain today; defer this pair")
    if left is None:
        raise starter.StepFailed(f"{phone.name}: could not read the remaining Special Trade allowance ({phone.save(image, 'special-allowance-unverified')})")
    print(f"[guaranteed] {phone.name}: {left} Special Trades remaining in the picker", flush=True)
    return left


async def select_individual(phone, participant, *, reselect=False, reserve=0):
    participant["special_remaining"] = await special_allowance(phone)
    term, stock_query = identity_query(participant)
    if reselect:
        # The shared phone may have spent its remaining Special Trade since this
        # quote. Read the actual allowance before retrying any inventory lookup.
        stock = await collect.choose_filtered_tile(phone, stock_query, "approved legendary stock", inspect_labels=[])
        if stock <= reserve:
            raise trade_local.TradeRefused(f"{phone.name}: {stock} eligible, configured reserve {reserve}")
        participant["stock"] = stock
    count = await collect.choose_filtered_tile(phone, term, f"{participant['species']} CP {participant['cp']}")
    if count != 1:
        raise trade_local.TradeRefused(f"{phone.name}: the proposed individual is no longer the unique picker match")
    participant["unique"] = True


async def review_pair(pair, participants, args, fleet, machines, owners, token=None, *, live_prompt=False):
    for n in pair:
        if n not in owners:
            raise pokemon_fleet.FleetError(f"{n}: offline; cannot verify this pair")
    async with contextlib.AsyncExitStack() as stack:
        phones = {}
        for n in sorted(pair):
            phones[n] = await stack.enter_async_context(remote_target.open_fleet_phone(
                n, args.shots, machine=owners[n], machines=machines, fleet=fleet))
        counts = await asyncio.to_thread(collector_day.day_counts, pair, machines, date.today())
        if tuple(sorted(pair)) in getattr(counts, "capped_pairs", ()) or any(counts[n] >= args.trades for n in pair):
            raise trade_local.TradeOver(f"{pair}: a participant already reached the selected daily cap of {args.trades}")
        reserves = {n: int(fleet.devices[n].config.get("trade_keep_last", 0)) for n in pair}
        choices = {}
        prepared = getattr(args, "prepared_only", False) or any("picker_favorite" in p for p in participants.values())
        if prepared:
            from . import ender_inventory
            snapshot = ender_inventory.load()
            for n in pair:
                choices[n] = favorite_choice(snapshot, fleet, n, participants[n])
                if not choices[n] or participants[n].get("picker_favorite", choices[n].name) != choices[n].name:
                    raise starter.StepFailed(f"{n}: quoted Pokémon is missing from its prepared favorite")
                participants[n]["picker_favorite"] = choices[n].name
        elif token is None:
            await trade_local.both(*(prepare_individual(phones[n], participants[n], reserves[n]) for n in pair))
        trainers = dict(zip(pair, await trade_local.both(*(trade_local.trainer_name(phones[n]) for n in pair))))
        for n in pair:
            if token and (trade_local.trainer_identity(participants[n]["trainer"])
                          != trade_local.trainer_identity(trainers[n])):
                raise trade_approval.ApprovalRequired(f"{n}: the signed-in trainer differs from the approved proposal")
        # trade_once verifies both named Lucky Friend profiles immediately
        # before entering the lobby. Finish storage work before this one visit.

        async def pick(phone):
            if prepared:
                n = phone.name
                participants[n]["special_remaining"] = await special_allowance(phone)
                participants[n]["stock"] = await ender_inventory.pick(
                    phone, choices[n], keep=reserves[n], exact_cp=int(participants[n]["cp"]))
                participants[n]["unique"] = True
                return
            await select_individual(phone, participants[phone.name], reselect=token is not None,
                                    reserve=reserves[phone.name])

        def record(receipts):
            from . import lucky_raid_offers
            starter_trade.record_trade(pair, {n: participants[n]["species"] for n in pair},
                                      "legendary-raid", "approved-guaranteed",
                                      offered_dex={n:int(participants[n]["dex"]) for n in pair}, receipts=receipts)
            lucky_raid_offers.invalidate_accounts(pair, changed_dex={int(participants[n]["dex"]) for n in pair})
        return await trade_local.trade_once(
            *(phones[n] for n in pair), trainers, {n:participants[n]["query"] for n in pair},
            {n:participants[n]["species"] for n in pair}, choosers=dict.fromkeys(pair, pick),
            require_guaranteed_lucky=True, guaranteed_details=participants, propose_special=True,
            propose_only=token is None and not live_prompt, reselect_approval_id=token,
            on_confirmed=record, daily_cap=args.trades,
        )


async def check_pair(pair, args, fleet, machines, owners, *, observe=None):
    """Check both named profiles and both live allowances before inventory discovery."""
    async with contextlib.AsyncExitStack() as stack:
        phones = {}
        for n in sorted(pair):
            phones[n] = await stack.enter_async_context(remote_target.open_fleet_phone(
                n, args.shots, machine=owners[n], machines=machines, fleet=fleet))
        await asyncio.to_thread(trade_local._check_trainer_pending, pair)
        trainers = dict(zip(pair, await trade_local.both(*(trade_local.trainer_name(phones[n]) for n in pair))))
        if not await collector_day.guaranteed_profiles(pair, phones, trainers):
            raise trade_local.LuckyRequired(f"{pair}: both Lucky Friends badges were not verified")
        async with trade_local.check_trade_lobby(*(phones[n] for n in pair)):
            await trade_local.both(*(special_allowance(phones[n]) for n in pair))
            if observe is not None:
                return await observe(phones)


async def eligible_pairs(pairs, args, fleet, machines, owners, day):
    names = {n for pair in pairs for n in pair}
    counts = await asyncio.to_thread(collector_day.day_counts, names, machines, day)
    capped = {n for n in names if counts[n] >= args.trades}
    no_specials = set()
    eligible = []
    for pair in pairs:
        try:
            if date.today() != day:
                break
            if capped.intersection(pair) or tuple(sorted(pair)) in getattr(counts, "capped_pairs", ()):
                raise starter.StepFailed("daily trade allowance exhausted")
            if not no_specials.isdisjoint(pair):
                raise starter.StepFailed("a participant has no Special Trades remaining")
            if any(n not in owners for n in pair):
                raise starter.StepFailed("a named phone is offline")
            await check_pair(pair, args, fleet, machines, owners)
        except SpecialUnavailable as exc:
            no_specials.update(n for n in pair if str(exc).startswith(f"{n}:"))
            print(f"[guaranteed] Skipping {pair}: {exc}", flush=True)
        except (RuntimeError, OSError) as exc:
            print(f"[guaranteed] Skipping {pair}: {exc}", flush=True)
        else:
            eligible.append(pair)
    return [pair for pair in eligible if no_specials.isdisjoint(pair)]


async def run(args, cfg, fleet, machines, owners, approved=None, *, held_pair=None, on_exchange=None):
    requested = list(approved["participants"]) if approved else held_pair or args.propose_guaranteed
    pairs = pair_list(requested, cfg, fleet)
    incomplete = False
    day = date.today()
    state = None
    if not approved:
        from . import lucky_raid_offers
        if getattr(args, "prepared_only", False):
            from . import ender_inventory
            snapshot = ender_inventory.load()
            state = lucky_raid_offers.load_state()
            pairs = [p for p in pairs if cached_pair_present(state, p, fleet, snapshot)]
            if not pairs:
                print("[guaranteed] No reciprocal prepared favorite choices. Skipping this pair.", flush=True)
                return 1
            pairs = await unspent_preparations(pairs, snapshot, machines, day)
            if not pairs:
                print("[guaranteed] An account traded after preparation. Skipping its cached offer.", flush=True)
                return 1
            if not args.plan:
                pairs = await eligible_pairs(pairs, args, fleet, machines, owners, day)
                if not pairs or date.today() != day:
                    return 1
        elif args.plan:
            state = lucky_raid_offers.load_state()
        else:
            eligible = await eligible_pairs(pairs, args, fleet, machines, owners, day)
            incomplete = len(eligible) < len(pairs)
            pairs = eligible
            if not pairs or date.today() != day:
                print("[guaranteed] No eligible pair to scan.", flush=True)
                return 1
            names = list(dict.fromkeys(n for pair in pairs for n in pair))
            try:
                state = await lucky_raid_offers.refresh_state(names, owners, machines, fleet, args.shots, pairs=pairs)
            except (starter.StepFailed, collect.home.HomeRecoveryError, collect.gbl_ios.IOSGBLError,
                    pokemon_fleet.FleetError, OSError) as exc:
                print(f"[guaranteed] Could not refresh live raid inventory: {exc}", flush=True)
                return 1
    planned = {}
    if not approved and getattr(args, "prepared_only", False):
        planned = {p:snapshot["raid_pairs"][pair_key(p)]["participants"] for p in pairs}
    elif not approved:
        available = [p for p in pairs if args.plan or all(n in owners for n in p)]
        planned, conflicts = proposal_itinerary(available, state or {}, fleet)
        for conflict in conflicts:
            listed = ", ".join(" ↔ ".join(p) for p in conflict)
            print(f"[guaranteed] Mutually exclusive alternatives in {listed}. Verified inventory and reserves "
                  "cannot support all of these offers in sequence.", flush=True)
    spent = set()
    prepared = []
    for pair in pairs:  # Two proposals may share a phone. Always park the first quote before opening the second.
        try:
            if date.today() != day:
                return 1
            unavailable = spent.intersection(pair)
            if unavailable:
                print(f"[guaranteed] {pair}: deferred, no Special Trade allowance left on {', '.join(unavailable)}", flush=True)
                incomplete = True
                continue
            if not args.plan and any(n not in owners for n in pair):
                print(f"[guaranteed] {pair}: a named phone is offline", flush=True)
                incomplete = True
                continue
            participants = approved["participants"] if approved else planned.get(pair)
            if not participants:
                missing = [n for n in pair if not (state or {}).get("devices", {}).get(n, {}).get("coverage")]
                if missing:
                    print(f"[guaranteed] {pair}: missing live raid-coverage evidence for {', '.join(missing)}", flush=True)
                else:
                    print(f"[guaranteed] {pair}: no mutually useful legendary swap qualified under the current "
                          "raid-role, eligibility, freshness and recipient coverage checks", flush=True)
                incomplete = True
                continue
            for donor, recipient in (pair, pair[::-1]):
                p = participants[donor]
                print(f"[guaranteed] {donor} offers {p['species']} #{p['dex']} CP {p['cp']} to {recipient}, "
                      f"{p['stock']} eligible. {p.get('reason', '')}", flush=True)
            if args.plan:
                print(f"[guaranteed] {pair}: inventory recommendation only; live cost and allowance unchecked", flush=True)
                continue
            receipts = await review_pair(pair, participants, args, fleet, machines, owners,
                                         args.approve_guaranteed, live_prompt=held_pair is not None)
            if not receipts:
                raise trade_local.TradeOutcomeUnknown(f"{pair}: no completion receipts returned")
            if on_exchange:
                on_exchange(pair, receipts)
            print(f"[guaranteed] Verified one approved exchange: {pair[0]} <-> {pair[1]}", flush=True)
        except trade_approval.ProposalReady as exc:
            prepared.append((pair, exc.proposal_id, participants))
            for n in pair:
                allowance = participants[n].get("special_remaining")
                print(f"[guaranteed] {n}: {allowance} Special Trades remaining at quote time", flush=True)
        except SpecialUnavailable as exc:
            spent.update(n for n in pair if str(exc).startswith(f"{n}:"))
            print(f"[guaranteed] {pair}: {exc}", flush=True)
            incomplete = True
        except (trade_approval.ApprovalRequired, trade_local.TradeOutcomeUnknown, starter.StepFailed,
                collect.home.HomeRecoveryError, collect.gbl_ios.IOSGBLError, pokemon_fleet.FleetError) as exc:
            print(f"[guaranteed] {pair}: {exc}", flush=True)
            incomplete = True
    if prepared:
        print(f"[guaranteed] Parked {len(prepared)} specific proposal(s). No exchanges made or allowances reserved.", flush=True)
        for i, (left, first_id, first) in enumerate(prepared):
            for right, second_id, second in prepared[i + 1:]:
                for shared in set(left).intersection(right):
                    one, two = first[shared], second[shared]
                    print(f"[guaranteed] {first_id} and {second_id} share a phone ({shared}). Both quotes were "
                          "made before any Special Trade was spent.", flush=True)
                    if (one["dex"], one["cp"], one.get("form")) == (two["dex"], two["cp"], two.get("form")):
                        print(f"[guaranteed] Both quote {shared}'s same {one['species']} CP {one['cp']}. "
                              "Accepting either makes the other individual unavailable.", flush=True)
                    if 1 in (one.get("special_remaining"), two.get("special_remaining")):
                        print(f"[guaranteed] {shared} showed one Special Trade remaining. Under that allowance "
                              "only one of these exchanges can proceed today.", flush=True)
    return int(incomplete)
