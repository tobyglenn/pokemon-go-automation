"""Build named manual storage searches from the receiver's audits and each phone's retained copies."""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

from . import clear_pokemon as clear, collect_trades, collector_day, config_paths, dex_registration, pokemon_fleet
from . import raid_gamedata, raid_meta, trade_favorite_names, trade_plan, trade_saved_search, trading_accounts

STRICT = "SafeTrash"
HUNDO_REVIEW = "HundoReview"
RAID_REVIEW = "Non-Raid"
LUCKY_TRADES = trade_favorite_names.LUCKY
HUNDO_TRADES = trade_favorite_names.HUNDO
PRIORITY_TRADES = trade_favorite_names.PRIORITY
EMPTY = "0*&4*&!4*"
# No CP or nickname inference: at least one of these excluded copies must be
# in the same account's storage before a species enters its trash search.
PREVIOUS_ANCHOR = "favorite,lucky,4*,dontrade,98Plus"
OLD_KEEP_ONE = "Keep One"
KEEP_ONE = "KeepOne"
SPACED_ANCHOR = PREVIOUS_ANCHOR + ",#Keep One"
TRANSITION_ANCHOR = SPACED_ANCHOR + ",#KeepOne"
ANCHOR = PREVIOUS_ANCHOR + ",#KeepOne"
# Protect candy for G-Max species and prominent Max Battle tanks and healers,
# even when the specimen itself lacks a Max form. Family closure includes babies.
MAX_CANDY = frozenset({
    3, 6, 9, 12, 25, 52, 68, 94, 99, 113, 131, 133, 143, 169, 202, 208,
    213, 226, 242, 248, 282, 306, 350, 376, 409, 411, 460, 464, 468, 530,
    534, 569, 598, 609, 615, 706, 713, 760, 809, 812, 815, 818, 820, 823,
    826, 834, 839, 841, 842, 844, 849, 851, 858, 861, 869, 879, 884,
    890, 892, 936, 937,
})
# GO exposes IV buckets, not the precise 14/15 individual values. Protect
# every two-perfect-stat spread on wider reviews to cover every untagged 98.
HIGH_IV_GUARD = ("0-3attack,0-3defense", "0-3attack,0-3hp", "0-3defense,0-3hp")
# A Pokémon with any positive stat is not an exact 0/0/0 collectible.
NUNDO_GUARD = "1-attack,1-defense,1-hp"


def _species_term(filters, dex):
    return "&".join([*filters, clear.format_dex_ranges(sorted(dex))]) if dex else EMPTY


def _spent_or_species(species):
    return "traded," + clear.format_dex_ranges(sorted(species)) if species else "traded"


def _families(numbers, children):
    linked = {}
    for parent, kids in children.items():
        for child in kids:
            linked.setdefault(parent, set()).add(child)
            linked.setdefault(child, set()).add(parent)
    seen = set(numbers)
    stack = list(seen)
    while stack:
        new = linked.get(stack.pop(), set()) - seen
        seen.update(new)
        stack.extend(new)
    return seen


@dataclass
class ReviewPolicy:
    receiver: str
    names: dict[int, str]
    low_raid: set[int]
    safe_species: set[int]
    lucky: set[int]
    hundo: set[int]
    priority: set[int]
    filters: tuple[str, ...]
    donor_filters: tuple[str, ...]
    donors: tuple[str, ...] = ()
    # None is reserved for synthetic or explicitly supplied policies. Loaded
    # fleet policies always use form metadata from the game master.
    living_forms: dict[int, tuple[str, ...]] | None = None
    living_labels: dict[int, set[str]] | None = None
    registered_both: set[int] | None = None
    # Species whose low-IV copies may go once they are already traded: every
    # released species except rare collectibles, PvP candy, the curated
    # return/pseudo pools and configured keeps. None keeps the old strict list.
    # Garbage: species with no raid, Mega, Max or notable PvP use. SafeTrash
    # shows only these; LocalTrade shows only the rest. None keeps the older
    # strict lists (synthetic test policies).
    trash_species: set[int] | None = None
    # The receiver additionally keeps its return-pool families, which it
    # spends as trade currency. None means the same as trash_species.
    receiver_trash_species: set[int] | None = None
    registered_lucky: frozenset[int] = frozenset()

    def favorites(self, device, anchored_dex=None):
        device = trading_accounts.account_for(device)
        filters = tuple(dict.fromkeys((*self.filters,"!4*")))
        if device == self.receiver:
            if self.living_forms is not None:
                forms = _living_form_guards(self)
                filters = tuple(dict.fromkeys((*filters,"!#dontrade")))
            else:
                anchors = load_verified_anchors(device) if anchored_dex is None else anchored_dex
                forms = dict.fromkeys(anchors, ())
            # The receiver keeps a verified specimen. safe_species already
            # excludes families targeted for returns, raids, Max, candy or PvP.
            if self.trash_species is None:
                trash = self.safe_species
            else:
                trash = self.trash_species if self.receiver_trash_species is None else self.receiver_trash_species
            return {
                # IVs reroll on every trade: below 98% only the species matters.
                STRICT: _scoped_species_term((*filters, *(HIGH_IV_GUARD if self.trash_species is not None
                                                          else ("0*,1*,2*",))), trash, forms),
                RAID_REVIEW: _scoped_species_term((*filters, *HIGH_IV_GUARD), self.low_raid, forms),
            }

        # GO evaluates each comma-separated OR clause within its ampersand
        # intersection. Already-traded specimens cannot fill any inbound gap.
        safe = self.safe_species
        donor_review = tuple(f for f in filters if f.casefold() != "!costume")
        donor_filters = tuple(dict.fromkeys((*self.donor_filters,"!4*")))
        low_iv = (*donor_review, "0*,1*,2*")
        if self.trash_species is None:
            free = safe - self.lucky - self.hundo - self.priority
            if self.registered_both is not None:
                free &= self.registered_both
            strict = _species_term((*low_iv, _spent_or_species(free)), safe)
        else:
            # Only garbage species. A traded copy can never be traded again,
            # so it goes. An untraded one must be nearby and have a verified
            # Lucky registration on the receiver. Hold priority targets and
            # unknown registrations. A trade rerolls IVs.
            trash = self.trash_species
            free = trash.intersection(self.registered_lucky) - self.lucky - self.priority
            # Any IV short of 98%: a trade rerolls IVs, so a garbage species is
            # garbage at every IV. Two-perfect-stat spreads stay out until appraised.
            strict = _species_term((*donor_review, *HIGH_IV_GUARD, "traded,distance-100",
                                    _spent_or_species(free)), trash)
        return {
            STRICT: strict,
            # This donor-only manual utility view can additionally include a
            # receiver's Lucky or Perfect Dex gap.
            RAID_REVIEW: _species_term((*donor_review, *HIGH_IV_GUARD), self.low_raid - self.priority),
            LUCKY_TRADES: _species_term(donor_filters, self.lucky),
            HUNDO_TRADES: _species_term(donor_filters, self.hundo),
            PRIORITY_TRADES: _species_term(donor_filters, self.priority),
        }


REGIONAL = {"ALOLA":"alola", "ALOLAN":"alola", "GALARIAN":"galar", "HISUIAN":"hisui", "PALDEA":"paldea"}
PREFERRED = "4*,#98Plus"
KEPT = "#KeepOne"
RETAINED = f"{PREFERRED},{KEPT}"
ORDINARY_FORM = "!costume&!background"
EVENT_FORMS = "costume,background"


def living_form_catalog(path, numbers):
    """Native search scopes for non-costumed forms. Empty means protect every copy."""
    records = json.loads(path.read_text())
    known = {}
    for row in records:
        key = re.fullmatch(r"FORMS_V(\d+)_POKEMON_.+", row.get("templateId", ""))
        if key is None:
            continue
        dex = int(key[1])
        spec = row["data"]["formSettings"]
        if dex not in numbers:
            continue
        species = spec["pokemon"]
        forms = [form["form"].removeprefix(species + "_") for form in spec.get("forms", [])]
        # The GO costume search misses some special versions (Armored Mewtwo,
        # for example). Never treat an unexplained form as ordinary stock.
        if not forms or len(forms) > 1 and any(f != "NORMAL" and f not in REGIONAL for f in forms):
            known[dex] = ()
        elif len(forms) == 1:
            known[dex] = (ORDINARY_FORM,)
        else:
            regions = list(dict.fromkeys(REGIONAL[f] for f in forms if f in REGIONAL))
            scopes = [ORDINARY_FORM + "&" + "&".join("!" + r for r in regions)] if "NORMAL" in forms else []
            known[dex] = tuple([*scopes, *(ORDINARY_FORM + "&" + r for r in regions)])
    if not known:
        raise pokemon_fleet.FleetError("Cannot verify receiver living Dex without game-master form metadata")
    return {dex: known.get(dex, ()) for dex in sorted(numbers)}


def living_journal(account):
    return config_paths.state_dir() / "storage-review" / "living-keepers" / (quote(account, safe="") + ".jsonl")


def living_evidence(account):
    path = living_journal(account)
    result = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row["account"] != account:
                raise pokemon_fleet.FleetError(f"{account}: living keeper evidence belongs to another trainer")
            result[(row["dex"],row["scope"])] = row
    return result


def retained_cp_query(scope, pairs, predicate=RETAINED):
    """Apply one distinct CP per species. Then each visible CP identifies only its assigned Dex."""
    if not isinstance(pairs,(list,tuple)) or any(not isinstance(p,(list,tuple)) or len(p) != 2 for p in pairs):
        return None
    dexes = [d for d, cp in pairs]
    cps = [cp for d, cp in pairs]
    if (len(dexes) < 2 or len(set(dexes)) != len(dexes) or len(set(cps)) != len(cps)
            or any(type(d) is not int or not 1 <= d <= 1025 for d in dexes)
            or any(type(cp) is not int or not 10 <= cp <= 99999 for cp in cps)):
        return None
    clauses = [f"!{dex},cp{cp}" for dex,cp in pairs]
    return _species_term((scope, predicate, *clauses), dexes)


def verified_living_row(row, dex, scope):
    state = row.get("status")
    if state == "retained singleton":
        day = row.get("singleton_tag_day", "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",day) or day > row.get("at","")[:10]:
            return False
        if row.get("method") != "native species CP clauses":
            return row.get("count",0) > 0 and row.get("query") == f"{dex}&{scope}&#dontrade&{dex}"
    if row.get("method") == "native species CP clauses":
        pairs = row.get("pairs", [])
        predicate = {"retained":RETAINED,"tagged":KEPT,"retained singleton":"#dontrade"}.get(state)
        term = retained_cp_query(scope, pairs, predicate) if predicate else None
        if (term is None or row.get("query") != term or row.get("count") != 1
                or (dex,row.get("observed_cp")) not in map(tuple,pairs)
                or not Path(row.get("shot","")).is_file()):
            return False
        if state == "tagged":
            # The CP screenshot proves each chosen individual. Independently
            # confirm that the unconstrained native population reconciled.
            sole = row.get("owned") == 1
            population = "!#KeepOne" if sole else "#KeepOne"
            return (row.get("group_total") == len(pairs)
                    and row.get("population_after") == (0 if sole else len(pairs))
                    and row.get("population_query") == _species_term((scope,population),[d for d,cp in pairs]))
        return True
    if row.get("status") == "preferred" and row.get("method") == "matched hundo CP":
        group = row.get("group", ())
        return (scope == ORDINARY_FORM and dex in group and row.get("count") == 1
                and row.get("historical_cp") == row.get("observed_cp") and row.get("observed_cp",0) >= 10
                and row.get("query") == _species_term((scope, "4*"), group)
                and Path(row["shot"]).is_file())
    expected = {"preferred": (PREFERRED,), "kept": (KEPT,"#KeepOne,#Keep One"),
                "retained": (RETAINED,), "tagged": ("#KeepOne",)}
    return (row.get("count", 0) > 0 and row.get("query")
            in {f"{dex}&{scope}&{expr}&{dex}" for expr in expected.get(row.get("status"), ())})


def verified_living_species(account, policy):
    evidence = living_evidence(account)
    # A number-only public review is safe only when every distinct form
    # could survive that review. A currently absent alternate form could be
    # caught tomorrow; requiring affirmative proof also protects that arrival.
    return {d for d, scopes in policy.living_forms.items() if scopes and
            all(verified_living_row(evidence.get((d, scope), {}), d, scope) for scope in scopes)}


def _living_form_guards(policy):
    """Map an eligible Dex to the extra native clauses needed to retain its unproven forms."""
    evidence = living_evidence(policy.receiver)
    guards = {}
    for dex, scopes in policy.living_forms.items():
        proven = {s for s in scopes if verified_living_row(evidence.get((dex,s),{}), dex, s)}
        if not proven:
            continue
        regional = {s: s.split("&")[-1] for s in scopes if s.split("&")[-1] in REGIONAL.values()}
        if proven.difference(regional):
            # Ordinary is retained. Each unproven regional form of this
            # number must independently fail an intersection clause.
            guards[dex] = tuple(f"!{dex},!{region}" for scope, region in regional.items() if scope not in proven)
        else:
            # Only named regional forms have positive evidence; the ordinary
            # specimen (including a first future catch) stays out.
            guards[dex] = (",".join([f"!{dex}", *(region for scope,region in regional.items() if scope in proven)]),)
    return guards


def _scoped_species_term(filters, allowed, forms):
    eligible = sorted(forms.keys() & allowed)
    return _species_term((*filters, *(clause for dex in eligible for clause in forms[dex])), eligible)


def receiver_outgoing_filter(policy):
    guards = ("!costume", "!background", "!#KeepOne", "!#98Plus", "!#dontrade", "!4*")
    forms = _living_form_guards(policy)
    return _scoped_species_term(guards, forms.keys(), forms)


def load_policy(clear_path: Path, cfg: dict) -> ReviewPolicy:
    settings = clear.load_clear_config(clear_path)
    receiver = cfg.get("receiver")
    if not receiver:
        raise pokemon_fleet.FleetError("Manual storage lists require the receiver's Lucky and Perfect audits")
    species_file = config_paths.find_config(settings["species_csv"], clear_path.parent, "species catalog")
    gd = raid_gamedata.load()
    names, children, special = clear.load_evolution_graph(species_file, gd)
    released = {int(row["dex"]) for row in gd.pokemon.values()}
    ranks = raid_meta.rankings()
    if not children or not released or any(not ranks.get(t) for t in raid_gamedata.TYPES):
        raise pokemon_fleet.FleetError("Incomplete evolution or raid data; cannot verify manual storage lists")
    raid, mega = clear._raid_protection_sets(gd=gd, children=children)
    if not raid or not mega:
        raise pokemon_fleet.FleetError("Missing raid or Mega protection data")
    # Keep the strongest Max attack and defensive stat lines of each type,
    # including forms and evolutions outside the normal raid damage ranking.
    candidates = list(gd.pokemon.values())
    protect = set(MAX_CANDY) | set(raid) | mega | special
    for kind in raid_gamedata.TYPES:
        typed = [row for row in candidates if kind in row["types"] and not row["legendary"]]
        for score in (lambda row: row["stats"][0], lambda row: row["stats"][1] * row["stats"][2]):
            ordered = sorted(typed, key=score, reverse=True)
            # Count distinct species so regional forms cannot crowd out an entire line.
            protect.update(list(dict.fromkeys(row["dex"] for row in ordered))[:10])
    protect.update(settings.get("extra_exclude_dex", ()))
    protect.update(cfg.get("exclude_return_dex", collect_trades.DEFAULT_EXCLUDE_RETURN_DEX))
    priority = set(cfg.get("collect_all_dex", ()))
    protect.update(priority)
    low_raid = released.intersection(names) - _families(protect, children)
    low_raid -= _families(clear.RARE_COLLECTIBLE_DEX, children)
    # Keep established ordinary trade pools and valuable PvP candy on strict lists.
    valuable = clear.parse_dex_ranges(collector_day.PSEUDO_DEX)
    for tier in cfg.get("return_dex_tiers") or collect_trades.DEFAULT_CURATED_RETURN_TIERS:
        valuable.update(clear.parse_dex_ranges(tier))
    ordinary = {clear.normalize_species_token(name) for family in trade_plan.families(gd, expanded=True)
                for name in family.species}
    valuable.update(dex for dex, name in names.items() if clear.normalize_species_token(name) in ordinary)
    safe = low_raid - _families(valuable | clear.NOTABLE_PVP_DEX, children)
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    if receiver not in fleet.devices:
        raise pokemon_fleet.FleetError(f"Manual storage receiver {receiver!r} is not in the fleet")
    gaps = collector_day.LuckyGaps.load(clear_path, receiver, fleet.devices[receiver].identifier)
    try:
        votes = config_paths.find_config(settings.get("lucky_votes"), clear_path.parent, "Lucky Pokédex observations")
    except config_paths.ConfigPathError:
        votes = None
    report = config_paths.find_config(settings["lucky_report"], clear_path.parent, "Lucky Pokédex report")
    lucky_status = dex_registration.lucky_status(report, votes, receiver, fleet.devices[receiver].identifier)
    perfect_status = dex_registration.perfect_status(cfg)
    lucky = lucky_status.missing - gaps.registered
    hundo = perfect_status.missing
    registered_both = perfect_status.registered & (lucky_status.registered | gaps.registered)
    filters, _ = clear.merge_safety_filters(settings.get("safety_filters", ()))
    filters, _ = clear.merge_safety_filters(filters, [
        "!xs", "!xl", "!buddy1-5", "!purified", "!year2016-2021", "!@special", NUNDO_GUARD,
    ])
    donor, _ = clear.merge_safety_filters(cfg.get("donor_filters", ()), collect_trades.REQUIRED_DONOR_FILTERS)
    donor, _ = clear.merge_safety_filters(donor, ["!4*", "!#KeepOne", "!buddy1-5", NUNDO_GUARD, *HIGH_IV_GUARD])
    donor = [f for f in donor if f.casefold() not in {"!costume", "!#keep one", "!keep one"}]
    donors = cfg.get("storage_review_donors")
    if donors is None:
        donors = list(dict.fromkeys(n for pair in cfg.get("guaranteed_proposal_pairs", ()) for n in pair if n != receiver))
    if (not isinstance(donors, list) or any(not isinstance(n, str) or not n or n == receiver for n in donors)):
        raise pokemon_fleet.FleetError("storage_review_donors must name donor accounts distinct from the receiver")
    forms = living_form_catalog(raid_gamedata.CACHE_DIR / "pokeminers-latest.json", set(names))
    family_names = {dex:{clear.normalize_species_token(names[n]) for n in _families({dex},children) if n in names}
                    for dex in forms}
    curated = clear.parse_dex_ranges(collector_day.PSEUDO_DEX)
    for tier in cfg.get("return_dex_tiers") or collect_trades.DEFAULT_CURATED_RETURN_TIERS:
        curated.update(clear.parse_dex_ranges(tier))
    kept = set(settings.get("extra_exclude_dex", ())) | set(
        cfg.get("exclude_return_dex", collect_trades.DEFAULT_EXCLUDE_RETURN_DEX)) | priority
    # low_raid already leaves out raid, Mega, Max, candy, rare and configured
    # families; PvP families go too. What remains is garbage (Nymble, Bidoof),
    # never a Scorbunny or a Magikarp (user, 23 Nov 2026).
    garbage = low_raid - _families(clear.NOTABLE_PVP_DEX | kept, children)
    receiver_garbage = garbage - _families(curated, children)
    return ReviewPolicy(receiver, names, low_raid, safe, lucky, hundo, priority, tuple(filters), tuple(donor),
                        tuple(donors), forms, family_names, registered_both, garbage, receiver_garbage,
                        registered_lucky=lucky_status.registered)


def _anchor_path(device, predicate=PREVIOUS_ANCHOR):
    folder = {PREVIOUS_ANCHOR:"exact-retained", SPACED_ANCHOR:"exact-retained-keep-one",
              TRANSITION_ANCHOR:"exact-retained-keepone", ANCHOR:"retained-keepone"}[predicate]
    return config_paths.state_dir() / "storage-review" / folder / (quote(device, safe="") + ".json")


def _matches_anchor_proof(dex, proof, predicate):
    if proof.get("query") == f"{dex}&{predicate}":
        return True
    # The individual native tag check independently proves this retained
    # specimen. Record the narrower query that actually ran on the phone.
    tag = OLD_KEEP_ONE if predicate == SPACED_ANCHOR else KEEP_ONE
    return (predicate in (ANCHOR, SPACED_ANCHOR, TRANSITION_ANCHOR) and proof.get("count") == 1
            and proof.get("query") == f"{dex}&#{tag}&{dex}")


def _evidence(device, predicate):
    path = _anchor_path(device, predicate)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("device") != device or data.get("filter") != predicate:
        raise pokemon_fleet.FleetError(f"{device}: anchor evidence belongs to a different device or query")
    return {dex: proof for dex, proof in data["checked"].items()
            if _matches_anchor_proof(dex, proof, predicate)}


def load_verified_anchors(device):
    device = trading_accounts.account_for(device)
    checked = _evidence(device, PREVIOUS_ANCHOR)
    checked.update(_evidence(device, SPACED_ANCHOR))
    checked.update(_evidence(device, TRANSITION_ANCHOR))
    checked.update(_evidence(device, ANCHOR))
    direct = {int(dex) for dex, proof in checked.items() if proof["count"] > 0}
    audit_day, perfect = _held_perfect(device)
    historical = _tagged_singletons(device)
    for dex in perfect:
        historical[dex] = max(historical.get(dex, ""), audit_day)
    # If a later retained check found zero, the older audit is superseded.
    return direct | {d for d, day in historical.items()
                     if str(d) not in checked or checked[str(d)]["day"] < day}


def _tagged_singletons(device):
    """Join an exact singleton scan to its verified dontrade tagging receipts."""
    state = config_paths.state_dir()
    folder = state / "living-dex"
    name = quote(device, safe="")
    scan = folder / f"{name}-scan.json"
    log = folder / f"{name}-tagged.jsonl"
    if not scan.is_file() or not log.is_file():
        return {}
    snapshot = json.loads(scan.read_text())
    scanned = datetime.fromisoformat(snapshot["updated_at"]).astimezone()
    intervals = {}
    for block, rows in snapshot["blocks"].items():
        lo, hi = map(int, block.split("-"))
        for start, end in rows:
            if not (0 < lo <= start <= end <= hi):
                return {}
            token = str(start) if start == end else f"{start}-{end}"
            intervals[token] = (start, end)
    previous_end = 0
    for start, end in sorted(intervals.values()):
        if start <= previous_end:
            return {}
        previous_end = end

    # A change in stock between the singleton scan and its collective
    # count/zero-untagged receipt could leave the total the same by coincidence.
    mutations = []
    for source in ("trade", "clear-pokemon"):
        path = state / source / f"history-{scanned.date()}.jsonl"
        if path.exists():
            for line in path.read_text().splitlines():
                event = json.loads(line)
                if event.get("device") == device or device in event.get("pair", ()):
                    mutations.append(datetime.fromisoformat(event["at"]).astimezone())
    confirmed = {}
    for line in log.read_text().splitlines():
        entry = json.loads(line)
        if entry.get("device") != device or entry.get("tag") != "dontrade" or entry.get("remaining_after") != 0:
            continue
        tagged = datetime.fromisoformat(entry["at"]).astimezone()
        if tagged < scanned or tagged.date() != scanned.date() or any(scanned <= t <= tagged for t in mutations):
            continue
        query = entry["plain"]
        tokens = query.split(",")
        if (entry.get("query_chars") != len(query) or entry.get("expected") != len(tokens)
                or len(set(tokens)) != len(tokens) or not all(t in intervals for t in tokens)):
            continue
        for token in tokens:
            start, end = intervals[token]
            if start == end:
                confirmed[start] = tagged.date().isoformat()
    return confirmed


def _held_perfect(device):
    audits = config_paths.state_dir() / "perfect-audit"
    for summary_path in sorted(audits.glob("*/summary.json"), reverse=True):
        folder = summary_path.parent
        summary = json.loads(summary_path.read_text())
        if summary.get("receiver") != device or summary.get("date") != folder.name:
            continue
        counts_file = folder / "storage-counts.json"
        if not counts_file.is_file():
            break
        counts = json.loads(counts_file.read_text())
        positive = {int(d): n for d, n in counts.items() if d.isdecimal() and type(n) is int and n > 0}
        if (len(positive) != summary.get("storage_perfect_species")
                or sum(positive.values()) != summary.get("storage_perfect_individuals")
                or counts.get("total") != sum(positive.values())):
            break
        return folder.name, {d for d in positive if (folder / f"stock-{d:03d}&4star.jpg").is_file()}
    return "", set()


def _checkpoint(device, checked, *, predicate):
    # A running phone may finish a request after another phone has reloaded
    # this module. Never infer a receipt's predicate from mutable globals.
    if any(not _matches_anchor_proof(dex, proof, predicate) for dex, proof in checked.items()):
        raise pokemon_fleet.FleetError(f"{device}: retained receipts contain a different query")
    path = _anchor_path(device, predicate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        json.dump({"device": device, "filter": predicate, "checked": checked}, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


def _prior_candidates(device, policy):
    # Historical singleton records can combine a range of unknown species into
    # one result. Only singleton Dex tokens identify an exact past anchor.
    living = config_paths.state_dir() / "living-dex" / f"{device}-tagged.jsonl"
    candidates = set()
    # The original proof accepted any tag, including expendable trade tags.
    # It provides a search hint only; every candidate needs current exact proof.
    old = config_paths.state_dir() / "storage-review" / "anchors" / (quote(device, safe="") + ".json")
    if old.exists():
        legacy = json.loads(old.read_text())
        if legacy.get("device") == device and legacy.get("filter") == "favorite,lucky,4*,#":
            candidates.update(int(d) for d, p in legacy["checked"].items() if p["count"] > 0)
    if living.exists():
        for line in living.read_text().splitlines():
            record = json.loads(line)
            if record.get("device") == device and record.get("tag") == "dontrade" and record.get("remaining_after") == 0:
                candidates.update(int(t) for t in record["plain"].split(",") if t.isdecimal())
    if device == policy.receiver:
        files = sorted((config_paths.state_dir() / "perfect-audit").glob("*/storage-counts.json"))
        if files:
            counts = json.loads(files[-1].read_text())
            candidates.update(int(d) for d, n in counts.items() if d.isdecimal() and int(n) > 0)
    return candidates


async def _grid_names(phone, term, pages):
    count, image, boxes, _ = await clear._apply_and_verify_query(phone, term)
    labels = Counter()
    previous = None
    for page in range(pages):
        grid = clear.sample_visible_species(image, boxes, limit=18)
        if not count or previous == grid:
            break
        labels.update(clear.normalize_species_token(label) for label in grid)
        if count <= len(clear.safe_grid_tiles(image, boxes)) or page + 1 == pages:
            break
        previous = grid
        x = int(image.width * .5)
        await phone.swipe([x, int(image.height * .82)], [x, int(image.height * .34)], image, .5, scroll=True)
        await asyncio.sleep(.4)
        image, boxes = await phone.read()
    return labels


async def verify_anchors(phone, policy, *, limit=24, candidates=None, pages=12):
    """Read candidate and retained grids, then prove an excluded exact-species representative."""
    account = trading_accounts.account_for(phone.name)
    if account != policy.receiver:
        return set()
    predicate = ANCHOR
    checked = _evidence(account, predicate)
    today = datetime.now().astimezone().date().isoformat()
    verified = load_verified_anchors(account)
    possible = policy.low_raid - verified - {int(d) for d, p in checked.items() if p["day"] == today}
    if not possible or limit <= 0:
        return verified
    if candidates is None:
        filters = (*policy.filters, "0*,1*,2*")
        labels = await _grid_names(phone, _species_term(filters, possible), pages)
        retained = await _grid_names(phone, _species_term((predicate,), possible), pages)
        hints = {d for d in possible if labels[clear.normalize_species_token(policy.names[d])]}
        retained_hints = {d for d in possible if retained[clear.normalize_species_token(policy.names[d])]}
        prior = _prior_candidates(account, policy) & possible
        protected = prior | retained_hints
        overlap = hints & protected
        candidates = sorted(possible, key=lambda d: (d not in overlap, d not in hints and d not in protected,
                            d not in policy.safe_species, d not in hints, d not in protected, str(d) in checked,
                            -labels[clear.normalize_species_token(policy.names[d])], d))
    else:
        candidates = list(dict.fromkeys(candidates))
    pending = [d for d in candidates if d in possible][:limit]
    if not pending:
        return verified
    image, boxes = await clear.starter.open_storage(phone)
    for dex in pending:
        # Verify all operators against native text, then separately verify the
        # new leading Dex in the grid. Vision often reads commas as low quotes.
        term = f"{dex}&{predicate}"
        enter = clear._enter_query_ios if phone.ios else clear._enter_query_android
        await enter(phone, term, image, boxes, require_exact=True)
        previous = None
        for attempt in range(6):
            image, boxes = await phone.read()
            count = _filtered_count(image, boxes)
            if count is not None and count == previous:
                if _anchor_grid_count(boxes, image.height, dex) != count:
                    # Narrow screens can scroll the Dex off the left edge.
                    # Reopen this actual result's editor and verify its full
                    # query, then recover the same result count after Done.
                    image, boxes = await _verify_grid_editor(phone, image, boxes, term, count)
                break
            previous = count
            await asyncio.sleep(.5)
        else:
            raise clear.ClearSafetyError(f"{phone.name}: retained count did not settle for #{dex}")
        checked[str(dex)] = {"query": term, "count": count, "day": today, "at": datetime.now(timezone.utc).isoformat(),
                             "phone": getattr(phone, "transport_name", phone.name)}
        _checkpoint(account, checked, predicate=predicate)
        print(f"[review] {phone.name}: #{dex} has {count} retained copies", flush=True)
    return load_verified_anchors(account)


def _anchor_grid_count(boxes, height, dex):
    for box in boxes:
        if .12 * height <= box.center_y < .23 * height and re.search(
                rf"(?<!\d){dex}\s*&\s*favorite", box.text, re.I):
            return clear.result_count(boxes, height)
    return None


def _filtered_count(image, boxes):
    if any(trade_saved_search.heading(boxes, name) for name in ("see more", "see less", "recent", "recommended")):
        return None
    if not clear.starter.storage_open(boxes) or clear._active_filter_box(image, boxes) is None:
        return None
    return clear.result_count(boxes, image.height)


async def _verify_grid_editor(phone, image, boxes, term, count):
    field = clear._storage_search_field(image, boxes)
    await phone.tap_box(field, image)
    await asyncio.sleep(.7)
    actual = await trade_saved_search.editor_text(phone)
    if not actual or actual.casefold() != term.casefold():
        raise clear.ClearSafetyError(f"{phone.name}: reopened search did not contain {term!r}")
    if phone.ios:
        await clear._ios_tap_done(phone)
    else:
        await clear.trade_local.submit(phone)
    def submitted_count(i, b):
        # iOS Done sometimes leaves Favorites visible with the filtered count
        # still in the storage header. The pre-edit counts came from the grid.
        return (clear.starter.storage_open(b) and clear._active_filter_box(i, b) is not None
                and clear.result_count(b, i.height) == count)
    ok, image, boxes = await clear.trade_local.settle(phone, submitted_count, timeout=8)
    if not ok:
        raise clear.ClearSafetyError(f"{phone.name}: retained count changed after reopening {term!r}")
    if _filtered_count(image, boxes) is None:
        return await clear.starter.open_storage(phone)
    return image, boxes
