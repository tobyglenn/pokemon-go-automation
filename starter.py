#!/usr/bin/env python3
"""Start the day on every phone: free Mega Evolutions, buddy play, gifts, then GBL."""

from __future__ import annotations

import sys

from sources import fleet_entrypoint, pokemon_fleet, starter


def main(arguments: list[str] | None = None) -> int:
    argv = fleet_entrypoint.apply_registry_override(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    helping = any(value in {"--help", "-h"} for value in argv)
    local = fleet_entrypoint.local_requested(argv)
    forwarded = fleet_entrypoint.clean_scope_flags(argv)
    if not local and not helping:
        return fleet_entrypoint.run_all_machines("starter.py", forwarded)
    if not helping:
        # adb lives in a directory the machine record names, not on PATH.
        fleet_entrypoint.apply_local_environment()
    return starter.main(forwarded)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
