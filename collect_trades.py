#!/usr/bin/env python3
"""Collect Lucky Dex gaps for the receiver and complete valuable trades across the fleet."""

from __future__ import annotations

import sys

from sources import collect_trades, fleet_entrypoint


def main(argv: list[str] | None = None) -> int:
    raw = fleet_entrypoint.apply_registry_override(sys.argv[1:] if argv is None else argv)
    arguments = fleet_entrypoint.clean_scope_flags(raw)
    if fleet_entrypoint.local_requested(raw):
        arguments.append("--local")
    return collect_trades.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
