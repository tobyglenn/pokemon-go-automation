"""Offline tests for the raid candy/XL planner: game data, math, scan protocol, plan.

The game-data fixture is a hand-cut slice of the real game master (CP
multipliers, power-up tables, a few species and moves), so the arithmetic is
checked against numbers the game itself prints -- a lucky hundo Gengar at level
50 shows CP 3254 on android-three's storage screen, and that is what `cp_of` must say.
"""

from __future__ import annotations

from tests import support as _test_support  # noqa: F401

import unittest

from PIL import Image

from sources import raid_gamedata, raid_inventory, raid_math, raid_plan, vision_ocr

CPM = [0.094, 0.16639787, 0.21573247, 0.25572005, 0.29024988, 0.3210876, 0.34921268,
       0.3752356, 0.39956728, 0.4225, 0.44310755, 0.4627984, 0.48168495, 0.49985844,
       0.51739395, 0.5343543, 0.5507927, 0.5667545, 0.5822789, 0.5974, 0.6121573,
       0.6265671, 0.64065295, 0.65443563, 0.667934, 0.6811649, 0.69414365, 0.7068842,
       0.7193991, 0.7317, 0.7377695, 0.74378943, 0.74976104, 0.7556855, 0.76156384,
       0.76739717, 0.7731865, 0.77893275, 0.784637, 0.7903, 0.7953, 0.8003, 0.8053,
       0.8103, 0.8153, 0.8203, 0.8253, 0.8303, 0.8353, 0.8403, 0.8453]
UPGRADES = {
    "candy": [1] * 10 + [2] * 10 + [3] * 5 + [4] * 5 + [6, 6, 8, 8, 10, 10, 12, 12, 15]
             + [0] * 11,
    "xl": [10, 10, 12, 12, 15, 15, 17, 17, 20, 20],
    "dust": [200, 200, 400, 400, 600, 600, 800, 800, 1000, 1000, 1300, 1300, 1600, 1600,
             1900, 1900, 2200, 2200, 2500, 2500, 3000, 3000, 3500, 3500, 4000, 4000, 4500,
             4500, 5000, 5000, 6000, 6000, 7000, 7000, 8000, 8000, 9000, 9000, 10000, 10000,
             11000, 11000, 12000, 12000, 13000, 13000, 14000, 14000, 15000],
    "shadow_candy": 1.2, "shadow_dust": 1.2, "purified_candy": 0.9, "purified_dust": 0.9,
}
ETERNATUS = {**UPGRADES,
             "candy": [c * 30 if c < 6 else c for c in UPGRADES["candy"]],
             "xl": [100, 100, 200, 200, 400, 400, 635, 635, 890, 890]}


def _mon(pid, name, dex, types, stats, fast, charged, *, shadow=True, legendary=False,
         megas=(), elite=()):
    return {"id": pid, "name": name, "search": name.lower(), "dex": dex, "types": list(types),
            "stats": list(stats), "fast": list(fast), "charged": list(charged),
            "elite": list(elite), "shadow": shadow, "legendary": legendary, "megas": list(megas)}


def fixture() -> raid_gamedata.GameData:
    moves = {
        "HEX": {"type": "Ghost", "power": 8.0, "duration": 1.0, "energy": 13, "fast": True},
        "LICK": {"type": "Ghost", "power": 5.0, "duration": 0.5, "energy": 6, "fast": True},
        "SUCKERPUNCH": {"type": "Dark", "power": 5.0, "duration": 1.0, "energy": 8, "fast": True},
        "SHADOWBALL": {"type": "Ghost", "power": 100.0, "duration": 3.0, "energy": -50, "fast": False},
        "SLUDGEBOMB": {"type": "Poison", "power": 85.0, "duration": 2.5, "energy": -50, "fast": False},
        "DRAGONTAIL": {"type": "Dragon", "power": 14.0, "duration": 1.0, "energy": 8, "fast": True},
        "OUTRAGE": {"type": "Dragon", "power": 110.0, "duration": 4.0, "energy": -50, "fast": False},
        "DRAGONCLAW": {"type": "Dragon", "power": 45.0, "duration": 1.5, "energy": -33, "fast": False},
    }
    names = {"HEX": "Hex", "LICK": "Lick", "SUCKERPUNCH": "Sucker Punch",
             "SHADOWBALL": "Shadow Ball", "SLUDGEBOMB": "Sludge Bomb",
             "DRAGONTAIL": "Dragon Tail", "OUTRAGE": "Outrage", "DRAGONCLAW": "Dragon Claw"}
    pokemon = {
        "gengar": _mon("gengar", "Gengar", 94, ["Ghost", "Poison"], [261, 149, 155],
                       ["HEX", "LICK", "SUCKERPUNCH"], ["SHADOWBALL", "SLUDGEBOMB"],
                       elite=["LICK"],
                       megas=[{"label": "Mega", "id": "gengar_mega", "stats": [349, 199, 155],
                               "types": ["Ghost", "Poison"], "first_cost": 200}]),
        "eternatus": _mon("eternatus", "Eternatus", 890, ["Poison", "Dragon"], [278, 192, 268],
                          ["DRAGONTAIL"], ["DRAGONCLAW", "SLUDGEBOMB"], shadow=False,
                          legendary=True),
        "dragonite": _mon("dragonite", "Dragonite", 149, ["Dragon", "Flying"], [263, 198, 209],
                          ["DRAGONTAIL"], ["OUTRAGE", "DRAGONCLAW"]),
    }
    effectiveness = {t: {d: 1.0 for d in raid_gamedata.TYPES} for t in raid_gamedata.TYPES}
    return raid_gamedata.from_dict({
        "schema": raid_gamedata.SCHEMA, "cpm": CPM, "upgrades": UPGRADES,
        "upgrade_overrides": {"890": ETERNATUS}, "effectiveness": effectiveness,
        "moves": moves, "move_names": names, "pokemon": pokemon,
    })


def box(text, x, y, w=200.0, h=33.0):
    return vision_ocr.TextBox(text=text, x=x, y=y, w=w, h=h)


class GameDataBuild(unittest.TestCase):
    def test_build_joins_both_sources(self):
        pvpoke = {
            "timestamp": "t",
            "moves": [{"moveId": "SHADOW_BALL", "name": "Shadow Ball"},
                      {"moveId": "HEX", "name": "Hex"}],
            "pokemon": [
                {"speciesId": "gengar", "speciesName": "Gengar", "dex": 94,
                 "types": ["ghost", "poison"], "baseStats": {"atk": 261, "def": 149, "hp": 155},
                 "fastMoves": ["HEX"], "chargedMoves": ["SHADOW_BALL"], "eliteMoves": [],
                 "tags": ["shadoweligible"], "released": True},
                {"speciesId": "gengar_mega", "speciesName": "Gengar (Mega)", "dex": 94,
                 "types": ["ghost", "poison"], "baseStats": {"atk": 349, "def": 199, "hp": 155},
                 "tags": ["mega"], "released": True},
                {"speciesId": "gengar_shadow", "speciesName": "Gengar (Shadow)", "dex": 94,
                 "types": ["ghost", "poison"], "baseStats": {"atk": 261, "def": 149, "hp": 155},
                 "tags": ["shadow"], "released": True},
            ],
        }
        miners = [
            {"templateId": "PLAYER_LEVEL_SETTINGS",
             "data": {"playerLevel": {"cpMultiplier": CPM + [0.85] * 29}}},
            {"templateId": "POKEMON_UPGRADE_SETTINGS",
             "data": {"pokemonUpgrades": {"candyCost": UPGRADES["candy"],
                                          "xlCandyCost": UPGRADES["xl"],
                                          "stardustCost": UPGRADES["dust"]}}},
            {"templateId": "POKEMON_UPGRADE_OVERRIDE_SETTINGS_V0890_POKEMON_ETERNATUS",
             "data": {"pokemonUpgrades": {"candyCost": ETERNATUS["candy"],
                                          "xlCandyCost": ETERNATUS["xl"],
                                          "stardustCost": UPGRADES["dust"]}}},
            {"templateId": "MEGA_EVOLUTION_LEVEL_0_V0094_POKEMON_GENGAR",
             "data": {"megaEvoLevelSettings": {"cooldown": {"bypassCostInitial": 200}}}},
            {"templateId": "V0070_MOVE_SHADOW_BALL",
             "data": {"moveSettings": {"movementId": "SHADOW_BALL", "pokemonType": "POKEMON_TYPE_GHOST",
                                       "power": 100.0, "durationMs": 3000, "energyDelta": -50}}},
            {"templateId": "V0261_MOVE_HEX_FAST",
             "data": {"moveSettings": {"movementId": "HEX_FAST", "pokemonType": "POKEMON_TYPE_GHOST",
                                       "power": 8.0, "durationMs": 1000, "energyDelta": 13}}},
            {"templateId": "V0593_MOVE_DIVE",
             "data": {"moveSettings": {"movementId": 593, "pokemonType": "POKEMON_TYPE_WATER",
                                       "power": 70.0, "durationMs": 3300, "energyDelta": -33}}},
        ]
        raw = raid_gamedata.build(pvpoke, miners)
        gengar = raw["pokemon"]["gengar"]
        self.assertEqual(list(raw["pokemon"]), ["gengar"])  # mega/shadow fold into the base row
        self.assertTrue(gengar["shadow"])
        self.assertEqual(gengar["megas"][0]["stats"], [349, 199, 155])
        self.assertEqual(gengar["megas"][0]["first_cost"], 200)
        self.assertEqual(raw["moves"]["HEX"]["energy"], 13)
        self.assertTrue(raw["moves"]["HEX"]["fast"])
        self.assertIn("DIVE", raw["moves"])  # numeric movementId named from the template
        self.assertEqual(raw["upgrade_overrides"]["890"]["xl"][-1], 890)


class Arithmetic(unittest.TestCase):
    gd = fixture()

    def test_cp_matches_the_game(self):
        gengar = self.gd.pokemon["gengar"]["stats"]
        self.assertEqual(raid_math.cp_of(gengar, (15, 15, 15), self.gd.cpm(50)), 3254)
        self.assertEqual(raid_math.hp_of(gengar, (15, 15, 15), self.gd.cpm(50)), 142)

    def test_half_level_multiplier(self):
        self.assertAlmostEqual(self.gd.cpm(40.5), 0.7928039, places=6)
        self.assertEqual(self.gd.levels[-1], 51.0)

    def test_full_costs(self):
        self.assertEqual(raid_math.cost_between(self.gd, 94, 1, 40),
                         raid_math.Cost(candy=304, xl=0, dust=270000))
        self.assertEqual(raid_math.cost_between(self.gd, 94, 40, 50).xl, 296)

    def test_eternatus_override_makes_xl_ruinous(self):
        self.assertEqual(raid_math.cost_between(self.gd, 890, 40, 50).xl, 8900)
        level, spent = raid_math.reachable_level(self.gd, 890, 40, 0, 54)
        self.assertEqual(level, 40.0)
        self.assertEqual(spent.xl, 0)

    def test_shadow_costs_more(self):
        self.assertEqual(raid_math.step_cost(self.gd, 94, 42, shadow=True).xl, 15)  # 12 x 1.2

    def test_price_pins_the_level(self):
        # Hatterene on android-three: 13,000 dust and 15 XL per power-up.
        self.assertEqual(raid_math.levels_for_powerup(self.gd, 858, 13000, 15),
                         {"normal": [45.0, 45.5]})
        self.assertIn("shadow", raid_math.levels_for_powerup(self.gd, 94, 12000, 18))

    def test_solve_finds_the_hundo(self):
        fits = raid_math.solve(self.gd, [self.gd.pokemon["gengar"]], 3254, 142,
                               min_iv_sum=45)
        self.assertEqual(fits, [raid_math.Fit("gengar", 50.0, (15, 15, 15))])

    def test_rating_rises_with_level_and_ranks_megas(self):
        pool = raid_math.attackers(self.gd)
        mega = next(a for a in pool if a.mega)
        plain = next(a for a in pool if a.pokemon_id == "gengar" and not a.mega and not a.shadow)
        low = raid_math.best_rating(self.gd, plain, "Ghost", 30)
        high = raid_math.best_rating(self.gd, plain, "Ghost", 50)
        self.assertGreater(high.er, low.er)
        self.assertGreater(raid_math.best_rating(self.gd, mega, "Ghost").er,
                           raid_math.best_rating(self.gd, plain, "Ghost").er)
        self.assertIsNone(raid_math.best_rating(self.gd, plain, "Dragon"))

    def test_known_moves_restrict_the_rating(self):
        plain = next(a for a in raid_math.attackers(self.gd)
                     if a.pokemon_id == "gengar" and not a.mega and not a.shadow)
        self.assertIsNone(raid_math.best_rating(self.gd, plain, "Ghost", charged=["SLUDGEBOMB"]))


class DetailParsing(unittest.TestCase):
    def test_candy_and_xl_on_rows_of_different_heights(self):
        # android-three Venusaur: the XL label sat on a row whose label box was taller,
        # and sorting by y/height put it first -- candy and XL came out swapped.
        boxes = [
            box("21,309,161", 200, 2050, 300, 70), box("STARDUST", 286, 2133, 194, 33),
            box("3,858", 851, 2054, 150, 63), box("BULBASAUR CANDY", 751, 2132, 330, 33),
            box("413", 331, 2273, 100, 48), box("BULBASAUR CANDY XI", 178, 2352, 340, 41),
            box("734", 877, 2273, 100, 48), box("ENUSAUR MEGA", 721, 2352, 330, 33),
        ]
        parsed = raid_inventory.parse_resources(boxes)
        self.assertEqual((parsed["candy"], parsed["xl_candy"]), (3858, 413))
        self.assertEqual(parsed["mega_energy"], 734)
        self.assertEqual(parsed["stardust"], 21309161)

    def test_two_plain_candy_labels_on_one_row(self):
        # Hatterene: Vision dropped the XL entirely; the right-hand cell is XL.
        boxes = [
            box("2,976", 617, 1816, 152, 63), box("HATENNA CANDY", 532, 1898.0000000002, 323, 33),
            box("502", 1041, 1816, 108, 52), box("HATENNA CANDY", 933, 1898, 323, 33),
        ]
        parsed = raid_inventory.parse_resources(boxes)
        self.assertEqual((parsed["candy"], parsed["xl_candy"]), (2976, 502))

    def test_energy_cell_with_unread_zero(self):
        boxes = [box("5,625", 851, 1788, 150, 60), box("FROAKIE CANDY", 777, 1872, 300, 33),
                 box("534", 331, 2009, 100, 48), box("FROAKIE CANDY XL", 204, 2091, 330, 33),
                 box("GRENINJA MEGA ENERGY", 699, 2091, 420, 33)]
        parsed = raid_inventory.parse_resources(boxes)
        self.assertTrue(parsed["mega_shown"])
        self.assertEqual(parsed["mega_energy"], 0)

    def test_no_energy_cell_means_no_mega(self):
        boxes = [box("2,976", 617, 1816, 152, 63), box("HATENNA CANDY", 532, 1898, 323, 33)]
        self.assertFalse(raid_inventory.parse_resources(boxes)["mega_shown"])

    def test_power_up_row_with_misreads(self):
        boxes = [box("DOWER UD", 201, 2527, 300, 45), box("113000", 695, 2515, 200, 57),
                 box("15", 1059, 2527, 60, 45)]
        parsed = raid_inventory.parse_power_up(boxes)
        self.assertEqual((parsed["dust_cost"], parsed["candy_cost"]), (13000, 15))

    def test_moves_stop_at_new_attack(self):
        boxes = [box("GYMS & RAIDS", 316, 871, 257, 33), box("Charm", 182, 999, 177, 65),
                 box("20", 1141, 1001, 78, 52), box("Dazzling Gleam", 186, 1116, 394, 56),
                 box("NEW ATTACK", 167, 1276, 375, 49), box("SWAP BUDDIES", 468, 1559, 435, 48)]
        self.assertEqual(raid_inventory.parse_moves(boxes, 1316), ["Charm", "Dazzling Gleam"])


class SearchField(unittest.TestCase):
    def test_glyph_tolerant_but_not_loose(self):
        boxes = [box("9 150", 300, 380, 200, 50)]
        self.assertTrue(raid_inventory.search_text_matches(boxes, "150", 2560))
        self.assertFalse(raid_inventory.search_text_matches(boxes, "50", 2560))
        self.assertFalse(raid_inventory.search_text_matches(
            [box("Q 094", 300, 380)], "194", 2560))

    def test_filters_and_padding(self):
        self.assertEqual(raid_inventory.dex_term(6), "006")
        self.assertEqual(raid_inventory.filters("094", False),
                         {"plain": "094", "hundo": "094&4*", "good": "094&3*,4*"})


class FakeHands:
    """A storage screen as a dict of search -> CP list."""

    def __init__(self, results, detail=None, stale=False):
        self.results = results
        self.detail = detail or []
        self.stale = stale
        self.searched = []
        self.opened = []

    def log(self, message):
        pass

    def ensure_grid(self):
        pass

    def _read(self, term):
        cps = self.results.get(term, [])
        entries = [type("E", (), {"cp": cp})() for cp in cps]
        return len(cps), entries, Image.new("RGB", (10, 10)), []

    def search(self, term):
        self.searched.append(term)
        if self.stale and len(self.searched) > 1:
            return None, [], Image.new("RGB", (10, 10)), []
        self.current = term
        return self._read(term)

    def regrid(self):
        return self._read(self.current)

    def open_cell(self, cp, image, entries):
        self.opened.append(cp)
        return True

    def read_detail(self):
        return self.detail

    def leave_detail(self):
        pass


class Protocol(unittest.TestCase):
    def test_bands_and_shadows_from_filters(self):
        hands = FakeHands({"094": [3254, 3244, 1062], "094&shadow": [1062],
                           "094&4*": [3254], "094&3*,4*": [3254, 3244]})
        record = raid_inventory.scan_species(hands, "094", "Gengar", True)
        self.assertEqual(record.owned, 3)
        self.assertEqual(hands.opened, [3254, 3244])
        bands = {i.cp: (i.iv_band, i.shadow) for i in record.individuals}
        self.assertEqual(bands, {3254: ("4*", False), 3244: ("3*", False)})

    def test_unconfirmed_search_is_an_error_not_a_species(self):
        hands = FakeHands({}, stale=True)
        hands.stale = True
        hands.searched = ["prior"]
        record = raid_inventory.scan_species(hands, "006", "Charizard", True)
        self.assertIn("did not take", record.error)
        self.assertFalse(record.complete)

    def test_unowned_is_complete(self):
        record = raid_inventory.scan_species(FakeHands({"045": []}), "045", "Vileplume", True)
        self.assertEqual(record.owned, 0)
        self.assertTrue(record.complete)

    def test_pools_take_the_majority_and_the_largest_energy(self):
        record = raid_inventory.SpeciesScan(search="006")
        pools = [
            {"candy": 2036, "xl_candy": 495, "stardust": 1, "mega_cost": None,
             "mega_energy": 0, "mega_shown": True},
            {"candy": 2036, "xl_candy": 495, "stardust": 1, "mega_cost": None,
             "mega_energy": 1253, "mega_shown": True},
            {"candy": 2086, "xl_candy": 495, "stardust": 1, "mega_cost": None,
             "mega_energy": None, "mega_shown": False},
        ]
        raid_inventory._resolve_pools(record, pools)
        self.assertEqual((record.candy, record.mega_energy, record.mega_shown), (2036, 1253, True))


class Planning(unittest.TestCase):
    gd = fixture()

    def scan(self, **kwargs):
        defaults = dict(search="094", display="Gengar", owned=1, candy=500, xl_candy=300,
                        mega_shown=True, mega_energy=2900, stardust=10_000_000)
        defaults.update(kwargs)
        return raid_inventory.SpeciesScan(**defaults)

    def test_identify_reads_level_and_ivs(self):
        individual = raid_inventory.Individual(cp=3254, hp=142, iv_band="4*", shadow=False,
                                               moves=["Hex", "Shadow Ball"])
        owned = raid_plan.identify(self.gd, self.scan(individuals=[individual]), individual)
        self.assertEqual(owned.level_range, (50.0, 50.0))
        self.assertEqual(owned.iv_range, (100.0, 100.0))
        self.assertEqual((owned.fast, owned.charged), (["HEX"], ["SHADOWBALL"]))

    def test_mega_needs_energy_and_a_cell(self):
        mega = next(a for a in raid_math.attackers(self.gd) if a.mega)
        self.assertEqual(raid_plan.mega_block(self.scan(), mega), "")
        self.assertIn("costs 200 energy, has 40", raid_plan.mega_block(self.scan(mega_energy=40), mega))
        self.assertIn("no Mega Energy", raid_plan.mega_block(self.scan(mega_shown=False), mega))

    def test_plan_spends_on_the_individual_that_ends_strongest(self):
        cpm = self.gd.cpm(30)
        low_cp = raid_math.cp_of([261, 149, 155], (15, 15, 15), cpm)
        low_hp = raid_math.hp_of([261, 149, 155], (15, 15, 15), cpm)
        individuals = [
            raid_inventory.Individual(cp=low_cp, hp=low_hp, iv_band="4*", shadow=False,
                                      moves=["Hex", "Shadow Ball"]),
        ]
        scan = self.scan(individuals=individuals, mega_shown=False)
        rankings = {"Ghost": raid_math.rank_type(self.gd, "Ghost").ratings}
        plan = raid_plan.build("phone", [scan], self.gd, rankings)
        ghost = plan.types["Ghost"]
        spend = ghost.spends(False)[0]
        self.assertEqual(spend.attacker.name, "Gengar")
        self.assertEqual(spend.reach_level, 50.0)
        self.assertGreater(spend.gain, 0)
        self.assertTrue(ghost.unavailable_megas)
        text = raid_plan.report(plan, self.gd)
        self.assertIn("SPEND:", text)
        self.assertIn("Mega Gengar (no Mega Energy cell on this account)", text)

    def test_off_type_attackers_are_not_options(self):
        # Gengar has no Dragon move, but even with one it is not a Dragon type.
        cpm = self.gd.cpm(40)
        stats = [261, 149, 155]
        individual = raid_inventory.Individual(
            cp=raid_math.cp_of(stats, (15, 15, 15), cpm), hp=raid_math.hp_of(stats, (15, 15, 15), cpm),
            iv_band="4*", moves=["Hex", "Sludge Bomb"])
        scan = self.scan(individuals=[individual])
        rankings = {"Poison": raid_math.rank_type(self.gd, "Poison").ratings,
                    "Ghost": raid_math.rank_type(self.gd, "Ghost").ratings}
        plan = raid_plan.build("phone", [scan], self.gd, rankings)
        self.assertTrue(all("Poison" in (o.attacker.mega_types or o.attacker.types)
                            for o in plan.types["Poison"].options))

    def test_a_maxed_individual_is_not_a_spend(self):
        cpm = self.gd.cpm(50)
        stats = [261, 149, 155]
        individual = raid_inventory.Individual(
            cp=raid_math.cp_of(stats, (15, 15, 15), cpm), hp=raid_math.hp_of(stats, (15, 15, 15), cpm),
            iv_band="4*", moves=["Hex", "Shadow Ball"])
        scan = self.scan(individuals=[individual], mega_shown=False)
        rankings = {"Ghost": raid_math.rank_type(self.gd, "Ghost").ratings}
        plan = raid_plan.build("phone", [scan], self.gd, rankings)
        self.assertEqual(plan.types["Ghost"].spends(False), [])

    def test_rare_candy_skips_what_its_own_candy_can_max(self):
        cpm = self.gd.cpm(20)
        stats = [261, 149, 155]
        individual = raid_inventory.Individual(
            cp=raid_math.cp_of(stats, (15, 15, 15), cpm), hp=raid_math.hp_of(stats, (15, 15, 15), cpm),
            iv_band="4*", moves=["Hex", "Shadow Ball"])
        rankings = {"Ghost": raid_math.rank_type(self.gd, "Ghost").ratings}
        rich = raid_plan.build("p", [self.scan(individuals=[individual], candy=5000, xl_candy=900,
                                               mega_shown=False)], self.gd, rankings)
        self.assertEqual(raid_plan.rare_targets(self.gd, rich), [])
        poor = raid_plan.build("p", [self.scan(individuals=[individual], candy=10, xl_candy=0)],
                               self.gd, rankings)
        target = raid_plan.rare_targets(self.gd, poor)[0]
        self.assertEqual((target.rare, target.rare_xl), (238, 296))
        self.assertIn("RARE CANDY XL", raid_plan.rare_report(poor, self.gd))

    def test_eternatus_is_never_an_xl_spend(self):
        cpm = self.gd.cpm(40)
        stats = [278, 192, 268]
        individual = raid_inventory.Individual(
            cp=raid_math.cp_of(stats, (15, 15, 15), cpm), hp=raid_math.hp_of(stats, (15, 15, 15), cpm),
            iv_band="4*", moves=["Dragon Tail", "Dragon Claw"], dust_cost=10000)
        scan = raid_inventory.SpeciesScan(search="890", display="Eternatus", owned=1, candy=178,
                                          xl_candy=54, individuals=[individual])
        rankings = {"Dragon": raid_math.rank_type(self.gd, "Dragon").ratings}
        plan = raid_plan.build("phone", [scan], self.gd, rankings)
        option = next(o for o in plan.types["Dragon"].options if o.attacker.pokemon_id == "eternatus")
        self.assertEqual(option.reach_level, 40.0)
        self.assertEqual(option.reach_cost.xl, 0)


if __name__ == "__main__":
    unittest.main()
