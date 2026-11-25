"""Prepare PH1's retained living Pokédex and record exact native species/form evidence."""
from __future__ import annotations

import asyncio
from datetime import datetime
from functools import lru_cache, partial
import json
import os
import re
import shlex
import struct
import subprocess
from urllib.parse import quote
from PIL import Image

from . import clear_pokemon as clear, config_paths, gbl_home_recovery, starter, storage_review as review
from . import raid_math, trade_local, trade_saved_search as saved


def _journal(device):
    return config_paths.state_dir() / "storage-review" / "keep-one" / (quote(device, safe="") + ".jsonl")


def _receipts(device):
    path = _journal(device)
    if not path.is_file():
        return {}
    records = {}
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row["device"] != device or row.get("tag") not in {review.KEEP_ONE, review.OLD_KEEP_ONE}:
            raise clear.ClearSafetyError(f"{device}: wrong Keep One receipt owner")
        records[row["dex"]] = row
    return records


def _record(device, dex, status, **details):
    path = _journal(device)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now().astimezone().isoformat(), "device": device,
           "tag": review.KEEP_ONE, "dex": dex, "status": status, **details}
    with path.open("a") as out:
        out.write(json.dumps(row) + "\n")
        out.flush()
        os.fsync(out.fileno())
    return row


class ReadUnavailable(clear.ClearSafetyError):
    """An inventory observation failed twice; it is not a count or a tag receipt."""


class EvolutionScopeUnverified(clear.ClearSafetyError):
    """A species-specific pass cannot proceed with family expansion unverified."""


READ_ERRORS = (starter.StepFailed, gbl_home_recovery.HomeRecoveryError, clear.gbl_vision.VisionOCRError,
               OSError, subprocess.TimeoutExpired)


def _shows_query(image, boxes, term, dex):
    if dex is None:
        return clear.search_field_matches_query(boxes, term, image.height)
    # On a narrow display the same submitted editor can align to either
    # end. Exact species mutation queries repeat the Dex at both boundaries.
    pill = clear._active_filter_box(image, boxes)
    return pill is not None and bool(re.search(rf"(?<!\d){dex}\s*&|&\s*{dex}(?!\d)", pill.text))


async def _grid(phone, term, dex, previous=None, *, require_tiles=False):
    if getattr(phone, "_living_android_fast", False):
        try:
            fast = await _android_grid(phone, term, dex, require_tiles)
            if fast is not None:
                return fast
        except READ_ERRORS:
            pass
        await _finish_android_editor(phone)
        previous = None
    for attempt in range(2):
        try:
            return await _read_grid(phone, term, dex, previous if not attempt else None, require_tiles=require_tiles)
        except EvolutionScopeUnverified:
            raise
        except READ_ERRORS as exc:
            if attempt:
                raise ReadUnavailable(f"{phone.name}: could not read {term!r} after retry: {exc}") from exc
            print(f"[keep-one] {phone.name}: retrying storage read: {exc}", flush=True)


async def _finish_android_editor(phone):
    if getattr(phone, "_living_editor_open", False):
        await asyncio.to_thread(phone.target._adb, "shell", "input keyevent 66")
        phone._living_editor_open = False


async def _android_storage_header(phone):
    """Read only the upper storage grid on local Android; keep coordinates in the original screen space."""
    size = getattr(phone, "_living_screen_size", None)
    if (size and isinstance(phone.target, gbl_home_recovery.AndroidTarget)
            and getattr(phone, "_living_raw_header", True)):
        w, h = size
        rows = int(.32*h)
        length = 16 + 4*w*rows
        display = getattr(phone.target, "display_id", None)
        cmd = "screencap" + (" -d " + shlex.quote(display) if display else "")
        try:
            result = await asyncio.to_thread(phone.target._adb, "exec-out", "sh", "-c",
                                             f"{cmd} | head -c {length}", binary=True, timeout=8)
            payload = result.stdout
            header = struct.unpack("<4I", payload[:16]) if len(payload) >= 16 else ()
            if (not result.returncode and len(payload) == length
                    and header[:3] == (w,h,1) and header[3] < 0x20000000):
                top = Image.frombytes("RGBA",(w,rows),payload[16:]).convert("RGB")
                boxes = await asyncio.to_thread(clear.gbl_vision.recognize,top)
                image = Image.new("RGB",size)
                image.paste(top, (0,0))
                return image,boxes
        except (OSError, subprocess.TimeoutExpired):
            pass
        phone._living_raw_header = False
    image = await phone.look()
    phone._living_screen_size = image.size
    rect = (0,0,image.width,int(.32*image.height))
    return image, await asyncio.to_thread(clear.gbl_vision.recognize,image,rect)


async def _android_grid(phone, term, dex, require_tiles):
    """Replace an already applied storage query, retaining native and grid proof."""
    read_header = getattr(phone, "read_verified_storage_header", None) or partial(_android_storage_header,phone)
    adb = phone.target._adb
    replace_verified = getattr(phone, "replace_verified_storage_editor", None)
    if not getattr(phone, "_living_editor_open", False):
        image, boxes = await read_header()
        if review._filtered_count(image, boxes) is None:
            return None
        await phone.tap_box(clear._storage_search_field(image,boxes), image)
        await asyncio.sleep(.45)
    if replace_verified is not None:
        # Handset-specific UiAutomator backends must verify the actual native
        # editor value after ACTION_SET_TEXT. The result grid is checked below.
        if await replace_verified(term) is not True:
            return None
        phone._living_editor_open = True
    else:
        if getattr(phone, "_living_ctrl_a", True):
            select = await asyncio.to_thread(adb,"shell","input keycombination 113 29")
            phone._living_ctrl_a = (select.returncode == 0 and "unknown" not in
                                    (str(select.stdout) + str(select.stderr)).lower())
        if not phone._living_ctrl_a:
            existing = await clear._android_edit_text(phone)
            if existing is None or len(existing) > 180:
                return None
            if existing:
                await asyncio.to_thread(adb, "shell", "input keyevent 123" + " 67" * (len(existing)+1))
        await asyncio.to_thread(adb,"shell","input text " + shlex.quote(term.replace(" ","%s")))
        typed = await clear._android_edit_text(phone)
        if typed is None or typed.strip().casefold() != term.casefold():
            phone._living_ctrl_a = False
            return None
        phone._living_editor_open = True
    if require_tiles:
        await asyncio.to_thread(adb,"shell","input keyevent 66")
        phone._living_editor_open = False
    before = None
    for _ in range(6):
        await asyncio.sleep(.45)
        image,boxes = await read_header()
        count = review._filtered_count(image,boxes)
        if count is not None and not clear.evolutionary_line_off(image, boxes):
            count = None
        if count is not None and count == before:
            if _shows_query(image,boxes,term,dex):
                if require_tiles and count:
                    image, boxes = await phone.read()
                    if (review._filtered_count(image,boxes) != count or not clear.evolutionary_line_off(image,boxes)
                            or not _shows_query(image,boxes,term,dex)):
                        return None
                    ime = await asyncio.to_thread(adb,"shell","dumpsys input_method")
                    if not re.search(r"\bmInputShown=false\b", ime.stdout or ""):
                        return None
                return count, (image,boxes)
        before = count
    return None


async def _read_grid(phone, term, dex, previous=None, *, require_tiles=False):
    """Confirm native text, two settled grid counts, and the submitted filter pill."""
    if previous is None:
        image, boxes = await phone.read()
        if review._filtered_count(image, boxes) is None:
            image, boxes = await starter.open_storage(phone)
    else:
        image, boxes = previous
    enter = clear._enter_query_ios if phone.ios else clear._enter_query_android
    await enter(phone, term, image, boxes, require_exact=True)
    prev = None
    uncertain_scope = False
    for _ in range(8):
        image, boxes = await phone.read()
        count = review._filtered_count(image, boxes)
        if count is not None:
            uncertain_scope = not clear.evolutionary_line_off(image, boxes)
            if uncertain_scope:
                count = None
        if count is not None and count == prev:
            if _shows_query(image,boxes,term,dex):
                return count, (image, boxes)
            if not require_tiles or count == 0:
                image, boxes = await review._verify_grid_editor(phone, image, boxes, term, count)
                return count, (image, boxes)
            break
        prev = count
        await asyncio.sleep(.45)
    if uncertain_scope:
        raise EvolutionScopeUnverified(f"{phone.name}: Show Evolutionary Line was not visibly off for {term!r}")
    raise clear.ClearSafetyError(f"{phone.name}: could not verify settled grid for {term!r} (saved {phone.save(image, 'keep-one-grid')})")


def _tag_row(boxes, name):
    return next((b for b in boxes if clear.gbl_vision.normalize(b.text).strip(" •·+") == name.casefold()), None)


async def _create_tag(phone, image, boxes):
    create = next((row for name in ("add new tag", "new tag", "create new tag", "create tag")
                   if (row := _tag_row(boxes, name))), None)
    if not create:
        return False
    await phone.tap_box(create, image)
    ok, image, boxes = await phone.settle(lambda i, b: starter.find_box(b, "create tag", exact=True) is not None
                                         and starter.find_box(b, "enter tag name", exact=True) is not None, timeout=6)
    if not ok:
        raise clear.ClearSafetyError(f"{phone.name}: tag creation sheet was not recognized")
    field = starter.find_box(boxes, "enter tag name", exact=True)
    await phone.tap_box(field, image)
    await asyncio.sleep(.5)
    await trade_local.type_text(phone, review.KEEP_ONE)
    if (await saved.editor_text(phone) or "").strip().casefold() != review.KEEP_ONE.casefold():
        raise clear.ClearSafetyError(f"{phone.name}: new tag name was not retained")
    if phone.ios:
        await clear._ios_tap_done(phone)
    else:
        await trade_local.submit(phone)
    image, boxes = await phone.read()
    exact = starter.find_box(boxes, "done", exact=True)
    if exact is None or starter.find_box(boxes, review.KEEP_ONE.casefold(), exact=True) is None:
        raise clear.ClearSafetyError(f"{phone.name}: cannot confirm the new tag name in the creation sheet")
    await phone.tap_box(exact, image)
    await asyncio.sleep(1)
    image, boxes = await phone.read()
    done = starter.find_box(boxes, "done", exact=True)
    if done is not None and starter.find_box(boxes, "tag 1 pokemon", exact=True) is not None:
        await phone.tap_box(done, image)
        await asyncio.sleep(.8)
    return True


async def _return_from_tagged_detail(phone):
    # Done returns to the individual detail, not Home. Go back exactly once
    # when the detail is recognized. The caller rechecks the actual tag grid.
    try:
        image, boxes = await phone.read()
        if clear.detail_page(image, boxes) and await phone.back(image):
            await phone.settle(lambda i,b: review._filtered_count(i,b) is not None, timeout=4)
    except READ_ERRORS:
        pass  # The next read opens verified storage normally if still needed.


async def _tag_best_tile(phone, grid):
    image, boxes = grid
    if not clear.evolutionary_line_off(image, boxes):
        raise EvolutionScopeUnverified(f"{phone.name}: cannot select a keeper without exact species scope")
    tiles = clear.safe_grid_tiles(image, boxes)
    if not tiles:
        raise clear.ClearSafetyError(f"{phone.name}: no CP tile in verified single-species grid")
    best = max(tiles, key=lambda b: int(re.search(r"\d+", b.text)[0]))
    if getattr(phone,"_living_android_fast",False) and getattr(phone,"_living_tag_exists",False):
        await _tag_selected_keepers(phone,grid,1,tile=best)
        return "assigned"
    await phone.tap([best.center_x, best.center_y + int(image.height * clear.TILE_RISE)], image)
    ok, image, boxes = await phone.settle(clear.detail_page, timeout=7)
    if not ok:
        raise clear.ClearSafetyError(f"{phone.name}: single Pokémon detail did not open")
    await phone.tap([int(image.width * clear.MENU_POINT[0]), int(image.height * clear.MENU_POINT[1])], image)
    ok, image, boxes = await phone.settle(lambda i,b: starter.find_box(b,"appraise",exact=True) is not None
                                         and starter.find_box(b,"tag",exact=True) is not None, timeout=7)
    if not ok:
        raise clear.ClearSafetyError(f"{phone.name}: single Pokémon Tag action did not open")
    await phone.tap_box(starter.find_box(boxes,"tag",exact=True), image)
    await asyncio.sleep(1)
    image, boxes = await phone.read()
    for _ in range(7):
        row = _tag_row(boxes, review.KEEP_ONE)
        if row is None and _tag_row(boxes, review.OLD_KEEP_ONE) is not None:
            raise clear.ClearSafetyError(f"{phone.name}: rename the existing {review.OLD_KEEP_ONE} tag in place first")
        if row:
            await phone.tap_box(row, image)
            await asyncio.sleep(.6)
            image, boxes = await phone.read()
            done = starter.find_box(boxes,"done",exact=True)
            if done is None:
                raise clear.ClearSafetyError(f"{phone.name}: tag sheet has no Done control")
            await phone.tap_box(done, image)
            await asyncio.sleep(.8)
            await _return_from_tagged_detail(phone)
            return "assigned"
        if await _create_tag(phone, image, boxes):
            # Creating a tag from the individual sheet can directly assign it;
            # let the exact postcondition determine whether another tap is needed.
            await _return_from_tagged_detail(phone)
            return "created"
        x = image.width // 2
        await phone.swipe([x, int(.72*image.height)], [x, int(.38*image.height)], image, .5, scroll=True)
        await asyncio.sleep(.6)
        image, boxes = await phone.read()
    raise clear.ClearSafetyError(f"{phone.name}: could not locate Keep One or its creation control (saved {phone.save(image, 'keep-one-tag-sheet')})")


async def _one(phone, policy, dex, *, retry_creation=True):
    account = review.trading_accounts.account_for(phone.name)
    if account != policy.receiver:
        raise clear.ClearSafetyError("Keep One is reserved for the receiver")
    physical = getattr(phone, "transport_name", phone.name)
    record = partial(_record, account, dex, phone=physical)
    predicate = review.ANCHOR
    retained_query = f"{dex}&{predicate}"
    count, grid = await _grid(phone, retained_query, dex)
    tag_term = f"{dex}&#{review.KEEP_ONE}&{dex}"
    if count:
        prior = _receipts(account).get(dex, {})
        if prior.get("status") == "tagging":
            tagged, _ = await _grid(phone, tag_term, dex, grid)
            if tagged != 1:
                raise clear.ClearSafetyError(f"{phone.name}: interrupted Keep One for #{dex} found {tagged} tagged")
            _save_anchor(account, dex, retained_query, count, predicate, physical)
            return record("verified one", count=prior["count"], tagged=tagged, query=tag_term)
        _save_anchor(account, dex, retained_query, count, predicate, physical)
        return record("already retained", count=count)
    filters = "&".join(dict.fromkeys((*policy.filters,"!4*","!#98Plus","!#KeepOne")))
    base = f"{dex}&{filters}"
    count, grid = await _grid(phone, f"{base}&{dex}", dex, grid)
    if count < 2:
        return record("fewer than two", count=count)
    # Search from highest IV bracket down, preferring highest visible CP
    # within that bracket. Only the individual detail menu can add the tag.
    for stars in (3, 2, 1, 0):
        term = f"{base}&{stars}*&{dex}"
        n, grid = await _grid(phone, term, dex, grid, require_tiles=True)
        if n:
            break
    else:
        raise clear.ClearSafetyError(f"{phone.name}: none of {count} candidates had a readable appraisal tier")
    record("tagging", count=count, selected_query=term)
    action = await _tag_best_tile(phone, grid)
    # Regardless of interrupted retries or creation-sheet behavior, require
    # exactly one account-local match before allowing duplicates into review.
    tagged, grid = await _grid(phone, tag_term, dex)
    if tagged == 0 and action == "created" and retry_creation:
        return await _one(phone, policy, dex, retry_creation=False)
    if tagged != 1:
        raise clear.ClearSafetyError(f"{phone.name}: expected one #{dex} with Keep One, found {tagged}")
    _save_anchor(account, dex, tag_term, tagged, predicate, physical)
    return record("verified one", count=count, tagged=tagged, query=tag_term)


def _save_anchor(device, dex, term, count, predicate, physical):
    checked = review._evidence(device, predicate)
    now = datetime.now().astimezone()
    checked[str(dex)] = {"query":term, "count":count, "day":now.date().isoformat(), "at":now.isoformat(), "phone":physical}
    review._checkpoint(device, checked, predicate=predicate)


def _unresolved_group_one(row):
    # Early receipts stored the group count directly in "count". Exact
    # single-species checks have no "group" and already checked for a keeper.
    return (row["status"] == "fewer than two" and bool(row.get("group")) and "isolated_dex" not in row
            and row.get("group_count", row.get("count")) == 1)


async def keep_one_duplicates(phone, policy, *, limit=None, candidates=None, pooled_ones_only=False):
    """One explicit pass. Reuse protected species and today's verified undersized groups."""
    account = review.trading_accounts.account_for(phone.name)
    if account != policy.receiver:
        raise clear.ClearSafetyError("Keep One is reserved for the receiver")
    previous = _receipts(account)
    day = datetime.now().astimezone().date().isoformat()
    pooled_ones = {dex for dex, row in previous.items() if _unresolved_group_one(row)}
    skip = review.load_verified_anchors(account) | {
        d for d, rec in previous.items() if rec["status"] == "fewer than two" and d not in pooled_ones
        and rec["at"][:10] == day}
    interrupted = {d for d, rec in previous.items() if rec["status"] == "tagging"}
    possible = (policy.low_raid - skip) | (policy.low_raid & interrupted)
    if pooled_ones_only:
        possible &= pooled_ones
        if candidates is None:
            candidates = sorted(possible)
    if candidates is None:
        if not possible:
            return []
        try:
            labels = await review._grid_names(phone, review._species_term(policy.filters, possible), 5)
        except READ_ERRORS as exc:
            # OCR hints only determine check order. Native inventory reads
            # remain mandatory, and can proceed on smaller groups.
            print(f"[keep-one] {phone.name}: skipping unavailable ordering hints: {exc}", flush=True)
            labels = {}
        def priority(d):
            return (d not in interrupted, -labels.get(clear.normalize_species_token(policy.names[d]), 0),
                    d not in policy.safe_species, d)
        candidates = sorted(possible, key=priority)
    pending = [d for d in dict.fromkeys(candidates) if d in possible]
    if limit is not None:
        pending = pending[:limit]
    records = []
    physical = getattr(phone, "transport_name", phone.name)

    async def check_candidate(dex, *, expected_single=False, grid=None):
        exact = f"{dex}&{'&'.join(policy.filters)}&{dex}"
        count, grid = await _grid(phone, exact, dex, grid)
        if expected_single and count != 1:
            return None
        if not count:
            return _record(account, dex, "fewer than two", count=0, phone=physical, eligible_query=exact)
        protected = f"{dex}&{review.ANCHOR}"
        held, _ = await _grid(phone, protected, dex, grid)
        if held:
            _save_anchor(account, dex, protected, held, review.ANCHOR, physical)
            return _record(account, dex, "already retained", count=held, phone=physical,
                           eligible_query=exact, retained_query=protected)
        status = "fewer than two" if count == 1 else "unprotected duplicates"
        return _record(account, dex, status, count=count, phone=physical,
                       eligible_query=exact, retained_query=protected, retained_count=0)

    async def isolate_single(group, group_term):
        # The parent query contains exactly one eligible Pokémon. Use small
        # subset queries to locate it, then *independently* verify the exact
        # eligible Dex and its disjoint protection predicate. No tag mutation.
        remaining = group
        grid = None
        while len(remaining) > 1:
            mid = len(remaining) // 2
            left = remaining[:mid]
            left_count, grid = await _grid(phone, review._species_term(policy.filters, left), None, grid)
            if left_count > 1:
                return False
            remaining = left if left_count == 1 else remaining[mid:]
        dex, = remaining
        row = await check_candidate(dex, expected_single=True, grid=grid)
        if row is None:
            return False
        records.append(row)
        for other in group:
            if other != dex:
                records.append(_record(account, other, "fewer than two", count=0, group_count=1,
                                       isolated_dex=dex, group_query=group_term, group=group, phone=physical))
        return True

    async def inspect(group):
        if len(group) >= 2 and not interrupted.intersection(group):
            group_term = review._species_term(policy.filters, group)
            try:
                count, _ = await _grid(phone, group_term, None)
            except ReadUnavailable:
                # A long expression can be unreadable where smaller ones work.
                count = None
            if count == 0:
                for dex in group:
                    records.append(_record(account, dex, "fewer than two", count=0, group_count=0, phone=physical,
                                           group=group, group_query=group_term))
                return
            if count == 1:
                try:
                    if await isolate_single(group, group_term):
                        return
                except ReadUnavailable:
                    pass  # Split fresh if any intermediate observation was inconclusive.
            if len(group) >= 8 or (len(group) >= 4 and (count is None or count <= len(group))):
                middle = len(group) // 2
                await inspect(group[:middle])
                await inspect(group[middle:])
                return
        for dex in group:
            try:
                # The supplemental pass is read-only even if stock has
                # changed since the historical group-one observation.
                result = (await check_candidate(dex) if pooled_ones_only else await _one(phone, policy, dex))
            except ReadUnavailable as exc:
                # No outcome can be inferred. In particular leave any existing
                # "tagging" journal entry intact for exact reconciliation.
                print(f"[keep-one] {phone.name}: deferring #{dex}: {exc}", flush=True)
                records.append({"device": account, "phone": physical, "dex": dex,
                                "status": "read deferred", "count": None, "error": str(exc)})
                continue
            records.append(result)
            print(f"[keep-one] {phone.name}: #{dex} {result['status']} ({result['count']})", flush=True)
    # Reconcile any possible prior tag separately; group candidate counts can
    # legitimately be zero once the specimen has the protective tag.
    for dex in pending:
        if dex in interrupted:
            await inspect([dex])
    fresh = [dex for dex in pending if dex not in interrupted]
    for start in range(0, len(fresh), 48):
        await inspect(fresh[start:start + 48])
    return records


def _living_record(account, phone, dex, scope, status, **values):
    row = {"account": account, "phone": getattr(phone, "transport_name", phone.name), "dex": dex,
           "scope": scope, "at": datetime.now().astimezone().isoformat(), "status": status, **values}
    path = review.living_journal(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:
        out.write(json.dumps(row) + "\n")
        out.flush()
        os.fsync(out.fileno())
    return row


@lru_cache(maxsize=2048)
def _historical_hundo_cps(path, modified):
    # These are cropped header/CP images taken for individual Dex+4* queries.
    # Do not infer forms here; a separate, form-bounded live query does that.
    with Image.open(path) as image:
        boxes = clear.gbl_vision.recognize(image.resize((image.width*2, image.height*2)))
    return {int(m[1]) for box in boxes if (m := re.fullmatch(r"\s*CP\s*(\d+)\s*", box.text, re.I))}


def _match_hundo_cp(group, domains, cp):
    # Do not attribute a CP to a species if a rounding variation for another
    # one in the same search could produce it. Unknown members invalidate the
    # entire batch, not just their own proof.
    if any(d not in domains for d in group):
        return None
    possible = [d for d in group if any(n in domains[d] for n in range(max(10, cp-2), cp+3))]
    return possible[0] if len(possible) == 1 and cp in domains[possible[0]] else None


def _save_batch_grid(account, first_dex, image):
    folder = review.living_journal(account).parent / "shots"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    shot = folder / f"{quote(account,safe='')}-{stamp}-{first_dex}.png"
    image.save(shot)
    return str(shot)


def _exact_live_grid(image, boxes, term, count):
    return (review._filtered_count(image, boxes) == count and clear.evolutionary_line_off(image, boxes)
            and clear.search_field_matches_query(boxes, term, image.height))


def _nominate_pairs(policy, group, image, boxes, *, singletons=False, highest_cp=False):
    pairs, used_dex, used_cp = [], set(), set()
    repeated = set()
    tiles = clear.safe_grid_tiles(image,boxes)
    if highest_cp:
        tiles = sorted(tiles,key=lambda tile:int(re.search(r"\d+",tile.text)[0]),reverse=True)
    for tile in tiles:
        cp = int(re.search(r"\d+", tile.text)[0])
        label = review.collect_trades._tile_species_label(image, boxes, tile)
        label = re.sub(r"(100|98|hundo|perfect)$", "", label)
        possible = [d for d in group if clear.normalize_species_token(policy.names[d]) == label]
        if not possible:
            possible = [d for d in group if label in policy.living_labels[d]]
        if len(possible) != 1:
            continue
        dex = possible[0]
        if dex in used_dex:
            repeated.add(dex)
        elif cp not in used_cp:
            pairs.append((dex,cp))
            used_cp.add(cp)
            used_dex.add(dex)
        elif singletons:
            repeated.add(dex)
    return [(d,cp) for d,cp in pairs if not singletons or d not in repeated]


async def _batch_retained(phone, account, policy, group, scope, term, count, *, singleton_days=None):
    """Use grid labels to nominate CPs; only the independent per-species CP query proves them."""
    group = [dex for dex in group if scope in policy.living_forms[dex]]
    if singleton_days is not None:
        group = [dex for dex in group if dex in singleton_days]
    if count < 1 or len(group) < 2 or not getattr(policy, "living_labels", None):
        return []
    image, boxes = await phone.read()
    if not _exact_live_grid(image, boxes, term, count):
        return []
    pairs = _nominate_pairs(policy, group, image, boxes)
    if (getattr(phone,"_living_android_fast",False) and len(group) >= 6 and len(pairs) < min(6,len(group))
            and count > len(clear.safe_grid_tiles(image,boxes))):
        await _finish_android_editor(phone)
        used_dex = {d for d,cp in pairs}
        used_cp = {cp for d,cp in pairs}
        previous_page = None
        for page in range(3):
            image,boxes = await phone.read()
            if not _exact_live_grid(image,boxes,term,count):
                break
            tiles = clear.safe_grid_tiles(image,boxes)
            signature = [(tile.text,tile.center_x,tile.center_y) for tile in tiles]
            if not tiles or signature == previous_page:
                break
            previous_page = signature
            before = len(pairs)
            for dex,cp in _nominate_pairs(policy,group,image,boxes):
                if dex not in used_dex and cp not in used_cp:
                    pairs.append((dex,cp))
                    used_dex.add(dex)
                    used_cp.add(cp)
            if (len(pairs) >= min(12,len(group)) or count <= len(tiles) or page == 2
                    or page > 0 and len(pairs) == before):
                break
            x = image.width//2
            await phone.swipe([x,int(.75*image.height)],[x,int(.38*image.height)],image,.45,scroll=True)
            await asyncio.sleep(.45)
    # Short native clauses expose more of the verified grid in each query.
    # Spread them evenly so the last group can also prove distinct CPs.
    predicate = review.RETAINED if singleton_days is None else "#dontrade"
    if len(pairs) == 1:
        # One named tile nominates an exact native Dex lookup. Only its
        # independent positive same-form count establishes membership.
        dex, _ = pairs[0]
        exact = f"{dex}&{scope}&{predicate}&{dex}"
        n,_ = await _grid(phone,exact,dex)
        if not n:
            return []
        if singleton_days is not None:
            return [_living_record(account,phone,dex,scope,"retained singleton",count=n,query=exact,
                                   singleton_tag_day=singleton_days[dex])]
        return [_living_record(account,phone,dex,scope,"retained",count=n,query=exact)]
    if len(pairs) > 8:
        results = []
        batches = (len(pairs)+7)//8
        for offset in range(batches):
            batch = pairs[offset::batches]
            conditioned = review.retained_cp_query(scope,batch,predicate)
            results.extend(await _prove_retained_pairs(phone,account,scope,batch,conditioned,singleton_days=singleton_days))
        return results
    conditioned = review.retained_cp_query(scope, pairs, predicate)
    if conditioned is None:
        return []
    return await _prove_retained_pairs(phone, account, scope, pairs, conditioned, singleton_days=singleton_days)


async def _prove_retained_pairs(phone, account, scope, pairs, conditioned, *, singleton_days=None):
    used_cp = {cp for _,cp in pairs}
    full_grid = len(pairs) >= 4 and getattr(phone,"_living_android_fast",False)
    observed, grid = await _grid(phone, conditioned, None, require_tiles=full_grid)
    if not observed:
        return []
    def visible(image, boxes):
        if not _exact_live_grid(image, boxes, conditioned, observed):
            return set()
        return {int(re.search(r"\d+", tile.text)[0]) for tile in clear.safe_grid_tiles(image, boxes)}
    first = visible(*(grid if full_grid else await phone.read()))
    if not first or not first.issubset(used_cp):
        return []
    await asyncio.sleep(.45)
    image, boxes = await phone.read()
    second = visible(image, boxes)
    found = first & second
    if not found or not second.issubset(used_cp):
        return []
    shot = _save_batch_grid(account, pairs[0][0], image)
    results = []
    for dex,cp in pairs:
        if cp in found:
            details = {"singleton_tag_day":singleton_days[dex]} if singleton_days is not None else {}
            status = "retained singleton" if singleton_days is not None else "retained"
            results.append(_living_record(account,phone,dex,scope,status,count=1,method="native species CP clauses",
                                         observed_cp=cp,pairs=pairs,query=conditioned,shot=shot,**details))
    return results


async def _complete_cp_grid(phone, term, pairs):
    """Require all nominated CPs simultaneously, with exactly one native result per pair."""
    expected = sorted(cp for dex,cp in pairs)
    count, grid = await _grid(phone,term,None,require_tiles=True)
    if count != len(expected):
        return None
    for attempt in range(2):
        image, boxes = grid
        if not _exact_live_grid(image,boxes,term,count):
            return None
        visible = [int(re.search(r"\d+",tile.text)[0]) for tile in clear.safe_grid_tiles(image,boxes)]
        if sorted(visible) != expected:
            return None
        if attempt == 0:
            await asyncio.sleep(.45)
            grid = await phone.read()
    return grid


BATCH_CANDIDATE = "!4*&!#98Plus&!#KeepOne&!#dontrade&!favorite&!buddy1-5"


async def _singleton_batch_proof(phone, scope, pairs):
    # The unconditional total is necessary: a CP-filtered result on its
    # own cannot tell a sole specimen from one of several duplicates.
    if len(pairs) < 2:
        return None
    owned = review._species_term((scope,),[dex for dex,cp in pairs])
    total, _ = await _grid(phone,owned,None)
    if total != len(pairs):
        return None
    term = review.retained_cp_query(scope,pairs,BATCH_CANDIDATE)
    grid = await _complete_cp_grid(phone,term,pairs) if term else None
    return (owned,term,grid) if grid is not None else None


def _selected_count_button(image, boxes, count):
    return next((b for b in boxes if b.center_y > .65*image.height and
                 re.fullmatch(rf"tag\s*\(\s*{count}\s*\)",b.text.strip(),re.I)),None)


async def _tag_selected_keepers(phone, grid, count, *, tile=None):
    image, boxes = grid
    if not clear.evolutionary_line_off(image,boxes):
        raise EvolutionScopeUnverified(f"{phone.name}: exact species scope changed before group selection")
    if tile is None:
        tile = clear.safe_grid_tiles(image,boxes)[0]
    await saved._hold(phone,[tile.center_x,tile.center_y+int(image.height*clear.TILE_RISE)],image)
    ok,image,boxes = await phone.settle(lambda i,b: _selected_count_button(i,b,1) is not None and
                                        (count == 1 or starter.find_box(b,"select all",exact=True) is not None),timeout=7)
    if not ok:
        raise clear.ClearSafetyError(f"{phone.name}: multi-select did not confirm one initial selection")
    if count > 1:
        await phone.tap_box(starter.find_box(boxes,"select all",exact=True),image)
        ok,image,boxes = await phone.settle(lambda i,b: _selected_count_button(i,b,count) is not None,timeout=6)
        if not ok:
            raise clear.ClearSafetyError(f"{phone.name}: selected keeper count did not reach {count}")
    await phone.tap_box(_selected_count_button(image,boxes,count),image)
    def sheet(i,b):
        return starter.find_box(b,f"tag {count} pokemon",exact=True) is not None
    ok,image,boxes = await phone.settle(sheet,timeout=7)
    if not ok:
        raise clear.ClearSafetyError(f"{phone.name}: could not confirm a tag sheet for {count} specimens")
    for _ in range(7):
        row = _tag_row(boxes,review.KEEP_ONE)
        if row:
            await phone.tap_box(row,image)
            await asyncio.sleep(.5)
            image,boxes = await phone.read()
            done = starter.find_box(boxes,"done",exact=True)
            if not sheet(image,boxes) or done is None:
                raise clear.ClearSafetyError(f"{phone.name}: could not verify KeepOne sheet before saving")
            await phone.tap_box(done,image)
            await asyncio.sleep(.6)
            return
        x = image.width//2
        await phone.swipe([x,int(.71*image.height)],[x,int(.39*image.height)],image,.5,scroll=True)
        await asyncio.sleep(.5)
        image,boxes = await phone.read()
        if not sheet(image,boxes):
            break
    raise clear.ClearSafetyError(f"{phone.name}: existing KeepOne tag not found on the batch assignment sheet")


async def _batch_singletons(phone, account, policy, group, scope, term, total):
    """Tag multiple exact-form sole copies only when the native total and every distinct CP agree."""
    candidates = [d for d in group if scope in policy.living_forms[d]]
    if (not 2 <= total <= 12 or len(candidates) < 2 or not getattr(policy,"living_labels",None)):
        return []
    image,boxes = await phone.read()
    if not _exact_live_grid(image,boxes,term,total):
        return []
    pairs = _nominate_pairs(policy,candidates,image,boxes,singletons=True)[:6]
    proof = await _singleton_batch_proof(phone,scope,pairs)
    if proof is None:
        return []
    owned,selected,grid = proof
    return await _assign_batch(phone,account,scope,pairs,selected,grid,owned_query=owned)


async def _batch_absent(phone,account,policy,group,scope,term,total):
    """Use visible names to propose a single native zero for the rest of a sparse group."""
    image,boxes = await phone.read()
    if not _exact_live_grid(image,boxes,term,total):
        return []
    nominees = {dex for dex,cp in _nominate_pairs(policy,group,image,boxes)}
    remainder = [d for d in group if d not in nominees]
    if not nominees or len(remainder) < 4:
        return []
    # Misspelled names, arbitrary nicknames, and hidden rows cannot clear a
    # species. Every member of the remainder must pass this exact zero.
    checked = review._species_term((scope,),remainder)
    count,_ = await _grid(phone,checked,None)
    if count:
        return []
    return [_living_record(account,phone,dex,scope,"absent",count=0,group=remainder,query=checked)
            for dex in remainder]


async def _assign_batch(phone, account, scope, pairs, selected, grid, *, owned_query=None, tier=None,
                        higher_tiers=None, higher_cps=None):
    proof = ({"owned":1,"owned_query":owned_query} if owned_query else
             {"tier":tier,"higher_tiers_query":higher_tiers,"higher_cp_query":higher_cps})
    for dex,cp in pairs:
        _living_record(account,phone,dex,scope,"tagging",count=1,selected=selected,observed_cp=cp,**proof)
    await _tag_selected_keepers(phone,grid,len(pairs))
    post = review.retained_cp_query(scope,pairs,review.KEPT)
    try:
        checked = await _complete_cp_grid(phone,post,pairs)
        # A true singleton group has no other untagged specimens. Duplicate
        # groups must retain exactly K tagged specimens across all IV tiers.
        population = "!#KeepOne" if owned_query else "#KeepOne"
        population_query = review._species_term((scope,population),[d for d,_ in pairs])
        after,_ = await _grid(phone,population_query,None)
    except ReadUnavailable as exc:
        raise clear.ClearSafetyError(f"{phone.name}: grouped keeper assignment awaits native verification") from exc
    if checked is None or after != (0 if owned_query else len(pairs)):
        raise clear.ClearSafetyError(f"{phone.name}: grouped keeper assignment did not reconcile")
    shot = _save_batch_grid(account,pairs[0][0],checked[0])
    return [_living_record(account,phone,dex,scope,"tagged",count=1,method="native species CP clauses",
                           observed_cp=cp,pairs=pairs,query=post,shot=shot,selected=selected,group_total=len(pairs),
                           population_query=population_query,population_after=after,**proof) for dex,cp in pairs]


async def _best_batch_proof(phone, policy, scope, pairs, tier):
    if len(pairs) < 2:
        return None
    excluded = ",".join([review.RETAINED,*(f"{s}*" for s in range(3,tier,-1))])
    while True:
        numbers = [dex for dex,_ in pairs]
        higher_tiers = review._species_term((scope,excluded),numbers)
        protected,_ = await _grid(phone,higher_tiers,None)
        if not protected:
            break
        if len(pairs) <= 2:
            return None
        # One existing keeper can block a whole group. Labels only remove
        # nominees. The smaller group must independently prove zero again.
        image,boxes = await phone.read()
        if not _exact_live_grid(image,boxes,higher_tiers,protected):
            return None
        blocked = {dex for dex,_ in _nominate_pairs(policy,numbers,image,boxes)}
        if not blocked:
            return None
        pairs = [(dex,cp) for dex,cp in pairs if dex not in blocked]
        if len(pairs) < 2:
            return None
    for attempt in range(2):
        constraints = [f"!{dex},cp{cp+1}-" for dex,cp in pairs]
        higher_cps = review._species_term((scope,f"{tier}*",*constraints),numbers)
        larger,_ = await _grid(phone,higher_cps,None)
        if not larger:
            selected = review.retained_cp_query(scope,pairs,f"{tier}*&{BATCH_CANDIDATE}")
            grid = await _complete_cp_grid(phone,selected,pairs) if selected else None
            return (pairs,selected,grid,higher_tiers,higher_cps) if grid is not None else None
        if attempt:
            break
        image,boxes = await phone.read()
        if not _exact_live_grid(image,boxes,higher_cps,larger):
            break
        hints = dict(_nominate_pairs(policy,numbers,image,boxes,highest_cp=True))
        improved = [(d,max(cp,hints.get(d,cp))) for d,cp in pairs]
        if improved == pairs or len({cp for d,cp in improved}) != len(improved):
            break
        pairs = improved
    return None


async def _batch_best_tier(phone,account,policy,group,scope):
    # The same IV tier lets all nominees share the two independent absence
    # checks. Native CP clauses make the final positives species-specific.
    for tier in (3,2,1,0):
        term = review._species_term((scope,f"{tier}*",BATCH_CANDIDATE),group)
        n,grid = await _grid(phone,term,None,require_tiles=True)
        if n < 2:
            continue
        if not _exact_live_grid(*grid,term,n):
            return []
        pairs = _nominate_pairs(policy,group,*grid,highest_cp=True)[:6]
        proof = await _best_batch_proof(phone,policy,scope,pairs,tier)
        if proof is None:
            continue
        pairs,selected,grid,higher_tiers,higher_cps = proof
        return await _assign_batch(phone,account,scope,pairs,selected,grid,tier=tier,
                                   higher_tiers=higher_tiers,higher_cps=higher_cps)
    return []


async def _batch_raid_hints(phone, account, policy, pending, evidence):
    # Old CPs are candidate values only. A stale or misidentified specimen
    # cannot pass the independent native guarded query for the current scope.
    path = config_paths.state_dir() / f"raidscan_{quote(account,safe='')}.json"
    try:
        raw = json.loads(path.read_text()) if path.is_file() else []
    except (OSError, ValueError):
        raw = []
    if not isinstance(raw,list):
        raw = []
    available = set(pending)
    raid_cps = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        search = str(row.get("search",""))
        if not search.isdecimal() or int(search) not in available:
            continue
        dex = int(search)
        # Prefer historical hundos, then the strongest individual known at
        # the time. Only an existing keeper can match the new native query.
        raid_cps[dex] = [cp for key in ("hundo_cps","good_cps") if isinstance(row.get(key),list)
                         for cp in sorted((n for n in row[key] if type(n) is int and 10 <= n <= 99999), reverse=True)]
    by_scope = {}
    day, perfect = review._held_perfect(account)
    for dex in pending:
        needed = [s for s in policy.living_forms[dex] if not review.verified_living_row(evidence.get((dex,s),{}),dex,s)]
        if not needed:
            continue
        choices = raid_cps.get(dex,[])
        if dex in perfect:
            # Conditional native CP clauses also work for Mega-capable and
            # regional species whose level-to-CP domains cannot identify them.
            shot = config_paths.state_dir()/"perfect-audit"/day/f"stock-{dex:03d}&4star.jpg"
            try:
                choices = sorted(_historical_hundo_cps(shot,shot.stat().st_mtime_ns),reverse=True) + choices
            except (OSError, clear.gbl_vision.VisionOCRError):
                pass
        for scope in needed:
            if choices:
                by_scope.setdefault(scope,{})[dex] = choices
    records = []
    for scope, hints in by_scope.items():
        groups = []
        for dex, choices in hints.items():
            for group in groups:
                used = {value for _,value in group}
                candidate = next((cp for cp in choices if cp not in used),None)
                if candidate and len(group) < 8:
                    group.append((dex,candidate))
                    break
            else:
                groups.append([(dex,choices[0])])
        for pairs in groups:
            conditioned = review.retained_cp_query(scope,pairs)
            if conditioned is not None:
                try:
                    records.extend(await _prove_retained_pairs(phone,account,scope,pairs,conditioned))
                except ReadUnavailable:
                    pass
    return records


async def _batch_hundos(phone, account, policy, pending, evidence):
    """Prove multiple historical hundo identities in a fresh ordinary-form grid."""
    date, perfect = review._held_perfect(account)
    prior = [d for d in pending if d in perfect and policy.living_forms[d] == (review.ORDINARY_FORM,)
             and not review.verified_living_row(evidence.get((d,review.ORDINARY_FORM), {}), d, review.ORDINARY_FORM)]
    if len(prior) < 2:
        return []
    data = review.raid_gamedata.load()
    domains, historic = {}, {}
    for d in prior:
        forms = [r for r in data.pokemon.values() if r["dex"] == d]
        if len(forms) != 1 or forms[0].get("megas"):
            continue
        path = config_paths.state_dir() / "perfect-audit" / date / f"stock-{d:03d}&4star.jpg"
        levels = data.levels
        if not levels or min(levels) > 1 or max(levels) < 51:
            continue
        try:
            cp = _historical_hundo_cps(path,path.stat().st_mtime_ns)
        except (OSError, clear.gbl_vision.VisionOCRError):
            continue
        if cp:
            domains[d] = {raid_math.cp_of(forms[0]["stats"],(15,15,15),data.cpm(level)) for level in levels}
            historic[d] = cp
    results = []
    # Do not put species together when that would make every historical CP
    # for one member ambiguous. This makes each grid more informative.
    groups = []
    for dex in domains:
        for group in groups:
            if len(group) < 8 and all(any(_match_hundo_cp([*group,dex],domains,cp) == d for cp in historic[d])
                                      for d in (*group,dex)):
                group.append(dex)
                break
        else:
            groups.append([dex])
    for group in groups:
        if len(group) < 2:
            continue
        term = review._species_term((review.ORDINARY_FORM,"4*"),group)
        try:
            count, _ = await _grid(phone,term,None)
            image,boxes = await phone.read()
        except ReadUnavailable:
            continue
        def visible(image, boxes):
            if not _exact_live_grid(image,boxes,term,count):
                return set()
            found = set()
            for tile in clear.safe_grid_tiles(image, boxes):
                cp = int(re.search(r"\d+",tile.text)[0])
                dex = _match_hundo_cp(group,domains,cp)
                if dex is None or cp not in historic[dex]:
                    continue
                label = review.collect_trades._tile_species_label(image,boxes,tile)
                # Owners often keep a pre-evolution nickname after evolving.
                # Require the independent CP identity and historical exact
                # Dex/CP first; a family nickname is corroboration only.
                label = re.sub(r"(100|98|hundo|perfect)$", "", label)
                if label in policy.living_labels[dex]:
                    found.add((dex,cp))
            return found
        seen = visible(image,boxes)
        if not seen:
            continue
        await asyncio.sleep(.45)
        image,boxes = await phone.read()
        proven = seen & visible(image,boxes)
        if not proven:
            continue
        shot = _save_batch_grid(account, group[0], image)
        for dex,cp in sorted(proven):
            results.append(_living_record(account,phone,dex,review.ORDINARY_FORM,"preferred",count=1,
                           method="matched hundo CP",group=group,query=term,historical_cp=cp,
                           observed_cp=cp,perfect_audit=date,shot=str(shot)))
    return results


async def _living_scope(phone, account, dex, scope, *, prior, commit=True, no_retained=False, singleton_tag_day=None,
                        retry_creation=True):
    """Classify a single exact species/form, retaining existing memberships and IV keepers."""
    prefix = f"{dex}&{scope}"
    record = partial(_living_record, account, phone, dex, scope)
    previous = None
    async def query(predicate=None, *, tiles=False):
        nonlocal previous
        term = f"{prefix}&{predicate}&{dex}" if predicate else f"{prefix}&{dex}"
        count, previous = await _grid(phone, term, dex, previous, require_tiles=tiles)
        return count, term

    # A failed attempt can leave the native tag assigned before the receipt
    # is written. Check the resulting membership before considering another tap.
    if prior.get("status") == "tagging":
        n, term = await query("#KeepOne")
        if n:
            return record("tagged", count=n, query=term)
    if not commit:
        owned, term = await query()
        if not owned:
            return record("absent", count=0, query=term)
        if owned > 1:
            n, retained_term = (0, None) if no_retained else await query(review.RETAINED)
            return record("forms protected", count=owned, query=term, retained_count=n, retained_query=retained_term)
    if singleton_tag_day:
        # Reuse the owner's original singleton only in a scope where a
        # dontrade member is still physically present. Other forms remain held.
        n, term = await query("#dontrade")
        if n:
            return record("retained singleton", count=n, query=term, singleton_tag_day=singleton_tag_day)
    if not no_retained:
        n, term = await query(review.RETAINED)
        if n:
            return record("retained", count=n, query=term)
    if commit:
        owned, term = await query()
        if not owned:
            return record("absent", count=0, query=term)

    # A numeric Dex does not identify variants of a costume or other group
    # without a native form token. One owned member is an unambiguous keeper.
    # All specimens in a larger ambiguous group remain outside outgoing lists.
    # No 4* / owner 98Plus / existing keeper. The highest IV bracket (then
    # visible CP) is safe to reserve even when this is the one existing copy.
    candidate = "!4*&!#98Plus&!#KeepOne"
    if singleton_tag_day:
        candidate += "&!#dontrade"
    if owned == 1:
        n, selected = await query(candidate, tiles=True)
        if n != 1:
            raise ReadUnavailable(f"{phone.name}: unprotected singleton count changed for {prefix}")
    else:
        for stars in (3, 2, 1, 0):
            n, selected = await query(f"{stars}*&{candidate}", tiles=True)
            if n:
                break
        else:
            raise ReadUnavailable(f"{phone.name}: no verified IV tier among {owned} copies of {prefix}")
    record("tagging", count=owned, selected=selected)
    action = await _tag_best_tile(phone, previous)
    previous = None
    n, term = await query("#KeepOne")
    if not n and action == "created" and retry_creation:
        # A newly created native tag may still require selecting it. Fresh
        # exact inventory is mandatory before revisiting a single Pokémon.
        return await _living_scope(phone, account, dex, scope, prior={"status":"tagging"}, commit=commit,
                                   retry_creation=False)
    if n != 1:
        raise clear.ClearSafetyError(f"{phone.name}: expected exactly one tagged {prefix}, found {n}")
    return record("tagged", count=n, owned=owned, query=term)


async def prepare_living_dex(phone, policy, *, limit=None, candidates=None, fast_android=True):
    """Prepare the receiver's catalog once, resuming verified species/form receipts.

    A bounded invocation considers at most `limit` pending Dex numbers. The
    Android search-field shortcut always falls back to the exact native
    input and settled grid checks if its own verification fails.
    """
    previous = getattr(phone, "_living_android_fast", False)
    previous_tag = getattr(phone, "_living_tag_exists", False)
    phone._living_android_fast = fast_android and not getattr(phone, "ios", True)
    try:
        return await _prepare_living_dex(phone, policy, limit=limit, candidates=candidates)
    finally:
        await _finish_android_editor(phone)
        phone._living_android_fast = previous
        phone._living_tag_exists = previous_tag


async def _prepare_living_dex(phone, policy, *, limit=None, candidates=None):
    """Explicit whole-catalog PH1 preparation. limit bounds numbered species per call.

    Ordinary and searchable regional forms are separate. A costume or ambiguous
    form never releases another form to a public transfer or trade search.
    """
    account = review.trading_accounts.account_for(phone.name)
    if account != policy.receiver or policy.living_forms is None:
        raise clear.ClearSafetyError("Living Dex preparation requires the receiver and its audited form catalog")
    evidence = review.living_evidence(account)
    singleton_days = review._tagged_singletons(account)
    tag_available = any(row.get("status") in {"kept","tagged"} and review.verified_living_row(row,d,s)
                        for (d,s),row in evidence.items())
    phone._living_tag_exists = tag_available
    day = datetime.now().astimezone().date().isoformat()
    def finished(dex, scope):
        row = evidence.get((dex,scope), {})
        return review.verified_living_row(row, dex, scope) or (
            row.get("status") in {"absent","forms protected"} and row["at"][:10] == day)
    def wanted(dex):
        scopes = policy.living_forms[dex] or (review.ORDINARY_FORM,)
        return [scope for scope in (*scopes, review.EVENT_FORMS) if not finished(dex, scope)]
    if candidates is None:
        # Existing protected-holding records are ordering hints; an old
        # species-only positive does not prove the ordinary or event form.
        prior = review.load_verified_anchors(account)
        _, perfect = review._held_perfect(account)
        candidates = sorted(policy.living_forms, key=lambda d: (d not in policy.safe_species, d not in perfect,
                                                                  d not in prior, d))
    pending = [d for d in dict.fromkeys(candidates) if d in policy.living_forms and wanted(d)]
    if limit is not None:
        pending = pending[:max(0,limit)]
    records = await _batch_hundos(phone,account,policy,pending,evidence)
    evidence.update({(r["dex"],r["scope"]):r for r in records})
    extra = await _batch_raid_hints(phone,account,policy,pending,evidence)
    records.extend(extra)
    evidence.update({(r["dex"],r["scope"]):r for r in extra})
    async def inspect(group, scope, no_retained=False):
        nonlocal tag_available
        if len(group) > 1:
            term = review._species_term((scope,), group)
            try:
                total, _ = await _grid(phone, term, None)
            except ReadUnavailable:
                total = None
            if total == 0:
                for dex in group:
                    records.append(_living_record(account,phone,dex,scope,"absent",count=0,group=group,query=term))
                return
            if (total and total < len(group) and len(group) >= 8 and getattr(phone,"_living_android_fast",False)
                    and getattr(policy,"living_labels",None)):
                try:
                    absent = await _batch_absent(phone,account,policy,group,scope,term,total)
                except ReadUnavailable:
                    absent = []
                if absent:
                    records.extend(absent)
                    cleared = {row["dex"] for row in absent}
                    group = [d for d in group if d not in cleared]
            if total and tag_available and getattr(phone,"_living_android_fast",False):
                candidates = [d for d in group if d not in singleton_days and
                              evidence.get((d,scope),{}).get("status") != "tagging"]
                try:
                    tagged = await _batch_singletons(phone,account,policy,candidates,scope,term,total)
                except ReadUnavailable:
                    tagged = []  # No tap has been made; a smaller exact read can still succeed.
                if tagged:
                    records.extend(tagged)
                    assigned = {row["dex"] for row in tagged}
                    group = [d for d in group if d not in assigned]
            # An aggregate zero proves that *every* member lacks that trait.
            # Positive totals never identify a species/form, so exact native
            # positive confirmations are still mandatory. Subgroups inherit
            # only verified negative observations.
            if len(group) >= 4 and not no_retained:
                try:
                    # A page may show several specimens of the same
                    # species. Removing proven species exposes the remaining
                    # retained grid, often completing it in one more query.
                    while len(group) >= 4:
                        retained_term = review._species_term((scope, review.RETAINED), group)
                        count, _ = await _grid(phone, retained_term, None)
                        no_retained = count == 0
                        found = await _batch_retained(phone, account, policy, group, scope, retained_term, count)
                        if not found:
                            break
                        records.extend(found)
                        proven = {row["dex"] for row in found}
                        group = [dex for dex in group if dex not in proven]
                        if count == sum(row.get("count",1) for row in found):
                            no_retained = True
                            break
                except ReadUnavailable:
                    pass
            old_singletons = [d for d in group if d in singleton_days and scope in policy.living_forms[d]]
            if len(old_singletons) >= 4 and getattr(phone,"_living_android_fast",False):
                try:
                    confirmed = review._species_term((scope,"#dontrade"),old_singletons)
                    count,_ = await _grid(phone,confirmed,None)
                    found = await _batch_retained(phone,account,policy,old_singletons,scope,confirmed,count,
                                                  singleton_days=singleton_days)
                    records.extend(found)
                    proven = {row["dex"] for row in found}
                    group = [d for d in group if d not in proven]
                except ReadUnavailable:
                    pass
            if (total and total >= len(group) and tag_available and getattr(phone,"_living_android_fast",False)
                    and getattr(policy,"living_labels",None)):
                candidates = [d for d in group if scope in policy.living_forms[d] and d not in singleton_days
                              and evidence.get((d,scope),{}).get("status") != "tagging"]
                while len(candidates) >= 3:
                    try:
                        best = await _batch_best_tier(phone,account,policy,candidates,scope)
                    except ReadUnavailable:
                        best = []
                    if not best:
                        break
                    records.extend(best)
                    assigned = {row["dex"] for row in best}
                    group = [d for d in group if d not in assigned]
                    candidates = [d for d in candidates if d not in assigned]
            if len(group) > 3 and (total is None or total < len(group)):
                middle = len(group) // 2
                await inspect(group[:middle], scope, no_retained)
                await inspect(group[middle:], scope, no_retained)
                return
        for dex in group:
            try:
                row = await _living_scope(phone,account,dex,scope,prior=evidence.get((dex,scope),{}),
                                          commit=scope != review.EVENT_FORMS and bool(policy.living_forms[dex]),
                                          no_retained=no_retained, singleton_tag_day=singleton_days.get(dex))
                records.append(row)
                if row["status"] == "tagged":
                    tag_available = True
                    phone._living_tag_exists = True
            except ReadUnavailable as exc:
                # Leave any mutation intent in the journal so retries reconcile it.
                records.append({"account":account,"dex":dex,"scope":scope,"status":"read deferred","error":str(exc)})
    scopes = {}
    for dex in pending:
        for scope in wanted(dex):
            scopes.setdefault(scope, []).append(dex)
    for scope, numbers in scopes.items():
        for start in range(0, len(numbers), 32):
            await inspect(numbers[start:start + 32], scope)
    return records
