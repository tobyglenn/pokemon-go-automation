#!/usr/bin/env python3
"""Where each account should spend candy and XL candy on raid attackers.

Two halves, one command:

- `--scan` reads each phone's Pokémon storage, read-only: per raid-relevant
  species the copies held, which are shadows, hundos and 3-star (82-98%), and
  for the best few individuals their CP, HP, power-up price and moves, plus the
  species' candy, XL candy and Mega Energy.  It runs on whichever computer the
  phone is plugged into.
- the default (no `--scan`) turns each device's scan into a per-type plan: the
  type's best attackers, the best you can field today, which individual your
  candy and XL candy should go into and what that costs, which Megas this
  account cannot use yet, and the top attackers it does not own.

The scan never powers anything up, teaches a move, Mega Evolves or transfers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources import (  # noqa: E402
    config_paths,
    device_owners,
    fleet_entrypoint,
    pokemon_fleet,
    raid_inventory,
    raid_plan,
    raid_scan,
)


def run_scan(argv: list[str]) -> int:
    """Hand the scan to whichever computers hold the selected phones."""
    if fleet_entrypoint.local_requested(argv) or fleet_entrypoint.has_option(
        argv, fleet_entrypoint.ALL_MACHINES_FLAG
    ):
        fleet_entrypoint.apply_local_environment()
        return raid_scan.main([a for a in argv if a != "--scan"])
    return fleet_entrypoint.run_all_machines("raidspend.py", fleet_entrypoint.clean_scope_flags(argv))


def _as_json(plan: raid_plan.AccountPlan) -> dict:
    types = {}
    for attack_type, tp in plan.types.items():
        def option(o: raid_plan.Option) -> dict:
            return {
                "attacker": o.attacker.name,
                "individual": o.owned.describe(),
                "moves": o.reach.moveset,
                "needs_tm": o.needs_tm,
                "now_pct": round(o.now.er / tp.top.er * 100, 1),
                "reach_level": o.reach_level,
                "reach_pct": round(o.reach.er / tp.top.er * 100, 1),
                "cost": {"candy": o.reach_cost.candy, "xl": o.reach_cost.xl,
                         "dust": o.reach_cost.dust},
            }
        types[attack_type] = {
            "meta": [{"name": r.attacker.name, "pct": round(r.er / tp.top.er * 100, 1),
                      "owned": r.attacker.key in tp.owned_keys} for r in tp.meta],
            "best_now": option(tp.best_now(False)) if tp.best_now(False) else None,
            "best_mega_now": option(tp.best_now(True)) if tp.best_now(True) else None,
            "spends": [option(o) for mega in (False, True) for o in tp.spends(mega)],
            "locked_megas": [{"name": r.attacker.name, "why": why}
                             for r, why in tp.unavailable_megas],
        }
    return {"device": plan.device, "stardust": plan.stardust, "types": types}


def run_plan(args) -> int:
    """Plan every selected device, reading each scan from the machine that took it."""
    fleet = pokemon_fleet.load_fleet(args.config)
    wanted = [name.strip() for name in (args.devices or []) if name.strip()]
    if "all" in wanted:
        wanted = list(fleet.devices)

    owners: dict[str, str] = {}
    if not args.local and args.scans is None:
        # One probe of every computer, so a phone whose scan lives on the other
        # machine is still reported rather than silently missing.
        rows, _ = device_owners.survey(fleet)
        owners = {row.name: (row.attached_to or "local") for row in rows}

    missing, payload = [], {}
    for device in wanted:
        machine = "local" if args.local or args.scans else owners.get(device, "local")
        rows, where = raid_scan.read_scan(device, args.scans, machine)
        scans = [raid_inventory.SpeciesScan.from_dict(r) for r in rows
                 if isinstance(r, dict) and r.get("schema") == raid_inventory.SCHEMA]
        if not scans:
            missing.append(device)
            continue
        plan = raid_plan.build(device, scans)
        if args.json:
            payload[device] = _as_json(plan)
        else:
            print(f"[scan from {where}]")
            print(raid_plan.rare_report(plan))
            if args.full:
                print()
                print(raid_plan.report(plan))
            print()
    if args.json:
        print(json.dumps(payload, indent=2))
    if missing:
        print(f"No scan yet for: {', '.join(missing)}. "
              f"Run: python raidspend.py --scan --devices {missing[0]}", file=sys.stderr)
    return 1 if missing and not payload and not args.json and len(missing) == len(wanted) else 0


def main(argv=None) -> int:
    argv = fleet_entrypoint.apply_registry_override(
        list(sys.argv[1:] if argv is None else argv)
    )
    helping = any(value in {"--help", "-h"} for value in argv)
    if "--scan" in argv and not helping:
        return run_scan(fleet_entrypoint.clean_scope_flags(argv))

    parser = argparse.ArgumentParser(
        description="Plan candy and XL candy spending on raid attackers, per device. "
                    "Add --scan to read the phones first (read-only)."
    )
    parser.add_argument("--scan", action="store_true",
                        help="scan the selected phones' storage instead of planning")
    parser.add_argument("--resume", action="store_true",
                        help="with --scan: keep species already read and do the rest")
    parser.add_argument("--devices", nargs="+", action="extend", default=[],
                        help="configured device names, or 'all'")
    parser.add_argument("--scans", type=Path, default=None,
                        help="read raidscan_<device>.json from this directory instead "
                             "of each phone's computer")
    parser.add_argument("--full", action="store_true",
                        help="also print the per-type power-up plan using the species' own candy")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--local", action="store_true",
                        help="only read scans stored on this computer")
    parser.add_argument("--config", type=Path,
                        default=config_paths.default_config("pokemon-fleet.yaml"))
    args = parser.parse_args(argv)
    args.devices = list(args.devices) or ["all"]
    if not helping:
        fleet_entrypoint.apply_local_environment()
    return run_plan(args)


if __name__ == "__main__":
    raise SystemExit(main())
