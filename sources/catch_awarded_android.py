#!/usr/bin/env python3
"""Work the awarded-Pokemon queue: START ENCOUNTER, throw, repeat.  Reached through `catch.py`."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from . import config_paths, excellent_throw_android, gbl_vision

# The award card's own words.  The headline says the card is up at all; the
# count line is the only place the queue length is written, and reading it is
# what ends a run -- a number typed at the command line only ever guesses.
HEADLINE = "a pokemon has appeared"
START_LABEL = "start encounter"
# LATER puts the whole queue away until the game offers it again, so it is read
# but never pressed.  Nothing in this file taps a label it did not match.
LATER_LABEL = "later"
REMAINING = re.compile(r"\byou have (\d+) pokemon left to catch\b")

# Text on this card is large -- the headline is 5% of the height -- so the read
# is made on a downscaled copy.  Vision scored every line on the moto's card
# 1.00 at 315px wide, and a foldable's full 1224x2992 frame is fifteen times
# the pixels for the same five words.
OCR_WIDTH = 480
OCR_CONFIDENCE = 0.5
# The card slides in; a tap sent during the slide lands on nothing.
CARD_SETTLE = 1.4
CARD_POLL = 0.9
# How long the card is given before anything is pressed to bring it out.
CARD_FIRST_WAIT = 8.0


class AwardQueueError(RuntimeError):
    pass


# The loops in this module and in claim_research_android are written against an
# Android phone, and an iPhone joins them through `catch_ios.IOSCatchPhone`.
# These are the only places the two platforms differ; everything else -- tap,
# input_swipe, label, viewport -- the iPhone answers the same way, in the pixels
# of its own screenshot.


def is_ios(device: Any) -> bool:
    return getattr(device, "platform", "android") == "ios"


async def capture_frame(device: Any) -> tuple[Image.Image, float]:
    if is_ios(device):
        return await device.capture_frame()
    return await excellent_throw_android.capture_frame(device)


def ball_visible(device: Any, image: Image.Image) -> bool:
    """Whether the encounter ball is on this frame."""
    if is_ios(device):
        return device.ball_visible(image)
    view = excellent_throw_android.analysis_view(device.viewport)
    return (
        excellent_throw_android.excellent_throw_ios.locate_throw_ball(
            image, view.viewport
        )
        is not None
    )


async def clear_catch_screens(device: Any, artifact_dir: Path) -> None:
    if is_ios(device):
        await device.clear_catch_screens(artifact_dir)
        return
    await excellent_throw_android.clear_catch_screens(device, artifact_dir)


async def throw_once(device: Any, **options: Any) -> bool:
    """One ball, with `excellent_throw_android.run_once`'s contract on both.

    True when the encounter ended; NoEncounterError when none ever loaded.
    """
    if is_ios(device):
        return await device.run_once(**options)
    return await excellent_throw_android.run_once(device, **options)


@dataclass(frozen=True)
class AwardCard:
    """The award dialog as this loop needs it: how many are left, and where to press."""

    remaining: int | None
    start_point: list[int]


def card_from_frame(image: Image.Image) -> AwardCard | None:
    """Read the award card out of one frame, or None when it is not up.

    Deliberately strict.  START ENCOUNTER alone is not enough to press: the
    button is only pressed when the card that owns it is also legible, either
    by its headline or by its count line.
    """
    scale = 1.0
    source = image
    if image.width > OCR_WIDTH:
        scale = image.width / OCR_WIDTH
        source = image.resize((OCR_WIDTH, max(1, round(image.height / scale))))
    try:
        boxes = gbl_vision.recognize(source)
    except gbl_vision.VisionOCRError:
        return None

    start_box = next(
        (
            box
            for box in boxes
            if box.confidence >= OCR_CONFIDENCE
            and gbl_vision.normalize(box.text) == START_LABEL
        ),
        None,
    )
    if start_box is None:
        return None

    seen = gbl_vision.labels(boxes, OCR_CONFIDENCE)
    remaining = next(
        (int(match.group(1)) for match in map(REMAINING.search, seen) if match), None
    )
    if HEADLINE not in seen and remaining is None:
        return None
    return AwardCard(
        remaining=remaining,
        start_point=[round(start_box.center_x * scale), round(start_box.center_y * scale)],
    )


async def read_award_card(device: excellent_throw_android.AndroidDevice) -> AwardCard | None:
    image, _ts = await capture_frame(device)
    return await asyncio.to_thread(card_from_frame, image)


async def wait_for_award_card(
    device: excellent_throw_android.AndroidDevice, timeout: float
) -> AwardCard | None:
    """Poll for the next card.  The game raises it on its own after a catch."""
    deadline = time.monotonic() + timeout
    while True:
        card = await read_award_card(device)
        if card is not None:
            return card
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(CARD_POLL)


async def next_award_card(
    device: excellent_throw_android.AndroidDevice,
    args: argparse.Namespace,
    artifact_dir: Path,
) -> AwardCard | None:
    """Wait for the next card, clearing what a catch left up if it does not come.

    `clear_catch_screens` presses the XP card and then the caught Pokemon's
    page, and it stops at the first frame that is neither -- which is right
    when the map is back, and wrong while the page is still sliding in.  The
    moto took a Meowth on 10 Sep 2026, dismissed the XP card into that gap and
    was left standing on the Pokemon's page: no card ever came, and the phone
    sat there with twelve awards still queued.  So a card that does not arrive
    is treated as a screen still standing in front of it, once, before the
    wait is called finished.
    """
    card = await wait_for_award_card(device, min(args.card_wait, CARD_FIRST_WAIT))
    if card is not None:
        return card
    print(f"[{device.label}] No card yet; clearing anything left over from the catch")
    await clear_catch_screens(device, artifact_dir)
    return await wait_for_award_card(device, args.card_wait)


async def close_item_sheet(device: excellent_throw_android.AndroidDevice) -> bool:
    """Put away a berry or ball chooser standing over an encounter.

    A sheet hides the ball, so `encounter_open` reads the phone as having
    nothing to throw at and the run calls the queue finished with the Pokemon
    still on screen behind it -- the moto did exactly that on 10 Sep 2026 with
    nine awards left.  Tapped away above the sheet, never with BACK, which
    leaves the encounter rather than the sheet.
    """
    image, _ts = await capture_frame(device)
    from . import berry_android

    rgba = image.convert("RGBA")
    if berry_android.picker_sheet_top(rgba.width, rgba.height, 0, rgba.tobytes()) is None:
        return False
    print(f"[{device.label}] An item sheet is up; putting it away")
    await device.tap(
        [round(device.viewport[0] * 0.50), round(device.viewport[1] * 0.45)]
    )
    await asyncio.sleep(CARD_SETTLE)
    return True


async def encounter_open(device: excellent_throw_android.AndroidDevice) -> bool:
    """Whether something throwable is on screen right now.

    One read, not the encounter wait's repeated agreeing reads: this only has
    to be good enough to decide whether the wait is worth spending at all.
    """
    image, _ts = await capture_frame(device)
    return ball_visible(device, image)


async def catch_one(
    device: excellent_throw_android.AndroidDevice,
    args: argparse.Namespace,
    artifact_dir: Path,
    lone_ball_reading: bool = False,
) -> bool:
    """Throw until this encounter is over.  True when it ended.

    One award encounter is not one ball.  `run_once` reports whether the ball
    stayed gone, and a Pokemon that broke out has to be thrown at again from
    the same encounter -- with no second berry, which is still in effect.

    `lone_ball_reading` is passed straight through to the throw: it says the
    caller has already established that a Pokemon is on the screen, so one
    clean sight of the ball is enough to throw at.
    """
    fresh_encounter = args.nanab
    for attempt in range(1, args.throws + 1):
        throw_dir = artifact_dir / f"throw-{attempt:02d}"
        throw_dir.mkdir(parents=True, exist_ok=True)
        try:
            ended = await throw_once(
                device,
                wait_seconds=args.wait,
                artifact_dir=throw_dir,
                dry_run=False,
                use_nanab=fresh_encounter,
                ring_hold=args.ring_hold,
                lone_ball_reading=lone_ball_reading,
            )
        except excellent_throw_android.NoEncounterError as exc:
            if attempt == 1:
                # The encounter never loaded, so there is nothing to throw at
                # and nothing was spent.  The caller re-reads the card.
                print(f"[{device.label}] {exc}")
                return False
            # Later, an empty screen means the opposite: the previous throw
            # did land, `wait_for_throw_result` mistook the resolution for a
            # break-out, and the encounter is already over.  Clear what the
            # catch left standing so the next card can come up.
            print(f"[{device.label}] Encounter already over after throw {attempt - 1}")
            await clear_catch_screens(device, throw_dir)
            return True
        except excellent_throw_android.AndroidExcellentThrowError as exc:
            print(f"[{device.label}] Throw {attempt} skipped: {exc}")
            await asyncio.sleep(0.8)
            continue
        fresh_encounter = False
        if ended:
            return True
    print(f"[{device.label}] Encounter still open after {args.throws} throws")
    return False


async def run_device(
    device: excellent_throw_android.AndroidDevice, args: argparse.Namespace
) -> int:
    base = args.artifacts / device.label
    base.mkdir(parents=True, exist_ok=True)
    caught = 0
    failures = 0
    for cycle in range(1, args.max_cards + 1):
        if args.count is not None and caught >= args.count:
            break
        cycle_dir = base / f"award-{cycle:02d}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        card = await next_award_card(device, args, cycle_dir)
        if card is None:
            # An encounter already open is the queue's own: this workflow is
            # run against a phone parked on the award card, and one left
            # standing over a Pokemon is one a previous attempt opened and
            # could not finish.  Throwing at it is how the run picks itself up.
            if not args.dry_run:
                await close_item_sheet(device)
            if not args.dry_run and await encounter_open(device):
                print(f"[{device.label}] No card, but an encounter is open; throwing at it")
                if await catch_one(device, args, cycle_dir):
                    caught += 1
                    failures = 0
                    continue
            print(
                f"[{device.label}] No award card within {args.card_wait:g}s; "
                f"queue finished after {caught} caught"
            )
            return 0
        left = "an unread number of" if card.remaining is None else card.remaining
        print(f"[{device.label}] Award card {cycle}: {left} Pokemon left to catch")
        if card.remaining == 0:
            print(f"[{device.label}] Queue empty; {caught} caught")
            return 0
        if args.dry_run:
            print(
                f"[{device.label}] Dry-run: START ENCOUNTER sits at "
                f"{card.start_point}; no touch sent"
            )
            return 0

        await device.tap(card.start_point)
        await asyncio.sleep(CARD_SETTLE)
        if await catch_one(device, args, cycle_dir):
            caught += 1
            failures = 0
            print(f"[{device.label}] Encounter {cycle} finished; {caught} caught")
            continue

        # A card still on screen means START ENCOUNTER never took, which is
        # free to retry.  Anything else is a phone left somewhere this loop
        # does not understand, and pressing on would only press blind.
        failures += 1
        await clear_catch_screens(device, cycle_dir)
        if failures >= args.max_failures:
            print(
                f"[{device.label}] Gave up after {failures} encounters in a row "
                f"that did not finish; {caught} caught"
            )
            return 1
        print(f"[{device.label}] Retrying (failure {failures} of {args.max_failures})")
    print(f"[{device.label}] Done: {caught} caught")
    return 0


def _resolve_selection(values: list[str] | None) -> list[str] | None:
    """Accept fleet device names as well as raw serials.

    A name is only translated when the private registry names it; anything
    else is passed through as the serial the operator typed.
    """
    if not values:
        return None
    selected = [value for value in values if value.strip() and value != "all"]
    if not selected:
        return None
    try:
        from . import pokemon_fleet

        fleet = pokemon_fleet.load_fleet(
            config_paths.default_config("pokemon-fleet.yaml")
        )
        serials = {
            name: spec.identifier
            for name, spec in fleet.devices.items()
            if spec.platform == "android" and spec.identifier
        }
    except Exception:
        serials = {}
    return [serials.get(value, value) for value in selected]


def _apply_fleet_labels(devices: list[excellent_throw_android.AndroidDevice]) -> None:
    """Log under the operator's own names when the registry supplies them."""
    try:
        from . import pokemon_fleet

        fleet = pokemon_fleet.load_fleet(
            config_paths.default_config("pokemon-fleet.yaml")
        )
        names = {
            spec.identifier: name
            for name, spec in fleet.devices.items()
            if spec.platform == "android" and spec.identifier
        }
    except Exception:
        return
    for device in devices:
        device.label = names.get(device.serial, device.label)


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    """The merged `catch.py` options; this loop reads its share of them."""
    from . import catch_rewards

    return catch_rewards.parse_args(arguments)
