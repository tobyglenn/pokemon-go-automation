#!/usr/bin/env python3
"""Wait on manually opened Pokémon GO encounters and throw an Excellent curve."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from sources import excellent_throw_android, excellent_throw_ios, fleet_entrypoint


def _has_android_devices() -> bool:
    """Return True when adb currently reports at least one connected Android."""
    adb = Path(__file__).resolve().parent / "adb"
    if not (adb.exists() and os.access(adb, os.X_OK)):
        adb_in_path = shutil.which("adb")
        if adb_in_path is None:
            return False
        adb = Path(adb_in_path)

    try:
        result = subprocess.run(
            [str(adb), "devices"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            return True
    return False


def _has_ios_devices() -> bool:
    """Return True when an iPhone has a matching excellent-throw profile."""
    try:
        return bool(excellent_throw_ios.select_configs())
    except Exception:
        return False


def _run_modules_parallel(args: list[str], run_ios: bool, run_android: bool) -> int:
    """Run enabled platform modules together when both are present."""
    if not (run_ios or run_android):
        return 0

    results: dict[str, int | Exception] = {}
    lock = threading.Lock()

    def launch(name: str, fn) -> threading.Thread:
        def worker() -> None:
            try:
                value = fn(args)
            except Exception as exc:  # pragma: no cover - delegates module-specific failure
                value = exc
            with lock:
                results[name] = value

        thread = threading.Thread(target=worker, name=name, daemon=True)
        thread.start()
        return thread

    threads: list[threading.Thread] = []
    if run_ios:
        threads.append(launch("iOS", excellent_throw_ios.main))
    if run_android:
        threads.append(launch("Android", excellent_throw_android.main))

    for thread in threads:
        thread.join()

    for name, value in results.items():
        if isinstance(value, Exception):
            print(f"{name} module failed: {value}", file=sys.stderr)
            return 1
        if value != 0:
            print(f"{name} module exited with status {value}", file=sys.stderr)
            return value

    return 0


def _drop_fleet_routing_args(arguments: list[str]) -> list[str]:
    """Drop internal fleet flags injected by run_all_machines."""
    # A direct Android invocation is allowed to select a serial with
    # ``--devices``.  Fleet workers are the only callers that append
    # ``--allow-empty``; use that marker so direct selectors are preserved.
    if "--allow-empty" not in arguments:
        return list(arguments)
    filtered: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument == "--allow-empty":
            continue
        if argument == "--devices":
            skip_next = True
            continue
        if argument.startswith("--devices="):
            continue
        filtered.append(argument)
    return filtered


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--android", action="store_true", help="Use Android adb thrower")
    parser.add_argument("--ios", action="store_true", help="Use iOS WebDriverAgent thrower")
    parser.add_argument(fleet_entrypoint.ALL_MACHINES_FLAG, action="store_true", help="Run on all configured hosts")
    parser.add_argument("--help", action="store_true")
    parsed, remaining = parser.parse_known_args(arguments)

    if parsed.help:
        if parsed.android and parsed.ios:
            print("Use only one platform selector: --android or --ios", file=sys.stderr)
            return 2
        if parsed.android:
            return excellent_throw_android.main(["--help"] + remaining)
        if parsed.ios:
            return excellent_throw_ios.main(["--help"] + remaining)
        return excellent_throw_ios.main(["--help"] + remaining)

    forward_args = _drop_fleet_routing_args(list(remaining))
    # The public command handles one encounter per attached phone.  A single
    # encounter can take several balls; the platform workers keep retrying
    # until the encounter has actually ended before this count advances.
    passive_modes = {"--check", "--dry-run", "--probe-ring"}
    if (
        "--throws" not in forward_args
        and not any(item.startswith("--throws=") for item in forward_args)
        and not any(item in forward_args for item in passive_modes)
    ):
        forward_args.extend(["--throws", "1"])
    fleet_forwarded = list(forward_args)
    if parsed.android:
        fleet_forwarded.insert(0, "--android")
    if parsed.ios:
        fleet_forwarded.insert(0, "--ios")

    if parsed.allmachines:
        return fleet_entrypoint.run_all_machines("excellent_throw.py", fleet_forwarded)

    if parsed.android and parsed.ios:
        print("Use only one platform selector: --android or --ios", file=sys.stderr)
        return 2

    run_android = parsed.android or (not parsed.ios and _has_android_devices())
    run_ios = parsed.ios or (not parsed.android and _has_ios_devices())

    if not run_android and not run_ios:
        print(
            "No iOS or Android device available for excellent_throw. "
            "Connect at least one supported phone.",
            file=sys.stderr,
        )
        return 1

    if run_android and not run_ios:
        return excellent_throw_android.main(forward_args)
    if run_ios and not run_android:
        return excellent_throw_ios.main(forward_args)

    return _run_modules_parallel(forward_args, run_ios=True, run_android=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Excellent-throw watcher cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (
        RuntimeError,
        excellent_throw_ios.ExcellentThrowError,
        excellent_throw_android.AndroidExcellentThrowError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
