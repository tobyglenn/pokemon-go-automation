#!/usr/bin/env python3
# Internal iOS lifecycle helper.
"""Targeted WebDriverAgent shutdown helpers for real iOS devices."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TypeVar

from . import config_paths

T = TypeVar("T")


WDA_PROJECT_MARKER = "WebDriverAgent.xcodeproj"

# A WebDriverAgent that has lost its XCTest link still looks healthy from the
# outside: /status returns 200, says "WebDriverAgent is ready to accept
# commands", and reports ready: true. Reattach to one and the first tap fails
# with "Not authorized for performing UI testing actions", raised as a
# StaleElementReferenceException against the Pokemon GO application element.
# It is a dead session wearing a live one's clothes.
#
# testmanagerdVersion == 65535 (0xFFFF, the "no answer" sentinel) used to be
# read as the tell. It is not one. WDA 16.1.6 on iOS 26.6 reports 65535 while
# perfectly attached -- the SE was mid-run, taps returning 200, saying 65535 --
# so that test called every live WDA detached and killed it, and the reuse path
# below could never be reached on that phone.
#
# What actually distinguishes them is the host: the XCTest link is owned by the
# xcodebuild process that started the runner, so it dies exactly when that
# process does. Ask the host, not the phone.

WDA_DEFAULT_PORT = 8100


def find_host_wda_pids(processes: str, udid: str) -> list[int]:
    destination = f"id={udid}"
    found = []
    for line in processes.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        command = fields[1]
        if (
            "xcodebuild" in command
            and WDA_PROJECT_MARKER in command
            and destination in command
        ):
            found.append(int(fields[0]))
    return found


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.1)
    return not process_alive(pid)


def stop_host_process(pid: int) -> None:
    for stop_signal, timeout in (
        (signal.SIGINT, 3.0),
        (signal.SIGTERM, 2.0),
        (signal.SIGKILL, 1.0),
    ):
        try:
            os.kill(pid, stop_signal)
        except ProcessLookupError:
            return
        if wait_dead(pid, timeout):
            return


def run_output(arguments: list[str], timeout: float = 15) -> str:
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return result.stdout + result.stderr


def find_device_wda_pids(processes: str) -> list[int]:
    found = []
    for line in processes.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        command = fields[1]
        if "WebDriverAgentRunner-Runner" in command:
            found.append(int(fields[0]))
    return found


def stop_device_wda_processes(udid: str, timeout: float = 15.0) -> list[int]:
    """Terminate any orphaned WebDriverAgentRunner processes on the physical device via devicectl."""
    try:
        output = run_output(
            ["xcrun", "devicectl", "device", "info", "processes", "--device", udid],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    pids = find_device_wda_pids(output)
    for pid in pids:
        try:
            run_output(
                [
                    "xcrun",
                    "devicectl",
                    "device",
                    "process",
                    "terminate",
                    "--device",
                    udid,
                    "--pid",
                    str(pid),
                    "--kill",
                ],
                timeout=10.0,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    return pids


def stop_wda_runner(udid: str) -> bool:
    """Stop WDA for one UDID and leave the automated application running."""
    stopped = False
    try:
        host_processes = run_output(["/bin/ps", "ax", "-o", "pid=,command="])
        for pid in find_host_wda_pids(host_processes, udid):
            stop_host_process(pid)
            stopped = True

        device_pids = stop_device_wda_processes(udid)
        if device_pids:
            stopped = True

    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Warning: could not stop iPhone WebDriverAgent: {exc}", flush=True)
        return False

    if stopped:
        print("iPhone WebDriverAgent stopped", flush=True)
    return stopped


def stale_wda_error(exc: Exception | str) -> bool:
    message = str(exc).lower()
    return (
        ("not authorized" in message and "ui testing actions" in message)
        or "stale element reference" in message
        or (
            "previously found element" in message
            and (
                "not in current view" in message
                # WDA's newer wording, met on the SE 23 Nov 2026 at session start.
                or "not present in the current view" in message
            )
        )
    )


@contextmanager
def device_lock(
    udid: str, lock_dir: Path, error: type[Exception], message: str
) -> Iterator[None]:
    """Hold this iPhone's fleet lock file for the block, or raise `error(message)`.

    The same file `pokemon_fleet.acquire_device_locks` takes, so a worker run
    by hand and a fleet command never drive one phone at once.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"{udid}.lock").open("a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise error(message) from exc
        yield
    finally:
        handle.close()


def xcuitest_capabilities(
    device: Mapping[str, Any],
    *,
    use_new_wda: bool = False,
    show_xcode_log: bool | None = None,
) -> dict[str, Any]:
    """The Appium XCUITest capabilities for one profiled iPhone.

    One builder for the workers that attach the same way, so a capability
    added for one (say derivedDataPath, which keeps WDA's signed build) is not
    missing from another.
    """
    capabilities: dict[str, Any] = {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": device["udid"],
        "appium:deviceName": device.get("name", "iPhone"),
        "appium:bundleId": device.get("bundle_id", "com.nianticlabs.pokemongo"),
        "appium:noReset": True,
        "appium:shouldTerminateApp": False,
        "appium:xcodeOrgId": device["team_id"],
        "appium:xcodeSigningId": device.get("xcode_signing_id", "Apple Development"),
        "appium:updatedWDABundleId": device["wda_bundle_id"],
        "appium:allowProvisioningDeviceRegistration": True,
        "appium:useNewWDA": use_new_wda,
        "appium:waitForIdleTimeout": 0,
        "appium:waitForQuiescence": False,
        "appium:animationCoolOffTimeout": 0,
    }
    if show_xcode_log is not None:
        capabilities["appium:showXcodeLog"] = show_xcode_log
    capabilities["appium:newCommandTimeout"] = 3600
    if isinstance(device.get("platform_version"), str):
        capabilities["platformVersion"] = device["platform_version"]
    if isinstance(device.get("wda_local_port"), int):
        capabilities["appium:wdaLocalPort"] = device["wda_local_port"]
    if isinstance(device.get("wda_remote_port"), int):
        capabilities["appium:wdaRemotePort"] = device["wda_remote_port"]
    elif isinstance(device.get("wda_local_port"), int):
        capabilities["appium:wdaRemotePort"] = WDA_DEFAULT_PORT
    if isinstance(device.get("mjpeg_server_port"), int):
        capabilities["appium:mjpegServerPort"] = device["mjpeg_server_port"]
    if isinstance(device.get("derived_data_path"), str):
        capabilities["appium:derivedDataPath"] = config_paths.expand_user_path(
            device["derived_data_path"]
        )
    if device.get("use_preinstalled_wda"):
        capabilities["appium:usePreinstalledWDA"] = True
    return capabilities


def start_session(
    create: Callable[[], T],
    capabilities: Mapping[str, Any],
    *,
    udid: str,
    name: str = "iPhone",
) -> T:
    """Open an Appium session, restarting a preinstalled WDA runner once.

    A preinstalled runner left over from an earlier session refuses a new one;
    stopping it and asking again clears that.  Anything else, and any failure
    of the second try, propagates.
    """
    try:
        return create()
    except Exception as exc:
        if not capabilities.get("appium:usePreinstalledWDA", False):
            raise
        print(
            f"[{name}] Preinstalled WebDriverAgent would not start ({exc});"
            " restarting the installed runner once",
            flush=True,
        )
        stop_wda_runner(udid)
        time.sleep(1)
        return create()


def read_wda_status_payload(port: int, timeout: float = 5.0) -> dict | None:
    """WebDriverAgent's full /status response payload, or None if unreachable."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/status", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict):
                return payload
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        pass
    return None


def read_wda_status(port: int, timeout: float = 5.0) -> dict | None:
    """WebDriverAgent's /status value dict, or None if nothing usable answered."""
    payload = read_wda_status_payload(port, timeout=timeout)
    if not payload:
        return None
    value = payload.get("value")
    return value if isinstance(value, dict) else payload


def wda_session_alive(port: int, session_id: str, timeout: float = 3.0) -> bool:
    """Check if an active WDA session can still perform UI testing actions.

    When an iOS device locks or testmanagerd revokes testing permissions,
    WDA continues responding to /status with 200 OK, but every UI call returns 404
    'Not authorized for performing UI testing actions'. Probing the session's
    window rect detects this immediately.
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/session/{session_id}/window/rect"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
        return False


def wda_screenshot_authorized(port: int, timeout: float = 3.0) -> bool:
    """Check if WDA can perform UI testing actions when no session is active.

    WebDriverAgent exposes /screenshot as a sessionless endpoint. If testmanagerd
    has revoked UI testing authorization, /screenshot returns HTTP 500
    (or an error payload with 'Not authorized for performing UI testing actions').
    Probing /screenshot detects detached runners even when there is no active session.
    """
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/screenshot")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
            val = payload.get("value")
            if isinstance(val, dict) and "error" in val:
                return False
            return isinstance(val, str) and len(val) > 0
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        OSError,
        TimeoutError,
        ValueError,
    ):
        return False


def host_runner_alive(udid: str) -> bool:
    """Is an xcodebuild test runner for this phone still up on this Mac?"""
    try:
        processes = run_output(["/bin/ps", "ax", "-o", "pid=,command="])
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(find_host_wda_pids(processes, udid))


def wda_health(udid: str, port: int = WDA_DEFAULT_PORT) -> str:
    """'live', 'detached', or 'absent' -- see the note above on how they differ.

    'live' means something answers the port, the xcodebuild that owns its
    XCTest link is still running, and any active session is still authorized
    to perform UI testing.
    """
    payload = read_wda_status_payload(port)
    if payload is None:
        status_val = read_wda_status(port)
        if status_val is None:
            return "absent"
        payload = {"value": status_val}

    if not host_runner_alive(udid):
        return "detached"
    session_id = payload.get("sessionId") or (
        payload.get("value", {}).get("sessionId")
        if isinstance(payload.get("value"), dict)
        else None
    )
    if session_id:
        if not wda_session_alive(port, str(session_id)):
            return "detached"
    elif not wda_screenshot_authorized(port):
        return "detached"
    return "live"


def clear_detached_wda(udid: str, port: int = WDA_DEFAULT_PORT) -> bool:
    """Stops a WebDriverAgent that answers but can no longer drive the phone,
    and terminates any orphaned runner on the device when WDA is not live.

    Call before opening a session. Returns True if one was cleared, in which
    case Appium will start a fresh WDA on connect -- which is the whole repair,
    so the caller does not have to ask for useNewWDA and pay for a reinstall.

    Only 'detached' is acted on for the host xcodebuild runner. 'absent' is the
    ordinary cold start. But if a runner process was left on the device while
    the port is absent, it will block xcodebuild from enabling automation mode
    (failing with code 65), so orphaned device runners are cleaned up when not live.
    """
    health = wda_health(udid, port)
    if health == "live":
        return False
    if health == "detached":
        print(
            "iPhone WebDriverAgent is up but detached from XCTest "
            "(it would fail every tap as 'Not authorized for performing UI "
            "testing actions'); restarting it",
            flush=True,
        )
        stop_wda_runner(udid)
        return True

    if host_runner_alive(udid):
        # Silent port, live host runner: WDA is starting, not orphaned. Killing
        # the runner here would pull the phone out from under the launch that
        # is already underway.
        return False

    device_pids = stop_device_wda_processes(udid)
    if device_pids:
        print(
            f"Terminated orphaned device runner(s) {device_pids} before WDA launch",
            flush=True,
        )
        return True
    return False


# `devicectl device info displays` ends with a line naming the backlight, and
# that line is the only place the phone admits its screen is off.  Everything
# else keeps saying the device is fine: it stays paired, the tunnel connects,
# `lockState` reports passcodeRequired false and unlockedSinceBoot true.
BACKLIGHT_LINE = "Main display backlight state:"


def display_backlight(udid: str, timeout: float = 60) -> bool | None:
    """Is the phone's screen lit?  None when devicectl will not say.

    A dark screen is the one device state that stops WebDriverAgent before it
    starts: SpringBoard refuses to launch anything ("Unable to launch ...
    because the device was not, or could not be, unlocked"), XCTest gives up
    with "Timed out while enabling automation mode", and Appium reports that as
    `xcodebuild failed with code 65` -- which reads like a broken Xcode setup
    and sends you off rebuilding WebDriverAgent for an hour.  A phone with no
    passcode is no exception; face up on a desk with the screen off is enough.
    """
    try:
        output = run_output(
            ["xcrun", "devicectl", "device", "info", "displays", "--device", udid,
             "--quiet", "--json-output", "-"],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # CoreDevice's aggregate can still say activeOn after locking. The
    # primary integrated panel reports its own backlight independently.
    try:
        result = json.loads(output)["result"]
        primary = [display for display in result.get("displays", [])
                   if display.get("primary") and "integrated" in display.get("type", {})]
        state = primary[0].get("backlightState") if len(primary) == 1 else result.get("backlightState")
        if state == "off":
            return False
        if state in {"activeOn", "on"}:
            return True
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(BACKLIGHT_LINE):
            state = stripped[len(BACKLIGHT_LINE) :].strip().lower()
            if "off" in state:
                return False
            if "on" in state:
                return True
    return None


def device_requires_unlock(udid: str, timeout: float = 60) -> bool | None:
    """Whether iOS currently requires a passcode before developer launch."""
    try:
        output = run_output(
            ["xcrun", "devicectl", "device", "info", "lockState", "--device", udid],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        if "passcodeRequired:" not in line:
            continue
        value = line.split("passcodeRequired:", 1)[1].strip().split()[0].lower()
        if value in {"true", "false"}:
            return value == "true"
    return None


def asleep_message(udid: str, name: str = "The iPhone") -> str | None:
    """What to tell whoever is watching, when the phone is simply asleep."""
    if display_backlight(udid) is not False:
        return None
    return (
        f"{name}'s screen is off, so iOS refuses to start WebDriverAgent on it "
        "(the failure shows up as 'xcodebuild failed with code 65'). Press its "
        "side button to wake it -- and set Settings > Display & Brightness > "
        "Auto-Lock to Never so an unattended run does not lose it again."
    )


# What iOS says, through Appium, when WebDriverAgent cannot start because the
# phone is waiting on its owner: a Face ID / passcode prompt to enable UI
# automation, a locked phone, or a dark screen.  All of them surface as an
# xcodebuild code 65, which is why that is on the list too -- a code 65 from a
# broken Xcode setup is retried as well, but every retry line prints the error
# so that case still shows up as itself.
OWNER_WAIT_PHRASES = (
    "enabling automation mode",
    "code 65",
    "unable to launch webdriveragent",
    "would not launch webdriveragent",
    "failed to initialize for ui testing",
    "device was not, or could not be, unlocked",
    "device was not unlocked",
    "device is locked",
    "screen is off",
    "passcode required",
    "not authorized for performing ui testing actions",
    "remotexpc upstream connect error",
    "connection was refused to port 8100",
    "developer app certificate is not trusted",
    "keep the phone unlocked",
)

# How often a waiting phone is tried again, and for how long in all.  Half an
# hour covers someone who sees the prompt late; a phone nobody is coming to
# still lets the run end.
OWNER_RETRY_SECONDS = 45.0
OWNER_WAIT_LIMIT_SECONDS = 30 * 60.0


def is_wda_biometric_or_unlock_error(exc: Exception) -> bool:
    """Whether WebDriverAgent failed because the phone is waiting on its owner."""
    message = str(exc).lower()
    return any(phrase in message for phrase in OWNER_WAIT_PHRASES)


# A free Apple developer team's provisioning profiles last seven days.  When the
# one WebDriverAgent was signed with runs out, a WDA that is already running
# keeps going -- so the failure waits for the next reinstall (a restart, an iOS
# update) and then reads as an ordinary code 65 that no prompt on the phone can
# fix.  These are the places a Mac keeps a profile WDA can be signed with.
WDA_PROFILE_SUFFIX = "WebDriverAgentRunner.xctrunner"


def wda_profile_paths(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    found: list[Path] = []
    for folder in (
        home / "Library/Developer/Xcode/UserData/Provisioning Profiles",
        home / "Library/MobileDevice/Provisioning Profiles",
    ):
        if folder.is_dir():
            found.extend(sorted(folder.glob("*.mobileprovision")))
    found.extend(sorted((home / "Library/Developer/Xcode/DerivedData").glob(
        "WebDriverAgent*/Build/Products/Debug-iphoneos/"
        "WebDriverAgentRunner-Runner.app/embedded.mobileprovision"
    )))
    return found


def read_profile(path: Path) -> dict | None:
    """A .mobileprovision's plist, or None when it cannot be decoded."""
    import plistlib

    try:
        raw = subprocess.run(
            ["security", "cms", "-D", "-i", str(path)],
            capture_output=True, timeout=15, check=False,
        ).stdout
        return plistlib.loads(raw) if raw else None
    except Exception:
        return None


def wda_profile_valid_for(
    udid: str,
    profiles: "list[dict] | None" = None,
    now: "Any" = None,
) -> bool | None:
    """Whether this Mac holds an unexpired WDA profile that lists `udid`.

    None when no WDA profile could be read at all (nothing to judge by), so a
    Mac set up some other way is never told its signing is broken.
    """
    import datetime as _dt

    if profiles is None:
        profiles = [p for p in map(read_profile, wda_profile_paths()) if p]
    now = now or _dt.datetime.now(_dt.timezone.utc)
    seen = False
    for profile in profiles:
        app_id = str((profile.get("Entitlements") or {}).get("application-identifier", ""))
        if not (app_id.endswith(WDA_PROFILE_SUFFIX) or app_id.endswith(".*")):
            continue
        seen = True
        expires = profile.get("ExpirationDate")
        if isinstance(expires, _dt.datetime) and expires.tzinfo is None:
            expires = expires.replace(tzinfo=_dt.timezone.utc)
        devices = profile.get("ProvisionedDevices")
        if (
            isinstance(expires, _dt.datetime) and expires > now
            and (devices is None or udid in devices)
        ):
            return True
    return False if seen else None


def expired_profile_message(name: str) -> str:
    return (
        f"[{name}] WebDriverAgent cannot be installed: this Mac has no unexpired "
        "provisioning profile for it (free-team profiles last 7 days; the phone "
        "rejects the old one as 'This provisioning profile has expired'). No "
        "prompt on the phone will fix this. Renew it on a Mac whose Xcode is "
        "signed in to the team: run xcodebuild build-for-testing on "
        "WebDriverAgent with -allowProvisioningUpdates, then copy the new "
        "~/Library/Developer/Xcode/UserData/Provisioning Profiles/*.mobileprovision "
        "to this Mac's same folder, and tap Trust on the phone (Settings > "
        "General > VPN & Device Management) once WDA reinstalls."
    )


def _short_reason(exc: Exception) -> str:
    """The first line of an Appium error, which is the part worth printing."""
    text = str(exc).strip().splitlines()
    first = text[0] if text else type(exc).__name__
    return first if len(first) <= 160 else first[:157] + "..."


def connect_with_retry(
    connect_fn: "Callable[[], T]",
    udid: str,
    name: str = "iPhone",
    interval: float = OWNER_RETRY_SECONDS,
    wait_limit: float = OWNER_WAIT_LIMIT_SECONDS,
) -> "T":
    """`connect_fn()`, tried again every `interval` while the phone waits on its owner.

    A Face ID prompt for UI automation, a locked phone or a dark screen each
    stop WebDriverAgent until someone picks the phone up, and that can be
    minutes.  Before this, the first failure ended the run and the phone sat
    out the day even after the prompt was answered.  Now the connection is
    retried until it works or `wait_limit` seconds pass; any other error is
    raised at once.

    A locked or dark phone is waited out without launching WebDriverAgent at
    all, since a launch on one can only fail.
    """
    deadline = time.monotonic() + wait_limit
    attempt = 0
    restarted_stale = False

    def wait_or_give_up(reason: str, cause: Exception | None = None) -> None:
        if time.monotonic() + interval > deadline:
            message = (
                f"[{name}] gave up after {int(wait_limit // 60)} minute(s) "
                f"waiting: {reason}"
            )
            if cause is not None:
                raise RuntimeError(message) from cause
            raise RuntimeError(message)
        print(
            f"[{name}] {reason}; trying again in {int(interval)}s "
            f"(attempt {attempt}). Unlock the phone and approve the prompt "
            "-- the run carries on by itself.",
            flush=True,
        )
        time.sleep(interval)

    while True:
        attempt += 1
        if display_backlight(udid) is False:
            wait_or_give_up("the screen is off")
            continue
        if device_requires_unlock(udid) is True:
            wait_or_give_up("the phone is locked")
            continue
        try:
            return connect_fn()
        except Exception as exc:
            if stale_wda_error(exc) and not restarted_stale:
                # A runner that lost its XCTest link (the SE, 23 Nov 2026:
                # "previously found element ... not present in the current
                # view").  A fresh runner fixes it; no prompt is waiting, so
                # restart at once instead of waiting 45s on the owner.
                restarted_stale = True
                stop_wda_runner(udid)
                print(
                    f"[{name}] WebDriverAgent had lost its link to the phone "
                    f"({_short_reason(exc)}); restarting it",
                    flush=True,
                )
                continue
            if not is_wda_biometric_or_unlock_error(exc):
                raise
            # The half-started runner would otherwise hold the port the next
            # attempt needs.
            stop_wda_runner(udid)
            if "code 65" in str(exc).lower() and wda_profile_valid_for(udid) is False:
                raise RuntimeError(expired_profile_message(name)) from exc
            try:
                wait_or_give_up(
                    "WebDriverAgent could not start -- a Face ID / passcode "
                    "prompt, a locked phone, or an untrusted developer app "
                    "(Settings > General > VPN & Device Management) "
                    f"({_short_reason(exc)})",
                    exc,
                )
            except RuntimeError:
                raise exc
