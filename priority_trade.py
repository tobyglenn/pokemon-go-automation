#!/usr/bin/env python3
"""Run receiver-first priority trades across the fleet."""

from __future__ import annotations

import sys

from sources import fleet_entrypoint, priority_trade


def main(argv: list[str] | None = None) -> int:
    raw = fleet_entrypoint.apply_registry_override(sys.argv[1:] if argv is None else argv)
    arguments = fleet_entrypoint.clean_scope_flags(raw)
    if fleet_entrypoint.local_requested(raw):
        arguments.append("--local")
    return priority_trade.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
