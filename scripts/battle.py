#!/usr/bin/env python3
"""Public battle command across configured hosts and connected devices."""
import sys
from pathlib import Path
# `python scripts/battle.py` puts scripts/ on the path, not the checkout root
# that holds `sources`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import fleet_entrypoint, pokemon_fleet

def main(arguments=None):
    return fleet_entrypoint.run_operation('battle', 'scripts/battle.py', arguments)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Automation cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
