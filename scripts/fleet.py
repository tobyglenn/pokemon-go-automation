#!/usr/bin/env python3
"""Fleet readiness and operation dispatcher."""
import sys
from pathlib import Path
# `python scripts/fleet.py` puts scripts/ on the path, not the checkout root
# that holds `sources`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import fleet_entrypoint, pokemon_fleet

def main(arguments=None):
    argv = fleet_entrypoint.apply_registry_override(list(sys.argv[1:] if arguments is None else arguments))
    local = fleet_entrypoint.local_requested(argv)
    argv = fleet_entrypoint.clean_scope_flags(argv)
    if not argv:
        argv = ["--help"]
    if not local and not fleet_entrypoint.coordinator_only(argv) and not any(value in {"--help", "-h"} for value in argv):
        return fleet_entrypoint.run_all_machines("scripts/fleet.py", argv)
    if not any(value in {"--help", "-h"} for value in argv):
        fleet_entrypoint.apply_local_environment()
    sys.argv = ["scripts/fleet.py", *argv]
    return pokemon_fleet.main()

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
