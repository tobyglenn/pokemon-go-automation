#!/usr/bin/env python3
"""Coordinate one Pokemon GO trade between one iOS and one Android device.

The Android side uses ADB and the existing AutoTraderConfig.yaml stored on the
phone. The iOS side uses Appium/XCUITest and a separate host-side coordinate
file. This runner intentionally refuses to start while gift_ios.py is active so
it cannot take over the gift runner's WebDriverAgent session.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Sequence
from urllib.error import URLError
from urllib.request import urlopen

import yaml

from . import config_paths, ios_wda_cleanup
from . import trade_android as android_trade


@dataclass(frozen=True)
class TradeStep:
    name: str
    delay_after: float
    use_delay_modifier: bool = False
    ios_optional: bool = False


DEFAULT_STEPS = (
    TradeStep("TRADE_BTN", 6, True),
    TradeStep("FIRST_PKMN_BTN", 1),
    TradeStep("NEXT_BTN", 3, True),
    TradeStep("MAX_LEVEL_RESET_BTN", 4, True, True),
    TradeStep("CONFIRM_BTN", 15, True),
    TradeStep("X_BTN", 1),
)

REQUIRED_IOS_COORDINATES = {
    "TRADE_BTN",
    "FIRST_PKMN_BTN",
    "NEXT_BTN",
    "CONFIRM_BTN",
    "X_BTN",
}
OPTIONAL_IOS_COORDINATES = {
    "MAX_LEVEL_RESET_BTN",
    "POWER_UP_CANCEL_BTN",
    "IOS_TRADING_UNAVAILABLE_OK_BTN",
    # "YES" on "Do you want to cancel the trade?", which is what a phone backed
    # out of an open trade lands on. Optional: without it recovery just leaves
    # the dialog alone and says so.
    "TRADE_CANCEL_YES_BTN",
    # The door in the top-left corner of a trade screen, and the only way off
    # the one this phone has not offered a Pokémon into. Android leaves that
    # screen with BACK and needs nothing mapped; an iPhone without this is left
    # standing on it, which is the trade the game later cancels.
    "TRADE_EXIT_BTN",
}
KNOWN_IOS_COORDINATES = REQUIRED_IOS_COORDINATES | OPTIONAL_IOS_COORDINATES

RECORD_SAMPLE_X = (0.05, 0.95)
RECORD_SAMPLE_Y = (0.58, 0.90)
RECORD_SAMPLE_STEPS = 9
RECORD_MIN_SATURATION = 140
RECORD_COLOR_TOLERANCE = 45
RECORD_MIN_SHARE = 0.65
RECORD_DISMISS_DELAY = 2
RECORD_MAX_ATTEMPTS = 2

STATE_GUARD_ATTEMPTS = 6
STATE_GUARD_DELAY = 2

# SpringBoard draws its own cards — AirPods connected, Low Battery, an incoming
# call — on top of whatever app is running. Pokémon GO is still there and still
# on the right screen, but it owns none of the pixels, so every metric reads
# about zero and the guard sees no state it recognises. A run died exactly this
# way at TRADE_COMPLETE with iOS at green 0.0 / white 0.006 while the Android
# side was a perfectly good friend screen. Re-activating the app dismisses the
# card without touching the game.
IOS_OVERLAY_RECOVERY_ATTEMPTS = 2   # per guard call
IOS_OVERLAY_SETTLE_DELAY = 2

# The two phones can end a cycle one screen apart: the trade goes through, but
# a run that dies anywhere after CONFIRM_BTN leaves the post-trade card open on
# whichever side never got its X_BTN tap. The next run then finds Android on
# the friend screen and iOS still showing the card, and no amount of waiting
# fixes it — the card needs a tap. Closing it on the straggler alone puts both
# back in step. Measured on the split that stopped a run at TRADE_BTN: Android
# white_panel 0.70 (a friend screen), iOS 0.91 (a post-trade card).
POST_TRADE_RESYNC_ATTEMPTS = 2
POST_TRADE_RESYNC_DELAY = 3
# `mobile: queryAppState` codes. 0 unknown, 1 not running, 2 suspended,
# 3 backgrounded, 4 frontmost.
IOS_APP_STATE_NOT_RUNNING = 1
RECOVERY_POLL_DELAY = 2
RECOVERY_UNKNOWN_ATTEMPTS = 15
RECOVERY_BACKOFF_START = 5
RECOVERY_BACKOFF_MAX = 30
IOS_TRADING_UNAVAILABLE_OK_POINT = (187, 372)
STEP_EXPECTED_STATE = {
    "TRADE_BTN": "friend",
    "FIRST_PKMN_BTN": "selection",
    "NEXT_BTN": "next",
    "CONFIRM_BTN": "lobby",
    "X_BTN": "post_trade",
}
STATE_ACTION_COORDINATE = {
    "friend": "TRADE_BTN",
    "next": "NEXT_BTN",
    "lobby": "CONFIRM_BTN",
    "post_trade": "X_BTN",
}

LOCK_PATH = Path("/tmp/pokemon-go-ios-trade.lock")
ARTIFACT_ROOT = config_paths.state_dir()
TRADE_DIAGNOSTICS_DIR = ARTIFACT_ROOT / "trades" / "diagnostics"


try:
    from .pokemon_fleet import FleetError as _FleetError
except ImportError:
    _FleetError = RuntimeError


class CrossPlatformTradeError(_FleetError):
    pass



@dataclass(frozen=True)
class RuntimeConfig:
    ios_coordinates: dict[str, list[int]]
    ios_device: dict[str, Any]
    appium_server_url: str
    android_serial: str | None
    delay_modifier: float
    steps: tuple[TradeStep, ...]
    # A serial named on the command line is an instruction; one left in the
    # config is only a preference, since phones get swapped between machines.
    android_serial_required: bool = False


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise CrossPlatformTradeError(f"Config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise CrossPlatformTradeError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CrossPlatformTradeError(f"Config root must be a YAML object: {path}")
    return value


def validate_point(name: str, point: Any, allow_placeholder: bool = False) -> list[int]:
    if (
        not isinstance(point, list)
        or len(point) != 2
        or not all(type(value) is int for value in point)
    ):
        raise CrossPlatformTradeError(
            f"iOS coordinate {name} must be [x, y] integer logical points"
        )
    if point == [0, 0] and not allow_placeholder:
        raise CrossPlatformTradeError(
            f"iOS coordinate {name} is still the [0, 0] calibration placeholder"
        )
    return point


def validate_ios_coordinates(
    value: Any,
    allow_placeholders: bool = False,
) -> dict[str, list[int]]:
    if not isinstance(value, dict):
        raise CrossPlatformTradeError("Config ios.coordinates must be a YAML object")
    unknown = set(value) - KNOWN_IOS_COORDINATES
    if unknown:
        raise CrossPlatformTradeError(
            "Unknown iOS coordinate(s): " + ", ".join(sorted(unknown))
        )
    missing = REQUIRED_IOS_COORDINATES - set(value)
    if missing:
        raise CrossPlatformTradeError(
            "Missing iOS coordinate(s): " + ", ".join(sorted(missing))
        )

    points: dict[str, list[int]] = {}
    for name, point in value.items():
        if name in OPTIONAL_IOS_COORDINATES and point is None:
            continue
        points[name] = validate_point(name, point, allow_placeholders)
    return points


def validate_ios_device(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CrossPlatformTradeError("The Appium config needs a device object")
    for key in ("udid", "team_id", "wda_bundle_id"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise CrossPlatformTradeError(f"Appium config device.{key} must be set")
    return value


def resolve_config_path(value: Any, relative_to: Path) -> Path:
    """Find the shared Appium YAML on whichever Mac is running the trader."""
    try:
        return config_paths.find_config(value, relative_to, "Config ios.appium_config")
    except config_paths.ConfigPathError as exc:
        raise CrossPlatformTradeError(str(exc)) from exc


def build_steps(delays: Any) -> tuple[TradeStep, ...]:
    if delays is None:
        delays = {}
    if not isinstance(delays, dict):
        raise CrossPlatformTradeError("Config delays must be a YAML object")
    unknown = set(delays) - {step.name for step in DEFAULT_STEPS}
    if unknown:
        raise CrossPlatformTradeError("Unknown delay(s): " + ", ".join(sorted(unknown)))

    result = []
    for step in DEFAULT_STEPS:
        value = delays.get(step.name, step.delay_after)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise CrossPlatformTradeError(f"Delay {step.name} must be a non-negative number")
        result.append(
            TradeStep(step.name, float(value), step.use_delay_modifier, step.ios_optional)
        )
    return tuple(result)


def load_runtime_config(
    path: Path,
    ios_config_override: Path | None = None,
    android_serial_override: str | None = None,
    delay_modifier_override: float | None = None,
    allow_placeholders: bool = False,
) -> RuntimeConfig:
    root = load_yaml(path)
    ios = root.get("ios")
    if not isinstance(ios, dict):
        raise CrossPlatformTradeError("Config needs an ios object")

    appium_path = ios_config_override or resolve_config_path(
        ios.get("appium_config"), path.parent
    )
    appium_config = load_yaml(appium_path)
    ios_device = validate_ios_device(appium_config.get("device"))
    server_url = ios.get("server_url", appium_config.get("server_url", "http://127.0.0.1:4723"))
    if not isinstance(server_url, str) or not server_url.startswith(("http://", "https://")):
        raise CrossPlatformTradeError("Appium server URL must start with http:// or https://")

    android = root.get("android", {})
    if not isinstance(android, dict):
        raise CrossPlatformTradeError("Config android must be a YAML object")
    android_serial = android_serial_override or android.get("serial")
    if android_serial is not None and not isinstance(android_serial, str):
        raise CrossPlatformTradeError("Config android.serial must be text or null")

    delay_modifier = (
        delay_modifier_override
        if delay_modifier_override is not None
        else root.get("delay_modifier", 0)
    )
    if isinstance(delay_modifier, bool) or not isinstance(delay_modifier, (int, float)):
        raise CrossPlatformTradeError("Config delay_modifier must be a number")

    return RuntimeConfig(
        ios_coordinates=validate_ios_coordinates(
            ios.get("coordinates"),
            allow_placeholders=allow_placeholders,
        ),
        ios_device=ios_device,
        appium_server_url=server_url.rstrip("/"),
        android_serial=android_serial,
        delay_modifier=float(delay_modifier),
        steps=build_steps(root.get("delays")),
        android_serial_required=android_serial_override is not None,
    )


def active_gift_runner_pids() -> list[int]:
    """Return live gift_ios.py PIDs without contacting its Appium session."""
    result = subprocess.run(
        ["pgrep", "-f", r"(^|[ /])gift_ios[.]py([ ]|$)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise CrossPlatformTradeError("Could not check for an active iOS gift runner")
    return [int(value) for value in result.stdout.split() if value.isdigit()]


def check_appium_status(server_url: str) -> None:
    try:
        with urlopen(f"{server_url}/status", timeout=3) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        raise CrossPlatformTradeError(
            f"Appium is not ready at {server_url}; start the existing Appium launcher first"
        ) from exc
    if not isinstance(payload, dict) or "value" not in payload:
        raise CrossPlatformTradeError(f"Unexpected Appium status response from {server_url}")


@contextmanager
def exclusive_trade_lock() -> Iterator[None]:
    handle = LOCK_PATH.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CrossPlatformTradeError("Another cross-platform trader is already running") from exc
        yield
    finally:
        handle.close()


async def load_android_config(device: Any) -> dict[str, list[int]]:
    remote_path = android_trade.CONFIG_FILE_DIR + android_trade.CONFIG_FILE_NAME
    with tempfile.TemporaryDirectory(prefix="cross-trade-") as directory:
        local_path = Path(directory) / android_trade.CONFIG_FILE_NAME
        await device.pull(remote_path, local_path)
        value = yaml.safe_load(local_path.read_text())
    if not isinstance(value, dict):
        raise CrossPlatformTradeError(f"Android config is not a YAML object: {remote_path}")
    missing = android_trade.BUTTON_NAMES - set(value)
    if missing:
        raise CrossPlatformTradeError(
            "Android config is missing coordinate(s): " + ", ".join(sorted(missing))
        )
    for name, point in value.items():
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(type(item) is int for item in point)
        ):
            raise CrossPlatformTradeError(f"Android coordinate {name} must be [x, y]")
    device.config = value
    return value


async def select_android_device(serial: str | None, serial_required: bool = False) -> Any:
    """Pick the Android to trade with, by serial when one is asked for.

    Any of the phones will do: the tap coordinates are read off the device
    itself, so the only thing a serial settles is which one to talk to when
    several are plugged in. A serial that came from the config is therefore
    treated as a preference and given up on when that phone is elsewhere,
    while one passed on the command line is honoured strictly.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            android_trade.ADB_BINARY,
            "start-server",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        raise CrossPlatformTradeError("Could not start the local ADB server") from exc
    if process.returncode:
        detail = stderr.decode(errors="replace").strip()
        raise CrossPlatformTradeError(f"Could not start the local ADB server: {detail}")

    try:
        devices = await android_trade.ClientAsync().devices()
    except Exception as exc:
        raise CrossPlatformTradeError(
            "Could not reach the local ADB server; run `adb start-server`"
        ) from exc
    if not devices:
        raise CrossPlatformTradeError("No authorized Android device is connected")
    names = ", ".join(device.serial for device in devices)

    if serial:
        wanted = [device for device in devices if device.serial == serial]
        if wanted:
            devices = wanted
        elif serial_required:
            raise CrossPlatformTradeError(
                f"Android device {serial!r} was not found; connected: {names}"
            )
        elif len(devices) > 1:
            raise CrossPlatformTradeError(
                f"Android {serial} from the config is not connected, and more than one "
                f"other phone is ({names}); pass --android-serial to choose"
            )
        else:
            print(f"Android {serial} from the config is not connected; using {names}")
    elif len(devices) > 1:
        raise CrossPlatformTradeError(
            f"More than one Android device is connected ({names}); "
            "set android.serial or pass --android-serial"
        )

    device = devices[0]
    try:
        await load_android_config(device)
    except CrossPlatformTradeError:
        raise
    except Exception as exc:
        raise CrossPlatformTradeError(
            f"Could not read AutoTraderConfig.yaml from Android {device.serial}"
        ) from exc
    device.display_id = await android_trade.find_display_id(device)
    return device


def has_record_card_image(image: Any) -> bool:
    width, height = image.size

    def axis(bounds: tuple[float, float], size: int) -> list[int]:
        low, high = bounds
        step = (high - low) / (RECORD_SAMPLE_STEPS - 1)
        return [int(size * (low + step * index)) for index in range(RECORD_SAMPLE_STEPS)]

    samples = [
        image.getpixel((x, y))[:3]
        for y in axis(RECORD_SAMPLE_Y, height)
        for x in axis(RECORD_SAMPLE_X, width)
    ]
    colored = sorted(
        pixel for pixel in samples if max(pixel) - min(pixel) >= RECORD_MIN_SATURATION
    )
    if not colored:
        return False
    dominant = colored[len(colored) // 2]
    hits = sum(
        1
        for pixel in samples
        if all(
            abs(pixel[channel] - dominant[channel]) <= RECORD_COLOR_TOLERANCE
            for channel in range(3)
        )
    )
    return hits / len(samples) >= RECORD_MIN_SHARE


def _fraction_in_crop(
    image: Any,
    bounds: tuple[float, float, float, float],
    predicate: Callable[[int, int, int], bool],
) -> float:
    left, top, right, bottom = bounds
    crop = image.crop(
        (
            int(image.width * left),
            int(image.height * top),
            int(image.width * right),
            int(image.height * bottom),
        )
    )
    crop.thumbnail((120, 120))
    pixels = list(crop.getdata())
    return sum(1 for red, green, blue in pixels if predicate(red, green, blue)) / len(pixels)


def _patch_fraction_at(
    image: Any,
    point: tuple[int, int] | None,
    predicate: Callable[[int, int, int], bool],
) -> float:
    if point is None:
        return 0.0
    center_x, center_y = point
    radius = max(24, int(min(image.size) * 0.05))
    left = max(0, center_x - radius)
    top = max(0, center_y - radius)
    right = min(image.width, center_x + radius + 1)
    bottom = min(image.height, center_y + radius + 1)
    crop = image.crop((left, top, right, bottom))
    pixels = list(crop.getdata())
    return sum(
        1 for red, green, blue in pixels if predicate(red, green, blue)
    ) / len(pixels)


def _is_green(red: int, green: int, blue: int) -> bool:
    return (
        green - red >= 20
        and green >= blue - 10
        and max(red, green, blue) - min(red, green, blue) >= 45
    )


def _is_orange(red: int, green: int, blue: int) -> bool:
    """The amber CANCEL pill the trade screen shows once this side has
    confirmed and is waiting on the other trainer.

    Measured at CONFIRM_BTN on the android-two's waiting screen: mean (249, 177, 54)
    filling 0.904 of the patch, against 0.0 on a live green CONFIRM pill, on
    the sparse waiting lobby and on the Pokémon picker.
    """
    return red >= 200 and red - blue >= 90 and 100 <= green <= 200 and red - green >= 40


def _green_fraction_at(image: Any, point: tuple[int, int] | None) -> float:
    return _patch_fraction_at(image, point, _is_green)


def _is_white(red: int, green: int, blue: int) -> bool:
    return (
        min(red, green, blue) >= 225
        and max(red, green, blue) - min(red, green, blue) <= 22
    )


def screen_metrics(
    image: Any,
    action_point: tuple[int, int] | None,
) -> dict[str, float]:
    return {
        "action_green": _green_fraction_at(image, action_point),
        # The same patch, asked whether the pill has turned into CANCEL. Only
        # the trade screen does that, and only after this side has confirmed.
        "action_orange": _patch_fraction_at(image, action_point, _is_orange),
        "search_pale": _fraction_in_crop(
            image,
            (0.20, 0.14, 0.85, 0.24),
            lambda red, green, blue: (
                red >= 180 and green >= 190 and blue >= 175 and green >= red - 5
            ),
        ),
        "white_panel": _fraction_in_crop(image, (0.05, 0.43, 0.95, 0.82), _is_white),
        # The head of the post-trade card — name and HP — which is white on
        # every handset, and the screen's outer columns beside it, which the
        # card reaches but a floating dialog does not. See the post_trade
        # branch of state_matches for the measurements these two carry.
        "card_band": _fraction_in_crop(image, (0.25, 0.38, 0.75, 0.48), _is_white),
        "card_edges": min(
            _fraction_in_crop(image, (0.02, 0.50, 0.09, 0.90), _is_white),
            _fraction_in_crop(image, (0.91, 0.50, 0.98, 0.90), _is_white),
        ),
    }


def state_matches(expected: str, metrics: dict[str, float]) -> bool:
    # No screen the sequence expects is a dialog floating over a dimmed one,
    # and one of them reads as a friend screen if this is not said: "Do you
    # want to cancel this trade?" measured green 0.60 at TRADE_BTN (its own YES
    # pill) with white_panel 0.466, which is inside every friend bound. A run
    # that trusted that tapped TRADE_BTN into the dialog and called the phone
    # recovered while it sat there.
    if is_floating_dialog(metrics):
        return False
    green = metrics["action_green"]
    pale = metrics["search_pale"]
    white = metrics["white_panel"]
    if expected == "friend":
        # The green floor only asks that a green action button is under the tap
        # point at all; white_panel and pale do the work of telling the friend
        # screen from the others. It has to stay well clear of how little green
        # the sparsest phone shows: the android-one measures 0.070 there, because
        # TRADE_BTN sits in the gap between the two lines of "LOCAL TRADE"
        # (points 40px away score 0.13), so a 0.07 floor rejected the very
        # screen it was meant to accept.
        return green >= 0.05 and 0.20 <= white <= 0.80 and pale < 0.80
    if expected == "selection":
        return pale >= 0.80
    if expected == "next":
        return green >= 0.55
    if expected == "lobby":
        # The trade screen keeps the same shape either side of CONFIRM: before
        # it, a green CONFIRM pill sits under the coordinate; after it, that
        # pill becomes an amber CANCEL while this phone waits on the other
        # trainer. Reading only the green turned the second half into a screen
        # nothing recognised, and a phone left waiting there was walked past by
        # every recovery round until the game gave up with "Trade expired."
        return green >= 0.55 or metrics["action_orange"] >= 0.55
    if expected == "post_trade":
        if green < 0.04 or pale >= 0.80:
            return False
        if white >= 0.82:
            return True
        # How much of white_panel's band the card fills depends on how many
        # rows of card the handset fits into it. The android-three's 1224x2992 panel
        # gets the Dynamax row, both pill buttons and the top of the move list
        # in there and measures 0.809-0.814 where the android-one measures 0.906-
        # 0.911, so the 0.82 floor turned away a card that was on screen with
        # its X button plainly visible. Dropping the floor instead is no good:
        # the android-three's own friend screen measures 0.771.
        #
        # The head of the card is the same rows on every handset. It reads
        # 0.905-0.939 against 0.33-0.40 for a friend screen — but on its own it
        # would also take the "Are you sure you want to trade this Pokémon?"
        # dialog, which measured 0.818 on an iPhone stopped at CONFIRM_BTN.
        # That dialog floats: the outer columns stay dark (0.332) where a card
        # reaches them (0.801-0.858). Both together admit the android-three without
        # letting go of anything the white floor was holding back.
        return metrics["card_band"] >= 0.80 and metrics["card_edges"] >= 0.60
    raise ValueError(f"Unknown expected screen state: {expected}")


# Pokémon GO floats its dialogs — "Do you want to cancel the trade?", "Are you
# sure you want to trade this Pokémon?" — on a dimmed copy of the screen
# underneath. The white card fills the head band the same way a post-trade card
# does, but it stops well short of the screen's outer columns: measured 0.804/
# 0.178 on the android-three, 0.840/0.220 on the android-one and 0.818/0.332 on an iPhone,
# against 0.90+/0.80+ for a card and 0.33-0.40 in the band for a friend screen.
FLOATING_DIALOG_BAND = 0.80
FLOATING_DIALOG_EDGES = 0.40

# Every screen the trade sequence can be left standing on, in the order they
# are tried. Only these are ever acted on during recovery; see
# pokemon_fleet.step_towards_friend for what each one answers to.
DESCRIBED_STATES = ("friend", "selection", "post_trade", "next", "lobby")


def is_floating_dialog(metrics: dict[str, float]) -> bool:
    """True for a dialog sitting on top of a dimmed screen."""
    return (
        metrics["card_band"] >= FLOATING_DIALOG_BAND
        and metrics["card_edges"] < FLOATING_DIALOG_EDGES
    )


# Finding the dialog's own button in the picture, rather than mapping one
# coordinate per dialog per handset. There is no single "the dialog": a run
# meets at least "Do you want to cancel the trade?" (YES above a NO link),
# "Do you want to make a Special Trade?" (the same shape) and "Trade expired."
# (one OK), and their buttons sit at different heights. What they share is a
# green gradient pill inside the white card, with the card's padding either
# side of it — nothing else on these screens looks like that, because the
# dimmed background behind the card has no white in it to flank anything.
#
# Validated against six captures: the found centre lands 1-11px from the
# hand-mapped button on the android-three (1224x2992), the android-one (1316x2560), the android-two
# (720x1600) and an iPhone SE (750x1334), where the pills are 400-680px wide
# and 75-110px tall.
DIALOG_SCAN_WIDTH = 240
DIALOG_PILL_MIN_SHARE = 0.20
# Anti-aliasing puts a blended pixel or two between the pill and the card, so
# the white flank is looked for just outside the run rather than against it.
DIALOG_PILL_FLANK = 4
DIALOG_PILL_ROW_GAP = 3
DIALOG_PILL_MIN_ROWS = 0.010

# Not every notice is a card. "New Mega Level available!" dims the screen
# underneath and writes straight onto it, so its OK pill has the dimmed detail
# screen either side of it and no white to be flanked by — find_dialog_button
# read nothing there, the caller fell back to the mapped TRADE_CANCEL_YES
# coordinate, and a android-one spent all four recovery rounds pressing empty
# background. Those pills are found by shape instead: a pill is thick, and the
# other wide green runs on such a screen are thin. Measured on the android-one's mega
# notice (1316x2560, scaled to 240x466): the pill fills rows 374-407 — 0.073 of
# the height, 0.53 of the width, centred — against an HP bar four rows tall at
# y 0.502 and a card's pills at 0.037-0.082 of the height across the fleet.
# Centred as well as thick because without the card there is nothing bounding
# where a stray green run may be found, and every one of these pills is centred.
OVERLAY_PILL_MIN_ROWS = 0.030
OVERLAY_PILL_MAX_OFFSET = 0.12
# And a button has the screen either side of it. Without the card's white to
# be flanked by, this is what keeps a full-bleed green band — a banner, a
# progress track — from reading as one. The pills measure 0.33-0.56 of the
# width across the fleet, the mega notice's OK 0.53.
OVERLAY_PILL_MAX_SHARE = 0.75


def _dialog_pill_rows(small: Any, flanked: bool = True) -> list[tuple[int, float]]:
    """Rows of a scaled dialog holding a wide green run.

    ``flanked`` asks for the card's white either side of the run, which is what
    tells a card's pill from anything else green on the screen. Only the
    overlay notices, which have no card, are read without it.
    """
    width, height = small.size
    pixels = small.load()
    found: list[tuple[int, float]] = []
    for y in range(height):
        row = [pixels[x, y] for x in range(width)]
        best_left = best_right = 0
        start: int | None = None
        for x in range(width + 1):
            green = x < width and _is_green(*row[x])
            if green and start is None:
                start = x
            elif not green and start is not None:
                if x - start > best_right - best_left:
                    best_left, best_right = start, x
                start = None
        if best_right - best_left < width * DIALOG_PILL_MIN_SHARE:
            continue
        if flanked:
            flanked_left = any(
                _is_white(*row[x])
                for x in range(max(0, best_left - DIALOG_PILL_FLANK), best_left)
            )
            flanked_right = any(
                _is_white(*row[x])
                for x in range(best_right, min(width, best_right + DIALOG_PILL_FLANK))
            )
            if not (flanked_left and flanked_right):
                continue
        elif best_right - best_left > width * OVERLAY_PILL_MAX_SHARE:
            continue
        found.append((y, (best_left + best_right) / 2))
    return found


def _pill_bands(
    rows: Sequence[tuple[int, float]], scaled_height: int, min_share: float
) -> list[list[tuple[int, float]]]:
    """Group rows into runs, keeping the ones thick enough to be a button."""
    if not rows:
        return []
    bands: list[list[tuple[int, float]]] = [[rows[0]]]
    for row in rows[1:]:
        if row[0] - bands[-1][-1][0] <= DIALOG_PILL_ROW_GAP:
            bands[-1].append(row)
        else:
            bands.append([row])
    min_rows = max(3, int(scaled_height * min_share))
    return [band for band in bands if band[-1][0] - band[0][0] + 1 >= min_rows]


def find_dialog_button(image: Any) -> tuple[int, int] | None:
    """Centre of the topmost pill in a dialog, or None if it has no pill.

    Topmost because that is the answer that moves a phone on: YES sits above
    NO on the two-button dialogs, and the one-button dialogs have only OK.
    None is a real answer — a screen that reads as a dialog but holds no pill
    is not one this can press, and the caller leaves it alone.
    """
    width, height = image.size
    scaled_height = max(1, int(DIALOG_SCAN_WIDTH * height / width))
    small = image.resize((DIALOG_SCAN_WIDTH, scaled_height))
    bands = _pill_bands(_dialog_pill_rows(small), scaled_height, DIALOG_PILL_MIN_ROWS)
    if not bands:
        # No card, so no white flanks: an overlay notice, whose pill has to
        # earn it by being thick and centred instead.
        middle = DIALOG_SCAN_WIDTH / 2
        bands = [
            band
            for band in _pill_bands(
                _dialog_pill_rows(small, flanked=False), scaled_height, OVERLAY_PILL_MIN_ROWS
            )
            if abs(sum(row[1] for row in band) / len(band) - middle)
            <= DIALOG_SCAN_WIDTH * OVERLAY_PILL_MAX_OFFSET
        ]
    if not bands:
        return None
    top = bands[0]
    center_y = (top[0][0] + top[-1][0]) / 2
    center_x = sum(row[1] for row in top) / len(top)
    return (
        int(center_x * width / DIALOG_SCAN_WIDTH),
        int(center_y * height / scaled_height),
    )


def is_map_screen(image: Any) -> bool:
    """True for the game's map — the pokéball at the bottom is what says so.

    Worth its own name because the map is not a trade screen at all and the
    colour tests cannot see that: a android-two that had fallen out to the map read
    as a Pokémon's detail screen, because the grass under NEXT_BTN's
    coordinate is as green as the button that belongs there. Recovery then
    pressed BACK on it, which is how a trade run ends up in front of "Do you
    want to exit Pokémon GO?".

    It answers the same way to the game's icon on a launcher screen, which is
    also a pokéball near the bottom of the picture; asking the phone which app
    is in front is what tells those two apart, and step_towards_friend does.
    Across the 44 real trade frames kept from failed runs it never once fired.
    """
    from . import gbl_home_recovery

    return gbl_home_recovery.find_pokeball(image) is not None


def describe_state(
    image: Any, action_point: Callable[[str], tuple[int, int] | None]
) -> tuple[str, dict[str, float]]:
    """Name the screen a phone is on, and return the metrics that named it.

    Each state is measured at its own action button, because action_green only
    says anything under the button that state expects to find there: a lobby
    read at TRADE_BTN looks like nothing at all. "unknown" is a real answer and
    the common one for anything outside the trade sequence — the caller is
    expected to leave those screens alone rather than guess at them.
    """
    metrics = screen_metrics(image, action_point("TRADE_BTN"))
    if is_floating_dialog(metrics):
        return "dialog", metrics
    for state in DESCRIBED_STATES:
        action_name = STATE_ACTION_COORDINATE.get(state)
        point = action_point(action_name) if action_name else None
        candidate = screen_metrics(image, point)
        if state_matches(state, candidate):
            # The friend screen is checked first and covers the map entirely,
            # so a phone that is home is never called anything else. Any other
            # match is only a colour under one coordinate, and the map wins
            # over it.
            if state == "friend" or not is_map_screen(image):
                return state, candidate
            break
    if is_map_screen(image):
        return "map", metrics
    if is_android_waiting_lobby(image):
        return "lobby", metrics
    if is_empty_handed_trade_screen(image):
        return "empty_lobby", metrics
    return "unknown", metrics


# The trade screen a phone sits on once the other trainer has offered and this
# one has not: the partner's card at the top, the flat trade blue everywhere
# else, and no pill of any colour. Nothing named it, so recovery left it alone
# for every round it had and the phone was still standing in a trade when the
# run ended — which is the trade the game then cancels and reports as expired.
#
# Measured on the lower band, where the screens it has to be told apart from
# put something: the in-trade detail screen fills 0.775 of it with blue and
# 0.123 with the green of its NEXT pill, against 1.000 and 0.003 here. The
# picker, the friend screen and a dialog measure 0.000 blue there.
EMPTY_LOBBY_BLUE = 0.90
EMPTY_LOBBY_GREEN = 0.03


def _is_trade_blue(red: int, green: int, blue: int) -> bool:
    return (
        85 <= red <= 130
        and 165 <= green <= 210
        and blue >= 220
        and blue - green >= 25
        and green - red >= 35
    )


def is_empty_handed_trade_screen(image: Any) -> bool:
    """True for a trade this phone has not yet put a Pokémon into."""
    blue = _fraction_in_crop(image, (0.05, 0.60, 0.95, 0.88), _is_trade_blue)
    if blue < EMPTY_LOBBY_BLUE:
        return False
    pill = _fraction_in_crop(image, (0.10, 0.60, 0.90, 0.95), _is_green)
    return pill < EMPTY_LOBBY_GREEN


def is_ios_trading_unavailable_dialog(image: Any) -> bool:
    """Match the location-error banner and its trading-unavailable dialog."""
    red_banner = _fraction_in_crop(
        image,
        (0.0, 0.0, 1.0, 0.12),
        lambda red, green, blue: (
            red >= 180 and red - green >= 35 and red - blue >= 20
        ),
    )
    white_dialog = _fraction_in_crop(image, (0.05, 0.28, 0.95, 0.62), _is_white)
    green_ok = _fraction_in_crop(
        image,
        (0.20, 0.42, 0.80, 0.54),
        lambda red, green, blue: (
            green - red >= 20
            and green >= blue - 10
            and max(red, green, blue) - min(red, green, blue) >= 45
        ),
    )
    return red_banner >= 0.65 and white_dialog >= 0.45 and green_ok >= 0.08


# Pokémon GO cancels a trade it has been kept waiting on and says so with a
# one-button "Trade expired." notice. Nothing about its shape tells it apart
# from the dialogs that ask a question — same white card, same green pill in
# the middle of it — and the answer to one of those is a trade going through,
# so this is the one place in the trade path that reads words rather than
# colour. The wording moves with who was traded with: "Trade expired." over
# "This trade was canceled." on the android-two, "This trade with <trainer> has
# expired" on the android-three, so both are matched instead of one fixed line. Neither
# reading takes a question: "Do you want to cancel the trade?" has no "expired"
# in it and no "was canceled" either.
def is_trade_expired_dialog(image: Any) -> bool:
    """True for the game's own notice that it has cancelled the trade.

    Gated on the cheap dialog geometry first, so the OCR only ever runs on a
    screen already known to be a dialog — about half a second, against a guard
    that would otherwise sit out its whole allowance in front of a screen no
    amount of waiting is going to change.
    """
    if not is_floating_dialog(screen_metrics(image, None)):
        return False
    from . import gbl_vision

    try:
        boxes = gbl_vision.recognize(image)
    except (gbl_vision.VisionOCRError, OSError):
        # A trade run is not worth ending because the OCR helper would not
        # start. Unread, the screen stays whatever the colour tests made of it.
        return False
    text = gbl_vision.normalize(" ".join(gbl_vision.lines(boxes)))
    return ("trade" in text and "expired" in text) or "trade was canceled" in text


def is_trade_limit_dialog(image: Any) -> bool:
    """True for "Daily trading limit reached. Come back tomorrow to trade more."

    The end of a day's trading, and nothing about it is a fault: the pair had
    done 12 trades this run when both phones showed it at FIRST_PKMN_BTN. Read
    by its words for the same reason the expired notice is — it wears the card
    and the single green pill every other dialog wears, and the run otherwise
    spends three retries and four recoveries finding out that tomorrow is the
    only thing that will fix it.
    """
    if not is_floating_dialog(screen_metrics(image, None)):
        return False
    from . import gbl_vision

    try:
        boxes = gbl_vision.recognize(image)
    except (gbl_vision.VisionOCRError, OSError):
        return False
    text = gbl_vision.normalize(" ".join(gbl_vision.lines(boxes)))
    return ("trading limit" in text or "trade limit" in text) and "reached" in text


def is_exit_game_dialog(image: Any) -> bool:
    """True for "Do you want to exit Pokémon GO?".

    This one matters more than the rest: its topmost pill is OK, and OK closes
    the game. Recovery presses the topmost pill on every dialog it meets, so
    without this the way out of a trade ends with Pokémon GO shut down on a
    phone nobody is watching. A android-two reached this screen with one recovery
    round left to spend.
    """
    if not is_floating_dialog(screen_metrics(image, None)):
        return False
    from . import gbl_vision

    try:
        boxes = gbl_vision.recognize(image)
    except (gbl_vision.VisionOCRError, OSError):
        return False
    text = gbl_vision.normalize(" ".join(gbl_vision.lines(boxes)))
    return ("exit" in text or "quit" in text) and "pokemon go" in text


# Notices the game raises by itself, on top of whatever screen the run had
# reached, about something the run did not do and does not care about. They
# have one button, pressing it puts the phone back where it was, and the trade
# carries on — but only the guard knows that, so it dismisses them and looks
# again rather than counting a phone standing behind one as a failed trade.
#
# "New Mega Level available!" arrives on the post-trade card when a traded
# Pokémon's mega level moves, which happens often enough to have stopped two
# runs (2026-09-02 and 2026-09-08, both at X_BTN, both on the android-one).
#
# Matched by their words, and only these words: every question the game asks
# during a trade wears the same green pill, and pressing one of those puts a
# trade through or cancels one. Anything saying "trade" is left to the branches
# that know what that trade was, which is why "trade" disqualifies a notice
# here rather than being one more phrase to match.
INCIDENTAL_NOTICES = {
    "mega level": "New Mega Level available!",
}


def incidental_notice(image: Any) -> str | None:
    """The name of a notice the game raised by itself, or None.

    Gated on the cheap dialog geometry first, so the OCR only runs on a screen
    already known to be a dialog — the same half-second the expired and limit
    notices pay.
    """
    if not is_floating_dialog(screen_metrics(image, None)):
        return None
    from . import gbl_vision

    try:
        boxes = gbl_vision.recognize(image)
    except (gbl_vision.VisionOCRError, OSError):
        # Unread, the screen stays whatever the colour tests made of it, and
        # recovery walks the phone home the long way.
        return None
    text = gbl_vision.normalize(" ".join(gbl_vision.lines(boxes)))
    if "trade" in text:
        return None
    for phrase, name in INCIDENTAL_NOTICES.items():
        if phrase in text:
            return name
    return None


# Measured on the android-two's exit prompt (720x1600): the card runs y 0.363-0.632,
# the OK pill fills y 0.470-0.533 at 0.56-0.69 green across the row, and the
# CANCEL label below it is white card carrying 0.08-0.13 green. A pill is a
# solid run; a label is a scattering, which is what tells the two apart.
DIALOG_LABEL_MIN_GREEN = 0.02
DIALOG_LABEL_MAX_GREEN = 0.35
DIALOG_LABEL_MIN_ROWS = 2


def _is_green_ink(red: int, green: int, blue: int) -> bool:
    """Looser than _is_green: a thin label is anti-aliased towards the card."""
    return green > red + 30 and green > blue + 10


def find_dialog_label_button(image: Any) -> tuple[int, int] | None:
    """The plain-text choice under the pill — CANCEL, in image pixels.

    Reads the rows below the pill rather than a mapped coordinate, because the
    card's height moves with the length of the question above it.
    """
    pill = find_dialog_button(image)
    if pill is None:
        return None
    width, height = image.size
    stride = max(1, width // 180)
    left, right = int(0.10 * width), int(0.90 * width)
    columns = range(left, right, stride)
    runs: list[list[tuple[int, float]]] = []
    for y in range(pill[1] + 1, height):
        row = [image.getpixel((x, y)) for x in columns]
        ink = sum(1 for pixel in row if _is_green_ink(*pixel[:3])) / len(row)
        white = sum(1 for pixel in row if _is_white(*pixel[:3])) / len(row)
        if white < 0.5 or not DIALOG_LABEL_MIN_GREEN <= ink <= DIALOG_LABEL_MAX_GREEN:
            if runs and runs[-1]:
                runs.append([])
            continue
        marks = [x for x, pixel in zip(columns, row) if _is_green_ink(*pixel[:3])]
        if not runs:
            runs.append([])
        runs[-1].append((y, sum(marks) / len(marks)))
    for run in runs:
        if len(run) >= DIALOG_LABEL_MIN_ROWS:
            return (
                int(sum(point[1] for point in run) / len(run)),
                (run[0][0] + run[-1][0]) // 2,
            )
    return None


def is_android_waiting_lobby(image: Any) -> bool:
    """Match the sparse blue 'Waiting for ... to be available' trade lobby."""
    waiting_blue = _fraction_in_crop(
        image,
        (0.05, 0.18, 0.95, 0.42),
        lambda red, green, blue: (
            85 <= red <= 125
            and 170 <= green <= 205
            and blue >= 225
            and blue - green >= 30
            and green - red >= 35
        ),
    )
    detail_panel_blue = _fraction_in_crop(
        image,
        (0.05, 0.18, 0.95, 0.42),
        lambda red, green, blue: (
            80 <= red <= 115 and 135 <= green <= 170 and blue >= 210
        ),
    )
    return waiting_blue >= 0.85 and detail_panel_blue <= 0.05


async def android_screenshot_image(device: Any) -> Any:
    frame = await android_trade.screencap_raw(device)
    if frame is None:
        raise CrossPlatformTradeError(f"Could not capture Android screen: {device.serial}")
    width, height, offset, data = frame
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        raise CrossPlatformTradeError(
            "Pillow is missing; install requirements-cross-platform.txt"
        ) from exc
    return Image.frombytes(
        "RGBA",
        (width, height),
        data[offset : offset + width * height * 4],
    ).convert("RGB")


class IOSController:
    def __init__(
        self,
        driver: Any,
        coordinates: dict[str, list[int]],
        bundle_id: str = "com.nianticlabs.pokemongo",
    ) -> None:
        self.driver = driver
        self.coordinates = coordinates
        self.bundle_id = bundle_id
        self.viewport = driver.get_window_rect()

    @classmethod
    def connect(cls, config: RuntimeConfig) -> "IOSController":
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ModuleNotFoundError as exc:
            raise CrossPlatformTradeError(
                "Appium client is missing; install requirements-cross-platform.txt"
            ) from exc

        device = config.ios_device
        asleep = ios_wda_cleanup.asleep_message(
            device["udid"], device.get("name", "The iPhone")
        )
        if asleep:
            raise CrossPlatformTradeError(asleep)

        # Reattaching to a warm WDA: see the note in gbl_ios.connect. A detached
        # one answers /status and then fails every tap.
        ios_wda_cleanup.clear_detached_wda(
            device["udid"],
            device.get("wda_local_port") or ios_wda_cleanup.WDA_DEFAULT_PORT,
        )
        capabilities = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            "appium:udid": device["udid"],
            "appium:deviceName": device.get("name", "iPhone"),
            "appium:bundleId": device.get("bundle_id", "com.nianticlabs.pokemongo"),
            "appium:noReset": True,
            "appium:shouldTerminateApp": False,
            "appium:xcodeOrgId": device["team_id"],
            "appium:xcodeSigningId": device.get(
                "xcode_signing_id", "Apple Development"
            ),
            "appium:updatedWDABundleId": device["wda_bundle_id"],
            "appium:allowProvisioningDeviceRegistration": True,
            "appium:useNewWDA": False,
            "appium:newCommandTimeout": 3600,
        }
        if isinstance(device.get("platform_version"), str):
            capabilities["platformVersion"] = device["platform_version"]
        if isinstance(device.get("wda_local_port"), int):
            capabilities["appium:wdaLocalPort"] = device["wda_local_port"]
        if isinstance(device.get("mjpeg_server_port"), int):
            capabilities["appium:mjpegServerPort"] = device["mjpeg_server_port"]
        if isinstance(device.get("derived_data_path"), str):
            capabilities["appium:derivedDataPath"] = config_paths.expand_user_path(
                device["derived_data_path"]
            )
        if device.get("use_preinstalled_wda"):
            capabilities["appium:usePreinstalledWDA"] = True

        name = device.get("name", "iPhone")
        try:
            driver = webdriver.Remote(
                command_executor=config.appium_server_url,
                options=XCUITestOptions().load_capabilities(capabilities),
            )
        except Exception as exc:
            if capabilities.get("appium:usePreinstalledWDA", False):
                print(
                    f"[{name}] Preinstalled WebDriverAgent would not start ({exc});"
                    " restarting the installed runner once",
                    flush=True,
                )
                ios_wda_cleanup.stop_wda_runner(device["udid"])
                time.sleep(1)
                try:
                    driver = webdriver.Remote(
                        command_executor=config.appium_server_url,
                        options=XCUITestOptions().load_capabilities(capabilities),
                    )
                except Exception as retry_exc:
                    raise CrossPlatformTradeError(
                        f"Could not create the iOS Appium session for {name} ({device['udid']}): {retry_exc}"
                    ) from retry_exc
            else:
                raise CrossPlatformTradeError(
                    f"Could not create the iOS Appium session for {name} ({device['udid']}): {exc}"
                ) from exc
        return cls(driver, config.ios_coordinates, capabilities["appium:bundleId"])

    async def tap_step(self, name: str) -> bool:
        point = self.coordinates.get(name)
        if point is None:
            return False
        await self.tap_point((point[0], point[1]))
        return True

    async def tap_point(self, point: tuple[int, int]) -> None:
        await asyncio.to_thread(
            self.driver.execute_script,
            "mobile: tap",
            {"x": point[0], "y": point[1]},
        )

    def _screenshot_image(self) -> Any:
        try:
            from PIL import Image
        except ModuleNotFoundError as exc:
            raise CrossPlatformTradeError(
                "Pillow is missing; install requirements-cross-platform.txt"
            ) from exc
        return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")

    async def screenshot_image(self) -> Any:
        return await asyncio.to_thread(self._screenshot_image)

    def image_point(self, image: Any, name: str) -> tuple[int, int] | None:
        point = self.coordinates.get(name)
        if point is None:
            return None
        return (
            int(point[0] * image.width / self.viewport["width"]),
            int(point[1] * image.height / self.viewport["height"]),
        )

    def point_from_image(self, image: Any, pixel: tuple[int, int]) -> tuple[int, int]:
        """Turn a pixel read off a screenshot back into a tappable point.

        A screenshot is in device pixels and `mobile: tap` wants logical
        points, so anything located by looking at the picture has to come back
        through here — on a 2x phone a pixel tapped as a point lands at half
        the height it was found at.
        """
        return (
            int(pixel[0] * self.viewport["width"] / image.width),
            int(pixel[1] * self.viewport["height"] / image.height),
        )

    def _post_trade_cleanup(self) -> None:
        cancel = self.coordinates.get("POWER_UP_CANCEL_BTN")
        if cancel:
            try:
                if "power up" in self.driver.page_source.lower():
                    print("    iOS Power Up screen detected — cancelling")
                    self.driver.execute_script("mobile: tap", {"x": cancel[0], "y": cancel[1]})
                    time.sleep(1)
            except Exception as exc:
                print(f"    iOS Power Up check skipped: {exc}")

        x_point = self.coordinates["X_BTN"]
        for _ in range(RECORD_MAX_ATTEMPTS):
            image = self._screenshot_image()
            if not has_record_card_image(image):
                return
            # This tap is the one the Android side warns about: on a screen
            # that needed no tap it desyncs the loop, and the run then dies a
            # step or two later somewhere that says nothing about why. Keep the
            # frame that triggered it so the next stop can be read back.
            saved = save_record_card_frame(image, "ios")
            print(f"    iOS size record card detected — dismissing (frame: {saved})")
            self.driver.execute_script(
                "mobile: tap", {"x": x_point[0], "y": x_point[1]}
            )
            time.sleep(RECORD_DISMISS_DELAY)

    async def post_trade_cleanup(self) -> None:
        await asyncio.to_thread(self._post_trade_cleanup)

    def _clear_system_overlay(self) -> bool:
        """Put Pokémon GO back in front of a SpringBoard card. True if it acted.

        Deliberately narrow: it only fires when something *else* holds the
        foreground, so an ordinary wrong-screen mismatch still fails closed
        rather than being papered over by an activate.
        """
        try:
            info = self.driver.execute_script("mobile: activeAppInfo")
        except Exception as exc:
            print(f"    iOS foreground check skipped: {exc}")
            return False
        front = info.get("bundleId") if isinstance(info, dict) else None
        if front is None or front == self.bundle_id:
            return False

        try:
            state = self.driver.execute_script(
                "mobile: queryAppState", {"bundleId": self.bundle_id}
            )
            # Activating an app that is not running *launches* it, and a cold
            # start drops the trade picker's hand-typed search filter, which
            # nothing here can retype. A dead app is left for a human.
            if state <= IOS_APP_STATE_NOT_RUNNING:
                print(
                    f"    iOS: {self.bundle_id} is not running (state {state}) — "
                    "leaving it alone rather than cold-starting the game"
                )
                return False
            print(f"    iOS foreground is {front} — restoring Pokémon GO")
            self.driver.execute_script(
                "mobile: activateApp", {"bundleId": self.bundle_id}
            )
        except Exception as exc:
            print(f"    iOS foreground restore failed: {exc}")
            return False
        return True

    async def clear_system_overlay(self) -> bool:
        return await asyncio.to_thread(self._clear_system_overlay)

    async def quit(self) -> None:
        await asyncio.to_thread(self.driver.quit)


RECORD_DIAGNOSTICS_DIR = ARTIFACT_ROOT / "trades" / "record-cards"


def save_record_card_frame(image: Any, name: str) -> Path:
    """Keep the frame a size-record dismissal fired on, named for the phone."""
    RECORD_DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORD_DIAGNOSTICS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
    image.save(path)
    return path


async def android_post_trade_cleanup(device: Any) -> None:
    await android_trade.dismiss_power_up_screen([device])
    await android_trade.dismiss_record_screen([device])


def save_mismatch_diagnostics(
    android_image: Any,
    ios_image: Any,
    step_name: str,
) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = TRADE_DIAGNOSTICS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    android_path = directory / f"trade-mismatch-{stamp}-{step_name}-android.png"
    ios_path = directory / f"trade-mismatch-{stamp}-{step_name}-ios.png"
    android_image.save(android_path)
    ios_image.save(ios_path)
    return directory


async def recover_ios_trade_entry(
    android: Any,
    ios: IOSController,
    initial_android_image: Any,
    initial_ios_image: Any,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    """Rejoin an Android waiting lobby after the known iOS service error."""
    recovery_attempt = 0
    unknown_attempts = 0
    pending_images: tuple[Any, Any] | None = (
        initial_android_image,
        initial_ios_image,
    )

    while True:
        if pending_images is not None:
            android_image, ios_image = pending_images
            pending_images = None
        else:
            android_image, ios_image = await asyncio.gather(
                android_screenshot_image(android),
                ios.screenshot_image(),
            )

        android_selection = state_matches(
            "selection", screen_metrics(android_image, None)
        )
        ios_selection = state_matches("selection", screen_metrics(ios_image, None))
        if android_selection and ios_selection:
            print("    Trade recovery complete — both selection grids are ready")
            return

        android_waiting = is_android_waiting_lobby(android_image)
        ios_unavailable = is_ios_trading_unavailable_dialog(ios_image)
        if android_waiting and ios_unavailable:
            recovery_attempt += 1
            backoff = min(
                RECOVERY_BACKOFF_START * (2 ** min(recovery_attempt - 1, 3)),
                RECOVERY_BACKOFF_MAX,
            )
            ok_point = ios.coordinates.get(
                "IOS_TRADING_UNAVAILABLE_OK_BTN",
                IOS_TRADING_UNAVAILABLE_OK_POINT,
            )
            print(
                "    Recoverable split state: Android is waiting and iOS trade "
                f"service is unavailable (retry {recovery_attempt}; wait {backoff:g}s)"
            )
            await ios.tap_point((ok_point[0], ok_point[1]))
            unknown_attempts = 0
            await sleep(backoff)
            continue

        if android_waiting:
            trade_point = ios.image_point(ios_image, "TRADE_BTN")
            ios_friend = state_matches(
                "friend", screen_metrics(ios_image, trade_point)
            )
            if ios_friend:
                backoff = min(
                    RECOVERY_BACKOFF_START * (2 ** min(recovery_attempt, 3)),
                    RECOVERY_BACKOFF_MAX,
                )
                print(
                    "    iOS friend screen restored — re-entering trade on iOS only; "
                    f"Android remains waiting (wait {backoff:g}s)"
                )
                await ios.tap_step("TRADE_BTN")
                unknown_attempts = 0
                await sleep(backoff)
                continue

        unknown_attempts += 1
        if unknown_attempts >= RECOVERY_UNKNOWN_ATTEMPTS:
            directory = save_mismatch_diagnostics(
                android_image,
                ios_image,
                "FIRST_PKMN_BTN-RECOVERY",
            )
            raise CrossPlatformTradeError(
                "Trade-entry recovery reached an unclassified screen state. "
                "Stopped without blind tapping; diagnostics saved in "
                f"{directory}."
            )
        if unknown_attempts in {1, 5, 10}:
            print(
                "    Trade recovery is waiting for a recognized screen transition "
                f"({unknown_attempts}/{RECOVERY_UNKNOWN_ATTEMPTS})"
            )
        await sleep(RECOVERY_POLL_DELAY)


async def close_stale_post_trade_card(
    android: Any,
    ios: IOSController,
    android_metrics: dict[str, float],
    ios_metrics: dict[str, float],
) -> bool:
    """Tap X_BTN on whichever phone is still showing a post-trade card.

    Only the straggler is tapped. Sending X_BTN to a phone already back on the
    friend screen would open that friend's Pokémon list instead, so the sides
    are decided independently. `post_trade` is the same classification the
    X_BTN step itself guards on, not a new one invented here — though note the
    green is sampled at the friend screen's TRADE_BTN point, so white_panel is
    doing the real work of telling the card apart.
    """
    taps = []
    stragglers = []
    if state_matches("post_trade", ios_metrics):
        stragglers.append("iOS")
        taps.append(ios.tap_step("X_BTN"))
    if state_matches("post_trade", android_metrics):
        android_point = android.config.get("X_BTN")
        if android_point:
            stragglers.append("Android")
            taps.append(android_trade.tap(android, android_point))
    if not taps:
        return False
    print(
        f"    Post-trade card still open on {' and '.join(stragglers)} — "
        "closing it to resync the two phones"
    )
    await asyncio.gather(*taps)
    return True


async def guard_screen_state(
    android: Any,
    ios: IOSController,
    expected: str,
    step_name: str,
) -> None:
    android_metrics: dict[str, float] = {}
    ios_metrics: dict[str, float] = {}
    android_image = None
    ios_image = None
    action_name = STATE_ACTION_COORDINATE.get(expected)
    overlay_recoveries = 0
    post_trade_resyncs = 0

    for attempt in range(1, STATE_GUARD_ATTEMPTS + 1):
        android_image, ios_image = await asyncio.gather(
            android_screenshot_image(android),
            ios.screenshot_image(),
        )
        android_point = None
        if action_name:
            raw = android.config.get(action_name)
            if raw:
                android_point = (raw[0], raw[1])
        ios_point = ios.image_point(ios_image, action_name) if action_name else None
        android_metrics = screen_metrics(android_image, android_point)
        ios_metrics = screen_metrics(ios_image, ios_point)
        if state_matches(expected, android_metrics) and state_matches(expected, ios_metrics):
            return
        if (
            expected == "selection"
            and is_android_waiting_lobby(android_image)
            and is_ios_trading_unavailable_dialog(ios_image)
        ):
            await recover_ios_trade_entry(
                android,
                ios,
                android_image,
                ios_image,
            )
            return
        if (
            not state_matches(expected, ios_metrics)
            and overlay_recoveries < IOS_OVERLAY_RECOVERY_ATTEMPTS
            and await ios.clear_system_overlay()
        ):
            overlay_recoveries += 1
            await asyncio.sleep(IOS_OVERLAY_SETTLE_DELAY)
            continue
        if (
            expected == "friend"
            and post_trade_resyncs < POST_TRADE_RESYNC_ATTEMPTS
            and await close_stale_post_trade_card(
                android, ios, android_metrics, ios_metrics
            )
        ):
            post_trade_resyncs += 1
            await asyncio.sleep(POST_TRADE_RESYNC_DELAY)
            continue
        if attempt < STATE_GUARD_ATTEMPTS:
            print(
                f"    Screen guard waiting before {step_name} "
                f"({attempt}/{STATE_GUARD_ATTEMPTS})"
            )
            await asyncio.sleep(STATE_GUARD_DELAY)

    directory = save_mismatch_diagnostics(android_image, ios_image, step_name)
    raise CrossPlatformTradeError(
        f"Screen mismatch before {step_name}; expected {expected}. "
        f"Android metrics={android_metrics}; iOS metrics={ios_metrics}. "
        f"Stopped without tapping; diagnostics saved in {directory}."
    )


async def execute_trade_sequence(
    android: Any,
    ios: IOSController,
    steps: tuple[TradeStep, ...],
    delay_modifier: float,
    dry_run: bool = False,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> None:
    for step in steps:
        delay = max(
            step.delay_after + (delay_modifier if step.use_delay_modifier else 0),
            0,
        )
        ios_enabled = step.name in ios.coordinates
        platforms = "Android + iOS" if ios_enabled else "Android only"
        print(f"    {step.name}: {platforms}; wait {delay:g}s")
        if not dry_run:
            expected = STEP_EXPECTED_STATE.get(step.name)
            if expected:
                await guard_screen_state(android, ios, expected, step.name)
            android_point = android.config.get(
                step.name,
                next(
                    (item.default_coords for item in android_trade.BUTTONS if item.name == step.name),
                    None,
                ),
            )
            if android_point is None:
                raise CrossPlatformTradeError(
                    f"Android coordinate {step.name} is unavailable"
                )
            tasks = [android_trade.tap(android, android_point)]
            if ios_enabled:
                tasks.append(ios.tap_step(step.name))
            await asyncio.gather(*tasks)
            await sleep(delay)
            if step.name == "CONFIRM_BTN":
                await asyncio.gather(
                    android_post_trade_cleanup(android),
                    ios.post_trade_cleanup(),
                )
    if not dry_run:
        await guard_screen_state(android, ios, "friend", "TRADE_COMPLETE")


async def run_trades(
    android: Any,
    ios: IOSController,
    config: RuntimeConfig,
    count: int,
    dry_run: bool,
) -> None:
    if not dry_run:
        await android_trade.pointer([android], True)
    try:
        for number in range(1, count + 1):
            print(f"  Starting cross-platform trade {number} of {count}")
            await execute_trade_sequence(
                android,
                ios,
                config.steps,
                config.delay_modifier,
                dry_run=dry_run,
            )
    finally:
        if not dry_run:
            await android_trade.pointer([android], False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=config_paths.default_config("ios-android-trader.yaml"),
        help="cross-platform trader YAML file",
    )
    parser.add_argument("--ios-config", type=Path, help="override the shared Appium YAML")
    parser.add_argument("--android-serial", help="select one Android when several are connected")
    parser.add_argument("--delay-modifier", type=float, help="add seconds to network-bound steps")
    parser.add_argument(
        "--count", type=int, default=100, help="how many trades to run (default 100)"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and devices without creating an Appium session",
    )
    parser.add_argument("--dry-run", action="store_true", help="connect but do not tap")
    parser.add_argument(
        "--capture-ios",
        type=Path,
        metavar="PNG",
        help="capture an untapped iOS screenshot for coordinate calibration",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    if args.count < 1:
        raise CrossPlatformTradeError("--count must be at least 1")
    config = load_runtime_config(
        args.config.resolve(),
        args.ios_config.resolve() if args.ios_config else None,
        args.android_serial,
        args.delay_modifier,
        allow_placeholders=args.capture_ios is not None,
    )
    check_appium_status(config.appium_server_url)
    gift_pids = active_gift_runner_pids()
    print(f"Appium ready: {config.appium_server_url}")
    if gift_pids:
        print("iOS busy: gift_ios.py is currently using Appium")
    else:
        print("iOS ready: no gift runner process detected")

    if args.capture_ios:
        if gift_pids:
            raise CrossPlatformTradeError(
                "The iOS gift runner is active. Stop it cleanly before capturing."
            )
        output = args.capture_ios.expanduser().resolve()
        if output.exists():
            raise CrossPlatformTradeError(f"Refusing to overwrite existing file: {output}")
        if active_gift_runner_pids():
            raise CrossPlatformTradeError(
                "The iOS gift runner started during preflight. Stop it before capturing."
            )
        ios = await asyncio.to_thread(IOSController.connect, config)
        try:
            screenshot = await asyncio.to_thread(ios.driver.get_screenshot_as_png)
            rect = await asyncio.to_thread(ios.driver.get_window_rect)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(screenshot)
            print(
                f"Saved {output}; Appium viewport is "
                f"{rect['width']}x{rect['height']} logical points"
            )
        finally:
            await ios.quit()
        return 0

    android = await select_android_device(
        config.android_serial, config.android_serial_required
    )
    print(f"Android ready: {android.serial}")

    if args.check:
        return 2 if gift_pids else 0
    if gift_pids:
        raise CrossPlatformTradeError(
            "The iOS gift runner is active. Stop it cleanly before starting a trade."
        )
    # Close the small race between the initial preflight and session creation.
    if active_gift_runner_pids():
        raise CrossPlatformTradeError(
            "The iOS gift runner started during preflight. Stop it before trading."
        )
    ios = await asyncio.to_thread(IOSController.connect, config)
    try:
        rect = await asyncio.to_thread(ios.driver.get_window_rect)
        print(f"iOS Appium viewport: {rect['width']}x{rect['height']} logical points")
        await run_trades(android, ios, config, args.count, args.dry_run)
    finally:
        try:
            await ios.quit()
        except Exception as exc:
            print(f"Warning: could not close the iOS Appium session cleanly: {exc}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        with exclusive_trade_lock():
            return asyncio.run(async_main(args))
    except CrossPlatformTradeError as exc:
        print(f"Cross-platform trade error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nCancelled")
        return 130
    except Exception as exc:
        print(
            f"Unexpected cross-platform trade error: {exc.__class__.__name__}: {exc}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    from . import fleet_entrypoint
    public_result = fleet_entrypoint.direct_module_operation('trade', 'trade_pokemon.py')
    if public_result is not None:
        raise SystemExit(public_result)

if __name__ == "__main__":
    raise SystemExit(main())
