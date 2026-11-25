#!/usr/bin/env python3
"""Start the day on every phone: buddy, battle, special trade, Mega Evolutions, bag, gifts, GBL."""

from __future__ import annotations

import sys

from sources import fleet_entrypoint, pokemon_fleet, starter


def _run_phase(forwarded: list[str], phase_steps: list[str], *, no_sleep: bool, label: str) -> int:
    phase_args = starter.phase_arguments(forwarded, phase_steps, no_sleep=no_sleep)
    try:
        return fleet_entrypoint.run_all_machines("starter.py", phase_args)
    except pokemon_fleet.FleetError as exc:
        if not str(exc).startswith("Host command failed:"):
            raise
        print(f"[starter] {label} phase had failures ({exc}); continuing", flush=True)
        return 1


def _run_all_machines(forwarded: list[str]) -> int:
    args = starter.parse_args(forwarded)
    machines = fleet_entrypoint.machine_configs()
    if args.plan:
        rc = fleet_entrypoint.run_all_machines("starter.py", forwarded)
        if "special" in args.steps and len(machines) > 1:
            fleet_entrypoint.apply_local_environment()
            receiver = starter.special_receiver()
            if receiver:
                include_special = True
                if args.devices:
                    from sources import trading_accounts
                    aliases = trading_accounts.aliases()
                    selected = {aliases.get(name, name) for name in args.devices}
                    include_special = receiver in selected and len(selected) > 1
                if include_special:
                    report = starter.run_special_coordinator(args)
                    rc = rc or int(not report.ok)
        return rc
    if "special" not in args.steps or args.dry_run or len(machines) <= 1:
        return fleet_entrypoint.run_all_machines("starter.py", forwarded)

    before, after = starter.split_around_special(args.steps)
    fleet_entrypoint.apply_local_environment()
    should_special, skip_reason = starter.special_selected(args)
    if not should_special:
        if skip_reason:
            print(f"[special] skipped: {skip_reason}", flush=True)
        remaining = [*before, *after]
        if not remaining:
            return 0
        return fleet_entrypoint.run_all_machines(
            "starter.py",
            starter.phase_arguments(forwarded, remaining, no_sleep=args.no_sleep),
        )

    failed = 0
    if before:
        failed |= _run_phase(forwarded, before, no_sleep=True, label="pre-special")
    report = starter.run_special_coordinator(args)
    failed |= int(not report.ok)
    if after:
        failed |= _run_phase(forwarded, after, no_sleep=args.no_sleep, label="post-special")
    return 1 if failed else 0


def main(arguments: list[str] | None = None) -> int:
    argv = fleet_entrypoint.apply_registry_override(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    helping = any(value in {"--help", "-h"} for value in argv)
    local = fleet_entrypoint.local_requested(argv)
    forwarded = fleet_entrypoint.clean_scope_flags(argv)
    if not local and not helping:
        return _run_all_machines(forwarded)
    if not helping:
        # adb lives in a directory the machine record names, not on PATH.
        fleet_entrypoint.apply_local_environment()
    return starter.main(forwarded, local_only=local)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
