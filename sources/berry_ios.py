#!/usr/bin/env python3
"""Guarded Pokemon GO gym berry feeding for one Appium-controlled iPhone.

Start on a defender's berry-feeding screen.  The implementation shares the
Android detector thresholds and pure image-analysis functions from ``berry.py``
but converts between Appium logical points and screenshot pixels.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import io
from pathlib import Path
import random
import subprocess
import sys
from typing import Any, Iterator

import yaml
from PIL import Image

from . import berry_android as android_berry
from . import config_paths, ios_wda_cleanup


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = config_paths.default_config("berry-ios.yaml")
ARTIFACT_ROOT = config_paths.state_dir() / "berries"
DEFAULT_CHECK_SCREENSHOT = ARTIFACT_ROOT / "ios-berry-check.png"
LOCK_DIR = Path("/tmp/pokemon-go-fleet")
REQUIRED_COORDINATES = {"BERRY_BTN", "NEXT_MON_FROM", "NEXT_MON_TO"}
REQUIRED_BANDS = {"CARD_BAND_Y", "BERRY_DISC_Y"}


class IOSBerryError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    appium_server_url: str
    ios_device: dict[str, Any]
    coordinates: dict[str, list[int]]
    detection: dict[str, list[int]]
    delay_modifier: float
    spend_cap: int | None


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise IOSBerryError(f"Config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise IOSBerryError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IOSBerryError(f"Config root must be a YAML object: {path}")
    return value


def resolve_path(value: Any, base: Path, label: str) -> Path:
    try:
        return config_paths.find_config(value, base, label)
    except config_paths.ConfigPathError as exc:
        raise IOSBerryError(str(exc)) from exc


def validate_point(name: str, value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 2 or not all(type(v) is int for v in value):
        raise IOSBerryError(f"{name} must be [x, y] integer logical points")
    if value == [0, 0]:
        raise IOSBerryError(f"{name} is still the [0, 0] calibration placeholder")
    return value


def load_runtime_config(path: Path) -> RuntimeConfig:
    root = load_yaml(path)
    appium_path = resolve_path(root.get("appium_config"), path.resolve().parent, "appium_config")
    appium = load_yaml(appium_path)
    device = appium.get("device")
    if not isinstance(device, dict):
        raise IOSBerryError(f"{appium_path} needs a device object")
    for key in ("udid", "team_id", "wda_bundle_id"):
        if not isinstance(device.get(key), str) or not device[key].strip():
            raise IOSBerryError(f"{appium_path}: device.{key} must be set")

    raw_points = root.get("coordinates")
    if not isinstance(raw_points, dict):
        raise IOSBerryError("Berry config needs coordinates")
    points = {key: validate_point(key, raw_points.get(key)) for key in REQUIRED_COORDINATES}

    raw_detection = root.get("detection")
    if not isinstance(raw_detection, dict):
        raise IOSBerryError("Berry config needs detection bands")
    detection: dict[str, list[int]] = {}
    for key in REQUIRED_BANDS:
        band = raw_detection.get(key)
        if (
            not isinstance(band, list)
            or len(band) != 2
            or not all(type(v) is int for v in band)
            or not 0 <= band[0] < band[1] <= 1000
        ):
            raise IOSBerryError(f"detection.{key} must be [start, end] per-mille")
        detection[key] = band

    spend_cap = root.get("spend_cap")
    if spend_cap is not None and (type(spend_cap) is not int or spend_cap < 1):
        raise IOSBerryError("spend_cap must be a positive integer or null")
    delay_modifier = root.get("delay_modifier", 0)
    if not isinstance(delay_modifier, (int, float)):
        raise IOSBerryError("delay_modifier must be numeric")
    server_url = appium.get("server_url", "http://127.0.0.1:4723")
    if not isinstance(server_url, str):
        raise IOSBerryError("Appium server_url must be a string")
    return RuntimeConfig(
        appium_server_url=server_url.rstrip("/"),
        ios_device=device,
        coordinates=points,
        detection=detection,
        delay_modifier=float(delay_modifier),
        spend_cap=spend_cap,
    )


def stale_wda_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        ("not authorized" in message and "ui testing actions" in message)
        or (
            "previously found element" in message
            and "not in current view" in message
        )
    )


class IOSBerryDevice:
    def __init__(self, driver: Any, config: RuntimeConfig) -> None:
        self.driver = driver
        self.config = config
        self.label = str(config.ios_device.get("name", "iPhone"))
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
                raise IOSBerryError(
                    f"iPhone target app ({bundle_id}) is not active and could not be restored: {activation_exc}"
                ) from activation_exc

    @classmethod
    def connect(cls, config: RuntimeConfig) -> "IOSBerryDevice":
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
        except ModuleNotFoundError as exc:
            raise IOSBerryError("The Appium Python client is missing") from exc
        device = config.ios_device
        # See the note in gbl_ios.connect: a detached WDA answers /status and
        # then fails every tap, so it has to go before the session opens.
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
                    raise IOSBerryError(
                        f"Could not start Appium session for {device['udid']}: {retry_exc}"
                    ) from retry_exc
            else:
                raise IOSBerryError(f"Could not start Appium session for {device['udid']}: {exc}") from exc
        return cls(driver, config)

    def _screenshot(self) -> Image.Image:
        try:
            return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")
        except Exception as exc:
            if not stale_wda_error(exc):
                raise
            bundle_id = self.config.ios_device.get("bundle_id", "com.nianticlabs.pokemongo")
            try:
                self.driver.execute_script("mobile: activateApp", {"bundleId": bundle_id})
                return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")
            except Exception:
                raise exc

    async def screenshot(self) -> Image.Image:
        return await asyncio.to_thread(self._screenshot)

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

    async def tap(self, point: list[int]) -> None:
        await asyncio.to_thread(self.driver.execute_script, "mobile: tap", {"x": point[0], "y": point[1]})

    async def swipe(
        self,
        start: list[int],
        end: list[int],
        *,
        duration: float | None = None,
        native: bool = False,
    ) -> None:
        if native:
            direction = "left" if start[0] > end[0] else "right"
            await asyncio.to_thread(
                self.driver.execute_script,
                "mobile: swipe",
                {"direction": direction},
            )
            return
        await asyncio.to_thread(
            self.driver.execute_script,
            "mobile: dragFromToForDuration",
            {
                "duration": duration or android_berry.SWIPE_MS / 1000,
                "fromX": start[0],
                "fromY": start[1],
                "toX": end[0],
                "toY": end[1],
            },
        )

    async def quit(self) -> None:
        try:
            await asyncio.to_thread(self.driver.quit)
        except Exception:
            pass


def log(device: IOSBerryDevice, *parts: Any) -> None:
    print(f"[{device.label}]", *parts, flush=True)


async def wait(device: IOSBerryDevice, seconds: float, use_modifier: bool = True) -> None:
    delay = seconds + (device.config.delay_modifier if use_modifier else 0)
    await asyncio.sleep(max(delay, 0))


def raw_frame(image: Image.Image) -> tuple[int, int, int, bytes]:
    rgba = image.convert("RGBA")
    return rgba.width, rgba.height, 0, rgba.tobytes()


def band(device: IOSBerryDevice, key: str) -> tuple[float, float]:
    start, end = device.config.detection[key]
    return start / 1000, end / 1000


def frame_metrics(device: IOSBerryDevice, image: Image.Image) -> tuple[float, int, bool]:
    frame = raw_frame(image)
    width, height, offset, data = frame
    x0, x1 = android_berry.BERRY_DISC_X
    y0, y1 = band(device, "BERRY_DISC_Y")
    total = 0.0
    for yi in range(android_berry.BERRY_DISC_STEPS):
        y = int(height * (y0 + (y1 - y0) * yi / (android_berry.BERRY_DISC_STEPS - 1)))
        for xi in range(android_berry.BERRY_DISC_STEPS):
            x = int(width * (x0 + (x1 - x0) * xi / (android_berry.BERRY_DISC_STEPS - 1)))
            index = offset + (y * width + x) * 4
            total += (data[index] + data[index + 1] + data[index + 2]) / 3
    disc = total / android_berry.BERRY_DISC_STEPS ** 2
    ink = sum(android_berry.band_bits(*frame, band(device, "CARD_BAND_Y")))
    matched = (
        android_berry.BERRY_DISC_RANGE[0] <= disc <= android_berry.BERRY_DISC_RANGE[1]
        and android_berry.NAME_INK_RANGE[0] <= ink <= android_berry.NAME_INK_RANGE[1]
    )
    return disc, ink, matched


async def card_fingerprint(device: IOSBerryDevice) -> list[bool] | None:
    image = await device.screenshot()
    return android_berry.band_bits(*raw_frame(image), band(device, "CARD_BAND_Y"))


async def berry_gold_share(device: IOSBerryDevice) -> float | None:
    image = await device.screenshot()
    point = device.image_point(device.config.coordinates["BERRY_BTN"], image)
    return android_berry.gold_share_of(*raw_frame(image), point)


def picker_disc_point(device: IOSBerryDevice, image: Image.Image) -> list[int]:
    x0, x1 = android_berry.BERRY_DISC_X
    _, y1 = device.config.detection["BERRY_DISC_Y"]
    pixel = [int(image.width * (x0 + x1) / 2), int(image.height * y1 / 1000)]
    return device.logical_point(pixel, image)


def logical_picker_items(
    device: IOSBerryDevice,
    image: Image.Image,
    listed: list[tuple[str, list[int]]],
) -> list[tuple[str, list[int]]]:
    return [(kind, device.logical_point(point, image)) for kind, point in listed]


async def open_berry_picker(
    device: IOSBerryDevice,
) -> tuple[list[tuple[str, list[int]]] | None, list[tuple[str, list[int]]] | None, tuple | None]:
    """Opens the berry picker and reads it.

    Returns the berries in logical points for tapping, the same berries in
    screenshot pixels, and the frame they were read off. The last two are for
    the count badges, which are measured in pixels and are only on screen while
    the sheet is: once a berry has been tapped the sheet is gone. None for the
    list means the picker will not open.

    The disc is a toggle, so a sheet that is up but not yet readable is waited
    out rather than tapped -- tapping it again shuts it, and the picker then
    reports itself unavailable, which ends the defender silently. See the
    fuller note on berry_android.open_berry_picker.
    """
    sheet_seen = False
    for _ in range(android_berry.PICKER_OPEN_TRIES):
        image = await device.screenshot()
        frame = raw_frame(image)
        listed = android_berry.picker_read(frame)
        if listed is not None:
            return logical_picker_items(device, image, listed), listed, frame
        if android_berry.picker_sheet_top(*frame) is not None:
            sheet_seen = True
            await wait(device, android_berry.PICKER_SETTLE, use_modifier=False)
            continue
        await device.tap(picker_disc_point(device, image))
        await wait(device, android_berry.PICKER_SETTLE, use_modifier=False)
    log(
        device,
        f"Berry picker did not open in {android_berry.PICKER_OPEN_TRIES} tries"
        + (
            " -- a sheet came up but could not be read"
            if sheet_seen
            else " -- the item disc is greyed out"
        ),
    )
    return None, None, None


async def select_cheap_berry(device: IOSBerryDevice) -> tuple[str | None, int]:
    """The cheapest berry left, and how many feeds it is good for.

    See berry_android.select_cheap_berry; the second value is the picker
    recheck window, widened when the count badge shows a double-digit stock.
    """
    listed, pixels, frame = await open_berry_picker(device)
    if listed is None:
        return android_berry.PICKER_UNAVAILABLE, android_berry.PICKER_RECHECK
    cheap = [
        (item, pixel)
        for item, pixel in zip(listed, pixels)
        if item[0] in android_berry.CHEAP_BERRIES
    ]
    if not cheap:
        log(
            device,
            "Only premium berries remain: " + ", ".join(kind for kind, _ in listed),
        )
        return None, android_berry.PICKER_RECHECK
    (kind, point), (_, pixel) = cheap[0]
    recheck = android_berry.picker_recheck_for(frame, pixel)
    await device.tap(point)
    await wait(device, android_berry.PICKER_SETTLE, use_modifier=False)
    image = await device.screenshot()
    if android_berry.picker_read(raw_frame(image)) is not None:
        raise IOSBerryError(
            f"Berry picker stayed open after tapping {kind} at {point}; stopped without feeding"
        )
    return kind, recheck


async def berry_offered(
    device: IOSBerryDevice,
) -> tuple[list[int], Image.Image, float | None] | None:
    best = 0.0
    for attempt in range(android_berry.BERRY_OFFER_READS):
        image = await device.screenshot()
        point = device.image_point(device.config.coordinates["BERRY_BTN"], image)
        frame = raw_frame(image)
        found, share = android_berry.find_berry_share(*frame, point)
        best = max(best, share)
        if found is not None:
            return found, image, android_berry.gold_share_of(*frame, point)
        if attempt < android_berry.BERRY_OFFER_READS - 1:
            await wait(
                device,
                android_berry.BERRY_RESPAWN
                + random.uniform(0, android_berry.BERRY_RESPAWN_JITTER),
                use_modifier=False,
            )
    log(
        device,
        f"No berry after {android_berry.BERRY_OFFER_READS} reads "
        f"(best {best:.3f} of {android_berry.BERRY_PRESENT} needed)",
    )
    return None


async def feed_berry(device: IOSBerryDevice, found: list[int], image: Image.Image) -> None:
    anchor = device.image_point(device.config.coordinates["BERRY_BTN"], image)
    span = image.height * android_berry.BERRY_FIND_HALF_Y / 2
    for delta in (-1, 0, 1):
        logical = device.logical_point([found[0], int(anchor[1] + span * delta)], image)
        await device.tap(logical)


async def turn_card(device: IOSBerryDevice, reverse: bool = False) -> bool:
    before = await card_fingerprint(device)
    start = device.config.coordinates["NEXT_MON_FROM"]
    end = device.config.coordinates["NEXT_MON_TO"]
    if reverse:
        start, end = end, start
    profiles = ((True, None), (False, 0.30), (False, android_berry.SWIPE_MS / 1000))
    for attempt, (native, duration) in enumerate(profiles, 1):
        await device.swipe(
            start,
            end,
            native=native,
            duration=duration,
        )
        await wait(device, android_berry.SWIPE_SETTLE, use_modifier=False)
        after = await card_fingerprint(device)
        if before is None or after is None:
            return True
        if not android_berry.cards_match(before, after):
            return True
        if attempt < android_berry.SWIPE_RETRIES:
            log(device, f"Card unchanged, retrying swipe ({attempt}/{android_berry.SWIPE_RETRIES})")
    return False


async def feed_defender(
    device: IOSBerryDevice,
    chosen: str | None,
    budget: int | None,
) -> tuple[int, str | None, str | None]:
    fed = 0
    refused = False
    stop: str | None = None
    recheck = android_berry.PICKER_RECHECK
    since_check = recheck
    blind_after_picker = 0
    for _ in range(android_berry.FEED_RUNAWAY):
        if budget is not None and fed >= budget:
            stop = "cap"
            break
        offer = await berry_offered(device)
        if offer is None:
            refused = True
            break
        if chosen is None or since_check >= recheck:
            previous = chosen
            chosen, recheck = await select_cheap_berry(device)
            since_check = 0
            if chosen == android_berry.PICKER_UNAVAILABLE:
                # Transient, not finished: the disc greys out for a moment
                # after every feed, and the read at the top of this loop just
                # said a berry is on offer. See the note in
                # berry_android.feed_defender.
                chosen = previous
                since_check = recheck
                blind_after_picker += 1
                if blind_after_picker >= android_berry.PICKER_BLIND_LIMIT:
                    refused = True
                    break
                log(device, "Item disc still greyed out after feeding;"
                            " waiting for the animation and looking again")
                await wait(device, android_berry.PICKER_SETTLE, use_modifier=False)
                continue
            if chosen is None:
                stop = "premium"
                break
            if chosen != previous:
                suffix = "" if previous is None else f" -- {previous} ran out"
                log(device, f"Feeding {chosen} berries{suffix}")
            # A miss as the sheet slides back down is not a refusal; the read
            # at the top of the loop is the one that means that. See the note
            # in berry_android.feed_defender.
            offer = await berry_offered(device)
            if offer is None:
                blind_after_picker += 1
                if blind_after_picker >= android_berry.PICKER_BLIND_LIMIT:
                    refused = True
                    break
                log(device, "No berry readable as the picker closed; looking again")
                continue
            # A round trip that named a berry and found one clears the tally.
            blind_after_picker = 0
        found, image, _ = offer
        await feed_berry(device, found, image)
        fed += 1
        since_check += 1
        await wait(device, android_berry.BERRY_TAP_GAP, use_modifier=False)
    if not refused and stop is None:
        log(
            device,
            f"WARNING: hit {android_berry.FEED_RUNAWAY}-tap cap with berry still visible; "
            "defender is NOT finished",
        )
    await wait(device, android_berry.FEED_SETTLE, use_modifier=True)
    return fed, stop, chosen


async def run_berries(device: IOSBerryDevice, spend_cap: int | None) -> int:
    image = await device.screenshot()
    disc, ink, matched = frame_metrics(device, image)
    if not matched:
        # The dim-disc second chance that used to live here, which allowed a
        # full defender through for traversal only, is covered by the widened
        # BERRY_DISC_RANGE floor -- see the note on it in berry_android.
        raise IOSBerryError(
            f"This does not look like a gym feeding screen (disc={disc:.1f} of "
            f"{android_berry.BERRY_DISC_RANGE}, name_ink={ink} of "
            f"{android_berry.NAME_INK_RANGE}); stopped without tapping"
        )

    share = await berry_gold_share(device)
    if share is not None:
        log(device, f"Initial floor-color diagnostic: {share:.0%} gold (not used for selection)")
    if spend_cap is not None:
        log(device, f"Spending up to {spend_cap} berries, feeding through changeovers")

    budget = spend_cap
    chosen: str | None = None
    seen: list[list[bool]] = []
    idle = 0
    reverse = False
    swept_back = False
    for _ in range(
        android_berry.MAX_DEFENDERS * android_berry.MAX_TURNS_PER_DEFENDER * 2
    ):
        card = await card_fingerprint(device)
        already_seen = card is not None and any(
            android_berry.cards_match(card, old) for old in seen
        )
        should_feed = not already_seen
        if already_seen:
            if await berry_offered(device) is None:
                idle += 1
            else:
                should_feed = True
                log(device, "Previously seen defender still offers a berry; feeding again")
        if should_feed:
            if already_seen:
                number = "recheck"
            else:
                number = str(len(seen) + 1) if card is not None else "?"
                if card is not None:
                    seen.append(card)
            if already_seen:
                log(device, "Rechecking previously seen defender")
            else:
                log(device, f"Defender {number}: feeding")
            count, stop, chosen = await feed_defender(device, chosen, budget)
            log(device, f"sent {count} feed attempt{'s' if count != 1 else ''}")
            if already_seen and count == 0:
                idle += 1
            else:
                idle = 0
            if budget is not None:
                budget -= count
            if stop == "premium":
                log(device, "No Razz, Nanab, or Pinap berries remain; premium berries were not spent")
                break
            if stop == "cap" or budget == 0:
                log(device, "Spent the berry cap, stopping here")
                break

        # The roster cap ends the run only once the gym has been swept both
        # ways -- see the note on the same check in berry_android.py. Reaching
        # MAX_DEFENDERS going forward does not mean every defender was reached,
        # so the cap turns the run round rather than ending it.
        full_roster = len(seen) >= android_berry.MAX_DEFENDERS
        if full_roster and swept_back:
            log(device, f"Fed the full roster of {android_berry.MAX_DEFENDERS}")
            break
        if full_roster or idle >= android_berry.IDLE_TURNS:
            if swept_back:
                log(
                    device,
                    f"Nothing unfinished in {android_berry.IDLE_TURNS} swipes either way; "
                    "whole gym checked",
                )
                break
            reverse, swept_back, idle = True, True, 0
            log(device, "Sweeping the carousel back in the other direction")
        log(device, "Swiping to next defender")
        if not await turn_card(device, reverse):
            if not swept_back:
                reverse, swept_back, idle = True, True, 0
                log(device, "Forward swipe did not move; trying the other direction")
                if await turn_card(device, reverse):
                    continue
            log(device, "Card would not change in either direction; stopping")
            break
    return len(seen)


def gift_runner_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", r"(^|[ /])gift_ios[.]py([ ]|$)"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(value) for value in result.stdout.split() if value.isdigit()]


@contextmanager
def device_lock(udid: str) -> Iterator[None]:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{udid}.lock"
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IOSBerryError("This iPhone is already controlled by another fleet command") from exc
        yield
    finally:
        handle.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--spend", type=int, help="maximum berries to spend across this run")
    parser.add_argument("--check", action="store_true", help="read-only screen/detector check")
    parser.add_argument(
        "--carousel-check",
        action="store_true",
        help="test an adaptive next-defender swipe without feeding",
    )
    parser.add_argument(
        "--picker-check",
        action="store_true",
        help="open the picker and select the cheapest non-premium berry without feeding",
    )
    parser.add_argument("--dry-run", action="store_true", help="connect and validate without tapping")
    parser.add_argument("--screenshot", type=Path, default=DEFAULT_CHECK_SCREENSHOT)
    parser.add_argument("--fleet-child", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


async def async_main(args: argparse.Namespace, config: RuntimeConfig) -> int:
    if gift_runner_pids():
        raise IOSBerryError("gift_ios.py is active; stop it before starting berry control")
    device = await asyncio.to_thread(IOSBerryDevice.connect, config)
    normal_exit = False
    try:
        image = await device.screenshot()
        disc, ink, matched = frame_metrics(device, image)
        anchor = device.image_point(config.coordinates["BERRY_BTN"], image)
        frame = raw_frame(image)
        found = android_berry.find_berry(*frame, anchor)
        share = android_berry.gold_share_of(*frame, anchor)
        print(
            f"Connected: viewport {device.viewport['width']}x{device.viewport['height']}; "
            f"screenshot {image.width}x{image.height}; disc={disc:.1f}; "
            f"name_ink={ink}; feed_screen={matched}; berry={found}; gold_share={share}",
            flush=True,
        )
        if args.carousel_check:
            if await card_fingerprint(device) is None:
                raise IOSBerryError("Defender card could not be read; carousel was not moved")
            if not await turn_card(device):
                raise IOSBerryError("Adaptive carousel swipe could not change the defender")
            print("Carousel check changed the defender; no berry was fed", flush=True)
            normal_exit = True
            return 0
        if args.picker_check:
            if not matched or found is None:
                raise IOSBerryError("Berry screen validation failed; picker was not opened")
            chosen, recheck = await select_cheap_berry(device)
            if chosen == android_berry.PICKER_UNAVAILABLE:
                raise IOSBerryError("Berry picker did not open; defender may already be full")
            if chosen is None:
                raise IOSBerryError("No Razz, Nanab, or Pinap berries remain")
            print(
                f"Picker check selected {chosen}, {recheck} feeds per picker read;"
                " no berry was fed",
                flush=True,
            )
            normal_exit = True
            return 0
        if args.check:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            image.save(args.screenshot)
            print(f"Saved {args.screenshot}")
            if not matched or found is None:
                raise IOSBerryError("Berry screen calibration check failed; no taps sent")
            normal_exit = True
            return 0
        if args.dry_run:
            if not matched or found is None:
                raise IOSBerryError("Berry screen validation failed; no taps sent")
            print("Dry run passed; no taps sent")
            normal_exit = True
            return 0
        completed = await run_berries(device, args.spend if args.spend is not None else config.spend_cap)
        print(f"Processed {completed} defender(s)", flush=True)
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
    args = parse_args()
    if args.spend is not None and args.spend < 1:
        raise IOSBerryError("--spend must be positive")
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
        raise SystemExit(fleet_entrypoint.run_operation('berries', 'feed_berries.py'))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
