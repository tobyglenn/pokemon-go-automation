#!/usr/bin/env python3
"""Take a raid storage scan on whichever computers hold the phones.

The scan itself is read-only and lives in the platform workers: this module
decides *which* phone to scan, and the public entry point decides which computer
that scan runs on.  A phone is scanned by the computer it is plugged into, so
moving a cable needs no configuration change.

Each scan writes to the private state directory of the computer that took it.
Nothing is copied between machines here: the state directory is where run state
belongs, and `raidspend.py` reads a remote scan back over SSH when it plans.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

from . import config_paths, fleet_entrypoint, pokemon_fleet, raid_meta

SCAN_OPERATION = "raidscan"


class RaidScanDispatchError(RuntimeError):
    """The scan could not be routed to a phone, or a worker failed."""


def read_scan(device_name: str, directory: Path | None = None,
              machine: str | None = None) -> tuple[list[dict], str]:
    """The scan rows for a device, and where they came from.

    A scan belongs to the computer that took it, because the private state
    directory is where run state belongs and a phone is scanned by whichever
    machine holds its cable.  Reporting a device whose scan was taken elsewhere
    therefore reads that file back over the same SSH alias the fan-out uses,
    rather than quietly reporting nothing.

    The remote is asked to resolve its own state directory rather than being
    told where to look: that path is the remote's to decide, and guessing it
    here would be a second source of truth.
    """
    local_path = scan_path_for(device_name, directory)
    if machine is None or machine == os.environ.get("POGO_MACHINE", "local"):
        if not local_path.is_file():
            return [], "local"
        return json.loads(local_path.read_text()), "local"

    machines = fleet_entrypoint.machine_configs()
    config = machines.get(machine)
    if not isinstance(config, dict):
        return [], machine
    reader = (
        "import json,pathlib,sys;from sources import config_paths;"
        "p=config_paths.state_dir()/sys.argv[1];"
        "print(p.read_text() if p.is_file() else '[]')"
    )
    command = fleet_entrypoint.remote_shell_command(machine, config, ["-c", reader, local_path.name])
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode or not result.stdout.strip():
        return [], machine
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], machine
    return (rows if isinstance(rows, list) else []), machine


def load_terms(path: Path | None = None) -> list[tuple[str, str, bool]]:
    """The species to scan: a file when given, otherwise today's rankings."""
    if path:
        payload = json.loads(Path(path).read_text())
        return [(str(t[0]), str(t[1]), bool(t[2]) if len(t) > 2 else True) for t in payload]
    return raid_meta.scan_terms()


def scan_path_for(device_name: str, directory: Path | None = None) -> Path:
    base = Path(directory) if directory else config_paths.state_dir()
    return base / f"raidscan_{device_name}.json"


def scan_android(spec, args, terms) -> int:
    """Run the Android storage worker for one configured handset."""
    from . import raid_scan_android

    out = scan_path_for(spec.name, args.out_dir)
    print(f"[{spec.name}] scanning {len(terms)} species into {out.name}", flush=True)
    records = raid_scan_android.scan_phone(
        spec.identifier, terms, label=spec.name, checkpoint=out, resume=args.resume,
    )
    return _summarise(spec.name, records)


def scan_ios(spec, args, terms) -> int:
    """Run the iOS storage worker for one configured iPhone or iPad."""
    try:
        from . import raid_scan_ios
    except ImportError as exc:
        raise RaidScanDispatchError(
            f"{spec.name}: the iOS storage worker is not installed on this "
            f"computer ({exc})"
        ) from exc

    out = scan_path_for(spec.name, args.out_dir)
    print(f"[{spec.name}] scanning {len(terms)} species into {out.name}", flush=True)
    records = raid_scan_ios.scan_device(
        spec, terms, label=spec.name, checkpoint=out, resume=args.resume,
    )
    return _summarise(spec.name, records)


def _summarise(name: str, records) -> int:
    failed = [r for r in records if r.error]
    held = [r for r in records if r.owned > 0]
    print(
        f"[{name}] done: {len(records)} scanned, {len(held)} held, "
        f"{len(failed)} failed",
        flush=True,
    )
    return 1 if records and len(failed) == len(records) else 0


def _scan_one(spec, args, terms) -> int:
    try:
        if spec.platform == "android":
            return scan_android(spec, args, terms)
        if spec.platform == "ios":
            return scan_ios(spec, args, terms)
        print(f"[{spec.name}] unsupported platform {spec.platform}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - one phone must not stop the others
        print(f"[{spec.name}] Error: {exc}", file=sys.stderr, flush=True)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan selected phones' Pokémon storage for raid attackers."
    )
    parser.add_argument("--devices", nargs="+", default=["all"],
                        help="configured device names, or 'all' by itself")
    parser.add_argument("--terms", type=Path, default=None,
                        help="JSON [search, display, shadow_eligible] list; default: today's rankings")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="where raidscan_<device>.json is written "
                             "(default: the private state directory)")
    parser.add_argument("--resume", action="store_true",
                        help="keep species already scanned and do the rest")
    parser.add_argument("--limit", type=int, default=0,
                        help="only scan the first N species (for a trial run)")
    parser.add_argument("--local", action="store_true",
                        help="only scan phones attached to this computer")
    parser.add_argument("--config", type=Path,
                        default=config_paths.default_config("pokemon-fleet.yaml"))
    args = parser.parse_args(argv)
    # Run directly (`-m sources.raid_scan`) nothing else has put this
    # machine's adb on PATH, and every Android phone then reads as detached.
    fleet_entrypoint.apply_local_environment()

    names = [value.strip() for value in args.devices if value.strip()]
    fleet = pokemon_fleet.load_fleet(args.config)

    machine = os.environ.get("POGO_MACHINE", "")
    try:
        specs = pokemon_fleet.select_devices(fleet, names, SCAN_OPERATION)
    except pokemon_fleet.FleetError as exc:
        # Most likely: a device with no `raidscan` key in its operations. Say
        # so plainly, because the fix is one line in the private config.
        print(f"Error: {exc}", file=sys.stderr)
        print(
            f"  A phone opts in to this workflow with a `{SCAN_OPERATION}` entry "
            f"under its `operations:` in {args.config}.",
            file=sys.stderr,
        )
        return 1

    if args.local:
        # This computer only: keep what is actually attached here. `select_devices`
        # already filters by connectivity when it can, and POGO_MACHINE names the
        # machine a delegated worker is running on.
        from . import device_owners

        snapshot = device_owners.local_snapshot(machine or "local")
        specs = [s for s in specs if snapshot.holds(s.platform, s.identifier)]
        if not specs:
            print(
                "Nothing selected is attached to this computer. "
                "`scripts/fleet.py owners` shows where each phone is.",
                file=sys.stderr,
            )
            return 1

    if not specs:
        print("No devices selected.", file=sys.stderr)
        return 1

    print(
        pokemon_fleet.attachment_summary(
            SCAN_OPERATION, [s.name for s in specs], 0, machine
        ),
        flush=True,
    )

    terms = load_terms(args.terms)
    if args.limit:
        terms = terms[: args.limit]
    # Each phone is its own handset and its own screen: scanning them one after
    # another only multiplies the wall time.
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        failures = sum(pool.map(lambda spec: _scan_one(spec, args, terms), specs))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())