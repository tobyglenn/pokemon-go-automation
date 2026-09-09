#!/usr/bin/env python3
"""Ask usbmuxd which iPhones are actually attached to this Mac.

Appium's XCUITest driver decides how to reach a phone from this same list: a
UDID that usbmuxd knows is driven over the usbmux port forwarder, and one it
does not know falls back to a RemoteXPC tunnel that is absent unless the tunnel
registry was started as root. An unplugged phone therefore fails deep inside
session creation with `Cannot create port forwarder via RemoteXPC tunnel`,
which reads like a tunnel misconfiguration rather than a missing cable.

Reading the same source Appium reads lets the commands pick the profile for the
phone in front of them, and lets them say "not plugged in" when it is not.

usbmuxd speaks a 16-byte little-endian header (payload length including the
header, protocol version, message type, tag) followed by a plist body.
Version 1 / message 8 is the plist protocol.
"""

from __future__ import annotations

import plistlib
import socket
import struct

USBMUXD_SOCKET = "/var/run/usbmuxd"
_PLIST_VERSION = 1
_PLIST_MESSAGE = 8
_HEADER = struct.Struct("<IIII")
_TIMEOUT_SECONDS = 5.0


class AttachedDeviceError(RuntimeError):
    """usbmuxd could not be reached or answered something unexpected."""


def _receive_exactly(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise AttachedDeviceError("usbmuxd closed the connection early")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _list_devices() -> list[dict]:
    request = plistlib.dumps(
        {
            "MessageType": "ListDevices",
            "ClientVersionString": "platform-tools",
            "ProgName": "platform-tools",
        }
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT_SECONDS)
            sock.connect(USBMUXD_SOCKET)
            sock.sendall(
                _HEADER.pack(
                    len(request) + _HEADER.size, _PLIST_VERSION, _PLIST_MESSAGE, 1
                )
                + request
            )
            length, _version, _message, _tag = _HEADER.unpack(
                _receive_exactly(sock, _HEADER.size)
            )
            if length < _HEADER.size:
                raise AttachedDeviceError(f"usbmuxd sent a short reply ({length} bytes)")
            payload = plistlib.loads(_receive_exactly(sock, length - _HEADER.size))
    except (OSError, plistlib.InvalidFileException) as exc:
        raise AttachedDeviceError(f"Could not query usbmuxd at {USBMUXD_SOCKET}: {exc}") from exc
    devices = payload.get("DeviceList")
    if not isinstance(devices, list):
        raise AttachedDeviceError("usbmuxd did not return a DeviceList")
    return devices


def attached_devices() -> dict[str, str]:
    """Every UDID usbmuxd can see, mapped to its connection type.

    A phone paired over Wi-Fi reports `Network`; only `USB` can carry
    WebDriverAgent without a separately created tunnel.
    """
    found: dict[str, str] = {}
    for entry in _list_devices():
        properties = entry.get("Properties")
        if not isinstance(properties, dict):
            continue
        udid = properties.get("SerialNumber")
        if not isinstance(udid, str) or not udid.strip():
            continue
        connection = properties.get("ConnectionType")
        connection = connection if isinstance(connection, str) else "Unknown"
        # A phone on both transports is usable, so let USB win the mapping.
        if found.get(udid) != "USB":
            found[udid] = connection
    return found


def usb_udids() -> set[str]:
    """The UDIDs reachable over the cable, which is what WebDriverAgent needs."""
    return {udid for udid, connection in attached_devices().items() if connection == "USB"}
