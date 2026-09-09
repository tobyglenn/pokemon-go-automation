from __future__ import annotations

from tests import support as _test_support

import unittest
import unittest.mock

from sources import gbl_meta, gbl_vision


GAME = {
    "pokemon": [
        {"speciesId": "lanturn", "speciesName": "Lanturn", "types": ["water", "electric"]},
        {"speciesId": "whiscash", "speciesName": "Whiscash", "types": ["water", "ground"]},
        {"speciesId": "skarmory", "speciesName": "Skarmory", "types": ["steel", "flying"]},
        {"speciesId": "charizard", "speciesName": "Charizard", "types": ["fire", "flying"]},
    ]
}
RANKS = [
    {"speciesId": "lanturn", "speciesName": "Lanturn", "score": 96, "stats": {"product": 2100}, "moveset": ["SPARK", "SURF", "THUNDERBOLT"]},
    {"speciesId": "whiscash", "speciesName": "Whiscash", "score": 95, "stats": {"product": 2050}, "moveset": ["MUD_SHOT", "MUD_BOMB", "BLIZZARD"]},
    {"speciesId": "skarmory", "speciesName": "Skarmory", "score": 94, "stats": {"product": 2150}, "moveset": ["STEEL_WING", "SKY_ATTACK", "BRAVE_BIRD"]},
    {"speciesId": "charizard", "speciesName": "Charizard", "score": 85, "stats": {"product": 1700}, "moveset": ["WING_ATTACK", "DRAGON_CLAW", "BLAST_BURN"]},
]


class MetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = gbl_meta.build_meta_index(GAME, RANKS)

    def test_builds_types_moves_and_scores(self) -> None:
        lanturn = self.index["lanturn"]
        self.assertEqual(lanturn.types, ("Water", "Electric"))
        self.assertEqual(lanturn.fast_move, "Spark")
        self.assertEqual(lanturn.charged_moves, ("Surf", "Thunderbolt"))

    def test_roster_page_pairs_columns_and_rows(self) -> None:
        boxes = [
            gbl_vision.OCRBox("CP 1497", 1, 100, 100, 120, 30),
            gbl_vision.OCRBox("CP 1494", 1, 400, 100, 120, 30),
            gbl_vision.OCRBox("Lanturn", 1, 90, 310, 150, 30),
            gbl_vision.OCRBox("Whiscash", 1, 390, 310, 160, 30),
        ]
        roster = gbl_meta.parse_roster_page(boxes, self.index)
        self.assertEqual([(item.name, item.cp) for item in roster], [("Lanturn", 1497), ("Whiscash", 1494)])

    def test_recommendation_uses_rank_and_coverage(self) -> None:
        roster = [
            gbl_meta.RosterPokemon("Lanturn", 1497),
            gbl_meta.RosterPokemon("Whiscash", 1494),
            gbl_meta.RosterPokemon("Skarmory", 1492),
            gbl_meta.RosterPokemon("Charizard", 1490),
        ]
        result = gbl_meta.recommend_team(roster, self.index)
        names = {item.name for item in result.team.members}
        self.assertIn("Lanturn", names)
        self.assertIn("Skarmory", names)
        self.assertEqual(len(names), 3)

    def test_recommendation_honors_always_include(self) -> None:
        roster = [
            gbl_meta.RosterPokemon("Lanturn", 1497),
            gbl_meta.RosterPokemon("Whiscash", 1494),
            gbl_meta.RosterPokemon("Skarmory", 1492),
            gbl_meta.RosterPokemon("Charizard", 1490),
        ]
        result = gbl_meta.recommend_team(
            roster, self.index, always_include=("Charizard",)
        )
        names = {item.name for item in result.team.members}
        self.assertIn("Charizard", names)
        self.assertEqual(len(names), 3)

    def test_under_levelled_copy_is_scaled_down(self) -> None:
        meta = self.index["lanturn"]
        ready = meta.team_member(1495)
        low = meta.team_member(1200)
        self.assertLess(low.rating, ready.rating)
        self.assertLess(low.bulk, ready.bulk)


class MoveTypeTests(unittest.TestCase):
    """The move->type index gbl_strategy.threat_types reads."""

    def test_move_ids_and_display_names_share_a_key(self) -> None:
        moves = {
            "moves": [
                {"moveId": "DRAGON_TAIL", "name": "Dragon Tail", "type": "dragon"},
                {"moveId": "AEROBLAST", "name": "Aeroblast", "type": "flying"},
                {"moveId": "STRUGGLE", "name": "Struggle", "type": "none"},
            ]
        }
        with unittest.mock.patch.object(gbl_meta, "_cache_json", return_value=moves):
            gbl_meta.load_move_types.cache_clear()
            try:
                types = gbl_meta.load_move_types()
            finally:
                gbl_meta.load_move_types.cache_clear()
        self.assertEqual(types["dragon tail"], "Dragon")
        self.assertEqual(types["aeroblast"], "Flying")
        # A typeless move contributes nothing rather than a bogus threat.
        self.assertNotIn("struggle", types)


if __name__ == "__main__":
    unittest.main()
