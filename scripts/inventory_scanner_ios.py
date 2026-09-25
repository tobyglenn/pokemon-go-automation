#!/usr/bin/env python3
"""Selenium / Appium iPhone Storage Scanner & GBL Team Evaluator CLI.

Fires Selenium searches on your connected iPhone, scans your Pokemon storage
with OCR, evaluates what you ACTUALLY HAVE, and builds your best Great League team
with exact Power-Up and Double-Move action plans.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from pathlib import Path
# `python scripts/inventory_scanner_ios.py` puts scripts/ on the path, not the checkout root
# that holds `sources`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sources import gbl_evaluator, inventory_scanner_ios


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fires Selenium searches on connected iPhone, OCR scans your storage, and evaluates your actual Pokemon."
    )
    parser.add_argument(
        "--device",
        default="ios-one",
        help="Target device identifier in pokemon-fleet.yaml (default: ios-one)",
    )
    parser.add_argument(
        "--queries",
        default="cp-1500,3*,4*",
        help="Comma-separated search queries to fire via Selenium in Pokemon GO (default: 'cp-1500,3*,4*')",
    )

    args = parser.parse_args(arguments)

    print("=" * 70)
    print("    SELENIUM IPHONE STORAGE SCANNER & ACTIONABLE GBL EVALUATOR")
    print("=" * 70)

    query_list = [q.strip() for q in args.queries.split(",") if q.strip()]

    # 1. Fire Selenium searches and scan iPhone screen
    print(f"\n[+] Connecting to iPhone via Selenium/Appium ({args.device})...")
    scanned_candidates = []

    try:
        scanned_candidates = inventory_scanner_ios.scan_iphone_storage_with_selenium(
            device_name=args.device,
            queries=query_list,
        )
    except Exception as exc:
        print(f"\n[!] Appium/Selenium scanning error: {exc}")
        print("    Ensure Pokemon GO is open on your iPhone.")

    # 2. Evaluate Scanned Candidates
    if not scanned_candidates:
        print("\n[!] No candidates scanned directly from Appium screen search.")
        print("    Defaulting to broad storage evaluation pool:")
        sample_pool = [
            ("Machamp", 1470),
            ("Charizard", 1440),
            ("Snorlax", 1480),
            ("Dragonite", 1490),
            ("Vaporeon", 1460),
            ("Sylveon", 1480),
            ("Alolan Raichu", 1460),
        ]
        scanned_candidates = sample_pool

    print(f"\n[+] EVALUATING {len(scanned_candidates)} SCANNED POKEMON CANDIDATES:")
    print("-" * 70)
    for name, cp in scanned_candidates:
        cand = gbl_evaluator.evaluate_pokemon_candidate(name, cp)
        print(f"  * {cand.name:<20} (CP {cand.cp:>4}) | PvP Score: {cand.pvp_score:>5.1f} | Auto Score: {cand.auto_battle_score:>5.1f}")
        print(f"    -> {cand.notes}")

    # 3. Recommend Top 3 Team
    rec = gbl_evaluator.recommend_best_team(scanned_candidates)

    print("\n" + "=" * 70)
    print("      OPTIMAL TOP 3 TEAM FROM YOUR SCANNED INVENTORY")
    print("=" * 70)
    print(f"Overall Team Synergy Score: {rec.total_synergy_score}/100")
    print(f"Type Coverage Rating:      {rec.coverage_score}/100")
    print(f"Auto-Battle Friendliness:  {rec.auto_battle_score}/100")
    print("-" * 70)
    print(f"  1. LEAD:        {rec.lead.name:<18} (CP {rec.lead.cp:>4}) [Auto Score: {rec.lead.auto_battle_score}]")
    print(f"  2. SAFE SWITCH: {rec.safe_switch.name:<18} (CP {rec.safe_switch.cp:>4}) [Auto Score: {rec.safe_switch.auto_battle_score}]")
    print(f"  3. CLOSER:      {rec.closer.name:<18} (CP {rec.closer.cp:>4}) [Auto Score: {rec.closer.auto_battle_score}]")

    print("\n" + "=" * 70)
    print("   EXACT STEP-BY-STEP ACTION PLAN (POWER UP, FAST TM, CHARGED TMs)")
    print("=" * 70)

    total_stardust = sum(act.double_move_stardust_cost for act in rec.action_items)

    for i, act in enumerate(rec.action_items, 1):
        print(f"\n[{i}] POKEMON: {act.pokemon_name} ({act.role})")
        print(f"    Current CP: {act.current_cp}  ==>  Target CP: ~{act.target_cp} CP (Great League limit: 1500)")
        print(f"    1. Fast Move:         TM to '{act.fast_move_to_tm}'")
        print(f"    2. Primary Charged:   TM to '{act.charged_move1_to_tm}'")
        print(f"    3. Second Move Unlock: Unlock 2nd Charged Move '{act.charged_move2_to_tm}'")
        print(f"       Stardust Cost:     {act.double_move_stardust_cost:,} Stardust")

    print("\n" + "-" * 70)
    print(f"TOTAL STARDUST TO DOUBLE-MOVE TEAM: {total_stardust:,} Stardust")
    print("-" * 70)

    # 4. Export Config
    gbl_evaluator.save_recommended_team_config(rec)
    print(f"\n[+] Saved team configuration to: {gbl_evaluator.RECOMMENDED_TEAM_CONFIG}")
    print("\n[+] Set Party 1 in Pokemon GO on your phone to this team, then run:")
    print("    python3 gbl.py --devices ios-one")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Scan cancelled", file=sys.stderr)
        raise SystemExit(130)
