#!/usr/bin/env python3
"""Remove friends with no gift, no halo and few enough hearts from an Android friends list."""

from __future__ import annotations

import sys

from sources import fleet_entrypoint, remove_friends_android


def main(arguments: list[str] | None = None) -> int:
    argv = fleet_entrypoint.apply_registry_override(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    helping = any(value in {"--help", "-h"} for value in argv)
    # Worked in front of a phone whose friends list is already open and sorted,
    # so this stays on one computer unless the operator asks for the fleet.
    # `--local` is also how run_all_machines marks its own workers, which is
    # what stops a delegated run delegating again.
    fleet_wide = fleet_entrypoint.has_option(argv, fleet_entrypoint.ALL_MACHINES_FLAG)
    forwarded = fleet_entrypoint.clean_scope_flags(argv)
    if fleet_wide and not fleet_entrypoint.local_requested(argv) and not helping:
        return fleet_entrypoint.run_all_machines("remove_friends.py", forwarded)
    if not helping:
        # adb lives in a directory the machine record names, not on PATH.
        fleet_entrypoint.apply_local_environment()
    return remove_friends_android.main(forwarded)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Friend removal run cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (
        remove_friends_android.FriendRemovalError,
        remove_friends_android.excellent_throw_android.AndroidExcellentThrowError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
