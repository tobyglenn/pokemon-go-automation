"""Daily 100-trade automation for high-value, low-IV Pokemon across attached fleet pairs.

Each computer runs its attached pair in parallel, targeting up to 100 trades/day:
1. Tier 1 (`98Plus == 0`): raid/Mega families either phone has zero 98Plus copies of.
2. Tier 2 (`98Plus < --target-good`): deepen 98Plus depth (mirror trades first).
3. Tier 3: fill remaining daily budget up to `--trades` with high-value mirror and
   cross-family IV/XL rerolls, keeping `< 3` spare species for Lucky Trades and
   respecting per-device `trade_keep_last`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Sequence

from . import config_paths, gbl_home_recovery as home, pokemon_fleet, screen_router, starter_trade, trade_plan


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--devices", nargs="+", help="only these fleet device names")
    parser.add_argument("--pair", nargs=2, metavar=("DEVICE_A", "DEVICE_B"),
                        help="explicit pair attached to the same host")
    parser.add_argument("--trades", type=int, default=trade_plan.DAILY_TRADES,
                        help=f"daily trade budget per pair (default: {trade_plan.DAILY_TRADES})")
    parser.add_argument("--target-good", type=int, default=6,
                        help="target 98Plus copies per high-value family for Tier 2 priority (default: 6)")
    parser.add_argument("--core-only", action="store_true",
                        help="use only the 45 core non-shadow raid families instead of the expanded ~93 set")
    parser.add_argument("--full-scan", action="store_true",
                        help="count every evolved form in Pass 2 instead of base/baby common forms")
    parser.add_argument("--reuse-counts", action="store_true",
                        help="plan/trade from counts saved within --max-count-age")
    parser.add_argument("--max-count-age", type=float, default=2 * 3600,
                        help="maximum age in seconds for --reuse-counts (default: 7200)")
    parser.add_argument("--ignore-history", action="store_true",
                        help="ignore today's recorded trade count when computing remaining budget")
    parser.add_argument("--plan", action="store_true",
                        help="list attached pair, daily budget, and family settings; touch no phone")
    parser.add_argument("--dry-run", "--trade-plan", dest="trade_plan_only", action="store_true",
                        help="count storage (or reuse counts) and print the 100-trade plan; make no trades")
    parser.add_argument("--shots", type=Path, default=None, help="directory for diagnostic screenshots")
    args = parser.parse_args(arguments)
    if args.trades < 1 or args.trades > trade_plan.DAILY_TRADES:
        parser.error(f"--trades must be between 1 and {trade_plan.DAILY_TRADES}")
    if args.target_good < 1:
        parser.error("--target-good must be at least 1")
    if args.devices and args.pair:
        parser.error("use either --devices or --pair, not both")
    args.expanded = not args.core_only
    args.fill_budget = True
    args.fast_scan = not args.full_scan
    args.shots = (args.shots or config_paths.state_dir() / "trade" / "shots").expanduser()
    return args


async def attached_pair(args: argparse.Namespace) -> list[pokemon_fleet.DeviceSpec]:
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    wanted = list(args.pair or args.devices or [])
    if wanted:
        missing = [name for name in wanted if name not in fleet.devices]
        if missing:
            raise pokemon_fleet.FleetError(f"Unknown configured device(s): {', '.join(missing)}")
    wanted_set = set(wanted)
    specs = [
        spec for spec in fleet.devices.values()
        if spec.enabled and (not wanted_set or spec.name in wanted_set)
    ]
    androids = await screen_router.attached_androids(specs)
    iphones = pokemon_fleet.ios_connected_udids()
    here = [
        spec for spec in specs
        if spec.identifier in (androids if spec.platform == "android" else iphones)
    ]
    return here


async def run(args: argparse.Namespace) -> int:
    here = await attached_pair(args)
    if args.pair and len(here) != 2:
        found = ", ".join(s.name for s in here) or "none"
        raise pokemon_fleet.FleetError(
            f"--pair {' '.join(args.pair)} requires both devices attached to this host (found: {found})"
        )
    if not here:
        print("No fleet phone is attached to this computer", flush=True)
        return 0
    if len(here) != 2:
        names = ", ".join(s.name for s in here)
        msg = f"Skipped: {len(here)} phone(s) attached here ({names}); daily trading needs a pair of 2"
        if args.devices:
            raise pokemon_fleet.FleetError(msg)
        print(msg, flush=True)
        return 0

    pair_names = f"{here[0].name} + {here[1].name}"
    fams = trade_plan.expanded_families() if args.expanded else trade_plan.families()
    from . import trade_history
    pair_names_today = [spec.name for spec in here]
    counts = (None if args.ignore_history
              else await asyncio.to_thread(trade_history.current_day_counts, pair_names_today))
    already = max(counts.values(), default=0) if counts is not None else 0
    remaining = max(0, args.trades - already)
    if counts is not None and trade_history.pair_capped(pair_names_today, counts):
        remaining = 0

    if args.plan:
        cached = starter_trade.saved_counts(here, max_age=args.max_count_age) is not None
        print(
            f"[daily-trade] pair: {pair_names} | families: {len(fams)} "
            f"({'expanded' if args.expanded else 'core'}) | target_good: {args.target_good} | "
            f"scan: {'fast' if args.fast_scan else 'full'}",
            flush=True,
        )
        print(
            f"[daily-trade] budget: {remaining}/{args.trades} remaining today "
            f"({already} recorded) | cached_counts_recent: {cached}",
            flush=True,
        )
        return 0

    print(
        f"[daily-trade] {pair_names}: planning up to {remaining} trade(s) "
        f"({len(fams)} families, target 98Plus={args.target_good})",
        flush=True,
    )
    try:
        report = await starter_trade.trade_step(here, args)
    except (starter_trade.StepFailed, home.HomeRecoveryError, pokemon_fleet.FleetError) as exc:
        print(f"[daily-trade] PROBLEM: {exc}", flush=True)
        return 1
    for line in report.lines:
        print(f"[daily-trade]   {line}", flush=True)
    print(f"[daily-trade] {'done' if report.ok else 'PROBLEM'}: {report.detail}", flush=True)
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    from . import fleet_entrypoint

    argv = fleet_entrypoint.apply_registry_override(sys.argv[1:])
    if not fleet_entrypoint.local_requested(argv) and not any(v in {"--help", "-h"} for v in argv):
        raise SystemExit(
            fleet_entrypoint.run_all_machines("daily_trade.py", fleet_entrypoint.clean_scope_flags(argv))
        )
    if not any(v in {"--help", "-h"} for v in argv):
        fleet_entrypoint.apply_local_environment()
    raise SystemExit(main(fleet_entrypoint.clean_scope_flags(argv)))
