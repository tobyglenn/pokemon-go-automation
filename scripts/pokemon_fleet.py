"""Compatibility alias for fleet.py."""
import sys
from pathlib import Path
# `python scripts/pokemon_fleet.py` puts scripts/ on the path, not the checkout root
# that holds `sources`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources.pokemon_fleet import *  # legacy imports used by worker modules
from scripts.fleet import main
if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(main())
    except (FleetError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
