#!/usr/bin/env python3
"""Public berries command across configured hosts and connected devices."""
import sys
from sources import fleet_entrypoint, pokemon_fleet

def main(arguments=None):
    return fleet_entrypoint.run_operation('berries', 'feed_berries.py', arguments)

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Automation cancelled", file=sys.stderr)
        raise SystemExit(130)
    except (pokemon_fleet.FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
