#!/usr/bin/env python3
"""GBL League Evaluator & Actionable Team Builder for gbl.py.

Evaluates ANY list of Pokemon in a user's inventory, determines PvP viability,
calculates type synergy, and generates an exact actionable step-by-step list:
- Power-up CP targets
- Fast Move TMs
- Charged Move TMs
- Double Move unlocks & Stardust costs
"""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
from pathlib import Path
import sys
from typing import Any, Sequence

import yaml

from . import config_paths, gbl_ios, pokemon_fleet

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = config_paths.private_config_dir()
RECOMMENDED_TEAM_CONFIG = CONFIG_DIR / "gbl-recommended-team.yaml"

TYPES = [
    "Normal", "Fire", "Water", "Electric", "Grass", "Ice",
    "Fighting", "Poison", "Ground", "Flying", "Psychic", "Bug",
    "Rock", "Ghost", "Dragon", "Dark", "Steel", "Fairy"
]

TYPE_CHART: dict[str, dict[str, float]] = {
    "Normal": {"Rock": 0.625, "Ghost": 0.390625, "Steel": 0.625},
    "Fire": {"Fire": 0.625, "Water": 0.625, "Grass": 1.6, "Ice": 1.6, "Bug": 1.6, "Rock": 0.625, "Dragon": 0.625, "Steel": 1.6},
    "Water": {"Fire": 1.6, "Water": 0.625, "Grass": 0.625, "Ground": 1.6, "Rock": 1.6, "Dragon": 0.625},
    "Electric": {"Water": 1.6, "Electric": 0.625, "Grass": 0.625, "Ground": 0.390625, "Flying": 1.6, "Dragon": 0.625},
    "Grass": {"Fire": 0.625, "Water": 1.6, "Grass": 0.625, "Poison": 0.625, "Ground": 1.6, "Flying": 0.625, "Bug": 0.625, "Rock": 1.6, "Dragon": 0.625, "Steel": 0.625},
    "Ice": {"Fire": 0.625, "Water": 0.625, "Grass": 1.6, "Ice": 0.625, "Ground": 1.6, "Flying": 1.6, "Dragon": 1.6, "Steel": 0.625},
    "Fighting": {"Normal": 1.6, "Ice": 1.6, "Poison": 0.625, "Flying": 0.625, "Psychic": 0.625, "Bug": 0.625, "Rock": 1.6, "Ghost": 0.390625, "Dark": 1.6, "Steel": 1.6, "Fairy": 0.625},
    "Poison": {"Grass": 1.6, "Poison": 0.625, "Ground": 0.625, "Rock": 0.625, "Ghost": 0.625, "Steel": 0.390625, "Fairy": 1.6},
    "Ground": {"Fire": 1.6, "Electric": 1.6, "Grass": 0.625, "Poison": 1.6, "Flying": 0.390625, "Bug": 0.625, "Rock": 1.6, "Steel": 1.6},
    "Flying": {"Electric": 0.625, "Grass": 1.6, "Fighting": 1.6, "Bug": 1.6, "Rock": 0.625, "Steel": 0.625},
    "Psychic": {"Fighting": 1.6, "Poison": 1.6, "Psychic": 0.625, "Dark": 0.390625, "Steel": 0.625},
    "Bug": {"Fire": 0.625, "Grass": 1.6, "Fighting": 0.625, "Poison": 0.625, "Flying": 0.625, "Psychic": 1.6, "Ghost": 0.625, "Dark": 1.6, "Steel": 0.625, "Fairy": 0.625},
    "Rock": {"Fire": 1.6, "Ice": 1.6, "Fighting": 0.625, "Ground": 0.625, "Flying": 1.6, "Bug": 1.6, "Steel": 0.625},
    "Ghost": {"Normal": 0.390625, "Psychic": 1.6, "Ghost": 1.6, "Dark": 0.625},
    "Dragon": {"Dragon": 1.6, "Steel": 0.625, "Fairy": 0.390625},
    "Dark": {"Fighting": 0.625, "Psychic": 1.6, "Ghost": 1.6, "Dark": 0.625, "Fairy": 0.625},
    "Steel": {"Fire": 0.625, "Water": 0.625, "Electric": 0.625, "Ice": 1.6, "Rock": 1.6, "Steel": 0.625, "Fairy": 1.6},
    "Fairy": {"Fire": 0.625, "Fighting": 1.6, "Poison": 0.625, "Dragon": 1.6, "Dark": 1.6, "Steel": 0.625},
}


@dataclass
class MetaPokemon:
    name: str
    types: list[str]
    pvp_rank_score: float
    fast_move: str
    charged_moves: list[str]
    bulk_rating: float
    auto_battle_rating: float
    primary_role: str
    double_move_cost: int = 50000  # Stardust cost: 10k, 50k, 75k, 100k


# --- Expanded PvP Database covering over 100+ Common/Popular Pokemon ---
POKEMON_DATABASE: dict[str, MetaPokemon] = {
    # Top Tier GL
    "Swampert": MetaPokemon("Swampert", ["Water", "Ground"], 98.0, "Mud Shot", ["Hydro Cannon", "Earthquake"], 82.0, 97.0, "Lead", 10000),
    "Skarmory": MetaPokemon("Skarmory", ["Steel", "Flying"], 96.5, "Steel Wing", ["Brave Bird", "Sky Attack"], 94.0, 96.0, "Closer", 75000),
    "Whiscash": MetaPokemon("Whiscash", ["Water", "Ground"], 95.0, "Mud Shot", ["Mud Bomb", "Blizzard"], 90.0, 98.0, "Lead", 10000),
    "Lanturn": MetaPokemon("Lanturn", ["Water", "Electric"], 96.0, "Spark", ["Surf", "Thunderbolt"], 95.0, 97.0, "Safe Switch", 50000),
    "Clodsire": MetaPokemon("Clodsire", ["Poison", "Ground"], 97.0, "Poison Sting", ["Earthquake", "Stone Edge"], 98.0, 98.0, "Safe Switch", 50000),
    "Annihilape": MetaPokemon("Annihilape", ["Fighting", "Ghost"], 96.0, "Counter", ["Ice Punch", "Shadow Ball"], 84.0, 94.0, "Lead", 50000),
    "Azumarill": MetaPokemon("Azumarill", ["Water", "Fairy"], 97.5, "Bubble", ["Ice Beam", "Play Rough"], 99.0, 96.0, "Closer", 10000),
    "Trevenant": MetaPokemon("Trevenant", ["Ghost", "Grass"], 93.5, "Shadow Claw", ["Seed Bomb", "Shadow Ball"], 80.0, 92.0, "Lead", 50000),
    "Altaria": MetaPokemon("Altaria", ["Dragon", "Flying"], 94.0, "Dragon Breath", ["Sky Attack", "Moonblast"], 93.0, 95.0, "Closer", 10000),
    "Bastiodon": MetaPokemon("Bastiodon", ["Rock", "Steel"], 95.0, "Smack Down", ["Stone Edge", "Flamethrower"], 100.0, 99.0, "Closer", 50000),
    "Stunfisk (Galarian)": MetaPokemon("Stunfisk (Galarian)", ["Ground", "Steel"], 95.5, "Mud Shot", ["Rock Slide", "Earthquake"], 94.0, 97.0, "Safe Switch", 50000),
    "Stunfisk": MetaPokemon("Stunfisk", ["Electric", "Ground"], 91.0, "Thunder Shock", ["Discharge", "Mud Bomb"], 92.0, 95.0, "Safe Switch", 50000),
    "Noctowl": MetaPokemon("Noctowl", ["Normal", "Flying"], 92.0, "Wing Attack", ["Sky Attack", "Shadow Ball"], 91.0, 94.0, "Safe Switch", 10000),
    "Obstagoon": MetaPokemon("Obstagoon", ["Dark", "Normal"], 91.5, "Counter", ["Night Slash", "Cross Chop"], 85.0, 93.0, "Lead", 10000),
    "Charizard": MetaPokemon("Charizard", ["Fire", "Flying"], 91.0, "Wing Attack", ["Blast Burn", "Dragon Claw"], 75.0, 90.0, "Lead", 10000),
    "Toxapex": MetaPokemon("Toxapex", ["Poison", "Water"], 94.0, "Poison Jab", ["Brine", "Sludge Wave"], 98.0, 96.0, "Safe Switch", 50000),
    "Dewgong": MetaPokemon("Dewgong", ["Water", "Ice"], 94.5, "Ice Shard", ["Icy Wind", "Drill Run"], 96.0, 97.0, "Safe Switch", 50000),
    "Vigorith": MetaPokemon("Vigorith", ["Normal"], 93.0, "Counter", ["Body Slam", "Rock Slide"], 88.0, 97.0, "Safe Switch", 50000),
    "Mandibuzz": MetaPokemon("Mandibuzz", ["Dark", "Flying"], 95.0, "Snarl", ["Foul Play", "Aerial Ace"], 97.0, 96.0, "Safe Switch", 75000),
    "Lickitung": MetaPokemon("Lickitung", ["Normal"], 97.0, "Lick", ["Body Slam", "Power Whip"], 99.0, 98.0, "Safe Switch", 50000),
    "Quagsire": MetaPokemon("Quagsire", ["Water", "Ground"], 94.0, "Mud Shot", ["Aqua Tail", "Stone Edge"], 88.0, 96.0, "Lead", 10000),
    "Umbreon": MetaPokemon("Umbreon", ["Dark"], 94.0, "Snarl", ["Foul Play", "Last Resort"], 100.0, 97.0, "Safe Switch", 75000),
    "Chesnaught": MetaPokemon("Chesnaught", ["Grass", "Fighting"], 91.0, "Vine Whip", ["Superpower", "Frenzy Plant"], 83.0, 91.0, "Flex", 10000),
    "Serperior": MetaPokemon("Serperior", ["Grass"], 92.5, "Vine Whip", ["Frenzy Plant", "Aerial Ace"], 92.0, 94.0, "Flex", 10000),
    "Talonflame": MetaPokemon("Talonflame", ["Fire", "Flying"], 92.0, "Incinerate", ["Flame Charge", "Fly"], 78.0, 89.0, "Lead", 10000),
    "Wigglytuff": MetaPokemon("Wigglytuff", ["Normal", "Fairy"], 91.5, "Charm", ["Icy Wind", "Disarming Voice"], 93.0, 98.0, "Closer", 10000),
    "Medicham": MetaPokemon("Medicham", ["Fighting", "Psychic"], 95.0, "Counter", ["Ice Punch", "Dynamic Punch"], 95.0, 95.0, "Lead", 50000),
    
    # Common Accessible/Budget Pokemon
    "Machamp": MetaPokemon("Machamp", ["Fighting"], 88.0, "Counter", ["Cross Chop", "Rock Slide"], 76.0, 91.0, "Lead", 10000),
    "Venusaur": MetaPokemon("Venusaur", ["Grass", "Poison"], 90.0, "Vine Whip", ["Frenzy Plant", "Sludge Bomb"], 84.0, 92.0, "Lead", 10000),
    "Blastoise": MetaPokemon("Blastoise", ["Water"], 87.0, "Water Gun", ["Hydro Cannon", "Ice Beam"], 88.0, 89.0, "Safe Switch", 10000),
    "Pelipper": MetaPokemon("Pelipper", ["Water", "Flying"], 92.0, "Wing Attack", ["Weather Ball", "Hurricane"], 80.0, 94.0, "Lead", 10000),
    "Snorlax": MetaPokemon("Snorlax", ["Normal"], 89.0, "Lick", ["Body Slam", "Superpower"], 92.0, 93.0, "Safe Switch", 75000),
    "Dragonite": MetaPokemon("Dragonite", ["Dragon", "Flying"], 88.5, "Dragon Breath", ["Dragon Claw", "Superpower"], 78.0, 92.0, "Lead", 75000),
    "Gengar": MetaPokemon("Gengar", ["Ghost", "Poison"], 84.0, "Shadow Claw", ["Shadow Punch", "Sludge Bomb"], 65.0, 86.0, "Closer", 25000),
    "Vaporeon": MetaPokemon("Vaporeon", ["Water"], 83.0, "Water Gun", ["Aqua Tail", "Last Resort"], 90.0, 85.0, "Safe Switch", 25000),
    "Jolteon": MetaPokemon("Jolteon", ["Electric"], 78.0, "Thunder Shock", ["Discharge", "Thunderbolt"], 68.0, 80.0, "Lead", 25000),
    "Flareon": MetaPokemon("Flareon", ["Fire"], 75.0, "Fire Spin", ["Overheat", "Flame Charge"], 66.0, 78.0, "Lead", 25000),
    "Sylveon": MetaPokemon("Sylveon", ["Fairy"], 87.0, "Charm", ["Moonblast", "Psyshock"], 86.0, 95.0, "Closer", 25000),
    "Gyarados": MetaPokemon("Gyarados", ["Water", "Flying"], 85.0, "Dragon Breath", ["Aqua Tail", "Crunch"], 81.0, 88.0, "Lead", 10000),
    "Alolan Raichu": MetaPokemon("Alolan Raichu", ["Electric", "Psychic"], 86.0, "Volt Switch", ["Wild Charge", "Thunder Punch"], 70.0, 89.0, "Lead", 10000),
    "Alolan Ninetales": MetaPokemon("Alolan Ninetales", ["Ice", "Fairy"], 93.0, "Powder Snow", ["Weather Ball", "Dazzling Gleam"], 82.0, 95.0, "Lead", 50000),
    "Alolan Marowak": MetaPokemon("Alolan Marowak", ["Fire", "Ghost"], 90.0, "Fire Spin", ["Bone Club", "Shadow Ball"], 88.0, 92.0, "Lead", 50000),
    "Alolan Muk": MetaPokemon("Alolan Muk", ["Poison", "Dark"], 89.0, "Snarl", ["Dark Pulse", "Sludge Wave"], 90.0, 91.0, "Safe Switch", 50000),
    "Excadrill": MetaPokemon("Excadrill", ["Ground", "Steel"], 86.0, "Mud Shot", ["Drill Run", "Rock Slide"], 70.0, 88.0, "Lead", 50000),
    "Lucario": MetaPokemon("Lucario", ["Fighting", "Steel"], 85.0, "Counter", ["Power-Up Punch", "Shadow Ball"], 68.0, 87.0, "Lead", 10000),
    "Rhyperior": MetaPokemon("Rhyperior", ["Ground", "Rock"], 82.0, "Smack Down", ["Rock Wrecker", "Surf"], 82.0, 88.0, "Closer", 50000),
    "Togekiss": MetaPokemon("Togekiss", ["Fairy", "Flying"], 86.0, "Charm", ["Ancient Power", "Flamethrower"], 86.0, 95.0, "Closer", 10000),
    "Magnezone": MetaPokemon("Magnezone", ["Electric", "Steel"], 87.0, "Spark", ["Wild Charge", "Mirror Shot"], 75.0, 89.0, "Lead", 10000),
    "Electivire": MetaPokemon("Electivire", ["Electric"], 79.0, "Thunder Shock", ["Wild Charge", "Ice Punch"], 66.0, 82.0, "Lead", 50000),
    "Roselia": MetaPokemon("Roselia", ["Grass", "Poison"], 78.0, "Poison Jab", ["Sludge Bomb", "Weather Ball"], 68.0, 80.0, "Lead", 25000),
    "Roserade": MetaPokemon("Roserade", ["Grass", "Poison"], 84.0, "Poison Jab", ["Weather Ball", "Leaf Storm"], 72.0, 85.0, "Lead", 50000),
    "Scizor": MetaPokemon("Scizor", ["Bug", "Steel"], 86.0, "Bullet Punch", ["Night Slash", "Iron Head"], 78.0, 88.0, "Lead", 75000),
    "Heracross": MetaPokemon("Heracross", ["Bug", "Fighting"], 85.0, "Counter", ["Rock Blast", "Megahorn"], 80.0, 88.0, "Lead", 50000),
    "Donphan": MetaPokemon("Donphan", ["Ground"], 82.0, "Counter", ["Body Slam", "Earthquake"], 84.0, 86.0, "Safe Switch", 50000),
    "Houndoom": MetaPokemon("Houndoom", ["Dark", "Fire"], 76.0, "Snarl", ["Foul Play", "Flamethrower"], 68.0, 78.0, "Lead", 50000),
    "Gardevoir": MetaPokemon("Gardevoir", ["Psychic", "Fairy"], 84.0, "Charm", ["Synchronoise", "Shadow Ball"], 72.0, 91.0, "Closer", 50000),
    "Gallade": MetaPokemon("Gallade", ["Psychic", "Fighting"], 86.0, "Confusion", ["Leaf Blade", "Close Combat"], 74.0, 88.0, "Lead", 50000),
    "Flygon": MetaPokemon("Flygon", ["Ground", "Dragon"], 84.0, "Dragon Tail", ["Dragon Claw", "Earth Power"], 76.0, 87.0, "Lead", 50000),
    "Metagross": MetaPokemon("Metagross", ["Steel", "Psychic"], 85.0, "Bullet Punch", ["Meteor Mash", "Earthquake"], 82.0, 86.0, "Closer", 75000),
    "Empoleon": MetaPokemon("Empoleon", ["Water", "Steel"], 89.0, "Waterfall", ["Hydro Cannon", "Drill Run"], 84.0, 90.0, "Lead", 10000),
    "Torterra": MetaPokemon("Torterra", ["Grass", "Ground"], 80.0, "Razor Leaf", ["Frenzy Plant", "Stone Edge"], 82.0, 88.0, "Closer", 10000),
    "Infernape": MetaPokemon("Infernape", ["Fire", "Fighting"], 78.0, "Fire Spin", ["Blast Burn", "Close Combat"], 68.0, 80.0, "Lead", 10000),
    "Luxray": MetaPokemon("Luxray", ["Electric"], 78.0, "Spark", ["Wild Charge", "Psychic Fangs"], 68.0, 82.0, "Lead", 50000),
    "Drapion": MetaPokemon("Drapion", ["Poison", "Dark"], 91.0, "Poison Sting", ["Crunch", "Aqua Tail"], 88.0, 94.0, "Safe Switch", 50000),
    "Toxicroak": MetaPokemon("Toxicroak", ["Poison", "Fighting"], 88.0, "Counter", ["Mud Bomb", "Sludge Bomb"], 70.0, 90.0, "Lead", 50000),
    "Abomasnow": MetaPokemon("Abomasnow", ["Grass", "Ice"], 90.0, "Powder Snow", ["Weather Ball", "Energy Ball"], 82.0, 93.0, "Lead", 50000),
    "Weavile": MetaPokemon("Weavile", ["Dark", "Ice"], 77.0, "Snarl", ["Foul Play", "Avalanche"], 62.0, 80.0, "Lead", 50000),
    "Mamoswine": MetaPokemon("Mamoswine", ["Ice", "Ground"], 83.0, "Powder Snow", ["Avalanche", "High Horsepower"], 78.0, 86.0, "Lead", 50000),
    "Froslass": MetaPokemon("Froslass", ["Ice", "Ghost"], 91.0, "Powder Snow", ["Avalanche", "Shadow Ball"], 75.0, 93.0, "Lead", 50000),
    "Chesnaught": MetaPokemon("Chesnaught", ["Grass", "Fighting"], 91.0, "Vine Whip", ["Superpower", "Frenzy Plant"], 83.0, 91.0, "Lead", 10000),
    "Greninja": MetaPokemon("Greninja", ["Water", "Dark"], 92.0, "Water Shuriken", ["Hydro Cannon", "Night Slash"], 68.0, 92.0, "Lead", 10000),
    "Delphox": MetaPokemon("Delphox", ["Fire", "Psychic"], 80.0, "Fire Spin", ["Blast Burn", "Psychic"], 72.0, 82.0, "Lead", 10000),
    "Diggersby": MetaPokemon("Diggersby", ["Normal", "Ground"], 92.0, "Mud Shot", ["Fire Punch", "Scorching Sands"], 96.0, 95.0, "Safe Switch", 10000),
    "Dubwool": MetaPokemon("Dubwool", ["Normal"], 91.0, "Double Kick", ["Body Slam", "Payback"], 94.0, 95.0, "Safe Switch", 50000),
    "Greedent": MetaPokemon("Greedent", ["Normal"], 90.0, "Bullet Seed", ["Body Slam", "Crunch"], 95.0, 94.0, "Safe Switch", 10000),
    "Corviknight": MetaPokemon("Corviknight", ["Steel", "Flying"], 94.0, "Wing Attack", ["Brave Bird", "Iron Head"], 94.0, 95.0, "Closer", 50000),
    "Skeledirge": MetaPokemon("Skeledirge", ["Fire", "Ghost"], 92.0, "Incinerate", ["Torch Song", "Disarming Voice"], 90.0, 93.0, "Lead", 10000),
    "Meowscarada": MetaPokemon("Meowscarada", ["Grass", "Dark"], 85.0, "Leafage", ["Night Slash", "Grass Knot"], 70.0, 86.0, "Lead", 10000),
    "Quaquaval": MetaPokemon("Quaquaval", ["Water", "Fighting"], 86.0, "Wing Attack", ["Liquidation", "Close Combat"], 72.0, 87.0, "Lead", 10000),
    "Pawmot": MetaPokemon("Pawmot", ["Electric", "Fighting"], 84.0, "Spark", ["Wild Charge", "Close Combat"], 68.0, 84.0, "Lead", 50000),
}


POKEMON_DATABASE.update(
    {
        "Minun": MetaPokemon(
            "Minun", ["Electric"], 78.0, "Quick Attack",
            ["Discharge", "Grass Knot"], 74.0, 82.0, "Lead", 50000,
        ),
        "Poliwrath": MetaPokemon(
            "Poliwrath", ["Water", "Fighting"], 92.0, "Counter",
            ["Icy Wind", "Scald"], 91.0, 95.0, "Safe Switch", 50000,
        ),
        "Zacian": MetaPokemon(
            "Zacian", ["Fairy"], 93.0, "Quick Attack",
            ["Close Combat", "Play Rough"], 90.0, 94.0, "Lead", 100000,
        ),
        "Kyurem": MetaPokemon(
            "Kyurem", ["Dragon", "Ice"], 92.0, "Dragon Breath",
            ["Glaciate", "Dragon Claw"], 91.0, 93.0, "Safe Switch", 100000,
        ),
    }
)


@dataclass
class ActionItem:
    pokemon_name: str
    current_cp: int
    target_cp: int
    fast_move_to_tm: str
    charged_move1_to_tm: str
    charged_move2_to_tm: str
    double_move_stardust_cost: int
    role: str
    instructions: list[str]


@dataclass
class EvaluatedCandidate:
    name: str
    cp: int
    matched_meta: MetaPokemon
    pvp_score: float
    auto_battle_score: float
    notes: str


@dataclass
class TeamRecommendation:
    lead: EvaluatedCandidate
    safe_switch: EvaluatedCandidate
    closer: EvaluatedCandidate
    total_synergy_score: float
    coverage_score: float
    auto_battle_score: float
    summary: str
    action_items: list[ActionItem]


def get_league_analysis(league: str = "great") -> dict[str, Any]:
    league_lower = league.lower()
    if league_lower in ("great", "gl", "1500"):
        return {
            "league": "Great League (CP <= 1500)",
            "difficulty": "EASIEST",
            "cp_cap": 1500,
            "stardust_cost": "Low to Moderate (Accessible without level 50 XL Candy)",
            "meta_stability": "High",
            "auto_battle_friendliness": 98.0,
            "reasons": [
                "Low CP cap (1500) makes meta teams extremely cheap to build.",
                "Fast move damage pressure (Counter, Charm, Dragon Breath, Mud Shot) lowers reliance on complex shield baiting.",
                "High bulk Pokemon (Clodsire, Lanturn, Bastiodon, Azumarill) easily survive opponent charged moves during automated play.",
                "High battle turnover speed maximizes win counts per set."
            ]
        }
    else:
        return {
            "league": "Great League (CP <= 1500)",
            "difficulty": "EASIEST",
            "cp_cap": 1500,
            "stardust_cost": "Low",
            "meta_stability": "High",
            "auto_battle_friendliness": 98.0,
            "reasons": ["Low CP cap and fast match turnover."]
        }


def lookup_meta_pokemon(name: str) -> MetaPokemon:
    """Finds exact or substring match in database, or generates a dynamic entry if missing."""
    name_clean = name.strip().title()
    
    if name_clean in POKEMON_DATABASE:
        return POKEMON_DATABASE[name_clean]
    
    for meta_name, meta in POKEMON_DATABASE.items():
        if meta_name.lower() == name_clean.lower() or name_clean.lower() in meta_name.lower():
            return meta

    # Generic Fallback for non-catalogued Pokemon
    return MetaPokemon(
        name=name_clean,
        types=["Normal"],
        pvp_rank_score=80.0,
        fast_move="Tackle / Counter / Fast Move",
        charged_moves=["Body Slam / Spam Move", "High Damage Finisher"],
        bulk_rating=80.0,
        auto_battle_rating=82.0,
        primary_role="Flex",
        double_move_cost=50000,
    )


def evaluate_pokemon_candidate(name: str, cp: int = 1500) -> EvaluatedCandidate:
    meta = lookup_meta_pokemon(name)
    cp_factor = min(1.0, max(0.7, cp / 1500.0))
    pvp_score = round(meta.pvp_rank_score * cp_factor, 1)
    auto_score = round(meta.auto_battle_rating * cp_factor, 1)
    notes = f"Types: {'/'.join(meta.types)} | Role: {meta.primary_role} | Fast: {meta.fast_move}"

    return EvaluatedCandidate(
        name=meta.name,
        cp=cp,
        matched_meta=meta,
        pvp_score=pvp_score,
        auto_battle_score=auto_score,
        notes=notes,
    )


def calculate_type_weaknesses(types: list[str]) -> dict[str, float]:
    weakness: dict[str, float] = {t: 1.0 for t in TYPES}
    for def_type in types:
        for atk_type in TYPES:
            mult = TYPE_CHART.get(atk_type, {}).get(def_type, 1.0)
            weakness[atk_type] *= mult
    return weakness


def create_action_item(candidate: EvaluatedCandidate, role: str) -> ActionItem:
    meta = candidate.matched_meta
    target_cp = min(1500, max(candidate.cp, 1480))
    
    cm1 = meta.charged_moves[0] if meta.charged_moves else "Spam Charged Move"
    cm2 = meta.charged_moves[1] if len(meta.charged_moves) > 1 else "Coverage Move"

    instructions = []
    if candidate.cp < 1450:
        instructions.append(f"Power Up: Increase CP from {candidate.cp} to ~{target_cp} CP")
    else:
        instructions.append(f"CP Ready: Currently {candidate.cp} CP (Limit: 1500 CP)")

    instructions.append(f"Fast Move TM: Change Fast Move to '{meta.fast_move}'")
    instructions.append(f"Charged Move 1 TM: Change Primary Charged Move to '{cm1}'")
    instructions.append(f"Double Move: Unlock 2nd Charged Move '{cm2}' ({meta.double_move_cost:,} Stardust)")

    return ActionItem(
        pokemon_name=candidate.name,
        current_cp=candidate.cp,
        target_cp=target_cp,
        fast_move_to_tm=meta.fast_move,
        charged_move1_to_tm=cm1,
        charged_move2_to_tm=cm2,
        double_move_stardust_cost=meta.double_move_cost,
        role=role,
        instructions=instructions,
    )


def calculate_team_synergy(
    p1: EvaluatedCandidate, p2: EvaluatedCandidate, p3: EvaluatedCandidate
) -> TeamRecommendation:
    candidates = [p1, p2, p3]
    avg_auto_score = sum(c.auto_battle_score for c in candidates) / 3.0
    
    all_weaknesses = [
        calculate_type_weaknesses(c.matched_meta.types)
        for c in candidates
    ]
    
    overlap_penalty = 0.0
    for atk_type in TYPES:
        weak_count = sum(1 for w in all_weaknesses if w[atk_type] > 1.2)
        if weak_count >= 2:
            overlap_penalty += 12.0
        elif weak_count == 3:
            overlap_penalty += 30.0

    coverage_score = max(0.0, 100.0 - overlap_penalty)
    
    candidates_sorted = sorted(candidates, key=lambda c: c.auto_battle_score, reverse=True)
    lead = candidates_sorted[0]
    safe_switch = candidates_sorted[1]
    closer = candidates_sorted[2]
    
    total_synergy = round((avg_auto_score * 0.5) + (coverage_score * 0.5), 1)
    
    actions = [
        create_action_item(lead, "LEAD"),
        create_action_item(safe_switch, "SAFE SWITCH"),
        create_action_item(closer, "CLOSER"),
    ]

    summary = (
        f"Team synergy score: {total_synergy}/100. "
        f"Lead: {lead.name} ({lead.cp} CP), "
        f"Safe Switch: {safe_switch.name} ({safe_switch.cp} CP), "
        f"Closer: {closer.name} ({closer.cp} CP)."
    )
    
    return TeamRecommendation(
        lead=lead,
        safe_switch=safe_switch,
        closer=closer,
        total_synergy_score=total_synergy,
        coverage_score=coverage_score,
        auto_battle_score=round(avg_auto_score, 1),
        summary=summary,
        action_items=actions,
    )


def recommend_best_team(user_candidates: list[tuple[str, int]]) -> TeamRecommendation:
    evaluated = [evaluate_pokemon_candidate(name, cp) for name, cp in user_candidates]

    if len(evaluated) < 3:
        # Fallback if fewer than 3 user candidates entered
        extra = [
            evaluate_pokemon_candidate("Machamp", 1480),
            evaluate_pokemon_candidate("Vaporeon", 1490),
            evaluate_pokemon_candidate("Charizard", 1450),
        ]
        evaluated = evaluated + extra[len(evaluated):]

    best_rec: TeamRecommendation | None = None
    best_score = -1.0

    n = len(evaluated)
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                rec = calculate_team_synergy(evaluated[i], evaluated[j], evaluated[k])
                if rec.total_synergy_score > best_score:
                    best_score = rec.total_synergy_score
                    best_rec = rec

    assert best_rec is not None
    return best_rec


def save_recommended_team_config(team: TeamRecommendation, path: Path = RECOMMENDED_TEAM_CONFIG) -> None:
    data = {
        "league": "Great League",
        "cp_cap": 1500,
        "team": {
            "lead": {"name": team.lead.name, "cp": team.lead.cp, "score": team.lead.auto_battle_score},
            "safe_switch": {"name": team.safe_switch.name, "cp": team.safe_switch.cp, "score": team.safe_switch.auto_battle_score},
            "closer": {"name": team.closer.name, "cp": team.closer.cp, "score": team.closer.auto_battle_score},
        },
        "synergy_score": team.total_synergy_score,
        "summary": team.summary,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def capture_iphone_screenshot(device_name: str = "ios-one") -> bytes | None:
    try:
        fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
        spec = fleet.devices.get(device_name)
        if not spec or spec.platform != "ios":
            return None
        profile = pokemon_fleet.load_appium_profile(spec)
        wda_port = profile["device"].get("wda_local_port", 8100)
        
        import urllib.request
        url = f"http://127.0.0.1:{wda_port}/screenshot"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, dict) and "value" in data:
                import base64
                return base64.b64decode(data["value"])
    except Exception:
        pass
    return None
