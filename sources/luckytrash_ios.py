#!/usr/bin/env python3
"""Safely transfer Pokémon from the filtered iOS storage list with Appium.

This is the iOS counterpart to luckyTrash.py. It preserves the Android
runner's important safety properties:

* a non-empty search filter is fingerprinted before the run and rechecked
  before every transfer;
* every tap is preceded by a screenshot classification for the screen on
  which that tap is valid;
* the irreversible YES button is found from the visible confirmation card and
  is never sent from an unverified fixed coordinate;
* the Pokémon count must visibly change after every completed transfer.

Coordinates in the YAML file are logical iOS points. Screenshots from the
current iPhone SE are twice that size in each dimension.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterator
from urllib.error import URLError
from urllib.request import urlopen

import yaml
from PIL import Image

from . import config_paths, ios_wda_cleanup


REQUIRED_COORDINATES = {
    "FIRST_TILE_BTN",
    "TILE_PITCH",
    "X_BTN",
    "TRASH_MENU_BTN",
    "TRANSFER_BTN",
    "TRANSFER_YES_BTN",
    "TRANSFER_NO_BTN",
    "FILTER_CLEAR_BTN",
}

# Delays are intentionally followed by fresh screenshot checks. A slow screen
# costs another poll; it never changes which screen receives the next tap.
DETAIL_DELAY = 1.0
MENU_DELAY = 0.8
DIALOG_DELAY = 0.8
TRANSFER_DELAY = 1.5
POLL_DELAY = 0.75
STATE_TIMEOUT = 8.0
COUNT_TIMEOUT = 8.0
BACKOUT_STEPS = 5
MAX_DIALOGS = 4

# Screen classifier. These values deliberately match the wide color gaps used
# by luckyTrash.py and were measured again on the 375x667 iPhone SE viewport.
PROBE_X = (0.10, 0.90)
TOP_BAND_Y = (110, 200)  # per-mille of screenshot height
MID_BAND_Y = (420, 480)
PROBE_STEPS = 24
GREEN_ON = 30
TOP_BRIGHT = 190
MID_BRIGHT = 190

# Search filter and visible result count.
QUERY_X = (0.25, 0.86)
QUERY_BAND_Y = (183, 208)
QUERY_NX, QUERY_NY = 64, 10
QUERY_INK = 140
QUERY_MATCH = 0.98
QUERY_MIN_INK = 0.06
FILTER_CLEAR_RADIUS = 10
FILTER_CLEAR_INK = 160
FILTER_CLEAR_MIN = 0.04

COUNT_X = (0.42, 0.56)
COUNT_BAND_Y = (90, 112)
COUNT_NX, COUNT_NY = 64, 16
COUNT_MOVED = 3

# Confirmation dialog geometry. The wide solid YES pill is detected inside the
# white card, so both the professor and Lucky confirmation layouts work.
DIALOG_X = (0.30, 0.70)
DIALOG_Y = (0.20, 0.90)
DIALOG_STEPS = 200
DIALOG_XSTEPS = 32
DIALOG_WHITE = 225
DIALOG_CARD = 0.90
DIALOG_PILL = 0.20
DIALOG_PILL_MIN = 0.03
NO_DROP = 1.32

# Storage grid occupancy is measured on the name label below each sprite.
TILE_HALF_X, TILE_HALF_Y = 0.110, 0.022
TILE_DROP = 0.33
TILE_STEPS = 60
TILE_INK = 150
TILE_OCCUPIED = 0.020
TILE_COLS, TILE_ROWS = 3, 3

LOCK_PATH = Path("/tmp/pokemon-go-ios-luckytrash.lock")
ARTIFACT_ROOT = config_paths.state_dir()
DEFAULT_SCREENSHOT = ARTIFACT_ROOT / "deletes" / "luckytrash-ios-check.png"


class LuckyTrashIOSError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    appium_server_url: str
    ios_device: dict[str, Any]
    coordinates: dict[str, list[int]]
    delay_modifier: float
    detection: dict[str, Any]


def stale_wda_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        ("not authorized" in message and "ui testing actions" in message)
        or (
            "previously found element" in message
            and "not in current view" in message
        )
    )


class IOSDevice:
    def __init__(self, driver: Any, config: RuntimeConfig) -> None:
        self.driver = driver
        self.config = config
        self.coordinates = config.coordinates
        try:
            self.viewport = driver.get_window_rect()
        except Exception as exc:
            if not stale_wda_error(exc):
                try:
                    driver.quit()
                except Exception:
                    pass
                raise
            print("iPhone target app is not active; activating Pokemon GO...", flush=True)
            bundle_id = config.ios_device.get("bundle_id", "com.nianticlabs.pokemongo")
            try:
                driver.execute_script("mobile: activateApp", {"bundleId": bundle_id})
                self.viewport = driver.get_window_rect()
            except Exception as activation_exc:
                try:
                    driver.quit()
                except Exception:
                    pass
                raise LuckyTrashIOSError(
                    f"iPhone target app ({bundle_id}) is not active and could not be restored: {activation_exc}"
                ) from activation_exc

    @classmethod
    def connect(cls, config: RuntimeConfig) -> "IOSDevice":
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ModuleNotFoundError as exc:
            raise LuckyTrashIOSError(
                "The Appium Python client is missing from this environment"
            ) from exc

        device = config.ios_device
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
                    raise LuckyTrashIOSError(
                        f"Could not create the iOS Appium session for {name}: {retry_exc}"
                    ) from retry_exc
            else:
                raise LuckyTrashIOSError(
                    f"Could not create the iOS Appium session for {name}: {exc}"
                ) from exc
        return cls(driver, config)

    def screenshot(self) -> Image.Image:
        try:
            return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")
        except Exception as exc:
            raise LuckyTrashIOSError("Could not capture the iPhone screen") from exc

    def tap(self, point: list[int] | tuple[int, int]) -> None:
        self.driver.execute_script("mobile: tap", {"x": point[0], "y": point[1]})

    def image_point(self, point: list[int] | tuple[int, int], image: Image.Image) -> tuple[int, int]:
        return (
            int(point[0] * image.width / self.viewport["width"]),
            int(point[1] * image.height / self.viewport["height"]),
        )

    def logical_point(self, point: tuple[int, int], image: Image.Image) -> list[int]:
        return [
            int(round(point[0] * self.viewport["width"] / image.width)),
            int(round(point[1] * self.viewport["height"] / image.height)),
        ]

    def quit(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise LuckyTrashIOSError(f"Config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise LuckyTrashIOSError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LuckyTrashIOSError(f"Config root in {path} must be a YAML object")
    return value


def validate_point(name: str, point: Any) -> list[int]:
    if (
        not isinstance(point, list)
        or len(point) != 2
        or not all(type(value) is int for value in point)
    ):
        raise LuckyTrashIOSError(f"Coordinate {name} must be [x, y] integer points")
    if point == [0, 0]:
        raise LuckyTrashIOSError(f"Coordinate {name} is still the [0, 0] placeholder")
    return point


def load_runtime_config(path: Path) -> RuntimeConfig:
    root = load_yaml(path)
    appium_value = root.get("appium_config", "ios-gifter.yaml")
    try:
        appium_path = config_paths.find_config(
            appium_value, path.resolve().parent, "appium_config"
        )
    except config_paths.ConfigPathError as exc:
        raise LuckyTrashIOSError(str(exc)) from exc
    appium = load_yaml(appium_path)
    device = appium.get("device")
    if not isinstance(device, dict):
        raise LuckyTrashIOSError(f"{appium_path} needs a device object")
    for key in ("udid", "team_id", "wda_bundle_id"):
        if not isinstance(device.get(key), str) or not device[key].strip():
            raise LuckyTrashIOSError(f"{appium_path} device.{key} must be set")

    values = root.get("coordinates")
    if not isinstance(values, dict):
        raise LuckyTrashIOSError("Config needs a coordinates object")
    missing = sorted(REQUIRED_COORDINATES - values.keys())
    if missing:
        raise LuckyTrashIOSError(f"Missing coordinates: {', '.join(missing)}")
    coordinates = {name: validate_point(name, point) for name, point in values.items()}

    server_url = appium.get("server_url", "http://127.0.0.1:4723")
    if not isinstance(server_url, str) or not server_url.strip():
        raise LuckyTrashIOSError("Appium server_url must be a non-empty string")
    detection = root.get("detection", {})
    if not isinstance(detection, dict):
        raise LuckyTrashIOSError("detection must be a YAML object")
    try:
        delay_modifier = float(root.get("delay_modifier", 0))
    except (TypeError, ValueError) as exc:
        raise LuckyTrashIOSError("delay_modifier must be a number") from exc
    return RuntimeConfig(server_url, device, coordinates, delay_modifier, detection)


def check_appium_status(server_url: str) -> None:
    try:
        with urlopen(f"{server_url}/status", timeout=3) as response:
            payload = json.load(response)
    except (OSError, URLError, ValueError) as exc:
        raise LuckyTrashIOSError(
            f"Appium is not ready at {server_url}; start the Appium launcher first"
        ) from exc
    if not isinstance(payload, dict) or "value" not in payload:
        raise LuckyTrashIOSError(f"Unexpected Appium status response from {server_url}")


def active_conflicting_runners() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", r"(^|[ /])(gift_ios|trade_ios_android)[.]py([ ]|$)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise LuckyTrashIOSError("Could not check for another iOS automation runner")
    return [int(value) for value in result.stdout.split() if value.isdigit()]


@contextmanager
def exclusive_runner_lock() -> Iterator[None]:
    handle = LOCK_PATH.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LuckyTrashIOSError("Another luckytrash_ios.py process is already running") from exc
        yield
    finally:
        handle.close()


def setting(device: IOSDevice, name: str, fallback: Any) -> Any:
    return device.config.detection.get(name, fallback)


def band_of(device: IOSDevice, name: str, fallback: tuple[int, int]) -> tuple[float, float]:
    value = setting(device, name, list(fallback))
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(type(item) is int for item in value)
    ):
        raise LuckyTrashIOSError(f"detection.{name} must be [start, end] integers")
    return value[0] / 1000, value[1] / 1000


def probe(image: Image.Image, band: tuple[float, float]) -> tuple[float, float]:
    y0, y1 = band
    bright = green = 0.0
    for yi in range(PROBE_STEPS):
        y = int(image.height * (y0 + (y1 - y0) * yi / (PROBE_STEPS - 1)))
        for xi in range(PROBE_STEPS):
            x = int(image.width * (PROBE_X[0] + (PROBE_X[1] - PROBE_X[0]) * xi / (PROBE_STEPS - 1)))
            r, g, b = image.getpixel((x, y))
            bright += (r + g + b) / 3
            green += g - (r + b) / 2
    count = PROBE_STEPS**2
    return bright / count, green / count


def screen_of(device: IOSDevice, image: Image.Image) -> str:
    top_bright, top_green = probe(image, band_of(device, "TOP_BAND_Y", TOP_BAND_Y))
    mid_bright, _ = probe(image, band_of(device, "MID_BAND_Y", MID_BAND_Y))
    if top_green >= float(setting(device, "GREEN_ON", GREEN_ON)):
        return "confirm" if mid_bright >= float(setting(device, "MID_BRIGHT", MID_BRIGHT)) else "menu"
    if top_bright >= float(setting(device, "TOP_BRIGHT", TOP_BRIGHT)):
        return "list"
    if mid_bright >= float(setting(device, "MID_BRIGHT", MID_BRIGHT)):
        return "detail"
    return "unknown"


def band_bits(
    device: IOSDevice,
    image: Image.Image,
    x_span: tuple[float, float],
    band_name: str,
    fallback: tuple[int, int],
    nx: int,
    ny: int,
) -> list[bool]:
    y0, y1 = band_of(device, band_name, fallback)
    threshold = float(setting(device, "QUERY_INK", QUERY_INK))
    bits: list[bool] = []
    for yi in range(ny):
        y = int(image.height * (y0 + (y1 - y0) * yi / (ny - 1)))
        for xi in range(nx):
            x = int(image.width * (x_span[0] + (x_span[1] - x_span[0]) * xi / (nx - 1)))
            bits.append(sum(image.getpixel((x, y))) / 3 < threshold)
    return bits


def query_bits(device: IOSDevice, image: Image.Image) -> list[bool]:
    return band_bits(device, image, QUERY_X, "QUERY_BAND_Y", QUERY_BAND_Y, QUERY_NX, QUERY_NY)


def count_bits(device: IOSDevice, image: Image.Image) -> list[bool]:
    return band_bits(device, image, COUNT_X, "COUNT_BAND_Y", COUNT_BAND_Y, COUNT_NX, COUNT_NY)


def bit_match(left: list[bool], right: list[bool]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return sum(a == b for a, b in zip(left, right)) / len(left)


def point_ink(device: IOSDevice, image: Image.Image, point: list[int], radius: int) -> float:
    cx, cy = device.image_point(point, image)
    rx = max(1, int(radius * image.width / device.viewport["width"]))
    ry = max(1, int(radius * image.height / device.viewport["height"]))
    threshold = float(setting(device, "FILTER_CLEAR_INK", FILTER_CLEAR_INK))
    dark = total = 0
    for y in range(cy - ry, cy + ry + 1, 2):
        for x in range(cx - rx, cx + rx + 1, 2):
            if 0 <= x < image.width and 0 <= y < image.height:
                total += 1
                dark += sum(image.getpixel((x, y))) / 3 < threshold
    return dark / total if total else 0.0


def filter_signature(device: IOSDevice, image: Image.Image) -> tuple[list[bool], float, float]:
    bits = query_bits(device, image)
    ink = sum(bits) / len(bits)
    clear_ink = point_ink(
        device,
        image,
        device.coordinates["FILTER_CLEAR_BTN"],
        int(setting(device, "FILTER_CLEAR_RADIUS", FILTER_CLEAR_RADIUS)),
    )
    return bits, ink, clear_ink


def assert_filtered_list(device: IOSDevice, image: Image.Image) -> list[bool]:
    state = screen_of(device, image)
    if state != "list":
        raise LuckyTrashIOSError(
            f"Not on the Pokémon storage list (detected {state}). Open the filtered list and try again."
        )
    bits, ink, clear_ink = filter_signature(device, image)
    min_ink = float(setting(device, "QUERY_MIN_INK", QUERY_MIN_INK))
    clear_min = float(setting(device, "FILTER_CLEAR_MIN", FILTER_CLEAR_MIN))
    if ink < min_ink or clear_ink < clear_min:
        raise LuckyTrashIOSError(
            "The search box does not look safely filtered "
            f"(text ink {ink:.3f}/{min_ink:.3f}, clear-button ink {clear_ink:.3f}/{clear_min:.3f}). "
            "Refusing to transfer anything from an unfiltered list."
        )
    return bits


def tile_point(device: IOSDevice, slot: int) -> list[int]:
    x, y = device.coordinates["FIRST_TILE_BTN"]
    pitch_x, pitch_y = device.coordinates["TILE_PITCH"]
    return [x + pitch_x * (slot % TILE_COLS), y + pitch_y * (slot // TILE_COLS)]


def tile_ink(device: IOSDevice, image: Image.Image, slot: int) -> float:
    logical_x, logical_y = tile_point(device, slot)
    pitch_y = device.coordinates["TILE_PITCH"][1]
    logical_y += int(pitch_y * TILE_DROP)
    cx, cy = device.image_point((logical_x, logical_y), image)
    on = total = 0
    threshold = float(setting(device, "TILE_INK", TILE_INK))
    for yi in range(TILE_STEPS):
        y = int(cy + image.height * TILE_HALF_Y * (2 * yi / (TILE_STEPS - 1) - 1))
        for xi in range(TILE_STEPS):
            x = int(cx + image.width * TILE_HALF_X * (2 * xi / (TILE_STEPS - 1) - 1))
            if not (0 <= x < image.width and 0 <= y < image.height):
                continue
            total += 1
            on += sum(image.getpixel((x, y))) / 3 < threshold
    return on / total if total else 0.0


def dialog_pill(image: Image.Image) -> tuple[int, int] | None:
    rows: list[tuple[int, float]] = []
    for yi in range(DIALOG_STEPS):
        y = int(image.height * (DIALOG_Y[0] + (DIALOG_Y[1] - DIALOG_Y[0]) * yi / (DIALOG_STEPS - 1)))
        white = 0
        for xi in range(DIALOG_XSTEPS):
            x = int(image.width * (DIALOG_X[0] + (DIALOG_X[1] - DIALOG_X[0]) * xi / (DIALOG_XSTEPS - 1)))
            white += sum(image.getpixel((x, y))) / 3 >= DIALOG_WHITE
        rows.append((y, white / DIALOG_XSTEPS))

    card = [y for y, white in rows if white >= DIALOG_CARD]
    if not card:
        return None
    top, bottom = min(card), max(card)
    best: tuple[int, int] | None = None
    run: tuple[int, int] | None = None
    for y, white in rows:
        if top < y < bottom and white < DIALOG_PILL:
            run = (y, y) if run is None else (run[0], y)
            continue
        if run and (best is None or run[1] - run[0] > best[1] - best[0]):
            best = run
        run = None
    if run and (best is None or run[1] - run[0] > best[1] - best[0]):
        best = run
    if best is None or best[1] - best[0] < image.height * DIALOG_PILL_MIN:
        return None
    return best


def dialog_yes_point(device: IOSDevice, image: Image.Image) -> list[int] | None:
    pill = dialog_pill(image)
    if pill is None:
        return None
    center = ((image.width // 2), (pill[0] + pill[1]) // 2)
    return device.logical_point(center, image)


def dialog_no_point(device: IOSDevice, image: Image.Image) -> list[int]:
    pill = dialog_pill(image)
    if pill is None:
        return device.coordinates["TRANSFER_NO_BTN"]
    yes_y = (pill[0] + pill[1]) // 2
    no_y = yes_y + int((pill[1] - pill[0]) * NO_DROP)
    return device.logical_point((image.width // 2, no_y), image)


def read_screen(device: IOSDevice) -> tuple[str, Image.Image]:
    image = device.screenshot()
    return screen_of(device, image), image


def pause(device: IOSDevice, seconds: float, use_modifier: bool = True) -> None:
    modifier = device.config.delay_modifier if use_modifier else 0
    time.sleep(max(0, seconds + modifier))


def wait_for_states(
    device: IOSDevice,
    expected: set[str],
    timeout: float = STATE_TIMEOUT,
) -> tuple[str, Image.Image]:
    deadline = time.monotonic() + timeout
    last_state = "unreadable"
    last_image: Image.Image | None = None
    while time.monotonic() < deadline:
        last_state, last_image = read_screen(device)
        if last_state in expected:
            return last_state, last_image
        pause(device, POLL_DELAY, use_modifier=False)
    if last_image is None:
        last_image = device.screenshot()
    return last_state, last_image


def back_out(device: IOSDevice) -> bool:
    for _ in range(BACKOUT_STEPS):
        state, image = read_screen(device)
        if state == "list":
            return True
        if state == "confirm":
            device.tap(dialog_no_point(device, image))
        elif state == "menu":
            device.tap(device.coordinates["TRASH_MENU_BTN"])
        elif state == "detail":
            device.tap(device.coordinates["X_BTN"])
        else:
            return False
        pause(device, MENU_DELAY)
    return False


def reach_confirmation(device: IOSDevice, slot: int) -> Image.Image | None:
    state, _ = read_screen(device)
    if state != "list":
        raise LuckyTrashIOSError(f"Expected the storage list before slot {slot + 1}, saw {state}")
    device.tap(tile_point(device, slot))
    pause(device, DETAIL_DELAY)
    state, _ = wait_for_states(device, {"detail"})
    if state != "detail":
        print(f"  no detail screen after tapping slot {slot + 1} (saw {state})", flush=True)
        return None

    device.tap(device.coordinates["TRASH_MENU_BTN"])
    pause(device, MENU_DELAY)
    state, _ = wait_for_states(device, {"menu"})
    if state != "menu":
        print(f"  menu did not open (saw {state})", flush=True)
        return None

    device.tap(device.coordinates["TRANSFER_BTN"])
    pause(device, DIALOG_DELAY)
    state, image = wait_for_states(device, {"confirm"})
    if state != "confirm":
        print(f"  no transfer dialog (saw {state}) — not transferable", flush=True)
        return None
    return image


def transfer_one(device: IOSDevice, slot: int) -> Image.Image | None:
    image = reach_confirmation(device, slot)
    if image is None:
        return None

    # The only irreversible tap is below. It requires both a positively
    # classified confirmation screen and a freshly detected solid YES pill.
    for dialog_number in range(1, MAX_DIALOGS + 1):
        yes = dialog_yes_point(device, image)
        if yes is None:
            raise LuckyTrashIOSError(
                f"Confirmation {dialog_number} is visible but its YES pill could not be located. "
                "Stopped without tapping YES."
            )
        print(f"  confirmation {dialog_number}: YES at ({yes[0]}, {yes[1]})", flush=True)
        device.tap(yes)
        pause(device, TRANSFER_DELAY)
        state, image = wait_for_states(device, {"list", "confirm"})
        if state == "list":
            return image
        if state != "confirm":
            raise LuckyTrashIOSError(
                f"A YES was sent and the screen is now {state}, not the list. "
                "Stopped — check the phone before running again."
            )
    raise LuckyTrashIOSError(f"Still on a dialog after {MAX_DIALOGS} YES taps. Stopped.")


def wait_for_count_change(
    device: IOSDevice,
    before: list[bool],
    first_image: Image.Image,
) -> tuple[Image.Image, int]:
    deadline = time.monotonic() + COUNT_TIMEOUT
    image = first_image
    moved = 0
    while time.monotonic() < deadline:
        if screen_of(device, image) != "list":
            raise LuckyTrashIOSError("The Pokémon list disappeared while verifying the transfer")
        moved = sum(a != b for a, b in zip(before, count_bits(device, image)))
        if moved >= int(setting(device, "COUNT_MOVED", COUNT_MOVED)):
            return image, moved
        pause(device, POLL_DELAY, use_modifier=False)
        image = device.screenshot()
    return image, moved


def trash_process(device: IOSDevice, limit: int) -> int:
    image = device.screenshot()
    reference = assert_filtered_list(device, image)
    _, ink, clear_ink = filter_signature(device, image)
    print(
        f"Filter locked in (text ink {ink:.3f}, clear-button ink {clear_ink:.3f})",
        flush=True,
    )

    done = slot = base = 0
    total = "" if limit >= 10**6 else f" of {limit}"
    while done < limit:
        image = device.screenshot()
        if screen_of(device, image) != "list":
            raise LuckyTrashIOSError("Left the Pokémon storage list unexpectedly")
        now, ink, clear_ink = filter_signature(device, image)
        match = bit_match(reference, now)
        if match < float(setting(device, "QUERY_MATCH", QUERY_MATCH)):
            raise LuckyTrashIOSError(
                f"The search filter changed mid-run (band match {match:.3f}). "
                "Stopped without transferring anything else."
            )
        if ink < float(setting(device, "QUERY_MIN_INK", QUERY_MIN_INK)) or clear_ink < float(
            setting(device, "FILTER_CLEAR_MIN", FILTER_CLEAR_MIN)
        ):
            raise LuckyTrashIOSError("The search filter no longer looks populated. Stopped.")

        occupied = tile_ink(device, image, slot)
        if occupied < float(setting(device, "TILE_OCCUPIED", TILE_OCCUPIED)):
            if slot == base == 0:
                print("Filter is empty — nothing left to transfer", flush=True)
            else:
                print(f"Nothing left to transfer past slot {slot + 1}", flush=True)
            return done
        before = count_bits(device, image)

        print(f"{done + 1}{total}: slot {slot + 1} (tile ink {occupied:.3f})", flush=True)
        result = transfer_one(device, slot)
        if result is None:
            if not back_out(device):
                raise LuckyTrashIOSError(
                    "Could not get back to the Pokémon list. Stopped rather than tapping blind."
                )
            base = slot = slot + 1
            if slot >= TILE_COLS * TILE_ROWS:
                raise LuckyTrashIOSError(
                    f"Transferred {done}, then nothing in the first {slot} slots would transfer. "
                    "The filter may be down to favorites, buddies, or gym defenders."
                )
            continue

        image, moved = wait_for_count_change(device, before, result)
        if moved < int(setting(device, "COUNT_MOVED", COUNT_MOVED)):
            raise LuckyTrashIOSError(
                f"The count beside POKÉMON did not move enough ({moved} cells). "
                "Stopped because the transfer outcome is unclear."
            )
        done += 1
        slot = base
        print(f"  transferred ({moved} count cells changed)", flush=True)
    return done


def run_check(device: IOSDevice, screenshot_path: Path) -> None:
    image = device.screenshot()
    reference = assert_filtered_list(device, image)
    _, ink, clear_ink = filter_signature(device, image)
    occupied = tile_ink(device, image, 0)
    image.save(screenshot_path)
    print(
        f"Check passed: viewport {device.viewport['width']}x{device.viewport['height']}; "
        f"screenshot {image.width}x{image.height}; filter ink {ink:.3f}; "
        f"clear-button ink {clear_ink:.3f}; first tile ink {occupied:.3f}; "
        f"signature cells {len(reference)}"
    )
    print(f"Saved {screenshot_path}")


def run_rehearsal(device: IOSDevice, screenshot_path: Path) -> None:
    before_image = device.screenshot()
    reference = assert_filtered_list(device, before_image)
    before_count = count_bits(device, before_image)
    image = reach_confirmation(device, 0)
    if image is None:
        if not back_out(device):
            raise LuckyTrashIOSError("Rehearsal could not return to the storage list")
        raise LuckyTrashIOSError("Rehearsal did not reach a transfer confirmation")
    yes = dialog_yes_point(device, image)
    no = dialog_no_point(device, image)
    if yes is None:
        device.tap(no)
        back_out(device)
        raise LuckyTrashIOSError("Rehearsal saw a dialog but could not locate its YES pill")
    image.save(screenshot_path)
    print(f"Rehearsal found confirmation YES at {yes} and NO at {no}; tapping NO", flush=True)
    device.tap(no)
    pause(device, MENU_DELAY)
    if not back_out(device):
        raise LuckyTrashIOSError("Rehearsal could not safely return to the storage list")
    final = device.screenshot()
    if screen_of(device, final) != "list":
        raise LuckyTrashIOSError("Rehearsal did not finish on the storage list")
    if bit_match(reference, query_bits(device, final)) < float(setting(device, "QUERY_MATCH", QUERY_MATCH)):
        raise LuckyTrashIOSError("The filter changed during the rehearsal")
    moved = sum(a != b for a, b in zip(before_count, count_bits(device, final)))
    if moved:
        raise LuckyTrashIOSError(f"The Pokémon count changed during rehearsal ({moved} cells)")
    print(f"Rehearsal passed without deleting anything. Saved dialog screenshot {screenshot_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=config_paths.default_config("luckytrash-ios.yaml"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="Read-only list/filter validation")
    action.add_argument("--rehearse", action="store_true", help="Reach the dialog, tap NO, and return")
    action.add_argument("--count", type=int, help="Transfer exactly this many matches")
    action.add_argument("--all", action="store_true", help="Transfer matches until the filtered list is empty")
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Required with --count or --all because transfers cannot be undone",
    )
    parser.add_argument("--max-transfers", type=int, default=500, help="Safety cap for --all")
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check or args.rehearse:
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
    if args.count is not None and args.count < 1:
        raise LuckyTrashIOSError("--count must be at least 1")
    if args.max_transfers < 1:
        raise LuckyTrashIOSError("--max-transfers must be at least 1")
    destructive = args.count is not None or args.all
    if destructive and not args.confirm_delete:
        raise LuckyTrashIOSError(
            "Transfers cannot be undone. Re-run with --confirm-delete after checking the filter."
        )

    config = load_runtime_config(args.config)
    conflicts = active_conflicting_runners()
    conflicts = [pid for pid in conflicts if pid != os.getpid()]
    if conflicts:
        raise LuckyTrashIOSError(
            f"Another iOS Appium runner is active (PID {', '.join(map(str, conflicts))})"
        )
    check_appium_status(config.appium_server_url)

    with exclusive_runner_lock():
        device: IOSDevice | None = None
        try:
            device = IOSDevice.connect(config)
            if args.check:
                run_check(device, args.screenshot)
                return 0
            if args.rehearse:
                run_rehearsal(device, args.screenshot)
                return 0
            limit = args.count if args.count is not None else args.max_transfers
            assert limit is not None
            print(
                f"Starting irreversible transfer run ({'all matches' if args.all else limit}). "
                "Press Ctrl+C to cancel.",
                flush=True,
            )
            completed = trash_process(device, limit)
            print(f"Completed {completed} transfer(s)", flush=True)
            if args.all and completed >= args.max_transfers:
                print(f"Stopped at the --max-transfers safety cap ({args.max_transfers})", flush=True)
            return 0
        finally:
            if device is not None:
                device.quit()


if __name__ == "__main__":
    import os
    from . import fleet_entrypoint, pokemon_fleet
    try:
        if os.environ.get("POKEMON_FLEET_CHILD") == "1" or "--fleet-child" in sys.argv:
            raise SystemExit(main())
        raise SystemExit(fleet_entrypoint.run_operation('delete', 'luckytrash.py'))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
