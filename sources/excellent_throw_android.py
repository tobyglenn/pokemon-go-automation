#!/usr/bin/env python3
"""Vision-guided excellent throws for Android phones using adb frame capture."""

from __future__ import annotations

from . import config_paths

import argparse
import asyncio
import contextlib
import io
import os
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from . import excellent_throw_ios

try:
    from ppadb.client_async import ClientAsync
    from ppadb.device_async import DeviceAsync
except ModuleNotFoundError as exc:  # pragma: no cover - environment dependency
    raise RuntimeError("Missing pure-python-adb dependency; install docs/requirements.txt") from exc

ARTIFACT_ROOT = config_paths.state_dir() / "excellent-throw"

# How many captures the calibration hold should have room for, and the bounds
# on the hold that buys them.  See hold_for_capture.
RING_SAMPLES = 2
# A hold's reading is only aimed at if it lands where a catch circle can be.
# The android-three's first hold on 1 Sep 2026 came back with (688,283) on a 2992-tall
# screen -- 0.09h, up in the sky -- and a ball thrown at that is thrown away.
RING_BAND = (0.22, 0.70)
RING_X_BAND = (0.15, 0.85)
# What to throw when no ring could be read.  The one hand-verified catching
# throw is the moto's, 360,1300 -> 360,560 at 720x1600: 0.46h of travel in
# 260ms.  A throw's power is that length, measured from wherever this phone's
# ball actually rests -- not a shared endpoint, which is a different throw on
# every handset.  The android-three's ball sits at 0.913h against the moto's 0.8125h, so
# ending both at 0.35h threw the android-three 22% harder than the swipe that worked and
# its balls sailed over the Pokemon on 1-2 Sep 2026.
PROVEN_THROW_RISE = 0.46
PROVEN_THROW_CEILING = 0.12  # never flick off the top of the screen
RING_HOLD_MARGIN = 0.4
RING_HOLD_CEILING = 4.0

# A Nanab still sitting in the hand is unmistakable: it is a pink and yellow
# bunch where a ball would be.  Measured over the android-one's own artifacts, the
# fraction of pink pixels inside the held disc is 0.36 with a berry in hand and
# 0.000 with a Great Ball, so anything above this is a berry that never left.
BERRY_PINK_FRACTION = 0.15
# Fallbacks only.  The corner buttons are found per frame by
# `excellent_throw_ios.locate_encounter_buttons`, because their y is not the
# same on every phone: 0.836 on the android-one, 0.853 on the moto, 0.914 on the android-three.
BERRY_BUTTON_POINT = (0.123, 0.855)
BALL_SWITCH_POINT = (0.872, 0.839)
# The Great Ball inside the chooser sheet, second in its first row.  Measured
# against the sheet, not the screen: it sits at 0.498 of the width and exactly
# 0.167 of the width below the sheet's top edge on both the android-one and the android-three,
# whose screen fractions are 0.747 and 0.843.  The pair below is the fallback.
BALL_CHOICE_X = 0.498
BALL_CHOICE_BELOW_SHEET = 0.167
BALL_CHOICE_POINT = (0.498, 0.750)
BALL_SHEET_SECONDS = 1.2
# How long a ball detection stays worth agreeing with. Two reads of the same
# ball used to have to be back-to-back, and on the android-one they never were: the
# reward catch on 2026-09-09 scored a flat 1.00 on the ball and still refused
# for 20 seconds, because every other frame either read a different blob (the
# Pokemon's head scores as well as the ball -- see the ring seeding notes) or
# came back blank, and each disagreement threw the good read away. Two reads
# that agree inside a second are the same evidence without that fragility.
#
# A second is only the floor.  A window shorter than the phone's own read
# cadence throws every reading away before the next one arrives, and the
# window can never be met: the foldable scored a flat 1.00 for the whole 25s
# of an award encounter on 10 Sep 2026 and never locked, because one capture
# and search on it costs about a second and the previous read had always just
# expired.  Hold a reading for a few reads instead of a fixed wall-clock
# second, so the lock costs the same number of agreeing frames on every phone.
#
# The count has to cover frames the ball is *not* legible in, too.  A tan
# Pokemon standing over the corridor the ball is searched in merges with it
# and the frame reads as nothing at all: the foldable's Fearow on 10 Sep 2026
# gave one clean reading -- the same (612,2517) r283 every time -- once every
# four reads, four seconds apart, and 2.5 reads of memory expired each one
# before its twin arrived.  Only a *current* detection can fire the lock, and
# it still has to agree on position, so holding the history longer cannot
# invent an encounter that has ended.
BALL_AGREE_WINDOW = 1.0
BALL_AGREE_READS = 6.0


def agree_window(read_seconds: float) -> float:
    """How long a ball reading stays worth agreeing with on this phone."""
    return max(BALL_AGREE_WINDOW, read_seconds * BALL_AGREE_READS)

# Every vision helper resizes its frame to the viewport it is handed, so the
# viewport is what sets the cost of a read.  The iPhone hands them 375x667
# points; the Android path was handing them the phone's own 1316x2560, which is
# thirteen times the pixels and, because the target search sweeps a corridor of
# center hypotheses at 6px steps, about fifteen times the hypotheses.  One held
# frame took 45s on the android-one and a single throw took nearly four minutes.
ANALYSIS_WIDTH = 420
# The berry is tapped, not thrown, so there is no flick duration here at all.
# See feed_berry.
NANAB_FEED_SETTLE_SECONDS = 1.6
NANAB_FEED_TAPS = 2      # the second is for a tap that missed a moving berry
ADB_BINARY = Path(__file__).resolve().parent.parent / "adb"
ADB_PATH = str(ADB_BINARY) if ADB_BINARY.exists() else "adb"
SCREENCAP_TIMEOUT = 10


class AndroidExcellentThrowError(RuntimeError):
    pass


class NoEncounterError(AndroidExcellentThrowError):
    """Nothing throwable was on screen before the wait ran out.

    Its own type because the callers that retry a refused throw are retrying
    the *berry* — and there is no point spending a second wait on an encounter
    that was never there.
    """


@dataclass(frozen=True)
class AndroidThrowConfig:
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
    result_capture_seconds: float


@dataclass
class AndroidDevice:
    device: "DeviceAsync"
    serial: str
    label: str
    viewport: tuple[int, int]
    config: AndroidThrowConfig
    display_id: str | None = None   # set by prepare_devices(); only foldables need it

    async def screenshot_raw(self) -> Image.Image:
        """One screencap as the raw framebuffer, decoded here rather than on the phone.

        `screencap -p` makes the handset encode a PNG, and that encode is most
        of the cost: measured on 24 Aug it is 0.98-1.73s on the moto and
        4.04-4.28s on the android-one, against 0.52-0.60s and 1.09-1.21s for the raw
        buffer.  The ring can only be read from a frame captured while the ball
        is still held, so the capture has to be the cheap one.
        """
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH,
            "-s",
            self.serial,
            "exec-out",
            "screencap",
            *(("-d", self.display_id) if self.display_id else ()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            data, _ = await asyncio.wait_for(proc.communicate(), SCREENCAP_TIMEOUT)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise AndroidExcellentThrowError(f"Screen capture timeout on {self.label}") from exc
        image = decode_raw_framebuffer(data)
        if image is None:
            # The android-three's buffer does not decode as any header this knows, and a
            # phone that cannot be read is a phone that cannot be driven at all.
            # Paying the on-device PNG encode is worth more than skipping it.
            image = await self.screenshot_png()
        if image is None:
            raise AndroidExcellentThrowError(f"Unreadable screenshot from {self.label}")
        return image

    async def screenshot_png(self) -> Image.Image | None:
        """The slow read: `screencap -p`, encoded on the handset.

        Only used when the raw framebuffer will not decode.  It costs about
        1.0s on the moto and 4.1s on the android-one against 0.7s and 1.3s raw, so it
        is a fallback and never the first choice.
        """
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH,
            "-s",
            self.serial,
            "exec-out",
            "screencap",
            "-p",
            *(("-d", self.display_id) if self.display_id else ()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            data, _ = await asyncio.wait_for(proc.communicate(), SCREENCAP_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        if not data:
            return None
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except OSError:
            return None

    async def input_swipe(self, x0: int, y0: int, x1: int, y1: int, duration_ms: int) -> None:
        await self.device.shell(f"input swipe {x0} {y0} {x1} {y1} {duration_ms}")

    async def stationary_hold(self, point: list[int], seconds: float) -> tuple[float, float]:
        x0, y0 = point
        x1 = max(0, min(self.viewport[0] - 1, x0 + 1))
        y1 = max(0, min(self.viewport[1] - 1, y0 + 1))
        started = time.monotonic()
        hold_ms = max(60, round(seconds * 1000))
        await self.input_swipe(x0, y0, x1, y1, hold_ms)
        ended = time.monotonic()
        return started, ended

    async def straight_throw(self, start: list[int], end: list[int], duration_ms: int) -> None:
        x0, y0 = start
        x1, y1 = end
        await self.input_swipe(
            int(max(0, min(self.viewport[0] - 1, x0))),
            int(max(0, min(self.viewport[1] - 1, y0))),
            int(max(0, min(self.viewport[0] - 1, x1))),
            int(max(0, min(self.viewport[1] - 1, y1))),
            int(duration_ms),
        )

    async def tap(self, point: tuple[int, int] | list[int]) -> None:
        x, y = point
        await self.device.shell(f"input tap {int(x)} {int(y)}")

    async def curve_throw(
        self,
        start: list[int],
        path: Iterable[tuple[int, int, int]],
        initial_pause: float | None = None,
    ) -> None:
        """Send one pointer gesture from spin through release.

        Not the throw path: every `input` in the batch is its own process, and
        the android-one charges about 0.7s for each, so a ten-segment curve reaches
        the game as a seven-second drag.  The ball is set down rather than
        thrown -- 683 Great Balls before three "throws" and 683 after.  A
        single `input swipe` is one gesture with real velocity and does throw.
        """
        help_text = await self.device.shell("input help")
        motion = "motionevent" if "motionevent" in str(help_text) else "event"
        x0 = max(0, min(self.viewport[0] - 1, int(start[0])))
        y0 = max(0, min(self.viewport[1] - 1, int(start[1])))
        commands = [f"input {motion} DOWN {x0} {y0}"]
        if initial_pause is not None and initial_pause > 0:
            commands.append(f"sleep {float(initial_pause):.3f}")
        last_x, last_y = x0, y0
        for x, y, duration_ms in path:
            last_x = max(0, min(self.viewport[0] - 1, int(round(x))))
            last_y = max(0, min(self.viewport[1] - 1, int(round(y))))
            commands.append(f"sleep {max(0.01, int(duration_ms) / 1000):.3f}")
            commands.append(f"input {motion} MOVE {last_x} {last_y}")
        commands.append(f"input {motion} UP {last_x} {last_y}")
        await self.device.shell("; ".join(commands))


def _coerce_path(paths: list[str] | None) -> set[str] | None:
    if not paths:
        return None
    return set(serial for serial in paths if serial.strip())


def _load_yaml(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    import yaml

    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        raise AndroidExcellentThrowError(f"Config {path} must be YAML object")
    return value


def _read_float(value: Any, *, label: str, default: float, minimum: float | None = None, maximum: float | None = None) -> float:
    if value is None:
        value = default
    if not isinstance(value, int | float):
        raise AndroidExcellentThrowError(f"{label} must be a number")
    value_f = float(value)
    if minimum is not None and value_f < minimum:
        raise AndroidExcellentThrowError(f"{label} must be >= {minimum}")
    if maximum is not None and value_f > maximum:
        raise AndroidExcellentThrowError(f"{label} must be <= {maximum}")
    return value_f


def _read_int(value: Any, *, label: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    if value is None:
        value = default
    if not isinstance(value, int):
        raise AndroidExcellentThrowError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise AndroidExcellentThrowError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise AndroidExcellentThrowError(f"{label} must be <= {maximum}")
    return value


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Optional YAML override for throw tuning")
    parser.add_argument("--wait", type=float, default=10.0, help="seconds to wait per encounter")
    parser.add_argument("--throws", type=int, help="max throws before exit")
    parser.add_argument("--dry-run", action="store_true", help="run detection and save evidence only")
    parser.add_argument("--check", action="store_true", help="take one screenshot and exit")
    parser.add_argument("--screenshot", type=Path, default=ARTIFACT_ROOT / "check.png", help="where --check screenshot is saved")
    parser.add_argument("--artifacts", type=Path, default=ARTIFACT_ROOT, help="base artifact directory")
    parser.add_argument("--devices", action="append", help="only these Android serials")
    parser.add_argument(
        "--ring-hold",
        action="store_true",
        help="hold the ball first to measure the catch circle (slow; see calibrate_target)",
    )
    parser.add_argument(
        "--straight",
        action="store_true",
        help="accepted for compatibility; Android always throws straight",
    )
    parser.add_argument("--curve-direction", choices=("clockwise", "counterclockwise"), default="clockwise")
    parser.add_argument("--curve-offset", type=float, default=0.09)
    parser.add_argument("--spin-radius", type=float, default=0.065)
    parser.add_argument("--spin-turns", type=float, default=2.0)
    parser.add_argument("--throw-ms", type=int, default=140)
    parser.add_argument("--result-capture", type=float, default=2.8)
    parser.add_argument("--pre-throw-pause", type=float, default=1.29)
    return parser.parse_args(arguments)


def load_config(raw_root: dict[str, Any] | None, args: argparse.Namespace) -> AndroidThrowConfig:
    ring = raw_root.get("ring") if isinstance(raw_root, dict) else {}
    throw = raw_root.get("throw") if isinstance(raw_root, dict) else {}
    if not isinstance(ring, dict):
        ring = {}
    if not isinstance(throw, dict):
        throw = {}

    wait_seconds = _read_float(args.wait, label="wait", default=10.0, minimum=1.0, maximum=3600.0)
    calibration_hold_seconds = _read_float(
        ring.get("calibration_hold_seconds", 0.35),
        label="ring.calibration_hold_seconds",
        default=0.35,
        minimum=0.05,
        maximum=1.0,
    )
    pulse_hold_seconds = _read_float(
        ring.get("pulse_hold_seconds", 0.2),
        label="ring.pulse_hold_seconds",
        default=0.2,
        minimum=0.06,
        maximum=0.8,
    )
    target_ratio_min = _read_float(
        ring.get("target_ratio_min", 0.14),
        label="ring.target_ratio_min",
        default=0.14,
        minimum=0.08,
        maximum=0.60,
    )
    target_ratio_max = _read_float(
        ring.get("target_ratio_max", 0.24),
        label="ring.target_ratio_max",
        default=0.24,
        minimum=target_ratio_min,
        maximum=0.60,
    )
    if target_ratio_max <= target_ratio_min:
        raise AndroidExcellentThrowError("ring.target_ratio_max must be greater than min")
    max_pulses = _read_int(ring.get("max_pulses", 20), label="ring.max_pulses", default=20, minimum=1, maximum=100)
    max_ring_attempts = _read_int(
        ring.get("max_attempts", 4),
        label="ring.max_attempts",
        default=4,
        minimum=1,
        maximum=100,
    )
    throw_duration_ms = _read_int(
        throw.get("duration_ms", args.throw_ms),
        label="throw.duration_ms",
        default=args.throw_ms,
        minimum=80,
        maximum=500,
    )
    end_offset_height = _read_float(
        throw.get("end_offset_height", 0.05),
        label="throw.end_offset_height",
        default=0.05,
        minimum=0.0,
        maximum=0.20,
    )
    curve_direction = str(throw.get("curve_direction", args.curve_direction or "clockwise"))
    if curve_direction not in {"clockwise", "counterclockwise"}:
        raise AndroidExcellentThrowError("throw.curve_direction must be clockwise or counterclockwise")
    curve_offset_width = _read_float(
        throw.get("curve_offset_width", args.curve_offset),
        label="throw.curve_offset_width",
        default=args.curve_offset,
        minimum=-0.40,
        maximum=0.40,
    )
    spin_radius_width = _read_float(
        throw.get("spin_radius_width", args.spin_radius),
        label="throw.spin_radius_width",
        default=args.spin_radius,
        minimum=0.025,
        maximum=0.12,
    )
    spin_turns = _read_float(
        throw.get("spin_turns", args.spin_turns),
        label="throw.spin_turns",
        default=args.spin_turns,
        minimum=1.0,
        maximum=4.0,
    )
    spin_segment_ms = _read_int(
        throw.get("spin_segment_ms", 24),
        label="throw.spin_segment_ms",
        default=24,
        minimum=10,
        maximum=80,
    )

    pre_throw_pause_seconds = _read_float(
        throw.get("pre_throw_pause_seconds", args.pre_throw_pause),
        label="throw.pre_throw_pause_seconds",
        default=args.pre_throw_pause,
        minimum=0,
        maximum=4,
    )
    result_capture_seconds = _read_float(
        throw.get("result_capture_seconds", args.result_capture),
        label="throw.result_capture_seconds",
        default=args.result_capture,
        minimum=0.2,
        maximum=10,
    )

    return AndroidThrowConfig(
        wait_seconds=wait_seconds,
        calibration_hold_seconds=calibration_hold_seconds,
        pulse_hold_seconds=pulse_hold_seconds,
        target_ratio_min=target_ratio_min,
        target_ratio_max=target_ratio_max,
        max_pulses=max_pulses,
        max_ring_attempts=max_ring_attempts,
        throw_duration_ms=throw_duration_ms,
        end_offset_height=end_offset_height,
        curve_direction=curve_direction,
        curve_offset_width=curve_offset_width,
        spin_radius_width=spin_radius_width,
        spin_turns=spin_turns,
        spin_segment_ms=spin_segment_ms,
        pre_throw_pause_seconds=pre_throw_pause_seconds,
        result_capture_seconds=result_capture_seconds,
    )


@dataclass(frozen=True)
class AnalysisView:
    """The small viewport the vision reads, and the way back to real pixels."""

    viewport: tuple[int, int]
    scale_x: float
    scale_y: float

    def grow_ball(self, ball: excellent_throw_ios.BallDetection) -> excellent_throw_ios.BallDetection:
        return excellent_throw_ios.BallDetection(
            ball.score,
            round(ball.center_x * self.scale_x),
            round(ball.center_y * self.scale_y),
            round(ball.radius * self.scale_x),
        )

    def shrink_ball(self, ball: excellent_throw_ios.BallDetection) -> excellent_throw_ios.BallDetection:
        return excellent_throw_ios.BallDetection(
            ball.score,
            round(ball.center_x / self.scale_x),
            round(ball.center_y / self.scale_y),
            round(ball.radius / self.scale_x),
        )

    def grow_lock(self, lock: excellent_throw_ios.RingLock) -> excellent_throw_ios.RingLock:
        return excellent_throw_ios.RingLock(
            round(lock.center_x * self.scale_x),
            round(lock.center_y * self.scale_y),
            round(lock.maximum_radius * self.scale_x),
            round(lock.current_radius * self.scale_x),
            lock.confidence,
        )


def analysis_view(viewport: tuple[int, int]) -> AnalysisView:
    """Read at roughly the iPhone's resolution, whatever the panel is."""
    width, height = viewport
    if width <= ANALYSIS_WIDTH:
        return AnalysisView(viewport, 1.0, 1.0)
    small_height = max(1, round(height * ANALYSIS_WIDTH / width))
    return AnalysisView(
        (ANALYSIS_WIDTH, small_height), width / ANALYSIS_WIDTH, height / small_height
    )


async def find_display_id(device: "DeviceAsync") -> str | None:
    """The display to capture, or None when the phone only has one.

    Foldables report two, and `screencap` with no -d prints a warning line onto
    stdout ahead of the framebuffer, so the header unpack reads that text as
    the width and height and every read fails.  The *active* display: folding
    the phone shut hands over to the cover display, a different coordinate
    space.  Same probe as `gbl_android.find_display_id`.
    """
    try:
        listed = await device.shell("dumpsys SurfaceFlinger --display-id")
        ids = re.findall(r"^Display (\d+)", listed, re.M)
        if len(ids) < 2:
            return None
        for viewport in (await device.shell("dumpsys display")).split("DisplayViewport{")[1:]:
            if "isActive=true" not in viewport:
                continue
            match = re.search(r"uniqueId=\'local:(\d+)\'", viewport)
            if match and match.group(1) in ids:
                return match.group(1)
        return ids[0]
    except Exception:
        return None


def decode_raw_framebuffer(data: bytes) -> Image.Image | None:
    """The pixels behind `adb exec-out screencap`, or None if this is not a frame.

    Android 10 and later write a colorspace word after the pixel format, older
    releases do not, and the fleet has both.  The buffer length is what says
    which header this phone wrote -- the same test `gbl_android.screencap_raw`
    makes.
    """
    if len(data) < 12:
        return None
    width, height, _fmt = struct.unpack("<III", data[:12])
    pixels = width * height * 4
    offset = 16 if len(data) >= pixels + 16 else 12
    if not width or not height or len(data) < pixels + offset:
        return None
    return Image.frombytes(
        "RGBA", (width, height), data[offset:offset + pixels]
    ).convert("RGB")


async def capture_frame(device: AndroidDevice) -> tuple[Image.Image, float]:
    """One decoded frame, and the moment it was taken.

    Every read on this path goes through the raw framebuffer.  `screencap -p`
    makes the handset encode a PNG first, which is 4.0-4.3s of the android-one's time
    against 1.1-1.2s for the raw buffer, and a whole catch has to fit inside
    thirty seconds.
    """
    last_error: AndroidExcellentThrowError | None = None
    for _attempt in range(3):
        try:
            return await device.screenshot_raw(), time.monotonic()
        except AndroidExcellentThrowError as exc:
            last_error = exc
            await asyncio.sleep(0.15)
    raise AndroidExcellentThrowError(
        f"Three screenshot attempts failed on {device.label}: {last_error}"
    ) from last_error


async def wait_for_connected_device(view_label: str, filter_devices: set[str] | None) -> list[DeviceAsync]:
    client = ClientAsync()
    devices = await client.devices()
    if filter_devices:
        devices = [device for device in devices if device.serial in filter_devices]
    if not devices:
        raise AndroidExcellentThrowError(f"No Android devices found for {view_label}")
    return devices


async def prepare_devices(args: argparse.Namespace, config: AndroidThrowConfig) -> list[AndroidDevice]:
    filter_devices = _coerce_path(args.devices)
    devices = await wait_for_connected_device("connected devices", filter_devices)
    prepared: list[AndroidDevice] = []
    for raw in devices:
        display_id = await find_display_id(raw)
        try:
            image, _ = await capture_frame(
                AndroidDevice(raw, raw.serial, raw.serial, (0, 0), config, display_id)
            )
        except AndroidExcellentThrowError as exc:
            print(f"[{raw.serial}] Failed initial screenshot; skipping ({exc})")
            continue
        prepared.append(
            AndroidDevice(
                device=raw,
                serial=raw.serial,
                label=raw.serial,
                viewport=image.size,
                config=config,
                display_id=display_id,
            )
        )
    if not prepared:
        raise AndroidExcellentThrowError("No Android device ready with usable screenshot")
    return prepared


async def detect_encounter_ball(
    device: AndroidDevice,
    wait_seconds: float | None,
    artifact_dir: Path,
    lone_reading_ok: bool = False,
) -> excellent_throw_ios.BallDetection:
    """Confirm ball geometry across captures using this device's read latency.

    Two agreeing detections are required by default. A moving Pokemon can
    hide the ball between captures, so callers with independent encounter
    evidence may explicitly allow one high-confidence detection.
    """
    deadline = time.monotonic() + wait_seconds if wait_seconds is not None else None
    view = analysis_view(device.viewport)
    recent: list[tuple[float, excellent_throw_ios.BallDetection]] = []
    last_score = -1.0
    image: Image.Image | None = None
    read_seconds = 0.0
    while deadline is None or time.monotonic() < deadline:
        read_started = time.monotonic()
        image, ts = await capture_frame(device)
        if (ts % 1.0) < 0.1:
            # keep logging throttled and avoid spamming on a 30Hz device.
            pass
        detection = excellent_throw_ios.locate_throw_ball(image, view.viewport)
        score = detection.score if detection is not None else 0.0
        read_seconds = time.monotonic() - read_started
        if detection is not None and detection.score >= 0.55:
            now = time.monotonic()
            recent = [(seen, ball) for seen, ball in recent
                      if now - seen <= agree_window(read_seconds)]
            agreed = any(
                excellent_throw_ios.same_encounter_ball(ball, detection, view.viewport)
                for _seen, ball in recent
            )
            recent.append((now, detection))
            if agreed or lone_reading_ok:
                image.save(artifact_dir / "encounter.jpg", "JPEG", quality=72)
                ready = view.grow_ball(detection)
                alone = "" if agreed else ", on one reading"
                print(
                    f"[{device.label}] Encounter ready: "
                    f"ball=({ready.center_x},{ready.center_y}) r={ready.radius} "
                    f"conf={ready.score:.2f}{alone}"
                )
                return ready
        # A blank or low-scoring frame ages the history rather than emptying
        # it: the lock only ever fires on a *current* detection agreeing with a
        # recent one, so an encounter that has really ended still cannot fire.
        if last_score < 0 or abs(score - last_score) >= 0.15:
            print(f"[{device.label}] Waiting for wild Pokemon ({score:.2f})")
            last_score = score
        await asyncio.sleep(0.15)
    # The frame it gave up on. Nothing was kept when this refused on the android-one's
    # reward catch, so the only evidence left was the printed scores -- and a
    # refusal at a flat 1.00 needs the picture to be read at all.
    if image is not None:
        image.save(artifact_dir / "refused.jpg", "JPEG", quality=72)
    raise NoEncounterError(f"No ready encounter appeared within {wait_seconds:g} seconds")


@dataclass(frozen=True)
class HeldFrame:
    """A frame already decoded here, in the shape `measure_ring` reads.

    `StreamFrame` carries encoded bytes and decodes them on demand, which is
    right for the iPhone's video stream.  The raw framebuffer is already
    pixels, and re-encoding it only to decode it again would put the phone's
    PNG cost back on the Mac.
    """

    timestamp: float
    decoded: Image.Image

    def image(self) -> Image.Image:
        return self.decoded


async def measure_capture_cost(device: AndroidDevice) -> float:
    """How long one raw screencap takes on this phone, right now."""
    started = time.monotonic()
    await device.screenshot_raw()
    return time.monotonic() - started


def hold_for_capture(cost: float, configured: float) -> float:
    """A hold long enough that captures land inside it, not after it.

    The ring is drawn only while the ball is held, so a frame is worth having
    only if the capture *finished* before the release.  The hold that shipped
    was the configured 0.35s against a capture costing 0.52-1.21s raw, so the
    single capture the loop started always completed after the release: every
    Android throw fell back to a guessed target, on every phone, every time.
    Fit RING_SAMPLES worth of captures instead, and keep a ceiling so a phone
    that has gone slow holds the ball rather than the whole run.
    """
    wanted = cost * RING_SAMPLES + RING_HOLD_MARGIN
    return max(configured, min(RING_HOLD_CEILING, wanted))


def ring_is_readable(
    ring: excellent_throw_ios.RingLock, viewport: tuple[int, int]
) -> bool:
    """Whether a reading is somewhere a catch circle could actually be."""
    width, height = viewport
    low, high = RING_BAND
    left, right = RING_X_BAND
    return (
        height * low <= ring.center_y <= height * high
        and width * left <= ring.center_x <= width * right
    )


def proven_throw_points(
    ball: excellent_throw_ios.BallDetection, viewport: tuple[int, int]
) -> tuple[list[int], list[int]]:
    """The moto's hand-verified flick, from wherever this ball rests.

    For when the ring could not be read.  `fallback_target` is the other
    option and it is worse than nothing here: with no circle drawn on the
    frame it degrades to the most ring-shaped blob around, which live was the
    Pokemon's shadow, and aiming at a shadow pulls the flick short.  Throwing
    the length that is known to catch, straight, beats aiming at a guess.
    """
    width, height = viewport
    end_y = max(
        round(height * PROVEN_THROW_CEILING),
        round(ball.center_y - height * PROVEN_THROW_RISE),
    )
    return [ball.center_x, ball.center_y], [ball.center_x, end_y]


async def calibrate_target(
    device: AndroidDevice,
    ball: excellent_throw_ios.BallDetection,
    artifact_dir: Path,
) -> excellent_throw_ios.RingLock | None:
    """Read the catch-circle center from frames taken while the ball is held."""
    cost = await measure_capture_cost(device)
    seconds = hold_for_capture(cost, device.config.calibration_hold_seconds)
    print(
        f"[{device.label}] Capture costs {cost:.2f}s; holding the ball {seconds:.2f}s "
        "to read the ring"
    )
    hold_task = asyncio.create_task(
        device.stationary_hold([ball.center_x, ball.center_y], seconds)
    )
    await asyncio.sleep(0.06)
    frames: list[HeldFrame] = []
    late = 0
    while not hold_task.done():
        started = time.monotonic()
        try:
            image = await device.screenshot_raw()
        except AndroidExcellentThrowError:
            continue
        finished = time.monotonic()
        # `hold_task.done()` is checked before the capture, so the last capture
        # of the loop can still overrun the release.  A frame taken after the
        # ball is gone has no ring on it and only costs a search.
        if hold_task.done():
            late += 1
            continue
        frames.append(HeldFrame(started + (finished - started) / 2, image))
    await hold_task
    for index, frame in enumerate(frames):
        frame.decoded.save(artifact_dir / f"timed-hold-{index:02d}.jpg", "JPEG", quality=72)
    if not frames:
        print(
            f"[{device.label}] No capture finished inside a {seconds:.2f}s hold "
            f"({late} landed late); using center fallback"
        )
        return None
    view = analysis_view(device.viewport)
    try:
        measured = excellent_throw_ios.measure_ring(frames, None, view.viewport)
    except excellent_throw_ios.ExcellentThrowError:
        measured = None
    if measured is not None:
        measured = view.grow_lock(measured)
    if measured is not None and not ring_is_readable(measured, device.viewport):
        print(
            f"[{device.label}] Ring read at ({measured.center_x},{measured.center_y}) "
            "is not where a catch circle can be; ignoring it"
        )
        measured = None
    if measured is None:
        print(
            f"[{device.label}] {len(frames)} held frame(s) but no ring on them; "
            "using center fallback"
        )
        return None
    print(
        f"[{device.label}] One-hold target=({measured.center_x},{measured.center_y}) "
        f"ring={measured.current_radius}/{measured.maximum_radius} "
        f"from {len(frames)} held frame(s)"
    )
    return measured


def berry_in_hand(image: Image.Image, viewport: tuple[int, int]) -> bool:
    """True while a Nanab is the item held over the throw spot.

    The discriminator itself lives in `excellent_throw_ios` so the iOS half of
    the fleet reads a held berry the same way; all this adds is the downscaled
    viewport the Android search runs at.
    """
    return excellent_throw_ios.berry_in_hand(
        image, analysis_view(viewport).viewport, pink_fraction=BERRY_PINK_FRACTION
    )


def ball_in_hand(image: Image.Image, viewport: tuple[int, int]) -> bool:
    """True when what is held over the throw spot is a ball, not an item.

    The other half of `berry_in_hand`, and the one worth asking before a throw:
    the pink test knows only the Nanab, so a phone out of Nanabs holds the
    Golden Razz the game hands it next, reads that as a ball, and flicks the
    berry at the Pokemon.  The discriminator lives in `excellent_throw_ios`;
    all this adds is the downscaled viewport the Android search runs at.
    """
    return excellent_throw_ios.ball_in_hand(image, analysis_view(viewport).viewport)


def item_in_hand(image: Image.Image, viewport: tuple[int, int]) -> bool:
    """True only when something is held over the throw spot and it is not a ball.

    `ball_in_hand` says no to a frame it cannot read at all, which is not the
    same answer: a Pokemon standing over its own ball hides the disc, and razr's
    Fearow on 10 Sep 2026 was read as an item held in the hand every time, sent
    the throw off to the berry picker, and lost the throw when the picker did
    not open.  Nothing legible is no reason to put the ball down.
    """
    view = analysis_view(viewport).viewport
    if excellent_throw_ios.locate_throw_ball(image, view) is None:
        return False
    return not excellent_throw_ios.ball_in_hand(image, view)


async def locate_held_item(
    device: AndroidDevice,
    fallback: excellent_throw_ios.BallDetection | None = None,
) -> tuple[excellent_throw_ios.BallDetection | None, Image.Image | None]:
    """Where the held item sits on a frame taken now.

    `detect_encounter_ball` reads the ball as the encounter opens, and several
    seconds of berry picker go by before anything is thrown.  That reading is
    not where the item is by then: on a tall foldable the ball is still
    dropping in when the encounter is first legible, and it was measured at
    0.794h against the 0.911h it settles at.  A gesture is only a throw if it
    starts on the thing being thrown, so the position is re-read rather than
    remembered.

    `locate_throw_ball` matches a held berry as readily as a ball -- it is the
    disc it finds, and `ball_in_hand` is what tells the two apart -- so this
    serves the flick and the throw alike.
    """
    image, _ts = await capture_frame(device)
    detection = excellent_throw_ios.locate_throw_ball(
        image, analysis_view(device.viewport).viewport
    )
    if detection is None:
        return fallback, image
    return analysis_view(device.viewport).grow_ball(detection), image


async def feed_berry(
    device: AndroidDevice,
    ball: excellent_throw_ios.BallDetection | None,
    artifact_dir: Path,
) -> bool:
    """Feed the held berry to the Pokemon, and say whether it went.

    A tap on the berry.  Not a throw -- and this cost two days of runs to
    settle, because a throw is what a berry looks like it wants.  Selecting a
    Nanab puts it in the hand where the ball sits, so every version of this
    swiped it at the Pokemon the way the ball is swiped: from the berry, at
    the Pokemon's x, then straight up at the proven throw's own speed.  None
    of them fed anything.  The moto's bag read x9 before and x9 after three
    of them; the foldable's read x36 either side.

    Tapping fed it first try, 9 -> 8, with the Nanab's swirl drawn over the
    Swinub (10 Sep 2026).  `berry_android.feed_berry` had already found the
    same thing on the gym screen and written it down: a press picks the berry
    up, so a swipe drags it and puts it down again, feeding nothing and
    consuming nothing.  The encounter screen treats it the same way.

    The feed reads itself back, which the flick could not: a berry that is
    eaten leaves the hand and the game hands the ball back, so a ball on the
    next frame is the receipt.  A berry still in the hand means the tap missed
    and is worth one more.  Bounded at NANAB_FEED_TAPS, because if the game
    ever does re-select a berry instead, every extra tap is another Nanab.

    No BACK key anywhere here.  It does not put the berry away, it runs from
    the encounter: the moto ended up on the map screen and the run counted the
    abandoned Pokemon as caught.
    """
    for attempt in range(1, NANAB_FEED_TAPS + 1):
        held, held_image = await locate_held_item(device, ball)
        if held_image is not None:
            held_image.save(artifact_dir / "berry-in-hand.jpg", "JPEG", quality=72)
            if ball_in_hand(held_image, device.viewport):
                print(f"[{device.label}] Berry eaten; ball back in hand")
                return True
        point = (
            [held.center_x, held.center_y]
            if held is not None
            else [device.viewport[0] // 2, round(device.viewport[1] * 0.82)]
        )
        await device.tap(point)
        print(f"[{device.label}] Berry fed at {point} (tap {attempt})")
        await asyncio.sleep(NANAB_FEED_SETTLE_SECONDS)
    held_image, _ts = await capture_frame(device)
    if ball_in_hand(held_image, device.viewport):
        print(f"[{device.label}] Berry eaten; ball back in hand")
        return True
    print(f"[{device.label}] Still holding an item after {NANAB_FEED_TAPS} feed taps")
    return False


async def swap_to_ball(device: AndroidDevice, artifact_dir: Path) -> bool:
    """Put a ball back in the hand, and say whether it is there.

    Feeding a berry does not hand the ball back -- the game reselects a berry --
    so without this the throw that follows throws the berry, and a thrown berry
    never catches anything.  That was every no-catch attempt on both Androids.
    """
    for attempt in range(1, 3):
        image, _ts = await capture_frame(device)
        await device.tap(encounter_button(image, device, "switch"))
        await asyncio.sleep(BALL_SHEET_SECONDS)
        sheet, _ts = await capture_frame(device)
        await device.tap(ball_choice_point(sheet, device))
        await asyncio.sleep(BALL_SHEET_SECONDS)
        image, _ts = await capture_frame(device)
        image.save(artifact_dir / "ball-in-hand.jpg", "JPEG", quality=72)
        if ball_in_hand(image, device.viewport):
            print(f"[{device.label}] Ball back in hand")
            return True
        print(f"[{device.label}] Still holding an item after swap {attempt}")
    return False


def ball_choice_point(image: Image.Image, device: AndroidDevice) -> list[int]:
    """Where the Great Ball sits in the open held-item chooser.

    Anchored to the sheet's own top edge, because the sheet is anchored to the
    bottom of the screen and phones are not all the same shape: the same ball
    is at 0.747 of the android-one's height and 0.843 of the android-three's.
    """
    from . import berry_android

    rgba = image.convert("RGBA")
    top = berry_android.picker_sheet_top(rgba.width, rgba.height, 0, rgba.tobytes())
    if top is None:
        return _fraction_point(device, BALL_CHOICE_POINT)
    return [
        round(image.width * BALL_CHOICE_X),
        round(top + image.width * BALL_CHOICE_BELOW_SHEET),
    ]


def encounter_button(
    image: Image.Image, device: AndroidDevice, which: str
) -> list[int]:
    """Where the berry or held-item button is on this frame.

    Found rather than assumed: the android-three's corner buttons sit at 0.914 of its
    height where the android-one's are at 0.836, so the fixed fraction the picker used
    to tap landed on bare grass and the berry picker never opened.
    """
    buttons = excellent_throw_ios.locate_encounter_buttons(image)
    if buttons is not None:
        return buttons[0 if which == "berry" else 1]
    fallback = BERRY_BUTTON_POINT if which == "berry" else BALL_SWITCH_POINT
    return _fraction_point(device, fallback)


def _fraction_point(device: AndroidDevice, fraction: tuple[float, float]) -> list[int]:
    return [
        round(device.viewport[0] * fraction[0]),
        round(device.viewport[1] * fraction[1]),
    ]


async def use_nanab_berry(
    device: AndroidDevice,
    ball: excellent_throw_ios.BallDetection | None,
    encounter_image: Image.Image,
    artifact_dir: Path,
    berry: str = "nanab",
) -> None:
    """Select a berry by image, then feed it to the current encounter.

    `berry` is a picker kind -- "silver" for a legendary -- and falls back to
    a Nanab when the bag has none.
    """
    from . import berry_android

    def frame(image: Image.Image) -> tuple[int, int, int, bytes]:
        rgba = image.convert("RGBA")
        return rgba.width, rgba.height, 0, rgba.tobytes()

    if berry_in_hand(encounter_image, device.viewport):
        print(f"[{device.label}] A berry is already in hand; feeding it")
        if not await feed_berry(device, ball, artifact_dir):
            await swap_to_ball(device, artifact_dir)
        return

    berry = berry_android.berry_to_feed(device.label, berry)
    if berry_android.pocket_known_empty(device.label, berry):
        # The picker was read earlier in this run and had none.  Opening it
        # again buys the same answer at the same four seconds, every throw.
        if not ball_in_hand(encounter_image, device.viewport):
            await swap_to_ball(device, artifact_dir)
        return

    listed: list[tuple[str, list[int]]] | None = None
    picker_image: Image.Image | None = None
    picker_frame: tuple[int, int, int, bytes] | None = None
    sheet_seen = False
    for attempt in range(4):
        picker_image, _ts = await capture_frame(device)
        picker_frame = frame(picker_image)
        listed = berry_android.picker_read(picker_frame)
        if listed is not None:
            break
        if berry_android.picker_sheet_top(*picker_frame) is not None:
            sheet_seen = True
        elif attempt == 0:
            await device.tap(
                encounter_button(picker_image, device, "berry")
            )
        await asyncio.sleep(1.0)
    if listed is None or picker_image is None:
        if sheet_seen:
            await device.tap(
                [round(device.viewport[0] * 0.50), round(device.viewport[1] * 0.45)]
            )
            await asyncio.sleep(0.8)
            print(f"[{device.label}] A berry is already active; keeping it for the throw")
            return
        raise AndroidExcellentThrowError(
            f"Nanab picker did not open on {device.label}; no ball was thrown"
        )
    picker_image.save(artifact_dir / "nanab-picker.jpg", "JPEG", quality=72)
    chosen = berry_android.pick_from_picker(device.label, listed, berry)
    if chosen is None:
        # An empty Nanab pocket is not a reason to keep the ball, but it is no
        # reason to throw the Golden Razz the game leaves selected either.
        # Close the picker, make sure a ball is back in the hand, and throw it
        # unbuffed -- raising here left the sheet open over the encounter.
        names = ", ".join(kind for kind, _point in listed)
        print(
            f"[{device.label}] No Nanab berry in the bag "
            f"(picker: {names}); throwing without one, and not looking again"
        )
        await device.tap(
            [round(device.viewport[0] * 0.50), round(device.viewport[1] * 0.45)]
        )
        await asyncio.sleep(0.8)
        held, _ts = await capture_frame(device)
        if not ball_in_hand(held, device.viewport):
            await swap_to_ball(device, artifact_dir)
        return
    kind, point = chosen
    if kind != berry:
        print(f"[{device.label}] No {berry} berry in the bag; feeding a {kind} instead")
    await device.tap(point)
    # The picker sheet has to finish closing before the flick, or the gesture
    # lands on the sheet and the berry stays in the hand.
    await asyncio.sleep(1.2)

    # The feed hands the ball back itself.  `swap_to_ball` is for the feed
    # that did not take: without a ball in the hand the throw that follows
    # throws the berry, and a thrown berry never caught anything.
    if not await feed_berry(device, ball, artifact_dir):
        await swap_to_ball(device, artifact_dir)


def fallback_target(
    image: Image.Image,
    ball: excellent_throw_ios.BallDetection,
    device: AndroidDevice,
) -> excellent_throw_ios.RingLock:
    view = analysis_view(device.viewport)
    return view.grow_lock(
        excellent_throw_ios._detect_immediate_target(
            image, view.shrink_ball(ball), view.viewport, runtime_config(device.config)
        )
    )


def runtime_config(config: AndroidThrowConfig) -> excellent_throw_ios.RuntimeConfig:
    # Keep a minimal object matching the fields used by curve throw and helper scoring.
    return excellent_throw_ios.RuntimeConfig(
        appium_server_url="",
        ios_device={"straight_throw": False, "throw_immediately": True},
        wait_seconds=config.wait_seconds,
        calibration_hold_seconds=config.calibration_hold_seconds,
        pulse_hold_seconds=config.pulse_hold_seconds,
        target_ratio_min=config.target_ratio_min,
        target_ratio_max=config.target_ratio_max,
        max_pulses=config.max_pulses,
        max_ring_attempts=config.max_ring_attempts,
        throw_duration_ms=config.throw_duration_ms,
        end_offset_height=config.end_offset_height,
        curve_direction=config.curve_direction,
        curve_offset_width=config.curve_offset_width,
        spin_radius_width=config.spin_radius_width,
        spin_turns=config.spin_turns,
        spin_segment_ms=config.spin_segment_ms,
        pre_throw_pause_seconds=config.pre_throw_pause_seconds,
        initial_spin_pause_seconds=0,
        attack_wait_seconds=20,
        attack_quiet_threshold=0.01,
        attack_motion_threshold=0.01,
        attack_trigger_delay_seconds=0,
        center_tolerance_width=0.06,
        still_frames_required=4,
        result_capture_seconds=config.result_capture_seconds,
    )


async def wait_for_throw_result(device: AndroidDevice, artifact_dir: Path) -> bool:
    """Return True only when the encounter ball stays gone through resolution."""
    await asyncio.sleep(max(2.2, device.config.result_capture_seconds))
    # Bounded, because a caught Pokemon never brings the ball back and this
    # loop would otherwise sit out its whole deadline on every success.  Two
    # ball-free reads on the raw path cost about 2.5s even on the android-one.
    deadline = time.monotonic() + 6.0
    view = analysis_view(device.viewport)
    ready_frames = 0
    last_image: Image.Image | None = None
    while time.monotonic() < deadline:
        last_image, _ts = await capture_frame(device)
        if excellent_throw_ios.locate_throw_ball(last_image, view.viewport) is not None:
            ready_frames += 1
            if ready_frames >= 2:
                last_image.save(artifact_dir / "result-encounter-returned.jpg", "JPEG", quality=72)
                print(f"[{device.label}] Pokemon still in encounter; re-aiming")
                return False
        else:
            ready_frames = 0
        await asyncio.sleep(0.30)
    if last_image is not None:
        last_image.save(artifact_dir / "result-encounter-ended.jpg", "JPEG", quality=72)
    print(f"[{device.label}] Encounter ball stayed gone; catch/encounter completed")
    return True


# The two screens a catch leaves standing in front of the map.  Four reads is
# the XP card, the Pokemon's own page, and one to confirm the map came back.
CATCH_SCREEN_READS = 4
MENU_SETTLE = 1.2        # the card slides in; a tap during the slide misses
HANDSET_CONFIG_PATH = "/storage/self/primary/AutoTraderConfig.yaml"


async def handset_close_button(device: AndroidDevice) -> list[int] | None:
    """The X on the caught Pokemon's page, read from the handset's own config.

    It is the one button in this file that cannot be found on the frame: a bare
    glyph with nothing written on it, and its y is 0.945 of the android-three's height
    against 0.887 of the moto's, so a fraction would miss.  Read from the same
    AutoTraderConfig.yaml the GBL runner reads rather than a copy in the repo,
    which is only ever a staging copy of what is actually on the phone.
    """
    proc = await asyncio.create_subprocess_exec(
        ADB_PATH, "-s", device.serial, "shell", "cat", HANDSET_CONFIG_PATH,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        data, _ = await asyncio.wait_for(proc.communicate(), SCREENCAP_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    match = re.search(rb"^CLOSE_BTN:\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]", data, re.MULTILINE)
    if match is None:
        return None
    return [int(match.group(1)), int(match.group(2))]


async def clear_catch_screens(device: AndroidDevice, artifact_dir: Path) -> None:
    """Press through what a catch leaves up, so the next encounter can start.

    A caught Pokemon puts up the XP card and then its own page, and neither
    goes away on its own.  A run against a phone left standing on them never
    sees another encounter: the second android-three run spent all 25 of its attempts
    reporting "No ready encounter appeared" at the summary of the first run's
    catch, which had to be dismissed by hand.

    Pressed here rather than in the encounter wait, which must not learn to
    press OK -- it is written on dialogs that spend things too, and it is only
    safe for the routine that just threw the ball.
    """
    from . import gbl_vision

    close: list[int] | None = None
    for _read in range(CATCH_SCREEN_READS):
        image = await device.screenshot_raw()
        try:
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        except gbl_vision.VisionOCRError:
            return

        summary = gbl_vision.dismiss_point(boxes)
        if summary is not None:
            # The only ground truth for throw quality: the card itemises the
            # bonuses, so a run that earned none says "TOTAL 100 XP" and one
            # that landed an Excellent says so in its own line.  Saved before
            # the OK, because the card is the last frame that carries it.
            image.save(artifact_dir / "catch-summary.jpg", "JPEG", quality=72)
            print(f"[{device.label}] Catch summary, taking OK at {summary}")
            await device.tap(summary)
            await asyncio.sleep(MENU_SETTLE)
            continue

        blocked = gbl_vision.blocked_label(boxes)
        if blocked is not None and gbl_vision.normalize(blocked) == "power up":
            if close is None:
                close = await handset_close_button(device)
            if close is None:
                print(f"[{device.label}] Caught Pokemon page, but no CLOSE_BTN on the phone")
                return
            print(f"[{device.label}] Caught Pokemon page, closing it at {close}")
            await device.tap(close)
            await asyncio.sleep(MENU_SETTLE)
            continue
        return


async def run_once(
    device: AndroidDevice,
    *,
    wait_seconds: float | None,
    artifact_dir: Path,
    dry_run: bool,
    use_nanab: bool = False,
    ring_hold: bool = False,
    lone_ball_reading: bool = False,
    berry: str = "nanab",
) -> bool:
    ball = await detect_encounter_ball(
        device, wait_seconds, artifact_dir, lone_reading_ok=lone_ball_reading
    )
    if dry_run:
        print(f"[{device.label}] Dry-run: encounter detected, no touch sent")
        return False

    if (artifact_dir / "encounter.jpg").exists():
        image = Image.open(artifact_dir / "encounter.jpg").convert("RGB")
    else:
        image, _ = await capture_frame(device)
        image.save(artifact_dir / "encounter.jpg", "JPEG", quality=72)

    # What is in the hand, and where, both read now.  `image` is the frame the
    # encounter first became legible in, and on both Androids here the ball is
    # still dropping into that frame: the moto's mid-drop disc fails the pale
    # test and reads as an item, so a Nanab went in before every throw -- three
    # at one Meowth on 10 Sep 2026 -- and the foldable's reading sits 350px
    # above where the ball comes to rest, which is a swipe that grabs grass.
    ball, held_image = await locate_held_item(device, ball)
    if held_image is None:
        held_image = image

    # The berry goes in after the ball is located, not before: the feed needs
    # to know where the berry is, and a fed Nanab keeps the Pokemon still for
    # the throw that follows.  A berry left over from an earlier attempt is fed
    # too, otherwise the throw below would throw the berry, which is what the
    # android-one spent a whole run doing.
    if use_nanab or item_in_hand(held_image, device.viewport):
        await use_nanab_berry(device, ball, held_image, artifact_dir, berry=berry)
        ball = (await locate_held_item(device, ball))[0]

    config = device.config
    runtime = runtime_config(config)
    # The hold is off by default.  It costs 16s of a 30s catch on the android-one --
    # a capture-cost probe, a two-second hold and a ring search per held frame
    # -- and it cannot buy an Excellent here anyway: an Excellent needs the
    # ball released at the bottom of the ring's shrink, and `input swipe` opens
    # its own touch, so the hold can never become the throw.  `--ring-hold`
    # brings it back for tuning.
    target = await calibrate_target(device, ball, artifact_dir) if ring_hold else None

    # One `input swipe`, always.  The curve gesture is delivered as a dozen
    # separate `input` processes and arrives too slowly to be a throw at all;
    # a single swipe of the same length threw the ball first time on the android-one.
    # A curve bonus is worth nothing next to a ball that actually leaves the
    # hand.
    if target is None:
        start, end = proven_throw_points(ball, device.viewport)
    else:
        start, end = excellent_throw_ios.straight_throw_points(
            ball, target, device.viewport, runtime
        )
    print(f"[{device.label}] Throw {start}->{end} over {runtime.throw_duration_ms}ms")
    await device.straight_throw(start, end, runtime.throw_duration_ms)

    ended = await wait_for_throw_result(device, artifact_dir)
    if ended:
        await clear_catch_screens(device, artifact_dir)
    return ended


async def run_device(device: AndroidDevice, args: argparse.Namespace, config: AndroidThrowConfig) -> int:
    base = args.artifacts / device.label
    base.mkdir(parents=True, exist_ok=True)
    limit = args.throws
    sent = 0
    attempt = 0
    wait_seconds = None if limit is None else None if args.wait is None else args.wait

    # Tied to encounters, not to the attempt number: the android-three spent attempts 1
    # and 2 waiting for a spawn that never came, so `attempt == 1` meant its
    # only real encounter got no berry and the Pokemon was free to move.  A
    # re-aim at the same encounter does not need another -- the berry is still
    # in effect.
    fresh_encounter = True

    while limit is None or sent < limit:
        attempt += 1
        attempt_dir = base / f"attempt-{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            did_end = await run_once(
                device,
                wait_seconds=wait_seconds,
                artifact_dir=attempt_dir,
                dry_run=args.dry_run,
                use_nanab=fresh_encounter,
                ring_hold=args.ring_hold,
            )
            fresh_encounter = did_end
            if did_end:
                sent += 1
            if args.throws is not None and sent >= args.throws:
                return 0
        except AndroidExcellentThrowError as exc:
            print(f"[{device.label}] Encounter attempt {attempt} skipped: {exc}")
            await asyncio.sleep(0.8)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[{device.label}] Unexpected error: {exc}")
            return 1


async def run_all(args: argparse.Namespace, config: AndroidThrowConfig) -> int:
    devices = await prepare_devices(args, config)
    print(f"Found Android devices: {', '.join(device.label for device in devices)}")
    if args.throws is not None:
        print(f"Running for up to {args.throws} throw(s) per device")

    tasks = [asyncio.create_task(run_device(device, args, config)) for device in devices]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for device, result in zip(devices, results):
        if isinstance(result, int):
            continue
        if isinstance(result, BaseException):
            print(f"[{device.label}] Device run crashed: {result}")
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        if not args.screenshot:
            raise AndroidExcellentThrowError("--screenshot is required for --check")
        config = load_config(_load_yaml(args.config), args)
        device_candidates = asyncio.run(prepare_devices(args, config))
        device = device_candidates[0]
        image, _ = asyncio.run(capture_frame(device))
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        image.save(args.screenshot)
        print(f"Saved screenshot to {args.screenshot}")
        return 0

    raw_config = _load_yaml(args.config)
    config = load_config(raw_config, args)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run_all(args, config))


if __name__ == "__main__":
    raise SystemExit(main())
