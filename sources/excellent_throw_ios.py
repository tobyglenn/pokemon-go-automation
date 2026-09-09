#!/usr/bin/env python3
"""Vision-guided Excellent throws for a manually opened Pokemon GO encounter.

The watcher is deliberately narrow: the player taps a wild Pokemon on the map,
then this module detects the full-size encounter ball, measures the shrinking
catch circle through WebDriverAgent's MJPEG stream, freezes it in the Excellent
band, and sends a continuous spin-and-release curve throw. It never taps the
map itself, and it keeps watching for later encounters until it is stopped.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextlib import contextmanager
import dataclasses
from dataclasses import dataclass
import fcntl
import io
import math
from pathlib import Path
import threading
import time
from typing import Any, Iterable, Iterator, Sequence
import urllib.request

import yaml
from PIL import Image, ImageChops, ImageFilter, ImageStat

from . import config_paths, ios_attached_devices, ios_wda_cleanup


CONFIG_GLOB = "excellent-throw*.yaml"
ARTIFACT_ROOT = config_paths.state_dir() / "excellent-throw"
LOCK_DIR = Path("/tmp/pokemon-go-fleet")
# Circle detection costs about a second per frame, so this bounds the wait
# between the calibration release and the first pulse. Measured on three live
# captures: the center and maximum radius are identical from 8 frames upward,
# and only the current radius drifts, which the pulses re-measure anyway.
# The circle is only drawn between holds, so give the release time to land and
# the UI to come back before reading it.
RING_SETTLE_SECONDS = 0.25

# Holds spent before a phone that has never once read the circle is written off
# as blind to it.  The SE spends its whole budget this way -- 30 holds, every
# one "no readable circle", then the flick it could have thrown at hold 1 --
# and each hold costs a nudge on a phone the user is waiting on.  A phone that
# has measured the ring even once is converging and keeps the full budget; this
# only cuts the case where there is nothing to converge towards.
BLIND_RING_ATTEMPTS = 4
# How much of the frozen circle to capture for the single post-release read.
RING_READ_SECONDS = 0.30
# Past this the circle has hit minimum and restarted, so a longer hold only
# walks into the next cycle.

# A ratio that jumps up by more than this is the circle restarting from full,
# not a bad measurement.
RING_RESET_JUMP = 0.08
# The smallest a real target circle can be, as a fraction of the view width.
# `target_candidates` will happily return 9-25px blobs off a Pokemon's own
# markings, and it returns them exactly when the real ring is not drawn: in the
# first frames of a hold, before it appears, and in the last, after it has
# reached minimum.  Seeding on one measures a ratio off scenery -- and because
# the next hold filters candidates against this maximum_radius, the junk lock
# then survives every remaining attempt.  A live SE run read 8-11 over 15-18
# for all thirty holds and never entered the band, while the real ring on the
# same frames sat at 45-54.  Radius is what separates them: over 168 captured
# frames the true ring never measured below 45 and the junk never above 25.
# Score does not -- the junk reached 1.06 and the true ring dropped to 0.83 --
# so it is deliberately not part of this gate.
MINIMUM_TARGET_RADIUS_WIDTH = 0.08
# Six seeds all landed on one Pokemon's head on the android-one, which lost a ring
# that scored higher than anything else on the frame.  Twelve spread-out seeds
# reach it and cost about a tenth of a second more per frame.
SEEDS_TO_REFINE = 12
MAXIMUM_HOLD_SECONDS = 0.60
ANGLE_COUNT = 72
ANGLES = tuple(2 * math.pi * index / ANGLE_COUNT for index in range(ANGLE_COUNT))
ANGLE_COS = tuple(math.cos(angle) for angle in ANGLES)
ANGLE_SIN = tuple(math.sin(angle) for angle in ANGLES)


class ExcellentThrowError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    appium_server_url: str
    ios_device: dict[str, Any]
    wait_seconds: float
    calibration_hold_seconds: float
    pulse_hold_seconds: float
    target_ratio_min: float
    target_ratio_max: float
    max_pulses: int
    max_ring_attempts: int
    throw_duration_ms: int
    end_offset_height: float
    curve_direction: str
    curve_offset_width: float
    spin_radius_width: float
    spin_turns: float
    spin_segment_ms: int
    pre_throw_pause_seconds: float
    initial_spin_pause_seconds: float
    attack_wait_seconds: float
    attack_quiet_threshold: float
    attack_motion_threshold: float
    attack_trigger_delay_seconds: float
    center_tolerance_width: float
    still_frames_required: int
    result_capture_seconds: float


@dataclass(frozen=True)
class StreamFrame:
    timestamp: float
    jpeg: bytes

    def image(self) -> Image.Image:
        return Image.open(io.BytesIO(self.jpeg)).convert("RGB")


@dataclass(frozen=True)
class RingCandidate:
    score: float
    center_x: int
    center_y: int
    radius: int


@dataclass(frozen=True)
class BallDetection:
    score: float
    center_x: int
    center_y: int
    radius: int


@dataclass(frozen=True)
class TargetCandidate:
    score: float
    center_x: int
    center_y: int
    radius: int


@dataclass(frozen=True)
class RingLock:
    center_x: int
    center_y: int
    maximum_radius: int
    current_radius: int
    confidence: float

    @property
    def ratio(self) -> float:
        return self.current_radius / self.maximum_radius


def _yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ExcellentThrowError(f"Config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ExcellentThrowError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExcellentThrowError(f"Config root must be a YAML object: {path}")
    return value


def _number(mapping: dict[str, Any], key: str, default: float) -> float:
    value = mapping.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ExcellentThrowError(f"{key} must be numeric")
    return float(value)


def load_runtime_config(path: Path) -> RuntimeConfig:
    root = _yaml(path)
    try:
        appium_path = config_paths.find_config(
            root.get("appium_config"), path.resolve().parent, "appium_config"
        )
    except config_paths.ConfigPathError as exc:
        raise ExcellentThrowError(str(exc)) from exc
    appium = _yaml(appium_path)
    device = appium.get("device")
    if not isinstance(device, dict):
        raise ExcellentThrowError(f"{appium_path} needs a device object")
    for key in ("udid", "team_id", "wda_bundle_id"):
        if not isinstance(device.get(key), str) or not device[key].strip():
            raise ExcellentThrowError(f"{appium_path}: device.{key} must be set")

    ring = root.get("ring", {})
    throw = root.get("throw", {})
    if not isinstance(ring, dict) or not isinstance(throw, dict):
        raise ExcellentThrowError("ring and throw must be YAML objects")
    wait_seconds = _number(root, "wait_seconds", 120)
    calibration = _number(ring, "calibration_hold_seconds", 2.2)
    pulse = _number(ring, "pulse_hold_seconds", 0.14)
    ratio_min = _number(ring, "target_ratio_min", 0.24)
    ratio_max = _number(ring, "target_ratio_max", 0.36)
    max_pulses = ring.get("max_pulses", 20)
    max_ring_attempts = ring.get("max_attempts", max_pulses * 4)
    duration_ms = throw.get("duration_ms", 140)
    offset = _number(throw, "end_offset_height", 0.045)
    curve_direction = throw.get("curve_direction", "clockwise")
    curve_offset = _number(throw, "curve_offset_width", 0.09)
    spin_radius = _number(throw, "spin_radius_width", 0.065)
    spin_turns = _number(throw, "spin_turns", 2.0)
    spin_segment_ms = throw.get("spin_segment_ms", 24)
    pre_throw_pause = _number(throw, "pre_throw_pause_seconds", 0.05)
    initial_spin_pause = _number(throw, "initial_spin_pause_seconds", 0.05)
    attack_wait = _number(throw, "attack_wait_seconds", 25)
    attack_quiet = _number(throw, "attack_quiet_threshold", 0.025)
    attack_motion = _number(throw, "attack_motion_threshold", 0.040)
    attack_delay = _number(throw, "attack_trigger_delay_seconds", 0.08)
    center_tolerance = _number(throw, "center_tolerance_width", 0.06)
    still_frames = throw.get("still_frames_required", 4)
    result_seconds = _number(throw, "result_capture_seconds", 2.8)
    if min(wait_seconds, calibration, pulse, result_seconds) <= 0:
        raise ExcellentThrowError("wait and gesture durations must be positive")
    if not 0.08 <= ratio_min < ratio_max <= 0.60:
        raise ExcellentThrowError("ring target ratios need 0.08 <= min < max <= 0.60")
    if type(max_pulses) is not int or not 1 <= max_pulses <= 100:
        raise ExcellentThrowError("ring.max_pulses must be an integer from 1 to 100")
    if type(max_ring_attempts) is not int or not 1 <= max_ring_attempts <= 100:
        raise ExcellentThrowError("ring.max_attempts must be an integer from 1 to 100")
    if type(duration_ms) is not int or not 60 <= duration_ms <= 500:
        raise ExcellentThrowError("throw.duration_ms must be an integer from 60 to 500")
    if not 0 <= offset <= 0.20:
        raise ExcellentThrowError("throw.end_offset_height must be between 0 and 0.20")
    if curve_direction not in {"clockwise", "counterclockwise"}:
        raise ExcellentThrowError(
            "throw.curve_direction must be clockwise or counterclockwise"
        )
    if not -0.40 <= curve_offset <= 0.40:
        raise ExcellentThrowError("throw.curve_offset_width must be between -0.40 and 0.40")
    if not 0.025 <= spin_radius <= 0.12:
        raise ExcellentThrowError("throw.spin_radius_width must be between 0.025 and 0.12")
    if not 1.0 <= spin_turns <= 4.0:
        raise ExcellentThrowError("throw.spin_turns must be between 1 and 4")
    if type(spin_segment_ms) is not int or not 10 <= spin_segment_ms <= 80:
        raise ExcellentThrowError("throw.spin_segment_ms must be an integer from 10 to 80")
    if not 0 <= pre_throw_pause <= 4.0:
        raise ExcellentThrowError("throw.pre_throw_pause_seconds must be between 0 and 4")
    if not 0 <= initial_spin_pause <= 2.0:
        raise ExcellentThrowError("throw.initial_spin_pause_seconds must be between 0 and 2")
    if not 5 <= attack_wait <= 120:
        raise ExcellentThrowError("throw.attack_wait_seconds must be between 5 and 120")
    if not 0.005 <= attack_quiet < attack_motion <= 0.20:
        raise ExcellentThrowError(
            "throw attack thresholds need 0.005 <= quiet < motion <= 0.20"
        )
    if not 0 <= attack_delay <= 0.5:
        raise ExcellentThrowError(
            "throw.attack_trigger_delay_seconds must be between 0 and 0.5"
        )
    if not 0.01 <= center_tolerance <= 0.30:
        raise ExcellentThrowError(
            "throw.center_tolerance_width must be between 0.01 and 0.30"
        )
    if type(still_frames) is not int or not 1 <= still_frames <= 60:
        raise ExcellentThrowError(
            "throw.still_frames_required must be an integer from 1 to 60"
        )
    server_url = appium.get("server_url", "http://127.0.0.1:4723")
    if not isinstance(server_url, str) or not server_url.strip():
        raise ExcellentThrowError("Appium server_url must be a string")
    return RuntimeConfig(
        appium_server_url=server_url.rstrip("/"),
        ios_device=device,
        wait_seconds=wait_seconds,
        calibration_hold_seconds=calibration,
        pulse_hold_seconds=pulse,
        target_ratio_min=ratio_min,
        target_ratio_max=ratio_max,
        max_pulses=max_pulses,
        max_ring_attempts=max_ring_attempts,
        throw_duration_ms=duration_ms,
        end_offset_height=offset,
        curve_direction=curve_direction,
        curve_offset_width=curve_offset,
        spin_radius_width=spin_radius,
        spin_turns=spin_turns,
        spin_segment_ms=spin_segment_ms,
        pre_throw_pause_seconds=pre_throw_pause,
        initial_spin_pause_seconds=initial_spin_pause,
        attack_wait_seconds=attack_wait,
        attack_quiet_threshold=attack_quiet,
        attack_motion_threshold=attack_motion,
        attack_trigger_delay_seconds=attack_delay,
        center_tolerance_width=center_tolerance,
        still_frames_required=still_frames,
        result_capture_seconds=result_seconds,
    )


def config_udid(path: Path) -> str | None:
    """The device UDID a throw config resolves to, or None when unreadable.

    Selection must not fail because some unrelated `excellent-throw*.yaml` in a
    search directory is half-written; such a file simply cannot match a phone.
    """
    try:
        appium_path = config_paths.find_config(
            _yaml(path).get("appium_config"), path.resolve().parent, "appium_config"
        )
        device = _yaml(appium_path).get("device")
    except (ExcellentThrowError, config_paths.ConfigPathError, OSError):
        return None
    if not isinstance(device, dict):
        return None
    udid = device.get("udid")
    return udid if isinstance(udid, str) and udid.strip() else None


def known_configs() -> list[Path]:
    """Every throw config on this machine, in config_paths preference order."""
    ordered: list[Path] = []
    for directory in config_paths.search_dirs():
        for path in sorted(directory.glob(CONFIG_GLOB)):
            resolved = path.resolve()
            if resolved not in ordered:
                ordered.append(resolved)
    return ordered



def select_configs() -> list[Path]:
    """Pick throw configs for each USB-attached iPhone.

    Returns one profile per connected phone, matching by udid.
    """
    try:
        attached = ios_attached_devices.attached_devices()
    except ios_attached_devices.AttachedDeviceError as exc:
        raise ExcellentThrowError(f"{exc}; pass --config choose profile") from exc

    usb = {udid for udid, connection in attached.items() if connection == "USB"}
    configs = known_configs()

    if not configs:
        raise ExcellentThrowError(
            f"No {CONFIG_GLOB} found in: "
            + ", ".join(str(item) for item in config_paths.search_dirs())
        )

    udid_to_configs: dict[str, list[Path]] = {}
    for path in sorted(configs):
        udid = config_udid(path)
        if udid is None or udid not in usb:
            continue
        udid_to_configs.setdefault(udid, []).append(path)

    if udid_to_configs:
        selected: list[Path] = []
        for _udid, matched in sorted(udid_to_configs.items()):
            if len(matched) == 1:
                selected.append(matched[0])
                continue

            specific = [
                p for p in sorted(matched)
                if "-main-" in p.name or "-secondary-" in p.name
            ]
            selected.append((specific or sorted(matched))[0])

        return selected

    wanted = {path.name: config_udid(path) for path in configs}
    detail = ", ".join(f"{name} wants {udid}" for name, udid in wanted.items())

    if not attached:
        raise ExcellentThrowError(
            f"No iPhone plugged into Mac ({detail}). "
            "Connect one with a data cable, unlock it, trust computer."
        )

    seen = ", ".join(f"{udid} over {connection}" for udid, connection in attached.items())
    raise ExcellentThrowError(
        f"No throw profile matches an attached phone. Attached: {seen}. Profiles: {detail}."
    )





def select_config() -> Path:
    """Pick throw config phone plugged into Mac (first matched device)."""
    return select_configs()[0]
def _ball_pixel(rgb: tuple[int, int, int]) -> bool:
    red, green, blue = rgb
    high = max(rgb)
    low = min(rgb)
    spread = high - low
    white = low > 170 and spread < 65
    dark = high < 70
    red_panel = red > 130 and red > green + 35 and red > blue + 15
    blue_panel = blue > 100 and blue > red + 25 and blue > green + 5
    yellow_panel = red > 140 and green > 120 and blue < 100 and min(red, green) - blue > 40
    return white or dark or red_panel or blue_panel or yellow_panel


def _strong_ball_pixel(rgb: tuple[int, int, int]) -> bool:
    """Match painted ball panels without treating a white transition as a ball."""
    red, green, blue = rgb
    high, low = max(rgb), min(rgb)
    spread = high - low
    white = low > 170 and spread < 65
    red_panel = red > 130 and red > green + 35 and red > blue + 15
    blue_panel = blue > 100 and blue > red + 25 and blue > green + 5
    yellow_panel = red > 140 and green > 120 and blue < 100 and min(red, green) - blue > 40
    return white or red_panel or blue_panel or yellow_panel


def _runs(values: Sequence[tuple[int, int]], *, minimum: int, gap: int = 2) -> list[list[tuple[int, int]]]:
    runs: list[list[tuple[int, int]]] = []
    for position, count in values:
        if count < minimum:
            continue
        if not runs or position > runs[-1][-1][0] + gap:
            runs.append([])
        runs[-1].append((position, count))
    return runs


def locate_throw_ball(
    image: Image.Image, viewport: tuple[int, int] = (375, 667)
) -> BallDetection | None:
    """Locate the large, centred encounter ball and reject fades/map controls."""
    width, height = viewport
    frame = image.convert("RGB").resize((width, height))
    pixels = frame.load()
    left, right = round(width * 0.28), round(width * 0.72)
    top, bottom = round(height * 0.70), height
    row_counts = [
        (y, sum(_strong_ball_pixel(pixels[x, y]) for x in range(left, right)))
        for y in range(top, bottom)
    ]
    row_runs = _runs(
        row_counts,
        minimum=max(8, round(width * 0.03)),
        gap=max(3, round(height * 0.015)),
    )
    candidates: list[BallDetection] = []
    for row_run in row_runs:
        y_min, y_max = row_run[0][0], row_run[-1][0]
        band_height = y_max - y_min + 1
        if band_height < height * 0.055:
            continue
        column_counts = [
            (
                x,
                sum(_strong_ball_pixel(pixels[x, y]) for y in range(y_min, y_max + 1)),
            )
            for x in range(left, right)
        ]
        column_runs = _runs(
            column_counts,
            minimum=max(4, round(band_height * 0.06)),
            gap=max(3, round(width * 0.025)),
        )
        for column_run in column_runs:
            x_min, x_max = column_run[0][0], column_run[-1][0]
            expected_x = width // 2
            if not x_min <= expected_x <= x_max:
                continue
            half_width = min(expected_x - x_min, x_max - expected_x)
            band_width = 2 * half_width + 1
            radius = round(max(band_width, band_height) / 2)
            aspect = band_width / band_height
            center_x = expected_x
            center_y = (
                y_min + radius
                if y_max >= height - 3
                else round((y_min + y_max) / 2)
            )
            # The upper cap is width-relative but the radius can come from the
            # vertical extent, so a tall phone's ball measures larger against
            # it: the android-three's is 0.225 of view width where the android-one's is 0.166
            # and the moto's 0.154.  At 0.20 the android-three's held ball was rejected
            # the moment it stopped being clipped by the bottom edge, and the
            # run read that as the encounter having ended.
            if not width * 0.075 <= radius <= width * 0.26:
                continue
            if not 0.55 <= aspect <= 1.55:
                continue
            mass = sum(count for _, count in row_run)
            density = mass / max(1, band_width * band_height)
            symmetry = 1.0
            size = min(1.0, radius / (width * 0.13))
            score = max(0.0, min(1.0, density * 0.9 + symmetry * 0.35 + size * 0.25))
            candidates.append(BallDetection(score, center_x, center_y, radius))
    return max(candidates, key=lambda candidate: candidate.score, default=None)


BUTTON_VIEW_WIDTH = 300


def _pale_button_pixel(rgb: tuple[int, int, int]) -> bool:
    """The translucent grey plate the corner buttons are drawn on."""
    return max(rgb) - min(rgb) < 42 and 120 < sum(rgb) / 3 < 240


def locate_encounter_buttons(
    image: Image.Image,
) -> tuple[list[int], list[int]] | None:
    """The berry and held-item buttons in the encounter screen's bottom corners.

    Their x is the same on every phone -- 0.123 and 0.872 of the width -- but
    their y is not: 0.836 on the android-one, 0.853 on the moto and 0.914 on the android-three,
    which is tall enough that the fixed 0.855 the picker used to tap landed on
    bare grass.  That is why the android-three could never open its berry picker.  So
    they are found, not assumed.

    Returns (berry, held-item) in full-image pixel coordinates, or None when
    this is not an encounter screen.
    """
    width, height = image.size
    view_width = BUTTON_VIEW_WIDTH
    view_height = max(1, round(view_width * height / width))
    frame = image.convert("RGB").resize((view_width, view_height))
    pixels = frame.load()
    found: list[list[int]] = []
    for x_from, x_to in ((0.01, 0.28), (0.72, 0.99)):
        left, right = int(view_width * x_from), int(view_width * x_to)
        top = int(view_height * 0.70)
        rows = [
            (y, sum(_pale_button_pixel(pixels[x, y]) for x in range(left, right)))
            for y in range(top, view_height)
        ]
        best: tuple[int, int, int] | None = None
        for row_run in _runs(rows, minimum=max(4, round(view_width * 0.02)), gap=2):
            y_min, y_max = row_run[0][0], row_run[-1][0]
            band_height = y_max - y_min + 1
            columns = [
                (
                    x,
                    sum(
                        _pale_button_pixel(pixels[x, y])
                        for y in range(y_min, y_max + 1)
                    ),
                )
                for x in range(left, right)
            ]
            for column_run in _runs(
                columns, minimum=max(3, round(band_height * 0.20)), gap=2
            ):
                x_min, x_max = column_run[0][0], column_run[-1][0]
                band_width = x_max - x_min + 1
                # A round plate, and neither the navigation bar below it nor a
                # line of white text beside it.
                if not 0.6 <= band_width / band_height <= 1.7:
                    continue
                if not view_width * 0.05 <= band_width <= view_width * 0.16:
                    continue
                area = band_width * band_height
                if best is None or area > best[2]:
                    best = (
                        round((x_min + x_max) / 2),
                        round((y_min + y_max) / 2),
                        area,
                    )
        if best is None:
            return None
        found.append(
            [
                round(best[0] * width / view_width),
                round(best[1] * height / view_height),
            ]
        )
    return found[0], found[1]


BERRY_PINK_FRACTION = 0.15
NANAB_FLICK_MS = 260
NANAB_FEED_SETTLE_SECONDS = 1.6
# Fallbacks only; the corner buttons are found per frame by
# `locate_encounter_buttons`, whose y differs from phone to phone.
BERRY_BUTTON_POINT = (0.123, 0.855)
BALL_SWITCH_POINT = (0.872, 0.839)
# The Great Ball inside the chooser sheet, second in its first row.  Measured
# against the sheet, not the screen: 0.498 of the width across, and 0.167 of
# the width below the sheet's top edge.
BALL_CHOICE_X = 0.498
BALL_CHOICE_BELOW_SHEET = 0.167
BALL_CHOICE_POINT = (0.498, 0.750)
BALL_SHEET_SECONDS = 1.2


def berry_in_hand(
    image: Image.Image,
    analysis_viewport: tuple[int, int],
    *,
    pink_fraction: float = BERRY_PINK_FRACTION,
) -> bool:
    """True while a Nanab is the item held over the throw spot.

    Feeding a berry hands the ball straight back, so this is what says whether
    the flick landed.  `locate_throw_ball` matches the berry as happily as it
    matches a ball -- it scored 0.99 on a held Nanab -- so the colour inside
    the disc is what separates them: the berry is pink and yellow, and no ball
    in the bag is.  Whiteness does not separate them; it is 0.02-0.10 for both.
    """
    detection = locate_throw_ball(image, analysis_viewport)
    if detection is None:
        return False
    scale_x = image.width / analysis_viewport[0]
    scale_y = image.height / analysis_viewport[1]
    center_x = round(detection.center_x * scale_x)
    center_y = round(detection.center_y * scale_y)
    radius = round(detection.radius * (scale_x + scale_y) / 2)
    if radius <= 0:
        return False
    pixels = image.convert("RGB").load()
    inside = pink = 0
    # Every second pixel: this is a fraction, and the phone's disc is 200k
    # pixels of pure-python sampling at full resolution.
    for y in range(max(0, center_y - radius), min(image.height, center_y + radius), 2):
        for x in range(max(0, center_x - radius), min(image.width, center_x + radius), 2):
            if (x - center_x) ** 2 + (y - center_y) ** 2 > radius * radius:
                continue
            red, green, blue = pixels[x, y]
            inside += 1
            if red > 200 and blue > 110 and green < 170 and red - green > 60:
                pink += 1
    if not inside:
        return False
    return pink / inside >= pink_fraction


# A ball's disc against every berry's, measured off the fleet's own captures:
# pale pixels are 0.36-0.39 of a Great Ball on both platforms and 0.00-0.10 of a
# held berry, and a ball carries its white below the middle while a berry is
# brightest on top.
BALL_PALE_FRACTION = 0.20
BALL_LOWER_LIFT = 15


def ball_in_hand(image: Image.Image, analysis_viewport: tuple[int, int]) -> bool:
    """True when what is held over the throw spot is a ball, not an item.

    `berry_in_hand` above knows one berry -- the pink Nanab -- and says no to
    every other item in the bag.  Once the fleet ran out of Nanabs the game
    handed each phone a Golden Razz instead, the pink test missed it, and the
    routine flicked the berry at the Pokemon throw after throw: four phones
    spent the morning of 6 Sep 2026 feeding rewards they never caught.

    Asked the other way round on purpose.  What a ball looks like is narrow and
    the same on every phone, and what an item looks like is every colour in the
    bag, so the ball is the thing worth naming: its lower half is white and its
    shell grey, which no berry is, and the white sits below the middle, which no
    berry does.
    """
    detection = locate_throw_ball(image, analysis_viewport)
    if detection is None:
        return False
    scale_x = image.width / analysis_viewport[0]
    scale_y = image.height / analysis_viewport[1]
    center_x = round(detection.center_x * scale_x)
    center_y = round(detection.center_y * scale_y)
    radius = round(detection.radius * (scale_x + scale_y) / 2)
    if radius <= 0:
        return False
    pixels = image.convert("RGB").load()
    inside = pale = 0
    upper = lower = 0.0
    upper_n = lower_n = 0
    for y in range(max(0, center_y - radius), min(image.height, center_y + radius), 2):
        for x in range(max(0, center_x - radius), min(image.width, center_x + radius), 2):
            if (x - center_x) ** 2 + (y - center_y) ** 2 > radius * radius:
                continue
            red, green, blue = pixels[x, y]
            inside += 1
            luminance = (red + green + blue) / 3
            if y < center_y:
                upper += luminance
                upper_n += 1
            else:
                lower += luminance
                lower_n += 1
            white = red > 200 and green > 200 and blue > 200
            grey = (
                abs(red - green) < 25
                and abs(green - blue) < 25
                and 120 < luminance < 225
            )
            if white or grey:
                pale += 1
    if not inside or not upper_n or not lower_n:
        return False
    lift = lower / lower_n - upper / upper_n
    return pale / inside >= BALL_PALE_FRACTION and lift >= BALL_LOWER_LIFT


def same_encounter_ball(
    previous: BallDetection,
    current: BallDetection,
    viewport: tuple[int, int],
) -> bool:
    """Keep a strong encounter ball lock through the tall-phone idle bounce."""
    width, height = viewport
    return (
        abs(current.center_x - previous.center_x) <= max(5, round(width * 0.02))
        and abs(current.center_y - previous.center_y) <= max(5, round(height * 0.14))
        and abs(current.radius - previous.radius) <= max(6, round(width * 0.03))
    )


def encounter_confidence(image: Image.Image, viewport: tuple[int, int] = (375, 667)) -> float:
    """Return a 0..1 guard score for a geometrically plausible encounter ball."""
    detection = locate_throw_ball(image, viewport)
    return detection.score if detection is not None else 0.0


def is_encounter(image: Image.Image) -> bool:
    return encounter_confidence(image) >= 0.55


def _ring_pixel(rgb: tuple[int, int, int]) -> float:
    """Mask the green/yellow/orange/red catch-ring palette, excluding cyan."""
    red, green, blue = rgb
    high, low = max(rgb), min(rgb)
    if high <= 135 or high - low <= 50:
        return 0.0
    green_to_yellow = green > 120 and blue < 135 and green - blue > 35
    orange_to_red = red > 140 and green < 200 and blue < 130
    return 1.0 if green_to_yellow or orange_to_red else 0.0


def _circle_score(
    pixels: Any, center_x: int, center_y: int, radius: int, width: int, height: int
) -> float:
    middle = inner = outer = 0.0
    inner_radius = max(1, radius - 4)
    outer_radius = radius + 4
    for cosine, sine in zip(ANGLE_COS, ANGLE_SIN):
        locations = (
            (round(center_x + radius * cosine), round(center_y + radius * sine)),
            (round(center_x + inner_radius * cosine), round(center_y + inner_radius * sine)),
            (round(center_x + outer_radius * cosine), round(center_y + outer_radius * sine)),
        )
        if any(not (0 <= x < width and 0 <= y < height) for x, y in locations):
            return 0.0
        middle += _ring_pixel(pixels[locations[0][0], locations[0][1]])
        inner += _ring_pixel(pixels[locations[1][0], locations[1][1]])
        outer += _ring_pixel(pixels[locations[2][0], locations[2][1]])
    coverage = middle / ANGLE_COUNT
    contrast = (middle - (inner + outer) / 2) / ANGLE_COUNT
    return coverage * 0.4 + max(0.0, contrast) * 0.8


def _target_pixel(rgb: tuple[int, int, int]) -> float:
    """Match the pale fixed target circle without accepting saturated scenery."""
    high, low = max(rgb), min(rgb)
    return 1.0 if low > 110 and high - low < 75 else 0.0


def _target_circle_score(
    pixels: Any,
    center_x: int,
    center_y: int,
    radius: int,
    width: int,
    height: int,
    *,
    angle_stride: int = 1,
) -> float:
    inner = middle = outer = 0.0
    sampled_cosines = ANGLE_COS[::angle_stride]
    sampled_sines = ANGLE_SIN[::angle_stride]
    sample_count = len(sampled_cosines)
    for cosine, sine in zip(sampled_cosines, sampled_sines):
        locations = (
            (round(center_x + (radius - 4) * cosine), round(center_y + (radius - 4) * sine)),
            (round(center_x + radius * cosine), round(center_y + radius * sine)),
            (round(center_x + (radius + 4) * cosine), round(center_y + (radius + 4) * sine)),
        )
        if any(not (0 <= x < width and 0 <= y < height) for x, y in locations):
            return 0.0
        inner += _target_pixel(pixels[locations[0][0], locations[0][1]])
        middle += _target_pixel(pixels[locations[1][0], locations[1][1]])
        outer += _target_pixel(pixels[locations[2][0], locations[2][1]])
    coverage = middle / sample_count
    contrast = (middle - (inner + outer) / 2) / sample_count
    return coverage * 0.7 + max(0.0, contrast) * 0.8


def _ring_component_seeds(
    frame: Image.Image, viewport: tuple[int, int]
) -> list[RingCandidate]:
    """Find saturated ring-shaped components before doing precise circle scoring."""
    width, height = viewport
    pixels = frame.load()
    mask = Image.new("L", (width, height))
    mask.putdata(
        [
            255 if _ring_pixel(pixels[x, y]) else 0
            for y in range(height)
            for x in range(width)
        ]
    )
    expanded = mask.filter(ImageFilter.MaxFilter(5))
    expanded_pixels = expanded.load()
    left, right = round(width * 0.16), round(width * 0.84)
    top, bottom = round(height * 0.10), round(height * 0.70)
    visited: set[tuple[int, int]] = set()
    seeds: list[RingCandidate] = []
    for start_y in range(top, bottom):
        for start_x in range(left, right):
            if not expanded_pixels[start_x, start_y] or (start_x, start_y) in visited:
                continue
            queue = deque([(start_x, start_y)])
            visited.add((start_x, start_y))
            area = 0
            x_min = x_max = start_x
            y_min = y_max = start_y
            while queue:
                x, y = queue.popleft()
                area += 1
                x_min, x_max = min(x_min, x), max(x_max, x)
                y_min, y_max = min(y_min, y), max(y_max, y)
                for next_x, next_y in (
                    (x - 1, y),
                    (x + 1, y),
                    (x, y - 1),
                    (x, y + 1),
                ):
                    if (
                        left <= next_x < right
                        and top <= next_y < bottom
                        and (next_x, next_y) not in visited
                        and expanded_pixels[next_x, next_y]
                    ):
                        visited.add((next_x, next_y))
                        queue.append((next_x, next_y))
            component_width = x_max - x_min + 1
            component_height = y_max - y_min + 1
            if area < 24 or min(component_width, component_height) < 10:
                continue
            if max(component_width, component_height) > width * 0.48:
                continue
            aspect = component_width / component_height
            if not 0.48 <= aspect <= 2.1:
                continue
            seed_x = round((x_min + x_max) / 2)
            seed_y = round((y_min + y_max) / 2)
            seed_radius = round((component_width + component_height) / 4)
            best: RingCandidate | None = None
            for center_x in range(max(1, seed_x - 4), min(width - 1, seed_x + 4) + 1):
                for center_y in range(max(1, seed_y - 4), min(height - 1, seed_y + 4) + 1):
                    for radius in range(max(7, seed_radius - 6), seed_radius + 5):
                        score = _circle_score(
                            pixels, center_x, center_y, radius, width, height
                        )
                        candidate = RingCandidate(score, center_x, center_y, radius)
                        if best is None or candidate.score > best.score:
                            best = candidate
            if best is not None and best.score >= 0.43:
                seeds.append(best)
    seeds.sort(key=lambda item: item.score, reverse=True)
    return seeds


def ring_candidates(
    image: Image.Image,
    *,
    viewport: tuple[int, int] = (375, 667),
    center_x_hint: int | None = None,
    center_y_hint: int | None = None,
    limit: int = 8,
) -> list[RingCandidate]:
    """Find coloured catch rings, including high/off-centre flying Pokemon."""
    width, height = viewport
    frame = image.convert("RGB").resize((width, height))
    pixels = frame.load()
    if center_x_hint is None or center_y_hint is None:
        best = _ring_component_seeds(frame, viewport)
    else:
        smallest = max(7, round(width * 0.022))
        largest = round(width * 0.21)
        coarse = [
            RingCandidate(
                _circle_score(
                    pixels, center_x_hint, center_y_hint, radius, width, height
                ),
                center_x_hint,
                center_y_hint,
                radius,
            )
            for radius in range(smallest, largest + 1)
        ]
        coarse.sort(key=lambda item: item.score, reverse=True)
        best = []
        for seed in coarse[:6]:
            for center_x in range(
                max(1, center_x_hint - 4), min(width - 1, center_x_hint + 4) + 1
            ):
                for center_y in range(
                    max(1, center_y_hint - 4), min(height - 1, center_y_hint + 4) + 1
                ):
                    for radius in range(
                        max(smallest, seed.radius - 3), min(largest, seed.radius + 3) + 1
                    ):
                        score = _circle_score(
                            pixels, center_x, center_y, radius, width, height
                        )
                        if score >= 0.43:
                            best.append(RingCandidate(score, center_x, center_y, radius))
        best.sort(key=lambda item: item.score, reverse=True)
    selected: list[RingCandidate] = []
    for candidate in best:
        if any(
            abs(candidate.center_x - prior.center_x) <= 2
            and abs(candidate.center_y - prior.center_y) <= 2
            and abs(candidate.radius - prior.radius) <= 2
            for prior in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def target_candidates(
    image: Image.Image,
    *,
    viewport: tuple[int, int] = (375, 667),
    center_x_hint: int | None = None,
    center_y_hint: int | None = None,
    limit: int = 6,
    minimum_radius: int | None = None,
) -> list[TargetCandidate]:
    """Find the fixed white target circle around a colour-ring seed.

    A caller that will discard everything under a radius should say so with
    ``minimum_radius`` rather than filtering the result.  Filtering afterwards
    lets the Pokemon's own markings -- which score well but are far too small
    to be a ring -- occupy every returned slot, so a real ring that is present
    on the frame comes back as nothing at all.
    """
    width, height = viewport
    frame = image.convert("RGB").resize((width, height))
    pixels = frame.load()
    if center_x_hint is None or center_y_hint is None:
        seeds = ring_candidates(frame, viewport=viewport, limit=10)
        center_hypotheses = [(seed.center_x, seed.center_y, seed.radius) for seed in seeds]
        # Dark or partly occluded coloured rings can fragment into arcs while
        # scenery produces a stronger false component elsewhere. Always add a
        # coarse center corridor for the brighter fixed target circle.
        center_hypotheses.extend(
            (center_x, center_y, 7)
            for center_x in range(round(width * 0.43), round(width * 0.57) + 1, 8)
            for center_y in range(round(height * 0.06), round(height * 0.78) + 1, 6)
        )
    else:
        center_hypotheses = [
            (center_x, center_y, 7)
            for center_x in range(
                max(1, center_x_hint - 12), min(width - 1, center_x_hint + 12) + 1, 4
            )
            for center_y in range(
                max(1, center_y_hint - 16), min(height - 1, center_y_hint + 16) + 1, 4
            )
        ]
    smallest = max(8, round(width * 0.025))
    if minimum_radius is not None:
        smallest = max(smallest, minimum_radius)
    # The target circle is drawn around the Pokemon, so big species carry big
    # rings while small or distant species carry compact rings.
    largest = round(width * 0.38)
    coarse: list[TargetCandidate] = []
    for center_x, center_y, inner_radius in center_hypotheses:
        for radius in range(max(smallest, inner_radius + 4), largest + 1, 2):
            score = _target_circle_score(
                pixels,
                center_x,
                center_y,
                radius,
                width,
                height,
                angle_stride=6,
            )
            coarse.append(TargetCandidate(score, center_x, center_y, radius))
    coarse.sort(key=lambda item: item.score, reverse=True)
    # Only the best radius at each centre is worth refining, and the top scores
    # cluster: a Pokemon's head answers at a dozen neighbouring centres before
    # the true ring is reached.  Spread the seeds out so the refinement passes
    # look at that many distinct places instead of one blob over and over.
    strongest_at_centre: dict[tuple[int, int], TargetCandidate] = {}
    for candidate in coarse:
        strongest_at_centre.setdefault((candidate.center_x, candidate.center_y), candidate)
    seeds_to_refine: list[TargetCandidate] = []
    for candidate in sorted(
        strongest_at_centre.values(), key=lambda item: item.score, reverse=True
    ):
        if any(
            abs(candidate.center_x - prior.center_x) <= 4
            and abs(candidate.center_y - prior.center_y) <= 3
            for prior in seeds_to_refine
        ):
            continue
        seeds_to_refine.append(candidate)
        if len(seeds_to_refine) >= SEEDS_TO_REFINE:
            break
    best: list[TargetCandidate] = []
    for seed in seeds_to_refine:
        for center_x in range(max(1, seed.center_x - 3), min(width - 1, seed.center_x + 3) + 1):
            for center_y in range(max(1, seed.center_y - 3), min(height - 1, seed.center_y + 3) + 1):
                for radius in range(max(smallest, seed.radius - 3), min(largest, seed.radius + 3) + 1):
                    score = _target_circle_score(
                        pixels, center_x, center_y, radius, width, height
                    )
                    if score >= 0.45:
                        best.append(TargetCandidate(score, center_x, center_y, radius))
    best.sort(key=lambda item: item.score, reverse=True)
    selected: list[TargetCandidate] = []
    for candidate in best:
        if any(
            abs(candidate.center_x - prior.center_x) <= 2
            and abs(candidate.center_y - prior.center_y) <= 2
            and abs(candidate.radius - prior.radius) <= 2
            for prior in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def analyze_ring_sequence(
    images: Sequence[Image.Image], viewport: tuple[int, int] = (375, 667)
) -> RingLock:
    """Measure a coloured ring only after proving its fixed white target circle."""
    if len(images) < 3:
        raise ExcellentThrowError("Too few MJPEG frames to measure the catch circle")
    width, _ = viewport
    target_frames = [
        target_candidates(image, viewport=viewport, limit=6) for image in images
    ]
    target_clusters: list[tuple[float, list[tuple[int, TargetCandidate]]]] = []
    hypotheses = [candidate for candidates in target_frames for candidate in candidates]
    for hypothesis in hypotheses:
        cluster: list[tuple[int, TargetCandidate]] = []
        for frame_index, candidates in enumerate(target_frames):
            nearby = [
                candidate
                for candidate in candidates
                if abs(candidate.center_x - hypothesis.center_x) <= 7
                and abs(candidate.center_y - hypothesis.center_y) <= 7
                and abs(candidate.radius - hypothesis.radius) <= 7
            ]
            if nearby:
                cluster.append((frame_index, max(nearby, key=lambda item: item.score)))
        if len(cluster) < 3:
            continue
        x_span = max(item.center_x for _, item in cluster) - min(
            item.center_x for _, item in cluster
        )
        y_span = max(item.center_y for _, item in cluster) - min(
            item.center_y for _, item in cluster
        )
        if max(x_span, y_span) > 14:
            continue
        confidence = len(cluster) * 10 + 50 * sum(
            item.score for _, item in cluster
        ) / len(cluster)
        target_clusters.append((confidence, cluster))
    if not target_clusters:
        raise ExcellentThrowError(
            "No stable white target circle appeared; the ball was not held or the Pokemon moved"
        )
    _, target_cluster = max(target_clusters, key=lambda item: item[0])
    center_x = round(sum(item.center_x for _, item in target_cluster) / len(target_cluster))
    center_y = round(sum(item.center_y for _, item in target_cluster) / len(target_cluster))
    maximum = round(sum(item.radius for _, item in target_cluster) / len(target_cluster))

    coloured: list[tuple[int, RingCandidate]] = []
    for frame_index, image in enumerate(images):
        candidates = ring_candidates(
            image,
            viewport=viewport,
            center_x_hint=center_x,
            center_y_hint=center_y,
            limit=8,
        )
        plausible = [candidate for candidate in candidates if candidate.radius <= maximum + 4]
        if plausible:
            coloured.append((frame_index, max(plausible, key=lambda item: item.score)))
    radii = [candidate.radius for _, candidate in coloured]
    if len(coloured) < 2 or not radii:
        raise ExcellentThrowError(
            "The coloured catch ring was not confirmed inside the white target; no throw was sent"
        )
    latest_frame = max(frame_index for frame_index, _ in coloured)
    latest = [candidate for frame_index, candidate in coloured if frame_index == latest_frame]
    current = max(latest, key=lambda candidate: candidate.score)
    return RingLock(
        center_x=center_x,
        center_y=center_y,
        maximum_radius=maximum,
        current_radius=current.radius,
        confidence=current.score,
    )


def current_ring(
    images: Sequence[Image.Image], prior: RingLock, viewport: tuple[int, int]
) -> RingLock | None:
    """Read the last ring in a pulse and prove the target stayed in place."""
    paired: list[tuple[TargetCandidate, RingCandidate]] = []
    for image in images:
        targets = target_candidates(
            image,
            viewport=viewport,
            center_x_hint=prior.center_x,
            center_y_hint=prior.center_y,
            limit=3,
        )
        valid_targets = [
            candidate
            for candidate in targets
            if abs(candidate.radius - prior.maximum_radius) <= 8
        ]
        if not valid_targets:
            continue
        target = max(valid_targets, key=lambda candidate: candidate.score)
        candidates = ring_candidates(
            image,
            viewport=viewport,
            center_x_hint=target.center_x,
            center_y_hint=target.center_y,
            limit=4,
        )
        plausible = [
            candidate for candidate in candidates if candidate.radius <= prior.maximum_radius + 4
        ]
        if plausible:
            paired.append((target, max(plausible, key=lambda candidate: candidate.score)))
    if len(paired) < 2:
        return None
    recent = paired[-2:]
    if (
        abs(recent[-1][0].center_x - recent[-2][0].center_x) > 6
        or abs(recent[-1][0].center_y - recent[-2][0].center_y) > 6
    ):
        return None
    target, latest = recent[-1]
    return RingLock(
        center_x=target.center_x,
        center_y=target.center_y,
        maximum_radius=prior.maximum_radius,
        current_radius=latest.radius,
        confidence=latest.score,
    )


def fast_current_ring(
    image: Image.Image, prior: RingLock, viewport: tuple[int, int]
) -> RingLock | None:
    """Track a known target cheaply enough to release a live pointer in-band."""
    width, height = viewport
    frame = image.convert("RGB").resize((width, height))
    pixels = frame.load()
    target_best: TargetCandidate | None = None
    for center_x in range(max(1, prior.center_x - 3), min(width - 1, prior.center_x + 3) + 1):
        for center_y in range(max(1, prior.center_y - 3), min(height - 1, prior.center_y + 3) + 1):
            for radius in range(
                max(8, prior.maximum_radius - 3), prior.maximum_radius + 4
            ):
                score = _target_circle_score(
                    pixels, center_x, center_y, radius, width, height
                )
                candidate = TargetCandidate(score, center_x, center_y, radius)
                if target_best is None or candidate.score > target_best.score:
                    target_best = candidate
    if target_best is None or target_best.score < 0.42:
        return None

    smallest = max(7, round(width * 0.022))
    coarse = [
        RingCandidate(
            _circle_score(
                pixels,
                target_best.center_x,
                target_best.center_y,
                radius,
                width,
                height,
            ),
            target_best.center_x,
            target_best.center_y,
            radius,
        )
        for radius in range(smallest, target_best.radius + 4)
    ]
    coarse.sort(key=lambda candidate: candidate.score, reverse=True)
    ring_best: RingCandidate | None = None
    for seed in coarse[:4]:
        for center_x in range(target_best.center_x - 2, target_best.center_x + 3):
            for center_y in range(target_best.center_y - 2, target_best.center_y + 3):
                for radius in range(max(smallest, seed.radius - 2), seed.radius + 3):
                    score = _circle_score(
                        pixels, center_x, center_y, radius, width, height
                    )
                    candidate = RingCandidate(score, center_x, center_y, radius)
                    if ring_best is None or candidate.score > ring_best.score:
                        ring_best = candidate
    if ring_best is None or ring_best.score < 0.42:
        return None
    return RingLock(
        center_x=target_best.center_x,
        center_y=target_best.center_y,
        maximum_radius=target_best.radius,
        current_radius=ring_best.radius,
        confidence=min(target_best.score, ring_best.score),
    )


def curve_throw_path(
    ball: BallDetection,
    lock: RingLock,
    viewport: tuple[int, int],
    config: RuntimeConfig,
) -> tuple[list[int], list[tuple[int, int, int]]]:
    """Build a continuous clockwise/counterclockwise spin and compensated release."""
    width, height = viewport
    start = [ball.center_x, ball.center_y]
    spin_radius = max(10, min(round(width * config.spin_radius_width), ball.radius // 2))
    # WDA serializes every pointer move as an XCTest event. A four-segment
    # circle is the shortest complete revolution and releases early enough
    # for the locked ring to remain frozen throughout a typical attack.
    segments_per_turn = 4
    segment_count = max(4, round(config.spin_turns * segments_per_turn))
    direction = 1 if config.curve_direction == "clockwise" else -1
    path: list[tuple[int, int, int]] = []
    for index in range(segment_count + 1):
        angle = direction * 2 * math.pi * index / segments_per_turn
        path.append(
            (
                round(ball.center_x + spin_radius * math.cos(angle)),
                round(ball.center_y + spin_radius * math.sin(angle)),
                config.spin_segment_ms,
            )
        )

    # Clockwise spin (direction > 0) curves LEFT in Pokemon GO physics, so flick RIGHT (+ horizontal).
    # Counterclockwise spin (direction < 0) curves RIGHT in Pokemon GO physics, so flick LEFT (- horizontal).
    corrected_end_y = round(lock.center_y - height * config.end_offset_height)
    horizontal = round(width * config.curve_offset_width)
    end_x = lock.center_x + horizontal if direction > 0 else lock.center_x - horizontal
    end = (
        max(2, min(width - 2, end_x)),
        max(round(height * 0.10), min(ball.center_y - 60, corrected_end_y)),
        config.throw_duration_ms,
    )
    path.append(end)
    return start, path


def straight_throw_points(
    ball: BallDetection,
    lock: RingLock,
    viewport: tuple[int, int],
    config: RuntimeConfig,
) -> tuple[list[int], list[int]]:
    """Aim a straight throw at the measured catch-circle center."""
    width, height = viewport
    corrected_end_y = round(lock.center_y - height * (config.end_offset_height * 0.5))
    return [ball.center_x, ball.center_y], [
        max(2, min(width - 2, lock.center_x)),
        max(round(height * 0.10), min(ball.center_y - 60, corrected_end_y)),
    ]


class MJPEGStream:
    def __init__(self, port: int, *, maximum_frames: int = 300) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self.frames: deque[StreamFrame] = deque(maxlen=maximum_frames)
        self.ready = threading.Event()
        self.stop_requested = threading.Event()
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="pokemon-mjpeg", daemon=True)
        self.thread.start()
        if not self.ready.wait(6):
            self.stop()
            detail = f": {self.error}" if self.error else ""
            raise ExcellentThrowError(f"WebDriverAgent MJPEG stream did not start{detail}")

    def _run(self) -> None:
        data = bytearray()
        try:
            with urllib.request.urlopen(self.url, timeout=8) as response:
                while not self.stop_requested.is_set():
                    data.extend(response.read(65536))
                    while True:
                        start = data.find(b"\xff\xd8")
                        end = data.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                        if start < 0 or end < 0:
                            if start > 0:
                                del data[:start]
                            break
                        self.frames.append(StreamFrame(time.monotonic(), bytes(data[start : end + 2])))
                        del data[: end + 2]
                        self.ready.set()
        except BaseException as exc:  # surfaced on the main thread
            if not self.stop_requested.is_set():
                self.error = exc
            self.ready.set()

    def since(self, timestamp: float, *, until: float | None = None) -> list[StreamFrame]:
        return [
            frame
            for frame in list(self.frames)
            if frame.timestamp >= timestamp and (until is None or frame.timestamp <= until)
        ]

    def latest(self) -> StreamFrame | None:
        return self.frames[-1] if self.frames else None

    def stop(self) -> None:
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


class IOSExcellentThrowDevice:
    def __init__(self, driver: Any, config: RuntimeConfig) -> None:
        self.driver = driver
        self.config = config
        rect = driver.get_window_rect()
        self.viewport = (int(rect["width"]), int(rect["height"]))
        self.label = str(config.ios_device.get("name", "iPhone"))

    @classmethod
    def connect(cls, config: RuntimeConfig) -> "IOSExcellentThrowDevice":
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ModuleNotFoundError as exc:
            raise ExcellentThrowError("The Appium Python client is missing") from exc
        device = config.ios_device
        port = int(device.get("wda_local_port", ios_wda_cleanup.WDA_DEFAULT_PORT))
        ios_wda_cleanup.clear_detached_wda(device["udid"], port=port)
        capabilities: dict[str, Any] = {
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
            "appium:showXcodeLog": bool(device.get("show_xcode_log", False)),
            "appium:useNewWDA": False,
            "appium:newCommandTimeout": 3600,
            "appium:waitForIdleTimeout": 0,
            "appium:waitForQuiescence": False,
            "appium:animationCoolOffTimeout": 0,
        }
        optional = (
            ("platform_version", "platformVersion"),
            ("wda_local_port", "appium:wdaLocalPort"),
            ("mjpeg_server_port", "appium:mjpegServerPort"),
        )
        for source, destination in optional:
            if device.get(source) is not None:
                capabilities[destination] = device[source]
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
            try:
                driver.update_settings({"mjpegServerFramerate": 30})
            except Exception:
                pass
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
                    try:
                        driver.update_settings({"mjpegServerFramerate": 30})
                    except Exception:
                        pass
                except Exception as retry_exc:
                    raise ExcellentThrowError(f"Could not start Appium session for {name}: {retry_exc}") from retry_exc
            else:
                raise ExcellentThrowError(f"Could not start Appium session for {name}: {exc}") from exc
        return cls(driver, config)

    def screenshot(self) -> Image.Image:
        return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")

    def _touch_action(
        self,
        start: Sequence[int],
        *,
        end: Sequence[int] | None = None,
        duration_ms: int = 80,
        hold_seconds: float = 0,
    ) -> None:
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
        from selenium.webdriver.common.actions.pointer_input import PointerInput

        finger = PointerInput(POINTER_TOUCH, "straight-finger")
        actions = ActionBuilder(self.driver, mouse=finger)
        source = actions.pointer_action.source
        source.create_pointer_move(
            duration=0, x=int(start[0]), y=int(start[1]), origin="viewport"
        )
        source.create_pointer_down(button=0)
        if hold_seconds:
            source.create_pause(hold_seconds)
        if end is not None:
            source.create_pointer_move(
                duration=int(duration_ms),
                x=int(end[0]),
                y=int(end[1]),
                origin="viewport",
            )
        source.create_pointer_up(button=0)
        actions.perform()

    def _curve_action(
        self,
        start: Sequence[int],
        path: Sequence[tuple[int, int, int]],
        *,
        initial_pause: float | None = None,
    ) -> None:
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
        from selenium.webdriver.common.actions.pointer_input import PointerInput

        finger = PointerInput(POINTER_TOUCH, "curve-finger")
        actions = ActionBuilder(self.driver, mouse=finger)
        source = actions.pointer_action.source
        source.create_pointer_move(
            duration=0, x=int(start[0]), y=int(start[1]), origin="viewport"
        )
        source.create_pointer_down(button=0)
        pause_seconds = (
            self.config.initial_spin_pause_seconds
            if initial_pause is None
            else initial_pause
        )
        if pause_seconds > 0:
            source.create_pause(float(pause_seconds))
        for x, y, duration_ms in path:
            source.create_pointer_move(
                duration=int(duration_ms), x=int(x), y=int(y), origin="viewport"
            )
        source.create_pointer_up(button=0)
        actions.perform()

    def curve_throw(
        self,
        start: Sequence[int],
        path: Sequence[tuple[int, int, int]],
        *,
        initial_pause: float | None = None,
    ) -> None:
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
        from selenium.webdriver.common.actions.pointer_input import PointerInput

        finger = PointerInput(POINTER_TOUCH, "curve-finger")
        actions = ActionBuilder(self.driver, mouse=finger)
        source = actions.pointer_action.source
        source.create_pointer_move(
            duration=0, x=int(start[0]), y=int(start[1]), origin="viewport"
        )
        source.create_pointer_down(button=0)
        pause_seconds = (
            self.config.initial_spin_pause_seconds
            if initial_pause is None
            else initial_pause
        )
        if pause_seconds > 0:
            source.create_pause(float(pause_seconds))
        for x, y, duration_ms in path:
            source.create_pointer_move(
                duration=int(duration_ms), x=int(x), y=int(y), origin="viewport"
            )
        source.create_pointer_up(button=0)
        actions.perform()

    def _pointer_down_action(self, point: Sequence[int]) -> None:
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
        from selenium.webdriver.common.actions.pointer_input import PointerInput

        finger = PointerInput(POINTER_TOUCH, "ring-lock-finger")
        actions = ActionBuilder(self.driver, mouse=finger)
        source = actions.pointer_action.source
        source.create_pointer_move(
            duration=0, x=int(point[0]), y=int(point[1]), origin="viewport"
        )
        source.create_pointer_down(button=0)
        actions.perform()

    def _hold_action(self, point: Sequence[int], seconds: float) -> None:
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
        from selenium.webdriver.common.actions.pointer_input import PointerInput

        finger = PointerInput(POINTER_TOUCH, "ring-lock-finger")
        actions = ActionBuilder(self.driver, mouse=finger)
        source = actions.pointer_action.source
        source.create_pointer_move(
            duration=0, x=int(point[0]), y=int(point[1]), origin="viewport"
        )
        source.create_pointer_down(button=0)
        source.create_pause(float(seconds))
        source.create_pointer_up(button=0)
        actions.perform()

    def _release_actions(self) -> None:
        from selenium.webdriver.remote.command import Command

        self.driver.execute(Command.W3C_CLEAR_ACTIONS)

    async def stationary_hold(self, point: Sequence[int], seconds: float) -> tuple[float, float]:
        # ActionBuilder sends the down/pause/up sequence in one W3C actions call.
        # WDA queues the sequence and returns immediately, so the MJPEG stream
        # keeps capturing while the catch ring is drawn. The previous
        # `mobile: touchAndHold` script blocked WDA, which paused the stream
        # and made the multi-pulse lock read 0 frames per hold.
        started = time.monotonic()
        await asyncio.to_thread(self._hold_action, point, seconds)
        return started, time.monotonic()

    async def curve_throw(
        self,
        start: Sequence[int],
        path: Sequence[tuple[int, int, int]],
        *,
        initial_pause: float | None = None,
    ) -> None:
        await asyncio.to_thread(self._curve_action, start, path, initial_pause=initial_pause)

    async def straight_throw(
        self, start: Sequence[int], end: Sequence[int], duration_ms: int
    ) -> None:
        await asyncio.to_thread(
            self.driver.execute_script,
            "mobile: dragFromToForDuration",
            {
                "duration": float(duration_ms) / 1000.0,
                "fromX": int(start[0]),
                "fromY": int(start[1]),
                "toX": int(end[0]),
                "toY": int(end[1]),
            },
        )

    async def flick(
        self, start: Sequence[int], end: Sequence[int], duration_ms: int
    ) -> None:
        """A short stepped drag from the held item to a point up the screen.

        Not `straight_throw`: that goes through `mobile: dragFromToForDuration`,
        which sends a single move event that Pokemon GO reads as a click on
        whatever sits under the finger -- so a berry 'thrown' that way is really
        just tapped, and a tap feeds nothing.
        """
        steps = 8
        x0, y0 = int(start[0]), int(start[1])
        x1, y1 = int(end[0]), int(end[1])
        per_step = max(8, round(duration_ms / steps))
        path = [
            (
                round(x0 + (x1 - x0) * step / steps),
                round(y0 + (y1 - y0) * step / steps),
                per_step,
            )
            for step in range(1, steps + 1)
        ]
        await asyncio.to_thread(self._curve_action, start, path, initial_pause=0.0)

    async def tap(self, point: Sequence[int]) -> None:
        await asyncio.to_thread(
            self._touch_action,
            [int(point[0]), int(point[1])],
            duration_ms=80,
        )

    async def quit(self) -> None:
        try:
            await asyncio.to_thread(self.driver.quit)
        except Exception:
            pass


def _images(frames: Iterable[StreamFrame]) -> list[Image.Image]:
    return [frame.image() for frame in frames]


def _save_frames(frames: Sequence[StreamFrame], directory: Path, prefix: str, zero: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index, frame in enumerate(frames):
        milliseconds = round((frame.timestamp - zero) * 1000)
        (directory / f"{prefix}-{index:03d}-{milliseconds:+05d}ms.jpg").write_bytes(frame.jpeg)


def pokemon_motion_score(
    prior: Image.Image,
    current: Image.Image,
    lock: RingLock,
    viewport: tuple[int, int],
) -> float:
    """Measure consecutive-frame movement around the Pokemon, excluding the ball."""
    width, height = viewport
    half_width = max(round(width * 0.20), lock.maximum_radius * 3)
    half_height = max(round(height * 0.10), lock.maximum_radius * 2)
    box = (
        max(0, lock.center_x - half_width),
        max(0, lock.center_y - half_height),
        min(width, lock.center_x + half_width + 1),
        min(height, lock.center_y + half_height + 1),
    )
    first = prior.convert("RGB").resize((width, height)).crop(box)
    second = current.convert("RGB").resize((width, height)).crop(box)
    difference = ImageChops.difference(first, second)
    return sum(ImageStat.Stat(difference).mean) / (3 * 255)


def track_target_center(
    prior: Image.Image,
    current: Image.Image,
    center_x: int,
    center_y: int,
    maximum_radius: int,
    viewport: tuple[int, int],
) -> tuple[int, int]:
    """Follow the Pokemon between ring lock and attack with block matching."""
    width, height = viewport
    first = prior.convert("L").resize((width, height))
    second = current.convert("L").resize((width, height))
    patch_radius = max(8, min(18, maximum_radius // 3))
    center_x = min(width - patch_radius - 1, max(patch_radius, center_x))
    center_y = min(height - patch_radius - 1, max(patch_radius, center_y))
    reference = first.crop(
        (
            center_x - patch_radius,
            center_y - patch_radius,
            center_x + patch_radius + 1,
            center_y + patch_radius + 1,
        )
    )
    search_radius = max(12, min(24, round(maximum_radius * 0.40)))
    best_score = float("inf")
    best = (center_x, center_y)
    for candidate_y in range(
        max(patch_radius, center_y - search_radius),
        min(height - patch_radius - 1, center_y + search_radius) + 1,
        2,
    ):
        for candidate_x in range(
            max(patch_radius, center_x - search_radius),
            min(width - patch_radius - 1, center_x + search_radius) + 1,
            2,
        ):
            candidate = second.crop(
                (
                    candidate_x - patch_radius,
                    candidate_y - patch_radius,
                    candidate_x + patch_radius + 1,
                    candidate_y + patch_radius + 1,
                )
            )
            difference = ImageChops.difference(reference, candidate)
            score = ImageStat.Stat(difference).mean[0]
            score += 0.08 * (
                abs(candidate_x - center_x) + abs(candidate_y - center_y)
            )
            if score < best_score:
                best_score = score
                best = (candidate_x, candidate_y)
    return best


def throw_window_state(
    score: float, offset: int, tolerance: int, config: RuntimeConfig
) -> str:
    """Classify one frame of the post-lock wait.

    `attacking` covers the lunge the ball must not be thrown into, `restless`
    the smaller idle sway that is not yet worth a ball, and `still` a target
    holding position. Only a `still` target sitting within `tolerance` of the
    screen center is `ready`.
    """
    if score >= config.attack_motion_threshold:
        return "attacking"
    if score > config.attack_quiet_threshold:
        return "restless"
    return "ready" if offset <= tolerance else "still"


async def wait_for_still_center(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    lock: RingLock,
    artifact_dir: Path,
) -> RingLock:
    """Hold the locked circle until the target is centered and not attacking.

    The Excellent-sized circle is already released and locked by this point, so
    waiting costs nothing but time: the ball is only thrown once the Pokemon has
    stopped moving and is standing near the middle of the screen, which is where
    the locked circle sits. A lunging Pokemon resets the count rather than
    triggering the throw.
    """
    config = device.config
    segment_started = time.monotonic()
    deadline = segment_started + 2.5
    cursor = segment_started
    previous: Image.Image | None = None
    tracked_x, tracked_y = lock.center_x, lock.center_y
    center_x = device.viewport[0] // 2
    tolerance = max(4, round(device.viewport[0] * config.center_tolerance_width))
    still_frames = 0
    captured: list[StreamFrame] = []
    while time.monotonic() < deadline:
        if stream.error is not None:
            print(
                f"[{device.label}] MJPEG stream idle ({stream.error}); "
                f"throwing at locked target ({tracked_x},{tracked_y})",
                flush=True,
            )
            return dataclasses.replace(lock, center_x=tracked_x, center_y=tracked_y)
        fresh = stream.since(cursor)
        if not fresh:
            await asyncio.sleep(0.04)
            continue
        for frame in fresh:
            cursor = frame.timestamp + 0.000001
            captured.append(frame)
            current = frame.image()
            if previous is None:
                previous = current
                continue
            tracked_lock = dataclasses.replace(
                lock, center_x=tracked_x, center_y=tracked_y
            )
            next_x, next_y = track_target_center(
                previous,
                current,
                tracked_x,
                tracked_y,
                lock.maximum_radius,
                device.viewport,
            )
            score = pokemon_motion_score(
                previous, current, tracked_lock, device.viewport
            )
            previous = current
            tracked_x, tracked_y = next_x, next_y
            offset = abs(tracked_x - center_x)
            state = throw_window_state(score, offset, tolerance, config)
            if state in ("attacking", "restless"):
                still_frames = 0
                continue
            still_frames += 1
            if state == "ready" and still_frames >= config.still_frames_required:
                settled = dataclasses.replace(
                    lock, center_x=tracked_x, center_y=tracked_y
                )
                _save_frames(captured, artifact_dir, "center-wait", segment_started)
                print(
                    f"[{device.label}] Target centered and still "
                    f"(motion {score:.3f}, {offset}px off center); "
                    f"live target=({settled.center_x},{settled.center_y}); "
                    "throwing through the locked circle",
                    flush=True,
                )
                if config.attack_trigger_delay_seconds:
                    await asyncio.sleep(config.attack_trigger_delay_seconds)
                return settled
        await asyncio.sleep(0.04)

    print(
        f"[{device.label}] Wait deadline reached; "
        f"throwing at locked target ({tracked_x},{tracked_y})",
        flush=True,
    )
    return dataclasses.replace(lock, center_x=tracked_x, center_y=tracked_y)


async def wait_for_encounter(
    device: IOSExcellentThrowDevice,
    timeout: float | None,
    artifact_dir: Path,
    stream: MJPEGStream | None = None,
) -> BallDetection:
    deadline = time.monotonic() + timeout if timeout is not None else None
    last_score = -1.0
    stable: list[BallDetection] = []
    required_stable = 2 if stream is not None else 3
    while deadline is None or time.monotonic() < deadline:
        if stream is not None and stream.latest() is not None:
            image = stream.latest().image()
        else:
            image = await asyncio.to_thread(device.screenshot)
        detection = locate_throw_ball(image, device.viewport)
        score = detection.score if detection is not None else 0.0
        if detection is not None and detection.score >= 0.55:
            if stable and not same_encounter_ball(
                stable[-1], detection, device.viewport
            ):
                stable.clear()
            stable.append(detection)
        else:
            stable.clear()
        if len(stable) >= required_stable:
            image.save(artifact_dir / "encounter.png")
            ready = stable[-1]
            print(
                f"[{device.label}] Encounter ready; ball=({ready.center_x},{ready.center_y}) "
                f"r={ready.radius} confidence={ready.score:.2f}",
                flush=True,
            )
            return ready
        if last_score < 0 or abs(score - last_score) >= 0.15:
            print(f"[{device.label}] Waiting for you to tap a wild Pokemon ({score:.2f})", flush=True)
            last_score = score
        await asyncio.sleep(0.03 if stream is not None else 0.45)
    raise ExcellentThrowError(f"No ready encounter appeared within {timeout:g} seconds")


def measure_ring(
    frames: Sequence[StreamFrame],
    prior: RingLock | None,
    viewport: tuple[int, int],
) -> RingLock | None:
    """Read the live ring from the newest frame that yields one.

    Newest first, because the ring is still shrinking while the frames are
    captured and the last reading is the one the next hold acts on.
    """
    width, height = viewport
    smallest = round(width * MINIMUM_TARGET_RADIUS_WIDTH)
    for frame in reversed(frames):
        image = frame.image()
        measured: RingLock | None = None
        if prior is not None:
            measured = fast_current_ring(image, prior, viewport)
        if measured is None:
            targets = target_candidates(
                image,
                viewport=viewport,
                limit=4 if prior is not None else 8,
                # Anything smaller than a real ring is the Pokemon's own
                # markings, and a frame with no ring drawn on it offers nothing
                # else.  Such a frame has to come back empty so the older ones
                # get their turn.
                minimum_radius=smallest,
            )
            if prior is not None:
                targets = [
                    target
                    for target in targets
                    if abs(target.center_x - prior.center_x) <= round(width * 0.12)
                    and abs(target.center_y - prior.center_y) <= round(height * 0.15)
                    and abs(target.radius - prior.maximum_radius) <= round(width * 0.04)
                ]
            for target in targets:
                seed = RingLock(
                    target.center_x,
                    target.center_y,
                    target.radius,
                    target.radius,
                    target.score,
                )
                measured = fast_current_ring(image, seed, viewport)
                if measured is not None:
                    break
        if measured is not None:
            return measured
    return None


def updated_shrink_rate(
    previous_ratio: float, ratio: float, hold: float, rate: float | None
) -> float | None:
    """Learn how much ratio a second of holding costs.

    A ratio that went up means the circle hit minimum and restarted from full,
    so the drop across that hold spans a reset and says nothing about the rate.
    """
    if ratio > previous_ratio + RING_RESET_JUMP or hold <= 0:
        return rate
    observed = (previous_ratio - ratio) / hold
    if observed <= 0:
        return rate
    return observed if rate is None else (rate + observed) / 2


def next_hold_seconds(
    ratio: float, target: float, rate: float | None, minimum: float
) -> float:
    """Ask for exactly the shortfall, once the rate is known.

    Below the target the circle can only be grown by letting it reach minimum
    and reset, so step in small holds until that reset shows up.
    """
    shortfall = ratio - target
    if shortfall <= 0 or rate is None:
        return minimum
    return min(MAXIMUM_HOLD_SECONDS, max(minimum, shortfall / rate))


async def lock_excellent_ring(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    artifact_dir: Path,
    ball: BallDetection,
) -> RingLock:
    """Shrink the catch circle with timed holds, measuring only between them.

    The circle is not drawn while the ball is held -- a frame taken mid-hold
    shows no ring and no UI chrome at all -- and it freezes at whatever size
    the finger lifted on. So the hold length is the only control, and every
    measurement is taken after a release, on a screen that is standing still.
    That is the easy case for the detector, and it needs no prior to gate on.

    A second press resumes shrinking from the frozen size, so the loop learns
    how much ratio a second of holding costs and asks for exactly the shortfall.
    """
    config = device.config
    probe_point = [ball.center_x, ball.center_y]
    target = (config.target_ratio_min + config.target_ratio_max) / 2

    last_measured: RingLock | None = None
    ratio = 1.0
    rate: float | None = None
    hold = config.calibration_hold_seconds
    for attempt in range(1, config.max_ring_attempts + 1):
        started, ended = await device.stationary_hold(probe_point, hold)
        # The ring is drawn only while the ball is held: it appears about 170ms
        # in, shrinks through the Excellent band between roughly 240ms and
        # 460ms, and is gone once it reaches minimum. Nothing is on screen after
        # the release -- the frozen size survives as game state, not as pixels.
        # So read the hold itself, newest frame first, which is the size the
        # finger lifted on.
        await asyncio.sleep(RING_SETTLE_SECONDS)
        frames = stream.since(started, until=ended)
        _save_frames(frames, artifact_dir, f"hold-{attempt:02d}", ended)
        if not frames:
            print(
                f"[{device.label}] Hold {attempt} ({hold:.2f}s): "
                f"no frames captured after release",
                flush=True,
            )
            continue
        measured = measure_ring(frames, last_measured, device.viewport)
        if measured is None:
            print(
                f"[{device.label}] Hold {attempt} ({hold:.2f}s): "
                f"{len(frames)} frames, no readable circle; nudging",
                flush=True,
            )
            if last_measured is None and attempt >= BLIND_RING_ATTEMPTS:
                raise ExcellentThrowError(
                    f"No catch circle was readable in {attempt} holds; "
                    f"this phone cannot be locked and the ball is better flicked"
                )
            hold = config.pulse_hold_seconds
            continue

        last_measured = measured
        print(
            f"[{device.label}] Hold {attempt} ({hold:.2f}s): "
            f"{measured.current_radius}/{measured.maximum_radius} "
            f"({measured.ratio:.2f})",
            flush=True,
        )
        if config.target_ratio_min <= measured.ratio <= config.target_ratio_max:
            return measured

        previous, ratio = ratio, measured.ratio
        if ratio > previous + RING_RESET_JUMP or ratio < config.target_ratio_min:
            print(f"[{device.label}] Circle reset to full; resuming", flush=True)
            ratio = 1.0
            last_measured = None
            rate = None
            hold = config.calibration_hold_seconds
            continue
        rate = updated_shrink_rate(previous, ratio, hold, rate)
        hold = next_hold_seconds(ratio, target, rate, config.pulse_hold_seconds)
    raise ExcellentThrowError(
        "The catch circle never entered the Excellent lock band; no throw was sent"
    )


async def probe_ring_visibility(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    artifact_dir: Path,
    hold: float,
) -> None:
    """Record one press-hold-release to find when the circle is on screen.

    Everything about sizing depends on knowing which frames actually contain a
    ring, and that has to be observed rather than assumed. This spends no ball:
    it presses and lifts without any swipe.
    """
    ball = await wait_for_encounter(device, None, artifact_dir)
    print(
        f"[{device.label}] Probing with a {hold:.2f}s hold on "
        f"({ball.center_x},{ball.center_y})",
        flush=True,
    )
    started, ended = await device.stationary_hold([ball.center_x, ball.center_y], hold)
    await asyncio.sleep(1.5)
    frames = stream.since(started - 0.2, until=ended + 1.5)
    _save_frames(frames, artifact_dir, "probe", started)
    print(
        f"[{device.label}] Saved {len(frames)} frames to {artifact_dir}; "
        f"release was at +{(ended - started) * 1000:.0f}ms",
        flush=True,
    )


async def calibrate_catch_circle(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    ball: BallDetection,
    artifact_dir: Path,
    hold_seconds: float = 0.35,
) -> RingLock | None:
    """One brief press-hold-release that spends no ball, just reads the circle.

    The catch circle only appears while the ball is held. A short press makes
    it visible long enough for `measure_ring` to lock onto the white target
    plus the colored ring, so the actual throw can aim at the real position
    rather than guessing from the resting encounter frame.
    """
    config = device.config
    hold = max(hold_seconds, config.pulse_hold_seconds)
    started, ended = await device.stationary_hold([ball.center_x, ball.center_y], hold)
    await asyncio.sleep(0.25)
    frames = stream.since(started, until=ended)
    _save_frames(frames, artifact_dir, "calibrate", ended)
    if not frames:
        return None
    measured = measure_ring(frames, None, device.viewport)
    if measured is None:
        return None
    print(
        f"[{device.label}] Calibrated catch circle at "
        f"({measured.center_x},{measured.center_y}) "
        f"r={measured.maximum_radius} confidence={measured.confidence:.2f}",
        flush=True,
    )
    return measured


async def use_nanab_berry(
    device: IOSExcellentThrowDevice,
    ball: BallDetection,
    encounter_image: Image.Image,
    artifact_dir: Path,
) -> None:
    """Select a Nanab by image, then feed it to the current encounter."""
    from . import berry_android

    if berry_in_hand(encounter_image, device.viewport):
        # The last attempt left one in the hand; opening the picker again would
        # only select a second. Throw the one that is already there.
        print(f"[{device.label}] A Nanab is already in hand; feeding it", flush=True)
        await flick_berry(device, ball, encounter_image, artifact_dir)
        await swap_to_ball(device, artifact_dir)
        return

    def frame(image: Image.Image) -> tuple[int, int, int, bytes]:
        rgba = image.convert("RGBA")
        return rgba.width, rgba.height, 0, rgba.tobytes()

    listed: list[tuple[str, list[int]]] | None = None
    picker_image: Image.Image | None = None
    picker_frame: tuple[int, int, int, bytes] | None = None
    sheet_seen = False
    for attempt in range(4):
        picker_image = await asyncio.to_thread(device.screenshot)
        picker_frame = frame(picker_image)
        listed = berry_android.picker_read(picker_frame)
        if listed is not None:
            break
        if berry_android.picker_sheet_top(*picker_frame) is not None:
            sheet_seen = True
        elif attempt == 0:
            await device.tap(_encounter_button(picker_image, device, "berry"))
        await asyncio.sleep(1.0)
    if listed is None or picker_image is None:
        if sheet_seen:
            await device.tap(
                [round(device.viewport[0] * 0.50), round(device.viewport[1] * 0.45)]
            )
            await asyncio.sleep(0.8)
            print(
                f"[{device.label}] A berry is already active; keeping it for the throw",
                flush=True,
            )
            return
        raise ExcellentThrowError(
            f"Nanab picker did not open on {device.label}; no ball was thrown"
        )
    picker_image.save(artifact_dir / "nanab-picker.png")
    nanab_pixel = next((point for kind, point in listed if kind == "nanab"), None)
    if nanab_pixel is None:
        # An empty berry pocket is not a reason to keep the ball -- the Android
        # routine has always thrown without one.  Raising here left the picker
        # open over the encounter, so the caller's fallback flick went into the
        # sheet, the encounter ended on the map, and the finished set re-offered
        # the same Pokemon forever: an SE spent five minutes on one Weezing at
        # nought throws a cycle.  Close the sheet and throw the ball unbuffed.
        names = ", ".join(kind for kind, _point in listed)
        print(
            f"[{device.label}] No Nanab berry in the bag "
            f"(picker: {names}); throwing without one",
            flush=True,
        )
        await device.tap(
            [round(device.viewport[0] * 0.50), round(device.viewport[1] * 0.45)]
        )
        await asyncio.sleep(0.8)
        held = await asyncio.to_thread(device.screenshot)
        if not ball_in_hand(held, device.viewport):
            # The game keeps the last berry it handed out selected, and a
            # thrown berry never catches anything.
            await swap_to_ball(device, artifact_dir)
        return
    nanab_logical = [
        round(nanab_pixel[0] * device.viewport[0] / picker_image.width),
        round(nanab_pixel[1] * device.viewport[1] / picker_image.height),
    ]
    await device.tap(nanab_logical)
    # The picker sheet takes about a second to slide away; flicking through it
    # feeds nothing and leaves the berry in hand for the throw.
    await asyncio.sleep(1.2)
    await flick_berry(device, ball, encounter_image, artifact_dir)
    await swap_to_ball(device, artifact_dir)


async def flick_berry(
    device: IOSExcellentThrowDevice,
    ball: BallDetection,
    encounter_image: Image.Image,
    artifact_dir: Path,
) -> None:
    """Throw the held berry at the Pokemon.

    Selecting a Nanab only puts it in the hand -- it has to be thrown with the
    same gesture as a ball, and the tap that used to be here fed nothing.

    Not verified afterwards: a successful feed makes the game select another
    berry, so a berry is in the hand either way.  One flick, then take the ball
    back with `swap_to_ball`.
    """
    target = _detect_immediate_target(
        encounter_image, ball, device.viewport, device.config
    )
    feed_y = max(
        round(device.viewport[1] * 0.34),
        min(round(device.viewport[1] * 0.58), target.center_y),
    )
    await device.flick(
        [ball.center_x, ball.center_y], [target.center_x, feed_y], NANAB_FLICK_MS
    )
    print(
        f"[{device.label}] Nanab flicked to ({target.center_x},{feed_y})", flush=True
    )
    await asyncio.sleep(NANAB_FEED_SETTLE_SECONDS)


async def swap_to_ball(
    device: IOSExcellentThrowDevice, artifact_dir: Path
) -> bool:
    """Put a ball back in the hand, and say whether it is there.

    Feeding does not hand the ball back; the game reselects a berry.  Without
    this the throw that follows throws the berry, which never catches anything.
    """
    for attempt in range(1, 3):
        frame = await asyncio.to_thread(device.screenshot)
        await device.tap(_encounter_button(frame, device, "switch"))
        await asyncio.sleep(BALL_SHEET_SECONDS)
        sheet = await asyncio.to_thread(device.screenshot)
        await device.tap(_ball_choice_point(sheet, device))
        await asyncio.sleep(BALL_SHEET_SECONDS)
        image = await asyncio.to_thread(device.screenshot)
        image.save(artifact_dir / "ball-in-hand.png")
        if ball_in_hand(image, device.viewport):
            print(f"[{device.label}] Ball back in hand", flush=True)
            return True
        print(
            f"[{device.label}] Still holding an item after swap {attempt}", flush=True
        )
    return False


def _ball_choice_point(
    image: Image.Image, device: IOSExcellentThrowDevice
) -> list[int]:
    """The Great Ball in the open chooser, anchored to the sheet's top edge."""
    from . import berry_android

    rgba = image.convert("RGBA")
    top = berry_android.picker_sheet_top(rgba.width, rgba.height, 0, rgba.tobytes())
    if top is None:
        return _viewport_fraction(device, BALL_CHOICE_POINT)
    return [
        round(device.viewport[0] * BALL_CHOICE_X),
        round(
            (top + image.width * BALL_CHOICE_BELOW_SHEET)
            * device.viewport[1]
            / image.height
        ),
    ]


def _encounter_button(
    image: Image.Image, device: IOSExcellentThrowDevice, which: str
) -> list[int]:
    """Where the berry or held-item button is on this frame, in viewport units."""
    buttons = locate_encounter_buttons(image)
    if buttons is not None:
        point = buttons[0 if which == "berry" else 1]
        return [
            round(point[0] * device.viewport[0] / image.width),
            round(point[1] * device.viewport[1] / image.height),
        ]
    fallback = BERRY_BUTTON_POINT if which == "berry" else BALL_SWITCH_POINT
    return _viewport_fraction(device, fallback)


def _viewport_fraction(
    device: IOSExcellentThrowDevice, fraction: tuple[float, float]
) -> list[int]:
    return [
        round(device.viewport[0] * fraction[0]),
        round(device.viewport[1] * fraction[1]),
    ]


async def run_once(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    *,
    wait_seconds: float | None,
    artifact_dir: Path,
    dry_run: bool,
    use_nanab: bool = False,
    timed_mode: bool = False,
) -> bool:
    ball = await wait_for_encounter(device, wait_seconds, artifact_dir, stream=stream)
    if dry_run:
        print(f"[{device.label}] Dry run complete; no touch sent", flush=True)
        return False
    if stream.latest() is not None:
        encounter_image = stream.latest().image()
    else:
        encounter_image = await asyncio.to_thread(device.screenshot)
    # Anything but a ball left in hand has to be dealt with even when this
    # attempt did not ask for a berry -- otherwise the throw throws it.
    if use_nanab or not ball_in_hand(encounter_image, device.viewport):
        await use_nanab_berry(device, ball, encounter_image, artifact_dir)
        ball = await wait_for_encounter(device, 8.0, artifact_dir, stream=stream)
    straight_mode = bool(device.config.ios_device.get("straight_throw", False))
    single_shot = timed_mode or bool(
        device.config.ios_device.get("single_shot_throw", False)
    )
    if single_shot:
        return await run_single_shot(
            device, stream, ball, artifact_dir, straight_mode=straight_mode
        )
    lock = await lock_excellent_ring(device, stream, artifact_dir, ball)
    print(
        f"[{device.label}] Excellent circle locked at {lock.current_radius}/"
        f"{lock.maximum_radius} ({lock.ratio:.2f})",
        flush=True,
    )
    if device.config.ios_device.get("throw_immediately", False):
        live_lock = lock
        print(
            f"[{device.label}] Immediate mode: throwing at locked target "
            f"({lock.center_x},{lock.center_y}) without waiting for a still target",
            flush=True,
        )
    else:
        live_lock = await wait_for_still_center(device, stream, lock, artifact_dir)
    if straight_mode:
        start, end = straight_throw_points(
            ball, live_lock, device.viewport, device.config
        )
        zero = time.monotonic()
        print(
            f"[{device.label}] Straight throw at locked target: "
            f"{start} -> {end}",
            flush=True,
        )
        await device.straight_throw(start, end, device.config.throw_duration_ms)
        await asyncio.sleep(device.config.result_capture_seconds)
        result_frames = stream.since(zero)
        _save_frames(result_frames, artifact_dir, "result", zero)
        latest_f = stream.latest()
        result_img = latest_f.image() if latest_f is not None else await asyncio.to_thread(device.screenshot)
        result_img.save(artifact_dir / "result-after.png")
        print(
            f"[{device.label}] Straight throw sent; evidence saved to {artifact_dir}",
            flush=True,
        )
        await clear_catch_screens(device, artifact_dir)
        return True
    start, path = curve_throw_path(ball, live_lock, device.viewport, device.config)
    end_x, end_y, release_ms = path[-1]
    zero = time.monotonic()
    print(
        f"[{device.label}] Excellent lock {lock.ratio:.2f}; "
        f"{device.config.curve_direction} curve {start} -> [{end_x},{end_y}] "
        f"({len(path) - 1} spin segments, {release_ms}ms release)",
        flush=True,
    )
    await device.curve_throw(start, path)
    await asyncio.sleep(device.config.result_capture_seconds)
    result_frames = stream.since(zero)
    _save_frames(result_frames, artifact_dir, "result", zero)
    latest_f = stream.latest()
    result_img = latest_f.image() if latest_f is not None else await asyncio.to_thread(device.screenshot)
    result_img.save(artifact_dir / "result-after.png")
    print(f"[{device.label}] Curve throw sent; evidence saved to {artifact_dir}", flush=True)
    await clear_catch_screens(device, artifact_dir)
    return True


# The two screens a catch leaves standing in front of the map.  Four reads is
# the XP card, the Pokemon's own page, and one to confirm the map came back.
CATCH_SCREEN_READS = 4
CATCH_MENU_SETTLE = 1.2
# The caught Pokemon's page closes on the X at the bottom centre.  These are the
# fractions gbl_ios already closes on, and on the SE's 375x667 they land on
# [187, 630], which is the CLOSE_BTN its config names.
CATCH_CLOSE_X = 0.499
CATCH_CLOSE_Y = 0.944


async def clear_catch_screens(
    device: "IOSExcellentThrowDevice", artifact_dir: Path
) -> None:
    """Press through what a catch leaves up, and keep the card it leaves.

    The XP card itemises the throw bonuses, so it is the only ground truth for
    whether the lock actually bought an Excellent -- the ring ratios say what
    was aimed at, not what the game scored.  It is saved before the OK, because
    the card is the last frame that carries it.

    Neither screen goes away on its own, and a run left standing on them never
    sees another encounter: the android-three spent all 25 attempts of one run reporting
    "No ready encounter appeared" at the summary of the previous run's catch.
    """
    from . import gbl_vision

    for _read in range(CATCH_SCREEN_READS):
        image = await asyncio.to_thread(device.screenshot)
        try:
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        except gbl_vision.VisionOCRError:
            return

        summary = gbl_vision.dismiss_point(boxes)
        if summary is not None:
            image.save(artifact_dir / "catch-summary.png")
            scale_x = device.viewport[0] / image.width
            scale_y = device.viewport[1] / image.height
            point = [round(summary[0] * scale_x), round(summary[1] * scale_y)]
            print(f"[{device.label}] Catch summary, taking OK at {point}", flush=True)
            await device.tap(point)
            await asyncio.sleep(CATCH_MENU_SETTLE)
            continue

        blocked = gbl_vision.blocked_label(boxes)
        if blocked is not None and gbl_vision.normalize(blocked) == "power up":
            close = [
                round(device.viewport[0] * CATCH_CLOSE_X),
                round(device.viewport[1] * CATCH_CLOSE_Y),
            ]
            print(
                f"[{device.label}] Caught Pokemon page, closing it at {close}",
                flush=True,
            )
            await device.tap(close)
            await asyncio.sleep(CATCH_MENU_SETTLE)
            continue
        return


def _detect_immediate_target(
    image: Image.Image,
    ball: BallDetection,
    viewport: tuple[int, int],
    config: RuntimeConfig,
) -> RingLock:
    """Pick a target for the immediate single-shot throw.

    The catch circle is only drawn while the ball is held, so the resting
    encounter frame cannot give us its real position. The best heuristic is
    "the Pokemon sits roughly between the ball and the top of the screen,
    slightly above the midpoint of the ball-to-top distance". That lines
    up the curve flick with the actual circle for the common case.
    """
    width, height = viewport
    # Clamp the Pokemon band to a sensible fraction of the screen height.
    # 0.30-0.45 of the screen gives a target well above the ball for small
    # Pokemon and still keeps a large flying Pokemon inside the view.
    default_y = max(round(height * 0.30), min(ball.center_y - round(height * 0.45), round(height * 0.45)))
    targets = target_candidates(image, viewport=viewport, limit=8)
    plausible = [
        candidate
        for candidate in targets
        if candidate.radius >= 10
        and round(width * 0.20) <= candidate.center_x <= round(width * 0.80)
        and round(height * 0.15) <= candidate.center_y <= round(height * 0.65)
    ]
    if plausible:
        # Reject UI artifacts that live in the top of the screen (camera
        # icon, AR toggle, weather pill, top bar). The Pokemon is always
        # in the lower-middle of the frame, below 0.40 of the screen.
        body_zone = [
            candidate
            for candidate in plausible
            if candidate.center_y >= round(height * 0.40)
        ]
        pool = body_zone if body_zone else plausible
        strongest = max(pool, key=lambda item: item.score)
        cluster_x = round(
            sum(c.center_x for c in pool) / len(pool)
        )
        cluster_y = round(
            sum(c.center_y for c in pool) / len(pool)
        )
        target_y = max(
            round(height * 0.30),
            min(
                round(height * 0.55),
                min(c.center_y for c in pool) - round(height * 0.05),
            ),
        )
        confidence = strongest.score
    else:
        cluster_x = ball.center_x
        target_y = default_y
        confidence = 0.0
    maximum_radius = max(70, min(round(width * 0.22), round(height * 0.16)))
    current_radius = max(7, round(maximum_radius * config.target_ratio_min))
    return RingLock(
        center_x=cluster_x,
        center_y=target_y,
        maximum_radius=maximum_radius,
        current_radius=current_radius,
        confidence=confidence,
    )


async def wait_for_throw_result(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    artifact_dir: Path,
) -> bool:
    """Return True only when the encounter ball stays gone through resolution."""
    await asyncio.sleep(max(2.2, device.config.result_capture_seconds))
    deadline = time.monotonic() + 10.0
    ready_frames = 0
    last_image: Image.Image | None = None
    while time.monotonic() < deadline:
        latest = stream.latest()
        if latest is not None:
            last_image = latest.image()
        else:
            last_image = await asyncio.to_thread(device.screenshot)
        if locate_throw_ball(last_image, device.viewport) is not None:
            ready_frames += 1
            if ready_frames >= 2:
                last_image.save(artifact_dir / "result-encounter-returned.png")
                print(
                    f"[{device.label}] Pokemon still in encounter; re-aiming",
                    flush=True,
                )
                return False
        else:
            ready_frames = 0
        await asyncio.sleep(0.55)
    if last_image is not None:
        last_image.save(artifact_dir / "result-encounter-ended.png")
    print(
        f"[{device.label}] Encounter ball stayed gone; catch/encounter completed",
        flush=True,
    )
    return True


async def run_single_shot(
    device: IOSExcellentThrowDevice,
    stream: MJPEGStream,
    ball: BallDetection,
    artifact_dir: Path,
    *,
    straight_mode: bool = False,
) -> bool:
    """Skip ring locking: throw until the Pokemon is caught (or we run out of balls).

    The catch circle only appears while the ball is held and the colored
    ring cycles through Excellent every ~700ms with a ~180ms Excellent
    window. The pre-throw pause lands the release inside one of those
    windows. A brief calibration press spends no ball and gives us the
    real white-target center instead of a guess from the resting frame.

    After each throw, watch for the encounter ball: when it stops showing
    up, the Pokemon either broke out or the catch animation finished. We
    treat the absence of the ball as "Pokemon is no longer in the
    encounter" and stop. The retry loop gives us several chances per
    cycle window without bothering the player.
    """
    config = device.config
    max_attempts = int(device.config.ios_device.get("max_attempts_per_encounter", 6))
    pause_step = float(
        device.config.ios_device.get("retry_pause_step_seconds", 0.18)
    )
    base_pause = config.pre_throw_pause_seconds
    zero = time.monotonic()
    if stream.latest() is not None:
        image = stream.latest().image()
    else:
        image = await asyncio.to_thread(device.screenshot)
    image.save(artifact_dir / "encounter.png")

    target: RingLock | None = None
    if bool(device.config.ios_device.get("calibrate_target", True)):
        target = await calibrate_catch_circle(
            device, stream, ball, artifact_dir
        )
    if target is None:
        target = _detect_immediate_target(image, ball, device.viewport, config)
        print(
            f"[{device.label}] Single-shot mode (heuristic target): "
            f"ball=({ball.center_x},{ball.center_y}) "
            f"target=({target.center_x},{target.center_y}) "
            f"r={target.maximum_radius} confidence={target.confidence:.2f}; "
            f"pre_throw_pause={base_pause:.2f}s",
            flush=True,
        )
    else:
        print(
            f"[{device.label}] Single-shot mode: ball=({ball.center_x},{ball.center_y}) "
            f"target=({target.center_x},{target.center_y}) "
            f"r={target.maximum_radius} confidence={target.confidence:.2f}; "
            f"pre_throw_pause={base_pause:.2f}s; max_attempts={max_attempts}",
            flush=True,
        )

    for attempt in range(1, max_attempts + 1):
        attempt_dir = artifact_dir / f"attempt-{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        # Walk the pre-throw pause by a fraction of a cycle each retry so
        # the release samples a different part of the ring window. A
        # 180ms step lands in the next Excellent band 700ms later.
        pause_seconds = base_pause + pause_step * (attempt - 1)
        if straight_mode:
            start, end = straight_throw_points(
                ball, target, device.viewport, config
            )
            await device.straight_throw(start, end, config.throw_duration_ms)
        else:
            start, path = curve_throw_path(ball, target, device.viewport, config)
            end_x, end_y, release_ms = path[-1]
            spin_ms = sum(int(d) for _, _, d in path)
            print(
                f"[{device.label}] Single-shot throw {attempt}/{max_attempts}: "
                f"pause={pause_seconds:.2f}s spin={spin_ms}ms release={release_ms}ms "
                f"flick=({end_x},{end_y})",
                flush=True,
            )
            await device.curve_throw(
                start, path, initial_pause=pause_seconds
            )
        await asyncio.sleep(config.result_capture_seconds)
        result_frames = stream.since(zero)
        _save_frames(result_frames, attempt_dir, "result", zero)
        latest_f = stream.latest()
        result_img = latest_f.image() if latest_f is not None else await asyncio.to_thread(device.screenshot)
        result_img.save(attempt_dir / "result-after.png")

        if await wait_for_throw_result(device, stream, attempt_dir):
            print(
                f"[{device.label}] Encounter ended after attempt {attempt}; "
                f"evidence in {attempt_dir}",
                flush=True,
            )
            return True
        print(
            f"[{device.label}] Attempt {attempt} missed (encounter still open); "
            f"re-aiming and retrying",
            flush=True,
        )
    print(
        f"[{device.label}] Hit max_attempts={max_attempts} without the "
        "encounter ending; restarting the aim cycle",
        flush=True,
    )
    return False


@contextmanager
def device_lock(udid: str) -> Iterator[None]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    handle = (LOCK_DIR / f"{udid}.lock").open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExcellentThrowError("This iPhone is already controlled by another automation") from exc
        yield
    finally:
        handle.close()




def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        help="throw profile use (default: profile phone plugged in)",
    )
    parser.add_argument("--wait", type=float, help="seconds wait manually opened encounter")
    parser.add_argument(
        "--throws",
        type=int,
        help="number curve throws send (default: keep watching until Ctrl-C)",
    )
    parser.add_argument("--dry-run", action="store_true", help="detect an encounter but do not touch")
    parser.add_argument("--check", action="store_true", help="take one guarded screenshot exit")
    parser.add_argument("--screenshot", type=Path, default=ARTIFACT_ROOT / "check.png")
    parser.add_argument("--artifacts", type=Path, help="override per-run evidence directory")
    parser.add_argument(
        "--ring-lock",
        action="store_true",
        help="hold and measure the catch circle, releasing inside the Excellent "
             "band, instead of the single-shot timing sweep",
    )
    parser.add_argument(
        "--probe-ring",
        type=float,
        metavar="HOLD",
        help="record one press-hold-release HOLD seconds exit; spends no ball",
    )
    return parser.parse_args(arguments)


def await_attached_throw_configs(delay_seconds: float = 5.0) -> list[Path]:
    """Wait until all configured throw profiles match USB-attached phones."""
    while True:
        profiles = known_configs()
        expected: list[str] = []
        for path in profiles:
            udid = config_udid(path)
            if udid is not None and udid not in expected:
                expected.append(udid)

        if not expected:
            raise ExcellentThrowError(
                f"No {CONFIG_GLOB} profiles found in: "
                + ", ".join(str(item) for item in config_paths.search_dirs())
            )

        try:
            attached = ios_attached_devices.attached_devices()
        except ios_attached_devices.AttachedDeviceError as exc:
            print(f"Could not read attached iPhones: {exc}; retrying", flush=True)
            time.sleep(delay_seconds)
            continue

        usb = {udid for udid, connection in attached.items() if connection == "USB"}
        if not usb:
            non_usb = [
                f"{udid} via {connection}"
                for udid, connection in attached.items()
                if connection != "USB"
            ]
            print(
                "Waiting for USB-attached iPhones. Connected phones are not on USB cable: "
                + (
                    ", ".join(non_usb)
                    if non_usb
                    else "none"
                ),
                flush=True,
            )
            time.sleep(delay_seconds)
            continue

        missing = [udid for udid in expected if udid not in usb]
        if missing:
            print(
                f"Waiting for throw devices: {', '.join(missing)}",
                flush=True,
            )
            time.sleep(delay_seconds)
            continue

        selected = select_configs()
        if not selected:
            print("Devices attached but no throw config matches yet; retrying", flush=True)
            time.sleep(delay_seconds)
            continue

    return selected


def await_attached_throw_configs(delay_seconds: float = 5.0) -> list[Path]:
    """Wait for any attached profiled iPhone, without requiring offline phones."""
    while True:
        try:
            selected = select_configs()
        except ExcellentThrowError as exc:
            print(f"Waiting for an attached throw device: {exc}", flush=True)
            time.sleep(delay_seconds)
            continue
        if selected:
            return selected
        time.sleep(delay_seconds)


async def async_main(args: argparse.Namespace, config: RuntimeConfig) -> int:
    device = await asyncio.to_thread(IOSExcellentThrowDevice.connect, config)
    normal_exit = False
    stream: MJPEGStream | None = None
    try:
        if args.check:
            image = await asyncio.to_thread(device.screenshot)
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            image.save(args.screenshot)
            score = encounter_confidence(image, device.viewport)
            print(
                f"[{device.label}] viewport={device.viewport[0]}x{device.viewport[1]} "
                f"encounter={score >= 0.55} confidence={score:.2f}; saved {args.screenshot}",
                flush=True,
            )
            normal_exit = True
            return 0

        run_stamp = time.strftime("%Y%m%d-%H%M%S")
        base = args.artifacts or ARTIFACT_ROOT / run_stamp
        base.mkdir(parents=True, exist_ok=True)

        wait_seconds = args.wait if args.wait is not None else config.wait_seconds
        port = config.ios_device.get("mjpeg_server_port")
        if type(port) is not int:
            raise ExcellentThrowError("device.mjpeg_server_port must be configured")

        stream = MJPEGStream(port)
        stream.start()

        if args.probe_ring is not None:
            directory = base / "probe"
            directory.mkdir(parents=True, exist_ok=True)
            await probe_ring_visibility(device, stream, directory, args.probe_ring)
            normal_exit = True
            return 0

        limit = 1 if args.dry_run else args.throws
        sent = 0
        attempt = 0
        # Tied to encounters, not to the attempt number: attempts spent waiting
        # for a spawn that never comes would otherwise use up `attempt == 1`
        # and leave the first real encounter without a berry.  A re-aim at the
        # same encounter does not need another; the berry is still in effect.
        fresh_encounter = True

        while limit is None or sent < limit:
            attempt += 1
            directory = base / f"attempt-{attempt:02d}"
            directory.mkdir(parents=True, exist_ok=True)
            try:
                did_throw = await run_once(
                    device,
                    stream,
                    wait_seconds=None if args.throws is not None else wait_seconds,
                    artifact_dir=directory,
                    dry_run=args.dry_run,
                    use_nanab=fresh_encounter,
                    timed_mode=not args.ring_lock,
                )
            except ExcellentThrowError as exc:
                if str(exc).startswith("No ready encounter"):
                    print(
                        f"[{device.label}] Attempt {attempt} refused: {exc}; still watching",
                        flush=True,
                    )
                    if limit is not None and args.dry_run:
                        return 1
                    await asyncio.sleep(0.8)
                    continue
                raise

            fresh_encounter = did_throw
            if did_throw:
                sent += 1
                normal_exit = True
                if limit is not None and sent >= limit:
                    return 0
    finally:
        if stream is not None:
            stream.stop()
        await device.quit()
        if normal_exit:
            print(f"[{device.label}] iPhone WebDriverAgent kept ready for next run", flush=True)
        else:
            await asyncio.to_thread(
                ios_wda_cleanup.stop_wda_runner,
                config.ios_device["udid"],
            )


async def run_device_with_lock(args: argparse.Namespace, config: RuntimeConfig) -> int:
    def _run() -> int:
        with device_lock(config.ios_device["udid"]):
            return asyncio.run(async_main(args, config))

    return await asyncio.to_thread(_run)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    if args.wait is not None and args.wait <= 0:
        raise ExcellentThrowError("--wait must be positive")
    if args.throws is not None and args.throws < 1:
        raise ExcellentThrowError("--throws must be positive")

    config_paths_list = (
        [args.config.resolve()] if args.config is not None else await_attached_throw_configs()
    )
    configs = [load_runtime_config(path) for path in config_paths_list]

    for c_path, cfg in zip(config_paths_list, configs):
        print(
            f"Using {c_path.name} {cfg.ios_device.get('name', 'iPhone')} "
            f"({cfg.ios_device['udid']})",
            flush=True,
        )

    if len(configs) == 1:
        config = configs[0]
        with device_lock(config.ios_device["udid"]):
            return asyncio.run(async_main(args, config))

    async def _run_all() -> int:
        tasks = [asyncio.create_task(run_device_with_lock(args, cfg)) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        failed = 0
        for result in results:
            if isinstance(result, Exception):
                print(f"[task] Device handler failed: {result}", flush=True)
                failed += 1

        return 1 if failed else 0

    return asyncio.run(_run_all())
