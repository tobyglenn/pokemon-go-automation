"""Catch what the game is holding for you, on Android phones and iPhones.

The current game screen selects the award queue, GO Pass rewards, or research
claims on each phone. Android and iPhone share the same reward loops.
`--mode encounter` waits for the wild encounters the player opens.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from pathlib import Path
from typing import Any

from . import (
    android_memory,
    catch_awarded_android,
    catch_ios,
    claim_gopass,
    claim_research_android,
    config_paths,
    excellent_throw_android,
    gbl_ios,
    ios_wda_cleanup,
    pokemon_fleet,
)

ARTIFACT_ROOT = config_paths.state_dir() / "catch"
MODES = ("auto", "award", "research", "pass", "encounter")
# How long a screen is given to name itself.  The award card slides in after a
# catch, so one blank read is not a phone with nothing to do.
DETECT_WINDOW = 8.0
DETECT_POLL = 1.0
# Times an encounter found open at the start is thrown at before giving up.
OPENING_ROUNDS = 5


class CatchError(RuntimeError):
    pass


async def detect_mode(device: Any, args: argparse.Namespace) -> str | None:
    """Which job this screen is: award, pass, research, encounter, or None.

    Read strictly in that order.  The award card, GO Pass screen and a claim
    button each carry their own words, and a research list never shows START
    ENCOUNTER or the GO Pass BASIC/DELUXE columns.
    """
    deadline = time.monotonic() + DETECT_WINDOW
    while True:
        image, _ts = await catch_awarded_android.capture_frame(device)
        if await asyncio.to_thread(catch_awarded_android.card_from_frame, image) is not None:
            return "award"
        if await asyncio.to_thread(claim_gopass.read_pass_screen, image) is not None:
            return "pass"
        if await asyncio.to_thread(claim_research_android.claim_buttons, image, args.label):
            return "research"
        if await asyncio.to_thread(claim_research_android.plate_visible, image):
            return "encounter"
        # A flying legendary carries the plate out of the read; the ball in
        # hand stays put.  android-three's Latios was left standing that way on
        # 6 Oct 2026 after a break-out outlasted the throw's result window.
        if await asyncio.to_thread(catch_awarded_android.ball_visible, device, image):
            return "encounter"
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(DETECT_POLL)


async def run_phone(device: Any, args: argparse.Namespace) -> int:
    mode = args.mode
    if mode == "auto":
        mode = await detect_mode(device, args)
        if mode is None and not args.dry_run:
            # A berry sheet or leftover XP card hides the ball or award card
            # behind it. Only try closing them once the first look found
            # nothing, so a white GO Pass page is never mistaken for a sheet.
            await catch_awarded_android.close_item_sheet(device)
            directory = args.artifacts / device.label / "leftover"
            directory.mkdir(parents=True, exist_ok=True)
            await catch_awarded_android.clear_catch_screens(device, directory)
            mode = await detect_mode(device, args)
        opening = 0
        while mode == "encounter" and not args.dry_run and opening < OPENING_ROUNDS:
            # A Pokemon already standing there belongs to whichever queue is
            # behind it, and that queue only shows itself once it is caught.
            # A slow break-out can read as a catch, so it is looked at again.
            opening += 1
            print(f"[{device.label}] An encounter is open; throwing at it first")
            directory = args.artifacts / device.label / f"opening-{opening:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            await catch_awarded_android.catch_one(device, args, directory, lone_ball_reading=True)
            await catch_awarded_android.clear_catch_screens(device, directory)
            await catch_awarded_android.close_item_sheet(device)
            mode = await detect_mode(device, args)
        if mode is None and opening and not args.dry_run:
            # The encounter caught above came from a queue, and its next card
            # can take longer than the detect window to slide in: the compact Android handset
            # called "nothing to catch" on 3 Nov 2026 with three still queued.
            # The award loop waits for it properly.
            print(f"[{device.label}] Caught the open encounter; waiting for its queue")
            mode = "award"
        if mode is None or (mode == "encounter" and not args.dry_run):
            print(
                f"[{device.label}] No award card, GO Pass, CLAIM button or encounter on screen; "
                "nothing to catch (--mode research looks further down a research list)"
            )
            return 0
    if mode == "encounter":
        return await catch_opened(device, args)
    if mode == "pass":
        print(f"[{device.label}] Working the GO Pass rewards")
        return await claim_gopass.run_device(device, args)
    print(f"[{device.label}] Working the {'award queue' if mode == 'award' else 'research claims'}")
    if mode == "award":
        return await catch_awarded_android.run_device(device, args)
    return await claim_research_android.run_device(device, args)


async def catch_opened(device: Any, args: argparse.Namespace) -> int:
    """Catch every Pokemon the player opens by hand, until --count or Ctrl-C.

    Nothing here opens an encounter unless `--open-map` is given: the loop
    waits `--wait` seconds for a ball in hand, and waits again when none
    comes.  Failures are not counted against `--max-failures` -- an empty map
    is the player walking, not a fault.

    `--open-map` (Android) taps a Meltan standing by the player on the map,
    for a Mystery Box run. `--sol-tune` sends numeric catch outcomes and throw
    settings to Sol every few encounters for this phone (see `throw_tuning`).
    """
    android = isinstance(device, excellent_throw_android.AndroidDevice)
    open_map = getattr(args, "open_map", False) and android
    if args.dry_run:
        if await catch_awarded_android.encounter_open(device):
            print(f"[{device.label}] Dry-run: an encounter is open")
        else:
            print(f"[{device.label}] Dry-run: no open encounter in view")
        return 0
    tuner = None
    if getattr(args, "sol_tune", False) and android:
        from . import throw_tuning

        run_dir = args.artifacts / device.label / f"sol-tune-{time.strftime('%Y%m%d')}"
        tuner = throw_tuning.SolTuner(device, run_dir, every=args.sol_every)
        print(f"[{device.label}] Sol retunes the throw every {tuner.every} encounters; notes in {run_dir}")
    if open_map:
        print(f"[{device.label}] Tapping Meltan on the map (Ctrl-C to stop)")
    else:
        print(f"[{device.label}] Waiting for you to open wild encounters (Ctrl-C to stop)")
    caught = 0
    opened = 0
    while args.count is None or caught < args.count:
        if tuner is not None:
            tuner.apply_pending()
        if open_map and not await open_map_spawn(device):
            await asyncio.sleep(MAP_POLL)
            continue
        directory = args.artifacts / device.label / f"wild-{opened + 1:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        throws_before = device.throws_sent if tuner is not None else 0
        ended = await catch_awarded_android.catch_one(device, args, directory)
        if tuner is not None:
            balls = device.throws_sent - throws_before
            if balls > 0:
                tuner.record(ended, balls)
        if not ended and open_map:
            # Tapped but nothing opened (a buddy or a Pokemon walking off), or
            # the throws ran out.  Either way the map is where to look next.
            await catch_awarded_android.clear_catch_screens(device, directory)
        if not ended:
            continue
        opened += 1
        caught += 1
        await catch_awarded_android.clear_catch_screens(device, directory)
        print(f"[{device.label}] Wild encounter {opened} finished; {caught} caught")
    return 0


# How often the map is looked at for a fresh spawn, and how long a tapped one
# is given to open its encounter.
MAP_POLL = 2.0
MAP_OPEN_WAIT = 6.0


async def open_map_spawn(device: Any) -> bool:
    """Tap a Meltan beside the player.  True once an encounter is opening.

    Also True when an encounter is already open (one the player started, or
    a tap from the previous round that was slow to load), so the catch takes
    it either way.
    """
    from . import map_spawn_android

    image, _ts = await excellent_throw_android.capture_frame(device)
    if not map_spawn_android.on_map(image):
        return await catch_awarded_android.encounter_open(device)
    point = map_spawn_android.find_meltan(image)
    if point is None:
        return False
    print(f"[{device.label}] Meltan on the map at {point}; tapping it")
    await device.tap(point)
    deadline = time.monotonic() + MAP_OPEN_WAIT
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        image, _ts = await excellent_throw_android.capture_frame(device)
        if not map_spawn_android.on_map(image):
            return True
    print(f"[{device.label}] The tap did not open an encounter")
    return False


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


async def android_phones(
    args: argparse.Namespace, serials: list[str] | None, required: bool
) -> list[excellent_throw_android.AndroidDevice]:
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


async def run_android(device: excellent_throw_android.AndroidDevice, args: argparse.Namespace) -> int:
    spec = pokemon_fleet.DeviceSpec(
        name=device.label,
        platform="android",
        enabled=True,
        config={"serial": device.serial},
        base_dir=config_paths.private_config_dir(),
    )
    with pokemon_fleet.acquire_device_locks([spec]):
        if not args.dry_run:
            await android_memory.free_memory_async([device])
        return await run_phone(device, args)


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
    tasks = [run_android(device, args) for device in androids]
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
    parser.add_argument("--mode", choices=MODES, default="auto", help="auto reads each phone's screen (default); award, pass or research forces one; encounter catches each Pokemon you open by hand")
    parser.add_argument("--count", type=int, help="award queue, GO Pass or encounter mode: stop after this many caught per phone (default: until it empties)")
    parser.add_argument("--wait", type=float, default=25.0, help="seconds to wait for an encounter to be throwable")
    parser.add_argument("--card-wait", type=float, default=30.0, help="award queue: seconds to wait for the next award card")
    parser.add_argument("--queue-wait", type=float, default=300.0, help="award queue: once cards have been worked, seconds a late card is still waited for before the queue is called finished")
    parser.add_argument("--claim-wait", type=float, default=25.0, help="research: seconds to wait for one reward to resolve")
    parser.add_argument("--throws", type=int, default=6, help="max balls at one encounter before moving on")
    parser.add_argument("--max-cards", type=int, default=60, help="award queue: hard cap on award cards worked per phone")
    parser.add_argument("--max-claims", type=int, default=40, help="research: hard cap on buttons pressed per phone")
    parser.add_argument("--max-failures", type=int, default=3, help="consecutive encounters or claims that did not finish before stopping")
    parser.add_argument("--scrolls", type=int, default=3, help="research: screenfuls to look down before calling the list finished")
    parser.add_argument("--label", default=claim_research_android.DEFAULT_LABEL, help="research: regex a button's own words must match")
    parser.add_argument("--berry", choices=("nanab", "pinap", "razz"), default=None, help="berry to feed encounters (default: pinap on GO Pass, nanab elsewhere; falls back to nanab if out)")
    parser.add_argument("--no-nanab", dest="nanab", action="store_false", help="throw without feeding any berry first")
    parser.add_argument("--open-map", action="store_true", help="encounter mode, Android: tap each Meltan that spawns beside the player (Mystery Box)")
    parser.add_argument("--sol-tune", action="store_true",
                        help="Android encounter mode: send throw settings, counts, and outcomes to Sol (OpenAI)")
    parser.add_argument("--sol-every", type=int, default=3, help="--sol-tune: encounters between Sol reviews")
    parser.add_argument("--ring-hold", action="store_true", help="Android: hold the ball first to measure the catch circle (slow)")
    parser.add_argument("--dry-run", action="store_true", help="read the screen and report; send no touch")
    parser.add_argument("--artifacts", type=Path, default=ARTIFACT_ROOT, help="base artifact directory")
    parser.add_argument("--allow-empty", action="store_true", help="exit 0 when no phone is attached")
    args = parser.parse_args(arguments)
    if args.count is not None and args.count < 1:
        parser.error("--count must be at least 1")
    if args.queue_wait < 0:
        parser.error("--queue-wait cannot be negative")
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
