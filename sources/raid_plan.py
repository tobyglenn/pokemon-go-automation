#!/usr/bin/env python3
"""Where one account should spend candy and XL candy to build raid attackers.

Input is a storage scan (`raid_inventory`) and the computed rankings
(`raid_meta`).  For every attacking type the plan answers four questions:

1. **What is best?**  The type's top Megas and top non-Megas, rated with their
   best moves at level 40 with perfect IVs.
2. **What do I have?**  Each opened individual's level and IVs, solved from
   CP, HP, the power-up price and the 3*/4* storage filters -- shown as a range
   when the screen did not pin it down.  A Mega counts only if this account
   can actually Mega Evolve it: the species' Mega Energy cell must exist, and
   the energy must cover the next evolution unless a Mega level makes it free.
3. **Where should candy go?**  For each owned individual, how far its own
   candy and XL candy take it (per-species cost tables included, which is what
   rules out Eternatus), and how much that moves its rating.  The pick is the
   individual that ends up strongest, not the one with the most candy.
4. **What is missing?**  Top attackers this account does not own at all.

Ratings are relative within a type: "84%" means 84% of that type's best
attacker at level 40 with perfect IVs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import raid_gamedata, raid_math, raid_meta
from .raid_gamedata import TYPES, GameData
from .raid_inventory import Individual, SpeciesScan

# A power-up that moves the rating by less than this is not worth a mention.
MIN_GAIN = 0.01
# Below this share of the type's #1 an attacker is not worth candy at all,
# however it compares with the rest of the account.
MIN_USEFUL = 0.55
# Normal is super effective against Fighting alone, so it is never where
# scarce XL should go first; it is still listed per type.
SKIP_SUMMARY = {"Normal"}
SHOWN_META = 6
SHOWN_SPENDS = 2

_IV_BANDS = {"4*": (45, 45), "3*": (37, 44), "<3*": (0, 36), "?": (0, 45)}


# --- what an individual is ---------------------------------------------------


@dataclass
class Owned:
    """One opened individual, with what could be solved about it."""

    scan: SpeciesScan
    individual: Individual
    form: dict
    shadow: bool
    purified: bool
    lucky: bool
    fits: list[raid_math.Fit]
    fast: list[str]
    charged: list[str]

    @property
    def level_range(self) -> tuple[float, float]:
        levels = [f.level for f in self.fits]
        return min(levels), max(levels)

    @property
    def iv_range(self) -> tuple[float, float]:
        values = [f.percent for f in self.fits]
        return min(values), max(values)

    @property
    def conservative(self) -> raid_math.Fit:
        """The weakest reading consistent with the screen: never oversell."""
        return min(self.fits, key=lambda f: (sum(f.ivs), f.level))

    def describe(self) -> str:
        low, high = self.level_range
        level = f"L{low:g}" if low == high else f"L{low:g}-{high:g}"
        iv_low, iv_high = self.iv_range
        ivs = f"{iv_low:.0f}%" if iv_low == iv_high else f"{iv_low:.0f}-{iv_high:.0f}%"
        tags = []
        if self.shadow:
            tags.append("shadow")
        if self.purified:
            tags.append("purified")
        if self.lucky:
            tags.append("lucky")
        label = self.form["name"]
        extra = f" ({', '.join(tags)})" if tags else ""
        return f"{label}{extra} CP {self.individual.cp} {level} {ivs}"


def _forms(gd: GameData, scan: SpeciesScan, individual: Individual) -> list[dict]:
    dex = int(scan.search)
    forms = [p for p in gd.pokemon.values() if p["dex"] == dex]
    if individual.types_line:
        wanted = {t.title() for t in individual.types_line.split("/") if t}
        typed = [p for p in forms if set(p["types"]) == wanted]
        forms = typed or forms
    return forms


def identify(gd: GameData, scan: SpeciesScan, individual: Individual) -> Owned | None:
    """Solve one individual, or None when nothing fits what the screen said."""
    if not individual.cp or not individual.hp:
        return None
    low, high = _IV_BANDS.get(individual.iv_band, (0, 45))
    forms = _forms(gd, scan, individual)
    if not forms:
        return None
    dex = forms[0]["dex"]
    by_variant = raid_math.levels_for_powerup(gd, dex, individual.dust_cost, individual.candy_cost)
    if individual.dust_cost and not by_variant:
        # A misread price must not silently become "any level".
        by_variant = {}
    shadow_known = individual.shadow
    if any("shadow bonus" in m.lower() for m in individual.moves):
        # A shadow's detail page carries a SHADOW BONUS card.
        shadow_known = True
    candidates: list[tuple[str, list[float] | None]] = []
    if by_variant:
        for variant, levels in by_variant.items():
            if variant == "shadow" and shadow_known is False:
                continue
            if variant != "shadow" and shadow_known is True:
                continue
            if variant == "lucky" and not individual.lucky:
                continue
            candidates.append((variant, levels))
    if not candidates:
        variant = "shadow" if shadow_known else ("lucky" if individual.lucky else "normal")
        # No price on screen: a maxed individual shows none, so the top
        # levels are as likely as any.
        candidates.append((variant, None))

    best: tuple[str, dict, list[raid_math.Fit]] | None = None
    for variant, levels in candidates:
        for form in forms:
            fits = raid_math.solve(gd, [form], individual.cp, individual.hp,
                                   levels=levels, min_iv_sum=low, max_iv_sum=high)
            if fits and (best is None or len(fits) < len(best[2])):
                best = (variant, form, fits)
    if best is None:
        return None
    variant, form, fits = best
    moves = [gd.move_by_name(name) for name in individual.moves]
    known = [m for m in moves if m is not None]
    return Owned(
        scan=scan, individual=individual, form=form,
        shadow=variant == "shadow" or bool(shadow_known),
        purified=variant == "purified", lucky=individual.lucky, fits=fits,
        fast=[m.key for m in known if m.fast and m.key in form["fast"]],
        charged=[m.key for m in known if not m.fast and m.key in form["charged"]],
    )


# --- the plan for one type ---------------------------------------------------


@dataclass
class Option:
    """One owned individual, as an attacker of one type.

    `now` and `reach` use the same moves -- the ones it knows when it knows a
    move of this type, otherwise the best it could be taught -- so the gain
    between them is what the candy buys and nothing else.
    """

    owned: Owned
    attacker: raid_math.Attacker
    now: raid_math.Rating
    needs_tm: bool
    reach_level: float
    reach_cost: raid_math.Cost
    reach: raid_math.Rating
    blocked: str = ""

    @property
    def gain(self) -> float:
        return self.reach.er - self.now.er


@dataclass
class TypePlan:
    attack_type: str
    top: raid_math.Rating
    meta: list[raid_math.Rating]
    owned_keys: set[str]
    options: list[Option] = field(default_factory=list)
    unavailable_megas: list[tuple[raid_math.Rating, str]] = field(default_factory=list)

    def pct(self, rating: raid_math.Rating | None) -> str:
        return f"{rating.er / self.top.er * 100:.0f}%" if rating else "-"

    def points(self, value: float) -> str:
        return f"{value / self.top.er * 100:+.0f} pts"

    def best_now(self, mega: bool) -> Option | None:
        """The strongest attacker of this type the account can field today."""
        ready = [o for o in self.options
                 if bool(o.attacker.mega) == mega and not o.blocked and not o.needs_tm]
        return max(ready, key=lambda o: o.now.er, default=None)

    def spends(self, mega: bool) -> list[Option]:
        """Owned individuals worth candy, strongest result first.

        Worth it means the candy moves it by at least MIN_GAIN, and the result
        beats, or comes close to, what the account can already field in that
        slot.  An individual that only becomes useful with a TM is listed when
        the result is better than anything usable today.
        """
        bar = self.bar(mega)
        useful = []
        for option in self.options:
            if bool(option.attacker.mega) != mega or option.blocked:
                continue
            if option.reach.er < self.top.er * MIN_USEFUL:
                continue
            # Candy that buys no level is not a spend; a TM-only gain is
            # reported apart, as a move to teach.
            if option.reach_level <= option.owned.conservative.level:
                continue
            if option.gain / self.top.er >= MIN_GAIN and option.reach.er >= bar * 0.97:
                useful.append(option)
        # One line per attacker: the individual that ends up strongest.
        best: dict[str, Option] = {}
        for option in sorted(useful, key=lambda o: o.reach.er, reverse=True):
            best.setdefault(option.attacker.key, option)
        return list(best.values())

    def bar(self, mega: bool) -> float:
        """The best this slot can do with no candy at all, a TM allowed."""
        return max((o.now.er for o in self.options
                    if bool(o.attacker.mega) == mega and not o.blocked), default=0.0)

    def candy_points(self, option: Option) -> float:
        """What the candy adds over the slot's no-candy best, in % of #1."""
        return (option.reach.er - self.bar(bool(option.attacker.mega))) / self.top.er

    def teach(self) -> list[Option]:
        """Attackers already built whose only problem is the moveset."""
        out: dict[str, Option] = {}
        bar = max((o.now.er for o in self.options if not o.needs_tm and not o.blocked),
                  default=0.0)
        for option in sorted(self.options, key=lambda o: o.now.er, reverse=True):
            if (option.needs_tm and not option.blocked
                    and option.now.er > bar * 1.03 and option.now.er >= self.top.er * MIN_USEFUL):
                out.setdefault(option.attacker.key, option)
        return list(out.values())


def mega_block(scan: SpeciesScan, attacker: raid_math.Attacker) -> str:
    """Why this account cannot field the Mega, or "" when it can."""
    if not scan.mega_shown:
        return "no Mega Energy cell on this account"
    # The price beside MEGA EVOLVE is not trusted on its own: a species with
    # two Megas shows two buttons, and a free evolution shows no number.  The
    # first-evolution price from the game master is the bar the energy has
    # to clear.
    cost = attacker.mega_first_cost
    energy = scan.mega_energy or 0
    if cost and energy < cost:
        return (f"first Mega Evolution costs {cost} energy, has {energy}; "
                "free or cheap if it has been Mega Evolved before")
    return ""


def plan_type(gd: GameData, attack_type: str, meta: Sequence[raid_math.Rating],
              owned: Sequence[Owned], pool: dict[str, raid_math.Attacker] | None = None) -> TypePlan:
    top = meta[0]
    plan = TypePlan(attack_type, top, list(meta), set())
    pool = pool or {a.key: a for a in raid_math.attackers(gd)}
    for mon in owned:
        variants = [pool.get(f"{mon.form['id']}:{'shadow' if mon.shadow else 'normal'}")]
        if not mon.shadow:
            # Shadows cannot Mega Evolve.
            variants += [a for k, a in pool.items()
                         if a.mega and k.startswith(mon.form["id"] + ":")]
        fit = mon.conservative
        level, cost = raid_math.reachable_level(
            gd, mon.form["dex"], fit.level, mon.scan.candy or 0, mon.scan.xl_candy or 0,
            shadow=mon.shadow, purified=mon.purified, lucky=mon.lucky,
        )
        for attacker in filter(None, variants):
            if attack_type not in (attacker.mega_types or attacker.types):
                continue
            # An unread move is any move; a read one is the one it knows.
            fast = mon.fast or None
            charged = mon.charged or None
            now = raid_math.best_rating(gd, attacker, attack_type, fit.level, fit.ivs,
                                        fast=fast, charged=charged)
            needs_tm = False
            if now is None:
                needs_tm = True
                fast = charged = None
                now = raid_math.best_rating(gd, attacker, attack_type, fit.level, fit.ivs)
                if now is None:
                    continue
            reach = raid_math.best_rating(gd, attacker, attack_type, level, fit.ivs,
                                          fast=fast, charged=charged)
            blocked = mega_block(mon.scan, attacker) if attacker.mega else ""
            plan.options.append(Option(mon, attacker, now, needs_tm, level, cost, reach, blocked))
            plan.owned_keys.add(attacker.key)
    for rating in meta:
        if not rating.attacker.mega:
            continue
        blocked = next((o.blocked for o in plan.options
                        if o.attacker.key == rating.attacker.key and o.blocked), "")
        if blocked:
            plan.unavailable_megas.append((rating, blocked))
    return plan


# --- the whole account ---------------------------------------------------------


@dataclass
class AccountPlan:
    device: str
    scans: list[SpeciesScan]
    owned: list[Owned]
    unsolved: list[tuple[SpeciesScan, Individual]]
    types: dict[str, TypePlan]

    @property
    def stardust(self) -> int | None:
        values = [s.stardust for s in self.scans if s.stardust]
        return max(values) if values else None


def build(device: str, scans: Iterable[SpeciesScan], gd: GameData | None = None,
          rankings: dict[str, list[raid_math.Rating]] | None = None) -> AccountPlan:
    gd = gd or raid_gamedata.load()
    rankings = rankings or raid_meta.rankings()
    scans = list(scans)
    owned: list[Owned] = []
    unsolved: list[tuple[SpeciesScan, Individual]] = []
    for scan in scans:
        for individual in scan.individuals:
            mon = identify(gd, scan, individual)
            if mon is None:
                unsolved.append((scan, individual))
            else:
                owned.append(mon)
    pool = {a.key: a for a in raid_math.attackers(gd)}
    types = {t: plan_type(gd, t, rankings[t], owned, pool) for t in TYPES if rankings.get(t)}
    return AccountPlan(device, scans, owned, unsolved, types)


# --- the report ----------------------------------------------------------------


def _cost_text(cost: raid_math.Cost) -> str:
    parts = []
    if cost.candy:
        parts.append(f"{cost.candy} candy")
    if cost.xl:
        parts.append(f"{cost.xl} XL")
    parts.append(f"{cost.dust // 1000}k dust")
    return ", ".join(parts)


def _why_stops(option: Option, gd: GameData) -> str:
    """Why the power-up stops where it does."""
    mon = option.owned
    if option.reach_level >= raid_math.MAX_LEVEL:
        return "reaches L50"
    to_fifty = raid_math.cost_between(gd, mon.form["dex"], option.reach_level, raid_math.MAX_LEVEL,
                                      shadow=mon.shadow, purified=mon.purified)
    left_xl = (mon.scan.xl_candy or 0) - option.reach_cost.xl
    left_candy = (mon.scan.candy or 0) - option.reach_cost.candy
    if option.reach_level >= 40:
        return f"stops at L{option.reach_level:g}: L50 needs {to_fifty.xl} more XL, {left_xl} left"
    return (f"stops at L{option.reach_level:g}: L40 needs "
            f"{to_fifty.candy} more candy, {left_candy} left")


def _option_lines(tp: TypePlan, option: Option, gd: GameData, label: str) -> list[str]:
    mon = option.owned
    moves = option.reach.moveset + (" (needs TM)" if option.needs_tm else "")
    if option.reach_level <= option.owned.conservative.level:
        action = "already as high as its candy allows"
    else:
        action = (f"-> L{option.reach_level:g} for {_cost_text(option.reach_cost)}: "
                  f"{tp.pct(option.now)} -> {tp.pct(option.reach)} ({tp.points(option.gain)})")
    return [
        f"  {label} {option.attacker.name}: {mon.describe()} {action}",
        f"         {moves}; {_why_stops(option, gd)}"
        f"; pools {mon.scan.candy} candy / {mon.scan.xl_candy} XL",
    ]


def report(plan: AccountPlan, gd: GameData | None = None) -> str:
    gd = gd or raid_gamedata.load()
    out: list[str] = []
    held = [s for s in plan.scans if s.owned]
    errors = [s for s in plan.scans if s.error]
    out.append(f"=== {plan.device} ===")
    dust = plan.stardust
    out.append(
        f"{len(plan.scans)} species scanned, {len(held)} held, "
        f"{len(plan.owned)} individuals solved"
        + (f", stardust {dust:,}" if dust else "")
    )
    if errors:
        out.append("  scan errors (re-run with --resume): "
                   + ", ".join(s.display for s in errors))
    if plan.unsolved:
        out.append("  could not solve (CP/HP/price disagree): " + ", ".join(
            f"{s.display} CP {i.cp}" for s, i in plan.unsolved))
    out.append("")

    summary: list[tuple[float, str]] = []
    for attack_type, tp in plan.types.items():
        out.append(f"-- {attack_type.upper()} " + "-" * (66 - len(attack_type)))
        for mega in (False, True):
            ranked = [r for r in tp.meta if bool(r.attacker.mega) == mega][:SHOWN_META]
            marks = " | ".join(
                ("*" if r.attacker.key in tp.owned_keys else "") + f"{r.attacker.name} {tp.pct(r)}"
                for r in ranked
            )
            out.append(f"  {'megas' if mega else 'meta '}: {marks}")
        for mega in (False, True):
            slot = "mega" if mega else "now "
            best = tp.best_now(mega)
            if best is None:
                out.append(f"  {slot}:  nothing usable today")
            else:
                out.append(f"  {slot}:  {best.attacker.name}: {best.owned.describe()}, "
                           f"{best.now.moveset} = {tp.pct(best.now)}")
        any_spend = False
        for mega in (False, True):
            for option in tp.spends(mega)[:SHOWN_SPENDS]:
                any_spend = True
                out.extend(_option_lines(tp, option, gd, "SPEND:"))
                if attack_type in SKIP_SUMMARY:
                    continue
                points = tp.candy_points(option)
                if points < MIN_GAIN:
                    continue
                summary.append((
                    points,
                    f"{attack_type:<9} {option.attacker.name} {option.owned.describe()} -> "
                    f"L{option.reach_level:g} ({_cost_text(option.reach_cost)}) = {tp.pct(option.reach)}"
                    + (" [needs TM]" if option.needs_tm else ""),
                ))
        for option in tp.teach()[:2]:
            out.append(f"  TM:    {option.attacker.name}: {option.owned.describe()} -> "
                       f"{option.now.moveset} would be {tp.pct(option.now)} with no candy")
        if not any_spend:
            weak = max((o for o in tp.options if not o.blocked), key=lambda o: o.reach.er,
                       default=None)
            if weak is not None and weak.reach.er < tp.top.er * MIN_USEFUL:
                out.append(f"  SPEND: nothing -- the best you own here ({weak.attacker.name}) "
                           f"tops out at {tp.pct(weak.reach)}; not raid-viable")
            else:
                out.append("  SPEND: nothing owned improves on what you can already field")
        for rating, why in tp.unavailable_megas[:3]:
            out.append(f"  locked: {rating.attacker.name} ({why})")
        missing = [r for r in tp.meta if not r.attacker.mega
                   and r.attacker.key not in tp.owned_keys][:4]
        if missing:
            out.append("  not owned: " + ", ".join(f"{r.attacker.name} {tp.pct(r)}" for r in missing))
        out.append("")

    out.append("-- BEST SPENDS ACROSS ALL TYPES " + "-" * 37)
    seen: set[str] = set()
    for gain, text in sorted(summary, reverse=True):
        if text in seen:
            continue
        seen.add(text)
        out.append(f"  +{gain * 100:3.0f} pts  {text}")
        if len(seen) >= 15:
            break
    if not summary:
        out.append("  none found")
    out.append("")
    out.append(
        "Ratings: % of the type's #1 (L40, perfect IVs, best moves); megas and "
        "non-megas are listed apart because a party fields one Mega. Levels/IVs are "
        "solved from CP, HP and the power-up price, conservatively when a range fits. "
        "Costs use this species' own candy and XL; stardust is shown, not budgeted."
    )
    return "\n".join(out)


# --- rare candy ----------------------------------------------------------------
#
# Rare Candy and Rare Candy XL turn into any species' candy, one for one.  They
# are only worth spending where the species' own pool runs out first, so an
# attacker the account can already max for free (Raichu with 6,000 candy) is
# never a target, however good it is.  What a rare candy buys is measured
# against the best the account can build in that slot *without* any, so a
# target only counts if it ends up stronger than the free build.

UNLIMITED = 10 ** 7
# A rare-candy target must end at least this close to the type's #1.
RARE_MIN_RESULT = 0.70
# Past a normal L40-50 run (296 XL, 360 for a shadow) a target is not
# finishable with rare candy: Eternatus needs 6,500.
RARE_XL_CAP = 400
RARE_CANDY_CAP = 400
# A gain smaller than this, in points of the type's #1, is not worth a mention.
RARE_MIN_PTS = 3.0


@dataclass
class RareTarget:
    attack_type: str
    option: Option
    rare: int            # Rare Candy needed (species-candy units, shadow x1.2)
    rare_xl: int         # Rare Candy XL needed to finish at L50
    with_rare: raid_math.Rating     # own XL after the Rare Candy
    with_rare_level: float
    maxed: raid_math.Rating         # L50 with both
    free_best: float     # best rating in the slot with no rare candy at all
    top: float

    def pts(self, rating: raid_math.Rating) -> float:
        return (rating.er - self.free_best) / self.top * 100

    def pct(self, rating: raid_math.Rating) -> float:
        return rating.er / self.top * 100


def _pools(scan: SpeciesScan) -> str:
    candy = "candy unread" if scan.candy is None else f"{scan.candy} candy"
    xl = "XL unread -- counted as 0, check it" if scan.xl_candy is None else f"{scan.xl_candy} XL"
    return f"pools {candy} / {xl}"


def rare_targets(gd: GameData, plan: AccountPlan) -> list[RareTarget]:
    out: list[RareTarget] = []
    for attack_type, tp in plan.types.items():
        for mega in (False, True):
            free_best = max((o.reach.er for o in tp.options
                             if bool(o.attacker.mega) == mega and not o.blocked), default=0.0)
            best: dict[str, RareTarget] = {}
            for o in tp.options:
                if bool(o.attacker.mega) != mega or o.blocked:
                    continue
                mon, fit = o.owned, o.owned.conservative
                own_candy, own_xl = mon.scan.candy or 0, mon.scan.xl_candy or 0
                flags = dict(shadow=mon.shadow, purified=mon.purified, lucky=mon.lucky)
                dex = mon.form["dex"]
                fast = mon.fast or None if not o.needs_tm else None
                charged = mon.charged or None if not o.needs_tm else None
                lvl1, cost1 = raid_math.reachable_level(gd, dex, fit.level, own_candy + UNLIMITED,
                                                        own_xl, **flags)
                cost2 = raid_math.cost_between(gd, dex, fit.level, raid_math.MAX_LEVEL, **flags)
                rare = max(0, cost1.candy - own_candy)
                rare_xl = max(0, cost2.xl - own_xl)
                if not rare and not rare_xl:
                    continue  # its own candy already maxes it: no rare candy needed
                if rare_xl > RARE_XL_CAP or rare > RARE_CANDY_CAP:
                    continue
                r1 = raid_math.best_rating(gd, o.attacker, attack_type, lvl1, fit.ivs,
                                           fast=fast, charged=charged)
                r2 = raid_math.best_rating(gd, o.attacker, attack_type, raid_math.MAX_LEVEL,
                                           fit.ivs, fast=fast, charged=charged)
                if r2 is None or r2.er < tp.top.er * RARE_MIN_RESULT or r2.er <= free_best * 1.01:
                    continue
                target = RareTarget(attack_type, o, rare, rare_xl, r1 or o.reach, lvl1, r2,
                                    free_best, tp.top.er)
                key = o.attacker.key
                if key not in best or r2.er > best[key].maxed.er:
                    best[key] = target
            out.extend(best.values())
    return out


def rare_report(plan: AccountPlan, gd: GameData | None = None) -> str:
    gd = gd or raid_gamedata.load()
    targets = [t for t in rare_targets(gd, plan) if t.attack_type not in SKIP_SUMMARY]
    out = [f"=== {plan.device}: where Rare Candy and Rare Candy XL go ===",
           "Only attackers whose own candy runs out before they are maxed, and only where",
           "the result beats the best you can build in that slot with no rare candy.",
           "'pts' = gain over that free build, as % of the type's #1. Rare candy counts",
           "are species-candy units (shadows cost 1.2x). Rare candy in the bag was not read.",
           ""]

    rc = [t for t in targets if t.rare and t.pts(t.with_rare) >= RARE_MIN_PTS]
    rc.sort(key=lambda t: t.pts(t.with_rare) / t.rare, reverse=True)
    out.append("-- RARE CANDY (gets it to L40; then its own XL takes it to "
               "the level shown) " + "-" * 4)
    for t in rc[:15]:
        m = t.option.owned
        out.append(f"  {t.attack_type:<9} {t.option.attacker.name:<28} {m.describe()}")
        out.append(f"            {t.rare:>3} Rare Candy -> L{t.with_rare_level:g} = "
                   f"{t.pct(t.with_rare):.0f}% (+{t.pts(t.with_rare):.0f} pts, "
                   f"{t.pts(t.with_rare) / t.rare * 100:.1f} pts/100)"
                   f"; {_pools(m.scan)}"
                   + (" [needs TM]" if t.option.needs_tm else ""))
    if not rc:
        out.append("  none: every attacker worth building can reach L40 on its own candy")
    out.append("")

    xl = [t for t in targets if t.rare_xl and t.pts(t.maxed) >= RARE_MIN_PTS]
    xl.sort(key=lambda t: t.pts(t.maxed) / t.rare_xl, reverse=True)
    out.append("-- RARE CANDY XL (to L50) " + "-" * 44)
    for t in xl[:15]:
        m = t.option.owned
        extra = f" + {t.rare} Rare Candy" if t.rare else ""
        out.append(f"  {t.attack_type:<9} {t.option.attacker.name:<28} {m.describe()}")
        out.append(f"            {t.rare_xl:>3} Rare Candy XL{extra} -> L50 = {t.pct(t.maxed):.0f}% "
                   f"(+{t.pts(t.maxed):.0f} pts, {t.pts(t.maxed) / t.rare_xl * 100:.1f} pts/100 XL)"
                   f"; {_pools(m.scan)}"
                   + (" [needs TM]" if t.option.needs_tm else ""))
    if not xl:
        out.append("  none")
    out.append("")

    out.append("-- BY TYPE: the one rare-candy target per type " + "-" * 23)
    for attack_type in plan.types:
        if attack_type in SKIP_SUMMARY:
            continue
        mine = [t for t in targets if t.attack_type == attack_type and t.pts(t.maxed) >= RARE_MIN_PTS]
        if not mine:
            out.append(f"  {attack_type:<9} none -- your free build is already as good as any rare-candy build")
            continue
        t = max(mine, key=lambda t: t.maxed.er)
        out.append(f"  {attack_type:<9} {t.option.attacker.name} ({t.option.owned.describe()}): "
                   f"{t.rare} RC + {t.rare_xl} RC XL -> {t.pct(t.maxed):.0f}% (+{t.pts(t.maxed):.0f} pts)")
    return "\n".join(out)
