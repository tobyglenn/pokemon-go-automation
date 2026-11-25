"""Per-phone buddy and gift work for the evening fleet coordinator."""
from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timedelta

from . import clear_pokemon, starter


async def heart_counts(phone):
    await phone.tap_first(starter.BUDDY_PORTRAIT, starter.buddy_profile, "the buddy profile")
    await asyncio.sleep(2)
    image, boxes = await phone.read()
    treats = battles = None
    for attempt in range(3):
        if treats is None:
            treats = starter.treat_hearts(image, boxes)
        row = starter.find_box(boxes, "battle together", "battle togeth")
        if row is not None:
            battles = starter.filled_hearts(image, row.center_y)
        if battles is not None and treats is not None:
            break
        if attempt < 2:
            x = int(image.width * .20)
            await phone.swipe([x, int(image.height * .80)], [x, int(image.height * .40)], image, .6, scroll=True)
            await asyncio.sleep(1.5)
            image, boxes = await phone.read()
    await phone.go_home()
    return treats, battles


async def finish_battles(phone):
    _, before = await heart_counts(phone)
    if before is None:
        return starter.Report("buddy battles", False, "could not read the Battle together row")
    for _ in range(max(0, 3 - before)):
        await starter.battle_step(phone)
        _, after = await heart_counts(phone)
        if after is None or after <= before:
            return starter.Report("buddy battles", False, f"could not verify progress beyond {before}/3")
        before = after
        if before >= 3:
            break
    return starter.Report("buddy battles", True, f"{before} verified Battle together hearts")


async def prepare_phone(spec, args, day):
    reports = []

    async def action(label, task):
        try:
            return await task
        except Exception as exc:
            reports.append(starter.Report(label, False, str(exc)))
            return None

    async def buddy(phone):
        return await starter.buddy_step(phone, args.berries)

    first = await action("buddy", starter.with_phone(spec, args, buddy))
    if first is not None:
        reports.append(first)
    battle = await action("buddy battles", starter.with_phone(spec, args, finish_battles))
    if battle is not None:
        reports.append(battle)
    gifts = await action("gifts", starter.run_phone(spec, args, {"gifts"}))
    reports.extend(gifts or [])
    # Gifting and battles use part of the feeding cooldown. Release the phone
    # between feedings and only repeat a feeding that advances the visible row.
    for _ in range(starter.TREAT_HEARTS_MAX):
        if date.today() != day:
            break
        counts = await action("buddy hearts", starter.with_phone(spec, args, heart_counts))
        if counts is None:
            break
        before = counts[0]
        if before is None:
            reports.append(starter.Report("buddy treats", False, "could not read the treat row"))
            break
        if before >= starter.TREAT_HEARTS_MAX:
            break
        wait = starter.treat_wait(spec.name)
        if (datetime.now() + timedelta(seconds=wait)).date() != day:
            reports.append(starter.Report("buddy treats", False, f"{before} hearts; next feeding is tomorrow"))
            break
        if wait > 0:
            print(f"[ender] {spec.name}: {before} treat hearts; next feeding in {wait/60:.1f} minutes", flush=True)
            await asyncio.sleep(wait + 1)
        result = await action("buddy treats", starter.with_phone(spec, args, buddy))
        if result is None:
            break
        after = await action("buddy hearts", starter.with_phone(spec, args, heart_counts))
        if after is None or after[0] is None or after[0] <= before:
            reports.append(starter.Report("buddy treats", False, f"could not verify another heart after {before}"))
            break
        if after[0] >= starter.TREAT_HEARTS_MAX:
            break
    for report in reports:
        print(f"[ender] {spec.name}: {report.step}: {report.detail}", flush=True)
    return all(report.ok for report in reports)


def prepare_handsets():
    from . import fleet_entrypoint, pokemon_fleet, config_paths, remote_target, trading_accounts
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    owners, _ = remote_target.discover_attached_devices(fleet, fleet_entrypoint.machine_configs())
    return list(trading_accounts.transports(owners, trading_accounts.aliases()).values())


async def prepare_local(selected=None):
    from . import trading_accounts
    args = starter.parse_args(["--only", "buddy", "battle", "gifts"])
    here = await clear_pokemon.attached_devices(argparse.Namespace(devices=None, exclude_devices=[]))
    if selected is None:
        selected = trading_accounts.transports([spec.name for spec in here], trading_accounts.aliases()).values()
    selected = set(selected)
    here = [spec for spec in here if spec.name in selected]
    if not here:
        print("[ender] no participating phones attached to this host", flush=True)
        return 0
    today = date.today()
    results = await asyncio.gather(*(prepare_phone(spec, args, today) for spec in here))
    return int(not all(results))
