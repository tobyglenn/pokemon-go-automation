"""Cross-machine device proxy so one coordinator can drive phones on peer Macs over SSH.

Why this exists:
- A Pokémon GO trade requires driving both phones at once from one Python process,
  because each trade synchronizes screen states step by step (partner profile ->
  LOCAL TRADE -> picker -> NEXT -> 100 Stardust CONFIRM -> finish).
- When the receiver (`sample-receiver`) is on the local Mac and a donor phone is plugged into
  a remote peer Mac (`example-worker`), `RemoteTarget` launches a single-device SSH worker
  on that peer Mac and forwards `Target` and `Phone` operations over newline-
  delimited JSON-RPC, while acquiring the remote phone's per-device lock cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from PIL import Image

from . import (
    clear_pokemon,
    config_paths,
    device_owners,
    fleet_entrypoint,
    gbl_home_recovery as home,
    ios_wda_cleanup,
    pokemon_fleet,
    starter,
)


def _decode_png(b64_data: str) -> Image.Image:
    raw = base64.b64decode(b64_data.encode("ascii"))
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _encode_png(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _readline_timeout(stream: Any, timeout: float) -> str:
    """Read a complete JSON line before the deadline, including large screenshots."""
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return stream.readline()

    deadline = time.monotonic() + timeout
    message = bytearray()
    while True:
        remaining = max(0, deadline - time.monotonic())
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise home.HomeRecoveryError(f"Remote device bridge timed out after {timeout:.0f}s")
        data = os.read(fd, 65536)
        if not data:
            if message:
                raise home.HomeRecoveryError("Incomplete reply from remote device bridge")
            return ""
        message.extend(data)
        if b"\n" in data:
            # A bridge sends one complete reply for each request.
            if message.count(b"\n") != 1 or not message.endswith(b"\n"):
                raise home.HomeRecoveryError("Unexpected data after remote device reply")
            return message.decode("utf-8")


def _close_process(proc: subprocess.Popen) -> None:
    """Give the peer time to release its device lock, then reap the SSH client."""
    for stream in (proc.stdin, proc.stdout):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            proc.wait(timeout=3)


class _RemoteIOSDriver:
    def __init__(self, target: "RemoteTarget") -> None:
        self._target = target

    def is_keyboard_shown(self) -> bool:
        res = self._target._call_sync({"cmd": "ios_keyboard"}, timeout=20.0)
        return bool(res.get("shown"))


class _RemoteIOSDevice:
    """Minimal async/sync adapter matching `IOSGBLDevice` calls used by `starter` and `trade_local`."""

    def __init__(
        self,
        target: "RemoteTarget",
        viewport: dict[str, int] | None = None,
        wda_port: int | None = None,
    ) -> None:
        self._target = target
        self.driver = _RemoteIOSDriver(target)
        self.viewport = dict(viewport or {"width": 375, "height": 667})
        self.config = SimpleNamespace(ios_device={"wda_local_port": wda_port})

    def logical_point(self, pixel: list[int] | tuple[int, int], image: Image.Image) -> list[int]:
        return [
            int(round(pixel[0] * self.viewport["width"] / image.width)),
            int(round(pixel[1] * self.viewport["height"] / image.height)),
        ]

    async def type_text(self, value: str) -> None:
        await self._target._call({"cmd": "ios_type_text", "text": value}, timeout=120.0)

    async def wda_keys(self, text: str, frequency: int = clear_pokemon.IOS_WDA_KEYS_FREQUENCY) -> None:
        await self._target._call(
            {"cmd": "ios_wda_keys", "text": text, "frequency": int(frequency)},
            timeout=120.0,
        )

    async def hide_keyboard(self) -> bool:
        res = await self._target._call({"cmd": "ios_hide_keyboard"}, timeout=20.0)
        return bool(res.get("hidden"))

    async def clear_text(self) -> bool:
        res = await self._target._call({"cmd": "ios_clear_text"}, timeout=20.0)
        return bool(res.get("cleared"))

    async def paste_editor(self, term: str) -> bool:
        reply = await self._target._call({"cmd": "ios_paste_editor", "text": term}, timeout=25.0)
        return bool(reply.get("verified"))

    async def read_editor(self) -> str | None:
        reply = await self._target._call({"cmd": "ios_read_editor"}, timeout=35.0)
        self.driver._pogo_editor_reason = reply.get("reason", "")
        return reply.get("value")

    async def dismiss_ios_return(self) -> bool:
        response = await self._target._call({"cmd": "ios_dismiss_return"}, timeout=20.0)
        return bool(response.get("dismissed"))

    async def tap_done(self) -> bool:
        res = await self._target._call({"cmd": "ios_tap_done"}, timeout=20.0)
        return bool(res.get("tapped"))

    async def delete_keys(self, count: int | None = None) -> None:
        await self._target._call({"cmd": "ios_delete_keys", "count": count}, timeout=60.0)

    async def scroll(self, a: Sequence[int], b: Sequence[int], seconds: float) -> None:
        await self._target._call({"cmd": "ios_swipe", "kind": "scroll", "a": list(a), "b": list(b), "seconds": seconds})

    async def stroke(self, a: Sequence[int], b: Sequence[int], seconds: float) -> None:
        await self._target._call({"cmd": "ios_swipe", "kind": "stroke", "a": list(a), "b": list(b), "seconds": seconds})

    async def touch_and_hold(self, point: Sequence[int], seconds: float) -> None:
        await self._target._call({"cmd": "ios_touch_hold", "point": list(point), "seconds": seconds})

    async def drag(self, a: Sequence[int], b: Sequence[int], seconds: float) -> None:
        await self._target._call({"cmd": "ios_swipe", "kind": "drag", "a": list(a), "b": list(b), "seconds": seconds})


class RemoteTarget(home.Target):
    """Client-side `home.Target` proxy driving a device connected to a peer Mac over SSH."""

    def __init__(
        self,
        label: str,
        machine: str,
        platform: str,
        proc: subprocess.Popen,
        ready: dict[str, Any] | None = None,
    ) -> None:
        self.label = label
        self.machine = machine
        self.platform = platform
        self.restored_cold = False
        self._proc = proc
        self._lock = threading.RLock()
        self._closed = False
        self._req_id = 0
        self.front_package = self._get_front_package if platform == "android" else None
        self.device = (
            _RemoteIOSDevice(
                self,
                viewport=(ready or {}).get("viewport"),
                wda_port=(ready or {}).get("wda_port"),
            )
            if platform == "ios"
            else None
        )

    @classmethod
    def connect_sync(
        cls,
        machine: str,
        machine_config: dict[str, Any],
        spec: pokemon_fleet.DeviceSpec,
    ) -> "RemoteTarget":
        worker = ["-u", "-m", "sources.remote_target", "--device", spec.name]
        cmd = fleet_entrypoint.remote_shell_command(machine, machine_config, worker)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        try:
            timeout = fleet_entrypoint.connect_timeout(machine, machine_config) + 90.0
            assert proc.stdout is not None
            line = _readline_timeout(proc.stdout, timeout)
            if not line:
                raise home.HomeRecoveryError(
                    f"{spec.name}@{machine}: remote device worker exited before ready (code={proc.poll()})"
                )
            try:
                ready = json.loads(line)
            except ValueError as exc:
                raise home.HomeRecoveryError(f"{spec.name}@{machine}: invalid ready payload") from exc
            if not isinstance(ready, dict):
                raise home.HomeRecoveryError(f"{spec.name}@{machine}: invalid ready payload")
            if not ready.get("ok"):
                raise home.HomeRecoveryError(
                    f"{spec.name}@{machine}: remote open_target failed: {ready.get('error', 'unknown error')}"
                )
            return cls(
                spec.name,
                machine,
                str(ready.get("platform") or spec.platform),
                proc,
                ready=ready,
            )
        except BaseException:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                _close_process(proc)
            raise

    @classmethod
    async def connect(
        cls,
        machine: str,
        machine_config: dict[str, Any],
        spec: pokemon_fleet.DeviceSpec,
    ) -> "RemoteTarget":
        return await asyncio.to_thread(cls.connect_sync, machine, machine_config, spec)

    def _call_sync(self, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
        with self._lock:
            if self._closed or self._proc.poll() is not None:
                raise home.HomeRecoveryError(f"{self.label}@{self.machine}: remote device bridge is closed")
            assert self._proc.stdin is not None and self._proc.stdout is not None
            self._req_id += 1
            req = {**payload, "id": self._req_id}
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
                line = _readline_timeout(self._proc.stdout, timeout)
                if not line:
                    raise home.HomeRecoveryError(f"Remote device bridge exited (code={self._proc.poll()})")
                res = json.loads(line)
                if not isinstance(res, dict) or type(res.get("id")) is not int or res["id"] != self._req_id:
                    raise home.HomeRecoveryError(f"Unexpected reply ID for remote request {self._req_id}")
            except (OSError, ValueError, home.HomeRecoveryError) as exc:
                # The last action may still be running. Fail closed rather
                # than sending another command on an out-of-sync stream.
                self._close_sync()
                raise home.HomeRecoveryError(f"{self.label}@{self.machine}: {exc}") from exc
            if not res.get("ok"):
                raise home.HomeRecoveryError(
                    f"{self.label}@{self.machine}: {res.get('error', 'remote command failed')}"
                )
            return res

    async def _call(self, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
        return await asyncio.to_thread(self._call_sync, payload, timeout)

    async def screenshot(self) -> Image.Image:
        res = await self._call({"cmd": "screenshot"}, timeout=60.0)
        return _decode_png(str(res["png"]))

    async def tap(self, point: list[int]) -> None:
        await self._call({"cmd": "tap", "point": [int(point[0]), int(point[1])]}, timeout=30.0)

    async def tap_pixel(self, point: list[int], image: Image.Image) -> None:
        await self._call(
            {"cmd": "tap", "point": [int(point[0]), int(point[1])], "size": [image.width, image.height]},
            timeout=30.0,
        )

    def mapped(self, key: str, image: Image.Image) -> list[int] | None:
        return None

    def _get_front_package(self) -> str:
        res = self._call_sync({"cmd": "front_package"}, timeout=20.0)
        return str(res.get("package") or "")

    def _adb(
        self,
        *arguments: str,
        binary: bool = False,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess:
        res = self._call_sync(
            {"cmd": "adb", "args": list(arguments), "binary": bool(binary), "timeout": float(timeout)},
            timeout=float(timeout) + 20.0,
        )
        if binary:
            stdout: bytes | str = base64.b64decode(str(res.get("stdout_b64", "")).encode("ascii"))
        else:
            stdout = str(res.get("stdout", ""))
        return subprocess.CompletedProcess(
            args=["adb", *arguments],
            returncode=int(res.get("returncode", 0)),
            stdout=stdout,
            stderr=str(res.get("stderr", "")),
        )

    async def restore(self) -> bool:
        res = await self._call({"cmd": "restore"}, timeout=60.0)
        self.restored_cold = bool(res.get("restored_cold"))
        return bool(res.get("acted"))

    def _close_sync(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            _close_process(self._proc)

    def _request_close_sync(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._call_sync({"cmd": "close"}, timeout=15.0)
                except home.HomeRecoveryError:
                    pass
                finally:
                    self._close_sync()

    async def close(self) -> None:
        await asyncio.to_thread(self._request_close_sync)


def discover_attached_devices(
    fleet: pokemon_fleet.FleetConfig | None = None,
    machines: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, str], dict[str, device_owners.Snapshot]]:
    """Return `({enabled_device_name: machine_name}, machine_snapshots)` across all configured Macs."""
    fleet = fleet or pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    machines = fleet_entrypoint.machine_configs() if machines is None else machines
    rows, snapshots = device_owners.survey(fleet, machines)
    owners: dict[str, str] = {}
    for row in rows:
        spec = fleet.devices.get(row.name)
        if spec is not None and spec.enabled and row.attached_to:
            owners[row.name] = row.attached_to
    return owners, snapshots


@contextlib.asynccontextmanager
async def open_fleet_phone(
    name: str,
    shots: Path,
    *,
    machine: str,
    machines: dict[str, dict[str, Any]],
    fleet: pokemon_fleet.FleetConfig,
    dry_run: bool = False,
):
    """Open `starter.Phone` for `name`, locally if attached here or via SSH `RemoteTarget` on a peer Mac."""
    local = fleet_entrypoint.local_machine(machines)
    spec = fleet.devices[getattr(fleet, "transports", {}).get(name, name)]
    def account_phone(target):
        phone = starter.Phone(name, target, shots / spec.name if spec.name != name else shots, dry_run)
        phone.transport_name = spec.name
        return phone
    if machine == local:
        with pokemon_fleet.acquire_device_locks([spec]):
            target = await home.open_target(spec.name)
            try:
                yield account_phone(target)
            finally:
                await target.close()
        return
    target = await RemoteTarget.connect(machine, machines[machine], spec)
    try:
        yield account_phone(target)
    finally:
        await target.close()


# --- Remote worker side -----------------------------------------------------------------


async def _handle_command(target: home.Target, spec: pokemon_fleet.DeviceSpec, req: dict[str, Any]) -> dict[str, Any]:
    cmd = req.get("cmd")
    if cmd == "screenshot":
        image = await target.screenshot()
        return {"ok": True, "png": _encode_png(image)}
    if cmd == "tap":
        point = [int(req["point"][0]), int(req["point"][1])]
        size = req.get("size")
        if hasattr(target, "tap_pixel") and size:
            await target.tap_pixel(point, SimpleNamespace(width=int(size[0]), height=int(size[1])))
        else:
            await target.tap(point)
        return {"ok": True}
    if cmd == "restore":
        acted = await target.restore()
        return {"ok": True, "acted": bool(acted), "restored_cold": bool(target.restored_cold)}
    if cmd == "front_package":
        if spec.platform == "android":
            pkg = await asyncio.to_thread(target.front_package)
        else:
            pkg = None
        return {"ok": True, "package": pkg}
    if cmd == "adb":
        binary = bool(req.get("binary", False))
        proc = await asyncio.to_thread(
            target._adb,
            *[str(a) for a in req.get("args", ())],
            binary=binary,
            timeout=float(req.get("timeout", 30.0)),
        )
        if binary:
            raw = proc.stdout if isinstance(proc.stdout, (bytes, bytearray)) else b""
            return {
                "ok": True,
                "returncode": int(proc.returncode),
                "stdout_b64": base64.b64encode(bytes(raw)).decode("ascii"),
                "stderr": str(proc.stderr or ""),
            }
        return {
            "ok": True,
            "returncode": int(proc.returncode),
            "stdout": str(proc.stdout or ""),
            "stderr": str(proc.stderr or ""),
        }
    if cmd == "ios_keyboard":
        shown = await asyncio.to_thread(lambda: bool(target.device.driver.is_keyboard_shown()))
        return {"ok": True, "shown": shown}
    if cmd == "ios_hide_keyboard":
        hidden = await target.device.hide_keyboard()
        return {"ok": True, "hidden": bool(hidden)}
    if cmd == "ios_paste_editor":
        from . import trade_saved_search
        verified = await asyncio.to_thread(trade_saved_search._paste_ios_editor, target.device.driver, req["text"])
        return {"ok": True, "verified": verified}
    if cmd == "ios_read_editor":
        from . import trade_saved_search
        value = await asyncio.to_thread(trade_saved_search.read_ios_editor, target.device.driver)
        return {"ok": True, "value": value, "reason": getattr(target.device.driver, "_pogo_editor_reason", "")}
    if cmd == "ios_clear_text":
        from appium.webdriver.common.appiumby import AppiumBy

        cleared = False
        elements = await asyncio.to_thread(
            target.device.driver.find_elements, AppiumBy.ACCESSIBILITY_ID, "Clear text"
        )
        for elem in elements or ():
            if await asyncio.to_thread(elem.is_displayed):
                await asyncio.to_thread(elem.click)
                await asyncio.sleep(0.8)
                cleared = True
                break
        return {"ok": True, "cleared": cleared}
    if cmd == "ios_type_text":
        await target.device.type_text(str(req["text"]))
        return {"ok": True}
    if cmd == "ios_wda_keys":
        wda_port = int(target.device.config.ios_device.get("wda_local_port") or ios_wda_cleanup.WDA_DEFAULT_PORT)
        await asyncio.to_thread(
            clear_pokemon._post_wda_keys,
            wda_port,
            str(req["text"]),
            int(req.get("frequency", clear_pokemon.IOS_WDA_KEYS_FREQUENCY)),
        )
        return {"ok": True}
    if cmd == "ios_dismiss_return":
        from . import trade_saved_search
        phone = starter.Phone(spec.name, target, config_paths.state_dir())
        return {"ok": True, "dismissed": await trade_saved_search.dismiss_ios_return(phone)}
    if cmd == "ios_tap_done":
        from appium.webdriver.common.appiumby import AppiumBy

        tapped = False
        buttons = await asyncio.to_thread(target.device.driver.find_elements, AppiumBy.ACCESSIBILITY_ID, "Done")
        for btn in buttons or ():
            if await asyncio.to_thread(btn.is_displayed):
                await asyncio.to_thread(btn.click)
                await asyncio.sleep(2.0)
                tapped = True
                break
        return {"ok": True, "tapped": tapped}
    if cmd == "ios_delete_keys":
        from .raid_scan_ios import CLEAR_KEYS, DELETE_KEY

        count = int(req.get("count") or CLEAR_KEYS)
        await asyncio.to_thread(
            target.device.driver.execute_script,
            "mobile: keys",
            {"keys": [DELETE_KEY] * count},
        )
        return {"ok": True}
    if cmd == "ios_touch_hold":
        x, y = map(int, req["point"])
        await target.device.gesture(
            "Hold saved search", target.device.driver.execute_script, "mobile: touchAndHold",
            {"x": x, "y": y, "duration": float(req["seconds"])},
        )
        return {"ok": True}
    if cmd == "ios_swipe":
        kind = str(req["kind"])
        if kind not in {"scroll", "stroke", "drag"}:
            raise home.HomeRecoveryError(f"Unsupported swipe kind: {kind!r}")
        await getattr(target.device, kind)(list(req["a"]), list(req["b"]), float(req["seconds"]))
        return {"ok": True}
    raise home.HomeRecoveryError(f"Unknown RPC command: {cmd!r}")


async def _serve_device(device_name: str, proto_out: Any) -> int:
    def send(payload: dict[str, Any]) -> None:
        proto_out.write(json.dumps(payload) + "\n")
        proto_out.flush()

    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    if device_name not in fleet.devices:
        send({"ok": False, "error": f"Unknown configured device {device_name!r}"})
        return 1
    spec = fleet.devices[device_name]
    with pokemon_fleet.acquire_device_locks([spec]):
        target = await home.open_target(spec.name)
        try:
            ready: dict[str, Any] = {"ok": True, "ready": True, "platform": spec.platform, "label": target.label}
            if spec.platform == "ios":
                ready["viewport"] = dict(target.device.viewport)
                ready["wda_port"] = int(
                    target.device.config.ios_device.get("wda_local_port") or ios_wda_cleanup.WDA_DEFAULT_PORT
                )
            send(ready)
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                req = json.loads(line)
                req_id = req.get("id")
                if req.get("cmd") == "close":
                    send({"id": req_id, "ok": True})
                    break
                try:
                    res = await _handle_command(target, spec, req)
                    send({"id": req_id, **res})
                except Exception as exc:
                    send({"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            await target.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # Reserve original stdout solely for JSON-RPC so stray `print()` calls hit stderr.
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_out = os.fdopen(proto_fd, "w", buffering=1, encoding="utf-8")

    fleet_entrypoint.apply_local_environment()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device", required=True, help="configured fleet device name to serve")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_serve_device(args.device, proto_out))
    except Exception as exc:
        with contextlib.suppress(Exception):
            proto_out.write(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}) + "\n")
            proto_out.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
