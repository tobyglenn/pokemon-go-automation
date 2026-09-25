"""Catch what the game is holding for you, on Android phones and iPhones.

Two queues end in the same encounter: the award card ("A Pokemon has
appeared!  You have N Pokemon left to catch") and a research screen's orange
CLAIM buttons.  Each phone's screen says which one it is parked on, so one
command works both: `catch_awarded_android` presses START ENCOUNTER until the
queue is empty, `claim_research_android` presses every CLAIM in the list and
catches what they pay.  An iPhone is driven through `catch_ios.IOSCatchPhone`,
which the same two loops cannot tell apart from an Android.

`--mode encounter` is the third job: catch each wild Pokemon the player opens
by hand, one after another, with the same throwers and Nanab as the queues.
`scripts/excellent_throw.py` keeps only its calibration flags.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path
from typing import Any

from . import (
    catch_awarded_android,
    catch_ios,
    claim_research_android,
    config_paths,
    excellent_throw_android,
    gbl_ios,
    ios_wda_cleanup,
    pokemon_fleet,
)

ARTIFACT_ROOT = config_paths.state_dir() / "catch"
MODES = ("auto", "award", "research", "encounter")
# How long a screen is given to name itself.  The award card slides in after a
# catch, so one blank read is not a phone with nothing to do.
DETECT_WINDOW = 8.0
DETECT_POLL = 1.0


class CatchError(RuntimeError):
    pass


async def detect_mode(device: Any, args: argparse.Namespace) -> str | None:
    """Which job this screen is: award, research, encounter, or None.

    Read strictly in that order.  The award card and a claim button both carry
    their own words, and a research list never shows START ENCOUNTER.
    """
    deadline = time.monotonic() + DETECT_WINDOW
    while True:
        image, _ts = await catch_awarded_android.capture_frame(device)
        if await asyncio.to_thread(catch_awarded_android.card_from_frame, image) is not None:
            return "award"
        if await asyncio.to_thread(claim_research_android.claim_buttons, image, args.label):
            return "research"
        if await asyncio.to_thread(claim_research_android.plate_visible, image):
            return "encounter"
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(DETECT_POLL)


async def run_phone(device: Any, args: argparse.Namespace) -> int:
    mode = args.mode
    if mode == "auto":
        mode = await detect_mode(device, args)
        if mode == "encounter" and not args.dry_run:
            # A Pokemon already standing there belongs to whichever queue is
            # behind it, and that queue only shows itself once it is caught.
            print(f"[{device.label}] An encounter is open; throwing at it first")
            directory = args.artifacts / device.label / "opening"
            directory.mkdir(parents=True, exist_ok=True)
            await catch_awarded_android.catch_one(device, args, directory, lone_ball_reading=True)
            await catch_awarded_android.clear_catch_screens(device, directory)
            mode = await detect_mode(device, args)
        if mode in {None, "encounter"}:
            print(
                f"[{device.label}] No award card, CLAIM button or encounter on screen; "
                "nothing to catch (--mode research looks further down a research list)"
            )
            return 0
    if mode == "encounter":
        return await catch_opened(device, args)
    print(f"[{device.label}] Working the {'award queue' if mode == 'award' else 'research claims'}")
    if mode == "award":
        return await catch_awarded_android.run_device(device, args)
    return await claim_research_android.run_device(device, args)


async def catch_opened(device: Any, args: argparse.Namespace) -> int:
    """Catch every Pokemon the player opens by hand, until --count or Ctrl-C.

    Nothing here opens an encounter: the loop waits `--wait` seconds for a ball
    in hand, and waits again when none comes.  Failures are not counted against
    `--max-failures` -- an empty map is the player walking, not a fault.
    """
    print(f"[{device.label}] Waiting for you to open wild encounters (Ctrl-C to stop)")
    caught = 0
    opened = 0
    while args.count is None or caught < args.count:
        directory = args.artifacts / device.label / f"wild-{opened + 1:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        if not await catch_awarded_android.catch_one(device, args, directory):
            continue
        opened += 1
        caught += 1
        await catch_awarded_android.clear_catch_screens(device, directory)
        print(f"[{device.label}] Wild encounter {opened} finished; {caught} caught")
    return 0


def split_selection(values: list[str] | None, ios_names: set[str]) -> tuple[list[str] | None, list[str] | None, bool]:
    """(iPhone names, Android selection, whether Android is wanted at all).

    No selection, or `all`, means every attached phone of both kinds.  Names
    the registry lists as iPhones are pulled out; the rest are Android names
    or serials, as they always were.
    """
    selected = [value for value in (values or []) if value.strip()]
    if not selected or "all" in selected:
        return None, None, True
    iphones = [value for value in selected if value in ios_names]
    android = [value for value in selected if value not in ios_names]
    return iphones, catch_awarded_android._resolve_selection(android), bool(android)


def load_ios_specs() -> dict[str, pokemon_fleet.DeviceSpec]:
    try:
        fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    except Exception:
        return {}
    return {name: spec for name, spec in fleet.devices.items() if spec.platform == "ios"}


def attached_iphones(specs: dict[str, pokemon_fleet.DeviceSpec], names: list[str] | None) -> list[pokemon_fleet.DeviceSpec]:
    if names is not None:
        return [specs[name] for name in names]
    if not specs:
        return []
    attached = pokemon_fleet.ios_connected_udids()
    return [spec for spec in specs.values() if spec.enabled and spec.identifier in attached]


async def android_phones(args: argparse.Namespace, serials: list[str] | None, required: bool) -> list[Any]:
    throw_args = excellent_throw_android.parse_args([])
    throw_args.wait = args.wait
    throw_args.devices = serials
    throw_args.ring_hold = args.ring_hold
    config = excellent_throw_android.load_config(None, throw_args)
    try:
        devices = await excellent_throw_android.prepare_devices(throw_args, config)
    except (excellent_throw_android.AndroidExcellentThrowError, RuntimeError, OSError) as exc:
        if required:
            raise
        print(f"No Android phone: {exc}")
        return []
    catch_awarded_android._apply_fleet_labels(devices)
    return devices


async def run_iphone(spec: pokemon_fleet.DeviceSpec, args: argparse.Namespace) -> int:
    udid = spec.identifier
    # The same lock every other iPhone command takes, so a GBL leg or a gift
    # run already driving this phone is left alone rather than fought over.
    with gbl_ios.device_lock(udid):
        phone = await asyncio.to_thread(catch_ios.IOSCatchPhone.connect, spec)
        normal = False
        try:
            status = await run_phone(phone, args)
            normal = True
            return status
        finally:
            await asyncio.to_thread(phone.close)
            if not normal:
                await asyncio.to_thread(ios_wda_cleanup.stop_wda_runner, udid)


async def run_all(args: argparse.Namespace) -> int:
    specs = load_ios_specs()
    iphone_names, serials, want_android = split_selection(args.devices, set(specs))
    explicit_android = bool(args.devices) and want_android and "all" not in args.devices
    androids = await android_phones(args, serials, explicit_android) if want_android else []
    iphones = attached_iphones(specs, iphone_names)
    if not androids and not iphones:
        if args.allow_empty:
            print("No phone to catch on")
            return 0
        raise CatchError("No Android phone or iPhone to catch on")
    print(
        "Catching on: "
        + ", ".join([device.label for device in androids] + [spec.name for spec in iphones])
    )
    if args.count is not None:
        print(f"Stopping after {args.count} caught per phone")

    labels = [device.label for device in androids] + [spec.name for spec in iphones]
    tasks = [run_phone(device, args) for device in androids]
    tasks += [run_iphone(spec, args) for spec in iphones]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    status = 0
    for label, result in zip(labels, results):
        if isinstance(result, BaseException):
            print(f"[{label}] Stopped: {result}")
            status = 1
        elif result:
            status = result
    return status


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", action="append", help="only these fleet device names or Android serials; repeat per phone")
    parser.add_argument("--mode", choices=MODES, default="auto", help="auto reads each phone's screen (default); award or research forces one; encounter catches each Pokemon you open by hand")
    parser.add_argument("--count", type=int, help="award queue or encounter mode: stop after this many caught per phone (default: until it empties)")
    parser.add_argument("--wait", type=float, default=25.0, help="seconds to wait for an encounter to be throwable")
    parser.add_argument("--card-wait", type=float, default=30.0, help="award queue: seconds to wait for the next award card")
    parser.add_argument("--claim-wait", type=float, default=25.0, help="research: seconds to wait for one reward to resolve")
    parser.add_argument("--throws", type=int, default=6, help="max balls at one encounter before moving on")
    parser.add_argument("--max-cards", type=int, default=40, help="award queue: hard cap on award cards worked per phone")
    parser.add_argument("--max-claims", type=int, default=40, help="research: hard cap on buttons pressed per phone")
    parser.add_argument("--max-failures", type=int, default=3, help="consecutive encounters or claims that did not finish before stopping")
    parser.add_argument("--scrolls", type=int, default=3, help="research: screenfuls to look down before calling the list finished")
    parser.add_argument("--label", default=claim_research_android.DEFAULT_LABEL, help="research: regex a button's own words must match")
    parser.add_argument("--no-nanab", dest="nanab", action="store_false", help="throw without feeding a Nanab berry first")
    parser.add_argument("--ring-hold", action="store_true", help="Android: hold the ball first to measure the catch circle (slow)")
    parser.add_argument("--dry-run", action="store_true", help="read the screen and report; send no touch")
    parser.add_argument("--artifacts", type=Path, default=ARTIFACT_ROOT, help="base artifact directory")
    parser.add_argument("--allow-empty", action="store_true", help="exit 0 when no phone is attached")
    args = parser.parse_args(arguments)
    if args.count is not None and args.count < 1:
        parser.error("--count must be at least 1")
    if args.throws < 1:
        parser.error("--throws must be at least 1")
    if args.max_claims < 1:
        parser.error("--max-claims must be at least 1")
    if args.scrolls < 0:
        parser.error("--scrolls cannot be negative")
    try:
        args.label = re.compile(args.label, re.IGNORECASE)
    except re.error as exc:
        parser.error(f"--label is not a regular expression: {exc}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run_all(args))
