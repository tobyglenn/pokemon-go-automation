#!/usr/bin/env python3
"""Scan a designated phone's Living Dex (1-1025) and tag sole copies `dontrade`.

Adaptive binary range scanning avoids 1,025 sequential species searches:
each block (default 16 Pokédex numbers) is queried once without `!dontrade`:
- `0` -> skip the whole block immediately.
- `1` -> record `(lo, hi)` directly (pigeonhole proof: that single Pokémon is
  guaranteed to be the sole copy of its species).
- `>= 2` -> split `[lo..mid]` and `[mid+1..hi]`, verify `left + right == parent`
  (retrying on OCR mismatch so multi-copy species are never misclassified as 1),
  and stop at single-species `lo == hi` with `count >= 2`.

Before tagging, each candidate range is checked again on the live handset.
Verified ranges are grouped for multi-select tagging, with detail-page tagging
for remaining Favorited or Buddy matches.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageOps  # noqa: E402

from sources import (  # noqa: E402
    clear_pokemon,
    collect_trades,
    config_paths,
    gbl_home_recovery as home,
    gbl_vision,
    pokemon_fleet,
    raid_inventory,
    raid_scan_android,
    starter,
    trade_local,
    vision_ocr,
)

OCRBox = clear_pokemon.OCRBox
LIVING_DEX_TAG = "dontrade"
DEFAULT_MIN_DEX = 1
DEFAULT_MAX_DEX = 1025
DEFAULT_BLOCK_SIZE = 16
DEFAULT_MAX_QUERY_CHARS = 700
SEARCH_CLEAR_GLYPHS = frozenset("q9aog@")
_CP_TOKEN_ANY = re.compile(r"^cp\s*(\d{1,5})$", re.I)


def default_target_device() -> str:
    """Resolve the default Living-Dex phone from private config without hardcoding names."""
    with contextlib.suppress(Exception):
        receiver = str(collect_trades.load_config().get("receiver", "")).strip()
        if receiver:
            return receiver
    with contextlib.suppress(Exception):
        cfg = json.loads(config_paths.default_config("special-trade.json").read_text())
        receiver = str(cfg.get("receiver", "")).strip()
        if receiver:
            return receiver
    return ""


def checkpoint_path(device: str) -> Path:
    return config_paths.state_dir() / "living-dex" / f"{device}-scan.json"


def tagged_log_path(device: str) -> Path:
    return config_paths.state_dir() / "living-dex" / f"{device}-tagged.jsonl"


def range_term(lo: int, hi: int) -> str:
    """Format a Pokédex range query for storage scanning (zero-pad singletons for OCR)."""
    if lo == hi:
        return f"{int(lo):03d}"
    return f"{int(lo)}-{int(hi)}"


def raw_range(lo: int, hi: int) -> str:
    """Compact Pokémon GO range syntax without zero-padding (for chunked tagging)."""
    return str(int(lo)) if lo == hi else f"{int(lo)}-{int(hi)}"


def count_visible_cp_tiles(boxes: Sequence[object], min_y: float) -> int:
    """Count all visible CP grid tiles (`CP >= 10`), including Level-1 Pokemon."""
    total = 0
    for box in boxes:
        text = raid_scan_android.normalise(box.text).strip()
        match = _CP_TOKEN_ANY.match(text)
        if not match:
            continue
        cp_val = int(match.group(1))
        cy = getattr(box, "center_y", getattr(box, "cy", 0))
        if cp_val >= 10 and cy > min_y:
            total += 1
    return total


def _matches_compact_range(boxes: Sequence[object], wanted: str, ceiling_y: float) -> bool:
    for box in boxes:
        raw_text = getattr(box, "text", "")
        if "(" in raw_text or ")" in raw_text:
            continue
        top_y = getattr(box, "y", 0)
        if top_y > ceiling_y:
            continue
        seen = clear_pokemon._compact_search(raw_text)
        if not seen:
            continue
        candidates = {seen}
        if seen[:1] in SEARCH_CLEAR_GLYPHS:
            candidates.add(seen[1:])
        if wanted in candidates:
            return True
    return False


def range_field_shows(
    image: Image.Image,
    boxes: Sequence[object],
    term: str,
    *,
    full_height: float | None = None,
) -> bool:
    """Verify the search box shows `term`, preserving hyphens and handling cropped OCR."""
    h = float(full_height if full_height is not None else image.height)
    wanted = clear_pokemon._compact_search(term)
    if not wanted:
        return False
    if _matches_compact_range(boxes, wanted, h * 0.35):
        return True

    counter = next(
        (
            b for b in sorted(boxes, key=lambda x: x.y)
            if b.y < h * 0.22 and re.search(r"\((\d{1,5})\)", raid_scan_android.normalise(b.text))
        ),
        None,
    )
    if counter is None:
        return False
    toggle = next((b for b in boxes if "evolution" in raid_scan_android.normalise(b.text).lower()), None)
    counter_bottom = getattr(counter, "bottom", counter.y + getattr(counter, "h", getattr(counter, "height", 0)))
    counter_h = getattr(counter, "h", getattr(counter, "height", 20))
    top = counter_bottom + counter_h * 0.3
    bottom = (
        toggle.y - getattr(toggle, "h", getattr(toggle, "height", 20)) * 0.3
        if toggle is not None
        else top + h * 0.07
    )
    if bottom - top < 8 or bottom > image.height:
        return False
    for left in (0.22, 0.18, 0.25):
        strip = image.crop((int(image.width * left), int(top), int(image.width * 0.80), int(bottom)))
        strip = ImageOps.expand(
            strip.resize((strip.width * 3, strip.height * 3)),
            border=60,
            fill=strip.getpixel((2, 2)),
        )
        if _matches_compact_range(vision_ocr.ocr(strip), wanted, float("inf")):
            return True
    return False


def scan_range_tree(
    lo: int,
    hi: int,
    query_fn: Callable[[int, int], int],
    *,
    expected_parent: int | None = None,
    max_retries: int = 3,
) -> list[tuple[int, int]]:
    """Adaptively find all disjoint `(lo, hi)` ranges containing exactly 1 Pokémon."""
    parent = int(query_fn(lo, hi)) if expected_parent is None else int(expected_parent)
    if parent == 0:
        return []
    if parent == 1:
        if expected_parent is not None:
            confirmed = int(query_fn(lo, hi))
            if confirmed != 1:
                parent = confirmed
            else:
                return [(lo, hi)]
        if parent == 1:
            return [(lo, hi)]
    if lo == hi:
        return []

    mid = (lo + hi) // 2
    last_left = last_right = -1
    for attempt in range(max_retries):
        left = int(query_fn(lo, mid))
        right = int(query_fn(mid + 1, hi))
        last_left, last_right = left, right
        if left + right == parent:
            out: list[tuple[int, int]] = []
            if left == 1:
                out.append((lo, mid))
            elif left >= 2 and lo < mid:
                out.extend(scan_range_tree(lo, mid, query_fn, expected_parent=left, max_retries=max_retries))
            if right == 1:
                out.append((mid + 1, hi))
            elif right >= 2 and (mid + 1) < hi:
                out.extend(scan_range_tree(mid + 1, hi, query_fn, expected_parent=right, max_retries=max_retries))
            return out
        if attempt + 1 < max_retries:
            parent = int(query_fn(lo, hi))
    if last_left + last_right > parent:
        raise RuntimeError(
            f"Range [{lo}-{hi}] split {last_left}+{last_right} > {parent}; "
            "SHOW EVOLUTIONARY LINE may be toggled ON — turn it OFF and retry."
        )
    raise RuntimeError(
        f"Range [{lo}-{hi}] OCR count mismatch after {max_retries} retries: "
        f"parent={parent}, split={last_left}+{last_right}"
    )


def build_tag_chunks(
    sole_ranges: Sequence[tuple[int, int]],
    *,
    tag: str = LIVING_DEX_TAG,
    max_chars: int = DEFAULT_MAX_QUERY_CHARS,
) -> list[tuple[str, str, int]]:
    """Group sole-copy `(lo, hi)` ranges into `<= max_chars` `(plain_expr, tagged_expr, count)` chunks."""
    ordered = sorted((int(lo), int(hi)) for lo, hi in sole_ranges)
    if not ordered:
        return []
    prefix = f"!{tag}&"
    chunks: list[tuple[str, str, int]] = []
    cur_tokens: list[str] = []
    cur_len = len(prefix)

    def flush() -> None:
        if not cur_tokens:
            return
        plain = ",".join(cur_tokens)
        chunks.append((plain, prefix + plain, len(cur_tokens)))
        cur_tokens.clear()

    for lo, hi in ordered:
        tok = raw_range(lo, hi)
        added = len(tok) + (1 if cur_tokens else 0)
        if cur_tokens and cur_len + added > max_chars:
            flush()
            cur_len = len(prefix)
        cur_tokens.append(tok)
        cur_len += len(tok) + (1 if len(cur_tokens) > 1 else 0)
    flush()
    return chunks


def dex_blocks(min_dex: int, max_dex: int, block_size: int) -> list[tuple[int, int]]:
    if block_size < 1:
        raise ValueError("block size must be positive")
    out: list[tuple[int, int]] = []
    cur = min_dex
    while cur <= max_dex:
        end = min(max_dex, cur + block_size - 1)
        out.append((cur, end))
        cur = end + 1
    return out


def _load_scan_checkpoint(path: Path) -> dict:
    if not path.is_file():
        return {"blocks": {}}
    with contextlib.suppress(Exception):
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("blocks"), dict):
            return data
    return {"blocks": {}}


def _save_scan_checkpoint(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(path)


def _record_tagged_chunk(device: str, tag: str, plain: str, expected: int, remaining: int) -> None:
    path = tagged_log_path(device)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "device": device,
        "tag": tag,
        "expected": expected,
        "remaining_after": remaining,
        "query_chars": len(plain),
        "plain": plain,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


class LivingDexScanner:
    """Fast Android storage range counter keeping the keyboard open and cropping below it."""

    def __init__(self, phone: raid_scan_android.AndroidPhone, *, verbose: bool = True) -> None:
        self.phone = phone
        self.scanner = raid_scan_android.StorageScanner(phone, verbose=verbose)
        self.verbose = verbose
        self.query_count = 0

    def _clear_short_or_long(self, first_time: bool) -> None:
        if first_time:
            self.phone.clear_field(settle=0.25)
            return
        keys = ["KEYCODE_MOVE_END"] + ["KEYCODE_DEL"] * 20
        self.phone._run("shell", "input", "keyevent", *keys, timeout=20)
        time.sleep(0.25)

    def _fast_read_top(self) -> tuple[Image.Image, list[vision_ocr.TextBox], int]:
        full = self.phone.screenshot()
        crop_h = int(full.height * 0.58)
        crop = full.crop((0, 0, full.width, crop_h))
        return crop, vision_ocr.ocr(crop), full.height

    def query_range(self, lo: int, hi: int) -> int:
        term = range_term(lo, hi)
        self.query_count += 1
        for attempt in range(4):
            if not self.scanner._field_focused:
                self.scanner.ensure_grid()
                image, boxes = self.phone.read()
                base = raid_scan_android.search_bar_fraction(boxes, image)
                if base is None:
                    raise raid_scan_android.RaidScanError(f"{self.phone.label}: cannot locate storage search field")
                self.phone.tap((base[0], max(0.02, base[1] + (attempt * 0.014 if attempt % 2 else 0.0))), settle=0.9)
                self._clear_short_or_long(first_time=True)
            else:
                self._clear_short_or_long(first_time=(attempt > 0))

            self.phone.type_text(term, settle=0.80)
            crop, boxes, full_h = self._fast_read_top()
            if raid_scan_android.classify(boxes) == "detail":
                self.phone.back(settle=1.2)
                self.scanner._field_focused = False
                continue

            count = raid_scan_android.parse_result_count(boxes, full_h)
            if count is not None and range_field_shows(crop, boxes, term, full_height=full_h):
                cp_tiles = count_visible_cp_tiles(boxes, full_h * 0.22)
                if count == 0 and cp_tiles > 0:
                    # Visual sanity caught a misread `(0)` when tiles are visible; retry.
                    self.scanner._field_focused = False
                    continue
                if count == 1 and cp_tiles >= 2:
                    self.scanner._field_focused = False
                    continue
                if count == 1 and cp_tiles == 0:
                    time.sleep(0.6)
                    crop2, boxes2, full_h2 = self._fast_read_top()
                    count2 = raid_scan_android.parse_result_count(boxes2, full_h2)
                    cp_tiles2 = count_visible_cp_tiles(boxes2, full_h2 * 0.22)
                    if count2 == 0 and cp_tiles2 == 0:
                        self.scanner._field_focused = True
                        return 0
                    if count2 == 1 and cp_tiles2 >= 2:
                        self.scanner._field_focused = False
                        continue
                self.scanner._field_focused = True
                return count

            self.scanner._field_focused = False
            if self.verbose:
                print(f"[{self.phone.label}] retry query '{term}' (count={count})", flush=True)
        raise raid_scan_android.RaidScanError(f"{self.phone.label}: range query '{term}' could not be verified")


async def _prepare_storage_screen(spec: pokemon_fleet.DeviceSpec, shots: Path) -> None:
    """Bring `spec` cleanly onto Pokémon storage and clear any active search pill."""
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        phone = starter.Phone(spec.name, target, shots)
        try:
            image, boxes = await starter.open_storage(phone)
            await clear_pokemon._clear_storage_pill_if_active(phone, image, boxes)
        finally:
            await target.close()


def run_living_dex_scan(
    spec: pokemon_fleet.DeviceSpec,
    *,
    min_dex: int,
    max_dex: int,
    block_size: int,
    resume: bool,
    shots: Path,
) -> list[tuple[int, int]]:
    """Run the adaptive scan synchronously on Android (with lock and checkpoint)."""
    ckpt_file = checkpoint_path(spec.name)
    data = _load_scan_checkpoint(ckpt_file) if resume else {"blocks": {}}
    blocks = dex_blocks(min_dex, max_dex, block_size)

    pending_blocks = [b for b in blocks if f"{b[0]}-{b[1]}" not in data["blocks"]]
    if pending_blocks:
        asyncio.run(_prepare_storage_screen(spec, shots))
        with pokemon_fleet.acquire_device_locks([spec]):
            phone = raid_scan_android.AndroidPhone(spec.identifier, spec.name)
            scanner = LivingDexScanner(phone)
            try:
                started = time.monotonic()
                for idx, (lo, hi) in enumerate(blocks, 1):
                    key = f"{lo}-{hi}"
                    if key in data["blocks"]:
                        continue
                    found = scan_range_tree(lo, hi, scanner.query_range)
                    if len(found) == 1 and found[0] == (lo, hi):
                        # Re-confirm top-level sole-copy hit if visual CP wasn't counted;
                        # splitting once guarantees left+right == 1 even on weird OCR frames.
                        mid = (lo + hi) // 2
                        if lo < hi:
                            left = scanner.query_range(lo, mid)
                            right = scanner.query_range(mid + 1, hi)
                            if left + right != 1:
                                found = scan_range_tree(lo, hi, scanner.query_range)
                    data["blocks"][key] = [[a, b] for a, b in found]
                    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                    _save_scan_checkpoint(ckpt_file, data)
                    if idx % 4 == 0 or found or idx == len(blocks):
                        elapsed = time.monotonic() - started
                        sole_total = sum(len(v) for v in data["blocks"].values())
                        print(
                            f"[living-dex] [{idx}/{len(blocks)}] {key}: +{len(found)} "
                            f"(total sole={sole_total}, queries={scanner.query_count}, {elapsed:.0f}s)",
                            flush=True,
                        )
            finally:
                with contextlib.suppress(Exception):
                    phone.back(settle=0.8)  # dismiss keyboard before opening tagging session

    sole: list[tuple[int, int]] = []
    for lo, hi in blocks:
        for pair in data["blocks"].get(f"{lo}-{hi}", []):
            sole.append((int(pair[0]), int(pair[1])))
    return sorted(sole)


async def _select_and_confirm_tag(phone: starter.Phone, tag: str) -> bool:
    """In the 'Tag N Pokémon' sheet, select `tag` (scrolling if needed) and tap DONE."""
    image, boxes = await phone.read()
    if starter.find_box(boxes, "transfer", exact=True) is not None:
        return False
    wanted = tag.lower()
    row = None
    for _ in range(5):
        row = next(
            (b for b in boxes if gbl_vision.normalize(b.text).lstrip("•· ").strip() == wanted),
            None,
        )
        if row is not None:
            break
        x = image.width // 2
        await phone.swipe([x, int(image.height * 0.58)], [x, int(image.height * 0.30)], image, 0.6, scroll=True)
        await asyncio.sleep(1.4)
        image, boxes = await phone.read()
    if row is None:
        return False
    await phone.tap_box(row, image)
    await asyncio.sleep(1.2)
    image, boxes = await phone.read()
    done = starter.find_box(boxes, "done", exact=True)
    if done is None:
        return False
    await phone.tap_box(done, image)
    await asyncio.sleep(2.4)
    return True


async def _dismiss_notice_if_present(
    phone: starter.Phone,
    image: Image.Image,
    boxes: Sequence[OCRBox],
) -> tuple[Image.Image, list[OCRBox]]:
    """Dismiss a Pokémon GO startup safety or Weather warning card if visible."""
    notice = gbl_vision.safety_notice_point(boxes, image)
    if notice is None:
        return image, list(boxes)
    phone.say("dismissing safety/weather warning")
    await phone.tap(notice, image)
    await asyncio.sleep(1.5)
    img, bx = await phone.read()
    return img, list(bx)


async def _tag_one_from_detail(phone: starter.Phone, tag: str) -> bool:
    """Open tile 0 on grid, tap menu -> TAG (exact) -> `tag` -> DONE, then close detail."""
    image, boxes = await phone.read()
    image, boxes = await _dismiss_notice_if_present(phone, image, boxes)
    tiles = clear_pokemon.safe_grid_tiles(image, boxes)
    if not tiles:
        return False
    tile = tiles[0]
    await phone.tap([tile.center_x, tile.center_y + int(image.height * clear_pokemon.TILE_RISE)], image)
    ok, image, boxes = await phone.settle(clear_pokemon.detail_page, timeout=8.0)
    if not ok:
        image, boxes = await _dismiss_notice_if_present(phone, image, boxes)
        await clear_pokemon.close_detail(phone)
        return False
    await phone.tap([int(image.width * clear_pokemon.MENU_POINT[0]), int(image.height * clear_pokemon.MENU_POINT[1])], image)
    ok_menu, image, boxes = await phone.settle(
        lambda _i, b: starter.find_box(b, "appraise", exact=True) is not None
        and starter.find_box(b, "tag", exact=True) is not None,
        timeout=6.0,
    )
    if not ok_menu:
        await _dismiss_notice_if_present(phone, image, boxes)
        await clear_pokemon.close_detail(phone)
        return False
    tag_btn = starter.find_box(boxes, "tag", exact=True)
    assert tag_btn is not None and gbl_vision.normalize(tag_btn.text) == "tag"
    await phone.tap_box(tag_btn, image)
    await asyncio.sleep(1.8)
    ok_sheet = await _select_and_confirm_tag(phone, tag)
    await clear_pokemon.close_detail(phone)
    return ok_sheet


async def _tagging_grid(
    phone: starter.Phone,
    term: str,
    image: Image.Image | None = None,
    boxes: Sequence[OCRBox] | None = None,
) -> tuple[int, Image.Image, list[OCRBox]]:
    count, image, boxes, _ = await clear_pokemon._apply_and_verify_query(
        phone, term, image, boxes, require_exact=True
    )
    if not clear_pokemon.evolutionary_line_off(image, boxes):
        raise clear_pokemon.ClearSafetyError(
            f"{phone.name}: turn off Show Evolutionary Line before tagging Living Dex copies"
        )
    return count, image, list(boxes)


async def _read_or_apply_tagged_count(
    phone: starter.Phone,
    tagged_expr: str,
) -> tuple[int, Image.Image, list[OCRBox]]:
    """Use the live filtered grid count if still active; re-apply `tagged_expr` only if needed."""
    image, boxes = await phone.read()
    image, boxes = await _dismiss_notice_if_present(phone, image, boxes)
    cur_count = clear_pokemon.result_count(boxes, image.height)
    if (cur_count is not None and clear_pokemon.evolutionary_line_off(image, boxes)
            and clear_pokemon.search_field_matches_query(boxes, tagged_expr, image.height)):
        return cur_count, image, list(boxes)
    return await _tagging_grid(phone, tagged_expr, image, boxes)


async def tag_sole_chunks(
    spec: pokemon_fleet.DeviceSpec,
    chunks: Sequence[tuple[str, str, int]],
    *,
    tag: str,
    dry_run: bool,
    shots: Path,
) -> tuple[int, int]:
    """Verify each sole-copy chunk and tag untagged matches `tag`. Returns `(tagged_chunks, remaining)`."""
    if not chunks:
        return 0, 0
    total_tagged_chunks = 0
    total_remaining = 0
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        phone = starter.Phone(spec.name, target, shots, dry_run)
        try:
            image, boxes = await starter.open_storage(phone)
            for idx, (plain_expr, tagged_expr, expected_count) in enumerate(chunks, 1):
                if expected_count > 1:
                    # A stale range can lose its singleton while another gains
                    # a second copy. The pooled total cannot detect that swap.
                    for candidate in plain_expr.split(","):
                        count, image, boxes = await _tagging_grid(phone, candidate, image, boxes)
                        if count != 1:
                            raise clear_pokemon.ClearSafetyError(
                                f"{spec.name}: saved range {candidate} now matches {count}; "
                                "rescan with --no-resume before tagging"
                            )
                total_count, image, boxes = await _tagging_grid(phone, plain_expr, image, boxes)
                if total_count != expected_count:
                    raise clear_pokemon.ClearSafetyError(
                        f"{spec.name}: chunk {idx}/{len(chunks)} expected {expected_count} total "
                        f"matches for plain query, saw {total_count}; aborting chunk for safety"
                    )

                untagged, image, boxes = await _tagging_grid(phone, tagged_expr, image, boxes)
                print(
                    f"[living-dex] chunk {idx}/{len(chunks)}: {expected_count} sole copies, "
                    f"{untagged} untagged (!{tag})",
                    flush=True,
                )
                if untagged == 0:
                    total_tagged_chunks += 1
                    _record_tagged_chunk(spec.name, tag, plain_expr, expected_count, 0)
                    continue
                if untagged > expected_count:
                    raise clear_pokemon.ClearSafetyError(
                        f"{spec.name}: chunk {idx} saw {untagged} untagged > {expected_count} expected"
                    )
                if dry_run:
                    total_remaining += untagged
                    continue

                batch_ran = False
                if untagged > 1 and not phone.ios:
                    nonfav_expr = f"!favorite&{tagged_expr}"
                    nonfav_count, image, boxes = await _tagging_grid(phone, nonfav_expr, image, boxes)
                    batch_ran = True
                    tiles = clear_pokemon.safe_grid_tiles(image, boxes) if nonfav_count > 0 else []
                    if tiles:
                        tile = tiles[0]
                        px, py = int(tile.center_x), int(tile.center_y + int(image.height * clear_pokemon.TILE_RISE))
                        await asyncio.to_thread(phone.target._adb, "shell", f"input swipe {px} {py} {px} {py} 1000")
                        ok_ms, ms_img, ms_boxes = await trade_local.settle(
                            phone,
                            lambda _i, b: starter.find_box(b, "tag", exact=True) is not None,
                            timeout=6.0,
                        )
                        if ok_ms:
                            if nonfav_count > 1:
                                sel_all = starter.find_box(ms_boxes, "select all", exact=True)
                                if sel_all is not None:
                                    await phone.tap_box(sel_all, ms_img)
                                    await asyncio.sleep(1.5)
                                    ms_img, ms_boxes = await phone.read()
                            tag_btn = starter.find_box(ms_boxes, "tag", exact=True)
                            if tag_btn is not None and gbl_vision.normalize(tag_btn.text) == "tag":
                                await phone.tap_box(tag_btn, ms_img)
                                await asyncio.sleep(1.5)
                                await _select_and_confirm_tag(phone, tag)

                # Confirm remaining count on `!tag&<plain_expr>` (re-applying after `!favorite` pass so overflowed right-edge match cannot mask Favorites).
                if batch_ran:
                    remaining, image, boxes = await _tagging_grid(phone, tagged_expr)
                else:
                    remaining, image, boxes = await _read_or_apply_tagged_count(phone, tagged_expr)
                print(
                    f"[living-dex] chunk {idx}/{len(chunks)}: {remaining} remaining for detail-page tagging",
                    flush=True,
                )
                stalls = 0
                while remaining > 0 and stalls < 3:
                    prev_remaining = remaining
                    ok_one = await _tag_one_from_detail(phone, tag)
                    remaining, image, boxes = await _read_or_apply_tagged_count(phone, tagged_expr)
                    print(f"[living-dex] tagged via detail; {remaining} remaining", flush=True)
                    if not ok_one or remaining >= prev_remaining:
                        stalls += 1
                    else:
                        stalls = 0

                _record_tagged_chunk(spec.name, tag, plain_expr, expected_count, remaining)
                total_remaining += remaining
                if remaining == 0:
                    total_tagged_chunks += 1
                else:
                    print(
                        f"[living-dex] WARNING: chunk {idx}/{len(chunks)} left {remaining} untagged",
                        flush=True,
                    )
            await clear_pokemon._clear_storage_pill_if_active(phone, image, boxes)
            await phone.go_home()
        finally:
            await target.close()
    return total_tagged_chunks, total_remaining


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", default=default_target_device(), help="target fleet device (Living Dex only)")
    parser.add_argument("--tag", default=LIVING_DEX_TAG, help=f"tag to apply (default: {LIVING_DEX_TAG})")
    parser.add_argument("--min-dex", type=int, default=DEFAULT_MIN_DEX)
    parser.add_argument("--max-dex", type=int, default=DEFAULT_MAX_DEX)
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument("--max-query-chars", type=int, default=DEFAULT_MAX_QUERY_CHARS)
    parser.add_argument("--plan", action="store_true", help="print plan and saved checkpoint summary without touching phone")
    parser.add_argument("--dry-run", action="store_true", help="scan/verify counts only; apply no tags")
    parser.add_argument("--scan-only", action="store_true", help="only run the Living Dex scan and save checkpoint")
    parser.add_argument("--tag-only", action="store_true", help="only tag from saved scan checkpoint")
    parser.add_argument("--no-resume", action="store_true", help="discard prior scan checkpoint and scan from start")
    parser.add_argument("--shots", type=Path, default=config_paths.state_dir() / "living-dex" / "shots")
    args = parser.parse_args(argv)
    if args.block_size < 1:
        parser.error("--block-size must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.device:
        print("[living-dex] specify --device (or configure receiver in private collect-trades.json)", flush=True)
        return 1
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    if args.device not in fleet.devices:
        print(f"[living-dex] unknown device: {args.device}", flush=True)
        return 1
    spec = fleet.devices[args.device]
    ckpt_file = checkpoint_path(spec.name)

    if args.plan:
        data = _load_scan_checkpoint(ckpt_file)
        sole = sorted((int(p[0]), int(p[1])) for rows in data["blocks"].values() for p in rows)
        chunks = build_tag_chunks(sole, tag=args.tag, max_chars=args.max_query_chars)
        print(
            f"[living-dex] plan: device={spec.name} ({spec.platform}), dex={args.min_dex}..{args.max_dex} "
            f"(block={args.block_size}), tag={args.tag!r}, "
            f"checkpoint={len(data['blocks'])} blocks / {len(sole)} sole copies / {len(chunks)} chunks",
            flush=True,
        )
        return 0

    if spec.platform != "android":
        print(f"[living-dex] {spec.name} is {spec.platform}; Android scanner required", flush=True)
        return 1

    if args.tag_only:
        data = _load_scan_checkpoint(ckpt_file)
        sole = sorted((int(p[0]), int(p[1])) for rows in data["blocks"].values() for p in rows)
    else:
        sole = run_living_dex_scan(
            spec,
            min_dex=args.min_dex,
            max_dex=args.max_dex,
            block_size=args.block_size,
            resume=not args.no_resume,
            shots=args.shots,
        )

    chunks = build_tag_chunks(sole, tag=args.tag, max_chars=args.max_query_chars)
    print(
        f"[living-dex] scan result on {spec.name}: {len(sole)} sole-copy species/ranges -> "
        f"{len(chunks)} query chunk(s)",
        flush=True,
    )
    if args.scan_only:
        for idx, (_plain, tagged, cnt) in enumerate(chunks, 1):
            print(f"  chunk {idx}: ({cnt}) {tagged}", flush=True)
        return 0

    tagged_chunks, remaining = asyncio.run(
        tag_sole_chunks(spec, chunks, tag=args.tag, dry_run=args.dry_run, shots=args.shots)
    )
    print(
        f"[living-dex] done on {spec.name}: chunks_ok={tagged_chunks}/{len(chunks)}, "
        f"untagged_remaining={remaining}{' [dry-run]' if args.dry_run else ''}",
        flush=True,
    )
    return 0 if (args.dry_run or remaining == 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
