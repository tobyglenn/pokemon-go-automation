#!/usr/bin/env python3
"""Plays Go Battle League sets in Pokemon GO on one Appium-controlled iPhone.

Start the phone on the GO BATTLE LEAGUE screen, the same place ``gbl.py``
expects on Android, and this walks the same loop:

    BATTLE -> CHOOSE YOUR LEAGUE -> bottom card -> USE THIS PARTY
    -> tap out the battle -> NEXT BATTLE -> CHOOSE YOUR LEAGUE -> ...

Every detector, threshold and screen-state rule is imported from ``gbl`` rather
than copied, exactly as ``berry_ios.py`` shares ``berry.py``.  Pokemon GO draws
the same artwork on both platforms, so the only thing that differs here is the
transport: screenshots arrive as PNGs from WebDriverAgent instead of a raw adb
framebuffer, and taps are logical Appium points instead of device pixels.  The
detectors return pixel coordinates in screenshot space, so every point they
hand back is converted with ``logical_point`` before it is tapped.

The one coordinate the loop cannot find for itself is the fast-attack point,
``GBL_MOVE_BTN``: the battlefield is a bare patch with no marking to search
for.  It is a logical point in ``gbl-ios.yaml``.

Android sends a batch of fast attacks down a single adb shell round trip.  The
equivalent here is one W3C pointer sequence carrying every tap in the batch, so
a batch still costs one request rather than ten -- but WebDriverAgent charges
per touch event, not per request, and the tap cadence below is set from what it
actually costs on this hardware rather than from the Android numbers.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import io
import os
import subprocess
from pathlib import Path
import sys
import time
from typing import Any, Iterator

import yaml
from PIL import Image

from . import config_paths, excellent_throw_ios, ios_wda_cleanup
from . import gbl_android as android_gbl
from . import gbl_home_recovery, gbl_meta, gbl_strategy, gbl_vision

try:
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.common.actions.action_builder import ActionBuilder
    from selenium.webdriver.common.actions.interaction import POINTER_TOUCH
    from selenium.webdriver.common.actions.pointer_input import PointerInput
except ModuleNotFoundError:  # reported at connect time with the Appium client
    ActionBuilder = None  # type: ignore[assignment]
    WebDriverException = Exception  # type: ignore[assignment,misc]


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = config_paths.default_config("gbl-ios.yaml")
ARTIFACT_ROOT = config_paths.state_dir() / "gbl"
DEFAULT_CHECK_SCREENSHOT = ARTIFACT_ROOT / "ios-gbl-check.png"
LOCK_DIR = Path("/tmp/pokemon-go-fleet")
REQUIRED_COORDINATES = {"GBL_MOVE_BTN"}
# Read from the GBL config when it names them, and never demanded: a phone that
# never pays a set out in a catch has no use for CLOSE_BTN, and refusing to
# start without it would strand the ordinary run over a screen it may not see.
# The two recovery points are optional for a stronger reason -- both are found
# in the screenshot on every handset tried, so the config is consulted only
# when detection comes back empty.  Keys absent from these two sets are dropped
# by load_config, which is why naming them here is what makes them readable.
OPTIONAL_COORDINATES = {
    "CLOSE_BTN",
    gbl_home_recovery.BALL_KEY,
    gbl_home_recovery.BATTLE_KEY,
}

# Fast attack batching: send 8 rapid taps in one WDA action to maintain
# consistent move queuing across 0.5s turn boundaries without artificial pauses,
# matching the SE and Android cadences.
MOVE_TAPS_PER_BATCH = 8
PROBE_EVERY_READS = 3

# How long to keep swiping the charged-move bubble field.  gbl_android sizes
# its sweeps for a spawn window of about 6.5-7.0s; this stops short of that so
# a slow phone cannot still be rastering when the field gives way to the next
# shield prompt, whose buttons sit inside the same band.
MINIGAME_SECONDS = 4.5

# How patient the start-up screen check is before it refuses to run.
START_LOOKS = 5
START_LOOK_GAP = 1.0

# Battle-screen geometry, measured on the iPhone rather than inherited.
# Android's fractions are tuned on a 19.5:9 panel and put the charged-move
# button at 0.83h; the Pro Max screenshot draws it lower, near the bottom of
# the panel.  Read and tapped at Android's numbers, the port was sampling
# grass -- reporting a lit move that was not lit -- and then tapping above the
# button, so the minigame never opened.  The button is also narrower here:
# about 0.05w across, against the 0.15w spread Android samples.
CHARGED_X = 0.50
# 0.925 was an eyeballed reading and sits within a hair of the disc's bottom
# edge; the traced set puts the lit disc at 0.855-0.9375h, so this is its
# measured centre.
CHARGED_Y = 0.896
# The post-battle tick's own grid, for gbl.teal_result_ready.  Unlike Android,
# where the tick sits a disc below the charged button, the two land on top of
# each other here, so these read almost like the charged numbers -- that is a
# coincidence of this panel and not a rule worth porting back.
TEAL_SAMPLE_X = (0.47, 0.50, 0.53)
TEAL_SAMPLE_Y = (0.925, 0.938, 0.951)
RESULT_TAP_X = 0.50
RESULT_TAP_Y = 0.938

# The Pro Max picker is vertically lower than the tall-layout device picker.  These were
# read from positioned Vision OCR on the real 1320x2868 screenshot: Search at
# y=758 (0.264), CANCEL/DONE at y=2645 (0.922).
IOS_PICKER_SEARCH_POINT = (0.50, 0.264)
IOS_PICKER_CLEAR_POINT = (0.92, 0.264)
IOS_PICKER_SEARCH_BACK_POINT = (0.08, 0.264)
IOS_PICKER_CANCEL_POINT = (0.28, 0.922)
IOS_PICKER_DONE_POINT = (0.73, 0.922)
IOS_PICKER_SCROLL = ((0.72, 0.82), (0.72, 0.25))

# Run-away confirmation on iPhone has a left-side cancel control; keep this
# conservative and biased to the left action in prompt layouts.
RUN_AWAY_CANCEL_X = 0.27
RUN_AWAY_CANCEL_Y = 0.80

# Pokemon GO reads one "mobile: dragFromToForDuration" as a click on whatever
# sits under the finger, so a one-shot drag never pages the roster: it re-reads
# page one, and if the finger lands on a tile it selects that Pokemon and drops
# the picker.  Pressing, pausing, moving in steps and pausing again gives the
# scroll view the deltas it needs -- the shape gift_ios proved on this same SE,
# where 12 of 12 stepped drags scrolled and the one-shot drag opened a lobby.
SCROLL_HOLD = 0.25
SCROLL_STEPS = 8

# Hunting a named league card: down through the list first, then back up, so a
# card above the fold is found too.  Past this the list does not hold it.
LEAGUE_SCROLL_LIMIT = 4

# Backing out of a set that was already running takes one close and one
# BATTLE.  Three rounds of that is enough for a slow screen; more than that
# is a phone that is not where this thinks it is.
LEAGUE_EXIT_LIMIT = 3

# A tap that changes nothing gets STALL_LIMIT tries, then a walk back to the
# GBL card.  Three of those rounds is enough for a screen that is merely slow;
# past that the button is dead and the run stops rather than spinning.
STALL_RECOVERY_LIMIT = 3

# Taps at the result point that leave the teal exactly where it was.  Past this
# the teal is scenery, not a checkmark, and the frame belongs to the bounded
# unrecognised-screen path instead.
TEAL_DISMISS_LIMIT = 4

# The exit door at the top left of a Go Battle League screen, measured on
# the SE's party screen at [78, 126] of 750x1334.  It is the way out of a
# set that is already running.  CLOSE_BTN is a panel button and lands
# inside the party card there, which is how three rounds of backing out
# moved nothing at all.
GBL_EXIT_DOOR = (0.104, 0.094)

# XCUITest's app states: 1 is "not running", anything above it is alive.
IOS_APP_STATE_NOT_RUNNING = 1

# How long Appium may spend getting WebDriverAgent up.  The preinstalled runner
# either answers at once or is not launchable at all, so it gets a short clock:
# waiting out the full two minutes before the rebuild retry is what a leg looks
# like when it seems to disconnect and reconnect for no reason.
WDA_LAUNCH_TIMEOUT_MS = 120000
PREINSTALLED_WDA_TIMEOUT_MS = 25000

# --- CHOOSE YOUR PARTY, for gbl.party_screen ---
# Measured here rather than inherited: this pill sits at 0.8936h against
# Android's 0.8434h, and the trainer photo above the panel reads 111-130 on this
# screen where Android reads 72-79, so Android's ceiling of 120 would reject
# roughly half of the iPhone's own party frames.  Across 52 traced menu frames
# nothing but the party screen lands inside this band; the nearest neighbour is
# 0.9085h.
PARTY_PILL_Y = 0.8936
PARTY_TOP_MAX = 140
# Rows across the charged-move disc, for gbl.charged_move_ready.  The disc is a
# fixed fraction of the screen *width* on both phones, so it is a taller slice
# of this shorter panel.  These were eyeballed at first and had the SE's bug in
# a milder form: measured off 149 traced Pro Max battle frames the lit disc
# spans 0.855-0.9375h with its centre at 0.8962, so the 0.950 row sat under the
# button and the old tuple matched 1 frame -- the phone played whole sets
# hitting nothing but fast attacks.  Centred on the measurement it matches 16,
# and still rejects the shield prompt and the charge cinematic above it.
CHARGED_DISC_ROWS = (0.875, 0.896, 0.917)

# --- SE 3rd gen, 16:9 --- #
# The rows above are Pro Max rows, and until now iOS had no per-handset branch
# at all, unlike gbl_android's compact_layout/moto_layout.  The old Pro Max tuple
# scored 0 of 493 traced SE battle frames: the lit disc measures 0.820-0.926h
# there, so its 0.950 row was below the button and disc_present could never be
# all-true.  The phone therefore played whole sets without seeing a ready move.
# Measured off the traced set: centre 0.872h, and these rows keep the two-move
# ICE BURN/FUSION FLARE layout while still rejecting the dimmed buttons that
# tighter rows read as lit.
SE_ASPECT_MAX = 1.90
SE_CHARGED_Y = 0.872
SE_CHARGED_DISC_ROWS = (0.845, 0.872, 0.899)


def se_layout(frame) -> bool:
    """Whether this is the shorter 16:9 SE battlefield rather than a Pro Max."""
    return frame[1] / frame[0] <= SE_ASPECT_MAX


def charged_disc_rows(frame):
    """The charged-move rows measured for whichever iPhone this is."""
    return SE_CHARGED_DISC_ROWS if se_layout(frame) else CHARGED_DISC_ROWS


def charged_centre_y(frame) -> float:
    """The charged-move button's centre row for whichever iPhone this is."""
    return SE_CHARGED_Y if se_layout(frame) else CHARGED_Y
SHIELD_TAP_X = 0.50
SHIELD_TAP_Y = 0.835
# --- "Attack incoming! Use a Protect Shield?" ---
# Measured on the iPhone: the prompt dims the lower half of the screen and puts
# a bright hexagon at 0.50w/0.835h with NOT NOW under it.  Blind-probing for it
# could never work -- the prompt is open for about two seconds and the probe
# went out every third read, so it mostly fired at empty battlefield.  Reads
# cost ~0.34s here against ~0.9s for a repositioned tap, so the cheap thing and
# the correct thing agree: look for it, then tap it.  The hexagon is a bright
# disc on a dark overlay, which is what the disc test already measures.
SHIELD_DISC_ROWS = (0.800, 0.825, 0.850)


def find_shield_point(frame) -> list[int] | None:
    """Return the centre of the visible Protect Shield hexagon.

    Checks standard rows first, then falls back to candidate disc search
    if visual effects, particles, or layout shifts altered row contrast.
    """
    shield = android_gbl.shield_point(frame, rows=SHIELD_DISC_ROWS, panel_y=0.72)
    if shield is not None:
        return shield
    if android_gbl.dark_overlay_row(frame, 0.72, threshold=140):
        return android_gbl.shield_candidate_point(frame)
    return None

# --- "Switch in a new Pokemon?" ---
# Also measured here rather than inherited: this sheet's top edge is at 0.755h,
# its title sits at 0.79h, and it offers two reserve cards centred at 0.337w and
# 0.663w, about 0.875h down.  The rows scanned are the
# clear band between the edge and the title.
SHEET_ABOVE_Y = 0.700
SHEET_SCAN_Y = (0.770, 0.790)
SHEET_CARD_Y = 0.875
SHEET_CARD_X = (0.337, 0.663, 0.50)
SWITCH_RESERVE_X = 0.90
SWITCH_RESERVE_Y = (0.605, 0.695)
SWITCH_ONLY_Y = 0.69
OPPONENT_CROP = (0.50, 0.02, 1.0, 0.32)

# GBL's top-left exit door, for the last-resort recovery in gbl.UNKNOWN_EXIT_AT.
# Measured here too: 0.075w/0.181h against Android's 0.104w/0.090h.
EXIT_DOOR_X = 0.075
EXIT_DOOR_Y = 0.181
# There was a second probe here, at 0.34w/0.88h, meant to pick a reserve during
# a forced switch.  It lands on the swap button instead, and opening the "Switch
# in a new Pokemon?" sheet hides the charged-move button and the fast-attack
# area for the rest of the battle -- the Android side had the same probe and its
# traces show whole battles spent tapping into that sheet.  Android now detects
# the sheet (gbl.switch_sheet); the heights it scans are measured on a 19.5:9
# panel and have not been measured here yet, so this side only stops opening it.


class IOSGBLError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    appium_server_url: str
    ios_device: dict[str, Any]
    coordinates: dict[str, list[int]]
    delay_modifier: float
    battles: int


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise IOSGBLError(f"Config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise IOSGBLError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IOSGBLError(f"Config root must be a YAML object: {path}")
    return value


def resolve_path(value: Any, base: Path, label: str) -> Path:
    try:
        return config_paths.find_config(value, base, label)
    except config_paths.ConfigPathError as exc:
        raise IOSGBLError(str(exc)) from exc


def validate_point(name: str, value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 2 or not all(type(v) is int for v in value):
        raise IOSGBLError(f"{name} must be [x, y] integer logical points")
    if value == [0, 0]:
        raise IOSGBLError(f"{name} is still the [0, 0] calibration placeholder")
    return value


def load_runtime_config(path: Path) -> RuntimeConfig:
    root = load_yaml(path)
    appium_path = resolve_path(root.get("appium_config"), path.resolve().parent, "appium_config")
    appium = load_yaml(appium_path)
    device = appium.get("device")
    if not isinstance(device, dict):
        raise IOSGBLError(f"{appium_path} needs a device object")
    for key in ("udid", "team_id", "wda_bundle_id"):
        if not isinstance(device.get(key), str) or not device[key].strip():
            raise IOSGBLError(f"{appium_path}: device.{key} must be set")

    raw_points = root.get("coordinates")
    if not isinstance(raw_points, dict):
        raise IOSGBLError("GBL config needs coordinates")
    points = {key: validate_point(key, raw_points.get(key)) for key in REQUIRED_COORDINATES}
    for key in OPTIONAL_COORDINATES:
        if key in raw_points:
            points[key] = validate_point(key, raw_points[key])

    battles = root.get("battles", android_gbl.BATTLES_PER_SET)
    if type(battles) is not int or battles < 1:
        raise IOSGBLError("battles must be a positive integer")
    delay_modifier = root.get("delay_modifier", 0)
    if not isinstance(delay_modifier, (int, float)):
        raise IOSGBLError("delay_modifier must be numeric")
    server_url = appium.get("server_url", "http://127.0.0.1:4723")
    if not isinstance(server_url, str):
        raise IOSGBLError("Appium server_url must be a string")
    return RuntimeConfig(
        appium_server_url=server_url.rstrip("/"),
        ios_device=device,
        coordinates=points,
        delay_modifier=float(delay_modifier),
        battles=battles,
    )


class IOSGBLDevice:
    def __init__(self, driver: Any, config: RuntimeConfig) -> None:
        self.driver = driver
        self.config = config
        self.viewport = driver.get_window_rect()
        self.label = str(config.ios_device.get("name", "iPhone"))

    def scale_point(self, point: list[int] | tuple[int, int], baseline: tuple[int, int] = (375, 667)) -> list[int]:
        return config_paths.scale_point_to_viewport(point, self.viewport, baseline)

    @classmethod
    def connect(cls, config: RuntimeConfig) -> "IOSGBLDevice":
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ModuleNotFoundError as exc:
            raise IOSGBLError("The Appium Python client is missing") from exc

        device = config.ios_device

        # Ask phone whether it is awake before asking Xcode to do anything.
        # A dark screen cannot run WebDriverAgent, and the failure it produces
        # names Xcode rather than the phone, four minutes later.
        asleep = ios_wda_cleanup.asleep_message(
            device["udid"], device.get("name", "The iPhone")
        )
        if asleep:
            raise IOSGBLError(asleep)

        # useNewWDA is False below, so Appium reattaches to whatever is already
        # listening.
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
            "appium:wdaLaunchTimeout": WDA_LAUNCH_TIMEOUT_MS,
        }
        if isinstance(device.get("platform_version"), str):
            capabilities["appium:platformVersion"] = device["platform_version"]
        if isinstance(device.get("wda_local_port"), int):
            capabilities["appium:wdaLocalPort"] = device["wda_local_port"]
        if isinstance(device.get("mjpeg_server_port"), int):
            capabilities["appium:mjpegServerPort"] = device["mjpeg_server_port"]
        if isinstance(device.get("derived_data_path"), str):
            capabilities["appium:derivedDataPath"] = config_paths.expand_user_path(
                device["derived_data_path"]
            )

        if device.get("use_preinstalled_wda"):
            # Launch the WebDriverAgent already on the phone instead of
            # having Appium rebuild it. On the worker host, a rebuild can hit
            # errSecInternalComponent when codesign runs over SSH.
            capabilities["appium:usePreinstalledWDA"] = True
            capabilities["appium:wdaLaunchTimeout"] = PREINSTALLED_WDA_TIMEOUT_MS

        def _start_session_once() -> Any:
            from . import pokemon_fleet

            pokemon_fleet.ensure_appium_server(config.appium_server_url)
            return cls._start_session(webdriver, XCUITestOptions, config, capabilities)

        def _start_session_via_devicectl(prefer_devicectl: str) -> Any:
            os.environ["APPIUM_XCUITEST_PREFER_DEVICECTL"] = prefer_devicectl
            if prefer_devicectl == "false":
                subprocess.run(["pkill", "-f", "appium"], check=False)
            return _start_session_once()

        def _connect_and_create() -> "IOSGBLDevice":
            try:
                driver = _start_session_via_devicectl(
                    os.environ.get("APPIUM_XCUITEST_PREFER_DEVICECTL", "true")
                )
            except Exception as exc:
                message = str(exc)
                if (
                    os.environ.get("APPIUM_XCUITEST_PREFER_DEVICECTL", "true") != "false"
                    and "RemoteXPC" in message
                    and "tunnel is not available for this session" in message
                ):
                    try:
                        driver = _start_session_via_devicectl("false")
                    except Exception as fallback_exc:
                        raise IOSGBLError(
                            f"Could not start Appium session for {device['udid']}: {fallback_exc}"
                        ) from fallback_exc
                else:
                    raise
            return cls(driver, config)

        try:
            return _connect_and_create()
        except Exception as exc:
            if ios_wda_cleanup.stale_wda_error(exc):
                print(
                    f"[{device.get('name', 'iPhone')}] WDA session lost UI authorization "
                    f"({exc}); restarting runner and retrying...",
                    flush=True,
                )
                ios_wda_cleanup.stop_wda_runner(device["udid"])
                time.sleep(2.0)
                try:
                    return _connect_and_create()
                except Exception as retry_exc:
                    raise IOSGBLError(
                        f"Could not start Appium session for {device['udid']} after restarting WDA: {retry_exc}"
                    ) from retry_exc
            raise IOSGBLError(
                f"Could not start Appium session for {device['udid']}: {exc}"
            ) from exc

    @staticmethod
    def _start_session(webdriver, options_class, config: RuntimeConfig, capabilities: dict):
        """The Appium session, retried once without the preinstalled runner.

        ``usePreinstalledWDA`` is the right capability on the worker host, where
        a rebuild cannot codesign over SSH. When Appium is running on a direct Mac
        and there is no RemoteXPC transport, it will fall back to building WDA
        when preinstalled WDA is unavailable.
        """

        name = config.ios_device.get("name", "iPhone")
        try:
            return webdriver.Remote(
                command_executor=config.appium_server_url,
                options=options_class().load_capabilities(capabilities),
            )
        except Exception as exc:
            if not capabilities.get("appium:usePreinstalledWDA", False):
                raise

            retry_capabilities = dict(capabilities)
            retry_capabilities["appium:wdaLaunchTimeout"] = WDA_LAUNCH_TIMEOUT_MS
            if "appium:usePreinstalledWDA" in retry_capabilities:
                del retry_capabilities["appium:usePreinstalledWDA"]
            print(
                f"[{name}] Preinstalled WebDriverAgent would not start ({exc});"
                " retrying build of its own",
                flush=True,
            )
            ios_wda_cleanup.stop_wda_runner(config.ios_device["udid"])
            time.sleep(1)
            return webdriver.Remote(
                command_executor=config.appium_server_url,
                options=options_class().load_capabilities(retry_capabilities),
            )

    def _restore_foreground(self) -> bool:

        """Put Pokemon GO back in front of a SpringBoard card. True if it acted.

        Callers ask for this only once a screen has already failed to read, and
        a game that is merely parked on the wrong Pokemon GO screen is not
        papered over by it: activating an app that is already frontmost changes
        nothing, and the caller reads the screen again and still refuses.
        A phone left on the home screen -- which is where a leg that stopped and
        took its WebDriverAgent runner down with it leaves one -- otherwise
        fails the start-up check on every following leg of the day.
        """
        bundle = self.config.ios_device.get("bundle_id", "com.nianticlabs.pokemongo")
        try:
            info = self.driver.execute_script("mobile: activeAppInfo")
        except Exception as exc:
            log(self, f"  Foreground check skipped ({exc})")
            return False
        front = info.get("bundleId") if isinstance(info, dict) else None
        try:
            state = self.driver.execute_script(
                "mobile: queryAppState", {"bundleId": bundle}
            )
            # Activating an app that is not running launches it, and a cold
            # start lands on the map with the game's own splash to sit through,
            # nowhere near the GO BATTLE LEAGUE screen this expects. A dead game
            # is left for a human.
            if state <= IOS_APP_STATE_NOT_RUNNING:
                log(self, f"  Pokemon GO is not running (state {state}); leaving it alone")
                return False
            if front == bundle:
                # The app switcher is a SpringBoard overlay: the game stays the
                # active app underneath it, so the front bundle cannot tell a
                # covered game from a parked one.  Asking for an app that is
                # already frontmost does nothing, and dismisses the switcher
                # when it is not -- which is where a killed leg leaves a phone,
                # and why the SE refused every leg of a day it could have run.
                log(self, "  Screen is unreadable; asking iOS for Pokemon GO again")
            else:
                log(self, f"  Foreground is {front}; bringing Pokemon GO back")
            self.driver.execute_script("mobile: activateApp", {"bundleId": bundle})
        except Exception as exc:
            log(self, f"  Foreground restore failed ({exc})")
            return False
        return True

    async def restore_foreground(self) -> bool:
        return await asyncio.to_thread(self._restore_foreground)

    def _screenshot(self) -> Image.Image:
        return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")

    async def screenshot(self) -> Image.Image | None:
        """One screenshot, or None.  Retried once: a dropped read is no verdict."""
        for _ in range(2):
            try:
                return await asyncio.to_thread(self._screenshot)
            except Exception as exc:
                log(self, f"Screen read failed ({exc}); retrying")
        return None

    def image_point(self, logical: list[int], image: Image.Image) -> list[int]:
        return [
            int(round(logical[0] * image.width / self.viewport["width"])),
            int(round(logical[1] * image.height / self.viewport["height"])),
        ]

    def logical_point(self, pixel: list[int] | tuple[int, int], image: Image.Image) -> list[int]:
        return [
            int(round(pixel[0] * self.viewport["width"] / image.width)),
            int(round(pixel[1] * self.viewport["height"] / image.height)),
        ]

    def fraction_point(self, x: float, y: float) -> list[int]:
        """A logical point from the screen fractions the detectors are written in."""
        return [int(self.viewport["width"] * x), int(self.viewport["height"] * y)]

    async def gesture(self, description: str, call, *args) -> None:
        """One touch command, where a refused command is not a reason to stop.

        Under the load of a battle WebDriverAgent sometimes answers a gesture
        with "the application under test is not running, possibly crashed" while
        the game is in fact still playing -- it has simply not answered the
        liveness check in time.  Letting that raise abandons the phone mid-fight,
        so gestures are reported and dropped instead.  A game that has genuinely
        gone stops the loop anyway, through the screen reads: an unreadable or
        motionless screen is already a stop condition.
        """
        try:
            await asyncio.to_thread(call, *args)
        except WebDriverException as exc:
            message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            log(self, f"  {description} refused by WebDriverAgent ({message}); carrying on")

    async def tap(self, point: list[int]) -> None:
        await self.gesture(
            f"Tap at {point}",
            self.driver.execute_script,
            "mobile: tap",
            {"x": point[0], "y": point[1]},
        )

    async def type_text(self, value: str) -> None:
        """Type into the currently focused Pokemon search field."""
        def send() -> None:
            # Pokemon GO's search field is custom-drawn and often has no
            # discoverable XCUIElement.  XCUITest's application-level keyboard
            # input targets the focused field without requiring an element id.
            self.driver.execute_script("mobile: keys", {"keys": list(value)})

        try:
            await asyncio.to_thread(send)
        except Exception as exc:
            raise IOSGBLError(f"Could not type picker search text: {exc}") from exc

    async def hide_keyboard(self) -> bool:
        try:
            await asyncio.to_thread(self.driver.hide_keyboard)
            return True
        except Exception as exc:
            # Callers verify whether the footer became visible, so a refused
            # hide is reported but never followed by an unguarded footer tap.
            message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            log(self, f"  Keyboard hide was refused ({message})")
            return False

    def _tap_burst(self, point: list[int], taps: int) -> None:
        if ActionBuilder is None:
            for _ in range(taps):
                self.driver.execute_script("mobile: tap", {"x": point[0], "y": point[1]})
            return
        actions = ActionBuilder(self.driver, mouse=PointerInput(POINTER_TOUCH, "finger"))
        # One move for the whole batch: repositioning is the expensive event.
        # Execute rapid touches to ensure continuous fast attack move registration.
        actions.pointer_action.move_to_location(point[0], point[1])
        for _ in range(taps):
            actions.pointer_action.pointer_down()
            actions.pointer_action.pointer_up()
        actions.perform()

    async def tap_burst(self, point: list[int], taps: int) -> None:
        """A whole batch of taps in one request, as adb sends one shell line."""
        await self.gesture(f"{taps} taps at {point}", self._tap_burst, point, taps)

    def _trace_path(self, points: list[list[int]], duration_ms: int) -> None:
        """Send one continuous multi-point touch path to WebDriverAgent."""
        if ActionBuilder is None:
            for start, end in zip(points[::8], points[8::8]):
                self.driver.execute_script(
                    "mobile: dragFromToForDuration",
                    {
                        "fromX": start[0], "fromY": start[1],
                        "toX": end[0], "toY": end[1],
                        "duration": max(duration_ms * 8 / 1000, 0.1),
                    },
                )
            return
        actions = ActionBuilder(
            self.driver,
            mouse=PointerInput(POINTER_TOUCH, "charged-minigame"),
            duration=duration_ms,
        )
        actions.pointer_action.move_to_location(points[0][0], points[0][1])
        actions.pointer_action.pointer_down()
        for point in points[1:]:
            actions.pointer_action.move_to_location(point[0], point[1])
        actions.pointer_action.pointer_up()
        actions.perform()

    async def trace_path(self, points: list[list[int]], duration_ms: int = 55) -> None:
        """Run a charged-minigame path as one low-latency WDA request."""
        await self.gesture(
            f"Charged minigame path ({len(points)} points)",
            self._trace_path,
            points,
            duration_ms,
        )

    async def drag(self, start: list[int], end: list[int], duration: float) -> None:
        await self.gesture(
            f"Drag {start} -> {end}",
            self.driver.execute_script,
            "mobile: dragFromToForDuration",
            {
                "duration": duration,
                "fromX": start[0],
                "fromY": start[1],
                "toX": end[0],
                "toY": end[1],
            },
        )

    def _scroll(self, start: list[int], end: list[int], duration: float) -> None:
        """One held, stepped scroll: press, pause, move in steps, pause, release."""
        step_ms = max(1, int(duration * 1000 / SCROLL_STEPS))
        actions = ActionBuilder(
            self.driver, mouse=PointerInput(POINTER_TOUCH, "finger"), duration=0
        )
        actions.pointer_action.move_to_location(start[0], start[1])
        actions.pointer_action.pointer_down()
        actions.pointer_action.pause(SCROLL_HOLD)
        for step in range(1, SCROLL_STEPS + 1):
            actions.pointer_action.move_to_location(
                int(start[0] + (end[0] - start[0]) * step / SCROLL_STEPS),
                int(start[1] + (end[1] - start[1]) * step / SCROLL_STEPS),
            )
            actions.pointer_action.pause(step_ms / 1000)
        actions.pointer_action.pause(SCROLL_HOLD)
        actions.pointer_action.pointer_up()
        actions.perform()

    async def scroll(self, start: list[int], end: list[int], duration: float = 0.5) -> None:
        """A scroll the game reads as a scroll rather than as a tap.

        Without the selenium action builder there is nothing to send a stepped
        gesture with, so the one-shot drag is all that is left; it is the wrong
        gesture, but a refused scroll is no better than a swallowed one.
        """
        if ActionBuilder is None:
            await self.drag(start, end, duration)
            return
        await self.gesture(f"Scroll {start} -> {end}", self._scroll, start, end, duration)

    async def quit(self) -> None:
        try:
            await asyncio.to_thread(self.driver.quit)
        except Exception:
            pass


TRACE_DIR: Path | None = None
_TRACE_SEQ = 0


def save_trace(image: Image.Image, state: str, point: list[int] | None) -> None:
    """Writes the frame a decision was made on, named for that decision.

    Read-only diagnosis aid, and the only way to check battle-screen geometry:
    the log says which state was picked, this says what the phone was actually
    showing when it was picked.
    """
    global _TRACE_SEQ

    assert TRACE_DIR is not None
    where = "none" if point is None else f"{point[0]}x{point[1]}"
    image.save(TRACE_DIR / f"{_TRACE_SEQ:03d}-{state}-{where}.png")
    _TRACE_SEQ += 1


def log(device: IOSGBLDevice, *parts: Any) -> None:
    print(f"[{device.label}]", *parts, flush=True)


async def wait(device: IOSGBLDevice, seconds: float, use_modifier: bool = True) -> None:
    delay = seconds + (device.config.delay_modifier if use_modifier else 0)
    await asyncio.sleep(max(delay, 0))


def raw_frame(image: Image.Image) -> tuple[int, int, int, bytes]:
    """The screenshot in the (width, height, offset, RGBA bytes) shape gbl.py reads."""
    rgba = image.convert("RGBA")
    return rgba.width, rgba.height, 0, rgba.tobytes()


async def smart_screen_state(
    image: Image.Image,
    profile: gbl_strategy.StrategyProfile,
    *,
    probe_unknown: bool = False,
    allow_row_scroll: bool = True,
) -> tuple[str, list[int] | None, str | None, list[gbl_vision.OCRBox]]:
    """Layer positioned OCR over the conservative color/shape detector.

    The iPhone 17 Pro Max reward panel is a known false ``league`` for the old
    detector.  OCR sees the actual lower BATTLE label, while the y guard in
    ``action_point`` excludes the BATTLE tab at the top of the same screen.
    """

    frame = raw_frame(image)
    state, point = android_gbl.read_screen_state(frame)
    boxes: list[gbl_vision.OCRBox] = []
    if state != "battle" or probe_unknown:
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        # An unclaimed reward outranks any action label on the same card. The
        # card a finished set leaves behind carries both, and its BATTLE is
        # spent -- greyed out until the tiles beside it are taken. The SE read
        # that dead pill as the way on and tapped it until the stall guard
        # stopped the run, four battles into the day, with the set's rewards
        # unclaimed and the next set behind them.
        reward = gbl_vision.reward_point(boxes, image.width, image.height)
        if reward is not None and gbl_vision.gbl_card_visible(boxes):
            return "orange", reward, None, boxes
        # OCR cannot always name that reward. The card's teal close X overlaps
        # the SE's SELECT label and only 'CT' survives it, so `reward_point`
        # finds no reward word and the spent BATTLE pill wins by default -- the
        # exact 0 battle day described above. The tile is still plainly there to
        # a colour scan, so ask for it before settling for an action label.
        if gbl_vision.gbl_card_visible(boxes):
            tile = android_gbl.find_reward_tile(frame)
            # ...but only if that tile is one this set has earned. The colour
            # scan cannot tell an earned tile from a locked one, and the row
            # regularly holds the earned tile off the left edge with two locked
            # ones in view: the SE took the locked "3 wins" tile, which opens
            # the rank roster, closed it, and took the same tile again every
            # five seconds until the day ran out.
            if tile is not None and not gbl_vision.locked_reward_tile(
                boxes, tile, image.width, image.height
            ):
                return "orange", tile, None, boxes
            # Locked tiles in view, no earned one found, and a tile cut off
            # by the left edge: the row is scrolled past what this set earned,
            # so pull it back rather than pressing anything. Without the cut-off
            # tile this is just a card whose rewards are all taken, and the
            # BATTLE pill below is the way on.
            row = gbl_vision.reward_row_point(boxes, image.width, image.height)
            if (
                row is not None
                and allow_row_scroll
                and android_gbl.clipped_reward_tile(frame)
            ):
                return "tiles", row, None, boxes
        action = gbl_vision.action_point(boxes, image.width, image.height)
        if action is not None:
            # Both reward tracks say BATTLE. Preserve the detector's upper
            # Basic/free pill instead of letting OCR's lower-most-label policy
            # select Premium.
            # The detector's point is only worth preferring when it really is
            # a pill.  `read_screen_state` falls back to the centre of a white
            # card when it finds none, and handing that back as a pill taps the
            # middle of a panel that is not a button: the SE pressed the text of
            # the GO Battle League welcome card 24 times over three recoveries,
            # ended its leg having played nothing, and the day was called done
            # with battles still owed to it.
            tier_pill = (
                android_gbl.find_pill(frame, android_gbl.REWARD_PILL_SCAN_Y)
                if android_gbl.reward_chooser_visible(frame)
                else None
            )
            if tier_pill is not None:
                return "pill", tier_pill, action[1], boxes
            return "pill", action[0], action[1], boxes
        if state == "pill" and point is not None and gbl_vision.gbl_card_visible(boxes):
            if gbl_vision.set_completed(boxes):
                return "unconfirmed", None, None, boxes
            return "pill", point, "battle", boxes
        # A screen with no reward and no action label is not answered here: the
        # run-away check and the league choice below both need these boxes, and
        # returning the shape classification early made them dead code.  The
        # same conservative fallback is the last line of this function.
    for box in boxes:
        if _is_runaway_prompt(box.text):
            return state, point, box.text, boxes
    min_y = int(image.height * 0.20)
    max_y = int(image.height * 0.90)
    # The reward-tier chooser lists the season's leagues as a blurb, so the
    # substring tests below match a sentence that is not a card and the tap
    # lands on prose.  The SE spent a day doing exactly that: eight dead taps
    # on "Great League, Scroll Cup: Great League Edition," while the shape
    # detector's own verdict -- scroll the tier button into view -- was thrown
    # away.  Naming the chooser first hands the screen back to that branch.
    if gbl_vision.reward_tier_prompt_visible(boxes):
        return state, point, None, boxes
    is_league_screen = state == "league" or any(
        gbl_vision.normalize(b.text) == "choose your league"
        or "league" in gbl_vision.normalize(b.text)
        or "cup" in gbl_vision.normalize(b.text)
        for b in boxes
    )
    if is_league_screen:
        if _wants_named_league(profile):
            # A configured league is the only card worth tapping.  The SE fits
            # two cards on screen, so the wanted one is regularly below the
            # fold, and the shape detector's fallback -- the bottom-most card --
            # then enters whichever league happens to be scrolled into view.
            # That is how a Master League day was spent in Great League.
            box = _named_league_box(
                boxes,
                profile.settings.preferred_league,
                min_y=min_y,
                max_y=max_y,
            )
            if box is not None:
                return "league", [box.center_x, box.center_y], box.text, boxes
            return "league_scroll", None, None, boxes
        choice = gbl_strategy.choose_easiest_league(
            boxes,
            profile.settings.preferred_league,
            profile.team,
            min_y=min_y,
            max_y=max_y,
        )
        if choice is not None:
            return "league", [choice.x, choice.y], choice.name, boxes
    return state, point, None, boxes


def _wants_named_league(profile: gbl_strategy.StrategyProfile) -> bool:
    """Whether the config names a league instead of asking for the easiest."""
    wanted = gbl_strategy.canonical_name(profile.settings.preferred_league)
    return bool(wanted) and wanted != "auto"


def _named_league_box(
    boxes: list[gbl_vision.OCRBox],
    preferred: str,
    *,
    min_y: int,
    max_y: int,
) -> gbl_vision.OCRBox | None:
    """The card for the configured league, with no difficulty scoring at all.

    ``gbl_strategy.league_score`` only recognises whole names, and Vision splits
    a card's label into "MASTER" and "LEAGUE" often enough that scoring alone
    would report the wanted league missing while it sits on screen.  The
    distinctive word is matched on its own for that reason; its box still sits
    inside the card, so its centre is a safe tap.

    The whole name is ranked above the word alone, and a card that says nothing
    else above one that says more, so a season showing both MASTER LEAGUE and
    MASTER LEAGUE CLASSIC takes the plain one.
    """
    wanted = gbl_strategy.canonical_name(preferred)
    keyword = " ".join(
        word for word in wanted.split() if word not in ("league", "cup")
    )
    coming_next_y = None
    for b in boxes:
        if "coming next" in gbl_strategy.canonical_name(str(getattr(b, "text", ""))):
            coming_next_y = int(getattr(b, "center_y", 0))
            break

    best: gbl_vision.OCRBox | None = None
    best_rank: tuple[int, int, float] | None = None
    for box in boxes:
        if not min_y <= box.center_y <= max_y:
            continue
        if coming_next_y is not None and box.center_y > coming_next_y:
            continue
        text = gbl_strategy.canonical_name(str(getattr(box, "text", "")))
        if text == wanted:
            match = 3
        elif gbl_strategy.league_matches(wanted, text):
            match = 2
        elif keyword and f" {keyword} " in f" {text} ":
            match = 1
        else:
            continue
        rank = (match, -len(text.split()), box.confidence)
        if best_rank is None or rank > best_rank:
            best, best_rank = box, rank
    return best


def _is_runaway_prompt(label: str | None) -> bool:
    if not label:
        return False
    canonical = gbl_strategy.canonical_name(label)
    lowered = canonical.lower()
    return (
        "run away" in lowered
        or "runaway" in lowered
        or "forfeit" in lowered
        or "surrender" in lowered
    )


def _run_away_cancel_point(
    image: Image.Image,
    device: IOSGBLDevice,
    menu_boxes: list[gbl_vision.OCRBox],
    fallback_point: list[int] | None,
) -> list[int]:
    for box in menu_boxes:
        label = gbl_strategy.canonical_name(box.text)
        if (
            label == "cancel"
            or "not now" in label
            or (label == "no" and box.confidence >= 0.4)
        ):
            return device.logical_point([box.center_x, box.center_y], image)

    if fallback_point is not None:
        return [max(0, min(fallback_point[0], int(device.viewport["width"] * RUN_AWAY_CANCEL_X))), fallback_point[1]]

    return [
        int(device.viewport["width"] * RUN_AWAY_CANCEL_X),
        int(device.viewport["height"] * RUN_AWAY_CANCEL_Y),
    ]


async def recognize_battle_context(image: Image.Image) -> tuple[str | None, bool]:
    """Read opponent and outgoing effectiveness cue in one OCR pass."""
    left = int(image.width * 0.40)
    top = int(image.height * OPPONENT_CROP[1])
    right = int(image.width * OPPONENT_CROP[2])
    bottom = int(image.height * 0.56)
    boxes = await asyncio.to_thread(
        gbl_vision.recognize,
        image,
        (left, top, right, bottom),
    )
    index = await asyncio.to_thread(gbl_meta.load_species_index)
    opponent = gbl_strategy.identify_pokemon(
        gbl_vision.lines(boxes), (meta.name for meta in index.values())
    )
    not_effective, _super_effective = android_gbl.battle_effectiveness(boxes)
    return opponent, not_effective


async def recognize_opponent(image: Image.Image) -> str | None:
    opponent, _not_effective = await recognize_battle_context(image)
    return opponent


async def recognize_player(image: Image.Image, names: tuple[str, ...]) -> str | None:
    crop = (0, int(image.height * 0.02), int(image.width * 0.50), int(image.height * 0.32))
    boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    return gbl_strategy.identify_pokemon(gbl_vision.lines(boxes), names)


async def confirm_switch(
    device: IOSGBLDevice,
    names: tuple[str, ...],
    expected: str,
    move_point: list[int] | None = None,
) -> str | None:
    """Watch the battle header until the incoming Pokemon appears.

    The swap animation runs for about two seconds, so reading the header a
    single POLL_GAP after the tap reports a failure that has not happened yet
    and the loop taps the same reserve again.  Returns the last name read so
    the caller can say what it saw.

    As on Android, the gap is spent attacking when the fast-move point is
    known rather than sleeping through it, so a swap no longer leaves the
    phone visibly idle for four seconds.
    """
    seen: str | None = None
    for _ in range(android_gbl.SWITCH_VERIFY_READS):
        if move_point is None:
            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
        else:
            await fast_attack(device, move_point)
        image = await device.screenshot()
        if image is None:
            continue
        seen = await recognize_player(image, names)
        if gbl_strategy.canonical_name(seen or "") == gbl_strategy.canonical_name(expected):
            return seen
    return seen


async def fast_attack(device: IOSGBLDevice, point: list[int]) -> None:
    await device.tap_burst(point, MOVE_TAPS_PER_BATCH)


async def charged_minigame(
    device: IOSGBLDevice,
    seconds: float = MINIGAME_SECONDS,
) -> None:
    """Cover the bubble field with repeated fresh-touch WDA gestures.

    Starts on the caller's verified tap rather than on a read SWIPE prompt.
    Polling for that prompt cost a screenshot plus an OCR per sample, which on
    both phones spent the whole 2.2s budget without ever catching the word --
    every launch logged a timeout -- so the first gesture only landed once the
    early field had already gone by.

    Coverage is bounded by the clock, not by a segment count, because iOS
    gesture cost differs far too much between handsets for a fixed raster to
    span the same window on both.
    """
    segments = android_gbl.minigame_segments(
        device.viewport["width"], device.viewport["height"]
    )
    chunk_size = max(1, len(segments) // 6)
    deadline = asyncio.get_event_loop().time() + seconds
    while True:
        for offset in range(0, len(segments), chunk_size):
            points: list[list[int]] = []
            for x1, y1, x2, y2 in segments[offset:offset + chunk_size]:
                start = [x1, y1]
                if not points or points[-1] != start:
                    points.append(start)
                points.append([x2, y2])
            await device.trace_path(points)
            await wait(device, 0.06, use_modifier=False)
            if asyncio.get_event_loop().time() >= deadline:
                return


async def read_charged_prompt(device: IOSGBLDevice) -> str | None:
    """Read GET READY/SWIPE off the battlefield, as gbl_android does."""
    image = await device.screenshot()
    if image is None:
        return None
    crop = (
        int(image.width * 0.08),
        int(image.height * 0.12),
        int(image.width * 0.92),
        int(image.height * 0.64),
    )
    try:
        boxes = await asyncio.to_thread(gbl_vision.recognize, image, crop)
    except gbl_vision.VisionOCRError:
        return None
    return android_gbl.charged_prompt_label(boxes)


async def launch_charged_move(
    device: IOSGBLDevice,
    attempts: int | None = None,
    target_point: list[int] | None = None,
) -> tuple[bool, int]:
    """Tap and screen-confirm a charged move, retrying dropped WDA taps.

    A button that has stopped reading as lit is not proof of a launch -- damage
    flashes and battle animation report success while the Pokemon stands idle,
    which is why the Android side latches on the game's own GET READY/SWIPE
    text instead.  This does the same, and polls for it: one read
    CHARGED_LAUNCH_CONFIRM after the tap is earlier than the prompt renders.
    """
    if attempts is None:
        attempts = android_gbl.CHARGED_LAUNCH_ATTEMPTS
    if target_point is None:
        # No frame to measure here, so the handset is told apart by its own
        # viewport shape rather than a screenshot's.
        viewport = device.viewport
        centre_y = (
            SE_CHARGED_Y
            if viewport["height"] / viewport["width"] <= SE_ASPECT_MAX
            else CHARGED_Y
        )
        target_point = device.fraction_point(android_gbl.CHARGED1_X, centre_y)
    for attempt in range(1, attempts + 1):
        await device.tap(target_point)
        for _ in range(android_gbl.CHARGED_LAUNCH_POLLS):
            await wait(device, android_gbl.CHARGED_LAUNCH_CONFIRM, use_modifier=False)
            if await read_charged_prompt(device) is not None:
                return True, attempt
    return False, attempts


async def scroll_reward_tiers(device: IOSGBLDevice, *, back: bool = False) -> None:
    """Scrolls the reward chooser toward its entry or completed-set button."""
    x = device.viewport["width"] // 2
    start, end = (0.46, 0.72) if back else (0.72, 0.52)
    await device.scroll(
        [x, int(device.viewport["height"] * start)],
        [x, int(device.viewport["height"] * end)],
        0.5,
    )


async def scroll_reward_row(device: IOSGBLDevice, point: list[int]) -> None:
    """Drags the end-of-set reward row right, back toward its first tile.

    Sideways, not down: the tiles a set pays out sit in one horizontal row and
    the earned one is left-most, so a row scrolled on is a row with nothing
    pressable in view. This is the gesture the fleet was rescued with by hand.
    """
    width = device.viewport["width"]
    await device.scroll(
        [int(width * 0.30), point[1]],
        [int(width * 0.92), point[1]],
        0.5,
    )


def _menu_point(device: IOSGBLDevice, image: Image.Image, frame) -> list[int]:
    """The map's pokeball, in logical points.

    The iPhone half of `android_gbl.recovery_menu_point`, and it matters more
    here: every iPhone in the fleet has a pokeball further down its screen than
    MAIN_MENU_POINT expects -- 0.921 on the SE, 0.935 on the Pro Max.
    """
    pixel = gbl_home_recovery.find_pokeball(image)
    if pixel is not None:
        return device.logical_point(pixel, image)
    configured = android_gbl.mapped_point(
        device.config.coordinates.get(gbl_home_recovery.BALL_KEY))
    if configured is not None:
        # Calibrated on the 375x667 baseline, like every other point in the iOS
        # configs, so it has to be scaled onto this phone's viewport first.
        return device.scale_point(configured)
    return device.fraction_point(*android_gbl.MAIN_MENU_POINT)


def _menu_battle_point(
    device: IOSGBLDevice,
    image: Image.Image,
    frame,
    boxes: list[gbl_vision.OCRBox],
) -> list[int]:
    """The main menu's BATTLE button, in logical points.

    Same order as `android_gbl.recovery_menu_battle_point`: the disc is located
    in the picture, then the BATTLE label by OCR, then whatever the handset has
    mapped, and the blind fraction only when nothing else answered.
    """
    pixel = gbl_home_recovery.find_menu_battle(image)
    if pixel is None:
        pixel = gbl_vision.menu_battle_point(boxes, frame[1])
    if pixel is not None:
        return device.logical_point(pixel, image)
    configured = android_gbl.mapped_point(
        device.config.coordinates.get(gbl_home_recovery.BATTLE_KEY))
    if configured is not None:
        return device.scale_point(configured)
    return device.fraction_point(*android_gbl.MAIN_MENU_BATTLE_POINT)


# The main menu and the GBL card both animate in, and how long they take is a
# property of the handset: the menu opens in well under a second on a 17 Pro Max
# and takes several on an SE.  Polled rather than slept through, so the fast
# phone is not made to wait and the slow one is not read mid-animation.
HOME_WALK_SETTLE = 12.0
HOME_WALK_POLL = 1.0


async def _settle_for(
    device: IOSGBLDevice,
    predicate,
    timeout: float = HOME_WALK_SETTLE,
) -> tuple[bool, Image.Image | None]:
    """Polls the phone until `predicate` holds, or until it has waited enough.

    The predicates below run OCR, which is a Swift helper and blocks, so they
    are handed to a thread rather than run on the event loop.
    """
    deadline = time.monotonic() + timeout
    image = await device.screenshot()
    while True:
        if image is not None and await asyncio.to_thread(predicate, image):
            return True, image
        if time.monotonic() >= deadline:
            return False, image
        await asyncio.sleep(HOME_WALK_POLL)
        image = await device.screenshot()


async def walk_home_to_gbl(device: IOSGBLDevice, image: Image.Image) -> bool:
    """Map -> pokeball -> main menu -> BATTLE -> GO BATTLE LEAGUE, verified.

    `gbl_home_recovery` walks exactly these two buttons between legs, finding
    each one in the picture and checking the screen it opened before pressing
    the next.  Its detectors are reused here so a leg that gets dropped on the
    map mid-run walks back the same way, instead of by the parity ladder below:
    that ladder chose its tap from the attempt number alone, so a walk that had
    already opened the menu pressed the pokeball again and closed it, and the SE
    bounced between those two taps for the rest of its day.

    Returns whether the GBL card is up at the end of it.
    """
    if not gbl_home_recovery.main_menu_open(image):
        ball = gbl_home_recovery.find_pokeball(image)
        if ball is not None:
            point = device.logical_point(ball, image)
            source = "found in the picture"
        else:
            mapped = android_gbl.mapped_point(
                device.config.coordinates.get(gbl_home_recovery.BALL_KEY))
            if mapped is None:
                log(device, "  Map recovery: no pokeball on screen and none mapped")
                return False
            point = device.scale_point(mapped)
            source = f"from {gbl_home_recovery.BALL_KEY}"
        log(device, f"  Map recovery: pokeball {source} at {point}")
        await device.tap(point)
        opened, image = await _settle_for(device, gbl_home_recovery.main_menu_open)
        if not opened or image is None:
            log(device, "  Map recovery: the pokeball did not open the main menu")
            return False
        log(device, "  Map recovery: main menu is up")

    battle = gbl_home_recovery.find_menu_battle(image)
    if battle is not None:
        point = device.logical_point(battle, image)
        source = "found in the picture"
    else:
        mapped = android_gbl.mapped_point(
            device.config.coordinates.get(gbl_home_recovery.BATTLE_KEY))
        if mapped is None:
            log(
                device,
                "  Map recovery: no BATTLE disc on screen and no "
                f"{gbl_home_recovery.BATTLE_KEY} mapped",
            )
            return False
        point = device.scale_point(mapped)
        source = f"from {gbl_home_recovery.BATTLE_KEY}"
    log(device, f"  Map recovery: menu BATTLE {source} at {point}")
    await device.tap(point)

    reached, _ = await _settle_for(device, gbl_home_recovery.gbl_card_reached)
    log(
        device,
        "  Map recovery: the GBL card is up"
        if reached
        else "  Map recovery: BATTLE did not lead to the GBL card",
    )
    return reached


async def recover_to_gbl(
    device: IOSGBLDevice,
    image: Image.Image,
    frame,
    boxes: list[gbl_vision.OCRBox],
    attempt: int,
) -> None:
    """One step back toward the GO BATTLE LEAGUE screen, or nothing.

    ``android_gbl.recover_to_gbl`` walks the same screens, but it taps through
    ``adb shell input`` in screenshot pixels.  Handed an iPhone it raises
    ``AttributeError: 'IOSGBLDevice' object has no attribute 'shell'`` and ends
    the run at the first screen off the battle flow.  The steps below are that
    routine's, sent through WebDriverAgent in logical points instead.
    """
    if android_gbl.picker_screen(boxes) or android_gbl.picker_body_screen(boxes):
        log(device, "  Party picker still open; cancelling out of it")
        await cancel_verified_picker(device)
        return

    if gbl_vision.gbl_card_visible(boxes):
        if attempt > 1:
            # Fractions of the screen, so `fraction_point` rather than a
            # screenshot-pixel conversion: this branch had never run, and it
            # called `logical_point` without the image it needs. The SE's day
            # ended twice on the TypeError, on 2026-09-09.
            shut = device.fraction_point(0.5, 0.94)
            log(device, f"  Stalled on GBL card; refreshing via close button at {shut}")
            await device.tap(shut)
            return
        log(device, "  Already on the GBL card; leaving it for the battle loop")
        return

    if gbl_vision.main_menu_visible(boxes):
        point = _menu_battle_point(device, image, frame, boxes)
        log(device, f"  Main menu is up; taking BATTLE back to GBL at {point}")
        await device.tap(point)
        return

    if android_gbl.teal_result_ready(frame):
        result_boxes = boxes
        if not result_boxes:
            try:
                result_boxes = await asyncio.to_thread(
                    gbl_vision.recognize, image
                )
            except gbl_vision.VisionOCRError:
                result_boxes = []
        if gbl_vision.gbl_card_visible(result_boxes):
            log(device, "  Teal disc here is the GBL card close button, not a checkmark; leaving it")
            return
        point = device.logical_point(android_gbl.result_tap_point(frame), image)
        log(device, f"  Closing the panel by its teal button at {point}")
        await device.tap(point)
        return

    # A screen that names itself is worth more than a counter.  The ladder
    # below picks its tap from `attempt` alone, which is only ever right by
    # luck: it cannot tell the map from the menu, so it presses the pokeball on
    # a menu that is already open and closes it again.  When either screen is
    # actually detected, walk them in order and check each one.
    if gbl_home_recovery.main_menu_open(image) or gbl_home_recovery.on_map(image):
        await walk_home_to_gbl(device, image)
        return

    recovery_step: int | None = None
    if attempt <= 2:
        recovery_step = (attempt - 1) % 2
    elif attempt >= android_gbl.BATTLE_STATIC_LIMIT:
        recovery_step = (attempt - android_gbl.BATTLE_STATIC_LIMIT) % 2
    elif attempt >= android_gbl.UNKNOWN_RECOVER_FROM:
        recovery_step = (attempt - android_gbl.UNKNOWN_RECOVER_FROM) % 2

    menu = _menu_point(device, image, frame)
    if recovery_step is not None:
        if recovery_step == 0:
            log(device, f"  Main-screen recovery: opening the pokeball at {menu}")
            await device.tap(menu)
        else:
            point = _menu_battle_point(device, image, frame, boxes)
            log(device, f"  Main-screen recovery: BATTLE via the menu flow at {point}")
            await device.tap(point)
        return

    close = device.config.coordinates.get("CLOSE_BTN")
    if close is not None and attempt <= 4:
        log(device, f"  Closing a panel by the mapped CLOSE_BTN at {close}")
        await device.tap(close)
        return

    log(device, f"  Opening the main menu at {menu} to find the way back")
    await device.tap(menu)


async def scroll_league_list(device: IOSGBLDevice, *, back: bool = False) -> None:
    """Moves the league list by roughly one card, to look for a named league."""
    x = device.viewport["width"] // 2
    start, end = (0.40, 0.72) if back else (0.72, 0.40)
    await device.scroll(
        [x, int(device.viewport["height"] * start)],
        [x, int(device.viewport["height"] * end)],
        0.5,
    )


async def ios_picker_snapshot(
    device: IOSGBLDevice,
) -> tuple[Image.Image, list[gbl_vision.OCRBox]]:
    image = await device.screenshot()
    if image is None:
        raise IOSGBLError("Could not read the iPhone party picker")
    boxes = await asyncio.to_thread(gbl_vision.recognize, image)
    if not android_gbl.picker_screen(boxes):
        raise IOSGBLError("iPhone party picker OCR guard did not match")
    return image, boxes


async def cancel_verified_picker(device: IOSGBLDevice) -> None:
    """Cancel only after OCR proves the iPhone picker is still visible."""
    for _attempt in range(3):
        image = await device.screenshot()
        if image is None:
            return
        try:
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        except gbl_vision.VisionOCRError:
            return
        if android_gbl.picker_screen(boxes):
            await device.tap(
                device.fraction_point(*IOS_PICKER_CANCEL_POINT)
            )
            await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)
            continue
        if android_gbl.picker_body_screen(boxes):
            hidden = await device.hide_keyboard()
            if not hidden:
                # Pokemon GO's custom field does not expose a dismissible XCUI
                # keyboard.  Its own verified search-back arrow closes both.
                await device.tap(device.fraction_point(*IOS_PICKER_SEARCH_BACK_POINT))
            await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)
            continue
        return


async def prepare_best_party(
    device: IOSGBLDevice,
    party_image: Image.Image,
    party_boxes: list[gbl_vision.OCRBox],
    profile: gbl_strategy.StrategyProfile,
) -> gbl_strategy.StrategyProfile | None:
    """OCR-scan, rank, select and re-verify the iPhone's 1500-CP party."""
    if not (profile.settings.auto_build_team and profile.settings.auto_select_team):
        return None

    configured = gbl_strategy.team_from_party_ocr(
        party_boxes, (member.name for member in profile.team.members)
    )
    if configured is not None and tuple(
        gbl_strategy.canonical_name(member.name) for member in configured.members
    ) == tuple(
        gbl_strategy.canonical_name(member.name) for member in profile.team.members
    ):
        log(device, "  Visible party already matches the ranked device profile")
        return profile

    await device.tap(device.fraction_point(*android_gbl.PARTY_SLOT_POINT))
    await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)
    picker_open = True
    try:
        initial_image, _initial_boxes = await ios_picker_snapshot(device)
        await device.tap(device.fraction_point(*IOS_PICKER_CLEAR_POINT))
        await wait(device, 0.3, use_modifier=False)
        await device.hide_keyboard()
        await wait(device, 0.5, use_modifier=False)
        # Search mode's Favorites overlay otherwise hides the owned roster.
        image = await device.screenshot()
        if image is None:
            raise IOSGBLError("Could not verify cleared iPhone picker")
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        if not android_gbl.picker_body_screen(boxes):
            raise IOSGBLError("iPhone picker body guard failed after clear")
        await device.tap(device.fraction_point(*IOS_PICKER_SEARCH_BACK_POINT))
        await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)

        league_name = _party_league_name(party_boxes) or profile.settings.preferred_league
        league_cap = gbl_meta.cap_for_league(league_name)
        index = await asyncio.to_thread(
            gbl_meta.load_meta_index,
            league_cap,
        )
        current = gbl_strategy.team_from_party_ocr(
            party_boxes, (meta.name for meta in index.values())
        )
        roster: list[gbl_meta.RosterPokemon] = []
        if current is not None:
            roster.extend(
                gbl_meta.RosterPokemon(member.name, member.cp)
                for member in current.members
            )

        previous: tuple[tuple[str, int], ...] | None = None
        seen_frame: tuple | None = None
        pages = max(1, profile.settings.roster_scan_pages)
        for page in range(pages):
            image, boxes = await ios_picker_snapshot(device)
            visible = gbl_meta.parse_roster_page(boxes, index)
            roster.extend(visible)
            summary = ", ".join(f"{item.name} {item.cp}" for item in visible)
            log(device, f"  Roster page {page + 1}: {summary or 'no ranked names'}")
            # A page that reads back pixel-identical to the one before it was
            # never scrolled, and the name fingerprint cannot say so on its own:
            # a page with no ranked names on it fingerprints empty whether the
            # roster moved or not, so eight identical reads would each log a
            # fresh page number.  Say it moved nothing and stop, rather than
            # sending six more gestures into an open picker where a swallowed
            # scroll lands as a tap on a tile.
            here = android_gbl.frame_signature(raw_frame(image))
            if here == seen_frame:
                log(device, "  Roster page did not move; ending the scan here")
                break
            seen_frame = here
            fingerprint = tuple(sorted((item.name, item.cp) for item in visible))
            if fingerprint and fingerprint == previous:
                break
            previous = fingerprint
            if page + 1 < pages:
                await device.scroll(
                    device.fraction_point(*IOS_PICKER_SCROLL[0]),
                    device.fraction_point(*IOS_PICKER_SCROLL[1]),
                    0.30,
                )
                await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)

        if len({(item.name, item.cp) for item in roster}) < 3:
            raise IOSGBLError("iPhone roster scan found fewer than three Pokemon")
        always_include = gbl_strategy.active_always_include(
            profile.settings, league_name, league_cap
        )
        ranked = await asyncio.to_thread(
            gbl_meta.recommend_team,
            roster,
            index,
            always_include=always_include,
        )
        names = " / ".join(member.name for member in ranked.team.members)
        log(device, f"  Ranked party {names} ({ranked.explanation})")
        await cancel_verified_picker(device)
        picker_open = False

        if current is not None and tuple(
            gbl_strategy.canonical_name(member.name) for member in current.members
        ) == tuple(
            gbl_strategy.canonical_name(member.name) for member in ranked.team.members
        ):
            return gbl_strategy.StrategyProfile(ranked.team, profile.settings)

        party = await device.screenshot()
        if party is None:
            raise IOSGBLError("Could not re-read iPhone party screen")
        party_boxes_now = await asyncio.to_thread(gbl_vision.recognize, party)
        frame = raw_frame(party)
        state, point = android_gbl.read_screen_state(frame)
        if state != "pill" or not (
            android_gbl.party_screen(frame, point)
            or android_gbl.party_screen_ocr(party_boxes_now)
        ):
            raise IOSGBLError("iPhone party guard failed before team selection")
        await device.tap(device.fraction_point(*android_gbl.PARTY_SLOT_POINT))
        await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)
        picker_open = True

        for position, member in enumerate(ranked.team.members, start=1):
            image = await device.screenshot()
            if image is None:
                raise IOSGBLError("Could not read iPhone picker during selection")
            boxes = await asyncio.to_thread(gbl_vision.recognize, image)
            if not android_gbl.picker_body_screen(boxes):
                raise IOSGBLError("iPhone picker body guard failed during selection")
            await device.tap(device.fraction_point(*IOS_PICKER_CLEAR_POINT))
            await wait(device, 0.3, use_modifier=False)
            await device.tap(device.fraction_point(*IOS_PICKER_SEARCH_POINT))
            await wait(device, 1.2, use_modifier=False)
            await device.type_text(member.name)
            await wait(device, 1.0, use_modifier=False)
            typed = await device.screenshot()
            if typed is None:
                raise IOSGBLError("Could not verify iPhone picker search")
            typed_boxes = await asyncio.to_thread(gbl_vision.recognize, typed)
            typed_text = gbl_strategy.canonical_name(
                " ".join(gbl_vision.lines(typed_boxes))
            )
            if gbl_strategy.canonical_name(member.name) not in typed_text:
                raise IOSGBLError(f"iPhone search text for {member.name} did not enter")
            choice = android_gbl.roster_choice(typed_boxes, index, member)
            if choice is None:
                visible = gbl_meta.parse_roster_page(typed_boxes, index)
                raise IOSGBLError(
                    f"iPhone search result for {member.name} CP {member.cp} was not "
                    f"verified; visible={[(item.name, item.cp) for item in visible]}"
                )
            log(device, f"  Party slot {position}: {choice.name} CP {choice.cp}")
            await device.tap(device.logical_point([choice.x, choice.y], typed))
            await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)

        image = await device.screenshot()
        if image is None:
            raise IOSGBLError("Could not read iPhone picker before DONE")
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
        if not android_gbl.picker_screen(boxes):
            if not android_gbl.picker_body_screen(boxes):
                raise IOSGBLError("iPhone picker guard failed before DONE")
            hidden = await device.hide_keyboard()
            if not hidden:
                await device.tap(device.fraction_point(*IOS_PICKER_SEARCH_BACK_POINT))
            await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)
            image, boxes = await ios_picker_snapshot(device)
        await device.tap(device.fraction_point(*IOS_PICKER_DONE_POINT))
        picker_open = False
        await wait(device, android_gbl.PICKER_SETTLE, use_modifier=False)

        verified_image = await device.screenshot()
        if verified_image is None:
            raise IOSGBLError("Could not verify iPhone selected party")
        verified_boxes = await asyncio.to_thread(gbl_vision.recognize, verified_image)
        verified_frame = raw_frame(verified_image)
        _state, action = android_gbl.read_screen_state(verified_frame)
        if not (
            android_gbl.party_screen(verified_frame, action)
            or android_gbl.party_screen_ocr(verified_boxes)
        ):
            raise IOSGBLError("iPhone selected-party screen guard failed")
        verified = gbl_strategy.team_from_party_ocr(
            verified_boxes, (member.name for member in ranked.team.members)
        )
        wanted = tuple(
            gbl_strategy.canonical_name(member.name) for member in ranked.team.members
        )
        got = (
            tuple(gbl_strategy.canonical_name(member.name) for member in verified.members)
            if verified is not None
            else ()
        )
        if got != wanted:
            raise IOSGBLError(
                f"iPhone selected party verification failed: {got or 'no OCR team'}"
            )
        return gbl_strategy.StrategyProfile(ranked.team, profile.settings)
    except (IOSGBLError, gbl_vision.VisionOCRError, OSError, ValueError) as exc:
        log(device, f"  Automatic party preparation stopped safely ({exc})")
        if picker_open:
            await cancel_verified_picker(device)
        return None


# The catch screen a finished set hands over.  The moto flicks from a fixed
# 0.81h because its ball is always drawn in the same place; here the ball is
# found on each frame instead.  It bobs -- 0.79h to 0.92h across four traced SE
# frames -- and the moto's 0.81h lands on the top edge of it on this phone,
# where a flick drags the map rather than throwing anything.
# The flick keeps the moto's length rather than its endpoint.  A throw's power
# is its speed, and the moto's caught a live set's Stunfisk by covering 0.46h --
# 0.81h to 0.35h -- in 260ms.  Starting lower here, as this ball sits at 0.895h,
# and still ending at 0.35h would be a harder throw than the one that worked;
# the same rise from wherever the ball is lands on the Pokemon, which stands at
# about 0.46h on both phones.
ENCOUNTER_THROW_RISE = 0.46
ENCOUNTER_THROW_CEILING = 0.10  # never flick off the top of the screen
ENCOUNTER_THROW_STEPS = 10
ENCOUNTER_THROW_MS = 26         # per step, so the moto's proven ~260ms flick
ENCOUNTER_THROWS = 4            # Great Balls worth spending on a reward mon
ENCOUNTER_READS = 24
# The reward encounter is already on screen when the excellent routine is asked
# for it, so this is only how long it will spend confirming the ball is really
# there before it hands back and lets the plain flick have the throw.
EXCELLENT_THROW_WAIT = 20.0
ENCOUNTER_SETTLE = 5.0          # the ball's wobble, then the XP card sliding in
# The caught Pokemon's own page closes on the X at the bottom centre.  This is
# the fraction of the SE's measured CLOSE_BTN, [187, 630] of 375x667, so a
# config that names the point wins and a config that does not still closes.
ENCOUNTER_CLOSE_X = 0.499
ENCOUNTER_CLOSE_Y = 0.944
# Leaving an encounter is not the same button as closing a caught Pokemon's
# page. ENCOUNTER_CLOSE_* is bottom centre, and on an encounter screen 0.944h is
# where the ball sits -- tapping it there throws rather than leaves. The flee
# control is the running figure in the top left. Measured off the SE's own
# encounter capture (750x1334): the figure's centre is 68,108.
ENCOUNTER_FLEE_X = 0.091
ENCOUNTER_FLEE_Y = 0.081
# How many times a reward encounter may be given up on before the routine stops
# paying for it. The budget below is four Great Balls; a Pokemon that has
# already refused four is not worth four more, and the main loop re-enters this
# routine from the top every time it sees the plate again, which resets `throws`
# and hands it a fresh budget. A live SE run spent 11 Great Balls that way in
# half an hour, re-offered the same CP 240 Marill forever, and played 2 battles
# out of 40. Past this many, it flees and never throws.
ABANDONED_ENCOUNTER_LIMIT = 1


async def throw_ball(device: IOSGBLDevice, image: Image.Image) -> bool:
    """Flick the encounter ball straight up the screen.  Says whether it went.

    One ``dragFromToForDuration`` is read as a tap by Pokemon GO -- that is how
    the trade lobby gets opened by accident -- so the flick is a stepped pointer
    path, the same shape the charged minigame sends.

    False means no ball was on this frame, which is ordinary: between throws it
    is off up the screen or gone into a caught Pokemon.  The caller looks again
    rather than flicking at where a ball used to be.
    """
    viewport = (device.viewport["width"], device.viewport["height"])
    ball = excellent_throw_ios.locate_throw_ball(image, viewport)
    if ball is None:
        return False
    if not excellent_throw_ios.ball_in_hand(image, viewport):
        # `locate_throw_ball` matches a held berry as happily as a ball -- it
        # scored 0.99 on one -- so without this the flick throws the berry, and
        # a thrown berry never catches anything.  Asked as "is this a ball"
        # rather than "is this a Nanab", which was blind to every other berry.
        # False sends the caller back for a fresh frame, where the ball will be.
        log(device, "  An item is in hand, not a ball; taking the ball back")
        await swap_to_ball(device, image)
        return False
    end_y = max(
        int(viewport[1] * ENCOUNTER_THROW_CEILING),
        ball.center_y - int(viewport[1] * ENCOUNTER_THROW_RISE),
    )
    rise = end_y - ball.center_y
    path = [
        [ball.center_x, ball.center_y + round(rise * step / ENCOUNTER_THROW_STEPS)]
        for step in range(ENCOUNTER_THROW_STEPS + 1)
    ]
    await device.trace_path(path, ENCOUNTER_THROW_MS)
    return True


async def swap_to_ball(device: IOSGBLDevice, image: Image.Image) -> None:
    """Put a ball back in the hand after a berry has been left in it.

    Two taps: the held-item chooser in the encounter screen's bottom right,
    then the Great Ball inside it.  Both are found on the frame rather than
    assumed -- the buttons' y is 0.836 of the android-one's height and 0.914 of the
    android-three's, and the chooser's contents are placed against the sheet's own top
    edge, which is anchored to the bottom of the screen.
    """
    buttons = excellent_throw_ios.locate_encounter_buttons(image)
    if buttons is None:
        return
    scale_x = (device.viewport["width"]) / image.width
    scale_y = (device.viewport["height"]) / image.height
    await device.tap(
        [round(buttons[1][0] * scale_x), round(buttons[1][1] * scale_y)]
    )
    await wait(device, android_gbl.MENU_SETTLE)
    sheet = await device.screenshot()
    if sheet is None:
        return
    rgba = sheet.convert("RGBA")
    top = gbl_vision_sheet_top(rgba)
    if top is None:
        return
    await device.tap(
        [
            round(sheet.width * excellent_throw_ios.BALL_CHOICE_X * scale_x),
            round(
                (top + sheet.width * excellent_throw_ios.BALL_CHOICE_BELOW_SHEET)
                * scale_y
            ),
        ]
    )
    await wait(device, android_gbl.MENU_SETTLE)


def gbl_vision_sheet_top(rgba: Image.Image) -> int | None:
    from . import berry_android

    return berry_android.picker_sheet_top(
        rgba.width, rgba.height, 0, rgba.tobytes()
    )


def encounter_close_point(device: IOSGBLDevice) -> list[int]:
    close = device.config.coordinates.get("CLOSE_BTN")
    if close is not None:
        return device.scale_point(close)
    return device.fraction_point(ENCOUNTER_CLOSE_X, ENCOUNTER_CLOSE_Y)


def encounter_flee_point(device: IOSGBLDevice) -> list[int]:
    """Where to press to walk away from a wild encounter.

    Overridable per phone like the close button: the icon is anchored under the
    status bar, so its fraction travels better than most, but a notched phone
    lays that strip out differently and y never transfers on trust.
    """
    flee = device.config.coordinates.get("FLEE_BTN")
    if flee is not None:
        return device.scale_point(flee)
    return device.fraction_point(ENCOUNTER_FLEE_X, ENCOUNTER_FLEE_Y)


def throw_profile_path(device: IOSGBLDevice) -> Path | None:
    """This phone's excellent-throw profile, or None when it has none.

    ``excellent_throw_ios.select_configs`` picks by what is plugged in, which is
    the wrong question here: the fleet runs both iPhones at once and each GBL
    run must throw with its own phone's curve.  The UDID this session is already
    driving is the answer instead, matched against the same profiles.
    """
    udid = device.config.ios_device.get("udid")
    if not isinstance(udid, str):
        return None
    matched = [
        path
        for path in excellent_throw_ios.known_configs()
        if excellent_throw_ios.config_udid(path) == udid
    ]
    if not matched:
        return None
    # select_configs' own tie-break: a profile named for one phone beats the
    # generic one when both resolve to the same UDID.  The SE has both.
    specific = [path for path in matched if "-main-" in path.name or "-secondary-" in path.name]
    return specific[0] if specific else matched[0]


class ExcellentThrower:
    """The excellent-throw routine, driven over this run's own WDA session.

    ``excellent_throw.py`` cannot simply be shelled out to from inside a GBL
    run: it connects an Appium session of its own and takes the fleet lock on
    the phone, and the run already holds both, so the subprocess would refuse
    with "already controlled by another automation".  Its device wrapper needs
    nothing but a live driver, so the throw code runs against ours -- and the
    MJPEG stream it measures the shrinking circle on is the one WebDriverAgent
    is already publishing for this session.

    The wrapped driver is *shared*, so ``close`` stops the stream and leaves the
    session alone.  Quitting it here would end the GBL run mid-set.
    """

    def __init__(self, device: IOSGBLDevice, config: Any) -> None:
        self.device = device
        self.thrower = excellent_throw_ios.IOSExcellentThrowDevice(device.driver, config)
        self.stream: excellent_throw_ios.MJPEGStream | None = None
        self.artifacts = ARTIFACT_ROOT / "reward-catch" / time.strftime("%Y%m%d-%H%M%S")
        self.attempts = 0

    def start(self) -> None:
        port = self.thrower.config.ios_device.get("mjpeg_server_port")
        if type(port) is not int:
            raise excellent_throw_ios.ExcellentThrowError(
                "device.mjpeg_server_port must be configured to lock the catch circle"
            )
        stream = excellent_throw_ios.MJPEGStream(port)
        stream.start()
        self.stream = stream

    async def throw(self) -> bool:
        if self.stream is None:
            raise excellent_throw_ios.ExcellentThrowError("The MJPEG stream is not running")
        self.attempts += 1
        directory = self.artifacts / f"throw-{self.attempts:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        # A berry every throw, not once an encounter: a Nanab's calm lasts
        # only until the Pokemon breaks out of a ball, so the throw after a
        # break-out is aimed at a Pokemon that is moving again unless it is fed
        # a second one.  Left off until 2 Sep 2026, which meant the reward
        # catch was the one throw in the fleet that never used a berry.
        return await excellent_throw_ios.run_once(
            self.thrower,
            self.stream,
            wait_seconds=EXCELLENT_THROW_WAIT,
            artifact_dir=directory,
            dry_run=False,
            use_nanab=True,
        )

    def close(self) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream = None


def open_excellent_thrower(device: IOSGBLDevice) -> ExcellentThrower | None:
    """Bring up the excellent-throw routine for this phone, or say why not.

    None is not a failure: it means this catch is thrown with the plain flick
    below, which is what every run did before.  A reward Pokemon is never worth
    stopping a set over, so every way this can go wrong is reported and dropped.
    """
    path = throw_profile_path(device)
    if path is None:
        log(device, "  No excellent-throw profile for this phone; flicking instead")
        return None
    thrower: ExcellentThrower | None = None
    try:
        config = excellent_throw_ios.load_runtime_config(path)
        # Reading the window rect off the shared driver is the first live call,
        # so WebDriverException belongs here with the rest: a phone that will
        # not answer should throw the plain flick, not stop the set.
        thrower = ExcellentThrower(device, config)
        thrower.start()
    except (
        excellent_throw_ios.ExcellentThrowError,
        config_paths.ConfigPathError,
        WebDriverException,
        OSError,
        ValueError,
    ) as exc:
        if thrower is not None:
            thrower.close()
        log(device, f"  Excellent throw unavailable ({exc}); flicking instead")
        return None
    log(device, f"  Throwing with {path.name}")
    return thrower


async def send_throw(
    device: IOSGBLDevice, thrower: ExcellentThrower | None, image: Image.Image
) -> bool:
    """One ball at the reward Pokemon: the excellent routine, else the flick.

    Says whether a ball went, which the caller counts against its budget.  The
    fallback matters more than it looks: the flick catches nothing reliably, but
    it does end the encounter, and an encounter that never ends strands every
    remaining set behind it.
    """
    if thrower is not None:
        try:
            return await thrower.throw()
        except excellent_throw_ios.ExcellentThrowError as exc:
            log(device, f"  Excellent throw refused ({exc}); flicking instead")
        except (OSError, ValueError, WebDriverException) as exc:
            log(device, f"  Excellent throw failed ({exc}); flicking instead")
        # Whatever it refused on is still up, and it is usually a bottom sheet
        # sitting over the ball.  Flicking at a ball found on the frame from
        # before the refusal throws into the sheet and drops the encounter to
        # the map, which is how the reward loop used to strand itself.
        fresh = await device.screenshot()
        if fresh is not None:
            image = fresh
    return await throw_ball(device, image)


async def take_reward_encounter(
    device: IOSGBLDevice, spend_balls: bool = True
) -> bool:
    """Catch what a finished set pays out, and get back to the card behind it.

    The Android routine of the same name, on this transport.  Returns whether
    the GBL card came back.  Bounded rather than persistent: a reward Pokemon
    that will not stay in the ball is worth four Great Balls, not a morning, and
    whatever is left over is handed to the ordinary recovery.

    Three screens follow a catch and all three are answered here rather than by
    the main loop, which must not learn to press either of the last two: OK is
    written on dialogs that spend things, and the caught Pokemon's own page
    carries POWER UP in the pill slot -- already a blocked label, and it stays
    blocked.  Pressing them is safe only for the routine that just threw a ball.

    The ball itself is thrown by ``excellent_throw.py``'s routine rather than by
    the flick: it locks the shrinking circle before it releases, so a reward
    Pokemon usually costs one Great Ball instead of the four budgeted here.
    """
    throws = 0
    thrower: ExcellentThrower | None = None
    try:
        for _read in range(ENCOUNTER_READS):
            image = await device.screenshot()
            if image is None:
                continue
            try:
                boxes = await asyncio.to_thread(gbl_vision.recognize, image)
            except gbl_vision.VisionOCRError:
                return False

            if gbl_vision.gbl_card_visible(boxes):
                log(device, f"  Back on the GBL card after {throws} throw(s)")
                return True

            plate = gbl_vision.encounter_plate(boxes, image.height)
            if plate is not None:
                if throws >= ENCOUNTER_THROWS or not spend_balls:
                    # Returning here is what trapped the SE: the caller sees the
                    # same plate on its next read and calls back in with a fresh
                    # budget. Walk away instead, and let the loop below confirm
                    # the card came back.
                    reason = (
                        "will not stay in the ball"
                        if spend_balls
                        else "was already given up on"
                    )
                    log(device, f"  {plate.text.strip()} {reason}; fleeing")
                    flee = encounter_flee_point(device)
                    await device.tap(flee)
                    await wait(device, android_gbl.MENU_SETTLE)
                    continue
                # Opened on the first plate, not up front: most sets pay out in
                # items, and those runs should not start an MJPEG stream.
                if thrower is None:
                    thrower = open_excellent_thrower(device)
                log(device, f"  Set reward is a catch: {plate.text.strip()}, throw {throws + 1}")
                if not await send_throw(device, thrower, image):
                    await wait(device, android_gbl.POLL_GAP, use_modifier=False)
                    continue
                throws += 1
                await wait(device, ENCOUNTER_SETTLE, use_modifier=False)
                continue

            summary = gbl_vision.dismiss_point(boxes)
            if summary is not None:
                summary = device.logical_point(summary, image)
                log(device, f"  Catch summary, taking OK at {summary}")
                await device.tap(summary)
                await wait(device, android_gbl.MENU_SETTLE)
                continue

            blocked = gbl_vision.blocked_label(boxes)
            if blocked is not None and gbl_vision.normalize(blocked) == "power up":
                close = encounter_close_point(device)
                log(device, f"  Caught Pokemon page, closing it at {close}")
                await device.tap(close)
                await wait(device, android_gbl.MENU_SETTLE)
                continue

            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
        return False
    finally:
        if thrower is not None:
            thrower.close()


async def start_on_encounter(device: IOSGBLDevice, image: Image.Image) -> bool:
    """Clear a reward catch the phone was already parked on when we connected.

    Says whether it did anything, so the caller knows to look at the screen
    again.  The startup check cannot name this screen -- it has no pill, no
    cards and no reward button -- and would otherwise refuse to start at all,
    which strands every remaining set behind one uncaught Pokemon.
    """
    try:
        boxes = await asyncio.to_thread(gbl_vision.recognize, image)
    except gbl_vision.VisionOCRError:
        return False
    if gbl_vision.encounter_plate(boxes, image.height) is None:
        return False
    log(device, "  Parked on a set's reward catch; taking it before starting")
    await take_reward_encounter(device)
    return True


def _welcome_card_visible(boxes: list[gbl_vision.OCRBox]) -> bool:
    """Whether the GO Battle League welcome card is covering the screen.

    The card greets a first visit of the day with `Welcome to the GO Battle
    League!` over a LET'S GO! pill.  It names a league, so `_party_league_name`
    reads `battle league` off it and the open-set check treats the card as a
    set running in the wrong league -- the Pro Max spent a leg tapping the exit
    door three times and stopped having played nothing.  The words arrive as
    separate boxes, so they are matched against the joined reading.
    """
    joined = " ".join(gbl_vision.normalize(box.text) for box in boxes)
    return "welcome to the go battle" in joined


def _party_league_name(boxes: list[gbl_vision.OCRBox]) -> str | None:
    """The league a CHOOSE YOUR PARTY screen says its set belongs to.

    One definition, shared with the Android loop, which needs the same reading
    for the same reason. Looked up on call rather than bound here: this module
    is imported *during* `gbl_android`'s own import.
    """
    return android_gbl.party_league_name(boxes)


async def leave_to_league_list(device: IOSGBLDevice) -> None:
    """Leaves the set that is already open so the league list comes back.

    A leg that starts while the phone is still inside a league never meets the
    league list, so the configured league is never chosen and the phone plays
    out whichever set happened to be open -- a Great League set ran for an hour
    that way while Master League was configured.
    """
    door = device.fraction_point(*GBL_EXIT_DOOR)
    log(device, f"  Leaving the open set by the exit door at {door}")
    await device.tap(door)


async def play_device(
    device: IOSGBLDevice,
    count: int,
    profile: gbl_strategy.StrategyProfile | None = None,
) -> int:
    """Plays up to `count` battles on this iPhone.  Returns how many it finished.

    The loop is the one in ``gbl.play_device``: read the screen, decide which of
    the four states it is in, act.  `armed` is the same safety catch -- fast
    attacks only go out once a pill has been tapped *after* a league card was
    selected, so a phone that has fallen out to the map stops instead of tapping
    at the map.
    """
    profile = profile or gbl_strategy.load_strategy_profile(device_name="ios-one")
    move = device.scale_point(device.config.coordinates["GBL_MOVE_BTN"])
    # A battle written off partway through is not a battle played.  Counting
    # the two together is what let a leg report a full day it never had: every
    # interruption moved `played` on, and the run finished by arithmetic.  The
    # tally below is only for the report -- `played` still ends the set, since
    # an interrupted battle does spend an entry.
    played = 0
    interrupted = 0
    phantom = 0
    home_seen = 0
    armed = False
    attacking = False
    deadline: float | None = None
    last_tap: tuple[str, int, int] | None = None
    repeats = 0
    stalls = 0
    saw_league = False
    reward_scrolls = 0
    league_attempts = 0
    league_exits = 0
    # A leg that never sees the league list never chooses one.
    league_confirmed = False
    wanted_league = (
        profile.settings.preferred_league if _wants_named_league(profile) else None
    )
    battle_reads = 0
    unknown = 0
    abandoned = 0
    teal_reads = 0
    saw_result = False
    static = 0
    switch_taps = 0
    signature: tuple | None = None
    memory = gbl_strategy.BattleMemory()
    sheet_seen = False
    pending_reads = 0
    shields_used = 0
    ocr_failed = False
    party_preparation_attempted = False
    matchmaking_failures = 0
    last_matchmaking_status: str | None = None
    # Set by the matchmaking read that says combat is one frame away, spent by
    # the next screen that is not a battlefield.  See the guard below.
    battle_imminent = False
    pending_outcome: str | None = None
    # Set when the reward row has had its scrolls and stayed put.
    row_scroll_spent = False

    while played < count:
        image = await device.screenshot()
        if image is None:
            log(device, "  Screen unreadable twice; stopping")
            break
        frame = raw_frame(image)
        try:
            state, pixel_point, vision_label, menu_boxes = await smart_screen_state(
                image,
                profile,
                probe_unknown=not armed,
                allow_row_scroll=not row_scroll_spent,
            )
        except gbl_vision.VisionOCRError as exc:
            if not ocr_failed:
                log(device, f"  Vision OCR unavailable ({exc}); using shape detectors")
                ocr_failed = True
            state, pixel_point = android_gbl.read_screen_state(frame)
            vision_label = None
            menu_boxes = []
        point = None if pixel_point is None else device.logical_point(pixel_point, image)
        if TRACE_DIR is not None:
            save_trace(image, state, point)

        # The weekly adventure-sync report covers the whole screen once a week
        # and fools every test below it: the pill detector finds an edge of its
        # close disc and taps left of centre into the card, the league-card
        # detector counts its white slab, and the sea left showing underneath
        # puts two samples into the teal grid -- so the loop dismisses a
        # post-battle checkmark that is not there and never stops.  The SE
        # spent a morning on it: 151 taps into the map, nought battles.  It is
        # never drawn over a battle, so a leg that is attacking is left alone.
        if not attacking and menu_boxes and android_gbl.weekly_report_visible(menu_boxes):
            close = android_gbl.weekly_report_close_point(frame)
            if close is None:
                log(device, "  Weekly report is up but its close disc is not; waiting")
                await wait(device, android_gbl.POLL_GAP, use_modifier=False)
                continue
            shut = device.fraction_point(0.5, close[1] / frame[1])
            log(device, f"  Weekly adventure report, closing it at {shut}")
            repeats = 0
            last_tap = None
            unknown = 0
            teal_reads = 0
            await device.tap(shut)
            await wait(device, android_gbl.MENU_SETTLE)
            continue

        # Same cap, same live-looking pill.  The SE reaches this first because
        # it starts earliest.
        if not attacking and menu_boxes and android_gbl.daily_cap_reached(menu_boxes):
            log(device, f"Daily battle cap reached; stopping after {played} battle(s)")
            break

        # The page the game opens on a Pokemon the reward encounter caught. Its
        # provenance line says "CAUGHT IN THE GBL MASTER LEAGUE: MEGA EDITION",
        # which is prose the league test reads as a card: the SE tapped its
        # move list every five seconds, "taking Master League", until it was
        # closed by hand. The catch routine already knows this page and closes
        # it the same way; the loop needs to know it too, for the times the
        # page outlives that routine.
        if not attacking and menu_boxes and gbl_vision.pokemon_detail_visible(menu_boxes):
            close = encounter_close_point(device)
            log(device, f"  A Pokemon's page is over the game, closing it at {close}")
            repeats = 0
            last_tap = None
            unknown = 0
            await device.tap(close)
            await wait(device, android_gbl.MENU_SETTLE)
            continue

        # Same sheet, same card, same wasted tap as on the android-three -- the
        # iPhones draw the GBL card from the same assets, so the words that
        # name it there name it here.
        if not attacking and menu_boxes and android_gbl.rank_modal_visible(menu_boxes):
            shut = device.fraction_point(
                android_gbl.RANK_MODAL_CLOSE_X, android_gbl.RANK_MODAL_CLOSE_Y
            )
            log(device, f"  Rank roster sheet over the card, closing it at {shut}")
            repeats = 0
            last_tap = None
            unknown = 0
            teal_reads = 0
            await device.tap(shut)
            await wait(device, android_gbl.MENU_SETTLE)
            continue

        if attacking and state != "battle" and android_gbl.battle_animation(menu_boxes):
            state = "battle"
            point = None

        # "Battle starting" is the last thing drawn before combat, so the screen
        # after it is the battlefield.  The SE reads that opening frame as a
        # bare pill instead, and the pill branch below recomputes `armed`
        # against it: no party screen, no league list, so the catch drops, the
        # frame is tapped as a result screen, and the loop falls into the
        # unrecognised-screen limbo for the rest of the fight -- 30 reads of map
        # recovery pressing a BATTLE label that is not there, then the exit
        # door, while the battle it is standing in runs down unplayed and is
        # lost without a move.  Twice in one live set on 24 Aug.  The party
        # screen, the league list and an expired challenge are the only things
        # that legitimately come between matchmaking and combat; anything else
        # here is the battle, so name it one and keep the catch.
        if (
            battle_imminent
            and armed
            and not attacking
            and state == "pill"
            and not android_gbl.challenge_expired(menu_boxes)
            and not android_gbl.league_cards(frame)
            and not android_gbl.party_screen(
                frame, pixel_point, PARTY_PILL_Y, PARTY_TOP_MAX
            )
            and not android_gbl.party_screen_ocr(menu_boxes)
        ):
            log(device, "  Battle starting; that screen is the battlefield, not a result")
            battle_imminent = False
            state = "battle"
            point = None

        # A one-panel white overlay during/resulting from combat is dismissed by
        # the same safe battlefield taps as other result screens.  Scrolling is
        # only useful before the party screen has armed the battle loop.
        if state == "scroll" and (armed or attacking):
            state = "battle"

        if point is not None:
            here = (state, point[0], point[1])
            same_place = (
                last_tap is not None
                and here[0] == last_tap[0]
                and abs(here[1] - last_tap[1]) <= 5
                and abs(here[2] - last_tap[2]) <= 5
            )
            repeats = repeats + 1 if same_place else 0
            last_tap = here

        if state == "battle" and vision_label is None:
            if not menu_boxes:
                try:
                    menu_boxes = await asyncio.to_thread(gbl_vision.recognize, image)
                except gbl_vision.VisionOCRError:
                    menu_boxes = []
            for box in menu_boxes:
                if _is_runaway_prompt(box.text):
                    vision_label = box.text
                    break

        if repeats >= android_gbl.STALL_LIMIT:
            # The same button, tapped STALL_LIMIT times, with nothing behind it
            # moving.  Clearing the tap history is the whole point of this
            # branch: `repeats` used to survive it, so every later read walked
            # straight back in here and the count ran out between two
            # screenshots -- the SE reported 25 battles in under a minute
            # having played none of them.
            stalled_at = point
            repeats = 0
            last_tap = None
            unknown = 0
            if attacking:
                # Only a battle in progress can be written off as interrupted.
                outcome = android_gbl.battle_outcome(menu_boxes) or pending_outcome
                pending_outcome = None
                suffix = f" ({outcome})" if outcome else " (interrupted)"
                if outcome is None:
                    interrupted += 1
                log(device, f"Battle {played + 1}/{count} over{suffix}")
                played += 1
                attacking = False
                if played >= count:
                    break
            else:
                # A menu that will not answer has not cost a battle, so it does
                # not spend one.  It does get a bounded number of goes at the
                # recovery below before the run is written off, otherwise a
                # dead button is a silent forever-loop.
                stalls += 1
                if stalls > STALL_RECOVERY_LIMIT:
                    log(
                        device,
                        f"  Tapped {stalled_at} {android_gbl.STALL_LIMIT} times with "
                        f"nothing changing, {STALL_RECOVERY_LIMIT} recoveries deep; "
                        "stopping",
                    )
                    break
                log(
                    device,
                    f"  {state} tap at {stalled_at} changed nothing; refreshing the "
                    f"battle flow ({stalls}/{STALL_RECOVERY_LIMIT})",
                )
            await recover_to_gbl(
                device,
                image,
                frame,
                menu_boxes,
                android_gbl.STALL_LIMIT + stalls,
            )
            await wait(device, android_gbl.MENU_SETTLE)
            continue
        if state != "battle":
            # The guard above had its look at this screen and passed on it, so
            # whatever matchmaking promised has been overtaken by a menu.
            battle_imminent = False
            if armed and not attacking and android_gbl.challenge_expired(menu_boxes):
                matchmaking_failures += 1
                if matchmaking_failures >= 3:
                    log(device, "  Matchmaking expired three times; stopping safely")
                    break
                log(
                    device,
                    f"  Matchmaking expired; retrying ({matchmaking_failures}/3)",
                )
                armed = False
            # Any screen with a button on it means the battle is behind us.
            unknown = 0
            if attacking:
                outcome = android_gbl.battle_outcome(menu_boxes) or pending_outcome
                pending_outcome = None
                attacking = False
                if outcome is None and battle_reads == 0:
                    # No battlefield was ever read between the party screen and
                    # this one, so there was no battle to be over.  Counting it
                    # spends an entry the phone never played, and five of them
                    # report a finished set from a phone standing still.
                    phantom += 1
                    log(device, "  No battlefield was ever read; not counting that as a battle")
                    armed = False
                    saw_league = False
                    saw_result = False
                    if phantom >= android_gbl.PHANTOM_LIMIT:
                        log(device, f"  {phantom} battles began on a menu screen; stopping")
                        break
                else:
                    played += 1
                    if outcome is None:
                        # No WIN or LOSS was ever read off this battle, so
                        # nothing here says the game counted it.  The day now
                        # ends when the league list is refused, not when this
                        # tally reaches a number, so an uncounted battle costs
                        # one more attempt and a wrongly counted one costs the
                        # rest of the day: the SE reported a full 25 this way
                        # and was still owed battles.
                        interrupted += 1
                    suffix = f" ({outcome})" if outcome else " (result OCR unavailable)"
                    log(device, f"Battle {played}/{count} over{suffix}")
                    if played >= count:
                        break
            if state == "league_scroll":
                # The configured league is not on screen.  Nothing here is worth
                # tapping: any other card starts five battles in the wrong
                # league, and the card wanted is usually one scroll away.
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls = 0
                row_scroll_spent = False
                league_attempts += 1
                if league_attempts > LEAGUE_SCROLL_LIMIT:
                    log(
                        device,
                        f"  {wanted_league} never appeared in the league list; "
                        "falling back to the easiest available league",
                    )
                    wanted_league = None
                    league_attempts = 0
                    continue
                back = league_attempts > LEAGUE_SCROLL_LIMIT // 2
                heading = "back up" if back else "down"
                log(
                    device,
                    f"  {wanted_league} is not on screen; scrolling {heading} the "
                    f"league list ({league_attempts}/{LEAGUE_SCROLL_LIMIT})",
                )
                await scroll_league_list(device, back=back)
                await wait(device, android_gbl.MENU_SETTLE)
                continue
            if state == "league":
                if wanted_league is not None and vision_label is None:
                    # Only the shape detector saw this list, and it cannot tell
                    # one card from another -- its answer is the bottom-most
                    # card whatever that card says.  Read again instead.
                    league_attempts += 1
                    if league_attempts > LEAGUE_SCROLL_LIMIT:
                        log(
                            device,
                            "  League cards never came back readable; stopping "
                            f"rather than guessing at {wanted_league}",
                        )
                        break
                    log(device, "  League list is unreadable; waiting for a clean read")
                    await wait(device, android_gbl.MENU_SETTLE)
                    continue
                armed = False
                saw_league = True
                saw_result = False
                reward_scrolls = 0
                row_scroll_spent = False
                league_attempts = 0
                detail = f" ({vision_label})" if vision_label else ""
                taking = wanted_league if wanted_league is not None else "the easiest"
                log(device, f"  League list, taking {taking} at {point}{detail}")
                league_confirmed = wanted_league is not None
            elif state == "orange":
                # End of a set: the rewards have to be taken before the BATTLE
                # pill comes back.
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls = 0
                row_scroll_spent = False
                log(device, f"  Set finished, taking the reward button at {point}")
            elif state == "tiles":
                # The reward row is scrolled past the tile this set earned.
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls += 1
                if reward_scrolls > android_gbl.REWARD_SCROLL_LIMIT:
                    # A row that will not move is not a reason to end the day.
                    # The card still has a BATTLE pill on it, and the next read
                    # is told to leave the row alone and press that instead.
                    row_scroll_spent = True
                    log(device, "  Reward row will not move; taking the card as it is")
                    continue
                log(
                    device,
                    "  Only locked reward tiles in view; scrolling the row back "
                    f"to the earned one ({reward_scrolls}/"
                    f"{android_gbl.REWARD_SCROLL_LIMIT})",
                )
                await scroll_reward_row(device, point)
                await wait(device, android_gbl.MENU_SETTLE)
                continue
            elif state in ("scroll", "scroll_back"):
                armed = False
                saw_league = False
                saw_result = False
                reward_scrolls += 1
                if reward_scrolls > android_gbl.REWARD_SCROLL_LIMIT:
                    log(device, "  Reward chooser did not reveal its action button; stopping")
                    break
                if state == "scroll_back":
                    log(device, "  Completed-set reward is above the fold; scrolling back to it")
                else:
                    log(device, "  Free reward-tier button is below the fold; scrolling it into view")
                await scroll_reward_tiers(device, back=state == "scroll_back")
                await wait(device, android_gbl.MENU_SETTLE)
                continue
            else:
                # The party screen is the only pill worth arming on, and there
                # are two ways to reach it: picking a league, or taking NEXT
                # BATTLE off a result screen.  Only the first battle of a set
                # comes through the league list, so arming on that alone left
                # battles two to five of every set standing still on the
                # battlefield.  A result screen is the pill with no white sheet
                # behind it; every menu pill sits on a white panel.
                # pixel_point, not point: party_screen measures the pill against
                # the frame, and the frame is in screenshot space while point
                # has already been converted to logical space for tapping.
                # The welcome card is a card, not an open set.  Take its
                # LET'S GO! before the league check below can mistake it for
                # one; the label is already an action label, so the pill is
                # found the same way every other GBL action is.
                if menu_boxes and _welcome_card_visible(menu_boxes):
                    welcome = gbl_vision.action_point(
                        menu_boxes, image.width, image.height
                    )
                    if welcome is not None:
                        target = device.logical_point(welcome[0], image)
                        log(device, f"  Welcome card; taking LET'S GO! at {target}")
                        await device.tap(target)
                        await wait(device, android_gbl.MENU_SETTLE)
                        continue

                on_party = android_gbl.party_screen(
                    frame, pixel_point, PARTY_PILL_Y, PARTY_TOP_MAX
                ) or android_gbl.party_screen_ocr(menu_boxes)
                if on_party and wanted_league is not None and not league_confirmed:
                    # Reaching a party screen without having chosen a league
                    # means the set was already running when this leg started.
                    # The screen names its own league, so a set that already is
                    # the configured one gets played rather than thrown away.
                    open_league = _party_league_name(menu_boxes)
                    if gbl_strategy.league_matches(wanted_league, open_league):
                        log(device, f"  The open set is {open_league}; playing it")
                        league_confirmed = True
                    else:
                        league_exits += 1
                        if league_exits > LEAGUE_EXIT_LIMIT:
                            log(
                                device,
                                "  Could not get back to the league list; stopping "
                                f"rather than playing a set that is not "
                                f"{wanted_league}",
                            )
                            break
                        naming = open_league or "an unnamed league"
                        log(
                            device,
                            f"  The open set is {naming}, not {wanted_league}; "
                            "backing out to the league list "
                            f"({league_exits}/{LEAGUE_EXIT_LIMIT})",
                        )
                        await leave_to_league_list(device)
                        await wait(device, android_gbl.MENU_SETTLE)
                        continue
                if on_party and not party_preparation_attempted:
                    party_preparation_attempted = True
                    prepared = await prepare_best_party(
                        device, image, menu_boxes, profile
                    )
                    if prepared is not None:
                        profile = prepared
                    # Preparation never taps USE THIS PARTY. Re-read the
                    # screen before the battle can start.
                    continue
                actual_team = gbl_strategy.team_from_party_ocr(
                    menu_boxes, (member.name for member in profile.team.members)
                )
                if on_party and actual_team is not None:
                    profile = gbl_strategy.StrategyProfile(actual_team, profile.settings)
                    names = ", ".join(member.name for member in actual_team.members)
                    log(device, f"  Verified visible party: {names}")
                armed = on_party or saw_league or saw_result
                saw_league = False
                saw_result = not armed and not android_gbl.league_cards(frame)
                reward_scrolls = 0
                row_scroll_spent = False
            if armed:
                log(device, f"  Party ready, using it at {point}")
            elif saw_result:
                log(device, f"  Result screen, taking NEXT BATTLE at {point}")
            elif vision_label and _is_runaway_prompt(vision_label):
                cancel_point = _run_away_cancel_point(
                    image=image,
                    device=device,
                    menu_boxes=menu_boxes,
                    fallback_point=point,
                )
                repeats = 0
                last_tap = None
                log(
                    device,
                    f"  Vision action looks like run-away prompt ({vision_label}); tapping cancel at {cancel_point}",
                )
                await wait(device, android_gbl.MENU_SETTLE)
                await device.tap(cancel_point)
                await wait(device, android_gbl.POLL_GAP, use_modifier=False)
                continue
            else:
                log(device, f"  Action button at {point}")
            await wait(device, android_gbl.MENU_SETTLE)
            await device.tap(point)  # type: ignore[arg-type]
            if state == "league":
                await wait(device, 1.5)
            else:
                await wait(device, android_gbl.POLL_GAP, use_modifier=False)
            continue

        # GOOD EFFORT!/VICTORY! draw a teal tick and nothing else, so no test
        # above can name the screen.  Checked before the armed guard for the
        # same two reasons as on Android: a battle that has just ended would
        # otherwise be attacked until the runaway timer, and a phone parked on
        # this screen by a previous run would sit here until UNKNOWN_LIMIT.
        if not android_gbl.teal_result_ready(frame, TEAL_SAMPLE_X, TEAL_SAMPLE_Y):
            teal_reads = 0
        elif teal_reads >= TEAL_DISMISS_LIMIT:
            # The same teal, tapped TEAL_DISMISS_LIMIT times, still there.  A
            # checkmark clears on the first tap, so this one is scenery the
            # sample grid happens to land on.  Falling through hands the frame
            # to the unrecognised-screen count below, which is bounded and
            # recovers; tapping on would not be, and was not.
            log(
                device,
                f"  Teal at the result point has not cleared in "
                f"{TEAL_DISMISS_LIMIT} taps; treating it as scenery",
            )
        else:
            result = device.fraction_point(RESULT_TAP_X, RESULT_TAP_Y)
            result_boxes = menu_boxes
            if not result_boxes:
                try:
                    result_boxes = await asyncio.to_thread(gbl_vision.recognize, image)
                except gbl_vision.VisionOCRError:
                    result_boxes = []
            pending_outcome = android_gbl.battle_outcome(result_boxes) or pending_outcome
            detail = f" ({pending_outcome})" if pending_outcome else ""
            log(device, f"  Post-battle checkmark{detail}, dismissing it at {result}")
            teal_reads += 1
            unknown = 0
            await device.tap(result)
            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
            continue

        # Neither a pill nor the league list, so this screen is unrecognised.
        # It is only a battlefield if a party screen was tapped through to get
        # here; otherwise it is a menu this module cannot read, and fast taps
        # would go into whatever it is drawing. The party editor's picker sits
        # one stray tap off the league flow, and taps landing there select
        # Pokemon, so an unrecognised screen has to be waited out, never tapped.
        # A finished set pays out as a catch screen drawn over the card.  Every
        # test above falls through it -- no pill, no cards, no league list --
        # and the tests are right to: there is nothing on it they should press.
        # The SE proved this the same way the moto did, stopping on a Stunfisk
        # with two sets collected and the rest of the day unplayed.  It is named
        # by its own name plate instead, and answered by the routine that knows
        # the three screens a catch leaves behind.
        if not armed and not attacking and menu_boxes \
                and gbl_vision.encounter_plate(menu_boxes, image.height) is not None:
            if await take_reward_encounter(
                device, spend_balls=abandoned < ABANDONED_ENCOUNTER_LIMIT
            ):
                unknown = 0
                abandoned = 0
                await wait(device, android_gbl.MENU_SETTLE)
            else:
                abandoned += 1
            continue

        # The shield prompt names itself, and it is drawn nowhere but on a
        # battlefield, so it is proof combat is running on a frame whose shapes
        # say nothing.  Android arms on it for exactly this reason; without it
        # here, a catch dropped on the way in leaves the loop sitting out the
        # battle it is standing in, taking the exit door below while the timer
        # runs down.
        if not armed and not attacking and menu_boxes \
                and android_gbl.shield_prompt_text(menu_boxes):
            log(device, "  Shield prompt: this is a battlefield, arming the loop")
            shield = find_shield_point(frame)
            if shield is not None:
                shield = device.logical_point(shield, image)
                shields_used = 1
                log(device, f"  Shield offered, taking it at {shield} (1/2 shields used)")
                await device.tap(shield)
            armed = True
            unknown = 0
            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
            continue

        if not armed:
            unknown += 1
            if unknown >= android_gbl.UNKNOWN_LIMIT:
                log(device, f" Unrecognised screen {unknown} reads; stopping without tapping")
                break
            # The map is the screen a dropped run lands on, and it is worth
            # naming before anything else here: walked back, it costs two taps,
            # and left unnamed it costs the rest of the day.
            if gbl_home_recovery.main_menu_open(image) or gbl_home_recovery.on_map(image):
                await walk_home_to_gbl(device, image)
                await wait(device, android_gbl.MENU_SETTLE)
                continue
            if unknown <= 2:
                await recover_to_gbl(device, image, frame, menu_boxes, unknown)
                await wait(device, android_gbl.MENU_SETTLE)
                continue
            if unknown in android_gbl.UNKNOWN_EXIT_AT:
                door = device.fraction_point(EXIT_DOOR_X, EXIT_DOOR_Y)
                log(device, f" Still unrecognised; taking exit door at {door}")
                await device.tap(door)
                await wait(device, android_gbl.POLL_GAP, use_modifier=False)
                continue
            # Nothing tapped a party through to get here, so this is not a
            # battlefield -- and yet the code below used to make it one.  Once
            # the two recovery attempts above were spent, this branch ran out of
            # things to do and fell through to the matchmaking test, which set
            # `attacking = True` on whatever was on screen.  That is how a phone
            # standing on the map reported battles it never played.  Wait and
            # read again instead; UNKNOWN_LIMIT is what ends a run that is
            # genuinely lost.
            log(device, f" Unrecognised screen ({unknown} reads); waiting, not tapping")
            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
            continue

        if not attacking:
            battle_boxes = menu_boxes
            if not battle_boxes:
                try:
                    battle_boxes = await asyncio.to_thread(gbl_vision.recognize, image)
                except gbl_vision.VisionOCRError:
                    battle_boxes = []
            match_status = android_gbl.matchmaking_status(battle_boxes)
            if match_status is not None:
                if match_status == "battle starting":
                    battle_imminent = True
                if match_status != last_matchmaking_status:
                    log(device, f"  Matchmaking: {match_status}")
                    last_matchmaking_status = match_status
                await wait(device, android_gbl.POLL_GAP, use_modifier=False)
                continue
            # `state` is "battle" only because none of the menu shapes matched:
            # it is a fallback, not a sighting of a battlefield.  A phone parked
            # on the map or on an open main menu fits that description too, and
            # `armed` carries over from the previous screen, so nothing else here
            # stops a standing phone from reporting a set it never played.  Name
            # those two screens and walk back instead.
            if gbl_home_recovery.on_map(image) or gbl_home_recovery.main_menu_open(image):
                log(device, "  Home screen behind that battle read; walking back to GBL")
                armed = False
                saw_league = False
                saw_result = False
                phantom += 1
                if phantom >= android_gbl.PHANTOM_LIMIT:
                    log(device, f"  Punted home {phantom} times without a battle; stopping")
                    break
                await walk_home_to_gbl(device, image)
                await wait(device, android_gbl.MENU_SETTLE)
                continue
            attacking = True
            # A battle starting is proof the menus answered; the stall budget
            # is for a phone that is stuck, not for one that is slow once.
            stalls = 0
            teal_reads = 0
            battle_imminent = False
            last_matchmaking_status = None
            battle_reads = 0
            static = 0
            switch_taps = 0
            signature = None
            memory = gbl_strategy.BattleMemory()
            shields_used = 0
            sheet_seen = False
            pending_reads = 0
            home_seen = 0
            deadline = asyncio.get_event_loop().time() + android_gbl.BATTLE_RUNAWAY
            log(device, f"  Battle {played + 1}/{count} started, attacking")
        if deadline is not None and asyncio.get_event_loop().time() > deadline:
            outcome = android_gbl.battle_outcome(menu_boxes) or pending_outcome
            pending_outcome = None
            suffix = f" ({outcome})" if outcome else " (interrupted)"
            if outcome is None:
                interrupted += 1
            log(device, f"Battle {played + 1}/{count} over{suffix}")
            played += 1
            if played >= count:
                break
            unknown = 0
            attacking = False
            await recover_to_gbl(
                device,
                image,
                frame,
                menu_boxes,
                android_gbl.BATTLE_STATIC_LIMIT,
            )
            await wait(device, android_gbl.MENU_SETTLE)
            continue

        # A battlefield never still: Pokemon bob, timers count down, energy
        # fills. screen module cannot name also does not move, so it stops instead
        # tapping the runaway timer timer out against it.
        here_signature = android_gbl.frame_signature(frame)
        static = static + 1 if here_signature == signature else 0
        signature = here_signature
        if static >= android_gbl.BATTLE_STATIC_LIMIT:
            outcome = android_gbl.battle_outcome(menu_boxes) or pending_outcome
            pending_outcome = None
            suffix = f" ({outcome})" if outcome else " (interrupted)"
            if outcome is None:
                interrupted += 1
            log(
                device,
                f" Screen unchanged for {static} armed reads; not a battle, stopping"
            )
            played += 1
            if played >= count:
                break
            unknown = 0
            attacking = False
            await recover_to_gbl(
                device,
                image,
                frame,
                menu_boxes,
                static,
            )
            await wait(device, android_gbl.MENU_SETTLE)
            continue
        # A battlefield is not the world map.  When the game drops the run home
        # mid-fight nothing below finds a control to press, the map keeps moving
        # so the static test never fires, and the only thing that ends the
        # battle is the runaway timer: five and a half minutes of tapping the
        # map before the recovery walk even begins.  One cheap pixel scan every
        # few reads names the screen instead, and two in a row are required so a
        # transition frame cannot abandon a real battle.  Only the pokeball is
        # looked for: it needs 20 columns of red over white in the bottom fifth
        # of the centre, which a battlefield has nowhere, while the menu's teal
        # rings share a screen region with the reserve portraits and are not
        # worth risking a real battle over.  A phone punted to an open menu is
        # one tap from the map, and the next read catches it there.
        if battle_reads % android_gbl.HOME_CHECK_EVERY == 0:
            if gbl_home_recovery.on_map(image):
                home_seen += 1
            else:
                home_seen = 0
        if home_seen >= 2:
            outcome = android_gbl.battle_outcome(menu_boxes) or pending_outcome
            pending_outcome = None
            attacking = False
            unknown = 0
            if outcome is None and battle_reads == 0:
                phantom += 1
                log(device, "  Punted home before any battlefield read; not counting that as a battle")
                if phantom >= android_gbl.PHANTOM_LIMIT:
                    log(device, f"  {phantom} battles began on a menu screen; stopping")
                    break
            else:
                played += 1
                if outcome is None:
                    interrupted += 1
                suffix = f" ({outcome})" if outcome else " (interrupted)"
                log(device, f"Battle {played}/{count} over{suffix}, back on the home screen")
                if played >= count:
                    break
            armed = False
            home_seen = 0
            await walk_home_to_gbl(device, image)
            await wait(device, android_gbl.MENU_SETTLE)
            continue
        if android_gbl.switch_sheet(frame, SHEET_ABOVE_Y, SHEET_SCAN_Y):
            if memory.pending_index is None:
                if memory.opponent_name is None:
                    try:
                        forced_opponent, _not_effective = await recognize_battle_context(image)
                    except gbl_vision.VisionOCRError:
                        forced_opponent = None
                    if forced_opponent is not None:
                        memory.opponent_name = forced_opponent
                        log(device, f"  Opponent recognized before forced switch: {forced_opponent}")
                memory.begin_forced_switch()
                decision = gbl_strategy.choose_switch(
                    profile.team,
                    memory,
                    memory.opponent_name,
                    profile.settings,
                    forced=True,
                )
                if decision.target_index is not None:
                    memory.request_switch(decision.target_index)
                    log(device, f"  {decision.reason}")
            target = memory.pending_index
            if target is None:
                xf = SHEET_CARD_X[switch_taps % len(SHEET_CARD_X)]
            else:
                slot = gbl_strategy.reserve_sheet_slot(memory, target)
                visible = sum(
                    alive and index != memory.active_index
                    for index, alive in enumerate(memory.alive)
                )
                xf = 0.50 if visible == 1 else (0.337, 0.663)[slot]
            card = device.fraction_point(xf, SHEET_CARD_Y)
            log(device, f"  Switch sheet up, choosing the planned reserve at {card}")
            switch_taps += 1
            sheet_seen = True
            await device.tap(card)
            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
            continue
        switch_taps = 0
        if sheet_seen and memory.pending_index is not None:
            chosen = memory.commit_switch()
            log(device, f"  Switched to {profile.team.members[chosen].name}")
            sheet_seen = False
            pending_reads = 0
        elif memory.pending_index is not None:
            pending_reads += 1
            if pending_reads > 3:
                log(device, "  Switch button did not open; cancelling the pending switch")
                memory.pending_index = None
                pending_reads = 0

        # The shield prompt stops the battle while it is open, so it is worth a
        # repositioned tap the moment it is seen.
        shield = find_shield_point(frame)
        if shield is not None:
            # shield_point measures the hexagon in the screenshot, so the point
            # has to come back to logical space like every other detector read.
            # Tapped raw it lands off the short edge of the SE's 375x667
            # viewport, the prompt is never dismissed, and the `continue` below
            # keeps the loop away from the charged-move branch for the whole
            # battle -- 62 taps and no charged move in one traced set.
            shield = device.logical_point(shield, image)
            shields_used += 1
            log(device, f"  Shield offered, taking it at {shield} ({min(2, shields_used)}/2 shields used)")
            await device.tap(shield)
            await wait(device, android_gbl.POLL_GAP, use_modifier=False)
            continue

        # Name OCR is deliberately less frequent than screenshots.  It runs on
        # the opponent half only, so the player's own name cannot win matching.
        if (
            memory.pending_index is None
            and battle_reads % profile.settings.opponent_ocr_every_reads == 0
            ):
                decision = None
                try:
                    opponent, not_effective = await recognize_battle_context(image)
                except gbl_vision.VisionOCRError as exc:
                    opponent = None
                    not_effective = False
                    if not ocr_failed:
                        log(device, f"  Opponent OCR unavailable ({exc})")
                        ocr_failed = True
                opponent = opponent or memory.opponent_name
                if opponent is not None:
                    if opponent != memory.opponent_name:
                        memory.opponent_name = opponent
                        log(device, f"  Opponent recognized: {opponent}")
                    if not_effective:
                        log(device, "  Outgoing attack is not very effective; seeking counter")
                    decision = gbl_strategy.choose_switch(
                        profile.team,
                        memory,
                        opponent,
                        profile.settings,
                        urgent=not_effective,
                    )
                if decision is not None and not_effective and decision.target_index is None:
                    log(device, f"  Counter-switch unavailable: {decision.reason}")
                if decision is not None and decision.target_index is not None:
                    target = decision.target_index
                    slot = gbl_strategy.reserve_sheet_slot(memory, target)
                    visible = sum(
                        alive and index != memory.active_index
                        for index, alive in enumerate(memory.alive)
                    )
                    yf = SWITCH_ONLY_Y if visible == 1 else SWITCH_RESERVE_Y[slot]
                    memory.request_switch(target)
                    swap = device.fraction_point(SWITCH_RESERVE_X, yf)
                    log(device, f"  {decision.reason}; tapping reserve at {swap}")
                    await device.tap(swap)
                    expected = profile.team.members[target].name
                    active = await confirm_switch(
                        device,
                        tuple(member.name for member in profile.team.members),
                        expected,
                        move_point=move,
                    )
                    if gbl_strategy.canonical_name(active or "") == gbl_strategy.canonical_name(expected):
                        memory.commit_switch()
                        log(device, f"  Verified switch to {expected}")
                    else:
                        failures = memory.record_failed_switch(target)
                        seen = f"read {active}" if active else "read no team name"
                        log(
                            device,
                            f"  Reserve switch to {expected} was not verified "
                            f"({seen}, attempt {failures})",
                        )
                        if memory.switch_blocked(target):
                            log(
                                device,
                                f"  Giving up on {expected} for this battle; "
                                f"the header never showed it",
                            )
                    continue

        rows = charged_disc_rows(frame)
        charged_px = android_gbl.charged_move_target_point(frame, rows)
        charge_ready = android_gbl.charged_move_ready(frame, rows)
        if charge_ready:
            # Which of the two moves is lit comes from the screenshot, but the
            # row is a measured fraction of the viewport, so only the x needs
            # converting -- pairing a pixel x with a logical y put the tap off
            # the right edge on both phones.
            charged_pt = [
                device.logical_point(charged_px, image)[0],
                int(device.viewport["height"] * charged_centre_y(frame)),
            ]
            log(device, f"  Charged move ready at {charged_pt}; launching")
            launched, attempts = await launch_charged_move(device, target_point=charged_pt)
            if launched:
                log(device, f"  Charged move launch verified after {attempts} tap(s)")
                await charged_minigame(device)
                rating_image = await device.screenshot()
                if rating_image is not None:
                    if TRACE_DIR is not None:
                        save_trace(rating_image, "charge-result", None)
                    try:
                        rating_boxes = await asyncio.to_thread(
                            gbl_vision.recognize, rating_image
                        )
                    except gbl_vision.VisionOCRError:
                        rating_boxes = []
                    rating = android_gbl.charged_minigame_rating(rating_boxes)
                    if rating is not None:
                        log(device, f"  Charged minigame: {rating}")
                battle_reads += 1
                continue
            log(device, "  Charged move tap did not launch; resuming fast attacks")

        await fast_attack(device, move)
        battle_reads += 1

    detail = f" ({interrupted} of them interrupted)" if interrupted else ""
    log(device, f"Finished: {played} battle(s){detail}")
    return played


async def probe(
    device: IOSGBLDevice,
    image: Image.Image,
    profile: gbl_strategy.StrategyProfile | None = None,
) -> str:
    """Read-only: prints the numbers behind the verdict without sending a tap."""
    profile = profile or gbl_strategy.load_strategy_profile(device_name="ios-one")
    frame = raw_frame(image)
    state, pixel_point, vision_label, _ = await smart_screen_state(
        image,
        profile,
        probe_unknown=True,
    )
    point = None if pixel_point is None else device.logical_point(pixel_point, image)
    log(
        device,
        f"viewport {device.viewport['width']}x{device.viewport['height']}; "
        f"screenshot {image.width}x{image.height} -> {state} at {point}",
    )
    if vision_label:
        log(device, f"  Vision decision: {vision_label}")
    log(
        device,
        f"  pill: {android_gbl.find_pill(frame)}  orange: {android_gbl.find_orange(frame)}",
    )
    cards = android_gbl.league_cards(frame)
    if cards:
        spans = ", ".join(
            f"{top}-{bottom} ({(bottom - top) / image.height:.3f}h)" for top, bottom in cards
        )
        log(device, f"  white cards: {spans}")
    else:
        log(device, "  white cards: none")
    move = device.config.coordinates["GBL_MOVE_BTN"]
    anchor = device.image_point(move, image)
    log(
        device,
        f"  GBL_MOVE_BTN {move} -> pixel {anchor}: "
        f"rgb={android_gbl.pixel(frame[0], frame[2], frame[3], anchor[0], anchor[1])}",
    )
    log(
        device,
        f"  charged_ready="
        f"{android_gbl.charged_move_ready(frame, charged_disc_rows(frame))} "
        f"teal_result="
        f"{android_gbl.teal_result_ready(frame, TEAL_SAMPLE_X, TEAL_SAMPLE_Y)}",
    )
    return state


@contextmanager
def device_lock(udid: str) -> Iterator[None]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{udid}.lock"
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IOSGBLError("This iPhone is already controlled by another fleet command") from exc
        yield
    finally:
        handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--battles", type=int, help="battles to play (default: the config's)")
    parser.add_argument("--check", action="store_true", help="read-only screen/detector check")
    parser.add_argument("--dry-run", action="store_true", help="connect and validate without tapping")
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_CHECK_SCREENSHOT)
    parser.add_argument("--fleet-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--trace",
        type=Path,
        help="write every frame a decision was made on into this directory",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace, config: RuntimeConfig) -> int:
    device = await asyncio.to_thread(IOSGBLDevice.connect, config)
    device_name = "ios-one"
    # Strategy lookup is explicit private profile data, never inferred from hardware identity.
    device_name = str(config.ios_device.get("strategy_key", "default"))
    normal_exit = False
    try:
        # Result screens animate their button in, so a single frame is not
        # enough to say the phone is on the wrong screen: the first look often
        # lands on GOOD EFFORT! before NEXT BATTLE has faded up.  Look a few
        # times before refusing, and stop as soon as a button appears.
        image = None
        state = "battle"
        verified_result = False
        for attempt in range(START_LOOKS):
            if attempt:
                await wait(device, START_LOOK_GAP, use_modifier=False)
            image = await device.screenshot()
            if image is None:
                raise IOSGBLError("Could not read the iPhone screen")
            state = await probe(device, image, profile)
            verified_result = android_gbl.teal_result_ready(
                raw_frame(image), TEAL_SAMPLE_X, TEAL_SAMPLE_Y
            )
            if state != "battle" or verified_result or args.check or args.dry_run:
                break
        if args.check:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            image.save(args.screenshot)
            print(f"Saved {args.screenshot}")
            normal_exit = True
            return 0
        if args.dry_run:
            print(f"Dry run passed on a '{state}' screen; no taps sent")
            normal_exit = True
            return 0
        if state == "battle" and not verified_result:
            # Before reading anything into the screen, check the screen belongs
            # to the game at all.  A leg that stops takes its WebDriverAgent
            # runner down with it and leaves the phone on the home screen, and
            # every later leg of the day then reads five SpringBoard frames and
            # refuses -- the phone is dropped for the day over an app switch.
            if await device.restore_foreground():
                await wait(device, START_LOOK_GAP, use_modifier=False)
                image = await device.screenshot()
                if image is None:
                    raise IOSGBLError("Could not read the iPhone screen")
                state = await probe(device, image, profile)
                verified_result = android_gbl.teal_result_ready(
                    raw_frame(image), TEAL_SAMPLE_X, TEAL_SAMPLE_Y
                )
        if state == "battle" and not verified_result:
            # The one screen worth naming before refusing.  A run that stopped
            # on a set's reward catch leaves the phone parked on it, and the
            # gate below then refuses every later run of the day -- which is
            # exactly how the SE sat on a Stunfisk with 20 battles unplayed.
            # The name plate says what it is, so this is not a guess, and the
            # catch routine only presses the three screens it put up itself.
            if await start_on_encounter(device, image):
                image = await device.screenshot()
                if image is None:
                    raise IOSGBLError("Could not read the iPhone screen")
                state = await probe(device, image, profile)
                verified_result = android_gbl.teal_result_ready(
                    raw_frame(image), TEAL_SAMPLE_X, TEAL_SAMPLE_Y
                )
        if state == "battle" and not verified_result:
            # The party picker is the other screen this run puts up itself, and
            # a leg that died with it open leaves every later leg of the day
            # refusing here -- the SE stalled on the picker with the keyboard
            # covering CANCEL and lost the rest of its sets to it.  OCR names
            # the screen, and cancelling out is the same guarded press
            # recover_to_gbl already makes, so this is not a blind tap.
            picker_boxes = await asyncio.to_thread(gbl_vision.recognize, image)
            if android_gbl.picker_screen(picker_boxes) or android_gbl.picker_body_screen(
                picker_boxes
            ):
                log(device, "  Started on the party picker; cancelling out of it")
                await cancel_verified_picker(device)
                image = await device.screenshot()
                if image is None:
                    raise IOSGBLError("Could not read the iPhone screen")
                state = await probe(device, image, profile)
                verified_result = android_gbl.teal_result_ready(
                    raw_frame(image), TEAL_SAMPLE_X, TEAL_SAMPLE_Y
                )
        if state == "battle" and not verified_result:
            # 'battle' is the fallback verdict: no pill, no league list, no
            # reward button.  Starting there would send fast attacks at whatever
            # the phone is actually showing, so refuse rather than guess.
            raise IOSGBLError(
                "No BATTLE pill, league list or reward button on screen; put the phone on the "
                "GO BATTLE LEAGUE screen. No taps were sent."
            )
        count = args.battles if args.battles is not None else config.battles
        played = await play_device(device, count, profile)
        print(f"Played {played} battle(s)", flush=True)
        normal_exit = True
        return 0
    finally:
        try:
            await device.quit()
            print("iPhone automation session closed", flush=True)
        finally:
            if normal_exit:
                print("iPhone WebDriverAgent kept ready for the next run", flush=True)
            else:
                await asyncio.to_thread(
                    ios_wda_cleanup.stop_wda_runner, config.ios_device["udid"]
                )


def main() -> int:
    global TRACE_DIR

    args = parse_args()
    if args.battles is not None and args.battles < 1:
        raise IOSGBLError("--battles must be positive")
    if args.trace is not None:
        TRACE_DIR = args.trace
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Tracing frames to {TRACE_DIR}")
    config = load_runtime_config(args.config.resolve())
    if args.fleet_child:
        return asyncio.run(async_main(args, config))
    with device_lock(config.ios_device["udid"]):
        return asyncio.run(async_main(args, config))


if __name__ == "__main__":
    import os
    from . import fleet_entrypoint, pokemon_fleet
    try:
        if os.environ.get("POKEMON_FLEET_CHILD") == "1" or "--fleet-child" in sys.argv:
            raise SystemExit(main())
        raise SystemExit(fleet_entrypoint.run_operation('gbl', 'battle_league.py'))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
