#!/usr/bin/env python3
"""Host-side Pokémon GO gift automation for a developer-enabled iPhone.

This is the iOS counterpart to gift.py.  Appium/WebDriverAgent replaces ADB,
but the open-loop step sequence and screenshot guard remain the same.  Button
coordinates are iOS logical points, not screenshot pixels.  For example, the
current iPhone SE viewport is 375x667 points while screenshots are 750x1334.
"""

from __future__ import annotations

import argparse
import fcntl
import io
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from PIL import Image
from selenium.webdriver.common.actions import interaction
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput

from . import config_paths, ios_wda_cleanup, pokemon_fleet


DEFAULT_CONFIG = config_paths.default_config("ios-gifter.yaml")
ARTIFACT_ROOT = config_paths.state_dir()
DEFAULT_SCREENSHOT = ARTIFACT_ROOT / "gifts" / "ios-gifter-check.png"


@dataclass(frozen=True)
class Step:
    name: str
    delay_after: float
    use_delay_modifier: bool = False


GIFT_STEPS = (
    Step("OPEN_BTN", 3.0, True),
    Step("CLOSE_BTN", 2.0, True),
    Step("SEND_GIFT_BTN", 2.0, True),
    Step("FIRST_GIFT_BTN", 1.5),
    Step("SEND_BTN", 3.0, True),
    Step("FRIEND_CLOSE_BTN", 3.5, True),
    Step("SORT_BTN", 1.5),
    Step("CAN_RECEIVE_GIFT_BTN", 1.5),
    Step("SORT_BTN", 1.5),
    Step("CAN_RECEIVE_GIFT_BTN", 1.5),
    Step("NEXT_FRIEND_BTN", 2.0, True),
)

# The close that leaves a friend's screen for the friends list, as opposed to
# the one that dismisses a gift screen. It gets its own step name so the
# friend_close_scroll below can hang off it, and falls back to CLOSE_BTN, which
# is where the X sits on any phone whose friend screen fits in one viewport.
FALLBACK_KEYS = {"FRIEND_CLOSE_BTN": "CLOSE_BTN"}
GIFT_KEYS = ({step.name for step in GIFT_STEPS} - FALLBACK_KEYS.keys()) | {"GIFT_SORT_BTN"}
NEEDS_FRIENDS_LIST = {"SORT_BTN", "NEXT_FRIEND_BTN"}
NEEDS_FRIEND_SCREEN = {"OPEN_BTN"}

# The top header on Pokémon GO's friends list is bright and nearly greyscale.
GUARD_SAMPLE_X = (0.05, 0.95)
GUARD_SAMPLE_Y = (0.02, 0.07)
GUARD_SAMPLE_STEPS = 10
GUARD_MIN_BRIGHTNESS = 200
GUARD_MAX_SATURATION = 20
GUARD_RECOVER_ATTEMPTS = 3
GUARD_RECOVER_DELAY = 3
# A flung profile keeps moving after the drag returns; tap before it settles
# and the close X is still travelling under the finger.
SCROLL_SETTLE = 1.5
# Seconds a recovered screen gets to finish painting before anything samples it.
# The header flips the moment the old screen starts fading, so a recovery that
# stopped there handed the caller a half-drawn frame. Measured on the SE from
# one failed run: the friends list is a 423KB screenshot, the frame right after
# the recovery tap 51KB, and the gift card that follows 449KB. OPEN_BTN sampled
# on that 51KB frame reads grey, which is how a friend with a gift waiting was
# taken for one without.
SCREEN_SETTLE = 1.0
# A drag now and then does not register at all, which used to leave the close
# tap on the action row and open a trade lobby. friend_close_probe watches a
# spot the row occupies, so the scroll can be repeated until it is really clear.
CLOSE_SCROLL_ATTEMPTS = 3
# Seconds held still after the press and before the release, so the game sees a
# scroll starting rather than a tap landing.
DRAG_HOLD = 0.25
# How many looks a send gets to disable Send Gift before it counts as
# unconfirmed. Each look costs a screenshot plus the pause below.
SEND_CONFIRM_ATTEMPTS = 6
SEND_CONFIRM_PAUSE = 1
# Seconds to let a send's overlay clear before the close scroll starts.
PRE_SCROLL_SETTLE = 4
CLOSE_PROBE_MIN_BRIGHTNESS = 245
CLOSE_PROBE_MAX_SATURATION = 15
MAP_RECOVERY_KEYS = ("AVATAR_BTN", "FRIENDS_TAB_BTN")


class GifterError(RuntimeError):
    pass


def acquire_direct_device_lock(udid: str):
    """Share the fleet lock when this worker is invoked outside its parent."""
    if os.environ.get("POKEMON_FLEET_LOCK_HELD") == "1":
        return None
    lock_dir = Path("/tmp/pokemon-go-fleet")
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"{udid}.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise GifterError(
            f"Another automation session already owns iPhone {udid}; "
            "stopped before creating a second Appium session"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def release_direct_device_lock(handle) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise GifterError(f"Config not found: {path}") from exc
    if not isinstance(config, dict):
        raise GifterError("Config root must be a YAML object")
    return config


def device_config(config: dict[str, Any]) -> dict[str, Any]:
    device = config.get("device")
    if not isinstance(device, dict):
        raise GifterError("Config needs a device object")
    for key in ("udid", "team_id", "wda_bundle_id"):
        if not isinstance(device.get(key), str) or not device[key].strip():
            raise GifterError(f"Config device.{key} must be set")
    return device


def coordinates(config: dict[str, Any]) -> dict[str, list[int]]:
    points = config.get("coordinates")
    if not isinstance(points, dict):
        raise GifterError("Config needs a coordinates object")
    missing = sorted(GIFT_KEYS - points.keys())
    if missing:
        raise GifterError(f"Missing coordinates: {', '.join(missing)}")
    for name, point in points.items():
        if (
            not isinstance(point, list)
            or len(point) != 2
            or not all(isinstance(value, int) for value in point)
        ):
            raise GifterError(f"{name} must be [x, y] integer logical points")
        if point == [0, 0]:
            raise GifterError(f"{name} is still the [0, 0] calibration placeholder")
    return points


def wda_startup_guidance(exc: Exception, name: str) -> GifterError | None:
    """Translate the common real-device trust failure into phone-side steps."""
    message = str(exc).lower()
    certificate_untrusted = (
        "developer app certificate is not trusted" in message
        or ("xcodebuild" in message and "code 65" in message)
    )
    if not certificate_untrusted:
        return None
    return GifterError(
        f"{name} would not launch WebDriverAgent. On the iPhone, open "
        "Settings > General > VPN & Device Management, open the Apple "
        "Development profile, and tap Trust. Keep the phone unlocked, then "
        "run gift.py again."
    )


def connect(
    config: dict[str, Any], use_new_wda: bool = False
) -> "webdriver.Remote":
    try:
        from appium import webdriver
        from appium.options.ios import XCUITestOptions
    except ModuleNotFoundError as exc:
        raise GifterError(
            "Appium client is missing; install requirements-cross-platform.txt"
        ) from exc

    device = device_config(config)
    name = device.get("name", "iPhone")
    if (
        ios_wda_cleanup.display_backlight(device["udid"]) is False
        or ios_wda_cleanup.device_requires_unlock(device["udid"]) is True
    ):
        raise GifterError(
            f"{name} is locked or its screen is off. Wake and unlock the "
            "iPhone before starting automation; iOS will not launch "
            "WebDriverAgent while locked."
        )
    if not use_new_wda:
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
        "appium:useNewWDA": use_new_wda,
        "appium:showXcodeLog": use_new_wda,
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
    server_url = config.get("server_url", "http://127.0.0.1:4723")
    try:
        return webdriver.Remote(
            command_executor=server_url,
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
                return webdriver.Remote(
                    command_executor=server_url,
                    options=XCUITestOptions().load_capabilities(capabilities),
                )
            except Exception as retry_exc:
                exc = retry_exc
        guidance = wda_startup_guidance(exc, name)
        if guidance is not None:
            raise guidance from exc
        if capabilities.get("appium:usePreinstalledWDA", False):
            raise GifterError(
                f"{name} could not launch the preinstalled WebDriverAgent. "
                "It was not rebuilt because rebuilding on a different Mac "
                "would replace its signing identity."
            ) from exc
        raise exc


def close_driver(driver: webdriver.Remote) -> None:
    try:
        driver.quit()
    except Exception as exc:
        print(
            f"Warning: could not close iPhone gift automation session: {exc}",
            file=sys.stderr,
            flush=True,
        )
    else:
        print("iPhone gift automation session closed", flush=True)


def stale_wda_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        ("not authorized" in message and "ui testing actions" in message)
        or (
            "previously found element" in message
            and "not in current view" in message
        )
    )


def connect_ready(
    config: dict[str, Any],
) -> tuple[webdriver.Remote, dict[str, int]]:
    driver = connect(config)
    try:
        return driver, driver.get_window_rect()
    except Exception as exc:
        if not stale_wda_error(exc):
            close_driver(driver)
            raise

        print("iPhone target app is not active; activating Pokemon GO...", flush=True)
        try:
            driver.execute_script(
                "mobile: activateApp",
                {
                    "bundleId": device_config(config).get(
                        "bundle_id", "com.nianticlabs.pokemongo"
                    )
                },
            )
            return driver, driver.get_window_rect()
        except Exception as activation_exc:
            if not stale_wda_error(activation_exc):
                close_driver(driver)
                raise
            close_driver(driver)
            raise GifterError(
                "iPhone automation remained stale after activating Pokemon GO; "
                "stopped without reinstalling WebDriverAgent"
            ) from activation_exc


def tap(driver: webdriver.Remote, point: list[int]) -> None:
    driver.execute_script("mobile: tap", {"x": point[0], "y": point[1]})


def drag(driver: webdriver.Remote, path: list[int], duration: float = 0.5) -> None:
    """Scroll with a held, stepped gesture rather than one jump.

    "mobile: dragFromToForDuration" sends a single move, and the game's UI reads
    that as a click on whatever sits under the finger: dragging a friend profile
    with it opens a trade lobby instead of scrolling, which is how a cycle ends
    up stranded. Pressing, pausing, then moving in steps gives the scroll view
    the deltas it needs to claim the gesture. Measured on the iPhone SE: 12 of
    12 stepped drags scrolled cleanly, where the one-shot drag opened a lobby.
    """
    from_x, from_y, to_x, to_y = path
    steps = 8
    step_ms = max(1, int(duration * 1000 / steps))
    finger = PointerInput(interaction.POINTER_TOUCH, "finger")
    actions = ActionBuilder(driver, mouse=finger, duration=0)
    actions.pointer_action.move_to_location(from_x, from_y)
    actions.pointer_action.pointer_down()
    actions.pointer_action.pause(DRAG_HOLD)
    for step in range(1, steps + 1):
        actions.pointer_action.move_to_location(
            int(from_x + (to_x - from_x) * step / steps),
            int(from_y + (to_y - from_y) * step / steps),
        )
        actions.pointer_action.pause(step_ms / 1000)
    actions.pointer_action.pause(DRAG_HOLD)
    actions.pointer_action.pointer_up()
    actions.perform()


def friend_close_scroll(config: dict[str, Any]) -> list[int] | None:
    """Optional drag that clears a friend screen's X before the close.

    On a short viewport the friend screen's SEND GIFT / TRADE / BATTLE row
    scrolls over the close X, which is painted at a fixed screen point. The
    iPhone SE's viewport is 375x667 points and the profile needs about 740, so
    with the profile at the top the row sits across the X and the row wins the
    tap: measured there, the TRADE icon covers x 157-216, y 596-639 and
    CLOSE_BTN is [187, 630], dead centre of it, so the close opens a trade
    lobby. That is what leaves a cycle off the friends list before SORT_BTN.
    Scrolling the profile to its bottom moves the row off the X, which is
    still at the same CLOSE_BTN point, so no second coordinate is needed.

    Phones whose friend screen fits in one viewport leave this unset and tap
    the X where it already is.
    """
    value = config.get("friend_close_scroll")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 4
        or not all(isinstance(item, int) for item in value)
    ):
        raise GifterError(
            "friend_close_scroll must be [from_x, from_y, to_x, to_y] logical points"
        )
    return value


def friend_close_probe(config: dict[str, Any]) -> list[int] | None:
    """Optional point that reads blank once friend_close_scroll has landed.

    Put it on the part of the action row furthest from the X, so a registered
    scroll and a swallowed one read differently. On the SE the BATTLE icon at
    [303, 618] measures 134.6 brightness / 109.5 saturation with the profile at
    the top and 255.0 / 0.0 once it is scrolled to the bottom.
    """
    value = config.get("friend_close_probe")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, int) for item in value)
    ):
        raise GifterError("friend_close_probe must be [x, y] logical points")
    return value


def screenshot_image(driver: webdriver.Remote) -> Image.Image:
    return Image.open(io.BytesIO(driver.get_screenshot_as_png())).convert("RGB")


def point_stats(
    driver: webdriver.Remote,
    point: list[int],
    logical_radius: int = 8,
) -> tuple[float, float]:
    """Mean brightness and RGB channel spread around a logical point."""
    image = screenshot_image(driver)
    rect = driver.get_window_rect()
    scale_x = image.width / rect["width"]
    scale_y = image.height / rect["height"]
    center_x = int(point[0] * scale_x)
    center_y = int(point[1] * scale_y)
    radius_x = max(1, int(logical_radius * scale_x))
    radius_y = max(1, int(logical_radius * scale_y))
    brightnesses: list[float] = []
    spreads: list[int] = []
    for y in range(center_y - radius_y, center_y + radius_y + 1, 4):
        for x in range(center_x - radius_x, center_x + radius_x + 1, 4):
            r, g, b = image.getpixel((x, y))
            brightnesses.append((r + g + b) / 3)
            spreads.append(max(r, g, b) - min(r, g, b))
    return sum(brightnesses) / len(brightnesses), sum(spreads) / len(spreads)


def point_saturation(
    driver: webdriver.Remote,
    point: list[int],
    logical_radius: int = 8,
) -> float:
    """Colored buttons score high; a blank stretch of the sheet scores near zero."""
    return point_stats(driver, point, logical_radius)[1]


def colored_action_available(driver: webdriver.Remote, point: list[int]) -> bool:
    return point_saturation(driver, point) >= 35


def is_friends_list(driver: webdriver.Remote) -> bool:
    image = screenshot_image(driver)
    width, height = image.size
    x0, x1 = GUARD_SAMPLE_X
    y0, y1 = GUARD_SAMPLE_Y
    last = GUARD_SAMPLE_STEPS - 1
    brightness = 0.0
    saturation = 0.0
    for yi in range(GUARD_SAMPLE_STEPS):
        y = int(height * (y0 + (y1 - y0) * yi / last))
        for xi in range(GUARD_SAMPLE_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / last))
            r, g, b = image.getpixel((x, y))
            brightness += (r + g + b) / 3
            saturation += max(r, g, b) - min(r, g, b)
    count = GUARD_SAMPLE_STEPS**2
    return brightness / count >= GUARD_MIN_BRIGHTNESS and saturation / count <= GUARD_MAX_SATURATION


def wait_for_screen_state(
    driver: webdriver.Remote,
    want_list: bool,
    timeout: float = 6,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_friends_list(driver) == want_list:
            time.sleep(SCREEN_SETTLE)
            return True
        time.sleep(1)
    return False


def wait_for_send_confirmation(driver: webdriver.Remote, point: list[int]) -> bool:
    """Poll until Send Gift goes inactive, which is what confirms the send.

    The "Gift sent!" toast can still be up when the step delay expires, and a
    single sample taken then reads the control as active and fails a send that
    actually went through.
    """
    for attempt in range(SEND_CONFIRM_ATTEMPTS):
        if not colored_action_available(driver, point):
            return True
        if attempt + 1 < SEND_CONFIRM_ATTEMPTS:
            time.sleep(SEND_CONFIRM_PAUSE)
    return False


def wait_for_send_completion(driver: webdriver.Remote, point: list[int]) -> bool:
    """Wait for stronger confirmation that sending completed."""
    deadline = time.monotonic() + (SEND_CONFIRM_ATTEMPTS * SEND_CONFIRM_PAUSE)
    while time.monotonic() < deadline:
        if wait_for_send_confirmation(driver, point):
            if _debug_send_flow():
                print(" Send confirmation path: Send button deactivated", flush=True)
            return True
        time.sleep(SEND_CONFIRM_PAUSE)
    return False


def _debug_send_flow() -> bool:
    return os.environ.get("GIFT_IOS_FLOW_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def close_button_clear(driver: webdriver.Remote, probe: list[int]) -> bool:
    brightness, saturation = point_stats(driver, probe)
    return (
        brightness >= CLOSE_PROBE_MIN_BRIGHTNESS
        and saturation <= CLOSE_PROBE_MAX_SATURATION
    )


def scroll_to_close(
    driver: webdriver.Remote,
    scroll: list[int],
    probe: list[int] | None,
) -> bool:
    """Drag the friend profile until its action row is off the close X.

    WDA drops a gesture now and then, and a dropped one used to be invisible:
    the close tap went to the row underneath and opened a trade lobby. With a
    probe configured the drag is repeated until the spot actually reads blank.
    """
    # A send leaves an overlay up for a few seconds, and a drag under it is not
    # treated as a scroll: it reaches the action row and opens a trade lobby.
    time.sleep(PRE_SCROLL_SETTLE)
    for attempt in range(1, CLOSE_SCROLL_ATTEMPTS + 1):
        drag(driver, scroll)
        time.sleep(SCROLL_SETTLE)
        if probe is None:
            return True
        if close_button_clear(driver, probe):
            if attempt > 1:
                print(f"  scroll registered on attempt {attempt}", flush=True)
            return True
    print("  friend screen would not scroll clear of its close button", flush=True)
    return False


def ensure_screen(
    driver: webdriver.Remote,
    points: dict[str, list[int]],
    step_name: str,
    scroll: list[int] | None = None,
    probe: list[int] | None = None,
) -> None:
    want_list = step_name in NEEDS_FRIENDS_LIST
    if is_friends_list(driver) == want_list:
        return
    recovery_key = "CLOSE_BTN" if want_list else "NEXT_FRIEND_BTN"
    expected = "friends list" if want_list else "friend screen"
    print(f"Not on {expected} before {step_name}; attempting recovery")
    for attempt in range(1, GUARD_RECOVER_ATTEMPTS + 1):
        if want_list and scroll is not None:
            # The stranded screen is usually a friend profile whose action row
            # is still over the close X, which is how the cycle got here: that
            # tap lands on TRADE. Repeat the scroll so the recovery tap cannot
            # make the same mistake the failed close did.
            scroll_to_close(driver, scroll, probe)
        tap(driver, points[recovery_key])
        if wait_for_screen_state(driver, want_list):
            print(f"Recovered after {attempt} tap(s)")
            return
    if want_list and all(key in points for key in MAP_RECOVERY_KEYS):
        print("Close recovery did not find the list; trying Map → Avatar → Friends")
        tap(driver, points["AVATAR_BTN"])
        time.sleep(GUARD_RECOVER_DELAY)
        tap(driver, points["FRIENDS_TAB_BTN"])
        if wait_for_screen_state(driver, True):
            print("Recovered from the map")
            return
    raise GifterError(
        f"Could not reach the {expected} before {step_name}; stopped instead of tapping blind"
    )


def run_gifts(
    driver: webdriver.Remote,
    config: dict[str, Any],
    count: int,
    guard: bool,
    dry_run: bool,
    all_mode: bool = False,
) -> int:
    points = coordinates(config)
    scroll = friend_close_scroll(config)
    probe = friend_close_probe(config)
    delay_modifier = float(config.get("delay_modifier", 0))

    completed = 0
    idle_cycles = 0
    outgoing_available = True

    for gift_number in range(1, count + 1):
        label = str(gift_number) if all_mode else f"{gift_number}/{count}"
        print(f"Gift {label}", flush=True)

        opened = False
        sent = False
        skip_send = False

        for step in GIFT_STEPS:
            point_name = step.name
            if point_name not in points:
                point_name = FALLBACK_KEYS.get(point_name, point_name)

            if all_mode and not outgoing_available and step.name == "CAN_RECEIVE_GIFT_BTN":
                point_name = "GIFT_SORT_BTN"

            if (
                guard
                and not dry_run
                and step.name in NEEDS_FRIENDS_LIST | NEEDS_FRIEND_SCREEN
            ):
                ensure_screen(driver, points, step.name, scroll, probe)

            if all_mode and not dry_run and step.name == "OPEN_BTN":
                # Gift screen only exists if friend actually has incoming gift.
                opened = colored_action_available(driver, points[step.name])
                if not opened:
                    print("  No incoming gift on friend; continuing Send Gift", flush=True)
                    continue

            if all_mode and not dry_run and step.name == "CLOSE_BTN" and not opened:
                print("  No gift screen dismiss; skipping close", flush=True)
                continue

            if all_mode and not dry_run and step.name == "SEND_GIFT_BTN":
                send_gift_btn_active = colored_action_available(driver, points[step.name])
                if not send_gift_btn_active:
                    outgoing_available = False
                    skip_send = True

            if all_mode and step.name == "FIRST_GIFT_BTN" and skip_send:
                continue

            if scroll is not None and all_mode and not dry_run and step.name == "FRIEND_CLOSE_BTN":
                print(f"  scrolling the friend screen clear of its close button: {scroll}", flush=True)
                if not scroll_to_close(driver, scroll, probe):
                    # Tapping now would hit action row and open trade lobby; guard cannot recover.
                    raise GifterError(
                        "Friend screen did not scroll clear close button; "
                        "stopped instead of tapping the action row"
                    )

            if step.name == "SEND_BTN" and not dry_run:
                if skip_send:
                    print(
                        "  Skipping Send action for this friend due prior unconfirmed send",
                        flush=True,
                    )
                else:
                    send_btn_active = colored_action_available(driver, points[step.name])
                    if not send_btn_active:
                        print(
                            "  Send button is not visibly active; attempting send anyway",
                            flush=True,
                        )

                    x, y = points["SEND_BTN"]
                    print(f"  SEND_BTN: ({x}, {y})", flush=True)

                    if _debug_send_flow():
                        send_btn_saturation = point_saturation(driver, points[step.name])
                        send_gift_saturation = point_saturation(
                            driver, points["SEND_GIFT_BTN"]
                        )
                        print(
                            f" Send BTN pre: sat={send_btn_saturation:.1f}, "
                            f"send-GIFT sat={send_gift_saturation:.1f}",
                            flush=True,
                        )

                    tap(driver, points[step.name])
                    send_confirmed = wait_for_send_completion(driver, points["SEND_BTN"])

                    if not send_confirmed:
                        if _debug_send_flow():
                            send_btn_saturation = point_saturation(driver, points[step.name])
                            send_gift_saturation = point_saturation(
                                driver, points["SEND_GIFT_BTN"]
                            )
                            print(
                                f" Send BTN post: sat={send_btn_saturation:.1f}, "
                                f"send-GIFT sat={send_gift_saturation:.1f}",
                                flush=True,
                            )

                        if _debug_send_flow():
                            print(
                                " Send confirmation did not arrive; retrying SEND_BTN once",
                                flush=True,
                            )

                        # Retry one light-touch tap on SEND_BTN.
                        tap(driver, points["SEND_BTN"])
                        send_confirmed = wait_for_send_completion(
                            driver, points["SEND_BTN"]
                        )

                    if send_confirmed:
                        sent = True
                    elif all_mode:
                        print(
                            "  Send action still not confirmed; continuing as non-fatal",
                            flush=True,
                        )
                        skip_send = True
                    else:
                        raise GifterError(
                            "Gift send was not confirmed; Send Gift is still active. "
                            "Stopped before reporting a false success."
                        )

                delay = step.delay_after + (delay_modifier if step.use_delay_modifier else 0)
                time.sleep(max(delay, 0))
                continue

            x, y = points[point_name]
            print(f"  {point_name}: ({x}, {y})", flush=True)
            if not dry_run:
                tap(driver, points[point_name])

            if step.name == "SEND_GIFT_BTN":
                # Give gift grid one frame to appear before first gift tap.
                time.sleep(0.1)

            delay = step.delay_after + (delay_modifier if step.use_delay_modifier else 0)
            time.sleep(max(delay, 0))

        if opened or sent:
            idle_cycles = 0
            completed += 1
        else:
            idle_cycles += 1
            print(f"  No action completed ({idle_cycles}/15 idle cycles)", flush=True)
            if idle_cycles >= 15:
                print("  Stopping after 15 idle cycles", flush=True)
                return completed

        if not outgoing_available:
            print("  Stopping: no selectable outgoing gift remains", flush=True)
            return completed

    return completed

def run_gifts_v3(
    driver: webdriver.Remote,
    config: dict[str, Any],
    count: int,
    guard: bool,
    dry_run: bool,
    all_mode: bool = False,
) -> int:
    """Run the gift flow with explicit, verified screen transitions.

    OPEN can return directly to the friend profile (notably when the daily
    open limit has been reached).  On the short iPhone viewport, tapping
    CLOSE_BTN from that state hits TRADE.  Every close and send below is
    therefore gated on the screen that is actually visible.
    """
    points = coordinates(config)
    scroll = friend_close_scroll(config)
    probe = friend_close_probe(config)
    delay_modifier = float(config.get("delay_modifier", 0))
    send_min_saturation = float(config.get("send_button_min_saturation", 60))
    completed = 0
    outgoing_available = True

    def pause(seconds: float, modified: bool = False) -> None:
        extra = delay_modifier if modified else 0
        time.sleep(max(seconds + extra, 0))

    def log_tap(name: str) -> None:
        x, y = points[name]
        print(f"  {name}: ({x}, {y})", flush=True)
        if not dry_run:
            tap(driver, points[name])

    def friend_profile_visible() -> bool:
        if is_friends_list(driver):
            return False
        # OPEN_BTN is a colored control on an incoming-gift card and lies on
        # the SEND control after a gift is selected.  It is blank on the
        # friend profile, whether SEND GIFT is active or already greyed out.
        return point_saturation(driver, points["OPEN_BTN"]) < 35

    def selected_gift_visible() -> tuple[bool, float]:
        saturation = point_saturation(driver, points["SEND_BTN"])
        return saturation >= send_min_saturation, saturation

    def sent_is_confirmed() -> bool:
        for attempt in range(SEND_CONFIRM_ATTEMPTS):
            if friend_profile_visible() and not colored_action_available(
                driver, points["SEND_GIFT_BTN"]
            ):
                return True
            if attempt + 1 < SEND_CONFIRM_ATTEMPTS:
                time.sleep(SEND_CONFIRM_PAUSE)
        return False

    def open_incoming_gift() -> bool:
        """Open a waiting gift and land on the friend profile; False if none."""
        if not colored_action_available(driver, points["OPEN_BTN"]):
            return False
        log_tap("OPEN_BTN")
        pause(3, modified=True)
        if friend_profile_visible():
            print(
                "  OPEN returned to the friend profile; skipping CLOSE_BTN",
                flush=True,
            )
        else:
            print("  Incoming-gift screen still open; closing it", flush=True)
            log_tap("CLOSE_BTN")
            pause(2, modified=True)
            if not friend_profile_visible():
                raise GifterError(
                    "OPEN did not reach the friend profile; stopped before "
                    "tapping Send Gift"
                )
        return True

    for gift_number in range(1, count + 1):
        label = str(gift_number) if all_mode else f"{gift_number}/{count}"
        print(f"Gift {label}", flush=True)
        opened = False
        sent = False

        if guard and not dry_run:
            ensure_screen(driver, points, "OPEN_BTN", scroll, probe)

        if dry_run:
            for name in (
                "OPEN_BTN",
                "SEND_GIFT_BTN",
                "FIRST_GIFT_BTN",
                "SEND_BTN",
                "FRIEND_CLOSE_BTN",
                "SORT_BTN",
                "CAN_RECEIVE_GIFT_BTN",
                "SORT_BTN",
                "CAN_RECEIVE_GIFT_BTN",
                "NEXT_FRIEND_BTN",
            ):
                log_tap(FALLBACK_KEYS.get(name, name))
            completed += 1
            continue

        opened = open_incoming_gift()
        if not opened:
            print("  No incoming gift to open; continuing Send Gift", flush=True)
            if not friend_profile_visible():
                # The card was still painting when OPEN_BTN was sampled, so it
                # read grey. The screen has settled by the time we get here:
                # open it rather than refuse a friend who does have one waiting.
                print("  Incoming gift painted late; opening it now", flush=True)
                opened = open_incoming_gift()

        if not friend_profile_visible():
            raise GifterError(
                "Not on the friend profile before Send Gift; stopped without "
                "tapping another control"
            )

        if colored_action_available(driver, points["SEND_GIFT_BTN"]):
            log_tap("SEND_GIFT_BTN")
            pause(2, modified=True)

            log_tap("FIRST_GIFT_BTN")
            pause(1.5)

            send_active, send_saturation = selected_gift_visible()
            if not send_active:
                outgoing_available = False
                print(
                    "  No selected outgoing gift: "
                    f"SEND_BTN saturation {send_saturation:.1f} "
                    f"(< {send_min_saturation:.1f})",
                    flush=True,
                )
                # On the picker/detail screen this coordinate is the real X.
                log_tap("CLOSE_BTN")
                pause(2, modified=True)
                if not friend_profile_visible():
                    raise GifterError(
                        "Could not leave the outgoing-gift picker after no gift "
                        "was selected"
                    )
                print("Stopping: no selectable outgoing gift remains", flush=True)
            else:
                print(
                    f"  SEND_BTN active (saturation {send_saturation:.1f})",
                    flush=True,
                )
                log_tap("SEND_BTN")
                if not sent_is_confirmed():
                    retry_active, retry_saturation = selected_gift_visible()
                    if retry_active:
                        print(
                            "  Send not yet confirmed; retrying the still-visible "
                            f"SEND_BTN (saturation {retry_saturation:.1f})",
                            flush=True,
                        )
                        log_tap("SEND_BTN")
                    if not sent_is_confirmed():
                        raise GifterError(
                            "Gift send was not confirmed; stopped without "
                            "reporting a false send"
                        )
                sent = True
                print("  Gift send confirmed", flush=True)
        else:
            print("  Friend cannot receive a gift; advancing", flush=True)

        if scroll is not None:
            print(
                f"  scrolling the friend screen clear of its close button: {scroll}",
                flush=True,
            )
            if not scroll_to_close(driver, scroll, probe):
                raise GifterError(
                    "Friend screen did not scroll clear of its close button; "
                    "stopped instead of tapping the action row"
                )
        log_tap("CLOSE_BTN")
        pause(2, modified=True)
        if not wait_for_screen_state(driver, True):
            raise GifterError(
                "Friend close did not return to the friends list; stopped before "
                "sorting"
            )

        completed += 1
        if not outgoing_available:
            return completed
        if not sent and not colored_action_available(
            driver, points["SORT_BTN"]
        ):
            raise GifterError("Friends list controls are not active after closing friend")

        for name in (
            "SORT_BTN",
            "CAN_RECEIVE_GIFT_BTN",
            "SORT_BTN",
            "CAN_RECEIVE_GIFT_BTN",
            "NEXT_FRIEND_BTN",
        ):
            log_tap(name)
            pause(1.5 if name != "NEXT_FRIEND_BTN" else 2, modified=name == "NEXT_FRIEND_BTN")

        if opened or sent:
            continue

    return completed


def main() -> int:
    def parse_args() -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--count", type=int)
        group.add_argument("--all", action="store_true")
        parser.add_argument("--max-cycles", type=int, default=100, help="Safety cap for --all")
        parser.add_argument("--check", action="store_true", help="Connect and save screenshot only")
        parser.add_argument("--screenshot", type=Path, default=DEFAULT_SCREENSHOT)
        parser.add_argument("--dry-run", action="store_true", help="Print configured taps without tapping")
        parser.add_argument("--no-guard", action="store_true")
        args = parser.parse_args()
        if args.count is None and not args.all and not args.check:
            args.all = True
        return args

    args = parse_args()
    if args.count is not None and args.count < 1:
        raise GifterError("--count must be positive")
    if args.max_cycles < 1:
        raise GifterError("--max-cycles must be positive")
    config = load_config(args.config)
    device = device_config(config)
    lock_handle = acquire_direct_device_lock(device["udid"])
    driver: webdriver.Remote | None = None
    try:
        driver, rect = connect_ready(config)
        image = screenshot_image(driver)
        print(
            f"Connected: viewport {rect['width']}x{rect['height']} points; "
            f"screenshot {image.width}x{image.height} pixels"
        )
        if args.check:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            image.save(args.screenshot)
            print(f"Saved {args.screenshot}")
            return 0
        if args.count is None and not args.all:
            raise GifterError("Use --count N, --all, or --check")
        requested = args.max_cycles if args.all else args.count
        completed = run_gifts_v3(
            driver,
            config,
            requested,
            not args.no_guard,
            args.dry_run,
            all_mode=args.all,
        )
        print(f"Completed {completed} gift cycle(s)", flush=True)
        return 0
    finally:
        try:
            if driver is not None:
                close_driver(driver)
        finally:
            try:
                ios_wda_cleanup.stop_wda_runner(device["udid"])
            finally:
                release_direct_device_lock(lock_handle)


if __name__ == "__main__":
    import os
    from . import fleet_entrypoint, pokemon_fleet
    try:
        if os.environ.get("POKEMON_FLEET_CHILD") == "1" or "--fleet-child" in sys.argv:
            raise SystemExit(main())
        raise SystemExit(fleet_entrypoint.run_operation('gifts', 'send_gifts.py'))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
