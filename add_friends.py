#!/usr/bin/env python3
"""Send friend requests from every selected connected Pokémon GO device."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from PIL import Image

from sources import config_paths, fleet_entrypoint, pokemon_fleet


ROOT = Path(__file__).resolve().parent
ARTIFACT_ROOT = config_paths.state_dir() / "friends"
DEFAULT_SOURCE = "https://pokemongofriendcodes.com/"
DEFAULT_COUNT = 100
DEFAULT_HISTORY = ARTIFACT_ROOT / "history.json"
DEFAULT_CACHE = ARTIFACT_ROOT / "latest-public-codes.json"
CODE_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){12}(?!\d)")
PUBLIC_CODE_PATTERN = re.compile(
    r'data-friend-code=["\']([^"\']+)["\']', re.IGNORECASE
)
DEFAULT_REQUEST_TIMEOUT = 45.0
# Pokémon GO pins a close "X" disc over the bottom of the Add Friend page while
# the content scrolls underneath it. A trainer field resting below this share of
# the screen sits under that disc: tapping it closes the page instead of opening
# the keyboard, and its teal pixels leak into trainer_field_has_code.
FIELD_REACHABLE_MAX_Y_SHARE = 0.78
FIELD_SCROLL_ATTEMPTS = 3
IOS_STARTUP_HTTP_TIMEOUT = 90
IOS_COMMAND_HTTP_TIMEOUT = 30


class FriendAutomationError(RuntimeError):
    pass


class FriendScreenError(FriendAutomationError):
    pass


class FriendAutomationCancelled(FriendAutomationError):
    pass


@dataclass(frozen=True)
class ImagePoint:
    x: int
    y: int


def normalize_code(value: str) -> str:
    code = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\d{12}", code):
        raise FriendAutomationError(f"Trainer code must contain exactly 12 digits: {value!r}")
    return code


def extract_trainer_codes(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in CODE_PATTERN.findall(text):
        code = re.sub(r"\D", "", match)
        if code not in seen:
            seen.add(code)
            found.append(code)
    return found


def _public_codes_from_page(text: str) -> list[str]:
    """Extract listed codes without mistaking the form placeholder for a code."""
    listed = PUBLIC_CODE_PATTERN.findall(text)
    return extract_trainer_codes("\n".join(listed) if listed else text)


def _page_url(url: str, page: int) -> str:
    if page == 1:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit(parts._replace(query=urlencode(query)))


def fetch_public_codes(
    url: str,
    cache_path: Path = DEFAULT_CACHE,
    minimum_count: int = 1,
) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    refresh_error: Optional[Exception] = None

    try:
        for page in range(1, 101):
            page_url = _page_url(url, page)
            request = Request(
                page_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Safari/537.36"
                    )
                },
            )
            with urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8", "ignore")

            new_codes = [
                code for code in _public_codes_from_page(text) if code not in seen
            ]
            if not new_codes:
                break
            seen.update(new_codes)
            codes.extend(new_codes)
            if len(codes) >= minimum_count:
                break

        if not codes:
            raise FriendAutomationError(f"No 12-digit trainer codes found at {url}")

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "source": url,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "codes": codes,
                },
                indent=2,
            )
            + "\n"
        )
        return codes
    except (OSError, ValueError, FriendAutomationError) as exc:
        refresh_error = exc

    try:
        cached = json.loads(cache_path.read_text())
        codes = [normalize_code(value) for value in cached.get("codes", [])]
    except (OSError, ValueError, FriendAutomationError):
        raise FriendAutomationError(
            f"Could not load public trainer codes from {url}: {refresh_error}"
        ) from refresh_error
    if not codes:
        raise FriendAutomationError(
            f"Could not load public trainer codes from {url}: {refresh_error}"
        ) from refresh_error
    print(f"Internet refresh failed; using {len(codes)} cached public codes")
    return codes


def _row_groups(rows: list[tuple[int, int, int]], step: int) -> list[list[tuple[int, int, int]]]:
    groups: list[list[tuple[int, int, int]]] = []
    for row in rows:
        if not groups or row[0] > groups[-1][-1][0] + step:
            groups.append([row])
        else:
            groups[-1].append(row)
    return groups


def _row_matches(
    image: Image.Image,
    predicate: Callable[[int, int, int], bool],
    *,
    start_y: float = 0.0,
    end_y: float = 1.0,
    minimum_share: float,
) -> tuple[list[tuple[int, int, int]], int]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = rgb.load()
    step = max(1, width // 600)
    left = int(width * 0.05)
    right = int(width * 0.95)
    sample_count = max(1, (right - left + step - 1) // step)
    max_gap = max(15, int(width * 0.06))
    rows: list[tuple[int, int, int]] = []
    for y in range(int(height * start_y), int(height * end_y), step):
        matches = [x for x in range(left, right, step) if predicate(*pixels[x, y])]
        if len(matches) < sample_count * minimum_share:
            continue
        clusters: list[list[int]] = []
        cur = [matches[0]]
        for x in matches[1:]:
            if x - cur[-1] <= max_gap:
                cur.append(x)
            else:
                clusters.append(cur)
                cur = [x]
        clusters.append(cur)
        best_cluster = max(clusters, key=len)
        if len(best_cluster) >= sample_count * minimum_share:
            rows.append((y, best_cluster[0], best_cluster[-1]))
    return rows, step


def find_trainer_field(image: Image.Image) -> Optional[ImagePoint]:
    width, height = image.size
    rgb = image.convert("RGB")

    def teal(red: int, green: int, blue: int) -> bool:
        return green > 135 and blue > 110 and red < 155 and green - red > 30

    rows, step = _row_matches(
        image,
        teal,
        start_y=0.15,
        end_y=0.97,
        minimum_share=0.20,
    )
    raw_groups = _row_groups(rows, step)
    groups: list[list[tuple[int, int, int]]] = []
    max_group_height = int(height * 0.04)
    for g in raw_groups:
        if g[-1][0] - g[0][0] <= max_group_height:
            groups.append(g)
        else:
            sub: list[tuple[int, int, int]] = []
            for row in g:
                if (
                    not sub
                    or row[0] > sub[-1][0] + step * 2
                    or (sub[-1][0] - sub[0][0]) >= max_group_height
                ):
                    if sub:
                        groups.append(sub)
                    sub = [row]
                else:
                    sub.append(row)
            if sub:
                groups.append(sub)

    for index, top_group in enumerate(groups):
        top = top_group[len(top_group) // 2]
        if top[2] - top[1] < width * 0.50:
            continue
        for bottom_group in groups[index + 1 :]:
            if bottom_group[0][0] - top_group[-1][0] > height * 0.14:
                break
            bottom = bottom_group[len(bottom_group) // 2]
            if bottom[2] - bottom[1] >= width * 0.50:
                center_y = round((top[0] + bottom[0]) / 2)
                interior_left = max(0, top[1] + round(width * 0.08))
                interior_right = min(width, top[2] - round(width * 0.08))
                interior = [
                    rgb.getpixel((x, center_y))
                    for x in range(interior_left, interior_right, max(1, step * 2))
                ]
                light = sum(
                    red > 150 and green > 150 and blue > 150
                    for red, green, blue in interior
                )
                if not interior or light < len(interior) * 0.20:
                    continue
                return ImagePoint(
                    x=round((top[1] + top[2]) / 2),
                    y=center_y,
                )
    return None


def find_gradient_button(image: Image.Image) -> Optional[ImagePoint]:
    width, height = image.size
    rgb = image.convert("RGB")

    def gradient(red: int, green: int, blue: int) -> bool:
        return red < 205 and green > 165 and green - red > 20 and blue > 50

    def is_button_group(group: list[tuple[int, int, int]]) -> bool:
        mid_row = group[len(group) // 2]
        center_x = (mid_row[1] + mid_row[2]) // 2
        if not (width * 0.30 <= center_x <= width * 0.70):
            return False
        mid_y = (group[0][0] + group[-1][0]) // 2
        sample_left = max(0, mid_row[1] - round(width * 0.03))
        sample_right = min(width - 1, mid_row[2] + round(width * 0.03))
        pr_left = rgb.getpixel((sample_left, mid_y))
        pr_right = rgb.getpixel((sample_right, mid_y))
        light_left = pr_left[0] > 190 and pr_left[1] > 190 and pr_left[2] > 190
        light_right = pr_right[0] > 190 and pr_right[1] > 190 and pr_right[2] > 190
        return light_left or light_right

    rows, step = _row_matches(
        image,
        gradient,
        start_y=0.12,
        end_y=0.84,
        minimum_share=0.25,
    )
    candidates = [
        group
        for group in _row_groups(rows, step)
        if group[-1][0] - group[0][0] >= height * 0.02 and is_button_group(group)
    ]
    if not candidates:
        return None
    group = max(candidates, key=lambda value: value[-1][0] - value[0][0])
    middle = group[len(group) // 2]
    return ImagePoint(
        x=round((middle[1] + middle[2]) / 2),
        y=round((group[0][0] + group[-1][0]) / 2),
    )


def find_add_friend_button(image: Image.Image) -> Optional[ImagePoint]:
    """Find the orange Add Friend icon on the upper-left of the Friends list."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    left = int(width * 0.08)
    right = int(width * 0.98)
    top = int(height * 0.05)
    bottom = int(height * 0.28)
    matches: list[tuple[int, int]] = []
    for y in range(top, bottom):
        for x in range(left, right):
            red, green, blue = rgb.getpixel((x, y))
            if red > 180 and green < 175 and blue < 150 and red - green > 35:
                matches.append((x, y))
    if len(matches) < 20:
        return None
    left_matches = [p for p in matches if p[0] < width * 0.40]
    target_matches = left_matches if len(left_matches) >= 20 else matches
    xs = [point[0] for point in target_matches]
    ys = [point[1] for point in target_matches]
    if max(xs) - min(xs) < width * 0.015 or max(ys) - min(ys) < height * 0.008:
        return None
    return ImagePoint(
        x=round(sum(xs) / len(xs)),
        y=round(sum(ys) / len(ys)),
    )


def dialog_height_share(image: Image.Image) -> float:
    width, height = image.size
    rgb = image.convert("RGB")
    pixels = rgb.load()
    backdrop_samples = 0
    for y_share in (0.07, 0.11, 0.16):
        for x_share in (0.20, 0.50, 0.80):
            red, green, blue = pixels[
                min(width - 1, round(width * x_share)),
                min(height - 1, round(height * y_share)),
            ]
            if red < 170 and green > 105 and blue > 75 and green - red > 20:
                backdrop_samples += 1
    if backdrop_samples < 5:
        return 0.0

    def white(red: int, green: int, blue: int) -> bool:
        return red > 230 and green > 230 and blue > 225

    rows, step = _row_matches(
        image,
        white,
        start_y=0.05,
        end_y=0.95,
        minimum_share=0.50,
    )
    if not rows:
        return 0.0
    return (rows[-1][0] - rows[0][0] + step) / height


def has_error_toast(image: Image.Image) -> bool:
    def pink(red: int, green: int, blue: int) -> bool:
        return red > 185 and blue > 110 and red - green > 35

    rows, step = _row_matches(
        image,
        pink,
        start_y=0.22,
        end_y=0.85,
        minimum_share=0.45,
    )
    return any(
        group[-1][0] - group[0][0] >= image.height * 0.015
        for group in _row_groups(rows, step)
    )


def has_ios_keyboard(image: Image.Image) -> bool:
    rgb = image.convert("RGB")
    width, height = rgb.size
    step = max(1, width // 375)
    dark = 0
    total = 0
    for y in range(int(height * 0.62), int(height * 0.98), step):
        for x in range(0, width, step):
            red, green, blue = rgb.getpixel((x, y))
            total += 1
            if red < 80 and green < 80 and blue < 80:
                dark += 1
    return total > 0 and dark >= total * 0.005


def trainer_field_has_code(image: Image.Image, field: ImagePoint) -> bool:
    """Distinguish entered dark digits from the pale trainer-code placeholder."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    left = max(0, field.x - round(width * 0.22))
    right = min(width, field.x + round(width * 0.22))
    top = max(0, field.y - round(height * 0.018))
    bottom = min(height, field.y + round(height * 0.018))
    dark_pixels = 0
    sampled_pixels = 0
    step = max(1, width // 750)
    for y in range(top, bottom, step):
        for x in range(left, right, step):
            red, green, blue = rgb.getpixel((x, y))
            sampled_pixels += 1
            # Real digits use the dark teal text color. The placeholder can
            # render as medium gray/teal on some versions, but its red channel
            # stays well above the entered text's range.
            if red < 140 and green < 180 and blue < 180:
                dark_pixels += 1
    return sampled_pixels > 0 and dark_pixels >= max(12, round(sampled_pixels * 0.002))


def has_secondary_dialog_action(image: Image.Image, primary: ImagePoint) -> bool:
    """Detect a second teal text action below a dialog's filled primary button."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    left = int(width * 0.25)
    right = int(width * 0.75)
    top = min(height, primary.y + int(height * 0.05))
    bottom = min(height, primary.y + int(height * 0.17))
    if bottom <= top:
        return False
    matches = 0
    for y in range(top, bottom, 2):
        for x in range(left, right, 2):
            red, green, blue = rgb.getpixel((x, y))
            if not (
                green > 145 and blue > 105 and red < 130 and green - red > 35
            ):
                continue
            pale_neighbors = 0
            for dx, dy in ((8, 0), (-8, 0), (0, 8), (0, -8)):
                neighbor_x = max(0, min(width - 1, x + dx))
                neighbor_y = max(0, min(height - 1, y + dy))
                near_red, near_green, near_blue = rgb.getpixel(
                    (neighbor_x, neighbor_y)
                )
                if near_red > 200 and near_green > 200 and near_blue > 195:
                    pale_neighbors += 1
            if pale_neighbors >= 2:
                matches += 1
    return matches >= 12


def find_confirmation_button(image: Image.Image) -> Optional[ImagePoint]:
    """Recognize the expected Send/Cancel prompt, including tall Android screens."""
    button = find_gradient_button(image)
    if button is None:
        return None
    return button


class History:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        try:
            value = json.loads(path.read_text())
            self.data = value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("version", 1)
        self.data.setdefault("devices", {})

    def sent(self, identifier: str) -> set[str]:
        with self._lock:
            records = self.data["devices"].get(identifier, [])
            return {
                str(record.get("code"))
                for record in records
                if isinstance(record, dict)
                and re.fullmatch(r"\d{12}", str(record.get("code")))
            }

    def record(self, identifier: str, code: str) -> None:
        with self._lock:
            records = self.data["devices"].setdefault(identifier, [])
            records.append(
                {"code": code, "sent_at": datetime.now(timezone.utc).isoformat()}
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, indent=2) + "\n")
            temporary.replace(self.path)


class FriendController:
    platform: str

    def __init__(self, spec: pokemon_fleet.DeviceSpec) -> None:
        self.spec = spec
        self.label = spec.name
        self.identifier = spec.identifier

    def screenshot(self) -> Image.Image:
        raise NotImplementedError

    def tap(self, point: ImagePoint, image: Image.Image) -> None:
        raise NotImplementedError

    def swipe_up(self, image: Image.Image) -> None:
        raise NotImplementedError

    def focus_and_type(self, point: ImagePoint, image: Image.Image, code: str) -> None:
        raise NotImplementedError

    def cancel_input(self) -> None:
        pass

    def hide_keyboard_if_needed(self) -> None:
        pass

    def close(self) -> None:
        pass


class AndroidFriendController(FriendController):
    platform = "android"

    def __init__(
        self, spec: pokemon_fleet.DeviceSpec, adb_binary: str
    ) -> None:
        super().__init__(spec)
        self.adb = adb_binary

    def _run(self, *arguments: str, timeout: float = 15) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            [self.adb, "-s", self.identifier, *arguments],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "ignore").strip()
            raise FriendAutomationError(f"{self.label}: adb failed: {message}")
        return result

    def _ensure_app_active(self) -> None:
        try:
            out = self._run("shell", "dumpsys", "window", "displays").stdout.decode(
                "utf-8", "ignore"
            )
            if "com.nianticlabs.pokemongo" not in out:
                print(f"[{self.label}] bringing Pokémon GO to foreground...", flush=True)
                self._run(
                    "shell",
                    "monkey",
                    "-p",
                    "com.nianticlabs.pokemongo",
                    "-c",
                    "android.intent.category.LAUNCHER",
                    "1",
                )
                time.sleep(2)
        except Exception:
            pass

    def screenshot(self) -> Image.Image:
        self._ensure_app_active()
        result = self._run("exec-out", "screencap", "-p", timeout=20)
        signature = b"\x89PNG\r\n\x1a\n"
        start = result.stdout.find(signature)
        if start < 0:
            raise FriendScreenError(f"{self.label}: Android screenshot has no PNG data")
        try:
            return Image.open(io.BytesIO(result.stdout[start:])).convert("RGB")
        except OSError as exc:
            raise FriendScreenError(f"{self.label}: invalid Android screenshot") from exc

    def tap(self, point: ImagePoint, image: Image.Image) -> None:
        self._run("shell", "input", "tap", str(point.x), str(point.y))

    def swipe_up(self, image: Image.Image) -> None:
        width, height = image.size
        self._run(
            "shell",
            "input",
            "swipe",
            str(width // 2),
            str(round(height * 0.65)),
            str(width // 2),
            str(round(height * 0.45)),
            "350",
        )

    def focus_and_type(self, point: ImagePoint, image: Image.Image, code: str) -> None:
        self.tap(point, image)
        time.sleep(0.6)
        delete_keys = ["KEYCODE_DEL"] * 16
        self._run("shell", "input", "keyevent", "KEYCODE_MOVE_END", *delete_keys)
        self._run("shell", "input", "text", code)
        time.sleep(0.5)

    def hide_keyboard_if_needed(self) -> None:
        self._hide_keyboard()

    def _hide_keyboard(self) -> None:
        # Never blind-tap the page to dismiss the IME: the SEND button covers
        # the middle of the entry screen, so a "neutral" tap submits the code
        # early. BACK is safe as long as the keyboard is genuinely up -- on a
        # closed keyboard it would leave the Add Friend page instead.
        for _ in range(3):
            if not self._keyboard_visible():
                return
            self._run("shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(0.6)

    def cancel_input(self) -> None:
        try:
            self._run("shell", "input", "keyevent", "KEYCODE_BACK")
        except FriendAutomationError:
            pass

    def _keyboard_visible(self) -> bool:
        state = self._run("shell", "dumpsys", "input_method").stdout.decode(
            "utf-8", "ignore"
        )
        return any(
            marker in state
            for marker in (
                "mInputShown=true",
                "mInputViewShown=true",
                "isInputViewShown=true",
            )
        )


class IOSFriendController(FriendController):
    platform = "ios"

    def __init__(self, spec: pokemon_fleet.DeviceSpec) -> None:
        super().__init__(spec)
        self.driver = self._connect(spec)
        try:
            self._dismiss_known_system_alert(self.driver, self.label)
            prefetched = Image.open(
                io.BytesIO(self.driver.get_screenshot_as_png())
            ).convert("RGB")
            point_scale = self._device_point_scale(spec)
        except Exception:
            try:
                self.driver.quit()
            except Exception:
                pass
            raise
        self._prefetched_screenshot: Optional[Image.Image] = prefetched
        self.viewport = {
            "x": 0,
            "y": 0,
            "width": round(prefetched.width / point_scale),
            "height": round(prefetched.height / point_scale),
        }
        print(
            f"[{self.label}] iPhone viewport "
            f"{self.viewport['width']}x{self.viewport['height']} "
            f"from {point_scale:g}x display scale",
            flush=True,
        )

    @staticmethod
    def _dismiss_known_system_alert(driver: Any, label: str) -> bool:
        try:
            alert = driver.switch_to.alert
            message = str(alert.text)
        except Exception:
            return False
        normalized = message.lower().replace("’", "'").replace("“", '"').replace("”", '"')
        is_paste_alert = (
            "paste from" in normalized
            or "pasting from" in normalized
            or ("paste" in normalized and ("allow" in normalized or "would like" in normalized or "pasting" in normalized))
        )
        if not is_paste_alert:
            raise FriendScreenError(
                f"{label}: unknown iPhone system alert is blocking automation: {message}"
            )
        try:
            alert.dismiss()
        except Exception as exc:
            raise FriendAutomationError(
                f"{label}: could not select Don't Allow Paste"
            ) from exc
        print(f"[{label}] dismissed iPhone paste-permission prompt", flush=True)
        time.sleep(0.5)
        return True

    @staticmethod
    def _point_scale_from_output(output: str) -> float:
        match = re.search(r"\bpointScale:\s*([0-9]+(?:\.[0-9]+)?)", output)
        if match is None:
            raise FriendAutomationError("CoreDevice display output has no pointScale")
        value = float(match.group(1))
        if value <= 0:
            raise FriendAutomationError("CoreDevice reported an invalid pointScale")
        return value

    @classmethod
    def _device_point_scale(cls, spec: pokemon_fleet.DeviceSpec) -> float:
        device = pokemon_fleet.load_appium_profile(spec)["device"]
        configured = device.get("point_scale")
        if isinstance(configured, (int, float)) and configured > 0:
            return float(configured)
        try:
            result = subprocess.run(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "info",
                    "displays",
                    "--device",
                    device["udid"],
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FriendAutomationError(
                f"{spec.name}: could not read iPhone display scale: {exc}"
            ) from exc
        output = result.stdout + "\n" + result.stderr
        if result.returncode != 0:
            detail = output.strip().splitlines()[-1] if output.strip() else "unknown error"
            raise FriendAutomationError(
                f"{spec.name}: could not read iPhone display scale: {detail}"
            )
        try:
            return cls._point_scale_from_output(output)
        except FriendAutomationError as exc:
            raise FriendAutomationError(f"{spec.name}: {exc}") from exc

    @staticmethod
    def _connect(spec: pokemon_fleet.DeviceSpec) -> Any:
        try:
            from appium import webdriver
            from appium.options.ios import XCUITestOptions
            from appium.webdriver.client_config import AppiumClientConfig
        except ModuleNotFoundError as exc:
            raise FriendAutomationError("The Appium Python client is missing") from exc
        profile = pokemon_fleet.load_appium_profile(spec)
        device = profile["device"]
        capabilities: dict[str, Any] = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            "appium:udid": device["udid"],
            "appium:deviceName": device.get("name", "iPhone"),
            "appium:bundleId": device.get("bundle_id", "com.nianticlabs.pokemongo"),
            "appium:noReset": True,
            "appium:shouldTerminateApp": False,
            "appium:xcodeOrgId": device["team_id"],
            "appium:xcodeSigningId": "Apple Development",
            "appium:updatedWDABundleId": device["wda_bundle_id"],
            "appium:allowProvisioningDeviceRegistration": True,
            "appium:useNewWDA": False,
            "appium:newCommandTimeout": 3600,
            # Pokémon GO continuously animates, so waiting for XCTest's
            # application-idle state can make otherwise successful taps hang.
            "appium:waitForIdleTimeout": 0,
            "appium:waitForQuiescence": False,
            "appium:animationCoolOffTimeout": 0,
        }
        for source, target in (
            ("platform_version", "platformVersion"),
            ("wda_local_port", "appium:wdaLocalPort"),
            ("mjpeg_server_port", "appium:mjpegServerPort"),
        ):
            if source in device:
                capabilities[target] = device[source]
        if isinstance(device.get("derived_data_path"), str):
            capabilities["appium:derivedDataPath"] = os.path.expanduser(
                device["derived_data_path"]
            )
        options = XCUITestOptions().load_capabilities(capabilities)
        client_config = AppiumClientConfig(
            profile["server_url"],
            timeout=IOS_STARTUP_HTTP_TIMEOUT,
            init_args_for_pool_manager={"retries": 0},
        )
        last_error: Optional[Exception] = None
        for attempt in range(2):
            print(
                f"[{spec.name}] starting iPhone automation session "
                f"(attempt {attempt + 1}/2)",
                flush=True,
            )
            try:
                driver = webdriver.Remote(
                    command_executor=profile["server_url"],
                    options=options,
                    client_config=client_config,
                )
                driver.command_executor.client_config.timeout = IOS_COMMAND_HTTP_TIMEOUT
                return driver
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    print(f"[{spec.name}] Appium connection failed; restarting WDA once")
                    try:
                        from sources.ios_wda_cleanup import stop_wda_runner

                        stop_wda_runner(device["udid"])
                    except Exception:
                        pass
                    time.sleep(2)
        try:
            from sources.ios_wda_cleanup import stop_wda_runner

            stop_wda_runner(device["udid"])
        except Exception:
            pass
        raise FriendAutomationError(
            f"{spec.name}: could not create iOS Appium session: {last_error}"
        )

    def screenshot(self) -> Image.Image:
        if self._prefetched_screenshot is not None:
            image = self._prefetched_screenshot
            self._prefetched_screenshot = None
            return image
        try:
            return Image.open(
                io.BytesIO(self.driver.get_screenshot_as_png())
            ).convert("RGB")
        except Exception as exc:
            raise FriendScreenError(f"{self.label}: could not capture iPhone") from exc

    def tap(self, point: ImagePoint, image: Image.Image) -> None:
        width, height = image.size
        logical = {
            "x": round(point.x * self.viewport["width"] / width),
            "y": round(point.y * self.viewport["height"] / height),
        }
        self.driver.execute_script("mobile: tap", logical)

    def swipe_up(self, image: Image.Image) -> None:
        self.driver.execute_script("mobile: swipe", {"direction": "up"})

    def _keyboard_points(self) -> tuple[dict[str, ImagePoint], ImagePoint]:
        deadline = time.monotonic() + 3
        last_source = ""
        while time.monotonic() < deadline:
            last_source = self.driver.page_source
            try:
                result = self._keyboard_points_from_source(last_source)
            except FriendScreenError:
                time.sleep(0.35)
                continue
            return result
        raise FriendScreenError(f"{self.label}: numeric iPhone keyboard not detected")

    def _keyboard_points_from_source(
        self, source: str
    ) -> tuple[dict[str, ImagePoint], ImagePoint]:
        try:
            root = ET.fromstring(source)
        except ET.ParseError as exc:
            raise FriendScreenError(f"{self.label}: could not read iPhone keyboard") from exc
        digits: dict[str, ImagePoint] = {}
        delete: Optional[ImagePoint] = None
        for element in root.iter():
            if element.attrib.get("type") != "XCUIElementTypeKey":
                continue
            name = element.attrib.get("name", "")
            try:
                point = ImagePoint(
                    int(element.attrib["x"]) + int(element.attrib["width"]) // 2,
                    int(element.attrib["y"]) + int(element.attrib["height"]) // 2,
                )
            except (KeyError, ValueError):
                continue
            if name in "0123456789" and len(name) == 1:
                digits[name] = point
            elif name == "delete":
                delete = point
        if len(digits) != 10 or delete is None:
            raise FriendScreenError(f"{self.label}: numeric iPhone keyboard not detected")
        return digits, delete

    def _tap_logical(self, point: ImagePoint) -> None:
        self.driver.execute(
            "actions",
            {
                "actions": [
                    {
                        "type": "pointer",
                        "id": "finger",
                        "parameters": {"pointerType": "touch"},
                        "actions": [
                            {
                                "type": "pointerMove",
                                "duration": 0,
                                "x": point.x,
                                "y": point.y,
                                "origin": "viewport",
                            },
                            {"type": "pointerDown", "button": 0},
                            {"type": "pause", "duration": 50},
                            {"type": "pointerUp", "button": 0},
                        ],
                    }
                ]
            },
        )

    def _keyboard_digit_points_from_viewport(self) -> dict[str, ImagePoint]:
        width = int(self.viewport["width"])
        height = int(self.viewport["height"])
        left = width * 0.059
        right = width * 0.941
        y = max(1, round(height - 187))
        return {
            digit: ImagePoint(
                x=round(left + index * (right - left) / 9),
                y=y,
            )
            for index, digit in enumerate("1234567890")
        }

    def _keyboard_delete_point_from_viewport(self) -> ImagePoint:
        return ImagePoint(
            x=round(int(self.viewport["width"]) * 0.941),
            y=max(1, round(int(self.viewport["height"]) - 80)),
        )

    def focus_and_type(self, point: ImagePoint, image: Image.Image, code: str) -> None:
        if trainer_field_has_code(image, point):
            # WDA can hang indefinitely when tapping the keyboard accessory's
            # clear control. Leave Pokémon GO's entry page instead, then reopen
            # it through the normal Add Friend navigation to get a fresh field.
            cancel = ImagePoint(
                x=point.x,
                y=min(image.height - 1, point.y + round(image.height * 0.205)),
            )
            print(
                f"[{self.label}] reopening Add Friend to clear the previous code",
                flush=True,
            )
            self.tap(cancel, image)
            time.sleep(0.8)
            image, point = prepare_entry_screen(self)
            if trainer_field_has_code(image, point):
                path = _save_diagnostic(self, image, "code-not-cleared")
                raise FriendScreenError(
                    f"{self.label}: previous trainer code did not clear; screenshot: {path}"
                )
        self.tap(point, image)
        time.sleep(0.75)
        self._dismiss_known_system_alert(self.driver, self.label)
        focused = self.screenshot()
        if not has_ios_keyboard(focused):
            self.tap(point, image)
            time.sleep(0.75)
            self._dismiss_known_system_alert(self.driver, self.label)
            focused = self.screenshot()
        if not has_ios_keyboard(focused):
            path = _save_diagnostic(self, focused, "keyboard-not-found")
            raise FriendScreenError(
                f"{self.label}: iPhone keyboard did not open; screenshot: {path}"
            )
        try:
            digit_points = self._keyboard_digit_points_from_viewport()
            for digit in code:
                self._tap_logical(digit_points[digit])
        except Exception as exc:
            raise FriendScreenError(
                f"{self.label}: iPhone could not enter the trainer code"
            ) from exc

    def cancel_input(self) -> None:
        # Quitting the Appium session removes keyboard automation safely. A
        # synthetic tap here can block WDA when no keyboard is present.
        pass

    def close(self) -> None:
        try:
            self.driver.quit()
        except Exception:
            pass


def _save_diagnostic(controller: FriendController, image: Image.Image, label: str) -> Path:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", controller.label)
    path = ARTIFACT_ROOT / f"{safe}-{label}.png"
    image.save(path)
    return path


def _dismiss_small_dialog(controller: FriendController, image: Image.Image) -> bool:
    share = dialog_height_share(image)
    if not 0.12 <= share < 0.40:
        return False
    button = find_gradient_button(image)
    if button is None:
        raise FriendScreenError(f"{controller.label}: dialog has no safe button")
    if has_secondary_dialog_action(image, button):
        raise FriendScreenError(
            f"{controller.label}: two-action dialog detected; refusing to choose"
        )
    controller.tap(button, image)
    time.sleep(1)
    return True


def field_is_reachable(image: Image.Image, field: ImagePoint) -> bool:
    """True when the trainer field sits clear of the pinned close-X disc."""
    return field.y <= image.height * FIELD_REACHABLE_MAX_Y_SHARE


def field_edge_below_fold(image: Image.Image) -> bool:
    """True when only the trainer field's top edge shows at the foot of the page.

    find_trainer_field needs both of the field's teal borders, so a field pushed
    under the fold reads as "no field at all". A wide teal rule low on the screen
    is the surviving half of that outline, and means scrolling will reveal it.
    """

    def teal(red: int, green: int, blue: int) -> bool:
        return green > 135 and blue > 110 and red < 155 and green - red > 30

    rows, _ = _row_matches(
        image,
        teal,
        start_y=FIELD_REACHABLE_MAX_Y_SHARE,
        end_y=1.0,
        minimum_share=0.20,
    )
    return any(row[2] - row[1] >= image.width * 0.50 for row in rows)


def _locate_field(
    controller: FriendController,
    image: Image.Image,
    *,
    on_entry_page: bool = False,
) -> tuple[Image.Image, Optional[ImagePoint]]:
    """Find the trainer field, scrolling the Add Friend page when it is needed.

    The page opens scrolled to the top, where the field either hides under the
    pinned close-X disc (Android) or falls below the fold entirely and cannot be
    detected at all (iPhone). Only scroll once we know we are on that page:
    either the field is already visible but out of reach, or the caller has just
    navigated here. Scrolling an unrecognised screen would pan the map instead.
    """
    field = find_trainer_field(image)
    if field is not None and field_is_reachable(image, field):
        return image, field
    if field is None and not on_entry_page and not field_edge_below_fold(image):
        return image, None
    for _ in range(FIELD_SCROLL_ATTEMPTS):
        print(f"[{controller.label}] scrolling the trainer field into reach", flush=True)
        controller.swipe_up(image)
        time.sleep(1.0)
        image = controller.screenshot()
        field = find_trainer_field(image)
        if field is not None and field_is_reachable(image, field):
            return image, field
    return image, None


def prepare_entry_screen(controller: FriendController) -> tuple[Image.Image, ImagePoint]:
    image = controller.screenshot()
    try:
        if _dismiss_small_dialog(controller, image):
            image = controller.screenshot()
    except FriendScreenError:
        controller.cancel_input()
        time.sleep(1)
        image = controller.screenshot()
    if dialog_height_share(image) >= 0.40:
        button = find_gradient_button(image)
        if button is not None and not has_secondary_dialog_action(image, button):
            print(
                f"[{controller.label}] dismissing prior single-action result dialog",
                flush=True,
            )
            controller.tap(button, image)
            time.sleep(1)
            image = controller.screenshot()
        else:
            print(f"[{controller.label}] dismissing modal dialog or prompt", flush=True)
            controller.cancel_input()
            time.sleep(1)
            image = controller.screenshot()
    width, height = image.size
    trainer_tab = ImagePoint(x=round(width * 0.28), y=round(height * 0.08))

    # Step 1: Direct trainer field check
    image, field = _locate_field(controller, image)
    if field is not None:
        return image, field

    # Step 2: If on Add Friend screen but QR CODE tab is open -> tap TRAINER CODE tab header
    controller.tap(trainer_tab, image)
    time.sleep(1.0)
    image = controller.screenshot()
    image, field = _locate_field(controller, image)
    if field is not None:
        return image, field

    # Step 3: Check if on Friends list tab -> tap ADD FRIEND button
    add_friend = find_add_friend_button(image)
    if add_friend is not None:
        print(f"[{controller.label}] opening Add Friend from Friends list", flush=True)
        controller.tap(add_friend, image)
        time.sleep(1.8)
        image = controller.screenshot()
        if _dismiss_small_dialog(controller, image):
            image = controller.screenshot()

        image, field = _locate_field(controller, image, on_entry_page=True)
        if field is not None:
            return image, field

        controller.tap(trainer_tab, image)
        time.sleep(1.0)
        image = controller.screenshot()
        image, field = _locate_field(controller, image, on_entry_page=True)
        if field is not None:
            return image, field

    # Step 4: Check if on ME tab in profile -> tap FRIENDS tab pill header (x = 0.50*w, y = 0.08*h)
    friends_tab_btn = ImagePoint(x=round(width * 0.50), y=round(height * 0.08))
    print(f"[{controller.label}] tapping Friends tab header (x={friends_tab_btn.x}, y={friends_tab_btn.y})", flush=True)
    controller.tap(friends_tab_btn, image)
    time.sleep(1.5)
    image = controller.screenshot()

    add_friend = find_add_friend_button(image)
    if add_friend is not None:
        print(f"[{controller.label}] opening Add Friend from Friends list", flush=True)
        controller.tap(add_friend, image)
        time.sleep(1.8)
        image = controller.screenshot()
        image, field = _locate_field(controller, image, on_entry_page=True)
        if field is not None:
            return image, field
        controller.tap(trainer_tab, image)
        time.sleep(1.0)
        image = controller.screenshot()
        image, field = _locate_field(controller, image, on_entry_page=True)
        if field is not None:
            return image, field

    # Step 5: Check if on Main Overworld Map -> tap Profile avatar (x = 0.125*w, y = 0.90*h)
    avatar_btn = ImagePoint(x=round(width * 0.125), y=round(height * 0.90))
    print(f"[{controller.label}] tapping Profile avatar from overworld map (x={avatar_btn.x}, y={avatar_btn.y})", flush=True)
    controller.tap(avatar_btn, image)
    time.sleep(2.0)
    image = controller.screenshot()

    controller.tap(friends_tab_btn, image)
    time.sleep(1.5)
    image = controller.screenshot()

    add_friend = find_add_friend_button(image)
    if add_friend is not None:
        print(f"[{controller.label}] opening Add Friend from Friends list", flush=True)
        controller.tap(add_friend, image)
        time.sleep(1.8)
        image = controller.screenshot()
        image, field = _locate_field(controller, image, on_entry_page=True)
        if field is not None:
            return image, field
        controller.tap(trainer_tab, image)
        time.sleep(1.0)
        image = controller.screenshot()
        image, field = _locate_field(controller, image, on_entry_page=True)
        if field is not None:
            return image, field

    path = _save_diagnostic(controller, image, "entry-not-found")
    raise FriendScreenError(
        f"{controller.label}: trainer-code field not found; screenshot: {path}"
    )


def _ensure_request_active(
    controller: FriendController,
    code: str,
    deadline: float,
    stop_event: Optional[threading.Event],
) -> None:
    if stop_event is not None and stop_event.is_set():
        raise FriendAutomationCancelled(f"{controller.label}: cancellation requested")
    if time.monotonic() >= deadline:
        raise FriendScreenError(
            f"{controller.label}: timed out while processing trainer code {code}"
        )


def send_friend_request(
    controller: FriendController,
    code: str,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    stop_event: Optional[threading.Event] = None,
) -> bool:
    deadline = time.monotonic() + request_timeout
    print(f"[{controller.label}] locating trainer-code field", flush=True)
    _ensure_request_active(controller, code, deadline, stop_event)
    image, field = prepare_entry_screen(controller)
    print(f"[{controller.label}] typing {code}", flush=True)
    _ensure_request_active(controller, code, deadline, stop_event)
    controller.focus_and_type(field, image, code)
    time.sleep(0.8)
    _ensure_request_active(controller, code, deadline, stop_event)
    entered = controller.screenshot()
    send = find_gradient_button(entered)
    if send is None:
        controller.hide_keyboard_if_needed()
        time.sleep(0.5)
        entered = controller.screenshot()
        send = find_gradient_button(entered)
    if send is None:
        path = _save_diagnostic(controller, entered, "send-not-found")
        raise FriendScreenError(
            f"{controller.label}: Send button not detected after code entry; screenshot: {path}"
        )
    print(f"[{controller.label}] code entered; opening confirmation", flush=True)
    controller.tap(send, entered)
    time.sleep(1.5)

    _ensure_request_active(controller, code, deadline, stop_event)
    confirm = None
    confirmation = None
    for attempt in range(3):
        confirmation = controller.screenshot()
        if has_error_toast(confirmation):
            print(
                f"[{controller.label}] {code}: invalid, expired, or unavailable trainer code",
                flush=True,
            )
            return False
        confirm = find_confirmation_button(confirmation)
        if confirm is not None:
            break
        time.sleep(0.8)

    if confirm is None:
        share = dialog_height_share(confirmation)
        if 0.12 <= share < 0.40:
            _dismiss_small_dialog(controller, confirmation)
            print(f"[{controller.label}] {code}: rejected, duplicate, or already a friend")
            return False
        path = _save_diagnostic(controller, confirmation, "confirmation-not-found")
        raise FriendScreenError(
            f"{controller.label}: trainer confirmation did not appear; screenshot: {path}"
        )
    if confirm is None:
        raise FriendScreenError(f"{controller.label}: confirmation Send button not found")
    print(f"[{controller.label}] confirming friend request", flush=True)
    controller.tap(confirm, confirmation)

    saw_animation = False
    result_deadline = min(deadline, time.monotonic() + 7)
    while time.monotonic() < result_deadline:
        time.sleep(0.45)
        _ensure_request_active(controller, code, deadline, stop_event)
        outcome = controller.screenshot()
        button = find_gradient_button(outcome)
        if button is not None:
            controller.tap(button, outcome)
            time.sleep(1)
            if has_secondary_dialog_action(outcome, button):
                print(
                    f"[{controller.label}] {code}: request rejected, duplicate, or already pending",
                    flush=True,
                )
                return False
            print(f"[{controller.label}] friend request for {code} submitted", flush=True)
            return True
        share = dialog_height_share(outcome)
        if share < 0.08:
            saw_animation = True
            continue
    raise FriendScreenError(f"{controller.label}: timed out waiting for friend-request result")


def make_controller(
    spec: pokemon_fleet.DeviceSpec, adb_binary: str
) -> FriendController:
    if spec.platform == "ios":
        return IOSFriendController(spec)
    return AndroidFriendController(spec, adb_binary)


def run_device(
    spec: pokemon_fleet.DeviceSpec,
    codes: Sequence[str],
    count: int,
    history: History,
    adb_binary: str,
    check: bool,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    stop_event: Optional[threading.Event] = None,
) -> int:
    print(f"[{spec.name}] connecting to {spec.platform} device", flush=True)
    controller = make_controller(spec, adb_binary)
    try:
        print(f"[{controller.label}] connected", flush=True)
        if check:
            image = controller.screenshot()
            field = find_trainer_field(image)
            path = _save_diagnostic(controller, image, "check")
            print(
                f"[{controller.label}] screenshot={path}; "
                f"trainer_field={'yes' if field else 'no'}; "
                f"dialog={dialog_height_share(image):.0%}"
            )
            return 0

        already_sent = history.sent(controller.identifier)
        sent = 0
        attempts = 0
        maximum_attempts = max(count * 4, count + 5)
        for code in codes:
            if stop_event is not None and stop_event.is_set():
                raise FriendAutomationCancelled(
                    f"{controller.label}: cancellation requested"
                )
            if sent >= count or attempts >= maximum_attempts:
                break
            if code in already_sent:
                continue
            attempts += 1
            print(f"[{controller.label}] entering {code}", flush=True)
            if send_friend_request(
                controller,
                code,
                request_timeout=request_timeout,
                stop_event=stop_event,
            ):
                history.record(controller.identifier, code)
                already_sent.add(code)
                sent += 1
                print(
                    f"[{controller.label}] friend request sent ({sent}/{count})",
                    flush=True,
                )
            time.sleep(0.8)
        if sent < count:
            print(
                f"[{controller.label}] stopped after {sent} successful request(s) "
                f"and {attempts} attempt(s)"
            )
        return sent
    finally:
        controller.cancel_input()
        controller.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=config_paths.default_config("pokemon-fleet.yaml"),
    )
    parser.add_argument("--devices", nargs="+", default=["all"])
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="maximum seconds allowed for one friend request (default: 45)",
    )
    parser.add_argument("--source-url", default=DEFAULT_SOURCE)
    parser.add_argument("--code", action="append", default=[])
    parser.add_argument("--codes-file", type=Path)
    parser.add_argument("--exclude-code", action="append", default=[])
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument(
        "--adb", default=str(ROOT / "adb") if (ROOT / "adb").exists() else "adb"
    )
    parser.add_argument("--check", action="store_true", help="read-only screenshot check")
    parser.add_argument("--plan", action="store_true", help="list devices/codes; do not connect")
    parser.add_argument(
        "--allmachines",
        action="store_true",
        help="run on phones attached to both the controller host and worker host",
    )
    parser.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _friend_file_arguments(arguments: Sequence[str], *, portable: bool) -> list[str]:
    """Resolve private input/state paths separately on each selected host."""
    roots = {"--codes-file": ("@config/", config_paths.private_config_dir()),
             "--history": ("@state/", config_paths.state_dir())}
    result = list(arguments)
    index = 0
    while index < len(result):
        option, separator, inline = result[index].partition("=")
        if option not in roots:
            index += 1
            continue
        value_index = index if separator else index + 1
        if value_index >= len(result):
            break
        value = inline if separator else result[value_index]
        prefix, root = roots[option]
        if portable:
            path = Path(config_paths.expand_user_path(value))
            if not value.startswith("@") and path.is_absolute():
                try:
                    value = prefix + path.resolve().relative_to(root).as_posix()
                except ValueError:
                    pass  # Outside private roots: caller must provide a path available on each host.
        else:
            if value.startswith(prefix):
                value = str(root / value[len(prefix):])
            elif not Path(config_paths.expand_user_path(value)).is_absolute():
                value = str(root / value)
            else:
                value = str(config_paths.expand_user_path(value))
        result[value_index] = option + "=" + value if separator else value
        index = value_index + 1
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    local = fleet_entrypoint.local_requested(raw_arguments)
    raw_arguments = fleet_entrypoint.clean_scope_flags(raw_arguments)
    if not local and not any(value in {"--help", "-h"} for value in raw_arguments):
        if not fleet_entrypoint.has_option(raw_arguments, "--devices"):
            raw_arguments += ["--devices", "all"]
        return fleet_entrypoint.run_all_machines("add_friends.py", _friend_file_arguments(raw_arguments, portable=True))
    if not any(value in {"--help", "-h"} for value in raw_arguments):
        fleet_entrypoint.apply_local_environment()
    args = parse_args(_friend_file_arguments(raw_arguments, portable=False))
    if args.count <= 0:
        raise FriendAutomationError("--count must be greater than zero")
    if args.request_timeout <= 0:
        raise FriendAutomationError("--request-timeout must be greater than zero")
    fleet = pokemon_fleet.load_fleet(args.config)
    try:
        specs = pokemon_fleet.select_devices(
            fleet, args.devices, "friends", allow_unready=args.plan
        )
    except pokemon_fleet.FleetError as exc:
        if args.allow_empty and "no matching connected devices" in str(exc).lower():
            print("No matching connected devices for friends; exiting (--allow-empty set)")
            return 0
        raise
    if not specs and args.allow_empty:
        print("No matching connected devices for friends; exiting (--allow-empty set)")
        return 0

    supplied: list[str] = [normalize_code(value) for value in args.code]
    if args.codes_file:
        supplied.extend(extract_trainer_codes(args.codes_file.read_text()))
    codes = supplied or fetch_public_codes(
        args.source_url,
        minimum_count=max(args.count * 4, args.count + 5),
    )
    source_label = "provided input" if supplied else args.source_url
    excluded = {normalize_code(value) for value in args.exclude_code}
    codes = [code for code in codes if code not in excluded]
    if not codes:
        raise FriendAutomationError("No trainer codes remain after filtering")

    print(f"Loaded {len(codes)} trainer code(s) from {source_label}")
    print("Selected: " + ", ".join(spec.name for spec in specs))
    if args.plan:
        print(f"Would send up to {args.count} request(s) on each selected device")
        return 0

    history = History(args.history)
    stop_event = threading.Event()
    with pokemon_fleet.acquire_device_locks(specs):
        total = 0
        errors: list[str] = []
        executor = ThreadPoolExecutor(
            max_workers=len(specs), thread_name_prefix="pokemon-friends"
        )
        futures: dict[Future[int], pokemon_fleet.DeviceSpec] = {
            executor.submit(
                run_device,
                spec,
                codes,
                args.count,
                history,
                args.adb,
                args.check,
                args.request_timeout,
                stop_event,
            ): spec
            for spec in specs
        }
        try:
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    total += future.result()
                except FriendAutomationCancelled:
                    if not stop_event.is_set():
                        message = f"{spec.name}: cancelled unexpectedly"
                        errors.append(message)
                        print(f"[{spec.name}] failed: {message}", file=sys.stderr, flush=True)
                except Exception as exc:
                    message = str(exc)
                    if not message.startswith(f"{spec.name}:"):
                        message = f"{spec.name}: {message}"
                    errors.append(message)
                    print(f"[{spec.name}] failed: {exc}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            stop_event.set()
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        if errors:
            raise FriendAutomationError("; ".join(errors))
    if not args.check:
        print(f"Sent {total} friend request(s) across {len(specs)} device(s)")
    return 0


def stop_ios_automation(config_path: Path) -> None:
    try:
        fleet = pokemon_fleet.load_fleet(config_path)
        from sources.ios_wda_cleanup import stop_wda_runner

        for spec in fleet.devices.values():
            if spec.platform == "ios" and spec.identifier:
                stop_wda_runner(spec.identifier)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Friend automation cancelled", file=sys.stderr)
        stop_ios_automation(parse_args().config)
        raise SystemExit(130)
    except (
        FriendAutomationError,
        pokemon_fleet.FleetError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
