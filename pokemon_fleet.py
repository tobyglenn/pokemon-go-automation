"""Compatibility alias for fleet.py."""
from sources.pokemon_fleet import *  # legacy imports used by worker modules
from fleet import main
if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(main())
    except (FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
