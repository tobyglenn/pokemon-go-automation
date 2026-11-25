#!/usr/bin/env python3
"""Finish buddy hearts, exchange gifts, trade across the fleet, and leave storage open for review."""
from __future__ import annotations

import argparse
import asyncio
from datetime import date
from importlib.util import find_spec
import sys

from sources import fleet_entrypoint, pokemon_fleet


def main(argv=None):
    raw = fleet_entrypoint.apply_registry_override(list(sys.argv[1:] if argv is None else argv))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="describe the evening routine without contacting phones")
    parser.add_argument("--local", action="store_true", help="only use phones attached to this computer")
    parser.add_argument("--prepare-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prepare-handsets", nargs="*", help=argparse.SUPPRESS)
    local = fleet_entrypoint.local_requested(raw)
    args = parser.parse_args(fleet_entrypoint.clean_scope_flags(raw))
    if args.plan:
        print("Finish available buddy play, snapshot, feeding and training-battle hearts; send and open gifts.")
        print("Check both Macs' daily trade receipts and the 100-trade cap for each account.")
        print("Use prepared named favorites for both sides of a trade. Skip missing or stale choices.")
        print("Guaranteed trades require both Lucky Friends badges, live Special Trade allowances and individual approval.")
        print("Open each handset's verified SafeTrash favorite for manual review, then turn the display off.")
        return 0
    if not args.prepare_worker and find_spec("sources.ender_inventory") is None:
        raise pokemon_fleet.FleetError(
            "This host is an Ender device worker. Start ender.py on the fleet coordinator holding the prepared favorites.")
    from sources import ender as evening
    evening_date = date.today()
    if args.prepare_worker or local:
        fleet_entrypoint.apply_local_environment()
        prep = asyncio.run(evening.prepare_local(args.prepare_handsets))
        if args.prepare_worker:
            return prep
    else:
        try:
            selected = evening.prepare_handsets()
            prep = fleet_entrypoint.run_all_machines("ender.py", ["--prepare-worker", "--prepare-handsets", *selected])
        except pokemon_fleet.FleetError as exc:
            print(f"[ender] preparation incomplete: {exc}", flush=True)
            prep = 1
    # One coordinator owns cross-host pairs, daily counts and the final review.
    from sources import collect_trades
    fleet_entrypoint.apply_local_environment()
    options = collect_trades.parse_args(["--finish-night", *(["--local"] if local else [])])
    trade = asyncio.run(collect_trades.run(options, evening_date=evening_date))
    return prep or trade


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"[ender] {exc}", file=sys.stderr)
        raise SystemExit(1)
