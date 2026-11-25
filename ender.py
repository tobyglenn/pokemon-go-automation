#!/usr/bin/env python3
"""Finish buddy hearts, exchange gifts, trade across the fleet, and leave storage open for review."""
from __future__ import annotations

import argparse
import asyncio
from importlib.util import find_spec
import sys

from sources import fleet_entrypoint, pokemon_fleet


def main(argv=None):
    raw = fleet_entrypoint.apply_registry_override(list(sys.argv[1:] if argv is None else argv))
    if "--goplus-only" in raw:
        import starter as starter_command

        forwarded = [arg for arg in raw if arg != "--goplus-only"]
        return starter_command.main([*forwarded, "--only", "goplus"])

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goplus-only", action="store_true",
                        help="run the fleet Go Plus reconnect without the evening routine")
    parser.add_argument("--plan", action="store_true", help="describe the evening routine without contacting phones")
    parser.add_argument("--local", action="store_true", help="only use phones attached to this computer")
    parser.add_argument("--prepare-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prepare-handsets", nargs="*", help=argparse.SUPPRESS)
    local = fleet_entrypoint.local_requested(raw)
    args = parser.parse_args(fleet_entrypoint.clean_scope_flags(raw))
    if args.plan:
        print("Finish available buddy play, snapshot, feeding and training-battle hearts; send and open gifts.")
        print("Check every host's daily trade receipts and the 100-trade cap for each account.")
        print("Trade receiver-first from each handset's live pinned favorites (priority_trade.py): "
              "XXL/XXS, then the receiver's Lucky and Perfect gaps, then donor-to-donor lists.")
        print("Guaranteed Lucky trades are skipped, never spent.")
        print("Open each handset's verified SafeTrash favorite for manual review, then turn the display off.")
        return 0
    if not args.prepare_worker and find_spec("sources.priority_trade") is None:
        raise pokemon_fleet.FleetError(
            "This host is an Ender device worker. Start ender.py on the fleet coordinator.")
    from sources import ender as evening
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
    fleet_entrypoint.apply_local_environment()
    trade = asyncio.run(evening.trade_and_review(local))
    return prep or trade


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"[ender] {exc}", file=sys.stderr)
        raise SystemExit(1)
