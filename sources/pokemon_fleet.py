#!/usr/bin/env python3
"""Safely coordinate Pokemon GO automation across registered iOS/Android devices.

The fleet command is the single entry point for status, gifts, filtered deletes,
trades, and friendly battles.  It never creates an Appium session for ``status``
or ``--plan``.  Live commands lock every selected device so two automations cannot
control the same phone at once.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Sequence
from urllib.error import URLError
from urllib.request import urlopen

import yaml

from . import config_paths, ios_attached_devices


SOURCE_DIR = Path(__file__).resolve().parent
ROOT = SOURCE_DIR.parent
LOCK_DIR = Path("/tmp/pokemon-go-fleet")
ARTIFACT_ROOT = config_paths.state_dir()
FLEET_DIAGNOSTICS_DIR = ARTIFACT_ROOT / "fleet" / "diagnostics"
BATTLE_CALIBRATION_DIR = ARTIFACT_ROOT / "battle-calibration"
IOS_REQUIRED_DEVICE_KEYS = ("udid", "team_id", "wda_bundle_id")
TRADE_REQUIRED = {"TRADE_BTN", "FIRST_PKMN_BTN", "NEXT_BTN", "CONFIRM_BTN", "X_BTN"}
TRADE_OPTIONAL = {
    "MAX_LEVEL_RESET_BTN",
    "POWER_UP_CANCEL_BTN",
    "IOS_TRADING_UNAVAILABLE_OK_BTN",
    # Both are recovery's business rather than a trade step's, and both are
    # iOS-only: Android answers the cancel dialog and leaves these screens with
    # BACK. Listed here so a fleet config that maps them is checked like any
    # other coordinate instead of being carried through unread.
    "TRADE_CANCEL_YES_BTN",
    "TRADE_EXIT_BTN",
}
BATTLE_REQUIRED = {"BATTLE_BTN", "USE_PARTY_BTN", "REMATCH_BTN"}
BATTLE_SURRENDER = {"RUN_BTN", "SURRENDER_BTN"}
BATTLE_STEPS = BATTLE_REQUIRED | BATTLE_SURRENDER
GIFT_KEYS = {
    "OPEN_BTN",
    "SEND_GIFT_BTN",
    "FIRST_GIFT_BTN",
    "SEND_BTN",
    "CLOSE_BTN",
    "SORT_BTN",
    "GIFT_SORT_BTN",
    "CAN_RECEIVE_GIFT_BTN",
    "NEXT_FRIEND_BTN",
}
DELETE_KEYS = {
    "FIRST_TILE_BTN",
    "TILE_PITCH",
    "X_BTN",
    "TRASH_MENU_BTN",
    "TRANSFER_BTN",
    "TRANSFER_YES_BTN",
    "TRANSFER_NO_BTN",
    "FILTER_CLEAR_BTN",
}
BERRY_KEYS = {"BERRY_BTN", "NEXT_MON_FROM", "NEXT_MON_TO"}
BERRY_BANDS = {"CARD_BAND_Y", "BERRY_DISC_Y"}
GBL_KEYS = {"GBL_MOVE_BTN"}
# How patient the start-up screen check is before it calls a phone unready.
GBL_START_LOOKS = 5
GBL_START_LOOK_GAP = 1.0


class FleetError(RuntimeError):
    pass


class TradeStopped(FleetError):
    """A trade that failed for a reason another cycle is not going to change.

    The cycle retry exists for the flaky half of this work — a dropped tap, a
    screen read mid-animation. Some stops are not that: a picker holding
    nothing this trade is allowed to take answers the same way however many
    times it is asked, and each retry walks both phones into another trade for
    the game to cancel, which is where the "Trade expired." notices come from.
    """


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    platform: str
    enabled: bool
    config: dict[str, Any]
    base_dir: Path

    @property
    def identifier(self) -> str:
        if self.platform == "android":
            return str(self.config.get("serial", ""))
        try:
            return str(load_appium_profile(self)["device"]["udid"])
        except (FleetError, KeyError):
            return ""

    def supports(self, operation: str) -> bool:
        if operation == "friends":
            return True
        operations = self.config.get("operations", {})
        return isinstance(operations, dict) and operation in operations

    def operation(self, operation: str) -> dict[str, Any]:
        operations = self.config.get("operations")
        value = operations.get(operation) if isinstance(operations, dict) else None
        if value is True:
            return {}
        if not isinstance(value, dict):
            raise FleetError(f"{self.name} does not support {operation}")
        return value


@dataclass(frozen=True)
class FleetConfig:
    path: Path
    devices: dict[str, DeviceSpec]


@dataclass
class DeviceStatus:
    name: str
    platform: str
    identifier: str
    connected: bool
    operations: dict[str, str]
    detail: str = ""


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise FleetError(f"Config not found: {path}. Run `pogo init-config` or set POGO_CONFIG_DIR to your private configuration directory.") from exc
    except yaml.YAMLError as exc:
        raise FleetError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FleetError(f"Config root must be a YAML object: {path}")
    return value


def resolve_path(value: Any, base_dir: Path, label: str) -> Path:
    try:
        return config_paths.find_config(value, base_dir, label)
    except config_paths.ConfigPathError as exc:
        raise FleetError(str(exc)) from exc


def load_fleet(path: Path) -> FleetConfig:
    root = load_yaml(path)
    if root.get("version") != 1:
        raise FleetError("pokemon-fleet.yaml version must be 1")
    raw_devices = root.get("devices")
    if not isinstance(raw_devices, dict) or not raw_devices:
        raise FleetError("Fleet config needs a nonempty devices object")
    devices: dict[str, DeviceSpec] = {}
    for name, raw in raw_devices.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise FleetError(f"Invalid device name: {name!r}")
        if not isinstance(raw, dict):
            raise FleetError(f"Device {name} must be a YAML object")
        platform = raw.get("platform")
        if platform not in {"ios", "android"}:
            raise FleetError(f"Device {name} platform must be ios or android")
        if platform == "android" and not str(raw.get("serial", "")).strip():
            raise FleetError(f"Android device {name} needs serial")
        devices[name] = DeviceSpec(
            name=name,
            platform=platform,
            enabled=raw.get("enabled", True) is True,
            config=raw,
            base_dir=path.resolve().parent,
        )
    fleet = FleetConfig(path=path.resolve(), devices=devices)
    validate_unique_ios_resources(fleet)
    return fleet


def load_appium_profile(spec: DeviceSpec) -> dict[str, Any]:
    path = resolve_path(spec.config.get("appium_config"), spec.base_dir, f"{spec.name}.appium_config")
    profile = load_yaml(path)
    device = profile.get("device")
    if not isinstance(device, dict):
        raise FleetError(f"{path} needs a device object")
    for key in IOS_REQUIRED_DEVICE_KEYS:
        if not isinstance(device.get(key), str) or not device[key].strip():
            raise FleetError(f"{path}: device.{key} must be set")
    server_url = profile.get("server_url", "http://127.0.0.1:4723")
    if not isinstance(server_url, str) or not server_url.startswith(("http://", "https://")):
        raise FleetError(f"{path}: server_url must be an HTTP URL")
    return {"path": path, "device": device, "server_url": server_url.rstrip("/")}


def validate_unique_ios_resources(fleet: FleetConfig) -> None:
    seen: dict[tuple[str, Any], str] = {}
    for spec in fleet.devices.values():
        if spec.platform != "ios" or not spec.enabled:
            continue
        profile = load_appium_profile(spec)
        device = profile["device"]
        for key in ("udid", "wda_local_port", "mjpeg_server_port", "derived_data_path"):
            value = device.get(key)
            if value in (None, ""):
                if key in {"wda_local_port", "mjpeg_server_port"}:
                    raise FleetError(
                        f"{spec.name} needs device.{key}; simultaneous iPhones require unique ports"
                    )
                continue
            token = (key, value)
            if token in seen:
                raise FleetError(f"{spec.name} and {seen[token]} share device.{key}={value!r}")
            seen[token] = spec.name


def validate_point(name: str, point: Any, *, allow_null: bool = False) -> list[int] | None:
    if allow_null and point is None:
        return None
    if not isinstance(point, list) or len(point) != 2 or not all(type(v) is int for v in point):
        raise FleetError(f"{name} must be [x, y] integer logical points")
    if point == [0, 0]:
        raise FleetError(f"{name} is still the [0, 0] calibration placeholder")
    return point


def ios_operation_readiness(spec: DeviceSpec, operation: str) -> str:
    try:
        registered_profile = load_appium_profile(spec)
        if operation == "friends":
            return "ready (Appium profile checked; connection checked on launch)"
        op = spec.operation(operation)
        if op.get("calibrated") is False:
            return "needs calibration"
        if operation == "gifts":
            path = resolve_path(op.get("config"), spec.base_dir, f"{spec.name}.operations.gifts.config")
            gift_root = load_yaml(path)
            points = gift_root.get("coordinates")
            if not isinstance(points, dict):
                raise FleetError(f"{path} needs coordinates")
            assert_same_ios_profile(spec.name, registered_profile["device"], gift_root.get("device"), path)
            for key in GIFT_KEYS:
                validate_point(f"{path}:{key}", points.get(key))
        elif operation == "delete":
            path = resolve_path(op.get("config"), spec.base_dir, f"{spec.name}.operations.delete.config")
            delete_root = load_yaml(path)
            points = delete_root.get("coordinates")
            if not isinstance(points, dict):
                raise FleetError(f"{path} needs coordinates")
            appium_path = resolve_path(
                delete_root.get("appium_config"), path.parent, f"{path}:appium_config"
            )
            appium_device = load_yaml(appium_path).get("device")
            assert_same_ios_profile(spec.name, registered_profile["device"], appium_device, appium_path)
            for key in DELETE_KEYS:
                validate_point(f"{path}:{key}", points.get(key))
        elif operation == "berries":
            path = resolve_path(op.get("config"), spec.base_dir, f"{spec.name}.operations.berries.config")
            berry_root = load_yaml(path)
            points = berry_root.get("coordinates")
            detection = berry_root.get("detection")
            if not isinstance(points, dict) or not isinstance(detection, dict):
                raise FleetError(f"{path} needs coordinates and detection")
            for key in BERRY_KEYS:
                validate_point(f"{path}:{key}", points.get(key))
            for key in BERRY_BANDS:
                band = detection.get(key)
                if (
                    not isinstance(band, list)
                    or len(band) != 2
                    or not all(type(value) is int for value in band)
                    or not 0 <= band[0] < band[1] <= 1000
                ):
                    raise FleetError(f"{path}:detection.{key} must be a per-mille band")
            appium_path = resolve_path(
                berry_root.get("appium_config"), path.parent, f"{path}:appium_config"
            )
            appium_device = load_yaml(appium_path).get("device")
            assert_same_ios_profile(spec.name, registered_profile["device"], appium_device, appium_path)
        elif operation == "gbl":
            path = resolve_path(op.get("config"), spec.base_dir, f"{spec.name}.operations.gbl.config")
            gbl_root = load_yaml(path)
            points = gbl_root.get("coordinates")
            if not isinstance(points, dict):
                raise FleetError(f"{path} needs coordinates")
            for key in GBL_KEYS:
                validate_point(f"{path}:{key}", points.get(key))
            appium_path = resolve_path(
                gbl_root.get("appium_config"), path.parent, f"{path}:appium_config"
            )
            appium_device = load_yaml(appium_path).get("device")
            assert_same_ios_profile(spec.name, registered_profile["device"], appium_device, appium_path)
        elif operation == "trade":
            points = op.get("coordinates")
            if not isinstance(points, dict):
                raise FleetError(f"{spec.name} trade needs coordinates")
            for key in TRADE_REQUIRED:
                validate_point(f"{spec.name}.trade.{key}", points.get(key))
            for key in TRADE_OPTIONAL:
                if key in points:
                    validate_point(f"{spec.name}.trade.{key}", points[key], allow_null=True)
        elif operation == "battle":
            points = op.get("coordinates")
            guards = op.get("guards")
            if not isinstance(points, dict):
                raise FleetError(f"{spec.name} battle needs coordinates")
            for key in BATTLE_REQUIRED:
                validate_point(f"{spec.name}.battle.{key}", points.get(key))
            if not isinstance(guards, dict):
                raise FleetError(f"{spec.name} battle needs visual guards")
            for key in points.keys() & BATTLE_STEPS:
                validate_battle_guard(spec.name, key, guards.get(key))
        return "ready"
    except FleetError as exc:
        return f"unavailable: {exc}"


def assert_same_ios_profile(
    device_name: str,
    registered: dict[str, Any],
    operation_device: Any,
    operation_path: Path,
) -> None:
    if not isinstance(operation_device, dict):
        raise FleetError(f"{operation_path} needs a device object")
    for key in ("udid", "wda_local_port", "mjpeg_server_port"):
        if operation_device.get(key) != registered.get(key):
            raise FleetError(
                f"{device_name} {operation_path.name} device.{key} does not match its fleet Appium profile"
            )


def validate_battle_guard(device_name: str, step: str, value: Any) -> None:
    if not isinstance(value, dict):
        raise FleetError(f"{device_name}.battle.guards.{step} is not calibrated")
    for key in ("min_saturation", "min_brightness"):
        number = value.get(key)
        if not isinstance(number, (int, float)) or not 0 <= number <= 255:
            raise FleetError(f"{device_name}.battle.guards.{step}.{key} must be 0..255")
    max_brightness = value.get("max_brightness", 255)
    if not isinstance(max_brightness, (int, float)) or not 0 <= max_brightness <= 255:
        raise FleetError(f"{device_name}.battle.guards.{step}.max_brightness must be 0..255")
    reference = value.get("reference_rgb")
    if (
        not isinstance(reference, list)
        or len(reference) != 3
        or not all(isinstance(channel, (int, float)) and 0 <= channel <= 255 for channel in reference)
    ):
        raise FleetError(f"{device_name}.battle.guards.{step}.reference_rgb must be three values 0..255")
    tolerance = value.get("max_color_distance")
    if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 255:
        raise FleetError(f"{device_name}.battle.guards.{step}.max_color_distance must be 0..255")


def operation_readiness(spec: DeviceSpec, operation: str) -> str:
    if not spec.enabled:
        return "disabled"
    if not spec.supports(operation):
        return "not configured"
    if spec.platform == "ios":
        return ios_operation_readiness(spec, operation)
    if operation == "friends":
        return "ready (connection checked on launch)"
    try:
        op = spec.operation(operation)
        if operation == "battle":
            if op.get("calibrated") is not True:
                return "needs visual-guard calibration"
            guarded_steps = set(BATTLE_REQUIRED)
            if op.get("surrenders") is True:
                guarded_steps |= BATTLE_SURRENDER
            guards = op.get("guards")
            if not isinstance(guards, dict):
                raise FleetError(f"{spec.name} battle needs visual guards")
            for step in guarded_steps:
                validate_battle_guard(spec.name, step, guards.get(step))
        return "ready (on-device config checked when connected)"
    except FleetError as exc:
        return f"unavailable: {exc}"


class ProbeError(FleetError):
    """A connectivity question could not be answered, one way or the other.

    Distinct from an answer of "nothing attached".  A caller that acts on a
    disconnect -- by stopping a run, say -- must not act on a probe that simply
    failed to run: live, `adb` was missing from PATH, `adb_states` turned that
    into `{}`, and a watchdog killed a trade with both phones plugged in.
    """


def default_adb_binary() -> str:
    """The adb this tree drives phones with.

    Not whatever `adb` PATH happens to resolve to -- usually nothing.  The
    platform-tools binary sitting next to this file is the one the automation
    itself uses, so connectivity checks have to ask the same one.
    """
    local = ROOT / "adb"
    return str(local) if local.exists() else "adb"


def adb_states_or_raise(adb_binary: str | None = None) -> dict[str, str]:
    """Every serial adb can see, or ProbeError if adb could not be asked."""
    binary = adb_binary or default_adb_binary()
    try:
        result = subprocess.run(
            [binary, "devices"], capture_output=True, text=True, timeout=8, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProbeError(f"could not run {binary} devices: {error}") from error
    if result.returncode != 0:
        raise ProbeError(f"{binary} devices failed: {result.stderr.strip()}")
    states: dict[str, str] = {}
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def adb_states(adb_binary: str = "adb") -> dict[str, str]:
    """As above, but a failed probe reads as an empty bus.

    Kept for the callers that only list or display: `status` wants to print an
    empty table rather than raise.  Anything that *acts* on the answer wants
    `adb_states_or_raise`.
    """
    try:
        return adb_states_or_raise(adb_binary)
    except ProbeError:
        return {}


def ios_device_is_connected(device: dict[str, Any]) -> bool:
    connection = device.get("connectionProperties", {})
    if not isinstance(connection, dict):
        return False
    # A phone paired over Wi-Fi reports `localNetwork`; only `wired` (USB) can carry
    # WebDriverAgent reliably without a separately created RemoteXPC tunnel.
    return connection.get("transportType") == "wired"


def ios_connected_udids() -> set[str]:
    """As `ios_connected_udids_or_raise`, but every failure reads as empty."""
    try:
        return ios_connected_udids_or_raise()
    except ProbeError:
        return set()


def ios_connected_udids_or_raise() -> set[str]:
    """Every attached iPhone, or ProbeError if none of the three ways worked.

    devicectl, usbmux and xctrace each fail in their own weather.  Falling
    through all three used to return an empty set, which reads as "no iPhones
    are plugged in" -- the same lie the adb path told.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="pokemon-fleet-devices-") as directory:
            output = Path(directory) / "devices.json"
            primary = subprocess.run(
                ["xcrun", "devicectl", "list", "devices", "--json-output", str(output)],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
            if primary.returncode == 0 and output.exists():
                payload = json.loads(output.read_text())
                devices = payload.get("result", {}).get("devices", [])
                found = set()
                for device in devices:
                    if not ios_device_is_connected(device):
                        continue
                    udid = device.get("hardwareProperties", {}).get("udid")
                    if isinstance(udid, str):
                        found.add(udid)
                return found
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

    try:
        usbmux_found = ios_attached_devices.usb_udids()
        if usbmux_found:
            return usbmux_found
    except ios_attached_devices.AttachedDeviceError:
        pass

    try:
        result = subprocess.run(
            ["xcrun", "xctrace", "list", "devices"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        # xctrace also prints paired phones under a separate "Devices Offline"
        # heading. Only parse the first live-device section.
        live = result.stdout.split("== Devices Offline ==", 1)[0]
        return set(re.findall(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{16,}\b", live))

    raise ProbeError("devicectl, usbmux and xctrace all failed to list iPhones")


def appium_ready(url: str) -> bool:
    try:
        with urlopen(f"{url.rstrip('/')}/status", timeout=3) as response:
            value = json.load(response)
        return isinstance(value, dict) and "value" in value
    except (OSError, URLError, ValueError):
        return False


# Appium is not installed the same way on every Mac in the fleet. One has it
# global under Homebrew; another only ever got it as a dependency of the
# trading checkout, so nothing named `appium` is on PATH there and the leg
# died at "launcher was not found" even though a working 3.6.0 sat in
# node_modules. Those local copies are `.bin` shims with a `#!/usr/bin/env
# node` line, so node has to be on PATH for them -- it is on both Macs.
def appium_launcher_candidates() -> list[Path]:
    override = os.environ.get("POKEMON_GO_APPIUM_BIN")
    candidates = [Path(override)] if override else []
    found = shutil.which("appium")
    if found:
        candidates.append(Path(found))
    candidates.append(Path("/opt/homebrew/bin/appium"))
    candidates.append(ROOT / "node_modules" / ".bin" / "appium")
    candidates.append(Path.home() / "ios-gifter" / "node_modules" / ".bin" / "appium")
    return candidates


def find_appium_launcher() -> Path | None:
    for candidate in appium_launcher_candidates():
        if candidate.exists():
            return candidate
    return None


def ensure_appium_server(url: str = "http://127.0.0.1:4723", timeout: float = 12.0) -> None:
    if appium_ready(url):
        return
    env = dict(os.environ)
    env.setdefault("APPIUM_XCUITEST_PREFER_DEVICECTL", "true")
    script = ROOT / "bin" / "start-ios-appium.command"
    appium_bin = find_appium_launcher()
    if script.is_file():
        subprocess.Popen(["/bin/zsh", str(script)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif appium_bin is not None:
        # A driver installed alongside a local Appium is only found when
        # APPIUM_HOME names that install's own project root. Started from
        # anywhere else the server comes up healthy and then turns the leg away
        # with "Could not find driver automationName 'XCUITest'", which reads
        # like a missing driver rather than a server started in the wrong
        # place. Deriving it from the launcher keeps the two from drifting.
        if appium_bin.parent.name == ".bin" and appium_bin.parent.parent.name == "node_modules":
            env.setdefault("APPIUM_HOME", str(appium_bin.parent.parent.parent))
        subprocess.Popen(
            [str(appium_bin), "--address", "127.0.0.1", "--port", "4723"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        looked = ", ".join(str(candidate) for candidate in appium_launcher_candidates())
        raise FleetError(
            f"Appium server is offline at {url} and Appium launcher was not found "
            f"(looked at: {looked}; set POKEMON_GO_APPIUM_BIN to point at one)"
        )

    start = time.time()
    while time.time() - start < timeout:
        if appium_ready(url):
            return
        time.sleep(0.5)

    if not appium_ready(url):
        raise FleetError(f"Appium server at {url} failed to respond after starting")



def collect_status(fleet: FleetConfig, adb_binary: str = "adb") -> tuple[list[DeviceStatus], list[str]]:
    android = adb_states(adb_binary)
    ios = ios_connected_udids()
    operations = ("gifts", "delete", "berries", "gbl", "friends", "trade", "battle")
    rows: list[DeviceStatus] = []
    registered_android: set[str] = set()
    appium_states: dict[str, bool] = {}
    for spec in fleet.devices.values():
        ident = spec.identifier
        connected = ident in ios if spec.platform == "ios" else android.get(ident) == "device"
        if spec.platform == "android":
            registered_android.add(ident)
        detail = ""
        if spec.platform == "ios":
            try:
                profile = load_appium_profile(spec)
                server_url = profile["server_url"]
                if server_url not in appium_states:
                    appium_states[server_url] = appium_ready(server_url)
                detail = "Appium ready" if appium_states[server_url] else "Appium offline"
            except FleetError as exc:
                detail = str(exc)
        elif ident in android and android[ident] != "device":
            detail = android[ident]
        rows.append(
            DeviceStatus(
                name=spec.name,
                platform=spec.platform,
                identifier=ident,
                connected=connected,
                operations={op: operation_readiness(spec, op) for op in operations},
                detail=detail,
            )
        )
    unregistered = sorted(serial for serial in android if serial not in registered_android)
    return rows, unregistered


def print_status(rows: Sequence[DeviceStatus], unregistered: Sequence[str], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"devices": [row.__dict__ for row in rows], "unregistered_android": list(unregistered)}, indent=2))
        return
    print("Fleet status (read-only; no Appium sessions created)")
    for row in rows:
        state = "CONNECTED" if row.connected else "offline"
        print(f"\n{row.name} [{row.platform}] {state} {row.identifier}")
        if row.detail:
            print(f"  {row.detail}")
        for operation, readiness in row.operations.items():
            print(f"  {operation:7} {readiness}")
    if unregistered:
        print("\nConnected but unregistered Android devices:")
        for serial in unregistered:
            print(f"  android:{serial}")


def dynamic_android(serial: str, base_dir: Path) -> DeviceSpec:
    return DeviceSpec(
        name=f"android-{serial}",
        platform="android",
        enabled=True,
        config={
            "platform": "android",
            "serial": serial,
            "operations": {
                "gifts": {},
                "delete": {},
                "berries": {},
                "gbl": {},
                "friends": {},
                "trade": {},
                "battle": {"calibrated": False},
            },
        },
        base_dir=base_dir,
    )


def select_devices(
    fleet: FleetConfig,
    names: Sequence[str],
    operation: str,
    *,
    allow_unready: bool = False,
    allow_empty: bool = False,
) -> list[DeviceSpec]:
    if not names:
        raise FleetError("Select at least one device")
    if "all" in names:
        if len(names) != 1:
            raise FleetError("Use 'all' by itself")
        candidates = [
            spec
            for spec in fleet.devices.values()
            if spec.enabled and spec.supports(operation)
        ]
        selected = candidates
        skipped: list[str] = []
        if not allow_unready:
            adb_binary = default_adb_binary()
            android = adb_states(adb_binary)
            ios = ios_connected_udids()
            connected = [
                spec
                for spec in candidates
                if (
                    spec.identifier in ios
                    if spec.platform == "ios"
                    else android.get(spec.identifier) == "device"
                )
            ]
            if not allow_unready or (operation in {"trade", "battle"} and len(connected) == 2):
                selected = connected

                selected_ids = {spec.identifier for spec in selected}
                configured_android_ids = {
                    spec.identifier
                    for spec in fleet.devices.values()
                    if spec.platform == "android"
                }
                for serial, state in android.items():
                    if (
                        state == "device"
                        and serial not in configured_android_ids
                        and serial not in selected_ids
                    ):
                        selected.append(dynamic_android(serial, fleet.path.parent))
                        selected_ids.add(serial)

                skipped = [spec.name for spec in candidates if spec not in selected]
            print(
                attachment_summary(
                    operation,
                    [spec.name for spec in selected],
                    len(skipped),
                    os.environ.get("POGO_MACHINE", ""),
                ),
                flush=True,
            )
            if not allow_unready:
                ready: list[DeviceSpec] = []
                unready: list[str] = []
                for spec in selected:
                    readiness = operation_readiness(spec, operation)
                    if readiness.startswith("ready"):
                        ready.append(spec)
                    else:
                        unready.append(f"{spec.name} ({readiness})")
                selected = ready
                if unready:
                    print(
                        "Skipping unready configured device(s): " + ", ".join(unready),
                        flush=True,
                    )
    else:
        selected = []
        for name in names:
            if name.startswith("android:"):
                serial = name.split(":", 1)[1].strip()
                if not serial:
                    raise FleetError("android:SERIAL needs a serial")
                selected.append(dynamic_android(serial, fleet.path.parent))
                continue
            try:
                selected.append(fleet.devices[name])
            except KeyError as exc:
                raise FleetError(f"Unknown fleet device: {name}") from exc
    if not selected and not allow_empty:
        raise FleetError(f"No devices selected for {operation}")
    identifiers: set[str] = set()
    for spec in selected:
        if not spec.enabled:
            raise FleetError(f"{spec.name} is disabled")
        if not spec.supports(operation):
            raise FleetError(f"{spec.name} does not support {operation}")
        if not allow_unready:
            readiness = operation_readiness(spec, operation)
            if not readiness.startswith("ready"):
                raise FleetError(f"{spec.name} {operation}: {readiness}")
        if spec.identifier in identifiers:
            raise FleetError(f"Device selected twice: {spec.identifier}")
        identifiers.add(spec.identifier)
    return selected


def attachment_summary(
    operation: str, attached: Sequence[str], missing: int, machine: str = ""
) -> str:
    """What this computer is about to work on, said as what it has.

    The fan-out prints one of these per machine, and both lines used to name
    the phones that were *not* there -- two near-identical lists of absences,
    neither saying which computer it came from, from which the reader had to
    work out the one thing they wanted: what is plugged in where.  The phones
    that are missing are counted rather than named; `fleet.py owners` is the
    command for chasing a particular phone.
    """
    where = f"[{machine}] " if machine else ""
    if attached:
        core = f"{operation} on {', '.join(attached)}"
    else:
        core = f"nothing attached for {operation}"
    total = len(attached) + missing
    tail = f" ({missing} of {total} configured not attached)" if missing else ""
    return f"{where}{core}{tail}"


def lock_name(spec: DeviceSpec) -> str:
    token = spec.identifier or spec.name
    return re.sub(r"[^A-Za-z0-9_.-]", "_", token)


def process_command(pid: int) -> str:
    """The holder's command line, or "" when it cannot be read."""
    try:
        listing = subprocess.run(
            ["/bin/ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return listing.stdout.strip().splitlines()[0].strip() if listing.stdout.strip() else ""


def lock_holder(path: Path) -> str:
    """Who is holding this lock, said the way the reader has to act on it.

    The flock is what refused us, so the pid in the file is a live process and
    not the leftover it looks like -- the files outlive their runs, the locks
    do not.  Naming the pid and its command turns "another fleet command" into
    something the operator can find, because the usual holder is not another
    command at all: it is a leg that outlived its supervisor and kept the
    phone (2026-09-21, razr, pid 55317, orphaned 19 minutes).
    """
    try:
        written = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    match = re.search(r"pid=(\d+)", written)
    if not match:
        return ""
    pid = int(match.group(1))
    command = process_command(pid)
    if not command:
        return f"pid {pid}"
    return f"pid {pid} ({command})"


@contextmanager
def acquire_device_locks(
    specs: Sequence[DeviceSpec], lock_dir: Path = LOCK_DIR
) -> Iterator[None]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    handles: list[Any] = []
    try:
        for spec in sorted(specs, key=lock_name):
            path = lock_dir / f"{lock_name(spec)}.lock"
            handle = path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                holder = lock_holder(path)
                blame = f" by {holder}" if holder else ""
                raise FleetError(
                    f"{spec.name} is already controlled{blame}; "
                    f"stop that process (kill -INT) before running this again"
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(
                f"pid={os.getpid()} device={spec.name} "
                f"supervisor={os.environ.get('POKEMON_SUPERVISOR_PID', '-')}\n"
            )
            handle.flush()
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def operation_config_path(spec: DeviceSpec, operation: str) -> Path:
    op = spec.operation(operation)
    return resolve_path(op.get("config"), spec.base_dir, f"{spec.name}.operations.{operation}.config")


CHILD_INTERRUPT_TIMEOUT_SECONDS = 15.0
CHILD_TERMINATE_TIMEOUT_SECONDS = 5.0


async def stop_child(label: str, process: Any) -> None:
    """Stop a fleet child while giving its own cleanup handler time to run."""
    if process.returncode is not None:
        return

    print(f"[{label}] stopping automation...", flush=True)
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(
            process.wait(), timeout=CHILD_INTERRUPT_TIMEOUT_SECONDS
        )
        return
    except asyncio.TimeoutError:
        print(
            f"[{label}] cleanup timed out; terminating child process...",
            flush=True,
        )

    try:
        process.terminate()
    except ProcessLookupError:
        return

    try:
        await asyncio.wait_for(
            process.wait(), timeout=CHILD_TERMINATE_TIMEOUT_SECONDS
        )
        return
    except asyncio.TimeoutError:
        print(f"[{label}] terminate timed out; killing child process...", flush=True)

    try:
        process.kill()
    except ProcessLookupError:
        return
    await process.wait()


async def run_child(label: str, arguments: Sequence[str], *, accepted: Sequence[int] = (0,)) -> int:
    print(f"[{label}] {' '.join(arguments)}", flush=True)
    env = {
        **os.environ,
        "POKEMON_FLEET_CHILD": "1",
        "POKEMON_FLEET_LOCK_HELD": "1",
        # `stop_child` below is skipped when this process is killed outright,
        # and the child then runs on without anybody reading its output.
        "POKEMON_SUPERVISOR_PID": str(os.getpid()),
    }
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout = getattr(process, "stdout", None)
        if stdout is not None:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                sys.stdout.write(line.decode("utf-8", errors="replace"))
                sys.stdout.flush()
        code = await process.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        await stop_child(label, process)
        raise
    if code not in accepted:
        raise FleetError(f"{label} exited with status {code}")
    return code


async def connected_android_gifters(specs: Sequence[DeviceSpec]) -> list[Any]:
    from . import gift_android as gift

    wanted = {spec.identifier for spec in specs}
    found = {device.serial: device for device in await gift.ClientAsync().devices()}
    missing = wanted - found.keys()
    if missing:
        raise FleetError(f"Android not connected/authorized: {', '.join(sorted(missing))}")
    ready = []
    for spec in specs:
        device = found[spec.identifier]
        device.label = spec.name
        try:
            await gift.get_config(device)
            device.display_id = await gift.find_display_id(device)
        except Exception as exc:
            raise FleetError(f"Could not prepare {spec.name} for gifts: {exc}") from exc
        ready.append(device)
    return ready


# `berry_ios --if-feed-screen` exits with this when the iPhone is not parked on
# a gym's feeding screen; it is `berry_ios.NOT_A_FEED_SCREEN`, kept here so the
# fleet does not import the Appium stack to read one number.
IOS_NOT_A_FEED_SCREEN = 3


def ios_gift_command(spec: DeviceSpec, args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "sources.gift_ios",
        "--config",
        str(operation_config_path(spec, "gifts")),
    ]
    if args.all:
        command.extend(["--all", "--max-cycles", str(args.max_cycles)])
    else:
        command.extend(["--count", str(args.count)])
    if args.no_guard:
        command.append("--no-guard")
    return command


def ios_berry_command(spec: DeviceSpec, args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
            "-m",
            "sources.berry_ios",
        "--config",
        str(operation_config_path(spec, "berries")),
        "--fleet-child",
    ]
    spend = args.spend_overrides.get(spec.name, args.spend)
    if spend is not None:
        command.extend(["--spend", str(spend)])
    return command


def set_berry_budgets(berry_module: Any, specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    berry_module.SPEND_CAP = args.spend
    berry_module.SPEND_CAPS = {
        spec.identifier: args.spend_overrides[spec.name]
        for spec in specs
        if spec.name in args.spend_overrides
    }


async def android_feed_screens(specs: Sequence[DeviceSpec]) -> set[str]:
    """Serials of the Android phones parked on a gym's feeding screen.

    `gift.py` feeds those and gifts from the rest.  A phone whose berry config
    will not load, or whose screen will not read, goes to the gifts -- what the
    command did with every phone before it fed berries too.
    """
    from . import berry_android as berry

    found = {device.serial: device for device in await berry.ClientAsync().devices()}
    feeding: set[str] = set()
    for spec in specs:
        device = found.get(spec.identifier)
        if device is None:
            continue
        device.label = spec.name
        try:
            await berry.get_config(device)
            device.display_id = await berry.find_display_id(device)
            frame = await berry.screencap_raw(device)
        except Exception as exc:
            print(f"[{spec.name}] Could not look for a gym feeding screen ({exc}); sending gifts", flush=True)
            continue
        if (
            frame is not None
            and berry.feed_screen_metrics(device, frame)[2]
            and not berry.encounter_showing(*frame)
        ):
            feeding.add(spec.identifier)
    return feeding


async def ios_gifts_or_berries(spec: DeviceSpec, args: argparse.Namespace) -> None:
    """Feed the gym this iPhone is parked on, or else send its gifts.

    Only the berry worker can read the feeding screen, so it goes first and
    steps aside, touching nothing, when the phone is somewhere else.
    """
    code = await run_child(
        spec.name,
        [*ios_berry_command(spec, args), "--if-feed-screen"],
        accepted=(0, IOS_NOT_A_FEED_SCREEN),
    )
    if code == IOS_NOT_A_FEED_SCREEN:
        await run_child(spec.name, ios_gift_command(spec, args))


async def run_gifts(specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    """Send gifts, except from a phone parked on a gym's feeding screen.

    That phone feeds the gym's defenders instead, so one command covers both:
    leave each phone on the screen for the job it should do.  `--gifts-only`
    skips the look and gifts from every phone, as the command used to.
    """
    berries_too = not getattr(args, "gifts_only", True)
    ios = [spec for spec in specs if spec.platform == "ios"]
    android = [spec for spec in specs if spec.platform == "android"]
    tasks: list[Any] = []
    if android:
        feeding = await android_feed_screens(android) if berries_too else set()
        gifters = [spec for spec in android if spec.identifier not in feeding]
        feeders = [spec for spec in android if spec.identifier in feeding]
        if gifters:
            count = (
                args.android_count
                if args.all and args.android_count is not None
                else args.max_cycles if args.all else args.count
            )
            if not isinstance(count, int) or count < 1:
                raise FleetError("Android gifts need a positive cycle safety cap")
            from . import gift_android as gift

            devices = await connected_android_gifters(gifters)
            tasks.append(
                asyncio.create_task(gift.gift_process(devices, count, all_mode=args.all))
            )
        if feeders:
            from . import berry_android

            print(
                "On a gym feeding screen, feeding berries: "
                + ", ".join(spec.name for spec in feeders),
                flush=True,
            )
            set_berry_budgets(berry_android, feeders, args)
            devices = await connected_android_berry_devices(feeders)
            tasks.append(asyncio.create_task(berry_android.berry_process(devices)))
    for spec in ios:
        if berries_too and operation_readiness(spec, "berries").startswith("ready"):
            tasks.append(asyncio.create_task(ios_gifts_or_berries(spec, args)))
        else:
            tasks.append(asyncio.create_task(run_child(spec.name, ios_gift_command(spec, args))))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise FleetError("; ".join(str(error) for error in errors))


def require_delete_authorization(confirm_delete: bool) -> None:
    if not confirm_delete:
        raise FleetError(
            "Filtered deletion is irreversible. Re-run with --confirm-delete only after reviewing "
            "the visible nonempty filter on every selected phone."
        )


async def run_deletes(specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    require_delete_authorization(args.confirm_delete)
    ios = [spec for spec in specs if spec.platform == "ios"]
    android = [spec for spec in specs if spec.platform == "android"]
    tasks: list[Any] = []
    if android:
        from . import luckytrash_android as luckyTrash

        devices = await luckyTrash.setup([spec.identifier for spec in android])
        limit = args.max_transfers if args.all else args.count
        tasks.append(asyncio.create_task(luckyTrash.trash_all(devices, limit)))
    for spec in ios:
        command = [
            sys.executable,
                "-m",
                "sources.luckytrash_ios",
            "--config",
            str(operation_config_path(spec, "delete")),
            "--confirm-delete",
        ]
        if args.all:
            command.extend(["--all", "--max-transfers", str(args.max_transfers)])
        else:
            command.extend(["--count", str(args.count)])
        tasks.append(asyncio.create_task(run_child(spec.name, command)))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise FleetError("; ".join(str(error) for error in errors))


async def connected_android_berry_devices(specs: Sequence[DeviceSpec]) -> list[Any]:
    from . import berry_android as berry

    wanted = {spec.identifier for spec in specs}
    found = {device.serial: device for device in await berry.ClientAsync().devices()}
    missing = wanted - found.keys()
    if missing:
        raise FleetError(f"Android not connected/authorized: {', '.join(sorted(missing))}")
    ready = []
    for spec in specs:
        device = found[spec.identifier]
        device.label = spec.name
        try:
            await berry.get_config(device)
            device.display_id = await berry.find_display_id(device)
        except Exception as exc:
            raise FleetError(f"Could not prepare {spec.name} for berries: {exc}") from exc
        ready.append(device)
    return ready


async def check_android_berry_screen(device: Any) -> None:
    from . import berry_android as berry

    frame = await berry.screencap_raw(device)
    if frame is None:
        raise FleetError(f"Could not capture {device.label}")
    disc, ink, matched = berry.feed_screen_metrics(device, frame)
    found = berry.find_berry(*frame, device.config["BERRY_BTN"])
    share = berry.gold_share_of(*frame, device.config["BERRY_BTN"])
    print(
        f"[{device.label}] disc={disc:.1f}; name_ink={ink}; "
        f"feed_screen={matched}; berry={found}; gold_share={share}",
        flush=True,
    )
    if not matched or found is None:
        raise FleetError(f"{device.label} berry screen check failed; no taps sent")


async def run_berries(specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    ios = [spec for spec in specs if spec.platform == "ios"]
    android = [spec for spec in specs if spec.platform == "android"]
    tasks: list[Any] = []
    android_devices: list[Any] = []
    berry_module: Any = None

    if android:
        from . import berry_android as berry_module

        android_devices = await connected_android_berry_devices(android)
        if args.check:
            tasks.extend(
                asyncio.create_task(check_android_berry_screen(device))
                for device in android_devices
            )
        else:
            set_berry_budgets(berry_module, android, args)
            tasks.append(asyncio.create_task(berry_module.berry_process(android_devices)))

    for spec in ios:
        command = ios_berry_command(spec, args)
        if args.check:
            command.extend(
                [
                    "--check",
                    "--screenshot",
                    str(ARTIFACT_ROOT / "berries" / f"{spec.name}-check.png"),
                ]
            )
        tasks.append(asyncio.create_task(run_child(spec.name, command)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise FleetError("; ".join(str(error) for error in errors))


async def connected_android_gbl_devices(specs: Sequence[DeviceSpec]) -> list[Any]:
    from . import gbl_android as gbl

    wanted = {spec.identifier for spec in specs}
    found = {device.serial: device for device in await gbl.ClientAsync().devices()}
    missing = wanted - found.keys()
    if missing:
        raise FleetError(f"Android not connected/authorized: {', '.join(sorted(missing))}")
    ready = []
    for spec in specs:
        device = found[spec.identifier]
        device.label = spec.name
        try:
            await gbl.get_config(device)
            device.display_id = await gbl.find_display_id(device)
        except Exception as exc:
            raise FleetError(f"Could not prepare {spec.name} for GBL: {exc}") from exc
        if "GBL_MOVE_BTN" not in device.config:
            print(f"[{spec.name}] No GBL_MOVE_BTN in its on-device config; skipping", flush=True)
            continue
        # Tap/swipe batches are sized from this, and it is per handset. Without
        # it the first fast attack of the first battle raises and takes every
        # Android phone on this host down with it.
        await gbl.prepare_device_timing(device)
        ready.append(device)
    if not ready:
        raise FleetError("No selected Android phone has GBL_MOVE_BTN mapped")
    return ready


async def check_android_gbl_screen(device: Any) -> None:
    from . import gbl_android as gbl

    profile = gbl.gbl_strategy.load_strategy_profile(
        device_name=str(device.label).strip()
    )

    # Result screens animate their button in, so one frame is not enough to
    # call the phone unready: the first look often lands on GOOD EFFORT!
    # before NEXT BATTLE has faded up.  Look a few times before refusing.
    for attempt in range(GBL_START_LOOKS):
        if attempt:
            await asyncio.sleep(GBL_START_LOOK_GAP)
        frame = await gbl.screencap_raw(device)
        if frame is None:
            raise FleetError(f"Could not capture {device.label}")
        try:
            state, point, vision_label, _ = await gbl.smart_screen_state(
                frame,
                profile,
                probe_unknown=True,
            )
        except gbl.gbl_vision.VisionOCRError:
            state, point = gbl.read_screen_state(frame)
            vision_label = None
        if state != "battle":
            break
    print(
        f"[{device.label}] {frame[0]}x{frame[1]} -> {state} at {point}; "
        f"pill={gbl.find_pill(frame)}; orange={gbl.find_orange(frame)}; "
        f"cards={gbl.league_cards(frame)}; move={device.config['GBL_MOVE_BTN']}",
        flush=True,
    )
    if vision_label:
        print(f"[{device.label}] Vision decision: {vision_label}", flush=True)
    if state == "battle":
        # 'battle' is the fallback verdict, so it also means "nothing found".
        raise FleetError(
            f"{device.label} shows no BATTLE pill, league list or reward button; no taps sent"
        )


async def run_gbl(specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    ios = [spec for spec in specs if spec.platform == "ios"]
    android = [spec for spec in specs if spec.platform == "android"]
    tasks: list[Any] = []

    if android:
        from . import gbl_android as gbl_module

        android_devices = await connected_android_gbl_devices(android)
        if getattr(args, "trace", None) and not args.check:
            gbl_module.TRACE_DIR = Path(args.trace) / "android"
            gbl_module.TRACE_DIR.mkdir(parents=True, exist_ok=True)
            print(f"Tracing Android frames to {gbl_module.TRACE_DIR}", flush=True)
        if args.check:
            tasks.extend(
                asyncio.create_task(check_android_gbl_screen(device))
                for device in android_devices
            )
        else:
            tasks.append(
                asyncio.create_task(gbl_module.gbl_process(android_devices, args.count))
            )

    for spec in ios:
        command = [
            sys.executable,
            "-m",
            "sources.gbl_ios",
            "--config",
            str(operation_config_path(spec, "gbl")),
            "--fleet-child",
        ]
        if args.check:
            command.extend(
                [
                    "--check",
                    "--screenshot",
                    str(ARTIFACT_ROOT / "gbl" / f"{spec.name}-check.png"),
                ]
            )
        else:
            command.extend(["--battles", str(args.count)])
            if getattr(args, "trace", None):
                command.extend(["--trace", str(Path(args.trace) / spec.name)])
        tasks.append(asyncio.create_task(run_child(spec.name, command)))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise FleetError("; ".join(str(error) for error in errors))


class TradeController:
    def __init__(self, spec: DeviceSpec, backend: Any, coordinates: dict[str, list[int] | None]):
        self.spec = spec
        self.backend = backend
        self.coordinates = coordinates

    def point(self, name: str) -> list[int] | None:
        value = self.coordinates.get(name)
        return value if isinstance(value, list) else None

    async def screenshot(self) -> Any:
        if self.spec.platform == "ios":
            return await self.backend.screenshot_image()
        from . import trade_ios_android as cross

        return await cross.android_screenshot_image(self.backend)

    def image_point(self, image: Any, name: str) -> tuple[int, int] | None:
        point = self.point(name)
        if point is None:
            return None
        if self.spec.platform == "ios":
            return self.backend.image_point(image, name)
        return point[0], point[1]

    def point_from_image(self, image: Any, pixel: tuple[int, int]) -> tuple[int, int]:
        """The inverse of image_point, for buttons located by looking."""
        if self.spec.platform == "ios":
            return self.backend.point_from_image(image, pixel)
        return pixel[0], pixel[1]

    async def tap_step(self, name: str) -> None:
        point = self.point(name)
        if point is None:
            raise FleetError(f"{self.spec.name} has no {name} coordinate")
        if self.spec.platform == "ios":
            await self.backend.tap_step(name)
        else:
            from . import trade_android as android_trade

            await android_trade.tap(self.backend, point)

    async def tap_point(self, point: tuple[int, int] | list[int]) -> None:
        if self.spec.platform == "ios":
            await self.backend.tap_point((point[0], point[1]))
        else:
            from . import trade_android as android_trade

            await android_trade.tap(self.backend, [point[0], point[1]])

    async def game_in_front(self) -> bool | None:
        """Whether Pokémon GO is the app on screen, or None if unanswerable.

        The map and the launcher look the same to a pixel test — the game's
        icon is a pokéball too — and the difference matters: one is a phone
        that wandered out of the trade, the other is a phone with the game
        shut down behind it.
        """
        if self.spec.platform != "android":
            return None
        try:
            focus = await self.backend.shell("dumpsys window | grep mCurrentFocus")
        except Exception:
            return None
        return GAME_PACKAGE in focus

    async def cleanup(self) -> None:
        if self.spec.platform == "ios":
            await self.backend.post_trade_cleanup()
        else:
            from . import trade_ios_android as cross

            await cross.android_post_trade_cleanup(self.backend)

    async def back(self) -> None:
        """Leave a trade lobby.

        Android's BACK key steps out of a "waiting for ..." lobby in one press.
        It is not a general escape: pressed inside an open trade it raises "Do
        you want to cancel the trade?" instead, so step_towards_friend only
        sends it at a lobby it has recognised. iOS has no such key and gets the
        close button, which the lobby answers to as well.
        """
        if self.spec.platform == "ios":
            point = self.point("X_BTN")
            if point is not None:
                await self.tap_point(point)
            return
        await self.backend.shell("input keyevent 4")

    async def pointer(self, on: bool) -> None:
        if self.spec.platform == "android":
            from . import trade_android as android_trade

            await android_trade.pointer([self.backend], on)

    async def quit(self) -> None:
        if self.spec.platform == "ios":
            await self.backend.quit()


def ios_trade_runtime(spec: DeviceSpec, delay_modifier: float) -> Any:
    from . import trade_ios_android as cross

    op = spec.operation("trade")
    profile = load_appium_profile(spec)
    points = dict(op["coordinates"])
    return cross.RuntimeConfig(
        ios_coordinates=points,
        ios_device=profile["device"],
        appium_server_url=profile["server_url"],
        android_serial=None,
        delay_modifier=delay_modifier,
        steps=tuple(cross.DEFAULT_STEPS),
    )


async def connect_trade_controller(spec: DeviceSpec, delay_modifier: float) -> TradeController:
    from . import trade_android as android_trade
    from . import trade_ios_android as cross

    if spec.platform == "ios":
        runtime = ios_trade_runtime(spec, delay_modifier)
        backend = await asyncio.to_thread(cross.IOSController.connect, runtime)
        return TradeController(spec, backend, dict(runtime.ios_coordinates))
    backend = await cross.select_android_device(spec.identifier)
    points = dict(backend.config)
    for button in android_trade.BUTTONS:
        if button.name not in points and button.default_coords is not None:
            points[button.name] = list(button.default_coords)
    return TradeController(spec, backend, points)


def save_trade_diagnostics(
    controllers: Sequence[TradeController],
    images: Sequence[Any],
    step: str,
    metrics: dict[str, Any] | None = None,
    names: Sequence[str] | None = None,
) -> Path:
    """Save one screenshot per phone. ``names`` renames the files, for a
    caller saving several frames of the same phone."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = FLEET_DIAGNOSTICS_DIR / f"fleet-trade-{stamp}-{step}"
    directory.mkdir(parents=True, exist_ok=True)
    labels = list(names) if names else [controller.spec.name for controller in controllers]
    for label, image in zip(labels, images):
        image.save(directory / f"{label}.png")
    # The numbers belong next to the screenshots they were taken from rather
    # than in the error the operator reads.
    if metrics:
        (directory / "metrics.txt").write_text(
            "".join(f"{name}: {values}\n" for name, values in metrics.items())
        )
    return directory


# A run that stops mid-sequence leaves both phones wherever it died — a
# Pokémon picker, a lobby waiting on the other trainer, a post-trade card — and
# every one of those fails the friend-screen check that opens the next cycle.
# Refusing to start is no help when nothing else is going to put the phones
# right, so the guard walks them back itself. Two plain looks come first, since
# a screen halfway through its animation is not stuck; only phones that are not
# already on the friend screen are ever touched.
FRIEND_RECOVERY_ROUNDS = 4
FRIEND_RECOVERY_DELAY = 3
FRIEND_GUARD_LOOKS_BEFORE_RECOVERY = 2
# Long enough for one of the game's cross-fades to finish. A frame caught
# halfway through one is tinted from edge to edge, and the tint takes the white
# out of every measurement that names a screen: the SE's friend screen, caught
# coming back from a trade on 2026-09-13, measured white_panel, card_band and
# card_edges all at exactly 0.000 and so read as a lobby. Recovery then pressed
# X_BTN on a friend screen, which is the one press that starts a fresh trade
# nobody answers — so the tint did not just lose a round, it queued another
# expired trade behind the one already on screen.
STATE_CONFIRM_DELAY = 1
# Pokémon GO raises one "Trade expired." per lobby that timed out and shows
# them one at a time, each in front of a screen identical to the last. Six were
# counted on the SE on 2026-09-13, five of whose frames were byte-for-byte the
# same — so a phone still on a dialog after being pressed looks exactly like a
# phone whose button did nothing, and recovery's four rounds ran out halfway
# down the queue. Dismissing one is progress and buys back the round it spent,
# this many times.
#
# Set high because nothing cheaper can measure the depth. Fourteen rounds of the
# SE's notice were saved on 2026-09-13 and every frame below the status bar was
# pixel-identical — the clock in the corner was the only thing that moved, so
# there is no reading of the picture that tells the tenth notice from the first.
# Pressing OK on a notice that has already gone is harmless, which makes a
# budget that is too large cost only time, while one that is too small ends the
# run; the two are not worth trading off evenly.
DIALOG_DRAIN_ROUNDS = 40
# Consecutive failed cycles before the whole run gives up. A trade that dies is
# retried from the friend screen rather than taking the other 99 down with it.
TRADE_CYCLE_RETRIES = 3
# How many of the game's own notices one guard will press away. Each is a
# different notice — the same one twice means the press missed — and a phone
# that has met three of them in one step is not being kept from the trade by
# notices.
NOTICE_DISMISS_ROUNDS = 3
# How many times a phone left standing on the picker is asked to select again.
# state_matches("selection") reads search_pale, which is measured in the search
# bar band and says nothing about whether any Pokémon have rendered below it:
# an empty picker and a full one both score 0.90 on the SE. So FIRST_PKMN_BTN
# can go in while the list is still filling, hit nothing, and leave that phone
# on the picker while the other side walks on to its detail screen — which is
# what "expected a Pokémon's detail screen" has been reporting.
PICKER_RESELECT_ROUNDS = 3


# What each recognised screen is worth saying out loud, and what closes it.
GAME_PACKAGE = "com.nianticlabs.pokemongo"
TRADE_STATE_LABELS = {
    "map": "the map, outside the trade entirely",
    "off_game": "a screen outside Pokémon GO",
    "exit_prompt": "the game’s own “exit Pokémon GO?” prompt",
    "friend": "the friend screen",
    "selection": "the Pokémon picker",
    "next": "a Pokémon's detail screen",
    "lobby": "a trade lobby",
    "post_trade": "the post-trade card",
    "empty_lobby": "a trade it has not offered a Pokémon into",
    "friendship": "the friendship-bonus sheet",
    "dialog": "a dialog",
    "unknown": "a screen it does not recognise",
}
# What closes each screen on a phone that has no BACK key. X_BTN is the only
# way off these on iOS, and on the post-trade card it is the way off for both
# platforms — that card has a real X and no trade left to cancel.
TRADE_STATE_CLOSES_ON = {
    "selection": "X_BTN",
    "next": "X_BTN",
    "post_trade": "X_BTN",
    # The sheet's close disc is drawn at the same point the friend screen puts
    # its own: measured on the SE's capture at pixel 374,1236, which is X_BTN
    # [187, 618] exactly. No new coordinate to calibrate, and closing it lands
    # back on the friend screen it was opened from.
    "friendship": "X_BTN",
}
# A phone that comes back to a screen it has already been stepped out of is
# not being stepped anywhere: the android-two spent all four of its recovery rounds
# going picker -> detail screen -> picker, because X on its picker reopens the
# Pokémon it had already offered and the mapped X on the detail screen is the
# menu disc below it. Pressing the same button a third time is not going to
# help, so the second visit uses the platform's other way out — BACK on
# Android, which raises "Do you want to cancel the trade?" and lands on the
# friend screen once that is answered, and the corner door on iOS.
#
# Which button is right is a per-handset question and this does not have to
# guess: the android-three's picker really does close on X_BTN, walks picker -> lobby ->
# friend screen, and never sees the same screen twice, so it never reaches
# this at all.
TRADE_CANCEL_YES = "TRADE_CANCEL_YES_BTN"
# The door in the top-left corner of a trade screen. Only iOS needs it mapped;
# Android leaves the same screens with BACK.
TRADE_EXIT = "TRADE_EXIT_BTN"


def repeat_escape(controller: TradeController, state: str | None = None) -> str | None:
    """The other way out, for a screen whose mapped button did not work."""
    if controller.spec.platform == "android":
        return "BACK"
    if state == "selection":
        # The picker has no door: its top-left corner is the search row, and
        # the iphone-second spent its last two recovery rounds on 2026-09-13
        # pressing that empty space. The disc at the bottom really does close
        # this screen — what sent the phone back to it was the lobby below,
        # which is handled where that screen is named.
        return None
    return TRADE_EXIT if controller.point(TRADE_EXIT) is not None else None


async def step_towards_friend(
    controller: TradeController, image: Any, seen: set[str] | None = None
) -> tuple[str, str | None]:
    """Take one step back towards the friend screen.

    Returns the screen it found and what it pressed, or None for a screen it
    left alone. Which button was used is the thing worth reading afterwards:
    two rounds that both say "stepping back" but pressed different buttons
    look identical in a log and mean entirely different things.

    ``seen`` is the screens this phone has already been stepped out of during
    this recovery; a state in it means the button used last time did not work.

    Nothing is tapped on a screen this cannot name. The blanket BACK that used
    to stand here pressed on regardless, and on the android-three's picker that raises
    "Do you want to cancel the trade?" — which nothing then answered, so a
    phone that was merely stranded became stuck, and every retry burned itself
    against the same dialog.
    """
    from . import trade_ios_android as cross

    state, metrics = cross.describe_state(
        image, lambda name: controller.image_point(image, name)
    )
    if state == "friend":
        return state, None
    if state == "map":
        # Nothing is pressed here. BACK on the map is "Do you want to exit
        # Pokémon GO?", and the only way back to a friend's screen goes
        # through the trainer's profile, which this run has no coordinates
        # for. Say plainly where the phone is and let the run stop: it needs
        # putting back on the friend screen by hand.
        if await controller.game_in_front() is False:
            return "off_game", None
        return state, None
    if state == "dialog":
        if cross.is_exit_game_dialog(image):
            # The one dialog whose top pill must never be pressed: it says OK
            # and it closes Pokémon GO. CANCEL is the label under it, and if
            # that cannot be found the phone is better left standing here than
            # answered wrongly.
            pixel = cross.find_dialog_label_button(image)
            if pixel is None:
                return "exit_prompt", None
            await controller.tap_point(controller.point_from_image(image, pixel))
            return "exit_prompt", "CANCEL"
        # Read the button out of the picture rather than trusting one mapped
        # coordinate: a run meets several dialogs and their buttons sit at
        # different heights. TRADE_CANCEL_YES_BTN stays as the fallback for a
        # dialog whose pill cannot be found.
        pixel = cross.find_dialog_button(image)
        point = (
            controller.point_from_image(image, pixel)
            if pixel
            else controller.point(TRADE_CANCEL_YES)
        )
        if point is None:
            return state, None
        await controller.tap_point(point)
        return state, "the dialog's button"
    # Only for a screen this can name. On a screen it cannot, the other way
    # out is still a blind press, and a blind BACK is what turned a stranded
    # phone into a stuck one in the first place.
    if seen is not None and state in seen and state in cross.DESCRIBED_STATES:
        escape = repeat_escape(controller, state)
        if escape == "BACK":
            await controller.back()
            return state, "BACK"
        if escape is not None:
            await controller.tap_point(controller.point(escape))
            return state, escape
    if state == "lobby":
        # Two screens answer to this name, and only one of them listens to
        # BACK. Once this phone has confirmed, the pill under the action point
        # turns from a green CONFIRM into an amber CANCEL and the trade is
        # waiting on the other trainer: nothing else on that screen does
        # anything, and the razr sat through all four of its recovery rounds
        # pressing BACK at it on 2026-09-13 while the iPhone it was waiting for
        # stood on the picker. The pill itself is the way out, and it is
        # already under a mapped coordinate.
        if metrics["action_orange"] >= cross.ACTION_PILL_FRACTION:
            point = controller.point("CONFIRM_BTN")
            if point is not None:
                await controller.tap_point(point)
                return state, "CANCEL"
        # Before CONFIRM there is no amber pill, and on iOS the disc BACK maps
        # to opens the Pokémon picker rather than closing anything — picker and
        # lobby then hand the phone back and forth until the rounds run out.
        # The door in the corner is the one press that leaves.
        point = controller.point(TRADE_EXIT)
        if point is not None:
            await controller.tap_point(point)
            return state, TRADE_EXIT
        await controller.back()
        return state, "BACK"
    if state == "empty_lobby":
        # The disc in the bottom middle is not a close button here: it opens
        # the picker, and the picker's X comes straight back to this screen.
        # That is the flip a phone spent all four of its recovery rounds doing.
        # iOS leaves by the door in the top-left corner. Android has no door
        # drawn on this screen; BACK is what raises the cancel dialog, which
        # the dialog branch then answers.
        point = controller.point(TRADE_EXIT)
        if point is not None:
            await controller.tap_point(point)
            return state, TRADE_EXIT
        if controller.spec.platform == "android":
            await controller.back()
            return state, "BACK"
        return state, None
    button = TRADE_STATE_CLOSES_ON.get(state)
    if button is None or controller.point(button) is None:
        return state, None
    await controller.tap_step(button)
    return state, button


def describe_step(
    name: str, state: str, pressed: str | None, round_number: int | None = None
) -> str:
    """One line saying where a phone is and what is being done about it."""
    where = TRADE_STATE_LABELS.get(state, state)
    if pressed:
        action = f"on {where} — pressing {pressed} to step back to the friend screen"
    elif state == "dialog":
        action = f"on {where} with no button it can find — leaving it alone"
    else:
        action = f"on {where} — leaving it alone"
    counter = f" ({round_number}/{FRIEND_RECOVERY_ROUNDS})" if round_number else ""
    return f"{name} is {action}{counter}"


async def settled_screens(
    stranded: Sequence[tuple[TradeController, Any]],
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> list[tuple[TradeController, Any]]:
    """The phones among these whose screen has stopped moving, and its picture.

    A phone caught mid-cross-fade is dropped for this round rather than pressed
    on: the tint of a fade takes the white out of every measurement, and the
    screen it is named after is not the screen that will be there when the press
    lands. Waiting a beat and asking again is the whole of it.

    What gets compared is the *name*, not the pixels. A friend screen is never
    pixel-stable — the avatar breathes — but it is always called "friend", and
    a fade is called one thing on the way in and another on the way out.
    """
    from . import trade_ios_android as cross

    def name_of(controller: TradeController, image: Any) -> str:
        state, _ = cross.describe_state(
            image, lambda point: controller.image_point(image, point)
        )
        return state

    before = [(controller, image, name_of(controller, image)) for controller, image in stranded]
    await sleep(STATE_CONFIRM_DELAY)
    settled = []
    for controller, _, was in before:
        image = await controller.screenshot()
        if cross.state_matches(
            "friend", cross.screen_metrics(image, controller.image_point(image, "TRADE_BTN"))
        ):
            # It was on its way home while the first picture was taken. Pressing
            # anything now would be pressing on the friend screen.
            continue
        if name_of(controller, image) != was:
            print(f"    {controller.spec.name} is between screens — looking again")
            continue
        settled.append((controller, image))
    return settled


async def recover_to_friend(
    controllers: Sequence[TradeController], sleep: Callable[[float], Any] = asyncio.sleep
) -> bool:
    """Step every phone back to the friend screen. True when they all made it."""
    from . import trade_ios_android as cross

    history: dict[str, set[str]] = {}
    trail: list[tuple[TradeController, Any, str]] = []
    allowance = FRIEND_RECOVERY_ROUNDS
    drained = 0
    round_number = -1
    while True:
        round_number += 1
        images = await asyncio.gather(*(controller.screenshot() for controller in controllers))
        stranded = [
            (controller, image)
            for controller, image in zip(controllers, images)
            if not cross.state_matches(
                "friend",
                cross.screen_metrics(image, controller.image_point(image, "TRADE_BTN")),
            )
        ]
        if not stranded:
            return True
        if round_number == allowance:
            # Recovery that runs out of rounds says only "did not come home",
            # and the screens it was walking are gone by the time anyone looks.
            # A android-two alternating picker -> detail screen -> picker for four
            # rounds is either a step that undoes itself or a screen being read
            # mid-animation, and neither can be told apart without the frames.
            trail.extend(
                (controller, image, f"{controller.spec.name}-round{round_number}")
                for controller, image in stranded
            )
            directory = save_trade_diagnostics(
                [controller for controller, _, _ in trail],
                [image for _, image, _ in trail],
                "RECOVERY",
                names=[label for _, _, label in trail],
            )
            print(f"    Recovery gave up; every round's screens saved in {directory}")
            return False
        settled = await settled_screens(stranded, sleep)
        trail.extend(
            (controller, image, f"{controller.spec.name}-round{round_number}")
            for controller, image in settled
        )
        dismissed = False
        for controller, image in settled:
            seen = history.setdefault(controller.spec.name, set())
            state, pressed = await step_towards_friend(controller, image, seen)
            seen.add(state)
            print(f"    {describe_step(controller.spec.name, state, pressed, round_number + 1)}")
            dismissed = dismissed or (state == "dialog" and bool(pressed))
        # A dismissed notice is a screen gone, even though the one behind it is
        # identical. Recovery only has rounds to spend on presses that might not
        # be working, so this one is given back.
        if dismissed and drained < DIALOG_DRAIN_ROUNDS:
            drained += 1
            allowance += 1
        await sleep(FRIEND_RECOVERY_DELAY)


def describe_screens(controllers: Sequence[TradeController], images: Sequence[Any]) -> str:
    """Name the screen each phone is on, for the message an operator reads.

    The raw metrics this replaced said nothing to anyone reading a run; they
    are written to metrics.txt beside the screenshots instead.
    """
    from . import trade_ios_android as cross

    named = []
    for controller, image in zip(controllers, images):
        state, _ = cross.describe_state(image, lambda name: controller.image_point(image, name))
        named.append(f"{controller.spec.name} is on {TRADE_STATE_LABELS.get(state, state)}")
    return ", ".join(named)


async def guard_trade_state(
    controllers: Sequence[TradeController], expected: str, step: str, sleep: Callable[[float], Any] = asyncio.sleep
) -> None:
    from . import trade_ios_android as cross

    last_images: Sequence[Any] = []
    last_metrics: dict[str, Any] = {}
    history: dict[str, set[str]] = {}
    recoveries = 0
    reselections = 0
    notices = 0
    # Rounds spent walking a phone home do not count against the guard's looks.
    # Picker -> lobby -> cancel dialog -> friend screen takes three of them,
    # which was every look the guard had, so the phone arrived home on the same
    # attempt that raised.
    allowance = cross.STATE_GUARD_ATTEMPTS
    attempt = 0
    while attempt < allowance:
        attempt += 1
        images = await asyncio.gather(*(controller.screenshot() for controller in controllers))
        last_images = images
        matches = []
        last_metrics = {}
        action_name = cross.STATE_ACTION_COORDINATE.get(expected)
        for controller, image in zip(controllers, images):
            action_point = controller.image_point(image, action_name) if action_name else None
            metrics = cross.screen_metrics(image, action_point)
            last_metrics[controller.spec.name] = metrics
            matches.append(cross.state_matches(expected, metrics))
        if all(matches):
            return

        # A notice the game raised by itself — "New Mega Level available!" on
        # the post-trade card — is not the trade going wrong. The screen the
        # step wants is underneath it, so it is pressed away and the guard
        # looks again, instead of the cycle stopping and recovery walking a
        # phone home from a card it never had to leave.
        raised = []
        for controller, image, matched in zip(controllers, images, matches):
            if matched:
                continue
            notice = cross.incidental_notice(image)
            if notice:
                raised.append((controller, image, notice))
        if raised and notices < NOTICE_DISMISS_ROUNDS:
            notices += 1
            allowance = min(allowance + 1, cross.STATE_GUARD_ATTEMPTS + NOTICE_DISMISS_ROUNDS)
            for controller, image, notice in raised:
                pixel = cross.find_dialog_button(image)
                if pixel is None:
                    # Nothing mapped is pressed here instead: the coordinates
                    # this run holds are trade buttons, and the trade this
                    # notice covers is finished.
                    print(
                        f"  {controller.spec.name} is showing “{notice}” and its button "
                        f"cannot be found — leaving it alone"
                    )
                    continue
                await controller.tap_point(controller.point_from_image(image, pixel))
                print(
                    f"  {controller.spec.name} is showing “{notice}” — pressing it away "
                    f"and looking again ({notices}/{NOTICE_DISMISS_ROUNDS})"
                )
            await sleep(cross.STATE_GUARD_DELAY)
            continue

        # Whatever a previous run left on screen, step it back rather than
        # standing here refusing to open the cycle.
        if (
            expected == "friend"
            and attempt > FRIEND_GUARD_LOOKS_BEFORE_RECOVERY
            and recoveries < FRIEND_RECOVERY_ROUNDS
        ):
            recoveries += 1
            allowance = min(allowance + 1, cross.STATE_GUARD_ATTEMPTS + FRIEND_RECOVERY_ROUNDS)
            for controller, image, matched in zip(controllers, images, matches):
                if matched:
                    continue
                seen = history.setdefault(controller.spec.name, set())
                state, pressed = await step_towards_friend(controller, image, seen)
                seen.add(state)
                print(f"  {describe_step(controller.spec.name, state, pressed)}")
            await sleep(FRIEND_RECOVERY_DELAY)
            continue

        # A phone still on the picker when the detail screen is due never had a
        # Pokémon selected — the tap went in before the list had filled, or was
        # dropped. Selecting one spends nothing and a detail screen opened twice
        # is harmless, so ask that phone alone to select again instead of taking
        # the cycle down. Phones that did advance are not touched, which is what
        # keeps the two sides in step.
        if step == "NEXT_BTN":
            stalled = [
                controller
                for controller, image, matched in zip(controllers, images, matches)
                if not matched
                and controller.point("FIRST_PKMN_BTN") is not None
                and cross.state_matches("selection", cross.screen_metrics(image, None))
            ]
            if stalled and reselections < PICKER_RESELECT_ROUNDS:
                reselections += 1
                allowance = min(
                    allowance + 1, cross.STATE_GUARD_ATTEMPTS + PICKER_RESELECT_ROUNDS
                )
                for controller in stalled:
                    print(
                        f"  {controller.spec.name} is still on the Pokémon picker — "
                        f"selecting again ({reselections}/{PICKER_RESELECT_ROUNDS})"
                    )
                    await controller.tap_step("FIRST_PKMN_BTN")
                await sleep(cross.STATE_GUARD_DELAY)
                continue
            if stalled:
                # A picker that has been asked this many times and has not
                # opened anything is not slow, it is refusing: the Pokémon it
                # is listing are ones this trade may not take. A search that
                # still shows them — a spent daily special trade leaves rare
                # species sitting there, tappable and inert — is the usual
                # reason, and nothing another cycle does will change it.
                names = ", ".join(controller.spec.name for controller in stalled)
                raise TradeStopped(
                    f"The Pokémon picker on {names} would not open anything: "
                    f"FIRST_PKMN_BTN went in {reselections + 1} times and the picker "
                    f"stayed put. What it is listing cannot be traded, so the search "
                    f"or the box has to change before this pair can trade again. "
                    f"Stopping rather than walking both phones into another trade."
                )

        # "Trade expired." — the game has cancelled the trade itself, so no
        # step is ever going to pass and the guard would spend its whole
        # allowance in front of a dead screen before saying anything useful
        # about it. Pressing the notice away costs nothing, since the trade it
        # refers to is already gone, and it leaves the phone somewhere
        # recover_to_friend can work from — which is what lets the cycle be
        # started again instead of taking the run down. Only this notice is
        # answered here, by its words: the dialogs that ask a question wear the
        # same white card and the same green pill, and pressing one of those
        # would put a trade through.
        expired = [
            (controller, image)
            for controller, image, matched in zip(controllers, images, matches)
            if not matched and cross.is_trade_expired_dialog(image)
        ]
        if expired:
            for controller, image in expired:
                pixel = cross.find_dialog_button(image)
                point = (
                    controller.point_from_image(image, pixel)
                    if pixel
                    else controller.point(TRADE_CANCEL_YES)
                )
                if point is not None:
                    await controller.tap_point(point)
            names = ", ".join(controller.spec.name for controller, _ in expired)
            raise FleetError(
                f"The trade expired before {step}; Pokémon GO cancelled it while "
                f"{names} waited. Dismissed the notice; the cycle starts again "
                f"from the friend screen."
            )

        # "Daily trading limit reached." — the day is over, and no retry,
        # recovery or search change reaches past that. The notice is pressed
        # away so the phones are not left standing on a modal, and the run
        # stops rather than walking the pair into another cycle.
        capped = [
            (controller, image)
            for controller, image, matched in zip(controllers, images, matches)
            if not matched and cross.is_trade_limit_dialog(image)
        ]
        if capped:
            for controller, image in capped:
                pixel = cross.find_dialog_button(image)
                if pixel is not None:
                    await controller.tap_point(controller.point_from_image(image, pixel))
            names = ", ".join(controller.spec.name for controller, _ in capped)
            raise TradeStopped(
                f"Pokémon GO's daily trading limit is reached: {names} said so "
                f"before {step}. Dismissed the notice. This pair can trade again "
                f"tomorrow."
            )

        # Safe recovery for the known split state: one device is already waiting in
        # the lobby while an iPhone shows the trading-unavailable dialog.
        if step == "FIRST_PKMN_BTN":
            waiting = [cross.is_android_waiting_lobby(image) for image in images]
            unavailable = [
                controller.spec.platform == "ios" and cross.is_ios_trading_unavailable_dialog(image)
                for controller, image in zip(controllers, images)
            ]
            if any(waiting) and any(unavailable):
                for controller, blocked in zip(controllers, unavailable):
                    if blocked:
                        point = controller.point("IOS_TRADING_UNAVAILABLE_OK_BTN") or [187, 372]
                        await controller.tap_point(point)
                await sleep(min(5 * (2 ** (attempt - 1)), 30))
                continue
            if any(waiting):
                recovered = False
                for controller, image, is_waiting in zip(controllers, images, waiting):
                    if is_waiting:
                        continue
                    point = controller.image_point(image, "TRADE_BTN")
                    if point and cross.state_matches("friend", cross.screen_metrics(image, point)):
                        await controller.tap_step("TRADE_BTN")
                        recovered = True
                if recovered:
                    await sleep(min(5 * (2 ** (attempt - 1)), 30))
                    continue
        if attempt < allowance:
            print(f"  Screen guard waiting before {step} ({attempt}/{allowance})")
            await sleep(cross.STATE_GUARD_DELAY)
    directory = save_trade_diagnostics(controllers, last_images, step, last_metrics)
    raise FleetError(
        f"Screen mismatch before {step}; expected {TRADE_STATE_LABELS.get(expected, expected)}. "
        f"{describe_screens(controllers, last_images)}. "
        f"Stopped without tapping; diagnostics saved in {directory}."
    )


async def execute_trade_sequence(
    controllers: Sequence[TradeController], delay_modifier: float, dry_run: bool = False,
    sleep: Callable[[float], Any] = asyncio.sleep, start_step: str | None = None,
) -> None:
    from . import trade_ios_android as cross

    steps = list(cross.DEFAULT_STEPS)
    if start_step:
        step_names = [s.name for s in steps]
        if start_step in step_names:
            idx = step_names.index(start_step)
            steps = steps[idx:]
        else:
            raise FleetError(f"Unknown trade start step: {start_step!r}")

    for step in steps:
        enabled = [controller for controller in controllers if controller.point(step.name) is not None]
        if not enabled:
            if step.ios_optional:
                continue
            raise FleetError(f"No selected device has {step.name}")
        delay = max(step.delay_after + (delay_modifier if step.use_delay_modifier else 0), 0)
        names = " + ".join(controller.spec.name for controller in enabled)
        print(f"  {step.name}: {names}; wait {delay:g}s")
        if not dry_run:
            expected = cross.STEP_EXPECTED_STATE.get(step.name)
            if expected:
                # The step a run is told to start from is guarded like any
                # other. Being told the phones are on the picker is not the
                # same as their being there, and the tap that goes in blind
                # lands on whatever is: state_matches("selection") cannot tell
                # a filled picker from an empty one, but it knows a picker from
                # a friend screen, which is the mistake worth catching. A phone
                # that is not where it was said to be fails this cycle, comes
                # home, and starts the next one from TRADE_BTN.
                await guard_trade_state(controllers, expected, step.name, sleep)
            await asyncio.gather(*(controller.tap_step(step.name) for controller in enabled))
        await sleep(delay)
        if not dry_run and step.name == "CONFIRM_BTN":
            await asyncio.gather(*(controller.cleanup() for controller in controllers))


async def run_trade(specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    from . import trade_ios_android as cross

    pids = cross.active_gift_runner_pids()
    if pids:
        raise FleetError(f"iOS gift runner active (PID {', '.join(map(str, pids))})")
    profiles = [load_appium_profile(spec) for spec in specs if spec.platform == "ios"]
    for profile in profiles:
        cross.check_appium_status(profile["server_url"])
    controllers: list[TradeController] = []
    try:
        controllers = list(
            await asyncio.gather(*(connect_trade_controller(spec, args.delay_modifier) for spec in specs))
        )
        await asyncio.gather(*(controller.pointer(True) for controller in controllers))

        start_step = getattr(args, "start_step", None)
        if getattr(args, "selection", False) or getattr(args, "from_selection", False) or getattr(args, "skip_trade_btn", False):
            start_step = "FIRST_PKMN_BTN"

        number = 0
        failures = 0
        while number < args.count:
            number += 1
            print(f"Starting trade {number}/{args.count}")
            cycle_start = start_step if number == 1 and not failures else None
            try:
                await execute_trade_sequence(
                    controllers, args.delay_modifier, args.dry_run, start_step=cycle_start
                )
            except TradeStopped:
                # The run is over, but two phones left standing inside a trade
                # are what the game cancels and then reports as "Trade
                # expired." on whichever screen the next run opens on. They
                # come home first, and the stop is still not retried.
                await recover_to_friend(controllers)
                raise
            except FleetError as error:
                failures += 1
                if failures > TRADE_CYCLE_RETRIES:
                    raise
                print(f"Trade {number} stopped: {error}")
                print(
                    f"  Walking both phones back to the friend screen and retrying "
                    f"({failures}/{TRADE_CYCLE_RETRIES})"
                )
                if not await recover_to_friend(controllers):
                    raise
                # The trade did not go through, so this number is still owed.
                number -= 1
                continue
            failures = 0
    finally:
        if controllers:
            await asyncio.gather(*(controller.pointer(False) for controller in controllers), return_exceptions=True)
            await asyncio.gather(*(controller.quit() for controller in controllers), return_exceptions=True)


def patch_metrics(image: Any, point: tuple[int, int], radius: int = 18) -> dict[str, Any]:
    left = max(0, point[0] - radius)
    top = max(0, point[1] - radius)
    right = min(image.width, point[0] + radius + 1)
    bottom = min(image.height, point[1] + radius + 1)
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    pixels = list(crop.getdata())
    if not pixels:
        return {"saturation": 0.0, "brightness": 0.0, "mean_rgb": [0.0, 0.0, 0.0]}
    saturation = sum(max(pixel) - min(pixel) for pixel in pixels) / len(pixels)
    brightness = sum(sum(pixel) / 3 for pixel in pixels) / len(pixels)
    mean_rgb = [round(sum(pixel[channel] for pixel in pixels) / len(pixels), 2) for channel in range(3)]
    return {
        "saturation": round(saturation, 2),
        "brightness": round(brightness, 2),
        "mean_rgb": mean_rgb,
    }


class BattleController(TradeController):
    def __init__(self, spec: DeviceSpec, backend: Any, coordinates: dict[str, list[int] | None], guards: dict[str, Any]):
        super().__init__(spec, backend, coordinates)
        self.guards = guards

    async def guarded_tap(self, step: str) -> None:
        image = await self.screenshot()
        point = self.image_point(image, step)
        if point is None:
            raise FleetError(f"{self.spec.name} has no {step}")
        metrics = patch_metrics(image, point)
        guard = self.guards.get(step)
        validate_battle_guard(self.spec.name, step, guard)
        color_distance = max(
            abs(metrics["mean_rgb"][index] - float(guard["reference_rgb"][index]))
            for index in range(3)
        )
        if not (
            metrics["saturation"] >= float(guard["min_saturation"])
            and float(guard["min_brightness"]) <= metrics["brightness"] <= float(guard.get("max_brightness", 255))
            and color_distance <= float(guard["max_color_distance"])
        ):
            directory = FLEET_DIAGNOSTICS_DIR / f"battle-{time.strftime('%Y%m%d-%H%M%S')}-{step}"
            directory.mkdir(parents=True, exist_ok=True)
            image.save(directory / f"{self.spec.name}.png")
            raise FleetError(
                f"{self.spec.name} visual guard rejected {step}: {metrics}; stopped without tapping. "
                f"Screenshot saved in {directory}."
            )
        await self.tap_step(step)


async def connect_battle_controller(spec: DeviceSpec) -> BattleController:
    from . import trade_android as android_trade
    from . import trade_ios_android as cross

    op = spec.operation("battle")
    guards = dict(op.get("guards", {}))
    if spec.platform == "ios":
        profile = load_appium_profile(spec)
        points = dict(op["coordinates"])
        runtime = cross.RuntimeConfig(
            ios_coordinates=points,
            ios_device=profile["device"],
            appium_server_url=profile["server_url"],
            android_serial=None,
            delay_modifier=0,
            steps=tuple(cross.DEFAULT_STEPS),
        )
        backend = await asyncio.to_thread(cross.IOSController.connect, runtime)
        return BattleController(spec, backend, points, guards)
    backend = await cross.select_android_device(spec.identifier)
    points = {key: backend.config.get(key) for key in BATTLE_STEPS if backend.config.get(key) is not None}
    missing = BATTLE_REQUIRED - points.keys()
    if missing:
        raise FleetError(f"{spec.name} Android config missing battle keys: {', '.join(sorted(missing))}")
    return BattleController(spec, backend, points, guards)


async def run_battle(specs: Sequence[DeviceSpec], args: argparse.Namespace) -> None:
    from . import battle_android as battle
    from . import trade_ios_android as cross

    for spec in specs:
        if spec.platform == "ios":
            cross.check_appium_status(load_appium_profile(spec)["server_url"])
    controllers: list[BattleController] = []
    try:
        controllers = list(await asyncio.gather(*(connect_battle_controller(spec) for spec in specs)))
        surrenderers = [controller for controller in controllers if BATTLE_SURRENDER <= controller.coordinates.keys()]
        if not surrenderers:
            raise FleetError("At least one device must have RUN_BTN and SURRENDER_BTN")
        for number in range(1, args.count + 1):
            start = "BATTLE_BTN" if args.first and number == 1 else "REMATCH_BTN"
            print(f"Starting battle {number}/{args.count}: {start}")
            if not args.dry_run:
                await asyncio.gather(*(controller.guarded_tap(start) for controller in controllers))
            await asyncio.sleep(max(battle.PARTY_SCREEN_DELAY + args.delay_modifier, 0))
            if not args.dry_run:
                await asyncio.gather(*(controller.guarded_tap("USE_PARTY_BTN") for controller in controllers))
            await asyncio.sleep(max(battle.BATTLE_LOAD_DELAY + args.delay_modifier, 0))
            for controller in surrenderers:
                if not args.dry_run:
                    await controller.guarded_tap("RUN_BTN")
                await asyncio.sleep(battle.RUN_MENU_DELAY)
                if not args.dry_run:
                    await controller.guarded_tap("SURRENDER_BTN")
            await asyncio.sleep(max(battle.SURRENDER_DELAY + args.delay_modifier, 0))
    finally:
        if controllers:
            await asyncio.gather(*(controller.quit() for controller in controllers), return_exceptions=True)


async def inspect_battle(specs: Sequence[DeviceSpec], step: str, output: Path) -> None:
    controllers: list[BattleController] = []
    try:
        controllers = list(await asyncio.gather(*(connect_battle_controller(spec) for spec in specs)))
        output.mkdir(parents=True, exist_ok=True)
        for controller in controllers:
            image = await controller.screenshot()
            point = controller.image_point(image, step)
            if point is None:
                print(f"{controller.spec.name}: no {step} coordinate")
                continue
            metrics = patch_metrics(image, point)
            path = output / f"{controller.spec.name}-{step}.png"
            image.save(path)
            print(f"{controller.spec.name} {step}: {metrics}; saved {path}")
            suggestion = {
                "min_saturation": max(0, round(metrics["saturation"] * 0.60, 1)),
                "min_brightness": max(0, round(metrics["brightness"] - 30, 1)),
                "max_brightness": min(255, round(metrics["brightness"] + 30, 1)),
                "reference_rgb": [round(value, 1) for value in metrics["mean_rgb"]],
                "max_color_distance": 35,
            }
            print(yaml.safe_dump({step: suggestion}, sort_keys=False).rstrip())
    finally:
        if controllers:
            await asyncio.gather(*(controller.quit() for controller in controllers), return_exceptions=True)


def print_plan(operation: str, specs: Sequence[DeviceSpec], detail: str) -> None:
    print(f"{operation} plan (offline; no device connections or taps)")
    for spec in specs:
        print(f"  {spec.name}: {spec.platform} {spec.identifier}")
    print(f"  {detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=config_paths.default_config("pokemon-fleet.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="read-only fleet and connection status")
    status.add_argument("--json", action="store_true")
    status.add_argument("--adb", default=default_adb_binary())

    owners = sub.add_parser("owners", help="which computer each configured phone is plugged into")
    owners.add_argument("--json", action="store_true")

    gifts = sub.add_parser("gifts", help="open/send gifts on any selected mixture")
    gifts.add_argument("--devices", nargs="+", default=["all"])
    gift_mode = gifts.add_mutually_exclusive_group(required=True)
    gift_mode.add_argument("--count", type=int)
    gift_mode.add_argument("--all", action="store_true")
    gifts.add_argument(
        "--android-count",
        type=int,
        help="legacy Android-only safety cap for --all; defaults to --max-cycles",
    )
    gifts.add_argument(
        "--max-cycles", type=int, default=100, help="per-phone safety cap for --all"
    )
    gifts.add_argument("--no-guard", action="store_true")
    gifts.add_argument(
        "--gifts-only",
        action="store_true",
        help="send gifts from every phone, even one parked on a gym's feeding screen",
    )
    gifts.add_argument("--spend", type=int, help="berry budget for a phone on a gym feeding screen")
    gifts.add_argument(
        "--spend-device",
        action="append",
        default=[],
        metavar="DEVICE=N",
        help="override berry budget for one fleet device",
    )
    gifts.add_argument("--plan", action="store_true")
    gifts.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)

    berries = sub.add_parser("berries", help="feed gym defenders on any selected mixture")
    berries.add_argument("--devices", nargs="+", default=["all"])
    berries.add_argument("--spend", type=int, help="berry budget per selected phone")
    berries.add_argument(
        "--spend-device",
        action="append",
        default=[],
        metavar="DEVICE=N",
        help="override berry budget for one fleet device",
    )
    berries.add_argument("--check", action="store_true", help="read-only detector check on every phone")
    berries.add_argument("--plan", action="store_true")
    berries.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)

    gbl = sub.add_parser("gbl", help="play Go Battle League sets on any selected mixture")
    gbl.add_argument("--devices", nargs="+", default=["all"])
    gbl.add_argument("--count", type=int, default=5, help="battles per phone (a set is 5)")
    gbl.add_argument("--check", action="store_true", help="read-only detector check on every phone")
    gbl.add_argument("--plan", action="store_true")
    gbl.add_argument("--trace", type=Path, help="write every decided-on frame under this directory")
    gbl.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)

    delete = sub.add_parser("delete", help="irreversibly transfer visible filtered matches")
    delete.add_argument("--devices", nargs="+", required=True)
    delete_mode = delete.add_mutually_exclusive_group(required=True)
    delete_mode.add_argument("--count", type=int)
    delete_mode.add_argument("--all", action="store_true")
    delete.add_argument("--max-transfers", type=int, default=500)
    delete.add_argument("--confirm-delete", action="store_true")
    delete.add_argument("--plan", action="store_true")
    delete.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)

    trade = sub.add_parser("trade", help="trade between exactly two registered devices")
    trade.add_argument("--pair", nargs=2, required=False, default=None, metavar=("DEVICE_A", "DEVICE_B"))
    trade.add_argument("--devices", nargs="+", default=None, help=argparse.SUPPRESS)
    trade.add_argument("--count", type=int, default=1)
    trade.add_argument("--delay-modifier", type=float, default=0)
    trade.add_argument("--selection", "--from-selection", "--skip-trade-btn", action="store_true", help="start first trade from Pokémon selection screen")
    trade.add_argument("--start-step", choices=("TRADE_BTN", "FIRST_PKMN_BTN", "NEXT_BTN", "MAX_LEVEL_RESET_BTN", "CONFIRM_BTN", "X_BTN"), default=None, help="step to start the first trade cycle from")
    trade.add_argument("--dry-run", action="store_true", help="connect and print sequence without taps")
    trade.add_argument("--plan", action="store_true", help="validate/print without connecting")
    trade.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)

    battle_parser = sub.add_parser("battle", help="friendly battle between exactly two devices")
    battle_parser.add_argument("--pair", nargs=2, required=False, default=None, metavar=("DEVICE_A", "DEVICE_B"))
    battle_parser.add_argument("--devices", nargs="+", default=None, help=argparse.SUPPRESS)
    battle_parser.add_argument("--count", type=int, default=1)
    battle_parser.add_argument("--first", action="store_true", help="first cycle starts from friend Battle button")
    battle_parser.add_argument("--delay-modifier", type=float, default=0)
    battle_parser.add_argument("--dry-run", action="store_true", help="connect without tapping")
    battle_parser.add_argument("--plan", action="store_true")
    battle_parser.add_argument("--allow-empty", action="store_true", help=argparse.SUPPRESS)

    inspect = sub.add_parser("battle-check", help="read-only screenshot/guard metrics for one battle step")
    inspect.add_argument("--pair", nargs=2, required=True, metavar=("DEVICE_A", "DEVICE_B"))
    inspect.add_argument("--step", choices=sorted(BATTLE_STEPS), required=True)
    inspect.add_argument("--output", type=Path, default=BATTLE_CALIBRATION_DIR)
    return parser


def positive(value: Any, label: str) -> None:
    if not isinstance(value, int) or value < 1:
        raise FleetError(f"{label} must be a positive integer")


def parse_spend_overrides(values: Sequence[str], selected: Sequence[DeviceSpec]) -> dict[str, int]:
    selected_names = {spec.name for spec in selected}
    result: dict[str, int] = {}
    for value in values:
        name, separator, raw_count = value.partition("=")
        if not separator or name not in selected_names:
            raise FleetError(f"--spend-device must be SELECTED_DEVICE=N, got {value!r}")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise FleetError(f"Invalid berry budget: {value!r}") from exc
        positive(count, f"berry budget for {name}")
        result[name] = count
    return result


def main() -> int:
    args = build_parser().parse_args()
    fleet = load_fleet(args.config)
    if args.command == "status":
        rows, unregistered = collect_status(fleet, args.adb)
        print_status(rows, unregistered, args.json)
        return 0

    if args.command == "owners":
        from . import device_owners  # deferred: it reads this module's probes

        rows, snapshots = device_owners.survey(fleet)
        device_owners.print_survey(rows, snapshots, device_owners.unregistered_serials(fleet, snapshots), args.json)
        return 0

    operation = "battle" if args.command == "battle-check" else args.command
    names = (
        args.pair
        if hasattr(args, "pair") and args.pair
        else (getattr(args, "devices", None) or ["all"])
    )
    allow_unready = bool(getattr(args, "plan", False)) or args.command == "battle-check"
    specs = select_devices(
        fleet,
        names,
        operation,
        allow_unready=allow_unready,
        allow_empty=bool(getattr(args, "allow_empty", False)),
    )
    if operation in {"trade", "battle"} and len(specs) != 2:
        names_str = ", ".join(spec.name for spec in specs)
        raise FleetError(
            f"{operation} requires exactly two devices (found {len(specs)}: {names_str or 'none'}). "
            f"Specify --pair DEVICE_A DEVICE_B if more than two devices are connected."
        )

    if args.command == "gifts":
        if args.all:
            positive(args.max_cycles, "--max-cycles")
            if args.android_count is not None:
                positive(args.android_count, "--android-count")
            android_cap = args.android_count or args.max_cycles
            detail = (
                "every phone until no outgoing gifts remain "
                f"(iOS cap {args.max_cycles}; Android cap {android_cap})"
            )
        else:
            positive(args.count, "--count")
            detail = f"{args.count} cycles per device"
        if args.spend is not None:
            positive(args.spend, "--spend")
        args.spend_overrides = parse_spend_overrides(args.spend_device, specs)
        if not args.gifts_only:
            detail += "; a phone on a gym feeding screen feeds berries instead"
        if args.plan:
            print_plan("gifts", specs, detail)
            return 0
        with acquire_device_locks(specs):
            asyncio.run(run_gifts(specs, args))
        return 0

    if args.command == "berries":
        if args.spend is not None:
            positive(args.spend, "--spend")
        args.spend_overrides = parse_spend_overrides(args.spend_device, specs)
        budgets = ", ".join(f"{name}={count}" for name, count in args.spend_overrides.items())
        detail = (
            "read-only detector check"
            if args.check
            else f"feed gyms; default spend cap={args.spend}; overrides={budgets or 'none'}"
        )
        if args.plan:
            print_plan("berries", specs, detail)
            return 0
        with acquire_device_locks(specs):
            asyncio.run(run_berries(specs, args))
        return 0

    if args.command == "gbl":
        positive(args.count, "--count")
        detail = (
            "read-only detector check"
            if args.check
            else f"{args.count} battle(s) per phone; start each on GO BATTLE LEAGUE"
        )
        if args.plan:
            print_plan("gbl", specs, detail)
            return 0
        with acquire_device_locks(specs):
            asyncio.run(run_gbl(specs, args))
        return 0

    if args.command == "delete":
        positive(args.max_transfers if args.all else args.count, "delete limit")
        detail = f"{'up to ' + str(args.max_transfers) if args.all else args.count} filtered transfers"
        if args.plan:
            print_plan("delete", specs, str(detail))
            if not args.confirm_delete:
                print("  live run will require --confirm-delete")
            return 0
        require_delete_authorization(args.confirm_delete)
        with acquire_device_locks(specs):
            asyncio.run(run_deletes(specs, args))
        return 0

    if args.command == "trade":
        positive(args.count, "--count")
        if args.plan:
            print_plan("trade", specs, f"{args.count} trade(s), delay modifier {args.delay_modifier:g}")
            return 0
        with acquire_device_locks(specs):
            asyncio.run(run_trade(specs, args))
        return 0

    if args.command == "battle":
        positive(args.count, "--count")
        if args.plan:
            print_plan("battle", specs, f"{args.count} battle(s); first={args.first}")
            return 0
        with acquire_device_locks(specs):
            asyncio.run(run_battle(specs, args))
        return 0

    if args.command == "battle-check":
        with acquire_device_locks(specs):
            asyncio.run(inspect_battle(specs, args.step, args.output))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    except (FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
