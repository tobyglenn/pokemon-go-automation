#!/usr/bin/env python3
"""Play a day's whole GBL allotment on the named phones, restarting stalled legs."""

from __future__ import annotations

import sys
from typing import Sequence

from sources import fleet_entrypoint, gbl_day, pokemon_fleet


def main(arguments: Sequence[str] | None = None) -> int:
    args_list = fleet_entrypoint.apply_registry_override(list(sys.argv[1:] if arguments is None else arguments))
    local = fleet_entrypoint.local_requested(args_list)
    args_list = fleet_entrypoint.clean_scope_flags(args_list)
    if not local and not any(value in {"--help", "-h"} for value in args_list):
        if not fleet_entrypoint.has_option(args_list, "--devices"):
            args_list += ["--devices", "all"]
        return fleet_entrypoint.run_all_machines("gbl_day.py", args_list)
    if not any(value in {"--help", "-h"} for value in args_list):
        fleet_entrypoint.apply_local_environment()
    return gbl_day.main(args_list)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("GBL day cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (gbl_day.GBLDayError, pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
