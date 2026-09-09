
from tests import support as _test_support
#!/usr/bin/env python3
import unittest
from pathlib import Path

from sources import gbl_evaluator


class GBLEvaluatorTests(unittest.TestCase):
    def test_get_league_analysis(self) -> None:
        analysis = gbl_evaluator.get_league_analysis("great")
        self.assertEqual(analysis["league"], "Great League (CP <= 1500)")
        self.assertEqual(analysis["difficulty"], "EASIEST")
        self.assertEqual(analysis["cp_cap"], 1500)
        self.assertGreater(analysis["auto_battle_friendliness"], 90.0)

    def test_lookup_meta_pokemon(self) -> None:
        swampert = gbl_evaluator.lookup_meta_pokemon("Swampert")
        self.assertIsNotNone(swampert)
        assert swampert is not None
        self.assertIn("Water", swampert.types)
        self.assertIn("Ground", swampert.types)
        self.assertEqual(swampert.fast_move, "Mud Shot")

    def test_evaluate_pokemon_candidate(self) -> None:
        candidate = gbl_evaluator.evaluate_pokemon_candidate("Skarmory", 1495)
        self.assertEqual(candidate.name, "Skarmory")
        self.assertEqual(candidate.cp, 1495)
        self.assertGreater(candidate.pvp_score, 90.0)
        self.assertGreater(candidate.auto_battle_score, 90.0)

    def test_calculate_type_weaknesses(self) -> None:
        weaknesses = gbl_evaluator.calculate_type_weaknesses(["Water", "Ground"])
        # Swampert is 4x weak to Grass (1.6 * 1.6 = 2.56)
        self.assertGreater(weaknesses["Grass"], 2.0)
        # Swampert (Water/Ground) takes 0.625 from Electric (1.6 * 0.390625 = 0.625)
        self.assertLess(weaknesses["Electric"], 0.8)

    def test_recommend_best_team(self) -> None:
        candidates = [
            ("Swampert", 1498),
            ("Skarmory", 1495),
            ("Clodsire", 1492),
            ("Lanturn", 1480),
        ]
        rec = gbl_evaluator.recommend_best_team(candidates)
        self.assertIsNotNone(rec.lead)
        self.assertIsNotNone(rec.safe_switch)
        self.assertIsNotNone(rec.closer)
        self.assertGreater(rec.total_synergy_score, 90.0)
        self.assertIn("Team synergy score", rec.summary)


if __name__ == "__main__":
    unittest.main()
