#!/usr/bin/env python3
"""The buddy's daily hearts on every phone, without the rest of starter.py.

Feeds berries until the buddy is full (at most one Golden Razz, only while
more than 25 are in the bag), takes a snapshot, pets it, then runs a Blanche
training battle with it for the Battle together heart.  It runs starter.py's
buddy and battle steps; every starter.py flag works here too
(`--devices`, `--berries`, `--skip battle`, `--dry-run`, `--local`).
"""

from __future__ import annotations

import sys

from sources import fleet_entrypoint, pokemon_fleet, starter

BUDDY_STEPS = ["buddy", "battle"]


def buddy_arguments(argv: list[str]) -> list[str]:
    """starter.py arguments for just the buddy steps, unless --only already picks some."""
    if "--only" in argv:
        return argv
    return ["--only", *BUDDY_STEPS, *argv]


def main(arguments: list[str] | None = None) -> int:
    argv = fleet_entrypoint.apply_registry_override(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    helping = any(value in {"--help", "-h"} for value in argv)
    local = fleet_entrypoint.local_requested(argv)
    forwarded = fleet_entrypoint.clean_scope_flags(argv)
    if not local and not helping:
        return fleet_entrypoint.run_all_machines("buddy.py", forwarded)
    if not helping:
        # adb lives in a directory the machine record names, not on PATH.
        fleet_entrypoint.apply_local_environment()
    return starter.main(buddy_arguments(forwarded), sleep_after=False)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
