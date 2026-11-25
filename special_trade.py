#!/usr/bin/env python3
"""Receiver's daily special trade: a spare legendary for a missing shiny.

Runs across all configured computers (`local` and peers over SSH) unless
`--local` is passed, so any attached partner device can trade directly with
the configured receiver.
"""

from __future__ import annotations

import sys

from sources import fleet_entrypoint, pokemon_fleet, special_trade


def main(arguments: list[str] | None = None) -> int:
    raw = fleet_entrypoint.apply_registry_override(sys.argv[1:] if arguments is None else arguments)
    argv = fleet_entrypoint.clean_scope_flags(raw)
    if fleet_entrypoint.local_requested(raw):
        argv.append("--local")
    if not any(value in {"--help", "-h"} for value in argv):
        # adb lives in a directory the machine record names, not on PATH.
        fleet_entrypoint.apply_local_environment()
    return special_trade.main(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
