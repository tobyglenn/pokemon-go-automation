"""Finish a fleet trading run with audited inventory cleanup and verified screen sleep."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
import sys
from pathlib import Path

from . import clear_pokemon, config_paths, display_sleep, fleet_entrypoint, pokemon_fleet


async def worker(name, payload, plan):
    from . import trading_accounts
    fleet_entrypoint.apply_local_environment()
    fleet = pokemon_fleet.load_fleet(config_paths.default_config("pokemon-fleet.yaml"))
    args = clear_pokemon.parse_args(["--devices", name, "--open", "--keep-rare"])
    here = await clear_pokemon.attached_devices(args)
    if len(here) != 1 or here[0].name != name:
        raise RuntimeError(f"{name}: no longer connected on the assigned host")
    # A legacy pending-only notification instructs this worker to sleep its
    # assigned phone and carries no inventory query to attribute.
    sleep_only = payload.get("pending") and not payload.get("term") and "device" not in payload and "account" not in payload
    if not sleep_only and (payload.get("device") != name or
                           payload.get("account", name) != trading_accounts.account_for(name)):
        raise RuntimeError(f"{name}: review query belongs to a different handset or account")
    spec = fleet.devices[name]
    if plan:
        if payload.get("error"):
            print(f"[night] {name}: {payload['error']}", flush=True)
            return 1
        print(f"[night] {name}: display on={await asyncio.to_thread(display_sleep.screen_on, spec)}; "
              f"open {payload['label']} ({len(payload['term'])} search characters) then switch the display off", flush=True)
        return 0
    complete = False
    locked_from_review = False
    async def leave_review(phone, image):
        nonlocal locked_from_review
        print(f"[night] {name}: review list {phone.save(image, 'storage-review')}", flush=True)
        await display_sleep.lock_display(spec, phone.target)
        locked_from_review = True

    try:
        if payload["pending"]:
            raise RuntimeError("unresolved trade receipt; preserving inventory")
        if payload.get("error"):
            raise RuntimeError(payload["error"])
        term = payload.get("term")
        if not isinstance(term, str) or not term.strip():
            raise RuntimeError("missing strict manual review query")
        if payload.get("favorite_only"):
            from . import starter, trade_saved_search
            async def open_saved(phone):
                grid, boxes = await trade_saved_search.open_named_storage(phone, payload["label"], term)
                count = clear_pokemon.result_count(boxes, grid.height)
                if count is None or not clear_pokemon.evolutionary_line_off(grid, boxes):
                    raise RuntimeError("could not verify saved review count and exact-species mode")
                await leave_review(phone, grid)
                return count
            count = await starter.with_phone(spec, args, open_saved)
            complete, result = True, f"{count} in saved review"
        else:
            complete, result = await clear_pokemon.run_device(spec, term, (), args, on_review=leave_review)
        print(f"[night] {name}: {payload['label']}: {result}", flush=True)
    except Exception as exc:
        print(f"[night] {name}: cleanup incomplete: {exc}", flush=True)
    if locked_from_review:
        await display_sleep.wait_until_off(spec)
    else:
        await display_sleep.sleep_phone(spec)
    print(f"[night] {name}: verified screen off", flush=True)
    return int(not complete)


async def finish(owners, machines, roster, clear_path, cfg, receiver, *, plan=False, transports=None, devices=None,
                 prepared_only=False, pinned=False):
    """Open each account's strict review list, then sleep its display.

    `pinned` opens the handset's saved favorite for the live policy term instead
    of typing it; the favorite must still match that term.
    """
    from . import storage_review, trade_history, trading_accounts

    if cfg.get("receiver") != receiver:
        raise ValueError("review policy receiver differs from the active trade receiver")
    try:
        if prepared_only:
            from . import ender_inventory
            snapshot = ender_inventory.load()
            policy = None
        else:
            policy = storage_review.load_policy(clear_path, cfg)
        policy_error = None
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        policy = None
        policy_error = str(exc)
    local = fleet_entrypoint.local_machine(machines)
    mapping = trading_accounts.aliases()
    selected = trading_accounts.transports(owners, mapping)
    hosts = {account: owners[phone] for account, phone in selected.items()}
    selected.update(transports or {})
    accounts = dict.fromkeys(mapping.get(n, n) for n in roster)

    ios_turn = asyncio.Lock()

    async def finish_one(name):
        physical = selected[name]
        payload = {"device": physical, "account": name, "label": storage_review.STRICT, "pending": False}
        try:
            await asyncio.to_thread(trade_history.PendingConfirmation([name], routes=(local, machines)).check)
            if policy_error is not None:
                raise RuntimeError(policy_error)
            if prepared_only:
                payload["favorite_only"] = True
                payload["term"] = ender_inventory.account_favorites(snapshot, name, physical)[storage_review.STRICT]["term"]
            else:
                payload["term"] = policy.favorites(name)[storage_review.STRICT]
                payload["favorite_only"] = pinned
        except trade_history.ConfirmationStateError as exc:
            payload["pending"] = True
            payload["error"] = str(exc)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            payload["error"] = f"strict review unavailable: {exc}"
        machine = hosts[name]
        cmd = ["-u", "-m", "sources.collector_night", "--probe" if plan else "--finish", physical]
        command = ([sys.executable, *cmd] if machine == local else
                   fleet_entrypoint.remote_shell_command(machine, machines[machine], cmd))
        # Each remote worker has its own local lock. Coordinate the iPhone
        # reviews here as Apple Handoff can copy text between the two hosts.
        spec = (devices or {}).get(physical)
        async with ios_turn if not plan and getattr(spec, "platform", None) == "ios" else nullcontext():
            process = await asyncio.create_subprocess_exec(*command, cwd=Path(__file__).resolve().parents[1],
                                                           stdin=asyncio.subprocess.PIPE)
            await process.communicate(json.dumps(payload).encode())
            return process.returncode

    results = await asyncio.gather(*(finish_one(n) for n in accounts if n in hosts), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            print(f"[night] {result}", flush=True)
    return int(any(result != 0 for result in results))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("--probe", "--finish"):
        raise SystemExit("Use collect_trades.py --finish-night")
    raise SystemExit(asyncio.run(worker(sys.argv[2], json.load(sys.stdin), sys.argv[1] == "--probe")))
