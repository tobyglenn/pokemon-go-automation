# Workflow guide

Run these commands from an installed checkout. The examples use public aliases and bounded counts. Use your own private fleet file to select real devices. `--help` is the authoritative option list for the installed version.

## Gifts — `send_gifts.py`

Use an explicit `--count` for a supervised run. If neither `--count` nor `--all` is supplied, the public entry point chooses `--all`, capped at 100 cycles per phone by `--max-cycles`.

**Purpose:** open and send gifts while moving through the Friends interface. The platform workers recognize expected states, handle picker transitions, and stop or recover when the visible state differs.

**Prepare:** connect and unlock the phone, open the Friends interface, and verify the appropriate gift profile/calibration. Gift availability and daily game limits still apply.

```sh
python send_gifts.py --devices all --count 1 --plan
python send_gifts.py --devices all --count 1
```

Implementation: `sources/gift_android.py` and `sources/gift_ios.py`, selected by the fleet controller. Compatibility name: `gift.py`.

## Add friends — `add_friends.py`

**Purpose:** submit trainer codes using Android or iOS screen interaction, track attempts, and avoid repeating codes already recorded in the chosen history.

**Prepare:** keep trainer codes and history in private files. Start with one code and a visible Friends/Add Friend interface. An accepted input or attempted request is not proof that a friendship was accepted by another trainer.

```sh
python add_friends.py --devices all --count 1 --codes-file "$POGO_CONFIG_DIR/trainer-codes.txt" --plan
python add_friends.py --devices all --count 1 --codes-file "$POGO_CONFIG_DIR/trainer-codes.txt"
```

Set `POGO_CONFIG_DIR` in your shell before using the path above. You can also pass `--codes-file trainer-codes.txt`; the relative path resolves under each selected computer's own private configuration directory. Absolute paths inside the coordinator's configuration directory are rebased for remote workers, but the code-list files must already exist there. Absolute paths outside that directory are forwarded unchanged; use `--local` for a file available only on this computer. No file synchronization is performed. Individual codes and a public source are also supported; consult `--help` and review the intended code list.

Implementation: the controllers and request loop in `add_friends.py`, using shared fleet selection and platform connections.

## GO Battle League — `battle_league.py`

The default is five battles per selected phone. Set `--count 1` for the initial supervised run; `gbl_day.py` is a separate longer-running workflow.

**Purpose:** coordinate league entry, party selection, combat input, result screens, and rewards. Each selected phone participates in its own matchmaking battle. This differs from the paired friendly battle command.

**Prepare:** verify the league/team configuration and enter the expected GBL screen. Screen recognition, reward state, the available cup list, and game daily limits all affect progress.

```sh
python battle_league.py --devices all --count 1 --plan
python battle_league.py --devices all --check
python battle_league.py --devices all --count 1
```

Implementation: `sources/gbl_android.py`, `sources/gbl_ios.py`, and shared vision, strategy, metadata, and home-recovery modules. `gbl_day.py` coordinates longer daily runs; begin with the bounded command above. Compatibility name: `gbl.py`.

## Trades — `trade_pokemon.py`

The default count is one trade. An explicit pair runs on its owning computer; both devices must belong to that same computer.

**Purpose:** execute the repeated trade confirmation sequence for a supported pair attached to the same computer.

**Prepare:** identify the intended pair, complete one manual trade, and prepare the selection/search that should be reused. Check what both phones are offering before starting. Trade cost and game restrictions remain in effect.

```sh
python trade_pokemon.py --pair android-one android-two --count 1 --plan
python trade_pokemon.py --pair android-one android-two --count 1
```

Implementation: the paired controllers and trade loop in `sources/pokemon_fleet.py` support Android/Android, iOS/iOS, and mixed pairs when both devices are configured and calibrated. This is implementation support, not evidence that every pair type has been verified on live phones. The older `sources/trade_android.py` and `sources/trade_ios_android.py` workers remain in the codebase. Compatibility name: `trade.py`.

## Transfers — `transfer_pokemon.py`

Select either `--count N` or `--all` explicitly. `--all` has a default limit of 500 transfers per phone through `--max-transfers`. Execution also requires `--confirm-delete`; a plan does not authorize or perform a transfer.

**Purpose:** transfer selected Pokémon using the existing LuckyTrash workers. Transfers remove Pokémon from the account; the program cannot undo that action.

**Prepare:** manually review the search/tag/selection, protected Pokémon, and transfer limit. Start with a plan and inspect the actual phone. The execution command below includes the required deletion confirmation and should only be used after that review.

```sh
python transfer_pokemon.py --devices all --count 1 --plan
python transfer_pokemon.py --devices all --count 1 --confirm-delete
```

Implementation: `sources/luckytrash_android.py` and `sources/luckytrash_ios.py`. Compatibility name: `luckytrash.py`. A new alias does not change the underlying selection or protection rules.

## Gym berries — `feed_berries.py`

Prepare the gym/defender interface and review the berry budget. Implementations are `sources/berry_android.py` and `sources/berry_ios.py`; compatibility name `berry.py`.

```sh
python feed_berries.py --devices all --spend 1 --plan
python feed_berries.py --devices all --spend 1
```

## Additional tools

- `fleet.py status`: read-only connectivity and configuration status.
- `battle.py`: paired friendly battles, with its own supported platforms and visual calibration.
- `excellent_throw.py`: catch/throw assistance; review its platform options and calibration before use.
- `gbl_evaluator.py`: inspect GBL strategy/evaluation inputs and outputs.
- `inventory_scanner_ios.py`: inspect iOS inventory; store account-derived output privately.
- `recover_home.py`: recovery utility where provided; review `--help` before allowing screen changes.

A workflow plan uses configured devices and does not probe attached phones. At execution time, `--devices all` can additionally discover unregistered Android phones. Run live fleet status to inspect attached hardware, or name devices explicitly when the run should be limited to a particular set.

## Stop and verify

Use Ctrl+C to interrupt a supervised run. Check every participating computer for leftover workers and confirm the phones stopped receiving input. Review the visible outcome separately from the command's exit status. Read [Testing](testing.md) before claiming an action completed.
