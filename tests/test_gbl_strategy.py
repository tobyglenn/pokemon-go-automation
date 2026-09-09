from __future__ import annotations

from tests import support as _test_support

from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sources import gbl_strategy


def member(name: str, cp: int = 1495) -> gbl_strategy.TeamMember:
    meta = gbl_strategy.gbl_evaluator.lookup_meta_pokemon(name)
    return gbl_strategy.TeamMember(
        meta.name,
        cp,
        tuple(meta.types),
        meta.fast_move,
        tuple(meta.charged_moves),
        meta.bulk_rating,
        meta.auto_battle_rating,
    )


class MatchupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.team = gbl_strategy.BattleTeam(
            (member("Noctowl"), member("Lanturn"), member("Whiscash"))
        )

    def test_super_effective_reserve_is_selected(self) -> None:
        memory = gbl_strategy.BattleMemory(active_index=0)
        decision = gbl_strategy.choose_switch(
            self.team,
            memory,
            "Dewgong",
            gbl_strategy.StrategySettings(minimum_score_gain=5),
            now=100,
        )
        self.assertTrue(decision.should_switch)
        self.assertIn(decision.target_index, (1, 2))
        self.assertIn("resists/neutralizes", decision.reason)

    def test_shadow_form_uses_full_species_typing_for_forced_switch(self) -> None:
        team = gbl_strategy.BattleTeam((
            gbl_strategy.TeamMember(
                "Bronzong", 1380, ("Steel", "Psychic"), "Confusion",
                ("Psyshock",), 86, 79),
            gbl_strategy.TeamMember(
                "Slowbro", 1365, ("Water", "Psychic"), "Water Gun",
                ("Surf",), 91, 80),
            gbl_strategy.TeamMember(
                "Togetic", 1451, ("Fairy", "Flying"), "Fairy Wind",
                ("Aerial Ace",), 91, 82),
        ))
        rapidash = SimpleNamespace(name="Rapidash (Shadow)", types=("Fire",))
        with patch(
                "sources.gbl_meta.load_meta_index",
                return_value={"rapidash shadow": rapidash}):
            decision = gbl_strategy.choose_switch(
                team,
                gbl_strategy.BattleMemory(active_index=0),
                "Rapidash (Shadow)",
                gbl_strategy.StrategySettings(),
                forced=True,
            )
        self.assertEqual(decision.target_index, 1)
        self.assertIn("Slowbro", decision.reason)

    def test_neutral_damage_active_does_not_switch(self) -> None:
        # Whiscash (Ground/Water) taking neutral incoming from Whiscash/Noctowl
        memory = gbl_strategy.BattleMemory(active_index=2) # Whiscash active
        decision = gbl_strategy.choose_switch(
            self.team,
            memory,
            "Whiscash",
            gbl_strategy.StrategySettings(minimum_score_gain=1),
            now=100,
        )
        self.assertFalse(decision.should_switch)
        self.assertIn("non-supereffective damage", decision.reason)

    def test_active_taking_supereffective_switches_to_safe_reserve(self) -> None:
        # Noctowl (active) vs Dewgong (Ice hits for 1.6x super-effective)
        memory = gbl_strategy.BattleMemory(active_index=0)
        decision = gbl_strategy.choose_switch(
            self.team,
            memory,
            "Dewgong",
            gbl_strategy.StrategySettings(minimum_score_gain=5),
            now=100,
        )
        self.assertTrue(decision.should_switch)
        self.assertIn(decision.target_index, (1, 2))
        self.assertIn("resists/neutralizes", decision.reason)

    def test_forced_switch_marks_fainted_and_maps_sheet_slot(self) -> None:
        memory = gbl_strategy.BattleMemory(active_index=0)
        memory.begin_forced_switch()
        decision = gbl_strategy.choose_switch(
            self.team,
            memory,
            "Skarmory",
            gbl_strategy.StrategySettings(),
            forced=True,
        )
        self.assertFalse(memory.alive[0])
        self.assertIn(decision.target_index, (1, 2))
        self.assertEqual(gbl_strategy.reserve_sheet_slot(memory, decision.target_index), decision.target_index - 1)

    def test_cooldown_blocks_voluntary_switch(self) -> None:
        memory = gbl_strategy.BattleMemory(active_index=0, last_switch_at=90)
        decision = gbl_strategy.choose_switch(
            self.team,
            memory,
            "Skarmory",
            gbl_strategy.StrategySettings(switch_cooldown_seconds=60),
            now=100,
        )
        self.assertFalse(decision.should_switch)
        self.assertIn("cooldown", decision.reason)


class OCRNameTests(unittest.TestCase):
    def test_exact_name_wins_over_shorter_substring(self) -> None:
        name = gbl_strategy.identify_pokemon(["Opponent sent out Alolan Ninetales!"])
        self.assertEqual(name, "Alolan Ninetales")

    def test_noisy_name_can_be_recovered(self) -> None:
        name = gbl_strategy.identify_pokemon(["SKARM0RY"], ["Skarmory", "Lanturn"])
        self.assertEqual(name, "Skarmory")

    def test_party_names_and_cp_are_read_left_to_right(self) -> None:
        from sources import gbl_vision

        boxes = [
            gbl_vision.OCRBox("CP 1399", 1, 100, 100, 120, 30),
            gbl_vision.OCRBox("CP 1418", 1, 400, 100, 120, 30),
            gbl_vision.OCRBox("cP 1466", 1, 700, 100, 120, 30),
            gbl_vision.OCRBox("Minun", 1, 100, 300, 120, 30),
            gbl_vision.OCRBox("Quagsire", 1, 400, 300, 120, 30),
            gbl_vision.OCRBox("Poliwrath", 1, 700, 300, 120, 30),
        ]
        team = gbl_strategy.team_from_party_ocr(boxes)
        self.assertIsNotNone(team)
        self.assertEqual([item.name for item in team.members], ["Minun", "Quagsire", "Poliwrath"])  # type: ignore[union-attr]
        self.assertEqual([item.cp for item in team.members], [1399, 1418, 1466])  # type: ignore[union-attr]


@dataclass
class Box:
    text: str
    center_x: int
    center_y: int


class LeagueTests(unittest.TestCase):
    def test_great_league_beats_ultra_and_master(self) -> None:
        choice = gbl_strategy.choose_easiest_league(
            [
                Box("Ultra League", 100, 300),
                Box("Great League", 100, 700),
                Box("Master League", 100, 1100),
            ]
        )
        self.assertIsNotNone(choice)
        self.assertEqual(choice.name, "Great League")  # type: ignore[union-attr]
        self.assertEqual((choice.x, choice.y), (100, 700))  # type: ignore[union-attr]

    def test_explicit_preference_can_override_default(self) -> None:
        choice = gbl_strategy.choose_easiest_league(
            [Box("Ultra League", 100, 300), Box("Great League", 100, 700)],
            "Ultra League",
        )
        self.assertEqual(choice.name, "Ultra League")  # type: ignore[union-attr]

    def test_ineligible_weather_cup_loses_to_open_1500_cup(self) -> None:
        team = gbl_strategy.BattleTeam(
            (member("Noctowl"), member("Machamp"), member("Excadrill"))
        )
        choice = gbl_strategy.choose_easiest_league(
            [Box("Weather Cup: Great", 100, 300), Box("Competitors Cup", 100, 700)],
            team=team,
        )
        self.assertEqual(choice.name, "Competitors Cup")  # type: ignore[union-attr]


class LeagueMatchTests(unittest.TestCase):
    """The rotation prints editions of a league; a config names the league."""

    def test_exact_name_matches(self) -> None:
        self.assertTrue(
            gbl_strategy.league_matches("Master League", "MASTER LEAGUE")
        )

    def test_edition_of_the_wanted_league_matches(self) -> None:
        self.assertTrue(
            gbl_strategy.league_matches(
                "Master League", "master league mega edition"
            )
        )

    def test_another_league_does_not_match(self) -> None:
        self.assertFalse(
            gbl_strategy.league_matches("Master League", "Great League")
        )

    def test_matching_is_on_whole_words(self) -> None:
        self.assertFalse(
            gbl_strategy.league_matches("Master League", "Grandmaster League")
        )

    def test_missing_name_does_not_match(self) -> None:
        self.assertFalse(gbl_strategy.league_matches("Master League", None))
        self.assertFalse(gbl_strategy.league_matches("", "Master League"))

    def test_mega_edition_is_taken_when_plain_master_is_absent(self) -> None:
        # 2026-09-05: the list held no plain Master League, so this card is
        # the only one that plays the configured league.
        choice = gbl_strategy.choose_easiest_league(
            [
                Box("Great League", 100, 300),
                Box("Master League: Mega", 100, 700),
            ],
            "Master League",
        )
        self.assertEqual(choice.name, "Master League: Mega")  # type: ignore[union-attr]


class VisionActionTests(unittest.TestCase):
    def test_lower_battle_label_beats_top_tab_and_taps_left_of_close(self) -> None:
        from sources import gbl_vision

        boxes = [
            gbl_vision.OCRBox("BATTLE", 1, 200, 100, 200, 50),
            gbl_vision.OCRBox("BATTLE", 1, 500, 2200, 200, 50),
        ]
        point, label = gbl_vision.action_point(boxes, 1320, 2868)  # type: ignore[misc]
        self.assertEqual(label, "BATTLE")
        self.assertEqual(point, [501, 2225])


class ConfigTests(unittest.TestCase):
    def test_per_device_team_override(self) -> None:
        config = """
preferred_league: auto
team_building: {enabled: true, select_team: true, roster_scan_pages: 8}
team:
  lead: {name: Noctowl, cp: 1490}
  safe_switch: {name: Lanturn, cp: 1495}
  closer: {name: Whiscash, cp: 1498}
devices:
  tall_device:
    team_building: {enabled: false, select_team: false}
    team:
      lead: {name: Skarmory, cp: 1490}
      safe_switch: {name: Swampert, cp: 1495}
      closer: {name: Clodsire, cp: 1498}
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.yaml"
            path.write_text(config)
            profile = gbl_strategy.load_strategy_profile(path, "tall_device")
        self.assertEqual(profile.team.members[0].name, "Skarmory")
        self.assertEqual(profile.team.members[2].name, "Clodsire")
        self.assertFalse(profile.settings.auto_build_team)
        self.assertFalse(profile.settings.auto_select_team)


class FailedSwitchTests(unittest.TestCase):
    """A reserve that never arrives must stop being asked for."""

    def setUp(self) -> None:
        self.team = gbl_strategy.BattleTeam(
            (member("Skarmory"), member("Swampert"), member("Clodsire"))
        )
        self.settings = gbl_strategy.StrategySettings(
            enabled=True, minimum_score_gain=0.0, switch_cooldown_seconds=0.0
        )

    def test_blocks_only_after_the_configured_attempts(self) -> None:
        memory = gbl_strategy.BattleMemory()
        for _ in range(gbl_strategy.SWITCH_VERIFY_ATTEMPTS - 1):
            memory.record_failed_switch(1)
            self.assertFalse(memory.switch_blocked(1))
        memory.record_failed_switch(1)
        self.assertTrue(memory.switch_blocked(1))

    def test_a_committed_switch_forgives_earlier_failures(self) -> None:
        memory = gbl_strategy.BattleMemory()
        for _ in range(gbl_strategy.SWITCH_VERIFY_ATTEMPTS):
            memory.record_failed_switch(1)
        memory.request_switch(1)
        memory.commit_switch()
        self.assertFalse(memory.switch_blocked(1))

    def test_choose_switch_skips_a_blocked_reserve(self) -> None:
        memory = gbl_strategy.BattleMemory()
        for _ in range(gbl_strategy.SWITCH_VERIFY_ATTEMPTS):
            memory.record_failed_switch(1)
        decision = gbl_strategy.choose_switch(
            self.team, memory, "Charizard", self.settings
        )
        self.assertNotEqual(decision.target_index, 1)

    def test_a_forced_switch_still_uses_a_blocked_reserve(self) -> None:
        # The active Pokemon fainted; naming nobody leaves the sheet up.
        memory = gbl_strategy.BattleMemory(alive=[False, True, False])
        memory.active_index = 0
        for _ in range(gbl_strategy.SWITCH_VERIFY_ATTEMPTS):
            memory.record_failed_switch(1)
        decision = gbl_strategy.choose_switch(
            self.team, memory, "Charizard", self.settings, forced=True
        )
        self.assertEqual(decision.target_index, 1)

    def test_attacks_when_every_reserve_has_refused(self) -> None:
        memory = gbl_strategy.BattleMemory()
        for index in (1, 2):
            for _ in range(gbl_strategy.SWITCH_VERIFY_ATTEMPTS):
                memory.record_failed_switch(index)
        decision = gbl_strategy.choose_switch(
            self.team, memory, "Charizard", self.settings
        )
        self.assertIsNone(decision.target_index)


class ThreatTypeTests(unittest.TestCase):
    """A Pokemon's typing is not the list of things it hits you with."""

    def setUp(self) -> None:
        self.lugia = gbl_strategy.gbl_evaluator.MetaPokemon(
            name="Lugia",
            types=("Psychic", "Flying"),
            pvp_rank_score=90.0,
            fast_move="Dragon Tail",
            charged_moves=("Sky Attack", "Aeroblast"),
            bulk_rating=90.0,
            auto_battle_rating=90.0,
            primary_role="Flex",
            double_move_cost=50000,
        )
        self.kyurem = gbl_strategy.TeamMember(
            "Kyurem", 2495, ("Dragon", "Ice"), "Dragon Breath",
            ("Glaciate", "Draco Meteor"), 88.0, 82.0,
        )

    def test_lead_move_type_is_added_to_the_typing(self) -> None:
        with patch(
            "sources.gbl_meta.load_move_types",
            return_value={"dragon tail": "Dragon", "sky attack": "Flying"},
        ):
            self.assertEqual(
                gbl_strategy.threat_types(self.lugia),
                ("Psychic", "Flying", "Dragon"),
            )

    def test_off_type_lead_move_removes_the_free_switch(self) -> None:
        """The bug this exists for: a run swapped into 2x damage.

        Lugia is Psychic/Flying, which is 1.0x into Dragon/Ice, so the incoming
        term scored the switch as free and the log read "Kyurem improves matchup
        by 22.7". Dragon Tail is what actually landed.
        """

        with patch(
            "sources.gbl_meta.load_move_types", return_value={"dragon tail": "Dragon"}
        ):
            scored = gbl_strategy.matchup_score(self.kyurem, self.lugia)
        with patch("sources.gbl_meta.load_move_types", return_value={}):
            typing_only = gbl_strategy.matchup_score(self.kyurem, self.lugia)
        self.assertLess(scored, typing_only)

    def test_missing_game_master_falls_back_to_bare_typing(self) -> None:
        with patch("sources.gbl_meta.load_move_types", side_effect=OSError("offline")):
            self.assertEqual(
                gbl_strategy.threat_types(self.lugia), ("Psychic", "Flying")
            )

    def test_typing_is_never_dropped(self) -> None:
        """Widening only: this must not make a bad matchup look survivable."""

        with patch("sources.gbl_meta.load_move_types", return_value={}):
            for name in ("Lugia", "Skarmory", "Charizard"):
                meta = gbl_strategy.lookup_matchup_pokemon(name)
                self.assertEqual(
                    set(meta.types) - set(gbl_strategy.threat_types(meta)), set()
                )


if __name__ == "__main__":
    unittest.main()
