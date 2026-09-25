# Workflow guide

Run these commands from an installed checkout. The examples use public aliases and bounded counts. Use your own private fleet file to select real devices. `--help` is the authoritative option list for the installed version.

## Whatever is on screen — `play.py`

Park each phone on the screen of the job you want, then run one command. Every configured computer looks at its attached phones (screenshots only) and starts:

- `gbl_day.py` on the GO Battle League card, the league page or the party picker;
- `scripts/berry.py` on a gym defender's berry-feeding card -- open a defender first, the gym overview itself is left alone;
- `gift.py --all --gifts-only` on the friends list or a friend's profile;
- `catch.py` on an award card, a research CLAIM button or an open wild encounter.

A phone on any other screen, a trade's Pokémon picker included, is named and left alone; a phone another command already holds is skipped. Phones on the same screen share one command, and different jobs run side by side. `--plan` reads and reports without starting anything; `--spend N` caps berries; `--local` keeps it to this computer.

```sh
python play.py --plan
python play.py --devices razr iphone-second
```

## Gifts and gym berries — `gift.py`

Use an explicit `--count` for a supervised run. If neither `--count` nor `--all` is supplied, the public entry point chooses `--all`, capped at 100 cycles per phone by `--max-cycles`.

**Screen routing:** before anything is tapped, each phone's screen is read. A phone parked on a gym's feeding screen (a defender open with its berry button showing) feeds the defenders instead, exactly as `scripts/berry.py` would, under `--spend`/`--spend-device` budgets; every other phone runs the gift loop. An open catch encounter passes the feeding-screen test on its own, so a ball in hand sends the phone to the gifts instead. On an iPhone the berry worker connects first and steps aside, touching nothing, when the phone is not on a feeding screen. `--gifts-only` skips the look and gifts from every phone.

**Purpose:** open and send gifts while moving through the Friends interface. The platform workers recognize expected states, handle picker transitions, and stop or recover when the visible state differs.

**Prepare:** connect and unlock the phone, open the Friends interface, and verify the appropriate gift profile/calibration. Gift availability and daily game limits still apply.

**Row selection:** before opening the next friend, both platform workers read the friends list from a screenshot. A friend pinned to the top by a remote trade awaiting a response is stepped over — that row has no gift to open and cannot receive one, so tapping it repeats forever. Of the remaining rows, a gift is only opened when the friend's row is drawn without the pale blue halo around their avatar; sending is unaffected and continues for every friend who can receive a gift, for as long as the gift bag holds any. A gift left unopened is closed first, because tapping the friend's row opens the gift full screen and the send buttons are on the profile behind it. Set `OPEN_FROM_HALOED_ROWS` in `sources/gift_android.py` or `sources/gift_ios.py` to `True` to open from the haloed rows instead. An unreadable screen, a part-scrolled list, or `--no-guard` falls back to tapping the configured `NEXT_FRIEND_BTN` point and opening whatever it finds.

```sh
python gift.py --devices all --count 1 --plan
python gift.py --devices all --count 1
python gift.py --devices all --count 1 --spend 10   # a phone on a gym feeds at most 10 berries
```

Implementation: `sources/gift_android.py` and `sources/gift_ios.py`, selected by the fleet controller; berries as below. Compatibility name: `scripts/send_gifts.py`.

## Add friends — `add_friends.py`

**Purpose:** submit trainer codes using Android or iOS screen interaction, track attempts, and avoid repeating codes already recorded in the chosen history.

**Prepare:** keep trainer codes and history in private files. Start with one code and a visible Friends/Add Friend interface. An accepted input or attempted request is not proof that a friendship was accepted by another trainer.

```sh
python add_friends.py --devices all --count 1 --codes-file "$POGO_CONFIG_DIR/trainer-codes.txt" --plan
python add_friends.py --devices all --count 1 --codes-file "$POGO_CONFIG_DIR/trainer-codes.txt"
```

Set `POGO_CONFIG_DIR` in your shell before using the path above. You can also pass `--codes-file trainer-codes.txt`; the relative path resolves under each selected computer's own private configuration directory. Absolute paths inside the coordinator's configuration directory are rebased for remote workers, but the code-list files must already exist there. Absolute paths outside that directory are forwarded unchanged; use `--local` for a file available only on this computer. No file synchronization is performed. Individual codes and a public source are also supported; consult `--help` and review the intended code list.

Implementation: the controllers and request loop in `add_friends.py`, using shared fleet selection and platform connections.

## GO Battle League — `gbl.py`

The default is five battles per selected phone. Set `--count 1` for the initial supervised run; `gbl_day.py` is a separate longer-running workflow.

**Purpose:** coordinate league entry, party selection, combat input, result screens, and rewards. Each selected phone participates in its own matchmaking battle. This differs from the paired friendly battle command.

**Prepare:** verify the league/team configuration and enter the expected GBL screen. Screen recognition, reward state, the available cup list, and game daily limits all affect progress.

On Android, season information articles are recognized before league cards. The worker closes them with Android Back and reads the screen again; league names in the article are never treated as selectable cards. Recovery is bounded if the article will not close.

```sh
python gbl.py --devices all --count 1 --plan
python gbl.py --devices all --check
python gbl.py --devices all --count 1
```

Implementation: `sources/gbl_android.py`, `sources/gbl_ios.py`, and shared vision, strategy, metadata, and home-recovery modules. `gbl_day.py` coordinates longer daily runs; begin with the bounded command above. Compatibility name: `gbl.py`.

## Trades — `trade.py`

The default count is one trade. An explicit pair runs on its owning computer; both devices must belong to that same computer.

**Purpose:** execute the repeated trade confirmation sequence for a supported pair attached to the same computer.

**Prepare:** identify the intended pair, complete one manual trade, and prepare the selection/search that should be reused. Check what both phones are offering before starting. Trade cost and game restrictions remain in effect.

```sh
python trade.py --pair android-one android-two --count 1 --plan
python trade.py --pair android-one android-two --count 1
```

Implementation: the paired controllers and trade loop in `sources/pokemon_fleet.py` support Android/Android, iOS/iOS, and mixed pairs when both devices are configured and calibrated. This is implementation support, not evidence that every pair type has been verified on live phones. The older `sources/trade_android.py` and `sources/trade_ios_android.py` workers remain in the codebase. Compatibility name: `trade.py`.

## Transfers — `luckytrash.py`

Select either `--count N` or `--all` explicitly. `--all` has a default limit of 500 transfers per phone through `--max-transfers`. Execution also requires `--confirm-delete`; a plan does not authorize or perform a transfer.

**Purpose:** transfer selected Pokémon using the existing LuckyTrash workers. Transfers remove Pokémon from the account; the program cannot undo that action.

**Prepare:** manually review the search/tag/selection, protected Pokémon, and transfer limit. Start with a plan and inspect the actual phone. The execution command below includes the required deletion confirmation and should only be used after that review.

```sh
python luckytrash.py --devices all --count 1 --plan
python luckytrash.py --devices all --count 1 --confirm-delete
```

Implementation: `sources/luckytrash_android.py` and `sources/luckytrash_ios.py`. Compatibility name: `luckytrash.py`. A new alias does not change the underlying selection or protection rules.

## Gym berries only — `scripts/berry.py`

`gift.py` already feeds any phone parked on a gym. This berries-only command is for `--check` (a read-only detector check) and for a run that must not fall back to gifts. Prepare the gym/defender interface and review the berry budget. Implementations are `sources/berry_android.py` and `sources/berry_ios.py`; compatibility name `scripts/feed_berries.py`.

```sh
python scripts/berry.py --devices all --spend 1 --plan
python scripts/berry.py --devices all --check
```

## Catching rewards — `catch.py`

Works whichever reward queue each phone is parked on, on Android and iPhone:

- the award card ("A Pokémon has appeared! You have N Pokémon left to catch"): START ENCOUNTER, feed a Nanab, throw, repeat until the queue empties;
- a research screen's orange CLAIM buttons: press each one and work what it pays out -- items are dismissed, Pokémon are thrown at.

An encounter already open is caught first, then the screen is read again. A screen showing none of these is left alone. `--mode award` or `--mode research` skips the read (research looks further down a list than the read does); `--dry-run` reports what it sees without touching the phone. On an iPhone the Excellent routine throws until it first refuses to lock the circle, then a plain flick for the rest of the run; there is no BACK key, so a screen that only BACK would leave is reported and left alone. `--mode encounter` catches wild Pokémon you open by hand, one after another, with the same throws and Nanab. It runs on this computer unless `--allmachines` is given.

```sh
python catch.py --dry-run
python catch.py --devices iphone-second --throws 30
python catch.py --mode encounter --count 5
```

## Additional tools

- `scripts/fleet.py status`: read-only connectivity and configuration status.
- `scripts/fleet.py owners`: read-only survey of every configured computer's USB buses, showing which one each phone is attached to, flagging any whose `machine` field disagrees, and naming a host whose probe did not answer. Run it after moving a cable; routing already follows what it reports.
- `scripts/battle.py`: paired friendly battles, with its own supported platforms and visual calibration.
- `scripts/excellent_throw.py`: the raw excellent-throw watcher, kept for its `--check` and `--probe-ring` calibration; day-to-day catching is `catch.py --mode encounter`.
- `remove_friends.py`: removes Android friends who have sent no gift, carry no blue halo and have at most `--hearts` hearts (default 0, up to 3). Run it with the phone on the FRIENDS list sorted by friendship level, lowest first. Without `--confirm-remove` it only lists who would go; with it, each removal is checked against the name in the unfriend dialog, and the removed names are written to the private state directory.
- `scripts/gbl_evaluator.py`: inspect GBL strategy/evaluation inputs and outputs.
- `scripts/inventory_scanner_ios.py`: inspect iOS inventory; store account-derived output privately.
- `recover_home.py`: recovery utility where provided; review `--help` before allowing screen changes.

A workflow plan uses configured devices and does not probe attached phones. At execution time, `--devices all` can additionally discover unregistered Android phones. Run live fleet status to inspect attached hardware, or name devices explicitly when the run should be limited to a particular set.

## Stop and verify

Use Ctrl+C to interrupt a supervised run. Check every participating computer for leftover workers and confirm the phones stopped receiving input. Review the visible outcome separately from the command's exit status. Read [Testing](testing.md) before claiming an action completed.
