#!/usr/bin/env python3
"""Claim Pokémon encounters from a GO Pass / Event Pass screen and catch them.  Reached through `catch.py`."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from . import (
    catch_awarded_android,
    claim_research_android,
    gbl_vision,
)

# Text markers for the GO Pass screen (both Basic and Deluxe, active and ended,
# top-of-page and scrolled where the top title banner collapses into the sticky bar).
PASS_HEADER_PHRASES = (
    "time left to claim rewards",
    "go pass",
)
POKEMON_APPEARS = re.compile(r"\bpokemon appears\b")
CLAIM_ALL = re.compile(r"^claim all$")
STORAGE_FULL = re.compile(r"\bpokemon (?:storage|box) is full\b|\bstorage is full\b")
BAG_FULL = re.compile(r"\bitem bag is full\b|\bbag is full\b")
# Labels that sit on gold/orange decorative chrome rather than claimable item tiles.
CHROME_LABELS = (
    "major milestone",
    "basic",
    "deluxe",
    "claim all",
    "go pass",
    "tier ",
    "pass bonuses",
    "event ended",
    "rank",
    "pokemon appears",
)

OCR_WIDTH = 480
OCR_CONFIDENCE = 0.5

# Orange density thresholds on the downscaled 480px frame. Measured across three
# Android resolutions on GO Pass Deluxe: Dynamax Trails: unclaimed tiles read
# 0.56-0.70 inside the label box and 0.91-1.00 in the tile body below; claimed
# grey tiles read 0.00.
TILE_LABEL_ORANGE_MIN = 0.30
TILE_BODY_ORANGE_MIN = 0.40
CLAIM_ALL_ORANGE_MIN = 0.30
ITEM_TILE_BODY_ORANGE_MIN = 0.35

# Bottom floating controls (`CLAIM ALL` pill on the left, teal `X` button in the
# middle) sit below ~0.86h. Only tap tiles whose tap point stays clearly above
# them; tiles clipped behind those controls are scrolled up first.
SAFE_BOTTOM_FRACTION = 0.84
TILE_TAP_Y_OFFSET_480 = 12.0
TILE_OVERLAP_MIN = 0.50

# Top-right view toggle `[ list | pokeball ]` switches between all ranks and
# "Showing Claimable Rewards". Measured teal pixel counts on 480px frames:
# active half ~1000px vs inactive half ~200-350px.
TOGGLE_MIN_PIXELS = 150
TOGGLE_MIN_WIDTH = 36
TOGGLE_MIN_HEIGHT = 14
TOGGLE_DOMINANCE_RATIO = 1.5

# Timings. Dynamax encounters play a ~3-4s stadium entrance before the ball and
# CP plate settle, and the "Showing Claimable Rewards" toast lingers ~1.8s.
CLAIM_SETTLE = 2.5
TOGGLE_SETTLE = 2.2
SCREEN_SETTLE = 1.2
CLAIM_POLL = 1.0
TAP_REJECT_GRACE = 5.0
RETURN_WAIT = 20.0
POST_GONE_GRACE = 4.0


@dataclass(frozen=True)
class PassTile:
    """One unclaimed orange POKÉMON APPEARS! tile, in native screen pixels."""

    label: str
    point: list[int]
    left: int
    top: int
    right: int
    bottom: int
    column: str

    def overlaps(self, other: "PassTile") -> bool:
        """True only when both vertical and horizontal spans overlap.

        BASIC and DELUXE tiles share a row, so vertical overlap alone would
        conflate the left and right columns.
        """
        v_span = min(self.bottom, other.bottom) - max(self.top, other.top)
        v_height = min(self.bottom - self.top, other.bottom - other.top)
        h_span = min(self.right, other.right) - max(self.left, other.left)
        h_width = min(self.right - self.left, other.right - other.left)
        return (
            v_height > 0
            and h_width > 0
            and (v_span / v_height) >= TILE_OVERLAP_MIN
            and (h_span / h_width) >= TILE_OVERLAP_MIN
        )


@dataclass(frozen=True)
class PassScreen:
    """Parsed state of one GO Pass frame."""

    tiles: list[PassTile]
    all_tiles_count: int
    claim_all_point: list[int] | None
    toggle_state: str | None
    toggle_claimable_point: list[int] | None
    unclaimed_non_pokemon: bool


def _orange_fraction(
    mask: Image.Image, x0: float, y0: float, x1: float, y1: float
) -> float:
    width, height = mask.size
    left = max(0, min(width, int(x0)))
    right = max(0, min(width, int(x1)))
    top = max(0, min(height, int(y0)))
    bottom = max(0, min(height, int(y1)))
    if right <= left or bottom <= top:
        return 0.0
    cropped = mask.crop((left, top, right, bottom)).tobytes()
    if not cropped:
        return 0.0
    return cropped.count(255) / len(cropped)


def _detect_filter_toggle(
    source: Image.Image, header_top: int, header_bottom: int, scale: float
) -> tuple[str | None, list[int] | None]:
    """Locate the top-right `[ list | pokeball ]` toggle and read which half is active."""
    width, height = source.size
    x_start = int(width * 0.70)
    x_end = int(width * 0.96)
    y_start = max(0, min(height, header_top))
    y_end = max(y_start, min(height, header_bottom))
    if y_end - y_start < TOGGLE_MIN_HEIGHT or x_end <= x_start:
        return None, None

    rgb = source.convert("RGB")
    pixels = rgb.load()
    min_x, max_x = width, -1
    min_y, max_y = height, -1
    points: list[tuple[int, int]] = []
    for y in range(y_start, y_end):
        for x in range(x_start, x_end):
            r, g, b = pixels[x, y]
            if r < 60 and g > 100 and b > 100 and (b - r) > 50:
                points.append((x, y))
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

    if (
        len(points) < TOGGLE_MIN_PIXELS
        or (max_x - min_x) < TOGGLE_MIN_WIDTH
        or (max_y - min_y) < TOGGLE_MIN_HEIGHT
    ):
        return None, None

    mid_x = (min_x + max_x) / 2.0
    left_teal = sum(1 for x, _y in points if x < mid_x)
    right_teal = len(points) - left_teal
    state: str | None = None
    if right_teal >= left_teal * TOGGLE_DOMINANCE_RATIO:
        state = "claimable"
    elif left_teal >= right_teal * TOGGLE_DOMINANCE_RATIO:
        state = "all"

    tap_x = round((min_x + (max_x - min_x) * 0.75) * scale)
    tap_y = round(((min_y + max_y) / 2.0) * scale)
    return state, [tap_x, tap_y]


def read_pass_screen(image: Image.Image) -> PassScreen | None:
    """Parse a GO Pass / Event Pass screen, or return None if this frame is not one."""
    scale = 1.0
    source = image
    if image.width > OCR_WIDTH:
        scale = image.width / OCR_WIDTH
        source = image.resize((OCR_WIDTH, max(1, round(image.height / scale))))
    try:
        boxes = gbl_vision.recognize(source)
    except gbl_vision.VisionOCRError:
        return None

    readable = [
        (box, gbl_vision.normalize(box.text))
        for box in boxes
        if box.confidence >= OCR_CONFIDENCE
    ]
    if not readable:
        return None

    joined = " ".join(norm for _box, norm in readable)
    has_pass_header = any(phrase in joined for phrase in PASS_HEADER_PHRASES)
    has_basic = any(norm == "basic" for _box, norm in readable)
    has_deluxe = any(norm == "deluxe" for _box, norm in readable)
    has_rank = any(norm == "rank" for _box, norm in readable)
    has_pokemon_label = any(POKEMON_APPEARS.search(norm) for _box, norm in readable)

    is_pass = (
        has_pass_header and (has_basic or has_deluxe or has_rank or has_pokemon_label)
    ) or (has_basic and has_deluxe and (has_rank or has_pokemon_label))
    if not is_pass:
        return None

    mask = claim_research_android.orange_mask(source)
    width, height = source.size

    column_boxes = [box for box, norm in readable if norm in {"basic", "deluxe"}]
    if column_boxes:
        header_bottom = max(box.y + box.height for box in column_boxes) + 4.0
    else:
        header_bottom = height * 0.14

    anchor_boxes = [
        box
        for box, norm in readable
        if "time left" in norm or norm == "rank"
    ]
    if anchor_boxes:
        toggle_top = max(0, int(min(box.y for box in anchor_boxes) - 16))
    else:
        toggle_top = int(height * 0.03)
    toggle_bottom = max(toggle_top, int(header_bottom - 4))
    toggle_state, toggle_claimable_point = _detect_filter_toggle(
        source, toggle_top, toggle_bottom, scale
    )

    claim_all_box = next(
        (
            box
            for box, norm in readable
            if CLAIM_ALL.match(norm)
            and box.center_y >= height * 0.80
            and box.center_x <= width * 0.45
            and _orange_fraction(
                mask, box.x, box.y, box.x + box.width, box.y + box.height
            )
            >= CLAIM_ALL_ORANGE_MIN
        ),
        None,
    )
    claim_all_point = (
        [round(claim_all_box.center_x * scale), round(claim_all_box.center_y * scale)]
        if claim_all_box is not None
        else None
    )

    safe_bottom = height * SAFE_BOTTOM_FRACTION
    if claim_all_box is not None:
        safe_bottom = min(safe_bottom, claim_all_box.y - 10.0)

    tiles: list[PassTile] = []
    all_tiles_count = 0
    unclaimed_non_pokemon = False

    for box, norm in readable:
        if POKEMON_APPEARS.search(norm):
            if box.y < header_bottom - 6.0:
                continue
            in_orange = _orange_fraction(
                mask, box.x, box.y, box.x + box.width, box.y + box.height
            )
            body_y0 = box.y + box.height + 4.0
            body_y1 = min(float(height), box.y + box.height + 45.0)
            below_orange = (
                _orange_fraction(mask, box.x, body_y0, box.x + box.width, body_y1)
                if body_y1 > body_y0 + 5.0
                else in_orange
            )
            if in_orange < TILE_LABEL_ORANGE_MIN or below_orange < TILE_BODY_ORANGE_MIN:
                continue
            all_tiles_count += 1
            tap_y_small = box.center_y + TILE_TAP_Y_OFFSET_480
            if tap_y_small > safe_bottom:
                continue
            column = "basic" if box.center_x < width * 0.52 else "deluxe"
            tiles.append(
                PassTile(
                    label=box.text.strip(),
                    point=[round(box.center_x * scale), round(tap_y_small * scale)],
                    left=round(box.x * scale),
                    top=round(box.y * scale),
                    right=round((box.x + box.width) * scale),
                    bottom=round(min(float(height), box.y + box.height + 120.0) * scale),
                    column=column,
                )
            )
            continue

        # Detect any unclaimed non-Pokémon orange reward tile sitting in the
        # BASIC/DELUXE grid (ignoring decorative banners and chrome).
        if (
            not unclaimed_non_pokemon
            and box.y >= header_bottom
            and box.center_y <= safe_bottom
            and width * 0.15 <= box.center_x <= width * 0.92
            and norm
            and not any(chrome in norm for chrome in CHROME_LABELS)
        ):
            in_orange = _orange_fraction(
                mask, box.x, box.y, box.x + box.width, box.y + box.height
            )
            body_y0 = box.y + box.height + 4.0
            body_y1 = min(float(height), box.y + box.height + 36.0)
            below_orange = (
                _orange_fraction(mask, box.x, body_y0, box.x + box.width, body_y1)
                if body_y1 > body_y0 + 5.0
                else in_orange
            )
            if in_orange >= TILE_LABEL_ORANGE_MIN and below_orange >= ITEM_TILE_BODY_ORANGE_MIN:
                unclaimed_non_pokemon = True

    row_bucket = max(1, round(image.height * 0.04))
    tiles.sort(key=lambda tile: (tile.top // row_bucket, tile.left))
    return PassScreen(
        tiles=tiles,
        all_tiles_count=all_tiles_count,
        claim_all_point=claim_all_point,
        toggle_state=toggle_state,
        toggle_claimable_point=toggle_claimable_point,
        unclaimed_non_pokemon=unclaimed_non_pokemon,
    )


def is_gopass_screen(image: Image.Image) -> bool:
    return read_pass_screen(image) is not None


def _dialog_notice(image: Image.Image) -> str | None:
    """Return 'storage_full', 'bag_full', or None if a blocking notice is showing."""
    source = image
    if image.width > OCR_WIDTH:
        scale = image.width / OCR_WIDTH
        source = image.resize((OCR_WIDTH, max(1, round(image.height / scale))))
    try:
        boxes = gbl_vision.recognize(source)
    except gbl_vision.VisionOCRError:
        return None
    joined = " ".join(
        gbl_vision.normalize(box.text)
        for box in boxes
        if box.confidence >= OCR_CONFIDENCE
    )
    if STORAGE_FULL.search(joined):
        return "storage_full"
    if BAG_FULL.search(joined):
        return "bag_full"
    return None


async def read_device_pass(device: Any) -> PassScreen | None:
    image, _ts = await catch_awarded_android.capture_frame(device)
    return await asyncio.to_thread(read_pass_screen, image)


async def wait_for_pass_return(
    device: Any, artifact_dir: Path, timeout: float = RETURN_WAIT
) -> PassScreen | None:
    """Wait for the GO Pass screen to come back after a catch or popup."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        screen = await read_device_pass(device)
        if screen is not None:
            return screen
        if await claim_research_android.encounter_on_screen(device):
            return None
        await catch_awarded_android.clear_catch_screens(device, artifact_dir)
        await catch_awarded_android.close_item_sheet(device)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(CLAIM_POLL)


async def claim_one_tile(
    device: Any,
    tile: PassTile,
    args: argparse.Namespace,
    artifact_dir: Path,
) -> tuple[bool, bool, str | None]:
    """Tap one orange POKÉMON APPEARS! tile and catch what appears.

    Returns `(took, caught, stop_reason)`. `stop_reason` is `"storage_full"`,
    `"award_card"`, or `None`.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    await device.tap(tile.point)
    await asyncio.sleep(CLAIM_SETTLE)

    started = time.monotonic()
    deadline = started + args.claim_wait
    gone = False
    while True:
        if await claim_research_android.encounter_on_screen(device):
            caught = await catch_awarded_android.catch_one(
                device,
                args,
                artifact_dir,
                lone_ball_reading=True,
                default_berry="pinap",
            )
            await catch_awarded_android.clear_catch_screens(device, artifact_dir)
            return True, caught, None

        image, _ts = await catch_awarded_android.capture_frame(device)
        if await asyncio.to_thread(catch_awarded_android.card_from_frame, image) is not None:
            return True, False, "award_card"

        notice = await asyncio.to_thread(_dialog_notice, image)
        if notice == "storage_full":
            image.save(artifact_dir / "storage-full.jpg", "JPEG", quality=72)
            return False, False, "storage_full"

        screen = await asyncio.to_thread(read_pass_screen, image)
        if screen is not None:
            still_there = any(tile.overlaps(other) for other in screen.tiles)
            if not still_there:
                if not gone:
                    gone = True
                    deadline = min(deadline, time.monotonic() + POST_GONE_GRACE)
            elif time.monotonic() - started >= TAP_REJECT_GRACE:
                return False, False, None
        else:
            # Away from the GO Pass and no encounter ball/plate yet: either a
            # Dynamax/wild intro animation is playing or a popup/summary needs
            # dismissing. `clear_catch_screens` only taps recognized dialogs.
            # Never call `close_item_sheet` here: the white flash of an
            # encounter transition reads as a white picker sheet.
            await catch_awarded_android.clear_catch_screens(device, artifact_dir)

        if time.monotonic() >= deadline:
            return gone, False, None
        await asyncio.sleep(CLAIM_POLL)


async def continue_award_queue(device: Any, args: argparse.Namespace, caught: int) -> int:
    remaining = None if args.count is None else args.count - caught
    # Each phone shares the original arguments. Keep its catch budget local.
    award_args = argparse.Namespace(**{**vars(args), "count": remaining})
    print(f"[{device.label}] Award card appeared; handing off to award queue")
    return await catch_awarded_android.run_device(device, award_args)


async def run_device(device: Any, args: argparse.Namespace) -> int:
    base = args.artifacts / device.label
    base.mkdir(parents=True, exist_ok=True)
    claimed = 0
    caught = 0
    failures = 0
    scrolls = 0
    tried_claim_all = False
    toggled_filter = False

    for cycle in range(1, args.max_claims + 1):
        if args.count is not None and caught >= args.count:
            break

        screen = await read_device_pass(device)
        if args.dry_run:
            if screen is None:
                print(f"[{device.label}] Dry-run: no GO Pass screen in view")
                return 0
            print(
                f"[{device.label}] Dry-run: GO Pass "
                f"(filter={screen.toggle_state or 'unknown'}, "
                f"{len(screen.tiles)} ready tile(s), "
                f"{screen.all_tiles_count} total in view, "
                f"unclaimed_items={screen.unclaimed_non_pokemon})"
            )
            if screen.claim_all_point is not None:
                print(f"[{device.label}]   CLAIM ALL at {screen.claim_all_point}")
            for tile in screen.tiles:
                print(f"[{device.label}]   {tile.column.upper()} {tile.label!r} at {tile.point}")
            return 0

        if screen is None:
            # Check for a monthly-style award card first.
            image, _ts = await catch_awarded_android.capture_frame(device)
            if await asyncio.to_thread(catch_awarded_android.card_from_frame, image) is not None:
                return await continue_award_queue(device, args, caught)

            # An encounter already open (or a late Dynamax break-out) gets
            # caught before anything else.
            if await claim_research_android.plate_on_screen(
                device, reads=claim_research_android.SCROLL_GUARD_READS
            ) or await catch_awarded_android.encounter_open(device):
                print(f"[{device.label}] An encounter is open; throwing at it")
                stray = base / f"stray-{cycle:02d}"
                stray.mkdir(parents=True, exist_ok=True)
                if await catch_awarded_android.catch_one(
                    device,
                    args,
                    stray,
                    lone_ball_reading=True,
                    default_berry="pinap",
                ):
                    caught += 1
                    failures = 0
                    scrolls = 0
                else:
                    failures += 1
                    print(
                        f"[{device.label}] Encounter would not take a throw "
                        f"({failures}/{args.max_failures})"
                    )
                    if failures >= args.max_failures:
                        print(
                            f"[{device.label}] Gave up on an encounter that will "
                            f"not take a throw; {claimed} claimed, {caught} caught"
                        )
                        return 1
                await catch_awarded_android.clear_catch_screens(device, stray)
                continue

            # Otherwise wait for the GO Pass to return after a catch (Dynamax
            # ball shakes + XP summary + caught detail page can take ~15s).
            screen = await wait_for_pass_return(device, base / f"return-{cycle:02d}")
            if screen is None:
                if await claim_research_android.encounter_on_screen(device):
                    # Broke out late during the return wait; loop to catch it.
                    continue
                failures += 1
                print(
                    f"[{device.label}] GO Pass screen not in view "
                    f"({failures}/{args.max_failures})"
                )
                if failures >= args.max_failures:
                    print(
                        f"[{device.label}] Gave up waiting for the GO Pass screen; "
                        f"{claimed} claimed, {caught} caught"
                    )
                    return 1
                continue

        # If non-Pokémon item tiles are still orange and CLAIM ALL is present,
        # press CLAIM ALL once to sweep items (or open the monthly award picker
        # if this pass uses one).
        if (
            screen.unclaimed_non_pokemon
            and screen.claim_all_point is not None
            and not tried_claim_all
        ):
            tried_claim_all = True
            print(
                f"[{device.label}] Unclaimed item rewards in view; pressing CLAIM ALL "
                f"at {screen.claim_all_point}"
            )
            await device.tap(screen.claim_all_point)
            await asyncio.sleep(CLAIM_SETTLE)
            after_dir = base / f"claim-all-{cycle:02d}"
            after_dir.mkdir(parents=True, exist_ok=True)
            await catch_awarded_android.clear_catch_screens(device, after_dir)
            continue

        # Switch to "Showing Claimable Rewards" so claimed grey rows collapse
        # and every remaining POKÉMON APPEARS! tile stacks cleanly.
        if (
            screen.toggle_state == "all"
            and screen.toggle_claimable_point is not None
            and not toggled_filter
        ):
            toggled_filter = True
            print(
                f"[{device.label}] Switching GO Pass to Showing Claimable Rewards "
                f"at {screen.toggle_claimable_point}"
            )
            await device.tap(screen.toggle_claimable_point)
            await asyncio.sleep(TOGGLE_SETTLE)
            continue

        if screen.tiles:
            scrolls = 0
            tile = screen.tiles[0]
            print(
                f"[{device.label}] GO Pass claim {cycle}: {tile.column.upper()} "
                f"{tile.label!r} at {tile.point} ({len(screen.tiles)} ready in view)"
            )
            took, mon, stop_reason = await claim_one_tile(
                device, tile, args, base / f"gopass-{cycle:02d}"
            )
            if stop_reason == "storage_full":
                print(
                    f"[{device.label}] Pokémon Storage is full; stopping after "
                    f"{claimed} claimed, {caught} caught"
                )
                return 1
            if stop_reason == "award_card":
                return await continue_award_queue(device, args, caught)
            if took:
                claimed += 1
                failures = 0
                if mon:
                    caught += 1
                # Re-arm the filter check if the game resets to the full list
                # after returning from an encounter.
                toggled_filter = False
                print(
                    f"[{device.label}] GO Pass claimed {claimed}"
                    f"{'; caught it' if mon else ''}"
                )
                await asyncio.sleep(SCREEN_SETTLE)
                continue

            failures += 1
            print(
                f"[{device.label}] GO Pass tile tap did not take "
                f"({failures}/{args.max_failures})"
            )
            if failures >= args.max_failures:
                print(
                    f"[{device.label}] Gave up after {failures} tiles in a row that "
                    f"did not take; {claimed} claimed, {caught} caught"
                )
                return 1
            continue

        # No safe-zone POKÉMON APPEARS! tiles visible right now. Guard against an
        # encounter sneaking in before scrolling, then look further down.
        if await claim_research_android.plate_on_screen(
            device, reads=claim_research_android.SCROLL_GUARD_READS
        ):
            continue

        if scrolls >= args.scrolls:
            break
        scrolls += 1
        print(
            f"[{device.label}] No ready GO Pass Pokémon tile in view; "
            f"scrolling ({scrolls}/{args.scrolls})"
        )
        await claim_research_android.scroll_list(device)

    print(f"[{device.label}] GO Pass finished; {claimed} claimed, {caught} caught")
    return 0


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    """The merged `catch.py` options; this loop reads its share of them."""
    from . import catch_rewards

    return catch_rewards.parse_args(arguments)
