#!/usr/bin/env python3
"""Catch the award queue or a research screen's rewards, on Android and iPhone."""

from __future__ import annotations

import sys

from sources import catch_rewards, fleet_entrypoint


def main(arguments: list[str] | None = None) -> int:
    argv = fleet_entrypoint.apply_registry_override(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    helping = any(value in {"--help", "-h"} for value in argv)
    # Worked in front of a phone already parked on the award card or a research
    # screen, so this stays on this computer unless the operator asks for the
    # fleet.  `--local` is also how run_all_machines marks its own workers,
    # which is what stops a delegated run delegating again.
    fleet_wide = fleet_entrypoint.has_option(argv, fleet_entrypoint.ALL_MACHINES_FLAG)
    forwarded = fleet_entrypoint.clean_scope_flags(argv)
    if fleet_wide and not fleet_entrypoint.local_requested(argv) and not helping:
        return fleet_entrypoint.run_all_machines("catch.py", forwarded)
    if not helping:
        # adb lives in a directory the machine record names, not on PATH.
        fleet_entrypoint.apply_local_environment()
    return catch_rewards.main(forwarded)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Catch run cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (
        catch_rewards.CatchError,
        catch_rewards.excellent_throw_android.AndroidExcellentThrowError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
