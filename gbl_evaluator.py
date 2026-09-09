#!/usr/bin/env python3
"""GBL League Evaluator & Actionable Team Builder CLI tool.

Evaluates ANY Pokemon in your inventory, picks your best 3-Pokemon team for Great League,
and outputs exact step-by-step instructions (Power-up CP, Fast TM, Charged TMs, Double Move costs).
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from sources import gbl_evaluator


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate Pokemon inventory and suggest exact Power-Up and Double-Move action plan for Great League auto-battling."
    )
    parser.add_argument(
        "--league",
        default="great",
        help="Target league: great (default), little, ultra, master",
    )
    parser.add_argument(
        "--device",
        default="ios-one",
        help="Target device identifier in pokemon-fleet.yaml (default: ios-one)",
    )
    parser.add_argument(
        "--candidates",
        "--my-pokemon",
        help="Comma-separated list of Pokemon names and optional CPs in your phone storage (e.g. 'Machamp:1480,Charizard:1450,Snorlax:1490,Dragonite:1495')",
    )

    args = parser.parse_args(arguments)

    print("=" * 70)
    print("        GBL INVENTORY EVALUATOR & ACTIONABLE TEAM BUILDER")
    print("=" * 70)

    # 1. Parse candidates
    candidate_tuples: list[tuple[str, int]] = []

    if args.candidates:
        for item in args.candidates.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                parts = item.split(":", 1)
                name = parts[0].strip()
                try:
                    cp = int(parts[1].strip())
                except ValueError:
                    cp = 1450
            else:
                name = item
                cp = 1450
            candidate_tuples.append((name, cp))

    # If no candidates passed via CLI, prompt interactively or offer common pool
    if not candidate_tuples:
        print("\n[!] Please list the top Pokemon you currently see on your phone.")
        print("    Example format: Machamp:1480, Charizard:1450, Snorlax:1490, Vaporeon:1470")
        try:
            user_input = input("\nEnter your Pokemon (or press Enter for sample inventory): ").strip()
            if user_input:
                for item in user_input.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    if ":" in item:
                        parts = item.split(":", 1)
                        name = parts[0].strip()
                        try:
                            cp = int(parts[1].strip())
                        except ValueError:
                            cp = 1450
                    else:
                        name = item
                        cp = 1450
                    candidate_tuples.append((name, cp))
        except (EOFError, KeyboardInterrupt):
            pass

    if not candidate_tuples:
        print("\n[+] Evaluating accessible common inventory candidates:")
        sample_pool = [
            ("Machamp", 1480),
            ("Charizard", 1450),
            ("Snorlax", 1490),
            ("Dragonite", 1495),
            ("Vaporeon", 1470),
            ("Gengar", 1420),
            ("Sylveon", 1480),
            ("Alolan Raichu", 1460),
        ]
        candidate_tuples = sample_pool

    # 2. Evaluate Candidate Pool
    print(f"\n[+] EVALUATING YOUR {len(candidate_tuples)} POKEMON CANDIDATES:")
    print("-" * 70)
    for name, cp in candidate_tuples:
        cand = gbl_evaluator.evaluate_pokemon_candidate(name, cp)
        print(f"  * {cand.name:<20} (CP {cand.cp:>4}) | PvP Score: {cand.pvp_score:>5.1f} | Auto Score: {cand.auto_battle_score:>5.1f}")
        print(f"    -> {cand.notes}")

    # 3. Recommend Top 3 Team
    rec = gbl_evaluator.recommend_best_team(candidate_tuples)

    print("\n" + "=" * 70)
    print("      OPTIMAL TOP 3 TEAM FROM YOUR INVENTORY")
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

    # 4. Save Config
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
        print("Evaluation cancelled", file=sys.stderr)
        raise SystemExit(130)
