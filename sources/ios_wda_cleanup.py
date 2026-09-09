#!/usr/bin/env python3
# Internal iOS lifecycle helper.
"""Targeted WebDriverAgent shutdown helpers for real iOS devices."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request


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
            and "not in current view" in message
        )
    )


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
    if session_id and not wda_session_alive(port, str(session_id)):
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
            ["xcrun", "devicectl", "device", "info", "displays", "--device", udid],
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
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
